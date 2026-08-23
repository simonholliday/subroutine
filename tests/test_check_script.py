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
import tomllib
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


def _running (step: dict[str, typing.Any]) -> list[dict[str, typing.Any]]:
	"""Return the ``run:`` steps one workflow step stands for.

	Usually itself, or nothing. **But a step may be a composite action this repository
	supplies**, and then the commands CI runs are inside it (`#1022`) — so this follows a
	``uses: ./`` into its ``action.yml`` and returns what it holds.

	Without that, moving a step into an action would take it out of :func:`_steps` and out of
	the comparison below, silently. The guard would go on passing while covering less, which is
	the shape this project keeps finding: a check that shares a blind spot with the thing it
	checks. A third-party action is deliberately not followed — what it runs is not ours to
	account for, and it is pinned to a commit for that reason.
	"""

	if "run" in step:
		return [step]

	uses = str(step.get("uses", ""))

	if not uses.startswith("./"):
		return []

	found = ROOT / uses.removeprefix("./") / "action.yml"

	assert found.exists(), f"a workflow names {uses!r} and there is no action.yml there"

	loaded = yaml.safe_load(found.read_text(encoding="utf-8"))

	return [inner for inner in (loaded["runs"].get("steps") or []) if "run" in inner]


def _steps () -> dict[tuple[str, str], str]:
	"""Return every ``run:`` step in the workflow, keyed by job name and step name.

	Steps with no ``run:`` — ``actions/checkout``, ``setup-python`` — are not checks, and are
	not part of what the workflow claims this project verifies. A step with no ``name:`` would
	be unaddressable, so it is refused loudly rather than skipped: a nameless step is one this
	comparison cannot see, and quietly ignoring it is how the two lists would come apart.

	**A composite action of ours counts as the steps inside it**, which is what stops moving a
	step into one from removing it from this comparison — see :func:`_running`.
	"""

	# `safe_load`, not `load`. Nothing reads this file at runtime, and a parser that can
	# construct arbitrary objects is still not the one to reach for out of habit.
	workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	found: dict[tuple[str, str], str] = {}

	for job in workflow["jobs"].values():
		name = job["name"]

		for step in job["steps"]:
			for running in _running(step):
				assert "name" in running, f"a step in {name!r} has a 'run:' and no 'name:'"

				found[(name, running["name"])] = running["run"]

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


#: The flag that spreads a command across every core, and the one command that must not carry it.
_PARALLEL = "-n"


def test_the_browser_command_is_not_spread_across_workers () -> None:
	"""`#936` — parallelising the browser tests does not merely fail to help, it fails.

	Measured on 8 cores: `pytest tests/test_browser.py` takes 9.9s serially and **15.7s with a
	red run** under `-n auto`, because a worker apiece launches its own Chromium and the
	machine saturates. What breaks is a 10-second `expect_event("page")` in
	`test_a_modified_click_still_belongs_to_the_browser` — a load-sensitive timeout rather
	than a defect, and exactly the flake that reads in CI as a real fault in the app.

	**Being spread through four thousand other tests does not save it**, which was the first
	thing tried and is why this guard is worded about the file rather than about the command.
	The argument was that only a few browsers would be alive at once; two full parallel runs
	passed, and the third — a gate run — failed on the same test for the same reason. So the
	whole-suite command excludes the file outright and this one runs it serially.

	Worth a guard rather than a comment, because adding `-n` to the remaining serial command is
	the obvious next tidy-up and the failure it buys is intermittent, which is the kind that
	gets three people re-running CI before anybody reads it.
	"""

	for entry in script.CHECKS:
		if "tests/test_browser.py" not in entry.command:
			continue

		assert _PARALLEL not in entry.command, (
			f"{entry.step!r} runs the browser tests across workers, which flakes on a "
			f"10-second page-event timeout — see this test's reasoning"
		)

	for path in sorted(WORKFLOWS.glob("*.yml")):
		loaded = yaml.safe_load(path.read_text(encoding="utf-8"))

		for job in (loaded.get("jobs") or {}).values():
			for step in job.get("steps") or []:
				# **Split into words before asking**, because the whole-suite command names
				# this same path — inside `--ignore=`, which is the opposite of running it. A
				# substring test flagged that as a browser command the moment the exclusion
				# was added, which is this repository's own lesson about guarding a spelling
				# rather than a thing, met inside the guard written to hold a measurement.
				words = (step.get("run") or "").split()

				if "tests/test_browser.py" in words:
					assert _PARALLEL not in words, (
						f"{path.name} runs the browser tests across workers"
					)


