"""Shared fixtures, chiefly the engine fixture that runs every test on both backends.

The dual-backend rule is not a nicety. SQLite has a single writer and no timezone-aware
storage, so it cannot express the failures that matter most — ordering of concurrent
inserts, NULL sort position, case sensitivity in ``LIKE``, ref allocation under
contention. A test that runs only on SQLite is a test that agrees with itself.
"""

import os
import pathlib
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.orm

import sample_models
import subroutine.config
import subroutine.db.migrate
import subroutine.db.session

#: Connects to the maintenance database so the throwaway test database can be created.
#: Override to point the suite at a different server.
POSTGRES_ADMIN_URL = os.environ.get(
	"SUBROUTINE_TEST_POSTGRES_ADMIN_URL", "postgresql+psycopg:///postgres"
)

#: A fresh name per test session, because the fixture below **drops** the database it is
#: about to use. A constant meant two pytest processes on one machine destroyed each other's
#: schema mid-run, and the failure surfaced as unrelated tests raising `relation "sample_row"
#: does not exist` or `database "subroutine_test" is being accessed by other users` — no hint
#: of the cause, and about the machine rather than the code. It cost this project three
#: separate false alarms, one of them mid-review. `test_migrations` already did this.
TEST_DATABASE_NAME = f"subroutine_test_{uuid.uuid4().hex[:12]}"

#: Turns an unreachable PostgreSQL from a skip into a failure. Set in CI, and the single
#: most important line in this file: without it, a runner whose database service failed to
#: start would run half the suite and report success, which is precisely the state the
#: dual-backend rule exists to prevent. A skip is a courtesy to someone working locally,
#: not something the build should ever be allowed to do quietly.
REQUIRE_POSTGRES = os.environ.get("SUBROUTINE_TEST_REQUIRE_POSTGRES", "").strip().lower() in {
	"1",
	"true",
	"yes",
	"on",
}


@pytest.fixture(autouse=True)
def _no_inherited_installation (
	tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Give every test its own empty XDG home, so none of them can read the real one.

	**Found by doing it.** A `backup_directory` was configured on this machine, pointing at a
	network volume, and the next test run wrote two backups *of the test database* into it —
	named identically to real ones, and distinguishable only by size. The test that did it
	patched ``XDG_DATA_HOME`` and not ``XDG_CONFIG_HOME``, so it took its own database
	directory and the developer's `config.toml`.

	The general rule is the point rather than that one fixture: a test must not read the
	configuration of the machine it happens to be running on, in either direction. Reading it
	lets a developer's settings change what the suite does, and lets the suite change the
	developer's data.

	``SUBROUTINE_TEST_*`` is left alone — those configure the harness, not the product.
	"""

	root = tmp_path_factory.mktemp("xdg")

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(root / variable.lower()))

	for name in list(os.environ):
		if name.startswith("SUBROUTINE_") and not name.startswith("SUBROUTINE_TEST_"):
			monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_inherited_directory (
	tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Run every test somewhere with no `.subroutine` marker above it (§13.7a).

	**Found by doing it, like its sibling above.** The suite ran from the project root, and the
	day this repository started carrying its own marker — naming a workspace that exists on the
	developer's instance and on none of the temporary ones — **154 tests failed at once**. The
	bug that caused it was real and is `#166`; this is the reason the suite had no opinion about
	it either way.

	A test whose result depends on the directory pytest was started in is a test that passes on
	one machine and fails on another, and the failure says nothing about the cause. `tmp_path`
	rather than the working tree, for the reason every other path here is: this share cannot
	give SQLite a lock.
	"""

	monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))


@pytest.fixture(autouse=True)
def _no_inherited_profile () -> typing.Iterator[None]:
	"""Make every test start on the default instance, whatever ran before it.

	``--profile`` works by *exporting* ``SUBROUTINE_PROFILE`` so that anything the process
	starts inherits the same instance (SPEC.md §12.5). In one pytest process that means a
	command-line test can leave the variable set, and the next test would then read and write a
	different database than the one it built — with a symptom (an empty listing, a missing row)
	that says nothing at all about the cause.

	Autouse and unconditional, because the tests that need to *notice* a leak are exactly the
	ones least likely to be looking for it.
	"""

	before = os.environ.get(subroutine.config.PROFILE_VARIABLE)
	os.environ.pop(subroutine.config.PROFILE_VARIABLE, None)

	yield

	if before is None:
		os.environ.pop(subroutine.config.PROFILE_VARIABLE, None)

	else:
		os.environ[subroutine.config.PROFILE_VARIABLE] = before


