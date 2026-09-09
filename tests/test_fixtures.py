"""Tests for the test harness itself.

Ordinarily testing a conftest would be indulgent. This one earned it: the first CI run
failed on all three Python versions with ``password authentication failed for user
"postgres"``, and the cause was a helper here rather than anything in the product. The
suite had no way to notice, because every check it makes about PostgreSQL runs *after* the
connection it could not make.
"""

import contextlib
import datetime
import io
import os
import pathlib
import subprocess
import sys
import typing
import uuid

import pytest
import rich.console
import sqlalchemy
import sqlalchemy.engine
import typer.rich_utils
import typer.testing

import conftest
import subroutine.cli.main
import subroutine.cli.output
import subroutine.connections
import subroutine.installations
import test_browser


def test_a_derived_url_keeps_its_password () -> None:
	"""``str()`` on a SQLAlchemy URL masks the password; the derived URL must not.

	The failure this guards against is invisible on a developer machine, where the admin
	URL has no password at all — peer authentication over the Unix socket — so masking it
	changes nothing. It appears in CI, in a container, and in any deployment that
	authenticates properly, which is to say everywhere that matters.
	"""

	derived = conftest.with_database(
		"postgresql+psycopg://postgres:s3cret@localhost:5432/postgres", "subroutine_test"
	)

	assert derived == "postgresql+psycopg://postgres:s3cret@localhost:5432/subroutine_test"
	assert "***" not in derived

	# The property that actually matters: it survives a round trip back into a URL.
	assert sqlalchemy.engine.make_url(derived).password == "s3cret"


def test_a_derived_url_without_a_password_is_unchanged () -> None:
	"""The local case still works, which is why the bug went unnoticed for a whole slice."""

	derived = conftest.with_database("postgresql+psycopg:///postgres", "subroutine_test")

	assert derived == "postgresql+psycopg:///subroutine_test"


def test_a_url_object_is_accepted_as_well_as_a_string () -> None:
	"""Callers hold both forms, and neither should have to convert before calling."""

	parsed = sqlalchemy.engine.make_url("postgresql+psycopg://u:p@host/postgres")

	assert conftest.with_database(parsed, "other") == conftest.with_database(
		"postgresql+psycopg://u:p@host/postgres", "other"
	)


def test_the_editors_plugin_variable_does_not_reach_a_test () -> None:
	"""No test may see the plugin the developer happens to have installed — item ``#381``.

	``CLAUDE_PLUGIN_ROOT`` is set by an editor in the environment of every process a plugin
	starts, which includes an MCP server and so any suite run from one. It is not a
	``SUBROUTINE_`` name, so the loop that clears the product's own variables never touched
	it, and ``installations.plugin()`` would have reported *this machine's* cached version
	inside a test that had said nothing about a plugin at all.

	The same leak as a developer's ``config.toml`` reaching the suite, arriving through a name
	this project does not own — which is why the check is here rather than left to the one
	autouse fixture asserting its own good behaviour.
	"""

	assert subroutine.installations.PLUGIN_ROOT not in os.environ
	assert subroutine.installations.plugin() is None


def test_the_editors_agent_variable_does_not_reach_a_test () -> None:
	"""No test may resolve a different credential for being run from inside an agent — `#1449`.

	``CLAUDECODE`` decides which of a connection's two stored tokens answers. A suite run from an
	agent's own shell — which is how most of this project's tests are run — would take the
	agent's where CI takes the person's, so a fixture storing one token and asserting on it would
	mean two different things depending on who pressed return.

	**Here rather than left to the fixture asserting its own good behaviour**, for the reason the
	two checks around it are: a fixture nothing checks is a control that can be deleted in
	silence, which is the shape `#303` is named for. Third variable this project does not own to
	need clearing, after ``CLAUDE_PLUGIN_ROOT`` and the colour settings.
	"""

	assert subroutine.connections.DEFAULT_AGENT_WHEN not in os.environ


