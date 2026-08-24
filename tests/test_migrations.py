"""Tests that the migrations build the schema the models describe, on both backends.

The drift check is the most important test in this file, and arguably in the project.
Without it, a model can be changed without its migration and nothing complains until a
real installation is upgraded and the schema no longer matches the code that queries it.
"""

import datetime
import pathlib
import typing
import unittest.mock
import uuid

import alembic.command
import alembic.runtime.migration
import pytest
import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.orm

import conftest
import subroutine.db.base
import subroutine.db.migrate
import subroutine.db.models
import subroutine.db.models.identity
import subroutine.db.seed
import subroutine.db.session
import subroutine.db.types


@pytest.fixture
def migrated_url (
	request: pytest.FixtureRequest, tmp_path: pathlib.Path
) -> typing.Iterator[str]:
	"""Yield the URL of an empty database with every migration applied.

	A fresh database each time, separate from the shared schema fixture, so this measures
	what the migrations actually build rather than what ``create_all`` built earlier.
	"""

	backend = request.param

	if backend == "sqlite":
		url = f"sqlite:///{tmp_path / 'migrated.db'}"

		subroutine.db.migrate.upgrade(url)

		yield url

	else:
		admin_url = request.getfixturevalue("postgres_url")
		database = f"subroutine_mig_{uuid.uuid4().hex[:12]}"
		admin = sqlalchemy.engine.make_url(admin_url).set(database="postgres")
		admin_engine = sqlalchemy.create_engine(admin, isolation_level="AUTOCOMMIT")

		with admin_engine.connect() as connection:
			connection.execute(sqlalchemy.text(f'CREATE DATABASE "{database}"'))

		url = conftest.with_database(admin_url, database)

		subroutine.db.migrate.upgrade(url)

		yield url

		with admin_engine.connect() as connection:
			connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{database}"'))

		admin_engine.dispose()


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_migrations_produce_no_drift_against_the_models (migrated_url: str) -> None:
	"""Upgrading an empty database yields exactly the schema the models describe.

	A non-empty result means someone changed a model without writing the migration to
	match. The failure message lists the differences, which is usually enough to write
	the missing migration without further investigation.
	"""

	engine = subroutine.db.session.create_engine(migrated_url)

	try:
		differences = subroutine.db.migrate.schema_differences(engine)

	finally:
		engine.dispose()

	assert differences == [], f"models and migrations disagree: {differences}"


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_check_constraints_match_the_models (migrated_url: str) -> None:
	"""The half of the schema autogenerate cannot see.

	Alembic does not compare CHECK constraints, and this project keeps its status
	categories, its entity-type vocabularies and its numeric ranges in them. Without this
	assertion, widening an ``enum_check`` passes the whole suite — which builds its schema
	from the models — and reaches production with the old constraint still in place.
	"""

	engine = subroutine.db.session.create_engine(migrated_url)

	try:
		differences = subroutine.db.migrate.check_constraint_differences(engine)

	finally:
		engine.dispose()

	assert differences == [], f"CHECK constraints disagree: {differences}"


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_migrations_create_every_table (migrated_url: str) -> None:
	"""Every table the models declare exists after an upgrade."""

	engine = subroutine.db.session.create_engine(migrated_url)

	try:
		present = set(sqlalchemy.inspect(engine).get_table_names())

	finally:
		engine.dispose()

	expected = set(subroutine.db.base.Base.metadata.tables)

	assert expected <= present
	assert "alembic_version" in present


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_a_migrated_database_reports_itself_up_to_date (migrated_url: str) -> None:
	"""The recorded revision matches the newest migration available."""

	engine = subroutine.db.session.create_engine(migrated_url)

	try:
		assert subroutine.db.migrate.is_up_to_date(engine)
		assert subroutine.db.migrate.current_revision(engine) == subroutine.db.migrate.head_revision()

	finally:
		engine.dispose()


def test_an_empty_database_is_not_up_to_date (tmp_path: pathlib.Path) -> None:
	"""A database that has never been migrated is detected as such.

	This is what lets ``subroutine doctor`` tell someone to run an upgrade rather than
	failing later with a missing-table error.
	"""

	engine = subroutine.db.session.create_engine(f"sqlite:///{tmp_path / 'empty.db'}")

	try:
		assert subroutine.db.migrate.current_revision(engine) is None
		assert not subroutine.db.migrate.is_up_to_date(engine)

	finally:
		engine.dispose()


def test_downgrade_removes_the_schema (tmp_path: pathlib.Path) -> None:
	"""The initial migration can be undone, which proves it was written both ways.

	A migration with no working downgrade is one nobody can back out of under pressure.
	"""

	url = f"sqlite:///{tmp_path / 'reversible.db'}"

	subroutine.db.migrate.upgrade(url)
	subroutine.db.migrate.downgrade(url, "base")

	engine = subroutine.db.session.create_engine(url)

	try:
		remaining = set(sqlalchemy.inspect(engine).get_table_names())

	finally:
		engine.dispose()

	assert remaining <= {"alembic_version"}


def test_head_revision_is_known () -> None:
	"""There is at least one migration, and it can be identified without a database."""

	assert subroutine.db.migrate.head_revision()


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_every_migration_survives_a_database_with_data_in_it (migrated_url: str) -> None:
	"""Walk the whole revision history backwards and forwards with real rows present.

	**Every other test in this file migrates an empty database, and that hid a defect for
	three revisions.** SQLite cannot alter most things in place, so Alembic rebuilds the
	table — create a copy, move the rows, drop the original, rename — and that drop is a
	foreign-key violation only when *another* table holds a row pointing at it. With no
	rows, nothing points at anything and every migration looks fine.

	Fixed in ``migrations/env.py`` by turning enforcement off for the duration. This is the
	test that would have caught it, and it is written against the history rather than
	against one revision so it goes on catching the next one.
	"""

	engine = subroutine.db.session.create_engine(migrated_url)

	try:
		_populate(engine)

		# Down to the first revision and back up, with the rows in place the whole way.
		subroutine.db.migrate.downgrade(migrated_url, "base")
		subroutine.db.migrate.upgrade(migrated_url)

		assert subroutine.db.migrate.is_up_to_date(engine)

	finally:
		engine.dispose()


