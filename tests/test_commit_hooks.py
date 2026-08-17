"""The commit hooks, driven rather than read — items ``#48`` and ``#51``.

Every test here makes a real commit in a real temporary repository with the real hook
installed, because the thing most likely to be wrong is the wiring: a hook that is not
executable, a shell quoting mistake, a ``grep`` that matches nothing. None of that is visible
to a test that calls a function.

**The instance calls are stubbed and the shell is not.** ``subroutine`` is put on the test's
``PATH`` as a script whose answers this file controls, so the hooks run byte for byte as they
ship while the questions they ask an instance are answered here. Driving a real instance
instead would test the CLI, which has its own tests, and would make these depend on a database
being reachable — the hooks themselves are the subject.

The one thing that cannot be tested here is the reason the shims exist at all: this working
tree is on a filesystem that refuses to execute anything, so the hooks are invoked through
``sh`` by path, exactly as ``scripts/install_hooks.py`` arranges in a real clone.
"""

import importlib.util
import pathlib
import subprocess
import textwrap
import types
import typing

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOKS = ROOT / "hooks"


def _installer () -> types.ModuleType:
	"""Load the installer by path, the way the release-script tests load their subjects.

	Not as ``scripts.install_hooks``: with ``scripts`` on the path as well, mypy finds the
	same file under two module names and stops checking the whole tree.
	"""

	spec = importlib.util.spec_from_file_location(
		"install_hooks", ROOT / "scripts" / "install_hooks.py"
	)

	assert spec is not None and spec.loader is not None

	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	return module


INSTALLER = _installer()


class Repo(typing.NamedTuple):
	"""A throwaway git repository with the hooks wired up and the instance stubbed."""

	path: pathlib.Path
	environment: dict[str, str]

	def run (self, *arguments: str) -> subprocess.CompletedProcess[str]:
		"""Run a git command in it, returning the result rather than raising."""

		return subprocess.run(
			["git", *arguments],
			cwd=self.path,
			env=self.environment,
			capture_output=True,
			text=True,
			check=False,
		)

	def commit (self, message: str, *arguments: str) -> subprocess.CompletedProcess[str]:
		"""Stage everything and try to commit it."""

		self.run("add", "-A")

		return self.run("commit", *arguments, "-m", message)

	def write (self, name: str, text: str) -> None:
		"""Put a file in the repository."""

		(self.path / name).write_text(text)

	def recorded (self) -> list[str]:
		"""Return the `subroutine` commands the hooks ran, in order."""

		log = self.path.parent / "calls.log"

		return log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def repo (tmp_path: pathlib.Path) -> Repo:
	"""A repository whose hooks run, with a stub standing in for the instance.

	``tmp_path`` rather than anywhere in the working tree, for the reason the rest of the
	suite uses it: this repository is on a network share, and git wants a lock.
	"""

	work = tmp_path / "work"
	work.mkdir()

	shims = tmp_path / "shims"

	# The stub records what it was asked and answers from a file the test can rewrite, so a
	# ref that does not resolve is expressed as data rather than as a second stub.
	stub = tmp_path / "bin"
	stub.mkdir()
	(stub / "subroutine").write_text(
		textwrap.dedent(f"""\
			#!/bin/sh
			echo "$@" >> "{tmp_path}/calls.log"

			case "$1" in
				whoami) exit 0 ;;
				show)
					grep -qx "$2" "{tmp_path}/known" 2>/dev/null || exit 1
					cat "{tmp_path}/comments.$2" 2>/dev/null
					exit 0
					;;
				comment)
					echo "$3" >> "{tmp_path}/comments.$2"
					exit 0
					;;
				uncomment)
					grep -v "$3" "{tmp_path}/comments.$2" > "{tmp_path}/c.tmp" 2>/dev/null || true
					mv "{tmp_path}/c.tmp" "{tmp_path}/comments.$2" 2>/dev/null || true
					exit 0
					;;
			esac
			exit 0
			"""),
	)
	(stub / "subroutine").chmod(0o755)

	(tmp_path / "known").write_text("42\n99\n")

	environment = {
		"PATH": f"{stub}:/usr/bin:/bin",
		"HOME": str(tmp_path),
		"GIT_AUTHOR_NAME": "Test",
		"GIT_AUTHOR_EMAIL": "test@example.com",
		"GIT_COMMITTER_NAME": "Test",
		"GIT_COMMITTER_EMAIL": "test@example.com",
	}

	built = Repo(path=work, environment=environment)

	built.run("init", "-q", ".")

	# **After `init`, and through the installer rather than beside it** (`#909`). This used to
	# write the shims before there was a repository and then set `core.hooksPath` by hand — so
	# the installer's own configuration step was exercised by nothing, and its `git config` went
	# to whichever repository the script happened to live in, which was this checkout.
	INSTALLER.install(into=shims, repository=work)

	return built


