"""Engine and session construction, including the SQLite settings that make it safe.

SQLite's defaults are wrong for a service. Foreign keys are not enforced unless asked
for, the default journal blocks readers behind a writer, and a busy database raises
immediately instead of waiting. All three are fixed per connection here, because a
setting applied once at startup does not reach connections the pool opens later.
"""

import contextlib
import typing

import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.event
import sqlalchemy.orm

import subroutine.db.base


def _apply_sqlite_pragmas (connection: typing.Any, _record: typing.Any) -> None:
	"""Configure a freshly opened SQLite connection for safe concurrent use."""

	cursor = connection.cursor()

	try:
		# Without this, every foreign key in the schema is decorative.
		cursor.execute("PRAGMA foreign_keys=ON")

		# Write-ahead logging lets readers carry on while a write is in progress.
		cursor.execute("PRAGMA journal_mode=WAL")

		# Safe under WAL, and markedly faster than the default.
		cursor.execute("PRAGMA synchronous=NORMAL")

		# Wait for a contended lock rather than failing the request outright.
		cursor.execute("PRAGMA busy_timeout=5000")

	finally:
		cursor.close()


def create_engine (
	database_url: str, *, echo: bool = False, **kwargs: typing.Any
) -> sqlalchemy.engine.Engine:
	"""Build an engine for ``database_url``, applying per-backend settings."""

	engine = sqlalchemy.create_engine(database_url, echo=echo, future=True, **kwargs)

	if engine.dialect.name == "sqlite":
		sqlalchemy.event.listen(engine, "connect", _apply_sqlite_pragmas)

	return engine


def create_session_factory (
	engine: sqlalchemy.engine.Engine,
) -> sqlalchemy.orm.sessionmaker[sqlalchemy.orm.Session]:
	"""Build a session factory bound to ``engine``.

	``expire_on_commit`` is off so that an object stays readable after the transaction
	that wrote it commits — otherwise every service would have to re-read what it just
	created in order to return it.
	"""

	return sqlalchemy.orm.sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextlib.contextmanager
def session_scope (
	factory: sqlalchemy.orm.sessionmaker[sqlalchemy.orm.Session],
) -> typing.Iterator[sqlalchemy.orm.Session]:
	"""Run a unit of work in one transaction, committing on success.

	Anything raised inside the block rolls the whole transaction back, which is what
	keeps the rule that a mutation and the event recording it either both happen or
	neither does.
	"""

	session = factory()

	try:
		yield session
		session.commit()

	except Exception:
		session.rollback()
		raise

	finally:
		session.close()


def create_all (engine: sqlalchemy.engine.Engine) -> None:
	"""Create every table directly from the models.

	For tests only. Real schema changes go through Alembic, so that an installation with
	data in it can be upgraded rather than recreated.
	"""

	subroutine.db.base.Base.metadata.create_all(engine)


def drop_all (engine: sqlalchemy.engine.Engine) -> None:
	"""Drop every table. For tests only."""

	subroutine.db.base.Base.metadata.drop_all(engine)
