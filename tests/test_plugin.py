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
import yaml

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

	server = _read(SERVERS)["mcpServers"]["tools"]

	assert server["command"] == "${user_config.command}"
	assert server["args"][0] == "mcp"


def test_the_token_travels_in_the_environment_and_is_held_as_a_secret () -> None:
	"""**The only arrangement §7.4 permits**, and the reason the token is declared at all.

	Never in a configuration file, never a command-line argument, never a query parameter. A
	``sensitive`` option is kept in secure storage rather than in ``settings.json``, and reaches
	the process as an environment variable — which is also the one place a token does not end
	up in shell history.
	"""

	assert _read(SERVERS)["mcpServers"]["tools"]["env"]["SUBROUTINE_TOKEN"] == (
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


def test_every_tool_that_exists_is_named_by_the_skill (
	session: sqlalchemy.orm.Session, tmp_path: pathlib.Path
) -> None:
	"""**The direction the test above cannot see** (`#318`).

	It asks whether every promise is kept and never whether every capability is offered, so a
	tool nobody wrote about is invisible to it. ``subroutine_search`` shipped that way (`#312`):
	the argument for adding it was that "a model reading tool *names* to decide what it could do
	had no reason to think searching was possible" — and it was then added to the catalogue and
	to nothing an agent reads, while ``q`` came *off* ``subroutine_list`` in the same change. So
	the surface got a search verb and the practice describing it said searching was impossible.

	The budget is the reason this is a real check rather than tidiness. Every tool costs context
	in every session whether or not it is called, so one nobody has been told about is pure
	cost — and the fix is a sentence, which is why nothing forces it.
	"""

	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True, database_url=f"sqlite:///{tmp_path}/x.db"),
		session_factory=api_support.factory_for(session),
	)

	with client:
		available = {tool.name for tool in subroutine.mcp.tools.catalogue(client)}

	# Read from the whole page rather than from fenced examples alone: naming a tool in a
	# sentence is teaching it, and requiring a code block would push prose into ceremony.
	unmentioned = {name for name in available if name not in _skill()}

	assert not unmentioned, (
		f"{sorted(unmentioned)} exist and the skill never names them, so they cost every "
		f"session context and teach nobody anything."
	)


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


def test_a_changed_plugin_carries_a_version_nobody_has_installed () -> None:
	"""`#380`. A manifest edited without a bump can never reach anybody.

	Claude Code caches an installed plugin under its **version**, so an install at a version
	already present is a no-op. `#333` added the `workspace` field, the version stayed at the
	tag's, and three sessions on another machine went on meeting the refusal it was built to
	remove — while the fix sat in the repository and every test passed.

	**The guards in this repository stop at its edge**, which is the family this belongs to:
	`#236` is that installing a plugin says nothing about whether its server starts. The code
	was right, the suite was green, and the artefact a user installs did not contain the change.

	So: if anything under `plugins/` differs from the newest tag, the manifest must not still
	name that tag's version. Deliberately *not* an equality check against the next version —
	nobody knows what that will be — only that the two cannot be the same while the contents
	differ.

	**Silent with no tags at all**, for the reason the version test skips: a fresh clone with
	no release history has nothing to compare against, and a guard that fails there fails for
	everybody who has just cloned.
	"""

	tags = subprocess.run(
		["git", "tag", "--list", "v*", "--sort=-v:refname"],
		capture_output=True, text=True, cwd=ROOT, check=False,
	)
	names = [line for line in tags.stdout.split() if line.startswith("v")]

	if tags.returncode != 0 or not names:
		pytest.skip("no release tags to compare against")

	newest = names[0]
	changed = subprocess.run(
		["git", "diff", "--name-only", newest, "--", "plugins/"],
		capture_output=True, text=True, cwd=ROOT, check=False,
	)

	if changed.returncode != 0 or not changed.stdout.strip():
		return

	declared = _read(PLUGIN)["version"]
	touched = ", ".join(sorted(changed.stdout.split()))

	assert declared != newest.removeprefix("v"), (
		f"the plugin has changed since {newest} ({touched}) and its manifest still says "
		f"{declared}. Claude Code caches by version, so nobody who installs it would get "
		f"these changes — bump the version in {PLUGIN.name}."
	)


def test_the_skill_frontmatter_parses_as_yaml () -> None:
	"""The check the suite did not have, and the one that would have caught today's worst edit.

	`#378` rewrote the skill's description to make an agent read it. The new text contained
	``the tool descriptions do not: how to open …`` — and a colon followed by a space inside an
	unquoted YAML scalar is a mapping indicator, so the frontmatter stopped parsing.

	**The failure is silent and total.** ``claude plugin validate`` reports it as "at runtime
	this skill loads with empty metadata (all frontmatter fields silently dropped)" — so the
	description, which is the entire mechanism by which a skill is discovered, would have been
	*gone*. A change made to get the skill read would have made it unreadable.

	2,391 tests passed over it. Nothing here had ever parsed this file's frontmatter; the
	manifests are covered because they are JSON that `_read` loads, and the one YAML in the
	plugin was the one nothing touched.

	`CLAUDE.md` already says to run ``claude plugin validate`` before committing either
	manifest, and I did not. A rule that depends on remembering is not the same as a check.
	"""

	text = SKILL.read_text(encoding="utf-8")

	assert text.startswith("---\n"), "the skill has no frontmatter block at all"

	front = text.split("---", 2)[1]

	try:
		parsed = yaml.safe_load(front)

	except yaml.YAMLError as error:
		raise AssertionError(
			f"the skill's frontmatter does not parse, so every field is silently dropped and "
			f"the skill becomes undiscoverable: {error}"
		) from None

	assert isinstance(parsed, dict), "the frontmatter is not a mapping"

	# Named explicitly rather than "some keys parsed": a scalar that swallows the rest of the
	# block still parses, and would leave exactly the fields that matter missing.
	assert parsed.get("name") == "subroutine"
	assert parsed.get("description"), "a skill with no description is one nothing will load"


def test_the_skill_does_not_promise_a_field_no_tool_can_write () -> None:
	"""`#392`. The skill argued for outcome-shaped titles from a field agents could not reach.

	    "your motivation is not lost … because it belongs in the description — which is one
	     field away and is where somebody looks next"

	It was not one field away from MCP; it was unreachable, and an agent that took the advice
	had nowhere to put the reasoning it had just been told to leave out of the title. It used
	comments, which is the wrong shelf by the skill's own §5.10 distinction.

	**This is the shape a per-method reach test cannot see** (`#149`, third instance): the
	capability is an *argument* on `update`, a method both surfaces already call, so nothing
	comparing method names notices it missing. So the check here is not "does a method exist"
	but "does the field this prose promises actually appear in a tool's schema".

	Deliberately narrow. It asserts the one promise the skill makes about a *field* rather than
	trying to parse every claim — a check that tried to would fail on every rewording and be
	switched off, which is the rule `tests/test_documentation.py` opens with.
	"""

	text = SKILL.read_text(encoding="utf-8")

	if "one field away" not in text:
		pytest.skip("the skill no longer makes that promise")

	class Fake:
		"""A client that opens nothing — only the schemas are being read."""

		connection = subroutine.connections.Connection(name="local")

	writable = {
		name
		for tool in subroutine.mcp.tools.catalogue(typing.cast(typing.Any, Fake()))
		for name in tool.schema.get("properties", {})
	}

	assert "description" in writable, (
		"the skill tells an agent its motivation belongs in the description, 'one field away' "
		"— and no tool takes one. Either a tool gains the field or the skill stops promising it."
	)