#: The revision before an event grew ``subject_type``/``subject_id``, when a comment's event
#: said what it was made on inside its ``changes`` document.
_BEFORE_SUBJECTS = "0c8f7a7027e6"


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_a_comment_event_written_before_subjects_gains_one (migrated_url: str) -> None:
	"""`#52`'s backfill, which is most of what that migration is for.

	The columns are what put a comment into the commented-on item's history. Adding them and
	leaving the existing rows alone would fix the defect for comments written after the
	upgrade and leave every earlier one invisible for good — the version of the fix that
	passes every test written against a fresh database.
	"""

	engine = subroutine.db.session.create_engine(migrated_url)

	try:
		subroutine.db.migrate.downgrade(migrated_url, _BEFORE_SUBJECTS)

		workspace_id, comment_id, subject_id = (subroutine.db.types.new_uuid() for _ in range(3))

		with engine.begin() as connection:
			_insert(connection, "workspace", {"id": workspace_id, "slug": "w", "title": "W"})
			_insert(
				connection,
				"comment",
				{
					"id": comment_id,
					"workspace_id": workspace_id,
					"entity_type": "task",
					"entity_id": subject_id,
					"body": "Ran the suite.",
				},
			)
			_insert(
				connection,
				"event",
				{
					"workspace_id": workspace_id,
					"entity_type": "comment",
					"entity_id": comment_id,
					# Explicit, because the two must be distinguishable and `seq` is what orders them.
					"seq": 1,
					"action": "created",
					"changes": {"on": {"from": None, "to": f"task:{subject_id}"}},
				},
			)
			# An edit, which never carried `on` at all. Backfilling from `changes` alone leaves
			# this one unattributed for ever, and it is the row a reader most wants.
			_insert(
				connection,
				"event",
				{
					"workspace_id": workspace_id,
					"entity_type": "comment",
					"entity_id": comment_id,
					"seq": 2,
					"action": "updated",
					"changes": {"body": {"from": "Ran it.", "to": "Ran the suite."}},
				},
			)

		subroutine.db.migrate.upgrade(migrated_url)

		created, updated = _events_about(engine, comment_id, "subject_type", "subject_id", "changes")

		assert created["subject_type"] == "task"
		assert created["subject_id"] == subject_id
		assert updated["subject_id"] == subject_id

		# The old copy is gone rather than left beside the new one: two places saying the same
		# thing is how they come to disagree. The edit keeps the diff that is its own content.
		assert created["changes"] is None
		assert updated["changes"] == {"body": {"from": "Ran it.", "to": "Ran the suite."}}

		# And back, because a downgrade that drops the only copy of a fact is not a downgrade.
		subroutine.db.migrate.downgrade(migrated_url, _BEFORE_SUBJECTS)

		was_created, was_updated = _events_about(engine, comment_id, "changes")

		assert was_created["changes"] == {"on": {"from": None, "to": f"task:{subject_id}"}}

		# Nothing invented on the edit: the older schema never wrote `on` onto one, so putting
		# it there would hand it a row it could not have produced.
		assert was_updated["changes"] == {"body": {"from": "Ran it.", "to": "Ran the suite."}}

	finally:
		engine.dispose()


#: The revision before ``start_at`` split into an appointment and a defer.
_BEFORE_THE_DATE_SPLIT = "a3f9c21d7e40"

#: The revision before an item type carried a category (`SR#1134`), which is the one whose
#: upgrade backfills it.
_BEFORE_THE_TYPE_CATEGORY = "a01dcd83a946"

#: The same, one vocabulary along: before a *link* type carried one (`SR#1157`).
_BEFORE_THE_LINK_CATEGORY = "491e1a09de04"


#: The task table as it stood at :data:`_BEFORE_THE_DATE_SPLIT`, carrying its real types.
#:
#: The current models no longer describe ``planned_for`` or ``start_is_all_day``, so a seed
#: written through ``Base.metadata`` cannot reach either. Typed rather than a ``text()``
#: statement for the reason this test exists: an untyped bind is rendered by the driver
#: rather than by the column.
_TASK_BEFORE = sqlalchemy.table(
	"task",
	sqlalchemy.column("id", subroutine.db.types.uuid_column()),
	sqlalchemy.column("workspace_id", subroutine.db.types.uuid_column()),
	sqlalchemy.column("project_id", subroutine.db.types.uuid_column()),
	sqlalchemy.column("type_id", subroutine.db.types.uuid_column()),
	sqlalchemy.column("status_id", subroutine.db.types.uuid_column()),
	sqlalchemy.column("ref", sqlalchemy.Integer()),
	sqlalchemy.column("title", sqlalchemy.String()),
	sqlalchemy.column("planned_for", subroutine.db.types.CalendarDate()),
	sqlalchemy.column("due_is_all_day", sqlalchemy.Boolean()),
	sqlalchemy.column("start_is_all_day", sqlalchemy.Boolean()),
	sqlalchemy.column("spent_minutes", sqlalchemy.Integer()),
	sqlalchemy.column("is_template", sqlalchemy.Boolean()),
	sqlalchemy.column("path", sqlalchemy.String()),
	sqlalchemy.column("depth", sqlalchemy.Integer()),
	sqlalchemy.column("position", sqlalchemy.Integer()),
	sqlalchemy.column("metadata", subroutine.db.types.json_column()),
	sqlalchemy.column("content_updated_at", subroutine.db.types.UtcDateTime()),
	sqlalchemy.column("created_at", subroutine.db.types.UtcDateTime()),
	sqlalchemy.column("updated_at", subroutine.db.types.UtcDateTime()),
	sqlalchemy.column("version", sqlalchemy.Integer()),
)


