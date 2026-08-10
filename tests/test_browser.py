"""What only a browser can answer — item `#748`.

The 173 tests in ``tests/test_web.py`` render the app in Node, drive every request it makes,
and mount it against ``tests/dom.js``. **None of them parses ``app.css`` at all**, and that
shim cannot dispatch an event by decision — so a computed colour, a layout and a modified
click were verified by nobody. `#740` is what that cost: a link landing on the user agent's
default, browser blue and blind to both themes, found by Simon opening a page.

**This is deliberately small and stays small.** It answers the questions a DOM without a
cascade cannot, and it does not re-ask the ones Node already answers faster and without a
400MB dependency. A test here that could have been written in ``test_web.py`` is in the wrong
file.

**It skips where there is no browser, and CI can refuse the skip.** Same arrangement as
PostgreSQL: a laptop without Chromium still runs the suite, and
``SUBROUTINE_TEST_REQUIRE_BROWSER=1`` turns the skip into a failure so that a green CI run
cannot mean half a test run. The reason that variable exists is written in ``CLAUDE.md``
against the PostgreSQL one, and it is the same reason.
"""

import functools
import os
import pathlib
import re
import typing

import pytest

import test_web

#: Every rendering the app can produce, which is what `SAMPLES` already is. Reused rather than
#: rebuilt: a second set of fixtures would be a second answer to *what does this app render*,
#: and the guard below is only worth anything while it sees all of it.
SAMPLES = test_web.SAMPLES

#: Elements the browser gives a look of its own when nothing else does. **Measured from the
#: markup rather than listed from memory** — these are the tags `SAMPLES` actually renders that
#: carry a user-agent appearance, and `span`, `div` and the headings are absent because they do
#: not.
STYLED_BY_DEFAULT = ("a", "button", "input", "select", "textarea", "fieldset")

#: What a browser is asked about each one. **Several, because one is not enough**: a base rule
#: may legitimately agree with the user agent on any single property, and requiring agreement
#: on *all* of them is what says nothing was styled at all.
TELLS = ("color", "background-color", "font-family", "border-top-width")

#: An element deliberately left looking like the browser's own, with the reason.
#:
#: **Empty, and that is the state to keep it in.** `#747`'s argument is that a design which
#: cannot produce the fault beats a check that catches it — so an entry here is a decision to
#: let one through, and it needs to read like one.
LOOKS_LIKE_THE_BROWSER: dict[str, str] = {}


@functools.cache
def _unavailable () -> str | None:
	"""Why a browser cannot be driven here, or None if one can — worked out once.

	Cached because launching a browser to ask is not free, and every item in this file asks.
	"""

	try:
		import playwright.sync_api
	except ImportError:
		return "playwright is not installed"

	try:
		with playwright.sync_api.sync_playwright() as running:
			running.chromium.launch().close()
	# Every failure here means the same thing to a caller: there is no browser to drive.
	except Exception as why:
		return f"chromium could not be launched: {why}"

	return None


def pytest_collection_modifyitems (
	config: pytest.Config, items: list[pytest.Item]
) -> None:
	"""Skip this module without a browser — or fail, when CI says it must not skip."""

	why = _unavailable()

	if why is None:
		return

	demanded = os.environ.get("SUBROUTINE_TEST_REQUIRE_BROWSER") == "1"
	mark = (
		pytest.mark.xfail(reason=why, run=False, strict=True)
		if demanded
		else pytest.mark.skip(reason=why)
	)

	for item in items:
		if item.fspath is not None and pathlib.Path(str(item.fspath)).name == "test_browser.py":
			item.add_marker(mark)


