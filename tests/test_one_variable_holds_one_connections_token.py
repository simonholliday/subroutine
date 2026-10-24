"""Two connections may not read their token from one variable - `SR#5130`.

**L-4 of the cold review of 2026-10-17**: a connection's name takes letters, digits, ``_`` and
``-``, and its token's variable folds both of the last to ``_``, so ``my-work`` and ``my_work`` read
``SUBROUTINE_TOKEN_MY_WORK`` and one exported token was presented to both instances.
"""

import pathlib
import typing

import typer.testing

import test_cli_connections

#: The connections suite's home and runner, bound here so pytest finds them.
home = test_cli_connections.home
run = test_cli_connections.run


def test_adding_a_name_whose_variable_is_taken_is_refused (
	home: pathlib.Path, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""``my_work`` beside ``my-work`` is refused before anything is reached, naming ``my-work``."""

	run("init")
	test_cli_connections.declare(
		home, '\n[connections.my-work]\nurl = "https://tasks.example.com"\n'
	)

	refused = " ".join(
		run(
			"connections", "add", "my_work", "--url", "https://other.example.com", expect=1
		).output.split()
	)

	assert (
		"'my_work' would read its token from SUBROUTINE_TOKEN_MY_WORK, which 'my-work' already "
		"reads from, so one exported token would be sent to both." in refused
	), refused


def test_a_file_holding_two_such_names_is_refused_naming_both (
	home: pathlib.Path, run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""A configuration already holding both stops a command that reads it, naming both."""

	run("init")
	test_cli_connections.declare(
		home,
		'\n[connections.my-work]\nurl = "https://tasks.example.com"\n'
		'\n[connections.my_work]\nurl = "https://other.example.com"\n',
	)

	refused = " ".join(run("connections", expect=1).output.split())

	assert (
		"'my-work' and 'my_work' would both read their token from SUBROUTINE_TOKEN_MY_WORK, so "
		"one exported token would be sent to both. Rename one of them." in refused
	), refused
