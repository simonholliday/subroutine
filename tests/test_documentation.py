"""Published documentation that quotes the program must keep quoting it accurately.

``docs/hosting.md`` is the page somebody reads while deciding whether to trust this with a
server, and its most load-bearing part is the refusal ``serve`` gives when asked to listen
beyond the machine without TLS — quoted verbatim so the reader meets it here rather than at two
in the morning. A reworded refusal would leave the page confidently wrong, and nothing about
the page would look stale.

This is the same guard as ``tests/test_errors.py``'s and ``tests/test_api_examples.py``'s, and
for the same reason: prose that quotes code is only as true as the last time somebody checked.
Prose that merely *describes* code is deliberately not covered — a check that could not tell
"still accurate" from "reworded" would fail on every edit and be switched off.
"""

import json
import pathlib
import re
import shlex
import subprocess
import textwrap
import typing

import click
import pytest
import typer.main
import typer.testing

import subroutine.api.meta
import subroutine.auth
import subroutine.cli.main
import subroutine.config
import subroutine.db.migrate

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOSTING = ROOT / "docs" / "hosting.md"
CONNECTING = ROOT / "docs" / "connecting.md"
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"
RELEASES = ROOT / "docs" / "releases.json"


@pytest.fixture
def refusal () -> typing.Callable[..., str]:
	"""Return what ``serve`` says when it declines a bind, for a given configuration.

	No database is needed and none is made: the TLS check runs before ``serve`` looks for one,
	which is itself worth pinning — a refusal that only appeared on an initialised instance
	would arrive after the operator had already committed to the setup.
	"""

	runner = typer.testing.CliRunner()

	def asked (*arguments: str) -> str:
		"""Run one ``serve`` invocation that is expected to be refused."""

		result = runner.invoke(subroutine.cli.main.app, ["serve", *arguments])

		assert result.exit_code == 1, result.output

		return result.output

	return asked


