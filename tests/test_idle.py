"""A served instance does work only when somebody asks it to - `SR#1687`.

**Measured before it was written down.** Every module a served instance loads was scanned for
the ways a Python process starts work of its own - a sleeping loop, a task, a thread, a timer,
a scheduler - and one was found: the release check, which a signed-in request starts, at most
once a day, where an operator asked for it. So an instance nobody is using uses no CPU.

**It was true by accident.** Nothing asserted it, and the change that ends it looks reasonable:
a sweep of stale sessions, a nightly prune, a warmed cache, a poll for a newer release. On one
instance each costs nothing anybody would measure, which is why it would pass review, and the
cost multiplies by every instance on a machine, arriving as *the server feels slow* rather than
as anything pointing at a job. So a new one fails here until :data:`EXCUSED` names it with the
reason it is not idle work, and ``CONTRIBUTING.md`` says the same to a contributor.
"""

import ast
import pathlib
import subprocess
import sys

import subroutine

#: Where :data:`EXCUSED` names a module from: the directory holding the package.
PACKAGE_ROOT = pathlib.Path(subroutine.__file__).resolve().parent.parent

#: The calls that start work of their own, by the dotted name they are called by.
STARTING = {
	"asyncio.create_task": "a task",
	"asyncio.ensure_future": "a task",
	"asyncio.sleep": "a coroutine that waits",
	"threading.Thread": "a thread",
	"threading.Timer": "a timer",
}

#: The names that schedule work whatever calls them: FastAPI's tasks after a response, and the
#: helper that repeats a function every so often.
SCHEDULING = {"BackgroundTasks", "repeat_every"}

#: The libraries whose whole purpose is work on a schedule or off a queue.
SCHEDULERS = {"apscheduler", "celery"}

#: Work started in the background that is not idle work, keyed by the module and what it starts,
#: with the reason. **Deleting an entry is what the reason expiring looks like**, and
#: :func:`test_every_excuse_still_names_something_that_is_there` fails for one left behind.
EXCUSED = {
	("subroutine/releases.py", "threading.Thread"): (
		"The release check: started by a request from somebody signed in, at most once a day, "
		"and only where an operator has set 'check' under [releases]. An instance nobody is "
		"using is never asked, so it never asks."
	),
}


def _dotted (node: ast.expr) -> str | None:
	"""Return ``a.b.c`` for a name or a chain of attributes, or ``None`` for anything else."""

	parts: list[str] = []

	while isinstance(node, ast.Attribute):
		parts.append(node.attr)
		node = node.value

	if not isinstance(node, ast.Name):
		return None

	return ".".join([node.id, *reversed(parts)])


def _starts (source: str) -> set[str]:
	"""Return what ``source`` starts of its own accord, by the name this file gives each.

	**A call, not a mention**: ``threading.Thread | None`` as an annotation starts nothing, so
	only calling one counts. The scheduling names count wherever they appear, because FastAPI
	takes ``BackgroundTasks`` as a parameter and never shows the call that runs them.
	"""

	found: set[str] = set()

	for node in ast.walk(ast.parse(source)):
		if isinstance(node, ast.Call):
			called = _dotted(node.func)

			if called in STARTING:
				found.add(called)

			if isinstance(node.func, ast.Attribute) and node.func.attr == "run_in_executor":
				found.add("run_in_executor")

		elif isinstance(node, ast.Name) and node.id in SCHEDULING:
			found.add(node.id)

		elif isinstance(node, ast.Attribute) and node.attr in SCHEDULING:
			found.add(node.attr)

		elif isinstance(node, ast.Import | ast.ImportFrom):
			modules = (
				[alias.name for alias in node.names]
				if isinstance(node, ast.Import)
				else [node.module or ""]
			)
			found.update(
				module.split(".")[0]
				for module in modules
				if module.split(".")[0] in SCHEDULERS
			)

		elif (
			isinstance(node, ast.While)
			and isinstance(node.test, ast.Constant)
			and node.test.value is True
		):
			found.add("while True")

	return found


def _served () -> list[pathlib.Path]:
	"""Return every module importing the application loads, measured in a fresh interpreter.

	**Measured rather than listed**, because a list of served packages is a second copy of the
	import graph and falls behind the day a module moves. ``subroutine serve`` also loads the
	command line that started it, whose interactive loop is a person at a terminal and never
	runs in a server; what runs when a request arrives is what the application imports.
	"""

	answered = subprocess.run(
		[
			sys.executable,
			"-c",
			"import subroutine.api.app, sys; print('\\n'.join(sorted("
			"module.__file__ for name, module in list(sys.modules.items()) "
			"if name.split('.')[0] == 'subroutine' and getattr(module, '__file__', None))))",
		],
		capture_output=True,
		text=True,
		check=True,
	)

	return [pathlib.Path(line) for line in answered.stdout.splitlines() if line]


