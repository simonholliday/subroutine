"""The upgrade rehearsal's own judgement - ``scripts/rehearse_upgrade.py``, item ``#5063``.

**The rehearsal itself is not run here.** It installs a release from the package index and takes
half an hour, so ``scripts/release.py`` runs it before every tag, and ``test_release_script``
holds where. What is tested here is what it decides from what it saw - which answers are server
errors and which commands crashed - and that its reads leave no query parameter unasked, because
a sweep that has quietly stopped asking reads exactly like one that found nothing wrong.
"""

import importlib.util
import json
import os
import pathlib
import sys
import types

import pytest

import subroutine.api.app

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"


def _load (name: str) -> types.ModuleType:
	"""Load one script by path, the way the other scripts' tests do."""

	spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")

	assert spec is not None and spec.loader is not None

	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	return module


@pytest.fixture(scope="module")
def rehearsal () -> types.ModuleType:
	"""Return ``scripts/rehearse_upgrade.py`` as a module."""

	return _load("rehearse_upgrade")


def test_readyz_answering_503_before_the_upgrade_is_not_a_server_error (rehearsal: types.ModuleType) -> None:
	"""Before ``db upgrade``, ``/readyz`` saying the database is behind is it doing its job."""

	answers = [{"who": "keanu", "path": "/readyz", "status": 503, "detail": "This database is at schema x"}]

	assert rehearsal.server_errors(answers, before_upgrade=True) == []


def test_after_the_upgrade_readyz_answering_503_is_one (rehearsal: types.ModuleType) -> None:
	"""After it, the same answer means the upgrade left the database somewhere this cannot serve."""

	answers = [{"who": "keanu", "path": "/readyz", "status": 503, "detail": "This database is at schema x"}]

	assert len(rehearsal.server_errors(answers, before_upgrade=False)) == 1


def test_every_other_server_error_before_the_upgrade_is_one (rehearsal: types.ModuleType) -> None:
	"""`SR#5057`: a read in the window answered 500, where it should have said the schema was behind.

	That is what the Hyperfence instance met on ``/v1/me``: an instance serves reads while it
	waits for ``db upgrade``, so its reads are where an operator meets a fault first.
	"""

	answers = [
		{"who": "laurence", "path": "/v1/me", "status": 500, "detail": "Internal server error"},
		{"who": "keanu", "path": "/v1/tasks?workspace_id=zion", "status": 409, "detail": "Schema mismatch"},
	]

	found = rehearsal.server_errors(answers, before_upgrade=True)

	assert len(found) == 1, found
	assert "500 on /v1/me" in found[0], found
	assert "laurence" in found[0], found


def test_one_fault_on_every_item_is_one_line_naming_everybody_who_met_it (rehearsal: types.ModuleType) -> None:
	"""An item's number is folded into the path's shape, and the commonest fault comes first."""

	answers = [
		*({"who": who, "path": f"/v1/tasks/{number}/events", "status": 500} for who in ("joe", "keanu") for number in (3, 17)),
		{"who": "hugo", "path": "/v1/journal?limit=2", "status": 502},
	]

	found = rehearsal.server_errors(answers, before_upgrade=False)

	assert len(found) == 2, found
	assert found[0].startswith("4 x 500 on /v1/tasks/N/events, as joe, keanu"), found
	assert found[1].startswith("1 x 502 on /v1/journal, as hugo"), found


@pytest.mark.parametrize(
	("code", "said", "crash"),
	[
		(0, "Done: Pay the gas bill", False),
		(1, "There is no #9 in metacortex.", False),
		(2, "No such option: --binds", True),
		(1, "Traceback (most recent call last):\n  File ...", True),
		(1, "Something went wrong that should not have.", True),
		(124, "It did not finish within 300 seconds.", True),
		(-9, "", True),
	],
	ids=["an answer", "a refusal", "a command line this build does not take", "a traceback",
	     "the program's own crash report", "a command that hung", "a process killed"],
)
def test_what_counts_as_a_crash (rehearsal: types.ModuleType, code: int, said: str, crash: bool) -> None:
	"""A refusal is an answer; a traceback, a hang and a command this build does not take are not."""

	assert rehearsal.crashed(rehearsal.Ran(code, said)) is crash