def with_database (url: str | sqlalchemy.engine.URL, name: str) -> str:
	"""Return ``url`` pointed at a different database, with its password intact.

	Exists because of a trap that cost a red CI run: **``str()`` on a SQLAlchemy ``URL``
	renders the password as ``***``**. It is a deliberate courtesy for logs, and it turns a
	derived URL into one that authenticates as the literal password ``***``. Nothing
	catches it locally, where the admin URL is ``postgresql+psycopg:///postgres`` and peer
	authentication over the Unix socket means there is no password to mask — so the bug
	appears only where a password is actually used, which is every environment except a
	developer's laptop.

	``render_as_string(hide_password=False)`` is the round-trippable form, and it should be
	used every time a URL is turned back into a string to connect with.
	"""

	parsed = url if isinstance(url, sqlalchemy.engine.URL) else sqlalchemy.engine.make_url(url)

	return parsed.set(database=name).render_as_string(hide_password=False)


def _postgres_url () -> str:
	"""Return the URL of the throwaway test database."""

	return with_database(POSTGRES_ADMIN_URL, TEST_DATABASE_NAME)


def _postgres_unavailable_reason () -> str | None:
	"""Return why PostgreSQL cannot be used, or ``None`` when it can."""

	engine = sqlalchemy.create_engine(POSTGRES_ADMIN_URL)

	try:
		with engine.connect() as connection:
			connection.execute(sqlalchemy.text("SELECT 1"))

	except Exception as error:
		return f"PostgreSQL is not reachable at {POSTGRES_ADMIN_URL}: {error}"

	finally:
		engine.dispose()

	return None


@pytest.fixture(scope="session")
def postgres_url () -> typing.Iterator[str]:
	"""Create a throwaway PostgreSQL database for the test session, and drop it after."""

	reason = _postgres_unavailable_reason()

	if reason is not None:
		if REQUIRE_POSTGRES:
			pytest.fail(
				f"{reason}\n\nSUBROUTINE_TEST_REQUIRE_POSTGRES is set, so a missing "
				f"PostgreSQL fails the run rather than halving it."
			)

		pytest.skip(reason)

	admin_engine = sqlalchemy.create_engine(POSTGRES_ADMIN_URL, isolation_level="AUTOCOMMIT")

	with admin_engine.connect() as connection:
		connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{TEST_DATABASE_NAME}"'))
		connection.execute(sqlalchemy.text(f'CREATE DATABASE "{TEST_DATABASE_NAME}"'))

	yield _postgres_url()

	with admin_engine.connect() as connection:
		connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{TEST_DATABASE_NAME}"'))

	admin_engine.dispose()


@pytest.fixture(scope="session")
def sqlite_url (tmp_path_factory: pytest.TempPathFactory) -> str:
	"""Return a SQLite URL under a temporary directory on local disk.

	Never inside the working tree: this repository lives on a network share where SQLite
	cannot take a lock.
	"""

	directory: pathlib.Path = tmp_path_factory.mktemp("sqlite")

	return f"sqlite:///{directory / 'test.db'}"


@pytest.fixture(scope="session", params=["sqlite", "postgresql"])
def engine (request: pytest.FixtureRequest) -> typing.Iterator[sqlalchemy.engine.Engine]:
	"""Yield an engine for each supported backend in turn, with the schema created."""

	if request.param == "sqlite":
		url = request.getfixturevalue("sqlite_url")

	else:
		url = request.getfixturevalue("postgres_url")

	engine = subroutine.db.session.create_engine(url)

	subroutine.db.session.create_all(engine)
	sample_models.SampleBase.metadata.create_all(engine)

	# The schema comes from the models, but a real installation's comes from Alembic and
	# says so in `alembic_version` — which the readiness check reads. Stamping makes a test
	# database describe itself the way a real one does, and the claim is honest because
	# `test_migrations` asserts the models and the head migration agree.
	subroutine.db.migrate.stamp(url)

	yield engine

	sample_models.SampleBase.metadata.drop_all(engine)
	subroutine.db.session.drop_all(engine)

	with engine.begin() as connection:
		connection.execute(sqlalchemy.text("DROP TABLE IF EXISTS alembic_version"))

	engine.dispose()


@pytest.fixture
def session (
	engine: sqlalchemy.engine.Engine,
) -> typing.Iterator[sqlalchemy.orm.Session]:
	"""Yield a session whose work is rolled back afterwards, leaving no residue.

	Each test runs inside one outer transaction that is never committed, so tests stay
	independent without paying to recreate the schema between them.
	"""

	connection = engine.connect()
	transaction = connection.begin()

	# `create_savepoint` keeps the session from taking ownership of the outer
	# transaction, so a commit inside a service under test is contained and the rollback
	# below still discards everything.
	factory = sqlalchemy.orm.sessionmaker(
		bind=connection,
		expire_on_commit=False,
		future=True,
		join_transaction_mode="create_savepoint",
	)
	db_session = factory()

	try:
		yield db_session

	finally:
		db_session.close()

		if transaction.is_active:
			transaction.rollback()

		connection.close()