def test_a_commit_that_cites_nothing_is_refused (repo: Repo) -> None:
	"""`#48`, and the whole of it. The item-first rule was enforced by nothing."""

	repo.write("thing.py", "value = 1\n")
	refused = repo.commit("Change something")

	assert refused.returncode != 0, refused.stdout
	assert "cites no item" in refused.stderr
	assert "SR#42" in refused.stderr, "and says how to cite one"
	assert "--no-verify" in refused.stderr, "and how to take the exemption deliberately"


def test_a_release_commit_cites_nothing_and_is_allowed (repo: Repo) -> None:
	"""`#955`. This hook refused every release for two days and nothing here noticed.

	`scripts/release.py` writes ``Release <version>`` and commits four files that are entirely
	version bumps. There is no author to remind and no change anybody designed — it is the
	mechanical consequence of work already recorded, and the changelog is its record.

	**A guard written between two rare events has never run at one.** The hooks went in on
	2026-08-15 and ``v0.7.1`` shipped on the 14th, so the first release after this hook existed
	was the first time it ever saw a release commit — `#893`'s shape one guard along, where
	`#859`'s changelog guard refused the *state* a release creates and this refused the
	*message* it creates. Both were found by a release failing rather than by a test.
	"""

	# A substantive change rather than a comment, so it is the *subject* being exempted here
	# and not the comment-only rule two tests down answering by accident.
	repo.write("plugin.json", '{"version": "9.9.9"}\n')
	released = repo.commit("Release 9.9.9\n\nSee CHANGELOG.md for what 9.9.9 contains.")

	assert released.returncode == 0, released.stderr


@pytest.mark.parametrize(
	"subject",
	[
		"Release the lock when the worker dies",
		"Release 0.7",
		"Released 0.7.5",
		"Prepare Release 0.7.5",
	],
)
def test_only_the_generated_release_subject_is_exempt (repo: Repo, subject: str) -> None:
	"""`#955`. The exemption is a shape a person does not type by accident, and only that.

	**Anchored at both ends against a version number**, because ``Release`` is an ordinary
	English verb: *Release the lock when the worker dies* is work and must still be refused. A
	prefix match would have exempted it, which is how an exemption written for one generated
	message becomes a way round the rule for anybody who starts a subject with the right word.

	Parametrised over the near misses rather than asserting once, because they fail the pattern
	at four different points — the version, its shape, the verb's tense and the anchor — and one
	case would prove nothing about the others.
	"""

	repo.write("thing.py", "value = 1\n")
	refused = repo.commit(subject)

	assert refused.returncode != 0, refused.stdout
	assert "cites no item" in refused.stderr


def test_a_bare_reference_is_refused_because_github_would_resolve_it (repo: Repo) -> None:
	"""§6.15. The one collision the resolve-or-prose rule cannot catch.

	A bare ``#42`` in a commit message is auto-linked by GitHub to *this repository's* issues,
	and **the link works** — so nobody reading it can tell it is about something else. That is
	why this is refused rather than accepted-and-rewritten.
	"""

	repo.write("thing.py", "value = 1\n")
	refused = repo.commit("Fixes #42")

	assert refused.returncode != 0, refused.stdout
	assert "GitHub" in refused.stderr
	assert "SR#42" in refused.stderr


def test_a_reference_inside_a_code_span_is_prose_rather_than_a_citation (repo: Repo) -> None:
	"""`#836`'s shape, met by this hook on its own first commit.

	The message adding these hooks *described* the rule — "never write a bare ``#42``" — and
	was refused by the rule it was describing. Correct prose, rewritten into worse prose to
	get past a checker, is exactly what that item recorded about the link checker.

	**The justification is exact rather than a convenience**: GitHub does not auto-link inside
	a code span, so a reference there cannot become a link to this repository's issues, which
	is the whole of what §6.15 forbids.

	The span is deliberately wrapped across two lines, because stripping line by line leaves
	an unmatched backtick on each half and refuses anyway — the obvious cheap fix, and the one
	`#836` walked into.
	"""

	repo.write("thing.py", "value = 1\n")
	allowed = repo.commit(
		"Explain the rule\n\nSR#42 — never write a bare `#42`, because GitHub\nauto-links `#7` "
		"to this repository."
	)

	assert allowed.returncode == 0, allowed.stderr


