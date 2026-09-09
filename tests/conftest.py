"""Shared fixtures, chiefly the engine fixture that runs every test on both backends.

The dual-backend rule is not a nicety. SQLite has a single writer and no timezone-aware
storage, so it cannot express the failures that matter most — ordering of concurrent
inserts, NULL sort position, case sensitivity in ``LIKE``, ref allocation under
contention. A test that runs only on SQLite is a test that agrees with itself.
"""

import datetime
import os
import pathlib
import re
import sys
import typing
import uuid

import pytest
import rich.console
import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.orm
import typer.rich_utils

import instance_templates
import sample_models
import subroutine.cli.main
import subroutine.config
import subroutine.connections
import subroutine.db.migrate
import subroutine.db.session
import subroutine.installations

#: Connects to the maintenance database so the throwaway test database can be created.
#: Override to point the suite at a different server.
POSTGRES_ADMIN_URL = os.environ.get(
	"SUBROUTINE_TEST_POSTGRES_ADMIN_URL", "postgresql+psycopg:///postgres"
)

#: What every throwaway database this suite makes is called first. One constant rather than a
#: literal in several places, because a prefix that has to agree with itself is a duplicated
#: rule waiting to disagree — and this one had already grown five spellings before `#1667`.
THROWAWAY_PREFIX = "subroutine_test_"

#: The prefixes four other fixtures wrote their own names under, before the shape moved into
#: :func:`throwaway_name`. **Nothing generates one now**, so the sweep reports anything found
#: under them and never drops it: they carry no time and their age cannot be established. The
#: entry goes away when nobody has one left, which is the question every allow-list here is
#: asked.
#:
#: They are listed at all because the leak was never one family. `#1667` counted 87 under the
#: prefix above; a `psql -l` on 2026-09-09 found five more, from six other fixtures and the same
#: mechanism — a per-run name that only teardown drops.
#:
#: **Six, and the first search found four.** Grepping for the prefixes that had *leaked* finds
#: the sites that happened to be interrupted, not the sites there are; grepping for
#: `CREATE DATABASE` finds all seven, which is what `test_every_database_this_suite_makes_is_named_by_one_function`
#: now holds. The four-entry version of this list read as complete.
RETIRED_PREFIXES: tuple[str, ...] = (
	"subroutine_changes_",
	"subroutine_copy_",
	"subroutine_cost_",
	"subroutine_lastused_",
	"subroutine_mig_",
	"subroutine_restore_",
)

#: How old a leftover must be before the sweep will drop it.
#:
#: **Twelve hours, which is extravagant, and the extravagance is the design.** A database
#: nothing will ever come back for costs nothing by being left another half-day, so the margin
#: is free — and it is what lets the sweep be safe without asking the operating system what is
#: running. The longest run measured here is eleven minutes locally and twenty-seven in CI.
#:
#: The hazard it is buying distance from is `#774`: two runs on one machine destroying each
#: other's schema, which surfaced as unrelated tests raising `relation "sample_row" does not
#: exist` — about the machine rather than the code, and it cost three false alarms. Sweeping a
#: live run would be that defect arriving through the fix for it.
ABANDONED_AFTER = datetime.timedelta(hours=12)

#: The shape :data:`TEST_DATABASE_NAME` is built to, and **the only shape the sweep will drop**.
#: A name written before `#1667` carries no time at all, so its age is unknowable and it is
#: reported rather than destroyed — an entry that goes away by itself once nobody has one left.
#:
#: It is also what makes the interpolation in :func:`swept` safe: `DROP DATABASE` takes no bound
#: parameter, and a name that matched this is a word, fourteen digits and twelve hex characters.
STAMPED_NAME = re.compile(
	rf"^{re.escape(THROWAWAY_PREFIX)}[a-z]+_(\d{{14}})_[0-9a-f]{{12}}$"
)


