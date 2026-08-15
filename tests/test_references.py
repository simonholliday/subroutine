"""A path this repository deliberately does not contain may not gain new mentions.

``SPEC.md`` and ``CLAUDE.md`` are ``.gitignore``d by decision, and the repository referred to
them 464 times anyway — 446 and 18 — in code comments, docstrings and tests. Every one is a
dangling pointer for anybody who clones this, and ten of them reached a *published* page:
``docs/errors.md`` is generated, describes a public semver'd contract, and told its reader to
consult a file that will never be there.

**The existing references are dead and stay dead** (Simon, 2026-08-04): rewriting 464 comments
buys nothing, because the specification they cite now lives in the instance under refs a comment
would have to be rewritten again to name. What is worth stopping is the *next* one, so this is a
ratchet rather than a repair. Each ceiling may fall and may never rise.

The rule this failed for a year was the ordinary one: nothing checked. ``#446``.

**Two of those 18 were never dangling** (``#546``): the plugins' skill names ``CLAUDE.md``
beside ``AGENTS.md`` as *the reader's* agent file, about a project that is not this one. So
the ceiling has come down to 16 and those mentions are excused by name and by file — which is
what ``counted_against`` is for, and why the exemption is not a directory.

Deliberately not a general dead-link checker. Measured before choosing: matching every
``*.md`` mention across the tree finds 481 dead ones, of which about a dozen are example
filenames in tests, a partial URL, and a file referred to by its basename — noise that would
have this switched off inside a month. ``tests/test_documentation.py`` covers the other
direction, relative *links* on the published pages, and the two do not overlap: a link is
``](target)`` on three pages, a mention is bare prose anywhere.
"""

import pathlib
import re
import subprocess
import typing

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: This module, which must be allowed to name the paths it exists to police. Excluded by name
#: rather than by pattern so that a second guard cannot quietly inherit the exemption.
SELF = pathlib.Path(__file__).resolve().relative_to(ROOT).as_posix()


class Absent (typing.NamedTuple):
	"""A path the repository does not contain, with the mentions it is still allowed."""

	#: How many mentions exist today. May be lowered, never raised.
	ceiling: int

	#: Why the file is not here, so that removing this entry is a decision somebody can check.
	why: str

	#: Files where naming this is about *the reader's* project rather than about this one, so
	#: the mention resolves for them and dangles for nobody (`#546`). Keyed by path with the
	#: reason beside it, and checked by :func:`test_no_generic_mention_is_excused_wrongly`.
	generic_in: tuple[tuple[str, str], ...] = ()


#: Every path named in tracked files that is deliberately not in the repository.
#:
#: **What makes an entry go away**: the file joining the repository, at which point the mentions
#: resolve and the entry is wrong rather than merely unnecessary — which
#: :func:`test_every_absent_path_is_still_absent` fails on, so it cannot be left behind.
ABSENT: dict[str, Absent] = {
	"SPEC.md": Absent(
		ceiling=441,
		why=(
			"Moved into the instance on 2026-08-04 as 25 documents under the SPEC project, "
			"index #472, and deleted from disk. Was never in the repository."
		),
	),
	"CLAUDE.md": Absent(
		ceiling=16,
		why=(
			"Stays a file and stays out of the repository (#411). It is loaded from a known "
			"path at session start, which an instance document would not be."
		),
		generic_in=(
			(
				"plugins/subroutine/skills/subroutine/SKILL.md",
				"Advice about the reader's own project — the agent file to write a pointer "
				"into, named beside AGENTS.md and a contributing guide. That file is theirs "
				"and it is there.",
			),
			(
				"plugins/subroutine-remote/skills/subroutine/SKILL.md",
				"The same skill. A plugin is self-contained, so the practice ships twice and "
				"test_the_two_plugins_carry_the_same_skill requires the copies to be identical.",
			),
		),
	),
}

#: A path that *is* in the repository, counted so that a scan reading nothing cannot pass. A
#: ceiling test is satisfied most comfortably by a walk that found no files at all, which is the
#: one thing a ratchet is structurally unable to notice about itself.
PRESENT = "docs/errors.md"