def test_the_machines_colour_setting_does_not_reach_a_test (
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""No test may render differently for being run on a build machine — `SR#1537`.

	``typer.rich_utils`` sets ``FORCE_TERMINAL`` from ``GITHUB_ACTIONS``, ``FORCE_COLOR`` or
	``PY_COLORS`` **when it is imported**, and every GitHub runner sets the first of those. So
	help was rendered as plain text on a developer's machine and with ANSI on all four of CI's,
	and an assertion about help text meant two different things depending on where it ran.

	**It cost a red CI on every interpreter against a green gate here.** rich styles an option
	name in parts — a styled ``-``, a reset, then ``-project`` — so ``--project`` is not a
	substring of a page that displays it perfectly.

	Same leak as ``CLAUDE_PLUGIN_ROOT`` above and as the developer's ``config.toml`` before it:
	the machine's own configuration reaching the suite through a name this project does not
	own. **And it is here rather than left to the fixture asserting its own good behaviour** —
	a fixture nothing checks is a control that can be deleted in silence, which is the shape
	`#303` is named for.

	**Both halves, because the flag alone is a claim about a variable rather than about
	output.** The second renders a real command through the real runner and asks whether any
	escape survived, which is the thing that actually broke.

	**And four checks now, because there are two consoles and each needs a different remedy**
	(`SR#2290`). Typer's help is the one above; everything the product prints goes through
	:class:`subroutine.cli.output.Terminal`. With ``FORCE_COLOR=3`` set, 29 tests failed on ANSI
	inside asserted strings while this test went on passing — it was watching the console that
	was already covered.

	**Rendered rather than asked, and it has to be.** ``no_color=True`` does not make this
	pass: Rich's highlighter still emits bold around the brackets in ``si (person)``, which is
	`#102`'s own note that ``NO_COLOR`` keeps the attribute and drops the hue. The property is
	that nothing a terminal would obey comes out, so that is what is asserted.

	**The two product checks cover disjoint sets and were arrived at by measuring, not by
	reading.** ``Console.__init__`` freezes a colour system while ``is_terminal`` stays dynamic,
	so a console built before the fixture runs — and both of the program's are, at the import of
	``cli/main`` — keeps rendering styles after the variables are gone. The loop covers those.
	The console built at the end covers every console made *while* a test runs, which is what
	the cleared variables are for and is otherwise exercised by nothing.
	"""

	assert typer.rich_utils.FORCE_TERMINAL is False, (
		"a test's rendering follows the machine it runs on, so help text asserted here means "
		"something else on a build machine"
	)

	rendered = typer.testing.CliRunner().invoke(
		subroutine.cli.main.app, ["list", "--help"]
	).output

	assert "--project" in rendered, "the help did not render, so the check below reads nothing"
	assert "\x1b" not in rendered.encode("unicode_escape").decode(), (
		"help came out styled, so any assertion about its text is measuring the styling too"
	)

	# **The consoles the program actually prints through, not a fresh one.** A console built
	# here is built after the fixture has cleared the environment and is therefore plain
	# whatever the fixture did about the frozen ones — so asserting on it would pass with that
	# half deleted, which is the whole defect this test is for.
	consoles = [
		(f"{module.__name__}.{name}", value)
		for module in list(sys.modules.values())
		if getattr(module, "__name__", "").startswith("subroutine")
		for name, value in list(vars(module).items())
		if isinstance(value, rich.console.Console)
	]

	assert consoles, "no console was found, so the checks below read nothing"

	for where, console in consoles:
		buffer = io.StringIO()
		monkeypatch.setattr(console, "_file", buffer, raising=False)
		console.print("si (person), via the local database")

		assert buffer.getvalue().strip(), f"{where} printed nothing, so this reads nothing"
		assert "\x1b" not in buffer.getvalue(), (
			f"{where} follows the machine it runs on, so every assertion about what a command "
			f"said is measuring the styling too: {buffer.getvalue()!r}"
		)

	# **And one built here, which is the half the loop above cannot reach.** Clearing the
	# variables and restoring the frozen colour system fix disjoint sets: the restore covers
	# consoles that already existed, and clearing covers every console built while a test runs
	# — `cli/personal` makes one per `_suggest`. Measured: with only the restore, all 533 of
	# the tests this defect was found in still pass, so nothing else exercises the clearing and
	# it would otherwise be a control read by nothing, which is `#303`'s shape exactly.
	later = io.StringIO()
	subroutine.cli.output.Terminal(file=later, width=80).print("si (person), via the local")

	assert later.getvalue().strip(), "the console built here printed nothing"
	assert "\x1b" not in later.getvalue(), (
		f"a console built during a test follows the machine it runs on: {later.getvalue()!r}"
	)


def test_a_machine_that_cannot_draw_text_is_a_skip_rather_than_66_errors () -> None:
	"""`SR#1567`. The browser probe asked whether Chromium *starts*, which is not the question.

	Measured on the machine that ran the cold review of 2026-08-28: no fonts and no fontconfig
	at all. Chromium launched perfectly well, so ``UNAVAILABLE`` was ``None``, so all 66 tests
	ran — and all 66 **errored** at the first ``set_content`` with a Playwright stack trace
	rather than skipping with a remedy. CI is unaffected, because ``playwright install-deps``
	pulls fonts onto the runner; the population that meets it is a contributor on a headless
	box or a slim container, which is precisely the population the guard exists for.

	**Third turn of the same wheel and the first two are in that file's own comments** —
	`SR#927`'s H-17, where the probe asked about the browser and every fixture also needs Node,
	and `SR#795`, where every test errored in CI for want of a browser on six commits while the
	local gate stayed green. Each of those closed the instance it met.

	**Asserted here rather than there, because this machine cannot reach the state.** Chromium
	has fonts here, and pointing it at an empty fontconfig did not take them away — so the
	branch is unreachable on the machine that wrote it, and a guard written where its own
	failure path cannot run is untested. The rule is pulled into a function that returns a
	value, which is this project's answer whenever a defect depends on the running machine
	differing from this one.

	**And that was not enough on its own, which is `SR#1585` and the half worth reading.** The
	rule was pulled out and asserted on here exactly as prescribed, and the probe went on
	passing on a fontless machine — because what it handed this function was the width of a
	``<p>``, a block box measuring its *container*. Every assertion below was true of a
	function being fed a number that could not vary with the thing it decides. So the probe
	reads the same element twice now, once with text and once empty, and the pair is what says
	the instrument works at all; the case that catches the original defect is the last one.
	"""

	assert test_browser._cannot_lay_out_text(120.5, 0) is None, (
		"text wider than the empty element is a usable browser"
	)

	for measured in (0, 0.0, None):
		refusal = test_browser._cannot_lay_out_text(measured, 0)

		assert refusal is not None, f"a width of {measured!r} is a browser that drew nothing"
		assert "fonts" in refusal, "the remedy has to name what is missing, not just refuse"

	# **The measured defect, as measured** (`SR#1585`): 1264 was what a fontless Chromium gave
	# the old probe, and it is what a Chromium *with* fonts gives it here — because it is the
	# window, not the words. A probe that answers the same with the text taken out is refused
	# rather than believed, and the refusal names the probe because that is what is broken.
	blind = test_browser._cannot_lay_out_text(1264, 1264)

	assert blind is not None, "a width that does not move when the text goes says nothing"
	assert "PROBE" in blind, "the remedy is to fix the probe, not to install fonts"
	assert "fonts" not in blind, "sending a reader after fonts they already have helps nobody"


# ---------------------------------------------------------------------------------------------
# The sweep that stops an unfinished run leaking a database for ever — `#1667`.
#
# **The name is the only place an age can live.** PostgreSQL records no creation time, and
# `pg_stat_file` — which would answer from the base directory — needs a privilege this account
# does not hold. So the sweep reads the name, and the first test below is the one that keeps the
# whole thing from going inert: a name the suite generates that the sweep cannot parse leaks
# exactly as before and says nothing about it.
# ---------------------------------------------------------------------------------------------


def _synthetic (*, made: datetime.datetime | None = None) -> str:
	"""Return a test-database name of the shape the sweep understands, made at a given moment.

	A fresh uuid every time, because these tests create real databases and several workers run
	them at once — two of them agreeing on a name would be `#774` reproduced inside the guard
	written for its consequence.
	"""

	stamp = made or datetime.datetime.now(datetime.UTC)

	return f"{conftest.THROWAWAY_PREFIX}probe_{stamp:%Y%m%d%H%M%S}_{uuid.uuid4().hex[:12]}"


def _legacy_name () -> str:
	"""Return a name shaped the way this suite wrote them before `#1667` — no time in it.

	**Nothing anywhere will drop one**, which is what makes it the safe thing to plant in a test
	that has to assert on a sweep while seven other workers are running sweeps of their own.
	"""

	return f"{conftest.THROWAWAY_PREFIX}{uuid.uuid4().hex[:12]}"


def _admin () -> sqlalchemy.engine.Engine:
	"""Return an autocommitting connection to the maintenance database, or skip."""

	reason = conftest._postgres_unavailable_reason()

	if reason is not None:
		if conftest.REQUIRE_POSTGRES:
			pytest.fail(reason)

		pytest.skip(reason)

	return sqlalchemy.create_engine(conftest.POSTGRES_ADMIN_URL, isolation_level="AUTOCOMMIT")


def _exists (connection: sqlalchemy.Connection, name: str) -> bool:
	"""Report whether a database of that name is on the server."""

	return (
		connection.execute(
			sqlalchemy.text("select 1 from pg_database where datname = :name"), {"name": name}
		).scalar()
		is not None
	)


def test_the_name_this_suite_generates_is_one_the_sweep_can_read () -> None:
	"""Without this the sweep is inert, and inert in the way that says nothing.

	The name and the pattern are two declarations that have to agree. If they part company,
	every database this suite makes becomes unreadable to the sweep, is reported rather than
	dropped, and accumulates exactly as it did before — while a sweep that runs on every session
	makes it look handled.
	"""

	assert conftest.STAMPED_NAME.match(conftest.TEST_DATABASE_NAME)


def test_a_name_from_before_this_is_reported_rather_than_dropped () -> None:
	"""Its age cannot be established, and *probably old* is not a thing to say before a DROP."""

	legacy = _legacy_name()
	old, unreadable = conftest.abandoned_names(
		[legacy], now=datetime.datetime.now(datetime.UTC)
	)

	assert old == []
	assert unreadable == [legacy]


def test_the_age_is_read_from_the_name_and_the_threshold_is_the_whole_rule () -> None:
	"""Driven at both sides of the boundary, which is where an off-by-one would live.

	**``now`` is derived from the stamp rather than taken from the clock**, and the first
	version of this was not: a name carries whole seconds, so ``now - ABANDONED_AFTER`` written
	from a clock reading is always a fraction of a second *older* than the boundary and lands
	on the same side of it either way. Changing ``>=`` to ``>`` left the test green, which is
	what said so.
	"""

	made = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
	abandoned = _synthetic(made=made)
	running = _synthetic(made=made + datetime.timedelta(seconds=1))

	old, unreadable = conftest.abandoned_names(
		[abandoned, running], now=made + conftest.ABANDONED_AFTER
	)

	assert old == [abandoned]
	assert unreadable == []


def test_a_database_that_something_is_connected_to_is_never_a_candidate () -> None:
	"""The `#774` guard, and the reason age alone would not be enough on its own.

	A worker between two tests holds no connection, so *nothing is connected* cannot decide
	this — but the reverse direction is absolute: something connected is something running, and
	a sweep that could reach it would be `#774` arriving through the fix for its consequence.
	The database here is made to look a year old so that only the connection can save it.
	"""

	name = _synthetic(made=datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=365))
	admin = _admin()
	holder = None

	try:
		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'CREATE DATABASE "{name}"'))

		holder = sqlalchemy.create_engine(
			conftest.with_database(conftest.POSTGRES_ADMIN_URL, name)
		)

		with holder.connect() as held:
			held.execute(sqlalchemy.text("select 1"))

			with admin.connect() as connection:
				assert name not in conftest.left_behind(connection)

				dropped, _unreadable = conftest.swept(
					connection, now=datetime.datetime.now(datetime.UTC)
				)

				assert name not in dropped
				assert _exists(connection, name)

	finally:
		if holder is not None:
			holder.dispose()

		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{name}"'))

		admin.dispose()


def test_the_sweep_drops_what_was_abandoned_and_leaves_what_was_not () -> None:
	"""End to end, against real databases, because the pure half cannot see the SQL."""

	now = datetime.datetime.now(datetime.UTC)
	abandoned = _synthetic(made=now - datetime.timedelta(days=1))
	recent = _synthetic(made=now)
	admin = _admin()

	try:
		with admin.connect() as connection:
			for name in (abandoned, recent):
				connection.execute(sqlalchemy.text(f'CREATE DATABASE "{name}"'))

			dropped, _unreadable = conftest.swept(connection, now=now)

			assert abandoned in dropped
			assert recent not in dropped

			assert not _exists(connection, abandoned)
			assert _exists(connection, recent)

			# The size is reported rather than counted, so it has to be read before the drop
			# and not after — a zero here would be the report quietly saying nothing was
			# reclaimed on a sweep that reclaimed a gigabyte.
			assert dropped[abandoned] > 0

	finally:
		with admin.connect() as connection:
			for name in (abandoned, recent):
				connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{name}"'))

		admin.dispose()


@contextlib.contextmanager
def _as_the_controller (config: pytest.Config) -> typing.Iterator[None]:
	"""Run the block with ``workerinput`` absent, which is what xdist means by *the controller*.

	Under ``-n auto`` this very file is running in a worker, so the controller's own path
	through the sweep is unreachable without saying so — and it is the path that does the work.
	"""

	held = getattr(config, "workerinput", None)

	if held is not None:
		del config.workerinput  # type: ignore[attr-defined]

	try:
		yield

	finally:
		if held is not None:
			config.workerinput = held  # type: ignore[attr-defined]


def _what_a_sweep_said (config: pytest.Config) -> str | None:
	"""Run the sweep as the controller would and return the sentence it left, if any."""

	conftest._SWEPT.pop(config.rootpath.as_posix(), None)

	with _as_the_controller(config):
		conftest.pytest_configure(config)

	return conftest._SWEPT.pop(config.rootpath.as_posix(), None)


def test_the_sweep_drops_what_it_can_and_reports_what_it_cannot (
	pytestconfig: pytest.Config,
) -> None:
	"""The hook end to end, which is the only path joining a real server to a real drop.

	**Both kinds are planted, and each answers a different hazard.** The stamped one proves the
	drop; the one shaped like a name from before this shipped proves the sentence, and it does
	so without a race — nothing anywhere will drop an unreadable name, where a stamped one could
	be taken by a parallel worker between the setup and the call. A test that passes because
	somebody else did the work proves nothing.
	"""

	name = _synthetic(made=datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1))
	legacy = _legacy_name()
	admin = _admin()

	try:
		with admin.connect() as connection:
			for one in (name, legacy):
				connection.execute(sqlalchemy.text(f'CREATE DATABASE "{one}"'))

		said = _what_a_sweep_said(pytestconfig)

		assert said is not None
		assert "abandoned test databases" in said
		assert "carries no time" in said

		with admin.connect() as connection:
			assert not _exists(connection, name)
			assert _exists(connection, legacy)

	finally:
		with admin.connect() as connection:
			for one in (name, legacy):
				connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{one}"'))

		admin.dispose()


def test_a_worker_sweeps_nothing_and_says_nothing (pytestconfig: pytest.Config) -> None:
	"""Eight workers racing to drop the same names would report eight different numbers.

	Driven with a name from before this shipped, for the reason above: an unreadable name is
	never dropped by anybody, so the only thing that can empty this answer is the guard itself.
	"""

	legacy = _legacy_name()
	admin = _admin()
	held = getattr(pytestconfig, "workerinput", None)

	try:
		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'CREATE DATABASE "{legacy}"'))

		conftest._SWEPT.pop(pytestconfig.rootpath.as_posix(), None)

		if held is None:
			pytestconfig.workerinput = {"workerid": "gw0"}  # type: ignore[attr-defined]

		conftest.pytest_configure(pytestconfig)

		assert pytestconfig.rootpath.as_posix() not in conftest._SWEPT

	finally:
		if held is None and hasattr(pytestconfig, "workerinput"):
			del pytestconfig.workerinput

		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{legacy}"'))

		admin.dispose()


def test_the_sweep_says_nothing_when_there_was_nothing_to_sweep (
	pytestconfig: pytest.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Noise that is correct still buries the signal, and the signal here is 981 MB.

	**The server's answer is supplied rather than arranged**, because *nothing left behind* is
	not a state one worker can establish while seven others are making databases. Everything
	below the substitution is the real hook.
	"""

	monkeypatch.setattr(conftest, "left_behind", lambda _connection: {})

	assert _what_a_sweep_said(pytestconfig) is None


def test_the_sentence_reaches_a_run_configured_the_way_this_project_runs () -> None:
	"""The only guard that could have caught either of the two ways this was already inert.

	**A subprocess, with the repository's own ``pyproject.toml`` in force**, because both
	failures were invisible from inside a test. ``pytest_report_header`` is the obvious home for
	this and is never called at all under the ``-q`` that ``addopts`` carries; and the terminal
	reporter is not registered when a conftest's ``pytest_configure`` runs, so the first version
	that survived the first problem wrote its line into a ``None``. Each read as correct in the
	source and did nothing in the only configuration that will ever run it.

	Planted with an unreadable name so the assertion cannot be satisfied — or spoiled — by
	another worker.
	"""

	legacy = _legacy_name()
	admin = _admin()

	try:
		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'CREATE DATABASE "{legacy}"'))

		finished = subprocess.run(
			[
				sys.executable,
				"-m",
				"pytest",
				"tests/test_accountability.py",
				"--collect-only",
				"-p",
				"no:randomly",
			],
			cwd=pathlib.Path(__file__).resolve().parent.parent,
			capture_output=True,
			text=True,
			check=False,
		)

		assert "abandoned test databases" in finished.stdout, finished.stdout[-2000:]

	finally:
		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{legacy}"'))

		admin.dispose()


