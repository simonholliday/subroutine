"""``serve`` says when it decides OSC differently from the rest of its machine - `#4024`.

Decision `#4098`. Each process decides for itself whether it sends OSC, and every process but
the server decides from ``config.toml`` alone. So a server started with ``--host``, or with its
address only in the service's environment, can withhold while a terminal on the same machine
sends to the address a workspace chose. It says so as it starts, naming the two ways to agree.
"""

import typing

import pytest
import uvicorn

import subroutine.api.app
import subroutine.cli.main
import subroutine.config
import subroutine.domain.settings

#: Said as ``serve`` starts when the two would disagree.
SAID = "config.toml"


def _served (
	monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *, host: str
) -> str:
	"""Start ``serve`` with everything that would reach the machine stubbed, and return what it said."""

	monkeypatch.setattr(subroutine.api.app, "create_app", lambda *, settings: object())
	monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
	monkeypatch.setattr(subroutine.cli.main, "_refuse_a_port_in_use", lambda host, port: None)
	monkeypatch.setattr(subroutine.cli.main, "_refuse_unusable_storage", lambda settings: None)
	monkeypatch.setattr(subroutine.cli.main, "_database_is_absent", lambda settings: False)
	monkeypatch.setenv("SUBROUTINE_SECRET_KEY", "a-key-so-serve-will-start")

	subroutine.cli.main.serve(host=host, port=8199, log_level="", insecure=True)

	return capsys.readouterr().out


def _configured (text: str) -> None:
	"""Write the configuration file every other process on this machine reads."""

	path = subroutine.config.config_file_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def test_a_server_bound_wider_than_its_file_says_so (
	monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Bound to every address by ``--host`` while the file says nothing: it withholds, the rest sends."""

	said = _served(monkeypatch, capsys, host="0.0.0.0")

	assert SAID in said and "osc_enabled" in said, said


def test_the_address_in_the_services_environment_alone_is_said_too (
	monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Not only ``--host``: a public address the file does not hold divides them the same way."""

	monkeypatch.setenv("SUBROUTINE_PUBLIC_URL", "https://subroutine.example.com")

	said = _served(monkeypatch, capsys, host="127.0.0.1")

	assert SAID in said and "osc_enabled" in said, said


@pytest.mark.parametrize(
	"configured", ['host = "0.0.0.0"\n', "osc_enabled = false\n", ""], ids=["host", "stated", "laptop"]
)
def test_nothing_is_said_where_the_file_and_the_server_agree (
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	configured: str,
) -> None:
	"""The guide's layouts keep the address in the file, and a laptop serves on loopback."""

	_configured(configured)

	said = _served(monkeypatch, capsys, host="" if configured == "" else "0.0.0.0")

	assert SAID not in said, said


@pytest.mark.parametrize("written", ['"true"', '"false"'])
def test_a_value_written_as_a_string_is_read_as_every_process_reads_it (
	monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], written: str
) -> None:
	"""`SR#4314`: compared as written, ``osc_enabled = "true"`` warned of a disagreement nobody had.

	The server and every other process both read the string as the boolean it spells.
	"""

	_configured(f"osc_enabled = {written}\n")

	said = _served(monkeypatch, capsys, host="127.0.0.1")

	assert SAID not in said, said


def test_the_withheld_sentence_speaks_for_the_server () -> None:
	"""Decision `#4098`: a terminal on the same machine may send, so the server speaks for itself."""

	withheld = typing.cast(str, subroutine.domain.settings.OSC.withheld)

	assert withheld.startswith("This server sends nothing over OSC"), withheld
