"""A project or workspace hides task statuses and document statuses separately - `SR#5147`.

L-7's bullet of the cold review of 2026-10-17, decided in `#5097`. Every workspace is seeded with
``superseded`` for tasks and for documents, and with ``active`` and ``archived`` for documents and for
projects, so one hidden list keyed by the word hid a task's status from documents too, and a rename
or a removal of one kind's status rewrote what the other kind hid. ``statuses.hidden`` keeps its
name for the tasks; ``document_statuses.hidden`` holds the documents'.
"""

import json
import re
import typing

import pytest
import sqlalchemy
import sqlalchemy.orm
import typer.testing

import subroutine.db.migrate
import subroutine.db.models.identity
import subroutine.db.seed
import subroutine.db.session
import subroutine.domain.projects
import subroutine.domain.settings
import test_api_tasks
import test_api_vocabulary
import test_migrations
import test_personal_path

#: The personal path's fresh home and its runner, and a migrated database on each backend, bound
#: here so pytest finds them as fixtures.
home = test_personal_path.home
run = test_personal_path.run
migrated_url = test_migrations.migrated_url

TASKS = subroutine.domain.settings.HIDDEN_STATUSES.key
DOCUMENTS = subroutine.domain.settings.HIDDEN_DOCUMENT_STATUSES.key

#: The schema before this item's migration, where one list held both kinds.
BEFORE = "bc50ce7db4bc"


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def _web (world: test_api_tasks.World) -> dict[str, typing.Any]:
	"""Make the project `web` and return it as the API reports it."""

	made = world.call("POST", "/v1/projects", json={"key": "web", "title": "Website rebuild"})

	assert made.status_code == 201, made.text

	return dict(made.json())


def _configured (world: test_api_tasks.World, settings: dict[str, typing.Any]) -> dict[str, typing.Any]:
	"""Set settings on `web` and return the project as the API then reports it."""

	changed = world.call("PATCH", "/v1/projects/web", json={"settings": settings})

	assert changed.status_code == 200, changed.text

	return dict(changed.json())


def _stored (world: test_api_tasks.World) -> dict[str, typing.Any]:
	"""Return the workspace's settings as stored, read afresh."""

	world.session.expire(world.workspace)

	return dict(world.workspace.settings)


def test_hiding_one_kinds_status_leaves_the_other_kinds_offered (world: test_api_tasks.World) -> None:
	"""A project hiding the task status ``superseded`` still offers the document status, and back.

	**Read through what publishes the answer**, the project's two resolved lists, since that is
	what every picker narrows by. Against the code before, the document list was not a setting at
	all and one list served both pickers.
	"""

	_web(world)
	tasks = _configured(world, {TASKS: ["superseded"]})

	assert tasks["hidden_statuses"] == ["superseded"]
	assert tasks["hidden_document_statuses"] == [], (
		"hiding a task status hid the document status of the same key"
	)

	documents = _configured(world, {TASKS: None, DOCUMENTS: ["superseded"]})

	assert documents["hidden_document_statuses"] == ["superseded"]
	assert documents["hidden_statuses"] == [], (
		"hiding a document status hid the task status of the same key"
	)


def test_a_project_inherits_each_list_from_its_workspace_on_its_own (
	world: test_api_tasks.World,
) -> None:
	"""Each list walks the chain by itself: set one on the project and the other still inherits."""

	hid = world.call(
		"PATCH",
		f"/v1/workspaces/{world.workspace.slug}",
		json={"settings": {TASKS: ["blocked"], DOCUMENTS: ["draft"]}},
	)

	assert hid.status_code == 200, hid.text

	_web(world)
	web = _configured(world, {TASKS: []})

	assert web["hidden_statuses"] == [], "the project's own empty list did not override"
	assert web["hidden_document_statuses"] == ["draft"], (
		"setting the task list on the project stopped the document list inheriting"
	)


def test_renaming_one_kinds_status_leaves_the_other_kinds_list (world: test_api_tasks.World) -> None:
	"""Renaming the task ``superseded`` rewrites the task list alone, and a project status neither.

	`#5097`'s case: renaming the task status ``superseded`` to ``replaced`` rewrote the one list,
	so superseded documents were offered again; renaming the project status ``archived`` un-hid
	archived documents. Nothing reported either.
	"""

	hid = world.call(
		"PATCH",
		f"/v1/workspaces/{world.workspace.slug}",
		json={"settings": {TASKS: ["superseded"], DOCUMENTS: ["archived", "superseded"]}},
	)

	assert hid.status_code == 200, hid.text

	for entity_type, was, now in (("task", "superseded", "replaced"), ("project", "archived", "retired")):
		which = test_api_vocabulary._keyed(
			test_api_vocabulary._statuses(world, entity_type=entity_type), was
		)["id"]
		renamed = world.call("PATCH", f"/v1/statuses/{which}", json={"key": now})

		assert renamed.status_code == 200, renamed.text

	stored = _stored(world)

	assert stored[TASKS] == ["replaced"], "the task list did not follow its own status's rename"
	assert stored[DOCUMENTS] == ["archived", "superseded"], (
		"a task's or a project's rename rewrote what is hidden from documents"
	)

	which = test_api_vocabulary._keyed(
		test_api_vocabulary._statuses(world, entity_type="document"), "archived"
	)["id"]

	assert world.call("PATCH", f"/v1/statuses/{which}", json={"key": "filed"}).status_code == 200
	assert _stored(world)[DOCUMENTS] == ["filed", "superseded"], (
		"the document list did not follow its own status's rename"
	)


