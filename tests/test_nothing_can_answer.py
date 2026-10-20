"""A command asks only where something can answer it - `SR#5153`.

M-16 of the cold review of 2026-10-17, with L-7's bullet that a confirmation asked whether anybody
was there of nothing, decided in `#5097`: **a command asks for what it was not given only when its
input is a terminal, a pipe or a file; with any other input - a socket, `/dev/null`, a closed
input - it refuses at once with the usage, saying what it needed.** A socket is what a bare
command's input is under an agent's shell, and asking there waited for ever.

**And a closed input is no terminal** (`SR#5187`): the three places that asked ``sys.stdin``
itself whether it was one raised, where it was ``None``.
"""

import os
import pathlib
import re
import socket
import subprocess
import sys
import typing

import alembic.script
import pytest
import typer.testing

import subroutine.cli.personal
import subroutine.config
import subroutine.db.migrate
import test_instances
import test_personal_path

#: The personal path's fresh home and its runner, bound here so pytest finds them as fixtures.
home = test_personal_path.home
run = test_personal_path.run

Run = typing.Callable[..., typer.testing.Result]

#: Asked in a child of its own, so the operating system's standard input is what answers.
ASKED = "import subroutine.cli.personal as p; print(p._somebody_can_answer())"


def _said (result: typer.testing.Result) -> str:
	"""Return what a command printed, as one line, with the frame drawn around an error gone."""

	return " ".join(re.sub(r"[│╭╮╰╯─]", " ", result.output).split())


def _answered (stdin: typing.Any, *, closed: bool = False) -> str:
	"""Return what a child given ``stdin`` says about whether anything can answer it."""

	command = [sys.executable, "-c", ASKED]

	if closed:
		command = ["sh", "-c", 'exec "$0" -c "$1" <&-', sys.executable, ASKED]

	answered = subprocess.run(
		command, stdin=stdin, capture_output=True, text=True, timeout=60, check=False
	)

	assert answered.returncode == 0, answered.stderr

	return answered.stdout.strip()


def test_only_a_terminal_a_pipe_or_a_file_can_answer (tmp_path: pathlib.Path) -> None:
	"""Asked of the operating system: a terminal, a pipe and a file can; nothing else can.

	**A closed input is given no stream at all**, and asking it whether it was a terminal raised.
	"""

	ours, theirs = socket.socketpair()

	try:
		assert _answered(theirs.fileno()) == "False", "a socket, which is an agent's shell"

	finally:
		ours.close()
		theirs.close()

	assert _answered(subprocess.DEVNULL) == "False"
	assert _answered(subprocess.DEVNULL, closed=True) == "False"

	written = tmp_path / "answer.txt"
	written.write_text("42\n", encoding="utf-8")

	with written.open("rb") as handle:
		assert _answered(handle) == "True"

	reading, writing = os.pipe()
	os.write(writing, b"42\n")
	os.close(writing)

	try:
		assert _answered(reading) == "True"

	finally:
		os.close(reading)

	leader, follower = os.openpty()

	try:
		assert _answered(follower) == "True"

	finally:
		os.close(leader)
		os.close(follower)


@pytest.mark.parametrize(
	"arguments",
	[
		("done",),
		("add",),
		("show",),
		("comment", "1"),
		("defer", "1"),
		("search",),
		("link",),
		("view", "save"),
	],
	ids=lambda arguments: " ".join(arguments),
)
def test_a_question_nothing_can_answer_is_refused_with_the_usage (
	run: Run, monkeypatch: pytest.MonkeyPatch, arguments: tuple[str, ...]
) -> None:
	"""Refused at once, below the command's usage, saying what it needed - where it waited."""

	run("init")
	run("add", "Collect the package from reception")
	monkeypatch.setattr(subroutine.cli.personal, "_somebody_can_answer", lambda: False)

	refused = run(*arguments, expect=2)
	said = _said(refused)

	assert "Usage:" in said, said
	assert subroutine.cli.personal.NOTHING_CAN_ANSWER in said, said


def test_a_piped_answer_is_still_read (run: Run, piped: None) -> None:
	"""``echo 1 | subroutine done`` finishes the first item, as it did before."""

	run("init")
	run("add", "Collect the package from reception")

	assert "Done:" in run("done", input="1\n").output


