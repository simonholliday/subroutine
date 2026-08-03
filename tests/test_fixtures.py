"""Tests for the test harness itself.

Ordinarily testing a conftest would be indulgent. This one earned it: the first CI run
failed on all three Python versions with ``password authentication failed for user
"postgres"``, and the cause was a helper here rather than anything in the product. The
suite had no way to notice, because every check it makes about PostgreSQL runs *after* the
connection it could not make.
"""

import os

import sqlalchemy.engine

import conftest
import subroutine.installations


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
