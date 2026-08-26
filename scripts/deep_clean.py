"""Remove every trace of an ordinary Subroutine install, so first contact can be driven again.

**A development tool, not a product command** (`#1342`). Testing what a stranger meets in their
first hour needs a machine that has never met this program, and there are only so many fresh
machines. This puts one back to that state.

It is written for the install the README describes: ``uv tool install subroutine``, SQLite, one
account, no service. **A systemd deployment is out of scope** and is said so rather than half
handled — an operator's instance has a unit file, a service account, a database owned by a role
this script cannot reach and probably backups somebody wants. Removing half of that would be
worse than removing none of it.

Every path comes from :mod:`subroutine.config`, so a directory that moves in the product moves
here too. Nothing is spelled out twice.

**What it will not do is the important half.** Anything it cannot prove belongs to Subroutine is
listed with the command to remove it by hand rather than taken: a ``~/.local/bin/subroutine``
that is a symlink into a virtualenv somebody manages, a backup directory pointed at a shared
volume, a database that is not the default SQLite file. Each of those is somebody's, and a tool
that guesses about them once is a tool nobody runs again.

    python scripts/deep_clean.py            # ask first, then remove what it can prove
    python scripts/deep_clean.py --dry-run  # say what it would do and touch nothing
    python scripts/deep_clean.py --yes      # for a script; skips the question, not the report

There is no undo. The database goes with everything else.
"""

import argparse
import dataclasses
import importlib.metadata
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import typing

import subroutine.config
import subroutine.directory

#: The plugins this repository publishes, and the marketplace they come from. Read from the
#: manifests rather than named here, so a third plugin is covered on the day it ships — the
#: same rule ``tests/test_plugin.py`` and ``scripts/release.py`` both follow.
MARKETPLACE_MANIFEST = ".claude-plugin/marketplace.json"

#: Where Claude Code keeps what it has installed, under whichever home this is asked about.
#: Removed through the ``claude`` CLI where that is available, because these files are its
#: business and hand-editing them is how a plugin ends up half-registered.
CLAUDE_PLUGINS = pathlib.PurePath(".claude") / "plugins"


@dataclasses.dataclass
class Step:
	"""One thing the clean did, or refused to do, with enough to act on either way."""

	kind: str
	subject: str
	outcome: str
	detail: str = ""

	#: A command that can be pasted and will work, or nothing at all. **Never prose** (`#1342`):
	#: a line labelled *by hand* that turns out to be advice, or that breaks on a path with a
	#: space in it, is the defect `#1322` is about — following it confirms the false statement.
	#: Anything explanatory belongs in :attr:`detail`, which is labelled *reason*.
	by_hand: str = ""


def _application_root (variable: str, *fallback: str) -> pathlib.Path:
	"""Return one XDG root for this application, ignoring any profile.

	**The unprofiled directory, which is the one that holds the profiles**, so removing these
	three takes every disposable instance with them. Asking for a *profiled* path would return
	one instance and leave its siblings behind — and a machine with a leftover profile is not
	the machine a stranger has.
	"""

	base = os.environ.get(variable) or pathlib.Path.home().joinpath(*fallback)

	return pathlib.Path(base) / subroutine.config.APPLICATION_NAME


def _roots () -> list[tuple[str, pathlib.Path]]:
	"""Return the three directories an install owns entirely, in a safe order.

	Configuration first and data last, which is :func:`subroutine.config.profile_directories`'
	own ordering and for its reason: an interrupted removal should leave something recognisable
	rather than a database nothing points at.
	"""

	return [
		("config", _application_root("XDG_CONFIG_HOME", ".config")),
		("state", _application_root("XDG_STATE_HOME", ".local", "state")),
		("data", _application_root("XDG_DATA_HOME", ".local", "share")),
	]


def _settings_before_removal () -> typing.Any:
	"""Return the install's settings, or ``None`` if there is nothing to read.

	**Read before anything is deleted**, because two of the questions this script has to ask —
	where the backups go, and whether the database is really the default SQLite file — are
	answered by a file it is about to remove. Asking afterwards would silently get the defaults
	and report that everything was ordinary.
	"""

	if not subroutine.config.config_file_path().exists():
		return None

	try:
		return subroutine.config.load_settings()
	# **Broad on purpose**: a configuration file too broken to load is exactly the machine
	# somebody wants to clean, so failing to read it must not stop the removal.
	except Exception as reason:
		print(f"  note     config          could not be read ({reason}); assuming defaults")

		return None


