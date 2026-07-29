"""Tests for the test harness itself.

Ordinarily testing a conftest would be indulgent. This one earned it: the first CI run
failed on all three Python versions with ``password authentication failed for user
"postgres"``, and the cause was a helper here rather than anything in the product. The
suite had no way to notice, because every check it makes about PostgreSQL runs *after* the
connection it could not make.
"""

import sqlalchemy.engine

import conftest


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
