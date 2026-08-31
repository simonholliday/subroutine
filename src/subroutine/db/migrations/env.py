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
import subroutine.db.integrity
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

	# Alembic's documented way for a host application to migrate on a connection it already
	# holds. Nothing in this project does that today — it is here because ``alembic`` itself
	# supports it and a future embedded caller would arrive through it, not because a caller
	# exists. Do not write a comment elsewhere that claims one does.
	connectable = config.attributes.get("connection", None)

	if connectable is not None:
		_run_with_connection(connectable)

		return

	engine = subroutine.db.session.create_engine(database_url())

	# Disposed in a ``finally`` because the pool is what makes the pragma below safe: a
	# physical connection carries its foreign-key setting until it is closed, so an engine
	# that outlives a *failed* migration is an engine holding a connection with enforcement
	# off.
	try:
		with engine.connect() as connection:
			_run_with_connection(connection)

	finally:
		engine.dispose()


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
	# pragma inside one.
	#
	# **The restore below is the only thing that turns it back on, and it is not optional.**
	# ``db/session.py`` applies its pragmas on the ``connect`` event, which fires once per
	# *physical* connection — not on checkout from the pool. Measured: the same DBAPI
	# connection handed out twice reports ``foreign_keys = 1`` then ``0``, and only
	# ``engine.dispose()`` gets a fresh one. So "the application will turn it back on for
	# itself" is false, and an earlier version of this comment said it.
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

	try:
		with alembic.context.begin_transaction():
			# What is already broken, so that a database some earlier migration damaged can
			# still be migrated — including upwards, towards the version that stops it
			# happening again. Refusing on the total would strand exactly those people.
			before = subroutine.db.integrity.dangling_references(connection)

			alembic.context.run_migrations()

			broken = subroutine.db.integrity.appeared(
				before, subroutine.db.integrity.dangling_references(connection))

			# **This reports; it does not prevent, and saying so is the point.** Measured:
			# Alembic's ``begin_transaction`` is a no-op on SQLite, and wrapping the run in a
			# real transaction does not help either — the writes and the version bump both
			# survive an exception. So the only thing that can stop a migration breaking a
			# reference is that migration refusing before it changes anything, which is what
			# ``9c41d0b7ae52`` and ``a986838fadc4`` do. This is the net beneath them, and what
			# it buys is that the damage is named at the moment it happens rather than found
			# later by somebody wondering why an item has no type (`SR#1689`).
			if broken:
				raise RuntimeError(
					f"This database now holds {subroutine.db.integrity.in_words(broken)}."
					f" Restore the backup taken before this ran."
				)

	finally:
		# In a ``finally`` because a migration that raises must not leave enforcement off on
		# a connection that goes back into a pool. The failure would be silent and would
		# outlive the thing that caused it.
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