def _remove (path: pathlib.Path, *, kind: str, dry_run: bool) -> Step:
	"""Remove one file or directory, saying what happened either way."""

	if not path.exists() and not path.is_symlink():
		return Step(kind, str(path), "absent")

	if dry_run:
		return Step(kind, str(path), "would remove")

	try:
		if path.is_dir() and not path.is_symlink():
			shutil.rmtree(path)
		else:
			path.unlink()
	except OSError as reason:
		return Step(
			kind, str(path), "FAILED", detail=str(reason), by_hand=f"rm -rf {shlex.quote(str(path))}"
		)

	return Step(kind, str(path), "removed")


def places (home: pathlib.Path) -> list[pathlib.Path]:
	"""Return the directories an installed program may be sitting in, for this home.

	**A function rather than a literal, so the contract can be asserted** (`#1345`). The rule is
	that everything this touches is under the home it was given — which is what lets a test
	point a destructive run at a scratch directory and know nothing can escape. A system
	directory is outside that, so it is consulted only when there is no isolation to break.

	**Written this way because the obvious test could not fail here.** Asserting on the *report*
	needs the program to actually be in ``/usr/local/bin``, and this machine has none — which is
	exactly how the defect reached CI, where ``pip install -e .`` into the system Python puts
	one there. Returning the list makes the claim checkable on any machine, with no fixture that
	has to contain the thing being guarded against.
	"""

	inside = [home / ".local" / "bin"]

	if home != pathlib.Path.home():
		return inside

	return [*inside, pathlib.Path("/usr/local/bin")]


def installed_names () -> list[str]:
	"""Return every command name this program installs itself under.

	**There are two, and this script knew about one** (`#1348`). ``pyproject.toml`` declares
	``subroutine`` and ``subr`` — one entry point under two names (`#752`) — so an ordinary
	``uv tool install`` puts two executables on the machine. Removing one of them and then the
	tree both point into leaves a dangling link, and the next install refuses rather than
	overwriting it. Simon met that on the first command of a first-contact run.

	**Read from the installed distribution rather than listed here**, because that is what
	``pyproject.toml`` was compiled into and so cannot disagree with it. ``_published_plugins``
	states the same rule for plugins: discovered, so a name added later is covered on the day it
	ships rather than when somebody remembers this file.

	Falling back to the application name is the honest answer when the package is reachable but
	not installed — a checkout on ``PYTHONPATH``. It under-reports rather than guessing, and the
	scan in :func:`_candidates` is what covers the gap.
	"""

	name = subroutine.config.APPLICATION_NAME

	try:
		found = importlib.metadata.distribution(name).entry_points
	except importlib.metadata.PackageNotFoundError:
		return [name]

	scripts = sorted({one.name for one in found if one.group == "console_scripts"})

	return scripts or [name]


def _candidates (
	directory: pathlib.Path, *, names: list[str], tools: pathlib.Path
) -> list[pathlib.Path]:
	"""Return everything in one directory that might be this program, in a stable order.

	The declared names, and then **anything at all pointing into the uv tool tree** — which is
	ours by construction, so a name this interpreter has never heard of is still ours to take.
	That second half is what survives the two installs being different versions: the metadata
	read above describes the package this script was run from, not the one on the machine.

	The scan is deliberately limited to symlinks into that one tree. A real file cannot be
	claimed this way, which is why the declared names still carry most of the weight.
	"""

	found = [directory / one for one in names]

	if not directory.is_dir():
		return found

	for entry in sorted(directory.iterdir()):
		if entry in found or not entry.is_symlink():
			continue

		try:
			pointed = entry.resolve()
		except OSError:
			continue

		if tools in pointed.parents:
			found.append(entry)

	return found


