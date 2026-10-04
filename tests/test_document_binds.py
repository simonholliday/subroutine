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


@pytest.mark.parametrize(
	("door", "method", "body"),
	[
		("a status out of force", "PATCH", {"status": "superseded"}),
		("a type that binds nobody", "PATCH", {"type": "note"}),
		("a move to another project", "PATCH", {"project": "web"}),
		("a delete", "DELETE", None),
	],
)
def test_retiring_a_marked_rule_takes_what_marking_it_takes (
	door: str, method: str, body: dict[str, str] | None, world: test_api_tasks.World
) -> None:
	"""`SR#4286`, M1 of the cold review of 2026-10-03, decision `#4134` as amended that day.

	A contributor, and a member's credential narrowed to some projects, were refused the mark and
	could still take a marked rule out of every agent's conventions by another door. **And the
	controls**: an unmarked rule goes through each door as before, and so does the marked one for
	somebody who may mark it.
	"""

	_carrieanne, contributor = _member(world, "carrieanne", "contributor")
	keanu, _member_secret = _member(world, "keanu", "member")
	web = world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"}).json()["id"]
	inbox = world.call("GET", "/v1/projects/inbox").json()["id"]
	narrowed = _credential(world, keanu, project_write_scope=[inbox, web])
	owner_narrowed = _credential(world, world.user, project_write_scope=[inbox, web])
	sent = {} if body is None else {"json": body}

	# Nobody below an administrator may delete a document, so a narrowed owner stands for them there.
	refused_to = [owner_narrowed] if method == "DELETE" else [contributor, narrowed]

	for number, secret in enumerate(refused_to):
		rule = _written(world, title=f"Every page loads fast ({door}, {number})", binds="workspace")
		path = f"/v1/documents/{rule['ref']}"
		answer = _as(world, secret, method, path, **sent)

		assert answer.status_code == 403, f"{door} went through: {answer.status_code} {answer.text}"
		assert "the whole workspace" in answer.text, answer.text

		after = world.call("GET", path).json()

		assert (after["status"], after["type"], after["binds"]) == (
			rule["status"],
			rule["type"],
			"workspace",
		), after

		plain = _written(world, title=f"Run the checklist before a release ({door}, {number})")

		assert _as(
			world, secret, method, f"/v1/documents/{plain['ref']}", **sent
		).status_code == 200, f"{door} was refused for a rule binding only its project"

		assert world.call(method, path, **sent).status_code == 200, f"{door} refused the owner"


def test_a_marked_section_goes_nowhere_its_document_cannot (world: test_api_tasks.World) -> None:
	"""`SR#4286`: a section moves and goes to the trash with the document it is part of.

	So an unmarked document holding a marked section is a door to the section.
	"""

	keanu, _member_secret = _member(world, "keanu", "member")
	web = world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"}).json()["id"]
	inbox = world.call("GET", "/v1/projects/inbox").json()["id"]
	narrowed = _credential(world, keanu, project_write_scope=[inbox, web])
	owner_narrowed = _credential(world, world.user, project_write_scope=[inbox, web])
	held = _written(world, title="The handbook")
	_written(world, title="Name the project on every capture", binds="workspace", parent=held["ref"])
	path = f"/v1/documents/{held['ref']}"

	moved = _as(world, narrowed, "PATCH", path, json={"project": "web"})

	assert moved.status_code == 403, moved.text

	deleted = _as(world, owner_narrowed, "DELETE", path)

	assert deleted.status_code == 403, deleted.text


