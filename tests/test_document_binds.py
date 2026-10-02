"""A document binds its project or the whole workspace - `SR#4133`, decision `#4134`.

**Marking takes what sharing a view takes** (`SR#3151`): ``project:write`` on a credential not
narrowed to some projects - for writing a document marked, for marking one, for setting it back,
and for superseding a marked one, since a successor carries the mark. Every other edit to a
marked document takes what it takes today.
"""

import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def _member (
	world: test_api_tasks.World, who: str, role: str
) -> tuple[subroutine.db.models.identity.User, str]:
	"""Make somebody a member of the workspace with this role, and return them and their credential."""

	person = subroutine.domain.users.create(world.session, username=who)
	subroutine.domain.workspaces.add_member(world.session, world.workspace, person, role_key=role)
	world.session.flush()

	return person, _credential(world, person)


def _credential (
	world: test_api_tasks.World, person: subroutine.db.models.identity.User, **narrowed: typing.Any
) -> str:
	"""Issue somebody a credential, narrowed as asked, and return its secret."""

	_row, issued = subroutine.domain.authentication.issue_token(
		world.session, user=person, title=f"{person.username}'s token", **narrowed
	)
	world.session.flush()

	return str(issued.value.get_secret_value())


def _as (
	world: test_api_tasks.World, secret: str, method: str, path: str, **kwargs: typing.Any
) -> typing.Any:
	"""Make a request with somebody else's credential."""

	return api_support.call(
		world.application, method, path, headers={"authorization": f"Bearer {secret}"}, **kwargs
	)


def _written (world: test_api_tasks.World, secret: str | None = None, **body: typing.Any) -> typing.Any:
	"""Write a decision, as the world's own account or with a credential, and return it."""

	body.setdefault("type", "decision")
	made = (
		world.call("POST", "/v1/documents", json=body)
		if secret is None
		else _as(world, secret, "POST", "/v1/documents", json=body)
	)

	assert made.status_code == 201, made.text

	return made.json()


def test_a_document_binds_its_project_until_it_is_marked (world: test_api_tasks.World) -> None:
	"""Every document starts binding its project, and the listing can be asked for the rest."""

	plain = _written(world, title="Run the deploy checklist before every release")
	marked = _written(world, title="Every agent reads this before its first write", binds="workspace")

	assert (plain["binds"], marked["binds"]) == ("project", "workspace")

	asked = world.call("GET", "/v1/documents", params={"binds.eq": "workspace"}).json()["items"]
	others = world.call("GET", "/v1/documents", params={"binds.ne": "workspace"}).json()["items"]

	assert [row["ref"] for row in asked] == [marked["ref"]], asked
	assert plain["ref"] in [row["ref"] for row in others], others
	assert marked["ref"] not in [row["ref"] for row in others], others


def test_a_word_a_document_cannot_bind_is_refused_naming_the_two_it_can (
	world: test_api_tasks.World,
) -> None:
	"""Refused on the write and on the listing, each naming ``project`` and ``workspace``."""

	written = world.call("POST", "/v1/documents", json={"title": "A rule", "binds": "everyone"})

	assert written.status_code == 422, written.text
	assert written.json()["errors"][0]["field"] == "binds", written.text
	assert "project, workspace" in written.text, written.text

	asked = world.call("GET", "/v1/documents", params={"binds.eq": "everyone"})

	assert asked.status_code == 422, asked.text
	assert "project, workspace" in asked.text, asked.text


def test_marking_takes_project_write_on_a_credential_reaching_the_whole_workspace (
	world: test_api_tasks.World,
) -> None:
	"""Decision `#4134`: a contributor, and a member's narrowed credential, cannot change the mark.

	**Both directions and the write**, since setting a mark back retires a rule for everybody. And a
	contributor still edits a marked document's text, because every other edit takes what it took.
	"""

	_carrieanne, contributor = _member(world, "carrieanne", "contributor")
	keanu, member = _member(world, "keanu", "member")
	inbox = world.call("GET", "/v1/projects/inbox").json()["id"]
	reading = _credential(world, keanu, project_scope=[inbox])
	writing = _credential(world, keanu, project_write_scope=[inbox])

	before = len(world.call("GET", "/v1/documents").json()["items"])
	refused = _as(
		world, contributor, "POST", "/v1/documents", json={"title": "A rule", "binds": "workspace"}
	)

	assert refused.status_code == 403, refused.text
	assert len(world.call("GET", "/v1/documents").json()["items"]) == before, "it was written anyway"

	rule = _written(world, contributor, title="Name the project on every capture")
	path = f"/v1/documents/{rule['ref']}"

	assert _as(world, contributor, "PATCH", path, json={"binds": "workspace"}).status_code == 403
	assert _as(world, reading, "PATCH", path, json={"binds": "workspace"}).status_code == 403

	marked = _as(world, member, "PATCH", path, json={"binds": "workspace"})

	assert marked.status_code == 200, marked.text
	assert marked.json()["binds"] == "workspace"

	for narrow in (reading, writing):
		back = _as(world, narrow, "PATCH", path, json={"binds": "project"})

		assert back.status_code == 403, back.text
		assert "cannot set a document that binds the whole workspace back" in back.text, back.text

	revised = _as(world, contributor, "PATCH", path, json={"body": "Name it with +key."})

	assert revised.status_code == 200, revised.text
	assert revised.json()["binds"] == "workspace", "an edit to the text moved the mark"


def test_a_successor_carries_the_mark_and_superseding_takes_what_marking_takes (
	world: test_api_tasks.World,
) -> None:
	"""Decision `#4134`: a rule does not stop binding by being revised, nor by being replaced.

	**The link is refused before it is written**, so a contributor's attempt leaves nothing, and a
	member's marks the successor as its own recorded change. A successor of an unmarked document
	needs nothing and binds its project.
	"""

	_carrieanne, contributor = _member(world, "carrieanne", "contributor")
	_keanu, member = _member(world, "keanu", "member")
	old = _written(world, title="Name the project on every capture", binds="workspace")
	new = _written(world, contributor, title="Name the project on every capture and every document")
	link = {"target": old["ref"], "target_type": "document", "link_type": "supersedes"}

	refused = _as(world, contributor, "POST", f"/v1/documents/{new['ref']}/links", json=link)

	assert refused.status_code == 403, refused.text
	assert world.call("GET", f"/v1/documents/{old['ref']}/links").json()["items"] == []

	drawn = _as(world, member, "POST", f"/v1/documents/{new['ref']}/links", json=link)

	assert drawn.status_code == 201, drawn.text

	after = world.call("GET", f"/v1/documents/{new['ref']}").json()

	assert after["binds"] == "workspace", after
	assert after["version"] == new["version"] + 1, after

	recorded: list[dict[str, typing.Any] | None] = list(
		world.session.scalars(
			sqlalchemy.select(subroutine.db.models.activity.Event.changes).where(
				subroutine.db.models.activity.Event.entity_id == uuid.UUID(new["id"])
			)
		)
	)

	assert {"binds": {"from": "project", "to": "workspace"}} in recorded, recorded

	first = _written(world, contributor, title="Tag every release")
	second = _written(world, contributor, title="Tag every release and say so")
	plain = _as(
		world,
		contributor,
		"POST",
		f"/v1/documents/{second['ref']}/links",
		json={**link, "target": first["ref"]},
	)

	assert plain.status_code == 201, plain.text
	assert world.call("GET", f"/v1/documents/{second['ref']}").json()["binds"] == "project"
