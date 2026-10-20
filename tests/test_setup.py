"""Wiring Subroutine into the harness on this machine — `#1122`.

Subroutine shipped no harness integration at all: two git hooks and no `.claude/settings.json`
anywhere. That matters for one class of act — the ones at the end of a session — because a
harness hook is the only channel here that **fires whether the agent attends or not**. Every
other lever measured on this instance is prose or a git hook, and prose cannot reach a session
that has stopped.

**The failure this file is mostly about is `#236`'s**: installing something and its taking
effect are separate moments, and only the first one reports. So the command reads back what it
wrote, and these tests check the shape against the only thing that reads it as Claude Code
does.
"""

import json
import os
import pathlib
import shutil
import stat
import subprocess
import typing

import pytest
import typer.testing

import subroutine.cli.main
import subroutine.cli.personal


@pytest.fixture
def home (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> typing.Iterator[pathlib.Path]:
	"""A fresh instance **and a fresh working directory**, because this command writes to both.

	`tests/test_personal_path.py`'s own fixture points the XDG roots at a temporary home and
	stops there, which is right for every command that only touches an instance. This one
	writes `.claude/settings.json` **relative to the current directory** — so a fixture that
	isolated the instance and not the directory would put a hook into this repository, on every
	run, while looking exactly like a sandbox. That is the isolation seam that covers half.
	"""

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(tmp_path / variable.lower()))

	for name in list(os.environ):
		if name.startswith(("SUBROUTINE_TOKEN", "SUBROUTINE_WORKSPACE", "SUBROUTINE_CONNECTION")):
			monkeypatch.delenv(name, raising=False)

	monkeypatch.setenv("SUBROUTINE_DEFAULT_TIMEZONE", "Europe/London")
	monkeypatch.chdir(tmp_path)

	yield tmp_path


@pytest.fixture
def run (home: pathlib.Path) -> typing.Callable[..., typer.testing.Result]:
	"""Run the real CLI, failing loudly on an unexpected exit."""

	runner = typer.testing.CliRunner()

	def invoke (*arguments: str, expect: int = 0) -> typer.testing.Result:
		"""Run one command and check how it ended."""

		subroutine.cli.main._said_unknown_settings = False
		result = runner.invoke(subroutine.cli.main.app, list(arguments))

		assert result.exit_code == expect, (
			f"'subroutine {' '.join(arguments)}' exited {result.exit_code}\n"
			f"{result.output}\n{result.exception!r}"
		)

		return result

	return invoke



def _settings (home: pathlib.Path) -> dict[str, typing.Any]:
	"""Read the settings file a run wrote."""

	return typing.cast(
		dict[str, typing.Any],
		json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8")),
	)


def test_the_hook_it_writes_is_a_shape_claude_code_accepts (tmp_path: pathlib.Path) -> None:
	"""Checked against the validator rather than against a docstring — `#236`.

	**A hook with a mistyped key is skipped in silence**, which is the worst available failure:
	the install reports success, nothing runs, and the only evidence is an absence. Git does
	exactly this and `scripts/install_hooks.py` exists because of it.

	``claude plugin validate`` reads a plugin's ``hooks/hooks.json``, which carries the same
	structure as the ``hooks`` section of a settings file — so wrapping ours in a throwaway
	plugin is a real check of the shape, run by the same tool CI already runs on three
	manifests. **What it cannot say is whether the harness runs it**, and the command says so
	out loud rather than implying otherwise.
	"""

	if shutil.which("claude") is None:
		pytest.skip("the harness's own validator is what this asks; there is none here")

	plugin = tmp_path / "probe"
	(plugin / ".claude-plugin").mkdir(parents=True)
	(plugin / "hooks").mkdir()
	(plugin / ".claude-plugin" / "plugin.json").write_text(
		json.dumps({"name": "probe", "version": "0.0.1", "description": "shape probe"}),
		encoding="utf-8",
	)
	(plugin / "hooks" / "hooks.json").write_text(
		json.dumps(subroutine.cli.personal._with_the_hook({})), encoding="utf-8"
	)

	checked = subprocess.run(
		["claude", "plugin", "validate", str(plugin)],
		capture_output=True,
		text=True,
		timeout=120,
	)

	assert checked.returncode == 0, checked.stdout + checked.stderr


