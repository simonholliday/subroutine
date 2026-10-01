"""The round trip: an export rebuilt into an empty database gives back every line - `#4055`.

Decision `#4049`: no import command in 1.0, and this is what proves an export is enough to leave
with. **The importer exists only here.** It writes each line's columns straight into its table,
works out only what an export leaves to be worked out - a materialised path, a depth, the rows
joining a tag to what it is on - and is then read back through the same export, as the same
reader, line by line. A value that changes on the way out, or a column no line carries, fails it.

**What the importer had to supply because no line carries it is :data:`SUPPLIED`**, each entry
naming the item that would put it in the export. That register is the honest list of what an
export does not yet hold; deleting an entry is what closes its item.
"""

import datetime
import pathlib
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import conftest
import subroutine.db.base
import subroutine.db.models
import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.saved
import subroutine.db.models.system
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.seed
import subroutine.db.session
import subroutine.domain.authentication
import subroutine.domain.events
import subroutine.domain.hierarchy
import subroutine.domain.tags
import subroutine.domain.users
import subroutine.exporting
import subroutine.views
import test_export
import test_transport_equivalence

pair = test_transport_equivalence.pair
Pair = test_transport_equivalence.Pair
Lines = dict[str, list[dict[str, typing.Any]]]


@pytest.fixture(autouse=True)
def _every_event_settled (monkeypatch: pytest.MonkeyPatch) -> None:
	"""Report events the moment they are written - ``test_export.py`` says why."""

	monkeypatch.setattr(subroutine.domain.events, "WATERMARK", datetime.timedelta(0))

#: **What the importer supplies because no line carries it**, and the item that would make it
#: carry it. Each is a fact about the workspace an export cannot give back today.
SUPPLIED: dict[str, str] = {
	"workspace": (
		"#4066 - only its id, short name and title reach the manifest. Its time zone, its "
		"description, its settings and its prioritised project are in no file."
	),
	"membership": (
		"#4066 - who is a member, with which role, and who belongs to a private project are in "
		"no file. The importer makes the reader the owner and a member of every project they "
		"own, which is what creating one writes."
	),
}


def _lines (pair: Pair) -> Lines:
	"""Return every kind's lines, as ``subroutine export`` writes them."""

	return {
		kind: [subroutine.exporting.line(kind, row) for row in pair.local.export(kind)]
		for kind in subroutine.views.EXPORTED
	}


def _seeded (pair: Pair) -> None:
	"""Fill the source with every kind, and the values likeliest to change on the way out."""

	made = test_export._seeded(pair)
	local = pair.local

	# A deadline for a whole day is stored at the last microsecond of it (`#1291`), the value a
	# careless reading rounds away; an item kept in another zone; text beyond the first plane.
	local.update(ref=made["notes"].ref, due="2026-10-09", due_is_all_day=True)
	local.update(ref=made["fix"].ref, timezone="Pacific/Auckland", importance=4, urgency=2)
	local.update(ref=made["fix"].ref, description="Ship it 🦊 before the release notes, #2.")

	decision = local.create_document(title="Why the deploy script moved", body="Because 🦊.")
	local.create_document(title="What moved with it", body="The cron line.", parent=decision.ref)
	local.create_project(key="ops", title="Operations", visibility="private")
	local.capture(text="Fix the leak on the third floor", project="ops")


def _value (column: sqlalchemy.Column[typing.Any], value: typing.Any) -> typing.Any:
	"""Return a JSON value as the type its column compares in, driven by the column's own type."""

	if value is None or not isinstance(value, str):
		return value

	try:
		python_type = column.type.python_type

	except NotImplementedError:
		return value

	if python_type is datetime.datetime:
		return datetime.datetime.fromisoformat(value)

	if python_type is datetime.date:
		return datetime.date.fromisoformat(value)

	if python_type is uuid.UUID:
		return uuid.UUID(value)

	return value


def _row (
	table: sqlalchemy.Table, line: dict[str, typing.Any], **more: typing.Any
) -> dict[str, typing.Any]:
	"""Return the columns of ``table`` that a line carries, typed, with ``more`` beside them."""

	carried = {
		column.key: _value(column, line[column.key])
		for column in table.columns
		if column.key in line
	}

	return {**carried, **more}