def test_a_section_already_in_the_trash_is_no_door_to_a_rule (world: test_api_tasks.World) -> None:
	"""`SR#4431`, R2-L39 of the cold review of 2026-10-04: a marked section in the trash still counted.

	A document whose only marked section was already in the trash was refused to a narrowed
	credential, moved or deleted, where one with no marked section went through. **Only a live rule
	counts**, as it does for a project holding one.
	"""

	keanu, _member_secret = _member(world, "keanu", "member")
	web = world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"}).json()["id"]
	inbox = world.call("GET", "/v1/projects/inbox").json()["id"]
	narrowed = _credential(world, keanu, project_write_scope=[inbox, web])
	owner_narrowed = _credential(world, world.user, project_write_scope=[inbox, web])
	held = _written(world, title="The handbook")
	section = _written(
		world, title="Name the project on every capture", binds="workspace", parent=held["ref"]
	)
	path = f"/v1/documents/{held['ref']}"
	gone = world.call("DELETE", f"/v1/documents/{section['ref']}")

	assert gone.status_code < 300, gone.text

	moved = _as(world, narrowed, "PATCH", path, json={"project": "web"})

	assert moved.status_code == 200, moved.text

	deleted = _as(world, owner_narrowed, "DELETE", path)

	assert deleted.status_code < 300, deleted.text

	# **Restoring still asks about every section** (`SR#4389`): one that went to the trash with its
	# document comes back with it, into force.
	other = _written(world, title="The runbook")
	_written(world, title="Page whoever is on call", binds="workspace", parent=other["ref"])
	gone = world.call("DELETE", f"/v1/documents/{other['ref']}")

	assert gone.status_code < 300, gone.text

	restored = _as(world, owner_narrowed, "POST", f"/v1/documents/{other['ref']}/restore")

	assert restored.status_code == 403, restored.text

	# **And one whose section stayed in the trash on its own brings no rule back**, and is restored.
	restored = _as(world, owner_narrowed, "POST", f"{path}/restore")

	assert restored.status_code == 200, restored.text


@pytest.mark.parametrize("door", ["private", "under a private project", "deleted"])
def test_hiding_the_project_that_holds_a_marked_rule_takes_what_marking_takes (
	door: str, world: test_api_tasks.World
) -> None:
	"""`SR#4286`: privacy inherits down the tree, and a deleted project takes its rules with it.

	Making the rule's project private, moving it under a private one, or deleting it each took the
	rule out of sight. **And the control**: a project holding no marked rule goes through each.
	"""

	ops = world.call("POST", "/v1/projects", json={"key": "ops", "title": "Operations"}).json()["id"]
	vault = world.call(
		"POST", "/v1/projects", json={"key": "vault", "title": "Vault", "visibility": "private"}
	).json()["id"]
	web = world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"}).json()["id"]
	narrowed = _credential(world, world.user, project_write_scope=[ops, vault, web])
	_written(world, title="Rotate the backup disks every week", binds="workspace", project="ops")

	def through (key: str) -> typing.Any:
		"""Go through the door for one project, with the narrowed credential."""

		if door == "private":
			return _as(world, narrowed, "PATCH", f"/v1/projects/{key}", json={"visibility": "private"})

		if door == "under a private project":
			return _as(world, narrowed, "POST", f"/v1/projects/{key}/move", json={"parent": "vault"})

		return _as(world, narrowed, "DELETE", f"/v1/projects/{key}")

	refused = through("ops")

	assert refused.status_code == 403, f"{door} went through: {refused.text}"
	assert "rule that binds the whole workspace" in refused.text, refused.text
	assert through("web").status_code == 200, f"{door} was refused for a project holding no rule"