def test_the_probe_would_notice_a_shape_that_is_wrong (tmp_path: pathlib.Path) -> None:
	"""And the validator is asked to refuse something, so the test above is not vacuous.

	A check that passes whatever it is given says nothing about what it passed. This is the
	same falsification the guards in this repository owe: feed the thing a defect through its
	own entry point.
	"""

	if shutil.which("claude") is None:
		pytest.skip("the harness's own validator is what this asks; there is none here")

	plugin = tmp_path / "broken"
	(plugin / ".claude-plugin").mkdir(parents=True)
	(plugin / "hooks").mkdir()
	(plugin / ".claude-plugin" / "plugin.json").write_text(
		json.dumps({"name": "broken", "version": "0.0.1", "description": "shape probe"}),
		encoding="utf-8",
	)
	# **A real event with the wrong shape, not an event nobody has heard of** (`#2097`). This
	# declared `WhenTheMoonIsFull`, and Claude Code 2.1.260 downgraded an unrecognised event
	# name to a warning — *unknown hook event; entry ignored at runtime*, then
	# `✔ Validation passed with warnings` and exit 0. So the probe stopped probing, silently,
	# because somebody else's program got more lenient.
	#
	# A malformed `PreToolUse` is refused outright and is the better subject anyway: this test
	# is named for a *shape* that is wrong, and what it drove was an unknown *name*. The two
	# were one thing until the validator started treating them differently.
	(plugin / "hooks" / "hooks.json").write_text(
		json.dumps({"hooks": {"PreToolUse": "not a list"}}), encoding="utf-8"
	)

	checked = subprocess.run(
		["claude", "plugin", "validate", str(plugin)],
		capture_output=True,
		text=True,
		timeout=120,
	)

	assert checked.returncode != 0, (
		"the validator accepts anything, so the check above is empty. If it now merely warns "
		"about this, find a fault it still refuses rather than deleting the probe — the gate's "
		"validate steps are worth nothing unless something is known to fail them."
	)