def tracked (root: pathlib.Path = ROOT) -> list[str]:
	"""Return every file git tracks, as repository-relative paths.

	Takes the tree as an argument so that a test can point it at a synthetic one. A scanner
	that can only ever read the real repository cannot be shown a planted offender, and this
	project has twice shipped a guard that was checking almost nothing.
	"""

	found = subprocess.run(
		["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
	)

	return found.stdout.split()


def _pattern (name: str) -> re.Pattern[str]:
	"""Return the pattern matching a bare mention of ``name``.

	The lookbehind keeps a longer path ending in the same basename from counting — a URL like
	``…/blob/main/docs/errors.md`` should not be read as a mention of ``errors.md``.
	"""

	return re.compile(r"(?<![\w/.-])" + re.escape(name) + r"\b")


def mentions (name: str, root: pathlib.Path = ROOT, skip: str | None = SELF) -> dict[str, int]:
	"""Return how many times each tracked file names ``name``, omitting files that never do."""

	pattern = _pattern(name)
	counted: dict[str, int] = {}

	for relative in tracked(root):
		if relative == skip:
			continue

		try:
			text = (root / relative).read_text(encoding="utf-8")

		except (OSError, UnicodeDecodeError):
			# A binary or unreadable file cannot mention anything. Not an error: the tree
			# holds images and a font, and refusing them would make this a file-type list.
			continue

		found = len(pattern.findall(text))

		if found:
			counted[relative] = found

	return counted


def counted_against (name: str, root: pathlib.Path = ROOT) -> dict[str, int]:
	"""Return the mentions of ``name`` that are pointers into *this* repository.

	**A filename is not always a path here** (`#546`). The plugins' skill tells an agent to
	write a pointer into "the project's agent file — ``CLAUDE.md``, ``AGENTS.md``, whichever it
	already uses", which names a well-known filename as a *category*, about a project that is
	not this one. That file exists for the reader, so it dangles for nobody, and counting it
	made the ratchet report a spelling rather than the thing it cares about.

	**Excused per name and per file, never per directory.** A skill citing ``SPEC.md §6.13``
	*would* be a dead pointer, and on the most public surface this repository has — excluding
	``plugins/*/skills/`` wholesale would stop counting it exactly where it matters most.
	``CLAUDE.md`` and ``AGENTS.md`` are files the reader has; ``SPEC.md`` is ours alone.
	"""

	excused = {path for path, _ in ABSENT[name].generic_in}

	return {
		path: found for path, found in mentions(name, root).items() if path not in excused
	}


def test_no_new_reference_to_an_absent_path () -> None:
	"""Every mention of a deliberately missing file is one that was already there.

	**Exact rather than "no more than"**, which is the ratchet's whole mechanism and not
	pedantry. A ceiling left above the real count is slack nobody granted, re-spendable by the
	next person to add a reference — so the count coming *down* has to move the ceiling with
	it. Written as one assertion for a reason: as two tests, the ``<=`` half could never fail
	while the ``==`` half held, and a test that cannot fail is what this file exists to stop.
	"""

	for name, absent in ABSENT.items():
		total = sum(counted_against(name).values())

		if total > absent.ceiling:
			raise AssertionError(
				f"{name} is named {total} times and {absent.ceiling} were allowed. It is not in "
				f"this repository and will not be: {absent.why} A reader who clones this cannot "
				f"follow the reference. Cite the instance instead — the specification is under "
				f"the SPEC project, index #472 — or say the thing rather than pointing at it."
			)

		assert total == absent.ceiling, (
			f"{name} is named {total} times now, and ABSENT still allows {absent.ceiling}. "
			f"Lower the ceiling to {total}, so the difference cannot be spent again."
		)


def test_no_generic_mention_is_excused_wrongly () -> None:
	"""An exemption naming a file that does not mention the name is measuring nothing.

	`#405`'s question of every allow-list here: what makes an entry go away? This one goes when
	the prose changes — the skill stops naming an agent file, or the second plugin stops
	shipping a copy — and until something fails on that, ``generic_in`` is a place to park an
	excuse nobody can delete. It is the shape `#500` found in ``UNREACHED_FIELDS``, where a
	written reason cited an item that had closed five days earlier.
	"""

	present = set(tracked())

	for name, absent in ABSENT.items():
		found = mentions(name)

		for path, why in absent.generic_in:
			assert path in present, (
				f"ABSENT[{name!r}] excuses {path}, which git does not track. Delete the entry."
			)

			assert path in found, (
				f"ABSENT[{name!r}] excuses {path}, which no longer names {name}. Delete the "
				f"entry and lower the ceiling, or the exemption is spendable again."
			)

			assert why.strip(), f"ABSENT[{name!r}]'s entry for {path} gives no reason"


def test_every_absent_path_is_still_absent () -> None:
	"""An entry naming a file that has since arrived is stale and must go.

	`#405` went round every allow-list in this repository asking what makes an entry go away.
	This is the answer for this one, and without it a file could join the repository while a
	test went on insisting its mentions were broken.

	**Tracked, not present on disk**, and the difference is the whole point: ``CLAUDE.md`` sits
	in this working tree right now and is still unreachable to anybody who clones. Written the
	other way first, and it failed on exactly that — which is the distinction this file is
	about, so it was worth meeting head-on rather than reasoning around.
	"""

	present = set(tracked())

	for name in ABSENT:
		assert name not in present, (
			f"{name} is tracked by git now, so its mentions resolve for anybody who clones "
			f"this. Delete its entry from ABSENT — the ceiling is measuring nothing."
		)


def test_the_scan_reaches_the_repository () -> None:
	"""A walk that reads nothing satisfies every ceiling above, so prove it read something."""

	files = tracked()

	assert len(files) > 150, f"git ls-files returned {len(files)} files — has this stopped reaching the tree?"

	present = mentions(PRESENT)

	assert present, (
		f"{PRESENT} is in this repository and is named in it, and the scan found no mention of "
		f"it. The reading is broken, not the tree."
	)


def test_the_scanner_finds_a_planted_reference (tmp_path: pathlib.Path) -> None:
	"""Feed a synthetic offender through the real scanner, in a real git tree.

	Falsified against a tree this test builds rather than against a mutation of the guard: the
	half that fails silently is the *scan*, and a check written from the same assumptions as
	the scanner cannot see it. `#405`.
	"""

	subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
	planted = tmp_path / "module.py"
	planted.write_text("# See SPEC.md for why.\n", encoding="utf-8")
	subprocess.run(["git", "add", "module.py"], cwd=tmp_path, check=True)

	counted = mentions("SPEC.md", root=tmp_path, skip=None)

	assert counted == {"module.py": 1}, f"the scanner missed a planted mention: {counted}"

	untracked = tmp_path / "ignored.py"
	untracked.write_text("# See SPEC.md too.\n", encoding="utf-8")

	assert mentions("SPEC.md", root=tmp_path, skip=None) == {"module.py": 1}, (
		"an untracked file was counted — the scan is walking the directory rather than what "
		"git tracks, so anything in a build directory would be policed too"
	)
