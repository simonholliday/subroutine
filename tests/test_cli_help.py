"""What the terminal help is allowed to say — item ``#170``.

**Help is published output, and it was being written as source.** A clean-room tester found
five leaks; this guard, written from the same class, found thirty-one across fifteen commands
— including two in a docstring written the same morning. That ratio is the argument for the
file: the leaks are individually trivial and collectively the first impression of the product,
and no amount of care catches them one at a time.

Typer renders a command's docstring verbatim into the terminal. So a docstring here is not a
note to the next programmer; it is the page a stranger reads. Reasoning that needs Markdown, a
specification section number or an item reference belongs in a comment above the function,
where it is still beside the code and is not shown to anybody typing ``--help``.
"""

import re
import typing

import click
import pytest
import typer.main
import typer.testing

import subroutine.cli.main
import subroutine.cli.personal

#: What may never appear in a published help string, and why each one matters.
#:
#: ``'ls'`` is the subtle one. It is a *hidden* synonym for ``list`` (§12.2a) — deliberately
#: absent from the command list, because a synonym you can see is a second thing to choose
#: between — so help that names it points at a word the reader has never met.
FORBIDDEN = {
	"Markdown emphasis": re.compile(r"\*\*"),
	"a backtick": re.compile(r"`"),
	"a reference to SPEC.md, which is not published": re.compile(r"SPEC\.md"),
	"a specification section sign": re.compile(r"§"),
	"the hidden synonym 'ls' rather than 'list'": re.compile(r"'ls'"),
}


def _commands () -> typing.Iterator[tuple[str, typing.Any]]:
	"""Yield every command the CLI publishes, by the name a person would type.

	**Walked by asking for the subcommands, not by ``isinstance``.** Typer vendors its own
	click shim, so a ``TyperGroup`` is *not* a ``click.Group`` — an isinstance check silently
	visits the root and nothing else, and this file passed for exactly as long as it took to
	notice it was testing one command. ``list_commands`` is the interface both kinds share.
	"""

	# A stack rather than recursion, so a cycle in a future layout is a hang in one place.
	#
	# **Typed loosely, with the reason written down.** Typer vendors its own click shim, so
	# what `get_command` returns is a `typer._click.core.Command` — a private class that is
	# not a `click.Command` and that Typer exports no name for. Claiming either type here
	# would be a cast asserting something untrue; the walk below only uses the two methods
	# both kinds carry, and the isinstance note above says why it must not test for a class.
	pending: list[tuple[typing.Any, str]] = [
		(typer.main.get_command(subroutine.cli.main.app), "subroutine")
	]

	while pending:
		command, path = pending.pop()

		yield path, command

		if not hasattr(command, "list_commands"):
			continue

		context = click.Context(command, info_name=path)

		for name in command.list_commands(context):
			child = command.get_command(context, name)

			if child is not None:
				pending.append((child, f"{path} {name}"))


def _texts (command: typing.Any) -> typing.Iterator[tuple[str, str]]:
	"""Yield every piece of help text one command publishes, and where it came from."""

	yield "its description", command.help or ""

	for parameter in command.params:
		yield f"the help for {parameter.name}", getattr(parameter, "help", "") or ""


@pytest.mark.parametrize("path,command", list(_commands()), ids=lambda value: str(value))
def test_published_help_is_prose_rather_than_source (
	path: str, command: typing.Any
) -> None:
	"""No command's help may carry markup, a section number or an unpublished name."""

	for where, text in _texts(command):
		for description, pattern in FORBIDDEN.items():
			found = pattern.search(text)

			assert found is None, (
				f"'{path}' has {description} in {where}, around: "
				f"{text[max(found.start() - 40, 0):found.end() + 40]!r}"
			)


def test_no_command_advertises_a_sentinel_as_a_default () -> None:
	"""A value that exists to mean "not given" must never be shown as one you may pass.

	``update --help`` said ``How much it matters, 1-5. [default: -1]``, which invites
	``--importance -1`` and answers "Nothing to change."; the string sentinels printed their
	own escape character as ``[default:  not given]``. ``serve`` advertised ``[default: 0]``
	for a port, which is not a port.

	Rendered rather than read off the parameters, because the defect is in what Typer *prints*
	— the declarations were all perfectly sensible.
	"""

	runner = typer.testing.CliRunner()

	for path, _command in _commands():
		arguments = [*path.split()[1:], "--help"]
		rendered = runner.invoke(subroutine.cli.main.app, arguments).output

		assert subroutine.cli.personal.UNGIVEN not in rendered, path
		assert "not given" not in rendered, path
		assert "[default: -1]" not in rendered, path
		assert "[default: 0]" not in rendered, path
