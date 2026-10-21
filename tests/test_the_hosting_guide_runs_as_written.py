"""The hosting guide's first account and its people, made by its own commands - `SR#5170`.

L-8 of the cold review of 2026-10-17, decided in `#5097`: the guide's ``user list`` sample was what
the program prints for an instance made with ``--username laurence --workspace acme``, and the
guide's own ``init`` passed neither - so, run as written by the service account, the first account
was ``subroutine`` and the first workspace ``Projects``, and the guide's next commands were refused.
**Read from the page and run**, so the page and the program cannot part again unnoticed.
"""

import pathlib
import shlex
import typing

import typer.testing

import test_personal_path

#: The personal path's fresh home and its runner, bound here so pytest finds them as fixtures.
home = test_personal_path.home
run = test_personal_path.run

Run = typing.Callable[..., typer.testing.Result]

HOSTING = pathlib.Path(__file__).resolve().parent.parent / "docs" / "hosting.md"

#: How the guide spells the program, as the service account runs it.
PROGRAM = "/opt/subroutine/bin/subroutine "


def _quoted (command: str) -> tuple[list[str], list[str]]:
	"""Return the arguments of the first guide line running ``command``, and what the guide shows
	it printing - the lines after it, up to the next command or the end of the block."""

	lines = HOSTING.read_text(encoding="utf-8").splitlines()
	start = next(number for number, line in enumerate(lines) if f"{PROGRAM}{command}" in line)
	arguments = shlex.split(lines[start].split(PROGRAM, 1)[1])
	shown = []

	for line in lines[start + 1:]:
		if line.startswith(("#", "```")):
			break

		shown.append(line.removeprefix("  "))

	while shown and not shown[-1].strip():
		shown.pop()

	return arguments, shown


def test_the_guide_s_init_makes_the_account_and_workspace_its_samples_name (run: Run) -> None:
	"""``init`` as the guide writes it, then its ``user create`` and ``user list``, each printing
	what the guide shows."""

	initialising, _ = _quoted("init")

	assert initialising[:1] == ["init"] and "--username" in initialising, initialising

	run(*initialising)

	for command in ("user create", "user list"):
		arguments, shown = _quoted(command)
		printed = [line.rstrip() for line in run(*arguments).output.splitlines()]

		assert printed == [line.rstrip() for line in shown], (command, printed)
