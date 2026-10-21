"""A setting of the instance written inside a connection's table - `SR#5166`.

NEW-E-1 of the verification of the cold review of 2026-10-17, with L-8's bullet on ``protected =
true`` under ``[connections.local]``, decided in `#5097`. TOML files every key after a table's
header into that table, so ``protected = true`` written below one protected nothing: every command
said it had no effect, those that read connections then refused the whole file anyway, and ``db
restore`` replaced the database unasked. **Every command refuses the file now, in one sentence
saying to move it; ``serve`` and ``mcp`` say the same and go on, and ``doctor`` names it.**
"""

import json
import pathlib
import re
import typing

import pytest
import typer.testing

import subroutine.cli.main
import subroutine.config
import test_cli_connections
import test_instances
import test_personal_path

#: The personal path's fresh home and its runner, bound here so pytest finds them as fixtures.
home = test_personal_path.home
run = test_personal_path.run

Run = typing.Callable[..., typer.testing.Result]

#: What every surface says, as approved.
INSIDE = "'protected' is written inside [connections.local], where it sets nothing."
MOVE = "Move it above the first [connections.…] table."

#: What it was said as, which contradicted the refusal that followed it.
NO_EFFECT = "having no effect"

#: A tool call after the handshake, which needs an instance to answer.
WHO_AM_I = json.dumps(
	{
		"jsonrpc": "2.0",
		"id": 2,
		"method": "tools/call",
		"params": {"name": "subroutine_whoami", "arguments": {}},
	}
)


def _said (result: typer.testing.Result) -> str:
	"""Return what a command printed, as one line, with the frame drawn around an error gone."""

	return " ".join(re.sub(r"[│╭╮╰╯─]", " ", result.output).split())


def _written_inside () -> str:
	"""Write ``protected = true`` below a ``[connections.local]`` header, and return the file as it
	was before."""

	path = subroutine.config.config_file_path()
	before = path.read_text(encoding="utf-8")
	path.write_text(before + "\n[connections.local]\nprotected = true\n", encoding="utf-8")

	return before


def _put_back (before: str) -> None:
	"""Put the configuration file back as it was before :func:`_written_inside`."""

	subroutine.config.config_file_path().write_text(before, encoding="utf-8")


def test_a_restore_is_refused_before_anything_is_replaced (run: Run) -> None:
	"""The act the setting exists to stop: ``db restore --recover`` replaced the database unasked."""

	run("init", "--workspace", "Real")
	run("add", "Keep me")
	name = test_instances._backup_name(run("db", "backup").output)
	run("add", "Written after the backup")
	before = _written_inside()

	refused = run("db", "restore", name, "--recover", expect=1)
	said = _said(refused)

	assert INSIDE in said and MOVE in said, said
	assert NO_EFFECT not in said, said

	_put_back(before)

	assert "Written after the backup" in run("list").output, "the database was replaced"


@pytest.mark.parametrize("command", [("list",), ("config", "show")], ids=["list", "config show"])
def test_a_command_refuses_in_one_sentence (run: Run, command: tuple[str, ...]) -> None:
	"""``list`` said it had no effect and then that it was not something a connection has, and
	``config show`` said it had no effect and showed the instance unprotected."""

	run("init")
	_written_inside()

	refused = run(*command, expect=1)
	said = _said(refused)

	assert said.count(INSIDE) == 1 and MOVE in said, said
	assert NO_EFFECT not in said and "not something a connection has" not in said, said


def test_serve_says_it_once_and_starts (run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
	"""``protected`` guards nothing ``serve`` does, and refusing would stop an instance serving
	after an upgrade."""

	run("init")
	_written_inside()
	started: dict[str, typing.Any] = {}

	def instead_of_listening (app: typing.Any, **given: typing.Any) -> None:
		"""Record that serve got as far as handing the application over."""

		started.update(given)

	monkeypatch.setattr("uvicorn.run", instead_of_listening)
	monkeypatch.setattr(subroutine.cli.main, "_refuse_a_port_in_use", lambda host, port: None)

	served = run("serve")
	said = _said(served)

	assert started, "serve refused, or never reached uvicorn"
	assert said.count(INSIDE) == 1 and MOVE in said and NO_EFFECT not in said, said


def test_mcp_says_it_once_and_answers_a_tool_call (run: Run) -> None:
	"""Nor anything an agent's tools do, which would otherwise stop answering at all."""

	run("init")
	_written_inside()

	answering = run("mcp", input=test_cli_connections.INITIALIZE + WHO_AM_I + "\n")
	answers = test_cli_connections.answered(answering)
	said = _said(answering)

	assert len(answers) == 2 and not [one for one in answers if "error" in one], answers
	assert not answers[1]["result"].get("isError"), answers[1]
	assert said.count(INSIDE) == 1 and NO_EFFECT not in said, said


def test_doctor_names_it_among_its_findings (run: Run) -> None:
	"""Read past, so it is said beside everything else that needs attention."""

	run("init")
	_written_inside()

	diagnosed = run("doctor", expect=1)
	said = _said(diagnosed)

	assert f"config.toml {subroutine.config.config_file_path()} cannot be used: {INSIDE}" in said
	assert said.count(INSIDE) == 1 and "needs attention" in said, said
	assert "nothing is reachable from outside" in said, "the findings after it were not reached"


def test_two_are_named_in_one_refusal (home: pathlib.Path) -> None:
	"""One sentence however many there are, each where it is."""

	path = subroutine.config.config_file_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(
		'[connections.local]\nprotected = true\n\n[connections.work]\nurl = "https://tasks.example.com"'
		"\nport = 9000\n",
		encoding="utf-8",
	)

	with pytest.raises(subroutine.config.SettingInsideAConnection) as refused:
		subroutine.config.read_config_file()

	assert refused.value.detail.endswith(
		": 'protected' inside [connections.local] and 'port' inside [connections.work] are "
		"written where they set nothing."
	), refused.value.detail
	assert refused.value.hint == "Move them above the first [connections.…] table."
	assert [one.field for one in refused.value.errors] == [
		"connections.local.protected",
		"connections.work.port",
	]


def test_a_misspelt_setting_is_still_only_a_warning (run: Run) -> None:
	"""`#175`'s rule stands for everything else: a stray key is named, and refuses nothing."""

	run("init")
	path = subroutine.config.config_file_path()
	path.write_text("protectd = true\n" + path.read_text(encoding="utf-8"), encoding="utf-8")

	listed = run("list")

	assert NO_EFFECT in listed.output and "Did you mean 'protected'?" in listed.output


def test_protected_above_the_tables_still_protects (run: Run) -> None:
	"""Written where it is read, it is read, with a connection's table below it."""

	run("init", "--workspace", "Real")
	name = test_instances._backup_name(run("db", "backup").output)
	path = subroutine.config.config_file_path()
	path.write_text(
		"protected = true\n" + path.read_text(encoding="utf-8") + "\n[connections.local]\n"
		"enabled = true\n",
		encoding="utf-8",
	)

	refused = run("db", "restore", name, "--recover", expect=1)

	assert "protected" in refused.output and INSIDE not in _said(refused), refused.output
