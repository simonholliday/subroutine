"""A command with a ``--workspace`` of its own, and the ``-w`` before it - decision ``#3831``.

Items ``#3248`` and ``#3814``: ``agent create`` and ``calendar create`` ignored a ``-w`` given
before them, while the refusal for a missing workspace advised exactly that ``-w``, so somebody
who followed its advice was refused again. These drive the real terminal against an instance
that can reach two workspaces, which is the only place the fault shows: with one, nothing ever
has to be named.
"""

import os
import pathlib
import re
import typing

import pytest
import typer.testing

import subroutine.cli.main
import subroutine.cli.personal
import subroutine.config
import subroutine.errors
import subroutine.mcp.relay

Run = typing.Callable[..., typer.testing.Result]

#: The nine commands with a ``--workspace`` of their own, as somebody types them - ``mcp`` since
#: ``SR#3942``.
OWN_WORKSPACE = (
	("mcp",),
	("token", "create"),
	("agent", "create"),
	("calendar", "create"),
	("user", "create"),
	("user", "list"),
	("user", "add"),
	("user", "role"),
	("user", "remove"),
)


@pytest.fixture
def home (tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
	"""Point every XDG directory at a fresh temporary home, with nothing inherited."""

	root = tmp_path / "home"

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(root / variable.lower()))

	for name in list(os.environ):
		if name.startswith("SUBROUTINE_"):
			monkeypatch.delenv(name, raising=False)

	monkeypatch.setenv("SUBROUTINE_DEFAULT_TIMEZONE", "Europe/London")

	return root


@pytest.fixture
def run (home: pathlib.Path) -> Run:
	"""Return a runner for the real CLI, failing loudly on an unexpected exit code."""

	runner = typer.testing.CliRunner()

	def invoke (*arguments: str, expect: int = 0) -> typer.testing.Result:
		"""Run one command and check how it ended."""

		os.environ.pop(subroutine.config.PROFILE_VARIABLE, None)
		subroutine.cli.main._said_unknown_settings = False
		result = runner.invoke(subroutine.cli.main.app, list(arguments))

		assert result.exit_code == expect, (
			f"'subroutine {' '.join(arguments)}' exited {result.exit_code}\n"
			f"{result.output}\n{result.exception!r}"
		)

		return result

	return invoke


@pytest.fixture
def two (run: Run) -> Run:
	"""Return the runner, on an instance whose operator can reach ``alpha`` and ``beta``."""

	run("init", "--workspace", "Alpha")
	run("workspace", "create", "beta", "Beta")

	return run


@pytest.mark.parametrize("command", OWN_WORKSPACE, ids=" ".join)
def test_every_command_with_its_own_workspace_takes_w_after_it (
	run: Run, command: tuple[str, str]
) -> None:
	"""The short form is on all eight, so ``-w`` after the command is never an unknown option."""

	shown = run(*command, "--help").output

	# The options table's own row, `--workspace  -w`: an example in the help names `--workspace`
	# too, and the word holds the characters `-w`, so a plain `in` on a line could not fail.
	assert re.search(r"--workspace\s+-w\b", shown), (
		f"'subroutine {' '.join(command)}' has no -w beside --workspace:\n{shown}"
	)


