"""One predicate decides whether a document binds everyone - `SR#4576`, decision `#4134`.

**Marked ``workspace``, of a governing type, in force, live and not beneath a private project**
(revised with Simon on 2026-10-05, Q5 and Q6 of the cold review of the permission model, and on
2026-10-09 when this was built). A write that changes whether a document binds everyone, or rewrites
one that does, takes what marking takes; nothing else does, so a draft, a superseded rule, a trashed
one and one in a private project block nothing (E #7). And a rule that binds everyone is read by every
credential in the workspace, whatever its project scope (E #6).
"""

import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.clients.base
import subroutine.clients.http
import subroutine.connections
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.documents
import subroutine.domain.projects
import subroutine.errors
import subroutine.mcp.tools
import subroutine.views
import test_api_tasks
import test_document_binds

_member = test_document_binds._member
_credential = test_document_binds._credential
_as = test_document_binds._as
_written = test_document_binds._written


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def _project (world: test_api_tasks.World, key: str, **body: typing.Any) -> str:
	"""Make a project as the world's own account, and return its id."""

	made = world.call("POST", "/v1/projects", json={"key": key, "title": key.title(), **body})

	assert made.status_code == 201, made.text

	return str(made.json()["id"])


def test_the_predicate_and_its_sql_agree_on_every_combination (world: test_api_tasks.World) -> None:
	"""The check reads the predicate in Python and a read asks it in SQL: two forms of one rule.

	**Every combination of the five facts**, and the three ways a document is out of the trash's
	reach or a private project's without being in it or in one itself: beneath a trashed document, in
	a trashed project, and in a public project beneath a private one.
	"""

	session = world.session
	documents = subroutine.domain.documents
	project = subroutine.domain.projects
	opened = project.create(session, workspace_id=world.workspace.id, key="open", title="Open")
	vault = project.create(
		session, workspace_id=world.workspace.id, key="vault", title="Vault", visibility="private"
	)
	expected: dict[uuid.UUID, bool] = {}

	for marked in (True, False):
		for kind in ("decision", "note"):
			for status in ("active", "draft"):
				for live in (True, False):
					for place in (opened, vault):
						row = documents.create(
							session,
							project=place,
							title=f"{marked} {kind} {status} {live} {place.key}",
							type_key=kind,
							status_key=status,
							binds="workspace" if marked else None,
						)

						if not live:
							documents.delete(session, row)

						expected[row.id] = (
							marked and kind == "decision" and status == "active" and live and place is opened
						)

	held = documents.create(session, project=opened, title="Held", type_key="decision")
	beneath = documents.create(
		session, project=opened, title="Beneath", type_key="decision", binds="workspace", parent=held
	)
	documents.delete(session, held)
	gone = project.create(session, workspace_id=world.workspace.id, key="gone", title="Gone")
	trashed = documents.create(
		session, project=gone, title="In the trash", type_key="decision", binds="workspace"
	)
	project.delete(session, gone)
	inside = project.create(
		session, workspace_id=world.workspace.id, key="inside", title="Inside", parent=vault
	)
	hidden = documents.create(
		session, project=inside, title="Hidden", type_key="decision", binds="workspace"
	)
	session.flush()

	expected |= {held.id: False, beneath.id: False, trashed.id: False, hidden.id: False}
	model = subroutine.db.models.work.Document
	places = subroutine.db.models.project.Project
	asked = set(
		session.scalars(
			sqlalchemy.select(model.id)
			.join(places, places.id == model.project_id)
			.where(
				model.workspace_id == world.workspace.id,
				documents.binding_everyone(model, places),
			)
		)
	)
	read = documents._rules(
		session, world.workspace.id, model.workspace_id == world.workspace.id, marking=list(expected)
	).binding()
	wanted = {one for one, binds in expected.items() if binds}

	assert len(wanted) == 1, wanted
	assert asked == wanted, (asked, wanted)
	assert read == wanted, (read, wanted)