def test_it_writes_a_session_end_hook_and_says_what_it_cannot_check (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""The whole command, driven."""

	run("init", "--username", "si", "--workspace", "Personal")
	written = run("setup", "claude", "--yes").output

	assert "Wired" in written, written
	assert subroutine.cli.personal._hooked(_settings(home))

	# **`#236`'s rule, and the item's own.** Whether the harness reads this file is provable by
	# a session ending and by nothing else, so the command must not imply it has checked.
	assert "Nothing here can confirm" in written, written


def test_it_says_when_the_hook_would_find_nothing_to_run (
	run: typing.Callable[..., typer.testing.Result],
	home: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`SR#3944`, L-12 of the cold review of 2026-09-28: a hook that runs nothing, said by nobody.

	The hook runs ``subroutine`` by name and gives up quietly where it is not on the ``PATH``,
	which is the plugin's own ``uvx`` arrangement, so a checkout wired that way gave nothing back
	while the command said *Wired*. **Said where the program cannot be found, and only there.**
	"""

	run("init", "--username", "si", "--workspace", "Personal")

	found = home / "found"
	found.mkdir()
	program = found / "subroutine"
	program.write_text("#!/bin/sh\n", encoding="utf-8")
	program.chmod(0o755)

	monkeypatch.setenv("PATH", str(home / "nowhere"))
	unfound = " ".join(run("setup", "claude", "--yes").output.split())

	(home / ".claude" / "settings.json").unlink()
	monkeypatch.setenv("PATH", str(found))
	wired = " ".join(run("setup", "claude", "--yes").output.split())

	assert "not on this PATH" in unfound, unfound
	assert "not on this PATH" not in wired, wired
	# **And one thing said about giving back, not two** (`SR#4028`, L-8 (5)).
	assert "now gives back" not in unfound and "now gives back" in wired, (unfound, wired)


def test_running_it_twice_changes_nothing (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""A hook added twice runs twice, which for a release is harmless and for a reader is not.

	Somebody re-running a setup command is the ordinary case — they have forgotten whether they
	did — and the answer has to be a sentence rather than a second entry.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	run("setup", "claude", "--yes")
	before = (home / ".claude" / "settings.json").read_text(encoding="utf-8")
	again = run("setup", "claude", "--yes").output

	assert "Already wired" in again
	assert (home / ".claude" / "settings.json").read_text(encoding="utf-8") == before


def test_it_keeps_everything_already_in_the_file (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""The file is the reader's, not ours.

	It holds their permissions, their model and their other hooks — and `#1043` is the recorded
	shape of a settings blob rewritten from a partial read, where another subsystem's live
	state went with it.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	where = home / ".claude" / "settings.json"
	where.parent.mkdir(parents=True, exist_ok=True)
	where.write_text(
		json.dumps(
			{
				"permissions": {"allow": ["WebSearch"]},
				"hooks": {
					"SessionStart": [
						{"hooks": [{"type": "command", "command": "echo hello"}]}
					]
				},
			}
		),
		encoding="utf-8",
	)

	run("setup", "claude", "--yes")
	after = _settings(home)

	assert after["permissions"] == {"allow": ["WebSearch"]}
	assert after["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo hello"
	assert subroutine.cli.personal._hooked(after)


@pytest.mark.parametrize(
	("written", "said"),
	[
		(b"{ this is not json", "is not valid JSON"),
		(b"[]", "holds a JSON list, where Claude Code reads an object."),
		('{"note": "caf\u00e9"}'.encode("latin-1"), "is not UTF-8 text, from byte 13."),
	],
	ids=["not json", "a list", "latin-1"],
)
def test_it_refuses_a_settings_file_it_cannot_read (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path, written: bytes, said: str
) -> None:
	"""Rather than replacing one on the strength of a syntax error.

	The worst outcome here is not a refusal; it is silently taking a shared store for our own
	and losing whatever the reader had in it. **Each fault by name, as `agent create --here`
	names it** (`SR#5158`): a list and a Latin-1 file ended in the crash report.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	where = home / ".claude" / "settings.json"
	where.parent.mkdir(parents=True, exist_ok=True)
	where.write_bytes(written)

	refused = run("setup", "claude", "--yes", expect=1)

	assert isinstance(refused.exception, SystemExit), repr(refused.exception)
	assert said in " ".join(refused.output.split()), refused.output
	assert where.read_bytes() == written, "it wrote anyway"


@pytest.mark.parametrize(
	("written", "part", "shape"),
	[
		('{"hooks": []}', "hooks", "an object"),
		('{"hooks": "x"}', "hooks", "an object"),
		('{"hooks": {"SessionEnd": "x"}}', "hooks.SessionEnd", "a list"),
		('{"hooks": {"SessionEnd": ["x"]}}', "hooks.SessionEnd[0]", "an object"),
		('{"hooks": {"SessionEnd": [{"hooks": "x"}]}}', "hooks.SessionEnd[0].hooks", "a list"),
		('{"hooks": {"SessionEnd": [{"hooks": ["x"]}]}}', "hooks.SessionEnd[0].hooks[0]", "an object"),
	],
	ids=["a list", "a string", "an event", "an entry", "an entry's hooks", "a hook"],
)
def test_it_refuses_hooks_it_cannot_add_a_hook_to (
	run: typing.Callable[..., typer.testing.Result],
	home: pathlib.Path,
	written: str,
	part: str,
	shape: str,
) -> None:
	"""`SR#5191`, found building `SR#5158`: it walked ``hooks`` in the shape Claude Code reads.

	So each of these ended in the crash report. Refused naming the part, leaving the file as it was.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	where = home / ".claude" / "settings.json"
	where.parent.mkdir(parents=True, exist_ok=True)
	where.write_text(written, encoding="utf-8")

	refused = run("setup", "claude", "--yes", expect=1)
	said = " ".join(refused.output.split())

	assert isinstance(refused.exception, SystemExit), repr(refused.exception)
	assert f"The '{part}' in" in said, said
	assert f"is not {shape}, so a hook cannot be added to it." in said, said
	assert where.read_text(encoding="utf-8") == written, "it wrote anyway"


def test_it_leaves_an_env_it_does_not_add_to_as_it_is (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""`SR#5158`: ``agent create --here`` refuses an ``env`` that is not an object, adding a variable.

	This adds a hook, and that refusal says a variable cannot be added, so it is not asked here
	(decision `#5097`).
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	where = home / ".claude" / "settings.json"
	where.parent.mkdir(parents=True, exist_ok=True)
	where.write_text('{"env": ["SUBROUTINE_TOKEN"]}', encoding="utf-8")

	run("setup", "claude", "--yes")
	after = _settings(home)

	assert after["env"] == ["SUBROUTINE_TOKEN"]
	assert subroutine.cli.personal._hooked(after)


def test_it_writes_the_settings_through_a_new_file_keeping_their_permissions (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""`SR#5158`: written in place, an interrupted write could leave the settings half there.

	**A new file renamed over the old** is what makes that impossible, and it is a new file on the
	disk. It keeps the old one's permissions, being a file to commit that holds no credential.
	"""

	run("init", "--username", "si", "--workspace", "Personal")
	where = home / ".claude" / "settings.json"
	where.parent.mkdir(parents=True, exist_ok=True)
	where.write_text('{"permissions": {"allow": ["Bash(ls:*)"]}}\n', encoding="utf-8")
	where.chmod(0o640)
	before = where.stat().st_ino

	run("setup", "claude", "--yes")

	assert where.stat().st_ino != before, "written in place"
	assert stat.S_IMODE(where.stat().st_mode) == 0o640
	assert _settings(home)["permissions"] == {"allow": ["Bash(ls:*)"]}
	assert sorted(path.name for path in where.parent.iterdir()) == ["settings.json"]