def _a_workspace_with_one_task (
	connection: sqlalchemy.Connection,
	table: sqlalchemy.TableClause,
	**task: typing.Any,
) -> uuid.UUID:
	"""Seed a workspace, a project and one task, inserting the task into ``table``.

	The task's shape differs either side of the revision under test, so the caller names it:
	:data:`_TASK_BEFORE` for the older schema, the live table for the current one. Everything
	else goes through the current models, which describe those tables unchanged across it.
	"""

	workspace, project, kind, status, identifier = (
		subroutine.db.types.new_uuid() for _ in range(5)
	)

	# A slug is unique across the instance, and this seeds a second workspace for the
	# application-written row it compares against. From the *end* of the UUID: version 7
	# leads with a timestamp, so two minted in the same millisecond share their prefix.
	slug = f"w{workspace.hex[-8:]}"

	_insert(connection, "workspace", {"id": workspace, "slug": slug, "title": slug})
	_insert(
		connection,
		"item_type",
		{"id": kind, "workspace_id": workspace, "key": "task", "label": "Task"},
	)
	_insert(
		connection,
		"status",
		{"id": status, "workspace_id": workspace, "key": "open", "label": "Open"},
	)
	_insert(
		connection,
		"project",
		{
			"id": project,
			"workspace_id": workspace,
			"key": "p",
			"title": "P",
			"status_id": status,
			"path": "p",
		},
	)

	stamp = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

	values: dict[str, typing.Any] = {
		"id": identifier,
		"workspace_id": workspace,
		"project_id": project,
		"type_id": kind,
		"status_id": status,
		"ref": 1,
		"title": "Dentist",
		"due_is_all_day": False,
		"spent_minutes": 0,
		"is_template": False,
		"path": str(identifier),
		"depth": 0,
		"position": 1,
		"metadata": {},
		"content_updated_at": stamp,
		"created_at": stamp,
		"updated_at": stamp,
		"version": 1,
	}

	# Renamed by the revision under test, so it exists in one shape and not the other.
	if "start_is_all_day" in table.c:
		values["start_is_all_day"] = False

	connection.execute(sqlalchemy.insert(table).values(**values, **task))

	return identifier


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_the_backfilled_link_category_is_the_one_the_seeder_would_have_written (
	migrated_url: str,
) -> None:
	"""`SR#1157`, and the same guard as its sibling below, one vocabulary along.

	The migration carries a ``key -> category`` map because a backfill cannot call the seeder, so
	the decision's table is written down twice. **Two copies that agree are invisible**: every
	other test here passes whether they agree or not, because both produce a value and the column
	is NOT NULL either way.

	Driven rather than diffed, for the reason spelled out below — reading the migration's dict
	and comparing it to the seeder checks my transcription and not the backfill.

	**The fallback is asserted by this rather than beside it.** If the ``WHERE key = …`` half
	stopped matching, every relation would come back ``describing`` and `blocks` is what would
	fail — which is also the one that matters, since it is the only category that hides work.
	"""

	engine = subroutine.db.session.create_engine(migrated_url)

	try:
		with sqlalchemy.orm.Session(engine) as session:
			workspace = subroutine.db.models.identity.Workspace(slug="w", title="W")

			session.add(workspace)
			subroutine.db.seed.seed_workspace(session, workspace)
			session.commit()

		subroutine.db.migrate.downgrade(migrated_url, _BEFORE_THE_LINK_CATEGORY)
		subroutine.db.migrate.upgrade(migrated_url)

		table = subroutine.db.base.Base.metadata.tables["link_type"]

		with engine.begin() as connection:
			backfilled: dict[str, str] = dict(
				connection.execute(
					sqlalchemy.select(table.c.key, table.c.category)
				).tuples().all()
			)

		wanted = {one.key: one.category for one in subroutine.db.seed.LINK_TYPES}

		assert len(wanted) >= 5, f"only {sorted(wanted)} are seeded, so this checks little"
		assert backfilled == wanted

	finally:
		engine.dispose()


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_the_backfilled_type_category_is_the_one_the_seeder_would_have_written (
	migrated_url: str,
) -> None:
	"""`SR#1134`: two copies of decision `SR#1133`'s table, and this is what holds them together.

	The migration carries a ``key -> category`` map because a backfill cannot call the seeder —
	it runs against a schema the models may have moved past. So the same eleven pairs are written
	down twice, in ``491e1a09de04`` and in ``seed._ITEM_TYPES``, and **two copies that agree are
	invisible**: every other test here passes whether they agree or not, because both produce a
	value and the column is NOT NULL either way.

	**Driven rather than compared.** Reading the migration's dict and diffing it against the
	seeder would check my transcription and not the backfill — an UPDATE with a typo'd column, a
	``WHERE`` that matches nothing, or a fallback swallowing the lot would all pass. So a real
	workspace is seeded at head, the column is dropped by going back one revision, and the
	migration puts it back: what is compared is what the database ends up holding.

	The fallback is asserted *by* this rather than beside it — if the ``WHERE key = …`` half
	stopped matching, every task type would come back ``work`` and every document ``record``,
	which is what these expectations would then fail on.
	"""

	engine = subroutine.db.session.create_engine(migrated_url)

	try:
		with sqlalchemy.orm.Session(engine) as session:
			workspace = subroutine.db.models.identity.Workspace(slug="w", title="W")

			session.add(workspace)
			subroutine.db.seed.seed_workspace(session, workspace)
			session.commit()

		subroutine.db.migrate.downgrade(migrated_url, _BEFORE_THE_TYPE_CATEGORY)
		subroutine.db.migrate.upgrade(migrated_url)

		table = subroutine.db.base.Base.metadata.tables["item_type"]

		with engine.begin() as connection:
			backfilled: dict[str, str] = dict(
				connection.execute(
					sqlalchemy.select(table.c.key, table.c.category)
				).tuples().all()
			)

		wanted = {one.key: one.category for one in subroutine.db.seed._ITEM_TYPES}

		assert len(wanted) >= 11, f"only {sorted(wanted)} are seeded, so this checks little"
		assert backfilled == wanted

	finally:
		engine.dispose()


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_an_absorbed_planned_day_is_stored_as_the_application_would_store_it (
	migrated_url: str,
) -> None:
	"""`#927`'s H-14 — the migrated row was invisible to its own column's range query.

	``_absorb_planned_days`` bound a Python datetime through a bare ``text()``, which carries
	no type information, so pysqlite rendered it ``2026-03-10 00:00:00+00:00`` where
	:class:`subroutine.db.types.UtcDateTime` renders ``2026-03-10 00:00:00.000000``.

	**Both read back as the same instant**, which is what made this worth a test rather than a
	glance: ``show`` displayed the right date and every equality comparison agreed. But SQLite
	compares a DATETIME as text and ``+`` sorts before ``.``, so the absorbed row fell outside
	the range query the agenda is built on — a planned day that survived the upgrade and then
	appeared on no list.

	Asserted two ways, because either alone is weak. The range query is the behaviour that
	broke and is meaningful on both backends. The comparison against a row the *application*
	wrote is the general rule — a backfilled value is indistinguishable from a live one — and
	it is what would catch the next migration doing this to a different column.
	"""

	day = datetime.date(2026, 3, 10)
	start = datetime.datetime.combine(day, datetime.time.min, tzinfo=datetime.UTC)

	engine = subroutine.db.session.create_engine(migrated_url)

	try:
		subroutine.db.migrate.downgrade(migrated_url, _BEFORE_THE_DATE_SPLIT)

		with engine.begin() as connection:
			absorbed = _a_workspace_with_one_task(
				connection, _TASK_BEFORE, planned_for=day
			)

		subroutine.db.migrate.upgrade(migrated_url)

		table = subroutine.db.base.Base.metadata.tables["task"]

		with engine.begin() as connection:
			# A second task holding the same instant, written the way the application writes
			# one — through the column's own type.
			written = _a_workspace_with_one_task(connection, table, starts_at=start)

			found = connection.execute(
				sqlalchemy.select(table.c.id).where(
					table.c.starts_at >= start,
					table.c.starts_at < start + datetime.timedelta(days=1),
				)
			).scalars().all()

			# Compared as text, because that is where the two disagree: on PostgreSQL both
			# rows are the same `timestamptz` and this says so trivially, where on SQLite it
			# is the raw stored string and the whole finding.
			stored: dict[typing.Any, typing.Any] = dict(
				connection.execute(
					sqlalchemy.select(
						table.c.id, sqlalchemy.cast(table.c.starts_at, sqlalchemy.String)
					)
				).tuples().all()
			)

		assert absorbed in found, "the absorbed planned day is outside its own day's range"
		assert written in found

		assert stored[absorbed] == stored[written]

	finally:
		engine.dispose()