def test_a_rule_that_binds_nobody_blocks_nothing_and_is_not_named (world: test_api_tasks.World) -> None:
	"""E #7 of the cold review of 2026-10-05, confirmed and widened by its verification.

	A project holding only a draft, a superseded or a trashed marked rule was refused to a narrowed
	credential, moved under a private project or deleted, as if the rule bound everybody. And a marked
	rule in somebody else's private project beneath refused the owner, telling them it held *a rule that
	binds the whole workspace* - about a document they could not see. **And the control**: a project
	holding a rule that binds everyone is still refused.
	"""

	ops = _project(world, "ops")
	web = _project(world, "web")
	vault = _project(world, "vault", visibility="private")
	docs = _project(world, "docs")
	narrowed = _credential(world, world.user, project_write_scope=[ops, web, vault, docs])

	for status in ("draft", "superseded"):
		rule = _written(world, title=f"Rotate the disks ({status})", binds="workspace", project="ops")

		assert world.call("PATCH", f"/v1/documents/{rule['ref']}", json={"status": status}).status_code == 200

	gone = _written(world, title="Rotate the disks (trashed)", binds="workspace", project="ops")

	assert world.call("DELETE", f"/v1/documents/{gone['ref']}").status_code < 300

	moved = _as(world, narrowed, "POST", "/v1/projects/ops/move", json={"parent": "vault"})

	assert moved.status_code == 200, moved.text
	assert _as(world, narrowed, "POST", "/v1/projects/vault/ops/move", json={"parent": "web"}).status_code == 200
	assert _as(world, narrowed, "DELETE", "/v1/projects/web/ops").status_code < 300

	# **Somebody else's private project beneath binds nobody outside it**, so it is neither counted nor
	# named: keanu files a rule in his own private project inside the website.
	_keanu, member = _member(world, "keanu", "member")
	inside = _as(
		world,
		member,
		"POST",
		"/v1/projects",
		json={"key": "mine", "title": "Mine", "parent": "web", "visibility": "private"},
	)

	assert inside.status_code == 201, inside.text

	_written(world, member, title="Keanu's rule for his corner", binds="workspace", project=inside.json()["id"])
	hidden = _as(world, narrowed, "PATCH", "/v1/projects/web", json={"visibility": "private"})

	assert "rule that binds the whole workspace" not in hidden.text, hidden.text
	assert _as(world, narrowed, "POST", "/v1/projects/web/move", json={"parent": "vault"}).status_code == 200

	_written(world, title="Every page loads fast", binds="workspace", project="docs")
	kept = _as(world, narrowed, "DELETE", "/v1/projects/docs")

	assert kept.status_code == 403, kept.text
	assert "rule that binds the whole workspace" in kept.text, kept.text


def test_marking_a_document_that_binds_nobody_is_free_and_putting_it_in_force_is_not (
	world: test_api_tasks.World,
) -> None:
	"""Decision `#4134` as revised on 2026-10-09: marking a draft or a note changes whom nothing binds.

	**Bringing it into force is the act that asks**, by a status or a type, and so is writing one that
	is in force the moment it exists.
	"""

	_carrieanne, contributor = _member(world, "carrieanne", "contributor")

	draft = _written(world, contributor, title="Tag every release", binds="workspace", status="draft")
	note = _written(world, contributor, title="How we tag", binds="workspace", type="note")
	plain = _written(world, contributor, title="Tag every release twice", status="draft")

	assert (draft["binds"], note["binds"]) == ("workspace", "workspace")

	for rule, change in ((draft, {"status": "active"}), (note, {"type": "decision"})):
		refused = _as(world, contributor, "PATCH", f"/v1/documents/{rule['ref']}", json=change)

		assert refused.status_code == 403, refused.text
		assert "the whole workspace" in refused.text, refused.text

	path = f"/v1/documents/{plain['ref']}"

	assert _as(world, contributor, "PATCH", path, json={"binds": "workspace"}).status_code == 200
	assert _as(world, contributor, "PATCH", path, json={"binds": "project"}).status_code == 200

	in_force = _as(
		world, contributor, "POST", "/v1/documents", json={"title": "A rule", "binds": "workspace", "type": "decision"}
	)

	assert in_force.status_code == 403, in_force.text


def test_a_rule_moves_freely_between_public_places_and_not_into_a_private_one (
	world: test_api_tasks.World,
) -> None:
	"""Decision `#4134` as revised on 2026-10-09: a rule binds everybody either side of a public move.

	**Into a private project takes the mark**, since it hides the rule: asked of the project's own
	owner on a narrowed credential, whom the steward's rule lets across (`SR#4570`).
	"""

	_carrieanne, contributor = _member(world, "carrieanne", "contributor")
	inbox = world.call("GET", "/v1/projects/inbox").json()["id"]
	web = _project(world, "web")
	vault = _project(world, "vault", visibility="private")
	narrowed = _credential(world, world.user, project_write_scope=[inbox, web, vault])
	rule = _written(world, title="Every page loads fast", binds="workspace")
	path = f"/v1/documents/{rule['ref']}"
	moved = _as(world, contributor, "PATCH", path, json={"project": "web"})

	assert moved.status_code == 200, moved.text
	assert moved.json()["binds"] == "workspace"

	hidden = _as(world, narrowed, "PATCH", path, json={"project": "vault"})

	assert hidden.status_code == 403, hidden.text
	assert hidden.json()["errors"][0]["field"] == "project", hidden.text
	assert world.call("PATCH", path, json={"project": "vault"}).status_code == 200


