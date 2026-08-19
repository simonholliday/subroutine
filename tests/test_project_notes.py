"""What ``CLAUDE.md`` says about this repository has to be something that is still true.

`#926`. **It is loaded automatically at the start of every session, it is 466 KB, and until
this file nothing read a byte of it.** ``tests/test_references.py`` names it, but only as a
path that must *not* be cited by tracked files — the ratchet is about who points at it, never
about what it says. So it was simultaneously the most-read document in the project and the only
one with no guard at all.

**Measured on 2026-08-16, three wrong records in one pass**, the worst of which said the old
specification file still existed on disk as a second copy and must not be deleted. It had been
deleted on 2026-08-04, and the same document said so four paragraphs earlier. A hand pass the
day before found two more across 455 refs. So the rate is roughly one wrong record per pass, and
every pass has been somebody happening to look.

**What this can check, and what it deliberately cannot.**

The file records five kinds of claim (`#926` lists them in order of how fast they rot) and only
two are answerable from the repository alone: a path, and a value copied from a tracked source.
The other three — a superlative, an asserted item status, an unresolvable ref — are claims about
the *instance*, and answering them would mean a test that makes network calls to a live server.
That is a worse trade than leaving them: a guard that is slow and can fail for reasons unrelated
to the code is one somebody turns off.

**So this is the local half, honestly bounded**, and `#926`'s own closing sentence says why that
is most of the value anyway: *everything on this page that is a value rots and everything that
is a pointer does not*. The cheapest fix for a rotting line is usually to replace it with the
query that regenerates it, and a guard is for what cannot be turned into a query.

**The known objection, from the item, and it is real**: the file is ``.gitignore``d, so this
runs on exactly the machines that have it and skips in CI for ever. It runs where the file is
*edited*, which is where the damage is done, and a skip elsewhere costs nothing because
elsewhere has no file to be wrong. If `#411` is ever decided the other way it becomes an
ordinary guard and the objection evaporates.
"""

import pathlib
import re
import tomllib

import pytest

import subroutine.db.migrate

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOTES = ROOT / "CLAUDE.md"
PYPROJECT = ROOT / "pyproject.toml"

#: Where a path named in the notes may be rooted.
#:
#: The notes are written from wherever the reader is standing, so ``domain/tasks.py`` and
#: ``src/subroutine/domain/tasks.py`` both appear and both mean the same file. Only the
#: prefixed spellings are checked (see :data:`_PREFIXES`); these are the bases they resolve
#: against, because ``migrations/env.py`` is real and lives under the package.
_BASES = (ROOT, ROOT / "src" / "subroutine", ROOT / "src" / "subroutine" / "db")

#: What makes a backticked span a claim about a file rather than prose that looks like one.
#:
#: **Scoped by running it rather than by reasoning about it**, which is this repository's own
#: rule and was worth every minute here: the obvious version — anything with a slash or a file
#: extension — flagged **153 of 223** spans, and almost every one was a false positive.
#: ``Europe/London``, ``/v1/tasks/next``, ``text/html``, ``160.79.104.0/21``, ``11/11``,
#: ``server/discover`` and ``substation/dist`` are a timezone, a route, a media type, a network,
#: a milestone count, a protocol method and a project address. A guard with that error rate is
#: one nobody can leave switched on.
_PREFIXES = (
	"src/",
	"tests/",
	"docs/",
	"scripts/",
	"plugins/",
	"migrations/",
	".github/",
	".claude-plugin/",
	"hooks/",
)

#: Files at the repository root that are named without a directory.
_ROOT_FILES = frozenset(
	{
		"pyproject.toml",
		"README.md",
		"CHANGELOG.md",
		"CONTRIBUTING.md",
		"CLA.md",
		"LICENSE",
		".gitignore",
	}
)

#: What a path's last segment may end in.
#:
#: This is what separates ``tests/test_packaging.py`` from ``tests/test_packaging._offered`` —
#: the second is a *member* of a module, written with a slash because that is how the notes
#: address code, and it names no file. Four of the six apparent failures on the first real run
#: were this, so without it the guard reports offenders that are not.
_SUFFIXES = frozenset({".py", ".md", ".toml", ".json", ".js", ".css", ".yml", ".yaml", ".txt"})

#: How few paths would mean this has stopped reading the file.
#:
#: `#405`'s floor. The assertions below report *offenders*, so a scan that reads nothing reports
#: none and is indistinguishable from a clean file. Fifty-eight at the time of writing.
_FEWEST_PATHS = 40

pytestmark = pytest.mark.skipif(
	not NOTES.exists(),
	reason=(
		"CLAUDE.md is not in the repository (`#411`), so this runs where the file is edited "
		"and nowhere else — which is where the damage is done"
	),
)


def _spans (text: str) -> set[str]:
	"""Return every backticked span, which is how the notes mark anything they name."""

	return set(re.findall(r"`([^`\n]+)`", text))


def _named_paths (text: str) -> list[str]:
	"""Return the spans that are claims about a file in this repository."""

	found = set()

	for span in _spans(text):
		if not re.fullmatch(r"[A-Za-z0-9_./-]+", span):
			continue

		if span in _ROOT_FILES:
			found.add(span)

			continue

		if not span.startswith(_PREFIXES):
			continue

		last = span.rstrip("/").rsplit("/", 1)[-1]

		if "." not in last or f".{last.rsplit('.', 1)[-1]}" in _SUFFIXES:
			found.add(span)

	return sorted(found)