def _executable (home: pathlib.Path, *, dry_run: bool) -> list[Step]:
	"""Remove the installed program, and refuse anything that is not one.

	**Under every name it installs itself as**, which :func:`installed_names` answers and which
	is two rather than one — the defect in `#1348`.

	Three things can be sitting at each of those names and only one of them is ours to take. A **uv tool**
	install puts a shim there and owns the tree behind it. A **symlink into a virtualenv** is a
	developer's checkout wearing the real name, which is an ordinary and deliberate setup — and
	removing it breaks a working tree rather than an install. Anything else is a stranger.

	**The tell is where it points**, not that it exists, which is why this reads the link rather
	than trusting the name.

	**A system directory is consulted only when this is the real machine** (`#1345`). Everything
	else here lives under ``home``, which is what lets a test point the whole run at a scratch
	directory and know nothing can escape; ``/usr/local/bin`` sits outside that contract and can
	only be honoured when there is no isolation to break. CI found this and no local run could
	have: it installs with ``pip install -e .`` into the system Python, so ``subroutine`` really
	is in ``/usr/local/bin`` there — the test asked for a clean machine, was told about a
	program it could not have removed, and failed on all four interpreters.
	"""

	name = subroutine.config.APPLICATION_NAME
	steps: list[Step] = []
	tools = home / ".local" / "share" / "uv" / "tools" / name

	for directory in places(home):
		for binary in _candidates(directory, names=installed_names(), tools=tools):
			if not binary.exists() and not binary.is_symlink():
				continue

			target = binary.resolve() if binary.is_symlink() else binary

			if tools in target.parents or target == binary:
				steps.append(_remove(binary, kind="executable", dry_run=dry_run))

				continue

			steps.append(Step(
				"executable",
				str(binary),
				"SKIPPED",
				detail=f"points at {target}, which this tool did not install",
				by_hand=f"rm {shlex.quote(str(binary))}",
			))

	steps.append(_remove(tools, kind="uv tool", dry_run=dry_run))

	return steps


def _published_plugins () -> list[str]:
	"""Return the plugin names this repository publishes, read from its marketplace manifest.

	Discovered rather than listed, so a plugin added later is uninstalled without anybody
	remembering this file — which is the rule ``tests/test_plugin.py`` already holds the
	repository to.
	"""

	manifest = pathlib.Path(__file__).resolve().parent.parent / MARKETPLACE_MANIFEST

	if not manifest.is_file():
		return []

	loaded = json.loads(manifest.read_text(encoding="utf-8"))
	market = loaded.get("name", subroutine.config.APPLICATION_NAME)

	return [f"{one['name']}@{market}" for one in loaded.get("plugins", []) if one.get("name")]


def _left_behind (home: pathlib.Path, market: str) -> bool:
	"""Say whether anything of ours is under Claude Code's directory (`#1347`).

	**Asked before declaring the plugin somebody else's problem.** The four places it could be:
	the version-keyed cache, the cloned marketplace, and the two registry files that name what
	is installed and where it came from. A machine with none of them has never had this plugin,
	whatever is or is not on the ``PATH``.

	Read rather than parsed — a substring is enough to answer *is there any trace*, and parsing
	Claude Code's private files to answer a yes/no would be a second thing to keep in step with
	a format that is not ours.
	"""

	plugins = home / CLAUDE_PLUGINS

	if (plugins / "cache" / market).exists() or (plugins / "marketplaces" / market).exists():
		return True

	for name in ("installed_plugins.json", "known_marketplaces.json"):
		try:
			if market in (plugins / name).read_text(encoding="utf-8"):
				return True
		except (OSError, UnicodeDecodeError):
			continue

	return False


