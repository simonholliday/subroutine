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
import re
import subprocess
import typing

import pytest
import sqlalchemy.orm

import api_support
import subroutine.cli.main
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.mcp.tools

ROOT = pathlib.Path(__file__).resolve().parent.parent
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN = ROOT / "plugins" / "subroutine" / ".claude-plugin" / "plugin.json"
SERVERS = ROOT / "plugins" / "subroutine" / ".mcp.json"


def _read (path: pathlib.Path) -> dict[str, typing.Any]:
	"""Return one manifest, as the object it must be."""

	loaded = json.loads(path.read_text(encoding="utf-8"))

	assert isinstance(loaded, dict), f"{path.name} is not an object"

	return loaded


def test_the_plugin_version_matches_the_tag_it_is_released_under () -> None:
	"""One repository, one tag, one version — which is the whole reason they share a repo.

	**The tag is the source now, not ``pyproject.toml``** (`#234`). The package derives its
	version from the tag and so cannot disagree with it; the plugin manifest is static JSON
	that Claude Code reads straight from the repository, with no build step to derive anything
	— so it is the only half left that can drift, and this is what stops it.

	**Checked only on a tagged commit, and that is the whole constraint rather than a weakening
	of it.** Between releases the manifest is deliberately *ahead* of the newest tag: bumping it
	is what makes the commit worth tagging. Asserting equality on every commit would fail on
	precisely the commit that does the bumping. What must never happen is a *release* whose two
	halves disagree, and a release is a tag.

	The release workflow makes the same comparison independently, because a test can be skipped
	by not running it and a published artefact cannot be recalled.
	"""

	tagged = subprocess.run(
		["git", "tag", "--points-at", "HEAD"],
		capture_output=True, text=True, cwd=ROOT, check=False,
	)

	names = [line for line in tagged.stdout.split() if line.startswith("v")]

	if tagged.returncode != 0 or not names:
		pytest.skip("HEAD carries no release tag, so there is nothing yet to agree with")

	assert _read(PLUGIN)["version"] in {name.removeprefix("v") for name in names}


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

	# Same correction as the skill's (`#237`): the listing is read by somebody who is about to
	# wire an editor to it, so the install it names has to be one that leaves the command
	# findable. `pip install` into a virtualenv satisfies "installed" and fails anyway.
	assert any(phrase in described for phrase in ("uv tool install", "pipx install"))
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


SKILL = ROOT / "plugins" / "subroutine" / "skills" / "subroutine" / "SKILL.md"

#: Every ``subroutine_x(`` the skill writes, and every ``subroutine x`` shell command it shows.
#: Both are promises to an agent that something exists.
#:
#: **Anywhere in the line, not only at the start of one.** The first version of this anchored
#: with ``^`` and so checked ``subroutine init`` in a fenced block while missing ``subroutine
#: defer 42`` in a sentence — half a guard, and the half that misses is the prose, which is
#: where a stale command is likeliest to survive.
_TOOL = re.compile(r"\b(subroutine_[a-z_]+)\s*\(")
_COMMAND = re.compile(r"\bsubroutine ([a-z][a-z-]*(?: [a-z][a-z-]*)?)")

#: What follows the word ``subroutine`` without naming a command: ordinary prose, and the
#: second word of a two-word phrase whose first word is the command. Listed rather than
#: guessed at, so that adding one is a decision.
_NOT_A_COMMAND = frozenset({"is", "now", "and", "the", "in", "on", "a", "it", "tools"})


def _skill () -> str:
	"""Return the skill's text."""

	return SKILL.read_text(encoding="utf-8")


def test_the_skill_declares_a_name_and_a_trigger () -> None:
	"""The description is half the value and is the part that is always in context.

	It is what makes an agent reach for Subroutine unprompted rather than waiting to be told,
	and a tool surface nobody invokes is context paid for and wasted.
	"""

	front = _skill().split("---")[1]

	assert "name: subroutine" in front
	assert "description:" in front

	described = front.split("description:", 1)[1]

	for trigger in ("what to work on", "log", "adopt"):
		assert trigger in described.lower(), f"the description does not fire on {trigger!r}"


