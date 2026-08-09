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
import subroutine.api.meta
import subroutine.cli.main
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.domain.projects
import subroutine.mcp.tools

ROOT = pathlib.Path(__file__).resolve().parent.parent
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN = ROOT / "plugins" / "subroutine" / ".claude-plugin" / "plugin.json"
SERVERS = ROOT / "plugins" / "subroutine" / ".mcp.json"

#: The plugin that reaches an instance by address instead of starting one — `#540`.
REMOTE = ROOT / "plugins" / "subroutine-remote" / ".claude-plugin" / "plugin.json"
REMOTE_SERVERS = ROOT / "plugins" / "subroutine-remote" / ".mcp.json"

#: Every plugin this repository ships, by the directory it lives in. **Derived from the
#: filesystem rather than listed**, so a third one is covered by every check below on the day
#: somebody creates it — which is the failure `#405` is about, one level up from an allow-list.
PLUGIN_DIRECTORIES = tuple(
	sorted(path for path in (ROOT / "plugins").iterdir() if (path / ".claude-plugin").is_dir())
)

#: Their manifests, in the same order.
MANIFESTS = tuple(path / ".claude-plugin" / "plugin.json" for path in PLUGIN_DIRECTORIES)


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

	**The manifest is read out of the tagged commit, not off disk** (`#428`). It used to decide
	*whether* to run from the tag on ``HEAD`` and then read the working tree — two different
	states the moment anything is uncommitted. So the ordinary next act after a release, editing
	something under ``plugins/`` before committing, turned the whole suite red over a
	disagreement between a tree that had moved and a tag that had not. Met exactly that way, and
	the claim being made is about a *release*, which is a commit.

	It fails safe either way and could never miss — reading the tree can only be stricter — but
	a guard that cries wolf once per release cycle, on the commit hardest to be confident about,
	is one somebody learns to run past.

	**It still checks only the tag on ``HEAD``, and widening it to every tag was considered and
	declined.** Doing so would run on every commit rather than one in fifty, and would have
	caught `#238` — 0.1.2 tagged with the manifest reading 0.1.1 — for ever rather than on the
	day. But a tag is published history: a mistake found later cannot be corrected without
	retagging something people have installed, so the suite would go permanently red over
	something nobody may fix. The check belongs at the moment it can still change the outcome,
	which is the tagged commit in CI, before the artefact is built. Measured 2026-08-04: all
	seven tags carry a manifest and all seven agree.
	"""

	tagged = subprocess.run(
		["git", "tag", "--points-at", "HEAD"],
		capture_output=True, text=True, cwd=ROOT, check=False,
	)

	names = [line for line in tagged.stdout.split() if line.startswith("v")]

	if tagged.returncode != 0 or not names:
		pytest.skip("HEAD carries no release tag, so there is nothing yet to agree with")

	# **Every manifest, not only the first.** Two plugins ship from this repository (`#540`) and
	# a release tags them together, so one left behind is a plugin nobody can install at the
	# version the release announces.
	for manifest in MANIFESTS:
		shown = subprocess.run(
			["git", "show", f"{names[0]}:{manifest.relative_to(ROOT).as_posix()}"],
			capture_output=True, text=True, cwd=ROOT, check=False,
		)

		if shown.returncode != 0:
			continue

		declared = json.loads(shown.stdout)["version"]

		assert declared in {name.removeprefix("v") for name in names}, (
			f"{manifest.relative_to(ROOT)} says {declared} on a commit tagged {names[0]}"
		)


def test_the_marketplace_points_at_every_plugin_that_is_here () -> None:
	"""A relative source resolves against the marketplace root, not the ``.claude-plugin`` directory.

	**And the listing is compared against the filesystem in both directions.** A plugin present
	and unlisted cannot be installed by anybody; a listing naming a directory that is not there
	fails at install time on somebody else's machine. Neither is visible from reading one file.
	"""

	entries = _read(MARKETPLACE)["plugins"]

	for entry in entries:
		source = entry["source"]

		assert source.startswith("./"), "a relative source must, or it is read as a git reference"
		assert (ROOT / source).is_dir(), f"{source} does not exist"
		assert entry["name"] == _read(ROOT / source / ".claude-plugin" / "plugin.json")["name"]

	assert {entry["source"] for entry in entries} == {
		f"./{path.relative_to(ROOT).as_posix()}" for path in PLUGIN_DIRECTORIES
	}, "the marketplace and the plugins directory disagree about what ships"


def test_the_remote_plugin_reaches_a_server_and_installs_nothing () -> None:
	"""`#540`, and every assertion here is the difference from the local plugin.

	**`type` is what decides whether a URL is read at all.** Claude Code reads an entry with no
	``type`` as a stdio server and skips one that has a ``url`` without it, reporting a
	configuration error — so the pair is load-bearing rather than decorative.

	**And it must declare no command.** A plugin that names one would need Subroutine installed,
	which is the entire thing this plugin exists not to need.
	"""

	server = _read(REMOTE_SERVERS)["mcpServers"]["tools"]

	assert server["type"] in ("http", "streamable-http")
	assert "command" not in server, "the whole point is that nothing is installed"
	assert server["url"] == "${user_config.url}"

	# The credential travels as a header rather than in the environment, because there is no
	# process here to give an environment to. `Bearer ` is written into the template rather than
	# asked of the user: a token pasted with the scheme already on it is the commonest way to
	# get this wrong, and the field's description says to paste the token alone.
	assert server["headers"]["Authorization"] == "Bearer ${user_config.token}"


def test_an_unconfigured_remote_plugin_is_dormant_rather_than_broken () -> None:
	"""**The measured asymmetry the two-plugin decision rests on** — `#538`.

	Claude Code accepts an ``http`` entry whose ``url`` is empty, shows it as ``Not
	configured``, and attempts no connection; a stdio entry with an empty ``command`` is
	refused outright as invalid. So a remote entry can ship inert and wait for somebody to fill
	it in, which is exactly what a plugin installed before its address is known has to do.

	**The url must therefore be the whole of that field and nothing else.** Appending anything —
	a path, a query string for the workspace — makes the value non-empty when the field is
	blank, and the plugin stops being dormant: it becomes a plugin pointed at a malformed
	address, which reports a connection failure to somebody who has not configured it yet.
	"""

	declared = _read(REMOTE)["userConfig"]

	assert declared["url"].get("default", "") == "", "an empty default is what makes it dormant"
	assert declared["url"].get("required") is not True, (
		"a required field would refuse to leave the plugin unconfigured, which is the state it "
		"is installed in"
	)

	template = _read(REMOTE_SERVERS)["mcpServers"]["tools"]["url"]

	assert template == "${user_config.url}", (
		f"{template!r} is not empty when the field is: the workspace goes in the address the "
		f"operator hands over, not into this template"
	)


def test_every_plugin_is_validated_by_the_build () -> None:
	"""A manifest nobody validates is one a stranger finds broken.

	``claude plugin validate`` is the only thing that reads these files as Claude Code will —
	they are JSON with no import and no type checker, so a mistyped key survives every other
	check here. `#382` is what that costs: a colon in the skill's frontmatter made it load with
	no description at all, so it would never have triggered, and 2,391 tests passed over it.

	**Derived from the filesystem rather than from a list**, because the failure this closes is
	somebody adding a third plugin and validating two. The workflow names each one explicitly so
	a failure says which manifest; this is what stops that list going stale.
	"""

	workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
	unvalidated = [
		path.name
		for path in PLUGIN_DIRECTORIES
		if f"claude plugin validate ./{path.relative_to(ROOT).as_posix()}" not in workflow
	]

	assert not unvalidated, (
		f"{', '.join(unvalidated)} ship and CI never validates them. Add a line to the Validate "
		f"step in ci.yml and a matching entry in scripts/check.py."
	)


def test_the_two_plugins_carry_the_same_skill () -> None:
	"""The duplication decision `#538` took, held to rather than merely regretted.

	A plugin is self-contained by construction, so the practice has to exist in both — and two
	copies of a file nothing compares is this codebase's signature defect with a fresh disguise.
	Byte-identical, so the answer to "which one is right" can never be interesting.

	**The skill is written to serve both**, which is why one file can do: its diagnosis section
	establishes which plugin is installed before offering any remedy, because every remedy for
	one is wasted effort on the other.
	"""

	copies = {
		path.name: (path / "skills" / "subroutine" / "SKILL.md").read_bytes()
		for path in PLUGIN_DIRECTORIES
	}

	assert len(set(copies.values())) == 1, (
		f"the skill differs between {', '.join(sorted(copies))}. Edit one and copy it to the "
		f"others; a reader cannot tell which copy the practice actually is."
	)


def test_the_marketplace_tells_a_reader_which_plugin_is_theirs () -> None:
	"""Two entries side by side, and the reader has to be able to choose without installing one.

	**The listing is the only thing they see first.** Somebody browsing does not know there are
	two, let alone that the difference is where their work lives — so each description has to
	name its own situation *and* point at the other. `#515`'s lesson one level up: an install
	that cannot work should say so before it is installed, not after.
	"""

	described = {entry["name"]: entry["description"] for entry in _read(MARKETPLACE)["plugins"]}

	assert "subroutine-remote" in described["subroutine"], (
		"the local plugin's listing must name the remote one, or somebody whose work is on a "
		"server installs the one that cannot reach it"
	)
	assert "subroutine" in described["subroutine-remote"]

	# Neither may claim the other's requirement. The remote one needs nothing installed, and
	# saying otherwise is the friction this whole plugin removes.
	assert not any(
		phrase in described["subroutine-remote"]
		for phrase in ("uv tool install", "pipx install", "subroutine init")
	), "the remote plugin needs no install, and its listing must not imply one"


#: An instruction to install the package the plugin would otherwise fetch for itself. Three
#: tool names rather than a bare ``install``, so *"install 'subroutine-remote' instead"* — which
#: is a plugin, and correct — is not caught by a rule about a Python package.
_INSTALL_THE_PACKAGE = re.compile(
	r"(?:pip|pipx|uv\s+tool)\s+install\s+subroutine\b|subroutine\s+installed\b",
	re.IGNORECASE,
)


@pytest.mark.parametrize(
	"directory", PLUGIN_DIRECTORIES, ids=lambda path: typing.cast(pathlib.Path, path).name
)
def test_the_marketplace_names_the_prerequisite_the_manifest_actually_has (
	directory: pathlib.Path,
) -> None:
	"""**What a plugin needs is derivable, and until `#720` nothing compared it with the claim.**

	This test used to be ``test_the_marketplace_says_the_product_is_a_separate_install`` and it
	asserted the opposite of what is true: that the description names ``uv tool install`` or
	``pipx install``, and ``subroutine init``. `#585` made both false the day 0.6.0 shipped —
	the server bootstraps through ``uvx`` now — and the guard sixty lines below this one has
	asserted exactly that since. **Two checks in one file, contradicting each other, both
	green**, and the older one was holding the false sentence in place: anybody correcting the
	description would have been failed by the suite for doing it.

	That is this codebase's signature defect in its sharpest form yet, and the shape to carry is
	that a guard written for a fact does not notice when the fact changes. It has to be derived
	from something that moves with the code.

	So: the manifest says what must already be on the machine, and the description has to agree.

	- A plugin that launches a **command** names the thing that provides it, because that is the
	  one prerequisite a reader has to satisfy before the plugin can work at all.
	- A plugin that **fetches the package itself** must not tell anybody to install it. It is
	  not merely redundant — it is the trip to a terminal `#585` exists to remove, offered to
	  somebody who had already been told there was nothing to do.
	"""

	described = next(
		entry["description"]
		for entry in _read(MARKETPLACE)["plugins"]
		if entry["name"] == directory.name
	)
	server = _read(directory / ".mcp.json")["mcpServers"]["tools"]

	launcher = server.get("command")

	if launcher:
		# `uvx` is what `uv` provides, and `uv` is what a reader installs — so the description
		# names the thing they would go and get, not the executable we happen to invoke.
		assert re.search(rf"\b{re.escape(launcher.removesuffix('x'))}\b", described), (
			f"{directory.name} launches {launcher!r} and its description never mentions it, so "
			f"the one thing a reader must install first is unsaid"
		)

	fetches = any(
		str(argument).startswith("subroutine~=") for argument in server.get("args", [])
	)

	if fetches:
		found = _INSTALL_THE_PACKAGE.search(described)

		assert not found, (
			f"{directory.name} fetches Subroutine itself, and its description says "
			f"{found.group(0)!r} — which is the trip to a terminal `#585` removed, offered to "
			f"somebody who has just been told there is nothing to install"
		)


def test_a_locally_launched_plugin_says_so_before_anybody_installs_it () -> None:
	"""**`#515`: it can be installed where it cannot possibly run, and nothing said so.**

	Installed in the Claude web client, which accepted it, opened a settings page with all four
	fields present and well written, and produced no tools. Every signal says success and the
	only evidence is an absence — so the reader concludes the product is broken rather than
	that the client is wrong for it. Both remedies a competent person tries next, checking the
	``PATH`` and installing the program, *confirm* that conclusion instead of correcting it.

	Prose is the only channel that reaches them, because nothing of ours executes on a client
	that cannot start the server. That is `#499`'s rule from an unfamiliar direction: the
	guaranteed channel here is the listing, and it named none of the conditions on using the
	thing. Both descriptions carry it — the marketplace's is read *before* installing and the
	manifest's *after*, and somebody meets one or the other rather than both.
	"""

	server = _read(SERVERS)["mcpServers"]["tools"]

	# **This guard named its own expiry, the condition fired, and the claim survived it** —
	# which is worth recording, because the next reader will otherwise take the note below as
	# stale and delete the check. It used to say: "only true while the server is a local
	# command; the moment `#516` offers an HTTP transport, 'not on the web' stops being true."
	#
	# `#516` shipped. An instance now serves MCP over HTTP and `subroutine-remote` reaches it
	# with nothing installed. **And "not on the web" is still true, for a different reason**:
	# claude.ai does not read Claude Code plugins at all, whichever transport they declare, so
	# neither plugin appears there. Reaching an instance from a browser is a *connector*, which
	# is `#514` and is not a plugin.
	#
	# So what makes *this* entry go away is narrower than it was: this plugin declaring a `url`,
	# which would mean the local and remote plugins had merged and this whole file wants
	# rewriting.
	assert "url" not in server, (
		"the local plugin has grown a url, so it is no longer the stdio half of a pair — "
		"revisit this guard, both descriptions and the two-plugin decision `#538` together"
	)

	listed = {entry["name"]: entry["description"] for entry in _read(MARKETPLACE)["plugins"]}

	for name, described in (
		("the marketplace listing", listed["subroutine"]),
		("the plugin manifest", _read(PLUGIN)["description"]),
	):
		assert "your own machine" in described, (
			f"{name} does not say the plugin runs a program on the reader's own machine, "
			f"which is the premise that makes the rest of it make sense"
		)

		assert "not on the web" in described, (
			f"{name} does not name the client where this cannot work, which is the whole of "
			f"`#515` — an absence of tools is indistinguishable from a broken product"
		)


def test_the_server_bootstraps_through_uvx_so_nothing_is_installed_first () -> None:
	"""``#585``. The plugin must work on arrival, not after a trip to a terminal.

	It used to run ``${user_config.command}``, defaulting to a bare ``subroutine`` — which
	needed Python, the package, and a ``PATH`` entry before the tools appeared, and reported
	``✘ Failed to connect`` when any of the three was missing. ``uvx`` fetches and caches on
	first use instead.

	**Measured before it was chosen**: 5.66s on a cold cache, 0.60s warm, 14 tools and a real
	capture through the relay. And ``uvx`` *prefers an already-installed uv tool* — proved by
	installing 0.5.0 into an isolated tool directory and watching ``uvx subroutine`` report
	0.5.0 rather than the published 0.6.0 — so somebody who ran ``uv tool install`` keeps their
	copy and one plugin serves both.

	The cost, taken deliberately: pointing at a virtualenv or a checkout is no longer a settings
	field, because ``.mcp.json`` takes a fixed argument list and ``uvx`` needs the package as
	its first argument — there is no way to spell "and skip that argument", since an empty one
	is passed through verbatim and refused. ``claude mcp add subroutine -- <path> mcp`` is that
	route now and is better for it: the marketplace copy is cached and lags.
	"""

	server = _read(SERVERS)["mcpServers"]["tools"]

	assert server["command"] == "uvx"
	assert server["args"][0].startswith("subroutine~="), (
		"the package uvx is asked for is the first argument, and it must be pinned"
	)
	assert server["args"][1] == "mcp"


def test_the_bootstrap_is_pinned_to_the_series_that_was_released () -> None:
	"""**An unpinned ``uvx`` moves the code under somebody's database on a day they did not
	choose**, which is the one thing ``uv tool install`` does not do.

	``uvx subroutine`` resolves to whatever is newest whenever the cache next looks. For a local
	instance that is the program *and* the instance, so a minor version arriving unasked is a
	migration arriving unasked. ``~=X.Y.0`` is a compatible release — patches yes, a minor no.

	Derived from ``docs/releases.json`` rather than restated, so the two cannot drift: that file
	is written by the same run of ``release.py`` that writes this pin. **Not** compared against
	the plugin manifest, which leads the package between releases (`#396`) and would ask PyPI
	for a version that does not exist yet.
	"""

	published = json.loads(
		(ROOT / "docs" / "releases.json").read_text(encoding="utf-8")
	)["releases"][0]["version"]
	major, minor, *_ = published.split(".")

	for path in sorted((ROOT / "plugins").glob("*/.mcp.json")):
		for server in _read(path)["mcpServers"].values():
			pinned = [a for a in server.get("args", []) if a.startswith("subroutine~=")]

			assert pinned == [] or pinned == [f"subroutine~={major}.{minor}.0"], (
				f"{path.parent.name} asks uvx for {pinned}, and the newest published release "
				f"is {published} — release.py writes both, so they have drifted"
			)


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


@pytest.mark.parametrize("option", ["connection", "workspace", "token"])
def test_every_declared_option_is_substituted_somewhere (option: str) -> None:
	"""An option nobody reads is a question asked for nothing.

	Cheap, and it fails in the direction that matters: adding a field to the dialog and
	forgetting to wire it is silent, and the user answering it has no way to find out.
	"""

	assert _read(PLUGIN)["userConfig"][option]
	assert "${user_config." + option + "}" in SERVERS.read_text(encoding="utf-8")


@pytest.mark.parametrize("option", ["connection", "workspace", "token"])
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

	# **The count is a completeness claim and it has moved twice**, which is why it is asserted
	# rather than left to prose. "Two causes" was true until the plugin became installable in a
	# browser (`#515`); "three" was true until `#540` shipped a second plugin that fails for
	# reasons none of the three cover. A reader told there are three stops looking after three.
	assert "Four causes" in text, (
		"the skill enumerates the causes of missing tools and the count is part of the claim; "
		"a reader told there are fewer will stop looking early"
	)

	assert "browser" in text, (
		"the skill must name the client where no remedy it gives can work, or it sends that "
		"reader to an install and a reconfiguration that cannot help"
	)

	# **And the remote plugin's own failure, which every remedy above is wasted effort on.**
	# The install instructions are actively wrong for a session connected by address — there is
	# nothing to install — so a skill that names them without naming the other plugin sends that
	# reader to an evening of `PATH` diagnosis for a rejected token.
	assert "subroutine-remote" in text, (
		"the skill offers install remedies, which are wrong for the plugin that installs "
		"nothing; it has to say which plugin it is talking to before it talks"
	)


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


@pytest.mark.parametrize(
	"plugin", [str(path) for path in PLUGIN_DIRECTORIES], ids=[p.name for p in PLUGIN_DIRECTORIES]
)
def test_a_changed_plugin_carries_a_version_nobody_has_installed (plugin: str) -> None:
	"""`#380`, and then `#393` when this was not strong enough.

	Claude Code caches an installed plugin under its **version**, so an install at a version
	already present is a no-op. A manifest edited without a bump can therefore never reach
	anybody: `claude plugin update` finds the version already there and correctly does nothing.

	**The first version of this compared against the newest tag, and Simon met the same failure
	three commits later.** The manifest said 0.2.1, the tag was v0.2.0, so it passed — and went
	on passing through the server rename and a skill fix, both of which were then uninstallable
	because 0.2.1 was already cached. It answered "has this changed since the last release" when
	the question is "has this changed since the last thing anybody could have installed", and
	what people install is a version.

	That is this repository's own lesson landing on a guard written hours earlier: **a guard
	checks the shape it was written from.** `#380` was written from the state where the manifest
	still named the tag's version, so that is the only state it could see.

	The rule now: if anything under a plugin's own directory differs from the commit that last
	*set* its current version, that version must be bumped again. It subsumes the tag
	comparison, because a manifest still naming the tag's version while the contents differ is
	the same condition.

	**The working tree counts**, deliberately — an edit in progress should demand the bump
	before the commit, not after somebody has failed to install it.

	**Per plugin, and that is the point of the parameter** (`#540`). A version is a cache key
	for the plugin that carries it, so comparing the whole of ``plugins/`` against one
	manifest's history would demand a bump of both whenever either changed — and, worse, could
	be satisfied by bumping *either*. Each plugin answers for its own subtree.
	"""

	directory = pathlib.Path(plugin)
	manifest = directory / ".claude-plugin" / "plugin.json"
	declared = _read(manifest)["version"]
	introduced = subprocess.run(
		["git", "log", "-1", "--format=%H", "-S", f'"version": "{declared}"', "--", str(manifest)],
		capture_output=True, text=True, cwd=ROOT, check=False,
	)
	commit = introduced.stdout.strip()

	if introduced.returncode != 0 or not commit:
		pytest.skip("no commit introduced this version, so there is nothing to compare against")

	changed = subprocess.run(
		["git", "diff", "--name-only", commit, "--", str(directory.relative_to(ROOT).as_posix())],
		capture_output=True, text=True, cwd=ROOT, check=False,
	)

	if changed.returncode != 0:
		pytest.skip("git could not compare the plugin against that commit")

	touched = sorted(changed.stdout.split())

	assert not touched, (
		f"{directory.name} has changed since {commit[:9]} set version {declared} "
		f"({', '.join(touched)}), and its manifest still says {declared}. Claude Code caches "
		f"by version, so anybody already on {declared} would never receive these — bump the "
		f"version in {manifest.relative_to(ROOT)}."
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


def test_the_skill_teaches_the_project_key_rule_the_product_has () -> None:
	"""`#533`. The skill told an agent to upper-case a key, use no hyphens, and stop at sixteen.

	All three were made false by `#508` on the previous day, and the sentence contradicted the
	example three lines below it, which was already lower case. Found by a stranger's agent
	asked to make a project called "Claude Test": it produced `claudetest`, which is what we
	told it to produce.

	**`#508`'s commit said the rule was written in eleven places and the suite found ten. This
	was the twelfth**, and no guard could see it: `test_plugin` checks that the skill names no
	command or tool that does not exist, which is a different question from whether the prose
	it teaches is true. So this asks the code.

	The tool schema was right the whole time — *"A key is permanent and lower case, like web or
	web-sales"* — which is the sharp part. The channel an agent is *guaranteed* to read was
	correct and the one it chose to read was wrong, so being thorough was what misled it.
	"""

	text = _skill()

	for demonstrated in re.findall(r'key="([^"]+)"', text):
		assert subroutine.domain.projects.KEY_PATTERN.fullmatch(demonstrated), (
			f"the skill demonstrates key={demonstrated!r}, which this product would refuse"
		)
		assert len(demonstrated) <= subroutine.domain.projects.MAX_KEY_LENGTH

	# **Whitespace collapsed first, or this dictates where the prose wraps.** It failed on the
	# corrected skill, because Markdown had broken the line between "32" and "characters" — a
	# guard that makes a true sentence fail is one somebody edits the sentence to satisfy.
	flowed = " ".join(text.split())

	assert f"{subroutine.domain.projects.MAX_KEY_LENGTH} characters" in flowed, (
		f"the skill states a key length that is not {subroutine.domain.projects.MAX_KEY_LENGTH}"
	)

	# **The word that was wrong, asserted absent.** §13.5b's precedent: where a surface must not
	# use a piece of vocabulary, the check is that it does not appear. A key has been lower case
	# since `#508` and displayed lower case since, so an instruction to upper-case one produces a
	# key the product immediately renames under the agent.
	assert "uppercase" not in text.lower(), (
		"the skill tells an agent to upper-case something; keys are lower case (`#508`)"
	)


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


def test_neither_listing_offers_a_route_that_cannot_carry_a_token () -> None:
	"""`#560`, from Simon's first contact on Claude Cowork (`#559`).

	The remote plugin's description used to end *"to reach an instance from claude.ai, add it
	there as a connector instead"*. Followed, that lands in an **Add custom connector** dialog
	offering a URL and OAuth client credentials — no header field, `static_headers` not offered
	— so a Subroutine token, the only credential this product issues, has nowhere to go. It was
	measured on the surface rather than predicted; `#514` had reached the same conclusion from
	Anthropic's documentation.

	**Worse than saying nothing**, which is `#515`'s framing one surface over: the reader
	arrives from a successful install, follows the one sentence that looks like an escape
	hatch, and the effort spent confirms the wrong conclusion.

	**Narrow on purpose.** This checks the two descriptions that name the connector route, and
	nothing else — a guard over prose that fires on correct prose is one somebody switches off
	(`#546`). What makes it go away is `#514`: when a connector *can* carry a credential, the
	advice becomes true and this test should be deleted with the sentence it forbids.
	"""

	listed = {entry["name"]: entry["description"] for entry in _read(MARKETPLACE)["plugins"]}

	for where, described in (
		("the marketplace listing", listed["subroutine-remote"]),
		("the plugin manifest", _read(REMOTE)["description"]),
	):
		assert "add it there as a connector" not in described, (
			f"{where} sends a reader to a dialog that cannot take a Subroutine token"
		)

		# The positive half, which is what makes the negative one safe: saying less would leave
		# somebody to discover the wall themselves, which is the same cost by a slower route.
		assert "OAuth" in described, (
			f"{where} does not say why a connector cannot be used, so a reader has no reason "
			f"not to go and try it"
		)


def test_the_skill_names_the_failure_that_every_other_signal_calls_healthy () -> None:
	"""`#570`. The ladder tested configuration on every rung, and the fault was not there.

	A first-contact review followed it to the bottom and cleared everything: the plugin was
	installed and enabled, the address was configured, the instance answered a well-formed 401
	in 0.2s, and `claude mcp list` reported `✔ Connected` — while the session it was run from
	had no tools at all. The session had started an hour before the plugin was configured, and
	MCP servers are attached when a session begins.

	**That is the state anybody is in during the minutes after they set this up**, so it is the
	likeliest rung to need and it was the one missing. Worse, the command the ladder offers to
	separate the causes is the one that misleads here: it runs in a new process and reports the
	configuration correctly, which reads as "connected but broken".

	Narrow on purpose (`#546`): this checks the skill says the two things a reader has to know —
	that a session can predate its configuration, and what to do about it.
	"""

	for directory in PLUGIN_DIRECTORIES:
		name = directory.name
		skill = (directory / "skills" / "subroutine" / "SKILL.md").read_text(encoding="utf-8")

		assert "predates" in skill or "predate" in skill, (
			f"{name}'s skill does not tell a reader a session can predate its configuration, "
			f"which is the one cause every other rung reports as healthy"
		)

		assert "Reload the window" in skill, (
			f"{name}'s skill names the cause without naming the remedy"
		)


def test_the_skill_asks_for_a_claim_without_a_condition_on_it () -> None:
	"""`#705`. The instruction was there all along and had a 0% hit rate — because of one clause.

	It read *"If anybody else works from this list, take the task before you start it."* An agent
	alone on an instance evaluates that as false, and it **was** false, for as long as there was
	one of us. By the time a second worker exists the habit needed to have been there already:
	measured on 2026-08-09, every open item in `projects` had `claimed_by_id: null` and nothing
	had ever been claimed.

	**A conditional instruction whose condition the reader evaluates for itself, in a system
	whose whole premise is that other workers cannot see your screen, switches itself off exactly
	when the population it guards against appears.**

	So the guard is on the *absence* of a condition, which is an odd thing to assert and is the
	thing that actually went wrong. Checked against the sentence that shipped rather than against
	any phrasing of it, so a rewrite that keeps the clause fails.
	"""

	skill = SKILL.read_text(encoding="utf-8")
	claiming = skill[skill.index("subroutine_claim"):]

	assert "If anybody else works from this list" not in skill, (
		"the claim instruction is conditional again, and the condition reads as false to the "
		"agent most likely to be alone with the instance"
	)

	# The three calls, in the order they have to happen. `release` last is the one measured to
	# be necessary rather than implied: completing a task leaves the claim exactly as it was.
	for named in ("subroutine_claim(ref=42)", "release=true", 'status="in_progress"'):
		assert named in claiming, f"the skill no longer shows {named}"


def test_the_skill_says_a_claim_and_a_status_are_different_facts () -> None:
	"""`#726`, and Simon's ruling on 2026-08-09 that neither is derived from the other.

	The first design had claiming write `in_progress` and releasing revert it. He refused it on
	two grounds and both are right: a claim is taken *before* the work and may be given back
	without any being done, and release has four possible destinations and cannot tell them
	apart — the worst being a task started and abandoned mid-context, which a revert would
	report as untouched.

	So the skill has to ask for **both**, and say why they are not the same thing. Without that
	an agent reads two instructions with one apparent purpose and drops one of them.
	"""

	skill = SKILL.read_text(encoding="utf-8")

	assert "two different facts" in skill, (
		"the skill asks for a claim and a status without saying they answer different "
		"questions, which is an invitation to treat one as redundant"
	)

	assert "Finishing does not release it" in skill, (
		"an agent told only to claim leaves one on every finished item until the lease runs "
		"out — measured: `done` changes neither the holder nor the expiry"
	)


def test_the_guaranteed_channel_names_the_practice_and_not_only_the_endpoint () -> None:
	"""`#499`: the channel every agent gets must name what the optional ones teach.

	`/v1/docs/agent` reaches an agent that has no plugin and so no skill. It named
	`POST /v1/tasks/{ref}/claim` in a table row — so `#705`'s claim that it *"describes claiming
	nowhere"* was not accurate, and is corrected on the item — but it said nothing about
	releasing, and nothing about a status. An agent reading only this would have taken leases
	and never given one back.
	"""

	guide = subroutine.api.meta.guide_text()

	assert "/release" in guide, "the guide names claiming and never says to give it back"

	assert "in_progress" in guide, (
		"the guide never tells an agent to say it has started, so a person watching sees items "
		"appear finished with nothing in between"
	)
