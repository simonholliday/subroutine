"""Run exactly the checks CI runs, here, in one command — item ``#402``.

**Written because I did not.** On 2026-08-03 two lint failures reached the history while three
commit messages and ``CLAUDE.md`` all said ruff was clean (`#401`). Neither failure was
interesting; what let them through was that "the checks" existed in two places — a workflow
nobody runs locally, and whatever I typed from memory — and only one of those was ever the
thing being reported. **Pushing is deliberately the operator's**, so CI had not run on
twenty-nine commits, and every green claim in that stretch was a claim about a command I chose.

Run it before a commit:

    python scripts/check.py                 # everything, in the order CI runs it
    python scripts/check.py --list          # what it would run, and what it deliberately does not

**The list is not a second copy of the workflow.** ``tests/test_check_script.py`` compares
every entry here against ``.github/workflows/ci.yml`` by job and step name, both ways, and
fails when they disagree — so a step added to CI is one this command starts refusing to be
silent about. That guard is the whole point; without it this file is a third place for the
truth to live, which is the defect it was written to answer.

**Every check reports, and the exit code is the verdict.** No early exit on the first failure:
a run that stops at ruff tells you nothing about mypy, and this project has already lost time
to reading the tail of a combined run and seeing a green suite above a red type check.
"""

import argparse
import dataclasses
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


@dataclasses.dataclass(frozen=True)
class Check:
	"""One thing to run, and the CI step it stands for."""

	#: The job's ``name:`` in the workflow, as a reader of the Actions page sees it.
	job: str

	#: The step's ``name:``. Job and step together are the identity, because a command can
	#: appear twice and a step name is what the failure is reported under.
	step: str

	#: Argument vector rather than a shell string. Nothing here needs a shell, and a list
	#: cannot be re-split by whitespace in a path.
	command: tuple[str, ...]

	#: Added to this process's environment for the run. Only the test job needs any, and it
	#: needs them to *fail* rather than skip when PostgreSQL is unreachable.
	env: tuple[tuple[str, str], ...] = ()


#: What runs, in CI's own order: cheapest feedback first, then the suite.
CHECKS: tuple[Check, ...] = (
	Check(job="Lint and types", step="Ruff", command=("ruff", "check", ".")),
	Check(job="Lint and types", step="Mypy", command=("mypy", "src", "tests", "scripts")),
	Check(
		job="Lint and types",
		step="Release notes",
		command=(sys.executable, "scripts/check_release_notes.py"),
	),
	Check(
		job="Plugin manifests",
		step="Validate",
		command=("claude", "plugin", "validate", "."),
	),
	Check(
		job="Plugin manifests",
		step="Validate",
		command=("claude", "plugin", "validate", "./plugins/subroutine"),
	),
	# One entry per plugin rather than a loop, so a failure names which manifest. That this list
	# covers every plugin that exists is held by `tests/test_plugin.py` rather than by anybody
	# remembering — a new plugin nobody validates is a manifest a stranger finds broken.
	Check(
		job="Plugin manifests",
		step="Validate",
		command=("claude", "plugin", "validate", "./plugins/subroutine-remote"),
	),
	Check(
		job="Tests (Python ${{ matrix.python-version }})",
		step="Tests on SQLite and PostgreSQL",
		command=("pytest",),
		# **The ones that must not be dropped to make a red run green.** Without the first an
		# unreachable PostgreSQL is a skip, and the run reports success on half a suite;
		# without the second a missing Node skips 198 tests the same way (`SR#927`'s H-17).
		env=(
			("SUBROUTINE_TEST_REQUIRE_POSTGRES", "1"),
			("SUBROUTINE_TEST_REQUIRE_NODE", "1"),
		),
	),
	# **Run again here, and the duplication is deliberate** (`SR#795`). In CI these tests skip
	# in the job above — the runner has no browser — and this job is the only place they run at
	# all. On a development machine they run in both, which costs half a minute and buys the
	# thing that was missing: the variable makes a broken browser a failure rather than a skip,
	# so the local gate stops being quietly narrower than CI.
	Check(
		job="Browser tests",
		step="Tests in a browser",
		command=("pytest", "tests/test_browser.py"),
		env=(
			("SUBROUTINE_TEST_REQUIRE_BROWSER", "1"),
			("SUBROUTINE_TEST_REQUIRE_NODE", "1"),
		),
	),
)

