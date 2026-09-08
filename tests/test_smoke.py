"""Smoke tests confirming the package imports and reports a sane version.

**Nothing here re-reads ``importlib.metadata.version`` and that is the point of `SR#1487`.**
``subroutine.__version__`` is evaluated once, when the package is imported; a fresh call
returns whatever is on disk *now*. The version lives in the ``.dist-info`` **directory
name**, so ``pip install -e .`` is a rename — and a fresh read picks a rename up immediately,
proven on a synthetic distribution. A reinstall racing a gate therefore left these tests
comparing one number against the other and failing by exactly one commit, on a tree that
passed minutes later.

**The frozen value is not merely the stabler of the two, it is the correct one.** After a
reinstall the metadata on disk describes different code — code this process is not running —
so during that window the fresh read is the wrong answer to "what version is this?" and the
module global is the right one. Reading it once is what these tests do now, and the fresh
read is advisory: useful for asking whether the install is stale, never for asking what is
running.
"""

import typer.testing

import subroutine
import subroutine.cli.main
import subroutine.db.migrate


def test_package_imports () -> None:
	"""The package can be imported and reports a version it got from the distribution.

	The placeholder is the honest failure ``_installed_version`` falls back to when the
	package is not installed at all, so excluding it is what makes this more than "a
	non-empty string" — and it is the check that used to be carried, racily, by comparing
	against a fresh metadata read (`SR#1487`).
	"""

	assert isinstance(subroutine.__version__, str)
	assert subroutine.__version__
	assert subroutine.__version__ != "0.0.0+unknown", (
		"the package is not installed, so every version this suite reports is a placeholder"
	)


def test_api_version_is_pinned () -> None:
	"""The wire API version is declared, and is distinct from the package version."""

	assert subroutine.API_VERSION == "1.0"


def test_the_version_flag_reports_the_installed_release_and_the_expected_schema () -> None:
	"""Both numbers are read from where they are actually defined, never written out here.

	This is the guard, rather than the flag existing: a literal in the source would pass a
	test that checked for *a* version and would go stale the first time ``pyproject.toml``
	moved without it. ``subroutine.__version__`` is the distribution's own number — read by
	``_installed_version`` and by nothing else — and the schema head is Alembic's, so the
	only way to make this pass is to keep reading them.
	"""

	result = typer.testing.CliRunner().invoke(subroutine.cli.main.app, ["--version"])

	assert result.exit_code == 0, result.output
	assert subroutine.__version__ in result.output
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
	assert subroutine.__version__ in result.output