@pytest.mark.parametrize(
	("door", "made", "method", "suffix", "body"),
	[
		("a status into force", {"status": "superseded"}, "PATCH", "", {"status": "active"}),
		("a draft into force", {"status": "draft"}, "PATCH", "", {"status": "active"}),
		("a type that binds everybody", {"type": "note"}, "PATCH", "", {"type": "decision"}),
		("a restore", None, "POST", "/restore", None),
	],
)
def test_bringing_a_marked_rule_into_force_takes_what_marking_it_takes (
	door: str,
	made: dict[str, str] | None,
	method: str,
	suffix: str,
	body: dict[str, str] | None,
	world: test_api_tasks.World,
) -> None:
	"""`SR#4389`, R2-M6 of the cold review of 2026-10-04, decision `#4134` as amended that day.

	Retiring a marked rule took the marking permission, and bringing one back did not: a contributor
	put a superseded rule back into force with a new title, and a credential refused the delete
	restored one from the trash. **And the controls**: an unmarked rule goes through each door, and
	the marked one does for somebody who may mark it.
	"""

	_carrieanne, contributor = _member(world, "carrieanne", "contributor")
	inbox = world.call("GET", "/v1/projects/inbox").json()["id"]
	owner_narrowed = _credential(world, world.user, project_write_scope=[inbox])

	# Nobody below an administrator may restore a document, so a narrowed owner stands for them there.
	secret = owner_narrowed if suffix == "/restore" else contributor
	sent = {} if body is None else {"json": body}

	for marked in (True, False):
		title = f"Every page loads fast ({door}, {'marked' if marked else 'plain'})"
		rule = _written(world, title=title, **({"binds": "workspace"} if marked else {}))
		path = f"/v1/documents/{rule['ref']}"
		prepared = (
			world.call("DELETE", path) if made is None else world.call("PATCH", path, json=made)
		)

		assert prepared.status_code in (200, 204), prepared.text

		answer = _as(world, secret, method, path + suffix, **sent)

		if not marked:
			assert answer.status_code == 200, f"{door} was refused for a rule binding only its project"
			continue

		assert answer.status_code == 403, f"{door} went through: {answer.status_code} {answer.text}"
		assert "the whole workspace" in answer.text, answer.text
		assert world.call(method, path + suffix, **sent).status_code == 200, f"{door} refused the owner"


@pytest.mark.parametrize("door", ["restored", "public", "out from under a private project"])
def test_showing_the_project_that_holds_a_marked_rule_takes_what_marking_takes (
	door: str, world: test_api_tasks.World
) -> None:
	"""`SR#4389`: the mirror of hiding one. Restoring the rule's project, making it public, or moving
	it out from under a private project each brought the rule into everybody's conventions.

	**And the control**: a project holding no marked rule goes through each.
	"""

	vault = world.call(
		"POST", "/v1/projects", json={"key": "vault", "title": "Vault", "visibility": "private"}
	).json()["id"]
	web = world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"}).json()["id"]
	made = []

	for key in ("ops", "docs"):
		if door == "public":
			made.append(
				world.call(
					"POST",
					"/v1/projects",
					json={"key": key, "title": key, "visibility": "private"},
				).json()["id"]
			)

		elif door == "out from under a private project":
			made.append(
				world.call(
					"POST", "/v1/projects", json={"key": key, "title": key, "parent": "vault"}
				).json()["id"]
			)

		else:
			made.append(
				world.call("POST", "/v1/projects", json={"key": key, "title": key}).json()["id"]
			)

	_written(world, title="Rotate the backup disks every week", binds="workspace", project=made[0])
	narrowed = _credential(world, world.user, project_write_scope=[vault, web, *made])

	def through (key: str) -> typing.Any:
		"""Go through the door for one project, with the narrowed credential."""

		address = f"vault/{key}" if door == "out from under a private project" else key

		if door == "restored":
			assert world.call("DELETE", f"/v1/projects/{address}").status_code in (200, 204)

			return _as(world, narrowed, "POST", f"/v1/projects/{address}/restore")

		if door == "public":
			return _as(world, narrowed, "PATCH", f"/v1/projects/{address}", json={"visibility": "public"})

		return _as(world, narrowed, "POST", f"/v1/projects/{address}/move", json={"parent": "web"})

	refused = through("ops")

	assert refused.status_code == 403, f"{door} went through: {refused.text}"
	assert "rule that binds the whole workspace" in refused.text, refused.text
	assert through("docs").status_code == 200, f"{door} was refused for a project holding no rule"


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
