"""Cutting a release — ``scripts/release.py``, item ``#239``.

The script exists because a version has to be written in three places at once and twice in the
first three releases it was not: 0.1.0's changelog still said *Unreleased*, and 0.1.2 was tagged
with the plugin manifest reading 0.1.1. **So what is worth testing is the refusals**, not the
happy path — a release tool that repairs what it should stop at is worse than none, because it
produces a tag nobody meant and the mistake is only visible afterwards.

Each test builds a whole miniature repository in ``tmp_path`` — local disk, and nothing to do
with this one. The script is *copied in*, because it resolves the files it edits relative to its
own location: that is what makes it safe to run from anywhere, and it is why a test cannot
simply import it and point it somewhere else.
"""

import json
import pathlib
import shutil
import subprocess
import sys
import typing

import pytest

REPOSITORY = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPOSITORY / "scripts" / "release.py"

#: A changelog with something waiting to go out, which is the state the script requires.
CHANGELOG = """# Changelog

Notable changes, newest first.

## Unreleased

### Fixed

- Something that was wrong is now right.

## 0.1.0 — 2026-08-01

The first release.
"""

#: A manifest with more in it than a version, so that rewriting one key can be shown to leave
#: the rest alone — the failure mode of editing JSON by pattern instead of by parser.
MANIFEST = {
	"name": "subroutine",
	"version": "0.1.0",
	"description": "Project management for people and agents, in equal measure.",
	"userConfig": {"command": {"type": "string", "default": "subroutine"}},
}


def _git (repository: pathlib.Path, *arguments: str) -> str:
	"""Run one git command in ``repository`` and return its output."""

	return subprocess.run(
		["git", *arguments], cwd=repository, capture_output=True, text=True, check=True
	).stdout


