"""What a caller says it is running reaches the instance that reports it — `SR#839`.

**The sending half shipped on 2026-08-24 and nothing read it for nine days.** That was
deliberate: a plugin is a *cache key*, so the copy that goes stale is the one on somebody's
machine, and a fix teaching only a future instance to notice would be useless for exactly the
population it is for. Whatever a release emits is what every later instance has to work with.
This file covers the other half arriving.

**Why it cannot be answered locally.** Since `#539` the tools run wherever the *instance* runs,
so :func:`subroutine.installations.plugin` reads the **server's** environment on a served
connection. `#564` made that honest — *"what you are running is not visible from here"* — and
this is what makes it answerable instead, for the callers that say.

**The population that matters is the remote plugin, and it sends only one of the two headers.**
``plugins/subroutine-remote/.mcp.json`` carries ``Subroutine-Plugin`` and no
``Subroutine-Program``, because nothing of ours runs on that machine. So a reader keyed on the
program alone would discard the one version those callers *do* send — which is the shape
``test_a_caller_that_names_only_its_plugin_is_still_answered`` exists for, and it is the
Hyperfence team's shape.
"""

import json
import pathlib
import typing

import sqlalchemy.orm

import subroutine
import subroutine.api.mcp
import subroutine.installations
import test_api_tasks

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The remote plugin's own manifest — read rather than restated, so a header it stops sending
#: cannot leave this file testing a shape nothing produces.
REMOTE = ROOT / "plugins" / "subroutine-remote" / ".mcp.json"


def _remote_headers () -> dict[str, str]:
	"""Return the headers the remote plugin really sends, minus its credential."""

	manifest = json.loads(REMOTE.read_text(encoding="utf-8"))
	headers = dict(manifest["mcpServers"]["tools"]["headers"])

	headers.pop("Authorization", None)

	return headers


def _whoami (world: test_api_tasks.World, **headers: str) -> str:
	"""Ask the served instance who the caller is, with whatever the caller announced."""

	answered = world.call(
		"POST",
		subroutine.api.mcp.PATH,
		content=json.dumps(
			{
				"jsonrpc": "2.0",
				"id": 1,
				"method": "tools/call",
				"params": {"name": "subroutine_whoami", "arguments": {}},
			}
		),
		headers={"content-type": "application/json", **headers},
	)

	assert answered.status_code == 200, answered.text

	result: dict[str, typing.Any] = answered.json()["result"]

	return str(result["content"][0]["text"])


# --- Reading what was sent ---------------------------------------------------------------


def test_the_reader_and_the_writer_are_driven_against_each_other () -> None:
	"""``said_by`` reads exactly what ``calling`` writes — `SR#839`.

	**The two halves live in one module for this reason.** A reader written at the far end would
	be a second copy of two header names, free to stop matching the day one of them moves, and
	the failure would be silent: a caller announcing itself and an instance reporting that it
	said nothing.
	"""

	sent = subroutine.installations.calling()
	read = subroutine.installations.said_by(sent)

	assert read.program == subroutine.installations.program()
	assert read.said_anything

	# The plugin half is present exactly when this process has one, which is the property
	# `calling` promises rather than a fact about the machine running the suite.
	assert read.plugin == subroutine.installations.plugin()


def test_a_header_name_is_read_however_it_is_capitalised () -> None:
	"""HTTP header names are case-insensitive and three libraries disagree about mappings.

	Starlette's own headers already fold case; a plain dict does not. **That difference passes
	in a unit test and fails on the wire**, which is the direction worth guarding.
	"""

	said = subroutine.installations.said_by(
		{"subroutine-program": "1.2.3", "SUBROUTINE-PLUGIN": "1.2.4"}
	)

	assert said == subroutine.installations.Caller(program="1.2.3", plugin="1.2.4")


def test_a_caller_that_said_nothing_is_not_mistaken_for_one_that_did () -> None:
	"""Null means *did not say*, never *is running nothing* — and an empty string is not a claim.

	Reporting a version nobody sent is `#564`'s defect exactly: *"Program X, instance X"* with
	X the instance twice and one of them labelled as the caller's.
	"""

	assert not subroutine.installations.said_by({}).said_anything
	assert not subroutine.installations.said_by({"Subroutine-Program": "   "}).said_anything
	assert not subroutine.installations.SAID_NOTHING.said_anything

	assert subroutine.installations.said_by({"Subroutine-Plugin": "0.8.10"}).said_anything


# --- What the answer says ----------------------------------------------------------------


def test_a_caller_that_names_itself_is_reported_rather_than_refused (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The whole of `SR#839`, driven through the real endpoint.

	`#564`'s refusal is what this replaces **for callers that say**, and the assertion pairs
	the two: the announced versions appear, and the sentence saying they could not be seen does
	not.
	"""

	world = test_api_tasks._world(session)

	answered = _whoami(
		world, **{"Subroutine-Program": "9.9.9", "Subroutine-Plugin": "9.9.8"}
	)

	# `Plugin` leads, so it takes the capital; the rest of the line is lower case.
	assert "Plugin 9.9.8, program 9.9.9, instance " in answered, answered
	assert "not visible from here" not in answered, (
		"the caller announced itself and was told its own versions could not be seen"
	)

	# And the instance is still named beside them, which is the point of a three-way check.
	assert f"instance {subroutine.__version__}" in answered


def test_a_caller_that_says_nothing_is_told_so_exactly_as_before (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#564`'s refusal survives untouched, and that is a requirement rather than a leftover.

	**This is what stops `SR#839` being closed by reporting something.** The instance's own
	version rendered under the word *Program* is the defect `#564` was filed for; an agent read
	it and concluded there was no version problem, which is the worst thing a check can do.
	"""

	world = test_api_tasks._world(session)
	answered = _whoami(world)

	assert "not visible from here" in answered, answered
	assert f"Program {subroutine.__version__}" not in answered, (
		"the instance's own version is being reported as the caller's, which is `#564`"
	)


def test_a_caller_that_names_only_its_plugin_is_still_answered (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The remote plugin's own shape, taken from its own manifest — and `SR#839`'s real audience.

	``plugins/subroutine-remote/.mcp.json`` sends ``Subroutine-Plugin`` and **no**
	``Subroutine-Program``, because nothing of ours runs on that machine: the editor posts
	straight to ``/mcp`` and the only version that exists is the literal in the manifest.

    **Keyed on the program alone, the refusal discarded it.** That was the same condition as
	*knowing nothing about the caller* right up until the reading half shipped, and then it
	stopped being — so this population would have been told exactly what it was told before,
	which is nothing, while announcing itself on every request.

	The headers come from the manifest rather than being restated here, so a header it stops
	sending cannot leave this test proving something about a shape nothing produces.
	"""

	world = test_api_tasks._world(session)
	headers = _remote_headers()

	assert "Subroutine-Plugin" in headers, "the remote plugin stopped announcing itself"
	assert "Subroutine-Program" not in headers, (
		"the remote plugin now sends a program version, so this test is about a shape that no "
		"longer exists — check whether the refusal below is still the right one"
	)

	answered = _whoami(world, **headers)

	assert f"Plugin {headers['Subroutine-Plugin']}" in answered, answered
	assert "not visible from here" not in answered, (
		"a caller that announced its plugin was told nothing about it could be seen"
	)
