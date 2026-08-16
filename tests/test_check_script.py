"""``scripts/check.py`` and the workflows agree about what is checked — items ``#402``, ``#927``.

Two questions, and the second was added by `#927`'s H-17. The first is what this file was
written for: ``scripts/check.py`` and ``ci.yml`` name the same steps, running the same commands.
The second is that **every workflow which runs the suite refuses every skip the suite offers** —
because ``release.yml`` ran it having installed neither Node nor a browser, so every release this
project has cut published on a suite that never rendered the browser app, and reported success.

**Without this file the script is a third place for the truth to live**, which is the defect it
was written to answer rather than a step towards answering it. `#401` happened because "the
checks" existed in two places and only one of them was ever run; a local runner that drifts
from the workflow reproduces that exactly, one level up, with the added confidence of a command
called ``check``.

So the comparison runs both ways. A CI step in neither list fails the build, and an entry
naming a step the workflow no longer has fails it too — the second direction being the one
`#290` found missing from every other allow-list here: an excuse whose reason has expired reads
like a considered decision for as long as nobody looks.

The script is loaded by path, the way ``tests/test_release_notes_script.py`` loads its sibling,
and at import time rather than in a fixture: the parametrised test below needs its list of
checks while pytest is still collecting.
"""

import importlib.util
import pathlib
import re
import sys
import types
import typing

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = pathlib.Path(__file__).resolve().parent
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "ci.yml"
SCRIPT = ROOT / "scripts" / "check.py"

#: How the workflow spells the interpreter. The script uses ``sys.executable``, because a bare
#: ``python`` on a developer's PATH is not reliably the virtualenv's — so the comparison below
#: translates one to the other rather than pretending they are the same string.
INTERPRETER = sys.executable


def _loaded () -> types.ModuleType:
	"""Import ``scripts/check.py`` by path."""

	spec = importlib.util.spec_from_file_location("check_script", SCRIPT)

	assert spec is not None and spec.loader is not None

	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	return module


script = _loaded()


def _steps () -> dict[tuple[str, str], str]:
	"""Return every ``run:`` step in the workflow, keyed by job name and step name.

	Steps with no ``run:`` — ``actions/checkout``, ``setup-python`` — are not checks, and are
	not part of what the workflow claims this project verifies. A step with no ``name:`` would
	be unaddressable, so it is refused loudly rather than skipped: a nameless step is one this
	comparison cannot see, and quietly ignoring it is how the two lists would come apart.
	"""

	# `safe_load`, not `load`. Nothing reads this file at runtime, and a parser that can
	# construct arbitrary objects is still not the one to reach for out of habit.
	workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	found: dict[tuple[str, str], str] = {}

	for job in workflow["jobs"].values():
		name = job["name"]

		for step in job["steps"]:
			if "run" not in step:
				continue

			assert "name" in step, f"a step in {name!r} has a 'run:' and no 'name:'"

			found[(name, step["name"])] = step["run"]

	return found


def _declared () -> set[tuple[str, str]]:
	"""Return the job and step each check stands for."""

	return {(entry.job, entry.step) for entry in script.CHECKS}


def test_every_ci_step_is_either_run_here_or_excused () -> None:
	"""The direction that stops a check being added to CI and never run locally again.

	This is the failure the script exists to prevent, applied to the script: a new step in the
	workflow is a new thing somebody's local run silently does not do, and they would find out
	from a red build after pushing — which is the loop `#402` closes.
	"""

	unaccounted = set(_steps()) - _declared() - set(script.NOT_LOCALLY)

	assert not unaccounted, (
		f"these CI steps are neither run by scripts/check.py nor listed in NOT_LOCALLY with "
		f"a reason: {sorted(unaccounted)}"
	)


def test_nothing_is_excused_that_the_workflow_no_longer_has () -> None:
	"""And the direction that keeps the excuses honest.

	An entry naming a step CI has dropped is indistinguishable from a considered decision, and
	will be read as one. Deleting the entry is what closes it — the property `#290` added to
	``test_reach`` after three stale exemptions sat there naming an item apiece.
	"""

	stale = set(script.NOT_LOCALLY) - set(_steps())

	assert not stale, f"NOT_LOCALLY names steps the workflow no longer has: {sorted(stale)}"


