"""Running and checking migrations, from the CLI or from a test.

Schema changes always go through Alembic. ``create_all`` exists for tests only: an
installation with real data in it needs to be upgraded, not recreated, and the moment
the two ways of building a schema can disagree, one of them is wrong and nobody knows
which.
"""

import pathlib
import typing

import alembic.autogenerate
import alembic.command
import alembic.config
import alembic.migration
import alembic.runtime.migration
import alembic.script
import sqlalchemy
import sqlalchemy.engine

import subroutine.db.base
import subroutine.db.models

#: Ships inside the package so that migrations are available from an installed wheel,
#: not only from a source checkout.
MIGRATIONS_DIRECTORY = pathlib.Path(__file__).parent / "migrations"


def build_config (database_url: str) -> alembic.config.Config:
	"""Return an Alembic configuration pointed at ``database_url``."""

	config = alembic.config.Config()
	config.set_main_option("script_location", str(MIGRATIONS_DIRECTORY))
	config.set_main_option("sqlalchemy.url", database_url)

	return config


def upgrade (database_url: str, revision: str = "head") -> None:
	"""Bring ``database_url`` up to ``revision``, creating the schema if it is empty."""

	alembic.command.upgrade(build_config(database_url), revision)


def downgrade (database_url: str, revision: str) -> None:
	"""Step ``database_url`` back to ``revision``."""

	alembic.command.downgrade(build_config(database_url), revision)


def current_revision (engine: sqlalchemy.engine.Engine) -> str | None:
	"""Report which migration a database is at, or ``None`` if it has never been run."""

	with engine.connect() as connection:
		context = alembic.migration.MigrationContext.configure(connection)

		return context.get_current_revision()


def head_revision () -> str | None:
	"""Report the newest migration available in the package."""

	config = build_config("sqlite://")
	script = alembic.script.ScriptDirectory.from_config(config)

	return script.get_current_head()


def is_up_to_date (engine: sqlalchemy.engine.Engine) -> bool:
	"""Report whether a database has every available migration applied."""

	return current_revision(engine) == head_revision()


def schema_differences (engine: sqlalchemy.engine.Engine) -> list[typing.Any]:
	"""Return the differences between the models and the live schema.

	An empty list means the migrations and the models agree. Anything else means someone
	changed a model without writing the migration to match — the drift that turns a
	routine deployment into an outage, and the reason this is checked in CI rather than
	remembered.
	"""

	with engine.connect() as connection:
		context = alembic.migration.MigrationContext.configure(
			connection,
			opts={
				"compare_type": True,
				"target_metadata": subroutine.db.base.Base.metadata,
				"include_object": _include_object,
			},
		)

		return list(
			alembic.autogenerate.compare_metadata(context, subroutine.db.base.Base.metadata)
		)


def _include_object (
	obj: typing.Any, name: str | None, type_: str, reflected: bool, compare_to: typing.Any
) -> bool:
	"""Exclude Alembic's own bookkeeping table from comparison."""

	return not (type_ == "table" and name == "alembic_version")