def test_the_skill_says_what_to_do_when_the_tools_are_missing () -> None:
	"""SPEC.md §21.4, and it has to be here because it cannot be anywhere else.

	Measured rather than assumed: a failing MCP server reports ``✘ Failed to connect`` and
	nothing more — a launcher script written to explain itself is not surfaced either. A skill
	is a file, so it loads whether or not the server started, which makes it the only place
	the explanation can be read once something has gone wrong.
	"""

	text = _skill()

	# **An install that lands on `PATH`, not merely an install** (`#237`). This asserted
	# `pip install subroutine` until the day somebody met the failure: the editor launches the
	# program itself, so a `pip install` into a virtualenv satisfies the old wording and leaves
	# the tools missing anyway. Either tool installer is accepted; naming one would pin a
	# preference this test has no business holding.
	assert any(phrase in text for phrase in ("uv tool install", "pipx install")), (
		"the skill must name an install that puts the command where an editor can find it"
	)
	assert "subroutine init" in text

	# The two escape hatches, both needed: one for somebody who will install it again, one for
	# somebody who would rather point at the copy they already have.
	assert "/plugin configure" in text, "the virtualenv case is the likely one, not the rare one"

	# **And the command that tells the two causes apart.** Not installed and installed-but-
	# unreachable are identical from inside a session — no tools, no error — so a skill that
	# describes the remedies without naming the diagnostic sends half its readers the wrong way.
	assert "claude mcp list" in text


def test_every_tool_the_skill_names_exists (
	session: sqlalchemy.orm.Session, tmp_path: pathlib.Path
) -> None:
	"""**The guard that would have caught three separate walls in one run.**

	``#134``, ``#136`` and ``#138`` were each a capability the HTTP API had and the surfaces a
	person or an agent touches did not, and each was found by hand, by writing a sentence and
	discovering it could not be true. Documentation that names a tool is a promise, and prose
	is exactly what nothing checks.
	"""

	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True, database_url=f"sqlite:///{tmp_path}/x.db"),
		session_factory=api_support.factory_for(session),
	)

	with client:
		available = {tool.name for tool in subroutine.mcp.tools.catalogue(client)}

	named = set(_TOOL.findall(_skill()))

	assert named, "the skill names no tools at all — has this test stopped reaching them?"
	assert named <= available, f"the skill promises {sorted(named - available)}, which do not exist"


def test_every_command_the_skill_shows_exists () -> None:
	"""The same promise in the other vocabulary, and the one the adoption procedure rests on.

	``subroutine project create`` was in SPEC's command map as *(not built)* while a procedure
	elsewhere depended on it. A page that shows a command somebody cannot run is worse than
	one that omits it: they will type it before they doubt it.
	"""

	# `name` is None when the decorator did not override it, and Typer then uses the function
	# name — so the callback is the fallback, and a command with neither is not a command.
	registered = {
		command.name or (command.callback.__name__ if command.callback else "")
		for command in subroutine.cli.main.app.registered_commands
	} | {group.name for group in subroutine.cli.main.app.registered_groups if group.name}

	shown = {
		phrase.split()[0]
		for phrase in _COMMAND.findall(_skill())
		if phrase.split()[0] not in _NOT_A_COMMAND
	}

	# **One is enough, and it used to be three.** The floor exists so that a broken regex
	# reads as a failure rather than as a clean run over nothing — not to assert the skill
	# keeps naming commands. `#149` took it from four commands to one by giving MCP the
	# tools it had been telling agents to shell out for, and a floor of three would have made
	# closing that gap fail this test. `install` is the one that can never go: it is how
	# somebody gets a `subroutine` to run at all.
	assert shown, "found no commands at all in the skill — has this test stopped reaching them?"
	assert shown <= registered, f"the skill shows {sorted(shown - registered)}, which do not exist"
