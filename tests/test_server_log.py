"""A served instance writes its own lines the way uvicorn writes its, at ``--log-level`` - `#2834`.

**Measured before it was fixed**: handed only a level, uvicorn configured the loggers it names and
no others, so the application's had no handler. Python's last resort then wrote a warning as a
bare message and an info line not at all, and the served instance's journal held no line of the
application's own in ten days. `#2224`'s note that a release check failed is an info line, so it
would never have been written.
"""

import json
import pathlib
import subprocess
import sys
import textwrap
import typing

import pytest
import uvicorn
import uvicorn.config

import subroutine.api.app
import subroutine.cli.main

#: Applied in a process of its own, because a logging configuration is global and would outlive
#: the test that applied it - in a suite whose own log capture depends on the one it found.
WRITES_TWO_LINES = textwrap.dedent("""
	import json
	import logging.config
	import sys

	logging.config.dictConfig(json.loads(sys.argv[1]))
	logging.getLogger("subroutine.releases").info("an info line")
	logging.getLogger("subroutine.releases").warning("a warning")
""")


def _configured (monkeypatch: pytest.MonkeyPatch, level: str) -> dict[str, typing.Any]:
	"""Start ``serve`` at one level, with uvicorn stood in for, and return what it was handed."""

	seen: dict[str, typing.Any] = {}

	monkeypatch.setattr(subroutine.api.app, "create_app", lambda *, settings: object())
	monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: seen.update(kwargs))
	monkeypatch.setattr(subroutine.cli.main, "_refuse_unusable_storage", lambda settings: None)
	monkeypatch.setattr(subroutine.cli.main, "_refuse_public_bind", lambda *a, **k: None)
	monkeypatch.setattr(subroutine.cli.main, "_database_is_absent", lambda settings: False)
	monkeypatch.setenv("SUBROUTINE_SECRET_KEY", "a-key-so-serve-will-start")

	subroutine.cli.main.serve(host="", port=0, log_level=level, insecure=False)

	assert seen["log_level"] == level

	return typing.cast(dict[str, typing.Any], seen["log_config"])


def _written (configured: dict[str, typing.Any], tmp_path: pathlib.Path) -> list[str]:
	"""Apply a configuration in a fresh process, write two lines, and return what came out."""

	done = subprocess.run(
		[sys.executable, "-c", WRITES_TWO_LINES, json.dumps(configured)],
		capture_output=True,
		text=True,
		timeout=60,
		check=False,
		cwd=tmp_path,
	)

	assert done.returncode == 0, done.stderr

	return (done.stdout + done.stderr).splitlines()


def test_the_applications_lines_are_written_as_uvicorns_are_at_the_level_asked_for (
	monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
	"""At ``info`` both lines are written, each with its level; at ``warning``, only the warning."""

	assert _written(_configured(monkeypatch, "info"), tmp_path) == [
		"INFO:     an info line",
		"WARNING:  a warning",
	]
	assert _written(_configured(monkeypatch, "warning"), tmp_path) == ["WARNING:  a warning"]


def test_uvicorns_own_configuration_is_left_as_it_was (monkeypatch: pytest.MonkeyPatch) -> None:
	"""The application's logger is added to a copy: uvicorn's dictionary is read by every server."""

	_configured(monkeypatch, "info")

	assert "subroutine" not in uvicorn.config.LOGGING_CONFIG["loggers"]