def _offenders (
	paths: list[pathlib.Path], *, root: pathlib.Path = PACKAGE_ROOT
) -> set[tuple[str, str]]:
	"""Return what these modules start that :data:`EXCUSED` does not name, as module and what.

	**Takes the files and where to name them from**, so a module planted with a timer reaches
	the same code the real scan does (`SR#405`): a scanner that has only ever read a clean tree
	has not shown it can see anything.
	"""

	found: set[tuple[str, str]] = set()

	for path in paths:
		named = path.resolve().relative_to(root.resolve()).as_posix()

		found.update(
			(named, started)
			for started in _starts(path.read_text(encoding="utf-8"))
			if (named, started) not in EXCUSED
		)

	return found


def test_an_idle_instance_starts_no_work_of_its_own () -> None:
	"""The property, over every module a served instance loads."""

	served = _served()
	named = {path.resolve().relative_to(PACKAGE_ROOT).as_posix() for path in served}

	# **A floor, because a scan that read nothing would pass.** The application's own module
	# must be among what was read, and a served instance loads well over a hundred of ours.
	assert "subroutine/api/app.py" in named, sorted(named)[:10]
	assert len(served) > 100, len(served)

	offenders = _offenders(served)

	assert not offenders, (
		f"Work of its own in a served module: {sorted(offenders)}. An instance nobody is using "
		"must use no CPU. If this is genuinely wanted, name it in EXCUSED with the reason it "
		"is not idle work."
	)


def test_every_excuse_still_names_something_that_is_there () -> None:
	"""An excuse whose code has gone must go too, or it reads as a decision about nothing."""

	served = {path.resolve().relative_to(PACKAGE_ROOT).as_posix(): path for path in _served()}

	for (module, started), reason in EXCUSED.items():
		assert module in served, f"{module} is excused and no longer served: {reason}"
		assert started in _starts(served[module].read_text(encoding="utf-8")), (
			f"{module} is excused for starting {started} and no longer does: {reason}"
		)


def test_the_scan_sees_each_way_of_starting_work (tmp_path: pathlib.Path) -> None:
	"""Planted through the scanner's own entry point, one module for each way it knows."""

	planted = {
		"asyncio.create_task": "import asyncio\nasyncio.create_task(sweep())\n",
		"asyncio.ensure_future": "import asyncio\nasyncio.ensure_future(sweep())\n",
		"asyncio.sleep": "import asyncio\nasync def f ():\n\tawait asyncio.sleep(60)\n",
		"threading.Thread": "import threading\nthreading.Thread(target=sweep).start()\n",
		"threading.Timer": "import threading\nthreading.Timer(60, sweep).start()\n",
		"run_in_executor": "loop.run_in_executor(None, sweep)\n",
		"BackgroundTasks": "def route (tasks: fastapi.BackgroundTasks) -> None:\n\tpass\n",
		"repeat_every": "@repeat_every(seconds=60)\ndef sweep () -> None:\n\tpass\n",
		"apscheduler": "import apscheduler.schedulers.background\n",
		"celery": "import celery\n",
		"while True": "while True:\n\tsweep()\n",
	}
	(tmp_path / "subroutine").mkdir()

	for number, (started, source) in enumerate(planted.items()):
		module = tmp_path / "subroutine" / f"planted_{number}.py"
		module.write_text(source, encoding="utf-8")

		assert _offenders([module], root=tmp_path) == {(f"subroutine/planted_{number}.py", started)}

	# **An excuse covers only what it names**: the release check's thread passes, and a timer
	# planted beside it in the same module does not.
	excused = tmp_path / "subroutine" / "releases.py"
	excused.write_text(planted["threading.Thread"] + planted["threading.Timer"], encoding="utf-8")

	assert _offenders([excused], root=tmp_path) == {("subroutine/releases.py", "threading.Timer")}

	# **And an annotation starts nothing**, which is what keeps the release check's own
	# ``threading.Thread | None`` from counting as a second thread.
	assert _starts("asking: threading.Thread | None = None\n") == set()