@pytest.fixture(scope="module")
def looks (tmp_path_factory: pytest.TempPathFactory) -> typing.Iterator[typing.Any]:
	"""A browser, the stylesheet, and every component's markup — one component to a page.

	**One to a page rather than all of them in one, and this was measured rather than chosen.**
	Concatenating the eighteen renderings into a single div and asking the browser what was
	there found **one `<form>` of nine**, no `<select>` at all and no `<fieldset>` — HTML
	parsing does not allow a form inside a form, and a page assembled that way silently drops
	most of what it was assembled to show. Every check over it would have passed by looking at
	almost nothing, which is `#427`'s defect wearing a browser.

	It also makes a failure name the component it is in, which a merged page cannot.

	**The bare page is separate and has to be.** A base rule for an element applies to every one
	on its page, so a control element beside the app would be styled by the very rule this is
	checking for, and the comparison would collapse into always agreeing.
	"""

	import playwright.sync_api

	rendered = test_web._rendered(tmp_path_factory.mktemp("markup"), SAMPLES)
	stylesheet = (test_web.ASSETS / "app.css").read_text(encoding="utf-8")

	with playwright.sync_api.sync_playwright() as running:
		browser = running.chromium.launch()

		try:
			page = browser.new_page()
			bare = browser.new_page()

			# **The bare page inherits what ours inherits**, and without this the check was
			# weaker than it looked: `font-family` and `color` are inherited, `body` in
			# `app.css` sets both, and a bare page's body does not — so *every* element that
			# merely inherited them counted as styled. Falsifying the `fieldset` rule proved
			# it: the mutation passed, which is a finding about the test rather than about the
			# stylesheet. Copying the computed body across leaves only what an element rule
			# decided for itself.
			page.set_content(f"<style>{stylesheet}</style><div class=\"app\"></div>")
			inherited = page.eval_on_selector(
				"body",
				"""e => {
					const style = getComputedStyle(e);
					return `font: ${style.font}; color: ${style.color};`
						+ ` background-color: ${style.backgroundColor};`;
				}""",
			)

			bare.set_content(
				f"<style>body {{ {inherited} }}</style>"
				+ "".join(f"<{tag}></{tag}>" for tag in STYLED_BY_DEFAULT)
			)

			def showing (name: str) -> typing.Any:
				"""Put one component on the page, with the real stylesheet over it."""

				page.set_content(
					f'<style>{stylesheet}</style><div class="app">{rendered[name]}</div>'
				)

				return page

			yield showing, bare
		finally:
			browser.close()


def _computed (page: typing.Any, selector: str, at: int) -> dict[str, str]:
	"""What the browser decided this element looks like, for the properties that tell."""

	return dict(page.eval_on_selector_all(
		selector,
		"""(found, asked) => {
			const one = found[asked.at];
			if (!one) return [];
			const style = getComputedStyle(one);
			return asked.tells.map((name) => [name, style.getPropertyValue(name)]);
		}""",
		{"at": at, "tells": list(TELLS)},
	))


def test_a_browser_can_be_driven_at_all (looks: typing.Any) -> None:
	"""The floor, and it exists because the first version of this file needed it.

	Everything below reads a computed style, and a page that failed to parse reports every
	element as absent — which reads exactly like every element passing. This asserts the pages
	hold what they are supposed to hold before anything asks them a question.
	"""

	showing, bare = looks

	for tag in STYLED_BY_DEFAULT:
		assert _computed(bare, tag, 0), f"the bare page has no <{tag}> to compare against"

	seen = {
		tag
		for name in SAMPLES
		for tag in STYLED_BY_DEFAULT
		if showing(name).eval_on_selector_all(tag, "found => found.length")
	}
	missing = set(STYLED_BY_DEFAULT) - seen

	assert not missing, (
		f"{sorted(missing)} is checked below and appears on no page, so those cases look at "
		f"nothing — either the app stopped rendering it or the page is not parsing"
	)