def test_nothing_is_both_run_and_excused () -> None:
	"""Two answers for one step is a state where a reader cannot tell which is true."""

	assert not _declared() & set(script.NOT_LOCALLY)


def test_every_excuse_says_something () -> None:
	"""An empty reason is the entry that will still be here in a year.

	Not a length check for its own sake: the value of ``NOT_LOCALLY`` is entirely in whether
	somebody reading it can decide the entry is still right, and a blank one cannot be judged
	at all.
	"""

	for step, reason in script.NOT_LOCALLY.items():
		assert len(reason) > 40, step


@pytest.mark.parametrize("entry", script.CHECKS, ids=lambda item: item.step)
def test_a_checks_command_is_the_one_the_workflow_runs (entry: typing.Any) -> None:
	"""The commands agree, not only the step names.

	**The half that would otherwise rot invisibly.** Matching on names alone would let ``mypy
	src`` here stand for ``mypy src tests scripts`` there — the two lists in step, the two runs
	checking different things, and the local one reporting success.

	**Matched line by line, not as a substring**, and that distinction was found by falsifying
	rather than reasoned about: written as containment, this test passed with the command
	changed to ``mypy src``, which is a substring of the workflow's — the exact failure the
	paragraph above claims it catches. A whole line has to be equal to a whole command.

	A ``run:`` block may hold several lines, which is why one of them rather than all: the
	plugin job validates two manifests in one step, and each is its own entry here so that a
	failure names which manifest.
	"""

	written = " ".join(entry.command).replace(INTERPRETER, "python")
	steps = _steps()

	assert (entry.job, entry.step) in steps, "the check names a step the workflow has not got"

	commands = [line.strip() for line in steps[(entry.job, entry.step)].splitlines()]

	assert written in commands, f"{written!r} is not one of {commands}"


def test_the_suite_is_told_to_fail_rather_than_skip () -> None:
	"""Every ``SUBROUTINE_TEST_REQUIRE_*`` the local gate sets is one the workflow sets too.

	Without them a missing resource is a *skip*, so a run reports success over part of a suite
	— and the defects this project cares most about are precisely the ones invisible without
	the missing half. Read from the workflow rather than compared against a literal, so the two
	cannot disagree about the name.

	**Compared as a set rather than by naming one** (`#927`'s H-17). This asserted
	``SUBROUTINE_TEST_REQUIRE_POSTGRES`` alone, which was every variable there was on the day
	it was written and two out of three by the time anybody looked.
	"""

	workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

	for entry in script.CHECKS:
		if not entry.env:
			continue

		declared: dict[str, typing.Any] = {}

		for job in workflow["jobs"].values():
			for step in job["steps"]:
				if step.get("name") == entry.step:
					declared = step.get("env") or {}

		wanted = {name: value for name, value in entry.env if name.startswith(_REFUSES_A_SKIP)}
		published = {
			name: str(value)
			for name, value in declared.items()
			if name.startswith(_REFUSES_A_SKIP)
		}

		assert wanted == published, f"{entry.step!r} sets {wanted} locally and {published} in CI"


#: The prefix every "fail rather than skip" variable shares.
_REFUSES_A_SKIP = "SUBROUTINE_TEST_REQUIRE_"


def _refusable () -> set[str]:
	"""Return every skip the suite offers to turn into a failure, read off the suite.

	Derived rather than listed, for `#405`'s reason: a list of what CI must set is a second
	copy of what the suite offers, and the copy is the one that goes stale. ``ADMIN_URL`` and
	friends are excluded by the prefix — they configure the harness rather than refuse a skip.
	"""

	found: set[str] = set()

	for path in sorted(TESTS.glob("*.py")):
		found.update(re.findall(rf"{_REFUSES_A_SKIP}[A-Z_]+", path.read_text(encoding="utf-8")))

	return found


