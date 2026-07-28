"""Alembic environment.

Two settings here matter more than the rest. ``render_as_batch`` makes SQLite migrations
possible at all — it cannot alter or drop most constraints in place, so Alembic rebuilds
the table instead, which needs every constraint to have a name (see ``db/base.py``).
``compare_type`` makes autogenerate notice a changed column type rather than silently
producing an empty migration.
"""

import logging.config
import typing

import alembic.context
import sqlalchemy

import subroutine.config
import subroutine.db.base
import subroutine.db.models
import subroutine.db.session

config = alembic.context.config

if config.config_file_name is not None:
	logging.config.fileConfig(config.config_file_name)

#: Populated by importing ``subroutine.db.models``; that import is what makes every
#: table visible to autogenerate.
target_metadata = subroutine.db.base.Base.metadata


def database_url () -> str:
	"""Return the database to migrate, preferring an explicitly configured one."""

	configured = config.get_main_option("sqlalchemy.url", None)

	if configured:
		return configured

	return subroutine.config.load_settings().database_url


def include_object (
	obj: typing.Any, name: str | None, type_: str, reflected: bool, compare_to: typing.Any
) -> bool:
	"""Keep autogenerate focused on tables this project owns."""

	# Alembic's own bookkeeping table is not part of the schema being managed.
	return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline () -> None:
	"""Emit SQL for the migration without connecting to a database."""

	alembic.context.configure(
		url=database_url(),
		target_metadata=target_metadata,
		literal_binds=True,
		dialect_opts={"paramstyle": "named"},
		compare_type=True,
		include_object=include_object,
		sqlalchemy_module_prefix="sqlalchemy.",
	)

	with alembic.context.begin_transaction():
		alembic.context.run_migrations()


def run_migrations_online () -> None:
	"""Apply the migration against a live database."""

	connectable = config.attributes.get("connection", None)

	if connectable is None:
		engine = subroutine.db.session.create_engine(database_url())

		with engine.connect() as connection:
			_run_with_connection(connection)

		engine.dispose()

	else:
		_run_with_connection(connectable)


def _run_with_connection (connection: sqlalchemy.Connection) -> None:
	"""Configure Alembic for ``connection`` and run the migration."""

	alembic.context.configure(
		connection=connection,
		target_metadata=target_metadata,
		compare_type=True,
		include_object=include_object,
		render_as_batch=connection.dialect.name == "sqlite",
		# Spell SQLAlchemy out in full; leave Alembic's `op.` alone, since
		# `batch_alter_table` emits it regardless of this setting.
		sqlalchemy_module_prefix="sqlalchemy.",
	)

	with alembic.context.begin_transaction():
		alembic.context.run_migrations()


if alembic.context.is_offline_mode():
	run_migrations_offline()

else:
	run_migrations_online()
