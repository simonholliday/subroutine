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
import pathlib
import re
import shutil
import typing
import urllib.parse

import pytest

import conftest
import subroutine.api.policy
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

#: The policy headers this instance really sends (`#805`). Taken from the module that builds
#: them rather than written out, because the value under test is the one an operator gets — and
#: the import map is allowed by a *hash* of the served bytes, so a copy here would be right only
#: until the map changed and the symptom would be a blank page.
POLICY = subroutine.api.policy.headers()

#: An element deliberately left looking like the browser's own, with the reason.
#:
#: **Empty, and that is the state to keep it in.** `#747`'s argument is that a design which
#: cannot produce the fault beats a check that catches it — so an entry here is a decision to
#: let one through, and it needs to read like one.
LOOKS_LIKE_THE_BROWSER: dict[str, str] = {}


@functools.cache
def _unavailable () -> str | None:
	"""Why this file cannot run here, and what to do about it — or None if it can.

	Cached because launching a browser to ask is not free, and every item in this file asks.

	**Each answer carries its own remedy**, because there are three causes now and one shared
	remedy is wrong for two of them. The first version of the refusal below said *install one
	with 'playwright install chromium'* whatever the reason, so the very first run of the new
	Node check advised a command that cannot supply Node — `#734`'s rule, met in the change
	that made it reachable.
	"""

	# **Node before the browser, and that omission was the whole of `#927`'s H-17.** Every
	# fixture here renders the app in Node before Chromium ever sees it, so a machine with a
	# browser and no runtime cannot run this file — but this function asked only about the
	# browser, so `SUBROUTINE_TEST_REQUIRE_BROWSER` was satisfied, nothing raised, and each
	# test then skipped *inside* the fixture. Measured on a Node-less PATH: 1 passed, 37
	# skipped, exit 0, from the variable whose entire purpose is refusing that skip. The
	# defect this file's guard exists to close, one level along and wearing its own clothes.
	#
	# Cheapest question first, too: this is a PATH lookup where the next one starts a browser.
	if shutil.which("node") is None:
		return (
			"there is no JavaScript runtime on PATH and every fixture here renders in one, "
			"so install Node"
		)

	try:
		import playwright.sync_api
	except ImportError:
		return "playwright is not installed, so install it with `pip install -e '.[dev]'`"

	try:
		with playwright.sync_api.sync_playwright() as running:
			running.chromium.launch().close()
	# Every failure here means the same thing to a caller: there is no browser to drive.
	except Exception as why:
		return f"chromium could not be launched ({why}), so run `playwright install chromium`"

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
#: **Through `conftest.required`, which is the second half of H-17.** This read `== "1"` where
#: `SUBROUTINE_TEST_REQUIRE_POSTGRES` has always accepted `true`, `yes` and `on` — so spelling
#: the variable either of the other ways set the guard and did nothing at all.
if UNAVAILABLE is not None and conftest.required("SUBROUTINE_TEST_REQUIRE_BROWSER"):
	raise RuntimeError(
		f"SUBROUTINE_TEST_REQUIRE_BROWSER is set and {UNAVAILABLE}. Do that, or unset the "
		f"variable — but not to make a red build green: the skip exists so a machine without "
		f"a browser can run everything else, and here it would mean reporting success on a "
		f"suite that did not run."
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
	# **What an owner really has**, because the app reads this now (`#927`'s M-25): a control
	# the reader may not use is no longer drawn, so an empty list is a page with no capture box
	# and no Complete — a different page from the one these tests are about. It was empty for as
	# long as nothing read it.
	"workspaces": [
		{
			"slug": "projects",
			"id": "w1",
			"role": "owner",
			"permissions": ["task:write", "comment:write", "task:delete"],
		}
	],
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
	# **Nested, since `SR#512` publishes an address and `SR#959` draws one.** A row filed at
	# the top level would render a one-segment label, which is what a bare key looked like —
	# so the whole point of the change would be invisible to every page built from this.
	"project_path": "subroutine/ui",
	"status_category": "todo", "created_at": "2026-08-10T14:22:00+00:00",
}