def test_the_suite_is_actually_run_across_workers () -> None:
	"""And the other direction, so the saving cannot be quietly reverted.

	The whole point of `#936` is that a gate run costs two minutes rather than twelve. Dropping
	the flag would give back ten minutes a run and fail nothing — the suite passes either way,
	which is what makes it worth asserting rather than trusting.

	`worksteal` by name, not merely `-n`: the default scheduler measures 160s against 125s
	here, so a quarter of the saving is in that flag alone and it would be the first thing
	dropped by somebody simplifying the command.
	"""

	suite = next(entry for entry in script.CHECKS if entry.step.startswith("Tests on"))

	assert _PARALLEL in suite.command, "the whole-suite run is back to one core"
	assert "worksteal" in suite.command, (
		"the default scheduler leaves workers idle at the tail of this suite's long fixtures"
	)


def _pytest_commands (runs: typing.Iterable[str]) -> list[list[str]]:
	"""Return each ``pytest`` invocation in some shell text, as its words."""

	found = []

	for run in runs:
		for line in run.splitlines():
			words = line.split()

			if words and words[0] == "pytest":
				found.append(words)

	return found


def _covers (command: list[str]) -> set[pathlib.Path]:
	"""Return the test files one ``pytest`` invocation runs.

	Two shapes, and they are not the same question. An invocation **with** positional targets
	runs exactly those and nothing else; one **without** runs the whole suite minus whatever
	its ``--ignore=`` arguments name. Reading them as one thing is how the first version of
	this guard passed its own falsification.
	"""

	# **A path rather than "not a flag"**, because ``-n auto`` and ``--dist worksteal`` put
	# their values in separate words — so the obvious reading made ``auto`` and ``worksteal``
	# look like positional targets and reported the whole suite as uncovered. Asking whether
	# the word names something on disk cannot be fooled by a flag's value.
	targets = [
		pathlib.Path(word)
		for word in command[1:]
		if not word.startswith("-") and (ROOT / word).exists()
	]
	excluded = [
		pathlib.Path(word.split("=", 1)[1])
		for word in command
		if word.startswith("--ignore=")
	]

	if targets:
		named = {pathlib.Path(one) for one in targets}

		return {
			found
			for found in _test_files()
			if any(found == one or one in found.parents for one in named)
		}

	return {
		found
		for found in _test_files()
		if not any(found == one or one in found.parents for one in excluded)
	}


def _test_files () -> set[pathlib.Path]:
	"""Return every test module in the suite, relative to the repository root.

	Enumerated rather than listed, for `SR#405`'s reason — and it is what makes the check
	*exact* rather than a comparison of two strings that happen to mention each other.
	"""

	return {
		found.relative_to(ROOT) for found in TESTS.rglob("test_*.py")
	}


def _uncovered (commands: list[list[str]]) -> set[pathlib.Path]:
	"""Return every test file no invocation in a set of them runs.

	The reconciliation `SR#1132` is about. A split gate's coverage is the *union* of its
	commands, and the union is only the whole suite while what one of them leaves out is
	picked up by another. Nothing else here asks that question: the skip-refusing guard reads
	environment variables, and the two ``-n`` guards read flags.
	"""

	covered: set[pathlib.Path] = set()

	for command in commands:
		covered |= _covers(command)

	return _test_files() - covered


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS.glob("*.yml")), ids=lambda one: one.name)
def test_what_one_job_leaves_out_another_runs (workflow: pathlib.Path) -> None:
	"""`SR#1132`. A gate split across jobs still has to cover the whole suite.

	**Found while splitting ``release.yml`` for `SR#1111`.** That file used to run a bare
	``pytest`` — one command, whole suite, nothing to get wrong — and now runs
	``--ignore=tests/test_browser.py`` in one job and ``tests/test_browser.py`` in another,
	which is what ``ci.yml`` and ``scripts/check.py`` have always done. The arrangement is
	right and it moves the risk: coverage became a property of two commands agreeing rather
	than of one command being complete, and no guard here was asking about it.

	Widening the exclusion to ``--ignore=tests/`` would have passed every other check in this
	file, passed the suite, and published a release verified on nothing — **silent in the
	direction that matters**, because fewer tests running is a shorter green run.

	Read off the workflow rather than compared against a literal list of what may be ignored,
	for `SR#405`'s reason: a list of the legitimate exclusions is a second copy of the split,
	and the copy is the one that goes stale.

	**The first version of this passed its own falsification**, and the fix is why it now
	enumerates the suite. It asked *is each excluded path covered by some target* — so
	``--ignore=tests/`` beside ``pytest tests/test_browser.py`` looked answered, because the
	target sits inside the exclusion. The question a split gate actually has is the other way
	round: *is each test module run by somebody*. Comparing two strings that mention each other
	is not the same as counting what runs.
	"""

	loaded = yaml.safe_load(workflow.read_text(encoding="utf-8"))
	commands = _pytest_commands(
		str(step.get("run", ""))
		for job in (loaded.get("jobs") or {}).values()
		for step in job.get("steps") or []
	)

	if not commands:
		pytest.skip("this workflow does not run pytest")

	orphaned = _uncovered(commands)

	assert not orphaned, (
		f"{workflow.name} runs pytest and {len(orphaned)} test modules are run by none of its "
		f"invocations, so nothing in this workflow ever runs them: {sorted(map(str, orphaned))[:5]}"
	)