def _paths (
	lines: list[dict[str, typing.Any]], parent: str
) -> dict[uuid.UUID, tuple[str, int]]:
	"""Return each row's materialised path and depth, worked out from who its parent is."""

	parents = {uuid.UUID(line["id"]): line[parent] for line in lines}
	found: dict[uuid.UUID, tuple[str, int]] = {}

	def placed (identifier: uuid.UUID) -> str:
		"""Return one row's path, placing its parent first."""

		if identifier not in found:
			above = parents[identifier]
			path = subroutine.domain.hierarchy.build_path(
				None if above is None else placed(uuid.UUID(above)), identifier
			)
			found[identifier] = (path, subroutine.domain.hierarchy.depth_of(path))

		return found[identifier][0]

	for identifier in parents:
		placed(identifier)

	return found


def _rebuilt (
	session: sqlalchemy.orm.Session,
	lines: Lines,
	*,
	workspace: subroutine.views.WorkspaceRef,
	reader: uuid.UUID,
) -> None:
	"""Write an export into an empty database, as an importer of it would."""

	tables = {
		table.name: table for table in subroutine.db.base.Base.metadata.sorted_tables
	}
	user = subroutine.db.models.identity.User

	session.add(subroutine.db.models.system.Instance(name="Rebuilt", timezone="UTC"))

	# **In an order that lets every reference be written as the row is**, never by an update
	# afterwards, which moves the row's own ``updated_at`` and fails the comparison for it.
	accounts = _paths(lines["users"], "responsible_user_id")

	for line in sorted(lines["users"], key=lambda one: accounts[uuid.UUID(one["id"])][1]):
		row = _row(
			tables["user"],
			line,
			username_normalized=subroutine.domain.users.normalize(line["username"]),
			updated_at=_value(tables["user"].c.created_at, line["created_at"]),
		)
		session.execute(sqlalchemy.insert(user), [row])

	# SUPPLIED["workspace"]: everything but its id, short name and title.
	rebuilt = subroutine.db.models.identity.Workspace(
		id=workspace.id, slug=workspace.slug, title=workspace.title
	)
	subroutine.db.seed.seed_workspace(session, rebuilt)
	session.flush()

	# The seeded words go, so the exported ones can come back under their own ids; the roles
	# stay, since no line carries them (SUPPLIED["membership"]).
	for model in (
		subroutine.db.models.vocabulary.Status,
		subroutine.db.models.vocabulary.ItemType,
		subroutine.db.models.vocabulary.LinkType,
	):
		session.execute(sqlalchemy.delete(model).where(model.workspace_id == workspace.id))

	for kind, table in (("statuses", "status"), ("item_types", "item_type")):
		for line in lines[kind]:
			session.execute(
				sqlalchemy.insert(tables[table]),
				[_row(tables[table], line, workspace_id=workspace.id)],
			)

	for kind, table in (("link_types", "link_type"), ("tags", "tag")):
		for line in lines[kind]:
			extra: dict[str, typing.Any] = {"workspace_id": workspace.id}

			if table == "tag":
				extra["name_normalized"] = subroutine.domain.tags.normalize(line["name"])

			session.execute(sqlalchemy.insert(tables[table]), [_row(tables[table], line, **extra)])

	owner = subroutine.db.models.identity.Role
	(role,) = session.scalars(
		sqlalchemy.select(owner).where(owner.workspace_id == workspace.id, owner.key == "owner")
	)
	session.add(
		subroutine.db.models.identity.WorkspaceMember(
			workspace_id=workspace.id, user_id=reader, role_id=role.id
		)
	)

	projects = _paths(lines["projects"], "parent_id")

	for line in sorted(lines["projects"], key=lambda one: projects[uuid.UUID(one["id"])][1]):
		path, depth = projects[uuid.UUID(line["id"])]
		session.execute(
			sqlalchemy.insert(tables["project"]),
			[_row(tables["project"], line, path=path, depth=depth)],
		)

		if line["owner_id"] is not None:
			session.add(
				subroutine.db.models.project.ProjectMember(
					workspace_id=workspace.id,
					project_id=uuid.UUID(line["id"]),
					user_id=uuid.UUID(line["owner_id"]),
					role_id=None,
				)
			)

	tag_ids = {line["name"]: uuid.UUID(line["id"]) for line in lines["tags"]}
	refs = {line["ref"]: uuid.UUID(line["id"]) for line in lines["tasks"]}

	for kind, table, joining, parent in (
		("tasks", "task", "task_tag", "parent_task_id"),
		("documents", "document", "document_tag", "parent_id"),
	):
		placed = _paths(lines[kind], parent)

		# A repeat's template before the occurrences made from it, at each depth.
		for line in sorted(
			lines[kind],
			key=lambda one: (placed[uuid.UUID(one["id"])][1], not one.get("is_template")),
		):
			identifier = uuid.UUID(line["id"])
			path, depth = placed[identifier]
			template = line.get("recurrence_template_ref")

			# **An occurrence reports its series' rule and stores none of its own**: the view
			# reads it off the template, so storing it here would be a second copy of a fact the
			# template holds - and the comparison of what is stored, below, says so.
			series = (
				{}
				if template is None
				else {
					"recurrence_template_id": refs[template],
					"recurrence_rule": None,
					"recurrence_anchor": None,
					"recurrence_trigger": None,
				}
			)
			session.execute(
				sqlalchemy.insert(tables[table]),
				[_row(tables[table], line, path=path, depth=depth, **series)],
			)

			for name in line["tags"]:
				column = "task_id" if table == "task" else "document_id"
				session.execute(
					sqlalchemy.insert(tables[joining]),
					[{column: identifier, "tag_id": tag_ids[name]}],
				)

	for line in lines["comments"]:
		session.execute(sqlalchemy.insert(tables["comment"]), [_row(tables["comment"], line)])

	link_types = {line["key"]: uuid.UUID(line["id"]) for line in lines["link_types"]}

	for line in lines["links"]:
		session.execute(
			sqlalchemy.insert(tables["link"]),
			[
				_row(
					tables["link"],
					line,
					workspace_id=workspace.id,
					link_type_id=link_types[line["link_type"]],
					source_type=line["source"]["entity_type"],
					source_id=uuid.UUID(line["source"]["id"]),
					target_type=line["target"]["entity_type"],
					target_id=uuid.UUID(line["target"]["id"]),
				)
			],
		)

	named = {line["username"]: uuid.UUID(line["id"]) for line in lines["users"]}

	for line in lines["verifications"]:
		session.execute(
			sqlalchemy.insert(tables["verification"]),
			[
				_row(
					tables["verification"],
					line,
					workspace_id=workspace.id,
					task_id=refs[line["task_ref"]],
					created_by=named.get(line["recorded_by"]),
				)
			],
		)

	for line in lines["events"]:
		session.execute(sqlalchemy.insert(tables["event"]), [_row(tables["event"], line)])

	for line in lines["saved_views"]:
		session.execute(
			sqlalchemy.insert(tables["saved_view"]),
			[_row(tables["saved_view"], line)],
		)

	session.flush()


