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
import math
import pathlib
import re
import shutil
import time
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
			# **A title, because the masthead reads it now** (`#975`). `views.WorkspaceRef` has
			# carried one since M1 and this fixture never sent it, so a control labelling
			# workspaces by name would have been drawn here from the slug — passing while every
			# real instance showed something else. The harness's own version of `#966`.
			"title": "Subroutine",
			"role": "owner",
			"permissions": ["task:write", "comment:write", "task:delete"],
		},
		# **A second one, because the agenda at `/` spans them and that is what makes a row say
		# which** (`#965`). With one workspace `spansWorkspaces` is false, every address is a
		# bare `#42`, and the page this file could build was the short case that always fitted.
		{
			"slug": "personal",
			"id": "w2",
			"title": "Personal",
			"role": "owner",
			"permissions": ["task:write", "comment:write", "task:delete"],
		}
	],
	"instance_permissions": [],
	"credential": None,
}

#: One workspace, which is a different page rather than a smaller one — `#975`. The control
#: naming it used to be inert text, so the only thing saying where the reader was could not be
#: used to go there. Nothing here could reach that state until this.
ALONE: dict[str, typing.Any] = {
	**IDENTITY,
	# Annotated where it is read, because `IDENTITY` is a literal and mypy infers a union of
	# every value shape in it — so indexing one key is an error about all of them.
	"workspaces": typing.cast(list[typing.Any], IDENTITY["workspaces"])[:1],
}

#: A workspace's projects as `projectsRequest` receives them: `order=path`, so a pre-order in
#: **creation** order. Deliberately not alphabetical at either level, because that is what the
#: server really sends (`#974`) and a fixture already in the right order proves nothing.
PROJECTS: dict[str, typing.Any] = {
	"items": [
		{"key": "inbox", "title": "Inbox", "is_inbox": True, "depth": 0},
		{"key": "websites", "title": "Websites", "depth": 0},
		{"key": "handouts", "title": "Handouts", "depth": 1},
		{"key": "acme", "title": "Acme", "depth": 0},
	],
	"page": {"has_more": False, "next_cursor": None, "total": None},
}

#: And where work can be filed *there* — `SR#1041`. One list served for every workspace made a
#: form offering the switcher's projects indistinguishable from one offering the item's, exactly
#: as one vocabulary did.
PROJECTS_ELSEWHERE: dict[str, typing.Any] = {
	"items": [
		{"key": "inbox", "title": "Inbox", "is_inbox": True, "depth": 0},
		{"key": "errands", "title": "Errands", "depth": 0},
	],
	"page": {"has_more": False, "next_cursor": None, "total": None},
}

#: Who is in the switcher's workspace, and **nobody is in the other one** — `SR#1041`. The
#: assignee control is drawn only where the roster answers with somebody, so two empty rosters
#: cannot tell an item furnished from the right workspace from one furnished from the wrong one.
#: **The membership shape, not the user's** — `views.WorkspaceMember` nests the account under
#: `user`, and a flat row makes `people()` throw into `roster`'s catch, which reports an empty
#: workspace. Written out here because a fixture that silently produces *nobody* is
#: indistinguishable from a workspace with nobody in it, which is the whole thing under test.
MEMBERS: dict[str, typing.Any] = {
	"items": [
		{"user": {"id": "u1", "username": "si", "is_service_account": False},
			"role": "owner"},
		{"user": {"id": "u2", "username": "claude", "is_service_account": True},
			"role": "member"},
	],
	"page": {"has_more": False, "next_cursor": None, "total": None},
}

#: What the instance answers for a ref nobody minted. Only the fields the app reads.
NOWHERE: dict[str, typing.Any] = {
	"type": "about:blank", "title": "Not found", "status": 404,
	"detail": "There is no task here.", "code": "not_found",
}


def _until (settled: typing.Callable[[], bool], seconds: float = 5.0) -> None:
	"""Give the page a moment to make the request a gesture set off.

	A wait rather than a sleep, so a fast machine does not pay for a slow one — and bounded, so
	the assertion that follows reports the claim rather than this timing out with nothing to say.
	"""

	deadline = time.monotonic() + seconds

	while not settled() and time.monotonic() < deadline:
		time.sleep(0.05)


def _prioritised (
	roster: list[typing.Any], slug: str, body: str | None
) -> None:
	"""Record on the fake identity which project a workspace has just prioritised — `SR#986`.

	The instance stores this on the workspace and every surface reads it back off ``/v1/me``, so
	a stand-in that took the write and went on answering the old value would make a correct page
	look wrong. Copied rather than mutated in place, because ``IDENTITY`` is a module constant
	shared by every test in this file.
	"""

	chosen = json.loads(body or "{}").get("prioritised_project")
	spaces = [
		{**space, "prioritised_project": chosen} if space["slug"] == slug else space
		for space in roster[0]["workspaces"]
	]

	roster[0] = {**roster[0], "workspaces": spaces}


def _absent (wanted: str, refs: set[str]) -> bool:
	"""Whether this path asks for an item the instance does not have.

	Both kinds, because a ref names a task *or* a document and the app reads a 404 from the
	first as *then it is the other* — so refusing one and answering the other would be an
	instance where every missing ref is a document, which is not a state any instance has.
	"""

	found = re.fullmatch(r"v1/(?:tasks|documents)/(\d+)", wanted)

	return found is not None and found.group(1) in refs


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