def test_a_reference_to_work_that_does_not_exist_is_refused (repo: Repo) -> None:
	"""Resolvable, not merely well-formed.

	``SR#999`` reads exactly like a citation of real work, which is what makes it worse than
	no citation at all: a history full of them is untrustworthy rather than obviously wrong.
	"""

	repo.write("thing.py", "value = 1\n")
	refused = repo.commit("Do the thing\n\nSR#77 — nothing is numbered 77")

	assert refused.returncode != 0, refused.stdout
	assert "not here: 77" in refused.stderr


def test_an_unreachable_instance_does_not_stop_anybody_committing (
	repo: Repo, tmp_path: pathlib.Path
) -> None:
	"""**A courtesy check, and courtesies do not hold the door shut.**

	The failure being separated here is *this ref is wrong* from *nothing could be asked*.
	Without that, an instance being down — or a laptop on a train — would make the repository
	uncommittable, which is a far worse outcome than an uncited commit.
	"""

	(tmp_path / "bin" / "subroutine").write_text("#!/bin/sh\nexit 1\n")
	(tmp_path / "bin" / "subroutine").chmod(0o755)

	repo.write("thing.py", "value = 1\n")
	allowed = repo.commit("Do the thing\n\nSR#42 — real work")

	assert allowed.returncode == 0, allowed.stderr
	assert "could not be reached" in allowed.stderr


def test_a_comment_only_change_needs_no_item (repo: Repo) -> None:
	"""Decision `#47`'s exemption: prose no program reads.

	Recognised conservatively — every changed line has to be a comment — because telling a
	docstring on an endpoint from one on an ordinary function needs to know which functions
	are routes, and a check that guessed would be worse than one that says what it cannot see.
	"""

	repo.write("thing.py", "# just a note\n")
	allowed = repo.commit("Reword a comment")

	assert allowed.returncode == 0, allowed.stderr


def test_a_change_that_is_only_partly_comment_still_needs_one (repo: Repo) -> None:
	"""The half that makes the exemption worth having rather than a hole.

	Without this the rule would be "did you touch a comment", which every commit does.
	"""

	repo.write("thing.py", "# a note\nvalue = 1\n")
	refused = repo.commit("Mixed")

	assert refused.returncode != 0, refused.stdout


def test_the_commit_is_recorded_against_every_item_it_cites (repo: Repo) -> None:
	"""`#51`, the return journey. Neither direction was answerable before.

	Two refs in one message, because the loop closing for one and not the other is the shape
	a single-ref test cannot see.
	"""

	repo.write("thing.py", "value = 1\n")
	made = repo.commit("Do two things\n\nSR#42 and SR#99 — both real")

	assert made.returncode == 0, made.stderr

	short = repo.run("rev-parse", "--short", "HEAD").stdout.strip()
	comments = [call for call in repo.recorded() if call.startswith("comment ")]

	assert len(comments) == 2, f"one per cited item, got {comments}"
	assert all(f"Committed as {short}" in call for call in comments)
	assert any(call.startswith("comment 42 ") for call in comments)
	assert any(call.startswith("comment 99 ") for call in comments)


def test_amending_replaces_the_record_rather_than_adding_to_it (repo: Repo) -> None:
	"""An amend replaces a commit, so the sha written a moment ago has stopped existing.

	Leaving it would put "Committed as 34d87d3" on an item where no such commit can be found,
	which is worse than saying nothing — a record pointing at something unreachable is the
	defect this whole item exists to remove, arriving from the other direction.
	"""

	repo.write("thing.py", "value = 1\n")
	repo.commit("Do the thing\n\nSR#42 — real work")

	first = repo.run("rev-parse", "--short", "HEAD").stdout.strip()

	# **Amended with a change**, because `--amend --no-edit` on an untouched tree within the
	# same second rebuilds an identical commit object — the sha does not move, and the test
	# proves nothing while passing. Found by asserting that it moved.
	repo.write("thing.py", "value = 2\n")
	repo.run("add", "-A")
	repo.run("commit", "--amend", "--no-edit")

	second = repo.run("rev-parse", "--short", "HEAD").stdout.strip()

	assert first != second, "an amend has to produce a different sha or this proves nothing"

	calls = repo.recorded()

	assert any(call == f"uncomment 42 Committed as {first}" for call in calls), (
		f"the superseded sha was not taken back out: {calls}"
	)
	assert any(f"Committed as {second}" in call for call in calls if call.startswith("comment "))


