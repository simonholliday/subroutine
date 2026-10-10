"""A saved view says what its reader may do with it, asked of each act's own check - `SR#4577`.

Decision `#4532`, on F P6 and F R3 of the cold review of 2026-10-05: the browser re-derived who may
forget a view - *its owner, or an administrator for a shared one* - and missed ``task:write``, so a
viewer demoted after saving a view was offered a *Forget* the instance refused. **Each view carries
``may``**, from the function each act asks, so what is offered and what is allowed cannot disagree.
"""

import itertools
import typing
import uuid

import pytest
import sqlalchemy.orm

import api_support
import subroutine.domain.authentication
import subroutine.domain.saved
import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_tasks

#: How each act is tried over HTTP, as the method and the body.
TRIED: dict[str, tuple[str, dict[str, typing.Any] | None]] = {
	"change": ("PATCH", {"q": "urgency>=3"}),
	"share": ("PATCH", {"shared": True}),
	"unshare": ("PATCH", {"shared": False}),
	"forget": ("DELETE", None),
}


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def _member (world: test_api_tasks.World, name: str, role: str) -> typing.Any:
	"""Make somebody a member with this role, and return the account."""

	person = subroutine.domain.users.create(world.session, username=f"{name}-{uuid.uuid4().hex[:6]}")
	subroutine.domain.workspaces.add_member(world.session, world.workspace, person, role_key=role)
	world.session.flush()

	return person


def _secret (world: test_api_tasks.World, person: typing.Any, **narrowed: typing.Any) -> str:
	"""Issue somebody a credential, narrowed as asked, and return its secret."""

	_row, issued = subroutine.domain.authentication.issue_token(
		world.session, user=person, title=f"{person.username} {uuid.uuid4().hex[:4]}", **narrowed
	)
	world.session.flush()

	return str(issued.value.get_secret_value())


def _as (world: test_api_tasks.World, secret: str, method: str, path: str, **kwargs: typing.Any) -> typing.Any:
	"""Make a request with this credential."""

	return api_support.call(
		world.application, method, path, headers={"authorization": f"Bearer {secret}"}, **kwargs
	)


def _saved (world: test_api_tasks.World, owner: typing.Any, *, shared: bool) -> str:
	"""Save a view as ``owner``, shared or not, and return its key."""

	row = subroutine.domain.saved.create(
		world.session,
		workspace_id=world.workspace.id,
		title=f"Queue {uuid.uuid4().hex[:6]}",
		arrangement="list",
		q="type:bug",
		shared=shared,
		actor=subroutine.domain.authentication.Principal(user=owner),
	)
	world.session.flush()

	return str(row.key)


def test_each_reader_is_told_what_they_may_do_with_a_view (world: test_api_tasks.World) -> None:
	"""Its author changes it; whoever manages it shares, unshares and forgets it, as decided."""

	trinity = _member(world, "trinity", "member")
	keanu = _member(world, "keanu", "member")
	own = _saved(world, trinity, shared=False)
	team = _saved(world, trinity, shared=True)
	inbox = world.call("GET", "/v1/projects/inbox").json()["id"]

	def may (secret: str, key: str) -> list[str] | None:
		"""Return what a reader is told they may do with a view, or ``None`` where they cannot see it."""

		read = _as(world, secret, "GET", f"/v1/views/{key}")

		return None if read.status_code == 404 else list(read.json()["may"])

	author = _secret(world, trinity)
	narrowed = _secret(world, trinity, project_write_scope=[inbox])

	assert may(author, own) == ["change", "share", "forget"]
	assert may(author, team) == ["change", "unshare", "forget"]

	# **An administrator manages a shared view and does not rewrite it**, and sees no private one.
	assert may(world.secret, team) == ["unshare", "forget"]
	assert may(world.secret, own) is None
	assert may(_secret(world, keanu), team) == []

	# **A narrowed credential keeps its own, and nothing in front of the workspace** (`#4558`).
	assert may(narrowed, own) == ["change", "forget"]
	assert may(narrowed, team) == []

	# **The case the browser missed** (F R3): demoted to viewer after saving it.
	subroutine.domain.workspaces.set_member_role(world.session, world.workspace, trinity, role_key="viewer")
	world.session.flush()

	assert may(author, own) == [], "a demoted viewer is offered what they may no longer do"


def test_what_a_view_offers_is_exactly_what_the_instance_allows (world: test_api_tasks.World) -> None:
	"""**The property, not a list**: every act a reader is offered goes through, and every other is refused.

	Each act is tried on a view saved for it alone, so one that went through cannot decide the next.
	"""

	trinity = _member(world, "trinity", "member")
	keanu = _member(world, "keanu", "member")
	inbox = world.call("GET", "/v1/projects/inbox").json()["id"]
	readers = {
		"its author": _secret(world, trinity),
		"an administrator": world.secret,
		"another member": _secret(world, keanu),
		"its author, narrowed": _secret(world, trinity, project_write_scope=[inbox]),
	}
	tried = 0

	for (who, secret), shared, act in itertools.product(readers.items(), (False, True), TRIED):
		key = _saved(world, trinity, shared=shared)
		read = _as(world, secret, "GET", f"/v1/views/{key}")

		if read.status_code == 404:
			continue

		method, body = TRIED[act]
		answer = _as(
			world, secret, method, f"/v1/views/{key}", **({} if body is None else {"json": body})
		)
		applies = not (act == "share" and shared) and not (act == "unshare" and not shared)

		if not applies:
			assert act not in read.json()["may"], (who, shared, act)
			continue

		tried += 1
		offered = act in read.json()["may"]

		assert (answer.status_code < 300) == offered, (who, shared, act, read.json()["may"], answer.text)

	# Three acts apply to each view, for four readers and two views, less the private view's six for
	# the two who cannot see it.
	assert tried == 18, f"{tried} acts were tried where 18 apply, so the scene is not what it says"