#: Where a database name may come from. :func:`conftest.throwaway_name` is the one the suite
#: uses; the rest are this file's own and exist because these tests must build names the helper
#: deliberately cannot — one dated in the past, one shaped the way names were before `#1667`,
#: and one under a prefix nothing generates any more. Named here rather than exempting the file,
#: so a name genuinely written by hand in this file is still an offender.
_MAKERS = ("throwaway_name(", "_synthetic(", "_legacy_name(", "RETIRED_PREFIXES[")

#: The statement this scan is about, **spelled in parts so the scan cannot read its own source
#: as a site**. Written whole, the four sentences in this file that describe the rule — this
#: comment among them — were reported as offenders, which is the recorded trap of a guard
#: counting its own explanation. Naming the opening quote and brace as well is what separates a
#: statement from prose about one.
_CREATION = "CREATE " + 'DATABASE "{'


def _handmade_names (root: pathlib.Path) -> list[str]:
	"""Return every ``CREATE DATABASE`` under ``tests/`` whose name did not come from one place.

	**Scanned at the ``CREATE DATABASE`` rather than at the prefix**, and the difference is the
	whole reason this exists. Grepping for the prefixes that had *leaked* found four of the seven
	sites — the ones that happened to be interrupted — and the resulting list read as complete.
	The statement that creates a database cannot hide from a scan the way an uninterrupted
	fixture can.

	**Takes the tree as an argument** so a synthetic offender goes through the real scanner
	(`#405`), after two guards here were found checking a re-implementation of their own logic
	and blind to a walk that read nothing.
	"""

	found = []

	for path in sorted(root.glob("*.py")):
		if path.name == "conftest.py":
			continue

		lines = path.read_text(encoding="utf-8").splitlines()

		for number, line in enumerate(lines, start=1):
			if _CREATION not in line:
				continue

			# The name is assigned above the statement, so the window is the fixture's head
			# rather than the line itself. Twenty lines covers every one of them today and is
			# the reason the *count* is asserted separately: a window that stopped finding
			# assignments would report every site as an offender, not none.
			window = "\n".join(lines[max(0, number - 21):number])

			if not any(maker in window for maker in _MAKERS):
				found.append(f"{path.name}:{number}")

	return found


