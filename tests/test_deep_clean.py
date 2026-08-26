"""The development tool that puts a machine back to never having met this program.

`SR#1342`. First contact can only be driven on a machine that has never seen Subroutine, and
there are not many of those. This is the script that makes one, and this file is what stops it
becoming a list of paths that used to be right.

**Two claims, and the second is the one with teeth**: it removes what an ordinary install
leaves, and it *refuses* what it cannot prove is ours. A destructive tool that guesses once is
one nobody runs again.
"""

import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import typing

import pytest

import subroutine.config
import subroutine.context
import subroutine.directory

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

# The line above is what makes this importable: `scripts/` is not a package and this is
# the only test that reaches into it.
import deep_clean


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