def _events_about (
	engine: sqlalchemy.engine.Engine, entity_id: uuid.UUID, *names: str
) -> list[sqlalchemy.RowMapping]:
	"""Read named columns off the events about ``entity_id``, oldest first.

	Through the table rather than a ``text()`` statement so every value passes through its
	column's type in both directions — SQLite stores a UUID as bare hex, so a literal bound
	string matches nothing and the row reads as missing rather than as wrong.
	"""

	table = subroutine.db.base.Base.metadata.tables["event"]
	columns = [table.c[name] for name in names]

	with engine.connect() as connection:
		found = connection.execute(
			sqlalchemy.select(*columns)
			.where(table.c.entity_id == entity_id)
			.order_by(table.c.seq.asc())
		)

		return list(found.mappings().all())


def _foreign_keys (connection: sqlalchemy.Connection) -> int:
	"""Return SQLite's foreign-key enforcement setting for this connection."""

	driver: typing.Any = connection.connection.driver_connection

	return int(driver.execute("PRAGMA foreign_keys").fetchone()[0])


def test_migrating_leaves_foreign_key_enforcement_on (tmp_path: pathlib.Path) -> None:
	"""``env.py`` turns enforcement off to migrate, and must turn it back on.

	Not merely tidiness. ``db/session.py`` applies its pragmas on the ``connect`` event,
	which fires once per *physical* connection and **not** on checkout from the pool — so a
	connection that goes back into a pool with enforcement off hands it out that way next
	time. The restore in ``env.py`` is the only thing that prevents it, which is why it has
	a test rather than a comment.
	"""

	url = f"sqlite:///{tmp_path / 'restored.db'}"
	engine = subroutine.db.session.create_engine(url)

	try:
		with engine.connect() as connection:
			assert _foreign_keys(connection) == 1, "the session pragmas ran"

			config = subroutine.db.migrate.build_config(url)
			config.attributes["connection"] = connection

			alembic.command.upgrade(config, "head")

			assert _foreign_keys(connection) == 1, "enforcement was restored"

	finally:
		engine.dispose()


def test_a_failed_migration_still_restores_foreign_key_enforcement (
	tmp_path: pathlib.Path,
) -> None:
	"""The restore is in a ``finally``, because the silent failure outlives its cause.

	A migration that raises half way through would otherwise leave the connection — and so
	the pooled connection behind it — with foreign keys off, in a process that goes on
	running. Nothing would report it and the next writer would simply not have referential
	integrity.
	"""

	url = f"sqlite:///{tmp_path / 'failed.db'}"
	engine = subroutine.db.session.create_engine(url)

	try:
		with engine.connect() as connection:
			config = subroutine.db.migrate.build_config(url)
			config.attributes["connection"] = connection

			# Patched rather than arranged: a genuine mid-migration failure needs a broken
			# migration in the tree. If Alembic moves this method the patch raises
			# AttributeError, which is a loud failure rather than a test that stops testing.
			with (
				unittest.mock.patch.object(
					alembic.runtime.migration.MigrationContext,
					"run_migrations",
					side_effect=RuntimeError("deliberate failure mid-migration"),
				),
				pytest.raises(RuntimeError, match="deliberate"),
			):
				alembic.command.upgrade(config, "head")

			assert _foreign_keys(connection) == 1, "enforcement was restored despite the failure"

	finally:
		engine.dispose()