#: CI steps this command deliberately does not run, each with the answer to "what makes this
#: entry go away?" — the question every allow-list in this repository is supposed to answer
#: and only some of them do.
NOT_LOCALLY: dict[tuple[str, str], str] = {
	("Lint and types", "Install"): (
		"Installs into the runner's environment. The virtualenv here is the operator's and "
		"is never modified silently."
	),
	("Tests (Python ${{ matrix.python-version }})", "Install"): (
		"The same, once per Python version. Locally there is one interpreter and it is "
		"already installed."
	),
	("Browser tests", "Install"): (
		"The same again. This job installs only the development extra, because it needs no "
		"database of its own."
	),
	("Browser tests", "Install a browser"): (
		"Downloads ~400MB of Chromium into the runner. A development machine has one already "
		"— that is what makes these tests runnable here at all — and fetching another on "
		"every gate run would be the slowest step by an order of magnitude."
	),
	("Dependency licences", "Install runtime dependencies only"): (
		"Needs a clean environment holding runtime dependencies and nothing else."
	),
	("Dependency licences", "Check"): (
		"Would walk this virtualenv, which holds ruff, mypy and pytest — development tools "
		"are not distributed, so a copyleft linter constrains nothing and would fail this "
		"locally for a reason CI is specifically arranged to avoid. Goes away if the script "
		"learns to read the declared runtime closure rather than what is installed."
	),
	("Plugin manifests", "Install Claude Code"): (
		"A global npm install. Whoever is running this command already has it."
	),
	("First run", "Install as a user would"): (
		"A non-editable install of the working tree, which would replace the operator's "
		"editable one."
	),
	("First run", "subroutine init"): (
		"Needs a machine with nothing configured, which is what that whole job is for. The "
		"suite covers the same ground with an empty XDG home; the gap this leaves is a real "
		"install on a bare runner, and it is the reason that job exists separately."
	),
	("First run", "The database is queryable afterwards"): (
		"Reads back the database the step above created, and asserts that a second 'init' "
		"says 'Already set up'. Both are about a machine that had no instance a moment ago, "
		"which the operator's is not."
	),
	("First run", "The personal test"): (
		"§13.5b's transcript. The suite runs it too, against a temporary instance; the job "
		"runs it again with nothing configured and no fixtures, which cannot be reproduced "
		"here without building a throwaway environment."
	),
}


def main (argv: list[str] | None = None) -> int:
	"""Run every check and report each one, returning non-zero if any failed."""

	parsed = _arguments(argv)

	if parsed.list:
		_listed()

		return 0

	results = [(check, _run(check)) for check in CHECKS]

	print()

	for check, ok in results:
		print(f"  {'pass' if ok else 'FAIL'}  {check.step}: {' '.join(check.command)}")

	failed = [check for check, ok in results if not ok]

	if failed:
		print(f"\n{len(failed)} of {len(results)} failed.", file=sys.stderr)

		return 1

	print(f"\nAll {len(results)} passed. {len(NOT_LOCALLY)} CI steps were not run; --list says why.")

	return 0


def _arguments (argv: list[str] | None) -> argparse.Namespace:
	"""Read the command line."""

	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument(
		"--list",
		action="store_true",
		help="Print what would run, and what is deliberately left to CI.",
	)

	return parser.parse_args(argv)


def _listed () -> None:
	"""Print both lists, because the second one is the part worth reading."""

	print("Runs here:\n")

	for check in CHECKS:
		print(f"  {check.job} / {check.step}\n      {' '.join(check.command)}")

	print("\nLeft to CI:\n")

	for (job, step), reason in NOT_LOCALLY.items():
		print(f"  {job} / {step}\n      {reason}")


def _run (check: Check) -> bool:
	"""Run one check from the repository root, letting its output through as it happens.

	Not captured. A check's own output is the useful part when it fails, and buffering it to
	re-print at the end is how a long suite comes to look like a hang.

	**A missing tool is a failed check, not a crash.** The first run of this script died with
	a ``FileNotFoundError`` traceback on ``ruff`` and never reached mypy or the suite — a
	runner whose whole promise is "every check reports" defeated by the first one being
	absent. A command that cannot be found says so and the rest go on.
	"""

	# **Flushed, because a subprocess writes to the same file descriptor and does not wait for
	# us.** Piped anywhere — a log, `tee`, a CI capture — Python buffers its own prints while
	# the tools' output goes straight out, so every heading arrives after every result and the
	# reader cannot tell which output belonged to which check.
	print(f"\n=== {check.step}: {' '.join(check.command)}", flush=True)

	environment = {**os.environ, **dict(check.env)}

	try:
		completed = subprocess.run(
			(_resolved(check.command[0]), *check.command[1:]),
			cwd=ROOT,
			env=environment,
			check=False,
		)

	except OSError as missing:
		print(f"could not run {check.command[0]!r}: {missing}", file=sys.stderr)

		return False

	return completed.returncode == 0


def _resolved (program: str) -> str:
	"""Prefer the tool installed beside the interpreter running this script.

	``python scripts/check.py`` from a virtualenv means *that* virtualenv's ruff, mypy and
	pytest, whether or not it has been activated — which is what somebody typing it means, and
	is the difference between checking this project and checking it with whatever tools
	happened to be on ``PATH``.

	Falls back to the name as given, so a tool that lives elsewhere — ``claude``, installed
	globally by npm — is found the ordinary way.
	"""

	beside = pathlib.Path(sys.executable).parent / program

	return str(beside) if beside.exists() else program


if __name__ == "__main__":
	sys.exit(main())