#: How few ``CREATE DATABASE`` statements would mean the scan above has stopped reading the
#: tree. Twelve today — the session's own, six fixtures that need a database to themselves, and
#: five in this file that plant one to sweep — so the floor is set below that rather than at it,
#: since a test removed is not a scanner broken.
FEWEST_DATABASE_CREATIONS = 7


def test_every_database_this_suite_makes_is_named_by_one_function () -> None:
	"""A name written by hand is a name the sweep cannot read, and it leaks in silence.

	Seven fixtures make their own database and six of them wrote their own name, two keyed on
	``os.getpid()`` — which is reused, so two runs a day apart could collide on a name one of
	them was about to drop. `#774` with a longer fuse, and invisible until somebody counted.
	"""

	assert _handmade_names(pathlib.Path(__file__).resolve().parent) == []


def test_the_scan_for_handmade_names_is_reading_the_tree (tmp_path: pathlib.Path) -> None:
	"""A scan that read nothing reports no offenders and looks exactly like a clean one."""

	# Assembled rather than written out, for the reason `_CREATION` gives: a file holding this
	# line as a literal would make *this* file a site of its own.
	(tmp_path / "test_offender.py").write_text(
		'name = "handmade"\n' + _CREATION + 'name}"\n', encoding="utf-8"
	)

	assert _handmade_names(tmp_path) == ["test_offender.py:2"]


