"""Tests that the migrations build the schema the models describe, on both backends.

The drift check is the most important test in this file, and arguably in the project.
Without it, a model can be changed without its migration and nothing complains until a
real installation is upgraded and the schema no longer matches the code that queries it.
"""

import datetime
import pathlib
import typing
import uuid

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


def _populate (engine: sqlalchemy.engine.Engine) -> None:
	"""Insert one row into every table that something else references.

	Built from the models rather than written out, so a column added later does not turn
	this into a guessing game — and deliberately minimal, because what matters is that the
	referencing rows *exist*, not what they say.
	"""

	workspace = subroutine.db.types.new_uuid()
	project = subroutine.db.types.new_uuid()
	item_type = subroutine.db.types.new_uuid()
	status = subroutine.db.types.new_uuid()

	rows: tuple[tuple[str, dict[str, typing.Any]], ...] = (
		("workspace", {"id": workspace, "slug": "w", "title": "W"}),
		(
			"item_type",
			{"id": item_type, "workspace_id": workspace, "key": "task", "label": "Task"},
		),
		("status", {"id": status, "workspace_id": workspace, "key": "open", "label": "Open"}),
		(
			"project",
			{
				"id": project,
				"workspace_id": workspace,
				"key": "SR",
				"title": "Work",
				"status_id": status,
				"visibility": "public",
			},
		),
		(
			"task",
			{
				"workspace_id": workspace,
				"project_id": project,
				"type_id": item_type,
				"status_id": status,
				"ref": 1,
				"title": "A task",
			},
		),
		(
			"document",
			{
				"workspace_id": workspace,
				"project_id": project,
				"type_id": item_type,
				"status_id": status,
				"ref": 2,
				"title": "A document",
			},
		),
	)

	with engine.begin() as connection:
		for table_name, values in rows:
			_insert(connection, table_name, values)


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
