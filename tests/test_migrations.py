"""Tests that the migrations build the schema the models describe, on both backends.

The drift check is the most important test in this file, and arguably in the project.
Without it, a model can be changed without its migration and nothing complains until a
real installation is upgraded and the schema no longer matches the code that queries it.
"""

import pathlib
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.engine

import subroutine.db.base
import subroutine.db.migrate
import subroutine.db.session


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

		url = str(sqlalchemy.engine.make_url(admin_url).set(database=database))

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