def _populate (engine: sqlalchemy.engine.Engine) -> None:
	"""Insert a row into every table that holds a reference to a rebuilt one.

	**Which tables those are is derived, not listed.** The first version of this seeded six
	tables by hand, which happened to cover everything pointing at ``project`` and nothing
	pointing at ``task`` or ``document`` — so it exercised one of the four rebuilds in the
	ref migration and would have passed a future migration that rebuilt only ``task``.
	:func:`_referencing_tables` now reports the set, and :func:`test_the_seeded_data_covers_
	every_referencing_table` fails if this function stops covering it.

	Rows are built from the models rather than written out, so a column added later does not
	turn this into a guessing game — and are deliberately minimal, because what matters is
	that the referencing rows *exist*, not what they say.
	"""

	workspace = subroutine.db.types.new_uuid()
	parent_project = subroutine.db.types.new_uuid()
	project = subroutine.db.types.new_uuid()
	item_type = subroutine.db.types.new_uuid()
	status = subroutine.db.types.new_uuid()
	user = subroutine.db.types.new_uuid()
	role = subroutine.db.types.new_uuid()
	parent_task = subroutine.db.types.new_uuid()
	task = subroutine.db.types.new_uuid()
	parent_document = subroutine.db.types.new_uuid()
	document = subroutine.db.types.new_uuid()
	tag = subroutine.db.types.new_uuid()
	token = subroutine.db.types.new_uuid()
	link_type = subroutine.db.types.new_uuid()

	rows: tuple[tuple[str, dict[str, typing.Any]], ...] = (
		("workspace", {"id": workspace, "slug": "w", "title": "W"}),
		("user", {"id": user, "username": "someone"}),
		(
			"item_type",
			{"id": item_type, "workspace_id": workspace, "key": "task", "label": "Task",
				"category": "work"},
		),
		("status", {"id": status, "workspace_id": workspace, "key": "open", "label": "Open"}),
		("role", {"id": role, "workspace_id": workspace, "key": "member", "title": "Member"}),
		("tag", {"id": tag, "workspace_id": workspace, "name": "t", "name_normalized": "t"}),
		# A parent and a child, so the self-referencing foreign keys are exercised too.
		(
			"project",
			{
				"id": parent_project,
				"workspace_id": workspace,
				"key": "top",
				"title": "Top",
				"status_id": status,
				"visibility": "public",
			},
		),
		(
			"project",
			{
				"id": project,
				"workspace_id": workspace,
				"parent_id": parent_project,
				"key": "SR",
				"title": "Work",
				"status_id": status,
				"visibility": "public",
			},
		),
		(
			"project_member",
			{"workspace_id": workspace, "project_id": project, "user_id": user},
		),
		(
			"task",
			{
				"id": parent_task,
				"workspace_id": workspace,
				"project_id": project,
				"type_id": item_type,
				"status_id": status,
				"ref": 1,
				"title": "A parent task",
			},
		),
		(
			"task",
			{
				"id": task,
				"workspace_id": workspace,
				"project_id": project,
				"parent_task_id": parent_task,
				"type_id": item_type,
				"status_id": status,
				"ref": 2,
				"title": "A task",
				# A migration that *backfills* returns early on an empty column, so seeding
				# none left every backfill here running over nothing — the DDL was walked and
				# the reason the migration exists was not. `f2b8c1a94d63` is the first one
				# this reaches, whose downgrade writes a planned day back out of it.
				"starts_at": datetime.datetime(2026, 3, 10, tzinfo=datetime.UTC),
			},
		),
		("task_tag", {"task_id": task, "tag_id": tag}),
		(
			"document",
			{
				"id": parent_document,
				"workspace_id": workspace,
				"project_id": project,
				"type_id": item_type,
				"status_id": status,
				"ref": 3,
				"title": "A parent document",
			},
		),
		(
			"document",
			{
				"id": document,
				"workspace_id": workspace,
				"project_id": project,
				"parent_id": parent_document,
				"type_id": item_type,
				"status_id": status,
				"ref": 4,
				"title": "A document",
			},
		),
		("document_tag", {"document_id": document, "tag_id": tag}),
		# Everything below references `workspace`, which the ref migration also rebuilds —
		# by dropping its server default, which SQLite can only do by recreating the table.
		("workspace_member", {"workspace_id": workspace, "user_id": user, "role_id": role}),
		(
			"api_token",
			{
				"id": token,
				"workspace_id": workspace,
				"user_id": user,
				"title": "A token",
				"token_prefix": "sr_probe0",
				"token_hash": "0" * 64,
			},
		),
		(
			"calendar_feed",
			{
				"id": subroutine.db.types.new_uuid(),
				"workspace_id": workspace,
				# **Filed under a project rather than at the workspace**, so the nullable
				# foreign key is exercised rather than skipped: `project` is one of the tables
				# a SQLite rebuild recreates, and a row that left this null would reference
				# only `workspace` and cover half of what it is here for.
				"project_id": project,
				"owner_id": user,
				"audience": "everything",
				"title": "A calendar",
				"token_prefix": "cal0probe",
				"token_hash": "0" * 64,
			},
		),
		(
			"comment",
			{
				"workspace_id": workspace,
				"entity_type": "task",
				"entity_id": task,
				"author_id": user,
				"body": "A comment",
			},
		),
		(
			"event",
			{
				"workspace_id": workspace,
				"entity_type": "task",
				"entity_id": task,
				"action": "created",
				"actor_user_id": user,
				"actor_token_id": token,
			},
		),
		(
			"link_type",
			{
				"id": link_type,
				"workspace_id": workspace,
				"key": "blocks",
				"title": "Blocks",
				"inverse_title": "Blocked by",
			},
		),
		(
			"link",
			{
				"workspace_id": workspace,
				"link_type_id": link_type,
				"source_type": "task",
				"source_id": task,
				"target_type": "document",
				"target_id": document,
				"created_by": user,
			},
		),
		(
			"mention",
			{
				"workspace_id": workspace,
				"source_type": "task",
				"source_id": task,
				"target_type": "document",
				"target_id": document,
			},
		),
		(
			"verification",
			{
				"workspace_id": workspace,
				"task_id": task,
				"passed": True,
				"summary": "5,610 passed",
				"ran_at": subroutine.db.types.utcnow(),
				"created_by": user,
			},
		),
	)

	with engine.begin() as connection:
		for table_name, values in rows:
			_insert(connection, table_name, values)


