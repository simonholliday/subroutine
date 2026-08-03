"""``scripts/check.py`` and ``.github/workflows/ci.yml`` name the same steps — item ``#402``.

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
import sys
import types
import typing

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
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
	"""``SUBROUTINE_TEST_REQUIRE_POSTGRES`` is the one environment variable that matters here.

	Without it an unreachable PostgreSQL is a *skip*, so a local run reports success over half
	a suite — and the backend-portability defects this project cares most about are precisely
	the ones invisible on SQLite. Read from the workflow rather than compared against a
	literal, so the two cannot disagree about the name.
	"""

	suite = next(entry for entry in script.CHECKS if entry.step.startswith("Tests on"))
	workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	declared: dict[str, typing.Any] = {}

	for job in workflow["jobs"].values():
		for step in job["steps"]:
			if step.get("name") == suite.step:
				declared = step["env"]

	assert "SUBROUTINE_TEST_REQUIRE_POSTGRES" in declared
	assert dict(suite.env)["SUBROUTINE_TEST_REQUIRE_POSTGRES"] == str(
		declared["SUBROUTINE_TEST_REQUIRE_POSTGRES"]
	)