def test_a_piped_answer_says_which_occurrences_an_edit_is_for (run: Run, piped: None) -> None:
	"""Which occurrences an edit of a repeating item is for was asked only at a terminal, and a
	pipe answers it now, as it answers every other question."""

	run("init")
	run("add", "Stand-up", "--repeat", "every tuesday")
	run("update", "2", "--title", "Morning stand-up", input="e\n")

	assert "Morning stand-up" in run("show", "1").output, "the piped answer was not taken"


def test_a_confirmation_nothing_can_answer_is_refused_naming_yes (
	run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A rename asks before it goes on, and with nothing to answer it is refused, unchanged."""

	run("init", "--workspace", "metacortex")
	monkeypatch.setattr(subroutine.cli.personal, "_somebody_can_answer", lambda: False)

	refused = run("workspace", "rename", "metacortex", "nebuchadnezzar", expect=2)

	assert subroutine.cli.personal.NOTHING_CAN_CONFIRM in _said(refused), refused.output
	assert "metacortex" in run("workspace", "list").output

	run("workspace", "rename", "metacortex", "nebuchadnezzar", "--yes")


def test_a_token_is_read_only_from_a_pipe_or_a_file (
	run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""``connections add`` read a line from whatever its input was, and a socket never ends one."""

	run("init")
	monkeypatch.setattr(subroutine.cli.personal, "_a_terminal_is_attached", lambda: False)
	monkeypatch.setattr(subroutine.cli.personal, "_something_was_piped", lambda: False)

	refused = run(
		# On this machine, so the code before, which read the line and went on, sends it nowhere.
		"connections", "add", "work", "--url", "https://127.0.0.1:9", input="sr_x\n", expect=1
	)

	assert "Nothing was piped in, so there is no token for 'work'." in refused.output


def test_a_protected_instance_is_not_answered_by_a_pipe (run: Run, piped: None) -> None:
	"""**The one exception** (decision `#5097`, M-16): a protected instance's question is put only
	at a terminal, so nothing unattended replaces that database on a piped ``y``."""

	run("init", "--workspace", "Real")
	run("add", "Collect the package from reception")
	name = test_instances._backup_name(run("db", "backup").output)

	with subroutine.config.config_file_path().open("a", encoding="utf-8") as handle:
		handle.write("\nprotected = true\n")

	run("add", "Feed the cat")
	refused = run("db", "restore", name, "--recover", input="y\n", expect=1)

	assert "is marked protected and this is not an interactive terminal" in _said(refused)
	assert "Feed the cat" in run("list").output, "a piped answer replaced a protected instance"


def test_a_restore_that_could_not_be_asked_about_its_upgrade_is_refused_before_it_begins (
	run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A backup on an older schema is offered an upgrade once restored, so where nothing could
	answer that, the restore is refused before anything is replaced (decision `#5097`, M-16)."""

	run("init")
	run("add", "Collect the package from reception")
	url = subroutine.config.load_settings().database_url
	head = subroutine.db.migrate.head_revision()

	assert head is not None

	script = alembic.script.ScriptDirectory.from_config(subroutine.db.migrate.build_config(url))
	earlier = script.get_revision(head).down_revision

	assert isinstance(earlier, str), earlier

	subroutine.db.migrate.stamp(url, earlier)

	try:
		taken = run("db", "backup")

	finally:
		subroutine.db.migrate.stamp(url, head)

	name = test_instances._backup_name(taken.output)
	run("add", "Feed the cat")
	monkeypatch.setattr(subroutine.cli.personal, "_somebody_can_answer", lambda: False)

	refused = run("db", "restore", name, "--recover", expect=2)

	assert "Upgrade the restored database now?" in _said(refused), refused.output
	assert subroutine.cli.personal.NOTHING_CAN_CONFIRM in _said(refused), refused.output
	assert "Feed the cat" in run("list").output, "the restore went ahead"


class _Closed:
	"""``sys``, as a program started with its standard input closed has it: ``None`` - `SR#5187`.

	**Patched over each module's own ``sys``**, because ``CliRunner`` replaces ``sys.stdin`` for an
	invocation (``test_personal_path._NoInput`` says so), and over both modules that ask, so the
	code before this is driven into the crash rather than past it.
	"""

	stdin = None

	def __getattr__ (self, name: str) -> typing.Any:
		"""Defer to the real module for everything except standard input."""

		return getattr(sys, name)


def _close_the_input (monkeypatch: pytest.MonkeyPatch) -> None:
	"""Give every command from here on the standard input of a program started with it closed."""

	for module in (subroutine.cli.main, subroutine.cli.personal):
		monkeypatch.setattr(module, "sys", _Closed())


def test_a_document_edit_naming_nothing_with_its_input_closed_says_so (
	run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#5187`: ``doc edit`` with nothing named asked a closed input whether it was a terminal."""

	run("init")
	run("doc", "create", "Plan B", "--body", "The original text.")
	_close_the_input(monkeypatch)

	refused = run("doc", "edit", "1", expect=1)

	assert "Nothing was named, so there is nothing to change." in _said(refused), refused.output
	assert "The original text." in run("show", "1").output


def test_a_protected_restore_with_its_input_closed_is_refused (
	run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#5187`: a protected instance's question, put only at a terminal, asked a closed input."""

	run("init", "--workspace", "Real")
	name = test_instances._backup_name(run("db", "backup").output)

	with subroutine.config.config_file_path().open("a", encoding="utf-8") as handle:
		handle.write("\nprotected = true\n")

	run("add", "Feed the cat")
	_close_the_input(monkeypatch)

	refused = run("db", "restore", name, "--recover", expect=1)

	assert "is marked protected and this is not an interactive terminal" in _said(refused)
	assert "Feed the cat" in run("list").output, "a protected instance was replaced"


def test_destroying_a_protected_profile_with_its_input_closed_is_refused (
	run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#5187`: and the same question before a protected profile is destroyed."""

	run("--profile", "scratch", "init", "--workspace", "Scratch")
	monkeypatch.delenv(subroutine.config.PROFILE_VARIABLE, raising=False)

	configuration = subroutine.config.profile_directories("scratch")[0] / "config.toml"

	with configuration.open("a", encoding="utf-8") as handle:
		handle.write("\nprotected = true\n")

	_close_the_input(monkeypatch)

	refused = run("profile", "destroy", "scratch", "--confirm", "scratch", expect=1)

	assert "is marked protected and this is not an interactive terminal" in _said(refused)
	assert "scratch" in subroutine.config.profile_names()


#: A ``-`` at each kind of place that takes one, all read through the one function the walk in
#: ``test_personal_path`` holds every prose argument to: ``#1`` is a task and ``#2`` a document.
HYPHENS = [
	("doc", "create", "Plan C", "--body", "-"),
	("doc", "edit", "2", "--body", "-"),
	("comment", "1", "-"),
	("add", "Fix the boiler", "--description", "-"),
	("update", "1", "--description", "-"),
	("project", "create", "home", "Home", "--description", "-"),
	("defer", "1", "monday", "--because", "-"),
]


@pytest.mark.parametrize("arguments", HYPHENS, ids=lambda arguments: " ".join(arguments))
def test_a_hyphen_with_nothing_piped_is_refused_at_once (
	run: Run, arguments: tuple[str, ...]
) -> None:
	"""`SR#5188`, decision `#4150`: ``-`` reads only what is handed over, a pipe or a file.

	The stream ``CliRunner`` puts in place of standard input is neither, as a socket, ``/dev/null``
	and a terminal are not, so what it holds is never read.
	"""

	run("init")
	run("add", "Collect the package from reception")
	run("doc", "create", "Plan B", "--body", "The original text.")

	refused = run(*arguments, input="Never read.\n", expect=1)

	assert "means read what is piped in, and nothing is." in _said(refused), refused.output
	assert "Never read." not in run("show", "1").output + run("show", "2").output


def test_a_hyphen_whose_input_is_a_socket_is_refused_rather_than_waiting (run: Run) -> None:
	"""`SR#5188`, driven as it was met: ``--body -`` under a socket whose other end stays open, as
	an agent's shell holds it, read until the end that never came."""

	run("init")
	ours, theirs = socket.socketpair()

	try:
		refused = subprocess.run(
			[sys.executable, "-m", "subroutine", "doc", "create", "Plan C", "--body", "-"],
			stdin=theirs.fileno(), capture_output=True, text=True, timeout=30, check=False,
		)

	finally:
		ours.close()
		theirs.close()

	said = " ".join((refused.stdout + refused.stderr).split())

	assert refused.returncode == 1, said
	assert "'--body -' means read what is piped in, and nothing is." in said, said
	assert "Plan C" not in run("list").output
