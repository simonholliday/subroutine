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
``SUBROUTINE_TEST_REQUIRE_BROWSER=1`` turns the skip into a failure. **Never remove that
variable to make a red build green** — the skip exists so a machine without a browser can
still run everything else, and in CI it would mean reporting success on half a test run.
"""

import functools
import json
import os
import pathlib
import re
import typing

import pytest

import subroutine.api.web
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


#: Why this file cannot run here, or None. Worked out at import because that is where
#: `pytestmark` needs it.
UNAVAILABLE = _unavailable()

#: **Refusing the skip is a collection error, not a mark** (`#795`). The first version of this
#: was a `pytest_collection_modifyitems` hook in this module — and **pytest registers hooks from
#: `conftest.py` and from plugins, never from a test module**, so it never ran. Every test here
#: *errored* in CI for want of a browser instead of skipping, on six commits, while the local
#: gate stayed green.
#:
#: It was undetectable from this machine by construction: Chromium launches here, so
#: `UNAVAILABLE` is None and the skip path was dead from the day it was written. *A test that
#: cannot fail reads exactly like the point of the test*, one layer out.
if UNAVAILABLE is not None and os.environ.get("SUBROUTINE_TEST_REQUIRE_BROWSER") == "1":
	raise RuntimeError(
		f"SUBROUTINE_TEST_REQUIRE_BROWSER is set and {UNAVAILABLE}. Install one with "
		f"'playwright install chromium', or unset the variable — but not to make a red build "
		f"green: the skip exists so a machine without a browser can run everything else, and "
		f"here it would mean reporting success on a suite that did not run."
	)

#: Skipped rather than failed where there is simply no browser, exactly as PostgreSQL is.
pytestmark = pytest.mark.skipif(
	UNAVAILABLE is not None, reason=UNAVAILABLE or "a browser is available"
)


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


#: The smallest set of answers the app needs to paint a listing. Written here rather than
#: reused from `test_web` because these are *wire* bodies for a browser, and that module's are
#: arguments to a render — the same values, and conflating them would make one file's fixture
#: quietly decide the other's coverage.
IDENTITY = {
	"user": {"username": "si", "is_service_account": False},
	"workspaces": [{"slug": "projects", "id": "w1", "role": "owner", "permissions": []}],
	"instance_permissions": [],
	"credential": None,
}

EMPTY = {"items": [], "page": {"has_more": False, "next_cursor": None, "total": None}}

#: A workspace's own vocabulary, which is what a column has to be resolved through. Renamed and
#: with the default second, because a board choosing by key would be right only here.
META = {
	"statuses": {"task": [
		{"key": "triage", "label": "Triage", "category": "todo", "is_default": False},
		{"key": "ready", "label": "Ready", "category": "todo", "is_default": True},
		{"key": "doing", "label": "Under way", "category": "in_progress", "is_default": True},
	]},
	"item_types": {}, "link_types": [], "linkable_types": [], "workspaces": [],
}

#: The one row every page here is built from. Named separately from the envelope because a
#: write is answered with the *item* and a read with a collection, and inferring the pair from
#: one literal gave the row a type nothing could index.
CARD: dict[str, typing.Any] = {
	"ref": 42, "kind": "task", "title": "Fix the pagination cursor", "project_key": "ui",
	"status_category": "todo", "created_at": "2026-08-10T14:22:00+00:00",
}

#: The row for :data:`CARD` itself, by its own address rather than by position.
CARD_ROW = ".rows li:has(a[href$='/42'])"

#: Five more cards in the same column, so the board has a tall column and an empty one — the
#: shape `#796` failed on, and the one a person meets on their first board.
CROWD = [dict(CARD, ref=100 + n, title=f"Task number {n}") for n in range(5)]

#: One page of rows, in the envelope every listing here uses. Enough for a link to click.
ROWS = {
	"items": [CARD, *CROWD],
	"page": {"has_more": False, "next_cursor": None, "total": None},
}


@pytest.fixture(scope="module")
def running (looks: typing.Any) -> typing.Iterator[typing.Any]:
	"""The real app, in a real browser, with every request answered out of memory.

	**A served page without a server**, which is the whole point: `page.route` hands the
	browser `api.web.FILES` — the same bytes the instance serves, import map and all — and
	fixture JSON for `/v1`. No socket, no database, no process to leak, and §2.2's *served as
	written* promise is what makes the substitution honest.

	It is what `tests/dom.js` cannot be. That shim mounts the app and records what it asks for;
	this one has a cascade, a layout and real events, which is the half `#722` and `#711` need
	and nothing else here can reach.
	"""

	shell = subroutine.api.web.FILES[subroutine.api.web.SHELL][0]
	#: Every write the page made, so a gesture can be checked by what it sent rather than by
	#: what the page then looks like — the request is the fact and the render is a consequence.
	written: list[tuple[str, str, str | None]] = []
	#: What `/v1/tasks` answers, so one test can ask about a board with rows and the same board
	#: without them. A holder rather than an argument to `answered`, because the route is
	#: registered on the context once and every page shares it.
	listing: list[typing.Any] = [ROWS]

	def answered (route: typing.Any) -> None:
		"""Serve one request the way the instance would.

		``fulfill`` is Playwright's spelling and not this project's; it is an API name rather
		than prose, so it stays as the library writes it.
		"""

		wanted = route.request.url.split("://", 1)[-1].split("/", 1)[-1].split("?")[0]

		if wanted.startswith("app/"):
			body, kind = subroutine.api.web.FILES[wanted.split("/", 1)[1]]
			route.fulfill(status=200, body=body, content_type=kind)

			return

		if wanted.startswith("v1/"):
			if route.request.method != "GET":
				written.append((route.request.method, wanted, route.request.post_data))
				route.fulfill(
					status=200, body=json.dumps(CARD),
					content_type="application/json",
				)

				return

			# **The narrower path first**, and this one bit: `"v1/meta".startswith("v1/me")` is
			# true, so the vocabulary was served the identity body — and the only symptom was a
			# drop refusing itself with *there is no status here that means in progress*, which
			# reads exactly like a workspace configured that way. `#722`'s fixture carries the
			# same warning about `/v1/tasks/42` inside `/v1/tasks/42/links`; it is the same
			# defect and this is the third place it has appeared.
			answer = (
				META if wanted.startswith("v1/meta")
				else IDENTITY if wanted.startswith("v1/me")
				else listing[0] if wanted.startswith("v1/tasks")
				else EMPTY
			)

			route.fulfill(
				status=200, body=json.dumps(answer), content_type="application/json"
			)

			return

		# Every other address is the app's own — `#638` gave an item one, and `#648` made
		# anything unclaimed a 404 keyed on `Accept`, which for a browser is this page.
		route.fulfill(status=200, body=shell, content_type="text/html; charset=utf-8")

	_, bare = looks
	# **Its own context, not the styling pages'.** A page made by `browser.new_page()` owns the
	# context it was given, so a second page cannot be opened from it — and a ctrl-click's whole
	# observable is a *second page appearing in the context*. Measured by the refusal.
	context = bare.context.browser.new_context()

	# **Routed on the context, not on the page**, and the difference is the whole of what this
	# fixture exists to observe: a page routes only itself, so a tab the *browser* opened had no
	# handler, could not resolve `app.test`, and sat at `about:blank`. The one test here about
	# a new tab would have been asserting on a tab that never loaded.
	context.route("**/*", answered)

	def opened (address: str = "/", rows: typing.Any = None) -> typing.Any:
		"""Open one address and wait for the app to have painted."""

		listing[0] = ROWS if rows is None else rows

		page = context.new_page()
		page.goto(f"http://app.test{address}")
		page.wait_for_selector(".app", timeout=10_000)

		return page

	try:
		yield opened, written
	finally:
		context.close()


def test_a_modified_click_still_belongs_to_the_browser (running: typing.Any) -> None:
	"""`#722`'s entire value, verified by nobody until there was a browser here.

	Every navigation in this app is a real anchor so that *open in a new tab*, *copy link
	address*, middle-click and the context menu all work — and `opens` decides which clicks the
	app keeps. That function is pure and covered; **whether a real browser then does what it
	predicts was covered by nothing**, and it is the half that reaches a reader.
	"""

	opened, _written = running
	page = opened("/projects")

	page.wait_for_selector("a[href]", timeout=10_000)

	with page.context.expect_page(timeout=10_000) as opened:
		page.click("a[href*='42']", modifiers=["Control"])

	tab = opened.value

	assert tab is not page, "a ctrl-click was handled by the app instead of the browser"

	# **Waited for, because a tab exists before it has been anywhere.** Read immediately it is
	# `about:blank`, which would have made this assert about timing rather than about the click.
	tab.wait_for_load_state("domcontentloaded")

	assert "42" in tab.url, f"the new tab opened somewhere else: {tab.url}"

	tab.close()


def test_a_card_is_draggable_on_the_board_and_nowhere_else (running: typing.Any) -> None:
	"""`#711`. A card that lifts with nowhere to drop it puts itself back.

	That is this codebase's inert-control defect in the one place a reader *feels* rather than
	reads — so the gesture is offered where a column can receive it and withheld on a list.

	**Here rather than in ``tests/test_web.py``** because that harness carries `href` through
	and nothing else by decision: it is a text harness, and an attribute is not text.
	"""

	opened, _written = running
	board = opened("/projects?view=board")

	board.wait_for_selector(".board .rows li", timeout=10_000)

	assert board.eval_on_selector_all(".rows li[draggable='true']", "found => found.length"), (
		"no card on the board can be lifted"
	)

	listed = opened("/projects?view=list")

	listed.wait_for_selector(".listing .rows li", timeout=10_000)

	assert not listed.eval_on_selector_all(
		".rows li[draggable='true']", "found => found.length"
	), "a row on a list claims to be draggable and has nowhere to be dropped"


def test_dragging_a_card_to_another_column_moves_it (running: typing.Any) -> None:
	"""`#711`, and the gesture is the whole of what could not be checked before.

	**Asserted on the request rather than on the page**: the write is the fact and the render is
	a consequence of it, and a board that looked right while sending the wrong status is exactly
	the shape this project keeps finding.

	`ready` rather than `triage`, and that is the point of the fixture — this workspace's `todo`
	holds two, and the one it calls ordinary is the second. A board choosing by key would pass
	against `seed.py` and fail on the first instance that renames anything.
	"""

	opened, written = running
	page = opened("/projects?view=board")

	page.wait_for_selector(".board .rows li[draggable='true']", timeout=10_000)
	written.clear()

	# **Named rather than *the first one***, because the board holds a crowd since `#796` and
	# whichever card is first is an accident of ordering rather than the subject of this test.
	page.drag_and_drop(f"{CARD_ROW}", "section.column:nth-of-type(2)")
	page.wait_for_timeout(300)

	moves = [one for one in written if one[0] == "PATCH"]

	assert moves, f"the drop wrote nothing: {written}"

	_method, where, body = moves[0]

	assert "42" in where, f"the write went to the wrong item: {where}"
	assert body is not None and json.loads(body) == {"status": "doing"}, (
		f"a drop on In progress sent {body!r} — a column is a category and the status has to "
		f"come from the workspace's own vocabulary"
	)


def test_a_card_dropped_where_it_already_was_is_not_a_write (running: typing.Any) -> None:
	"""`#711`. The commonest way a drag ends is somebody thinking better of it.

	Reporting *#42 is in progress* about a card nobody moved is a true-sounding falsehood, which
	is the shape this project keeps finding — and a wasted `PATCH` on every abandoned gesture
	besides.

	**Found by falsifying**: removing the guard left every test green, because the only drag
	being driven went somewhere else. A mutation that survives is a finding about the tests.
	"""

	opened, written = running
	page = opened("/projects?view=board")

	page.wait_for_selector(".board .rows li[draggable='true']", timeout=10_000)
	written.clear()

	page.drag_and_drop(f"{CARD_ROW}", "section.column:nth-of-type(1)")
	page.wait_for_timeout(300)

	assert not [one for one in written if one[0] != "GET"], (
		f"a card dropped back where it started was written anyway: {written}"
	)


def test_a_column_is_a_drop_target_for_its_whole_height (running: typing.Any) -> None:
	"""`#796`, and it is the finding Simon's first-contact run produced that no test could.

	Measured before the fix, six cards in *To do* and none in *In progress*: **307px against
	78px**. The drop handler is on the column, so dragging sideways from anywhere below those
	78 pixels put the pointer over nothing — and *a full column to an empty one* is the
	commonest drag there is. `#711` shipped a gesture that mostly did not connect.

	**No test could see it and the reason matters.** `page.drag_and_drop` moves to the computed
	*centre* of the target, so it hits however small the element is; a synthetic gesture that
	teleports cannot discover that a target is too small to reach. The mechanism was right, the
	wiring was right, and the geometry — which only a browser has — was what failed.

	So this asks about the geometry, and then drops at the **bottom** of the empty column
	rather than at its middle, which is the point the old layout had nothing under.
	"""

	opened, written = running
	page = opened("/projects?view=board")

	page.wait_for_selector(".board .rows li[draggable='true']", timeout=10_000)

	heights = page.eval_on_selector_all(
		"section.column", "found => found.map((one) => one.getBoundingClientRect().height)"
	)

	assert len(heights) > 1, "a board with one column cannot show this"
	assert max(heights) - min(heights) < 2, (
		f"the columns are {heights} — a short one is a drop target only where it reaches, and "
		f"a card dragged from further down the tall one lands on nothing"
	)

	written.clear()
	page.drag_and_drop(
		".rows li[draggable='true']",
		"section.column:nth-of-type(2)",
		target_position={"x": 120, "y": max(heights) - 20},
	)
	page.wait_for_timeout(300)

	assert [one for one in written if one[0] == "PATCH"], (
		f"a card dropped at the foot of the next column wrote nothing: {written}"
	)

	# **And a board with nothing on it yet**, which stretching alone does not answer: four
	# columns of equal height are still four columns of almost no height, and the person moving
	# their first card is exactly the one who cannot afford that. Falsifying the `min-height`
	# left every check above green, because the tallest column had cards in it.
	bare = opened("/projects?view=board", rows=EMPTY)

	bare.wait_for_selector("section.column", timeout=10_000)
	empty = bare.eval_on_selector_all(
		"section.column", "found => found.map((one) => one.getBoundingClientRect().height)"
	)

	assert empty and min(empty) > 120, (
		f"an empty board's columns are {empty} — a person's first card has almost nowhere to "
		f"be dropped"
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

	**Counted in tests rather than in lines, which is a correction.** The first version bounded
	the file at 160 lines, fired on its author at 162, and was raised to 180 — then the fixture
	that serves the app took it to 235 with no test added. A line count measures the prose and
	the harness; `#748`'s scope is *ten tests answering what only a browser can*, so the bound
	is now the thing the argument is about. Infrastructure may grow; the surface may not.

	**And a list of forbidden words was the version before that**, which failed on its own
	list — `#546`'s shape for the third time here, and ``tests/dom.js`` records the same trap
	arriving through the word *click* inside the sentence forbidding it.
	"""

	source = pathlib.Path(__file__).read_text(encoding="utf-8")
	tests = re.findall(r"^def (test_\w+)", source, re.M)

	assert len(tests) > 1, "no tests were found, so this is checking nothing"

	assert len(tests) <= 10, (
		f"this file holds {len(tests)} tests: {tests}. Ten answering what only a browser can "
		f"is the agreed scope; past this it is a second suite, and the fast one is the one "
		f"that stops being run. Raising it is a decision — read the addition for fat first."
	)