def _claude (home: pathlib.Path, *, dry_run: bool) -> list[Step]:
	"""Uninstall the plugins and forget the marketplace, through Claude Code's own CLI.

	**Its files, its commands.** ``installed_plugins.json``, ``known_marketplaces.json`` and the
	version-keyed cache under ``plugins/cache`` are Claude Code's bookkeeping, and editing them
	by hand is how a plugin ends up listed and absent — a state that reports success and starts
	no server, which is `#236` exactly. So this drives ``claude`` where it can, and where it
	cannot it says what is left rather than reaching in.
	"""

	steps: list[Step] = []
	claude = shutil.which("claude")
	market = subroutine.config.APPLICATION_NAME

	# **``HOME`` is passed to the child, not inherited** (`SR#1342`). ``claude`` finds its
	# registry under the home of whatever started it, so a run pointed at a scratch directory
	# would still uninstall the *real* plugins — which is not a hypothetical: writing this
	# file's tests did exactly that, and the machine had to be put back by hand. Injecting the
	# path into the Python half and leaving the subprocess inheriting is isolation that covers
	# the part you can see.

	if claude is None:
		# **Nothing installed is not something left to do** (`#1347`). No ``claude`` on the
		# machine and no trace of ours under its directory means the plugin was never here —
		# which is the outcome asked for, so it is reported as one. Saying *skipped* instead
		# made a machine that had never seen Claude Code report work outstanding for ever, and
		# CI is exactly that machine: its test jobs install no editor, so all four failed on a
		# clean run. Third time an expected absence has been dressed as an unfinished job here.
		if not _left_behind(home, market):
			steps.append(Step("claude", "plugins and marketplace", "absent"))

			return steps

		# **But a trace with no `claude` to remove it *is* somebody's job.** Its registry files
		# are its own bookkeeping and hand-editing them is how a plugin ends up listed and
		# absent, which reports success and starts no server (`#236`).
		steps.append(Step(
			"claude",
			"plugins and marketplace",
			"SKIPPED",
			detail="something of ours is under ~/.claude and there is no 'claude' to remove it",
			by_hand=" && ".join([
				*(f"claude plugin uninstall {one}" for one in _published_plugins()),
				f"claude plugin marketplace remove {market}",
			]),
		))

		return steps

	for plugin in _published_plugins():
		if dry_run:
			steps.append(Step("claude plugin", plugin, "would uninstall"))

			continue

		done = subprocess.run(
			[claude, "plugin", "uninstall", plugin],
			capture_output=True,
			text=True,
			check=False,
			env={**os.environ, "HOME": str(home)},
		)
		# **Not installed is a success here**, because the outcome asked for is that it is gone.
		# **Not installed is the outcome asked for, so it is reported as one.** Passing the
		# CLI's own words through made an already-clean machine read as two failures — a red
		# cross and a truncated sentence against a line whose outcome column says ``absent``.
		# A report of a destructive operation is read for what went wrong, and putting the
		# ordinary case in that register is how somebody stops reading it.
		steps.append(Step(
			"claude plugin", plugin, "uninstalled" if done.returncode == 0 else "absent"
		))

	if dry_run:
		steps.append(Step("claude marketplace", market, "would remove"))
	else:
		done = subprocess.run(
			[claude, "plugin", "marketplace", "remove", market],
			capture_output=True,
			text=True,
			check=False,
			env={**os.environ, "HOME": str(home)},
		)
		steps.append(Step(
			"claude marketplace", market, "removed" if done.returncode == 0 else "absent"
		))

	# **Then check, because uninstalling is not the same act as the cache being gone** — the
	# copy is keyed by version and several can be behind one install (`#236`'s shape again).
	plugins = home / CLAUDE_PLUGINS

	for left in (plugins / "cache" / market, plugins / "marketplaces" / market):
		if left.exists():
			steps.append(_remove(left, kind="claude leftover", dry_run=dry_run))

	return steps


def _things_nobody_else_may_decide (
	settings: typing.Any, connections: typing.Sequence[tuple[str, str]]
) -> list[Step]:
	"""Report what this will not touch, and say how to deal with each.

	**A list rather than a decision.** Every entry here is something that is plausibly not ours:
	a backup directory pointed at a shared volume, a database that is not the default file, a
	profile selected by an environment variable this process cannot unset for the shell that
	started it. Taking any of them on a guess is the failure mode that makes a destructive tool
	untrustworthy, and being told is enough — the whole list is two or three commands.
	"""

	steps: list[Step] = []
	default = subroutine.config.default_database_path()

	if settings is not None:
		url = (getattr(settings, "database_url", "") or "").strip()

		if url and not url.startswith("sqlite"):
			steps.append(Step(
				"database",
				url.split("@")[-1],
				"SKIPPED",
				detail=(
					"not SQLite, so the data is in a server this tool does not administer — "
					"drop it yourself if it was only for this install"
				),
			))
		elif url and pathlib.Path(url.split("///")[-1]) != default:
			steps.append(Step(
				"database",
				url,
				"SKIPPED",
				detail="a SQLite file somewhere this tool did not put one",
				by_hand=f"rm {shlex.quote(url.split('///')[-1])}",
			))

		configured = (getattr(settings, "backup_directory", "") or "").strip()

		if configured:
			where = pathlib.Path(configured).expanduser()

			if not str(where).startswith(str(_application_root("XDG_DATA_HOME", ".local", "share"))):
				steps.append(Step(
					"backups",
					str(where),
					"SKIPPED",
					detail="outside the data directory, so it may be shared or a mount",
					by_hand=f"rm -rf {shlex.quote(str(where))}",
				))

	# **A connection that names a server is data this cannot reach** (§13.7). A basic install
	# has none, which is the case this script is written for; a machine that has grown one is
	# telling you that removing these directories does not remove the work.
	#
	# **A *local* connection is excluded and that is the whole subtlety.** It points at the
	# SQLite file above, which this does remove — reporting it as *elsewhere* would be exactly
	# backwards, and would say the one thing being destroyed is safe.
	steps.extend(
		Step(
			"connection",
			name,
			"note",
			detail=f"names {where}, which is not this machine's install",
		)
		for name, where in connections
	)

	if os.environ.get("SUBROUTINE_PROFILE"):
		steps.append(Step(
			"environment",
			"SUBROUTINE_PROFILE",
			"SKIPPED",
			detail=(
				"set in the shell that started this, which no child process can unset; take "
				"it out of your shell profile too"
			),
			by_hand="unset SUBROUTINE_PROFILE",
		))

	# **``SUBROUTINE_TEST_*`` configures the harness, not the product**, which is the same line
	# ``tests/conftest.py`` draws and for the same reason. Reporting them told somebody running
	# the suite that their machine carried overrides it did not — and it was the *gate* that
	# found this, because those variables are only set there.
	for name in sorted(
		one
		for one in os.environ
		if one.startswith("SUBROUTINE_") and not one.startswith("SUBROUTINE_TEST_")
	):
		if name == "SUBROUTINE_PROFILE":
			continue

		steps.append(Step(
			"environment",
			name,
			"SKIPPED",
			detail=(
				"an override this install may have been relying on; take it out of your "
				"shell profile too"
			),
			by_hand=f"unset {name}",
		))

	return steps