def test_the_local_gate_covers_what_it_excludes () -> None:
	"""The same question of ``scripts/check.py``, which splits the suite the same way.

	Its two entries are compared against ``ci.yml`` step for step by the test above them, and
	that comparison holds them *equal to the workflow* rather than *complete*. Both could drift
	together — which is exactly what a shared edit does.
	"""

	orphaned = _uncovered(
		[list(entry.command) for entry in script.CHECKS if entry.command[0] == "pytest"]
	)

	assert not orphaned, (
		f"scripts/check.py never runs {len(orphaned)} test modules, so the local gate is "
		f"green over tests nobody ran: {sorted(map(str, orphaned))[:5]}"
	)


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS.glob("*.yml")), ids=lambda one: one.name)
def test_a_hung_test_is_stopped_before_the_job_that_would_kill_it (
	workflow: pathlib.Path,
) -> None:
	"""`SR#1048`. Two ceilings, and the inner one is worth nothing if it is not the smaller.

	`Tests (Python 3.11)` hung on 2026-08-20 and was killed by `SR#1015`'s job ceiling while
	its four siblings did the same work in six to nine minutes. **That ceiling did exactly what
	it was built for** — 25 minutes rather than six hours — and a killed job keeps no log at
	all: ``gh run view --log`` returns nothing and the archive answers ``BlobNotFound``, so all
	that survived was a step that started and never finished. Nothing said *which test*.

	``faulthandler_timeout`` is pytest's own, so it costs no dependency. It dumps every
	thread's stack and exits the worker, which ``xdist`` reports as ``worker 'gw0' crashed
	while running <test id>`` — a named failure rather than a blank.

	**The number has to sit under every job that runs pytest, and that is the whole of this
	test.** Above them, the job dies first and there is no log to read: a control that cannot
	act, which is the defect this repository finds most often. Below the slowest real test it
	would fire on a slow runner instead, which the comment in ``pyproject.toml`` records with
	the measurement — but only one of the two ends is checkable from here, and it is this one.

	**Every workflow, since `SR#1111`, and it said "every job" while reading one file.** It was
	written against ``ci.yml`` alone, so the release gate's own ceiling — the one that killed a
	green run at 87% and published nothing — was outside what it could see. A rule stated
	unqualified and applied to one of two files is the shape this repository keeps finding, met
	inside the test that states it.
	"""

	settings = tomllib.loads(
		(ROOT / "pyproject.toml").read_text(encoding="utf-8")
	)["tool"]["pytest"]["ini_options"]

	seconds = int(settings["faulthandler_timeout"])

	assert settings.get("faulthandler_exit_on_timeout") is True, (
		"the stacks are dumped and the test carries on, so a hang still costs the whole job "
		"and `xdist` still has nothing to attribute it to"
	)

	loaded = yaml.safe_load(workflow.read_text(encoding="utf-8"))
	bounded = {}

	for name, job in (loaded.get("jobs") or {}).items():
		runs = "\n".join(str(step.get("run", "")) for step in job.get("steps") or [])

		if "pytest" in runs:
			bounded[name] = job.get("timeout-minutes")

	if not bounded:
		pytest.skip("this workflow does not run pytest")

	assert all(bounded.values()), (
		f"{sorted(one for one, limit in bounded.items() if not limit)} run pytest with no "
		f"job ceiling, so `SR#1015`'s six-hour default is back and nothing bounds them"
	)

	tightest = min(bounded.values())

	assert seconds < tightest * 60, (
		f"a test may run {seconds}s and the tightest job that runs pytest is bounded at "
		f"{tightest} minutes ({bounded}). The job would be killed first, and a killed job "
		f"keeps no log — which is the state this setting exists to end."
	)