def test_mcp_serves_the_workspace_w_names_before_it_or_after_it (
	two: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#3942`, S2 of L-10 of the cold review of 2026-09-28: ``mcp`` was not among the eight.

	``subroutine -w beta mcp`` served no workspace, without a word; ``mcp -w beta`` was *No such
	option*; and given both, the one after it won. **Either spelling names it, and two different
	ones are refused by name.**
	"""

	served: list[str | None] = []
	turned_away: list[str] = []

	def serving (
		*_arguments: typing.Any,
		workspace: str | None,
		refused: subroutine.errors.SubroutineError | None = None,
		**_options: typing.Any,
	) -> None:
		"""Record the workspace the relay was started for, and what it was to refuse."""

		served.append(workspace)

		if refused is not None:
			turned_away.append(refused.detail)

	monkeypatch.setattr(subroutine.mcp.relay, "run", serving)

	two("-w", "beta", "mcp")
	two("mcp", "-w", "beta")

	# **Refused at the handshake rather than before it** (`SR#4314`), where the client reads it.
	two("-w", "beta", "mcp", "--workspace", "alpha")

	assert served == ["beta", "beta", None], served
	assert ["two different workspaces" in one for one in turned_away] == [True], turned_away


def test_agent_create_pins_to_the_w_before_it (two: Run) -> None:
	"""``#3248``: the global option was read for the connection and dropped for the credential."""

	assert "in beta" in two("-w", "beta", "agent", "create", "web").output


def test_agent_create_pins_to_the_w_after_it (two: Run) -> None:
	"""The short form of its own option, which it did not have."""

	assert "in beta" in two("agent", "create", "web", "-w", "beta").output


def test_the_refusal_quotes_the_command_back_and_offers_no_use (two: Run) -> None:
	"""The advice names a spelling this command reads, and not ``use``, which it never reads."""

	refused = two("agent", "create", "web", expect=1).output

	assert "subroutine -w <workspace> agent create" in refused, refused
	assert "subroutine use" not in refused, refused


def test_following_the_refusal_s_advice_is_not_refused_again (two: Run) -> None:
	"""``#3248``'s own sequence: refused, then doing exactly what the refusal said."""

	two("agent", "create", "web", expect=1)

	assert "in beta" in two("-w", "beta", "agent", "create", "web").output


def test_use_does_not_choose_what_a_credential_is_pinned_to (two: Run) -> None:
	"""A workspace remembered by ``use`` would pin a credential silently, so it is not taken."""

	two("use", "beta")

	refused = two("agent", "create", "web", expect=1).output

	assert "subroutine -w <workspace> agent create" in refused, refused


def test_w_and_a_different_workspace_are_refused_by_name (two: Run) -> None:
	"""Somebody who typed two workspaces meant one of them, and only they know which."""

	refused = two("-w", "alpha", "agent", "create", "web", "--workspace", "beta", expect=1).output

	assert "'-w alpha' and '--workspace beta' name two different workspaces" in refused, refused


def test_the_same_workspace_named_twice_is_not_a_disagreement (two: Run) -> None:
	"""Only a difference is refused, and a difference of case is none."""

	assert "in beta" in two("-w", "Beta", "agent", "create", "web", "--workspace", "beta").output


def test_two_spellings_of_one_workspace_agree_and_two_workspaces_do_not () -> None:
	"""`SR#3904`, L-10 of the cold review of 2026-09-28: the two were compared by letter case alone.

	``maße`` casefolds to ``masse``, so a token meant for one workspace was made for the other
	without a word, while ``My Team`` beside ``my-team`` - one workspace, as the program reads it -
	was refused as two. **Compared as the program resolves a workspace.**
	"""

	def named (before: str, after: str) -> str:
		"""Return what ``-w before`` and ``--workspace after`` settle on."""

		return subroutine.cli.personal.workspace_named(
			after, subroutine.cli.personal.Selected(workspace=before)
		)

	assert named("My Team", "my-team") == "my-team"
	assert named("Beta", "beta") == "beta"

	with pytest.raises(subroutine.errors.ValidationError, match="two different workspaces"):
		named("maße", "masse")


def test_calendar_create_takes_w_before_it_and_after_it (two: Run) -> None:
	"""``#3814``: the calendar check that could not be started, in both spellings."""

	two("-w", "beta", "calendar", "create", "Clock Test")
	two("calendar", "create", "Clock Test Again", "-w", "beta")

	refused = two("calendar", "create", "Nowhere", expect=1).output

	assert "subroutine -w <workspace> calendar create" in refused, refused


def test_token_create_pins_to_the_w_before_it (two: Run) -> None:
	"""Where it used to make a token for every workspace, and say nothing about the ``-w``."""

	two("-w", "beta", "token", "create", "--title", "Probe")

	assert "in beta only" in two("token", "list").output


def test_user_create_joins_the_w_before_it_and_quotes_itself_when_it_asks (two: Run) -> None:
	"""A membership is chosen out loud, like a credential, and the question says how."""

	refused = two("user", "create", "keanu", expect=1).output

	assert "subroutine -w alpha user create" in refused, refused
	assert "beta" in two("-w", "beta", "user", "create", "keanu").output


def test_user_list_lists_the_members_of_the_w_before_it (two: Run) -> None:
	"""With a workspace named, its members; with none, still every account."""

	two("user", "create", "keanu", "-w", "beta")

	assert "keanu" in two("-w", "beta", "user", "list").output
	assert "keanu" not in two("-w", "alpha", "user", "list").output
	assert "keanu" in two("user", "list").output


def test_user_add_takes_w_after_it (two: Run) -> None:
	"""The three that already took ``use`` and a marker keep them, and gain the short form."""

	two("user", "create", "keanu", "-w", "alpha")
	two("user", "add", "keanu", "--role", "member", "-w", "beta")

	assert "keanu" in two("-w", "beta", "user", "list").output


def test_the_command_a_refusal_quotes_is_not_carried_into_the_next_one (two: Run) -> None:
	"""The selection outlives a command in one process, so what it records is cleared."""

	two("agent", "create", "web", expect=1)

	assert subroutine.cli.main._selected.command == "agent create"

	two("list")

	assert subroutine.cli.main._selected.command is None


def test_either_spelling_names_the_workspace_and_two_different_ones_are_refused () -> None:
	"""The helper every one of the eight goes through, fed each case directly."""

	named = subroutine.cli.personal.workspace_named
	before = subroutine.cli.personal.Selected(workspace="beta")

	assert named("", before) == "beta"
	assert named("BETA", before) == "BETA"
	assert named("", subroutine.cli.personal.Selected()) == ""

	with pytest.raises(subroutine.errors.ValidationError, match="two different workspaces"):
		named("alpha", before)
