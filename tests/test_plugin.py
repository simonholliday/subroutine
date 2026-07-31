"""The Claude Code plugin's manifests — SPEC.md §21, item ``#131``.

The plugin exists to launch ``subroutine mcp``, so **the MCP server it configures is this
package**. Anything that lets the two disagree about which version either of them is produces
tools that fail in a way neither side can explain, and the failure arrives at whoever installed
it rather than at whoever shipped it.

These are cheap structural checks against a file nothing else reads. They are here because the
plugin is JSON with no import, no type checker and no runtime: without a test, a mistyped key
is found by a stranger.
"""

import json
import pathlib
import tomllib
import typing

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN = ROOT / "plugins" / "subroutine" / ".claude-plugin" / "plugin.json"
SERVERS = ROOT / "plugins" / "subroutine" / ".mcp.json"


def _read (path: pathlib.Path) -> dict[str, typing.Any]:
	"""Return one manifest, as the object it must be."""

	loaded = json.loads(path.read_text(encoding="utf-8"))

	assert isinstance(loaded, dict), f"{path.name} is not an object"

	return loaded


def test_the_plugin_version_matches_the_package () -> None:
	"""One repository, one tag, one version — which is the whole reason they share a repo.

	``claude plugin tag`` validates that the manifest and the marketplace entry agree with each
	other; nothing but this checks that either agrees with ``pyproject.toml``.
	"""

	packaged = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

	assert _read(PLUGIN)["version"] == packaged["project"]["version"]


def test_the_marketplace_points_at_the_plugin_that_is_here () -> None:
	"""A relative source resolves against the marketplace root, not the ``.claude-plugin`` directory."""

	entries = _read(MARKETPLACE)["plugins"]

	assert len(entries) == 1, entries

	entry = entries[0]
	source = entry["source"]

	assert source.startswith("./"), "a relative source must, or it is read as a git reference"
	assert (ROOT / source).is_dir(), f"{source} does not exist"
	assert entry["name"] == _read(PLUGIN)["name"]


def test_the_marketplace_says_the_product_is_a_separate_install () -> None:
	"""§21.4's third layer, and the earliest of the three.

	Somebody browsing a marketplace has no reason to know there is anything else to install,
	and a plugin that fails on first use with ``✘ Failed to connect`` explains nothing — that
	was measured, and a launcher script written to explain itself is not surfaced either.
	"""

	described = _read(MARKETPLACE)["plugins"][0]["description"]

	assert "pip install subroutine" in described
	assert "subroutine init" in described


def test_the_server_runs_the_configured_command_rather_than_a_fixed_one () -> None:
	"""The trap this option exists for, reproduced live before it was written down.

	Installed with ``command`` left at its default, the server on this machine reported
	``✘ Failed to connect``: a bare ``subroutine`` does not resolve when the program lives in a
	virtualenv the editor does not activate. That is why a committed ``.mcp.json`` was written
	for this repository and deliberately deleted, and why the value is prompted for instead.
	"""

	server = _read(SERVERS)["mcpServers"]["subroutine"]

	assert server["command"] == "${user_config.command}"
	assert server["args"][0] == "mcp"


def test_the_token_travels_in_the_environment_and_is_held_as_a_secret () -> None:
	"""**The only arrangement §7.4 permits**, and the reason the token is declared at all.

	Never in a configuration file, never a command-line argument, never a query parameter. A
	``sensitive`` option is kept in secure storage rather than in ``settings.json``, and reaches
	the process as an environment variable — which is also the one place a token does not end
	up in shell history.
	"""

	assert _read(SERVERS)["mcpServers"]["subroutine"]["env"]["SUBROUTINE_TOKEN"] == (
		"${user_config.token}"
	)
	assert _read(PLUGIN)["userConfig"]["token"]["sensitive"] is True


@pytest.mark.parametrize("option", ["command", "connection", "token"])
def test_every_declared_option_is_substituted_somewhere (option: str) -> None:
	"""An option nobody reads is a question asked for nothing.

	Cheap, and it fails in the direction that matters: adding a field to the dialog and
	forgetting to wire it is silent, and the user answering it has no way to find out.
	"""

	assert _read(PLUGIN)["userConfig"][option]
	assert "${user_config." + option + "}" in SERVERS.read_text(encoding="utf-8")


@pytest.mark.parametrize("option", ["command", "connection", "token"])
def test_an_optional_value_left_empty_is_safe (option: str) -> None:
	"""Every option but the command may honestly be left blank, and blank must mean "as before".

	Verified against the program rather than assumed: an empty ``SUBROUTINE_TOKEN`` falls
	through the truthiness checks in ``credentials.resolve`` to whatever would have been used
	anyway, and ``subroutine mcp --connection ""`` becomes ``connection=None``, which is the
	current one. So the argument list can be fixed rather than assembled conditionally, and a
	dialog somebody skipped behaves like a dialog nobody ever saw.
	"""

	declared = _read(PLUGIN)["userConfig"][option]

	assert declared.get("required") is not True, (
		"a field with a default is never empty, so calling it required states something untrue"
	)
	assert "default" in declared