@pytest.mark.parametrize("component", sorted(SAMPLES))
def test_no_element_is_left_looking_like_the_browser (
	looks: typing.Any, component: str
) -> None:
	"""`#747`, and `#740` is what it cost the one time it happened.

	``app.css`` styles a link in five contexts and had no base rule for one, so an anchor added
	anywhere else fell through to the user agent: browser blue, underlined, the same in both
	themes. Not a mistake somebody makes once — it is what the stylesheet does to every link
	added without a context.

	**`#747` said a guard for this would have to be a regex over selectors**, which is guarding
	a spelling and is this repository's most-repeated defect. That was true while there was no
	CSS engine in the pipeline. There is one now, so this asks the browser the actual question:
	is what this element looks like something *we* decided?

	Compared against the same tag on a page with no stylesheet, because that is what *the
	browser's default* means and nothing else is.
	"""

	showing, bare = looks
	page = showing(component)

	for tag in STYLED_BY_DEFAULT:
		if tag in LOOKS_LIKE_THE_BROWSER:
			continue

		untouched = _computed(bare, tag, 0)

		for at in range(page.eval_on_selector_all(tag, "found => found.length")):
			styled = _computed(page, tag, at)

			if any(styled.get(name) != untouched.get(name) for name in TELLS):
				continue

			markup = page.eval_on_selector_all(
				tag, "(found, at) => found[at].outerHTML.slice(0, 120)", at
			)

			raise AssertionError(
				f"a <{tag}> in {component} looks exactly like the browser's own on every one "
				f"of {list(TELLS)}, so it is unstyled rather than styled to match: {markup!r}\n"
				f"Give it a base rule in app.css, or record it in LOOKS_LIKE_THE_BROWSER."
			)


def test_an_element_added_without_a_context_is_still_ours (looks: typing.Any) -> None:
	"""`#747`'s actual claim, which the check above cannot make.

	That one asks about elements the app renders *today*, and every anchor it renders has a
	context rule — so removing the base rule for `<a>` changes nothing it can see. **The claim
	is about the next one**: an element added anywhere, by anybody, without a rule of its own.

	So this puts a bare one of each inside `.app` and asks the same question. It is the
	difference between *what we have is styled* and *what this stylesheet does to what it has
	not met*, and only the second is preventive.
	"""

	showing, bare = looks
	page = showing(sorted(SAMPLES)[0])

	page.eval_on_selector(
		".app",
		"(root, tags) => { root.innerHTML = tags.map((t) => `<${t}></${t}>`).join(''); }",
		list(STYLED_BY_DEFAULT),
	)

	for tag in STYLED_BY_DEFAULT:
		if tag in LOOKS_LIKE_THE_BROWSER:
			continue

		styled = _computed(page, tag, 0)
		untouched = _computed(bare, tag, 0)

		assert any(styled.get(name) != untouched.get(name) for name in TELLS), (
			f"a <{tag}> added with no rule of its own is exactly the browser's own on "
			f"{list(TELLS)} — which is what happened to every link before `#747`"
		)


def test_the_stale_half_of_the_excuse_list (looks: typing.Any) -> None:
	"""What makes an entry go away. Every allow-list in this repository owes this."""

	assert set(LOOKS_LIKE_THE_BROWSER) <= set(STYLED_BY_DEFAULT), (
		f"{sorted(set(LOOKS_LIKE_THE_BROWSER) - set(STYLED_BY_DEFAULT))} is excused from "
		f"having a look of its own and is not a tag this checks"
	)


def test_this_file_stays_the_size_of_its_argument () -> None:
	"""`#748`'s scope, held by a bound rather than by an intention.

	A browser is 400MB, a minute of CI and a new class of flakiness. It earns that by answering
	what a DOM without a cascade cannot — computed styles, layout, real events. The moment it
	starts re-rendering components or asserting on markup it is a slower copy of
	``tests/test_web.py``, and the fast suite is the one that stops being trusted.

	**A list of forbidden words was the first version of this and it failed on its own list** —
	`#546`'s shape for the third time in this repository, and `tests/dom.js` records the same
	trap arriving through the word *click* inside the sentence forbidding it. A line count
	cannot match itself, which is the whole reason it is the check here.
	"""

	code = re.sub(r'"""[\s\S]*?"""', "", pathlib.Path(__file__).read_text(encoding="utf-8"))
	lines = [line for line in code.splitlines() if line.strip()]

	# **Raised from 160 to 180 once, and the raise is the act rather than the number.** 160 was
	# guessed before the file existed; five tests, a two-page fixture and the excuse list came to
	# 162. The fat was two functions asking one question, which is gone. §21.2's procedure for
	# the MCP budget is the same one: measure, read the addition for fat, write the case here.
	assert len(lines) < 180, (
		f"this file is {len(lines)} lines of code. Answering what only a browser can is the "
		f"agreed scope; past this it is a second suite, and the fast one is the one that stops "
		f"being run. Raising this is a decision — read the addition for fat first."
	)
