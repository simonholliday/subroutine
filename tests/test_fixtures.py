"""Tests for the test harness itself.

Ordinarily testing a conftest would be indulgent. This one earned it: the first CI run
failed on all three Python versions with ``password authentication failed for user
"postgres"``, and the cause was a helper here rather than anything in the product. The
suite had no way to notice, because every check it makes about PostgreSQL runs *after* the
connection it could not make.
"""

import io
import os
import sys

import pytest
import rich.console
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