def test_a_credential_is_never_printed_back (rehearsal: types.ModuleType) -> None:
	"""The rehearsal's credentials are throwaway, and still never shown: a report gets pasted."""

	said = "Your token: sr_ab12cd34_s3cr3t-Value.x\nStore it now."

	assert "s3cr3t" not in rehearsal.tail(said)
	assert "s3cr3t" not in rehearsal.masked(said)


def test_a_refusal_drawn_in_a_panel_is_reported_as_its_words (rehearsal: types.ModuleType) -> None:
	"""The command line draws a refusal in a box; a report line is the sentence, not the box."""

	said = "\u256d\u2500 Error \u2500\u2500\u2500\u256e\n\u2502 Missing argument 'slug'.      \u2502\n\u2570\u2500\u2500\u2500\u256f\n"

	assert rehearsal.tail(said) == "Error | Missing argument 'slug'."


def test_a_command_that_asks_a_question_reads_nothing_rather_than_waiting (
	rehearsal: types.ModuleType, tmp_path: pathlib.Path
) -> None:
	"""Run from a release at a terminal, a prompt must not wait on the operator's keyboard.

	**This process's own input holds something while it runs**, standing for that keyboard: the
	suite's is empty already, so a command that inherited it would read nothing and pass.
	"""

	here = rehearsal.Installation(tmp_path / "installation", "sqlite", None)
	reader = rehearsal.Program("a reader", (sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"))
	kept = os.dup(0)
	reading, writing = os.pipe()
	os.write(writing, b"typed by the operator\n")
	os.close(writing)

	try:
		os.dup2(reading, 0)
		said = here.run(reader).said

	finally:
		os.dup2(kept, 0)
		os.close(kept)
		os.close(reading)

	assert said.strip() == "''", f"a command read what was on the caller's input: {said!r}"


def test_an_item_number_is_read_past_what_follows_the_answer (rehearsal: types.ModuleType) -> None:
	"""A release told that input was piped in notes, after its answer, that it read none."""

	said = '{\n  "ref": 21,\n  "title": "We run PostgreSQL in production"\n}\nAnything piped in was not read.\n'

	assert rehearsal.number_in(said) == 21
	assert rehearsal.number_in("Nothing is numbered here.") is None


def test_a_server_error_is_reported_in_the_problem_s_own_words (rehearsal: types.ModuleType) -> None:
	"""The line is the sentence the problem document gives, not the end of its JSON."""

	body = json.dumps({"detail": "Something went wrong that should not have.", "code": "internal_error", "instance": "/v1/me"})
	found = rehearsal.server_errors([{"who": "keanu", "path": "/v1/me", "status": 500, "detail": body}], before_upgrade=True)

	assert found == ["1 x 500 on /v1/me, as keanu: Something went wrong that should not have. (internal_error)"], found


def test_it_starts_from_the_newest_release_the_record_names (
	rehearsal: types.ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The record ``scripts/release.py`` writes, whose first entry is the last release published.

	A tag nothing was published from is never in it - ``v0.6.2`` and ``v0.7.0`` are tags and not
	releases - so it is the version an operator can be running, which the newest tag may not be.
	"""

	record = tmp_path / "releases.json"
	record.write_text(json.dumps({"releases": [{"version": "1.2.3"}, {"version": "1.2.2"}]}), encoding="utf-8")
	monkeypatch.setattr(rehearsal, "RELEASES", record)

	assert rehearsal.last_release() == "1.2.3"


def test_every_query_parameter_a_read_route_declares_is_asked () -> None:
	"""A route is asked once for each sample of each parameter it declares, so each needs one.

	**Both ways.** A parameter with no sample and no written reason is one the sweep never sends,
	and a sample or a reason naming a parameter no route declares is about something gone.
	"""

	sweep = _load("rehearse_sweep")
	declared = {
		one["name"]
		for methods in subroutine.api.app.create_app().openapi()["paths"].values()
		for one in methods.get("get", {}).get("parameters", [])
		if one["in"] == "query"
	}
	covered = set(sweep.samples("1")) | set(sweep.NOT_SAMPLED)

	assert not declared - covered, f"asked by no sample and excused nowhere: {sorted(declared - covered)}"
	assert not covered - declared, f"a sample or an excuse for a parameter no read route declares: {sorted(covered - declared)}"
	assert not set(sweep.samples("1")) & set(sweep.NOT_SAMPLED), "a parameter is both sampled and excused"
