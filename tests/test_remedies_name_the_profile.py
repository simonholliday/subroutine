"""Every remedy that acts on an instance names the profile in force - `SR#5297`.

M-9 of the cold review of 2026-11-11, decided in `#5285`, "Every remedy that acts on an instance
names the profile in force, and a guard keeps it so". Under ``--profile scratch`` a refused upgrade
said *'subroutine db restore <scratch backup> --recover' puts it back*, which, typed in a fresh
shell, put scratch's backup over the default instance; about twenty-five more remedies dropped the
profile the same way. **Each is written through** ``subroutine.db.migrate.command`` **now**, and
this guard refuses one written by hand: a string literal under ``src`` - not a comment and not a
docstring - naming ``subroutine db ``, ``subroutine init`` or ``subroutine config `` is refused
unless :data:`NOT_A_REMEDY` says in writing why it is not one.
"""

import ast
import pathlib
import re
import typing

import pytest
import typer.testing

import subroutine.cli.main
import subroutine.config
import subroutine.errors
import test_personal_path

#: The personal path's fresh home and its runner, bound here so pytest finds them as fixtures.
home = test_personal_path.home
run = test_personal_path.run

REPOSITORY = pathlib.Path(__file__).resolve().parent.parent

#: A command that acts on an instance, as a reader would type it.
ACTING = re.compile(r"subroutine (db |init|config )")

#: Every literal under ``src`` that names such a command and is not a remedy, by file and text.
#: **Deleting an entry is what makes the guard ask of it again.**
NOT_A_REMEDY: dict[tuple[str, str], str] = {
	(
		"src/subroutine/cli/main.py",
		"Which port to listen on. Defaults to the one in 'subroutine config show'.",
	): "an option's help, written when the command is defined, before any --profile is read",
	(
		"src/subroutine/views.py",
		"Whether a newer release exists is 'subroutine db upgrade --check', which asks only when "
		"you do.",
	): "asks whether a newer release exists, which is one answer for every instance, and is a "
	"constant the instance prints as well as the terminal",
	(
		"src/subroutine/mcp/protocol.py",
		"subroutine init",
	): "a key of NO_TOOL_DOES_THIS, matched against what a remedy says, not a remedy",
}


def _literals (root: pathlib.Path) -> list[tuple[str, str]]:
	"""Return every string literal under ``root/src`` naming a command that acts on an instance,
	docstrings left out, as its file and its text."""

	found = []

	for path in sorted((root / "src").rglob("*.py")):
		if "migrations/versions" in path.as_posix():
			continue

		tree = ast.parse(path.read_text(encoding="utf-8"))
		docstrings = {
			id(node.body[0].value)
			for node in ast.walk(tree)
			if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
			and node.body
			and isinstance(node.body[0], ast.Expr)
			and isinstance(node.body[0].value, ast.Constant)
		}

		for node in ast.walk(tree):
			if (
				isinstance(node, ast.Constant)
				and isinstance(node.value, str)
				and id(node) not in docstrings
				and ACTING.search(node.value)
			):
				found.append((path.relative_to(root).as_posix(), node.value))

	return found


def test_every_remedy_that_acts_on_an_instance_is_written_through_the_helper () -> None:
	"""A literal naming such a command drops the profile in force, whatever the sentence says."""

	written_by_hand = [one for one in _literals(REPOSITORY) if one not in NOT_A_REMEDY]

	assert not written_by_hand, "\n".join(f"{path}: {text!r}" for path, text in written_by_hand)


def test_every_excuse_still_names_a_literal_that_is_there () -> None:
	"""An entry whose literal has gone would go on reading as a considered decision."""

	found = set(_literals(REPOSITORY))

	for entry, why in NOT_A_REMEDY.items():
		assert entry in found, f"{entry} is excused and no longer there"
		assert why.strip(), f"{entry} is excused without a reason"


def test_the_scan_finds_one_written_by_hand_and_leaves_a_docstring (
	tmp_path: pathlib.Path,
) -> None:
	"""The guard fed a defect through its own entry point: a hint written by hand is found, and the
	same words in a docstring and in a comment are not."""

	source = tmp_path / "src" / "subroutine"
	source.mkdir(parents=True)
	(source / "offender.py").write_text(
		'"""Run \'subroutine db backup\' first."""\n\n'
		"# Run 'subroutine init' first.\n"
		"HINT = \"Run 'subroutine db backup' first.\"\n",
		encoding="utf-8",
	)

	assert _literals(tmp_path) == [
		("src/subroutine/offender.py", "Run 'subroutine db backup' first.")
	]


def test_the_scan_reaches_the_source_tree () -> None:
	"""A walk reading nothing would pass the first test against anything."""

	assert len(_literals(REPOSITORY)) >= len(NOT_A_REMEDY)
	assert any(path == "src/subroutine/mcp/protocol.py" for path, _text in _literals(REPOSITORY))


def test_a_restore_names_the_profile_and_quotes_its_path (monkeypatch: pytest.MonkeyPatch) -> None:
	"""The sentence M-9 measured, under a profile and without one, with a path holding a space."""

	held = pathlib.Path("/backups/with space/subroutine-scratch.db")

	monkeypatch.delenv(subroutine.config.PROFILE_VARIABLE, raising=False)

	assert subroutine.cli.main._restore_command(held) == (
		"subroutine db restore '/backups/with space/subroutine-scratch.db' --recover"
	)

	monkeypatch.setenv(subroutine.config.PROFILE_VARIABLE, "scratch")

	assert subroutine.cli.main._restore_command(held) == (
		"subroutine --profile scratch db restore '/backups/with space/subroutine-scratch.db' "
		"--recover"
	)


def test_no_instance_yet_names_the_profile (monkeypatch: pytest.MonkeyPatch) -> None:
	"""L-4: the shared refusal for a machine with no instance said 'subroutine init' under any."""

	monkeypatch.setenv(subroutine.config.PROFILE_VARIABLE, "scratch")

	assert subroutine.errors.no_instance_yet().hint == (
		"Run 'subroutine --profile scratch init' to create one. It takes no arguments."
	)


@pytest.mark.parametrize("profile", [None, "scratch"], ids=["no profile", "a profile"])
def test_an_empty_instance_s_backups_name_the_command_for_that_instance (
	run: typing.Callable[..., typer.testing.Result], profile: str | None
) -> None:
	"""Driven end to end: ``db backups`` on an instance with none says how to take one there."""

	given = [] if profile is None else ["--profile", profile]
	run(*given, "init")
	said = run(*given, "db", "backups").output

	expected = (
		"subroutine db backup" if profile is None else f"subroutine --profile {profile} db backup"
	)

	assert f"Run '{expected}'." in said, said
