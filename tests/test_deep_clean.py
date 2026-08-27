"""The development tool that puts a machine back to never having met this program.

`SR#1342`. First contact can only be driven on a machine that has never seen Subroutine, and
there are not many of those. This is the script that makes one, and this file is what stops it
becoming a list of paths that used to be right.

**Two claims, and the second is the one with teeth**: it removes what an ordinary install
leaves, and it *refuses* what it cannot prove is ours. A destructive tool that guesses once is
one nobody runs again.
"""

import importlib.metadata
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tomllib
import typing

import pytest

import subroutine.config
import subroutine.context
import subroutine.directory

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

# The line above is what makes this importable: `scripts/` is not a package and this is
# the only test that reaches into it.
import deep_clean


@pytest.fixture(autouse=True)
def _one_seam (tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
	"""Put this test's XDG roots under the same home it hands the script — `SR#1349`.

	**Two seams was the defect, and these tests had it.** ``tests/conftest.py`` gives every test
	XDG roots under a session directory, and every test here passes ``home=tmp_path`` — two
	unrelated scratch trees, each isolated by something the other does not know about. That is
	exactly the arrangement `SR#1349` is about: the ``home`` parameter looked like the sandbox
	and covered the executable and marker halves, while the config, state and data halves were
	isolated by the environment instead.

	It was invisible from here for that reason. The script now refuses a named ``home`` the
	environment escapes, so **this fixture is what these tests need in order to be about the
	script rather than about the harness** — and its absence would fail them all, loudly, which
	is the guard working.

	Applied to the file rather than per test because every test in it drives ``main``, and one
	that forgot would be the same silent second seam arriving in the tests written to prevent
	it.
	"""

	for variable, parts in (
		("XDG_CONFIG_HOME", (".config",)),
		("XDG_STATE_HOME", (".local", "state")),
		("XDG_DATA_HOME", (".local", "share")),
	):
		monkeypatch.setenv(variable, str(tmp_path.joinpath(*parts)))


def _declared_names () -> list[str]:
	"""Return the command names ``pyproject.toml`` declares, read from the file itself.

	**The authority for the scenario below, deliberately not** :func:`deep_clean.installed_names`.
	A guard that asks the code under test what to check can only report that it agrees with
	itself — and against the original defect it would have failed on a missing attribute rather
	than on the executable left behind, which is a different claim.
	"""

	manifest = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"

	return sorted(tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]["scripts"])


def _installed () -> dict[str, pathlib.Path]:
	"""Build what an ordinary install leaves behind, at the paths the product itself names.

	**Every path comes from :mod:`subroutine.config`**, which is the property this file is
	really guarding: a directory that moves in the product has to move here, and a fixture that
	spelled the paths out would go on passing while the script cleaned somewhere nobody lives.
	"""

	config = subroutine.config.config_home()
	data = subroutine.config.data_home()
	state = subroutine.config.state_home()

	for directory in (config, data, state):
		directory.mkdir(parents=True, exist_ok=True)

	made = {
		"config": subroutine.config.config_file_path(),
		"credentials": config / "credentials.toml",
		"database": subroutine.config.default_database_path(),
		"context": state / subroutine.context.FILE_NAME,
	}

	made["config"].write_text("[connections.local]\nenabled = true\n", encoding="utf-8")
	made["credentials"].write_text("[tokens]\nlocal = 'sr_x'\n", encoding="utf-8")
	made["database"].write_bytes(b"SQLite format 3\x00")
	made["context"].write_text("workspace = 'personal'\n", encoding="utf-8")

	# A disposable instance under every root, which is what `--profile` leaves and what a
	# clean that only knew about the default one would walk straight past.
	for root in (config, data, state):
		spare = root / subroutine.config.PROFILES_DIRECTORY / "probe"
		spare.mkdir(parents=True, exist_ok=True)
		(spare / "kept.toml").write_text("x = 1\n", encoding="utf-8")

	made["profile"] = config / subroutine.config.PROFILES_DIRECTORY / "probe"

	return made


