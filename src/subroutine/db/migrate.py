"""Running and checking migrations, from the CLI or from a test.

Schema changes always go through Alembic. ``create_all`` exists for tests only: an
installation with real data in it needs to be upgraded, not recreated, and the moment
the two ways of building a schema can disagree, one of them is wrong and nobody knows
which.
"""

import pathlib
import re
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

	Anything in the returned list means someone changed a model without writing the
	migration to match — the drift that turns a routine deployment into an outage, and the
	reason this is checked in CI rather than remembered.

	**An empty list does not mean the schemas are identical.** Alembic's autogenerate
	compares tables, columns, types, indexes and foreign keys; it does **not** compare
	CHECK constraints. So changing the vocabulary inside an ``enum_check`` — adding a
	status category, say — is invisible here. Worse than invisible: the test suite builds
	its schema with ``create_all`` straight from the models, so the new vocabulary is
	enforced in every test and reported as zero drift by this function, while every
	migrated database, fresh installs included, still carries the old CHECK.

	:func:`check_constraint_differences` covers that gap and is asserted alongside this.
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


def check_constraint_differences (engine: sqlalchemy.engine.Engine) -> list[str]:
	"""Return the CHECK constraints where the models and the live schema disagree.

	Exists because :func:`schema_differences` cannot see them, and because CHECK
	constraints here are not decoration: they carry the status categories, the entity-type
	vocabularies and the importance range. A model that has moved on from its migration
	means a database that accepts values the code believes are impossible.

	Constraints are matched by name, and their *values* compared rather than their text.
	Comparing text is not possible across backends: PostgreSQL rewrites ``x IN ('a', 'b')``
	into ``x = ANY (ARRAY['a', 'b'])`` when it stores the constraint, so a verbatim
	comparison would report drift on every table on every run.

	What is compared is the multiset of literals — quoted strings and numbers — which both
	backends preserve exactly. That covers the constraints where drift actually costs
	something: the status categories, the entity-type vocabularies, the importance and
	urgency ranges, the instance singleton. It does *not* notice a restructured boolean
	expression containing no literals, such as ``ck_link_not_self``; those are compared on
	presence alone.
	"""

	inspector = sqlalchemy.inspect(engine)
	differences: list[str] = []

	for name, table in sorted(subroutine.db.base.Base.metadata.tables.items()):
		if name not in inspector.get_table_names():
			continue

		declared = {
			str(constraint.name): _check_literals(str(constraint.sqltext))
			for constraint in table.constraints
			if isinstance(constraint, sqlalchemy.CheckConstraint) and constraint.name is not None
		}
		reflected = {
			str(found["name"]): _check_literals(str(found["sqltext"]))
			for found in inspector.get_check_constraints(name)
			if found.get("name")
		}

		for constraint_name in sorted(set(declared) | set(reflected)):
			in_models = declared.get(constraint_name)
			in_database = reflected.get(constraint_name)

			if in_models is None:
				differences.append(f"{name}.{constraint_name}: in the database, not in the models")

			elif in_database is None:
				differences.append(f"{name}.{constraint_name}: in the models, not in the database")

			elif in_models != in_database:
				differences.append(
					f"{name}.{constraint_name}: models allow {list(in_models)}, "
					f"database allows {list(in_database)}"
				)

	return differences


def _check_literals (text: str) -> tuple[str, ...]:
	"""Return the literal values a CHECK expression constrains against, in a stable order.

	Quoted strings and bare numbers only. Keywords, operators, casts and parenthesisation
	are all discarded, because those are exactly what the two backends spell differently.
	"""

	quoted = re.findall(r"'([^']*)'", text)
	numbers = re.findall(r"(?<![\w'])(\d+)(?![\w'])", text)

	return tuple(sorted(quoted)) + tuple(sorted(numbers))


def _include_object (
	obj: typing.Any, name: str | None, type_: str, reflected: bool, compare_to: typing.Any
) -> bool:
	"""Exclude Alembic's own bookkeeping table from comparison."""

	return not (type_ == "table" and name == "alembic_version")