def _current_section (text: str) -> str:
	"""Return the topmost dated section, which is the only one making claims about now.

	The notes carry a *"Where we were"* section per arc, and an old one naming an old schema
	head is a correct record rather than a stale claim. Checking every occurrence would refuse
	the file's own history, so only the newest section is held to what is true today.
	"""

	starts = [match.start() for match in re.finditer(r"^## Where we ", text, re.MULTILINE)]

	if not starts:
		return text

	return text[starts[0] : starts[1]] if len(starts) > 1 else text[starts[0] :]


def _standing (text: str) -> str:
	"""Return the parts of the notes that are claims about now rather than a record of then.

	**Two thirds of this file is dated history and it must not be searched for a current
	value.** The licence, the product's own description and the practice section all appear in
	the standing text once and in old sections several times — so asking whether a value appears
	*anywhere* is answered by any one of those, and the check passes over a header that has been
	reworded into something false.

	Found by falsifying: replacing the first occurrence of the licence left this green, because
	three more were sitting in sections describing 2026-08-08. A guard nothing can falsify is
	the shape this repository keeps finding, and it took planting the defect to see it.

	The standing text is what comes before the first dated section, plus everything from the
	first *undated* heading after the last one — the opening statement of what this project is,
	and the rules and traps at the end.
	"""

	starts = [match.start() for match in re.finditer(r"^## Where we ", text, re.MULTILINE)]

	if not starts:
		return text

	after = re.search(r"^## (?!Where we )", text[starts[-1] :], re.MULTILINE)
	tail = text[starts[-1] + after.start() :] if after else ""

	return text[: starts[0]] + tail


def test_every_repository_path_it_names_exists () -> None:
	"""`#926`. The class of error that was actually measured, and the worst of the three.

	A note saying the old specification file still existed on disk, and must not be deleted,
	survived in a document loaded into every session — four paragraphs below the passage
	recording that it had been deleted. A path is the one claim here that is cheap to check and
	expensive to be wrong about, because it sends the next reader looking for something that is
	not there.

	**This guard caught itself on its first gate run**, which is worth recording: the file was
	still untracked, and `#626` had made the reference ratchet read untracked files an hour
	earlier — so it refused a draft of this docstring for naming that path. Before that morning
	the gate would have passed and the next run, after ``git add``, would have failed.

	`#446` built this ratchet for *tracked* files and scoped it to them deliberately, so this
	file was excluded by construction — the guard existed, and the document most in need of it
	was the one it could not see.
	"""

	text = NOTES.read_text(encoding="utf-8")
	named = _named_paths(text)

	assert len(named) >= _FEWEST_PATHS, (
		f"only {len(named)} paths were found in CLAUDE.md, fewer than the {_FEWEST_PATHS} that "
		f"are there — this has stopped reading the file, and no offenders reads exactly like a "
		f"clean one"
	)

	missing = [
		name for name in named if not any((base / name).exists() for base in _BASES)
	]

	assert not missing, (
		f"CLAUDE.md names files that are not there: {missing}. Either the path moved and the "
		f"note did not, or the note is describing something that has been deleted — which is "
		f"the failure this was written for."
	)


def test_the_values_it_copies_from_a_tracked_source_still_match () -> None:
	"""`#926`'s third class: a value duplicated out of ``pyproject.toml``.

	**Every published surface is already held to that file** — the marketplace, both plugin
	manifests, the README standfirst, the CLI's help and the API's summary — and this one was
	outside all of them.

	**Asked of the standing text rather than of the whole file**, and the first version was not:
	falsifying it by rewriting the licence left it green, because three more copies were sitting
	in sections dated 2026-08-08. See :func:`_standing`. It is the source of the sentence at the top of this very document, and
	that sentence stayed wrong here for three days after `#731` changed it, which is recorded in
	the file's own opening paragraph.
	"""

	standing = _standing(NOTES.read_text(encoding="utf-8"))
	declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]

	# The description carries a full stop in `pyproject.toml` and is quoted in prose without
	# one, so the sentence rather than the field is what has to appear.
	wanted = {
		"the licence": declared["license"],
		"the product's one-line description": declared["description"].rstrip("."),
	}
	absent = sorted(name for name, value in wanted.items() if value not in standing)

	assert not absent, (
		f"CLAUDE.md no longer carries {absent}, which pyproject.toml declares. Either it was "
		f"changed there and not here, or the note has been reworded past recognition — and "
		f"this file is the one nothing else checks."
	)


def test_the_schema_head_it_reports_is_the_one_this_code_is_at () -> None:
	"""`#926`'s third class again, from the source that moves most often.

	The notes open every arc by saying whether the schema moved, because a reader has to know
	whether a migration is waiting. A stale head there is worse than none: it says *no migration
	since* about a database that needs one.

	**Only the newest section**, because the older ones are a record of what was true then and
	naming an old head in one is correct. Held against ``head_revision()`` rather than a
	literal, so the migration that moves it is what makes this fail.
	"""

	current = _current_section(NOTES.read_text(encoding="utf-8"))
	live = subroutine.db.migrate.head_revision()

	# `head_revision` is typed as optional because a tree with no migrations has no head. This
	# one has thirteen, and a run where that stopped being true is a different failure.
	assert live is not None, "this package ships migrations, so there is a head to compare"

	assert live in current, (
		f"the newest section of CLAUDE.md does not name the current schema head {live!r}. It "
		f"names {sorted(set(re.findall(r'[0-9a-f]{12}', current)))}, so either a migration has "
		f"landed since that section was written or the section is about an older tree."
	)
