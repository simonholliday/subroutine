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

import conftest
import subroutine.db.base
import subroutine.db.migrate
import subroutine.db.models
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
			{"id": item_type, "workspace_id": workspace, "key": "task", "label": "Task"},
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
				"key": "TOP",
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
		"comment",
		"event",
		"link_type",
		"link",
		"mention",
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
	"item_type": {"entity_type": "task"},
	"status": {"entity_type": "task", "category": "todo"},
}


def _insert (
	connection: sqlalchemy.Connection, table_name: str, values: dict[str, typing.Any]
) -> None:
	"""Insert one row, filling in whatever else the table insists on.

	Through the table's own ``insert()`` rather than a ``text()`` statement, so every value
	passes through its column's type. Raw SQL has no type information to bind against, and
	pysqlite refuses a ``uuid.UUID`` outright — which is what the first version of this did.
	"""

	table = subroutine.db.base.Base.metadata.tables[table_name]
	row = dict(values)

	for column in table.columns:
		if column.name in row or column.nullable:
			continue

		if column.default is not None or column.server_default is not None:
			continue

		vocabulary = VOCABULARY.get(table_name, {})
		row[column.name] = vocabulary.get(column.name, _filler(column))

	connection.execute(sqlalchemy.insert(table).values(**row))


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