def test_removing_one_kinds_status_leaves_the_other_kinds_list (world: test_api_tasks.World) -> None:
	"""A task status and a document status both keyed ``later``: removing one keeps the other hidden."""

	made = {
		kind: world.call(
			"POST",
			"/v1/statuses",
			json={"entity_type": kind, "key": "later", "label": "Later", "category": category},
		)
		for kind, category in (("task", "todo"), ("document", "draft"))
	}

	for kind, response in made.items():
		assert response.status_code == 201, (kind, response.text)

	world.call(
		"PATCH",
		f"/v1/workspaces/{world.workspace.slug}",
		json={"settings": {TASKS: ["later"], DOCUMENTS: ["later"]}},
	)

	assert world.call("DELETE", f"/v1/statuses/{made['task'].json()['id']}").status_code == 204

	stored = _stored(world)

	assert stored[TASKS] == []
	assert stored[DOCUMENTS] == ["later"], "removing a task status un-hid the document status"


@pytest.mark.parametrize(
	("setting", "key", "said"),
	[
		(TASKS, "on_hold", "This workspace has no task status keyed 'on_hold'."),
		(DOCUMENTS, "on_hold", "This workspace has no document status keyed 'on_hold'."),
		(TASKS, "draft", "This workspace has no task status keyed 'draft'."),
		(DOCUMENTS, "blocked", "This workspace has no document status keyed 'blocked'."),
	],
	ids=["project-in-tasks", "project-in-documents", "document-in-tasks", "task-in-documents"],
)
def test_a_list_refuses_a_key_its_kind_does_not_have (
	world: test_api_tasks.World, setting: str, key: str, said: str
) -> None:
	"""Each list takes its own kind's keys, so a project status's key is refused by either.

	``on_hold`` is only ever a project's status, which no picker offers; the check accepted it
	before, because it asked whether any status here had the key.
	"""

	refused = world.call(
		"PATCH", f"/v1/workspaces/{world.workspace.slug}", json={"settings": {setting: [key]}}
	)

	assert refused.status_code == 422, refused.text
	assert refused.json()["detail"] == said, refused.text


@pytest.mark.parametrize("sent", ["blocked", [5]], ids=["not-a-list", "not-a-word"])
@pytest.mark.parametrize("setting", [TASKS, DOCUMENTS], ids=["tasks", "documents"])
def test_what_a_shape_refusal_suggests_is_what_its_list_accepts (
	world: test_api_tasks.World, setting: str, sent: typing.Any
) -> None:
	"""An example in a refusal is what somebody sends next, so it has to be one this list takes.

	Found driving this before it landed: the two lists shared one check, whose examples were task
	statuses, so following its hint on the document list was refused in turn. **Sent as a reader
	would send it**, rather than compared with a list of the seeded keys.
	"""

	path = f"/v1/workspaces/{world.workspace.slug}"
	refused = world.call("PATCH", path, json={"settings": {setting: sent}})

	assert refused.status_code == 422, refused.text

	hint = refused.json()["errors"][0]["hint"]
	suggested = re.findall(r"""["']([a-z_]+)["']""", hint)

	assert suggested, f"nothing to follow in {hint!r}"

	followed = world.call("PATCH", path, json={"settings": {setting: suggested}})

	assert followed.status_code == 200, f"following {hint!r} was refused: {followed.text}"