@pytest.fixture
def repository (tmp_path: pathlib.Path) -> pathlib.Path:
	"""Build a repository with a changelog, a manifest, the script, and one release behind it."""

	root = tmp_path / "project"
	manifest = root / "plugins" / "subroutine" / ".claude-plugin"
	manifest.mkdir(parents=True)
	(root / "scripts").mkdir()

	(root / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
	(manifest / "plugin.json").write_text(json.dumps(MANIFEST, indent=2) + "\n", encoding="utf-8")
	shutil.copy(SCRIPT, root / "scripts" / "release.py")

	_git(root, "init", "-q")
	_git(root, "config", "user.email", "test@example.com")
	_git(root, "config", "user.name", "Test")
	_git(root, "add", ".")
	_git(root, "commit", "-q", "-m", "the state before a release")
	_git(root, "tag", "-a", "v0.1.0", "-m", "0.1.0")

	return root


@pytest.fixture
def cut (repository: pathlib.Path) -> typing.Callable[..., subprocess.CompletedProcess[str]]:
	"""Return a runner for the script, in the repository the other fixture built."""

	def run (*arguments: str) -> subprocess.CompletedProcess[str]:
		"""Cut a release and hand back whatever happened.

		**``sys.executable``, never ``"python"``** (`#254`). A bare ``python`` is a PATH lookup,
		and the interpreter running this suite is only on PATH under that name when the
		virtualenv happens to be *activated* — so ``/home/…/venvs/subroutine/bin/python -m
		pytest``, a perfectly ordinary way to run it, failed all eleven tests here with
		``FileNotFoundError``. It is also the more correct thing to ask for: a test spawning a
		subprocess wants the interpreter running the test, not whichever one a shell would find.

		The direction is worth noting because it is the opposite of `#227`, `#228` and `#230`
		— this one passed in CI, which activates the venv, and failed locally.
		"""

		return subprocess.run(
			[sys.executable, str(repository / "scripts" / "release.py"), *arguments],
			cwd=repository, capture_output=True, text=True, check=False,
		)

	return run


def test_one_version_reaches_the_changelog_the_manifest_and_the_tag (
	repository: pathlib.Path, cut: typing.Callable[..., subprocess.CompletedProcess[str]]
) -> None:
	"""The whole point, asserted in all three places rather than in the one that is easiest."""

	done = cut("0.1.1", "--date", "2026-08-02")

	assert done.returncode == 0, done.stderr

	assert "## 0.1.1 — 2026-08-02" in (repository / "CHANGELOG.md").read_text(encoding="utf-8")

	manifest = json.loads(
		(repository / "plugins/subroutine/.claude-plugin/plugin.json").read_text(encoding="utf-8")
	)

	assert manifest["version"] == "0.1.1"
	assert "v0.1.1" in _git(repository, "tag", "--list")

	# And the tag names the commit that carries the change, rather than the one before it —
	# which is the ordering mistake the script exists to remove.
	assert _git(repository, "tag", "--points-at", "HEAD").strip() == "v0.1.1"
	assert not _git(repository, "status", "--porcelain").strip(), "it left the tree dirty"


def test_the_manifest_keeps_everything_that_is_not_the_version (
	repository: pathlib.Path, cut: typing.Callable[..., subprocess.CompletedProcess[str]]
) -> None:
	"""A manifest that stops being valid JSON is one nobody can install.

	The failure this rules out is editing it by pattern: a regex that catches the version also
	catches a default of the same shape, and the damage arrives at a stranger rather than here.
	"""

	assert cut("0.1.1", "--date", "2026-08-02").returncode == 0

	manifest = json.loads(
		(repository / "plugins/subroutine/.claude-plugin/plugin.json").read_text(encoding="utf-8")
	)

	assert manifest["name"] == "subroutine"
	assert manifest["description"] == MANIFEST["description"]
	assert manifest["userConfig"] == MANIFEST["userConfig"]


def test_nothing_is_pushed (
	repository: pathlib.Path, cut: typing.Callable[..., subprocess.CompletedProcess[str]]
) -> None:
	"""Publishing is outward-facing and belongs to a person, so this stops at a tag.

	Asserted by the repository having no remote at all: a script that pushed would fail here,
	and one that succeeds has demonstrably not tried.
	"""

	done = cut("0.1.1", "--date", "2026-08-02")

	assert done.returncode == 0, done.stderr
	assert not _git(repository, "remote").strip(), "the fixture is only meaningful with no remote"
	assert "git push" in done.stdout, "it must say what is left to do"


def test_a_dry_run_changes_nothing (
	repository: pathlib.Path, cut: typing.Callable[..., subprocess.CompletedProcess[str]]
) -> None:
	"""Saying what would happen has to be free, or nobody uses it before the real thing."""

	before = (repository / "CHANGELOG.md").read_text(encoding="utf-8")

	done = cut("0.1.1", "--date", "2026-08-02", "--dry-run")

	assert done.returncode == 0
	assert "0.1.1" in done.stdout
	assert (repository / "CHANGELOG.md").read_text(encoding="utf-8") == before
	assert not _git(repository, "tag", "--list", "v0.1.1").strip()


@pytest.mark.parametrize(
	("version", "expected"),
	[
		("v0.1.1", "is not a version"),
		("0.1", "is not a version"),
		("0.1.0", "already exists"),
		("0.0.9", "not ahead of"),
	],
	ids=["a leading v", "two numbers", "a tag that exists", "going backwards"],
)
def test_a_release_that_is_not_the_next_one_is_refused (
	cut: typing.Callable[..., subprocess.CompletedProcess[str]], version: str, expected: str
) -> None:
	"""Four ways of naming the wrong release, each refused by name rather than by exception.

	`v0.1.1` is the interesting one: it would have produced a `vv0.1.1` tag, which is not a
	thing hatch-vcs matches — so the release would have built a development version and PyPI
	would have refused the upload, three steps and one CI run later than here.
	"""

	done = cut(version)

	assert done.returncode == 1
	assert expected in done.stderr


def test_a_dirty_tree_is_refused (
	repository: pathlib.Path, cut: typing.Callable[..., subprocess.CompletedProcess[str]]
) -> None:
	"""A tag has to name a state somebody can go back to, and uncommitted work is not one."""

	(repository / "CHANGELOG.md").write_text("edited but not committed\n", encoding="utf-8")

	done = cut("0.1.1")

	assert done.returncode == 1
	assert "working tree has changes" in done.stderr
	assert not _git(repository, "tag", "--list", "v0.1.1").strip()


def test_a_changelog_with_nothing_unreleased_is_refused (
	repository: pathlib.Path, cut: typing.Callable[..., subprocess.CompletedProcess[str]]
) -> None:
	"""**The requirement, not a limitation.**

	Promoting an existing section means the changelog was written while the work was fresh, by
	whoever did it. Inserting an empty heading instead would let a release ship saying nothing,
	which is the failure 0.1.0 nearly had — its section still read *Unreleased* on the day.
	"""

	changelog = repository / "CHANGELOG.md"
	changelog.write_text(
		CHANGELOG.replace("## Unreleased", "## 0.1.0 — 2026-08-01"), encoding="utf-8"
	)

	_git(repository, "commit", "-qam", "nothing waiting to go out")

	done = cut("0.1.1")

	assert done.returncode == 1
	assert "no '## Unreleased' section" in done.stderr


def test_an_empty_unreleased_section_is_refused (
	repository: pathlib.Path, cut: typing.Callable[..., subprocess.CompletedProcess[str]]
) -> None:
	"""`#243`. Right guard, and it used to be on the wrong side of the irreversible step.

	`release_notes.py` refuses an empty section too — but that runs in the job *after* the
	upload, so a heading with nothing under it passed here, published to PyPI, and then failed
	on the way to the GitHub release, having already spent a version number that cannot be
	reused. Caught here it costs nothing.

	Told apart from a *missing* section on purpose: one looks like somebody started and the
	other like nobody did, and the remedy is worded differently for each.
	"""

	changelog = repository / "CHANGELOG.md"
	changelog.write_text(
		CHANGELOG.replace("### Fixed\n\n- Something that was wrong is now right.\n", ""),
		encoding="utf-8",
	)

	_git(repository, "commit", "-qam", "a heading with nothing under it")

	done = cut("0.1.1")

	assert done.returncode == 1
	assert "is empty" in done.stderr
	assert not _git(repository, "tag", "--list", "v0.1.1").strip()


def test_a_release_below_the_plugin_manifest_is_refused (
	repository: pathlib.Path, cut: typing.Callable[..., subprocess.CompletedProcess[str]]
) -> None:
	"""`#396`. The plugin's version leads, and a release may not walk it backwards.

	The two numbers do different jobs at different frequencies. **The plugin's is a cache
	key** — Claude Code stores an installed copy under it, so it must move on any change under
	`plugins/` or the artefact cannot be delivered at all, which was met twice as `#380` and
	`#393`. The package's is a *release*, an act with a changelog behind it, and tagging every
	plugin bump would make a release mean nothing.

	So the manifest runs ahead between releases and the next release takes the number it has
	reached. **Cutting below it would publish a plugin version somebody already has cached** —
	`#380` with the numbers reversed, and no way for anybody to notice, because the install
	would report success and change nothing.

	A skipped package version is cheap. That is the trade, stated.
	"""

	manifest = repository / "plugins" / "subroutine" / ".claude-plugin" / "plugin.json"
	carried = json.loads(manifest.read_text(encoding="utf-8"))
	carried["version"] = "0.3.0"
	manifest.write_text(json.dumps(carried, indent=2) + "\n", encoding="utf-8")
	_git(repository, "commit", "-am", "bump the plugin between releases")

	done = cut("0.2.0")

	assert done.returncode == 1
	assert "behind 0.3.0" in done.stderr
	assert "cached under that number" in done.stderr
	assert not _git(repository, "tag", "--list", "v0.2.0").strip(), "and nothing was tagged"


def test_a_release_matching_the_plugin_manifest_is_allowed (
	repository: pathlib.Path, cut: typing.Callable[..., subprocess.CompletedProcess[str]]
) -> None:
	"""Equal is the ordinary case, not a near miss.

	The manifest reaching the number first is exactly how this is meant to work — it is bumped
	when the plugin changes, and the release then takes it. A guard that refused equality would
	refuse every release cut the intended way.
	"""

	manifest = repository / "plugins" / "subroutine" / ".claude-plugin" / "plugin.json"
	carried = json.loads(manifest.read_text(encoding="utf-8"))
	carried["version"] = "0.1.1"
	manifest.write_text(json.dumps(carried, indent=2) + "\n", encoding="utf-8")
	_git(repository, "commit", "-am", "bump the plugin between releases")

	done = cut("0.1.1")

	assert done.returncode == 0, done.stderr
