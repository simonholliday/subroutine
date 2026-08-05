"""The release check that a migration is advertised — SPEC.md §12.4a, item ``#100``.

``scripts/check_release_notes.py`` is a gate, and an untested gate is the exact shape of defect
this project keeps finding: a rule written down, believed, and enforced by nothing. Worse than
most, because it only ever runs on the day of a release — so a broken one is discovered by the
release it was supposed to protect.

What is worth testing is not "does it pass on this repository" but **does it fail when it
should**. Every test here sets up a history that ought to be refused and checks that it is,
including the two failures that would otherwise pass quietly: a directory the check cannot read
migrations out of, and a notice copied forward from the previous release with the old revision
still in it.

The repositories are built in ``tmp_path`` — local disk, and nothing to do with this one.
"""

import importlib.util
import pathlib
import subprocess
import types

import pytest

REPOSITORY = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPOSITORY / "scripts" / "check_release_notes.py"

#: A migration as Alembic writes one, reduced to the two lines the check reads. The annotations
#: are there because the real template has them and the pattern has to cope with them.
MIGRATION = '''"""{name}

Revision ID: {revision}
"""

revision: str = {revision!r}
down_revision: str | None = {down!r}


def upgrade () -> None:
	"""Do nothing at all."""
'''


@pytest.fixture(scope="module")
def check () -> types.ModuleType:
	"""Load the release check by path.

	It lives in ``scripts/`` rather than in the package, because it is release engineering and
	is not shipped to anybody. That keeps it off the import path, so it is loaded rather than
	imported — the alternative being to ship a gate to every user who will never run it.
	"""

	specification = importlib.util.spec_from_file_location("check_release_notes", SCRIPT)

	assert specification is not None and specification.loader is not None

	module = importlib.util.module_from_spec(specification)
	specification.loader.exec_module(module)

	return module


@pytest.fixture
def repository (tmp_path: pathlib.Path) -> pathlib.Path:
	"""Return an empty git repository with one commit, on local disk."""

	root = tmp_path / "release"
	root.mkdir()

	_git(root, "init", "--initial-branch", "main")
	_git(root, "config", "user.email", "test@example.com")
	_git(root, "config", "user.name", "Test")

	(root / "README.md").write_text("nothing here\n", encoding="utf-8")

	_git(root, "add", "-A")
	_git(root, "commit", "-m", "first")

	return root


def _git (root: pathlib.Path, *arguments: str) -> str:
	"""Run one git command inside a repository."""

	found = subprocess.run(
		["git", "-C", str(root), *arguments], capture_output=True, text=True, check=True
	)

	return found.stdout


def _add_migration (
	root: pathlib.Path, revision: str, down: str | None, versions: str
) -> None:
	"""Write one migration into a repository and commit it."""

	where = root / versions
	where.mkdir(parents=True, exist_ok=True)

	(where / f"{revision}_thing.py").write_text(
		MIGRATION.format(name="thing", revision=revision, down=down), encoding="utf-8"
	)

	_git(root, "add", "-A")
	_git(root, "commit", "-m", f"migration {revision}")