def test_the_terminal_hides_each_kind_and_hide_nothing_offers_both (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`--hide-document-status` beside `--hide-status`, and `--hide-nothing` acting on both (`#5097`).

	The workspace hides one of each, which is the arrangement where inheriting and offering
	everything differ. Clearing the document list goes back to inheriting while the task list
	stays as it was told.
	"""

	run("init")
	run("project", "create", "web", "Web")
	run("workspace", "update", "projects", "--hide-status", "blocked", "--hide-document-status", "draft")

	def hidden () -> tuple[list[str], list[str]]:
		"""What the listing says `web` does not offer, tasks then documents."""

		rows = json.loads(run("project", "list", "--json").output)
		web = next(one for one in rows if one["key"] == "web")

		return web["hidden_statuses"], web["hidden_document_statuses"]

	assert hidden() == (["blocked"], ["draft"])

	shown = run("project", "settings", "web").output

	assert "draft - inherited from the workspace" in shown, (
		f"the terminal did not say where the document list comes from: {shown}"
	)

	run("project", "update", "web", "--hide-nothing")

	assert hidden() == ([], []), "--hide-nothing left one of the lists inheriting"

	run("project", "update", "web", "--hide-document-status", "")

	assert hidden() == ([], ["draft"]), "clearing the document list did not go back to inheriting"

	run("project", "update", "web", "--hide-document-status", "superseded")

	assert hidden() == ([], ["superseded"])

	refused = run(
		"project", "update", "web", "--hide-nothing", "--hide-document-status", "draft", expect=1
	)

	assert "--hide-document-status and --hide-nothing say opposite things." in refused.output, (
		refused.output
	)


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_the_upgrade_splits_each_list_by_kind_and_going_back_merges_them (
	migrated_url: str,
) -> None:
	"""The migration, with rows in first, on both backends (`#1689`'s rule for a data migration).

	The workspace's one list holds a key both kinds have (``superseded``), one each (``blocked``,
	``draft``) and a project status's (``on_hold``). **A project that stated a list gets both**,
	even where a side comes out empty, because its one list replaced its workspace's whole: one
	hiding only ``blocked`` offered every document status, and goes on doing so. One that said
	nothing gains nothing. Going back merges each pair into one list.
	"""

	engine = subroutine.db.session.create_engine(migrated_url)
	# **Each table as the migration reads it**: an id keeps the model's type, which is what binds a
	# UUID on SQLite, and the settings are the JSON they are stored as.
	identity_type = subroutine.db.models.identity.Workspace.__table__.c.id.type
	settings_of = {
		kind: sqlalchemy.table(
			kind,
			sqlalchemy.column("id", identity_type),
			sqlalchemy.column("settings", sqlalchemy.JSON()),
		)
		for kind in ("workspace", "project")
	}

	def read (kind: str, identity: typing.Any) -> dict[str, typing.Any]:
		"""Return one row's settings as the database holds them."""

		table = settings_of[kind]

		with engine.connect() as connection:
			return dict(
				connection.execute(
					sqlalchemy.select(table.c.settings).where(table.c.id == identity)
				).scalar_one()
			)

	def write (kind: str, identity: typing.Any, settings: dict[str, typing.Any]) -> None:
		"""Store one row's settings whole, as the revision before held them."""

		table = settings_of[kind]

		with engine.begin() as connection:
			connection.execute(table.update().where(table.c.id == identity).values(settings=settings))

	try:
		with sqlalchemy.orm.Session(engine) as session:
			workspace = subroutine.db.models.identity.Workspace(slug="metacortex", title="Metacortex")

			session.add(workspace)
			subroutine.db.seed.seed_workspace(session, workspace)
			session.flush()

			made = {
				key: subroutine.domain.projects.create(
					session, workspace_id=workspace.id, key=key, title=key.title()
				).id
				for key in ("web", "errands", "docs", "garden")
			}
			workspace_id = workspace.id

			session.commit()

		subroutine.db.migrate.downgrade(migrated_url, BEFORE)

		before: dict[tuple[str, typing.Any], dict[str, typing.Any]] = {
			("workspace", workspace_id): {TASKS: ["blocked", "draft", "on_hold", "superseded"]},
			("project", made["web"]): {TASKS: ["blocked"]},
			("project", made["errands"]): {TASKS: []},
			("project", made["docs"]): {TASKS: ["draft"], "appearance.colour": "teal"},
			("project", made["garden"]): {"appearance.colour": "green"},
		}

		for (kind, identity), settings in before.items():
			write(kind, identity, settings)

		subroutine.db.migrate.upgrade(migrated_url)

		assert read("workspace", workspace_id) == {
			TASKS: ["blocked", "superseded"], DOCUMENTS: ["draft", "superseded"],
		}, "the workspace's list was not split by the kinds holding each key"
		assert read("project", made["web"]) == {TASKS: ["blocked"], DOCUMENTS: []}, (
			"a project hiding a task status alone began inheriting its workspace's document list"
		)
		assert read("project", made["errands"]) == {TASKS: [], DOCUMENTS: []}
		assert read("project", made["docs"]) == {
			TASKS: [], DOCUMENTS: ["draft"], "appearance.colour": "teal",
		}, "a setting beside the list was lost, or the list went to the wrong kind"
		assert read("project", made["garden"]) == {"appearance.colour": "green"}, (
			"a project that said nothing about statuses was given a list"
		)

		subroutine.db.migrate.downgrade(migrated_url, BEFORE)

		assert read("workspace", workspace_id) == {TASKS: ["blocked", "draft", "superseded"]}, (
			"going back did not merge the two lists into one"
		)
		assert read("project", made["docs"]) == {TASKS: ["draft"], "appearance.colour": "teal"}
		assert read("project", made["garden"]) == {"appearance.colour": "green"}

		subroutine.db.migrate.upgrade(migrated_url)

		assert read("workspace", workspace_id) == {
			TASKS: ["blocked", "superseded"], DOCUMENTS: ["draft", "superseded"],
		}, "going forward again did not split the merged list as before"

	finally:
		engine.dispose()