def test_running_the_hook_twice_says_the_same_thing_once (repo: Repo) -> None:
	"""Asked before writing, so a hook run by hand cannot duplicate the record."""

	repo.write("thing.py", "value = 1\n")
	repo.commit("Do the thing\n\nSR#42 — real work")

	before = len([call for call in repo.recorded() if call.startswith("comment ")])

	subprocess.run(
		["sh", str(HOOKS / "post-commit")],
		cwd=repo.path,
		env=repo.environment,
		capture_output=True,
		check=False,
	)

	after = len([call for call in repo.recorded() if call.startswith("comment ")])

	assert after == before, "the second run wrote the same commit down again"


def test_a_commit_with_no_reference_records_nothing (repo: Repo) -> None:
	"""The exempt case must not reach the instance at all.

	Worth asserting rather than assuming: a hook that asked anyway would put a comment on
	nothing, or spend a round trip per prose commit, and neither would ever be noticed.
	"""

	repo.write("thing.py", "# just a note\n")
	repo.commit("Reword a comment")

	assert not [call for call in repo.recorded() if call.startswith("comment ")]


def test_every_tracked_hook_is_installed (tmp_path: pathlib.Path) -> None:
	"""The installer discovers hooks rather than naming them, and this is what says so.

	A list in the installer would be a second place to add a hook and the one somebody
	forgets — so the guard is that what is installed matches what is in `hooks/`.
	"""

	elsewhere = tmp_path / "elsewhere"
	elsewhere.mkdir()
	subprocess.run(["git", "init", "-q", "."], cwd=elsewhere, check=True)

	installed = INSTALLER.install(into=tmp_path / "shims", repository=elsewhere)
	tracked = sorted(path.name for path in HOOKS.iterdir() if path.is_file())

	assert sorted(installed) == tracked
	assert tracked, "the walk found no hooks at all"

	for name in tracked:
		shim = tmp_path / "shims" / name

		# **The property the shims exist for.** A hook git cannot execute is skipped in
		# silence, so an installer that produced one would report success and change nothing.
		assert shim.stat().st_mode & 0o111, f"{name} was installed and cannot be run"
		assert str(HOOKS / name) in shim.read_text(), "a copy would go stale; this runs the source"


def test_installing_elsewhere_leaves_this_checkout_alone (tmp_path: pathlib.Path) -> None:
	"""**`#909`, and it is the half the suite was silently doing to itself.**

	`install` took a directory for the shims and then ran `git config core.hooksPath` against
	the repository the *script* lives in, whatever that directory said — so two tests here
	pointed this clone at their own `tmp_path` on every run. It worked, because a shim runs the
	tracked hook by path; it would have stopped working in silence the moment pytest collected
	the directory, three runs later. **Git skips a hook it cannot find without a word**, which
	is the measured fact the shims exist to work around in the first place.

	Asserts on the configuration rather than on the shims, because the shims were never the
	part that leaked.
	"""

	def hooks_path (repository: pathlib.Path) -> str:
		answer = subprocess.run(
			["git", "config", "--get", "core.hooksPath"],
			cwd=repository, capture_output=True, text=True,
		)
		return answer.stdout.strip()

	before = hooks_path(ROOT)

	elsewhere = tmp_path / "elsewhere"
	elsewhere.mkdir()
	subprocess.run(["git", "init", "-q", "."], cwd=elsewhere, check=True)

	INSTALLER.install(into=tmp_path / "shims", repository=elsewhere)

	# **The harm first, deliberately.** Asserting the named repository was set would fire on the
	# same defect and report it as *the install did nothing*, which sends the next reader after
	# the wrong half — the shims were written and the configuration went somewhere else.
	assert hooks_path(ROOT) == before, (
		f"installing into {tmp_path} moved this checkout's core.hooksPath: "
		f"{before!r} -> {hooks_path(ROOT)!r}"
	)
	assert hooks_path(elsewhere) == str(tmp_path / "shims"), "the named repository was not set"


def test_the_installer_refuses_to_write_shims_without_saying_whose (tmp_path: pathlib.Path) -> None:
	"""The two arguments are one argument, and passing half of it is what caused `#909`.

	Refusing the pair rather than defaulting the second, because a default is precisely what
	let the halves drift: `into` reads as the isolation seam and is only half of one.
	"""

	with pytest.raises(ValueError, match="together or neither"):
		INSTALLER.install(into=tmp_path / "shims")

	with pytest.raises(ValueError, match="together or neither"):
		INSTALLER.install(repository=tmp_path)

	assert not (tmp_path / "shims").exists(), "it wrote the shims before refusing"
