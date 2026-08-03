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
import subprocess
import typing

import pytest
import typer.testing

import subroutine.auth
import subroutine.cli.main
import subroutine.db.migrate

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOSTING = ROOT / "docs" / "hosting.md"
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


@pytest.mark.parametrize("page", [HOSTING, README, CHANGELOG], ids=lambda path: path.name)
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
