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

import pathlib
import re
import shlex
import subprocess
import typing

import click
import pytest
import typer.main
import typer.testing

import subroutine.auth
import subroutine.cli.main
import subroutine.config
import subroutine.db.migrate

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOSTING = ROOT / "docs" / "hosting.md"
CONNECTING = ROOT / "docs" / "connecting.md"
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"


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
	re.compile(r"This version expects schema ([0-9a-f]{12})\."),
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


@pytest.mark.parametrize(
	"page", [HOSTING, CONNECTING, README, CHANGELOG], ids=lambda path: path.name
)
def test_published_pages_link_only_to_files_that_exist (page: pathlib.Path) -> None:
	"""A relative link in published documentation is a promise about the repository.

	Worth having because the README's links point *out* of its own directory: moving or
	renaming anything under ``docs/`` breaks them from a distance, and a dead link in the file
	GitHub renders first is read as a project that has stopped being maintained.
	"""

	text = page.read_text(encoding="utf-8")
	checked = 0

	for fragment in text.split("](")[1:]:
		target = fragment.split(")")[0].split("#")[0]

		if not target or target.startswith(("http://", "https://", "mailto:")):
			continue

		assert (page.parent / target).exists(), f"{page.name} links to a missing {target}"
		checked += 1

	# Otherwise this passes just as happily on a page whose links have all been deleted, which
	# is the failure mode a link checker is least able to notice about itself.
	assert checked, f"{page.name} has no relative links — has this test stopped reaching them?"


def _anchors (page: pathlib.Path) -> set[str]:
	"""Return the anchors a heading in this page can be linked to.

	Derived the way GitHub derives them — lower-cased, everything but word characters, spaces
	and hyphens dropped, spaces turned into hyphens — because that is what the link resolves
	against. Reading the headings and trusting a hand-written anchor beside them would check
	the wrong half.
	"""

	found = set()

	for line in page.read_text(encoding="utf-8").splitlines():
		if line.startswith("#"):
			heading = line.lstrip("#").strip()
			found.add(re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-"))

	return found


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

	for fragment in text.split("](")[1:]:
		target = fragment.split(")")[0]

		if target.startswith(("http://", "https://", "mailto:")) or "#" not in target:
			continue

		path, _, anchor = target.partition("#")
		where = page if not path else page.parent / path

		if not where.is_file():
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
_COUNTS_TOOLS = re.compile(
	r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
	r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\s+tools\b",
	re.IGNORECASE,
)


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
		f"{README.name}: {found.group(0)!r}"
		for found in _COUNTS_TOOLS.finditer(README.read_text(encoding="utf-8"))
	]

	assert not wrong, (
		"a published page states how many MCP tools there are, which is true until the next one "
		f"is added and then quietly false: {', '.join(wrong)}. Say what they do instead — "
		"tests/test_mcp.py is where the number lives, because there it can fail."
	)


def test_the_tool_count_scan_would_notice_one () -> None:
	"""Falsified through the pattern itself, since a regex that matches nothing passes above."""

	assert _COUNTS_TOOLS.search("Eleven tools: list, search, show"), "the spelt-out form"
	assert _COUNTS_TOOLS.search("all 14 tools are"), "and the digits"
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
		"sr_d78d5d93_hU5ak4GqR_E2GyX2lC0Zq8Mz5JA1kbm-byrlb5hXEfY",
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
_NOT_A_COMMAND = frozenset({"add", "today", "done", "help", "explain"})


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
	``uv``/``claude`` lines are somebody else's programs and are skipped by name; everything
	the README asks of *this* one is run, in order, against an empty XDG home.
	"""

	home = tmp_path / "fresh"

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(home / variable.lower()))

	block = next(one for one in _blocks(README) if _THE_AGENT_PATH in one)
	ours = [line for line in _typed(block) if line.startswith("subroutine ")]

	assert ours, (
		"the agent block asks nothing of subroutine itself, so a reader following it has no "
		"instance — which is what `#399` was"
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
		[_page(tmp_path, "claude mcp add subroutine -- subroutine mcp", "subroutine today")]
	)

	assert [invocation.line for invocation in found] == ["subroutine today"]


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