def test_a_rules_words_take_the_mark_while_it_binds_everyone (world: test_api_tasks.World) -> None:
	"""Q5 of the cold review of 2026-10-05: the text is what reaches every agent's conventions.

	A contributor and a member's narrowed credential rewrote a marked rule's title or body, 200,
	where a shared view's text already took what sharing it takes (`#3589`). **And the controls**:
	a marked draft and an unmarked rule are edited as before, and the rule by a member.
	"""

	_carrieanne, contributor = _member(world, "carrieanne", "contributor")
	keanu, member = _member(world, "keanu", "member")
	inbox = world.call("GET", "/v1/projects/inbox").json()["id"]
	narrowed = _credential(world, keanu, project_write_scope=[inbox])
	rule = _written(world, title="Name the project on every capture", body="With +key.", binds="workspace")
	path = f"/v1/documents/{rule['ref']}"

	for secret in (contributor, narrowed):
		for field, value in (("title", "Name nothing"), ("body", "Never mind.")):
			refused = _as(world, secret, "PATCH", path, json={field: value})

			assert refused.status_code == 403, refused.text

			# The narrowed credential is told which part of the request took it to the workspace.
			if secret == narrowed:
				assert refused.json()["errors"][0]["field"] == field, refused.text

	after = world.call("GET", path).json()

	assert (after["title"], after["body"]) == (rule["title"], rule["body"]), after

	draft = _written(world, title="Name the project", binds="workspace", status="draft")
	plain = _written(world, title="Name the project always")

	for other in (draft, plain):
		assert _as(
			world, contributor, "PATCH", f"/v1/documents/{other['ref']}", json={"body": "Revised."}
		).status_code == 200

	assert _as(world, member, "PATCH", path, json={"body": "With +key, always."}).status_code == 200


def test_a_rule_that_binds_everyone_is_read_by_every_credential_in_the_workspace (
	world: test_api_tasks.World,
) -> None:
	"""Q6 of the cold review of 2026-10-05, and E #6: a narrowed agent never saw the rules it exists for.

	alice's rule in ``ops`` was absent for a credential narrowed to ``web``, from the listing and from
	``subroutine://conventions``, which did not say so. **Read by every credential in the workspace**,
	as a shared view is, and nothing more: a marked draft, an unmarked rule and a marked one in a
	private project the reader is shared into stay narrowed away, and a write still asks for its project.
	"""

	web = _project(world, "web")
	_project(world, "ops")
	_project(world, "vault", visibility="private")
	keanu, _member_secret = _member(world, "keanu", "member")
	shared = world.call("POST", "/v1/projects/vault/members", json={"username": keanu.username})

	assert shared.status_code < 300, shared.text

	narrowed = _credential(world, keanu, project_scope=[web])
	rule = _written(world, title="Rotate the backup disks every week", binds="workspace", project="ops")
	left = [
		_written(world, title="Rotate them nightly", binds="workspace", project="ops", status="draft"),
		_written(world, title="Label every disk", project="ops"),
		_written(world, title="Keep the keys in the vault", binds="workspace", project="vault"),
	]
	listed = _as(world, narrowed, "GET", "/v1/documents")

	assert listed.status_code == 200, listed.text

	refs = {row["ref"] for row in listed.json()["items"]}

	assert rule["ref"] in refs, listed.text
	assert not refs & {one["ref"] for one in left}, listed.text
	assert _as(world, narrowed, "GET", f"/v1/documents/{rule['ref']}").status_code == 200

	for one in left:
		assert _as(world, narrowed, "GET", f"/v1/documents/{one['ref']}").status_code == 404

	# **Refused as beyond its projects, not as out of sight**, which would deny a project it can see.
	written = _as(world, narrowed, "PATCH", f"/v1/documents/{rule['ref']}", json={"title": "Rotate"})

	assert written.status_code == 403, written.text
	assert "beyond the projects" in written.json()["detail"], written.text

	with subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url="https://example.com"),
		token=narrowed,
		transport=api_support.SyncTransport(world.application),
		base_url=api_support.BASE_URL,
	) as client:
		index = subroutine.mcp.tools._conventions(client, world.workspace.slug)

	assert rule["title"] in index, index
	assert "Label every disk" not in index, index


def test_a_draft_successor_is_marked_freely_and_put_in_force_by_somebody_who_may_mark (
	world: test_api_tasks.World,
) -> None:
	"""Decision `#4134`: a successor carries the mark, and the link takes the mark where that binds.

	**A draft revision of a rule is the ordinary way to amend one**, and marking it binds nobody, so a
	contributor writes and links it; putting it in force is the act that asks.
	"""

	_carrieanne, contributor = _member(world, "carrieanne", "contributor")
	_keanu, member = _member(world, "keanu", "member")
	old = _written(world, title="Name the project on every capture", binds="workspace")
	new = _written(world, contributor, title="Name it on every capture and document", status="draft")
	link = {"target": old["ref"], "target_type": "document", "link_type": "supersedes"}
	drawn = _as(world, contributor, "POST", f"/v1/documents/{new['ref']}/links", json=link)

	assert drawn.status_code == 201, drawn.text

	path = f"/v1/documents/{new['ref']}"

	assert world.call("GET", path).json()["binds"] == "workspace"
	assert _as(world, contributor, "PATCH", path, json={"status": "active"}).status_code == 403
	assert _as(world, member, "PATCH", path, json={"status": "active"}).status_code == 200


