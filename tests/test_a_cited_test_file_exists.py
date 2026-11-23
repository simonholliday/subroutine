"""A test file the source cites is one the tree holds - `SR#5344`.

Point 6 of L-5 of the cold review of 2026-11-11, decided in `#5285`. ``api/security.py`` cited
``tests/test_api_security.py`` and ``db/backup.py`` cited ``tests/test_backup.py``, and neither
file has ever existed: a citation is what a reader checks *instead of* the claim, and one pointing
at nothing tells them less than none would. ``tests/test_references.py`` declines a general
dead-link checker over the noise in the published pages; test-file citations in the source
measured none, so this checks them.
"""

import pathlib
import re

#: The repository, whose ``src`` and ``scripts`` are read.
ROOT = pathlib.Path(__file__).resolve().parent.parent

#: A test file named in prose: a module directly under ``tests/`` whose name starts ``test_``.
CITED = re.compile(r"tests/(test_[A-Za-z0-9_]+\.py)")

#: Citations to a test file that does not exist, each made on purpose, with why.
ON_PURPOSE = {
	("src/subroutine/permissions.py", "test_permissions.py"): (
		"history: the sentence says an earlier one named a file that does not exist (`#742`)"
	),
}


def _missing (root: pathlib.Path) -> set[tuple[str, str]]:
	"""Return each module under ``root``'s ``src`` or ``scripts``, with a test file it cites that
	``root/tests`` does not hold."""

	found = set()

	for top in ("src", "scripts"):
		for path in sorted((root / top).rglob("*.py")):
			for name in CITED.findall(path.read_text(encoding="utf-8")):
				if not (root / "tests" / name).is_file():
					found.add((path.relative_to(root).as_posix(), name))

	return found


def test_every_test_file_the_source_cites_exists () -> None:
	"""Every ``tests/test_*.py`` a module under ``src`` or ``scripts`` names is in the tree."""

	assert _missing(ROOT) - set(ON_PURPOSE) == set()


def test_every_citation_excused_is_still_made () -> None:
	"""An excuse goes once the citation it excuses does."""

	assert set(ON_PURPOSE) <= _missing(ROOT)


def test_the_scan_finds_a_citation_to_nothing (tmp_path: pathlib.Path) -> None:
	"""The scan above, fed a citation to a missing file through its own entry point."""

	for directory in ("src", "scripts", "tests"):
		(tmp_path / directory).mkdir()

	(tmp_path / "tests" / "test_here.py").write_text("", encoding="utf-8")
	(tmp_path / "src" / "cites.py").write_text(
		"# tests/test_here.py holds this, and tests/test_gone.py holds that.\n", encoding="utf-8"
	)

	assert _missing(tmp_path) == {("src/cites.py", "test_gone.py")}