def throwaway_name (purpose: str) -> str:
	"""Return a name for a database this run may destroy, carrying the moment it was made.

	**A fresh name per run, because everything built this way is *dropped* before it is used.**
	A constant meant two pytest processes on one machine destroyed each other's schema mid-run,
	and the failure surfaced as unrelated tests raising ``relation "sample_row" does not exist``
	— no hint of the cause, about the machine rather than the code, and it cost this project
	three separate false alarms, one of them mid-review (`#774`).

	**It carries the moment it was made, in UTC, and that is `#1667`.** The uuid above traded
	collision for accumulation and only the first half was ever noticed: teardown does not run
	when the process does not finish, so a Ctrl-C, the gate's own ``timeout`` or an OOM kill
	leaves a name nothing will ever generate again. 87 of them reached 981 MB here before
	anybody counted. PostgreSQL records no creation time and ``pg_stat_file`` needs a privilege
	this account does not hold — measured 2026-09-09 — so **the name is the only place an age
	can live**.

	**One place, because there were five.** Four other fixtures each wrote their own, and two
	keyed on ``os.getpid()``, which is reused — so two runs a day apart could collide on a name
	one of them was about to drop. That is `#774` again with a longer fuse.

	``purpose`` is a word a person will read in ``psql -l``, and is the only part of the name
	meant for them.
	"""

	stamp = datetime.datetime.now(datetime.UTC)

	return (
		f"{THROWAWAY_PREFIX}{purpose}_"
		f"{stamp:%Y%m%d%H%M%S}_{uuid.uuid4().hex[:12]}"
	)


#: The database the whole session runs against, named once at import — so under ``-n auto``
#: there is one per worker, which is the shape :func:`swept` exists to clean up after.
TEST_DATABASE_NAME = throwaway_name("session")

#: What counts as *set* for a ``SUBROUTINE_TEST_REQUIRE_*`` variable.
_MEANS_YES = frozenset({"1", "true", "yes", "on"})


def required (name: str) -> bool:
	"""Whether a missing resource should fail this run rather than skip it.

	**One reader for all three of them** (`#927`'s H-17). This was written out here for
	PostgreSQL and again in ``tests/test_browser.py`` as ``== "1"`` — so
	``SUBROUTINE_TEST_REQUIRE_BROWSER=true`` set the browser guard and did nothing, which is
	a guard reading a spelling rather than a value. Two copies of a rule is how they come to
	disagree, and this pair already had.
	"""

	return os.environ.get(name, "").strip().lower() in _MEANS_YES


