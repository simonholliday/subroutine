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
import subroutine.errors


def _apply_sqlite_pragmas (connection: typing.Any, _record: typing.Any) -> None:
	"""Configure a freshly opened SQLite connection for safe concurrent use."""

	try:
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

	# **A damaged database fails here, and nothing else can close what it leaves open**
	# (`#228`). SQLite opens lazily, so the file's header is not read until
	# ``journal_mode=WAL`` touches it — and at that moment the driver's connection exists
	# while the pool has not yet recorded it, so ``engine.dispose()`` never sees it. Every
	# attempt to open a corrupt database therefore held a file handle until the process
	# ended; `db restore --recover` is the command most likely to meet one, and it opens the
	# database twice. Python 3.13 is the first to say so out loud, as a ResourceWarning at
	# collection, which is how this was found at all.
	except Exception:
		connection.close()
		raise


#: The two backends this is built and tested on (SPEC.md §10.3). Every test runs against both,
#: and the disagreements between them — NULL ordering, ``LIKE`` case sensitivity, ref
#: allocation under concurrency — are the reason the list is short and closed.
SUPPORTED_BACKENDS = ("sqlite", "postgresql")


def create_engine (
	database_url: str, *, echo: bool = False, **kwargs: typing.Any
) -> sqlalchemy.engine.Engine:
	"""Build an engine for ``database_url``, applying per-backend settings.

	**A backend we do not support is refused by name, before SQLAlchemy looks for a driver**
	(`#175`). ``database_url = "mysql://…"`` produced ``No module named 'MySQLdb'``, which
	invites an operator to go and install a driver for a database this does not support and
	cannot be made to — and then to meet a much stranger failure once they have.
	"""

	_refuse_an_unsupported_backend(database_url)

	try:
		engine = sqlalchemy.create_engine(database_url, echo=echo, future=True, **kwargs)

	except ModuleNotFoundError as missing:
		_refuse_a_backend_with_no_driver(database_url, missing)

	if engine.dialect.name == "sqlite":
		sqlalchemy.event.listen(engine, "connect", _apply_sqlite_pragmas)

	return engine


def _refuse_an_unsupported_backend (database_url: str) -> None:
	"""Refuse a URL naming a database this is not built for, saying which ones it is."""

	try:
		backend = sqlalchemy.engine.make_url(database_url).get_backend_name()

	# Broad on purpose: `make_url` raises several different types for different malformed
	# inputs, and none of them is this function's subject. A URL that cannot be parsed at all
	# is left to `create_engine`, whose message about it is already the better one.
	except Exception:
		return

	if backend in SUPPORTED_BACKENDS:
		return

	raise subroutine.errors.ValidationError(
		f"{backend!r} is not a database Subroutine can use.",
		hint=(
			f"It runs on {' and '.join(SUPPORTED_BACKENDS)}. Set 'database_url' to a "
			f"'sqlite:///…' path or a 'postgresql+psycopg://…' URL — "
			f"'subroutine config show' says where the file is."
		),
	)


def _refuse_a_backend_with_no_driver (
	database_url: str, missing: ModuleNotFoundError
) -> typing.NoReturn:
	"""Refuse a supported backend whose driver was never installed, naming what installs it.

	`#927`'s H-20, and the sibling of :func:`_refuse_an_unsupported_backend` one step along:
	that one is a database this cannot use, and this is one it can, on a machine that did not
	take the extra. PostgreSQL is optional precisely so a person keeping a shopping list is not
	made to install a database driver, so meeting this is an ordinary state rather than a rare
	one — anyone who edits ``database_url`` before running ``pip install 'subroutine[postgres]'``.

	**Untranslated it reads as a bug in this program.** ``ModuleNotFoundError`` escaped
	``clients/local.Client.__init__``, which builds its engine outside every guard, so
	``subroutine list`` answered *"Something went wrong that should not have… please report it
	at github.com"* and wrote a crash file. ``serve`` printed ``Serving on …`` and *then* died,
	and ``doctor`` showed the raw exception. Three surfaces, none naming the one line in the
	README that fixes it.

	Raised as a ``SubroutineError`` so it travels: ``fanout._attempt`` catches only those, so
	this reaches an operator as one connection failing rather than as the whole agenda dying.
	"""

	backend = sqlalchemy.engine.make_url(database_url).get_backend_name()
	extra = _EXTRA_FOR.get(backend, backend)

	raise subroutine.errors.ServiceUnavailable(
		f"This installation has no {backend} driver: {missing.name} is not installed.",
		hint=(
			f"Install it with \"pip install 'subroutine[{extra}]'\", or point 'database_url' "
			f"at a 'sqlite:///…' path. 'subroutine config show' says where that setting is."
		),
	)


#: Which optional dependency supplies each backend's driver, for the refusal above. Keyed on
#: the backend rather than on the module, because the module is what is *missing* and the
#: extra is what an operator types.
_EXTRA_FOR = {"postgresql": "postgres"}


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
