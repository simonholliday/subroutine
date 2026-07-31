"""Smoke tests confirming the package imports and reports a sane version."""

import importlib.metadata

import typer.testing

import subroutine
import subroutine.cli.main
import subroutine.db.migrate


def test_package_imports () -> None:
	"""The package can be imported and exposes a version string."""

	assert isinstance(subroutine.__version__, str)
	assert subroutine.__version__


def test_api_version_is_pinned () -> None:
	"""The wire API version is declared, and is distinct from the package version."""

	assert subroutine.API_VERSION == "1.0"


def test_the_version_flag_reports_the_installed_release_and_the_expected_schema () -> None:
	"""Both numbers are read from where they are actually defined, never written out here.

	This is the guard, rather than the flag existing: a literal in the source would pass a
	test that checked for *a* version and would go stale the first time ``pyproject.toml``
	moved without it. Comparing against the distribution's own metadata and against Alembic's
	head means the only way to make this pass is to keep reading them.
	"""

	result = typer.testing.CliRunner().invoke(subroutine.cli.main.app, ["--version"])

	assert result.exit_code == 0, result.output
	assert importlib.metadata.version("subroutine") in result.output
	assert str(subroutine.db.migrate.head_revision()) in result.output


def test_the_version_flag_answers_before_the_profile_is_resolved () -> None:
	"""§12.5 refuses a bad profile name rather than falling back — every command but this one.

	"What am I running?" is the question somebody asks *while* untangling a broken
	``--profile`` or a stale ``SUBROUTINE_PROFILE`` in their environment, so the one command
	that answers it may not be refused along with the rest.

	Verified by breaking it: printing the version from the callback *body* instead — the
	obvious way to write this — makes it exit 2 with a message about the profile, because the
	body resolves the profile first. Handling it as a parameter callback is what runs it before
	that happens.
	"""

	result = typer.testing.CliRunner().invoke(
		subroutine.cli.main.app, ["--profile", "../evil", "--version"]
	)

	assert result.exit_code == 0, result.output
	assert importlib.metadata.version("subroutine") in result.output