#: The tables the ref migration rebuilds. A SQLite batch rebuild drops and recreates the
#: table, so every one of these needs referencing rows present for the test to mean anything.
REBUILT = ("workspace", "project", "task", "document")

#: What :func:`_populate` puts a row into. Declared as data so the guard below can compare
#: it against the schema rather than against a reading of the function.
SEEDED = frozenset(
	{
		"workspace",
		"user",
		"item_type",
		"status",
		"role",
		"tag",
		"project",
		"project_member",
		"task",
		"task_tag",
		"document",
		"document_tag",
		"workspace_member",
		"api_token",
		"calendar_feed",
		"comment",
		"event",
		"link_type",
		"link",
		"mention",
		"verification",
	}
)


def _referencing_tables () -> set[str]:
	"""Return every table holding a foreign key into one of :data:`REBUILT`."""

	return {
		name
		for name, table in subroutine.db.base.Base.metadata.tables.items()
		if {key.column.table.name for key in table.foreign_keys} & set(REBUILT)
	}


def test_the_seeded_data_covers_every_referencing_table () -> None:
	"""The guard on the guard: seeded coverage must keep up with the schema.

	Without this, :func:`_populate` is a hand-written list that silently stops covering the
	thing it was written for the next time a table gains a foreign key — which is exactly how
	its first version came to exercise one of four rebuilds while reading as though it
	covered them all.
	"""

	missing = sorted(_referencing_tables() - SEEDED)

	assert not missing, (
		f"These tables reference a rebuilt table but are not seeded by _populate: "
		f"{', '.join(missing)}. Add a row, or the migration test stops covering them."
	)


#: Columns whose CHECK constraint a generic filler value would violate.
VOCABULARY: dict[str, dict[str, typing.Any]] = {
	"item_type": {"entity_type": "task", "category": "work"},
	"link_type": {"category": "describing"},
	"status": {"entity_type": "task", "category": "todo"},
}


def _insert (
	connection: sqlalchemy.Connection, table_name: str, values: dict[str, typing.Any]
) -> None:
	"""Insert one row, filling in whatever else the table insists on.

	Through the table's own ``insert()`` rather than a ``text()`` statement, so every value
	passes through its column's type. Raw SQL has no type information to bind against, and
	pysqlite refuses a ``uuid.UUID`` outright — which is what the first version of this did.

	**Narrowed to the columns the database actually has** (`SR#1134`). Callers use this at an
	*older* revision — that is the whole point of ``test_an_absorbed_planned_day_…``, which
	downgrades, writes a row and upgrades — while the table here is the model's, which is
	today's. So every column added to a table this seeds broke that test, with a message about
	SQLite rather than about the fixture, and the breakage arrived one migration late.

	The model's table is still what builds the statement, because the types are the point; only
	the *set* of columns comes from the database.
	"""

	table = subroutine.db.base.Base.metadata.tables[table_name]
	present = {
		column["name"] for column in sqlalchemy.inspect(connection).get_columns(table_name)
	}
	row = dict(values)

	for column in table.columns:
		if column.name in row or column.nullable:
			continue

		if column.default is not None or column.server_default is not None:
			continue

		vocabulary = VOCABULARY.get(table_name, {})
		row[column.name] = vocabulary.get(column.name, _filler(column))

	connection.execute(
		sqlalchemy.insert(table).values(
			**{name: value for name, value in row.items() if name in present}
		)
	)