#: Directories a marker is never in and which are expensive to walk. Pruned rather than
#: filtered afterwards, so the cost is not paid at all.
NOT_WORTH_WALKING = frozenset({
	".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox",
	".cache", ".mypy_cache", ".pytest_cache", ".npm", ".mozilla", ".local",
})


def _markers (home: pathlib.Path) -> list[Step]:
	"""Find the ``.subroutine`` files in somebody's checkouts, and report what is there.

	**Searched rather than suggested** (`SR#1342`). This used to hand over a ``find`` command,
	which meant the one question the whole script exists to answer — *is there any trace left* —
	was the operator's to run. Driven on a real machine it found four, in repositories the
	person had forgotten about.

	**Reported and never removed.** A marker names a project by id and belongs to the repository
	it sits in, very often committed; deleting somebody's tracked file because it mentions this
	program is exactly the guess a destructive tool may not make.

	**And it is not merely a trace.** A marker decides which project a bare ``subroutine add``
	files into, so a checkout carrying one is not a machine that has never met this program —
	which is the whole state this script exists to produce. A first-contact run made inside such
	a directory would answer differently and nobody would know why.

	**And the search is reported even when it finds nothing**, because *no line* and *no
	markers* look identical in the output — the difference between a check that ran and one
	that was skipped, which is the thing a report of an irreversible operation must not blur.
	"""

	found: list[pathlib.Path] = []

	for root, directories, files in os.walk(home, followlinks=False):
		directories[:] = [one for one in directories if one not in NOT_WORTH_WALKING]

		if subroutine.directory.FILE_NAME in files:
			found.append(pathlib.Path(root) / subroutine.directory.FILE_NAME)

	if not found:
		return [Step("markers", f"searched {home}", "note", detail="none found")]

	return [
		Step(
			"markers",
			str(one),
			"SKIPPED",
			detail=(
				"a checkout carrying one does not behave like a fresh one, and it is very "
				"often committed"
			),
			by_hand=f"rm {shlex.quote(str(one))}",
		)
		for one in sorted(found)
	]


def _elsewhere () -> list[tuple[str, str]]:
	"""Return each configured connection that points somewhere this script cannot reach.

	Read from the configuration file rather than from a built roster, because a **disabled**
	connection still names data and is exactly what somebody mid-migration has. A connection
	with no URL, or a SQLite one, is this machine's own and is removed with the rest.
	"""

	try:
		declared = subroutine.config.read_config_file().get("connections") or {}
	except Exception:
		return []

	if not isinstance(declared, dict):
		return []

	found = []

	for name, table in sorted(declared.items()):
		url = (table or {}).get("url") if isinstance(table, dict) else None

		if isinstance(url, str) and url.strip() and not url.strip().startswith("sqlite"):
			found.append((name, url.strip().split("@")[-1]))

	return found