def test_the_suite_still_makes_the_databases_this_is_about () -> None:
	"""The floor under the scan, which reports offenders and so cannot report having read none."""

	root = pathlib.Path(__file__).resolve().parent
	creations = sum(
		path.read_text(encoding="utf-8").count(_CREATION) for path in root.glob("*.py")
	)

	assert creations >= FEWEST_DATABASE_CREATIONS


def test_a_database_under_a_retired_prefix_is_still_found () -> None:
	"""Six databases on this machine are only visible because that list is consulted.

	**Skipped rather than asserted when the list is empty**, because emptying it is the correct
	end of its life: nothing generates those names now, and the entry goes away when no machine
	has one left. A guard demanding the list stay populated would make the right change fail.
	"""

	if not conftest.RETIRED_PREFIXES:
		pytest.skip("nothing generates a retired name any more, which is the intended end")

	name = f"{conftest.RETIRED_PREFIXES[0]}{uuid.uuid4().hex[:12]}"
	admin = _admin()

	try:
		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'CREATE DATABASE "{name}"'))

			assert name in conftest.left_behind(connection)

			# And never dropped, whatever its apparent age: the name carries none.
			dropped, unreadable = conftest.swept(
				connection, now=datetime.datetime.now(datetime.UTC)
			)

			assert name not in dropped
			assert name in unreadable

	finally:
		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{name}"'))

		admin.dispose()