def _filler (column: sqlalchemy.Column[typing.Any]) -> typing.Any:
	"""A value that will satisfy one column's type, for a row nobody reads."""

	kind = str(column.type).upper()

	if "BOOL" in kind:
		return False

	if "JSON" in kind:
		return {}

	# Before the bare DATE test, since a datetime column renders as DATETIME.
	if "DATETIME" in kind or "TIMESTAMP" in kind:
		return datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

	if "DATE" in kind:
		return datetime.date(2026, 1, 1)

	if "INT" in kind or "NUMERIC" in kind or "FLOAT" in kind:
		return 1

	if "CHAR" in kind or "TEXT" in kind:
		return "x"

	return subroutine.db.types.new_uuid()


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_going_back_leaves_a_workspace_in_the_zone_it_was_inheriting (
	migrated_url: str,
) -> None:
	"""``233f898a2bee`` made ``workspace.timezone`` nullable so a workspace could inherit.

	Its downgrade has to put a value back, because the column stops allowing NULL — and it
	wrote the literal ``'UTC'``. So every workspace that was inheriting was silently re-zoned,
	by the operation whose whole job is putting things back, on any instance not in UTC. It
	reads ``instance.timezone`` now, before that column is dropped a few lines further down.

	**Seeded here rather than in ``_populate``**, because what makes the case is precisely a
	NULL beside an instance that is not UTC — the general fixture has no reason to carry one,
	and H-14 is the record of what a fixture that seeds no interesting value costs.
	"""

	engine = subroutine.db.session.create_engine(migrated_url)

	try:
		with engine.begin() as connection:
			_insert(
				connection,
				"instance",
				{
					"id": subroutine.db.types.new_uuid(),
					"name": "Test",
					"timezone": "Australia/Sydney",
				},
			)
			_insert(
				connection,
				"workspace",
				{
					"id": subroutine.db.types.new_uuid(),
					"slug": "inheriting",
					"title": "W",
					"timezone": None,
				},
			)

		# **To its parent, not to itself.** Downgrading *to* a revision runs the downgrades of
		# everything after it and stops — so naming this one would leave its own untouched,
		# which is a test that measures the revision above it.
		subroutine.db.migrate.downgrade(migrated_url, "ea3e86ad12c4")

		with engine.begin() as connection:
			zone = connection.execute(
				sqlalchemy.text("SELECT timezone FROM workspace WHERE slug = 'inheriting'")
			).scalar_one()

		assert zone == "Australia/Sydney", (
			f"the workspace was inheriting the instance's zone and came back as {zone!r}"
		)

	finally:
		engine.dispose()


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_going_back_past_a_reused_slug_says_what_is_in_the_way (
	migrated_url: str,
) -> None:
	"""``ea3e86ad12c4`` made the slug index partial, which is what lets a slug be reused.

	So an installation that deleted a workspace and made another with the same name has two
	rows the older schema cannot hold — and the downgrade met that as an integrity error
	naming a constraint, which tells an operator nothing about their own data.

	It refuses by name now, saying which slug and what to do. That is the honest answer: the
	old schema genuinely cannot represent this, and silently renaming somebody's workspace to
	make room would be a data change nobody asked for inside an operation meant to undo one.
	"""

	engine = subroutine.db.session.create_engine(migrated_url)

	try:
		with engine.begin() as connection:
			for deleted in (True, False):
				_insert(
					connection,
					"workspace",
					{
						"id": subroutine.db.types.new_uuid(),
						"slug": "reused",
						"title": "W",
						"deleted_at": (
							datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
							if deleted
							else None
						),
					},
				)

		with pytest.raises(RuntimeError, match="reused"):
			subroutine.db.migrate.downgrade(migrated_url, "2fee457e5b0b")

	finally:
		engine.dispose()


#: The revision before the actor columns stopped being foreign keys.
_BEFORE_DURABLE_ACTORS = "da7628199bff"


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_an_events_actor_survives_the_migration_that_makes_it_durable (
	migrated_url: str,
) -> None:
	"""`SR#672`. The rebuild must not renumber or lose what it is protecting.

	Dropping a constraint is an ordinary ``ALTER`` on PostgreSQL and a **copy-drop-rename** on
	SQLite, which cannot alter one in place. So the fix for a data-durability defect is itself
	the one shape of migration that can lose data — and ``event.seq`` is the primary key every
	client resumes a feed on, so renumbering would be worse than the defect: a caller asking
	*what is after 4,812* would silently skip or repeat.

	**Asserted against the rows rather than against the schema.** The generic history walk in
	this file proves a populated database still migrates; it reads no values back, so a rebuild
	that preserved the row count and scrambled the keys would pass it.

	**With a real user, because the downgrade puts the constraint back.** An actor that does not
	resolve is exactly what this migration makes possible and exactly what the old schema
	refuses, so a round trip cannot carry one — which is the migration's own docstring being
	true rather than a limitation of the test. The durability property is asserted next door,
	where it does not need to travel backwards.
	"""

	engine = subroutine.db.session.create_engine(migrated_url)

	try:
		with sqlalchemy.orm.Session(engine) as session:
			workspace = subroutine.db.models.identity.Workspace(slug="w", title="W")
			actor = subroutine.db.models.identity.User(username="u", username_normalized="u")

			session.add_all((workspace, actor))
			subroutine.db.seed.seed_workspace(session, workspace)
			session.flush()

			for which in range(4):
				session.add(
					subroutine.db.models.activity.Event(
						workspace_id=workspace.id,
						entity_type="task",
						entity_id=subroutine.db.types.new_uuid(),
						action="created",
						actor_user_id=actor.id,
						changes={"n": which},
					)
				)

			session.commit()

		before = _every_actor(engine)

		assert len(before) == 4, f"the fixture wrote {len(before)} events, not four"
		assert all(row[2] is not None for row in before), "no actor to preserve"

		# Down across the revision and back up, with the rows in place the whole way.
		subroutine.db.migrate.downgrade(migrated_url, _BEFORE_DURABLE_ACTORS)
		subroutine.db.migrate.upgrade(migrated_url)

		assert _every_actor(engine) == before, (
			"an event's seq or actor changed while the table was rebuilt"
		)

	finally:
		engine.dispose()


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_deleting_a_user_no_longer_erases_what_they_did (migrated_url: str) -> None:
	"""The defect itself, reproduced against the migrated schema — `SR#672`.

	Both actor columns were foreign keys with ``ON DELETE SET NULL``, so a hard delete rewrote
	every event that actor had ever written: retroactively, across the whole history, with
	nothing recording that it used to say more. A GDPR erasure **is** a hard user delete, and
	clearing out unused credentials is exactly the tidying nobody thinks of as destructive.

	**A hard delete rather than the soft one**, deliberately. ``User`` carries
	``SoftDeleteMixin``, so ordinary departure keeps the row — which is why this was latent, and
	why the erasure case is the one worth driving.

	It also fixes what ``NULL`` means. It stood for *either* a system action *or* somebody acted
	and the database forgot who; nothing nulls these now, so it means the first.
	"""

	engine = subroutine.db.session.create_engine(migrated_url)

	try:
		with sqlalchemy.orm.Session(engine) as session:
			workspace = subroutine.db.models.identity.Workspace(slug="w", title="W")
			actor = subroutine.db.models.identity.User(username="u", username_normalized="u")

			session.add_all((workspace, actor))
			subroutine.db.seed.seed_workspace(session, workspace)
			session.flush()

			who = actor.id

			session.add(
				subroutine.db.models.activity.Event(
					workspace_id=workspace.id,
					entity_type="task",
					entity_id=subroutine.db.types.new_uuid(),
					action="created",
					actor_user_id=who,
				)
			)
			session.commit()

			session.delete(actor)
			session.commit()

		assert [row[2] for row in _every_actor(engine)] == [who], (
			"the event's actor was erased by deleting the row it pointed at"
		)

	finally:
		engine.dispose()