#: What the **other** workspace calls things — `SR#1041`. Until this existed one vocabulary was
#: served for every workspace, so an open item furnished from the wrong one and an open item
#: furnished from the right one produced identical markup, and no test here could tell them
#: apart. The same blindness `SR#1040` had, one field along.
#:
#: **`ready` and `doing` are deliberately in both**, because the tests that drive writes on an
#: item in this workspace choose `doing` — a vocabulary with nothing in common would make those
#: fail for a reason that is not their subject. What separates the two is `parked` here and
#: `triage` there.
META_ELSEWHERE: dict[str, typing.Any] = {
	"statuses": {"task": [
		{"key": "parked", "label": "Parked", "category": "todo", "is_default": False},
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
	# **The colour in force for its project** (`SR#1027`). Without one here the accent bar is
	# drawn by nothing and the wiring that puts it there is checked by nothing — a harness that
	# cannot reach a state looks exactly like one nobody needed it to, which is the fourth time
	# a gap here has been indistinguishable from a missing test.
	"project_colour": "teal",
	"status_category": "todo", "created_at": "2026-08-10T14:22:00+00:00",
}

#: What an item's links answer — `#970`. Three ends, because the claim is that a reader can tell
#: them apart without opening any of them: an unfinished blocker, a finished one, and a link that
#: is not a blocker at all and so counts towards nothing.
#:
#: **A closed end and an open one of the same kind**, because the strikethrough is a rule about
#: one of them and a fixture where every end is closed cannot tell *this line is struck through*
#: from *this stylesheet strikes everything*.
LINKED: dict[str, typing.Any] = {
	"items": [
		{"id": "l-1", "link_type": "blocks", "label": "Blocked by", "direction": "incoming",
			"other": {"entity_type": "task", "ref": 9, "title": "Still going",
				"type": "bug", "status": "doing", "status_is_default": False,
				"project_path": "subroutine/api", "blocked": True, "is_complete": False}},
		{"id": "l-2", "link_type": "blocks", "label": "Blocked by", "direction": "incoming",
			"other": {"entity_type": "task", "ref": 10, "title": "Already finished",
				"type": "chore", "status": "cancelled", "status_is_default": False,
				"project_path": "subroutine/ui", "is_complete": True}},
		{"id": "l-3", "link_type": "relates_to", "label": "Relates to", "direction": "outgoing",
			"other": {"entity_type": "document", "ref": 4, "title": "The decision",
				"type": "decision", "project_path": "subroutine/ui", "is_complete": False}},
	],
	"page": {"has_more": False, "next_cursor": None, "total": 3},
}

#: What an item's parts answer — `SR#1218`. Two, because the claim is that a reader can tell a
#: finished part from an unfinished one without opening either.
#:
#: **A closed one and an open one**, for `LINKED`'s reason exactly: a fixture where every part is
#: closed cannot tell *this line is struck through* from *this stylesheet strikes everything*.
PARTS: dict[str, typing.Any] = {
	"items": [
		{"id": "p-1", "ref": 7, "kind": "task", "title": "The first piece", "type": "bug",
			"status": "ready", "status_is_default": True, "project_path": "subroutine/ui",
			"is_complete": False},
		{"id": "p-2", "ref": 8, "kind": "task", "title": "The second piece", "type": "chore",
			"status": "done", "status_is_default": False, "project_path": "subroutine/ui",
			"is_complete": True},
	],
	"page": {"has_more": False, "next_cursor": None, "total": 2},
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
#: **One of them wears no colour**, which is what makes the accent bar falsifiable: a page where
#: every row is coloured can say the edge is drawn and never that an uncoloured row is left
#: alone. `no colour` is a real answer — nothing up that project's tree has chosen one — and it
#: has to look different from a colour nobody can see.
CROWD = [
	dict(CARD, ref=100 + n, title=f"Task number {n}", project_colour=None if n == 0 else "teal")
	for n in range(5)
]

#: What `/v1/agenda` answers — one row in each of two workspaces, so the page spans them.
#:
#: **The refs are deliberately different lengths.** `#965` was an address overflowing the column
#: it sits in, and a fixture where every address is the same width can say the gap is there and
#: never that it survives the long one.
AGENDA: dict[str, typing.Any] = {
	"date": "2026-08-17",
	"timezone": "Europe/London",
	"overdue": [],
	"today": [dict(CARD, ref=2, title="Dentist appointment", workspace_id="w2")],
	"in_progress": [dict(CARD, ref=94, title="Recurring tasks", workspace_id="w1")],
	"upcoming": [],
	"unscheduled": [dict(CARD, ref=95, title="Release 0.8.0", workspace_id="w1")],
	"unscheduled_total": 1,
}

#: An agenda whose rows are all in one workspace — `#966`'s case, and the one :data:`AGENDA`
#: cannot make: it spans two on purpose, so `spansWorkspaces` is true and every row's address
#: carries a workspace whether the label does or not.
ONE_WORKSPACE: dict[str, typing.Any] = {
	"date": "2026-08-17",
	"timezone": "Europe/London",
	"overdue": [],
	"today": [],
	"in_progress": [dict(CARD, ref=94, title="Recurring tasks", workspace_id="w1")],
	"upcoming": [],
	"unscheduled": [dict(CARD, ref=95, title="Release 0.8.0", workspace_id="w1")],
	"unscheduled_total": 1,
}

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
	#:
	#: **Four parts, and the fourth was added for `SR#1040`.** The second is the path with the
	#: query cut off, which is what every test here matches on — and it meant the whole of
	#: *which workspace* was invisible to this fixture, since a workspace only ever reaches a
	#: write as `?workspace_id=`. A write to the wrong workspace was byte-identical here to the
	#: right one, so no test could have caught the defect that shipped. The full URL is the
	#: fourth part rather than a replacement for the second, so the eighteen tests reading
	#: position 1 are untouched.
	written: list[tuple[str, str, str | None, str]] = []
	#: What `/v1/tasks` answers, so one test can ask about a board with rows and the same board
	#: without them. A holder rather than an argument to `answered`, because the route is
	#: registered on the context once and every page shares it.
	listing: list[typing.Any] = [ROWS]
	daily: list[typing.Any] = [AGENDA]
	#: What an item's parts answer — `SR#1218`. A holder for `listing`'s reason, and it must be
	#: its own branch rather than falling through to the collection: ``?parent=`` is still
	#: ``/v1/tasks``, so without this every open item would draw the whole listing as its own
	#: sub-tasks and the strikethrough test next door would be counting rows from a board.
	parts: list[typing.Any] = [EMPTY]
	#: The status every write is answered with, or ``None`` for the ordinary success.
	refusing: list[int | None] = [None]
	#: Who the reader is, so one test can ask about an instance holding a single workspace —
	#: `#975`. A holder for `listing`'s reason: the route is registered on the context once.
	roster: list[typing.Any] = [IDENTITY]
	#: Refs this instance does not have, so a jump to one can be made to fall back — `#976`.
	#: Both kinds refuse, because a ref names a task *or* a document (§6.2) and the app reads a
	#: 404 from the first as *then it is the other*.
	missing: list[set[str]] = [set()]
	#: Every path the page asked for, reads included — `#975`. `written` holds the writes, and
	#: a *read* is the only evidence that a navigation moved the page rather than the address
	#: bar: `#962`'s defect leaves the URL right, the markup unchanged and no request made, so
	#: asserting on either of the first two passes against it. `tests/dom.js` reaches the same
	#: conclusion from the other end — *assert on the requests, not on the markup*.
	reads: list[str] = []
	#: Whether the *other* workspace's vocabulary can be read at all — `SR#1041`. A reader whose
	#: credential does not reach it is a real state, and it is the one that says what an open
	#: item offers when its own words cannot be had: nothing, rather than another workspace's.
	unreadable: list[bool] = [False]

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
			reads.append(route.request.url.split("://", 1)[-1].split("/", 1)[-1])

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
				written.append((
					route.request.method,
					wanted,
					route.request.post_data,
					route.request.url.split("://", 1)[-1].split("/", 1)[-1],
				))

				# **A write the identity has to reflect, because a reader reads it back**
				# (`SR#986`). This harness answers every write with a card and remembers nothing,
				# which was true enough while every write was about an item somebody then
				# reloaded. A prioritised project is a fact about the *workspace*, and the whole
				# claim being made is that the page refetches the identity and every label
				# changes — against a stand-in that answered the old value, the product would
				# have looked broken and the harness would have looked like it had no test.
				# Fourth time a harness gap here has been indistinguishable from a missing test.
				if wanted.startswith("v1/workspaces/") and route.request.method == "PATCH":
					_prioritised(roster, wanted.split("/")[2], route.request.post_data)

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
				# **Per workspace, which it was not until `SR#1041`.** One vocabulary served for
				# every workspace made a picker furnished from the wrong one indistinguishable
				# from one furnished from the right one — `SR#1040`'s blindness, one field along.
				(
					None if unreadable[0] else META_ELSEWHERE
				) if "workspace_id=personal" in route.request.url and wanted.startswith("v1/meta")
				else META if wanted.startswith("v1/meta")
				else roster[0] if wanted.startswith("v1/me")
				# **Before the two branches below it**, which answer any numbered path with a
				# card — narrower path first, the trap this block records three times over.
				else None if _absent(wanted, missing[0])
				else (
					PROJECTS_ELSEWHERE if "workspace_id=personal" in route.request.url
					else PROJECTS
				) if wanted.split("?")[0] == "v1/projects"
				# **One workspace has members and the other has none**, which is what makes an
				# assignee control drawn from the wrong roster visible at all.
				else MEMBERS if wanted == "v1/workspaces/projects/members"
				# **One task, by its ref, before the collection it lives in** — narrower path
				# first, which is the trap this block already warns about twice. `v1/tasks/42`
				# starts with `v1/tasks`, so it was served the *collection* envelope: the app
				# read an item where a page was, and every attempt to open one landed on the
				# failure page. Nothing was asserting on an open item, so it looked like a
				# harness that simply had no test for it.
				else daily[0] if wanted.split("?")[0] == "v1/agenda"
				# **An item's links, which fell through to `EMPTY` until `#970`** — so every
				# item this harness could open had none, and a links list that said nothing
				# was indistinguishable from one nobody had written a test for.
				#
				# **Order does not matter for this one, unlike every branch above it.**
				# `fullmatch` is why: `v1/tasks/42/links` is not a full match for
				# `v1/tasks/\d+`, where it *is* a `startswith` match — which is the whole
				# difference between the two traps this block records and this line.
				else LINKED if re.fullmatch(r"v1/tasks/\d+/links", wanted)
				else CARD if re.fullmatch(r"v1/tasks/\d+", wanted)
				# **The collection, and only the collection.** `startswith` also matched every
				# sub-resource — `v1/tasks/42/links` was answered with a page of *tasks* — so
				# opening an item read links that were rows and fell to the failure page. An
				# empty collection is what the fall-through gives them, which is true.
				# **An item's parts, before the collection they are drawn from** — the
				# narrower request first, which is the trap this block already records three
				# times. `?parent=` is a query rather than a path, so `wanted` cannot see it and
				# the URL has to be read the way the workspace branches above read it.
				else parts[0] if (
					wanted.split("?")[0] == "v1/tasks" and "parent=" in route.request.url
				)
				else listing[0] if wanted.split("?")[0] == "v1/tasks"
				else EMPTY
			)

			if answer is None:
				route.fulfill(
					status=404, body=json.dumps(NOWHERE),
					content_type="application/problem+json",
				)

				return

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

	def opened (
		address: str = "/", rows: typing.Any = None, agenda: typing.Any = None,
		made_of: typing.Any = None,
	) -> typing.Any:
		"""Open one address and wait for the app to have painted."""

		listing[0] = ROWS if rows is None else rows
		#: **Empty unless a caller asks**, so every test that predates parts opens an item with
		#: none — which is what keeps this addition from changing eighteen other assertions.
		parts[0] = EMPTY if made_of is None else made_of
		# **A holder like `listing`, for the same reason**: the route is registered on the
		# context once and every page shares it, so a caller that wants a different agenda
		# cannot be given one as an argument to the route.
		daily[0] = AGENDA if agenda is None else agenda

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

	# **Unpacked with `*_` at every call site**, so a fifth holder costs one line here rather
	# than eighteen edits to tests that do not care. What each one *is* is documented above;
	# a test names only the holders it uses.
	try:
		yield opened, written, refusing, roster, missing, reads, unreadable
	finally:
		context.close()


def test_a_modified_click_still_belongs_to_the_browser (running: typing.Any) -> None:
	"""`#722`'s entire value, verified by nobody until there was a browser here.

	Every navigation in this app is a real anchor so that *open in a new tab*, *copy link
	address*, middle-click and the context menu all work — and `opens` decides which clicks the
	app keeps. That function is pure and covered; **whether a real browser then does what it
	predicts was covered by nothing**, and it is the half that reaches a reader.
	"""

	opened, _written, _refusing, *_ = running
	page = opened("/projects?view=list")

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

	opened, _written, _refusing, *_ = running
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

	opened, written, _refusing, *_ = running
	page = opened("/projects?view=board")

	page.wait_for_selector(".board .rows li[draggable='true']", timeout=10_000)
	written.clear()

	# **Named rather than *the first one***, because the board holds a crowd since `#796` and
	# whichever card is first is an accident of ordering rather than the subject of this test.
	page.drag_and_drop(f"{CARD_ROW}", "section.column:nth-of-type(2)")
	page.wait_for_timeout(300)

	moves = [one for one in written if one[0] == "PATCH"]

	assert moves, f"the drop wrote nothing: {written}"

	_method, where, body, _url = moves[0]

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

	opened, written, _refusing, *_ = running
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

	opened, written, _refusing, *_ = running
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

	**Twenty-one for `#965`**, which is `#911`'s case with a different pair of boxes: whether an
	address touches the title beside it is whether two rectangles overlap. The rule that
	produces it is one CSS declaration with no JavaScript to unit-test, so unlike the others
	here there is no cheaper reader at all.

	**Twenty-two for `#966`**, and it is the only raise here that is not a new *category*: it is
	`#959`'s claim driven on the state that broke it, an agenda whose rows are all in one
	workspace. `AGENDA` spans two on purpose, so the case Simon met was the one nothing could
	build — the second harness gap of the day, and the same shape as the first.

	**Twenty-three for `#969`**, and the case is a control's *current value*: a `<select>` fires
	no `change` for the option already selected, so a page that names a workspace it is not
	narrowed to also cannot reach it. Neither half is markup — `tests/dom.js` drops every
	attribute but `href`, so it cannot read `selected`, and "choosing what is already chosen
	does nothing" is a fact about a real control.

	**Twenty-four for `#970`**, and the number has now moved 17 → 24 in two days. That is the
	first thing to say about this raise, because six of them in a row is the shape a cap stops
	meaning anything in — it is on `#970` for Simon, and the next person wanting one should read
	all seven of these paragraphs as a set rather than only the one above.

	The case: two claims about the *cascade*, which is what this file exists for and what
	`tests/dom.js` structurally cannot answer — it drops every attribute but ``href``, so it
	cannot see a class, let alone what the class resolves to.

	- **A closed item is struck through.** One CSS declaration with no JavaScript to unit-test,
	  which is `#965`'s argument: there is no cheaper reader at all.
	- **A mark in a link line is drawn like a mark on a row**, which is the whole of Simon's
	  request — *the same type of indicators should be present on all, so a user may familiarize
	  themselves*. A consistency claim about appearance is a cascade question by definition, and
	  it caught a real collision: ``.linked a`` is one class more specific than ``.mark``, so
	  every chip on the page took the link colour and lost its outline. A shared component makes
	  two surfaces *say* the same thing and does nothing about how they are drawn.

	**Read for fat**: one page for the reference and one for the subject, because a selector that
	could fall back to the link's own chip would compare the thing with itself. Three properties
	rather than one, because ``.linked a`` set colour, border and padding, and a mark that took
	only two of them would still read as styled. The strikethrough half asserts *exactly one* of
	three lines, which is what tells the rule from a stylesheet that strikes everything.

	**Read for fat**: two gestures and three assertions, and the fixture gains one holder. The
	success half is not padding — it is one line away from the refusal half in the code, and a
	form that never cleared would put the last capture into the next one, which is the failure
	this change could most easily have introduced.

	**24 to 26, for `SR#975` and `SR#976`, and the two share one argument.** Both are new
	instances of `SR#962` — `go` writes the address bar and nothing else, so a handler that stops
	there moves the address and leaves the page as it was. That has shipped **three** times, was
	found by a browser every time, and is invisible to everything cheaper: the callbacks live in
	`App`, which `tests/dom.js` cannot execute (`SR#640`), and that file will not dispatch an
	event on purpose — *a test that wants to click needs a real DOM, not a larger pretence*.

	**What was kept out is the argument for the two that went in.** Which entries the masthead
	offers, how a project tree orders, and whether a query is nothing but a ref are all pure
	functions, and all three are checked in `tests/test_web.py` at no cost here. What is left is
	the wiring, which is the only half that has ever actually broken.

	**The number is more interesting than any single raise**, which `SR#964` is the item for: 17
	to 26 in three days, and the shape most of them share is this one.

	**26 to 27, for `SR#986`, and it is that same shape a fifth time.** A prioritised project is
	set from a button in `App`'s own callback, which does three things — write, refetch the
	identity, reload the listing — and each one alone leaves a page that looks entirely correct
	and is wrong: writing without refetching moves the rows under labels still saying the old
	thing, and refetching without reloading does the reverse. `tests/dom.js` cannot execute
	`App` (`SR#640`) and will not dispatch an event by decision, so there is no cheaper reader.

	**27 to 28, for `SR#1008`, and this one is geometry rather than wiring.** A folded column
	claims to take about a character's width, and `writing-mode: vertical-rl` is what makes that
	true — one CSS declaration with no JavaScript, where a `transform` would look identical in a
	screenshot and occupy the width it had. `SR#965` is the same argument one property along and
	is the strongest kind this file takes: there is no cheaper reader, because there is nothing
	to unit-test. The wiring rode along rather than earning a slot of its own — the decision,
	the storage and the rendering are pure and checked in `tests/test_web.py`, three tests there
	against one gesture here.

	**28 to 29, for `SR#1019`, and it is `SR#965`'s kind again — there is nothing cheaper.** The
	claim is that three families of mark are *drawn* as three different things, and the whole of
	that lives in the cascade: `.mark.identity` fills, `.mark.state` outlines, `.mark.address`
	does neither. `tests/dom.js` drops every attribute (`SR#784`), so it can report that a class
	was written and nothing about whether anything looked different — which is precisely the
	defect being fixed, since eleven chips already differed by a class that drew them alike.

	**Read for fat, and the rest of a day's work stayed out.** The families, both suppression
	rules, the sigils, the claim wording and the tag marks are all decided in `marks`, which is
	pure — seven tests in `tests/test_web.py` against one here, and this one asserts three
	relationships rather than any literal, so it survives a theme change.

	**29 to 30, for `SR#1027`, and this is the strongest case this file takes.** The claim is
	that eight colours are each *legible* and each *distinguishable from the other seven*, in
	both themes. Every value is a `light-dark()` pair, so the number a reader actually meets is
	computed by the browser from `color-scheme` — `tests/dom.js` cannot compute one at all, and
	a literal written into a Python test would assert the value we typed rather than the value
	anybody sees. `SR#1021` is the recorded proof: a token that did not exist satisfied every
	comparison in this file while every filled mark on the served instance was blank.

	**And the second property has no other reader anywhere.** Contrast says a colour can be read
	against its background; nothing said two colours can be told apart from *each other*, which
	is the entire point of a categorical palette. A palette can pass every contrast assertion
	ever written and still put two projects in shades nobody separates.

	**Read for fat, and the rest stayed out.** The registry, the refusals, the inheritance walk
	and the merge rule are all pure and are checked in `tests/test_settings.py` and
	`tests/test_transport_equivalence.py` — thirteen tests there against one here, and this one
	asserts two arithmetic properties rather than any literal, so it survives every value being
	retuned.

	**Raised to thirty-two for `SR#1040`, and these two are the plainest raises this file has
	had.** A person changed the status of an item and it cancelled a *different* item, in a
	different workspace, wearing the same number — then the page followed it there, so they were
	left looking at somebody else's work which they had just altered. Six other controls had the
	same defect and nobody had met them; the widest overwrites a title, a description, both
	dates and a status in one request.

	**Nothing cheaper could have found it, in two separate ways.** The rule lives in `App`,
	which `tests/dom.js` cannot execute (`SR#640`) — that alone is the ordinary argument. The
	second is sharper: **every other test in this file opens an item in the workspace the
	switcher holds**, where the right answer and the wrong one are the same string. A fixture
	that cannot reach a state looks exactly like one nobody needed it to, for the fifth time
	here, and the state in question is *an item opened from the agenda*.

	**Two rather than one, because they are two mechanisms.** The first drives five controls on
	an open item and asserts each write names the item's own workspace; the second presses Back
	and Forward, which involves no write at all and which fixing every write leaves untouched.
	Folding them together would have hidden that.

	**Read for fat**: the first is a loop over a table of gestures rather than five tests, and
	the two it cannot reach are named in `NOT_DRIVEN_HERE` with the reason. Everything about
	*which* workspace an address means is `chosenWorkspace` and `parseAddress`, both pure and
	both already checked in `tests/test_web.py` — what is left here is the wiring, which is the
	only half that broke.

	**Raised to thirty-three for `SR#1041`, which is `SR#1040`'s other half and fails as a wrong
	*offer* rather than as a wrong write.** An item opened from the agenda was furnished from the
	workspace the switcher held: its status picker offered statuses that item cannot be moved to
	and omitted the ones it can, its project list named somewhere it cannot be filed, and its
	controls were drawn on permissions from the wrong workspace — so Edit, Complete and the
	comment box appeared for a reader who may only read there, and every one would have been
	refused when pressed. `allowedIn`'s own comment calls that worse than not drawing them.

	**Which options a control really holds, and whether a control exists at all, are facts about
	a rendered page.** `tests/dom.js` drops every attribute but `href` (`SR#784`) and calls
	components as plain functions, so it can neither read a `<select>`'s options nor run the
	binding, which lives in `App` (`SR#640`).

	**And the fixture could not have shown it until now**, which is the same finding as
	`SR#1040`'s: one vocabulary was served for every workspace and both workspaces carried
	identical permissions, so furnishing an item from the wrong one produced byte-identical
	markup. `META_ELSEWHERE` and the roster swap are what make the two tellable apart.

	**Read for fat**: three claims in one test — the vocabulary, the requests the other two
	answers were asked for, and the controls — because they are one rule and would otherwise be
	three pages of the same setup. `offered`, `notOffered` and `allowedIn` are pure and are all
	checked in `tests/test_web.py`; what is left here is the wiring.

	**Raised to thirty-four for `SR#1041`'s sibling `SR#1042`, and this is the last of that
	family.** The item was right and what furnished it was right; the **listing underneath**
	still came from the workspace the switcher held, so going into a project from an item opened
	elsewhere — or stepping forward onto one — left one workspace's backlog under an address
	naming another, and a linked item was addressed into the wrong workspace with it.

	**Four gestures in one test because they are one claim reached four ways**: a chip on an
	open item (which the masthead also reaches, through `narrow`), Back and Forward (the
	`popstate` handler), the masthead's own options, and a search. The first three fall to
	separate mutations; the fourth is the only thing that distinguishes *refetching* the new
	workspace's answers from *moving the state everything else reads*, which are two halves of
	one step and were separably wrong.

	**Nothing cheaper reaches any of it.** `popstate` is a real history event, a `<select>`'s
	options are a rendered fact, and every one of these bindings lives in `App` (`SR#640`).
	`parseAddress` and `chosenWorkspace` are pure and are both already checked in
	`tests/test_web.py`; what is left here is the wiring, which is the only half that broke.

	**Read for fat, and most of the feature was kept out.** `prioritisedHere`,
	`prioritisedSentence`, `rankedByPriority` and both dropdown marks are pure functions and are
	all checked in `tests/test_web.py` at no cost here — six tests there against one gesture and
	three assertions here. What is left is the wiring, which is the only half that has ever
	actually broken.

	**Raised to thirty-five for `SR#800`, and the case is that the raise beside it did not
	cover it.** `SR#863` measures the add form across two *views* and this measures the add form
	against the *edit* form — which sits inside `.detail` and is 50px narrower, one whole column
	at `minmax(190px, 1fr)`. A cap is a maximum, so `SR#863`'s fix could never bind there, and
	its test could never see it: both of its pages measure the same element in the same
	container. **Two tests of one shape, and neither is the other's duplicate.**

	**Nothing cheaper reaches it.** The claim is where a field lands, which is the cascade, a
	container's padding and a grid resolving against an available width — `tests/test_web.py`
	has no cascade and `SR#863`'s own comment records that asking for the specified
	`grid-template-columns` measures the stylesheet rather than the layout.

	**Read for fat**: one gesture per form and two assertions, and the second is what stops the
	first passing at a viewport where both collapse to one column.
	"""

	source = pathlib.Path(__file__).read_text(encoding="utf-8")
	tests = re.findall(r"^def (test_\w+)", source, re.M)

	assert len(tests) > 1, "no tests were found, so this is checking nothing"

	assert len(tests) <= 35, (
		f"this file holds {len(tests)} tests: {tests}. Seventeen answering what only a browser "
		f"can is the agreed scope; past this it is a second suite, and the fast one is the one "
		f"that stops being run. Raising it is a decision — read the addition for fat first, and "
		f"read every raise in this docstring as a set: it has moved 17 to 34 in nine days."
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

	opened, _written, _refusing, *_ = running
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


def test_the_two_forms_put_a_field_in_the_same_place (running: typing.Any) -> None:
	"""`SR#800`, Simon driving `SR#755`: *"I instinctively look in the same place."*

	The add form and the edit form are the same component and the same field order — what
	differed was the **wrapping**, because ``auto-fit`` asks the container and the two forms have
	different containers. The edit form sits inside ``.detail``, which spends 24px of padding and
	a 1px border on each side. 50px, which at ``minmax(190px, 1fr)`` is a whole column.

	**`SR#863` fixed the neighbouring half and could not fix this one**, which is why this
	outlived it by ten days. That gave ``.adding`` its own ``max-width``, and a maximum binds
	only where there is room to spare — inside ``.detail`` the form was *already* narrower, so
	the cap was never reached and the comment claiming the count was "now stable" was true of one
	form and false of the other.

	**Wide on purpose.** Below 906px both forms collapse to the same smaller count and agree for
	a reason that has nothing to do with the fix — the same trap `SR#863`'s own test records one
	function down.

	**Read for fat**: one gesture per form and two assertions. The second is what stops the first
	passing against a viewport where both forms are one column, which is the state a broken fix
	most easily produces.
	"""

	opened, _written, _refusing, *_ = running

	# The same measurement `SR#863` uses, and for its reason: a `<fieldset>` lays its contents
	# out in an anonymous box, so the *specified* `grid-template-columns` measures the stylesheet
	# rather than the layout. Distinct left offsets are the column count a reader meets.
	lands = """node => new Set(
		[...node.children].map(kid => Math.round(kid.getBoundingClientRect().left))
	).size"""

	# **Two pages, because an open item carries no capture box** — `Adding` is rendered by the
	# list and the board and not by `Detail`. The viewport is set identically on both, which is
	# the whole of what has to be held equal for the comparison to mean anything.
	listing = opened("/projects/subroutine")
	listing.set_viewport_size({"width": 2200, "height": 900})
	listing.click(".adding .more")
	listing.wait_for_selector(".adding .details", timeout=10_000)
	adding = listing.eval_on_selector(".adding .details", lands)
	listing.close()

	item = opened("/projects/subroutine/ui/42")
	item.set_viewport_size({"width": 2200, "height": 900})
	item.click(".detail button.edit")
	item.wait_for_selector(".detail form.editing .details", timeout=10_000)
	editing = item.eval_on_selector(".detail form.editing .details", lands)
	item.close()

	assert adding == editing, (
		f"the add form lays its fields out in {adding} columns and the edit form in {editing}, "
		f"so the same field is in a different place depending on which one you opened. A form "
		f"whose fields move cannot be filled in without reading every label."
	)

	assert adding > 1, (
		f"both forms are {adding} column wide, so they agree for a reason that has nothing to "
		f"do with the fix and this would pass against any stylesheet at all."
	)


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

	opened, _written, _refusing, *_ = running
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

	opened, _written, _refusing, *_ = running

	def background (page: typing.Any) -> str:
		"""Return the colour the page has actually painted behind everything."""

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

	opened, _written, _refusing, *_ = running
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

	opened, _written, _refusing, *_ = running
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

	opened, written, refusing, *_ = running
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

	opened, _written, _refusing, *_ = running
	page = opened("/projects?view=list")
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

	opened, _written, _refusing, *_ = running
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

	opened, _written, _refusing, *_ = running
	page = opened("/projects?view=board&include_completed=true")
	page.wait_for_selector(".board .rows li", timeout=10_000)

	page.locator("h1 a").click()

	# The agenda has neither, so both halves of the defect are one assertion.
	page.wait_for_url("http://app.test/", timeout=10_000)

	# **Waited for, not read once.** The board was on the page a moment ago, so a selector that
	# was satisfied then is satisfied again immediately and the assertion lands before the
	# re-render — which is how a test of this passes against the defect it was written for.
	page.wait_for_selector(".board", state="detached", timeout=10_000)

	# **And waited for the thing that replaces it** (`#998`). Waiting for the board to go
	# is right and is why the line above exists, but `count()` is a read rather than a
	# wait — so between the board detaching and the agenda's fetch resolving there is a
	# window, and under a loaded machine this landed inside it. It then failed saying the
	# page had not followed the address, which is `#962` exactly, about a page that was
	# one tick behind. A guard that accuses the product of the defect it was written for
	# is worse than one that is merely slow.
	page.wait_for_selector(".listing.agenda", timeout=10_000)

	# Kept, because a wait that times out and an assertion that fails say different
	# things and the second is the one that names what was wrong.
	assert page.locator(".listing.agenda").count() > 0, (
		"the address went home and the page did not follow it"
	)

	# **And the frame came with it** (`SR#967`). `SR#962` and `SR#963` shipped in one commit, each
	# with its own guard, and the case that failed was where they touch: `home` wrote an
	# address with no view and never told the state, so the agenda arrived at the board's
	# width. This test pressed the control and asserted the rows changed, which was true.
	#
	# **Two fixes landing together are a third thing to check**, and neither one's own test
	# can see it.
	frame = page.evaluate(
		"() => Math.round(document.querySelector('div.app').getBoundingClientRect().width)"
	)
	screen = page.evaluate("() => document.documentElement.clientWidth")

	assert frame < screen, (
		f"the agenda is {frame}px of a {screen}px screen — it kept the board's frame, because "
		f"going home wrote the address and left the view saying 'board'"
	)


def test_a_row_leaves_a_gap_between_its_address_and_its_title (running: typing.Any) -> None:
	"""`SR#965`, Simon 2026-08-17, from the agenda at `/`.

	`projects/#94Recurring tasks` — the address ran into the title with nothing between them,
	because the column it sits in was a fixed 4.5rem and twelve monospace characters do not fit
	in nine. **A grid item does not wrap a track, it overflows it**, so there was no width at
	which this got better rather than worse.

	**Measured rather than looked at**, which is `SR#911`'s rule: the claim is that two boxes do
	not touch, and `tests/dom.js` has no layout at all. Driven at `/`, because that is the one
	page whose rows carry a workspace — `spansWorkspaces` is what puts it there, and every
	listing inside a workspace has short refs that sit on the floor.
	"""

	opened, _written, _refusing, *_ = running
	page = opened("/")
	page.wait_for_selector(".listing.agenda .row", timeout=10_000)

	measured = page.eval_on_selector_all(
		".listing.agenda .row",
		"""rows => rows.map((row) => {
			const ref = row.querySelector(".ref");
			const title = row.querySelector(".title");
			if (!ref || !title) return null;

			/* **The text's own extent, not the element's box.** `.ref` is a grid item, so its
			   box is the track — 72px whatever it holds — and text that does not fit simply
			   overflows it. Measuring the boxes says the gap is 12px on exactly the row a
			   reader can see is broken, which is what the first version of this test did and
			   why it survived being falsified against the fixed track. A `Range` reports where
			   the glyphs actually end. */
			const extent = document.createRange();

			extent.selectNodeContents(ref);

			return {
				said: ref.textContent.trim(),
				gap: Math.round(
					title.getBoundingClientRect().left - extent.getBoundingClientRect().right
				),
			};
		}).filter(Boolean)""",
	)

	assert measured, "no agenda row carries both an address and a title"
	assert any("/" in row["said"] for row in measured), (
		f"no row says which workspace it is from, so this is measuring the short case that "
		f"always fitted: {measured}"
	)

	for row in measured:
		assert row["gap"] > 0, (
			f"the address {row['said']!r} touches the title beside it — its column is too "
			f"narrow for it and a grid item overflows rather than wrapping: {measured}"
		)


def test_the_agenda_names_a_workspace_even_where_they_all_agree (
	running: typing.Any,
) -> None:
	"""`SR#966`, Simon 2026-08-17. `/` names no workspace, so every row says which one it is in.

	**This asked `spansWorkspaces` and so dropped the label whenever the rows agreed** — the
	drop-if-uniform rule decision `SR#957` §4 rules out for this surface, and for the reason
	written into `projectLabel`: the page polls, so a label that shortens because a stranger
	filed something elsewhere is a clickable control changing under the cursor. It demonstrated
	itself within the hour, on rows that had not changed.

	**Driven on an agenda holding one workspace**, because the fixture spans two and the case
	that broke is the one nothing built. That is the same gap twice today: a harness that cannot
	reach a state looks exactly like one nobody needed it to.
	"""

	opened, _written, _refusing, *_ = running
	page = opened("/", agenda=ONE_WORKSPACE)
	page.wait_for_selector(".listing.agenda .mark", timeout=10_000)

	labels = page.eval_on_selector_all(
		".listing.agenda a.mark", "marks => marks.map((mark) => mark.textContent.trim())"
	)

	assert labels, "no row carries a project label at all"

	for said in labels:
		assert said.startswith("projects/"), (
			f"a row on the agenda says {said!r} and not which workspace it is in — every row "
			f"being in one is not the address having named it"
		)

	# **And the ref beside it** (`SR#968`). `SR#966` fixed the label and left this asking whether
	# the rows *happened* to span workspaces — the same drop-if-uniform rule, one column along,
	# raised as a neighbouring question on that item and not joined to it.
	refs = page.eval_on_selector_all(
		".listing.agenda .ref", "cells => cells.map((cell) => cell.textContent.trim())"
	)

	assert refs, "no row carries a ref"

	for said in refs:
		assert said.startswith("projects/"), (
			f"a row is addressed {said!r} — with no workspace in the URL a bare number does "
			f"not say which workspace the item is in"
		)


def test_the_workspace_control_says_what_is_showing_and_goes_both_ways (
	running: typing.Any,
) -> None:
	"""`SR#969`, Simon 2026-08-17, from the agenda at `/`.

	It marked a workspace selected while the page showed **every** workspace — untrue, and a
	dead end with it: a `<select>` fires no `change` for the option already selected, so the one
	workspace the control claimed was the one it could not reach.

	**Only a browser can answer either half.** `tests/dom.js` drops every attribute but `href`,
	so it cannot read `selected`; and *choosing the value that is already chosen does nothing*
	is a fact about a real control, not about markup.

	**Driven as a round trip**, because a hint that cannot be chosen again would pass the first
	half and leave the control one-way — which is the same inert shape one step along.

	**Its values became addresses in `SR#975`** and the intent is unchanged: the control now
	offers projects as well as workspaces, so an entry has to say which it is, and `placesToGo`
	answers that by emitting the address `goTo` reads back. What is asserted here is the same
	claim in the spelling the control now uses.
	"""

	opened, _written, _refusing, *_ = running
	page = opened("/")
	page.wait_for_selector(".listing.agenda", timeout=10_000)

	control = page.locator("header .who select")

	assert control.input_value() == "", (
		"the agenda shows every workspace and the control names one of them"
	)

	control.select_option("/projects")
	page.wait_for_url("http://app.test/projects*", timeout=10_000)
	# **Still an agenda, and that is `SR#1215` rather than a regression.** Choosing a workspace
	# used to leave the agenda because `/` was the only address one had; a place has its own now,
	# and the arrangement the reader is in follows them into it (`SR#745`). What this test is
	# about — that one workspace is still somewhere you can go — is unchanged.
	page.wait_for_selector(".listing.agenda", timeout=10_000)

	assert control.input_value() == "/projects", "narrowing left the control saying otherwise"

	control.select_option("")
	page.wait_for_url("http://app.test/", timeout=10_000)
	page.wait_for_selector(".listing.agenda", timeout=10_000)

	assert control.input_value() == "", "there is no way back to everything from the control"


def test_one_workspace_is_still_something_you_can_choose_and_go_into (
	running: typing.Any,
) -> None:
	"""`SR#975`, Simon's, and it is two claims that need the same page.

	**One workspace used to render the name as inert text**, so the only thing saying where the
	reader was could not be used to reach it — and on the agenda, `/` is the only address there
	is. **And the control now offers projects**, which is the half that has actually broken
	elsewhere: `go` writes the address bar and nothing else (`SR#962`), so a handler that stops
	there moves the address and leaves the page exactly as it was. That has shipped three times
	and every one was found by a browser, because the callback lives in `App` (`SR#640`).

	**So the address *and* the rows are both asserted.** Either alone passes against the defect:
	the address moves whether or not the page followed, and the rows would be right if the app
	had simply been opened there.

	**A project the fixture nests**, because the address for one is rebuilt from the tree —
	`key` is unique only among its siblings since `SR#958` — and a root project's address is its
	key either way, so a one-level case cannot tell a reassembled path from a bare key.
	"""

	opened, _written, _refusing, roster, _missing, reads, *_ = running
	roster[0] = ALONE
	page = opened("/")
	page.wait_for_selector(".listing.agenda", timeout=10_000)

	control = page.locator("header .who select")

	assert control.count() == 1, "one workspace still has no control to choose it with"

	# **By name, in one register with the projects underneath** — `SR#980`. The fixture's
	# workspace is slugged `projects` and is called `Subroutine`, so a version reading the slug
	# renders a different word here.
	assert [one.strip() for one in control.locator("option").all_inner_texts()] == [
		"All workspaces", "Subroutine",
	]

	control.select_option("/projects")
	page.wait_for_url("http://app.test/projects*", timeout=10_000)
	# **Still an agenda since `SR#1215`**, for the reason written out on the test above: a
	# place has an agenda of its own now, and the arrangement follows the reader into it.
	page.wait_for_selector(".listing.agenda", timeout=10_000)

	# The projects arrive with the workspace, so the tree is only offered once inside one.
	#
	# **And they arrive on their own request** (`#998`). Waiting for the listing says the
	# workspace landed, not that its projects did — so reading the options here caught a
	# control holding two of six and failed as though the tree had been flattened, which
	# is a claim about the code rather than about the clock. Second instance of the shape
	# in this file: wait for the thing being asserted on, not only for what preceded it.
	#
	# **Said in CSS rather than in JavaScript** (`#1000`). `wait_for_function` takes its
	# predicate as a *string*, which Playwright evaluates in the page — and `#805` serves a
	# policy with no `unsafe-eval`, so Chromium refuses it and the guard blames the product
	# for a policy the product is right to have. `attached` because an `<option>` is never
	# *visible* to Playwright, so the default state times out on a control already correct.
	page.wait_for_selector(
		"header .who select option:nth-child(3)", state="attached", timeout=10_000
	)

	assert [one.strip() for one in control.locator("option").all_inner_texts()] == [
		"All workspaces", "Subroutine", "Acme", "Inbox", "Websites", "Handouts",
	], "the tree is in creation order, or the nesting was flattened"

	reads.clear()
	control.select_option("/projects/websites/handouts")
	page.wait_for_url("http://app.test/projects/websites/**", timeout=10_000)

	# **The page followed, not only the bar** — the whole of `SR#962`, and the request is the
	# only thing that says so. **Not the control's own value**, which was this test's first
	# version and could not fail: `select_option` sets the DOM value itself, so it reads back
	# whatever was chosen whether or not anything re-rendered. **Not the markup either**, since
	# this fixture answers every listing with the same rows.
	_until(lambda: any("project=websites%2Fhandouts" in one for one in reads))

	assert any("project=websites%2Fhandouts" in one for one in reads), (
		f"nothing was asked for that project, so the address moved and the page did not: {reads}"
	)

	# **The path, because the arrangement rides along by `SR#745`**: a view is written out even
	# when it is the default, so what you send somebody is what you were looking at.
	assert page.url.split("?")[0] == "http://app.test/projects/websites/handouts", (
		"the page moved and the address did not"
	)

	# **Put back, which this did not do until `SR#1040`.** `running` is module-scoped, so a
	# one-workspace roster left here is what every later test reads — and the two tests that
	# need an item in the *other* workspace failed on a thirty-second timeout for a row that
	# could not be built, which says nothing about the roster. `test_prioritising_a_project…`
	# already restores its own holder and says why; this one is the same rule, missed.
	roster[0] = IDENTITY


def test_searching_for_a_ref_opens_that_item_and_a_dead_one_still_searches (
	running: typing.Any,
) -> None:
	"""`SR#976`, Simon's. Both halves, because either alone passes against the wrong build.

	A version that always jumps passes the first; one that never jumps passes the second. What
	separates them is one instance answering for `SR#42` and refusing `SR#4242`.

	**In a browser because the wiring is the risk.** Whether `#4242` parses as a ref is a pure
	function and is checked in `tests/test_web.py`; whether choosing it *moves the page* is
	`SR#962`'s shape, which has shipped three times and has never once been caught by anything
	else. `tests/dom.js` cannot dispatch an event, and says so.
	"""

	opened, _written, _refusing, _roster, missing, *_ = running
	missing[0] = {"4242"}
	page = opened("/projects?view=list")
	page.wait_for_selector(".listing", timeout=10_000)

	search = page.locator("header form.seeking input[name='q']")

	search.fill("#42")
	page.locator("header form.seeking button[type='submit']").click()

	# **The item, not a results page.** `.detail` is the opened item; the address is asserted
	# beside it because moving one without the other is the whole failure being guarded against.
	page.wait_for_selector(".detail", timeout=10_000)
	page.wait_for_url("http://app.test/projects/**42", timeout=10_000)

	assert "q=" not in page.url, "a ref that resolved still wrote a search into the address"

	# **And one that resolves nowhere is an ordinary search**, which is what keeps this a
	# shortcut rather than a second grammar the reader has to know.
	search.fill("#4242")
	page.locator("header form.seeking button[type='submit']").click()
	page.wait_for_url("**q=%234242", timeout=10_000)

	assert page.locator(".detail").count() == 0, (
		"a ref nobody minted opened something anyway"
	)


def test_prioritising_a_project_writes_it_and_reads_back_what_it_changed (
	running: typing.Any,
) -> None:
	"""`SR#986`, decision `SR#982`, and the claim is that **three things happen, not one**.

	The callback writes to the workspace, refetches the identity, and reloads the listing.
	Writing alone reorders nothing a reader can see; reloading alone moves the rows under labels
	still saying the old thing — the masthead's mark, the form's dropdown and the header
	sentence all read `me.workspaces[].prioritised_project`, so a write that skipped the refetch
	would change the order and leave three places disagreeing with it.

	**Only a browser can answer it.** The callback lives in `App`, which `tests/dom.js` cannot
	execute (`SR#640`), and that file will not dispatch an event by decision — so the button and
	everything behind it are reachable from nowhere else. This is `SR#962`'s recorded shape a
	fifth time: the rule right, the display right, and the wire between them checked by nothing.

	**Asserted on the requests rather than on the markup**, which is this fixture's own rule and
	`SR#962`'s lesson: a handler that stops halfway leaves the page looking exactly as it should.
	"""

	opened, written, _refusing, roster, _missing, reads, *_ = running
	before = roster[0]
	page = opened("/projects/subroutine?view=list")
	page.wait_for_selector(".narrowed", timeout=10_000)

	control = page.locator(".narrowed button.prioritise")

	assert control.inner_text().strip() == "Prioritise", (
		"the fixture's workspace has no focus, so the control offers to give it one"
	)

	reads.clear()
	control.click()

	# **Waited on the control's own label, which is the claim rather than a proxy for it.**
	# *Stop prioritising* can only be drawn after the identity came back and every label
	# recomputed from it, so this is the refetch observed rather than assumed.
	#
	# **Not `.narrowed`, which never left** — `SR#998`'s trap, where a wait already satisfied
	# lets the assertion run a tick early and blame the product. And **not `_until`**, which
	# cannot work here: Playwright's synchronous API dispatches route handlers only while the
	# caller is inside one of its own calls, so a Python spin-loop waiting on what a handler
	# records waits for something that cannot happen until it stops waiting. Measured — the
	# three requests below all arrive, and none of them had while this waited five seconds.
	page.wait_for_selector(".narrowed button.prioritise:text('Stop prioritising')",
		timeout=10_000)

	# **Selected rather than taken as the first**, because `running` is module-scoped: `written`
	# holds every write this whole file has made, and reading position 0 is reading somebody
	# else's test. Passed alone and failed in a full run, which is the one direction that
	# ordering-dependent state fails in.
	sent = [one for one in written if one[0] == "PATCH" and "workspaces" in one[1]]

	assert sent, f"pressing Prioritise wrote no workspace change: {written}"
	assert sent[-1][1].endswith("/workspaces/projects"), (
		f"the write went to {sent[-1][1]}, and the state belongs to the workspace"
	)
	assert "subroutine" in (sent[-1][2] or ""), (
		f"the body named no project: {sent[-1][2]!r}"
	)

	# **The identity is proved by the label above; this is the third step.** Reloading is what
	# makes the reordering visible, and it is the half that fails silently — a page whose labels
	# all agree, over rows in the order they were in before.
	assert any("/tasks" in one for one in reads), (
		f"nothing reloaded the listing, so the rows the change is *for* have not moved: {reads}"
	)

	# **Put back, unlike the rest of this file.** The fixture is module-scoped, so a prioritised
	# project left on the roster would mark a project in every masthead drawn after this — a
	# state no other test asked for and none would explain.
	roster[0] = before


def _comments (page: typing.Any) -> None:
	"""Write a comment on the open item."""

	page.fill(".detail form.saying textarea", "Noted from the wrong workspace.")
	page.click(".detail form.saying button[type='submit']")


def _renames (page: typing.Any) -> None:
	"""Open the edit form and save a new title."""

	page.click(".detail button.edit")
	page.fill(".detail form.editing input[name=title]", "Renamed by accident")
	page.click(".detail form.editing button[type='submit']")


#: Every control on an open item that writes, and the gesture that works it. **The list is the
#: population**: a control added to `Detail` and not driven here is one nothing has asked this
#: question of, which is how the reported defect reached seven controls at once.
#:
#: **Two are missing and are named rather than left as an unexplained absence.** *Assign* is
#: drawn only where the roster answers with members, and this fixture answers every collection
#: it does not know with an empty one. *Link* needs a ref that resolves, and the form tries each
#: linkable kind in turn — so driving it here would assert on how this stand-in answers a search
#: rather than on which workspace it was asked about. Both go through the same one value as the
#: five below, and that value is what the falsification pass moved.
#:
#: **The widest is `save`**, which carries the title, the description, both dates and the status
#: in one request: against the wrong item it overwrites all of them at once.
ITEM_WRITES: tuple[tuple[str, str, typing.Callable[[typing.Any], None]], ...] = (
	(
		"status",
		"PATCH",
		lambda page: page.select_option(".detail .doing label.assign select", "doing"),
	),
	("complete", "POST", lambda page: page.click(".detail .doing button.finish")),
	("comment", "POST", _comments),
	("save", "PATCH", _renames),
	("unlink", "DELETE", lambda page: page.click(".detail button.unlink >> nth=0")),
)


def test_a_write_from_an_open_item_names_the_workspace_that_item_is_in (
	running: typing.Any,
) -> None:
	"""`SR#1040`, Simon 2026-08-20, on the served instance. It cancelled the wrong task.

	Two workspaces held an item numbered 20. Changing the status of the one in `sandbox` moved
	the one in `projects`, and the page then followed it there — so the reader was left looking
	at a different item, in a different workspace, which they had just changed by accident.

	**`App` holds two answers to *which workspace* and the writes read the wrong one.**
	`open.slug` is the workspace the item was actually read from — its own comment at `show`
	says it "is the only copy that is always right" — and `workspace` is the switcher's, set on
	mount and by the switcher alone. Opening a row from the agenda moves the first and leaves
	the second, and every write was built from the second.

	**Driven from the agenda, because that is the only place the two can disagree.** Every other
	test in this file opens an item in the workspace the switcher holds, where the right answer
	and the wrong one are the same string — so a test asserting that the write named *a*
	workspace passes whichever variable produced it. A harness that cannot reach a state looks
	exactly like one nobody needed it to, for the fifth time here.

	**Asserted on the requests**, which is this fixture's own rule: the wrong write succeeds,
	so the page looks entirely correct afterwards — it is simply describing somebody else's
	item.
	"""

	opened, written, _refusing, roster, _missing, reads, *_ = running

	# **Asserted rather than set**, because a one-workspace roster left behind by an earlier
	# test makes this fail on a thirty-second timeout for a row that cannot be built — which
	# reads as a broken selector rather than as the fixture it is. Setting it here would hide
	# the leak instead; `running` is module-scoped and a test that changes a holder puts it back.
	assert len(roster[0]["workspaces"]) > 1, (
		"the roster holds one workspace, so no row can be in another one and this test cannot "
		"tell the item's workspace from the switcher's. An earlier test left it behind."
	)

	for name, method, drive in ITEM_WRITES:
		page = opened("/")
		page.wait_for_selector(".listing.agenda", timeout=10_000)

		# The row from the *other* workspace — `AGENDA`'s `today` is the only one in `w2`.
		page.click(".listing.agenda a.row[href='/personal/subroutine/ui/2']")
		page.wait_for_selector(".detail .doing", timeout=10_000)

		assert "/personal/" in page.url, (
			f"opening the row left the address at {page.url!r}, so this test is not about an "
			f"item outside the switcher's workspace and can prove nothing"
		)

		written.clear()
		reads.clear()
		drive(page)

		page.wait_for_selector(".note", timeout=10_000)

		sent = [one for one in written if one[0] == method]

		assert sent, f"{name} wrote nothing at all: {written}"

		# **The write first, because it is the harm.** What follows it is a consequence — the
		# re-read below lands on whatever the write moved — and a failure reported there sends
		# the next reader after the wrong half.
		for one in sent:
			assert "workspace_id=personal" in one[3], (
				f"{name} sent {one[0]} {one[3]!r} about an item in 'personal'. A ref is unique "
				f"per workspace, so this changes whatever item wears that number in the "
				f"workspace the switcher happens to hold, and the instance does as it is told."
			)

		# **And the read afterwards**, which is what put the reader in front of the wrong item.
		# `wrote` re-reads through `show`, whose slug defaults to the switcher's too — so this
		# is the same defect a second time rather than a consequence of the first, and fixing
		# only the write would leave the page still walking away from the item on screen.
		asked = [one for one in reads if one.startswith("v1/tasks/")]

		assert asked, f"{name} named no task at all: {reads}"

		for one in asked:
			assert "workspace_id=personal" in one, (
				f"after {name} the page asked for {one!r}, so it read an item back out of the "
				f"wrong workspace and rewrote the address to match"
			)

		page.close()


def _statuses_offered (page: typing.Any) -> list[str]:
	"""What the open item's status control is willing to move it to."""

	return [
		one.strip()
		for one in page.locator(".detail .doing label.assign select option").all_inner_texts()
	]


def test_an_open_item_is_furnished_from_the_workspace_it_is_in (
	running: typing.Any,
) -> None:
	"""`SR#1041`, the read half of `SR#1040` and the half that fails as a wrong *offer*.

	Everything workspace-shaped — the statuses, the projects work can be filed in, who it can be
	handed to, and what the reader may do — was fetched for the workspace the switcher held and
	handed to an item that may be in another one. So a status picker offered statuses this item
	cannot be moved to and omitted the ones it can, and controls were drawn that the instance
	would have refused when pressed.

	**`words`'s own comment already states the rule and applies it to the wrong axis**: *"a type
	dropdown offering another workspace's types is worse than one offering none: the second is
	visibly unfinished and the first is confidently wrong."* That was written about switching
	workspaces and never carried to an item opened from elsewhere.

	**Both halves, because they fail in opposite directions.** A build that fetched the item's
	vocabulary and kept the switcher's permissions passes the first; one that did the reverse
	passes the second.

	**Only a browser can ask it**: which options a `<select>` really holds and whether a control
	exists at all are facts about a rendered page, and every binding here lives in `App`, which
	`tests/dom.js` cannot execute (`SR#640`).
	"""

	opened, _written, _refusing, roster, _missing, reads, unreadable = running
	before = roster[0]

	page = opened("/")
	page.wait_for_selector(".listing.agenda", timeout=10_000)

	# The switcher's own workspace first, so the two vocabularies are shown to differ before
	# anything is claimed about which one an item got. Without this the test passes against a
	# fixture serving one vocabulary everywhere, which is what it did until now.
	page.click(".listing.agenda a.row[href='/projects/subroutine/ui/94']")
	page.wait_for_selector(".detail .doing", timeout=10_000)
	mine = _statuses_offered(page)

	# **The switcher's workspace has people in it and the item's has none**, which is what makes
	# a control drawn from the wrong roster visible at all. Asserted here rather than assumed,
	# for the reason the vocabulary is: a fixture where both are empty proves nothing.
	assert page.locator(".detail .doing label.assign").count() == 2, (
		"the switcher's workspace has members and no assignee control was drawn, so the "
		"comparison below cannot tell a right roster from a wrong one"
	)
	assert "Triage" in mine, f"the switcher's workspace does not offer Triage: {mine}"
	assert "Parked" not in mine, f"the two workspaces do not differ, so this proves nothing: {mine}"

	page.go_back()
	page.wait_for_selector(".listing.agenda", timeout=10_000)

	reads.clear()
	page.click(".listing.agenda a.row[href='/personal/subroutine/ui/2']")
	page.wait_for_selector(".detail .doing", timeout=10_000)
	# **Waited on the option itself, not on `.detail`, which never left.** `SR#998`'s trap: a
	# wait already satisfied lets the assertion run a tick early and blame the product for being
	# one render behind. `attached` because an `<option>` is never *visible* to Playwright.
	page.wait_for_selector(
		".detail .doing label.assign option:text('Parked')", state="attached", timeout=10_000
	)

	theirs = _statuses_offered(page)

	assert "Parked" in theirs, (
		f"the item's own workspace offers Parked and the control does not: {theirs}"
	)
	assert "Triage" not in theirs, (
		f"the control offers {theirs}, which is the *switcher's* vocabulary — this item cannot "
		f"be moved to Triage, and the instance would refuse it by name"
	)

	# **And who it can be handed to**, asked before the edit form replaces this block. Nobody is
	# in this item's workspace, so the control is absent; drawn from the switcher's roster it
	# would offer two people who are not there. Both labels here are `label.assign` — one is the
	# status, and a second one is the assignee.
	assert page.locator(".detail .doing label.assign").count() == 1, (
		"an assignee control was drawn for an item whose workspace has nobody in it, so it came "
		"from the switcher's roster"
	)

	# **Where it can be filed.** Asserted on what the form offers rather than on the request
	# that fetched it: the request proves the fetch and says nothing about which answer the
	# control was handed, which is the half that was wrong.
	page.click(".detail button.edit")
	page.wait_for_selector(".detail form.editing", timeout=10_000)

	filable = [
		one.strip()
		for one in page.locator(
			".detail form.editing select[name=project] option"
		).all_inner_texts()
	]

	assert any("Errands" in one for one in filable), (
		f"the form offers {filable}, and this item's own workspace has Errands in it"
	)
	assert not any("Websites" in one for one in filable), (
		f"the form offers {filable} — those are the *switcher's* projects, and filing this "
		f"item into one of them is a refusal by name"
	)


	# **The second half: what the reader may do *there*.** The roster is swapped rather than the
	# fixture changed, because five other tests need to write on this very item.
	roster[0] = json.loads(json.dumps(before))
	elsewhere = next(one for one in roster[0]["workspaces"] if one["slug"] == "personal")
	elsewhere["permissions"] = ["task:read"]

	page.close()
	page = opened("/")
	page.wait_for_selector(".listing.agenda", timeout=10_000)
	page.click(".listing.agenda a.row[href='/personal/subroutine/ui/2']")
	page.wait_for_selector(".detail", timeout=10_000)

	assert page.locator(".detail form.saying").count() == 0, (
		"the reader may not comment in this item's workspace and was offered the box anyway — "
		"a control that refuses when pressed is worse than one that is not there"
	)
	assert page.locator(".detail button.edit").count() == 0, "Edit was offered and cannot work"
	assert page.locator(".detail .doing button.finish").count() == 0, (
		"Complete was offered and cannot work"
	)

	page.close()

	# **Put back**, because `running` is module-scoped and a read-only workspace left on the
	# roster would take the controls off every later test that needs them.
	roster[0] = before

	# **Nothing rather than the wrong thing, which is the half a fallback would hide.** The two
	# mutations that matter here are opposite: furnishing from the switcher fails the assertions
	# above, and *falling back* to the switcher while the item's own words are unavailable
	# survives every one of them — because by then the right words have arrived. Refusing them
	# outright is the state that separates the two, and it is a real one: a reader whose
	# credential does not reach that workspace.
	unreadable[0] = True

	try:
		page = opened("/")
		page.wait_for_selector(".listing.agenda", timeout=10_000)
		page.click(".listing.agenda a.row[href='/personal/subroutine/ui/2']")
		page.wait_for_selector(".detail .doing", timeout=10_000)
		page.wait_for_timeout(500)

		nothing = _statuses_offered(page)

		assert nothing == [], (
			f"this item's own workspace could not be read and the control offered {nothing} — "
			f"which is the *switcher's* vocabulary, and the one thing worse than an empty "
			f"picker is a confidently wrong one"
		)

		# The rest of the page is unharmed, so this is an empty control rather than a failure.
		assert page.locator(".detail .doing button.finish").count() == 1, (
			"the item stopped being completable, so this is a broken page rather than a "
			"control with nothing to offer"
		)

		page.close()

	finally:
		unreadable[0] = False


def test_the_rows_a_page_shows_come_from_the_workspace_its_address_names (
	running: typing.Any,
) -> None:
	"""`SR#1042`, the last of `SR#1040`'s family and the one no write is involved in.

	`SR#1040` made the *item* read from the workspace its address names and `SR#1041` made what
	it is furnished with follow; the **listing underneath** still came from the switcher's. So
	going into a project from an item opened elsewhere, or stepping forward onto one, left one
	workspace's backlog under an address naming another — and closing the item showed it.

	**Two ways in, because they are two mechanisms and one is not enough.** A project chip goes
	through `narrow`, which the masthead also reaches; Back and Forward go through the
	`popstate` handler. Fixing either alone leaves the other, and both end at `load`.

	**Asserted on the request**, which is this fixture's own rule: it answers every listing with
	the same rows, so the page looks identical either way and only what it asked for says which
	workspace the rows are from.
	"""

	opened, _written, _refusing, roster, _missing, reads, _unreadable = running

	assert len(roster[0]["workspaces"]) > 1, "there is no second workspace to be wrong about"

	page = opened("/")
	page.wait_for_selector(".listing.agenda", timeout=10_000)
	page.click(".listing.agenda a.row[href='/personal/subroutine/ui/2']")
	page.wait_for_selector(".detail", timeout=10_000)

	# Into that item's project, by the control on the item itself.
	reads.clear()
	# **A linked item's project chip**, which is the only address on this page that goes into a
	# project — and both ends of a link are in the item's workspace, never the switcher's.
	chip = page.locator(".detail a.mark.address").first
	into = chip.get_attribute("href")

	assert into is not None and into.startswith("/personal/"), (
		f"a link on an item in 'personal' is addressed {into!r} — following it opens whatever "
		f"wears that number in the workspace the switcher holds"
	)

	chip.click()
	page.wait_for_url(f"**{into}*", timeout=10_000)

	# **Either endpoint, because the arrangement rides along now** (`SR#1215`). The reader was
	# on an agenda, so narrowing into a project keeps them on one — and the claim being made
	# here is about *which workspace was asked*, not about which of the two reads answered it.
	# Pinning the endpoint would make this test fail whenever the default arrangement changed,
	# which is a fact about the page rather than about the defect it guards.
	rows = ("v1/tasks", "v1/agenda")

	_until(lambda: any(one.split("?")[0] in rows for one in reads))
	listed = [one for one in reads if one.split("?")[0] in rows]

	assert listed, f"nothing loaded any rows, so the address moved and the page did not: {reads}"

	for one in listed:
		assert "workspace_id=personal" in one, (
			f"the address says personal and the rows were asked of {one!r} — a listing of one "
			f"workspace under an address naming another"
		)

	# And the same claim reached by stepping forward, which never touches `narrow`.
	page.goto("http://app.test/")
	page.wait_for_selector(".listing.agenda", timeout=10_000)
	page.click(".listing.agenda a.row[href='/personal/subroutine/ui/2']")
	page.wait_for_selector(".detail", timeout=10_000)
	page.go_back()
	page.wait_for_selector(".listing.agenda", timeout=10_000)

	reads.clear()
	page.go_forward()
	page.wait_for_selector(".detail", timeout=10_000)
	_until(lambda: any(one.split("?")[0] in rows for one in reads))

	stepped = [one for one in reads if one.split("?")[0] in rows]

	assert stepped, f"stepping forward loaded no listing behind the item: {reads}"

	for one in stepped:
		assert "workspace_id=personal" in one, (
			f"the listing behind the item was asked of {one!r}, so closing it shows another "
			f"workspace's backlog"
		)

	# **And the switcher followed, which loading the right rows does not by itself say.** The
	# state everything *else* is drawn from — the capture box, the masthead, the next narrowing
	# — is what diverged in the first place, and a page whose rows are right while that still
	# names another workspace is the same defect one action later. Falsified: leaving the load
	# correct and dropping `enter` passes every assertion above.
	page.click(".detail a.back")
	page.wait_for_selector(".listing .adding", timeout=10_000)

	page.wait_for_selector(
		"header .who select option:text('Errands')", state="attached", timeout=10_000
	)

	places = [
		one.strip() for one in page.locator("header .who select option").all_inner_texts()
	]

	assert "Errands" in places and "Websites" not in places, (
		f"the masthead offers {places} — the rows came from the right workspace and the state "
		f"everything else is drawn from still names the other one, so the capture box, the next "
		f"narrowing and this control all start from the wrong place"
	)

	# **And one more gesture, because the two halves of arriving are separable.** Refetching the
	# projects moves the masthead; moving `workspace` is what every *other* reader of it needs,
	# and `chooseSearch`'s own comment says it takes "this one" with nothing to decide. So a
	# search is the cheapest thing that asks the state directly — falsified by leaving the
	# refetch in and dropping the assignment, which passes everything above.
	reads.clear()
	page.fill("form.seeking input[name=q]", "cursor")
	page.press("form.seeking input[name=q]", "Enter")
	_until(lambda: any("q=cursor" in one for one in reads))

	searched = [one for one in reads if "q=cursor" in one]

	assert searched, f"the search asked for nothing: {reads}"

	for one in searched:
		assert "workspace_id=personal" in one, (
			f"the search went to {one!r} — refs are per workspace, so this reader is searching "
			f"somewhere other than the page they are looking at"
		)

	page.close()


def test_stepping_back_onto_an_item_reads_it_where_its_address_says (
	running: typing.Any,
) -> None:
	"""`SR#1040`'s second half, which no write is involved in and nobody has met.

	`arrive` — the `popstate` handler — has the address in hand and reads neither half of it: it
	asks `show` for the ref and lets the slug default to the switcher's workspace. So stepping
	*forward* onto an item outside it opens whatever wears that number inside it instead, under
	an address saying otherwise.

	**The same variable as the writes above and a different mechanism**, which is why it is its
	own test: fixing every write leaves this untouched, and a reader who steps back and forward
	is not doing anything unusual. It is a read, so nothing is damaged — what is wrong is that
	the page confidently shows the wrong item.

	**Only a browser can answer it.** `popstate` is a real history event; `tests/dom.js` cannot
	press Back and will not dispatch an event by decision (`SR#640`).
	"""

	opened, _written, _refusing, roster, _missing, reads, *_ = running

	assert len(roster[0]["workspaces"]) > 1, (
		"the roster holds one workspace, so there is no second one to step back into."
	)

	page = opened("/")
	page.wait_for_selector(".listing.agenda", timeout=10_000)
	page.click(".listing.agenda a.row[href='/personal/subroutine/ui/2']")
	page.wait_for_selector(".detail", timeout=10_000)

	there = page.url

	assert "/personal/" in there, f"the item did not open in its own workspace: {there!r}"

	page.go_back()
	page.wait_for_selector(".listing.agenda", timeout=10_000)

	reads.clear()
	page.go_forward()
	page.wait_for_selector(".detail", timeout=10_000)

	asked = [one for one in reads if one.startswith("v1/tasks/")]

	assert asked, f"stepping forward read no item at all: {reads}"

	for one in asked:
		assert "workspace_id=personal" in one, (
			f"the address says {there!r} and the page asked for {one!r} — so it is showing "
			f"whichever item wears that number in the workspace the switcher holds"
		)

	page.close()


def test_a_closed_item_is_struck_through_and_a_mark_is_drawn_as_a_row_does (
	running: typing.Any,
) -> None:
	"""`SR#970`, Simon 2026-08-17, reading `SR#94`'s own links.

	Two claims, and both are about the cascade rather than about markup — which is what makes
	them this file's rather than `tests/dom.js`'s. That harness drops every attribute (`SR#784`),
	so it can say the *words* are on the page and nothing at all about how they are drawn.

	**A closed item is struck through**, which he asked for and which no computed style would
	have had a reason to be checked without. Its own status chip is what keeps `SR#102`
	satisfied, and the words are asserted next door; this is the half that needs a cascade.

	**Both lists this page draws**, since `SR#1218` gave it a *Parts* list in the same format.
	One claim reached twice rather than two tests: the rule is about a closed item and not about
	which list it is on, and a stylesheet striking everything would satisfy either half alone.

	**And a mark looks the same here as on a row**, which is the whole request: *the same type
	of indicators should be present on all, so a user may familiarize themselves*. It is not
	free — `.linked li > a` is one class more specific than `.mark`, so before it was narrowed
	to direct children every chip on this page took the link colour and lost its border, and
	`.mark.late` and `.mark.blocked` stopped saying anything. That is a rule the shared
	component cannot enforce by itself, and the reason this test measures a *colour* rather
	than a class name.
	"""

	opened, _written, _refusing, *_ = running

	# **The row's own chip, read first and off a listing page.** It is the reference the claim
	# is about, so it has to come from the surface being matched — a literal colour written
	# down here would pass on a theme this app does not serve, and a selector that could fall
	# back to a link's own mark would compare the thing with itself and pass whatever happened.
	#
	# **`a.mark`, which is the project label and the only mark that is an anchor.** Measured
	# rather than assumed: the families are drawn differently on purpose since `SR#1019`, so
	# *any two marks* is not a comparison — it is two different kinds of chip and it fails
	# whatever the cascade does. The anchor is also the one the specificity actually reaches,
	# since `.linked li > a` can only ever have hit an `<a>`.
	listing = opened("/projects")
	listing.wait_for_selector(".rows a.mark", timeout=10_000)
	reference = listing.eval_on_selector(
		".rows a.mark",
		"""one => ({ tone: getComputedStyle(one).color,
			edge: getComputedStyle(one).borderTopStyle,
			pad: getComputedStyle(one).paddingLeft })""",
	)

	page = opened("/projects/subroutine/ui/42", made_of=PARTS)
	page.wait_for_selector(".links li", timeout=10_000)

	drawn = page.eval_on_selector_all(
		# **`.links`, not `.linked`.** Since `SR#1218` this page draws two lists in the same
		# format, so the shared class means *a row in either* — and this test's whole method is
		# counting how many lines are struck through.
		".links li",
		"""rows => rows.map((row) => {
			const anchor = row.querySelector(":scope > a, :scope > button");
			const chip = row.querySelector("a.mark");

			return {
				said: anchor ? anchor.textContent.trim() : "",
				struck: anchor
					? getComputedStyle(anchor).textDecorationLine.includes("line-through")
					: false,
				marks: [...row.querySelectorAll(".marks .mark")].map((one) =>
					one.textContent.trim()),
				/* **The chip's own colour, border and padding, read off the page.** All three,
				   because `.linked a` set every one of them — a mark that took those would be
				   accent-coloured, borderless and flush, and would still read as *styled* to
				   anything checking class names. */
				tone: chip ? getComputedStyle(chip).color : null,
				edge: chip ? getComputedStyle(chip).borderTopStyle : null,
				pad: chip ? getComputedStyle(chip).paddingLeft : null,
			};
		})""",
	)

	assert len(drawn) == 3, f"the links did not render: {drawn}"

	struck = [row["said"] for row in drawn if row["struck"]]

	assert len(struck) == 1, (
		f"exactly one end here is closed, so exactly one line may be struck through — a "
		f"stylesheet that strikes all of them or none says nothing: {drawn}"
	)
	assert "Already finished" in struck[0], f"the wrong line is struck through: {drawn}"

	assert all(row["marks"] for row in drawn), (
		f"a link line carries no marks, so a reader still has to open it to judge it: {drawn}"
	)

	chips = [row for row in drawn if row["tone"] is not None]

	assert chips, (
		f"no link line drew a project chip, so the comparison below has nothing to make — "
		f"the fixture needs an end whose project differs from the page's: {drawn}"
	)

	# **The same rule on the other list this page draws** (`SR#1218`). Folded in here rather
	# than given a test of its own, because it is one claim — *a closed item is struck through*
	# — reached on a second list, and everything else about parts is checked in
	# `tests/test_web.py` at no cost: the rollup, the ordering above `Links`, the cap line and
	# the words beside each row are all attribute-free and belong in the fast suite.
	#
	# **Two parts, one closed**, for the reason the links fixture carries: where every part is
	# finished, *this line is struck* and *this stylesheet strikes everything* are the same
	# observation.
	parts = page.eval_on_selector_all(
		".parts li",
		"""rows => rows.map((row) => {
			const anchor = row.querySelector(":scope > a, :scope > button");

			return {
				said: anchor ? anchor.textContent.trim() : "",
				struck: anchor
					? getComputedStyle(anchor).textDecorationLine.includes("line-through")
					: false,
			};
		})""",
	)

	assert len(parts) == 2, f"the parts did not render: {parts}"

	finished = [row["said"] for row in parts if row["struck"]]

	assert len(finished) == 1, (
		f"exactly one part here is finished, so exactly one may be struck through: {parts}"
	)
	assert "The second piece" in finished[0], f"the wrong part is struck through: {parts}"

	for row in chips:
		assert (row["tone"], row["edge"], row["pad"]) == (
			reference["tone"], reference["edge"], reference["pad"]
		), (
			f"a mark inside a link line is drawn differently from the same mark on a row — "
			f"`.linked li > a` is one class more specific than `.mark`, so without the `>` it "
			f"takes the link colour and loses its outline: {row} against {reference}"
		)


def _contrast (ink: str, behind: str) -> float:
	"""Return the WCAG contrast ratio between two computed CSS colours — `SR#1021`.

	`SR#902` measured this by hand once, found the item number on every row at 3.31:1 against a
	4.5 minimum, and left nothing that could ask again. This is that arithmetic, so a claim
	about legibility is a number rather than a comparison of two other numbers.

	**Takes what the browser computed**, which is always `rgb(...)` or `rgba(...)` — never a
	token, never a hex — so it needs no colour parsing beyond pulling the digits out.
	"""

	def channels (value: str) -> tuple[float, float, float]:
		"""Read a computed `rgb(...)` into its three numbers."""

		found = [float(part) for part in re.findall(r"[\d.]+", value)][:3]

		assert len(found) == 3, f"not a computed colour: {value!r}"

		return typing.cast(tuple[float, float, float], tuple(found))

	def luminance (value: str) -> float:
		"""Return one colour's relative luminance, the way WCAG defines it."""

		linear = []

		for part in channels(value):
			scaled = part / 255
			linear.append(
				scaled / 12.92 if scaled <= 0.04045 else ((scaled + 0.055) / 1.055) ** 2.4
			)

		return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

	lighter, darker = sorted((luminance(ink), luminance(behind)), reverse=True)

	return (lighter + 0.05) / (darker + 0.05)


def test_the_three_families_of_mark_are_drawn_as_three_different_things (
	running: typing.Any,
) -> None:
	"""`SR#1019`, Simon: *"it's hard to tell what kind of label each is."*

	**Eleven chips differed only by tone**, so a project address, a tag, a status and a state
	were one rounded lozenge and the *category* of a mark was carried by nothing at all. The
	families are the fix, and this is the only place that can say they arrived: `tests/dom.js`
	drops every attribute (`SR#784`), so it can report that a class was written and nothing
	whatever about whether the cascade drew anything differently.

	**Three properties rather than one**, because each family is defined by a different one and
	a check on any single property passes for the wrong reason: identity is a *fill*, state is
	an *outline*, and address is the absence of both. Reading only `borderTopStyle` would call
	identity and state the same thing.

	**Read off the page rather than compared against literals.** A colour written down here
	would pass on a theme this app does not serve — `SR#906` made both themes one declaration
	per colour, so the values are the browser's to compute and only their *relationships* are
	this project's to assert.

	**And the word has to be legible against the fill, which is `SR#1021`.** The first version
	of this test compared two backgrounds and two borders and never read the text colour — so
	`color: var(--page)`, naming a token that does not exist, satisfied every assertion here
	while every identity mark on the served instance was a blank lozenge. `SR#965`'s lesson one
	property along: **an element's box is not its content**, and a test that measures boxes is
	blind to the whole family of defects a reader can see.
	"""

	opened, *_ = running

	page = opened("/projects/subroutine/ui/42")
	page.wait_for_selector(".linked .mark", timeout=10_000)

	# What an unfilled mark is actually read against, so the contrast check below has a real
	# second colour rather than `rgba(0, 0, 0, 0)`.
	reference_page = page.eval_on_selector(
		"body", "one => getComputedStyle(one).backgroundColor"
	)

	drawn = page.eval_on_selector_all(
		".linked .mark",
		"""marks => marks.map((one) => {
			const style = getComputedStyle(one);

			return {
				said: one.textContent.trim(),
				family: one.classList.contains("identity") ? "identity"
					: one.classList.contains("state") ? "state"
					: one.classList.contains("address") ? "address"
					: one.classList.contains("context") ? "context"
					: "",
				fill: style.backgroundColor,
				edge: style.borderTopStyle,
				line: style.borderTopColor,
				ink: style.color,
			};
		})""",
	)

	found = {mark["family"] for mark in drawn}

	# **The fixture has to be able to show the difference before anything is compared**, which
	# is `SR#1010`'s lesson: three families and a page carrying one is a test that passes by
	# having nothing to disagree about. `LINKED` serves a type, a non-default status, a blocker
	# and a project, which is one of each.
	assert {"identity", "state", "address"} <= found, (
		f"the page does not carry all three families, so this compares nothing: {drawn}"
	)

	def pick (family: str) -> dict[str, typing.Any]:
		"""Return the first mark of one family, which the assertion above proved is there."""

		return next(one for one in drawn if one["family"] == family)

	identity, state, address = pick("identity"), pick("state"), pick("address")

	assert identity["fill"] != state["fill"], (
		f"identity and state are filled the same, so the two are one shape: {drawn}"
	)
	assert state["edge"] == "solid" and "0)" not in state["line"], (
		f"a state has no visible outline, which is the only thing marking its family: {drawn}"
	)
	assert "0)" in address["line"] or address["edge"] == "none", (
		f"an address is boxed, which is the confusion this replaced — a boxed tag beside a "
		f"boxed status reads as the same kind of thing: {drawn}"
	)
	assert address["fill"] != identity["fill"], (
		f"an address is filled like an identity: {drawn}"
	)

	# **Every mark, not the three picked above.** A filled chip whose word is the colour of its
	# own fill is what shipped, and it is invisible to any comparison *between* marks — so this
	# asks each one whether it can be read, which is the claim a reader actually has.
	for mark in drawn:
		# **A transparent fill is not a colour to be read against**, and `rgba(0, 0, 0, 0)` is
		# a perfectly truthy string — so the page's own background is what an unfilled mark
		# actually sits on. Measured wrongly first, which reported every outlined mark as
		# failing against black.
		behind = reference_page if mark["fill"].endswith(", 0)") else mark["fill"]

		assert _contrast(mark["ink"], behind) >= 4.5, (
			f"a mark's word is not legible against what is behind it: {mark}. `SR#1021` was "
			f"`color: var(--page)` — a token that does not exist, so the declaration was "
			f"dropped and the text inherited the fill it was sitting on."
		)


def test_a_column_that_is_over_starts_folded_and_opens_when_asked (running: typing.Any) -> None:
	"""`#1008`. Simon: cancelled and superseded work need not take the width all day.

	**Geometry, and a browser is the only thing that has it.** The claim is that a shut column
	takes about a character's width where an open one takes 260 or more — `writing-mode:
	vertical-rl` is what makes that true, and it is one CSS declaration with no JavaScript to
	unit-test. A rotation applied with `transform` would look identical in a screenshot and
	occupy the old width, which is the defect this asserts the absence of.

	**And the wiring, which is `#640` for the sixth time.** The state, the storage and the
	callback all live in `App`, which `tests/dom.js` cannot execute and which will not dispatch
	an event by decision — so the decision (`collapsedColumns`), the storage (`collapsedChoices`,
	`rememberCollapsed`) and the rendering are all checked in `tests/test_web.py` at no cost
	here, and what is left is the half that has ever actually broken: that pressing the thing
	changes the page, and that the change survives a reload.

	**The reload is not a flourish.** Remembering is the whole reason this is `localStorage`
	rather than component state, and a version that renders correctly and forgets on every
	navigation would pass every other check in this file.
	"""

	opened, _written, *_ = running
	page = opened("/projects?view=board&include_completed=true")

	page.wait_for_selector("section.column.collapsed", timeout=10_000)

	def measured (target: typing.Any) -> typing.Any:
		"""Every column's label, width and whether it is folded."""

		return target.eval_on_selector_all("section.column", """found => found.map((one) => ({
			label: (one.querySelector("h2") || {}).textContent.trim(),
			width: one.getBoundingClientRect().width,
			height: one.getBoundingClientRect().height,
			shut: one.classList.contains("collapsed"),
		}))""")

	columns = measured(page)
	shut = [one for one in columns if one["shut"]]
	open_ones = [one for one in columns if not one["shut"]]

	assert shut and open_ones, f"nothing folded, or everything did: {columns}"

	assert all("Cancelled" in one["label"] for one in shut), (
		f"something other than work that is over started folded: {shut}"
	)

	# **The number is the claim.** A column merely styled narrower would still be 260px wide
	# and would pass a check that only asked whether the class was applied.
	assert max(one["width"] for one in shut) < 60, (
		f"a folded column is {shut} — that is not a character's width, so the heading did not "
		f"turn and the column gave nothing back"
	)
	assert min(one["width"] for one in open_ones) > 200, (
		f"folding one column shrank the others: {open_ones}"
	)

	assert not page.eval_on_selector_all("section.column.collapsed .rows li", "f => f.length"), (
		"a folded column rendered its rows, so it is hiding nothing and costing a click"
	)

	# **The name has to still be readable, and a box measurement cannot see that** — `#965`'s
	# lesson, met again. The width above is satisfied by `width: 38px` alone, so deleting
	# `writing-mode: vertical-rl` left every other assertion here green while the label spilled
	# out of a column that clips it: the reader would see `Can`. An element's box is not its
	# content, so the content is what this compares.
	spilling = page.eval_on_selector_all("section.column.collapsed h2 button", """f => f.map(
		(one) => ({ over: one.scrollWidth - one.clientWidth, text: one.textContent.trim() })
	)""")

	assert spilling, "nothing was folded, so this measured nothing"
	assert all(one["over"] <= 2 for one in spilling), (
		f"a folded column's name does not fit in it and is clipped: {spilling} — the heading "
		f"did not turn, so it is laid out along the axis the column has no room on"
	)

	# **A folded column must take height rather than demand it**, and the first version of this
	# demanded it. A turned heading is laid out along the vertical axis, so its intrinsic size
	# is the length of the words; with `height: 100%` inside an auto-height column the two
	# resolved against each other and *Cancelled* asked for 718px, making the board 209px
	# taller. Nothing here saw it — what failed was `test_dragging_a_card_to_another_column`,
	# because the drop target moved below the fold and Playwright scrolled mid-gesture and
	# lifted a different card. A guard on the width alone is blind to it, which is why this
	# asks about the other axis.
	assert max(one["width"] for one in shut) < min(one["height"] for one in shut) , (
		f"a folded column is not taller than it is wide: {shut}"
	)
	assert max(one["height"] for one in shut) <= max(one["height"] for one in open_ones), (
		f"a folded column is making the board taller than its contents need: {columns}"
	)

	page.click("section.column.collapsed h2 button")
	page.wait_for_selector("section.column:not(.collapsed) .rows li", timeout=10_000)

	# Read back rather than trusting the click, because the class is what was clicked on.
	assert not [one for one in measured(page) if one["shut"]], (
		"the column was pressed and stayed folded"
	)

	# **A second page in the same context**, which is a fresh render reading the same storage —
	# the one thing component state would pass every other assertion here without doing.
	again = opened("/projects?view=board&include_completed=true")

	again.wait_for_selector("section.column", timeout=10_000)

	assert not [one for one in measured(again) if one["shut"]], (
		"the reader opened a column and the next page folded it again, so nothing was remembered"
	)


#: How far apart two of the palette's colours must be, in OKLab, to count as distinguishable.
#:
#: **Measured rather than chosen.** The closest pair in the palette as it stands is 0.096 in
#: light and 0.116 in dark, so this refuses a *regression* — somebody adding a ninth hue into
#: the gap, or retuning one into its neighbour — rather than describing what is there. A
#: just-noticeable difference is around 0.02, so eight colours at 0.08 apart is comfortable and
#: is roughly where a categorical palette stops being able to grow.
FURTHEST_TWO_HUES_MAY_BE = 0.08


def _oklab (value: str) -> tuple[float, float, float]:
	"""Return a computed CSS colour in OKLab, where distance is roughly perceptual.

	**Not sRGB, where distance means nothing** — two colours the same Euclidean distance apart
	in sRGB can be obviously different or indistinguishable depending where they sit. OKLab is
	the cheapest space in which one number answers *can a reader tell these apart*.
	"""

	found = [float(part) for part in re.findall(r"[\d.]+", value)][:3]

	assert len(found) == 3, f"not a computed colour: {value!r}"

	def linear (part: float) -> float:
		"""Undo sRGB's transfer function for one channel."""

		scaled = part / 255

		return scaled / 12.92 if scaled <= 0.04045 else ((scaled + 0.055) / 1.055) ** 2.4

	red, green, blue = (linear(part) for part in found)

	long = (0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue) ** (1 / 3)
	medium = (0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue) ** (1 / 3)
	short = (0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue) ** (1 / 3)

	return (
		0.2104542553 * long + 0.7936177850 * medium - 0.0040720468 * short,
		1.9779984951 * long - 2.4285922050 * medium + 0.4505937099 * short,
		0.0259040371 * long + 0.7827717662 * medium - 0.8086757660 * short,
	)


def test_every_colour_a_project_may_wear_is_legible_and_unlike_the_other_seven (
	running: typing.Any,
) -> None:
	"""`SR#1027`, design `SR#1023`. Two properties, eight colours, both themes.

	**Legible**, because the hue is the project's *label* as well as its bar — `SR#102` as
	amended requires the colour to be redundant with a word, and a word nobody can read is not
	redundant with anything. 4.5:1 is `SR#902`'s floor and the same one every mark is held to.

	**And unlike each other**, which nothing else in this repository asks of anything. A palette
	whose every member clears the contrast floor can still put two projects in shades a reader
	does not separate — and that failure looks like the feature working, which is the worst kind
	this project keeps finding. Contrast is about a colour and its background; this is about a
	colour and its neighbours, and the two are independent.

	**Both themes, because a `light-dark()` pair is two values and only one is ever on screen.**
	`SR#906` made every token one declaration precisely so a value cannot exist in one theme and
	not the other — but it does nothing to stop the *dark* half of two hues colliding while the
	light halves are fine. Measured by pinning `data-theme`, which is the reader's own override
	(`SR#908`) and therefore the mechanism a person would actually use.
	"""

	opened, *_ = running

	page = opened("/")
	page.wait_for_selector(".app", timeout=10_000)

	names = [
		"amber", "green", "teal", "cyan", "indigo", "violet", "magenta", "slate",
	]

	for theme in ("light", "dark"):
		# The reader's own override rather than an emulated system preference: `light-dark()`
		# reads `color-scheme`, and this attribute is what pins it (`SR#908`).
		page.evaluate("theme => { document.documentElement.dataset.theme = theme }", theme)

		measured = page.evaluate(
			"""names => {
				const root = document.documentElement;
				const probe = document.createElement("span");

				document.body.appendChild(probe);

				const read = {};

				for (const name of names) {
					probe.style.color = `var(--hue-${name})`;
					read[name] = getComputedStyle(probe).color;
				}

				probe.remove();

				return {
					hues: read,
					page: getComputedStyle(document.body).backgroundColor,
					sunken: getComputedStyle(root).getPropertyValue("--bg-sunken").trim(),
				};
			}""",
			names,
		)

		hues = measured["hues"]

		# **The probe has to have resolved something**, or every assertion below compares eight
		# copies of the same inherited colour and passes by having nothing to disagree about.
		# An undefined custom property is not an error — the declaration is simply dropped
		# (`SR#923`), which is exactly how `SR#1021` shipped.
		assert len(set(hues.values())) == len(names), (
			f"the palette does not resolve to {len(names)} distinct colours in {theme}, so a "
			f"token is missing and the declaration was dropped: {hues}"
		)

		for name, value in hues.items():
			assert _contrast(value, measured["page"]) >= 4.5, (
				f"--hue-{name} is not legible on the page in {theme}: "
				f"{_contrast(value, measured['page']):.2f}:1 against a floor of 4.5. It is a "
				f"project's label as well as its bar, and a word nobody can read is not the "
				f"redundancy `SR#102` requires."
			)

		for first in range(len(names)):
			for second in range(first + 1, len(names)):
				one, other = names[first], names[second]
				apart = math.dist(_oklab(hues[one]), _oklab(hues[other]))

				assert apart >= FURTHEST_TWO_HUES_MAY_BE, (
					f"--hue-{one} and --hue-{other} are {apart:.4f} apart in {theme}, under "
					f"{FURTHEST_TWO_HUES_MAY_BE}. Two projects would wear shades a reader does "
					f"not separate, which looks exactly like the feature working."
				)

	# **And the wiring, which is the half a pure function cannot reach.** The rule being right
	# and the value being legible say nothing about whether a row is given the attribute the
	# rule keys on — `SR#640`'s shape, six times in this repository, and `tests/dom.js` drops
	# every attribute (`SR#784`) so it cannot see this either.
	listed = opened("/projects?view=list")
	listed.wait_for_selector(".rows li", timeout=10_000)

	drawn = listed.eval_on_selector_all(
		".rows li",
		"""all => all.map(one => ({
			colour: one.dataset.colour || null,
			shadow: getComputedStyle(one).boxShadow,
		}))""",
	)

	coloured = [one for one in drawn if one["colour"]]
	plain = [one for one in drawn if not one["colour"]]

	# The fixture has to carry both, or one of the two assertions below is about nothing.
	assert coloured and plain, (
		f"the listing does not hold both a coloured row and an uncoloured one, so this "
		f"compares nothing: {drawn}"
	)

	for one in coloured:
		assert one["shadow"] != "none", (
			f"a row whose project has a colour draws no accent bar: {one}. The attribute is "
			f"written and the cascade is not reaching it."
		)

	for one in plain:
		assert one["shadow"] == "none", (
			f"a row whose project has no colour is still drawing a bar: {one}. `no colour` is "
			f"a real answer, and it has to look different from a colour nobody can see."
		)