def test_a_deep_clean_leaves_nothing_an_ordinary_install_put_there (
	tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Every file an install writes is gone afterwards, profiles included.

	**Driven rather than read**: the assertion is that the paths do not exist, not that the
	script mentioned them. A report naming a file it failed to remove is the failure this is
	for, and it is the one a reader of the output would believe.
	"""

	made = _installed()

	for what, path in made.items():
		assert path.exists(), f"the fixture did not create {what}"

	assert deep_clean.main(["--yes"], home=tmp_path) in (0, 2)

	for what, path in made.items():
		assert not path.exists(), (
			f"{what} survived a deep clean: {path}\n{capsys.readouterr().out}"
		)


def test_a_deep_clean_refuses_an_executable_it_did_not_install (
	tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""A symlink into somebody's virtualenv is left alone, and said so.

	This is the developer's own machine: ``~/.local/bin/subroutine`` pointing into a checkout's
	virtualenv, deliberately, so a bare command runs the working tree. Taking it would break a
	working tree rather than remove an install — and it looks identical to a real install from
	the name alone, which is why the tell has to be where it points.
	"""

	_installed()

	venv = tmp_path / "venvs" / "subroutine" / "bin"
	venv.mkdir(parents=True)
	(venv / subroutine.config.APPLICATION_NAME).write_text("#!/bin/sh\n", encoding="utf-8")

	binaries = tmp_path / ".local" / "bin"
	binaries.mkdir(parents=True)
	link = binaries / subroutine.config.APPLICATION_NAME
	link.symlink_to(venv / subroutine.config.APPLICATION_NAME)

	assert deep_clean.main(["--yes"], home=tmp_path) == 2

	printed = capsys.readouterr().out

	assert link.is_symlink(), "a symlink into a virtualenv was removed"
	assert "SKIPPED" in printed and str(link) in printed, (
		f"the refusal is not in the report, so nobody would know to act on it:\n{printed}"
	)
	assert f"rm {link}" in printed, (
		f"a refusal has to carry the command that finishes the job:\n{printed}"
	)


def test_a_deep_clean_removes_an_executable_it_did_install (
	tmp_path: pathlib.Path
) -> None:
	"""The other half, without which the refusal above could be *never removes anything*.

	A ``uv tool`` install owns the tree behind the shim, so a link pointing into it is ours.
	Both cases have to be driven or the guard passes against a script that skips every
	executable it meets — which would leave the program on the machine and report success.
	"""

	_installed()

	name = subroutine.config.APPLICATION_NAME
	tools = tmp_path / ".local" / "share" / "uv" / "tools" / name / "bin"
	tools.mkdir(parents=True)
	(tools / name).write_text("#!/bin/sh\n", encoding="utf-8")

	binaries = tmp_path / ".local" / "bin"
	binaries.mkdir(parents=True)
	link = binaries / name
	link.symlink_to(tools / name)

	deep_clean.main(["--yes"], home=tmp_path)

	assert not link.is_symlink() and not link.exists(), "the installed program is still there"
	assert not (tmp_path / ".local" / "share" / "uv" / "tools" / name).exists(), (
		"the uv tool directory behind the shim was left"
	)


def test_a_dry_run_removes_nothing_and_says_so (
	tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""A rehearsal that deleted something would be the worst defect this file could have.

	And it must not *say* it removed anything either: the count is the same number in both
	modes and only the verb differs, which is the whole distinction a reader is relying on.
	"""

	made = _installed()

	deep_clean.main(["--dry-run"], home=tmp_path)

	for what, path in made.items():
		assert path.exists(), f"a dry run removed {what}"

	printed = capsys.readouterr().out

	assert "would remove" in printed
	assert "\n0 removed" not in printed and " removed," not in printed.split("would remove")[-1], (
		f"a dry run reported that it removed something:\n{printed}"
	)


def test_a_deep_clean_reports_a_connection_that_is_not_this_machine (
	tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""A served instance is data this cannot reach, and silence would imply it had gone.

	**And a local connection is not reported**, which is the half that is easy to get backwards:
	it points at the SQLite file the script does remove, so calling it *elsewhere* would say the
	one thing being destroyed is safe.
	"""

	_installed()
	subroutine.config.config_file_path().write_text(
		"[connections.local]\nenabled = true\n\n"
		"[connections.work]\nurl = 'https://example.invalid'\n",
		encoding="utf-8",
	)

	deep_clean.main(["--yes"], home=tmp_path)

	printed = capsys.readouterr().out
	noted = [one for one in printed.splitlines() if "connection" in one]

	assert any("work" in one for one in noted), (
		f"a served connection was not reported:\n{printed}"
	)
	assert not any("local" in one for one in noted), (
		f"the local connection was reported as being somewhere else:\n{printed}"
	)


def test_the_clean_names_every_plugin_the_repository_publishes () -> None:
	"""Discovered from the marketplace manifest, so a third plugin needs no edit here.

	The same rule ``tests/test_plugin.py`` holds the repository to. A hardcoded pair would be
	right today and would silently leave the next plugin installed on a machine somebody
	believed was clean.
	"""

	manifest = json.loads(
		(
			pathlib.Path(__file__).resolve().parent.parent
			/ deep_clean.MARKETPLACE_MANIFEST
		).read_text(encoding="utf-8")
	)
	published = {one["name"] for one in manifest["plugins"]}
	named = {one.split("@")[0] for one in deep_clean._published_plugins()}

	assert named == published, (
		f"the clean uninstalls {sorted(named)} and this repository publishes "
		f"{sorted(published)}"
	)


def test_the_clean_reads_its_paths_from_the_product (
	monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
	"""Move the roots and the clean moves with them, which is the anti-drift claim itself.

	**The one property that cannot be checked by reading**: a script naming ``~/.config/
	subroutine`` looks exactly like one asking :mod:`subroutine.config` where it lives, right up
	to the day a path changes and a machine is reported clean while an install sits on it.
	"""

	moved = tmp_path / "somewhere-else"
	monkeypatch.setenv("XDG_CONFIG_HOME", str(moved / "config"))
	monkeypatch.setenv("XDG_DATA_HOME", str(moved / "data"))
	monkeypatch.setenv("XDG_STATE_HOME", str(moved / "state"))

	made = _installed()

	assert str(moved) in str(made["database"]), "the fixture did not follow the environment"

	deep_clean.main(["--yes"], home=tmp_path)

	for what, path in made.items():
		assert not path.exists(), f"{what} survived when the roots were moved: {path}"




def test_a_home_the_environment_escapes_is_refused_before_anything_is_removed (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	"""`SR#1349`. The parameter is what persuades somebody it is safe to run.

	``places()`` said in writing that *everything this touches is under the home it was given*.
	That was false: the config, state and data roots came from :mod:`subroutine.config`, which
	reads the environment and has never heard of ``home``. **Measured at real cost** — driving
	``main(["--yes"], home=<scratch>)` from a plain interpreter removed this machine's own
	`config.toml`, its credentials, its state and its data directory, including the rollback
	copy of a migrated database and six disposable profiles.

	Threading ``home`` into the XDG fallbacks fixes the *unset* case, which is the ordinary
	state outside pytest and is the one that did the damage. **It cannot fix the set case**, and
	must not: the environment is meant to win. So that one is refused.

	**Nothing is removed**, which is the assertion that matters — a refusal that fires after the
	first delete is a report rather than a guard.
	"""

	elsewhere = tmp_path / "not-under-the-home"
	home = tmp_path / "home"

	for variable in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
		monkeypatch.setenv(variable, str(elsewhere / variable.lower()))

	made = elsewhere / "xdg_config_home" / subroutine.config.APPLICATION_NAME
	made.mkdir(parents=True)
	(made / "config.toml").write_text("[connections]\n", encoding="utf-8")

	home.mkdir()

	assert deep_clean.main(["--yes"], home=home) == 1

	said = capsys.readouterr().out

	assert "outside it" in said, said
	assert "Nothing has been removed" in said, said
	assert (made / "config.toml").exists(), (
		"the refusal fired after something had already been deleted"
	)

	# **And the three variables are named**, because *set them under that directory* is only
	# actionable if the reader knows which three.
	for variable in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
		assert variable in said, said


def test_a_run_with_no_home_is_not_second_guessed_about_its_own_environment (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#1349`. The refusal is for a *named* home only, and that is the whole of its scope.

	Somebody cleaning their own machine has roots wherever their environment says, which is what
	XDG means. Refusing them would be this guard deciding it knows better — and it would refuse
	every ordinary run on a machine that sets the variables at all, which is most of them.

	Driven as a dry run, because the subject is whether it *proceeds* rather than what it
	removes, and a real one here would delete the roots this test set up around it.
	"""

	monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "somewhere" / "config"))
	monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "somewhere" / "state"))
	monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "somewhere" / "data"))
	# **`pathlib.Path` imported here, not reached through `deep_clean`** — mypy refuses the
	# second because a module does not re-export what it imports, and `SR#1348` is the last
	# time that was worked around rather than written properly.
	monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda _cls: tmp_path))

	assert deep_clean.main(["--dry-run"]) in (0, 2)


def test_the_roots_follow_the_home_when_the_environment_says_nothing (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#1349`'s other half, and it is the case that did the damage.

	With the XDG variables unset — the ordinary state outside pytest — the roots fell back to
	``pathlib.Path.home()``, the *real* one, whatever ``home`` said. So a run pointed at a
	scratch directory computed the real config, state and data roots and removed them.

	**Asserted on the paths rather than on the outcome of a run**, and that is not only a
	preference. The defect is what :func:`deep_clean._roots` returns, and a scenario that
	deleted the right things could still be reading them from somewhere this parameter does not
	control — but the deciding reason is that **the scenario cannot be written safely**. Driving
	``main`` with the variables unset against the unfixed code deletes the developer's real
	config, state and data directories, which is precisely what filed `SR#1349`.

	So the falsification here is honest and weaker than usual, and it is worth saying which:
	against `HEAD` this raises ``TypeError`` because ``_roots`` took no argument, rather than
	failing on an escaped path. The escape test above is the one that reproduces the behaviour,
	and it can only do so because it points the environment somewhere it is safe to lose.
	"""

	for variable in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
		monkeypatch.delenv(variable, raising=False)

	scratch = tmp_path / "scratch"
	found = dict(deep_clean._roots(scratch))

	assert set(found) == {"config", "state", "data"}, found

	for kind, path in found.items():
		assert path.is_relative_to(scratch), f"the {kind} root escaped the home it was given: {path}"


def test_the_clean_never_reaches_past_the_home_it_was_given (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#1342`. Writing this file's tests uninstalled the developer's real Claude plugins.

	``home`` was threaded through every path this script builds, and the ``claude`` subprocess
	was left inheriting the environment — so it found the registry under the *real* home and
	uninstalled from it. The Python half was isolated and the half that does the work was not,
	which is isolation covering the part you can see.

	**Driven at the boundary rather than by reading the source.** A grep for ``env=`` would pass
	against a call that passes the wrong one; this replaces :func:`subprocess.run` and asserts
	on what every child would actually have been given.
	"""

	seen: list[dict[str, str]] = []

	def _recorded (command: typing.Sequence[str], **kwargs: typing.Any) -> typing.Any:
		"""Stand in for a `claude` that is present and answers."""

		seen.append(kwargs.get("env") or dict(os.environ))

		return subprocess.CompletedProcess(list(command), 0, stdout="", stderr="")

	# **Patched on the modules themselves**, which is what the script reaches through: it
	# does `import shutil` and calls `shutil.which`, so there is one object to replace and
	# no second spelling of the name to drift.
	monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/claude")
	monkeypatch.setattr(subprocess, "run", _recorded)

	_installed()
	deep_clean.main(["--yes"], home=tmp_path)

	assert seen, "no child was started, so this guard is asserting about nothing"

	for given in seen:
		assert given.get("HOME") == str(tmp_path), (
			f"a child was started against {given.get('HOME')!r} rather than the home this run "
			f"was given, so it would act on the machine instead of on the scratch directory"
		)


def test_a_deep_clean_finds_the_markers_rather_than_handing_over_a_command (
	tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""`SR#1342`. The one question this whole script answers was left for the operator to run.

	It printed ``find ~ -name .subroutine`` and called that a step. Driven on a real machine,
	the operator ran it and found four — in repositories they had forgotten about. A marker is
	not merely a trace either: it decides which project a bare ``subroutine add`` files into, so
	a checkout carrying one is *not* a machine that has never met this program, which is the
	whole state being produced.

	Reported and never removed, because a marker is very often a committed file belonging to
	somebody's repository.
	"""

	_installed()

	checkout = tmp_path / "work" / "something"
	checkout.mkdir(parents=True)
	marker = checkout / subroutine.directory.FILE_NAME
	marker.write_text("project = 'sr'\n", encoding="utf-8")

	assert deep_clean.main(["--yes"], home=tmp_path) == 2

	printed = capsys.readouterr().out

	assert marker.exists(), "a marker in somebody's repository was removed"
	assert str(marker) in printed, (
		f"the marker was not found, so the operator is still running find by hand:\n{printed}"
	)
	assert f"rm {marker}" in printed, "a refusal has to carry the command that finishes it"


def test_a_clean_machine_reports_that_nothing_is_left (
	tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""`SR#1342`. Run twice on a real machine, the second run said *1 left for you*.

	The markers entry fired unconditionally, so the summary could never say the job was done and
	the exit code was never zero. **A tool that cannot report success is one nobody reads the
	end of** — and this one is read at the end precisely because everything above it is
	irreversible.

	**The search is still reported when it finds nothing**, as a note rather than a job: no line
	and no markers look identical in the output, and the difference is whether the check ran at
	all.
	"""

	assert deep_clean.main(["--yes"], home=tmp_path) == 0

	printed = capsys.readouterr().out

	assert "nothing left for you" in printed, (
		f"a clean machine was still reported as having work outstanding:\n{printed}"
	)
	assert "searched" in printed, (
		f"the marker search is invisible, so a skipped check reads as a clean result:"
		f"\n{printed}"
	)
	assert "Run the commands above" not in printed, (
		f"a clean run tells the operator to run commands that are not there:\n{printed}"
	)


def test_every_command_this_offers_survives_a_path_with_a_space_in_it (
	tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""`SR#1342`. The advice was unquoted, and real directories have spaces in their names.

	Driven on a real home, the report offered ``rm <a path with a space in it>/.subroutine`` —
	which a shell reads as two arguments, neither of which exists, so pasting it removes nothing
	and says so twice. That is `SR#1322`'s finding in the tool written to be careful about it: a
	line labelled *by hand* that does not work is worse than no line, because following it
	confirms the false statement.

	**Every test before this used ``tmp_path``, which never has a space in it.** A fixture that
	cannot contain the defect is the whole reason this shipped.

	**The commands are parsed rather than matched.** ``shlex.split`` is what a shell does with
	that string, so a quoting bug shows up as an argument list that names the wrong file — and
	the assertion is that the argument is the path, which is the claim the line makes.
	"""

	awkward = tmp_path / "Dev" / "Two Words"
	awkward.mkdir(parents=True)
	marker = awkward / subroutine.directory.FILE_NAME
	marker.write_text("project = 'sr'\n", encoding="utf-8")

	venv = tmp_path / "my venvs" / "subroutine" / "bin"
	venv.mkdir(parents=True)
	(venv / subroutine.config.APPLICATION_NAME).write_text("#!/bin/sh\n", encoding="utf-8")

	binaries = tmp_path / ".local" / "bin"
	binaries.mkdir(parents=True)
	(binaries / subroutine.config.APPLICATION_NAME).symlink_to(
		venv / subroutine.config.APPLICATION_NAME
	)

	_installed()
	deep_clean.main(["--yes"], home=tmp_path)

	offered = [
		one.split("by hand:", 1)[1].strip()
		for one in capsys.readouterr().out.splitlines()
		if "by hand:" in one
	]

	assert offered, "nothing was offered, so this guard is asserting about nothing"

	removals = [shlex.split(one) for one in offered if one.startswith("rm ")]

	assert removals, f"no removal was offered for a path with a space in it: {offered}"

	for parsed in removals:
		# **The command names one thing.** Unquoted, the space makes it two, and the second is
		# a relative path that resolves against wherever the person happened to be standing.
		targets = [one for one in parsed[1:] if not one.startswith("-")]

		assert len(targets) == 1, (
			f"a removal names {len(targets)} things, so a path was split on its space: {parsed}"
		)
		assert pathlib.Path(targets[0]).exists(), (
			f"the command offered points at nothing: {' '.join(parsed)}"
		)

	assert str(marker) in [
		one for parsed in removals for one in parsed[1:]
	], "the marker with a space in its path was not among the commands offered"


def test_a_by_hand_line_is_always_a_command_and_never_advice (
	tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#1342`. Two of them were prose, and one carried a ``<name>`` placeholder.

	*"unset SUBROUTINE_PROFILE, and take it out of your shell profile"* and *"claude plugin
	uninstall <name>@subroutine"* are both unpasteable, and they sat under the same label as
	the ones that work. **One label, one meaning**: if it says *by hand* it is a command, and
	the explanation goes under *reason*, which is what that field is for.
	"""

	monkeypatch.setenv("SUBROUTINE_PROFILE", "spare")
	monkeypatch.setattr(shutil, "which", lambda _name: None)

	_installed()
	deep_clean.main(["--yes"], home=tmp_path)

	offered = [
		one.split("by hand:", 1)[1].strip()
		for one in capsys.readouterr().out.splitlines()
		if "by hand:" in one
	]

	assert offered, "nothing was offered, so this guard is asserting about nothing"

	for line in offered:
		assert "<" not in line and ">" not in line, (
			f"a command carries a placeholder nobody can paste: {line}"
		)
		assert ", and " not in line, f"a command is carrying a sentence: {line}"

		for word in shlex.split(line):
			assert word == "&&" or not word.endswith(","), (
				f"a command has prose punctuation in it: {line}"
			)


def test_a_run_pointed_at_a_scratch_home_looks_nowhere_else (
	tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""`SR#1345`. CI failed on all four interpreters and no local run could have.

	The isolation contract is *everything this touches is under the home it was given*, which
	is what lets a test point the whole run at a scratch directory. ``/usr/local/bin`` was
	consulted unconditionally, outside that contract — and CI installs with ``pip install -e .``
	into the system Python, so ``subroutine`` genuinely is there. The clean-machine test asked
	for nothing outstanding and was told about a program it had no way to remove.

	**This machine has no ``/usr/local/bin/subroutine``, so the fixture could not hold the
	defect** — the same reason the quoting bug shipped, one commit earlier, and the reason this
	asserts on *every path mentioned* rather than on the one that bit.

	Driven by reading the report rather than the source: a path outside the given home appearing
	anywhere in the output is the failure, whichever directory it came from.
	"""

	_installed()

	assert deep_clean.main(["--yes"], home=tmp_path) == 0, capsys.readouterr().out

	printed = capsys.readouterr().out
	roots = (str(tmp_path), "searched", "connection")

	for line in printed.splitlines():
		if not line.strip() or line.lstrip().startswith(("reason:", "by hand:")):
			continue

		mentioned = [one for one in line.split() if one.startswith("/")]

		for path in mentioned:
			# The XDG roots are redirected by `conftest.py` and are legitimately elsewhere;
			# what must never appear is a system directory nobody pointed this at.
			assert not path.startswith(("/usr/", "/opt/", "/etc/", "/bin/", "/sbin/")), (
				f"a run given {tmp_path} reported on {path}, which is outside it:\n{printed}"
			)

	assert any(one in printed for one in roots), (
		f"the report mentions none of the scratch home, so this checked nothing:\n{printed}"
	)


def test_the_places_searched_for_a_program_stay_inside_the_home_given (
	tmp_path: pathlib.Path
) -> None:
	"""`SR#1345`, and the assertion the report-reading version could not make.

	The contract is that everything touched is under the home passed in. ``/usr/local/bin`` broke
	it unconditionally, and CI failed on all four interpreters because it installs with
	``pip install -e .`` into the system Python and really does have a ``subroutine`` there.

	**No local run could have caught that by reading the report**, because this machine has no
	such file — the same blind fixture that shipped the quoting defect one commit earlier. So
	the claim is asserted where it is decided instead: what is *searched*, rather than what
	happened to be found.

	Both directions, because a rule that returns nothing outside the home would also return
	nothing at all, and then the program would never be removed from a real machine.
	"""

	scratch = deep_clean.places(tmp_path)

	assert scratch, "a scratch run searches nowhere, so nothing would ever be removed"

	for one in scratch:
		assert tmp_path in one.parents or one == tmp_path, (
			f"a run given {tmp_path} would search {one}, which is outside it"
		)

	# **And on the real machine the system directory is back**, or an ordinary install that put
	# the program in `/usr/local/bin` is quietly left behind by a tool reporting a clean machine.
	real = deep_clean.places(pathlib.Path.home())

	assert pathlib.Path("/usr/local/bin") in real, (
		f"a real run searches only {real}, so a system install would survive it"
	)


def test_a_machine_that_never_had_claude_code_is_not_told_it_has_work_left (
	tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#1347`. CI has no editor on its test runners, so all four jobs failed on a clean run.

	No ``claude`` on the ``PATH`` and no trace of ours under its directory means the plugin was
	never on this machine — which is the outcome being asked for. Reporting it as *skipped* made
	such a machine say *1 left for you* for ever, and pointed the reader at a command they
	cannot run.

	**Third time an expected absence has been dressed as an unfinished job here**, after the
	plugin uninstall's own error text and the unconditional markers step. The register a report
	of a destructive operation is read in is the whole of why it keeps mattering.

	**Driven by removing `claude` rather than by finding a machine without one** — this one has
	it, which is exactly why the defect reached CI.
	"""

	monkeypatch.setattr(shutil, "which", lambda _name: None)

	_installed()

	assert deep_clean.main(["--yes"], home=tmp_path) == 0, capsys.readouterr().out

	printed = capsys.readouterr().out

	assert "nothing left for you" in printed, (
		f"a machine that never had Claude Code was told it has work outstanding:\n{printed}"
	)


def test_a_trace_left_with_no_claude_to_remove_it_is_still_reported (
	tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#1347`, and the half that stops the fix becoming *never mention it*.

	Claude Code uninstalled while its plugin cache remains is a real state, and the registry
	files are its own bookkeeping — hand-editing them is how a plugin ends up listed and absent,
	which reports success and starts no server. So a trace with no way to remove it is genuinely
	somebody's job and has to say so.

	Both directions, because a rule that only ever answers *absent* would pass the guard above
	while quietly leaving a plugin installed on every machine that has one.
	"""

	# A cache directory is one of the four traces; the others are the marketplace clone and
	# the two registry files.
	cache = tmp_path / ".claude" / "plugins" / "cache" / subroutine.config.APPLICATION_NAME
	cache.mkdir(parents=True)
	(cache / "0.8.1").mkdir()

	monkeypatch.setattr(shutil, "which", lambda _name: None)

	_installed()

	assert deep_clean.main(["--yes"], home=tmp_path) == 2

	printed = capsys.readouterr().out

	assert "SKIPPED" in printed and "claude" in printed, (
		f"a plugin left on the machine was not reported:\n{printed}"
	)


def test_a_deep_clean_removes_every_name_the_package_installs_under (
	tmp_path: pathlib.Path
) -> None:
	"""`SR#1348`. Two console scripts, and this removed one of them.

	``pyproject.toml`` declares ``subroutine`` and ``subr`` — one entry point under two names
	(`#752`) — so an ordinary ``uv tool install`` puts two executables on the machine. Taking one
	and then the tree they both point into leaves the other dangling, and the next install
	refuses rather than overwriting it. Simon met that on the first command of a first-contact
	run, which is the only place it could have surfaced.

	**The fixture is the finding.** Every test above this one built the executable itself, under
	the single name the script looked for, so the input that would have shown the defect was the
	one no fixture ever supplied. This builds what an install builds instead.
	"""

	_installed()

	names = _declared_names()

	assert len(names) > 1, (
		f"this guard needs a package that installs under more than one name, and found {names}"
	)

	tools = tmp_path / ".local" / "share" / "uv" / "tools" / subroutine.config.APPLICATION_NAME
	shims = tools / "bin"
	shims.mkdir(parents=True)

	binaries = tmp_path / ".local" / "bin"
	binaries.mkdir(parents=True)

	for one in names:
		(shims / one).write_text("#!/bin/sh\n", encoding="utf-8")
		(binaries / one).symlink_to(shims / one)

	deep_clean.main(["--yes"], home=tmp_path)

	left = [one for one in names if (binaries / one).is_symlink() or (binaries / one).exists()]

	assert not left, f"the clean left {left} behind, and the next install will refuse"


def test_the_names_a_clean_looks_for_come_from_the_package () -> None:
	"""Derived rather than listed, so a third console script is covered on the day it ships.

	**Two genuinely separate sources**: the declaration in ``pyproject.toml``, and what the
	install compiled that into. They cannot be made to agree by a shared helper, which is what
	makes this a cross-check rather than a guard normalising like its subject.

	This is the half that is checkable on any machine (`#1345`'s lesson). The scenario above
	needs a fixture; this needs only the package to be installed, so it fails on a laptop the
	same way it fails on a runner.
	"""

	assert set(deep_clean.installed_names()) == set(_declared_names()), (
		"the names the clean looks for and the names the package declares have parted company"
	)


def test_a_name_the_package_never_declared_is_still_taken_from_the_tool_tree (
	tmp_path: pathlib.Path
) -> None:
	"""The scan beside the declared names, and it covers the case the metadata cannot.

	:func:`installed_names` describes the package **this script was run from**, which need not be
	the one on the machine — a checkout cleaning up after an older install declares fewer names
	than that install created. Anything pointing into the uv tool tree is ours by construction,
	so it is taken whether or not this interpreter has heard of it.

	Without this the fix would be one release behind for ever: correct for the names we ship
	today and blind to the ones already on somebody's disk.
	"""

	_installed()

	tools = tmp_path / ".local" / "share" / "uv" / "tools" / subroutine.config.APPLICATION_NAME
	shims = tools / "bin"
	shims.mkdir(parents=True)

	stranger = "subroutine-from-an-older-release"
	(shims / stranger).write_text("#!/bin/sh\n", encoding="utf-8")

	assert stranger not in deep_clean.installed_names(), "the fixture has to be a name we do not declare"

	binaries = tmp_path / ".local" / "bin"
	binaries.mkdir(parents=True)
	left = binaries / stranger
	left.symlink_to(shims / stranger)

	deep_clean.main(["--yes"], home=tmp_path)

	assert not left.is_symlink() and not left.exists(), (
		"a shim into our own tool tree was left because its name was not declared"
	)


def test_a_package_that_is_reachable_but_not_installed_falls_back_to_the_one_name (
	monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A checkout on ``PYTHONPATH`` has no distribution metadata, and must not raise.

	The clean has to keep working on a machine whose install it cannot interrogate — that is the
	machine most in need of it. It under-reports rather than guessing, and the tool-tree scan
	above is what covers the gap.
	"""

	def _missing (name: str) -> typing.Any:
		"""Answer as an interpreter that can import the package but has no metadata for it."""

		raise importlib.metadata.PackageNotFoundError(name)

	# Patched on the module itself rather than through ``deep_clean``, which reaches an
	# attribute that module never declared — the shared module object is the same one either
	# way, and only one of the two spellings type-checks.
	monkeypatch.setattr(importlib.metadata, "distribution", _missing)

	assert deep_clean.installed_names() == [subroutine.config.APPLICATION_NAME]
