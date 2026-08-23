"""A path this repository deliberately does not contain may not gain new mentions.

``SPEC.md`` and ``CLAUDE.md`` are ``.gitignore``d by decision, and the repository referred to
them 464 times anyway — 446 and 18 — in code comments, docstrings and tests. Every one is a
dangling pointer for anybody who clones this, and ten of them reached a *published* page:
``docs/errors.md`` is generated, describes a public semver'd contract, and told its reader to
consult a file that will never be there.

**The ``SPEC.md`` half of that is over** (`#945`, Simon's decision of 2026-08-17). The
specification is at ``docs/design.md`` and ships with the code it describes, so the 438 mentions
were rewritten to name it and the 1,959 bare ``§n.m`` citations resolve for the first time. The
three left are that document's own account of its former name.

**Which reverses the reasoning this file was written on**, and the reversal is worth reading
rather than deleting. It said (Simon, 2026-08-04) that *rewriting 464 comments buys nothing,
because the specification they cite now lives in the instance under refs a comment would have to
be rewritten again to name*. That was true of every fix available then. It stopped being true
when a third became available — publish the referent, and every citation resolves without a
comment being touched. **A ratchet is the right answer while the thing pointed at is out of
reach, and the wrong one once it can be brought into reach.**

``CLAUDE.md`` stays out and stays ratcheted: it is loaded from a known path at session start,
which is the whole of what makes it work, and an instance document or a published file would not
be. Each ceiling may fall and may never rise.

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

import ast
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

	#: Files whose text is a frozen historical record, so their mentions are a closed set that
	#: cannot grow (`#945`). Separate from :attr:`generic_in`, which means something else — a
	#: filename naming *the reader's* file — and putting these under that name would have been
	#: an excuse filed against a reason that is not the reason.
	#:
	#: **This exists because a ceiling may never rise and one had to.** Publishing
	#: ``docs/design.md`` took ``CLAUDE.md`` from 16 mentions to 25 without anybody writing a
	#: reference: a file *joined* the repository already containing nine, in text that is
	#: frozen and therefore cannot gain a tenth. The ratchet cannot tell that from somebody
	#: adding one, and the difference is the whole of what it is for.
	#:
	#: Held to the same checks as ``generic_in`` by
	#: :func:`test_no_generic_mention_is_excused_wrongly`: the file must be tracked, must still
	#: name the path, and must carry a reason.
	frozen_in: tuple[tuple[str, str], ...] = ()


#: Every path named in tracked files that is deliberately not in the repository.
#:
#: **What makes an entry go away**: the file joining the repository, at which point the mentions
#: resolve and the entry is wrong rather than merely unnecessary — which
#: :func:`test_every_absent_path_is_still_absent` fails on, so it cannot be left behind.
ABSENT: dict[str, Absent] = {
	"SPEC.md": Absent(
		ceiling=0,
		why=(
			"The content is here — docs/design.md — and only this filename is not. It was "
			"SPEC.md until 2026-08-04, then 25 documents in the instance, then one published "
			"file (#945). Nothing outside that document names it any more."
		),
		frozen_in=(
			(
				"docs/design.md",
				"The published design document, describing the repository as it was planned. "
				"Its text is frozen (#945), so these mentions are a closed set.",
			),
		),
	),
	"CLAUDE.md": Absent(
		# **Raised from 16 to 23 for `#926`**, and the case is the same shape as the browser
		# suite's cap: the guard that reads CLAUDE.md has to name it. Seven mentions in
		# `tests/test_project_notes.py` — the module docstring saying what it checks, the
		# constants saying where, and the skip saying why it does not run in CI.
		#
		# **The ratchet was right to ask.** Everything else that has ever pushed this number up
		# was a reference a reader could not follow, and this is the first that is a *subject*
		# rather than a pointer: the file is what that module is about, so naming it is the
		# opposite of leaving a dangling reference. That distinction is not derivable, which is
		# why it is written here rather than pattern-matched.
		ceiling=22,
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
			(
				"src/subroutine/cli/personal.py",
				"`setup claude` looks for the reader's own agent file and names both "
				"candidates as data rather than as a citation — the same distinction "
				"ILLUSTRATIVE draws for item refs, where only reading tells a demonstration "
				"from a dead pointer.",
			),
		),
		frozen_in=(
			(
				"docs/design.md",
				"The published design document, describing the repository as it was planned. "
				"Its text is frozen (#945), so these mentions are a closed set.",
			),
		),
	),
}

#: A path that *is* in the repository, counted so that a scan reading nothing cannot pass. A
#: ceiling test is satisfied most comfortably by a walk that found no files at all, which is the
#: one thing a ratchet is structurally unable to notice about itself.
PRESENT = "docs/errors.md"


def tracked (root: pathlib.Path = ROOT) -> list[str]:
	"""Return every file bound for the repository, as repository-relative paths.

	Takes the tree as an argument so that a test can point it at a synthetic one. A scanner
	that can only ever read the real repository cannot be shown a planted offender, and this
	project has twice shipped a guard that was checking almost nothing.

	**Untracked files count, and leaving them out is what `#626` was** (`--others
	--exclude-standard`). ``git ls-files`` alone answers *what is committed*, and the ratchet's
	question is *what is about to be* — so a new module was invisible here until it was staged.
	Measured: the gate reported seven steps green, ``git add`` and ``git commit`` followed, and
	the very next run of the same command failed on a file that had not changed. `df5369f` sat
	red in the history because of it.

	**That is `c35a64b`'s lesson in a disguise the recorded version does not cover.** The rule
	people take from it is *run the gate just before committing* — which is exactly the moment a
	new file is still untracked, so the gate is at its blindest when it is trusted most.

	``--exclude-standard`` is what keeps this honest rather than merely wider: anything
	``.gitignore`` covers stays out, so a scratch file, ``CLAUDE.md`` and the session's own
	notes cannot fail somebody's build. What is left is a file that is not ignored and not yet
	added, which is a file on its way in.
	"""

	found = subprocess.run(
		["git", "ls-files", "--cached", "--others", "--exclude-standard"],
		cwd=root,
		capture_output=True,
		text=True,
		check=True,
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

	entry = ABSENT[name]
	excused = {path for path, _ in (*entry.generic_in, *entry.frozen_in)}

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
				f"this repository under that name: {absent.why} A reader who clones this cannot "
				f"follow the reference. The specification is published at docs/design.md — cite "
				f"that, or say the thing rather than pointing at it."
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

		for path, why in (*absent.generic_in, *absent.frozen_in):
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

	# **A file that is not yet added is still on its way in** (`#626`). Leaving it out is what
	# let a new module cross this ratchet: the gate passed, `git add` and `git commit`
	# followed, and the next run of the same command failed on a file that had not changed.
	coming = tmp_path / "new_module.py"
	coming.write_text("# See SPEC.md as well.\n", encoding="utf-8")

	assert mentions("SPEC.md", root=tmp_path, skip=None) == {
		"module.py": 1,
		"new_module.py": 1,
	}, "a file that is not ignored and not yet added was missed, which is `#626` exactly"

	# **And the reason this stayed narrow for so long is a real one**, so it is asserted rather
	# than dropped: a scan walking the directory would police a build directory, somebody's
	# virtualenv, and every note they had not meant to publish. `--exclude-standard` is what
	# separates the two — ignored is out, not-yet-added is in.
	(tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
	(tmp_path / "build").mkdir()
	(tmp_path / "build" / "generated.py").write_text("# See SPEC.md.\n", encoding="utf-8")

	assert "build/generated.py" not in mentions("SPEC.md", root=tmp_path, skip=None), (
		"an ignored file was counted — anything under a build directory would fail the build"
	)


# --- Items in this instance are not addresses a reader of the source has ---------------------
#
# `#944`, Simon's decision of 2026-08-17, option C. The repository cites this project's own
# backlog about 4,600 times, and every one of those is a number only somebody with an account on
# a private instance can resolve. **Almost all of them are footnotes** — the sentence beside them
# says the rule, so a stranger loses a pointer rather than the reasoning — and stripping 4,600
# comments would cost more than it buys, remove the trail from code to decision, and risk the
# thing below.
#
# **So the line is drawn where somebody actually reads it**: what the API publishes, and what a
# person or an agent is handed. Those must cite nothing they cannot follow. Everything else keeps
# its footnotes, and the convention that makes them harmless is stated rather than tested —
# *state the rule, then cite it*.

#: The pages a reader is handed. ``docs/design.md`` is deliberately absent: it is frozen
#: (`#945`), so its citations are a closed set that cannot grow, and it is registered under
#: :attr:`Absent.frozen_in` for the same reason.
PUBLISHED_PAGES: tuple[str, ...] = (
	"README.md",
	"CHANGELOG.md",
	"CONTRIBUTING.md",
	"SECURITY.md",
	"docs/errors.md",
	"docs/hosting.md",
	"docs/connecting.md",
	"plugins/subroutine/skills/subroutine/SKILL.md",
	"plugins/subroutine-remote/skills/subroutine/SKILL.md",
)

#: Refs a published page may name, because on these pages a number is the **product**
#: demonstrating its own central concept rather than a pointer at our backlog.
#:
#: **This distinction is the whole difficulty and it cannot be drawn from the digits.** `#42` in
#: *"`#42` is the same task tomorrow"* teaches what a ref is; `#245` in *"macOS and Windows are
#: `#245`"* was a dead pointer on the page people read while upgrading. Both are backticked, both
#: are integers, and only reading tells them apart — which is why this is a register with reasons
#: rather than a pattern.
#:
#: A number here is a claim that it is an *example*. Adding one is a decision; the alternative is
#: nearly always to say the thing rather than point at it.
ILLUSTRATIVE: dict[int, str] = {
	1: "the first item in every transcript on every page — 'Call the dentist', 'Pay the gas "
	"bill' — and the example for numbering starting again in a new workspace",
	2: "'start with #2', explaining where a workspace's ref sequence resumes",
	3: "the skill's warning that a reader holding 'finding 3' and #3 cannot tell them apart",
	7: "'move 42 --under 7', the worked example of making one item part of another",
	38: "the skill's worked comment, and the sentence saying a #38 in a body becomes a link",
	42: "the ref used to explain what a ref is, on every page that explains one",
	46: "'what closed #46', CONTRIBUTING's example of the question the commit hook answers",
	442: "'Blocks #442', the example of a link rendering that says whether it is finished",
	862: "the search example — searching 862 finds #862 itself as well as text mentioning it",
}


def _published_docstrings () -> list[tuple[str, str, str]]:
	"""Return every docstring FastAPI publishes, as ``(path, function, text)``.

	Read from the decorators rather than from a list of files, because the property that
	matters is *this is served to a caller* and that is what ``@router.get`` and its siblings
	decide. A list would fall behind the first router somebody adds.
	"""

	found = []

	for relative in tracked():
		if not (relative.startswith("src/") and relative.endswith(".py")):
			continue

		tree = ast.parse(ROOT.joinpath(relative).read_text(encoding="utf-8"))

		for node in ast.walk(tree):
			if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				continue

			decorated = " ".join(ast.unparse(one) for one in node.decorator_list)

			if "router." in decorated and ast.get_docstring(node):
				found.append((relative, node.name, ast.get_docstring(node) or ""))

	return found


#: A ref as it is written anywhere — ``#42``, but not ``##1``, not ``#42FF00``, not ``issue#1``.
#: The same lookarounds ``mentions.REF_PATTERN`` uses, for the same reasons.
_REF = re.compile(r"(?<![\w#])#(\d{1,4})(?!\w)")


def test_nothing_the_api_publishes_cites_an_item () -> None:
	"""An endpoint's docstring is its OpenAPI ``description``, and that document is public.

	**Measured on the served instance rather than reasoned about** (`#944`):
	``GET /v1/openapi.json`` answers with no credential at all and carried 51 citations, so
	``PATCH /v1/tasks/{id_or_ref}`` told a stranger to consult ``SPEC.md`` and
	``POST /v1/login-links`` cited ``#248``. A generated client, a documentation browser and
	anybody reading the schema got a pointer into a tracker they have no account on.

	**Zero, with no register**, unlike the pages below. Nothing an endpoint needs to say about
	itself requires an example ref: the path parameter is called ``id_or_ref`` and the grammar
	is explained where somebody is typing one, not in a schema.
	"""

	offenders = [
		(path, name, line.strip())
		for path, name, doc in _published_docstrings()
		for line in doc.splitlines()
		if _REF.search(line)
	]

	assert not offenders, (
		"an endpoint docstring cites an item, and FastAPI publishes it as the route's "
		"description in /v1/openapi.json — which answers without a credential. Say the thing "
		"rather than pointing at it; the item stays in the commit message, which is where the "
		"trail from code to decision lives.\n"
		+ "\n".join(f"  {p} {n}: {line}" for p, n, line in offenders)
	)


def test_no_published_page_cites_an_item_it_is_not_demonstrating () -> None:
	"""The pages a reader is handed name refs only as examples of what a ref is.

	The one this caught on its first run was ``CHANGELOG.md``'s *"macOS and Windows are
	`#245`"* — in a released section, on the document somebody reads while deciding whether to
	upgrade, pointing at a private tracker.
	"""

	offenders = []

	for relative in PUBLISHED_PAGES:
		path = ROOT / relative

		if not path.exists():
			continue

		for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
			for found in _REF.finditer(line):
				if int(found.group(1)) not in ILLUSTRATIVE:
					offenders.append(f"  {relative}:{number}: {line.strip()[:96]}")

	assert not offenders, (
		"a published page cites an item a reader cannot look up. Say the thing rather than "
		"pointing at it — or, if the number really is an example of what a ref looks like, add "
		"it to ILLUSTRATIVE with the reason.\n" + "\n".join(offenders)
	)


def test_every_illustrative_ref_is_still_demonstrated () -> None:
	"""An entry naming a ref no page uses is an excuse nobody can delete.

	`#405`'s question of every register here: what makes an entry go away? This one goes when
	the prose stops using the number — and without this, ``ILLUSTRATIVE`` would slowly become a
	list of refs it is acceptable to cite anywhere, which is the opposite of what it says.
	"""

	used: set[int] = set()

	for relative in PUBLISHED_PAGES:
		path = ROOT / relative

		if path.exists():
			used.update(int(one) for one in _REF.findall(path.read_text(encoding="utf-8")))

	stale = sorted(set(ILLUSTRATIVE) - used)

	assert not stale, (
		f"ILLUSTRATIVE excuses {stale}, which no published page names any more. Delete the "
		f"entries, or the exemption is spendable on a real citation later."
	)

	assert used, "no published page names a ref at all, so this scan is reading nothing"