def _report (steps: typing.Sequence[Step]) -> int:
	"""Print every step and return how many need the operator."""

	width = max((len(one.kind) for one in steps), default=8)
	# **Measured, not guessed.** A hardcoded column is one word away from running the outcome
	# into the subject, which is how a report of a destructive operation becomes unreadable.
	said = max((len(one.outcome) for one in steps), default=8)

	for step in steps:
		print(f"  {step.outcome:<{said}}  {step.kind:<{width}}  {step.subject}")

		if step.detail:
			print(f"  {'':<{said}}  {'':<{width}}  reason: {step.detail}")

		if step.by_hand:
			print(f"  {'':<{said}}  {'':<{width}}  by hand: {step.by_hand}")

	# **A note is not an outstanding job.** It says the machine is bigger than this script's
	# scope; nothing is left undone by it, and counting it would send somebody looking for a
	# command that does not exist.
	return sum(1 for one in steps if one.outcome in {"SKIPPED", "FAILED"})


def _confirmed (roots: typing.Sequence[tuple[str, pathlib.Path]]) -> bool:
	"""Say what is about to go, and require it to be typed back.

	**Named before asked**, because *are you sure* is a question nobody can answer without being
	told what about — and the thing most likely to be irreplaceable here is a database somebody
	has been keeping real work in.
	"""

	database = subroutine.config.default_database_path()

	print("This removes a Subroutine install and everything in it. There is no undo.\n")

	for kind, path in roots:
		print(f"  {kind:<8} {path}")

	if database.exists():
		size = database.stat().st_size
		print(f"\n  The database is {size:,} bytes and it is not backed up by this.")

	print("\nType 'remove' to go ahead: ", end="")

	return input().strip() == "remove"


def main (
	argv: typing.Sequence[str] | None = None, *, home: pathlib.Path | None = None
) -> int:
	"""Run the clean, and return non-zero if anything is left for the operator.

	**``home`` is a parameter because a test of a destructive tool must not be able to escape**
	(`#405`: give the scanner the tree). ``tests/conftest.py`` gives every test its own XDG
	roots and deliberately leaves ``HOME`` alone, so a test that only patched the environment
	would still find — and delete — the developer's real Claude plugin cache. Passing the
	directory in means the isolation is in the signature rather than in somebody remembering.
	"""

	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument(
		"--dry-run", action="store_true", help="say what would happen and touch nothing"
	)
	parser.add_argument(
		"--yes", action="store_true", help="do not ask; the report is printed either way"
	)
	options = parser.parse_args(argv)

	home = home or pathlib.Path.home()
	roots = _roots()

	if not options.dry_run and not options.yes and not _confirmed(roots):
		print("\nNothing was removed.")

		return 1

	print()

	settings = _settings_before_removal()
	# **Both read before anything is deleted, for the same reason.** `_settings_before_removal`
	# says why; this is the same file, and asking after the removal returned an empty mapping
	# and reported no connections at all — a machine mid-migration told that everything it had
	# was local. Written as one line beside the other so the pairing is visible.
	connections = _elsewhere()
	steps: list[Step] = []

	# **Named on its own line before the directory that contains it.** Removing the data root
	# takes the database with it, and a report that only says *data* leaves the one
	# irreplaceable thing in this whole operation unmentioned.
	database = subroutine.config.default_database_path()

	if database.exists():
		steps.append(Step(
			"database",
			str(database),
			"would remove" if options.dry_run else "removed",
			detail=f"{database.stat().st_size:,} bytes, with the data directory below",
		))

	for kind, path in roots:
		steps.append(_remove(path, kind=kind, dry_run=options.dry_run))

	steps.extend(_executable(home, dry_run=options.dry_run))
	steps.extend(_claude(home, dry_run=options.dry_run))
	steps.extend(_things_nobody_else_may_decide(settings, connections))
	steps.extend(_markers(home))

	outstanding = _report(steps)
	removed = sum(
		1 for one in steps if one.outcome in {"removed", "uninstalled", "would remove"}
	)

	# **A dry run has removed nothing and may not say it has.** The count is the same number
	# either way and the verb is not, which is the whole difference between a rehearsal and the
	# thing itself.
	did = "would remove" if options.dry_run else "removed"

	print(
		f"\n{removed} {did}, {outstanding} left for you"
		if outstanding
		else f"\n{removed} {did}, nothing left for you."
	)

	if outstanding:
		print("Run the commands above, then re-run this to check.")

	return 0 if not outstanding else 2


if __name__ == "__main__":
	sys.exit(main())