#: Turns an unreachable PostgreSQL from a skip into a failure. Set in CI, and the single
#: most important line in this file: without it, a runner whose database service failed to
#: start would run half the suite and report success, which is precisely the state the
#: dual-backend rule exists to prevent. A skip is a courtesy to someone working locally,
#: not something the build should ever be allowed to do quietly.
REQUIRE_POSTGRES = required("SUBROUTINE_TEST_REQUIRE_POSTGRES")


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

	**And two variables that are nobody's here.** An editor sets ``CLAUDE_PLUGIN_ROOT`` in the
	environment of any process a plugin starts, which includes an MCP server and therefore any
	suite run from one (`#381`); left alone, ``installations.plugin()`` would report the
	*developer's* installed plugin version inside a test. And ``CLAUDECODE`` decides which of a
	connection's two stored tokens is resolved (`#1449`), so a suite run from inside an agent
	would take the agent's where CI took the person's — a fixture whose answer depends on who
	ran it, which is the same class of leak as the `config.toml` above.
	"""

	root = tmp_path_factory.mktemp("xdg")

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(root / variable.lower()))

	monkeypatch.delenv(subroutine.installations.PLUGIN_ROOT, raising=False)
	monkeypatch.delenv(subroutine.connections.DEFAULT_AGENT_WHEN, raising=False)

	for name in list(os.environ):
		if name.startswith("SUBROUTINE_") and not name.startswith("SUBROUTINE_TEST_"):
			monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_inherited_colour (monkeypatch: pytest.MonkeyPatch) -> None:
	"""Render the terminal's help the same way on every machine (`SR#1537`).

	**Typer decides colour from the environment**: ``typer.rich_utils`` sets ``FORCE_TERMINAL``
	from ``GITHUB_ACTIONS``, ``FORCE_COLOR`` or ``PY_COLORS``, and every GitHub runner sets the
	first of those. So one assertion about help text was measured against plain text here and
	against ANSI there, which is not a difference any test means to be making.

	**It cost a red CI on all four interpreters against a green gate on every machine here.**
	rich styles an option name in parts — a styled ``-``, a reset, then ``-project`` — so
	``--project`` is not a substring of a help page that displays it perfectly. `SR#1537` also
	rewrote that test to ask the command rather than its rendering, which is the better half of
	the fix; this is here so the next one cannot meet the trap at all.

	The rule is the one :func:`_no_inherited_installation` already states in full: a test must
	not read the configuration of the machine it happens to be running on. Colour is that, and
	it was the piece of it nobody had thought of.

	**Patched as an attribute rather than as an environment variable.** Those are read once,
	when ``typer.rich_utils`` is imported, which has already happened by the time any fixture
	runs — so ``monkeypatch.delenv`` would look right and do nothing. ``_get_rich_console``
	reads this global on every call.

	**And the environment as well, because there are two consoles and each needs the other's
	remedy** (`SR#2290`). The attribute above is Typer's help; the product prints through
	:class:`subroutine.cli.output.Terminal`, a plain ``rich.console.Console`` that reads
	``os.environ`` afresh on every construction — so for that one the variables *are* the
	control and an attribute patch reaches nothing. Half of one rule applied is how this looked
	from the inside: the sentence above was right, written down, and covering one of two.

	**Clearing the variables is necessary and is not sufficient, which took measuring.** Two
	obvious fixes are both wrong here and each looks right:

	- ``no_color=True`` does not do it. A console set that way still writes
	  ``si \x1b[1m(\x1b[0mperson`` — Rich's highlighter emits *bold* around the brackets, and
	  `#102`'s note that ``NO_COLOR`` turns ``dim cyan`` into ``dim`` says precisely that: the
	  hue goes and the attribute stays.
	- ``delenv`` alone does not either, because it is too late. ``Console.__init__`` calls
	  ``_detect_color_system`` and **freezes the answer**, while ``is_terminal`` stays dynamic —
	  so a console built with ``FORCE_COLOR`` set reports ``is_terminal False`` after the
	  variable is cleared and goes on rendering styles through a colour system of
	  ``STANDARD``. Measured, both halves.

	**And the product's consoles are built when ``cli/main`` is imported**, which is collection
	time, long before any fixture runs. So the frozen value has to be put back by hand — the
	same shape as the attribute patch above, for the same reason, one library along.

	**The consoles are found rather than named.** Two of them exist today and a third declared
	at module level in any ``subroutine`` module would otherwise inherit the machine's colour in
	silence, which is this codebase's signature defect at its smallest.

	With ``FORCE_COLOR=3`` exported — which Claude Code sets, and so do many terminals and CI
	images — 29 tests failed on ANSI inside asserted strings before this.
	"""

	monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", False)

	for name in ("FORCE_COLOR", "PY_COLORS", "NO_COLOR"):
		monkeypatch.delenv(name, raising=False)

	for module in list(sys.modules.values()):
		if not getattr(module, "__name__", "").startswith("subroutine"):
			continue

		for value in list(vars(module).values()):
			# `_color_system` is private and is the whole of what was frozen. Reaching for it
			# beats rebuilding these consoles, which would mean restating the arguments each
			# was constructed with — a second copy that can disagree.
			if isinstance(value, rich.console.Console):
				monkeypatch.setattr(value, "_color_system", None, raising=False)


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
	starts inherits the same instance (docs/design.md §12.5). In one pytest process that means a
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
	"""Return why PostgreSQL cannot be used, or ``None`` when it can.

	**Building the engine is inside the ``try``, and that is the whole of `#1073`.** A
	SQLAlchemy dialect imports its DBAPI when the engine is *constructed*, not when it
	connects — so on a machine with no ``psycopg`` this raised ``ModuleNotFoundError`` out of a
	function whose contract is to return a reason. Every PostgreSQL fixture then errored
	rather than skipping: **1,563 errors and four failures**, which is what a contributor's
	first ``pytest`` looked like, against a ``CONTRIBUTING.md`` promising a skip.

	It survived because this machine and CI both have the driver, so the branch had never been
	reached here — the same shape as `#532`, a path only somebody else's machine takes.

	**The two obstacles are named apart**, because the remedies are not the same sentence:
	installing an extra is not starting a server, and *"PostgreSQL is not reachable"* sends
	somebody to check a server that is running perfectly well.
	"""

	engine = None

	try:
		engine = sqlalchemy.create_engine(POSTGRES_ADMIN_URL)

		with engine.connect() as connection:
			connection.execute(sqlalchemy.text("SELECT 1"))

	except ModuleNotFoundError as error:
		return (
			f"the driver {POSTGRES_ADMIN_URL} names is not installed ({error}). "
			f"Install it with: pip install -e '.[dev,postgres]'"
		)

	except Exception as error:
		return f"PostgreSQL is not reachable at {POSTGRES_ADMIN_URL}: {error}"

	finally:
		if engine is not None:
			engine.dispose()

	return None


def abandoned_names (
	names: typing.Iterable[str], *, now: datetime.datetime
) -> tuple[list[str], list[str]]:
	"""Split leftover database names into the ones old enough to drop and the ones to report.

	**Pure, and it takes ``now``**, which is what lets the age rule be driven without a database
	and without waiting twelve hours for one. `#405`'s rule: a guard is fed its defect through
	the real entry point rather than through a second copy of the logic.

	A name that does not match :data:`STAMPED_NAME` comes back in the second list and is never
	dropped. Its age cannot be established, and *"probably old"* is not a thing to say before a
	`DROP DATABASE`.
	"""

	old = []
	unreadable = []

	for name in names:
		found = STAMPED_NAME.match(name)

		if found is None:
			unreadable.append(name)

			continue

		made = datetime.datetime.strptime(found[1], "%Y%m%d%H%M%S").replace(
			tzinfo=datetime.UTC
		)

		if now - made >= ABANDONED_AFTER:
			old.append(name)

	return sorted(old), sorted(unreadable)


def left_behind (connection: sqlalchemy.Connection) -> dict[str, int]:
	"""Return every test database on this server nothing is connected to, with its size.

	**No open connection is necessary and is nowhere near sufficient**, which is why the caller
	weighs the age as well: a worker between two tests holds none, so a sweep that asked
	PostgreSQL alone would destroy a running suite — `#774` arriving through the fix for it.

	``starts_with`` rather than ``LIKE``, because ``_`` is a single-character wildcard and the
	prefix has two of them: ``LIKE 'subroutine_test_%'`` also matches a database somebody else
	named ``subroutineXtestY``.
	"""

	rows = connection.execute(
		sqlalchemy.text(
			"select datname, pg_database_size(datname) from pg_database "
			"where exists ("
			"select 1 from unnest(cast(:prefixes as text[])) as wanted(prefix) "
			"where starts_with(pg_database.datname, wanted.prefix)"
			") "
			"and not exists ("
			"select 1 from pg_stat_activity "
			"where pg_stat_activity.datname = pg_database.datname"
			")"
		),
		{"prefixes": [THROWAWAY_PREFIX, *RETIRED_PREFIXES]},
	)

	return dict(rows.tuples().all())


def swept (
	connection: sqlalchemy.Connection, *, now: datetime.datetime
) -> tuple[dict[str, int], list[str]]:
	"""Drop the test databases earlier runs abandoned, and report what was there.

	Returns what was dropped with each one's size, and the names left alone because nothing
	could say how old they were.
	"""

	found = left_behind(connection)
	old, unreadable = abandoned_names(found, now=now)
	dropped = {}

	for name in old:
		try:
			# Interpolated because `DROP DATABASE` accepts no bound parameter, and safe because
			# `STAMPED_NAME` has already established that this name is the prefix, fourteen
			# digits and twelve hex characters.
			connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{name}"'))

		except sqlalchemy.exc.SQLAlchemyError:
			# Somebody connected between the query and the drop, or a second sweep got there
			# first. Both are ordinary, neither is this run's business, and the next run will
			# find it again if it really was abandoned.
			continue

		dropped[name] = found[name]

	return dropped, unreadable


def said_about (dropped: dict[str, int], unreadable: list[str]) -> str:
	"""Return the one line the header prints about a sweep that found something."""

	said = []

	if dropped:
		megabytes = sum(dropped.values()) / 1_000_000
		said.append(f"dropped {len(dropped)}, reclaiming {megabytes:.0f} MB")

	if unreadable:
		said.append(
			f"left {len(unreadable)} whose name carries no time — from before this swept, and "
			f"yours to drop once no run is using them"
		)

	return f"abandoned test databases: {'; '.join(said)}"


#: Where :func:`pytest_configure` leaves what it found, for :func:`pytest_sessionstart` to say.
#: **Two hooks because neither can do both**: the terminal reporter is not registered when a
#: conftest's ``configure`` runs — measured, and it is why the first version of this wrote its
#: line into a ``None`` — and ``pytest_report_header``, the obvious home, is never called at
#: all under this project's ``-q``.
_SWEPT: dict[str, str] = {}


def pytest_configure (config: pytest.Config) -> None:
	"""Sweep what earlier runs left behind — `#1667`.

	**At the start of a run, because only a session that is running can say what is live.** A
	teardown cannot help here: the whole defect is that teardown did not happen.

	**Once per run, not once per worker.** Under ``-n auto`` every worker imports this file, so
	eight processes would race to drop the same names and report eight different numbers;
	``workerinput`` is what xdist puts on a worker's config and on nothing else.
	"""

	if hasattr(config, "workerinput"):
		return

	if _postgres_unavailable_reason() is not None:
		return

	engine = sqlalchemy.create_engine(POSTGRES_ADMIN_URL, isolation_level="AUTOCOMMIT")

	try:
		with engine.connect() as connection:
			dropped, unreadable = swept(connection, now=datetime.datetime.now(datetime.UTC))

	except sqlalchemy.exc.SQLAlchemyError:
		# A sweep is a courtesy and never a reason a run does not start. None of this is the
		# suite's subject, and a failure to tidy up must not read as a failing test.
		return

	finally:
		engine.dispose()

	if not dropped and not unreadable:
		# **Silent when there was nothing to do**, which is most runs. A line every time would
		# be noise that is correct, and the number this exists to surface — 981 MB — would be
		# buried among the runs that had none.
		return

	_SWEPT[config.rootpath.as_posix()] = said_about(dropped, unreadable)


def pytest_sessionstart (session: pytest.Session) -> None:
	"""Say what the sweep found, once there is somewhere to say it."""

	said = _SWEPT.pop(session.config.rootpath.as_posix(), None)

	if said is None:
		return

	reporter = session.config.pluginmanager.get_plugin("terminalreporter")

	if reporter is None:
		# Not the ordinary path and not a reason to lose the sentence. A report nobody sees is
		# the half of `#1667` that is easiest to ship without noticing.
		print(said, file=sys.stderr)

		return

	reporter.write_line(said)


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


@pytest.fixture(scope="session")
def instances (tmp_path_factory: pytest.TempPathFactory) -> instance_templates.Store:
	"""Hold one initialised instance per shape, so the terminal tests copy rather than build.

	Session-scoped because that is the whole point: `SR#1830` measured ``subroutine init`` at
	456 ms against 0.5 ms to copy its result, run 479 times across the suite for a byte-identical
	tree. Under ``-n auto`` this is one build per shape per worker.

	``tests/instance_templates.py`` carries what makes the substitution safe and what would make it
	unsafe; ``tests/test_instances_template.py`` is what holds it to that.
	"""

	return instance_templates.Store(tmp_path_factory.mktemp("instances"))


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