def _every_actor (engine: sqlalchemy.engine.Engine) -> list[tuple[typing.Any, ...]]:
	"""Return every event's key and actor, oldest first."""

	table = subroutine.db.base.Base.metadata.tables["event"]

	with engine.connect() as connection:
		found = connection.execute(
			sqlalchemy.select(
				table.c.seq, table.c.id, table.c.actor_user_id, table.c.actor_token_id
			).order_by(table.c.seq.asc())
		)

		return [tuple(row) for row in found]


_BEFORE_BOTH_ENDS = "a1b3cef13c45"


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_a_link_event_written_before_the_second_subject_gains_the_far_end (
	migrated_url: str,
) -> None:
	"""`SR#302`'s backfill, and the half that has to be driven rather than read.

	The migration and ``links._far_end`` state one rule — *the end that is not the subject* —
	in SQL and in Python, which a backfill cannot avoid: it runs where the domain does not
	exist. So the guard is not that the two agree but that the SQL is **exercised in both
	directions**, because the ordinary link and the inverse one are the two branches of its
	``CASE`` and a fresh database only ever produces the first.

	**Inverting the ``CASE`` is the falsification**, and it fails both assertions below. With
	only the ordinary link here it would fail neither: subject and source coincide, so both
	branches return the target and the bug is invisible.
	"""

	engine = subroutine.db.session.create_engine(migrated_url)

	try:
		subroutine.db.migrate.downgrade(migrated_url, _BEFORE_BOTH_ENDS)

		workspace_id, kind_id = (subroutine.db.types.new_uuid() for _ in range(2))
		near, far, other = (subroutine.db.types.new_uuid() for _ in range(3))
		ordinary, inverse, subjectless = (subroutine.db.types.new_uuid() for _ in range(3))

		with engine.begin() as connection:
			_insert(connection, "workspace", {"id": workspace_id, "slug": "w", "title": "W"})
			_insert(
				connection,
				"link_type",
				{
					"id": kind_id,
					"workspace_id": workspace_id,
					"key": "blocks",
					"title": "Blocks",
					"inverse_title": "Blocked by",
					"category": "gating",
					"is_symmetric": False,
				},
			)

			# One row per link, because the backfill reads the **link** rather than `changes`
			# — a link is soft-deleted, so this is the fact that survives a withdrawal.
			for identifier, source, target in (
				(ordinary, near, far),
				(inverse, far, near),
				# Its own pair of items: the unique index is on the ends and the relation, so a
				# third link between the same two would be refused before it could be a fixture.
				(subjectless, near, other),
			):
				_insert(
					connection,
					"link",
					{
						"id": identifier,
						"workspace_id": workspace_id,
						"source_type": "task",
						"source_id": source,
						"target_type": "task",
						"target_id": target,
						"link_type_id": kind_id,
					},
				)

			# The ordinary shape: somebody stood on the source, so the far end is the target.
			_insert(
				connection,
				"event",
				{
					"workspace_id": workspace_id,
					"entity_type": "link",
					"entity_id": ordinary,
					"seq": 1,
					"subject_type": "task",
					"subject_id": near,
					"action": "created",
				},
			)

			# `SR#816`'s inversion, and the branch a fresh database never writes. The row is
			# stored `far blocks near` because a row has one direction; the person was on
			# `near`, which is the **target**, so the far end here is the *source*.
			_insert(
				connection,
				"event",
				{
					"workspace_id": workspace_id,
					"entity_type": "link",
					"entity_id": inverse,
					"seq": 2,
					"subject_type": "task",
					"subject_id": near,
					"action": "created",
				},
			)

			# Older than `SR#252`, so it has no subject at all. There is no *end that is not
			# the subject* to name, and the row reaches nobody as it stands — inventing one
			# would be a claim about what somebody did, made to hide what is already hidden.
			_insert(
				connection,
				"event",
				{
					"workspace_id": workspace_id,
					"entity_type": "link",
					"entity_id": subjectless,
					"seq": 3,
					"action": "created",
				},
			)

		subroutine.db.migrate.upgrade(migrated_url)

		(was_ordinary,) = _events_about(engine, ordinary, "subject_b_type", "subject_b_id")
		(was_inverse,) = _events_about(engine, inverse, "subject_b_type", "subject_b_id")
		(was_subjectless,) = _events_about(engine, subjectless, "subject_b_type", "subject_b_id")

		assert was_ordinary["subject_b_type"] == "task"
		assert was_ordinary["subject_b_id"] == far, (
			"the reader stood on the source, so the end they may not be entitled to is the target"
		)

		assert was_inverse["subject_b_id"] == far, (
			"the reader stood on the target of an inverse link, so the far end is the source — "
			"and the same `CASE` has to answer both"
		)

		assert was_subjectless["subject_b_type"] is None
		assert was_subjectless["subject_b_id"] is None

		# And back, because the columns are the only copy of this and a downgrade that leaves
		# them behind is not one.
		subroutine.db.migrate.downgrade(migrated_url, _BEFORE_BOTH_ENDS)

		with engine.connect() as connection:
			after = {
				column["name"]
				for column in sqlalchemy.inspect(connection).get_columns("event")
			}

		assert "subject_b_type" not in after and "subject_b_id" not in after

	finally:
		engine.dispose()
