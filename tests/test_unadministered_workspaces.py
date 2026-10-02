"""Which workspaces nobody can administer, and which a departure would leave so - `SR#4154`.

**Decided on `#3950`**: deactivating a workspace's last administrator who can act is allowed and
never silent, as a private project's last member's departure is (`#1453`). The server answers it,
counted as authentication decides who can act, so these drive that answer over HTTP on both
backends: the only administrator, the only administrator of two workspaces, a superuser leaving
themselves, the person an administering agent answers to, and who may ask at all.
"""

import typing
import uuid

import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.domain.accountability
import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_tasks


def _asked (
	world: test_api_tasks.World, *, leaving: str | None = None, expect: int = 200
) -> list[str]:
	"""Ask the server, and return the short names it gives."""

	query = {} if leaving is None else {"leaving": leaving}
	answer = world.call("GET", "/v1/instance/unadministered-workspaces", params=query)

	assert answer.status_code == expect, answer.text

	if expect != 200:
		return []

	return [one["slug"] for one in answer.json()["items"]]


def _person (session: sqlalchemy.orm.Session, name: str) -> subroutine.db.models.identity.User:
	"""Make an account with a name nobody else in the run has."""

	return subroutine.domain.users.create(session, username=f"{name}-{uuid.uuid4().hex[:6]}")


def _founded (
	session: sqlalchemy.orm.Session, slug: str, owner: subroutine.db.models.identity.User
) -> subroutine.db.models.identity.Workspace:
	"""Make a workspace whose only administrator is ``owner``."""

	return subroutine.domain.workspaces.create(
		session, slug=f"{slug}-{uuid.uuid4().hex[:6]}", title=slug.title(), owner=owner
	)


def test_the_only_administrator_leaving_is_named_before_and_after (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Named while he is still here, when asked with ``leaving``, and once he has gone, without."""

	world = test_api_tasks._world(session)
	thomas = _person(session, "thomas")
	zion = _founded(session, "zion", thomas)
	session.flush()

	assert _asked(world, leaving=thomas.username) == [zion.slug]
	assert _asked(world) == [], "nothing is left so before anybody has gone"

	subroutine.domain.users.set_active(session, thomas, active=False)
	session.flush()

	assert _asked(world) == [zion.slug]
	assert _asked(world, leaving=thomas.username) == [], (
		"a workspace already left so is not his departure's to name"
	)


def test_the_only_administrator_of_two_workspaces_names_both (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Every workspace, not the first; and one with a second administrator is not named."""

	world = test_api_tasks._world(session)
	thomas = _person(session, "thomas")
	trinity = _person(session, "trinity")
	zion = _founded(session, "zion", thomas)
	hammer = _founded(session, "hammer", thomas)
	shared = _founded(session, "shared", thomas)
	subroutine.domain.workspaces.add_member(session, shared, trinity, role_key="admin")
	session.flush()

	assert sorted(_asked(world, leaving=thomas.username)) == sorted([zion.slug, hammer.slug])


def test_a_superuser_leaving_names_the_workspace_only_they_administer (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The operator asking about themselves: the workspace ``init`` made them the owner of."""

	world = test_api_tasks._world(session)

	assert _asked(world, leaving=world.user.username) == [world.workspace.slug]


def test_the_person_an_administering_agent_answers_to_is_caught (
	session: sqlalchemy.orm.Session,
) -> None:
	"""An agent whose person leaves cannot act, so a workspace only it administers is named."""

	world = test_api_tasks._world(session)
	thomas = _person(session, "thomas")
	agent = subroutine.domain.users.create(
		session,
		username=f"sentinel-{uuid.uuid4().hex[:6]}",
		is_service_account=True,
		actor=subroutine.domain.authentication.Principal(user=world.user, token=None),
	)
	agent.responsible_user_id = thomas.id
	zion = _founded(session, "zion", agent)
	session.flush()

	assert subroutine.domain.accountability.chain(session, agent)[-1] is thomas
	assert _asked(world, leaving=thomas.username) == [zion.slug]


def test_only_an_administrator_of_the_installation_may_ask (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`instance:admin`, which no role carries, on a credential not pinned to one workspace."""

	world = test_api_tasks._world(session)
	member = _person(session, "mouse")
	subroutine.domain.workspaces.add_member(session, world.workspace, member, role_key="admin")
	_row, issued = subroutine.domain.authentication.issue_token(session, user=member, title="mine")
	_row, pinned = subroutine.domain.authentication.issue_token(
		session, user=world.user, title="pinned", workspace_id=world.workspace.id
	)
	session.flush()

	refused: typing.Any = [
		world._replace(secret=issued.value.get_secret_value()),
		world._replace(secret=pinned.value.get_secret_value()),
	]

	for caller in refused:
		_asked(caller, expect=403)
