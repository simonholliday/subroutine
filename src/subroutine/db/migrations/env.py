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

	# SQLite cannot alter most things in place, so ``render_as_batch`` below makes Alembic
	# rebuild the table instead: create a copy, move the rows, drop the original, rename.
	# That drop is a foreign-key violation the moment any *other* table holds a row
	# pointing at it — so migrating a database with data in it fails on a constraint that
	# the migration is not actually breaking. Enforcement is therefore off for the
	# duration, which is what Alembic's own batch-mode documentation calls for.
	#
	# It has to be issued before Alembic opens a transaction, because SQLite ignores this
	# pragma inside one. The connection is discarded when the migration finishes and every
	# application connection turns enforcement back on for itself (``db/session.py``), so
	# this cannot leak into normal use.
	sqlite = connection.dialect.name == "sqlite"

	if sqlite:
		_sqlite_foreign_keys(connection, False)

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

	# Restored rather than left off, because a caller may have handed us a connection it
	# intends to go on using — the test suite does exactly that.
	if sqlite:
		_sqlite_foreign_keys(connection, True)


def _sqlite_foreign_keys (connection: sqlalchemy.Connection, enabled: bool) -> None:
	"""Turn SQLite's foreign-key enforcement on or off for this connection.

	Issued against the driver's own connection rather than through SQLAlchemy, which is
	not fussiness. ``Connection.exec_driver_sql`` opens a SQLAlchemy transaction, and
	Alembic then finds one already in progress, makes its own ``begin_transaction`` a
	no-op, and commits nothing — so the migration runs, reports success and is rolled back
	when the connection closes. Found by migrating a database twice and watching the
	second run start from the first revision again.
	"""

	setting = "ON" if enabled else "OFF"
	driver: typing.Any = connection.connection.driver_connection

	driver.execute(f"PRAGMA foreign_keys={setting}")


if alembic.context.is_offline_mode():
	run_migrations_offline()

else:
	run_migrations_online()