def test_the_head_at_a_ref_is_the_end_of_the_chain (
	check: types.ModuleType, repository: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Read out of git, so that "what was the head at that tag" can be asked at all."""

	monkeypatch.chdir(repository)

	_add_migration(repository, "aaaaaaaaaaaa", None, check.VERSIONS)
	_git(repository, "tag", "v0.1.0")

	_add_migration(repository, "bbbbbbbbbbbb", "aaaaaaaaaaaa", check.VERSIONS)

	assert check._head_at("v0.1.0") == "aaaaaaaaaaaa"
	assert check._head_at("HEAD") == "bbbbbbbbbbbb"


def test_a_ref_with_no_migrations_is_refused_rather_than_reported_as_unchanged (
	check: types.ModuleType, repository: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""**The failure that would otherwise pass quietly.**

	If the migrations directory ever moves, every ref looks empty — and an empty answer
	compared against another empty answer is "the schema did not change", which is a release
	shipping without its warning. A check that cannot see the thing it is checking must say so.
	"""

	monkeypatch.chdir(repository)

	with pytest.raises(SystemExit) as refused:
		check._head_at("HEAD")

	assert "No migrations found" in str(refused.value)


def test_a_branched_history_is_refused_rather_than_guessed (
	check: types.ModuleType, repository: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Two ends means two answers, and picking one would be picking at random."""

	monkeypatch.chdir(repository)

	_add_migration(repository, "aaaaaaaaaaaa", None, check.VERSIONS)
	_add_migration(repository, "bbbbbbbbbbbb", "aaaaaaaaaaaa", check.VERSIONS)
	_add_migration(repository, "cccccccccccc", "aaaaaaaaaaaa", check.VERSIONS)

	with pytest.raises(SystemExit) as refused:
		check._head_at("HEAD")

	assert "needs a person to look at it" in str(refused.value)


def test_a_release_that_moves_the_schema_without_a_notice_is_refused (
	check: types.ModuleType,
	repository: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""End to end, and the whole reason the script exists."""

	_prepare(check, repository, monkeypatch)
	(repository / "CHANGELOG.md").write_text("# Changelog\n\n## 0.2.0\n\nFaster.\n", "utf-8")

	assert check.main([]) == 1
	assert "missing" in capsys.readouterr().err


def test_the_same_release_passes_once_the_notice_is_there (
	check: types.ModuleType,
	repository: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""And the wording ``--emit`` prints is wording the check accepts.

	Those are two halves of one promise. A generator whose output its own checker rejects is
	worse than having neither, because the person following the instructions is the one who
	finds out.
	"""

	_prepare(check, repository, monkeypatch)
	(repository / "CHANGELOG.md").write_text(
		f"# Changelog\n\n## 0.2.0\n\n{check._notice('aaaaaaaaaaaa', 'bbbbbbbbbbbb')}\n",
		"utf-8",
	)

	assert check.main([]) == 0
	assert "carries the migration notice" in capsys.readouterr().out


def test_a_notice_left_over_from_the_previous_release_is_refused (
	check: types.ModuleType,
	repository: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""**The one a human review misses**, which is why the check reads the revision.

	Copied forward, still correctly worded, still naming a command that exists — and describing
	the *previous* release's migration. Everything about it looks right except the number.
	"""

	_prepare(check, repository, monkeypatch)
	(repository / "CHANGELOG.md").write_text(
		f"# Changelog\n\n## 0.2.0\n\n{check._notice(None, 'aaaaaaaaaaaa')}\n", "utf-8"
	)

	assert check.main([]) == 1
	assert "bbbbbbbbbbbb" in capsys.readouterr().err


def test_a_release_that_does_not_move_the_schema_needs_no_notice (
	check: types.ModuleType,
	repository: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""Otherwise the notice appears on every release and stops being read."""

	monkeypatch.chdir(repository)
	_add_migration(repository, "aaaaaaaaaaaa", None, check.VERSIONS)
	_git(repository, "tag", "v0.1.0")

	monkeypatch.setattr(
		check.subroutine.db.migrate, "head_revision", lambda: "aaaaaaaaaaaa"
	)

	assert check.main([]) == 0
	assert "unchanged" in capsys.readouterr().out


def _prepare (
	check: types.ModuleType, repository: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Build a repository tagged at one migration and carrying a second, uncut.

	The head *now* comes from Alembic in the real script, which is the asymmetry that keeps a
	parser bug failing safe. Here it is the value the second migration declares, so these tests
	exercise the comparison rather than this repository's own history.
	"""

	monkeypatch.chdir(repository)

	_add_migration(repository, "aaaaaaaaaaaa", None, check.VERSIONS)
	_git(repository, "tag", "v0.1.0")

	_add_migration(repository, "bbbbbbbbbbbb", "aaaaaaaaaaaa", check.VERSIONS)

	monkeypatch.setattr(
		check.subroutine.db.migrate, "head_revision", lambda: "bbbbbbbbbbbb"
	)
	monkeypatch.setattr(check, "CHANGELOG", pathlib.Path("CHANGELOG.md"))


def test_the_check_passes_on_this_repository_as_it_stands (
	check: types.ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Because CI runs exactly this, and a gate nobody can satisfy gets switched off.

	No arguments and no fixtures: whatever the state of the working tree, ``main()`` has to
	reach a verdict rather than raise.
	"""

	assert check.main([]) == 0
	assert capsys.readouterr().out.strip()


@pytest.mark.parametrize(
	("text", "expected"),
	[
		("# Title\n\n## First\n\nbody\n\n## Second\n\nmore\n", ("First", "\nbody\n")),
		("# Title\n\nno sections at all\n", None),
	],
)
def test_the_topmost_section_is_the_release_being_prepared (
	check: types.ModuleType, text: str, expected: tuple[str, str] | None
) -> None:
	"""Topmost rather than "the one matching pyproject", which would be a second thing to keep
	in step — and the answer to "what is about to ship" is the top of the file in every
	changelog anybody writes."""

	found = check._first_section(text)

	assert found == expected


def test_the_notice_names_the_command_that_does_it_safely (check: types.ModuleType) -> None:
	"""``subroutine upgrade``, not ``db upgrade``.

	The blunt one migrates and nothing else. The point of the notice is that somebody who has
	just installed a release takes a backup first, and that is the difference between the two.
	"""

	notice = check._notice("aaaaaaaaaaaa", "bbbbbbbbbbbb")

	assert "subroutine db upgrade" in notice
	assert "backup" in notice
	assert "down for the length of the migration" in notice