def _refused_by (path: pathlib.Path) -> set[str]:
	"""Return every such variable a workflow sets, anywhere in it.

	The union across the whole file rather than per job, because splitting the suite across
	jobs is a legitimate arrangement — ``ci.yml`` runs the browser tests in their own job so
	Chromium is downloaded once rather than once per Python version. What must not happen is a
	workflow running the tests and covering *none* of a resource.
	"""

	loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
	found: set[str] = set()

	for job in (loaded.get("jobs") or {}).values():
		for step in job.get("steps") or []:
			found.update(
				name for name in (step.get("env") or {}) if name.startswith(_REFUSES_A_SKIP)
			)

	return found


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS.glob("*.yml")), ids=lambda one: one.name)
def test_a_workflow_that_runs_the_suite_refuses_every_skip (workflow: pathlib.Path) -> None:
	"""`#927`'s H-17 — every release published on a suite that never ran the browser app.

	``release.yml`` installed neither Node nor a browser and set only the PostgreSQL variable,
	so 198 of ``test_web.py``'s tests and all 38 of ``test_browser.py``'s skipped in silence
	and the job reported success. **Nothing read that file for what it runs**, so the gap was
	invisible from inside the repository — and it is the shape ``SUBROUTINE_TEST_REQUIRE_*``
	exists to prevent, met one level up by the workflow that publishes.

	Parametrised over the workflows rather than looping inside one test, so a second one
	falling behind fails on its own name instead of hiding behind the first.
	"""

	if not any(
		"pytest" in (step.get("run") or "")
		for job in (yaml.safe_load(workflow.read_text(encoding="utf-8")).get("jobs") or {}).values()
		for step in job.get("steps") or []
	):
		pytest.skip(f"{workflow.name} does not run the suite")

	missing = _refusable() - _refused_by(workflow)

	assert not missing, (
		f"{workflow.name} runs the suite and never sets {sorted(missing)}, so a runner "
		f"missing that resource would skip those tests and report success"
	)


def test_the_suite_offers_the_skips_this_file_thinks_it_does () -> None:
	"""And the floor: that the scan above found anything at all.

	Every assertion in the parametrised test is a subtraction, and an empty left-hand side
	makes each one vacuously true — the "reads nothing and passes" shape this repository has
	now met three times. Named individually as well as counted, because a scan finding two of
	three is what H-17 actually was.
	"""

	found = _refusable()

	assert {
		"SUBROUTINE_TEST_REQUIRE_POSTGRES",
		"SUBROUTINE_TEST_REQUIRE_NODE",
		"SUBROUTINE_TEST_REQUIRE_BROWSER",
	} <= found, f"only {sorted(found)} were found under {TESTS}"


def test_the_comparison_notices_a_command_that_has_drifted () -> None:
	"""Item ``#405``: the falsification that was done by hand, left as a test.

	When this file was written the command check compared by *containment*, and it passed with
	``mypy src`` standing in for ``mypy src tests scripts`` — a substring of the workflow's
	command, and the exact failure its own docstring claimed to catch. That was found by
	editing the script, running the suite and putting it back; the finding lived in a commit
	message afterwards, where nothing can reach it.

	A hand-falsification proves the guard fired **once**. This proves it fires.
	"""

	step = _steps()[("Lint and types", "Mypy")]
	commands = [line.strip() for line in step.splitlines()]

	assert "mypy src tests scripts" in commands, "the workflow still runs the full check"
	assert "mypy src" not in commands, (
		"a prefix of the workflow's command must not count as the workflow's command"
	)


def test_the_workflow_is_actually_read () -> None:
	"""And the half every check here rests on: that ``ci.yml`` was parsed at all.

	Each test above compares two sets, and an empty one on the workflow side makes three of
	them vacuously true — the "reads nothing and passes" shape this repository has met twice.
	The floor is deliberately well under what is there; the point is that it is not zero.
	"""

	steps = _steps()

	assert len(steps) > 8, f"only {len(steps)} run-steps parsed out of {WORKFLOW}"
	assert ("Lint and types", "Ruff") in steps, "and the one every commit depends on is there"