#: What the instance answers when it will not do something — a problem document, which is what
#: every refusal here really is. The detail is what the page shows beside the form.
REFUSED: dict[str, typing.Any] = {
	"type": "about:blank", "title": "Forbidden", "status": 403, "code": "forbidden",
	"detail": "This needs the 'task:write' permission.",
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

#: What `POST /v1/recurrence/parse` answers here, keyed by the phrase (`SR#94`, §6.7).
#:
#: **Copied from the real endpoint rather than invented**, and the wording matters: the whole
#: property being drawn is that the sentence comes back in *different words from the ones
#: typed*, so a fixture echoing its own key would make the page's test vacuous by agreeing
#: with it. What the server makes of a phrase is `tests/test_api_recurrence.py`'s question.
READABLE_REPEATS = {
	"every other tuesday": {
		"rule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU",
		"description": "every other week, on Tuesday",
		"text": "every other tuesday",
		"occurrences": ["2026-08-18T09:00:00Z", "2026-09-01T09:00:00Z"],
	},
}

#: And what it answers for one it cannot read — a problem document, which is what `api`'s
#: `refusal` parses and what the disclosure has to turn into a sentence beside the box.
UNREADABLE_REPEAT = {
	"type": "about:blank",
	"title": "Unprocessable Entity",
	"status": 422,
	"code": "invalid_field_value",
	"detail": (
		"'every fortnight' is not a repeat this understands. Try 'every day', "
		"'every 14 days', 'every other tuesday', 'every month on the 30th', "
		"'every month on the last thursday' or 'every year on 19 august'."
	),
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
	#: Every page here is served under the policy the instance really sends (`#805`), so all
	#: five tests below run against it rather than one test asserting a header string. A policy
	#: that blocked the import map would show up as the app never painting — which is what
	#: `opened` already waits for, so the failure lands on whichever test met it with the reason
	#: in `violations`.
	violations: list[str] = []
	#: Every write the page made, so a gesture can be checked by what it sent rather than by
	#: what the page then looks like — the request is the fact and the render is a consequence.
	written: list[tuple[str, str, str | None]] = []
	#: What `/v1/tasks` answers, so one test can ask about a board with rows and the same board
	#: without them. A holder rather than an argument to `answered`, because the route is
	#: registered on the context once and every page shares it.
	listing: list[typing.Any] = [ROWS]
	#: The status every write is answered with, or ``None`` for the ordinary success.
	refusing: list[int | None] = [None]

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
			# **Before the catch-all below, which answers every write with a task** (`SR#94`).
			# A repeat preview is a POST that stores nothing, so without this it was handed a
			# card and the page rendered an empty sentence — the narrower-path-first trap this
			# fixture already records twice, arriving by method rather than by prefix.
			#
			# **It decides yes or no from the phrase**, because both branches are the point:
			# what this stands in for is the server saying *I understood* or *I did not*, and a
			# route that always agreed would leave the refusal path drawn by nothing. What the
			# words actually mean is `tests/test_api_recurrence.py`'s question, not this one.
			if wanted == "v1/recurrence/parse":
				asked = json.loads(route.request.post_data or "{}").get("text", "")
				known = asked in READABLE_REPEATS

				route.fulfill(
					status=200 if known else 422,
					body=json.dumps(
						READABLE_REPEATS[asked] if known else UNREADABLE_REPEAT
					),
					content_type="application/json",
				)

				return

			if route.request.method != "GET":
				written.append((route.request.method, wanted, route.request.post_data))

				# **A write can be made to fail**, because half of what a form does is decided
				# by the refusal: a page that only ever succeeds cannot show whether typing
				# survives one. A holder rather than an argument, for `listing`'s reason — the
				# route is registered on the context once and every page shares it.
				if refusing[0] is not None:
					route.fulfill(
						status=refusing[0],
						body=json.dumps(REFUSED),
						content_type="application/problem+json",
					)

					return

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
				# **One task, by its ref, before the collection it lives in** — narrower path
				# first, which is the trap this block already warns about twice. `v1/tasks/42`
				# starts with `v1/tasks`, so it was served the *collection* envelope: the app
				# read an item where a page was, and every attempt to open one landed on the
				# failure page. Nothing was asserting on an open item, so it looked like a
				# harness that simply had no test for it.
				else CARD if re.fullmatch(r"v1/tasks/\d+", wanted)
				# **The collection, and only the collection.** `startswith` also matched every
				# sub-resource — `v1/tasks/42/links` was answered with a page of *tasks* — so
				# opening an item read links that were rows and fell to the failure page. An
				# empty collection is what the fall-through gives them, which is true.
				else listing[0] if wanted.split("?")[0] == "v1/tasks"
				else EMPTY
			)

			route.fulfill(
				status=200, body=json.dumps(answer), content_type="application/json"
			)

			return

		# Every other address is the app's own — `#638` gave an item one, and `#648` made
		# anything unclaimed a 404 keyed on `Accept`, which for a browser is this page.
		route.fulfill(
			status=200, body=shell, content_type="text/html; charset=utf-8", headers=POLICY
		)

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
		page.on(
			"console",
			lambda message: violations.append(message.text)
			if "Content Security Policy" in message.text
			else None,
		)
		page.goto(f"http://app.test{address}")

		# **The policy is checked on the way past rather than in a test of its own.** A directive
		# too narrow for what the app does has two symptoms and they need different handling: a
		# blocked *module* means nothing paints, so the wait below times out with nothing to say;
		# a blocked *feature* paints fine and fails silently. Reporting the violations covers
		# both, and turns a thirty-second timeout into a diagnosis.
		try:
			page.wait_for_selector(".app", timeout=10_000)
		except Exception:
			assert not violations, (
				f"the app never painted, and it broke its own Content-Security-Policy first: "
				f"{violations}. The policy is built from the served page, so this usually means "
				f"the import map changed and its hash did not follow."
			)

			raise

		assert not violations, (
			f"the app broke its own Content-Security-Policy: {violations}. It painted anyway, so "
			f"nothing else here would have failed — a directive too narrow for what the app does "
			f"shows up as a feature quietly not working."
		)

		return page

	try:
		yield opened, written, refusing
	finally:
		context.close()


def test_a_modified_click_still_belongs_to_the_browser (running: typing.Any) -> None:
	"""`#722`'s entire value, verified by nobody until there was a browser here.

	Every navigation in this app is a real anchor so that *open in a new tab*, *copy link
	address*, middle-click and the context menu all work — and `opens` decides which clicks the
	app keeps. That function is pure and covered; **whether a real browser then does what it
	predicts was covered by nothing**, and it is the half that reaches a reader.
	"""

	opened, _written, _refusing = running
	page = opened("/projects")

	page.wait_for_selector("a[href]", timeout=10_000)

	# **The masthead is one of those anchors** (`#868`), and this is the only place that can
	# say so: it lives in `App`, which uses hooks, so the render harness cannot call it and
	# `#640` has shipped four faults from exactly that gap. The *rule* — `opens` and `followed`
	# — is pure, covered, and proven against a real browser by the assertions below; what
	# needed driving is that the masthead goes through it rather than being a click handler.
	home = page.query_selector("h1 a[href='/']")

	assert home is not None, (
		"the product name is not a link home, so a reader cannot middle-click it, copy its "
		"address, or hear a screen reader announce it as a link"
	)

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

	opened, _written, _refusing = running
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

	opened, written, _refusing = running
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

	opened, written, _refusing = running
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

	opened, written, _refusing = running
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


#: Two *sites*, not merely two origins. `SameSite` compares registrable domains, so a second
#: port on one host would be same-site and the check below would pass by construction — which
#: is the shape of a test that cannot fail.
OURS = "http://app.test"
THEIRS = "http://elsewhere.test"

#: A form that submits itself, because what is under test is a page needing no cooperation
#: from the reader. `#803`'s attack is a click, and this is the version of it that asks for
#: nothing at all.
POSTING = (
	"<body onload='document.forms[0].submit()'>"
	"<form method='post' action='{where}/v1/session'>"
	"<input name='link' value='sr_lnk_whatever'></form></body>"
)


def test_a_lax_cookie_is_withheld_from_a_cross_site_post (looks: typing.Any) -> None:
	"""**The one thing `#803`'s defence rests on, and only a browser can say it.**

	The confirmation page stops nothing by itself: whoever can make a browser follow one link
	can make it follow two. What stops the attack is that answering requires the *standing*
	session cookie — so the whole control is the claim that a browser will not attach a
	`SameSite=lax` cookie to a `POST` from another site.

	`api_support` sends whatever cookies a test names, so the fast suite is structurally unable
	to check this: it would be asserting on its own fixture. This drives Chromium.

	**Both directions in one test**, because the withholding half alone would pass just as well
	if cookies never worked here at all — *one of a thing* applied to a fixture.
	"""

	_showing, bare = looks
	context = bare.context.browser.new_context()

	#: Whether each submission arrived with the cookie, in the order the pages were opened.
	#: **A list rather than a map keyed on the target**, which the first version was — the
	#: target is `app.test` both times, so the cross-site run overwrote the same-site one and
	#: the control half silently stopped being checked.
	carried: list[bool] = []

	def answered (route: typing.Any) -> None:
		"""Serve the two sites, and record whether the write arrived with the cookie."""

		if "/v1/session" in route.request.url:
			carried.append("subroutine_session" in route.request.headers.get("cookie", ""))
			route.fulfill(status=204, body="")

			return

		route.fulfill(status=200, content_type="text/html", body=POSTING.format(where=OURS))

	context.route("**/*", answered)
	context.add_cookies([{
		"name": "subroutine_session", "value": "sr_web_pretend", "domain": "app.test",
		"path": "/", "sameSite": "Lax",
	}])

	try:
		for site in (OURS, THEIRS):
			page = context.new_page()
			page.goto(f"{site}/")
			page.wait_for_timeout(500)
			page.close()
	finally:
		context.close()

	assert len(carried) == 2, (
		f"{len(carried)} of 2 forms reached the endpoint, so this is not comparing two cases"
	)

	assert carried[0] is True, (
		"the app's own page could not post its session cookie, so the case below proves "
		"nothing — a fixture where cookies never work would pass it either way"
	)

	assert carried[1] is False, (
		"a form on another site reached POST /v1/session carrying the session cookie. "
		"`#803`'s confirmation is then a page that warns and stops nobody, because the "
		"attacker can submit it as easily as they can send the link."
	)


def test_the_stale_half_of_the_excuse_list () -> None:
	"""What makes an entry go away. Every allow-list in this repository owes this.

	**It took the ``looks`` fixture and never used it** (`#947`, cold review `#927`'s L-3), so a
	comparison between two module-level constants was paying for a Chromium launch — in the one
	file whose own test bounds it at seventeen cases, because a browser is 400MB and a minute of
	CI. It reads no page, so it takes no browser.

	**The subset is vacuous while the register is empty, and that is correct rather than a
	second defect.** :data:`LOOKS_LIKE_THE_BROWSER` is meant to stay `{}` — `#747`'s argument is
	that a design which cannot produce the fault beats a check that catches it. A stale-entry
	test over an empty register has nothing to say, and is what says so on the day somebody adds
	the first entry.
	"""

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

	**Raised to eleven on 2026-08-11, and the case is the standard to hold a raise to**
	(`#803`). The addition measures whether a browser withholds a `SameSite=lax` cookie from a
	cross-site `POST` — which is not our code but is the entire defence behind the sign-in
	confirmation, and `api_support` sends whatever cookies a test names, so the fast suite could
	only ever assert on its own fixture. A security control resting on a browser behaviour is
	what this file is for; a raise for anything a DOM could answer is not.

	**And a list of forbidden words was the version before that**, which failed on its own
	list — `#546`'s shape for the third time here, and ``tests/dom.js`` records the same trap
	arriving through the word *click* inside the sentence forbidding it.

	**Raised to twelve on 2026-08-13 for `#846`, and it is the clearest raise so far.**
	``#748`` named three things this file exists for — a modified click, **layout**, and
	``axe-core`` — and until now it had measured none of the second. The addition asks whether
	a board's columns fit the window, which is a question about the cascade, the flex line and
	the viewport at once: there is no arrangement of ``tests/dom.js`` that can answer it, so it
	fails the "could a DOM do this" test in the direction that justifies the cost.

	**What earns it is the defect, not the category.** Three of seven columns were off-screen
	on a wide display, and the only thing announcing them sat at the bottom of a container as
	tall as its tallest column — several thousand pixels down. It was found by a person opening
	the page, which is where nine of this browser's defects have come from, and this file is
	the only thing that could have found it first.

	**Read for fat before raising**, as this docstring requires of itself: the addition sets
	two viewports and makes two assertions, and the second is what stops the first passing
	vacuously against a board that never overflowed. Neither is removable.

	**Raised to thirteen for `#863`, and the case is that it is `#846`'s own bill.** That fix
	uncapped the *frame* so a board could use the screen; the form standing above the board
	inherited it, and since the fields are laid out with ``auto-fit`` the column count — and
	whether a time sits beside its date or below it — followed whichever view you had opened
	the box from. Found by the same person on the same surface, which is where ten of this
	browser's defects have come from.

	It is squarely `#748`'s named third scope and there is no arrangement of ``tests/dom.js``
	that reaches it: the question is what ``repeat(auto-fit, minmax(190px, 1fr))`` *resolves
	to* against a computed container width, which needs a cascade and a layout.

	**Read for fat**: the addition opens the same form in two views at one viewport and
	compares one computed value. The viewport is set wide deliberately — at a narrow one both
	views collapse to a single column and agree for a reason that has nothing to do with the
	fix, which is the vacuous pass this docstring already warns about once.

	**Raised to fourteen for `#908`, and it is the plainest case yet.** The theme is built on
	``light-dark()``, which resolves against the *cascaded* ``color-scheme`` — so what colour
	the page actually paints is a question with no answer outside a CSS engine, and
	``tests/dom.js`` has none by decision. Every other test could pass with the whole feature
	inverted.

	The requirement is `#441`'s eighth and it names the case exactly: dark and light **with a
	user choice**. Its one interesting state is a reader whose system says dark and who wants
	this page light — which needs an emulated media preference and a stored value at once, and
	is reachable nowhere else in this repository.

	**Read for fat**: three measurements, not two. *The page changed* is not the claim — pinning
	light has to land on the colour a light system gets, or the page has merely become a third
	thing; and the first assertion is what stops the comparison passing against a stylesheet with
	no themes in it at all. The reload is the no-flash script's only witness, since ``app.js``
	writes the attribute only when the control is used.

	**Raised to fifteen for `#911`, and it is `#846`'s category with a sharper number.** `#748`
	names layout as one of the three things this file exists for, and the addition asks where two
	elements are *relative to each other* — beside, or below. There is no arrangement of
	``tests/dom.js`` that answers it: the shim has no cascade and no layout, so an element laid
	out alongside another is indistinguishable there from one stacked under it.

	**What earns it is the defect and its size.** Falsified by restoring the old flex row, the
	identity line measures **191px inside a 300px card** — over a third of every card's width
	going to a button while the title wrapped to three lines beside it. Found by Simon opening
	the board, which is where eleven of this browser's defects have come from.

	**Read for fat**: two assertions and one query. The first is the defect; the second says the
	properties and the action went *under* the title rather than being deleted, which is the
	other way a title could be given its width and is not this.

	**Raised to sixteen for `#94`, and it paid for itself before it was committed.** The repeat
	preview is `#640` in its purest form: every piece of it is pure and separately tested — the
	request builder, the component, the two rules that decide what goes on the wire — and the
	*wire* is a real `input` event, a fetch, and a `useState` that has to land in the render
	that reads it. That is the seam four faults have shipped through, each found by Simon
	rather than by the build, and `tests/dom.js` calls components as plain functions so it can
	render an answer handed to it and can never ask whether anything fetches one.

	**What it caught on its first run was mine, and it was live.** The guard dropping an answer
	overtaken in flight compared the new phrase against the one already *held* — true of every
	new phrase — so the first preview stuck and nothing typed after it was ever shown. Every
	other test passed. The fix is a ref written at ask-time, which is a question only something
	recorded when the request goes out can answer.

	**Read for fat**: two gestures and three assertions, and none is removable. The first pair
	is the readable branch and the property that makes it a check rather than a mirror — the
	sentence must not be the words that were typed. The third is the refusal, which is a
	different path through `App` and the one a reader stuck on wording actually meets; without
	it the `catch` is drawn by nothing. What the server makes of a phrase is deliberately not
	asked here — that is `tests/test_api_recurrence.py`, and this fixture stands in for the
	server saying yes or no rather than for its judgement.

	**Raised to seventeen for `#927`'s M-24, and the case is that this file could not see the
	*failing* half of anything.** Every harness here answers a write with a card, so the whole
	of what a form does when a write is refused — which is where its typing lives — was drawn
	by nothing. Three forms cleared themselves synchronously while the request was in flight, so
	a 403, a 409, a 429 or a dropped connection reported the failure over a box that had already
	been emptied.

	It is not a category this file has covered and it is exactly what it is for: a real
	``submit``, a promise that has not settled, and the value of a DOM node afterwards.
	``tests/dom.js`` calls components as plain functions, so there is no event to dispatch and
	no element to read back — the same argument as `#94`'s above, aimed at the branch nobody had
	built a fixture for.

	**Raised to eighteen for `#959`**, and the case is *pressing* a control. Decision `#957` §4
	makes a project label clickable and says it narrows the page to that path; `tests/dom.js`
	drops every attribute but ``href`` and has no navigation at all, so it can say a mark
	carries an address and never that following it arrives anywhere. A link nobody has clicked
	is the inert control this project keeps shipping.

	**Raised to nineteen for `#963`**, and the case is geometry a unit test cannot reach —
	`#911`'s argument, one page along. Whether an item opened from a board is narrower than the
	screen is a question about layout, and ``tests/dom.js`` has no cascade; the rule beside it
	in ``tests/test_web.py`` says the frame is given the right class and says nothing about how
	wide the page then is.

	**And it is the first test here that opens an item at all**, which is why it cost fixture
	work rather than a docstring: the route answered every path beginning ``v1/tasks`` with the
	*collection*, so a single item read as a page and every attempt landed on the failure page.
	Nothing had ever tried, so a harness that could not do it looked like one nobody needed.

	**Raised to twenty for `#962`**, and the case is that this is the *third* defect of one
	shape: `go` writes the address bar and nothing else, so a handler that stops there moves
	the address and leaves the page as it was. The other two were the project label and
	`widen`'s own missing half, and both were caught here — nothing in the fast suite can see
	it, because every one of these callbacks lives in `App`, which `tests/dom.js` cannot call
	(`#640`). A category with three instances and no cheaper reader is what this file is for.

	**Read for fat**: two gestures and three assertions, and the fixture gains one holder. The
	success half is not padding — it is one line away from the refusal half in the code, and a
	form that never cleared would put the last capture into the next one, which is the failure
	this change could most easily have introduced.
	"""

	source = pathlib.Path(__file__).read_text(encoding="utf-8")
	tests = re.findall(r"^def (test_\w+)", source, re.M)

	assert len(tests) > 1, "no tests were found, so this is checking nothing"

	assert len(tests) <= 20, (
		f"this file holds {len(tests)} tests: {tests}. Seventeen answering what only a browser "
		f"can is the agreed scope; past this it is a second suite, and the fast one is the one "
		f"that stops being run. Raising it is a decision — read the addition for fat first."
	)


def test_a_wide_screen_shows_every_column_the_board_has (running: typing.Any) -> None:
	"""`#846`, and the first thing here that measures *layout* — `#748`'s named third scope.

	Found by the browser's only reader, on a display twice as wide as the page was using. A
	list wants a reading measure and the frame gave one to everything; a board wants columns,
	and a task's four categories plus a document's three are seven of them. Three sat off the
	right-hand edge, and the one thing announcing them — the scrollbar — is at the bottom of a
	container as tall as its tallest column, which on a real board is thousands of pixels down.

	**Both viewports, because one of them cannot fail.** Wide alone would pass against a board
	that had never overflowed; narrow alone says nothing about the fix. The pair says the
	columns fit when there is room and scroll when there is not, which is the whole claim.
	"""

	opened, _written, _refusing = running
	mixed = {
		"items": [
			dict(CARD, ref=n, kind=kind, status_category=category, title=f"{category} {n}")
			for n, (kind, category) in enumerate(
				[
					("task", "todo"), ("task", "in_progress"), ("task", "done"),
					("document", "draft"), ("document", "current"), ("document", "superseded"),
				],
				start=200,
			)
		],
		"page": {"has_more": False, "next_cursor": None, "total": None},
	}
	page = opened("/projects?view=board", rows=mixed)

	page.wait_for_selector(".board .column", timeout=10_000)
	page.set_viewport_size({"width": 2200, "height": 900})

	# **Asked of the element that scrolls**, not of the window: the page can be perfectly
	# scrollable while a strip inside it is clipping content, which is exactly what happened.
	clipped = ".board .columns"
	room = page.eval_on_selector(
		clipped, "node => node.scrollWidth - node.clientWidth"
	)

	assert room <= 1, f"{room}px of columns are off-screen on a display with room for them"

	page.set_viewport_size({"width": 700, "height": 900})

	assert page.eval_on_selector(clipped, "node => node.scrollWidth - node.clientWidth") > 1, (
		"nothing overflows even at 700px, so the check above passes whatever the frame does"
	)

	page.close()


def test_a_form_keeps_its_measure_in_every_view (running: typing.Any) -> None:
	"""`#863`, found by Simon driving `#755`: the fields move when you open the box elsewhere.

	The frame is capped at a reading measure and ``.app.wide`` removes that cap so a board can
	use the screen (`#846`) — but the class is on the *frame*, so the capture form standing
	above the board was widened too. ``.adding .details`` is ``repeat(auto-fit, minmax(190px,
	1fr))``, so the number of columns is derived from that width, and a date and its time are
	siblings in one cell, so they wrapped differently as well. His objection is the right one:
	a form whose fields move cannot be filled in from memory.

	**The computed value rather than the rule**, because the rule was never the thing that was
	wrong — ``auto-fit`` did exactly what it says against two different widths. Asking the
	stylesheet whether ``.adding`` declares a cap would be guarding a spelling, which is a trap
	this repository has recorded three times.
	"""

	opened, _written, _refusing = running
	measured = {}

	for view, address in (("list", "/projects"), ("board", "/projects?view=board")):
		page = opened(address)

		# Wide on purpose: at a narrow viewport both views collapse to one column and agree for
		# a reason that has nothing to do with the fix.
		page.set_viewport_size({"width": 2200, "height": 900})
		page.click(".adding .more")
		page.wait_for_selector(".adding .details", timeout=10_000)

		# **Where the fields land, not what the rule says.** `getComputedStyle` returns the
		# *specified* `grid-template-columns` here rather than resolved tracks, because a
		# `<fieldset>` lays its contents out in an anonymous box — so asking for the tracks
		# measures the stylesheet rather than the layout, which is the thing that was never in
		# doubt. The distinct left offsets of the children are the column count as a reader
		# meets it.
		measured[view] = page.eval_on_selector(
			".adding .details",
			"""node => {
				const lefts = [...node.children].map(
					kid => Math.round(kid.getBoundingClientRect().left)
				);
				return {
					width: Math.round(node.getBoundingClientRect().width),
					columns: new Set(lefts).size,
				};
			}""",
		)

		page.close()

	assert measured["list"] == measured["board"], (
		f"the same form is laid out differently depending on the view it was opened from: "
		f"{measured}. A field is then in a different place, which is what stops somebody "
		f"filling the form in without looking at it."
	)

	assert measured["list"]["columns"] > 1, (
		f"the form is one column wide at 2200px ({measured['list']}), so the comparison above "
		f"would agree whatever the frame did"
	)


def test_a_pinned_theme_beats_the_machines (running: typing.Any) -> None:
	"""`#908`, requirement 8 of `#441`: dark and light **with a user choice**.

	`prefers-color-scheme` is the machine's answer, and it was the only answer — so somebody
	whose system is dark and who wants *this* page light could not say so. The case that proves
	it is exactly that one, and nothing short of a browser reaches it: `light-dark()` resolves
	against the cascaded `color-scheme`, which `tests/dom.js` has no engine to compute.

	**Three measurements rather than two**, because *the page changed* is not the claim. Pinning
	light against a dark system has to land on the same colour a light system gets, or the page
	has merely become a third thing.

	Also asserts the attribute is on `<html>` after a reload, which is the inline shell script
	having run: `app.js` writes it only when the control is used, so on a fresh load it is the
	only thing that could have.
	"""

	opened, _written, _refusing = running

	def background (page: typing.Any) -> str:
		painted = page.eval_on_selector(
			"body", "node => getComputedStyle(node).backgroundColor"
		)
		return str(painted)

	page = opened("/projects")
	page.emulate_media(color_scheme="light")
	light = background(page)

	page.emulate_media(color_scheme="dark")
	followed = background(page)

	page.evaluate("localStorage.setItem('theme', 'light')")
	page.reload()
	page.wait_for_selector(".foot", timeout=10_000)
	pinned = background(page)

	assert followed != light, (
		f"a dark system and a light one paint the same background ({light}), so this test "
		f"cannot see a theme at all"
	)

	assert pinned == light, (
		f"pinning light on a dark system paints {pinned}, where a light system paints {light}"
	)

	assert page.evaluate("document.documentElement.dataset.theme") == "light", (
		"the shell did not apply the stored theme, so a pinned choice flashes the wrong one "
		"on every load"
	)


def test_a_card_gives_its_whole_width_to_the_title (running: typing.Any) -> None:
	"""`#911`, reported by Simon from the board on a real screen.

	*Complete* cannot be nested in the row — a button inside a button is invalid — so it is a
	sibling, and a sibling laid out beside the row takes width down the card's entire height.
	Four titles of four wrapped to three lines each while the space alongside the button stood
	empty.

	**Two assertions, and the first is the defect.** The identity line must be as wide as the
	card: under the old layout it was narrower by the width of a button, which is what pushed
	the title into wrapping. The second says where the properties and the action went — under
	the title rather than beside it — because a title could also be given its width by deleting
	the control, and that is not this.

	Only a browser can answer either. `tests/dom.js` has no cascade and no layout, so it cannot
	tell an element laid out beside another from one laid out below it.
	"""

	opened, _written, _refusing = running
	page = opened("/projects?view=board")
	page.wait_for_selector(".board .rows li", timeout=10_000)

	measured = page.eval_on_selector(
		".board .rows li:has(.finish)",
		"""card => {
			const row = card.querySelector(".row");
			const meta = card.querySelector(".meta");
			return {
				card: Math.round(card.getBoundingClientRect().width),
				row: Math.round(row.getBoundingClientRect().width),
				rowBottom: Math.round(row.getBoundingClientRect().bottom),
				metaTop: meta ? Math.round(meta.getBoundingClientRect().top) : null,
			};
		}""",
	)

	assert measured["metaTop"] is not None, (
		"no card carries a Complete button, so this measured a card without the thing that "
		"used to take the width"
	)

	assert measured["row"] == measured["card"], (
		f"the identity line is {measured['row']}px inside a {measured['card']}px card, so "
		f"something beside it is taking width the title could have used: {measured}"
	)

	assert measured["metaTop"] >= measured["rowBottom"], (
		f"the properties and the action are level with the title rather than under it, so the "
		f"title's width is whatever they left: {measured}"
	)


def test_a_written_repeat_is_read_back_before_it_is_committed_to (running: typing.Any) -> None:
	"""`#94`, §6.7, and Simon's completion bar: *recurring events are not completed until a user
	can add and edit an item's recurrence via the Web UI*.

	**The preview is the only reason the endpoint exists**, and it is the one part of this
	feature that lives entirely in `App`'s wiring — a real `input` event, a fetch, and a
	`useState` that has to land in the render that reads it. `#640` is the item saying nothing
	covers that, and four faults have shipped from it, every one found by Simon rather than by
	the build. `tests/dom.js` calls components as plain functions, so it can render `Reading`
	with an answer handed to it and can never ask whether anything *fetches* one.

	**Both branches, because they fail differently and separately.** A phrase this can read has
	to come back **in different words from the ones typed** — that is what makes it a check
	rather than a mirror, and *every month on the 30th* against *every 30 days* is the pair
	whose difference does not show until February. A phrase it cannot read has to say so where
	somebody can still change it, which is the `catch` and a different path through `App`.
	"""

	opened, _written, _refusing = running
	page = opened("/projects")

	page.click(".adding .more")
	page.wait_for_selector(".adding .details", timeout=10_000)
	page.click(".repeats summary")

	page.fill(".repeats input[name=recurrence]", "every other tuesday")
	page.wait_for_selector(".repeats .reading strong", timeout=10_000)

	read = page.inner_text(".repeats .reading")

	assert "Tuesday" in read, read
	assert "every other tuesday" not in read, (
		f"the preview echoed the phrase back rather than reading it: {read!r}. Echoing confirms "
		f"nothing — the words have to come from the stored rule."
	)

	page.fill(".repeats input[name=recurrence]", "every fortnight")
	page.wait_for_selector(".repeats .reading.bad", timeout=10_000)

	refused = page.inner_text(".repeats .reading.bad")

	assert "every day" in refused, (
		f"a phrase this cannot read must name the shapes that work: {refused!r}. A reader stuck "
		f"on wording needs an example rather than a complaint."
	)

	page.close()


def test_a_refused_write_leaves_what_was_typed_where_it_was (running: typing.Any) -> None:
	"""The capture box emptied itself while the request was still in flight.

	`form.reset()` ran synchronously after handing the values over, so a 403, a 409, a 429 or a
	dropped connection answered *"That was not added"* over a box that had already been cleared
	— everything typed, gone, with nothing to retry from and no way to get it back.
	`Conflict`'s own comment in `app.js` calls exactly that the worst possible answer, about
	the neighbouring case.

	**Only a browser can ask this.** It is a real ``submit`` event, a promise that has not
	settled, and the value of a DOM node afterwards — three things `tests/dom.js` has none of:
	it calls components as plain functions, so there is no event to dispatch and no element to
	read back. The failing path is also the one nothing else covers, because every other
	harness here answers a write with a card.

	Both branches, because they are one line apart and fail in opposite directions: a refusal
	must keep the text, and success must still clear it — a form that never cleared would put
	the last capture into the next one.
	"""

	opened, written, refusing = running
	page = opened("/projects")

	refusing[0] = 403
	page.fill(".adding input[name=text]", "Something worth not losing")
	page.press(".adding input[name=text]", "Enter")
	page.wait_for_selector(".note.bad", timeout=10_000)

	assert written, "the page did not even try to write"
	assert page.input_value(".adding input[name=text]") == "Something worth not losing", (
		"the box was emptied while the write was in flight, so a refusal took the typing with it"
	)

	refusing[0] = None
	page.press(".adding input[name=text]", "Enter")
	page.wait_for_selector(".note.good", timeout=10_000)

	assert page.input_value(".adding input[name=text]") == "", (
		"a write that landed has to clear the box, or the next capture starts with this one"
	)

	page.close()


def test_a_project_label_is_a_link_that_narrows_the_page (running: typing.Any) -> None:
	"""`#959`, decision `#957` §4, and the claim is about what is on screen.

	**Only a browser can answer it.** `tests/dom.js` drops every attribute but `href` and has
	no navigation, so it can say a mark carries an address and not that pressing it goes there
	— and *"clicking it filters the view to that project path"* is the whole requirement.

	Driven from a page that names a workspace and no project, which is where the label carries
	the most: the whole path, and clicking it leaves the page showing that project alone.
	"""

	opened, _written, _refusing = running
	page = opened("/projects")
	page.wait_for_selector(".rows li .mark", timeout=10_000)

	label = page.locator(".rows li a.mark").first

	assert label.count() > 0, "no project label is a link, so there is nothing to press"

	address = label.get_attribute("href")
	said = (label.inner_text() or "").strip()

	assert address is not None and address.startswith("/projects/"), (
		f"a project label points at {address!r}, which is not a place in this workspace"
	)
	assert said and said.lower() == said, (
		f"the label is {said!r} — slug form, lower case, is what a reader can type back"
	)

	label.click()
	page.wait_for_url(f"**{address}*", timeout=10_000)
	page.wait_for_selector(".rows li", timeout=10_000)

	# **The page moved and stayed the app**, which is what separates a link that narrows from
	# one that reloads into a 404 — since `#648` an unclaimed address is served this page, so
	# an address that named nothing would still render something.
	# The path alone, because `go` carries the arrangement in the query — `?view=list` is the
	# view the reader was already on travelling with them, which is decision `#649`'s rule that
	# the path says which rows there are and the query says how they look.
	assert urllib.parse.urlparse(page.url).path == address, (
		f"pressing the label left the reader at {page.url}"
	)

	# **Waited for rather than read once.** The rows were already on the page before the click,
	# so a selector that was satisfied a moment ago is satisfied again immediately and the
	# assertion lands before the re-render — a test that passes for the wrong reason, and on a
	# page that polls, one that would sometimes pass for the right one.
	page.wait_for_selector(".rows li a.mark", state="detached", timeout=10_000)

	after = page.eval_on_selector_all(
		".rows li .mark", "marks => marks.map((mark) => mark.textContent.trim())"
	)

	assert address.removeprefix("/projects/") not in after, (
		f"the page is that project and its rows still name it: {after}"
	)


def test_an_item_opened_from_a_board_is_read_at_the_measure (running: typing.Any) -> None:
	"""`SR#963`, Simon 2026-08-17, from the served instance.

	**Only a browser can answer it**, for `SR#911`'s reason: the claim is geometry, and
	`tests/dom.js` has no cascade and no layout. The unit test beside this one says the frame
	is given the right class; this says the page is actually narrower than the screen, which is
	the thing that was wrong.

	**Driven by clicking through from a board**, not by opening the item's address. Refreshing
	that address is what *hid* the defect — it carries no `?view=`, so the view falls back to
	the list — so a test that navigated straight there would have passed against the fault.
	"""

	opened, _written, _refusing = running
	page = opened("/projects?view=board")
	page.wait_for_selector(".board .rows li", timeout=10_000)

	on_the_board = page.evaluate(
		"() => Math.round(document.querySelector('div.app').getBoundingClientRect().width)"
	)
	screen = page.evaluate("() => document.documentElement.clientWidth")

	assert on_the_board > screen * 0.9, (
		f"the board is {on_the_board}px of a {screen}px screen, so this is measuring a page "
		f"that never had the width — SR#846 is what the second assertion is against"
	)

	# **The row's own link, found by the address it carries.** `.first` of every anchor in a
	# card is not it: since `SR#959` the project label is an anchor too, and clicking that
	# narrows the page instead of opening anything — a test that pressed the wrong control
	# would measure a listing and call it an item.
	page.locator(f"{CARD_ROW} a[href$='/42']").first.click()
	# **`div.detail`, not `.detail`.** The failure page carries a `<span class="detail">` — the
	# problem document's own `detail` member — so a bare class selector was satisfied by the
	# page that says the item could not be read, which is the opposite of what is being waited
	# for. It matched, and the measurement after it then found no frame at all.
	page.wait_for_selector("div.detail", timeout=10_000)

	reading = page.evaluate(
		"() => Math.round(document.querySelector('div.app').getBoundingClientRect().width)"
	)

	assert reading < on_the_board, (
		f"the item is {reading}px, the same width the board had — it inherited the board's "
		f"frame because the view is still 'board' and opening an item does not clear it"
	)


def test_the_masthead_takes_the_page_home_and_not_only_the_address (
	running: typing.Any,
) -> None:
	"""`SR#962`, Simon 2026-08-17, from the served instance.

	He clicked **Subroutine** on a narrowed board and the address said `/` while the page went
	on showing the board, still narrowed. `go` writes the address bar and nothing else — no
	``popstate`` fires for a ``pushState`` we made ourselves — so a handler that stops there
	moves the address and leaves everything as it was.

	**Only a browser can see it.** ``tests/dom.js`` calls components as plain functions and
	cannot call ``App`` at all (`SR#640`), which is where every one of these callbacks lives;
	the fast suite can say the address is right and never that the page followed it.

	**It is the third of exactly this shape**, and the first two were caught here too: the
	project label in `SR#959`, and `widen`'s own missing half before it.
	"""

	opened, _written, _refusing = running
	page = opened("/projects?view=board&include_completed=true")
	page.wait_for_selector(".board .rows li", timeout=10_000)

	page.locator("h1 a").click()

	# The agenda has neither, so both halves of the defect are one assertion.
	page.wait_for_url("http://app.test/", timeout=10_000)

	# **Waited for, not read once.** The board was on the page a moment ago, so a selector that
	# was satisfied then is satisfied again immediately and the assertion lands before the
	# re-render — which is how a test of this passes against the defect it was written for.
	page.wait_for_selector(".board", state="detached", timeout=10_000)

	assert page.locator(".listing.agenda").count() > 0, (
		"the address went home and the page did not follow it"
	)
