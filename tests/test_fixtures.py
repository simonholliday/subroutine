"""Tests for the test harness itself.

Ordinarily testing a conftest would be indulgent. This one earned it: the first CI run
failed on all three Python versions with ``password authentication failed for user
"postgres"``, and the cause was a helper here rather than anything in the product. The
suite had no way to notice, because every check it makes about PostgreSQL runs *after* the
connection it could not make.
"""

import os

import sqlalchemy.engine
import typer.rich_utils
import typer.testing

import conftest
import subroutine.cli.main
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


def test_the_machines_colour_setting_does_not_reach_a_test () -> None:
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
	"""

	assert test_browser._cannot_lay_out_text(120.5) is None, "a real width is a usable browser"

	for measured in (0, 0.0, None):
		refusal = test_browser._cannot_lay_out_text(measured)

		assert refusal is not None, f"a width of {measured!r} is a browser that drew nothing"
		assert "fonts" in refusal, "the remedy has to name what is missing, not just refuse"