def _marked (**facts: typing.Any) -> subroutine.views.Document:
	"""Return a document view holding only what the wording of a mark reads, marked and binding."""

	held: dict[str, typing.Any] = {
		"binds": "workspace",
		"type": "decision",
		"status_category": subroutine.domain.documents.CURRENT_CATEGORY,
		"deleted_at": None,
		"project_path": "web/blog",
	}

	return subroutine.views.Document.model_construct(**{**held, **facts})


@pytest.mark.parametrize(
	("facts", "private", "said"),
	[
		({}, frozenset(), subroutine.clients.base.BINDS_EVERYBODY),
		({}, frozenset({"ops"}), subroutine.clients.base.BINDS_EVERYBODY),
		({"project_path": "webinar"}, frozenset({"web"}), subroutine.clients.base.BINDS_EVERYBODY),
		({"binds": "project"}, frozenset(), None),
		(
			{"status_category": subroutine.domain.documents.DRAFT_CATEGORY},
			frozenset(),
			subroutine.clients.base.MARKED_TO_BIND,
		),
		({"type": "note"}, frozenset(), subroutine.clients.base.MARKED_TO_BIND),
		(
			{"deleted_at": subroutine.db.types.utcnow()},
			frozenset(),
			subroutine.clients.base.MARKED_TO_BIND,
		),
		({}, frozenset({"web/blog"}), subroutine.clients.base.MARKED_TO_BIND),
		({}, frozenset({"web"}), subroutine.clients.base.MARKED_TO_BIND),
		({}, None, subroutine.clients.base.MARKED_TO_BIND),
		({"project_path": ""}, frozenset(), subroutine.clients.base.MARKED_TO_BIND),
	],
	ids=[
		"in force and public",
		"a private project elsewhere",
		"a project whose name only starts the same",
		"not marked",
		"a draft",
		"a note",
		"in the trash",
		"in a private project",
		"beneath a private project",
		"projects that could not be listed",
		"no path to place it by",
	],
)
def test_a_mark_is_said_to_bind_only_where_the_one_predicate_says_it_does (
	facts: dict[str, typing.Any], private: frozenset[str] | None, said: str | None
) -> None:
	"""`SR#4853`: marked is not binding, and three surfaces read the mark alone.

	``show``, the browser and the agent tools said *binds the whole workspace* of any marked
	document: a draft, a note, a rule in the trash and one inside a private project, which bind
	nobody outside. The words now follow :func:`subroutine.domain.documents.binds_everyone`, and
	say only that it is marked where the predicate says no - or where it could not be asked,
	because the projects could not be listed or the document names no path.
	"""

	assert subroutine.clients.base.said_to_bind(_marked(**facts), private) == said


def test_the_private_places_include_archived_ones_and_are_untold_where_listing_is_refused () -> None:
	"""`SR#4853`: what :func:`said_to_bind` is told, and the difference between none and unknown.

	Finishing with a project does not make it public, so the archived are asked for too. A
	credential that may not list projects is answered ``None`` - not an empty set, which would say
	every marked rule sits in the open.
	"""

	class Listing:
		"""A client that lists two projects, or refuses, and notes what it was asked."""

		def __init__ (self, refuses: bool = False) -> None:
			"""Remember whether to refuse."""

			self.refuses = refuses
			self.asked: dict[str, typing.Any] = {}

		def projects (self, **asked: typing.Any) -> list[subroutine.views.Project]:
			"""List a public project and a private one, as an instance would."""

			self.asked = asked

			if self.refuses:
				raise subroutine.errors.Forbidden("You may not list projects here.")

			# Only the two fields the reader takes, unpacked so the model's other fields are not asked for.
			rows: list[dict[str, typing.Any]] = [
				{"path": "web", "visibility": "public"},
				{"path": "vault", "visibility": "private"},
			]

			return [subroutine.views.Project.model_construct(**row) for row in rows]

	listing = Listing()
	client = typing.cast(subroutine.clients.base.Client, listing)

	assert subroutine.clients.base.private_places(client, workspace="metacortex") == frozenset(
		{"vault"}
	)
	assert listing.asked["include_archived"] is True, listing.asked
	assert listing.asked["workspace"] == "metacortex", listing.asked

	refusing = typing.cast(subroutine.clients.base.Client, Listing(refuses=True))

	assert subroutine.clients.base.private_places(refusing) is None