def test_the_hosting_page_quotes_the_bind_refusal_as_it_is_actually_worded (
	refusal: typing.Callable[..., str], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Both halves of it: with no ``public_url`` at all, and with one that is not https."""

	page = HOSTING.read_text(encoding="utf-8")

	for line in refusal("--host", "0.0.0.0").splitlines():
		assert line.strip() in page, f"docs/hosting.md no longer quotes: {line.strip()!r}"

	monkeypatch.setenv("SUBROUTINE_PUBLIC_URL", "http://tasks.example.com")

	for line in refusal("--host", "0.0.0.0").splitlines():
		assert line.strip() in page, f"docs/hosting.md no longer quotes: {line.strip()!r}"


#: The three ways ``docs/hosting.md`` quotes *this build's* schema revision back to a reader —
#: ``db current``, ``/readyz`` and ``upgrade``. Each is somebody checking their own instance
#: against the page, so each has to name the head this build actually wants. Deliberately not
#: every twelve-hex token on the page: the upgrade walkthrough also quotes an *older* revision,
#: on purpose, and a guard that could not tell the two apart would have to be switched off.
_QUOTED_HEAD = (
	re.compile(r"Schema is at ([0-9a-f]{12})\."),
	re.compile(r'"schema_revision":"([0-9a-f]{12})"'),
	# The version is deliberately not pinned: it is illustrative like the backup filename's
	# timestamp beside it, and pinning it would make every release edit this page (`#343`).
	re.compile(r"Subroutine \S+ expects schema ([0-9a-f]{12})\."),
)


#: The heading the settings table sits under. Named rather than matched by shape, because the
#: page carries other tables whose first column is also a backticked word — the roles table
#: among them, which is what a shape-matching scan reads as four extra settings.
SETTINGS_HEADING = "### Every setting, and what it does"


def _documented_settings () -> set[str]:
	"""Return every setting the hosting page's table names."""

	page = HOSTING.read_text(encoding="utf-8")
	start = page.index(SETTINGS_HEADING)
	end = page.index("\n### ", start + len(SETTINGS_HEADING))

	return set(re.findall(r"^\| `([a-z_]+)` \|", page[start:end], re.M))


def test_every_setting_an_operator_can_set_is_on_the_hosting_page () -> None:
	"""Both directions, derived from `config.Settings` rather than from a list (`SR#1061`).

	**The page is what an operator reads while deciding whether to trust this with a
	database**, and a setting missing from it is one they cannot know exists. A row naming a
	setting that has gone is worse: they will try it, and `extra="ignore"` means nothing
	refuses it.

	Nothing checked this, which is how `max_page_size`'s row came to say *"the most a caller
	may ask for"* — true when written and made false by `SR#1037` four days before a cold
	review read it. That was found by asking which published sentence a change had broken, and
	that question does not scale.

	This guards the table's *membership*, not its prose. What a row says is still somebody's to
	get right; what this ends is a row that has no subject, or a subject that has no row.
	"""

	documented = _documented_settings()
	declared = set(subroutine.config.Settings.model_fields)

	assert declared - documented == set(), (
		f"settings an operator can set and the hosting page never names: "
		f"{sorted(declared - documented)}"
	)
	assert documented - declared == set(), (
		f"the hosting page names settings this build has not got: "
		f"{sorted(documented - declared)}"
	)


def test_the_settings_scan_reads_the_table_and_not_the_page () -> None:
	"""A floor, and the reason it is not a scan over every table row.

	`SR#405`: the assertion above is satisfied by two empty sets. And a scan matching every
	``| `word` |`` on this page reads the *roles* table too — measured, four extra names — so
	one that passed would be agreeing about a set that is not the settings.
	"""

	documented = _documented_settings()

	assert len(documented) > 20, f"only {len(documented)} rows read from the settings table"
	assert "max_page_size" in documented
	assert "worker" not in documented, (
		"the scan reached past the settings table into the roles one"
	)


def test_the_hosting_page_quotes_the_schema_revision_this_build_expects () -> None:
	"""A migration moves the head and leaves the page confidently wrong (`#314`).

	It had, eleven times, including both of the steps the page tells an operator to run *to
	check the setup worked* — so somebody following it and seeing a different revision had no
	way to tell a healthy instance from a broken one. The page opens by promising every quoted
	output is what the program actually printed, which is the promise that makes it worth
	reading and the one nothing was holding.

	The other quoted revisions are left alone deliberately. A backup filename records the
	revision it was taken at, and the upgrade example needs an older one to upgrade *from*;
	both are illustrative, and only these three are claims about the software the reader has.
	"""

	page = HOSTING.read_text(encoding="utf-8")
	head = subroutine.db.migrate.head_revision()

	assert head is not None

	for pattern in _QUOTED_HEAD:
		found = pattern.findall(page)

		assert found, f"docs/hosting.md no longer quotes {pattern.pattern!r} at all"

		for quoted in found:
			assert quoted == head, (
				f"docs/hosting.md says this build expects schema {quoted}, and it expects "
				f"{head}. A migration has landed since the page was written."
			)


#: An absolute link to a file in *this* repository, which is how the README has to write them
#: so they work on PyPI as well as on GitHub (`#716`). Matched so the checks below go on
#: reading them: a link that stops being relative must not stop being checked, and both of
#: these tests have a floor that fired the moment the spelling changed — which is the floor
#: doing its job and the reason they were widened rather than the README exempted.
_OUR_BLOB = re.compile(r"^https://github\.com/simonholliday/subroutine/blob/[^/]+/")

#: A fence opening or closing a code block, indented up to the three spaces Markdown allows
#: before it stops being one.
_FENCE = re.compile(r"^\s{0,3}(?P<fence>`{3,}|~{3,})")

#: A run of backticks. Its *length* is what matters: a code span is closed by the next run of
#: exactly the same length, which is the rule that makes ``a `` ` `` b`` mean what it looks like.
_BACKTICKS = re.compile(r"`+")


def _outside_code_fences (text: str) -> str:
	"""Return the page with its fenced blocks removed.

	A fenced block is shown to the reader as characters rather than as Markdown, so nothing
	inside one is a heading or a link however it is spelled. ``docs/hosting.md`` quotes 33
	shell comments beginning with ``#``, every one of which was being read as a heading — so
	a link to a section that no longer exists could resolve against a line of shell.
	"""

	kept = []
	closing = None

	for line in text.splitlines():
		found = _FENCE.match(line)

		if closing is not None:
			# Closed only by the same character, at least as long, per Markdown's own rule.
			if found is not None and found.group("fence").startswith(closing):
				closing = None

			continue

		if found is not None:
			closing = found.group("fence")

			continue

		kept.append(line)

	return "\n".join(kept)


def _prose (text: str) -> str:
	"""Return the page with everything it quotes as code removed.

	``[a](b)`` inside backticks is a page *showing* link syntax, which the browser's own
	renderer arrived to make worth doing — and the checks below split on ``](`` and knew
	nothing about code spans, so correct prose failed and had to be reworded into worse prose
	(`#836`). Rewording is the wrong repair: the guard should read what renders.

	Scanned over the whole page rather than line by line, deliberately. Sixteen of this
	repository's code spans are wrapped across two lines by the paragraph width, and a
	per-line scan would see an unmatched backtick on each half — which is this project's
	twice-recorded trap of a scan defeated by where Markdown wrapped.
	"""

	body = _outside_code_fences(text)
	out = []
	at = 0

	while True:
		opened = _BACKTICKS.search(body, at)

		if opened is None:
			out.append(body[at:])

			return "".join(out)

		closed = _BACKTICKS.search(body, opened.end())

		while closed is not None and closed.group() != opened.group():
			closed = _BACKTICKS.search(body, closed.end())

		if closed is None:
			# An unmatched run is literal text, and so is the rest of the page. Keeping it
			# is what stops one stray backtick hiding every link below it from the check.
			out.append(body[at:])

			return "".join(out)

		out.append(body[at : opened.start()])
		out.append(" ")
		at = closed.end()


def _in_repository (page: pathlib.Path, target: str) -> pathlib.Path | None:
	"""Return the file a link names in this repository, or ``None`` if it points elsewhere.

	Both spellings resolve to the same place, which is the point: one page writes its links
	absolutely because of where it is published, and every other page writes them relatively.
	"""

	if _OUR_BLOB.match(target):
		return ROOT / _OUR_BLOB.sub("", target)

	if target.startswith(("http://", "https://", "mailto:")):
		return None

	return page.parent / target


def _missing_targets (page: pathlib.Path, text: str) -> tuple[list[str], int]:
	"""Return the links in this page that name a file this repository does not have.

	Takes the text rather than reading it, so a page built to carry a known broken link can
	be put through the same code the real pages go through — `#405`'s rule, which is the only
	way to tell a checker that reads nothing from one that reads everything.
	"""

	missing = []
	checked = 0

	for fragment in _prose(text).split("](")[1:]:
		target = fragment.split(")")[0].split("#")[0]

		if not target:
			continue

		where = _in_repository(page, target)

		if where is None:
			continue

		if not where.exists():
			missing.append(target)

		checked += 1

	return missing, checked


#: The section of ``CHANGELOG.md`` that is being written rather than read — the one a release
#: is cut from. Checked by name rather than by position because "the topmost section" is what
#: ``scripts/check_release_notes.py`` means by it, and two definitions of *the release being
#: prepared* is one more than this repository should have.
PREPARING = "Unreleased"


def _changelog_headings (text: str) -> dict[str, list[str]]:
	"""Return each ``## `` section's ``### `` headings, in the order they appear.

	Text in and data out, so a synthetic changelog can be fed to exactly the code that reads
	the real one (`#405`). A scanner whose subject is baked in can only be falsified by a copy
	of its own rule, which is how this repository has twice shipped a check that read nothing.
	"""

	found: dict[str, list[str]] = {}
	section: str | None = None

	for line in text.splitlines():
		if line.startswith("## "):
			section = line[3:].strip()
			found[section] = []

		elif line.startswith("### ") and section is not None:
			found[section].append(line[4:].strip())

	return found


def test_the_release_being_prepared_has_one_heading_of_each_kind () -> None:
	"""`#859`, which is `#692` recurring — and `#692` was fixed without a guard.

	`#692` found two ``### Fixed`` headings in the section about to ship, with the licence
	change between them, and the finding was that **half the fixes sat below the entry most
	likely to stop somebody reading**. A person upgrading reads down until they have what they
	need; a second ``### Added`` two hundred lines later is content they will never reach. It
	is invisible top to bottom and obvious the moment somebody asks `#686`'s third question —
	*does this section describe what a person upgrading needs?*

	Nothing was built to stop it, so within four days the same section had **three** ``Added``,
	**three** ``Changed`` and **two** ``Fixed``. That is the argument for the guard rather than
	the fix, made again by the same defect.

	**Only the section being prepared**, and that is a scoping decision rather than an
	oversight. A released section is the record of what shipped, not a draft, so tidying one is
	rewriting a published page — and `0.4.0` carries this same defect today for exactly that
	reason. What this guard is for is stopping the *next* release going out with it, and every
	section passes through :data:`PREPARING` on its way there.
	"""

	sections = _changelog_headings(CHANGELOG.read_text(encoding="utf-8"))

	# **The floor is that the scan read *something*, not that a release is being prepared.**
	# `#893`: the two were one assertion, and `scripts/release.py` renames `## Unreleased` to
	# `## <version> — <date>` — so the first commit of every release has no section being
	# prepared, and this failed CI on all four Python versions with nothing wrong. It was
	# written after v0.6.4 and had never been through a release when it blocked one.
	assert sections, (
		f"no version section was read from {CHANGELOG.name} at all, so nothing was checked "
		f"and this guard is inert"
	)

	# **Nothing being prepared is a real state and a brief one**: it lasts from the release
	# commit until the next change worth telling somebody about. There is no draft to check,
	# which is different from a draft nobody could find.
	if PREPARING not in sections:
		return

	headings = sections[PREPARING]
	repeated = sorted({name for name in headings if headings.count(name) > 1})

	assert not repeated, (
		f"'{PREPARING}' in {CHANGELOG.name} has more than one heading called each of "
		f"{repeated}, out of {headings}. Merge them: a reader stops at the first one that "
		f"answers their question, so anything under the second is content nobody reaches."
	)


def test_the_duplicate_heading_check_can_fail () -> None:
	"""Feed the scanner a changelog that is wrong, through its own entry point.

	Without this the assertion above is a rule nobody has seen fire, and it goes on passing if
	``_changelog_headings`` ever stops finding headings at all — which is a scan reading
	nothing, and reads exactly like a clean file.
	"""

	sections = _changelog_headings(
		"# Changelog\n\n"
		f"## {PREPARING}\n\n### Added\n\n- One.\n\n### Fixed\n\n- Two.\n\n"
		"### Added\n\n- Three.\n\n"
		"## 0.1.0 — 2026-01-01\n\n### Added\n\n- Four.\n"
	)

	assert sections[PREPARING] == ["Added", "Fixed", "Added"], sections
	assert sections["0.1.0 — 2026-01-01"] == ["Added"], (
		"a heading was attributed to the wrong section, so the real check is scoped wrongly"
	)


@pytest.mark.parametrize(
	"page", [HOSTING, CONNECTING, README, CHANGELOG], ids=lambda path: path.name
)
def test_published_pages_link_only_to_files_that_exist (page: pathlib.Path) -> None:
	"""A link in published documentation is a promise about the repository.

	Worth having because the README's links point *out* of its own directory: moving or
	renaming anything under ``docs/`` breaks them from a distance, and a dead link in the file
	GitHub renders first is read as a project that has stopped being maintained.
	"""

	missing, checked = _missing_targets(page, page.read_text(encoding="utf-8"))

	assert not missing, f"{page.name} links to a missing {', '.join(missing)}"

	# Otherwise this passes just as happily on a page whose links have all been deleted, which
	# is the failure mode a link checker is least able to notice about itself.
	assert checked, f"{page.name} has no links into the repository — has this stopped reaching them?"


def test_the_link_check_reads_the_prose_and_not_the_code_it_quotes () -> None:
	"""A page may *show* link syntax, and showing it is not a promise about a file.

	Both halves have to hold at once, which is why they are one page rather than two tests:
	a fix that simply stopped looking would pass the second assertion and fail the first, and
	the reverse — the reword `#836` was filed for — passes the first and fails the second.
	"""

	page = ROOT / "README.md"
	missing, checked = _missing_targets(
		page,
		"A real one: [gone](docs/no-such-page.md).\n"
		"Shown rather than followed: `![alt](docs/also-missing.md)`.\n"
		"And one wrapped by the paragraph width, `![alt](docs/\n"
		"wrapped-and-missing.md)`, which a line-by-line reader would still follow.\n"
		"```\n"
		"[fenced](docs/fenced-and-missing.md)\n"
		"```\n"
		"A working one: [the changelog](CHANGELOG.md).\n",
	)

	assert missing == ["docs/no-such-page.md"]
	assert checked == 2


def _anchors (page: pathlib.Path) -> set[str]:
	"""Return the anchors a heading in this page can be linked to.

	Derived the way GitHub derives them — lower-cased, everything but word characters, spaces
	and hyphens dropped, spaces turned into hyphens — because that is what the link resolves
	against. Reading the headings and trusting a hand-written anchor beside them would check
	the wrong half.

	Fenced blocks are dropped and code spans are *not*: a ``#`` opening a line of shell is not
	a heading, while a heading naming ``config.toml`` in backticks anchors on the word.
	"""

	found = set()

	for line in _outside_code_fences(page.read_text(encoding="utf-8")).splitlines():
		if line.startswith("#"):
			heading = line.lstrip("#").strip()
			found.add(re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-"))

	return found


def test_a_line_of_shell_is_not_a_heading_a_link_can_resolve_against (tmp_path: pathlib.Path) -> None:
	"""``docs/hosting.md`` quotes 33 shell comments, and every one was an anchor.

	Which is the check failing in the direction nobody notices: an anchor link is compared
	against the headings a page has, so a *surplus* heading means a link to a section somebody
	has since renamed goes on passing — against a line of shell that happens to read alike.
	None of the four pages relies on one today, measured, so removing them costs nothing now
	and is worth having before it does.

	The code span in the real heading is the other half. GitHub anchors on the words inside
	one, so dropping spans as well as fences here would quietly break every link to a section
	named after a file.
	"""

	page = tmp_path / "page.md"
	page.write_text(
		"## Installing it as `subroutine`\n"
		"```\n"
		"# useradd --system subroutine\n"
		"```\n",
		encoding="utf-8",
	)

	assert _anchors(page) == {"installing-it-as-subroutine"}


@pytest.mark.parametrize(
	"page", [HOSTING, CONNECTING, README, CHANGELOG], ids=lambda path: path.name
)
def test_every_anchor_a_published_page_links_to_exists (page: pathlib.Path) -> None:
	"""The half the link check above throws away, and the half that rots quietly.

	It splits a target on ``#`` and keeps the file, so ``docs/hosting.md#reaching-it-from-an-
	agent`` passes on the strength of ``hosting.md`` existing. Renaming a section leaves the
	link resolving to the page and landing at the top of it, which reads as the reader's mistake
	rather than as a broken pointer — and this page sends people across files precisely when
	they are trying to get something working.
	"""

	text = page.read_text(encoding="utf-8")
	checked = 0

	for fragment in _prose(text).split("](")[1:]:
		target = fragment.split(")")[0]

		if "#" not in target:
			continue

		path, _, anchor = target.partition("#")
		where = page if not path else _in_repository(page, target.split("#")[0])

		if where is None or not where.is_file():
			# The file's own existence is the other test's, and reporting it twice would mean
			# fixing one failure and meeting its twin.
			continue

		assert anchor in _anchors(where), (
			f"{page.name} links to {target}, and {where.name} has no heading with that anchor"
		)
		checked += 1

	assert checked, f"{page.name} has no anchor links — has this test stopped reaching them?"


#: Everything a stranger reads that shows a project key — `#521`. Wider than ``PUBLISHED``,
#: which is about commands: a plugin's settings field and the skill inside it are read by people
#: who will never open this repository, so they are exactly where teaching the old form costs
#: most.
SHOWS_A_KEY = (
	README,
	HOSTING,
	CONNECTING,
	ROOT / "plugins" / "subroutine" / "skills" / "subroutine" / "SKILL.md",
	ROOT / "plugins" / "subroutine-remote" / "skills" / "subroutine" / "SKILL.md",
	ROOT / "plugins" / "subroutine" / ".claude-plugin" / "plugin.json",
)

#: Where a project key is unmistakably a project key, whatever it is called.
#:
#: This catches a key nobody has thought of, which the list below cannot: a new example using
#: ``--project BILLING`` fails without anybody adding an entry anywhere.
_A_KEY_FOLLOWS = re.compile(r"(?:\+|--project\s+|--write\s+)([A-Za-z][\w-]*)")

#: The keys these pages actually use in examples, in the form the program stores and prints.
#:
#: **The list exists for the case the pattern above cannot see**: a *transcript* naming a
#: project — "Restricted to web and anything filed underneath" — carries no sigil and no flag,
#: and those were three of the lines `#521` was filed for. It is small and closed because these
#: pages use three example keys between them, and
#: :func:`test_every_example_key_is_one_the_pages_still_use` fails when one stops being used.
KEYS_IN_EXAMPLES = frozenset({"web", "sr", "other"})


def test_no_published_page_writes_a_project_key_in_capitals () -> None:
	"""`#508` made a key lower case in storage and in display; the pages kept the old form.

	Input is still case-insensitive, so every *command* on those pages worked — which is why
	nothing noticed. What broke is narrower and worse: three lines of ``docs/hosting.md`` were
	*output*, and that page's central promise is that every quoted output is what the program
	actually printed. It is the promise `#363` refused to redact a credential out of, on the
	grounds that an exception would make the page's own claim conditional.

	And a plugin's settings field and the skill inside it teach the form to somebody who will
	never see this repository at all.
	"""

	wrong = []

	for page in SHOWS_A_KEY:
		text = page.read_text(encoding="utf-8")

		for found in _A_KEY_FOLLOWS.finditer(text):
			if found.group(1) != found.group(1).lower():
				wrong.append(f"{page.name}: {found.group(0)!r} — a project key is lower case")

		for key in sorted(KEYS_IN_EXAMPLES):
			if re.search(rf"\b{key.upper()}\b", text):
				wrong.append(f"{page.name}: names {key.upper()!r}, which is stored as {key!r}")

	assert not wrong, "\n".join(wrong)


def test_every_example_key_is_one_the_pages_still_use () -> None:
	"""`#405`'s question of this list: what makes an entry go away?

	An example rewritten to stop using a key leaves its entry behind, still policing a word
	nothing says — which reads as a considered decision and is a dead one. That is the shape
	`#500` found in ``UNREACHED_FIELDS``, where a reason cited an item closed five days earlier.
	"""

	together = "\n".join(page.read_text(encoding="utf-8") for page in SHOWS_A_KEY)

	for key in sorted(KEYS_IN_EXAMPLES):
		assert re.search(rf"\b{key}\b", together), (
			f"no published page uses {key!r} any more, so guarding its capitals is guarding "
			f"nothing. Take it out of KEYS_IN_EXAMPLES."
		)


def test_the_key_scan_reaches_the_pages_it_names () -> None:
	"""A walk that read nothing would satisfy the ceiling above most comfortably."""

	for page in SHOWS_A_KEY:
		assert page.is_file(), f"{page} is named by the key scan and is not there"

	together = "\n".join(page.read_text(encoding="utf-8") for page in SHOWS_A_KEY)

	assert _A_KEY_FOLLOWS.search(together), (
		"no page shows a project key behind a sigil or a flag — has this stopped reaching them?"
	)


#: Prose that counts the tools, which is the one thing about the surface that keeps rotting.
#:
#: Words as well as digits, because the README said "Eleven tools" rather than "11".
#:
#: **And up to two words may sit between the number and "tools"** (`#582`). Without that this
#: pattern found *nothing at all* in the README, for as long as the sentence it was written for
#: said "eleven MCP tools" — the count it exists to catch, on the page it is scoped to, invisible
#: because of one word in the middle. The width was set by running it over all four published
#: pages and reading what it caught: two words adds no hit inside the README beyond the real one.
_COUNTS_TOOLS = re.compile(
	r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
	r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)"
	r"\s+(?:[A-Za-z-]+\s+){0,2}tools\b",
	re.IGNORECASE,
)


def _counted_tools (text: str) -> list[str]:
	"""Return every phrase in this page that states how many tools there are.

	Takes the text rather than reading the file, so a synthetic defect can be fed through the
	real scanner instead of through a copy of its rule (`#405`).
	"""

	return [found.group(0) for found in _COUNTS_TOOLS.finditer(text)]


def test_no_published_page_counts_the_tools () -> None:
	"""`#522`, and `#198` before it, which is the whole argument for a test rather than a note.

	`#198` found the tool count stale in the README, the CHANGELOG and two comments at once, and
	the response was to stop repeating it: ``tests/test_mcp.py`` holds the figure and this
	project's own notes say in as many words that it is deliberately not restated. The README
	kept its copy, and by the time anybody read it again it said eleven of fourteen — omitting
	``claim``, ``whoami`` and ``call_api``, the last of which is the one a reader deciding
	whether the surface is enough most needs to know exists.

	**Recording a rule does not inoculate you against it; only a guard does.** That sentence is
	already written down here about something else, which is exactly why this is a test.

	Deliberately about the *count* rather than the tool names. Naming what the tools do is
	prose that ages slowly and readably; naming how many is a claim that is false the moment
	one is added, and false silently.

	**The README alone, and the other pages are excluded on evidence rather than by oversight.**
	Written over all four first, it reported two findings and both were correct prose:
	``docs/hosting.md`` says a merged agenda beats keeping things "in two tools", meaning two
	*products*; and ``CHANGELOG.md`` records a past release serving "nine tools", which was true
	of that release and is what a changelog is for. A guard that fires on those would be
	switched off inside a month, which is the argument ``tests/test_references.py`` makes for
	not being a general dead-link checker. The README is the one page that describes this
	surface.
	"""

	wrong = [
		f"{README.name}: {phrase!r}"
		for phrase in _counted_tools(README.read_text(encoding="utf-8"))
	]

	assert not wrong, (
		"a published page states how many MCP tools there are, which is true until the next one "
		f"is added and then quietly false: {', '.join(wrong)}. Say what they do instead — "
		"tests/test_mcp.py is where the number lives, because there it can fail."
	)


def test_the_tool_count_scan_catches_the_sentence_it_was_written_for () -> None:
	"""`#582`. The falsification here used to prove only that the *regex* worked.

	It asserted `_COUNTS_TOOLS` matched "Eleven tools: list, search, show" — a string invented
	for the test — and passed for weeks while the README two directories away said **"eleven MCP
	tools and 7 KB, held by a test"** and the scan returned nothing at all. A guard tested
	against a copy of its own rule cannot notice that the real text is shaped differently, which
	is precisely the defect this file exists to catch elsewhere.

	So the case that matters is the *verbatim* sentence, and it is pinned here rather than
	described.
	"""

	assert _counted_tools("Compact replies; eleven MCP tools and 7 KB, held by a test.") == [
		"eleven MCP tools"
	], "the exact sentence the README carried, which the scan could not see until `#582`"

	assert _counted_tools("Eleven tools: list, search, show"), "the spelt-out form, no filler"
	assert _counted_tools("all 14 tools are"), "and the digits"
	assert _counted_tools("fourteen agent-facing tools"), "and a hyphenated qualifier"


def test_the_tool_count_scan_leaves_ordinary_prose_alone () -> None:
	"""The width was chosen by measurement, and this is what it must not start catching.

	Two intervening words is enough for "MCP tools" and "agent-facing tools" and short enough
	that a sentence merely containing a number and the word later on does not match. A guard
	that fires on ordinary prose is switched off within a month — the argument
	``tests/test_references.py`` already makes for not being a general dead-link checker.
	"""

	assert not _counted_tools("two of the three answers came from other tools we compared")
	assert not _counted_tools("eleven. Tools are declared in the manifest")
	assert not _counted_tools("nine releases ago the surface had a different set of tools")
	assert not _COUNTS_TOOLS.search("the tools are few"), "and nothing about a count"


def test_the_hosting_guide_lists_every_one_of_its_sections () -> None:
	"""`#274`. A table of contents that is quietly incomplete is worse than none.

	`## Credentials` had fallen out of it, and nothing noticed — so a reader looking for
	credentials in the list concludes the document does not cover them. The list is maintained
	by hand and drifts every time somebody adds a section; I added one today and got it right
	by luck.

	**The anchor is derived rather than trusted**, because that is what the link depends on:
	GitHub lowercases the heading, drops everything but word characters, spaces and hyphens,
	and turns the spaces into hyphens. A list entry pointing at an anchor that does not exist
	is the same defect one level down.
	"""

	text = (ROOT / "docs" / "hosting.md").read_text(encoding="utf-8")
	contents = text[text.index("## Contents") : text.index("## An account")]

	listed = dict(re.findall(r"^- \[(.+?)\]\(#(.+?)\)$", contents, re.MULTILINE))
	headings = [
		line.removeprefix("## ")
		for line in text.splitlines()
		if line.startswith("## ") and line != "## Contents"
	]

	assert list(listed) == headings, "the contents list and the sections disagree"

	for heading, anchor in listed.items():
		expected = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")

		assert anchor == expected, f"{heading!r} links to #{anchor}, but its anchor is #{expected}"


def test_every_setting_config_show_prints_is_documented () -> None:
	"""`#187`. ``config show`` lists every setting, which is how somebody finds out what there
	is to change — and several of them appeared in no operator-facing document at all.

	**The list is the population, not an allow-list.** It comes off ``Settings.model_fields``,
	so a setting added without a line here fails the build rather than being discovered by a
	reader who cannot find out what it does. That is the half `#187` was really about: the five
	the review named were found by reading one screen, and the comparison found fourteen.

	**No excuse register, deliberately.** Every other guard here has one; this one would only
	ever be used to park a setting somebody could not be bothered to describe, and a setting
	nobody can describe should not exist. `#133`'s rule is the alternative and it is better:
	a setting for an unbuilt feature belongs with the feature, so the answer to "what do I
	write about this" is sometimes to delete it — which is what happened to the retention pair
	and to ``require_verification_to_complete``.
	"""

	text = HOSTING.read_text(encoding="utf-8")
	heading = "### Every setting, and what it does"

	# The floor. Without it, deleting the section makes this fail with `ValueError: substring
	# not found` from `.index` — which is a failure, so nothing passes vacuously, but it says
	# nothing about what is wrong to whoever has to fix it.
	assert heading in text, f"docs/hosting.md no longer has a {heading!r} section to check"

	opening = text.index(heading)

	# Scoped to that one section rather than to the page. **Its own stale check caught this**:
	# a page-wide match swept up the agent-profile table's first column and reported four role
	# names as settings that no longer exist. A guard whose two directions check each other is
	# worth more than either.
	section = text[opening : text.index("\n## ", opening)]

	documented = set(re.findall(r"^\| `(\w+)` \|", section, re.MULTILINE))
	settings = set(subroutine.config.Settings.model_fields)

	missing = sorted(settings - documented)

	assert not missing, (
		f"{missing} are printed by 'config show' and appear in no table in docs/hosting.md. "
		f"Describe them there, or remove them if nothing reads them (`#133`)."
	)

	stale = sorted(documented - settings)

	assert not stale, (
		f"docs/hosting.md documents {stale}, which is not a setting any more — a reader can "
		f"set it, get no error, and believe it."
	)


#: The credentials the published transcripts quote, each **verified dead**.
#:
#: `docs/hosting.md` promises that every quoted output is what the program actually printed,
#: and a credential is output — a reader who has just run `agent create` needs to recognise
#: what came back, and `sr_<prefix>_…` is a shape the program never prints. So the rule here is
#: not "no credential in a document"; it is that every credential in one is a credential that
#: cannot be used, and that anything *else* full-length is an accident.
#:
#: **Verified on 2026-08-03 rather than assumed**: each was presented to the served instance
#: and refused with a 401, and each is absent from the retired SQLite database — they were
#: issued on throwaway instances that no longer exist. `#363` records how that was checked, and
#: adding an entry here means doing it again. Nothing about "it looks old" is evidence.
PUBLISHED_CREDENTIALS = frozenset(
	{
		"sr_d9fb02fa_UxzFqMe7i_NGb_eXRbOAsVhcm5_O-4pphVO6JhPe494",
		"sr_7e6abdce_S2MRP1ehbK3imO9G5hPlGw3ABblhxSi6KUh0Xi4Zv24",
	}
)

#: A credential as it is printed: the `sr_` marker, the prefix it is looked up by, and the
#: secret. Only the third part is dangerous, so the pattern requires it at *full length* — a
#: transcript quoting `sr_7e6abdce_…` is the redaction and not a match, and neither is
#: `sr_deadbeef_nonesuch`, which is how the suite spells a credential that was never issued.
#:
#: **Built from `subroutine.auth`'s own constants rather than from a count of characters in a
#: string somebody pasted.** A guard that hardcoded 43 would stop firing the day the secret
#: length changed, silently, which is the failure it exists to prevent one level up.
WHOLE_CREDENTIAL = re.compile(
	rf"{subroutine.auth.TOKEN_SCHEME}_"
	rf"[0-9a-f]{{{subroutine.auth.TOKEN_PREFIX_LENGTH}}}_"
	rf"[A-Za-z0-9_-]{{{(subroutine.auth.TOKEN_SECRET_BYTES * 4) // 3},}}"
)


def test_no_published_page_quotes_a_whole_credential () -> None:
	"""`#359`. The third one reached a published page, and the first two are still there.

	**The rule was written down after the first two and did not stop the third**, which is the
	part worth building a guard from rather than a note: recording a lesson does not inoculate
	you against it. `#345` made the same point twice in one day.

	The hazard is not the strings — none of them resolves anywhere. It is that `docs/hosting.md`
	promises every transcript is what the program actually printed, so the next person verifying
	one against a live instance produces a *working* credential in a file bound for a public
	repository. Two true rules colliding, which is how `#189` was found in the first place.

	Tracked files only, and read through git so that a scratch file or an untracked note cannot
	fail somebody's build.
	"""

	listed = subprocess.run(
		["git", "ls-files", "-z"],
		cwd=ROOT,
		capture_output=True,
		text=True,
		check=True,
	)
	found: dict[str, list[str]] = {}

	for name in listed.stdout.split("\0"):
		path = ROOT / name

		if not name or not path.is_file():
			continue

		try:
			text = path.read_text(encoding="utf-8")

		except UnicodeDecodeError:
			continue

		quoted = [
			match
			for match in WHOLE_CREDENTIAL.findall(text)
			if match not in PUBLISHED_CREDENTIALS
		]

		if quoted:
			found[name] = quoted

	assert not found, (
		f"a whole credential is committed in {sorted(found)} and is not one of the published "
		f"transcripts' own. If it belongs in a document, revoke it, check it is refused, and "
		f"add it to PUBLISHED_CREDENTIALS with the date. Otherwise take it out — and revoke it "
		f"anyway, because it has been in a repository."
	)


def test_the_credential_guard_can_actually_fire () -> None:
	"""The floor beside the ceiling: a pattern that matches nothing passes silently.

	Written because the guard above is only ever seen passing, and a typo in the regular
	expression would look exactly like a clean repository.
	"""

	# **Minted here rather than pasted**, which is not fastidiousness: a sample written into
	# this file would be a whole credential committed to the repository, and the guard above
	# would fail on its own test. It did, on the first run. Nothing stores this one.
	minted = subroutine.auth.generate_token().value.get_secret_value()

	assert WHOLE_CREDENTIAL.search(f"  {minted}\n")
	assert not WHOLE_CREDENTIAL.search("give it to a client as SUBROUTINE_TOKEN")
	assert not WHOLE_CREDENTIAL.search("sr_deadbeef_nonesuch"), (
		"the suite's own spelling for a credential that was never issued"
	)

	for published in PUBLISHED_CREDENTIALS:
		assert WHOLE_CREDENTIAL.fullmatch(published), (
			"an excused string the pattern would not have caught anyway excuses nothing"
		)


def test_every_published_credential_is_one_a_reader_would_recognise () -> None:
	"""The registry is not a place to park a redaction.

	`docs/hosting.md` promises every quoted output is what the program actually printed, and
	an entry here is a claim that some transcript prints this. So each one has to be a
	credential this program could have minted — same scheme, same prefix width, same secret
	length — rather than something shortened, masked or invented.

	Written after the opposite was tried. The first fix for `#359` cut the published credential
	back to `sr_<prefix>_…`, which is safe and is a shape the program never prints: it made the
	page's opening promise carry an exception, to protect strings that turned out to be dead
	anyway. Quoting a dead credential whole is the honest version, and this is what keeps
	"dead" from quietly becoming "abbreviated".
	"""

	assert PUBLISHED_CREDENTIALS, "the registry is empty — has the guard stopped being used?"

	for published in PUBLISHED_CREDENTIALS:
		scheme, prefix, secret = published.split("_", 2)

		assert scheme == subroutine.auth.TOKEN_SCHEME
		assert len(prefix) == subroutine.auth.TOKEN_PREFIX_LENGTH
		assert subroutine.auth.parse_token(published) == (prefix, secret), (
			"a published credential that this program would not even parse is not output"
		)

	pages = "\n".join(
		path.read_text(encoding="utf-8") for path in (HOSTING, README, CHANGELOG)
	)
	orphaned = sorted(
		published
		for published in PUBLISHED_CREDENTIALS
		if published not in pages
	)

	assert not orphaned, (
		f"{len(orphaned)} entries in PUBLISHED_CREDENTIALS appear in no published page. An "
		f"excuse outliving the thing it excused is how an allow-list stops meaning anything — "
		f"delete them: {[entry.rsplit('_', 1)[0] for entry in orphaned]}"
	)


#: The XDG variables the service runs with. Every step a `docs/hosting.md` reader performs *as
#: the service account* has to carry all three, because each is what points the command at the
#: service's own files rather than at the reader's.
SERVICE_ENVIRONMENT = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")


def _console_blocks (text: str) -> list[str]:
	"""Return the ```console fenced blocks in a document, in order."""

	return [block.split("```", 1)[0] for block in text.split("```console\n")[1:]]


def _commands (block: str) -> list[str]:
	"""Return each command in a console block, with its continuation lines joined.

	A prompt line starts a command and a trailing backslash continues it, which is how the
	multi-line ``sudo -u subroutine env …`` invocations are written. Splitting on lines alone
	would see the environment and the program as separate commands and check neither.
	"""

	found: list[str] = []

	for line in block.splitlines():
		stripped = line.strip()

		if stripped.startswith(("#", "$")):
			found.append(stripped)

		elif found and found[-1].endswith("\\"):
			found[-1] = f"{found[-1][:-1].rstrip()} {stripped}"

	return found


def test_every_step_running_subroutine_as_the_service_account_names_its_environment () -> None:
	"""`#376`. The upgrade step elided them behind a `…`, and it cost a broken instance.

	`subroutine db upgrade` acts on a *database* and finds it through configuration, so run
	without these it reads the operator's own `config.toml` — which on the machine this
	happened on still named a retired SQLite file. It reported on that one and looked like it
	had worked, while the served instance sat on the old schema under new code.

	Fifty lines earlier the same document spells all three out for `init`. So the page knew
	they mattered and dropped them at the one step where getting it wrong is silent, which is
	how a runbook fails: a written procedure has code's failure modes and none of its
	verification.

	**The rule is about what reads Subroutine's configuration, not about `sudo`.** The first
	version of this checked every `sudo -u subroutine` command and failed on
	`sudo -u subroutine psql -c '\\conninfo'`, which is correct as written — psql has no
	reason to know where a config file is. A guard that fires on a command it has no claim
	over is one somebody switches off.
	"""

	page = HOSTING.read_text(encoding="utf-8")
	checked = 0

	for block in _console_blocks(page):
		for command in _commands(block):
			if "sudo -u subroutine" not in command:
				continue

			if "/opt/subroutine/bin/subroutine" not in command:
				continue

			checked += 1
			missing = [name for name in SERVICE_ENVIRONMENT if name not in command]

			assert not missing, (
				f"this step runs Subroutine as the service account and omits "
				f"{', '.join(missing)}, so it would read the reader's own configuration and "
				f"act on the wrong database:\n\n    {command}"
			)

	# Otherwise this passes just as happily on a page where those steps have been rewritten
	# some other way, which is the failure mode a check like this cannot notice about itself.
	assert checked >= 2, (
		f"found only {checked} steps running Subroutine as the service account — has this "
		f"stopped reaching them?"
	)


def test_the_upgrade_walkthrough_stops_the_service_before_it_migrates () -> None:
	"""The ordering is the part that has to survive an edit, and it is invisible in a diff.

	Installing first and starting last is what keeps the window shut where new code serves an
	old database. That window is not theoretical — it is what a `503` from `/readyz` and a
	`500` from every listing looked like on 2026-08-03.
	"""

	page = HOSTING.read_text(encoding="utf-8")
	upgrading = page[page.index("## Upgrading") :]
	block = next(
		block for block in _console_blocks(upgrading) if "pip install --upgrade" in block
	)

	steps = [line.strip() for line in block.splitlines() if line.strip().startswith("#")]
	ordered = " → ".join(steps)

	assert steps[0].endswith("systemctl stop subroutine"), (
		f"the upgrade walkthrough no longer stops the service first: {ordered}"
	)
	assert steps[-1].endswith("systemctl start subroutine"), (
		f"the upgrade walkthrough no longer starts it last: {ordered}"
	)


#: A fenced ``console`` block, with its body captured. The README quotes commands the reader
#: is meant to type and output the program is meant to have printed, and only the first of
#: those is what the tests below reach for — a ``$`` prefix is what separates them.
_CONSOLE = re.compile(r"```console\n(.*?)```", re.DOTALL)

#: What the README says after ``subroutine`` without naming a command. Kept separate from
#: ``tests/test_plugin.py``'s list rather than shared: that one reads the skill's prose and
#: this one reads shell lines, so the two collect different noise and merging them would make
#: each carry the other's exceptions.
_NOT_A_COMMAND = frozenset({"add", "done", "help", "explain"})


def _typed (block: str) -> list[str]:
	"""Return the commands a reader would type from one console block, without the ``$``."""

	return [
		line.strip().removeprefix("$").strip()
		for line in block.splitlines()
		if line.strip().startswith("$")
	]


def _blocks (page: pathlib.Path) -> list[str]:
	"""Return every fenced console block on a page."""

	return _CONSOLE.findall(page.read_text(encoding="utf-8"))


def test_the_readme_only_shows_commands_that_exist () -> None:
	"""Every ``subroutine <command>`` the README tells somebody to type is a real one.

	The same guard ``tests/test_plugin.py`` puts on the skill, pointed at the page a stranger
	reads first. A README naming a command that was renamed is worse than one saying less: the
	reader has no way to tell a typo of theirs from a promise of ours.
	"""

	registered = {
		command.name or (command.callback.__name__ if command.callback else "")
		for command in subroutine.cli.main.app.registered_commands
	} | {group.name for group in subroutine.cli.main.app.registered_groups if group.name}

	shown = {
		typed.split()[1]
		for block in _blocks(README)
		for typed in _typed(block)
		if typed.startswith("subroutine ") and len(typed.split()) > 1
	}

	assert shown, "found no commands at all — has this test stopped reaching the blocks?"
	assert shown <= registered | _NOT_A_COMMAND, (
		f"the README shows {sorted(shown - registered - _NOT_A_COMMAND)}, which do not exist"
	)


#: The line that identifies the block a reader follows to set an agent up. Matched on rather
#: than counted to, so that reordering the page does not silently point this at another block.
_THE_AGENT_PATH = "claude plugin install"


def test_the_documented_agent_path_produces_a_working_agent (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Item ``#399``. Follow the README's plugin block and the first tool call must work.

	**The block omitted `subroutine init`**, so somebody who took *"You never have to learn the
	CLI"* literally had their first interaction fail. It failed *well* — the tool says which
	command to run — and an agent with a shell recovers by itself, which is exactly why nobody
	would have found this from inside.

	What is checked here is the claim the page makes: run these, and an agent can read. The
	``claude`` lines are somebody else's program and are skipped by name; everything the README
	asks of *this* one is run, in order, against an empty XDG home.

	**A ``uvx subroutine …`` line is one of ours** (`#585`). ``uvx`` is a launcher for this
	program rather than a different program, so the arguments after the package name are what
	the reader is really being asked to run — and dropping such a line would have made this
	guard quietly vacuous on the day the README stopped saying ``uv tool install``. It is run
	in process rather than through ``uvx`` itself, because reaching PyPI from a test would make
	the suite depend on a network and would exercise the *published* version rather than this
	one.
	"""

	home = tmp_path / "fresh"

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(home / variable.lower()))

	# **Every block up to and including the agent's, in the order a reader meets them** — not
	# the agent block alone. `#752` split the TL;DR in two, one for the person and one for the
	# agent, and the second holds nothing but `claude` lines. Scanning it alone then found no
	# `subroutine` command at all, which is the state this test's floor exists to refuse.
	#
	# A reader follows the page downwards, so what they have run by the time they reach the
	# plugin is everything above it. That is the claim being checked, and it survives the page
	# being reorganised again.
	blocks = _blocks(README)
	upto = next(i for i, one in enumerate(blocks) if _THE_AGENT_PATH in one)
	ours = [
		line.removeprefix("uvx ")
		for one in blocks[: upto + 1]
		for line in _typed(one)
		if line.startswith(("subroutine ", "uvx subroutine "))
	]

	assert ours, (
		"nothing above the plugin block asks anything of subroutine itself, so a reader "
		"following the page has no instance — which is what `#399` was"
	)

	runner = typer.testing.CliRunner()

	for line in ours:
		done = runner.invoke(subroutine.cli.main.app, line.split()[1:])

		assert done.exit_code == 0, f"'{line}' exited {done.exit_code}:\n{done.output}"

	# The claim the block is making, asked of the surface it hands over to. `list` rather than
	# a write, because reading is what an agent does first and is the call `#399` was found by.
	listed = runner.invoke(subroutine.cli.main.app, ["list"])

	assert listed.exit_code == 0, listed.output
	assert "no Subroutine instance" not in listed.output


#: The pages whose console blocks are read as commands somebody will type — item ``#406``.
#: ``docs/errors.md`` is generated and quotes no shell; the changelog quotes commands as
#: prose about a release rather than as instructions, and holds ones that no longer exist on
#: purpose.
PUBLISHED = (README, HOSTING, CONNECTING)

#: Shell metacharacters that make a line more than one invocation. A line carrying any of
#: these is skipped rather than guessed at: the guard's value is that it is quiet, and a
#: parser inventing failures on a pipeline would spend that immediately.
_SHELL = ("|", ">", "<", "$(", "`", "&&", ";")

#: A stand-in the reader is meant to replace — ``<file>``, ``<connection>``. Substituted
#: before the check above, and **that ordering is the whole of it**: ``<`` is also a redirect,
#: so a placeholder read as one made every line carrying a stand-in vanish from the scan.
#:
#: Found by falsifying rather than by reading. Renaming ``--as-clone`` on the page produced no
#: failure at all, because ``subroutine db restore <file> --as-clone`` was being skipped — a
#: guard reporting a clean page it had never looked at, which is the exact shape this file's
#: floors exist for and which the floor did not catch, since thirty other lines still passed.
_PLACEHOLDER = re.compile(r"<[A-Za-z][\w-]*>")


class Invocation(typing.NamedTuple):
	"""One line of a published page that runs this program."""

	page: str
	line: str

	#: Everything after the program's own name, as a shell would split it.
	words: list[str]


def _invocations (pages: typing.Sequence[pathlib.Path] = PUBLISHED) -> list[Invocation]:
	"""Return every published line that invokes ``subroutine``, split into arguments.

	**The pages are an argument so that the guard can be shown a defect** (`#405`). Feeding it
	a synthetic page reaches this scanner rather than a copy of its rule — and a scanner that
	quietly stopped parsing would report no findings in exactly the same way as a correct
	page, which is the failure mode this project has met twice.

	``sudo -u subroutine …`` and a leading ``NAME=value`` are stepped over, because the page
	uses both and the program being run is what decides whether a line is ours. A line whose
	program is something else — ``claude mcp add subroutine -- subroutine mcp`` — is not.
	"""

	found: list[Invocation] = []

	for page in pages:
		for block in _blocks(page):
			for typed in _typed(block):
				# A trailing `# …` on the page is a note to the reader, not an argument.
				line = re.sub(r"\s+#\s.*$", "", typed)

				# **Before the shell check, never after.** A `<file>` stand-in is an argument
				# and a `<` is a redirect, and reading the first as the second removed every
				# line carrying a placeholder from the scan without changing a single result.
				if any(character in _PLACEHOLDER.sub("_", line) for character in _SHELL):
					continue

				try:
					words = _program(shlex.split(line))

				except ValueError:
					# Unbalanced quotes: prose in a console block, not a command.
					continue

				if not words or not words[0].endswith("subroutine"):
					continue

				found.append(Invocation(page=page.name, line=line, words=words[1:]))

	return found


def _program (words: list[str]) -> list[str]:
	"""Return the command being run, stepping over ``sudo`` and environment assignments."""

	rest = list(words)

	while rest:
		if rest[0] == "sudo":
			rest.pop(0)

			while rest and rest[0].startswith("-"):
				flag = rest.pop(0)

				if flag in ("-u", "-g") and rest:
					rest.pop(0)

			continue

		if re.fullmatch(r"[A-Z_][A-Z0-9_]*=.*", rest[0]):
			rest.pop(0)

			continue

		break

	return rest


def _resolved (words: list[str]) -> tuple[typing.Any, list[str], list[str]]:
	"""Walk the command tree as click would, returning the command, its path and the rest.

	Stops at the first word that is not a subcommand, which is how a positional argument —
	``show 42``, ``token revoke a1b2c3d4`` — is told from a mistyped command: the first
	resolves to a leaf that takes arguments, the second leaves a group holding a word it does
	not know.
	"""

	command: typing.Any = typer.main.get_command(subroutine.cli.main.app)
	path = ["subroutine"]
	rest = list(words)

	while rest and not rest[0].startswith("-"):
		if not hasattr(command, "get_command"):
			break

		child = command.get_command(
			click.Context(command, info_name=" ".join(path)), rest[0]
		)

		if child is None:
			break

		command = child
		path.append(rest.pop(0))

	return command, path, rest


def _declared_options (command: typing.Any) -> set[str]:
	"""Return every option spelling a command accepts, including a ``--no-x`` counterpart."""

	found: set[str] = set()

	for parameter in command.params:
		found.update(getattr(parameter, "opts", ()) or ())
		found.update(getattr(parameter, "secondary_opts", ()) or ())

	return found


def test_every_flag_the_published_pages_type_is_one_the_command_declares () -> None:
	"""Item ``#406``. A renamed option is the commonest way a page like this goes wrong.

	``--scope``, ``--project``, ``--write``, ``--profile``, ``--as-clone`` and ``--recover``
	are all quoted in ``docs/hosting.md``, and until now nothing compared any of them against
	what the commands actually declare. The reader is on a server, following instructions,
	and a flag that no longer exists is a refusal in the middle of setting something up.
	"""

	wrong = []

	for invocation in _invocations():
		command, path, rest = _resolved(invocation.words)
		declared = _declared_options(command)

		for token in rest:
			# **Long options only, and that is a limit rather than an oversight.** A value can
			# look exactly like a short flag — `--order -priority_score` is the page's own
			# example, and reading that `-priority_score` as an option produced this guard's
			# first and only false failure. Telling the two apart needs each option's arity,
			# which is knowable but buys nothing here: these pages use long spellings
			# throughout, and a wrong *value* is not what a renamed flag looks like.
			#
			# A bare `--` ends the options; it is POSIX punctuation, not a flag.
			if token == "--" or not token.startswith("--"):
				continue

			name = token.split("=")[0]

			if name not in declared:
				wrong.append(
					f"{invocation.page}: '{' '.join(path)}' has no {name} — {invocation.line}"
				)

	assert not wrong, "\n".join(wrong)


def test_every_command_the_published_pages_type_exists () -> None:
	"""And the subcommand, which the README's word-level check cannot see.

	That one reads the first word after ``subroutine``, so ``db backupz`` passes it — ``db``
	is real and the mistake is one word further along. A group left holding a word it does not
	know is the signal, and a leaf holding one is an ordinary positional argument.
	"""

	wrong = []

	for invocation in _invocations():
		command, path, rest = _resolved(invocation.words)
		leftover = [word for word in rest if not word.startswith("-")]

		if not leftover or not hasattr(command, "list_commands"):
			continue

		context = click.Context(command, info_name=" ".join(path))

		if command.list_commands(context):
			wrong.append(
				f"{invocation.page}: '{' '.join(path)}' has no {leftover[0]!r} command "
				f"— {invocation.line}"
			)

	assert not wrong, "\n".join(wrong)


def test_the_scan_reaches_the_commands_on_the_page () -> None:
	"""The half the two tests above cannot assert about themselves.

	Both are satisfied by finding nothing wrong, and a scanner that stopped parsing finds
	nothing wrong too. The floor is deliberately well under what is there — 43 invocations
	across the two pages when this was written — because the number is not the point.
	"""

	found = _invocations()

	assert len(found) > 30, f"only {len(found)} invocations read from {len(PUBLISHED)} pages"
	assert {invocation.page for invocation in found} == {page.name for page in PUBLISHED}, (
		"one of the published pages contributed nothing"
	)


def _page (tmp_path: pathlib.Path, *lines: str) -> pathlib.Path:
	"""Write a page holding one console block, for showing the scanner a defect."""

	written = tmp_path / "sample.md"
	body = "\n".join(f"$ {line}" for line in lines)
	written.write_text(f"Some prose.\n\n```console\n{body}\n```\n", encoding="utf-8")

	return written


def test_the_scan_reports_a_flag_the_command_does_not_have (tmp_path: pathlib.Path) -> None:
	"""Item ``#406``, checked the way ``#405`` says a guard has to be.

	The pages this reads are clean today, so both tests above pass by finding nothing — which
	is the same green a scanner that stopped parsing would produce. A synthetic page is the
	only thing that tells those apart, and it goes through ``_invocations`` rather than
	through a copy of its rule.
	"""

	found = _invocations([_page(tmp_path, "subroutine token create --stealth")])

	assert len(found) == 1

	command, _path, rest = _resolved(found[0].words)

	assert "--stealth" not in _declared_options(command)
	assert "--title" in _declared_options(command), "and the real ones are found"
	assert rest == ["--stealth"]


def test_the_scan_reports_a_subcommand_that_does_not_exist (
	tmp_path: pathlib.Path,
) -> None:
	"""``db backupz`` is the case the README's word-level check structurally cannot see."""

	found = _invocations([_page(tmp_path, "subroutine db backupz")])
	command, path, rest = _resolved(found[0].words)

	assert path == ["subroutine", "db"], "it stopped at the group"
	assert rest == ["backupz"]
	assert command.list_commands(click.Context(command)), "which is a group, so this is wrong"


def test_the_scan_leaves_a_correct_line_alone (tmp_path: pathlib.Path) -> None:
	"""The other half, without which a scanner that flagged everything would pass the two above.

	It would also fail the real pages, which is loud — but a scanner that flagged everything
	*and* had its reader broken would pass all four, and that is the combination this whole
	family of defects is made of.
	"""

	found = _invocations(
		[_page(tmp_path, "subroutine db backup", "subroutine token create --title 'A laptop'")]
	)

	assert len(found) == 2

	for invocation in found:
		command, _path, rest = _resolved(invocation.words)
		declared = _declared_options(command)

		assert not [
			token for token in rest if token.startswith("--") and token not in declared
		]
		assert not hasattr(command, "list_commands"), "both reached a leaf command"


def test_the_scan_ignores_a_line_that_runs_something_else (tmp_path: pathlib.Path) -> None:
	"""``claude mcp add subroutine -- subroutine mcp`` is not this program being run.

	The README's real line, and the reason ``_program`` decides by what is being *executed*
	rather than by whether the word appears: read the other way, this guard would have
	checked ``claude``'s arguments against our command tree from its first run.
	"""

	found = _invocations(
		[_page(tmp_path, "claude mcp add subroutine -- subroutine mcp", "subroutine agenda")]
	)

	assert [invocation.line for invocation in found] == ["subroutine agenda"]


def test_the_scan_steps_over_sudo_and_an_environment_prefix (
	tmp_path: pathlib.Path,
) -> None:
	"""Both spellings the hosting page uses to run this as the service account.

	A page that ran every command through ``sudo -u subroutine`` would otherwise be skipped
	entirely, and the guard would report a clean scan of nothing — on the half of the page
	written for the machine where mistakes cost the most.
	"""

	found = _invocations(
		[
			_page(
				tmp_path,
				"sudo -u subroutine /opt/subroutine/bin/subroutine db current",
				"SUBROUTINE_PROFILE=scratch subroutine init",
			)
		]
	)

	assert [invocation.words for invocation in found] == [["db", "current"], ["init"]]


def test_the_scan_reads_a_line_carrying_a_placeholder (tmp_path: pathlib.Path) -> None:
	"""``<file>`` is an argument the reader replaces, not a redirect — item ``#406``.

	**This was a real hole and falsification is what found it.** Renaming ``--as-clone`` on
	``docs/hosting.md`` produced no failure, because ``subroutine db restore <file>
	--as-clone`` was being skipped: ``<`` is in the shell list, and the placeholder tripped it.
	Every line on the page carrying a stand-in was invisible, and the floor did not notice
	because thirty other lines still passed — a guard reporting a clean page it had never
	fully read.

	The two are told apart by *order*: placeholders are substituted, then the line is judged.
	A genuine redirect still has its ``<``.
	"""

	found = _invocations(
		[
			_page(
				tmp_path,
				"subroutine db restore <file> --recover",
				"subroutine serve < somefile",
			)
		]
	)

	assert [invocation.words for invocation in found] == [
		["db", "restore", "<file>", "--recover"]
	], "the placeholder is read and the redirect is not"


def test_the_connecting_page_says_how_to_configure_and_how_to_check () -> None:
	"""`#562`. It listed the two fields and never said where to put them.

	Found by a first-contact review that had installed the plugin successfully and had nowhere
	to go: the page said "open its settings", which names no command, no menu and no panel.
	The command exists — but only in the README and the skill, and only for the *other* plugin,
	inside advice about virtualenv paths. So the one place it appeared was the one place this
	reader had no reason to look, which is `#499`'s rule failing from an unfamiliar direction.

	Three sentences, and the middle one is the one nobody had noticed was missing: a session
	that was open when the plugin was configured keeps the tool list it started with, so
	everything reads as correctly set up and there are no tools.
	"""

	page = CONNECTING.read_text(encoding="utf-8")

	assert "/plugin" in page, "the page does not name the command that sets the two fields"

	assert "reload the window" in page.lower(), (
		"the page does not say the session must be restarted, which is the state that looks "
		"exactly like a broken install"
	)

	assert "subroutine_whoami" in page, (
		"the page does not say how to check it worked, so an absence of errors is the only "
		"signal a reader has — and an unconfigured plugin produces no errors either"
	)


def test_no_surface_says_where_a_credential_is_stored_beyond_what_was_measured () -> None:
	"""`#572`. Four places told a token's holder it was in their system keychain. It was not.

	Measured on Windows during a first-contact review: the token sits in
	`%USERPROFILE%\\.claude\\.credentials.json` in plaintext. The claim appeared on both plugin
	manifests' `token` fields, in the README and on the connecting page — every surface the
	person pasting the credential in actually reads.

	**A claim about where a secret lives, made to the person handing over the secret**, is the
	kind believed without checking, which is precisely what we did when we wrote it. Somebody
	who read it and concluded the file needed no protecting would have been wrong.

	**Not our storage; our sentence.** The client decides where the credential goes and we
	described that decision without measuring it on any platform. So the rule is narrow: do not
	name a storage mechanism. Saying "keychain" was wrong, and replacing it with a confident
	"a file" everywhere would repeat the mistake in the other direction — macOS and Linux were
	never measured.
	"""

	surfaces = {
		"README": README,
		"the connecting page": CONNECTING,
		"the local plugin's manifest": ROOT / "plugins" / "subroutine" / ".claude-plugin" / "plugin.json",
		"the remote plugin's manifest": ROOT / "plugins" / "subroutine-remote" / ".claude-plugin" / "plugin.json",
	}

	for where, path in surfaces.items():
		text = path.read_text(encoding="utf-8")

		assert "keychain" not in text.lower(), (
			f"{where} says a credential is kept in a keychain. That was measured false on "
			f"Windows (`#572`), and it is the client's choice rather than ours to describe"
		)


_MARKETPLACE = "claude plugin marketplace add"

_NAMES_GIT = re.compile(r"\bgit\b", re.IGNORECASE)


def _sections (text: str) -> list[tuple[str, str]]:
	"""Split a Markdown page into its headed sections, each keeping its own heading."""

	found: list[tuple[str, str]] = []
	heading = "the opening"
	body: list[str] = []

	for line in text.splitlines():
		if not line.startswith("#"):
			body.append(line)
			continue

		if body:
			found.append((heading, "\n".join(body)))

		heading = line.lstrip("#").strip()
		body = [line]

	if body:
		found.append((heading, "\n".join(body)))

	return found


def _silent_about_git (pages: dict[str, str]) -> list[str]:
	"""Name every section that publishes the marketplace command without its prerequisite."""

	offenders = []

	for where, text in pages.items():
		for heading, body in _sections(text):
			if _MARKETPLACE in body and not _NAMES_GIT.search(body):
				offenders.append(f"{where}, under {heading!r}")

	return offenders


def _published_pages () -> dict[str, str]:
	"""Read the three pages a reader arrives at, keyed by what to call each in a refusal."""

	return {
		"README": README.read_text(encoding="utf-8"),
		"the connecting page": CONNECTING.read_text(encoding="utf-8"),
		"the hosting page": HOSTING.read_text(encoding="utf-8"),
	}


def test_every_page_publishing_the_marketplace_command_names_git () -> None:
	"""`#561`. Five places published a command that refuses without Git, and none said so.

	Found by a first-contact review on a Windows machine with no development tools — the only
	kind of machine that could have found it. `claude plugin marketplace add owner/repo` clones
	the repository to read its manifest, so it needs a `git` binary. That is Claude Code's
	requirement rather than ours; the omission is ours.

	**Every machine on this project had already paid for it**, which is why it survived five
	publications and a suite. A prerequisite is invisible to everybody who has it.

	The check is per *section* rather than per page, because a reader arrives at one situation
	and reads it. Naming Git in the operator's chapter does not help somebody in the
	freelancer's, and the page these appear on is organised so that only one of them is read.
	"""

	offenders = _silent_about_git(_published_pages())

	assert not offenders, (
		"these publish 'claude plugin marketplace add' and never mention Git, so a reader "
		f"with no development tools is stopped before anything of ours runs: {offenders}"
	)


def test_the_git_scan_reads_the_sections_that_publish_the_command () -> None:
	"""A scan that matched nothing would pass the check above and prove nothing.

	`#405`'s rule, and the floor is not the point: what is asserted is that the command really
	is found in the real pages, so the guard above is answering about them rather than about an
	empty set.
	"""

	publishing = [
		f"{where}: {heading}"
		for where, text in _published_pages().items()
		for heading, body in _sections(text)
		if _MARKETPLACE in body
	]

	assert len(publishing) >= 4, (
		f"the scan found the marketplace command in only {publishing}, which is fewer than "
		"the pages are known to publish — the section split has probably stopped working"
	)


def test_the_git_scan_reports_a_section_that_omits_it () -> None:
	"""Falsified against the defect itself: the wording `#561` was filed about."""

	page = (
		"## An agent, with nothing installed\n"
		"\n"
		"It needs nothing on your machine at all.\n"
		"\n"
		"```console\n"
		f"$ {_MARKETPLACE} simonholliday/subroutine\n"
		"```\n"
		"\n"
		"**What it needs:** an address and a token. That is the whole list.\n"
	)

	assert _silent_about_git({"a page": page}) == [
		"a page, under 'An agent, with nothing installed'"
	]


def test_the_git_scan_leaves_a_section_that_names_it_alone () -> None:
	"""And it must not fire on the fixed wording, or it would be switched off within a week."""

	page = (
		"## An agent, with nothing installed\n"
		"\n"
		"```console\n"
		f"$ {_MARKETPLACE} simonholliday/subroutine\n"
		"```\n"
		"\n"
		"**On your own machine you need Claude Code and Git** — the marketplace is a\n"
		"repository, and the command clones it.\n"
	)

	assert _silent_about_git({"a page": page}) == []


def test_the_git_scan_does_not_let_a_neighbouring_section_answer_for_one () -> None:
	"""The whole reason it is per section: a reader arrives at one situation and reads it."""

	page = (
		"## An agent, on the machine holding the work\n"
		"\n"
		f"$ {_MARKETPLACE} simonholliday/subroutine\n"
		"\n"
		"You will need Git for that, since the marketplace is a repository.\n"
		"\n"
		"## An agent, with nothing installed\n"
		"\n"
		f"$ {_MARKETPLACE} simonholliday/subroutine\n"
	)

	assert _silent_about_git({"a page": page}) == [
		"a page, under 'An agent, with nothing installed'"
	]


_TIMEOUT_STOP = re.compile(r"^TimeoutStopSec=(\d+)s\s*$", re.MULTILINE)


def test_the_documented_stop_timeout_outlasts_the_graceful_shutdown () -> None:
	"""`#567`. Two numbers in two files that have to agree, which is this project's own trap.

	The server stops accepting, gives what is in flight a stated window and exits; systemd's
	timeout is the outer bound. If the outer one were the shorter, systemd would SIGKILL a
	shutdown that was about to complete — the unbounded wait fixed by hand, with the operator
	still not choosing the number that decides it.

	The page states the inner figure in prose as well, because an operator setting
	`TimeoutStopSec` needs to know what it has to be longer *than*. Derived from the constant
	rather than repeated, so the two cannot drift.
	"""

	page = HOSTING.read_text(encoding="utf-8")
	grace = subroutine.cli.main.SHUTDOWN_GRACE_SECONDS

	stated = _TIMEOUT_STOP.search(page)

	assert stated, (
		"the published unit file sets no TimeoutStopSec, so an operator following it inherits "
		"systemd's 90-second default as the real answer to how long a stuck restart takes"
	)

	assert int(stated.group(1)) > grace, (
		f"the unit stops the service after {stated.group(1)}s while the server itself waits "
		f"{grace}s, so systemd would kill a shutdown that was going to finish"
	)

	assert re.search(rf"\b{grace}\s+seconds\b", page), (
		f"the page never says the server waits {grace} seconds, so the number TimeoutStopSec "
		"has to exceed is not written anywhere the operator setting it can read"
	)


#: Ways this repository has claimed that ``config.toml`` carries nothing worth protecting.
#: Derived by running the scan and reading what it caught — **five** sites, where `#828`'s
#: review had found three and I had guessed four. Each is a phrase somebody wrote while
#: arguing something true about *tokens*, which is why they all read as reasonable.
_DENIES_A_SECRET = re.compile(r"holds? no secrets|no secrets live", re.IGNORECASE)

#: Where the claim may still appear, with the reason for each.
#:
#: ``CHANGELOG.md`` quotes what changed, so an entry describing the old behaviour has to keep
#: its old wording — the same exclusion `#753` made for the standfirst scan, and for the same
#: reason: a changelog edited to match the present tense stops being a record.
#:
#: This file holds the pattern and the sentences it was falsified against, so it matches itself
#: — `#546`'s shape, met immediately on the first run. The alternative is a scan that cannot be
#: shown to work, which is worse than one that has to skip one file by name.
_MAY_STILL_SAY_IT = ("CHANGELOG.md", "tests/test_documentation.py")


def test_nothing_claims_the_config_file_holds_no_secrets () -> None:
	"""`#831`. ``config.toml`` is ``0600`` and holds ``secret_key``, which ``init`` always writes.

	**The claim was in five tracked files and one of them contradicted itself four lines
	apart** — ``cli/main.py`` said ``init`` writes only ``secret_key`` and then gave "this file
	holds no secrets" as the reason not to write a database password beside it. Every one was
	written while arguing something true about *tokens*, which is what made them all read
	reasonably and none of them get checked.

	**This is a scan over a spelling and that is a weaker guard than this repository likes**,
	so it is worth saying what it can and cannot do. It cannot notice a fresh way of saying the
	same thing. What it can do is stop these five coming back, which is the failure mode that
	actually happened: the correct sentence already existed in ``docs/hosting.md`` the whole
	time, and five other places went on disagreeing with it.

    The pattern was scoped by running it rather than by reasoning about it — ``import secrets``
	and ``token=secret`` are what an eager version catches, and neither is a claim about
	anything.
	"""

	listed = subprocess.run(
		["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
	)
	offenders: list[str] = []

	for name in listed.stdout.split("\0"):
		path = ROOT / name

		if not name or not path.is_file() or name in _MAY_STILL_SAY_IT:
			continue

		try:
			text = path.read_text(encoding="utf-8")

		except UnicodeDecodeError:
			continue

		offenders.extend(
			f"{name}:{number}"
			for number, line in enumerate(text.splitlines(), start=1)
			if _DENIES_A_SECRET.search(line) and "used to" not in line
		)

	assert not offenders, (
		"config.toml is 0600 and holds secret_key, so these say something untrue about it: "
		+ ", ".join(offenders)
	)


def test_the_secret_denial_scan_catches_the_sentence_it_was_written_for () -> None:
	"""Fed the real wording through the real pattern, so the scan is not vacuous.

	`#405`'s rule: a guard is tested by putting a defect through its own entry point. The two
	strings below are the ones that were in the tree before `#831`, character for character.
	"""

	assert _DENIES_A_SECRET.search("§12.3a is that this file holds no secrets")
	assert _DENIES_A_SECRET.search("No secrets live here. Where the tokens are is")
	assert _DENIES_A_SECRET.search("connections, urls, defaults. No secrets.") is None, (
		"the table cell is caught by the mode assertion in test_config, not by this"
	)
	assert not _DENIES_A_SECRET.search("import secrets")
	assert not _DENIES_A_SECRET.search("return prefix, secret")


#: How the connecting page counts its own contents. Written as a word, so the guard has to know
#: the words rather than scan for a digit.
_WAYS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def test_the_connecting_page_counts_its_own_ways_correctly () -> None:
	"""`#721`. The page opens with a number and then lists the situations it counted.

	**A number in prose that nothing checks is this repository's signature defect**, and it has
	been paid for at least twice: `#582`'s "eleven MCP tools" on the README's front page, where
	the guard written for that exact sentence returned zero hits on the whole file; and `#198`'s
	tool count, stale in four places at once. This page said *five* and gained a sixth reader
	here.

	**Derived from the table rather than from the headings**, deliberately. The table is what a
	reader is sent to — *find yourself in the table, read that one section* — so it is the copy
	that has to be right, and a section with no row is a section nobody is routed to.
	"""

	page = CONNECTING.read_text(encoding="utf-8")
	opening = page.split("\n\n", 2)[1]

	stated = [word for word in _WAYS if f"There are {word} ways" in opening]

	assert len(stated) == 1, f"the page does not open by counting its ways: {opening[:120]!r}"

	# Every row of the routing table, which is the block between its header rule and the first
	# blank line after it.
	table = page.split("| --- | --- | --- |\n", 1)[1].split("\n\n", 1)[0]
	rows = [line for line in table.splitlines() if line.startswith("|")]

	assert len(rows) == _WAYS.index(stated[0]) + 1, (
		f"the page says {stated[0]} ways and its table routes to {len(rows)}"
	)


#: A command written into prose rather than into a console block: `` `subroutine db upgrade` ``
#: in the middle of a sentence. The console blocks are read and *run* by the scan further down;
#: this catches the ones nobody would type from a transcript and everybody reads.
_NAMED_IN_PROSE = re.compile(r"`(subroutine\s+[^`]*)`")

#: Every page a stranger reads that names a command in prose.
#:
#: **`SECURITY.md` is here because nothing scanned it at all**, and it told a person patching a
#: vulnerability to run ``subroutine upgrade`` — removed by `#509`, which deliberately kept no
#: alias because the name had come to mean very nearly the opposite. So the one page whose
#: reader is in a hurry and cannot ask us pointed at a command that exits 2.
#:
#: The changelog is deliberately absent, for ``PUBLISHED``'s reason: it records what a release
#: did, and a release that renames a command has to be able to say the old name.
_PROSE_PAGES: tuple[pathlib.Path, ...] = (
	README,
	HOSTING,
	CONNECTING,
	ROOT / "SECURITY.md",
	ROOT / "CONTRIBUTING.md",
)


def test_every_command_named_in_prose_exists () -> None:
	"""The console blocks are run; the sentences around them were read by nothing.

	``tests/test_plugin.py`` puts this on the skill and :func:`test_the_readme_only_shows_commands_that_exist`
	puts it on the README's blocks — and a command named in the middle of a sentence went
	through neither. That is where the stale one was.

	Only the word after ``subroutine`` is checked, and only when it is a plain word: a page
	writing ``subroutine <ref>`` or ``subroutine --version`` is not naming a command, and a
	guard that guessed at those would be turned off rather than trusted. Measured over every
	page here before it was written — 37 spans, no false positives.
	"""

	registered = {
		command.name or (command.callback.__name__ if command.callback else "")
		for command in subroutine.cli.main.app.registered_commands
	} | {group.name for group in subroutine.cli.main.app.registered_groups if group.name}

	named: dict[str, set[str]] = {}

	for page in _PROSE_PAGES:
		for span in _NAMED_IN_PROSE.finditer(page.read_text(encoding="utf-8")):
			words = span.group(1).split()

			if len(words) > 1 and words[1].isalpha():
				named.setdefault(page.name, set()).add(words[1])

	assert sum(len(words) for words in named.values()) > 20, (
		f"only {named} were found, so this is reading almost nothing"
	)

	wrong = {
		name: sorted(words - registered - _NOT_A_COMMAND)
		for name, words in named.items()
		if words - registered - _NOT_A_COMMAND
	}

	assert not wrong, f"These pages name commands that do not exist: {wrong}"


def test_the_upgrade_transcript_is_an_upgrade_that_could_have_happened () -> None:
	"""Three impossible things in six quoted lines, and every one of them a rendered variable.

	The page opens by promising that every quoted output is what the program actually printed,
	"with only paths and hostnames moved". The upgrade transcript said the database was at one
	revision and then that it had been upgraded *from a different one* — two renderings of one
	variable, disagreeing. Its backup filename was missing the instance-name segment that every
	other example on the page carries. And its version and schema were a pair no release has
	ever had.

	**Checked against what the program does rather than against a copy of the transcript.** The
	two revisions have to be the same value because one variable prints them both, and the
	filename is rebuilt from the parts ``db/backup`` composes it from.

	**The version and the schema are deliberately not compared against
	``docs/releases.json``**, and the review that found the other two thought they should be.
	They cannot both hold: `#314` requires the ``expects schema`` line to name *this build's*
	head, because that is the line a reader compares against their own instance, and this
	build's head is by definition unreleased whenever a migration is unshipped. `#343` settled
	the other half — the version is illustrative and is not pinned, because pinning it would
	make every release edit this page. So the pair is a version from ``releases.json`` beside
	the schema the reader will actually see, and the third thing the review counted is a
	consequence of two decisions rather than a mistake.
	"""

	page = HOSTING.read_text(encoding="utf-8")
	quoted = re.search(
		r"Subroutine (?P<version>\S+) expects schema (?P<head>\w+)\.\n"
		r"\s*The database is at (?P<at>\w+)\.\n"
		r"\s*About to upgrade[^\n]*\n"
		r"\s*Backed up to (?P<backup>\S+) \([\d,]+ bytes\)\.\n"
		r"\s*Upgraded from (?P<origin>\w+) to (?P<reached>\w+)\.",
		page,
	)

	assert quoted is not None, "the upgrade transcript is not on the page in the shape it had"

	assert quoted["at"] == quoted["origin"], (
		f"the database is said to be at {quoted['at']} and then upgraded from "
		f"{quoted['origin']} — one variable, printed twice, disagreeing"
	)
	assert quoted["head"] == quoted["reached"], (
		f"it expects {quoted['head']} and reaches {quoted['reached']}"
	)

	# The filename `db/backup` composes: the program, the instance, the instant, the revision
	# it was taken at. The instance segment is what the old example had lost.
	name = pathlib.PurePosixPath(quoted["backup"]).name
	parts = name.removesuffix(".sql").split("-")

	assert parts[0] == "subroutine", name
	assert parts[1] == "default", f"{name} names no instance, and every backup here does"
	assert parts[-1] == quoted["origin"], f"{name} is not stamped with the revision it holds"

	published = {
		release["version"] for release in json.loads(RELEASES.read_text(encoding="utf-8"))["releases"]
	}

	assert quoted["version"] in published, (
		f"the transcript quotes version {quoted['version']}, which was never released — the "
		f"version is illustrative but it should still be one somebody could have installed"
	)


def _signposts () -> set[str]:
	"""Return the commands that exist only to say where they went.

	Derived from the callback's name rather than listed, so it cannot fall behind — and
	asserted non-empty, because a derivation that finds nothing reads exactly like a tree with
	nothing to find. `#509` is the one there is: ``subroutine upgrade`` is registered, hidden,
	and prints a sentence naming ``subroutine db upgrade``.
	"""

	found = {
		command.name
		for command in subroutine.cli.main.app.registered_commands
		if command.callback is not None and command.callback.__name__.endswith("_moved")
	}

	assert found, (
		"no command reads as a signpost, so either they are all gone or the suffix that "
		"marks one has been renamed — see cli/main.upgrade_moved"
	)

	return {name for name in found if name}


def test_no_page_tells_somebody_to_run_a_command_that_only_says_it_moved () -> None:
	"""Which is what the last guard cannot see, because such a command *does* exist.

	`SECURITY.md` told a person patching a vulnerability to run ``subroutine upgrade``. That is
	still registered — `#509` kept it deliberately, because Typer's nearest match for it is
	``update``, which edits a task, so a bare removal pointed an operator migrating a database
	at the command that renames things. It prints where the command went and stops.

	So *does this command exist* answers yes, and the page is still wrong: it sends somebody
	who is in a hurry down an extra round trip, on the one page whose reader cannot ask us.
	Naming one of these in prose is what is refused, and the changelog — which has to be able
	to say the old name — is deliberately not scanned.
	"""

	moved = _signposts()
	wrong = []

	for page in _PROSE_PAGES:
		for span in _NAMED_IN_PROSE.finditer(page.read_text(encoding="utf-8")):
			words = span.group(1).split()

			if len(words) > 1 and words[1] in moved:
				wrong.append(f"{page.name} says {' '.join(words[:2])!r}")

	assert not wrong, (
		"These pages tell somebody to run a command that exists only to say it moved: "
		+ ", ".join(sorted(wrong))
		+ ". Name where it went instead."
	)


#: Where else the number of ways to reach an instance is written down, and the phrase each one
#: uses. Two words apiece, because "three ways past the TLS refusal" and "Three ways in, and
#: they compose" are different counts of different things on the same pages — a guard matching
#: ``<word> ways`` alone would fire on those and be turned off.
_ALSO_COUNTS: tuple[tuple[str, str], ...] = (
	("README.md", "is organised by which of {word} situations you are in"),
	("README.md", "the {word} ways to reach an instance"),
	("docs/hosting.md", "organised by which of {word} situations a person reaching an instance"),
)


def test_every_page_that_counts_the_ways_in_counts_the_same () -> None:
	"""The guard above read one page, and the number is written on three.

	Its own docstring says a number in prose that nothing checks is this repository's signature
	defect — and it was written to check *the page that owns the number*, which left the two
	that quote it unguarded. `connecting.md` grew a sixth way; the README said five twice and
	the hosting page said five once, for as long as anybody cared to look.

	**The count is derived from the routing table**, exactly as the guard above derives it, so
	there is still only one place where the answer lives. What is added is the other readers,
	and each is matched on the whole sentence rather than on ``<word> ways`` — both pages
	*also* count other things in those words, and a guard that fired on those would be relaxed
	rather than fixed.
	"""

	page = CONNECTING.read_text(encoding="utf-8")
	table = page.split("| --- | --- | --- |\n", 1)[1].split("\n\n", 1)[0]
	ways = len([line for line in table.splitlines() if line.startswith("|")])
	word = _WAYS[ways - 1]

	wrong = []

	for name, phrase in _ALSO_COUNTS:
		text = (ROOT / name).read_text(encoding="utf-8")
		found = [
			written for written in _WAYS if phrase.format(word=written) in text
		]

		if found != [word]:
			wrong.append(f"{name} says {found or 'nothing recognisable'} where the table has {ways}")

	assert not wrong, (
		"These pages disagree with the number of ways connecting.md routes to: "
		+ "; ".join(wrong)
		+ ". The table is the answer; correct the prose, or correct the phrase here if the "
		+ "sentence was reworded."
	)


def test_the_prepared_section_check_survives_a_release_being_cut () -> None:
	"""**`SR#893`. The guard above blocked every release, and had never seen one.**

	`scripts/release.py` renames `## Unreleased` to `## <version> — <date>` as its first act, so
	the release commit has no section being prepared. The floor said that state meant *nothing
	was checked* and failed — on all four Python versions, in the Release workflow as well, so
	Build, TestPyPI, GitHub release and PyPI were all skipped and v0.7.0 shipped nothing.

	**Two things were one assertion.** *The scan read nothing* is a bug in the guard; *there is
	no draft yet* is a legitimate and brief state — it lasts from the release commit until the
	next change worth telling somebody about. Collapsing them made a correct changelog fail.

	Driven through the real entry point on the shape `release.py` actually leaves behind, so
	this checks the thing rather than a description of it.
	"""

	just_released = textwrap.dedent(
		"""\
		# Changelog

		## 0.7.0 — 2026-08-14

		### Added

		- Something.

		### Fixed

		- Something else.
		"""
	)

	sections = _changelog_headings(just_released)

	assert "0.7.0 — 2026-08-14" in sections, "the probe did not parse as a released section"
	assert PREPARING not in sections, "the probe is meant to have nothing being prepared"

	# The scan is what has to be non-empty. A changelog with nothing readable in it is the
	# failure the floor exists for, and it still is.
	assert not _changelog_headings("# Changelog\n\nNothing at all.\n")


def test_a_documented_default_is_the_default () -> None:
	"""`#931`. The settings table's *names* were checked and its *values* were not.

	The section above it says "a test fails the build if the two disagree", and one did — the
	guard beside this compares the first column only. So `rate_limit_per_minute` was published
	as 120 against a real 600, and `rate_limit_failures_per_minute` as 20 against 30: an
	operator sizing a proxy, or deciding whether the default was tight enough, was reading a
	number nothing had checked since it was typed.

	**Only literal defaults are compared, and that is the honest scope.** Several rows describe
	theirs in prose — "SQLite under `$XDG_DATA_HOME`" — because the value is derived and a
	backticked constant would be a worse answer than a sentence. Those are skipped by shape
	rather than by name, so a row that *becomes* a literal is covered without anybody adding it
	here.

	**Read from ``model_fields`` rather than from ``Settings()``**, which was the first version
	and was wrong: constructing one reads this machine's own `config.toml`, so it reported
	`protected` and `default_connection` as drift when what it had actually found was the
	developer's configuration. `tests/conftest.py` gives every test an empty XDG home for
	exactly this reason and the model's declared default is the thing being documented anyway.
	"""

	text = HOSTING.read_text(encoding="utf-8")
	heading = "### Every setting, and what it does"

	assert heading in text, f"docs/hosting.md no longer has a {heading!r} section to check"

	opening = text.index(heading)
	section = text[opening : text.index("\n## ", opening)]

	compared = 0
	wrong: list[str] = []

	for name, documented in re.findall(r"^\| `(\w+)` \| ([^|]+?) \|", section, re.MULTILINE):
		said = documented.strip()

		if not (said.startswith("`") and said.endswith("`")):
			continue

		field = subroutine.config.Settings.model_fields.get(name)

		if field is None:
			continue

		default = (
			field.default_factory()  # type: ignore[call-arg]
			if field.default_factory is not None
			else field.default
		)

		# A boolean is written `false` in a TOML-facing table and `False` in Python, and both
		# are honest; a list is written the way it is spelled in a config file.
		spellings = (
			{str(default).lower()} if isinstance(default, bool) else {str(default), repr(default)}
		)

		compared += 1

		if said.strip("`") not in spellings:
			wrong.append(f"{name}: the page says {said} and the default is {default!r}")

	assert not wrong, (
		"docs/hosting.md publishes a default that is not the default:\n  "
		+ "\n  ".join(wrong)
	)

	# The floor. Every row could stop being a literal — by somebody rewording the column, or
	# by the regex drifting — and a comparison of nothing passes silently.
	assert compared >= 10, f"only {compared} defaults were literal enough to compare"


def _readme_rows () -> dict[str, bool]:
	"""Return the README's feature table: the row's text, and whether it claims **Built**.

	The table is a two-column Markdown one whose right cell is exactly ``**Built**`` or
	``Planned``. Anything else in the file is not a row of it and is skipped, which keeps this
	from reading the transcripts and the install table as features.
	"""

	found: dict[str, bool] = {}

	for line in README.read_text(encoding="utf-8").splitlines():
		row = re.match(r"^\|\s*(.+?)\s*\|\s*(\*\*Built\*\*|Planned)\s*\|$", line)

		if row is not None:
			found[row.group(1)] = row.group(2) != "Planned"

	return found


def test_nothing_the_api_calls_unbuilt_is_advertised_as_built () -> None:
	"""`#927`'s H-19 — the README's table drifted for fifteen commits and nothing read it.

	Five rows were wrong when the review found them, and **all five understated**: recurring
	tasks and re-parenting were shipped and still marked *Planned*, the board too, and the
	agenda front page. A page whose premise is *"a tool that overstates itself wastes your
	afternoon"* sent a reviewer looking for a feature that had landed a fortnight earlier.

	**This does not check the whole table and says so.** Most rows name something no program
	can be asked about. What it does check is the overlap with a list that *is* already
	guarded: ``api.meta.UNBUILT`` is the sentence `/v1/docs/agent` tells every agent, and
	`#355` fails the build when the app serves something it names. So the two published claims
	about what does not exist yet cannot disagree — which is the half where being wrong reaches
	a reader who has no way to check.

	A row's *wording* is still nobody's to verify but a person's. This is a floor under it.
	"""

	rows = _readme_rows()

	assert len(rows) > 30, f"only {len(rows)} feature rows were read from {README}"

	for name, _fragment in subroutine.api.meta.UNBUILT:
		claimed = [row for row, built in rows.items() if built and name.split()[0] in row.lower()]

		assert not claimed, (
			f"/v1/meta calls {name!r} unbuilt and the README lists it as Built: {claimed}"
		)


def test_the_readme_has_both_kinds_of_row () -> None:
	"""The floor under the floor: a scan that read one kind would satisfy the test above.

	If the pattern stopped matching ``Planned`` rows the comparison becomes vacuous — every
	unbuilt feature would be trivially unclaimed — and a table entirely of **Built** would read
	exactly the same way. Both counts, so a regex that has drifted fails here by name.
	"""

	rows = _readme_rows()

	assert sum(rows.values()) > 25, "no Built rows were read"
	assert sum(not built for built in rows.values()) > 5, "no Planned rows were read"


#: What a search actually reads, so a hint naming one of these is a claim about the mechanism
#: rather than about the reader's outcome. Kept as words rather than derived from
#: `search.anywhere`, which takes its columns from four call sites and so has no single list to
#: read — and the failure being prevented is prose, so the words are the right subject.
_SEARCH_MECHANISM = ("title", "description", "body", "bodies", "comment", "prose")

#: The one place each surface tells a reader what search does. A fourth surface with a hint of
#: its own belongs here; the floor below is what refuses a scan that stopped finding them.
SEARCH_HINTS = {
	"browser": (
		ROOT / "src" / "subroutine" / "web" / "assets" / "app.js",
		re.compile(r'placeholder="(Search[^"]*)"'),
	),
	"agent": (
		ROOT / "src" / "subroutine" / "mcp" / "tools.py",
		re.compile(r'description="(Find items[^"]*)"'),
	),
	"terminal": (
		ROOT / "src" / "subroutine" / "cli" / "personal.py",
		re.compile(r'"""(Find things[^\n]*)'),
	),
}


def test_no_search_hint_names_the_places_a_search_looks () -> None:
	"""`#1009`, Simon 2026-08-18, reading the browser's own search box.

	It said *"Search titles and descriptions"*, which had been false since `#823` widened search
	to read comments — and the agent's said *"in titles and bodies"*, wrong the same way, and the
	terminal's named the title and what you wrote about an item. Three surfaces, three wordings,
	none of them checked, all of them understating.

	**The rule is about which question the hint answers.** *Where the program looks* is a
	mechanism and changes as the product improves; *what the reader gets* does not. That is this
	project's own Voice rule and the coding-style skill's — prefer the outcome over the
	mechanism — and the evidence is in the three: the terminal's was the vaguest and is the only
	one that survived, because "what you wrote about them" covers a comment where "descriptions"
	cannot.

	**`#582`'s shape, and `test_no_published_page_counts_the_tools` is its sibling.** A countable
	claim is false the moment one is added; an enumerated one is false the moment somewhere else
	is read. Both are silent, and both were found by a person reading rather than by the suite.

	**Not a check that the wording is identical.** Three surfaces address three readers at three
	lengths, and demanding one string would be the divergence this project actually has — two
	copies of one sentence that must agree — wearing a guard's clothes. What they must share is
	that none of them enumerates.
	"""

	found = {}

	for surface, (path, pattern) in SEARCH_HINTS.items():
		match = pattern.search(path.read_text(encoding="utf-8"))

		assert match is not None, (
			f"the {surface}'s search hint was not found in {path.name}, so this checked "
			f"nothing there — the scan has gone stale rather than the tree having got better"
		)

		found[surface] = match.group(1)

	naming = [
		f"{surface}: {hint!r} names {word!r}"
		for surface, hint in found.items()
		for word in _SEARCH_MECHANISM
		if word in hint.casefold()
	]

	assert not naming, (
		"a search hint lists where a search looks, which is a claim that goes stale the next "
		f"time it reads somewhere else: {'; '.join(naming)}. Say what the reader gets instead "
		"— 'Search anything' is the browser's, and Simon's, answer."
	)