@pytest.fixture
def empty (pair: Pair, tmp_path: pathlib.Path) -> typing.Iterator[sqlalchemy.orm.Session]:
	"""An empty database on the same engine as the source, with the schema and nothing else."""

	if pair.session.get_bind().dialect.name == "sqlite":
		url = f"sqlite:///{tmp_path / 'rebuilt.db'}"
		dropped: str | None = None

	else:
		dropped = conftest.throwaway_name("rebuilt")
		admin = sqlalchemy.create_engine(conftest.POSTGRES_ADMIN_URL, isolation_level="AUTOCOMMIT")

		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'CREATE DATABASE "{dropped}"'))

		admin.dispose()
		url = conftest.with_database(conftest.POSTGRES_ADMIN_URL, dropped)

	engine = subroutine.db.session.create_engine(url)

	try:
		subroutine.db.base.Base.metadata.create_all(engine)

		with sqlalchemy.orm.Session(engine) as session:
			yield session

	finally:
		engine.dispose()

		if dropped is not None:
			admin = sqlalchemy.create_engine(
				conftest.POSTGRES_ADMIN_URL, isolation_level="AUTOCOMMIT"
			)

			with admin.connect() as connection:
				connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{dropped}"'))

			admin.dispose()


def test_an_export_rebuilt_into_an_empty_database_gives_back_every_line (
	pair: Pair, empty: sqlalchemy.orm.Session
) -> None:
	"""Every kind, every line, every field the export carries, as the same reader, both engines."""

	_seeded(pair)
	before = _lines(pair)
	workspace = pair.local.identity().workspaces[0]

	_rebuilt(empty, before, workspace=workspace, reader=pair.user.id)

	rebuilt = empty.get(subroutine.db.models.identity.User, pair.user.id)

	assert rebuilt is not None, "the reader's account did not come back"

	reader = subroutine.domain.authentication.Principal(user=rebuilt)

	for kind in subroutine.views.EXPORTED:
		again = [
			subroutine.exporting.line(kind, row)
			for row in _read(empty, reader, workspace.id, kind)
		]

		assert before[kind], f"the seed made no {kind}, so this compared nothing"
		assert len(again) == len(before[kind]), (kind, len(again), len(before[kind]))

		# Field by field, so a failure names what changed rather than printing two rows.
		for was, now in zip(before[kind], again, strict=True):
			changed = {
				key: (was.get(key), now.get(key))
				for key in was.keys() | now.keys()
				if was.get(key) != now.get(key)
			}

			assert not changed, (kind, was.get("ref", was.get("id")), changed)


#: The table each kind is stored in, for the comparison of what is stored.
TABLES = {
	"projects": "project",
	"tasks": "task",
	"documents": "document",
	"comments": "comment",
	"links": "link",
	"verifications": "verification",
	"events": "event",
	"tags": "tag",
	"statuses": "status",
	"item_types": "item_type",
	"link_types": "link_type",
	"saved_views": "saved_view",
	"users": "user",
}


def test_every_value_a_line_carries_is_stored_again_exactly_as_it_was (
	pair: Pair, empty: sqlalchemy.orm.Session
) -> None:
	"""The stronger half: the rows, not the lines, compared column by column.

	**Comparing lines alone cannot see a rendering that loses something the same way twice.**
	A deadline rounded to the second on the way out would be stored rounded, rendered rounded
	again, and match. Compared where it is stored, the last microsecond of a whole day's
	deadline (`#1291`) either came back or did not.
	"""

	_seeded(pair)
	before = _lines(pair)
	workspace = pair.local.identity().workspaces[0]
	_rebuilt(empty, before, workspace=workspace, reader=pair.user.id)
	tables = {table.name: table for table in subroutine.db.base.Base.metadata.sorted_tables}

	for kind, name in TABLES.items():
		table = tables[name]
		carried = [column for column in table.columns if column.key in before[kind][0]]
		key = table.c.seq if name == "event" else table.c.id
		wanted = [
			_value(key, line["seq" if name == "event" else "id"]) for line in before[kind]
		]
		statement = sqlalchemy.select(*carried).where(key.in_(wanted)).order_by(key)
		was = [dict(row._mapping) for row in pair.session.execute(statement)]
		now = [dict(row._mapping) for row in empty.execute(statement)]

		assert len(was) == len(now) == len(before[kind]), kind

		for stored, again in zip(was, now, strict=True):
			changed = {
				column: (stored[column], again[column])
				for column in stored
				if stored[column] != again[column]
			}

			assert not changed, (kind, stored.get("ref", stored.get("id")), changed)


def _read (
	session: sqlalchemy.orm.Session,
	reader: subroutine.domain.authentication.Principal,
	workspace_id: uuid.UUID,
	kind: str,
) -> list[typing.Any]:
	"""Return every row of one kind the rebuilt database exports, walking its pages."""

	found: list[typing.Any] = []
	after = None

	for _ in range(1000):
		page = subroutine.exporting.page(
			session, reader, workspace_id=workspace_id, kind=kind, after=after, size=1000
		)
		found.extend(page.items)

		if not page.has_more:
			return found

		after = page.resume

	raise AssertionError(f"the pages of {kind} never ended")


def test_everything_the_importer_supplies_names_the_item_that_would_carry_it () -> None:
	"""A gap with no item is one nobody closes."""

	for name, reason in SUPPLIED.items():
		assert reason.startswith("#"), name
