"""The browser app — item `#597`.

Two things nothing else in this suite can check.

**Its licences**, because ``scripts/check_licences.py`` walks ``importlib.metadata`` and is
structurally blind to a JavaScript file copied into the package. `#445` recorded that as the
argument against a build step and an npm closure, and the same hole opens for three files as
for three hundred.

**That its templates render.** htm is a *tagged template literal*, so a malformed template
parses perfectly and throws when it is rendered — which is a blank page for the reader and a
green build for us. Syntax checking cannot see it; only rendering can.
"""

import ast
import datetime
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import textwrap
import typing
import urllib.parse
import uuid

import fastapi
import httpx
import pytest
import sqlalchemy
import sqlalchemy.orm
import starlette.requests

import api_support
import conftest
import subroutine.api.app
import subroutine.api.documents
import subroutine.api.pagination
import subroutine.api.routing
import subroutine.api.sessions
import subroutine.api.shaping
import subroutine.api.tasks
import subroutine.api.web
import subroutine.cli.personal
import subroutine.cli.topics
import subroutine.db.mixins
import subroutine.db.seed
import subroutine.domain.agenda
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.claims
import subroutine.domain.ordering
import subroutine.domain.tasks
import subroutine.views
import subroutine.web.vendored

ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSETS = subroutine.api.web.ASSETS

#: Where a copied file that is **only ever run by a test** lives.
#:
#: Not beside the served ones, and that is the point: ``api/web._collected`` *walks* the vendor
#: directory, so a file dropped in there is served to every reader — 9 KB of something the page
#: never imports, inside the set a reader is invited to audit.
TEST_VENDOR = pathlib.Path(__file__).resolve().parent / "vendor"

#: The smallest document Preact will render into, so that effects run and `App` can be driven
#: rather than only rendered — `SR#640`. Written here rather than copied from anywhere, which is
#: why it sits outside :data:`TEST_VENDOR`.
DOM = pathlib.Path(__file__).resolve().parent / "dom.js"

#: The same record `subroutine.web.vendored` keeps, for the half that is not shipped.
#:
#: `#445`'s hole is the same either way — ``scripts/check_licences.py`` walks
#: ``importlib.metadata`` and is structurally blind to a JavaScript file — so the licence is
#: recorded and checked here exactly as it is there, by the same tests.
TEST_ONLY: tuple[subroutine.web.vendored.Vendored, ...] = (
	subroutine.web.vendored.Vendored(
		filename="render-to-string.js",
		package="preact-render-to-string",
		version="6.7.0",
		licence="MIT",
		source="https://unpkg.com/preact-render-to-string@6.7.0/dist/index.mjs",
		notice="render-to-string.LICENSE",
		digest="sha256:619dd7d8058ec9f9d5dec610023f51b80493b28228c16c9e275d815e412675be",
	),
)

#: Sample props for every component that takes them, shaped like the API's real answers —
#: which were read off a live instance rather than invented, because a component fed a shape
#: nobody serves is a test of a shape nobody serves.
SAMPLES: dict[str, dict[str, typing.Any]] = {
	# **What `marks` decided, drawn** (`SR#970`) — its own component since a listing row and an
	# item's links both draw it, and two copies of twenty lines of markup is how the two would
	# come to look different while sharing the code that decided what they say.
	#
	# **Both branches in one sample.** A mark carrying an `href` is an anchor and everything else
	# is a span, and a sample that renders only spans would have absorbed the anchor half
	# silently — which is exactly the omission `Row`'s own workspace note below is about.
	#
	# **`onGo` is deliberately absent**, because `_rendered` supplies every `on…` prop the app
	# uses as a real no-op function. Naming it here would override that with whatever was
	# written, and a boolean renders the anchor branch perfectly while making it throw on the
	# one gesture it exists for.
	"Marks": {
		"badges": [
			{"text": "bug"},
			{"text": "Blocked", "tone": "blocked"},
			{"text": "subroutine/ui", "href": "/projects/subroutine/ui"},
		],
	},
	"Row": {
		"item": {
			"ref": 42,
			"kind": "task",
			"title": "Fix the pagination cursor",
			"due_at": "2020-01-01T00:00:00Z",
			"blocked": True,
			"project_key": "sr",
			"assignee": "si",
			"status": "in_progress",
			"status_is_default": False,
		},
		"showKind": True,
		# **The workspace is here so the default sample renders a row as it actually ships**
		# (`SR#722`). Without it `Row` has no address to link to and falls back to a button —
		# which is a legitimate branch, and it silently absorbed the change from button to
		# anchor: every one of these tests went on passing while the path a reader takes was
		# covered by nothing. A sample that omits an input tests the fallback, and reads
		# exactly like a test of the thing.
		"workspace": "personal",
	},
	"Listing": {
		"items": [
			{"ref": 1, "kind": "task", "title": "A task", "status_is_default": True},
			{"ref": 2, "kind": "document", "title": "A document", "status_is_default": True},
		]
	},
	# The bar the list and the board share — one component since `SR#986`, because they held it
	# byte for byte and a second control in it would have been the moment they drifted.
	"Narrowed": {"project": "web", "prioritised": ["web"]},
	"Board": {
		"items": [
			{
				"ref": 1, "kind": "task", "title": "Not started",
				"status_category": "todo", "status_is_default": True,
			},
			{
				"ref": 2, "kind": "task", "title": "Underway",
				"status_category": "in_progress", "status": "in_progress",
				"status_is_default": False,
			},
			{
				"ref": 3, "kind": "document", "title": "A decision",
				"status_category": "current", "status_is_default": True,
			},
		]
	},
	"Agenda": {
		"buckets": [
			{
				"key": "overdue",
				"label": "Overdue",
				"items": [
					{
						"ref": 1, "kind": "task", "title": "Late already",
						"workspace": "projects", "due_at": "2020-01-01T00:00:00Z",
						"status_is_default": True,
					}
				],
			},
			{
				"key": "unscheduled",
				"label": "Unscheduled",
				"items": [
					{
						"ref": 2, "kind": "task", "title": "Someday",
						"workspace": "personal", "status_is_default": True,
					}
				],
			},
		],
		"more": 12,
		"where": "projects",
	},
	# **Found by the completeness guard on its first run** (`SR#652`): this is the app's whole
	# trust boundary — the only `dangerouslySetInnerHTML` there is — and its template had never
	# been rendered by the harness at all. What `markdown.render` emits is covered exhaustively;
	# what wraps it was not.
	"Prose": {
		"text": "A description with a **word** in it, and a mention of #42.",
		"where": "/projects",
	},
	"Facts": {
		"item": {
			"ref": 42,
			"title": "Fix the pagination cursor",
			"status": "open",
			"project_key": "sr",
			"importance": 4,
			"urgency": 3,
			"tags": ["api", "pagination"],
			"parent_ref": 7,
			"parent_title": "The release",
			"updated_at": "2026-08-08T10:00:00Z",
		}
	},
	"Detail": {
		"item": {"ref": 42, "title": "A task", "description": "Why it matters."},
		"links": [
			{
				"id": "one",
				"label": "Blocks",
				"other": {"ref": 43, "title": "The next one", "entity_type": "task"},
			}
		],
		"comments": [{"id": "c1", "created_at": "2026-08-08T10:00:00Z", "body": "Reproduced."}],
		# Same reason as `Row`'s: without it the linked items have no address and render as
		# buttons, so the default sample would go on testing the fallback (`SR#722`).
		"workspace": "personal",
		"backTo": "/personal",
	},
	"Failed": {"error": {"status": 500, "message": "Something went wrong."}},
	"Adding": {"busy": False},
	# `#776`: a prose box with a way to see it as it will read. Rendered writing rather than
	# previewing, because that is the state every one of them is in most of the time.
	"Written": {"name": "description", "label": "Description", "rows": "3", "value": "Some prose."},
	# **The shared field block** (`SR#757`), rendered with everything it can draw so that a
	# malformed template in any one control fails here rather than in front of a reader.
	"Fields": {
		"busy": False,
		"values": {"description": "why", "importance": "4", "due": "2026-09-01", "tags": "a, b"},
		"projects": [{"key": "inbox", "title": "Inbox", "is_inbox": True, "depth": 0}],
		"members": [{"username": "si", "label": "si"}],
		"vocabulary": {
			"item_types": {"task": [{"key": "task", "label": "Task", "is_default": True}]},
			"statuses": {"task": [{"key": "open", "label": "Open", "is_default": True}]},
		},
	},
	"Editing": {
		"busy": False,
		"item": {
			"ref": 42, "title": "A task", "version": 3, "timezone": "Etc/UTC",
			"description": "why", "status": "open", "type": "task", "tags": ["a"],
			"due_at": "2026-09-01T23:59:59.999999Z",
		},
		"members": [{"username": "si", "label": "si"}],
		"projects": [{"key": "inbox", "title": "Inbox", "is_inbox": True, "depth": 0}],
		"vocabulary": {
			"item_types": {"task": [{"key": "task", "label": "Task", "is_default": True}]},
			"statuses": {"task": [{"key": "open", "label": "Open", "is_default": True}]},
		},
	},
	"Conflict": {"theirs": {"ref": 42, "title": "What it says now"}},
	# **Decision `SR#1249` §6's question, rendered with what a real gesture puts in it.**
	# `what` is the phrase the sentence is built around and it differs per gesture — a save
	# says *this change*, handing an item over says *giving it to jo* — so a sample with an
	# empty one would render a sentence nobody meets.
	"Asking": {"what": "this change", "busy": False},
	# **Filled rather than empty**, because the disclosure's own rule is that it opens when the
	# item already repeats — a sample with no rule in it would render the closed case and say
	# nothing about the one a reader editing a repeat actually meets (`SR#94`).
	"Repeats": {
		"busy": False,
		"held": {"recurrence": "every other tuesday", "recurrence_anchor": "completion"},
		"reading": {
			"description": "every other week, on Tuesday",
			"occurrences": ["2026-08-18T09:00:00Z", "2026-09-01T09:00:00Z"],
		},
	},
	# The refusal, not the answer: `Repeats` above renders the readable case, so this covers the
	# branch a reader stuck on wording actually sees.
	"Reading": {"reading": {"problem": "That is not a repeat this understands."}},
	"DocumentFields": {
		"busy": False,
		"values": {"body": "Prose.", "type": "decision", "status": "active"},
		"projects": [{"key": "inbox", "title": "Inbox", "is_inbox": True, "depth": 0}],
		"vocabulary": {
			"item_types": {"document": [{"key": "note", "label": "Note", "is_default": True}]},
			"statuses": {"document": [{"key": "draft", "label": "Draft", "is_default": True}]},
		},
	},
	"Saying": {"busy": False},
	"Seeking": {"busy": False, "asked": ""},
	"Linking": {"busy": False, "types": [
		{"key": "blocks", "title": "Blocks"},
		{"key": "relates_to", "title": "Relates to"},
	]},
	"Doing": {
		"item": {
			"ref": 42,
			"kind": "task",
			"title": "Fix the pagination cursor",
			"status_category": "todo",
			"assignee": "si",
		},
		"members": ["si", "ada"],
		"busy": False,
	},
	"Note": {
		"note": {
			"text": "Completed #42 Fix the pagination cursor.",
			"tone": "good",
			"undo": {"ref": 42, "kind": "task", "title": "Fix it", "status": "open"},
		}
	},
	"Foot": {"count": 7, "theme": "system"},
	"Wordmark": {"version": "0.6.7"},
	"Theme": {"chosen": "dark"},
	"Icon": {"name": "bug"},
}

#: The one case with its own branch, and the one a reader is likeliest to meet: a page loaded
#: by somebody who has never signed in.
UNAUTHENTICATED = {"error": {"status": 401, "message": "This endpoint needs a credential."}}


#: A module specifier as it appears in an ``import``. Anchored on the keyword and refusing a
#: value containing whitespace, which is what keeps prose out: a comment in `app.js` reads
#: ``"sign in" from "something went wrong"``, and a bare `from "…"` scan reported that as an
#: import. A specifier with a space in it is not one.
_SPECIFIER = re.compile(r"""\b(?:from|import)\s*["']([^"'\s]+)["']""")


def _import_map () -> dict[str, str]:
	"""Return what the page tells a browser each bare specifier resolves to."""

	page = (ASSETS / "index.html").read_text(encoding="utf-8")
	declared = page.split('<script type="importmap">')[1].split("</script>")[0]

	return dict(json.loads(declared)["imports"])


def _bare_imports (text: str) -> set[str]:
	"""Return the bare specifiers this module asks for.

	Bare meaning "not a path": anything starting with ``.``, ``/`` or a scheme resolves by
	itself, and everything else needs the import map to say what it means.
	"""

	return {
		found
		for found in _SPECIFIER.findall(text)
		if not found.startswith((".", "/", "http:", "https:", "file:", "data:"))
	}


def _served_modules () -> dict[str, str]:
	"""Return every JavaScript module this instance serves, by name."""

	return {
		name: body.decode("utf-8")
		for name, (body, kind) in subroutine.api.web.FILES.items()
		if name.endswith(".js")
	}


def _node () -> str:
	"""Return a JavaScript runtime, or skip — unless CI has said a skip is not acceptable.

	**Skipped rather than faked.** A stub that pretended to render would be a test that cannot
	fail, which is the shape this project has been bitten by most; a skip at least says out
	loud that nothing was checked here.

	**And a skip CI can refuse**, exactly as PostgreSQL and a browser are (`#927`'s H-17).
	Until this existed there was no way to say *a run without Node is not a run*: the release
	workflow installed none, so 198 of this file's tests skipped in silence on every release
	this project has ever cut, and the job reported success.
	"""

	found = shutil.which("node")

	if found is None:
		reason = "no JavaScript runtime on PATH, so the app cannot be rendered"

		if conftest.required("SUBROUTINE_TEST_REQUIRE_NODE"):
			pytest.fail(
				f"{reason}\n\nSUBROUTINE_TEST_REQUIRE_NODE is set, so a missing runtime "
				f"fails the run rather than quietly leaving the browser app unchecked."
			)

		pytest.skip(reason)

	return found


def _staged (tmp_path: pathlib.Path) -> pathlib.Path:
	"""Lay the served app out so Node can import it, and return its entry module.

	The bare specifiers are rewritten to file paths, which is exactly what the import map in
	``index.html`` does for a browser — so what runs is the file that is served, with its
	imports resolved the same way rather than transformed.
	"""

	vendor = subroutine.web.vendored.DIRECTORY

	#: **Flat, because that is how they are served** (`#764`). These used to be staged in a
	#: subdirectory, which worked for as long as every vendored file was reached by a *bare*
	#: specifier — the import map rewrote those to wherever they were put, so the layout never
	#: had to match. `phosphor.js` is imported relatively, like `markdown.js`, and a relative
	#: import resolves against the importing file: `api/web._collected` flattens both directories
	#: into one map, so beside `app.js` is where a browser finds it.
	staged = tmp_path

	# **Resolution comes from the page's own import map**, longest specifier first so that
	# `preact/hooks` is rewritten before `preact` can match its prefix.
	#
	# The first version of this carried its own list of three, and that is why it passed while
	# the served page was blank: `htm` was missing from the map, the harness supplied it
	# anyway, and the test proved the components render while saying nothing about whether a
	# browser could load them. A harness that substitutes the mechanism under test can only
	# ever confirm the half that was not broken.
	rewrites = {
		specifier: (staged / pathlib.Path(target).name).as_uri()
		for specifier, target in sorted(
			_import_map().items(), key=lambda pair: -len(pair[0])
		)
	}

	def resolved (text: str) -> str:
		"""Point every bare specifier at the staged copy the import map names.

		**Both spellings, because one of the files is minified**: `preact-hooks.js` contains
		``from"preact"`` with no space, which is the import the first version of this missed
		entirely — it rewrote the app and left the vendored file asking for a package name.
		"""

		for specifier, target in rewrites.items():
			text = text.replace(f'from "{specifier}"', f'from "{target}"')
			text = text.replace(f'from"{specifier}"', f'from"{target}"')

		return text

	for entry in subroutine.web.vendored.CATALOGUE:
		(staged / entry.filename).write_text(
			resolved((vendor / entry.filename).read_text(encoding="utf-8")), encoding="utf-8"
		)

	# **Every module we wrote, under the name it is served as**, rather than `app.js` alone
	# renamed to `.mjs`. The app imports `./markdown.js` relatively — which is how a relative
	# specifier avoids the import map entirely, and so avoids the class of fault that shipped
	# a blank page — and a relative import can only resolve if the file it names is beside the
	# one asking for it. Derived from what is served, so a third module needs nothing here.
	(tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")

	vendored = {entry.filename for entry in subroutine.web.vendored.CATALOGUE}

	for name, body in _served_modules().items():
		if name not in vendored:
			(tmp_path / name).write_text(resolved(body), encoding="utf-8")

	# **Staged beside the served files and served from nowhere** — the renderer that lets `App`
	# be called at all is a test's tool, not part of the app. `resolved` applies to it because
	# it imports `preact` under the same bare specifier the page does.
	for entry in TEST_ONLY:
		(tmp_path / entry.filename).write_text(
			resolved((TEST_VENDOR / entry.filename).read_text(encoding="utf-8")),
			encoding="utf-8",
		)

	# **Ours rather than vendored, and beside them for the same reason** (`SR#640`): it is a
	# test's tool and the page must never see it. `api/web._collected` walks the *served* vendor
	# directory, so anything dropped in there reaches every reader.
	(tmp_path / DOM.name).write_text(DOM.read_text(encoding="utf-8"), encoding="utf-8")

	return tmp_path / "app.js"


def _ran (tmp_path: pathlib.Path, body: str) -> typing.Any:
	"""Run one script against the staged app and return the JSON it writes."""

	harness = tmp_path / "harness.js"
	harness.write_text(textwrap.dedent(body), encoding="utf-8")

	done = subprocess.run(
		[_node(), str(harness)], capture_output=True, text=True, timeout=120, check=False
	)

	assert done.returncode == 0, f"the app threw:\n{done.stderr}"

	return json.loads(done.stdout)


def _rendered (
	tmp_path: pathlib.Path, components: typing.Mapping[str, typing.Any]
) -> dict[str, str]:
	"""Render each named component with its props, in Node, and return the HTML."""

	module = _staged(tmp_path)

	# Rendering to a string needs no DOM: a Preact vnode is a plain object, so walking it is
	# enough to prove every template produced one rather than throwing.
	#
	# **The cost, stated rather than discovered later: this renders hook-free components only.**
	# A component is called as a plain function, so `useState` and the rest throw for want of a
	# renderer — which is why `App`, where every write lives, is absent from `SAMPLES` and is
	# not covered here at all. `SR#640` is that gap. Everything below it is deliberately written
	# without hooks so it *can* be checked, which is a better shape anyway.
	#
	# `dangerouslySetInnerHTML` is read here rather than skipped, because a component whose
	# whole output is that property would otherwise flatten to nothing and every assertion
	# about what it says would pass vacuously.
	# Every `onSomething=` the app passes to a component. Read off the source so a new one is
	# supplied the moment it is written, rather than the next time a test notices.
	handlers = sorted(set(re.findall(r"\b(on[A-Z][A-Za-z]*)=", _served_modules()["app.js"])))

	return dict(_ran(tmp_path, f"""
		import * as app from "{module.as_uri()}";

		const asked = {json.dumps(components)};
		const out = {{}};

		function flatten (node) {{
			if (node === null || node === undefined || node === false) return "";
			if (Array.isArray(node)) return node.map(flatten).join("");
			if (typeof node !== "object") return String(node);

			const raw = node.props && node.props.dangerouslySetInnerHTML;
			if (raw) return raw.__html;

			const type = typeof node.type === "function" ? node.type : null;
			const inner = type ? flatten(type(node.props)) : flatten(node.props.children);

			/* **`href` is the one attribute carried through** (`SR#722`). A row is a link now,
			   and a flatten that drops every attribute cannot tell a link from a span — which is
			   the whole of the difference that change makes. Nothing else is carried: this is a
			   text harness, and a test that wants to assert on layout wants a browser. */
			const address = node.props && node.props.href;
			const said = address ? ' href="' + address + '"' : "";

			/* **A textarea's value is its content**, which is what HTML says and what makes this
			   truthful rather than an exception (`SR#1044`). Every prose box in this app is
			   *uncontrolled* by `SR#757`'s decision — filled by `defaultValue` so a re-render
			   cannot reach in and reset somebody's typing — so an attribute-dropping harness
			   could not see what any of them held. `SR#1044` is what that cost: a form offering
			   an empty box where a document's body should be, with nothing here able to ask.
			   Textareas only, deliberately: an `<input>`'s value as *markup* would be an
			   invention, and this stays a text harness. */
			const held = node.type === "textarea" && node.props
				? String(node.props.defaultValue ?? node.props.value ?? "")
				: "";

			return typeof node.type === "string"
				? `<${{node.type}}${{said}}>${{held}}${{inner}}`
				: inner;
		}}

		/* Every handler a component may be given, **derived from the app rather than listed**.
		   A hand-kept list went stale three times in one day, and each time the symptom was a
		   control missing from the render — which is indistinguishable from a reader who is not
		   allowed to use it, and that distinction is what several tests here turn on. */
		const handlers = {{}};
		for (const name of {json.dumps(handlers)}) handlers[name] = () => {{}};

		for (const [name, props] of Object.entries(asked)) {{
			out[name] = flatten(app[name]({{ ...handlers, ...props }}));
		}}

		process.stdout.write(JSON.stringify(out));
	"""))


def _markdown (tmp_path: pathlib.Path, sources: typing.Sequence[str]) -> list[str]:
	"""Render each piece of Markdown with the app's own renderer, and return the HTML.

	Driven directly rather than through a component, because the renderer is a pure function
	from text to a string — which is the whole reason it was written that way. A payload can
	be fed in and the exact bytes a browser would be handed can be asserted on, with no DOM
	and nothing standing between the test and the thing being tested.
	"""

	module = _staged(tmp_path)

	return list(_ran(tmp_path, f"""
		import * as markdown from "{(module.parent / "markdown.js").as_uri()}";

		const sources = {json.dumps(list(sources))};

		process.stdout.write(JSON.stringify(sources.map((source) => markdown.render(source))));
	"""))


def test_every_component_renders (tmp_path: pathlib.Path) -> None:
	"""The check that syntax cannot make: a template that is wrong throws when rendered."""

	rendered = _rendered(tmp_path, SAMPLES)

	assert set(rendered) == set(SAMPLES)

	for name, markup in rendered.items():
		assert markup, f"{name} rendered nothing at all"


def test_a_row_says_in_words_what_it_says_in_colour (tmp_path: pathlib.Path) -> None:
	"""Decision `#102`, carried from the terminal to the browser.

	Colour marks an exception and never carries the information by itself. Overdue is red
	*and* reads "Overdue"; blocked is marked *and* reads "Blocked" — so a reader who cannot
	separate the hues loses nothing. Asserted on the rendered text rather than on the class
	names, because the class is the colour and the text is the claim.
	"""

	markup = _rendered(tmp_path, {"Row": SAMPLES["Row"]})["Row"]

	assert "Blocked" in markup
	assert "Overdue" in markup
	assert "#42" in markup


def _from_now (*, hours: float) -> str:
	"""An instant that many hours from this moment, as the API writes one.

	**For any fixture a component compares against the wall clock** (`SR#737`). `marks` asks
	`holding` without a moment, so it reads `Date.now()` — and a fixture written as a fixed
	time of day is live until that hour and dead afterwards, which is a test that passes in the
	morning and fails in the evening with nothing changed.
	"""

	return (datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=hours)).isoformat()


def _holding (
	tmp_path: pathlib.Path, cases: typing.Sequence[tuple[dict[str, typing.Any], int]]
) -> list[typing.Any]:
	"""Ask the app who is holding each row, at the moment given with it."""

	module = _staged(tmp_path)

	return list(_ran(tmp_path, f"""
		import * as app from "{module.as_uri()}";

		process.stdout.write(JSON.stringify(
			{json.dumps([list(case) for case in cases])}.map(([item, now]) =>
				app.holding(item, now))
		));
	"""))


def test_a_row_says_who_is_holding_it (tmp_path: pathlib.Path) -> None:
	"""`SR#726`. Simon: *"I cannot see what is being worked on."*

	**And not by inferring a status from it**, which was the first answer and was wrong: a claim
	is taken *before* the work — Simon, *"the agent might be considering whether to start on the
	task, and decide not to"* — and release has four possible destinations and cannot tell them
	apart. So the lease is shown as what it is, and `in_progress` stays a declaration.

	**An expired lease is no longer drawn, and that reverses `SR#726`** (`SR#1019`, Simon). It
	used to have its own mark on the argument that *started and walked away from* is what a
	person watching agents work most wants to see. What outweighed it: a chip reads as a
	property of the item and *left it* is an event, whose home is the item's history.

	**`holding` is unchanged and still reports it**, which is the half worth keeping — the
	distinction is live, `views.Task` publishes the expiry so a client can make it, and this
	still drives both readings. Only the *drawing* stopped.
	"""

	# **Relative to the moment this runs, not a date somebody typed** (`SR#737`). The first
	# version put the expiry at 18:00 on the day it was written; the rendered assertions below
	# go through `marks`, which reads the real clock, so they passed until six o'clock that
	# evening and failed after it. A green gate says nothing about a test that turns over.
	#
	# `SAMPLES["Row"]` is the contrast: its `due_at` is 2020-01-01, which `overdue` answers the
	# same way for ever. Far past, far future, or computed — never same-day.
	held = {"ref": 1, "kind": "task", "title": "Being done", "claimed_by_id": "u1",
		"claimed_by": "agent", "claim_expires_at": _from_now(hours=1)}
	stale = {**held, "claim_expires_at": _from_now(hours=-1)}
	free = {"ref": 2, "kind": "task", "title": "Nobody on it"}

	# The pure function is asked at an explicit moment, which is the half that can be exact:
	# a fixed lease read at a fixed instant has one right answer whenever it is run.
	fixed = {**held, "claim_expires_at": "2026-08-09T18:00:00+00:00"}
	expired = {**held, "claim_expires_at": "2026-08-09T09:00:00+00:00"}
	noon = int(datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC).timestamp() * 1000)

	assert _holding(tmp_path, [(fixed, noon), (expired, noon), (free, noon)]) == [
		{"held": True, "who": "agent"},
		{"held": False, "who": "agent"},
		None,
	]

	rendered = _rendered(tmp_path, {
		"Row": {"item": held, "workspace": "projects"},
		"Listing": {"items": [stale]},
	})

	assert "claimed by @agent" in rendered["Row"], (
		f"a row does not say who is holding it: {rendered['Row']}"
	)

	# **The other half, and without it the wording above is the only thing asserted.** An
	# expired lease must draw nothing at all — not the live wording, and not a mark of its own.
	assert "agent" not in rendered["Listing"], (
		f"an expired claim was still drawn, which `SR#1019` removed: {rendered['Listing']}"
	)


def test_a_lease_with_no_name_still_says_somebody_holds_it (tmp_path: pathlib.Path) -> None:
	"""An instance older than `claimed_by` defaults it to null, and the fact still matters.

	`SR#345`'s direction: a client one commit ahead of an instance must read what it sends. The
	half that survives is the one worth having — *somebody has this* — and reporting nothing
	would make an older instance look idle rather than unreadable.
	"""

	nameless = {"ref": 3, "kind": "task", "title": "Held", "claimed_by_id": "u1",
		"claim_expires_at": _from_now(hours=1)}

	rendered = _rendered(tmp_path, {"Row": {"item": nameless, "workspace": "projects"}})["Row"]

	assert "Claimed" in rendered, f"a lease with no name reported nobody: {rendered}"


class _Leased:
	"""The two attributes `claims.held_by` actually reads, and nothing else.

	A real task through a real claim would be a database, a workspace and a principal to compare
	one arithmetic expression — and it would compare the instance against a *fixture's* idea of
	an expiry rather than against the moment itself. What matters here is the boundary.
	"""

	def __init__ (self, expires: datetime.datetime) -> None:
		"""Stand in for a claimed row, holding only what the renderer reads."""

		self.claimed_by_id = uuid.uuid4()
		self.claim_expires_at = expires


def test_the_browser_and_the_instance_read_a_lease_the_same_way (
	tmp_path: pathlib.Path,
) -> None:
	"""**Two copies of one comparison, so a test holds them together** (`SR#726`).

	`domain.claims.held_by` answers this server-side and the browser answers it again, because
	`claim_expires_at` is published precisely so a client need not ask per row. Same
	justification as the finished-category set, and the same obligation: measure that they
	agree rather than assume it.

	**The exact boundary is the case worth having.** `held_by` treats `expires <= now` as gone,
	so a lease is dead at the instant it expires rather than a millisecond after — and `<` for
	`<=` is the likeliest way to write this wrong on either side, invisible to any test that
	only ever asks about an hour before and an hour after.
	"""

	expires = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)
	task = _Leased(expires)
	stamp = int(expires.timestamp() * 1000)

	moments = [stamp - 60_000, stamp, stamp + 60_000]

	instance = [
		subroutine.domain.claims.held_by(
			typing.cast(typing.Any, task),
			now=datetime.datetime.fromtimestamp(at / 1000, tz=datetime.UTC),
		)
		is not None
		for at in moments
	]

	row = {
		"claimed_by_id": str(task.claimed_by_id),
		"claim_expires_at": expires.isoformat(),
		"claimed_by": "agent",
	}
	browser = [
		answer is not None and answer["held"]
		for answer in _holding(tmp_path, [(row, at) for at in moments])
	]

	assert instance == [True, False, False], f"the instance's own reading changed: {instance}"

	assert browser == instance, (
		f"a minute before expiry, at it, and a minute after, the browser reads {browser} where "
		f"the instance reads {instance}"
	)


def test_a_row_is_a_link_to_the_item_it_names (tmp_path: pathlib.Path) -> None:
	"""`#722`. Simon: *"I CTRL-click an item and it loads in the same tab, replacing the list."*

	A button has no address, so there is nothing for a modified click to do differently — and
	with it go *open in a new tab*, *copy link address*, middle-click, the hover target and the
	link's existence to a screen reader. `#638` gave every item a durable address precisely so
	that this would be possible.

	**The address is the item's own**, so a row from another workspace links there rather than to
	whichever workspace the switcher happens to hold — the same precedence `App` already applies
	to opening and completing an agenda row, and the fault `#650` was.
	"""

	rendered = _rendered(tmp_path, {
		"Row": {"item": {"ref": 42, "kind": "task", "title": "Here",
			"project_key": "sr"}, "workspace": "personal"},
	})["Row"]

	assert 'href="/personal/sr/42"' in rendered, (
		f"a row is not a link to its own item: {rendered}"
	)

	elsewhere = _rendered(tmp_path, {
		"Row": {"item": {"ref": 1, "kind": "task", "title": "Elsewhere",
			"workspace": "sandbox"}, "workspace": "projects", "showWhere": True},
	})["Row"]

	assert 'href="/sandbox/1"' in elsewhere, (
		f"an agenda row linked to the wrong workspace: {elsewhere}"
	)


def test_a_linked_item_is_a_link_to_it (tmp_path: pathlib.Path) -> None:
	"""`SR#722`. The other end of a link is what a reader on this page most wants in a tab.

	The address is the durable `{workspace}/{ref}` form, and it has to be: the far end of a link
	is reported as a ref and a type with no project key, so the readable form is not available
	here. `SR#638` guarantees the durable one resolves.
	"""

	rendered = _rendered(tmp_path, {"Detail": SAMPLES["Detail"]})["Detail"]

	assert 'href="/personal/43"' in rendered, (
		f"a linked item is not a link to it: {rendered}"
	)


def test_a_row_with_no_address_stays_a_button (tmp_path: pathlib.Path) -> None:
	"""The branch the sample above used to take by accident, now taken on purpose.

	`agendaBuckets` leaves `workspace` null for a row whose workspace nobody can name, and there
	is then no address to link to. An `<a>` with no `href` is not a link and cannot be tabbed to,
	which is worse than the button it replaced — so the button stays for that case.
	"""

	rendered = _rendered(tmp_path, {
		"Row": {"item": {"ref": 7, "kind": "task", "title": "Nowhere"}, "workspace": None},
	})["Row"]

	assert "<button" in rendered, f"a row with no address rendered something unfocusable: {rendered}"
	assert "href" not in rendered, f"a row with no address claimed to have one: {rendered}"


def test_every_navigation_hands_a_modified_click_back_to_the_browser (
	tmp_path: pathlib.Path,
) -> None:
	"""`#722`. The rule `Prose` had and nothing else did, now shared and asked of all eight cases.

	Ctrl and cmd open a tab, shift opens a window, alt downloads, and any button but the primary
	one is the browser's — middle-click is *open in a tab* everywhere. A keyboard activation
	reports no button at all, and refusing there would break the path this exists to serve.

	**The decision is a pure function because `tests/dom.js` cannot dispatch an event**, by its
	own scope test. Lifting it out is what makes it checkable at all, which is `#640`'s cheapest
	route for the sixth time.
	"""

	module = _staged(tmp_path)
	cases = [
		{}, {"button": 0}, {"button": None},
		{"ctrlKey": True}, {"metaKey": True}, {"shiftKey": True}, {"altKey": True},
		{"button": 1}, {"button": 2},
	]

	answered = list(_ran(tmp_path, f"""
		import * as app from "{module.as_uri()}";

		process.stdout.write(JSON.stringify(
			{json.dumps(cases)}.map((event) => app.opens(event))
		));
	"""))

	assert answered[:3] == [True, True, True], (
		"a plain left click, and a keyboard activation reporting no button, must be handled here"
	)

	assert answered[3:] == [False] * 6, (
		f"a modified or non-primary click was swallowed rather than left to the browser: "
		f"{list(zip(cases[3:], answered[3:], strict=True))}"
	)


def test_a_finished_row_says_when_it_finished_rather_than_when_it_was_due (
	tmp_path: pathlib.Path,
) -> None:
	"""`#706`, and `#661`'s complaint answered for one view.

	`overdue` deliberately returns false for anything done, so a task finished a week late used
	to read `due 3 Aug` with nothing to say it had been dealt with — a fact about a date that
	stopped mattering, printed in the one cell there is.

	It is also the field the done view is **ordered** on. Simon's words on `#661`: *if the view
	does not show the values of the fields on which it is ordered, it is unclear.* A column of
	finish dates descending is a page a reader can check; the same rows showing deadlines are an
	order they have to take on trust.

	**A cancelled row says `cancelled`**, because it carries a `completed_at` too and calling it
	*done* would be the one word that misdescribes it.
	"""

	rendered = _rendered(tmp_path, {
		"Row": {"item": {"ref": 42, "kind": "task", "title": "Late but finished",
			"due_at": "2026-08-03T09:00:00+00:00", "status_category": "done",
			"completed_at": "2026-08-09T14:00:00+00:00"}, "showKind": False},
		"Listing": {"items": [{"ref": 43, "kind": "task", "title": "Abandoned",
			"status_category": "cancelled",
			"completed_at": "2026-08-07T14:00:00+00:00"}]},
	})

	assert "done" in rendered["Row"], (
		f"a finished row did not say when it finished: {rendered['Row']}"
	)

	assert "due" not in rendered["Row"], (
		f"a finished row still showed the deadline it no longer has: {rendered['Row']}"
	)

	assert "cancelled" in rendered["Listing"], (
		f"a cancelled row was described as done: {rendered['Listing']}"
	)


def test_a_deadline_with_an_o_clock_says_it_on_a_row_as_it_does_on_the_fact_sheet (
	tmp_path: pathlib.Path,
) -> None:
	"""`#1298`. The two renderings of one deadline disagreed about whether it had a time.

	`#864` gave the *start* its flag when `#797` taught the capture grammar `at 14:00`, and left
	the deadline behind — so the item page's `Facts` said *2 Dec 2026, 17:00* while the row two
	clicks away said *due 2 Dec 2026*. `when` and `marks` both read `day` with the flag omitted,
	which defaults to a whole day, so the omission read as a deliberate choice.

	**The comment above them said so in terms**: *"a deadline and a planned day stay days: a
	time on one would be precision the writer never supplied"*. True when it was written, false
	from the day the grammar could supply it, and the line below it had already been changed.

	Both a timed one and a whole-day one, because appending unconditionally is the same defect
	pointed the other way: an all-day deadline is stored at the **last** microsecond of its day,
	so guessing from the clock would print `23:59` against every ordinary one.
	"""

	timed = {"ref": 42, "kind": "task", "title": "Send the invoice",
		"due_at": "2026-12-02T17:00:00+00:00", "due_is_all_day": False,
		"timezone": "Europe/London"}
	whole = {"ref": 43, "kind": "task", "title": "Renew the licence",
		"due_at": "2026-12-02T23:59:59.999999+00:00", "due_is_all_day": True,
		"timezone": "Europe/London"}

	rendered = _rendered(tmp_path, {
		"Row": {"item": timed, "showKind": False},
		"Listing": {"items": [whole]},
	})

	assert "17:00" in rendered["Row"], (
		f"a row dropped the o'clock the fact sheet shows: {rendered['Row']}"
	)
	assert "23:59" not in rendered["Listing"], (
		f"a whole-day deadline was given the o'clock it is stored at: {rendered['Listing']}"
	)


def test_a_reader_who_is_not_signed_in_is_told_what_to_ask_for (
	tmp_path: pathlib.Path,
) -> None:
	"""A 401 here is the ordinary case, not a fault.

	Nothing on the page can hand out a session — a link is minted at a terminal until `#599`
	mails one — so it must say what to ask for rather than offer a form that cannot work. An
	empty page, or a stack trace, is how somebody decides the product is broken.
	"""

	markup = _rendered(tmp_path, {"Failed": UNAUTHENTICATED})["Failed"]

	assert "not signed in" in markup
	assert "subroutine login link" in markup


def test_the_kind_is_shown_only_when_the_page_holds_more_than_one (
	tmp_path: pathlib.Path,
) -> None:
	"""§12.2a: a column that says the same thing on every row says nothing.

	A blank beside "Document" reads as missing data rather than as "ordinary", and the word
	"Task" on every line of a list of tasks is noise. The rule is the CLI's and this is it
	holding on the other surface.
	"""

	mixed = _rendered(tmp_path, {"Listing": SAMPLES["Listing"]})["Listing"]

	assert "Document" in mixed and "Task" in mixed

	tasks_only = {
		"Listing": {
			"items": [row for row in SAMPLES["Listing"]["items"] if row["kind"] == "task"]
		}
	}
	plain = _rendered(tmp_path, tasks_only)["Listing"]

	assert "Task" not in plain, "the kind is on every row of a page that has only one kind"


def test_the_render_harness_would_notice_a_broken_template (
	tmp_path: pathlib.Path,
) -> None:
	"""Falsified through its own entry point, with the failure this exists to catch.

	A component that throws must fail the harness rather than being reported as empty — the
	difference between "rendered nothing" and "could not render" is the whole value here, and
	a harness that swallowed the exception would pass every test above forever.
	"""

	broken = tmp_path / "broken"
	broken.mkdir()

	with pytest.raises(AssertionError, match="the app threw"):
		_rendered(broken, {"Facts": {"item": None}})


def test_every_vendored_file_is_recorded_with_its_licence () -> None:
	"""A copied file the licence gate cannot see is one nothing checks at all.

	`scripts/check_licences.py` walks `importlib.metadata`, so it knows about Python
	distributions and nothing else. This is the other half, and it is a two-directional check
	for `#405`'s reason: an unrecorded file fails, and a record naming a file that has gone
	fails too.
	"""

	for directory, catalogue in (
		(subroutine.web.vendored.DIRECTORY, subroutine.web.vendored.CATALOGUE),
		(TEST_VENDOR, TEST_ONLY),
	):
		on_disk = {path.name for path in directory.iterdir() if path.suffix == ".js"}
		recorded = {entry.filename for entry in catalogue}

		assert on_disk, f"{directory.name} is empty, so this test is measuring nothing"
		assert on_disk == recorded, (
			f"{directory} and its catalogue disagree: only on disk "
			f"{sorted(on_disk - recorded)}, only recorded {sorted(recorded - on_disk)}"
		)


#: Every copied file and where it sits, so both tests below ask about all of them.
_VENDORED = (
	[(entry, subroutine.web.vendored.DIRECTORY) for entry in subroutine.web.vendored.CATALOGUE]
	+ [(entry, TEST_VENDOR) for entry in TEST_ONLY]
)


@pytest.mark.parametrize(
	("entry", "directory"),
	_VENDORED,
	ids=lambda value: value.filename if isinstance(value, subroutine.web.vendored.Vendored) else "",
)
def test_each_vendored_file_carries_a_permissive_licence (
	entry: subroutine.web.vendored.Vendored, directory: pathlib.Path
) -> None:
	"""All of these require the notice to travel with the code, and no build has one.

	§2.2a is the constraint: a copyleft *dependency* would bind the owner even though our own
	licence does not, so the allowed set is named rather than pattern-matched. **The test-only
	copy is held to the same rule**, because a licence obligation is not about who runs the file.
	"""

	assert entry.licence in subroutine.web.vendored.ALLOWED

	notice = directory / entry.notice

	assert notice.is_file(), f"{entry.filename} has no licence text beside it"
	assert notice.stat().st_size > 500, f"{notice.name} is too short to be a licence"


@pytest.mark.parametrize(
	("entry", "directory"),
	_VENDORED,
	ids=lambda value: value.filename if isinstance(value, subroutine.web.vendored.Vendored) else "",
)
def test_every_vendored_file_is_the_one_that_was_reviewed (
	entry: subroutine.web.vendored.Vendored, directory: pathlib.Path
) -> None:
	"""Nothing pinned these, and `script-src 'self'` admits whatever is in them by definition.

	The catalogue recorded the package, the version, the licence and the address it was fetched
	from — and none of that says what *arrived*. Replacing ``preact.js`` with arbitrary code
	passed the entire suite, and the policy's own argument is that the app loads nothing from
	another host, which is a claim about where the files come from rather than about what is in
	them.

	**The digest is of the file as it is served, not of the upstream download.**
	``phosphor.js`` is not the upstream file at all — it is a handful of path strings lifted
	out of a tarball — so pinning the source would be uncheckable for a quarter of the
	catalogue, and would answer a different question anyway. What matters is that the bytes in
	this repository are the bytes somebody read.

	Updating one is then: fetch it, look at it, and record the new digest in the same commit —
	which is the review, made into a step somebody has to take rather than one they might.
	"""

	body = (directory / entry.filename).read_bytes()
	found = f"sha256:{hashlib.sha256(body).hexdigest()}"

	assert found == entry.digest, (
		f"{entry.filename} is not the file recorded in the catalogue: it hashes to {found} and "
		f"{entry.digest} was written down. If you updated it deliberately, read the diff and "
		f"record the new digest in the same commit."
	)


def test_a_file_only_the_tests_run_is_never_served () -> None:
	"""**The renderer used to check the app is not part of the app.**

	`api/web._collected` *walks* the vendor directory, so where a copied file sits decides
	whether every reader downloads it. Nine kilobytes the page never imports would be served
	from the moment it landed in the wrong directory, and nothing about the file would say so.
	"""

	for entry in TEST_ONLY:
		assert entry.filename not in subroutine.api.web.FILES, (
			f"{entry.filename} is being served, so a test-only copy has become part of the app"
		)
		assert not (subroutine.web.vendored.DIRECTORY / entry.filename).exists()


def test_the_app_is_served_from_files_that_exist () -> None:
	"""The map is built at import, so a missing asset is an import-time failure — but a
	*typo* in the shell's name would only surface on the first request somebody made."""

	assert subroutine.api.web.SHELL in subroutine.api.web.FILES

	for name in ("app.js", "app.css", "preact.js", "preact-hooks.js", "htm.js"):
		assert name in subroutine.api.web.FILES, f"{name} is not served"


#: Tokens carrying text, and the ones they are read against. Both lists are the *whole* set
#: rather than the pairs anybody uses today: a rule moving `--warn` onto `--bg-raised` is an
#: ordinary edit, and it must not be the edit that first has to think about contrast.
INKS = ("--ink", "--ink-soft", "--ink-faint", "--accent", "--warn")
GROUNDS = ("--bg", "--bg-sunken", "--bg-raised")

#: WCAG 2.1 AA for text below 18pt. The stylesheet's largest step is 20px, so everything here
#: is small text and there is no large-text exemption to reason about.
AA_SMALL_TEXT = 4.5

#: AAA, which is what a reader asking their system for more contrast should get.
AAA_SMALL_TEXT = 7.0


def _themes (block: str | None = None) -> dict[str, dict[str, str]]:
	"""Each theme's colour tokens, read out of the stylesheet.

	**Every colour is one ``light-dark(a, b)`` declaration**, so both halves are stated
	together and neither theme can quietly inherit the other's value. That is what the
	arrangement before it could do: dark redefined ten tokens of twelve inside a media query,
	so a thirteenth added to the light block alone would have been the same colour in both and
	nothing would have said so.

	``block`` selects which ``:root`` to read — the default set, or the one inside a media
	query for a reader who has asked for more contrast.
	"""

	if block is None:
		# **The first `:root` block only.** Reading the whole file picks up the higher-contrast
		# block too, and since a later match wins, the defaults come back *as* the raised set —
		# so a comparison between them reports every pair as unchanged. Caught by the guard
		# below failing with `was 7.98:1 and is 7.98:1`, which is a defect in the reading rather
		# than in the palette.
		found = re.search(r":root\s*\{([^}]*)\}", (ASSETS / "app.css").read_text(), re.DOTALL)
		assert found is not None, "no :root block at all"
		block = found.group(1)

	text = block
	pairs = re.findall(
		r"(--[a-z-]+):\s*light-dark\(\s*(#[0-9a-fA-F]{6})\s*,\s*(#[0-9a-fA-F]{6})\s*\)", text
	)

	assert pairs, "no light-dark() colours found, so this would check nothing"

	return {
		"light": {name: light for name, light, _ in pairs},
		"dark": {name: dark for name, _, dark in pairs},
	}


def _contrast (one: str, other: str) -> float:
	"""The WCAG 2.1 ratio between two ``#rrggbb`` colours, from 1 to 21."""

	def luminance (colour: str) -> float:
		"""Return one hex colour's relative luminance, the way WCAG defines it."""

		channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
		linear = [
			channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
			for channel in channels
		]
		return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

	lit, dim = sorted((luminance(one), luminance(other)), reverse=True)

	return (lit + 0.05) / (dim + 0.05)


def test_every_ink_clears_the_contrast_minimum_on_every_background () -> None:
	"""**`#902`, and it computes the ratios rather than asserting the hexes.**

	Asserting a value would pass for ever while saying nothing about whether it is readable,
	and it would fail on a palette change that was *better* — so the guard has to do the
	arithmetic the eye cannot. The defect it was written for is what that catches:
	`--ink-faint` was `#868e98`, **3.31:1 on white against a 4.5:1 minimum**, and it renders
	`.row .ref` — the item number on every list row, at 13px, which `#441` calls the primary
	address. It was wrong from the day the palette was written and nothing had ever asked.

	**Every ink against every ground, not the pairs in use.** A rule pairing two of them is an
	ordinary edit and must not be the edit that first has to think about this.

	The dark half is the one arithmetic alone would find: it passed on two grounds and missed
	the third by **0.02**.
	"""

	failures = []

	for theme, tokens in _themes().items():
		missing = [name for name in INKS + GROUNDS if name not in tokens]
		assert not missing, f"{theme} declares no {missing}, so this checked almost nothing"

		for ink in INKS:
			for ground in GROUNDS:
				ratio = _contrast(tokens[ink], tokens[ground])
				if ratio < AA_SMALL_TEXT:
					failures.append(
						f"{theme}: {ink} ({tokens[ink]}) on {ground} ({tokens[ground]}) is "
						f"{ratio:.2f}:1, under {AA_SMALL_TEXT}"
					)

	assert not failures, "text below the contrast minimum:\n  " + "\n  ".join(failures)


def test_asking_for_more_contrast_gets_more_contrast () -> None:
	"""**`#908`. A `prefers-contrast` block that is merely present is worth nothing.**

	The failure this exists for is a block somebody adds to satisfy a checklist, whose values
	are a shade different in the wrong direction or the same shade copied — a control that is
	declared, documented and does nothing, which is a defect this codebase has found eight
	times. So it asserts the two things that make it real: **every ink it redefines is
	strictly higher against every ground**, and text reaches **AAA**.

	Backgrounds are deliberately not redefined, so they come from the default set. Moving one
	would change what every other ratio here was computed against.
	"""

	stronger = re.search(
		r"@media \(prefers-contrast: more\)\s*\{(.*?)\n\}", (ASSETS / "app.css").read_text(),
		re.DOTALL,
	)

	assert stronger is not None, "no prefers-contrast block; `#441` calls it not optional"

	default = _themes()
	raised = _themes(stronger.group(1))

	assert set(raised["light"]) >= {"--ink-soft", "--ink-faint"}, (
		f"only {sorted(raised['light'])} raised — the faint inks are the point of this"
	)

	failures = []

	for theme in ("light", "dark"):
		for token, colour in raised[theme].items():
			for ground in GROUNDS:
				was = _contrast(default[theme][token], default[theme][ground])
				now = _contrast(colour, default[theme][ground])

				if now <= was:
					failures.append(
						f"{theme}: {token} on {ground} was {was:.2f}:1 and is {now:.2f}:1 — "
						f"asking for more contrast got no more"
					)
				elif token in INKS and now < AAA_SMALL_TEXT:
					failures.append(
						f"{theme}: {token} on {ground} reaches {now:.2f}:1, under AAA's "
						f"{AAA_SMALL_TEXT}"
					)

	assert not failures, "the higher-contrast palette is not higher:\n  " + "\n  ".join(failures)


#: Properties whose lengths are the spacing scale's business. Borders, radii and the reading
#: measure are deliberately absent: `--radius` already works, and a measure is one number rather
#: than a step on a scale.
SPACING_PROPERTIES = ("padding", "margin", "gap", "row-gap", "column-gap")

#: Floors, so a scanner that reads nothing fails rather than reporting a clean stylesheet. Both
#: are comfortably under what is there — they catch a broken scan, not a shrinking file.
LEAST_TYPE_DECLARATIONS = 40
LEAST_SPACING_DECLARATIONS = 90


def _rules_only (text: str) -> str:
	"""The stylesheet with its comments removed.

	**Necessary rather than tidy.** The comment introducing the spacing scale quotes
	`.row { padding: 10px 14px }` as the argument for the scale's shape, so a scan that reads
	comments reports the documentation as the violation — which is `#836` exactly, where a link
	checker read Markdown inside a code span as live syntax and correct prose was reworded into
	worse prose to satisfy it.
	"""

	return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def test_every_token_the_stylesheet_names_is_one_that_exists () -> None:
	"""`#923`, and it took `#1021` shipping to get built.

	**An undefined custom property is not an error — the declaration is simply dropped**, so
	`color: var(--page)` renders as *no colour was set* and the browser reports it to nobody.
	That shipped on 2026-08-19: the token is `--bg`, so a filled chip's text fell back to
	inheriting its own fill and every identity mark on the served instance was a blank lozenge,
	in both themes, found by Simon within minutes of a deploy.

	**The guard beside this one could not see it, and `#923` was closed on the belief that it
	could.** `test_every_size_in_the_stylesheet_comes_from_a_named_step` reads two property
	families — `font-size` and the spacing properties — against `--text*`, `--space*` and
	`--control*`. It never looks at a colour, a border or a radius. Its docstring describes what
	it *does* accurately and says nothing about what it *covers*, and the close matched the
	prose to this item's words. **A guard's docstring describes its mechanism, not its reach.**

	**Both sides derived from the stylesheet**, which is `#907`'s argument and the reason this
	covers a token added tomorrow: a second list of names is the thing that goes stale.

	**Fallbacks are refused by name rather than allowed.** `var(--x, 4px)` resolves even when
	`--x` is missing, so it would walk straight past this — and a fallback is a second value in
	a system whose whole point is one. There are none today, which is the cheapest moment to
	say so.
	"""

	text = (ASSETS / "app.css").read_text(encoding="utf-8")
	rules = _rules_only(text)

	# Declared anywhere, not only on a bare `:root`: `#908`'s theme blocks redefine tokens under
	# `@media (prefers-color-scheme: dark)` and `[data-theme]`, and a token first declared in one
	# of those is still declared.
	declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", text))

	assert len(declared) >= 20, (
		f"only {len(declared)} tokens were found, so this is checking almost nothing"
	)

	used = set(re.findall(r"var\(\s*(--[a-z0-9-]+)\s*[),]", rules))

	assert len(used) >= 20, (
		f"only {len(used)} `var()` references were found, so this has stopped reading the rules"
	)

	missing = sorted(used - declared)

	assert not missing, (
		f"the stylesheet names {missing}, which nothing declares. A browser drops the whole "
		f"declaration rather than raising, so the property is simply unset and the page looks "
		f"almost right — `#1021` was text the colour of its own background."
	)

	# **A fallback is a second value, and this is where that is refused** (`#923`). It also
	# hides the failure above: `var(--page, red)` resolves and this check would pass.
	fallbacks = sorted(set(re.findall(r"var\(\s*--[a-z0-9-]+\s*,[^)]*\)", rules)))

	assert not fallbacks, (
		f"{fallbacks} carry a fallback. A token that needs one is a token that is not a step, "
		f"and a fallback resolves even where the token is missing — which is exactly the "
		f"failure above, made invisible again."
	)


def test_every_size_in_the_stylesheet_comes_from_a_named_step () -> None:
	"""**`#907`, applying decision `#906`. This is what makes it a system rather than a rename.**

	Without it this is 153 edits that begin accreting again the next afternoon — which is
	exactly how the state it replaced arose. Colour got tokens because `#102` forced a decision
	about colour; **nothing ever forced one about type or space**, so `font-size` reached 45
	declarations across eight values with no token at all, and `--gap` was used four times
	against 109 literal spacing declarations.

	**Checks the token is a declared one, not merely that a literal is absent.** `font-size:
	var(--radius)` has no literal in it and is nonsense, and a typo naming `--space-9` would
	silently resolve to nothing at all — which renders as the property being unset rather than
	as an error, and is the failure a browser reports to nobody.

	**Derived from `:root` rather than listed here**, so adding a step is one edit and the guard
	covers it; and so this cannot drift from the stylesheet the way a second copy would.
	"""

	text = (ASSETS / "app.css").read_text(encoding="utf-8")
	rules = _rules_only(text)

	steps = dict(re.findall(r"(--(?:text|space|control)[a-z0-9-]*):\s*([^;]+);", text))
	type_steps = {name for name in steps if name.startswith("--text")}

	#: **A control size counts as spacing here** (`#763`). The three of them are compositions
	#: of space steps — `--control-field` is `var(--space-5) var(--space-6)` — so they add no
	#: value to the scale; what they add is a *name for a control's size*, which is the thing
	#: that was missing when 44 control rules carried 13 distinct paddings between them.
	space_steps = {
		name for name in steps if name.startswith(("--space", "--control"))
	}

	#: A literal usually *is* a step's value — somebody wrote `10px` where `--space-5` says the
	#: same thing. Saying only "10px is not a step" reads as nonsense in that case, so the
	#: refusal names the token to use whenever it can. §13's rule: say what to do next.
	#:
	#: **Scoped to the scale being checked**, because the two overlap: 13px is `--text-small`
	#: and is nothing at all in spacing, so an unscoped lookup answers a question about padding
	#: with a type token. Caught by reading the message a falsification printed.
	def advice (part: str, allowed: set[str]) -> str:
		"""Name the token this literal should have been, where one matches it."""

		for name in sorted(allowed):
			if steps[name].strip() == part:
				return f"write var({name})"

		return "not on the scale"

	assert len(type_steps) >= 5, f"only {type_steps} declared, so this checks almost nothing"
	assert len(space_steps) >= 8, f"only {space_steps} declared, so this checks almost nothing"

	assert sum(name.startswith("--control") for name in space_steps) == 3, (
		f"a control has three sizes and {sorted(space_steps)} says otherwise — a fourth is a\n"
		f"decision (`#763`), not a value somebody needed once"
	)

	failures = []

	sizes = re.findall(r"font-size:\s*([^;}]+)", rules)
	for value in sizes:
		named = re.fullmatch(r"var\((--[a-z0-9-]+)\)", value.strip())
		if named is None or named.group(1) not in type_steps:
			failures.append(f"font-size: {value.strip()} — {advice(value.strip(), type_steps)}")

	spacing = re.findall(
		r"\b(?:" + "|".join(SPACING_PROPERTIES) + r")(?:-[a-z]+)?:\s*([^;}]+)", rules
	)
	for value in spacing:
		for part in value.split():
			if part in ("0", "auto", "inherit") or not part:
				continue
			named = re.fullmatch(r"var\((--[a-z0-9-]+)\)", part)
			if named is None or named.group(1) not in space_steps:
				failures.append(f"spacing: {value.strip()} — {part}: {advice(part, space_steps)}")

	assert len(sizes) >= LEAST_TYPE_DECLARATIONS, (
		f"found {len(sizes)} font-size declarations, expected at least "
		f"{LEAST_TYPE_DECLARATIONS} — the scan is broken, not the stylesheet"
	)
	assert len(spacing) >= LEAST_SPACING_DECLARATIONS, (
		f"found {len(spacing)} spacing declarations, expected at least "
		f"{LEAST_SPACING_DECLARATIONS} — the scan is broken, not the stylesheet"
	)

	assert not failures, (
		f"{len(failures)} sizes are not on the scale (`#906` decides it; add a step there "
		f"rather than a literal here):\n  " + "\n  ".join(sorted(set(failures)))
	)


def test_a_row_says_where_its_item_lives (tmp_path: pathlib.Path) -> None:
	"""**`#959`, and the assertion that matters is the *wiring*, not the function.**

	**This reverses `#912`, which put the project's *title* here.** That decision was taken
	when the chip was one segment and the argument was register — `Document` beside
	`subroutine`, two registers in one row. Decision `#957` §4 answers a different question:
	`Subroutine` and `Web UI` are **not distinct from one another**, because Web UI is inside
	Subroutine and the chip did not say so. A path made of titles has to invent a separator
	that reads as hierarchy and stops being the thing you can type back (`#151`), so the label
	is the address, in slug form.

	**Driven through the three views rather than through `Row`**, because this project has
	shipped four faults of exactly one shape — the rule right, the display right, and no wire
	between them (`#640`). `projectName` being correct proves nothing about whether `Listing`
	passes anything to it, and a `Row` rendered directly with `projects` would prove only that
	the prop I just added works.

	**A row whose project was not among the fetched ones is asserted too.** The app asks for
	two hundred and a workspace may hold more, and the chip is built from the row's own
	``project_path`` — so the case that used to lose the chip cannot any more, and this holds
	that property rather than describing it.

	**Nothing is passed a project list.** It was, until `#971`: the label came from
	``projectName`` under `#912` and needed the workspace's projects to turn a key into a
	title. `#959` made it the address, composed from a field the row already carries, and the
	argument stayed behind reading nothing.
	"""

	filed = {"ref": 1, "kind": "task", "title": "A task", "status_is_default": True}
	elsewhere = dict(filed, project_key="unfetched", project_path="unfetched")

	shown = _rendered(tmp_path, {
		"Listing": {
			"items": [dict(filed, project_key="sr", project_path="sr")],
		},
		# Nested, and the board names no project — so the whole address is what it shows.
		"Board": {
			"items": [dict(
				filed, project_key="ui", project_path="subroutine/ui", status_category="todo"
			)],
		},
		# The agenda spans workspaces, so its label leads with the row's own — `/` names none.
		"Agenda": {
			"buckets": [{
				"key": "overdue", "label": "Overdue",
				"items": [dict(
					filed, project_key="sr", project_path="sr", workspace="projects"
				)],
			}],
			"where": "projects",
		},
	})

	assert "sr" in shown["Listing"], f"the listing shows no address: {shown['Listing']}"
	assert "subroutine/ui" in shown["Board"], f"the board shows no address: {shown['Board']}"
	assert "projects/sr" in shown["Agenda"], (
		f"the agenda spans workspaces and named none: {shown['Agenda']}"
	)

	unknown = _rendered(tmp_path, {
		"Listing": {"items": [elsewhere]},
	})["Listing"]

	assert "unfetched" in unknown, (
		f"a row whose project was not among the fetched ones lost its chip: {unknown}"
	)


TITLED_PLACES = [
	{"key": "subroutine", "title": "Subroutine", "depth": 0},
	{"key": "ui", "title": "Web UI", "depth": 1},
	{"key": "ops", "title": "Release and hosting", "depth": 1},
	{"key": "acme", "title": "Acme", "depth": 0},
]

TITLED_SPACES = [{"slug": "projects", "title": "Projects"}, {"slug": "personal", "title": "Errands"}]


def test_every_page_says_which_item_or_place_it_is_showing (tmp_path: pathlib.Path) -> None:
	"""`SR#1214`, Simon: *"I have multiple tabs open and they all just say 'Subroutine'."*

	Nothing wrote a title at all — `index.html` carried a static one and `document.title` was
	assigned nowhere — so it was never that the title was wrong; every tab on every page said one
	word.

	**The scope reads with `/` and the view with `:`**, which is his: they are different axes and
	one separator for both would read as a four-level path.

	**Titles rather than slugs**, which is not a contradiction of `SR#151`. That rule is about a
	row's project chip, which is the thing you can type back into an address; a tab is read at a
	glance and never typed, and a reader scanning eight of them wants the word they call the
	place.
	"""

	shown = _views(tmp_path, [
		("pageTitle", {"item": {"ref": 1111, "title": "The release gate finishes in time"}}),
		("pageTitle", {"place": None, "showing": {"view": "agenda", "selection": {}},
			"workspaces": TITLED_SPACES, "projects": TITLED_PLACES}),
		("pageTitle", {"place": {"workspace": "projects", "project": None},
			"showing": {"view": "agenda", "selection": {}},
			"workspaces": TITLED_SPACES, "projects": TITLED_PLACES}),
		("pageTitle", {"place": {"workspace": "projects", "project": "subroutine"},
			"showing": {"view": "board", "selection": {"include_completed": "true"}},
			"workspaces": TITLED_SPACES, "projects": TITLED_PLACES}),
		("pageTitle", {"place": {"workspace": "projects", "project": "subroutine/ui"},
			"showing": {"view": "board", "selection": {"include_completed": "true"}},
			"workspaces": TITLED_SPACES, "projects": TITLED_PLACES}),
	])

	assert shown == [
		"#1111 The release gate finishes in time · Subroutine",
		"Agenda · Subroutine",
		"Projects: Agenda · Subroutine",
		"Projects / Subroutine: Board · Subroutine",
		"Projects / Subroutine / Web UI: Board · Subroutine",
	], shown


def test_a_tab_title_names_the_control_that_is_highlighted (tmp_path: pathlib.Path) -> None:
	"""The view segment comes from `chips`, not from `showing.view` — `SR#1214`.

	**Because *done* is a selection rather than an arrangement** (`SR#738`), and its arrangement
	is a list. A title built from the view name would call that page `List`, which is the word
	beside a control the reader can see is *not* the one lit up — the tab and the switcher
	disagreeing about the same page.

	**And an address no control produces highlights nothing**, which `chips` already answers by
	computing `chosen` rather than remembering it. The view name is the fallback there, because a
	title is not a place to refuse anything.
	"""

	finished, odd = _views(tmp_path, [
		("pageTitle", {"place": {"workspace": "projects", "project": None},
			"showing": {"view": "list",
				"selection": {"status_category": "done", "order": "-completed_at"}},
			"workspaces": TITLED_SPACES, "projects": TITLED_PLACES}),
		("pageTitle", {"place": {"workspace": "projects", "project": None},
			"showing": {"view": "list", "selection": {"status_category": "in_progress"}},
			"workspaces": TITLED_SPACES, "projects": TITLED_PLACES}),
	])

	assert finished == "Projects: Done · Subroutine", finished
	assert odd == "Projects: List · Subroutine", (
		f"an address no control produces named a control anyway: {odd}"
	)


def test_the_page_actually_writes_the_title_it_decided_on (tmp_path: pathlib.Path) -> None:
	"""`SR#1214`'s wiring half, which is the half that has ever been wrong here.

	`pageTitle` being right is worth nothing until something hands its answer to the document —
	`SR#640`'s lesson, and the reason this harness exists: four of this arc's shipped faults were
	a component handing a correct rule the wrong value, and none was the rule.

	**Both an item and a place**, because they take different branches: an item takes no scope at
	all, exactly as its address takes neither an arrangement nor a selection (`SR#766`).
	"""

	place = _driven(tmp_path, pathname="/projects", search="?view=list")

	# **Lower case because this harness's workspace carries no title**, which is the documented
	# fallback rather than a defect: `placesToGo` labels one `title || slug` for the same reason.
	# Whether a title is preferred over a slug is asked of `pageTitle` directly next door; what
	# this is for is that *something* reached the document at all.
	assert place["title"] == "projects: List · Subroutine", (
		f"the tab does not say which place it is showing: {place['title']!r}"
	)

	item = _driven(
		tmp_path,
		pathname="/projects/42",
		answers={"/v1/tasks/42": {"ref": 42, "title": "Fix the pagination cursor",
			"kind": "task", "status_is_default": True}},
	)

	assert item["title"] == "#42 Fix the pagination cursor · Subroutine", (
		f"the tab does not say which item it is showing: {item['title']!r}"
	)


def test_a_project_the_tree_does_not_describe_still_names_itself (
	tmp_path: pathlib.Path,
) -> None:
	"""A segment with no title falls back to its key rather than vanishing.

	`SR#959`'s reason, one surface along: a chip that disappears is worse than one naming
	something unfamiliar. The tree is capped at 200 projects and an address is anybody's to type,
	so a path this page cannot describe is a real state rather than a defensive branch.
	"""

	[shown] = _views(tmp_path, [
		("pageTitle", {"place": {"workspace": "projects", "project": "subroutine/unfetched"},
			"showing": {"view": "list", "selection": {}},
			"workspaces": TITLED_SPACES, "projects": TITLED_PLACES}),
	])

	assert shown == "Projects / Subroutine / unfetched: List · Subroutine", shown


def test_the_agenda_accounts_for_what_it_is_not_showing (tmp_path: pathlib.Path) -> None:
	"""`SR#1215`, Simon's decision of 2026-08-24, and `SR#649`'s amendment made visible.

	An arrangement may draw its rows from a different endpoint, and when it does it must say what
	it left behind. Two of the agenda's four exclusions have reported a count since `SR#997` and
	`SR#888`; the other two were silent, which was harmless while the agenda had one address and
	became a gap a reader can see the moment it sits beside `?view=list` at the same one.

	**One line, which was his choice against three alternatives**, and the arithmetic behind it
	is guarded in `tests/test_agenda.py` rather than here: what makes the accounting trustworthy
	is that the counts sum to the difference, not how many lines they are printed on.

	**A cause contributing nothing is left out rather than printed as zero** — §12.2a one surface
	along, and on this instance `paused` is zero on every page.
	"""

	shown = _rendered(tmp_path, {"Agenda": {
		"buckets": [], "more": 12, "heldUp": 4, "later": 3, "deferred": 9, "paused": 0,
		"gone": 2,
	}})["Agenda"]

	assert "30 not shown here" in shown, (
		f"the day does not account for what it left behind: {shown}"
	)
	# **The second capped bucket, counted like the first** (`SR#1285`). It is the one cause
	# here that the page itself chose to hide rather than the day holding back, which is why
	# it reads *more* like `unscheduled` does.
	assert "4 more waiting on somebody else" in shown, (
		f"the blocked section capped what it drew and did not say so: {shown}"
	)
	assert "9 put off until later" in shown, (
		f"work somebody deferred vanishes with nothing saying so, which is the gap this was "
		f"built for: {shown}"
	)
	assert "12 more unscheduled" in shown and "3 dated further out" in shown, shown

	# **The fifth, and the only one nobody chose** (`SR#1236`, decision `SR#1235` §3). A passed
	# event is not *completed*, so `?view=list` at this scope still holds it — which makes it
	# exactly the unexplained difference between two views of one place that this line exists
	# to close.
	assert "2 already past" in shown, (
		f"an event that has gone by leaves the day with nothing saying so: {shown}"
	)

	assert "nobody is running" not in shown, (
		f"a cause hiding nothing was printed anyway, so the line carries a zero a reader has "
		f"to work out is meaningless: {shown}"
	)

	# **Nothing at all on a day that is showing everything**, rather than a line reading zero.
	whole = _rendered(tmp_path, {"Agenda": {"buckets": [], "more": 0}})["Agenda"]

	assert "not shown here" not in whole, (
		f"a complete day claims to be hiding something: {whole}"
	)


def test_a_scoped_agenda_strips_the_place_its_address_already_names (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#1215`, and the defect Simon found by reading a page the gate called green.

	Decision `SR#957` §4: the exception is what is drawn. A row's project chip says where that
	row is *relative to where the reader already is*, so a listing at `/projects/subroutine`
	labels a row in `subroutine/ui` as `ui` and labels one filed directly in `subroutine` not at
	all.

	**The agenda hardcoded its place to nowhere**, which was true while `/` was its only address
	and became wrong the moment a project had one: every row on `/projects/subroutine` was
	labelled `projects/subroutine` under a heading that already said it. The listing and the
	board took `place` all along — only this had the assumption baked into the component.

	**Both directions, because either alone passes against half the fix.** A version that always
	strips loses the merged agenda's whole address, which is `SR#966` and `SR#968` undone; one
	that never strips is the defect.
	"""

	rows = [{
		"key": "overdue", "label": "Overdue",
		"items": [
			{"ref": 1, "kind": "task", "title": "Under the project", "status_is_default": True,
				"project_key": "ui", "project_path": "subroutine/ui", "workspace": "projects"},
			{"ref": 2, "kind": "task", "title": "In the project itself",
				"status_is_default": True, "project_key": "subroutine",
				"project_path": "subroutine", "workspace": "projects"},
		],
	}]

	# **Read as a reader reads it, with the tags taken out.** An `href` must stay complete
	# whatever the chip says — `SR#638` gives an item one durable address — so asserting over the
	# raw markup would match the link target and pass against the defect. The claim here is about
	# the *label*.
	def words (markup: str) -> str:
		"""Return the text a reader sees, with every tag and attribute removed."""

		return re.sub(r"<[^>]*>", " ", markup)

	scoped = words(_rendered(tmp_path, {"Agenda": {
		"buckets": rows, "where": "projects",
		"place": {"workspace": "projects", "project": "subroutine"},
	}})["Agenda"])

	assert "projects/subroutine" not in scoped, (
		f"a scoped agenda repeats the place its own address names on every row: {scoped}"
	)
	assert "ui" in scoped, (
		f"the sub-project a row is actually in is not shown, so the chip says nothing: {scoped}"
	)

	# **The row filed directly in the addressed project gets no chip at all**, which is the
	# other half of §4: there is no exception to draw.
	assert scoped.count("ui") == 1, (
		f"a row filed in the addressed project was labelled anyway: {scoped}"
	)

	merged = words(_rendered(tmp_path, {"Agenda": {
		"buckets": rows, "where": "projects",
	}})["Agenda"])

	assert "projects/subroutine/ui" in merged, (
		f"the merged agenda names no place, so a row must carry its whole address: {merged}"
	)

	# **The ref's workspace prefix is the other half, and it is a separate rule** (`SR#968`,
	# `SR#638`): an item's durable address is `{workspace}/{ref}`, so showing the prefix is
	# showing more of the address rather than adding a fact. Found by falsifying — the
	# assertions above pass with the prefix wrong in either direction, because the project chip
	# and the ref are two controls answering one question.
	assert "projects/#1" in merged, (
		f"the merged agenda spans workspaces and a row did not say which it is from: {merged}"
	)
	assert "projects/#1" not in scoped, (
		f"a scoped agenda names the workspace on every row under an address that already says "
		f"it: {scoped}"
	)
	assert "#1" in scoped, f"the row lost its number as well as its prefix: {scoped}"


def test_the_browser_and_the_terminal_call_a_blocker_the_same_thing () -> None:
	"""**`#913`. The one copy of these words that cannot import the others.**

	`cli/personal` and `mcp/tools` both take them from `views` now, so those two cannot
	disagree. The browser is JavaScript and has to carry its own, which is the arrangement that
	produced the defect: a card said *Holds up* where the item it opened said *Blocks*, and
	`#674`'s lesson is that a rule written in more than one place needs something comparing
	them rather than somebody noticing.

	**Case-insensitively, because the difference is deliberate.** §13.5b's output is lower case
	throughout and the browser capitalises a mark, so requiring an exact match would force one
	surface to adopt the other's register — which is a different decision from this one and not
	the one being guarded.
	"""

	source = _without_comments((ASSETS / "app.js").read_text(encoding="utf-8"))
	marks = re.search(r"export function marks \(.*?\n\}", source, re.DOTALL)

	assert marks is not None, "`marks` has moved, so this is scanning nothing"

	body = marks.group(0)
	shown = {text.lower() for text in re.findall(r'text:\s*"([^"]+)"', body)}

	# **The floor counts what the function emits, not what it emits as a plain string.** Most
	# marks are template literals — a deadline, a defer, a lease all carry a value — so a floor
	# on the string set would have to be 2, which is the number this test is *about* and could
	# not tell a broken scan from a complete one.
	#
	# **Every push rather than `found.push`** (`SR#1019`): the function gathers into three
	# arrays now, one per family, so counting the first would have fallen to 3 and read as a
	# scan that had stopped matching the body. Twelve at the time of writing.
	pushed = body.count(".push(")

	assert pushed >= 10, (
		f"`marks` pushes {pushed} marks, so the body was not fully matched"
	)

	for name, word in (
		("BLOCKED_MARK", subroutine.views.BLOCKED_MARK),
		("BLOCKING_MARK", subroutine.views.BLOCKING_MARK),
	):
		assert word.lower() in shown, (
			f"`views.{name}` is {word!r} and the browser's `marks` says none of {sorted(shown)}. "
			f"One relationship with two names is what `#913` was"
		)


def test_a_theme_nobody_recognises_reads_as_the_system_one (tmp_path: pathlib.Path) -> None:
	"""**`#908`. A stored value the app does not know must not strand the page.**

	Storage on this origin outlives any version of this app, and a person can type into it. A
	value from an older release, a typo, or another page's key has to land somewhere a control
	can get out of — and `system` is that place, because it is also what nothing-stored means.

	**Storage throwing is the same answer**, not an exception: a private window can refuse, and
	a to-do list that will not render because it could not read a preference is a worse bargain
	than one that renders in the system theme.
	"""

	answers = _ran(tmp_path, f"""
		import {{ themeChoice }} from "{_staged(tmp_path).as_uri()}";

		const stored = (value) => ({{ getItem: () => value }});
		const broken = {{ getItem: () => {{ throw new Error("no storage here"); }} }};

		console.log(JSON.stringify({{
			light: themeChoice(stored("light")),
			dark: themeChoice(stored("dark")),
			system: themeChoice(stored("system")),
			nothing: themeChoice(stored(null)),
			nonsense: themeChoice(stored("solarized")),
			absent: themeChoice(undefined),
			broken: themeChoice(broken),
		}}));
	""")

	assert answers["light"] == "light"
	assert answers["dark"] == "dark"
	assert answers["system"] == "system"

	for case in ("nothing", "nonsense", "absent", "broken"):
		assert answers[case] == "system", f"{case} read as {answers[case]!r}"


def test_choosing_the_system_theme_removes_the_attribute (tmp_path: pathlib.Path) -> None:
	"""**`#908`, and it is the one that would have shipped broken.**

	The stylesheet's three states are two selectors and their absence: `[data-theme="light"]`,
	`[data-theme="dark"]`, and nothing at all for `system`. So writing `data-theme="system"`
	matches neither rule and pins the page to light — *going back to* the system theme would be
	the one choice that did not work, and it would look like the control being ignored.

	Also asserts a refused write still applies: not remembering a choice costs the next load,
	where not honouring it costs this one.
	"""

	answers = _ran(tmp_path, f"""
		import {{ applyTheme }} from "{_staged(tmp_path).as_uri()}";

		const store = () => {{
			const held = {{}};
			return {{ held, setItem: (key, value) => {{ held[key] = value; }} }};
		}};
		const refuses = {{ setItem: () => {{ throw new Error("storage is full"); }} }};

		const after = (chosen, storage) => {{
			const root = {{ dataset: {{}} }};
			const applied = applyTheme(chosen, storage, root);
			return {{ applied, attribute: root.dataset.theme ?? null }};
		}};

		const kept = store();
		const pinned = after("dark", kept);
		const back = after("system", store());
		const nonsense = after("solarized", store());
		const unwritable = after("dark", refuses);

		console.log(JSON.stringify({{ pinned, back, nonsense, unwritable, remembered: kept.held }}));
	""")

	assert answers["pinned"] == {"applied": "dark", "attribute": "dark"}
	assert answers["remembered"] == {"theme": "dark"}

	assert answers["back"] == {"applied": "system", "attribute": None}, (
		"choosing the system theme wrote an attribute, which matches no rule and pins the page "
		"to light — the one choice that would not work"
	)

	assert answers["nonsense"]["applied"] == "system", "an unknown theme was applied as itself"
	assert answers["unwritable"]["attribute"] == "dark", (
		"storage refused the write and the theme was not applied either, so a private window "
		"cannot change theme at all"
	)


def test_the_page_asks_for_nothing_this_instance_does_not_serve () -> None:
	"""Every asset the page names is one of the files, so a fresh load fetches nothing 404.

	The failure this prevents is silent in the worst way: a browser reports a missing module
	in its console and shows a blank page, and the server logs a 404 nobody is watching.
	"""

	page = (ASSETS / "index.html").read_text(encoding="utf-8")
	wanted = {
		part.split('"')[0]
		for part in page.split('"/app/')[1:]
	}

	assert wanted, "the page names no assets, so this is checking nothing"

	for name in sorted(wanted):
		assert name in subroutine.api.web.FILES, f"index.html asks for /app/{name}"


def test_every_asset_the_stylesheet_reaches_for_is_one_this_instance_serves () -> None:
	"""`SR#1536`. A stylesheet that names a file nobody serves fails in silence.

	The masthead's mark is a `mask` over an exported SVG, which is the first `url()` this
	stylesheet has ever contained — so there was no precedent for checking that what it asks
	for arrives. **A 404 here paints nothing and reports nothing**: the rule still applies, the
	computed `mask-image` still reads back as the URL that failed, and the only symptom is a
	mark that is not there. The browser test beside it catches the rule being *removed* and
	cannot catch this, because from inside a cascade the two look the same.

	**Relative, so it is checked as the browser would resolve it.** The stylesheet is served
	from `/app/app.css`, so a bare `favicon.svg` in it means `/app/favicon.svg` — and a name
	with a path in it would mean something this check should not quietly approve, so anything
	that is not a bare filename fails here rather than being normalised into passing.

	Written over the whole file rather than over the one rule, so the next asset somebody
	reaches for is covered on the day it is added.
	"""

	# **Comments stripped first, because this guard's own explanation contains the thing it
	# scans for.** The rule it was written for is documented in the stylesheet, that prose
	# says the words `url()`, and the first version of this read them as an asset named by
	# the empty string. A scan over text counting its own reason is this project's recorded
	# shape, and it arrived here within a minute of the scan being written.
	stylesheet = re.sub(
		r"/\*.*?\*/", "", (ASSETS / "app.css").read_text(encoding="utf-8"), flags=re.S
	)
	wanted = {
		part.split(")")[0].strip().strip("\"'")
		for part in stylesheet.split("url(")[1:]
	}

	assert wanted, "the stylesheet names no assets, so this is checking nothing"

	for name in sorted(wanted):
		assert "/" not in name, (
			f"the stylesheet asks for {name!r}, which is not a bare filename — assets are "
			f"served flat at /app/<name> and a path cannot be resolved against that"
		)

		assert name in subroutine.api.web.FILES, (
			f"app.css asks for /app/{name} and nothing serves it, so whatever it draws is "
			f"invisible with no error anywhere"
		)


def test_every_page_this_instance_serves_wears_the_same_mark () -> None:
	"""**`SR#1286`.** Two pages declare the icon and one of them is where a new user lands.

	The app shell and the sign-in page carried the same three lines as two copies, so changing
	the mark on one would leave the old one on the first page anybody handed a login link ever
	sees. That is one fact rendered twice, on a line nobody would think to compare — `#583` and
	`#674`'s defect in its quietest form, because both pages go on working.

	**One is interpolated and one cannot be.** ``sessions.py`` builds its head in Python and
	uses :data:`subroutine.api.web.ICON_LINKS` directly; ``index.html`` is served verbatim, so
	the same block is written there and this is what holds them together.
	"""

	page = (ASSETS / "index.html").read_text(encoding="utf-8")
	source = pathlib.Path(subroutine.api.sessions.__file__).read_text(encoding="utf-8")

	assert subroutine.api.web.ICON_LINKS in page, (
		"`index.html` no longer carries the icon block `api.web.ICON_LINKS` declares, so the "
		"app shell and the sign-in page can show different marks"
	)
	assert "subroutine.api.web.ICON_LINKS" in source, (
		"the sign-in page has stopped interpolating the shared block and is writing its own"
	)

	# **And every file it names is served**, which the shell's own check does for the shell.
	# This block is a *string*, so nothing else reads it: it could name three assets that do
	# not exist and the only symptom would be a browser tab with no icon on it.
	asked = {
		part.split('"')[0]
		for part in subroutine.api.web.ICON_LINKS.split('"/app/')[1:]
	}

	assert len(asked) == 3, f"the mark names {len(asked)} files rather than three: {asked}"

	for name in sorted(asked):
		assert name in subroutine.api.web.FILES, f"the mark asks for /app/{name}"


def test_the_placeholder_mark_is_gone_and_nothing_still_asks_for_it () -> None:
	"""**`SR#1286`**, and the deletion is the point rather than a side-effect.

	``icon.svg`` was `#644`'s placeholder — a blue rounded square with a white tick — and its
	own comment argued against ever shipping a multi-resolution bitmap: *"a build step in a
	project whose whole web design decision was not to have one."* That was right for a
	placeholder and does not survive a real mark. **These are exported, not built**: the sizes
	already exist, there is no step to add, and a designed identity is worth being correct at
	16px rather than merely legible.

	So it is deleted rather than left beside the new one, and this says so — otherwise the
	deletion reads as an oversight to whoever finds the reference next.
	"""

	assert "icon.svg" not in subroutine.api.web.FILES, (
		"the placeholder mark is still served, so two icons ship and one of them is `#644`'s "
		"blue square"
	)

	for path in (ASSETS / "index.html", pathlib.Path(subroutine.api.sessions.__file__)):
		assert "icon.svg" not in path.read_text(encoding="utf-8"), (
			f"{path.name} still asks for the placeholder, which this instance no longer serves"
		)


def test_the_mark_is_served_as_something_a_browser_will_draw () -> None:
	"""**`SR#1286`.** ``TYPES`` is a closed map and it used to close out every bitmap.

	``_collected`` skips a suffix it does not know **in silence**, so the whole icon set could
	sit in ``assets`` and be served by nothing at all, with no test failing and no error
	anywhere. A closed map is only safe while somebody notices what it closes out, and nothing
	did until the files arrived.

	**Driven through the real lookup rather than asserted about the map**, because the map
	being right and the walk reading it are two facts.
	"""

	for name, kind in (
		("favicon-on-black.ico", "image/x-icon"),
		("favicon-on-black.svg", "image/svg+xml"),
		("apple-touch-icon.png", "image/png"),
	):
		body, served = subroutine.api.web.FILES[name]

		assert served == kind, f"{name} is served as {served!r}"
		assert body, f"{name} is served empty"

	# **The bytes are the exporter's**, checked at the file's own signature rather than by
	# size: a truncated copy has the right name and the right suffix and draws nothing.
	assert subroutine.api.web.FILES["apple-touch-icon.png"][0].startswith(b"\x89PNG\r\n")
	assert subroutine.api.web.FILES["favicon-on-black.ico"][0].startswith(b"\x00\x00\x01\x00")


def test_the_app_reaches_only_the_public_api () -> None:
	"""`#351`: the UI talks to the same API everything else does, and to nothing private.

	Anything this page can show, a script can too — which is what stops the browser quietly
	becoming the only way to do something, and is why this is a product rule rather than a
	tidiness one.
	"""

	source = (ASSETS / "app.js").read_text(encoding="utf-8")

	assert 'fetch(`/v1${path}`' in source, "the one place a request is made has moved"
	assert source.count("fetch(") == 1, "a second fetch would be a second set of rules"


def test_every_bare_specifier_a_served_module_imports_is_in_the_import_map () -> None:
	"""**The check that was missing, and the page was blank without it.**

	Nothing rewrites these files on the way to a browser — that is the point of having no
	build step — so a bare specifier with no import-map entry is a module that never loads.
	The browser says `Failed to resolve module specifier "htm"` in a console nobody is
	watching and renders an empty page. No request 404s. Nothing is logged server-side. Every
	other check here passed while it was broken.

	Derived from the modules rather than listed, so a new import is covered the moment it is
	written — and it scans the *vendored* files too, which is where the requirement comes
	from: the minified `preact-hooks.js` asks for `preact` itself.
	"""

	resolvable = set(_import_map())
	missing: dict[str, set[str]] = {}

	for name, source in _served_modules().items():
		unresolved = _bare_imports(source) - resolvable

		if unresolved:
			missing[name] = unresolved

	assert _served_modules(), "no modules were scanned, so this is checking nothing"
	assert not missing, (
		f"these bare specifiers have no import-map entry, so the browser cannot load them: "
		f"{ {name: sorted(found) for name, found in missing.items()} }"
	)


def test_the_import_map_points_only_at_files_that_are_served () -> None:
	"""The other direction: an entry naming a file that is not served is a 404 on load.

	Same failure, opposite cause, and equally silent — so the map is checked both ways for
	`#405`'s reason rather than only in the direction that has already bitten.
	"""

	for specifier, target in _import_map().items():
		assert target.startswith("/app/"), f"{specifier!r} resolves outside the app's files"

		name = target.removeprefix("/app/")

		assert name in subroutine.api.web.FILES, (
			f"the import map sends {specifier!r} to {target}, which this instance does not serve"
		)


def test_the_specifier_scan_ignores_prose () -> None:
	"""A `from "…"` scan reported a *comment* as an import, which would have made this guard
	demand a map entry for a sentence. Falsified through the real scanner rather than a copy
	of its rule."""

	assert _bare_imports('/* tell "sign in" from "something went wrong" */') == set()
	assert _bare_imports('import htm from "htm";') == {"htm"}
	assert _bare_imports('import{a}from"preact/hooks";') == {"preact/hooks"}
	assert _bare_imports('import x from "./local.js";') == set()


# ---- the Markdown renderer (`#637`) --------------------------------------------------------

#: Ways somebody could try to make stored text become markup. Written as *source* rather than
#: as expected output, because what matters is that none of them produces anything a browser
#: acts on — and asserting the exact HTML for each would pin the renderer's formatting rather
#: than its safety, so a harmless change to spacing would read as a security failure.
#:
#: Several are the ones a prefix test on the scheme lets through, which is why the scheme is
#: parsed instead: two spellings of `javascript:` that a browser accepts and a literal check
#: does not, a protocol-relative address that looks like a path, and a `data:` document that is
#: same-origin in some browsers. **And one is protocol-relative behind a control character**
#: (`#682`) — the case that showed the two checks were reading two different values.
HOSTILE = [
	"<script>alert(1)</script>",
	"<img src=x onerror=alert(1)>",
	'<a href="javascript:alert(1)">click</a>',
	"<iframe srcdoc='<script>alert(1)</script>'></iframe>",
	"<svg/onload=alert(1)>",
	"<style>body{display:none}</style>",
	"<base href='https://evil.example'>",
	"[click](javascript:alert(1))",
	"[click](JaVaScRiPt:alert%281%29)",
	"[click](java\tscript:alert(1))",
	"[click](java\nscript:alert(1))",
	"[click](//evil.example/steal)",
	# `#682`: one leading control character skipped the protocol-relative refusal, because that
	# test read the trimmed destination while the *scheme* test read the stripped one. Measured
	# before the fix — it came back unchanged, and a browser resolves it to evil.example.
	"[click](\u0001//evil.example/steal)",
	# `#927` H-16: the same address written with backslashes, which a browser resolves
	# identically and which took **both** branches of the slash test — `/\` began with `/` and
	# not `//`, and `\\` began with neither, so no single refusal could have caught both.
	"[click](/\\evil.example/steal)",
	"[click](\\\\evil.example/steal)",
	"[click](\\/evil.example/steal)",
	"[click](data:text/html,<script>alert(1)</script>)",
	"[click](vbscript:msgbox(1))",
	'![x](https://evil.example/p.gif" onerror="alert(1))',
	"`<script>alert(1)</script>`",
	"```\n<script>alert(1)</script>\n```",
	"> <script>alert(1)</script>",
	"| <script>alert(1)</script> |\n| --- |\n| x |",
	"- <script>alert(1)</script>",
	"# <script>alert(1)</script>",
	"**<script>alert(1)</script>**",
	'<div onmouseover="alert(1)">hover</div>',
	"<!-- --><script>alert(1)</script><!-- -->",
	"&lt;script&gt;alert(1)&lt;/script&gt;",
]

#: Every tag the renderer is allowed to produce. Anything else means either that source HTML
#: reached the output or that a construct emits something nobody chose — and `img`, `iframe`,
#: `script`, `style` and `svg` are absent on purpose, so their absence is asserted by this
#: list existing rather than by naming each of them.
EMITTED_TAGS = {
	"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "del", "code", "pre",
	"blockquote", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td", "a",
}


def _tags (html: str) -> set[str]:
	"""Return the tag names that appear in some HTML."""

	return {found.lower() for found in re.findall(r"</?([a-zA-Z][a-zA-Z0-9]*)", html)}


def _elements (html: str) -> list[str]:
	"""Return the opening tags in some HTML, which is where every attribute lives.

	**Real tags only.** A first version of the check below scanned the whole string for
	``on…=`` and failed on ``&lt;img src=x onerror=alert(1)&gt;`` — which is the renderer
	working perfectly, showing an attack as the text it is. A test that reads escaped output
	as dangerous would push somebody towards making it less safe.
	"""

	return re.findall(r"<[a-zA-Z][^>]*>", html)


def _without_comments (source: str) -> str:
	"""Return some JavaScript with its comments removed.

	Counting a construct in a file that *documents* that construct counts the documentation,
	so the guard below would fire on the comment explaining why there is only one of them.
	Measuring the thing rather than the spelling is this project's oldest lesson about guards.
	"""

	return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", source, flags=re.S))


def _without_prose (source: str) -> str:
	"""Return some JavaScript with its comments *and the text of its strings* removed.

	`_without_comments` is the blunt version and stays that way, because its callers count
	constructs and a mangled string cannot spell one. A guard that looks for a *name* needs
	more: this app says "There is no project called …" in a message to a reader, and a scan
	for the identifier `project` cannot tell that sentence from a variable.

	So this walks the source once rather than substituting. Comments and strings are decided
	in the same pass because they decide each other — stripping `//` first turns the `https://`
	in a link into an unterminated quote, and every scan after that is reading a string it
	invented. `${…}` is kept: an interpolation is code, and this file puts real reads in one.

	**Regex literals are passed through as ordinary characters**, which is safe only because
	`app.js` has exactly one and it holds no quote, backtick or slash pair. A second one that
	does would need this to know about them; the test that uses this would fail loudly rather
	than quietly, because the source after it would be misread wholesale.

	**What is removed is blanked rather than dropped, and newlines survive** (`#684`). Every
	caller until then counted constructs, for which the two are the same; a caller that wants
	to say *where* needs an offset that still means something, and one walker answering both is
	better than a second copy of a walker this subtle. Blanking cannot create a match — a run
	of spaces spells nothing — so no existing count moves.
	"""

	kept: list[str] = []
	quote: str | None = None
	depth: list[int] = []
	index = 0

	while index < len(source):
		char = source[index]
		following = source[index + 1:index + 2]

		if quote is None:
			if char == "/" and following == "/":
				ends = source.find("\n", index)
				kept.append(" " * ((len(source) if ends < 0 else ends) - index))
				index = ends

				if index < 0:
					break

				continue

			if char == "/" and following == "*":
				ends = source.find("*/", index) + 2
				kept.append(_blanked(source[index:ends]))
				index = ends

				continue

			if char in "\"'`":
				quote, kept = char, [*kept, " "]
				index += 1

				continue

			# The braces of an interpolation, so its end is the one that matches its start
			# rather than the first `}` in a nested object or template.
			if depth and char in "{}":
				depth[-1] += 1 if char == "{" else -1

				if depth[-1] == 0:
					depth.pop()
					quote = "`"

			kept.append(char)
			index += 1

			continue

		if char == "\\":
			kept.append(_blanked(source[index:index + 2]))
			index += 2

			continue

		if char == quote:
			quote, kept = None, [*kept, " "]
			index += 1

			continue

		if quote == "`" and char == "$" and following == "{":
			quote, index = None, index + 2
			depth.append(1)
			# **The brace is kept, not blanked** (`#684`). The `}` that closes an interpolation
			# survives below as itself, so blanking the `${` that opened it leaves every
			# template contributing one unmatched brace — and a caller walking them to find an
			# enclosing block then reads spans that belong to nothing. Measured: 51 live names
			# reported as dead. Two characters either way, so no offset moves.
			kept.append(" {")

			continue

		kept.append(_blanked(char))
		index += 1

	return "".join(kept)


def _blanked (text: str) -> str:
	"""Return the same length of nothing, with the line breaks left where they were."""

	return "".join("\n" if char == "\n" else " " for char in text)


def _declared_and_never_read (source: str) -> list[tuple[int, str]]:
	"""Return every ``const`` or ``let`` in this file that nothing after it reads — `#684`.

	**No linter covers `src/subroutine/web/assets/`**: ruff is Python-only and there is no npm
	closure by decision (§22.3), so dead code, shadowed names and unreachable branches are
	invisible here in a way they are nowhere else in this repository. `#445` §5's rule is that
	the guard comes before any `package.json`, which argues for this rather than against it.

	**Block-scoped, and that is not optional.** Counting a name across the whole file finds
	`item.ref` and `row.ref` and reports every `const ref` as live — measured, and it flagged
	nothing at all. The enclosing braces are found by walking, which needs
	:func:`_without_prose` to have blanked strings and templates first, or a `}` inside a
	rendered fragment closes a block that is still open.

	**Three rules decide what counts as a read, and each was arrived at by measuring:**

	- A property access does not: `item.ref` is not a read of a local `ref`.
	- **A spread does.** ``...status`` ends in a dot and the first version rejected it, which
	  reported four live names as dead.
	- **A key does not, and a key is a name followed immediately by a colon.** ``ref: names``
	  names a field; ``mayWrite ? assign : null`` is a ternary and reads one. Excluding every
	  every ``name`` followed by whitespace and a colon flagged five live callbacks; the space
	  is what separates them, and it is a
	  dependency on this file's formatting rather than on JavaScript. Said out loud because a
	  reformat would make this lie, and the failure would be a name reported as dead.

	Destructuring is skipped — ``const {a, b} =`` matches nothing here — so this under-reports
	rather than over-reports, which is the right direction for a scan nobody can turn off.
	"""

	blanked = _without_prose(source)
	opens: list[int] = []
	spans: list[tuple[int, int]] = []

	for position, char in enumerate(blanked):
		if char == "{":
			opens.append(position)

		elif char == "}" and opens:
			spans.append((opens.pop(), position))

	def block_around (position: int) -> tuple[int, int] | None:
		"""Return the innermost braces holding this offset."""

		holding = [span for span in spans if span[0] < position < span[1]]

		return max(holding, default=None, key=lambda span: span[0])

	dead: list[tuple[int, str]] = []

	for found in re.finditer(r"(?:^|[;{}\s])(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=", blanked):
		name = found.group(1)
		at = found.start(1)
		span = block_around(at)

		if span is None:
			continue

		block = blanked[span[0]:span[1]]
		reads = [
			read
			for read in re.finditer(
				rf"(?:(?<=\.\.\.)|(?<![.\w$])){re.escape(name)}\b(?!:)", block
			)
			if span[0] + read.start() != at
		]

		if not reads:
			dead.append((blanked[:at].count("\n") + 1, name))

	return sorted(dead)


#: What a callback does when it changes *which rows there are*, as opposed to where the address
#: bar says you are — `#964`. A `set…` covers every piece of state this app holds, `now…` covers
#: `nowShowing` and `nowOpen`, and the three verbs are the ways a page refetches.
#:
#: **A prefix register rather than a list of names**, so a state added tomorrow is covered on
#: the day it is written: every one of them is `const [x, setX] = useState(…)`.
MOVES_THE_PAGE = ("set", "now", "load(", "readAgenda(", "refresh(", "retick(")


def _pushes_without_moving (source: str) -> list[tuple[int, str]]:
	"""Return every ``go(`` whose callback changes the address and nothing else — `#964`.

	> `go` writes the address bar and nothing else. **No `popstate` fires for a `pushState` we
	> made ourselves**, so a handler that stops there moves the address and leaves the page
	> exactly as it was.

	Three defects of that shape have shipped: `widen` was written with the three steps, `#959`'s
	project label shipped with one and its own browser test caught it within the hour, and
	`#962` was live from `#868` until Simon met it. **Nothing cheaper can see any of them** —
	every one of these callbacks lives in `App`, which the render harness cannot call at all
	(`#640`), so each has cost a browser test.

	**Two shapes, and the second is the one that matters.** A braced callback is checked for
	doing anything from :data:`MOVES_THE_PAGE`; a brace-less arrow — ``() => go("/")`` — is
	flagged outright, because its whole body *is* the push. **That is what `#962` actually
	was**, and the first version of this walked straight past it looking for braces that were
	never there.

	**What it cannot do, stated rather than discovered.** It answers *does this handler change
	anything*, never *does it change the right thing* — a callback that resets the wrong piece
	of state passes. The item says as much, and a scan that claimed otherwise would be worse
	than this one: `#405`'s rule is that a guard is tested by feeding it a defect through its
	own entry point, and the defect this admits to missing is one nobody can express.
	"""

	blanked = _without_prose(source)
	opens: list[int] = []
	spans: list[tuple[int, int]] = []

	for position, char in enumerate(blanked):
		if char == "{":
			opens.append(position)

		elif char == "}" and opens:
			spans.append((opens.pop(), position))

	def callback_around (position: int) -> tuple[int, int] | None:
		"""Return the innermost braces that are a function body rather than an object."""

		holding = sorted(
			(span for span in spans if span[0] < position < span[1]),
			key=lambda span: span[0],
			reverse=True,
		)

		return next(
			(
				span
				for span in holding
				if re.search(r"(=>|\))\s*$", blanked[max(0, span[0] - 40):span[0]])
			),
			None,
		)

	bare: list[tuple[int, str]] = []

	for pushed in re.finditer(r"(?<![.\w$])go\(", blanked):
		line = blanked[:pushed.start()].count("\n") + 1

		if re.search(r"=>\s*$", blanked[max(0, pushed.start() - 20):pushed.start()]):
			bare.append((line, "a brace-less arrow whose whole body is the push"))

			continue

		body = callback_around(pushed.start())

		if body is None:
			bare.append((line, "a push in no callback at all"))

			continue

		if not any(move in blanked[body[0]:body[1]] for move in MOVES_THE_PAGE):
			bare.append((line, "a callback that pushes the address and changes nothing"))

	return sorted(bare)


def test_a_navigation_that_changes_which_rows_there_are_also_loads_them () -> None:
	"""`#964`. Three defects of one shape, and the third was found by Simon rather than a test.

	Measured while building this, which is the item's own instruction — *the first hour is
	measuring whether the cheap half has false positives on this file, not writing it*: it
	flags **nothing** on the file as it stands, and it flags both known shapes when they are
	put back. The record of what it cannot see is in `_pushes_without_moving`.
	"""

	bare = _pushes_without_moving(_served_modules()["app.js"])

	assert not bare, "the address moves and the page does not: " + ", ".join(
		f"line {line} — {why}" for line, why in bare
	)


def test_nothing_in_the_browser_app_is_declared_and_never_read () -> None:
	"""`#684`, finding 7 of review `#677`. A reader found it; nothing else could have.

	`const ref = Number(last)` sat in `parseAddress` assigned and never read — `names`,
	computed two lines later, is what the function returns. Trivial on its own, and what it
	said about its surroundings is the item: **this file has no linter of any kind**.

	Measured while building this, which is the whole of the item's *"weigh the cheap version"*:
	the scan flags **one** name on the file as it stands and it is that one, with no false
	positives. Three earlier versions of it reported four, five and six, every one of them
	live — the record is in `_declared_and_never_read`, because each wrong answer was a rule
	about JavaScript that reading would not have supplied.
	"""

	dead = _declared_and_never_read(_served_modules()["app.js"])

	assert not dead, (
		"declared and never read in app.js: "
		+ ", ".join(f"line {line}: {name}" for line, name in dead)
	)


#: Documents that are *finished* and correct, used as a source of half-written ones — `#776`.
#:
#: **A preview renders incomplete Markdown, which stored prose never is.** Everything the
#: renderer has ever been asked came from a database, so it was whole; somebody typing meets
#: every state on the way to it — an unclosed fence, a lone bracket, a table with one pipe, a
#: heading with no text. That is the half of this item that is not one call.
ON_THE_WAY = [
	"# A heading\n\nSome prose with a [link](https://example.com) in it.\n",
	"```python\nprint('hello')\n```\n\nAfter the fence.\n",
	"| one | two |\n| --- | --- |\n| a | b |\n",
	"- first\n- second\n  - nested\n\n> quoted\n> across two lines\n",
	"Some **bold** and `code` and a #42 mention, then a\n\n---\n\nrule.\n",
]


def test_every_prose_box_offers_a_preview () -> None:
	"""Three boxes want it — a task's description, a document's body, and a comment (`#776`).

	**Asserted over the source, and the reason is the same one `#964` gives**: the wiring is in
	`App`, which uses hooks, so `tests/dom.js` cannot call it (`#640`). What is checked is that
	no ``<textarea`` is left standing outside `Written` — a box added later without one would
	be the only one in the app that could not be read back, silently.
	"""

	# **`_without_comments`, not `_without_prose`**: the markup lives inside template
	# literals, and the version that blanks strings blanks every element with them — measured,
	# it found no textareas at all and the guard passed for the wrong reason.
	source = _without_comments(_served_modules()["app.js"])
	boxes = re.findall(r"<textarea", source)
	written = re.findall(r"<\$\{Written\}", source)

	assert len(boxes) == 1, (
		f"{len(boxes)} textareas are declared outside `Written`, so a prose box exists that "
		f"cannot be previewed"
	)
	assert len(written) >= 3, f"only {len(written)} prose boxes go through `Written`"


def test_a_prose_box_can_be_read_as_it_will_look (tmp_path: pathlib.Path) -> None:
	"""`#776`, Simon's suggestion of 2026-08-10.

	**Cheap, because the renderer is already ours and already pure.** ``markdown.render`` is a
	function from text to a string — which is what let 25 hostile payloads be fed through its
	own entry point in `#637` — so a preview is that call plus somewhere to put the answer.

	**The box stays mounted when the preview is showing**, and that is the assertion that
	matters. Swapping it out drops an *uncontrolled* field, so coming back would show
	``defaultValue`` — the stored text rather than what somebody has been typing, which is the
	loss `#757` chose ``defaultValue`` to prevent, arriving from a friendlier direction.
	"""

	writing, previewing = [
		rendered["Written"]
		for rendered in (
			_rendered(tmp_path, {"Written": {
				"name": "description", "label": "Description", "rows": "3",
				"value": "A **bold** claim.", "onPreviewing": True,
			}}),
			_rendered(tmp_path, {"Written": {
				"name": "description", "label": "Description", "rows": "3",
				"value": "A **bold** claim.", "onPreviewing": True,
				"previewing": {"name": "description", "text": "A **bold** claim."},
			}}),
		)
	]

	assert "<textarea" in writing and "<strong>" not in writing, (
		f"the box is previewing before anybody asked: {writing}"
	)
	assert "Preview" in writing, "there is no way to ask for one"

	assert "<strong>bold</strong>" in previewing, (
		f"the preview is not rendered as Markdown: {previewing}"
	)
	assert "<textarea" in previewing, (
		"the box was unmounted, so going back to it loses what was being typed"
	)
	assert "Write" in previewing, "there is no way back to the box"


def test_only_the_box_that_was_asked_about_previews (tmp_path: pathlib.Path) -> None:
	"""One answer for the page, so opening a second box closes the first.

	Two previews at once is a state nobody asked for, and the state lives in `App` for the
	reason `Adding`'s own comment gives — no form component here keeps its own, so the render
	harness can call every one of them (`#640`).
	"""

	[other] = _views_of_written(tmp_path, "body", {"name": "description", "text": "elsewhere"})

	assert "<textarea" in other and "elsewhere" not in other, (
		f"a box previewed something asked about another one: {other}"
	)


def _views_of_written (
	tmp_path: pathlib.Path, name: str, previewing: dict[str, str] | None
) -> list[str]:
	"""Render one prose box, in one state."""

	return [
		_rendered(tmp_path, {"Written": {
			"name": name, "label": "What it says", "rows": "3", "value": "stored",
			"onPreviewing": True, "previewing": previewing,
		}})["Written"]
	]


def test_prose_being_written_renders_at_every_length (tmp_path: pathlib.Path) -> None:
	"""`#776`. Every prefix of a valid document, because that is what typing looks like.

	The renderer has only ever been handed *stored* text, which is whole by the time it gets
	there. A preview is the first thing to ask it about a document that is not finished yet,
	and `#679`/`#680` are why that is worth a test rather than an assumption: ``markdown.blocks``
	overflowed the stack at 3,360 nested blockquotes, and **typing ``>>>>>>…`` is a thing a
	person does**.

	Prefixes rather than whole documents, which is the case none of the existing generated
	inputs builds: `HOSTILE` is a table of finished attacks and `ON_THE_WAY` is a table of
	finished prose, and what is checked here is everything in between.
	"""

	prefixes = [
		whole[:length]
		for whole in ON_THE_WAY
		for length in range(1, len(whole) + 1)
	]

	assert len(prefixes) > 250, f"only {len(prefixes)} prefixes, so this is barely a scan"

	for source, html in zip(prefixes, _markdown(tmp_path, prefixes), strict=True):
		assert _tags(html) <= EMITTED_TAGS, (
			f"{source!r} produced {_tags(html) - EMITTED_TAGS}"
		)
		assert "<script" not in html.lower(), f"{source!r} produced a script element"


def test_prose_being_written_survives_what_a_person_leans_on (
	tmp_path: pathlib.Path,
) -> None:
	"""The other half, and it is the one `#679` actually met.

	A prefix of a valid document is well-behaved by construction. What is not is a key held
	down — ``>>>>>>…``, ``####…``, ``[[[[…`` — which nobody would ever *store* and everybody
	produces on the way to something. ``markdown.blocks`` is capped at 32 levels for exactly
	this, and the cap has never been asked about from the surface that reaches it.
	"""

	leaned = [character * 200 for character in (">", "#", "[", "*", "`", "-", "|", "_")]

	for source, html in zip(leaned, _markdown(tmp_path, leaned), strict=True):
		assert _tags(html) <= EMITTED_TAGS, (
			f"{source[:8]!r}… produced {_tags(html) - EMITTED_TAGS}"
		)


def test_stored_text_cannot_become_markup (tmp_path: pathlib.Path) -> None:
	"""**The one that matters.** Every hostile source, through the real renderer.

	This page holds a session cookie, and a description is written by anybody with a
	credential — including every agent, whose output is whatever it read somewhere. So the
	question is not whether our own prose renders nicely; it is whether somebody else's can
	reach the browser as anything but text.
	"""

	# `strict=` because a short answer would otherwise end the loop early and every payload
	# past that point would go unchecked, silently — on the one test that has to be right.
	for source, html in zip(HOSTILE, _markdown(tmp_path, HOSTILE), strict=True):
		assert _tags(html) <= EMITTED_TAGS, f"{source!r} produced {_tags(html) - EMITTED_TAGS}"
		assert "<script" not in html.lower(), f"{source!r} produced a script element"

		for element in _elements(html):
			assert not re.search(r"\son[a-z]+\s*=", element, re.I), (
				f"{source!r} produced an event handler in {element!r}"
			)

		for href in re.findall(r'href="([^"]*)"', html):
			# **Read the way a browser reads it** (`#682`). A browser ignores control characters
			# inside a destination, so `\u0001//evil.example` resolves off-origin — and the
			# version of this loop that tested `href` directly could not see that, because the
			# string does not *start* with `//`. It had exactly the defect it was guarding
			# against: a check shaped around the same assumption as the code it checks.
			# **And a backslash is a slash to a browser** (`#927` H-16). This loop stripped
			# controls and stopped there, so `/\evil.example` did not *start* with `//` here
			# either — the guard carrying the same blind spot as the code, which is the very
			# thing the paragraph above says it was rewritten to stop doing. Measured against
			# the WHATWG parser: `/\`, `\\` and `\/` all resolve off-origin.
			bare = re.sub(r"[\x00-\x20]", "", href).replace("\\", "/")
			scheme = bare.split(":", 1)[0].lower() if ":" in bare else ""

			assert scheme in ("", "http", "https", "mailto"), f"{source!r} linked to {href!r}"
			assert not bare.startswith("//"), f"{source!r} linked off-origin as {href!r}"


def test_a_refused_link_shows_what_was_written (tmp_path: pathlib.Path) -> None:
	"""A destination we will not follow is rendered as its source, not quietly dropped.

	Dropping the destination and keeping the text would tell the reader there was never a
	link there, which is a false statement about what somebody wrote — and it is the reading
	under which a suspicious link becomes invisible rather than obvious.
	"""

	[rendered] = _markdown(tmp_path, ["[click](javascript:alert(1))"])

	assert "<a " not in rendered
	assert "javascript:alert(1)" in rendered, "the destination stopped being visible"


#: The ways of writing *another host* that a browser reads as one and the eye reads as a path.
#:
#: All three resolve to ``https://evil.example/steal`` against a page on this instance, measured
#: with the WHATWG URL parser. They are here as a table rather than folded into ``HOSTILE``
#: because the assertion is different in kind: ``HOSTILE`` proves nothing became *markup*, and
#: this proves a destination was refused — a payload that silently stopped being parsed as a
#: link at all would satisfy the first and say nothing about the second.
AUTHORITY_RELATIVE = [
	"//evil.example/steal",
	"/\\evil.example/steal",
	"\\\\evil.example/steal",
	"\\/evil.example/steal",
	"\u0001//evil.example/steal",
	"\u0001/\\evil.example/steal",
]


@pytest.mark.parametrize("destination", AUTHORITY_RELATIVE)
def test_a_destination_naming_another_host_is_refused (
	tmp_path: pathlib.Path, destination: str
) -> None:
	"""`#927`'s H-16 — three spellings of another host were returned as this instance's path.

	``target`` refused ``//`` and nothing else, and a backslash survives control-stripping. So
	``/\\evil.example/x`` began with ``/``, did not begin with ``//``, and came back unchanged —
	while a browser resolves it off-origin, because the URL parser treats ``\\`` as ``/`` in the
	relative-slash state. Anyone who can write a comment could plant one that reads as internal.

	**Both branches, which is why one refusal could not have closed it**: ``/\\`` took the
	is-a-path branch and ``\\\\`` took neither, so they were wrong in two different places.

	The parametrisation is over spellings rather than a loop inside one test, so a spelling that
	starts getting through fails on its own name.
	"""

	[rendered] = _markdown(tmp_path, [f"[click]({destination})"])

	assert "<a " not in rendered, f"{destination!r} was rendered as a link"

	# And `#682`'s rule: the destination is still on the page, because a refusal that hid it
	# would make a suspicious link invisible rather than obvious.
	assert "evil.example" in rendered


def test_an_ordinary_path_is_still_a_link (tmp_path: pathlib.Path) -> None:
	"""The other half of H-16's fix, and the reason it is a normalisation rather than a ban.

	A *single* leading backslash resolves to a path on this instance and nobody else's, so
	refusing it would be refusing an ordinary destination for how it looks. Without this, the
	cheap fix — reject anything containing a backslash — passes every test above.
	"""

	for destination in ("/ordinary/path", "\\ordinary/path", "#fragment", "?q=1"):
		[rendered] = _markdown(tmp_path, [f"[click]({destination})"])

		assert "<a " in rendered, f"{destination!r} stopped being a link"


def test_an_external_link_cannot_reach_back (tmp_path: pathlib.Path) -> None:
	"""Every anchor carries `rel="noopener noreferrer"`, which is half of why links are safe."""

	[rendered] = _markdown(tmp_path, ["[docs](https://example.com/x) and https://example.com/y"])

	assert rendered.count("<a ") == 2, "the bare address was not linked"
	assert rendered.count('rel="noopener noreferrer"') == 2


def test_what_looks_like_a_tag_is_shown_rather_than_swallowed (tmp_path: pathlib.Path) -> None:
	"""**Escaping is the correct rendering here, not a safety tax.**

	103 placeholders written as `<ref>`, `<path>`, `<workspace>` and the like appear in this
	instance's own prose. A renderer that passed HTML through would hand each of them to the
	browser as an unknown element, and the reader would see a gap where the word was.
	"""

	[rendered] = _markdown(tmp_path, ["Pass <workspace>/<ref> as the address."])

	assert "&lt;workspace&gt;/&lt;ref&gt;" in rendered


def test_an_underscore_in_a_name_is_left_alone (tmp_path: pathlib.Path) -> None:
	"""Emphasis is asterisks only, and 205 intraword underscores in this instance say why.

	`assignee_id`, `project_scope` and `next_ref_number` are written in ordinary prose without
	backticks. Underscore emphasis would have to implement CommonMark's flanking rules to
	leave them alone; not implementing it reaches the same answer with nothing to get wrong.
	"""

	[rendered] = _markdown(tmp_path, ["assignee_id and project_scope and __all__"])

	assert "<em>" not in rendered and "<strong>" not in rendered
	assert "assignee_id and project_scope and __all__" in rendered


def test_a_ref_at_the_start_of_a_line_is_not_a_heading (tmp_path: pathlib.Path) -> None:
	"""`#42` is how everything here is addressed, and it appears 2,196 times in this prose.

	A heading needs a space after its hashes; a ref never has one. That is what keeps the
	commonest thing anybody writes from becoming an `h3` whenever it starts a line.
	"""

	[rendered] = _markdown(tmp_path, ["#637 is the item\n\n# A real heading"])

	assert "<p>#637 is the item</p>" in rendered
	assert "<h3>A real heading</h3>" in rendered


def test_every_construct_the_backlog_uses_renders (tmp_path: pathlib.Path) -> None:
	"""The measured subset, each with the tag it has to produce.

	Taken from what this instance actually contains rather than from a specification: over 291
	descriptions and documents, these are the constructs that appear. Tables are on the list
	because 20% of the prose here has one, which is why a renderer without them was refused.
	"""

	wanted = {
		"# Heading": "<h3>",
		"**bold**": "<strong>",
		"*italic*": "<em>",
		"~~struck~~": "<del>",
		"`code`": "<code>",
		"```\nfenced\n```": "<pre>",
		"    indented": "<pre>",
		"- one\n- two": "<ul>",
		"1. one\n2. two": "<ol>",
		"> quoted": "<blockquote>",
		"---": "<hr>",
		"| a | b |\n| --- | --- |\n| 1 | 2 |": "<table>",
		"[a](https://example.com)": "<a ",
		"plain words": "<p>",
	}

	rendered = _markdown(tmp_path, list(wanted))

	for (source, expected), html in zip(wanted.items(), rendered, strict=True):
		assert expected in html, f"{source!r} rendered as {html!r}, with no {expected}"


def test_the_renderer_is_the_only_way_html_is_injected () -> None:
	"""One trust boundary, and a test that says so.

	`dangerouslySetInnerHTML` is the only way markup can reach this page, so the argument that
	the app is safe is exactly the argument that `markdown.render` is — and that argument only
	holds while there is one call site. A second would be a second thing to be sure about.
	"""

	app = _without_comments(_served_modules()["app.js"])

	assert app.count("dangerouslySetInnerHTML") == 1, "a second injection point has appeared"
	assert "markdown.render" in app, "the one injection point stopped going through the renderer"


# ---- writing from a browser (`SR#597`) -----------------------------------------------------


def test_only_a_task_offers_to_be_completed (tmp_path: pathlib.Path) -> None:
	"""A document cannot be completed, so it is not offered the control.

	One counter per workspace serves tasks and documents (§6.2), so a listing holds both — and
	a button whose only possible outcome is a refusal is worse than no button. Same rule as
	`subroutine done` turning a document down by name rather than pretending it is missing.
	"""

	task = _rendered(tmp_path, {
		"Row": {"item": {"ref": 1, "kind": "task", "title": "A task"}, "showKind": True}
	})
	document = _rendered(tmp_path, {
		"Row": {"item": {"ref": 2, "kind": "document", "title": "A document"}, "showKind": True}
	})

	assert "Complete" in task["Row"], "a task was not offered completion"
	assert "Complete" not in document["Row"], "a document was offered completion"


def test_a_reader_who_cannot_write_is_shown_no_controls (tmp_path: pathlib.Path) -> None:
	"""The handlers are what say whether this reader may act, and their absence is the answer.

	It matters because the write half arrived after the read half: a surface that renders its
	buttons regardless and refuses on press would be the same defect as `SR#515` — effort spent
	confirming the wrong conclusion.
	"""

	rendered = _rendered(tmp_path, {
		"Row": {"item": {"ref": 1, "kind": "task", "title": "A task"}, "onComplete": None},
		"Listing": {"items": [{"ref": 1, "kind": "task", "title": "A task"}], "onAdd": None},
	})

	assert "Complete" not in rendered["Row"]
	assert "<form" not in rendered["Listing"], "an add box appeared with nothing to add through"


@pytest.mark.parametrize("category", sorted(subroutine.domain.tasks.FINISHED_CATEGORIES))
def test_a_finished_task_is_not_asked_to_finish_again (
	tmp_path: pathlib.Path, category: str
) -> None:
	"""Neither on the page that shows one item, nor on the rows of a listing — `#724`.

	`status_category` is the fixed field a client may branch on; the status key is renameable.

	**Parametrised over the instance's own set rather than over the word `done`**, which is what
	both copies of this rule got wrong in different ways. `Doing` named `done` and so offered a
	*cancelled* task both its controls; `Row` asked only whether the item was a task and so put a
	**Complete** button on every card in the board's *Done* column, where pressing it moves the
	record of when the work finished (`#723`).

	Sorted so the cases are named stably, and the set is measured for emptiness by
	``test_the_browser_and_the_instance_agree_on_what_finished_means`` — a parametrisation over an
	empty constant produces zero cases, and "no cases failed" reads exactly like "the guard ran".
	"""

	item = {"ref": 42, "kind": "task", "title": "Over already",
		"status_category": category, "assignee": None}

	rendered = _rendered(tmp_path, {
		"Doing": {"item": item, "members": ["si"]},
		"Row": {"item": item, "showKind": False},
	})

	assert rendered["Doing"] == "", f"a {category} task still offered to be finished"
	assert "Complete" not in rendered["Row"], f"a {category} row still offered to be finished"


def test_an_unfinished_row_still_offers_the_control (tmp_path: pathlib.Path) -> None:
	"""The other half, without which the test above passes on a page that offers nothing at all.

	`#405`'s rule in its cheapest form: a refusal test proves only that something is absent, and
	absence is also what a broken component produces.
	"""

	rendered = _rendered(tmp_path, {
		"Row": {"item": {"ref": 1, "kind": "task", "title": "Still going",
			"status_category": "in_progress"}, "showKind": False},
	})

	assert "Complete" in rendered["Row"], "an open task was not offered completion"


def test_the_browser_and_the_instance_agree_on_what_finished_means () -> None:
	"""The browser holds its own copy of `FINISHED_CATEGORIES`, and this is what makes that safe.

	It is a copy on purpose: `status_category` is published to clients precisely so they may
	branch on it, and the alternative — asking the instance whether each row is finished — is a
	request per row for something already in the row. What is not safe is a copy nothing compares,
	which is this codebase's signature defect and cost eleven sites on `#508`.

	**Set equality, so it fails in both directions.** A category added to the instance and not to
	the browser leaves finished work offering to be finished; one added to the browser alone hides
	the control on work that is still open, which is the more expensive way round.
	"""

	source = _served_modules()["app.js"]
	found = re.search(r"const FINISHED = new Set\(\[([^\]]*)\]\)", source)

	assert found, "the browser's set of finished categories could not be read from app.js"

	written = set(re.findall(r'"([^"]+)"', found.group(1)))

	assert written == set(subroutine.domain.tasks.FINISHED_CATEGORIES), (
		f"the browser calls {sorted(written)} finished and the instance calls "
		f"{sorted(subroutine.domain.tasks.FINISHED_CATEGORIES)} finished"
	)


def test_the_add_box_teaches_the_capture_grammar (tmp_path: pathlib.Path) -> None:
	"""**The only place a browser-only reader can learn that §6.13 exists.**

	`SR#484` settled that the grammar has exactly one delivery channel per surface and that a
	surface without one silently stops using it. At a terminal that is `subroutine explain
	capture`; here there is no terminal, so it is the placeholder or nothing.
	"""

	rendered = _rendered(tmp_path, {"Adding": {}})["Adding"]

	assert "<form" in rendered and "<input" in rendered and "<button" in rendered

	# The placeholder is read off the source, because `flatten` walks the tree rather than
	# rendering attributes — so the assertion has to go where the words actually are.
	#
	# **Read from the named constant**, which is what it is for. It was anchored on
	# `name="text"` until `SR#761` gave that attribute a ternary — a document's title asks a
	# different question — and a regex over the markup would then find whichever branch was
	# written first and check the wrong one.
	source = _served_modules()["app.js"]
	placeholder = re.search(r'export const CAPTURE_HINT = "([^"]+)";', source)

	assert placeholder is not None, "the add box stopped saying what can be typed into it"
	assert "+" in placeholder.group(1) and "!" in placeholder.group(1), (
		f"the placeholder {placeholder.group(1)!r} no longer shows any of the grammar"
	)


def test_the_capture_box_is_the_same_box_whether_the_form_is_open_or_not (
	tmp_path: pathlib.Path,
) -> None:
	"""§1.4, which `SR#756` is the first real test of in this app.

	*No entity from §14 or §15 may ever be required to create, find or complete a task.* A form
	carrying a type, a status and a project is exactly such an entity, so it is a **disclosure**:
	the one line stays, stays first, and stays the only required control. A form that replaced
	the box — or moved it, or made one of its own fields required — would be the thing §1.4
	forbids while looking like an improvement.

	Both states are rendered rather than reasoned about, because *"the box is still there"* is a
	claim about a tree and this harness can walk one.
	"""

	shut = _rendered(tmp_path, {"Adding": {"expanded": False, "onExpand": True}})["Adding"]
	open_ = _rendered(tmp_path, {"Adding": {"expanded": True, "onExpand": True}})["Adding"]

	for markup in (shut, open_):
		assert markup.index("<input") < markup.index("<button"), (
			"the capture box is no longer the first control in the form"
		)

	assert "<fieldset" not in shut, "the form's fields are showing before anybody asked for them"
	assert "<fieldset" in open_, "asking for the fields did not produce any"

	# **Both states render the same single required control.** `Adding` and `Editing` share
	# `Fields` since `SR#757`, so the count below is over `Adding` alone and the title input
	# that `Editing` requires is deliberately outside it.

	# **Nothing in the disclosure is required.** `required` on a disclosed field would make the
	# form refuse to submit for a reason the reader cannot see while it is shut.
	#
	# Comments are stripped first, or this counts the paragraphs explaining the rule — which is
	# five hits and a guard that measures its own documentation. **`_without_comments` and not
	# `_without_prose`**: the attribute lives inside a template literal, and the version that
	# empties strings takes the thing being counted with it. Measured, after writing the wrong
	# one first — it reported zero, which reads exactly like the box losing its `required`.
	source = _without_comments(_served_modules()["app.js"])
	form = source[source.index("export function Adding ("):source.index("export function Editing (")]

	assert form.count("required") == 1, (
		"something other than the capture line is required, so the one-line path can be blocked "
		"by a control that is not on screen"
	)


def test_a_control_nobody_touched_is_not_sent (tmp_path: pathlib.Path) -> None:
	"""`SR#756`'s only real rule, and the one that would have shipped broken.

	A form's untouched control gives an empty string, and this endpoint refuses those **by
	name** — measured against the served instance on 2026-08-10:

	| sent | answered |
	| --- | --- |
	| `assignee: ""` | *There is nobody called ''* |
	| `type: ""` | *No task type with key ''* |
	| `estimate: ""` | *A duration cannot be empty* |
	| `title: ""` beside a `text` | *A title is required* |

	So a body assembled by copying the controls is refused by whichever field the reader left
	alone first — which is every field, on the commonest submission there is. `_calls` drives
	exactly that submission against a real instance; this says what the body should be.
	"""

	[body] = _views(tmp_path, [("filed", {"slug": "projects", "values": {
		"text": "buy milk",
		"description": "", "project": "", "type": "", "status": "", "assignee": "",
		"importance": "", "urgency": "", "estimate": "",
		"starts": "", "snooze": "", "due": "", "tags": "",
	}})])

	assert body == {"workspace_id": "projects", "text": "buy milk"}, (
		f"an untouched form sends {sorted(body)} when it should send the line and nothing else"
	)


def test_a_line_and_a_form_are_one_submission (tmp_path: pathlib.Path) -> None:
	"""What makes the form a disclosure rather than a second way in.

	`POST /v1/tasks` takes `text` **and** structured fields, and anything explicit wins over what
	the line said. Measured: `text: "… !4/3 ~1h #typed"` with `importance: 5` and `estimate:
	"30m"` stored importance 5, estimate 30m, urgency 3 from the line, and the tag from the line.

	That is why the capture box does not have to move, be duplicated, or grow a rival title
	field: it stays the title, doing exactly what it did, and the form adds the rest.

	**Numbers are sent as numbers** (`SR#549`). `{"today": "false"}` was truthy in Python and a
	filter came on — a plausible, complete, wrong answer — because a published schema was never
	used as a schema. `Create` declares these `int | None`; a parser that coerces `"4"` is a
	thing that happens to work rather than one that is promised.
	"""

	[body] = _views(tmp_path, [("filed", {"slug": "projects", "values": {
		"text": "fix the header !2/2 ~1h",
		"importance": "5", "urgency": "1", "estimate": "30m",
		"type": "bug", "due": "2026-08-14", "tags": " #health,, admin  ",
	}})])

	assert body["text"] == "fix the header !2/2 ~1h", "the line is no longer sent as the line"
	assert body["importance"] == 5 and body["urgency"] == 1, (
		f"importance is {body['importance']!r}, which is a string where the schema says integer"
	)

	# **The `#` is optional and the separators are either**, because that is how a person writes
	# a list of tags. The sigil belongs to the capture line; typing it here should not make a tag
	# called `#health`.
	assert body["tags"] == ["health", "admin"], f"tags parsed as {body['tags']}"


def test_an_edit_clears_what_a_creation_would_omit (tmp_path: pathlib.Path) -> None:
	"""`SR#757`, and it is the **opposite** rule from `SR#756`'s.

	Creating omits an empty control, because `POST /v1/tasks` refuses an empty string by name.
	Editing must send **`null`**, because §8.3 says a field left out is *unchanged* and only an
	explicit null clears it.

	**Reusing `filed` here would make clearing a deadline impossible**: blank the box, the field
	is omitted, the deadline stays, and the form reports success. A silent no-op is the worst of
	the three failures available — worse than a refusal, which a person can act on, and worse
	than a wrong value, which they can see.

	The measurement behind it, on the served instance: `{"due": null}` clears the deadline,
	`{"tags": []}` clears the tags, and every other clearable field takes null the same way.
	"""

	item = {"ref": 42, "version": 7, "title": "Was", "timezone": "Etc/UTC"}

	[body] = _views(tmp_path, [("edited", {"item": item, "values": {
		"title": "Now", "status": "open", "type": "task", "project": "web",
		"description": "", "assignee": "", "importance": "", "urgency": "", "estimate": "",
		"starts": "", "snooze": "", "due": "", "tags": "",
	}})])

	assert body["due"] is None and body["starts"] is None and body["snooze"] is None, (
		"a blanked date was omitted rather than cleared, so clearing one does nothing"
	)
	assert body["description"] is None and body["assignee"] is None
	assert body["importance"] is None and body["urgency"] is None and body["estimate"] is None
	assert body["tags"] == []

	# **The four a task must have are never nulled.** Every control that carries one always
	# holds a value, and `null` would mean *clear it* to a route that cannot.
	assert body["title"] == "Now" and body["status"] == "open"
	assert body["type"] == "task" and body["project"] == "web"

	# **§8.9, and the whole reason this item is `!4/4`.** `expected_version` is opt-in and
	# `None` means *did not ask* rather than *asked and passed* — so a form omitting it wins
	# silently over whatever somebody saved while it was open.
	assert body["expected_version"] == 7, (
		"the edit did not say which version it was based on, so it will overwrite a save it "
		"never saw"
	)


def test_a_form_opens_holding_what_the_item_already_says (tmp_path: pathlib.Path) -> None:
	"""`SR#757`. An edit form that starts empty is a delete with extra steps.

	**Every date goes through the item's own timezone** (`SR#773`). An all-day deadline is
	stored at the last instant of its day in the task's zone, so reading it any other way puts
	the day *after* the deadline into the box — and then saving moves it. That is a display bug
	becoming data loss the moment a form is filled from the same value, which is why `SR#773`
	was fixed before this was built rather than after.
	"""

	[held] = _views(tmp_path, [("fromItem", {"item": {
		"ref": 42, "version": 3, "title": "A task", "description": "why",
		"project_key": "web", "type": "bug", "status": "open", "assignee": "si",
		"importance": 4, "urgency": 3, "estimate_human": "90m",
		# Stored in Los Angeles, so the task's day and the UTC day are different numbers.
		"timezone": "America/Los_Angeles",
		"due_at": "2126-08-16T06:59:59.999999Z",
		"snoozed_until": "2126-08-10T07:00:00Z",
		"starts_at": "2126-08-12T21:30:00Z",
		# **Stated rather than left out**, because `timeFor` tests `allDay !== false`: an item
		# that never says is treated as a whole day, so an appointment would lose its clock.
		"starts_is_all_day": False,
		"tags": ["health", "admin"],
	}})])

	assert held["due"] == "2126-08-15", (
		f"the form opened on {held['due']}, which is not the day the deadline is on"
	)
	assert held["snooze"] == "2126-08-10"

	# **Read back where the task lives, not where the reader is** (`#773`). 21:30 UTC is the
	# 12th in Los Angeles and the 13th in UTC, so a form opening on the wrong one would move
	# somebody's appointment by a day every time they edited anything else.
	assert held["starts"] == "2126-08-12", "a start was read in the wrong zone"
	assert held["starts_time"] == "14:30"

	# **Everything as the string a control holds**, because that is what comes back out of one.
	assert held["importance"] == "4" and held["urgency"] == "3"
	assert held["tags"] == "health, admin"
	assert held["estimate"] == "90m"
	assert held["project"] == "web" and held["assignee"] == "si"

	# **A field nobody set opens empty rather than absent**, so the control renders and the
	# reader can fill it in — the opposite of §12.2c's display rule, and deliberately so.
	[bare] = _views(tmp_path, [("fromItem", {"item": {"ref": 1, "version": 1, "title": "x"}})])

	assert set(bare.values()) == {""}, f"an unset field opened holding {bare}"


def test_a_search_is_free_text_and_is_still_bounded (tmp_path: pathlib.Path) -> None:
	"""`SR#775`. The first selection parameter whose values cannot be enumerated.

	`SR#738` put a bound on the address so it could never become a passthrough to
	`api/query.py`: a name the browser does not know is refused here rather than forwarded, and
	each name's values are a list. **A search term has no list**, so admitting it relaxes half
	of that — and the half it relaxes is worth saying out loud rather than adding a key and
	hoping.

	**What still holds is the part that matters**: a selection may only be one the caller could
	have sent anyway, and may never widen what a credential can see. `q` *narrows* —
	`domain/search.matching` is an extra predicate on a query `domain/scoping` has already
	narrowed — so any value admits nothing a reader could not already read.

	**An empty search is refused rather than sent**, which is `domain/search.terms`' own rule
	said on this side: a query with no words narrows nothing, and `q=" "` was once a real
	filter matching every row containing a space.
	"""

	free, blank, spaces, listed, wrong, unknown = _views(tmp_path, [
		("permits", {"name": "q", "value": "anything at all"}),
		("permits", {"name": "q", "value": ""}),
		("permits", {"name": "q", "value": "   "}),
		("permits", {"name": "status_category", "value": "done"}),
		("permits", {"name": "status_category", "value": "finished"}),
		("permits", {"name": "colour", "value": "red"}),
	])

	assert free is True, "a search term was refused, so nothing can be searched for"
	assert blank is False and spaces is False, (
		"an empty search would be sent as a filter, which is a question nobody put"
	)

	# **The enumerated rule is untouched**, which is what stops this being a relaxation of the
	# whole bound rather than of one entry.
	assert listed is True
	assert wrong is False, "a value outside its list was admitted"
	assert unknown is False, "a name the browser does not know became a passthrough"

	# And it survives the round trip through an address, which is where it is actually used.
	#
	# **An unknown name is ignored rather than refused**, and that is `selectionOf`'s existing
	# shape rather than something this changed: it walks the names it knows and never looks at
	# the rest, so nothing unknown can reach the query layer whatever `permits` would say about
	# it. `permits` refusing one is the belt to that brace.
	[read] = _views(tmp_path, [("selectionOf", "?q=render%20the%20backlog&colour=red")])

	assert read == {"selection": {"q": "render the backlog"}, "refused": []}


def test_a_document_is_offered_only_the_fields_a_document_has (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#761`. A document is a different shape from a task.

	It has a title, prose, a type and a status, and **no priority, no dates, no estimate and no
	assignee**. Handing it the task form would offer eight fields it cannot have, which is
	§12.2a's *a column that says the same thing on every row* wearing a different costume.

	**The kind is a control inside the disclosure, not a third button beside the box.** A
	document is not what a to-do list is for, so it stays behind the same *More* a task's other
	fields are behind — and the collapsed state, which is the one §1.4 protects, gains nothing.
	"""

	vocabulary = {
		"item_types": {"document": [
			{"key": "note", "label": "Note", "is_default": True},
			{"key": "decision", "label": "Decision"},
		]},
		"statuses": {"document": [
			{"key": "draft", "label": "Draft", "is_default": True},
			{"key": "active", "label": "Active"},
		]},
	}

	paper = _rendered(tmp_path, {"DocumentFields": {
		"busy": False, "vocabulary": vocabulary, "projects": [], "values": {},
	}})["DocumentFields"]

	assert "Decision" in paper and "Active" in paper, (
		"the document form's vocabulary is not this workspace's"
	)

	for absent in ("Importance", "Urgency", "Estimate", "Assignee", "Due", "Planned",
			"Hidden until", "Tags"):
		assert absent not in paper, (
			f"the document form offers {absent!r}, which a document does not have"
		)

	# **The disclosure carries the choice, and the collapsed box does not.**
	# **`onWriting` is passed explicitly.** The harness supplies every handler it finds written
	# as `onX=` in the source, and this one reaches `Adding` through a spread — so it is absent
	# unless said, and the control would be missing for a reason that is not the code's.
	shut = _rendered(tmp_path, {"Adding": {"expanded": False, "onWriting": True}})["Adding"]
	open_ = _rendered(tmp_path, {"Adding": {"expanded": True, "onWriting": True}})["Adding"]

	assert "A document" not in shut, "the collapsed box grew a control §1.4 protects it from"
	assert "A document" in open_ and "A task" in open_

	# **Choosing it has to reach the fields**, which rendering `DocumentFields` directly says
	# nothing about — a mutation wiring `Adding` to the task fields whatever the choice passed
	# everything above. The rule right, the display right, and no wire between them, for the
	# sixth time in this arc.
	chosen = _rendered(tmp_path, {"Adding": {
		"expanded": True, "onWriting": True, "writing": True, "vocabulary": vocabulary,
	}})["Adding"]

	assert "What it says" in chosen, "choosing a document did not produce a document's fields"
	assert "Estimate" not in chosen and "Tags" not in chosen, (
		"choosing a document left a task's fields on screen"
	)


def test_a_revision_says_which_version_it_is_based_on (tmp_path: pathlib.Path) -> None:
	"""`SR#761`, §8.9, and it matters more here than on a task.

	`doc edit` is a whole-body replace, so what is at stake is the **entire document** rather
	than one field: two people with it open, last save wins, and the other person's paragraphs
	are gone with no record that they existed.

	Writing a new one sends no version, because there is nothing to be based on — and the same
	function does both, so that distinction is one branch rather than two builders whose field
	lists drift.
	"""

	values = {"title": "A conclusion", "body": "Prose.", "type": "decision", "status": "active"}

	fresh, revised, emptied = _views(tmp_path, [
		("written", {"values": values, "item": None}),
		("written", {"values": values, "item": {"ref": 4, "version": 6}}),
		("written", {"values": {**values, "body": ""}, "item": {"ref": 4, "version": 6}}),
	])

	assert "expected_version" not in fresh, "a new document claimed to be based on a version"
	assert revised["expected_version"] == 6, (
		"a revision did not say which version it was based on, so it will overwrite a save it "
		"never saw — and a document's whole body is what is at stake"
	)

	# **A body emptied on a revision is cleared**, for `SR#757`'s reason: §8.3 says a field left
	# out is unchanged, so omitting it would make emptying a document impossible.
	assert emptied["body"] is None

	# **And omitted on a creation**, because there is nothing to clear and the endpoint would
	# take the null as a value.
	[bare] = _views(tmp_path, [
		("written", {"values": {**values, "body": ""}, "item": None}),
	])

	assert "body" not in bare


def test_a_form_with_no_body_control_cannot_clear_a_body (tmp_path: pathlib.Path) -> None:
	"""`SR#1044`, and it is the half of that defect nobody had met.

	The rule above is right: a body emptied on a revision must be **cleared**, or emptying a
	document is impossible. What it cannot see is *which of two things it is looking at* — an
	emptied box and a form that has no such box are both falsy here, and until `SR#1044` nothing
	could hand it the second.

	Then `Editing` handed it exactly that. It rendered the **task** form for every item, so a
	document being edited had no body control at all, `readForm` returned no ``body`` key, and
	one press of Save sent ``body: null`` and emptied the document. The reported symptom was the
	*empty box*; this is what pressing on would have done.

	**`readForm` reads the named controls off the DOM**, so the key's presence is exactly the
	question *was there a box*. That is what separates the two, and it is why this is asserted
	on the absent key rather than on the empty string — which is the other case, one line up.
	"""

	values = {"title": "A conclusion", "type": "decision", "status": "active"}
	item = {"ref": 4, "version": 6}

	absent, emptied = _views(tmp_path, [
		("written", {"values": values, "item": item}),
		("written", {"values": {**values, "body": ""}, "item": item}),
	])

	assert "body" not in absent, (
		"a form that never offered a body control was read as somebody having emptied one, so "
		"saving it clears the document — which is what SR#1044 shipped"
	)

	# **And the real clearing still works**, which is the direction a fix here could break: a
	# guard that refused to send null at all would make emptying a document impossible and would
	# pass the assertion above.
	assert emptied["body"] is None, "emptying the box no longer clears the body"


def test_editing_a_document_offers_a_documents_fields (tmp_path: pathlib.Path) -> None:
	"""`SR#1044`, Simon 2026-08-20, from the served instance — the reported half.

	Opening **Edit** on a document showed an empty *Description* and no way to reach the body.
	`Editing` rendered `Fields` — the task form — for every item, so a document was offered a
	task's eight fields and none of its own. `Adding` has made this choice since `SR#761`, six
	lines away; only the editing half never did.

	**Both directions**, because a fix that renders the document form for everything is the same
	defect facing the other way and passes the first assertion on its own.
	"""

	document = {
		"ref": 4, "kind": "document", "title": "A conclusion", "version": 3,
		"body": "What was decided.", "type": "decision", "status": "active",
	}
	vocabulary = {
		"item_types": {
			"task": [{"key": "task", "label": "Task", "is_default": True}],
			"document": [{"key": "decision", "label": "Decision", "is_default": True}],
		},
		"statuses": {
			"task": [{"key": "open", "label": "Open", "is_default": True}],
			"document": [{"key": "active", "label": "Active", "is_default": True}],
		},
	}

	rendered = _rendered(tmp_path, {
		"Editing": {
			"busy": False, "item": document, "members": [],
			"projects": [{"key": "inbox", "title": "Inbox", "is_inbox": True, "depth": 0}],
			"vocabulary": vocabulary,
		},
	})["Editing"]

	assert "What was decided." in rendered, (
		f"the body of the document being edited is nowhere on its own form: {rendered[:400]}"
	)
	assert "Description" not in rendered, (
		"a document was offered a task's Description, which it does not have — so the box was "
		"empty and its own prose was unreachable"
	)

	# **The other direction**, on the sample this file already carries: a task must still get a
	# task's form, or the fix is the same defect the other way round.
	task = _rendered(tmp_path, {"Editing": SAMPLES["Editing"]})["Editing"]

	assert "Description" in task, "a task stopped being offered its own description"
	assert "What it says" not in task, "a task was offered a document's body"


def test_a_link_can_be_made_and_taken_apart (tmp_path: pathlib.Path) -> None:
	"""`SR#760`, and `SR#658` beside it.

	**Removing matters as much as adding**, and there is evidence rather than a principle: on
	2026-08-09 two items were linked to a stranger's `SR#731` by assuming a ref, and
	`subroutine unlink` is what undid it. A browser that can only add is one that cannot fix a
	mistake, which makes every reader careful in the way that stops them using it.

	**`SR#658`: whether the other end is over.** The link view has carried `is_complete` since
	M1 and nothing read it, so a reader looking at *Blocks #442* had to click through to find
	out whether they were still blocked. Said in a word rather than in styling alone (`SR#102`).
	"""

	# **The shape `linkChoices` produces** (`SR#799`), which is what `Detail` hands it.
	box = _rendered(tmp_path, {"Linking": {"busy": False, "types": [
		{"value": "blocks", "label": "Blocks"},
		{"value": "invented", "label": "Something this workspace added"},
	]}})["Linking"]

	assert "Something this workspace added" in box, (
		"the link types are a literal list, so a workspace that adds one cannot use it"
	)

	# **Nothing to offer means no control.** A workspace with no link types would otherwise get
	# an empty select and a button that always refuses.
	assert _rendered(tmp_path, {"Linking": {"busy": False, "types": []}})["Linking"] == ""

	shared = {"item": {"ref": 42, "title": "A task", "status": "open", "kind": "task"},
		"comments": [], "workspace": "projects", "members": [],
		"vocabulary": {"link_types": [{"key": "blocks", "title": "Blocks"}]}}
	links = [
		{"id": "l-1", "link_type": "blocks", "link_category": "gating", "label": "Blocks", "direction": "outgoing",
			"other": {"entity_type": "task", "ref": 9, "title": "Still going",
				"is_complete": False}},
		{"id": "l-2", "link_type": "blocks", "link_category": "gating", "label": "Blocked by", "direction": "incoming",
			# **The title deliberately does not contain the type.** It read *The decision* and
			# carried `type: decision`, so `"decision" in shown` was satisfied by the title
			# and survived deleting the marks outright — a test that could not fail, found by
			# falsifying rather than by reading.
			"other": {"entity_type": "document", "ref": 4, "title": "What we settled",
				"type": "decision", "status": "superseded", "project_path": "subroutine/spec",
				"is_complete": True}},
		{"id": "l-3", "link_type": "blocks", "link_category": "gating", "label": "Blocked by", "direction": "incoming",
			"other": {"entity_type": "task", "ref": 7, "title": "Not started",
				"type": "bug", "status": "open", "status_is_default": True,
				"is_complete": False}},
	]

	shown = _rendered(tmp_path, {"Detail": {**shared, "links": links}})["Detail"]

	assert shown.count("Remove") == 3, "a link cannot be taken apart from where it is shown"
	assert "Still going" in shown and "What we settled" in shown

	# **The whole of `SR#970` in four assertions**, and each is one of Simon's four complaints
	# about reading `SR#94`'s links: *I cannot see which are documents, what status they are in,
	# which are bugs, or which project they are in.*
	#
	# **Asserted on the text rather than on the classes, because this harness drops every
	# attribute** (`SR#784`). That is the right half to check here — `SR#102`'s rule is that
	# nothing may be said in styling alone, so every one of these has to survive the attributes
	# going away. The strikethrough is the half that cannot, and it is a browser test.
	assert "decision" in shown, "a link does not say a bug from a decision"
	assert "superseded" in shown, "a link does not say what state the other end is in"
	assert "subroutine/spec" in shown, "a link does not say which project it points into"
	assert "(1 of 2 blockers done)" in shown, (
		"the page cannot say how much of a milestone is left, which is what `SR#84` models a "
		"milestone as and what `subroutine show` has answered since `SR#210`"
	)

	# **The default status is not a mark** (§12.2a), which is why `SR#970` publishes
	# `status_is_default` beside the key rather than the key alone: *open* on every open item
	# is a word that says nothing, and a chip that appears on everything stops being read.
	assert shown.count("open") == 1, (
		"a status every item has is drawn as a mark, so no mark on this line means anything"
	)

	# **The section shows with nothing in it once there is a form**, for `SR#759`'s reason: an
	# item with no links and no way to make one reads as a page that does not do links.
	empty = _rendered(tmp_path, {"Detail": {**shared, "links": []}})["Detail"]
	mute = _rendered(tmp_path, {"Detail": {**shared, "links": [], "onLink": None}})["Detail"]

	assert "Links" in empty and "<form" in empty
	assert "Links" not in mute


def test_the_item_page_lists_what_it_is_made_of (tmp_path: pathlib.Path) -> None:
	"""`SR#1218`. The page drew the pointer up and not the list down.

	`app.js` said *this is part of `SR#1207`* and could not say *these four are part of this*,
	so the only thing a milestone's page carried about its contents was whatever prose somebody
	had typed into the description. Simon met it reading a milestone in the browser: *"I see
	'Sub-items: Four, split by what one change closes' — but this is in prose only."*

	The capability was on the terminal, over MCP and over HTTP the whole time, which is what
	§14.1 forbids: nothing an agent can see may be invisible to a person.

	**The titles deliberately carry none of the words being asserted for.** A part titled *The
	done one* would satisfy ``"done" in shown`` from the title alone and survive deleting every
	mark — the trap the links test above records having walked into once already.
	"""

	shared = {"item": {"ref": 42, "title": "A parent", "status": "open", "kind": "task"},
		"links": [], "comments": [], "workspace": "projects", "members": [],
		"vocabulary": {"link_types": [{"key": "blocks", "title": "Blocks"}]}}
	parts = {"items": [
		{"id": "t-1", "ref": 7, "title": "The first piece", "type": "bug",
			"status": "open", "status_is_default": True, "is_complete": False},
		{"id": "t-2", "ref": 8, "title": "The second piece", "type": "task",
			"status": "done", "is_complete": True},
	], "has_more": False}

	shown = _rendered(tmp_path, {"Detail": {**shared, "parts": parts}})["Detail"]

	assert "The first piece" in shown and "The second piece" in shown, (
		"a parent still cannot say what it is made of"
	)

	# **The rollup `SR#84` specifies, computed from the children rather than stored.** A parent
	# never auto-completes, so a full count beside an open parent is a question being put to a
	# person — and it has to render as exactly that.
	assert "(1 of 2 done)" in shown, (
		"the page cannot say how much of a parent is finished, which is the number "
		"`subroutine show` prints and the reason `SR#84` needs no schema"
	)

	# **Each part's state, through the same `marks` a link end wears.** Asserted on the text
	# because this harness drops every attribute (`SR#784`) — which is the right half here:
	# `SR#102` says nothing may be said in styling alone, so the strikethrough has a chip beside
	# it and the line reads correctly with every style switched off. The strikethrough itself is
	# a browser test.
	assert "done" in shown, "a finished part is not distinguishable from an unfinished one"
	assert "bug" in shown, "a part does not say what kind of work it is"

	# **Above `Links`**, which is Simon's placement and the terminal's: a parent's parts are what
	# somebody opened it to read, and its links are context around them.
	linked = [{"id": "l-1", "link_type": "blocks", "link_category": "gating",
		"label": "Blocks", "direction": "outgoing",
		"other": {"entity_type": "task", "ref": 9, "title": "Elsewhere",
			"is_complete": False}}]
	both = _rendered(
		tmp_path, {"Detail": {**shared, "parts": parts, "links": linked}}
	)["Detail"]

	assert both.index("Sub-tasks") < both.index("Links"), (
		"the parts are drawn below the links, so the thing the page is about is under the "
		"context around it"
	)

	# **A cap that says it is one** (`SR#888`). Fifty parts and fifty-one look identical, and
	# this is the only thing that tells them apart.
	capped = _rendered(tmp_path, {"Detail": {
		**shared, "parts": {**parts, "has_more": True},
	}})["Detail"]

	assert "Showing the first 50" in capped, (
		"a truncated list of parts claims a completeness it cannot have, which is the shape "
		"`SR#1175` is open about"
	)
	assert "Showing the first" not in shown, (
		"an uncapped list says it was capped, so the line means nothing"
	)

	# **Nothing at all on an item with no parts**, rather than an empty heading — the same rule
	# `blockersDone` follows. Most items are not parents.
	bare = _rendered(tmp_path, {"Detail": shared})["Detail"]

	assert "Sub-tasks" not in bare, "an ordinary item is drawn as though it were a parent"


def test_the_item_page_says_what_has_been_checked (tmp_path: pathlib.Path) -> None:
	"""`SR#1121`, and §14.1 is why it is here rather than only on the agent's surface.

	Nothing an agent stores may be invisible to the person, so a verification the browser
	could not show would be an agent-only surface — which §14.15 forbids by name.

	**A record, not a proof**: the heading says *recorded* and never *verified*, and the words
	*passed* and *failed* carry it rather than a colour (`SR#102`).
	"""

	shared = {"item": {"ref": 42, "title": "A task", "status": "open", "kind": "task"},
		"links": [], "comments": [], "workspace": "projects", "members": [],
		"vocabulary": {"link_types": [{"key": "blocks", "title": "Blocks"}]}}
	checked = [
		{"id": "v-1", "passed": True, "summary": "5,610 passed, 41 skipped",
			"tree_hash": "abcdef1234567890abcdef1234567890abcdef12"},
		{"id": "v-2", "passed": False, "summary": "3 failed", "tree_hash": None},
	]

	shown = _rendered(tmp_path, {"Detail": {**shared, "checked": checked}})["Detail"]

	assert "Recorded checks" in shown
	assert "verified" not in shown.lower(), "a record is reported as a proof"
	assert "5,610 passed, 41 skipped" in shown
	assert "passed" in shown and "failed" in shown
	assert "tree abcdef1" in shown, "the tree it ran against is not shown"

	# **A record with no tree says what that means**, rather than looking like every other one.
	# It cannot go out of date, which is a different answer from being current.
	assert "cannot go out of date" in shown, shown

	# Silent where nothing has been checked, like every other section on this page.
	assert "Recorded checks" not in _rendered(tmp_path, {"Detail": shared})["Detail"]


def test_the_item_page_says_what_to_read_before_starting (tmp_path: pathlib.Path) -> None:
	"""`SR#1119`. The workspace's *what is in force here*, narrowed to one item.

	**It lands here in the same commit as the agent's** because the whole claim for the feature
	is that it serves both participants: a person writes the decisions and an agent reads them
	at the moment it needs them, and a version only one of them can see is half a feature.

	**Above the links**, because it is the section somebody reads before doing anything and the
	links are what they read afterwards.
	"""

	shared = {"item": {"ref": 42, "title": "A task", "status": "open", "kind": "task"},
		"links": [], "comments": [], "workspace": "projects", "members": [],
		"vocabulary": {"link_types": [{"key": "blocks", "title": "Blocks"}]}}
	governing = [
		{"link_type": "documents", "document": {
			"entity_type": "document", "ref": 4, "title": "What we settled",
			"type": "decision", "status": "active", "is_complete": False}},
	]

	shown = _rendered(tmp_path, {"Detail": {**shared, "governing": governing}})["Detail"]

	assert "Read first" in shown
	assert "What we settled" in shown
	assert "#4" in shown, "the number is how a reader opens it"
	assert "decision" in shown, "the reading list does not say what kind of thing each one is"

	# **Silent when nothing governs**, which is the §1.4 rule this section is most at risk
	# from: a personal to-do list writes no decisions, and a heading that appeared empty would
	# put the word *govern* in front of somebody whose list says *buy milk*. Unlike Links,
	# there is no form here to keep an empty section honest.
	assert "Read first" not in _rendered(tmp_path, {"Detail": shared})["Detail"]

	# **Above the links.** Asserted by position rather than by eye, because the whole reason it
	# is a separate section is the order somebody reads them in.
	with_links = _rendered(tmp_path, {"Detail": {
		**shared, "governing": governing,
		"links": [{"id": "l-1", "link_type": "blocks", "link_category": "gating", "label": "Blocks",
			"direction": "outgoing", "other": {"entity_type": "task", "ref": 9,
				"title": "Still going", "is_complete": False}}],
	}})["Detail"]

	assert with_links.index("Read first") < with_links.index("Links")


def test_what_an_item_is_joined_to_is_read_before_its_description (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#1149`, Simon: *"we don't see them without scrolling when the description is long."*

	The rule he settled, and it decides the whole page rather than one section: **what you need
	before reading the item goes above the description; what accumulated about it stays below.**
	So *Read first* and *Links* say what binds this and what it is joined to — which is how a
	reader decides whether to read the description at all — and *Recorded checks* and *Comments*
	are the record of what happened, looked up deliberately rather than scanned.

	**`SR#1119`'s argument survives rather than being inverted.** It put *Read first* above
	*Links* because it is what somebody must read before doing anything; that reason applies
	harder against a long description than the links' does, so both moved and their order held.

	**Asserted as one sequence rather than as four pairs.** Each member checked is not the set
	checked: four `a < b` assertions all pass on an arrangement no single one of them describes,
	and the thing being fixed here is the order of the page.
	"""

	rendered = _rendered(tmp_path, {"Detail": {
		"item": {"ref": 42, "title": "A task", "status": "open", "kind": "task",
			"description": "MARKER-DESCRIPTION"},
		"workspace": "projects", "members": [],
		"vocabulary": {"link_types": [{"key": "blocks", "title": "Blocks"}]},
		"governing": [{"link_type": "documents", "document": {
			"entity_type": "document", "ref": 4, "title": "What we settled",
			"type": "decision", "status": "active", "is_complete": False}}],
		"links": [{"id": "l-1", "link_type": "blocks", "link_category": "gating", "label": "Blocks",
			"direction": "outgoing", "other": {"entity_type": "task", "ref": 9,
				"title": "Still going", "is_complete": False}}],
		"checked": [{"id": "v-1", "passed": True, "summary": "5,610 passed",
			"tree_hash": "abcdef1234567890abcdef1234567890abcdef12"}],
		"comments": [{"id": "c-1", "body": "It happened", "created_at": "2026-08-23T09:00:00Z"}],
	}})["Detail"]

	wanted = ["Read first", "Links", "MARKER-DESCRIPTION", "Recorded checks", "Comments"]
	found = sorted(wanted, key=rendered.index)

	assert found == wanted, f"the page reads in the order {found}"


def test_which_kind_a_ref_names_is_resolved_rather_than_asked (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#760`. One ref counter serves tasks and documents (§6.2).

	So `#4` is a decision document on this instance and `#42` is a task, and a reader typing a
	ref into a link box should not have to say which — `subroutine show 4` does not ask, and
	neither does opening one in this app, which already tries each kind in turn.

	**The order comes from `/v1/meta`**, so an installation that grows a third linkable kind is
	tried too rather than being silently unreachable.
	"""

	published, absent, empty = _views(tmp_path, [
		("linkableTypes", {"vocabulary": {"linkable_types": ["task", "document", "wibble"]}}),
		("linkableTypes", {"vocabulary": None}),
		("linkableTypes", {"vocabulary": {"linkable_types": []}}),
	])

	assert published == ["task", "document", "wibble"], (
		"a kind this instance publishes is not tried, so a ref naming one is unreachable"
	)

	# **A fallback rather than nothing**, because the link box is worth more than it costs: an
	# instance that published no list at all would otherwise refuse every link.
	assert absent == ["task", "document"]
	assert empty == ["task", "document"]


def test_a_thread_says_who_spoke (tmp_path: pathlib.Path) -> None:
	"""`SR#759`. A comment carries `author_id` and no name.

	A thread that cannot say who spoke is a transcript with the names cut out — and on this
	instance four of five members are agents (`SR#770`), so *who wrote this* is the difference
	between a colleague's note and a machine's. `SR#474` is that delegation has never once been
	used here; attribution is the half of it a reader actually sees.

	**The roster first, the row second, and null only when neither answers** (`SR#636`). The
	roster's label marks a service account — *claude (agent)* — which the response's bare
	username cannot, and that distinction is this function's whole reason for existing. But
	somebody who has left the workspace is on no roster, and showing nothing then is a
	transcript with one name cut out, which is the defect this exists to prevent. The response
	carries ``author`` since `SR#636` and answers exactly that case.

	Inventing *Unknown* is still refused: it would claim the lookup happened and found an
	answer, where null says plainly that nothing here knows.
	"""

	members = [
		{"id": "u-1", "username": "si", "label": "si"},
		{"id": "u-2", "username": "claude", "label": "claude (agent)"},
	]

	person, agent, gone, nobody, departed = _views(tmp_path, [
		("authorOf", {"comment": {"author_id": "u-1"}, "members": members}),
		("authorOf", {"comment": {"author_id": "u-2"}, "members": members}),
		("authorOf", {"comment": {"author_id": "u-9"}, "members": members}),
		("authorOf", {"comment": {"author_id": "u-1"}, "members": []}),
		# On no roster, and the response says who it was — `SR#636`.
		("authorOf", {
			"comment": {"author_id": "u-9", "author": "jo"}, "members": members,
		}),
	])

	assert person == "si"
	assert agent == "claude (agent)", "an agent's note is attributed as though a person wrote it"
	assert gone is None, "an author neither the roster nor the row can name was named anyway"
	assert nobody is None
	assert departed == "jo", (
		"somebody who has left the workspace loses their name from the transcript"
	)

	# **The roster still wins where both answer**, or the agent marker is lost — which is the
	# one thing the response cannot carry and the roster can.
	[marked] = _views(tmp_path, [
		("authorOf", {
			"comment": {"author_id": "u-2", "author": "claude"}, "members": members,
		}),
	])

	assert marked == "claude (agent)", "the bare username displaced the roster's label"

	# **Composed with the roster this page actually holds**, which is the half a hand-written
	# `members` cannot check: `people` builds those rows, and it kept only the username until
	# this — so a mutation dropping the id passed, because the only test of the reader had
	# built its own input. Same shape as `refusal`, one commit earlier.
	[roster] = _views(tmp_path, [("people", {"roster": [
		{"user": {"id": "u-7", "username": "claude", "is_service_account": True}},
	]})])

	[resolved] = _views(tmp_path, [
		("authorOf", {"comment": {"author_id": "u-7"}, "members": roster}),
	])

	assert resolved == "claude (agent)", (
		"the roster this page holds cannot resolve an author, so no comment will be attributed"
	)


def test_a_comment_can_be_written_and_says_what_it_is_for (tmp_path: pathlib.Path) -> None:
	"""`SR#759`. §5.10: a comment is what happened; a document is what you concluded.

	**One textarea and nothing else**, which is the endpoint's whole request model — *a comment
	that needed a title, a type or a project would be a document*. A form offering those fields
	would be quietly proposing the other thing, and that distinction is the one this product is
	least willing to blur.

	**The heading shows with nothing under it once there is a box.** An empty thread with no way
	to start one reads as absent rather than as empty.

	**The labels ask rather than instruct, which is `#865` and Simon's.** §5.10's sentence is
	right where it *teaches* — the specification, the agent guide, the skill, and
	`subroutine_comment`'s own description, all read by somebody choosing between a comment and
	a document. On the box and over the thread it stopped being a distinction and became an
	instruction at the moment somebody is writing, and *"I have asked the supplier"* is neither
	wrong nor what happened.
	"""

	box = _rendered(tmp_path, {"Saying": {"busy": False}})["Saying"]

	assert "What happened" not in box, (
		f"the box tells somebody what their comment has to be about: {box}"
	)

	# **The label and the placeholder are read from the source, not from the render, and that
	# is a limit of the harness rather than a preference.** `_rendered` emits an element's
	# children and its `href` and drops every other attribute (`#784`) — so a mutation putting
	# the instruction back into `aria-label` *survived* the assertion above, which is a test
	# that cannot fail. The screen-reader half of Simon's point lives entirely in attributes,
	# so it has to be checked where it is written. Prose is stripped first: the reasoning
	# beside these strings quotes the words they no longer use.
	assert "What happened" not in _without_comments(_served_modules()["app.js"]), (
		"a label or placeholder still tells somebody what their comment has to be about"
	)

	assert "<textarea" in box and "<button" in box
	assert "<select" not in box and "<input" not in box, (
		"the comment box offers a field that would make it a document"
	)

	shared = {"item": {"ref": 42, "title": "A task", "status": "open", "kind": "task"},
		"links": [], "workspace": "projects", "members": []}

	# **`onComment` is nulled explicitly rather than omitted.** The harness supplies every
	# `onSomething=` it finds in the source, so leaving one out tests nothing at all — a fixture
	# filling in the caller's wiring makes that wiring unfalsifiable, and those tests read
	# exactly like coverage.
	empty = _rendered(tmp_path, {"Detail": {**shared, "comments": []}})["Detail"]
	mute = _rendered(tmp_path, {"Detail": {**shared, "comments": [], "onComment": None}})["Detail"]

	# **The heading is `Comments` since `#865`, and the intent is unchanged.** This asserted
	# *What happened*, so rewording the label failed a test whose subject is whether the section
	# appears at all — the satisfier moved and the question did not. Kept rather than relaxed:
	# the point is that an empty thread with a box is a section, and one with neither is not.
	assert "Comments" in empty and "<textarea" in empty, (
		"an item with no comments offers no way to write the first one"
	)
	assert "Comments" not in mute, (
		"a heading with nothing under it and no box, which is a section that is not there"
	)


def test_a_status_can_be_changed_without_opening_the_form (tmp_path: pathlib.Path) -> None:
	"""`SR#758`. *To do* → *in progress* → *done* is the commonest write anybody makes.

	**The vocabulary comes from the workspace**, never a literal list: a status is renameable
	and an installation may add one, so a control carrying its own three words is wrong on the
	first instance that does either — and wrong silently, because it still looks complete.

	**A status is not a claim and neither is derived from the other** (`SR#726`, Simon's
	ruling), so the write has one field in it. It would be easy to make *in progress* claim the
	item on the way past; that is a write nobody asked for, and it would make a claim's meaning
	depend on which surface moved the status.
	"""

	statuses = {"task": [
		{"key": "open", "label": "Open", "is_default": True},
		{"key": "in_progress", "label": "In progress"},
		{"key": "parked", "label": "Parked"},
	]}
	item = {"ref": 42, "kind": "task", "title": "A task", "status": "open",
		"status_category": "todo"}

	rendered = _rendered(tmp_path, {"Doing": {
		"item": item, "members": [], "statuses": statuses, "onStatus": True, "busy": False,
	}})["Doing"]

	assert "Parked" in rendered, (
		"a status this workspace has invented is not offered, so the control is a literal list"
	)
	assert "Complete" in rendered, "the quick path displaced the control it sits beside"

	# **Available on something already over**, which is where it is most wanted and where the
	# whole block used to disappear: `Doing` returned null unless the item was completable, so
	# a cancelled item could not be moved back to open from here at all.
	over = _rendered(tmp_path, {"Doing": {
		"item": {**item, "status": "cancelled", "status_category": "cancelled"},
		"members": [], "statuses": statuses, "onStatus": True, "busy": False,
	}})["Doing"]

	assert "Parked" in over, "a finished item offers no way back"
	assert "Complete" not in over, (
		"an item that is already over is offered Complete, whose only outcome is a refusal"
	)


def test_an_open_item_offers_an_edit_and_becomes_one (tmp_path: pathlib.Path) -> None:
	"""`SR#757`. The rules being right is worth nothing if the page never reaches them.

	A mutation making `Detail` never render the form passed everything else, because every
	other test of this arc checks a pure function. That is the fault this app keeps shipping —
	the rule right, the display right, and no wire between them — and it is why `SR#640` says
	to lift decisions out *and* drive the component that uses them.

	**The form replaces the item's display rather than sitting beside it.** Two copies of a
	title on one screen, one of them stale, is the shape this project keeps paying for; and a
	reader has to be able to see what they are changing without a second version arguing with
	it.
	"""

	item = {
		"ref": 42, "title": "A task", "version": 3, "timezone": "Etc/UTC",
		"description": "why", "status": "open", "type": "task", "tags": [],
	}
	shared = {"item": item, "links": [], "comments": [], "workspace": "projects",
		"members": [{"username": "si", "label": "si"}]}

	reading = _rendered(tmp_path, {"Detail": {**shared, "editing": False}})["Detail"]
	writing = _rendered(tmp_path, {"Detail": {**shared, "editing": True}})["Detail"]

	assert "Edit" in reading, "an open item offers no way to change it"
	assert "<fieldset" not in reading, "the form is showing before anybody asked for it"

	assert "<fieldset" in writing, "asking to edit produced no form"
	assert "<h2>" not in writing, (
		"the item's own title is still on screen beside the one being edited, so a reader has "
		"two versions of it and one of them is stale"
	)

	# **A conflict is shown inside the form**, where the reader's typing still is — not in place
	# of it. `SR#102`: the whole of it is in words, so nothing depends on the border colour.
	clashed = _rendered(tmp_path, {"Detail": {
		**shared, "editing": True, "conflict": {"ref": 42, "title": "What it says now"},
	}})["Detail"]

	assert "<fieldset" in clashed, "a conflict discarded the form somebody was typing into"
	assert "What it says now" in clashed and "Somebody else saved this" in clashed


def test_a_save_somebody_else_beat_is_news_rather_than_a_failure (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#757`, §8.9, and the reason `expected_version` is worth sending at all.

	Sending it turns *last write wins* into a refusal, and a refusal is only an improvement if
	the person on the other end of it is told something they can act on. The 409 carries the
	current entity — `concurrency.reporting()` attaches it deliberately — and the browser
	discarded it: `api` parsed the problem document, kept `detail` and threw the rest away, one
	line before anybody could read it.

	**A 409 with nothing attached is not a conflict to show.** An older instance, or a proxy
	that rewrote the body, would otherwise put a warning on screen naming nothing — which reads
	as a bug in the page rather than as news about the item. That is the half that would have
	been forgotten, and it is why this is a function rather than an `if`.
	"""

	theirs = {"ref": 42, "title": "What it says now", "version": 8}

	conflict, bare, refused, none = _views(tmp_path, [
		("conflictIn", {"failure": {"status": 409, "body": {"current": theirs}}}),
		("conflictIn", {"failure": {"status": 409, "body": {"detail": "Version conflict"}}}),
		("conflictIn", {"failure": {"status": 422, "body": {"current": theirs}}}),
		("conflictIn", {"failure": None}),
	])

	assert conflict == theirs
	assert bare is None, "a 409 carrying nothing was shown as a conflict naming nothing"
	assert refused is None, "an ordinary refusal was mistaken for a version conflict"
	assert none is None

	# **The two steps composed, which is the half a synthetic failure cannot check.** A mutation
	# putting `api` back to discarding the body passed everything above, because the only test
	# of the reader had built its own input — the rule right, the display right, and no wire
	# between them, for the fourth time in this arc.
	#
	# The document below is the shape the served instance actually answered with, copied from a
	# real 409 rather than invented.
	[whole] = _views(tmp_path, [("refused", {"status": 409, "problem": {
		"code": "version_conflict",
		"detail": "#6 has changed since you read it: you have version 1, and it is now at 2.",
		"expected_version": 1, "current_version": 2, "current": theirs,
	}})])

	assert whole["status"] == 409
	assert "has changed since you read it" in whole["message"], (
		"the instance's own words did not reach the reader"
	)
	assert whole["conflict"] == theirs, (
		"a real refusal does not carry the item, so the conflict message would name nothing"
	)


def test_a_dropdown_is_built_from_the_workspace_and_not_from_a_list (
	tmp_path: pathlib.Path,
) -> None:
	"""Types and statuses are workspace vocabulary: renameable, and an instance may add one.

	A form carrying its own array is wrong on the first workspace that does either, and wrong
	**silently** — the control still looks complete. `/v1/meta` publishes them, which is what it
	is for.

	**Which one is pre-selected is read too.** `task` and `open` are what `seed.py` happens to
	install here, not something the model promises, so the default comes from `is_default`.
	"""

	vocabulary = {"task": [
		{"key": "task", "label": "Task", "is_default": False},
		{"key": "wibble", "label": "Wibble", "is_default": True},
	], "document": [{"key": "note", "label": "Note", "is_default": True}]}

	chosen, absent, unknown = _views(tmp_path, [
		("offered", {"vocabulary": vocabulary, "kind": "task"}),
		("offered", {"vocabulary": None, "kind": "task"}),
		("offered", {"vocabulary": vocabulary, "kind": "sandwich"}),
	])

	assert [one["key"] for one in chosen] == ["task", "wibble"]
	assert [one["chosen"] for one in chosen] == [False, True], (
		"the pre-selection is not being read from the workspace's own default"
	)

	# **Nothing to offer is an empty list, never a guess.** The form disables a select it cannot
	# fill, which is visibly unfinished; inventing `task` and `open` would be confidently wrong.
	assert absent == [] and unknown == []


def test_a_project_narrows_the_statuses_a_picker_offers (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#1029`. Measured on the instance that asked for it: 171 open tasks, every one `open`.

	**It narrows the offer and refuses nothing** (Simon, 2026-08-20). Any surface may still set
	any status the workspace has — a preference, not a permission — so what is being checked
	here is a control's contents rather than anything about what a write is allowed to do.

	**The two escapes are the point.** A picker always offers the status the thing would have if
	you did nothing: what an existing item is in, and what a new one will be given. Both were
	found by working the case through rather than by the requirement asking for them, and both
	are failures a reader would meet on the first hidden status.
	"""

	vocabulary = {"task": [
		{"key": "open", "label": "Open", "is_default": True},
		{"key": "blocked", "label": "Blocked", "is_default": False},
		{"key": "needs_input", "label": "Needs input", "is_default": False},
		{"key": "done", "label": "Done", "is_default": False},
	]}

	everything, narrowed, held, defaulted = _views(tmp_path, [
		("offered", {"vocabulary": vocabulary, "kind": "task"}),
		("offered", {
			"vocabulary": vocabulary, "kind": "task",
			"hidden": ["blocked", "needs_input"], "keep": "open",
		}),
		# An item already in a status the project has stopped offering.
		("offered", {
			"vocabulary": vocabulary, "kind": "task",
			"hidden": ["blocked", "needs_input"], "keep": "blocked",
		}),
		# A project that hid the status new work is created in.
		("offered", {
			"vocabulary": vocabulary, "kind": "task",
			"hidden": ["open", "blocked"], "keep": None,
		}),
	])

	assert [one["key"] for one in everything] == ["open", "blocked", "needs_input", "done"], (
		"a project that has configured nothing must be offered the whole vocabulary"
	)

	assert [one["key"] for one in narrowed] == ["open", "done"]

	# **Without this a `<select>` reports a blocked task as Open**, because a value matching no
	# option renders blank or falls back to the first entry — and saving anything else on the
	# form then writes that back. The one failure here that loses data.
	assert "blocked" in [one["key"] for one in held], (
		"an item in a hidden status was offered no way to say what it is in"
	)

	# **Without this a project that hid its default could not file an ordinary task.** The
	# control would pick whatever came first, and the server hands out the hidden one anyway.
	assert [one["key"] for one in defaulted] == ["open", "needs_input", "done"]
	assert defaulted[0]["chosen"] is True, (
		"the default survived being hidden and is still the pre-selection"
	)


def test_the_form_offers_only_the_statuses_the_project_it_files_into_offers (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#1029`, and this is the half that `offered`'s own test cannot reach.

	**A pure function being right says nothing about anything calling it.** That is `SR#640`, and
	it has shipped six times here — the rule right, the display right, and no wire between them.
	`offered` narrowing correctly and `Fields` never passing it a project would pass every other
	test in this file.

	**The project is read live from the form**, so choosing a different one re-narrows in the
	same render — which is why the value passed in is the whole address rather than a key.
	"""

	statuses = [
		{"key": "open", "label": "Open", "is_default": True},
		{"key": "blocked", "label": "Blocked", "is_default": False},
	]
	sample: dict[str, typing.Any] = {
		"busy": False,
		"values": {"description": "why"},
		"project": "shopping",
		"projects": [{
			"id": "aaa", "key": "shopping", "title": "Shopping", "is_inbox": False,
			"depth": 0, "hidden_statuses": ["blocked"],
		}],
		"members": [{"username": "si", "label": "si"}],
		"vocabulary": {
			"item_types": {"task": [{"key": "task", "label": "Task", "is_default": True}]},
			"statuses": {"task": statuses},
		},
	}

	markup = _rendered(tmp_path, {"Fields": sample})["Fields"]

	assert "Open" in markup, "the form lost the status its project does offer"
	assert "Blocked" not in markup, (
		"the form offered a status this project hides, so nothing is passing it the project"
	)

	# **The same form filing somewhere that hides nothing offers both**, which is what stops the
	# assertion above being satisfied by a `Blocked` that never renders anywhere.
	sample["projects"][0]["hidden_statuses"] = []

	assert "Blocked" in _rendered(tmp_path, {"Fields": sample})["Fields"]


def test_an_items_status_control_offers_what_its_own_project_offers (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#1029`. The item page's control finds its project by id, where a form uses the address.

	Driven as well as unit-tested for `SR#640`'s reason: `Doing` receiving no roster at all would
	leave `offered` correct and this control unnarrowed, and nothing else here would notice.
	"""

	statuses = [
		{"key": "open", "label": "Open", "is_default": True},
		{"key": "needs_input", "label": "Needs input", "is_default": False},
	]
	sample: dict[str, typing.Any] = {
		"item": {
			"ref": 42, "kind": "task", "title": "Buy milk", "status": "open",
			"status_category": "todo", "project_id": "aaa",
		},
		"members": ["si"],
		"busy": False,
		"statuses": {"task": statuses},
		"projects": [{
			"id": "aaa", "key": "shopping", "depth": 0, "hidden_statuses": ["needs_input"],
		}],
	}

	markup = _rendered(tmp_path, {"Doing": sample})["Doing"]

	assert "Open" in markup
	assert "Needs input" not in markup, (
		"the item page offered a status its project hides"
	)

	sample["projects"][0]["hidden_statuses"] = []

	assert "Needs input" in _rendered(tmp_path, {"Doing": sample})["Doing"]


def test_the_item_page_hands_its_status_control_the_project_roster (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#1029`. One wire further out than the control's own test can see.

	`Doing` takes the roster as an optional argument, so a `Detail` that forgot to pass it would
	leave every narrowing correct and every picker unnarrowed — silently, because the default is
	*nothing hidden* and that is indistinguishable from a project that hides nothing.

	**This is `SR#640`'s shape and it is the one that keeps shipping**: the rule right, the
	display right, no wire between them. `Detail` is renderable, so unlike `App` it costs a
	Node test rather than a browser one.
	"""

	item = {
		"ref": 42, "kind": "task", "title": "Buy milk", "status": "open",
		"status_category": "todo", "project_id": "aaa", "description": "Why it matters.",
	}
	sample = {
		"item": item,
		"links": [],
		"comments": [],
		"workspace": "personal",
		"backTo": "/personal",
		"members": ["si"],
		# `Doing` is rendered only where the page can act, so the callback has to be here.
		"onComplete": True,
		"statuses": {"task": [
			{"key": "open", "label": "Open", "is_default": True},
			{"key": "needs_input", "label": "Needs input", "is_default": False},
		]},
		"projects": [{
			"id": "aaa", "key": "shopping", "depth": 0, "hidden_statuses": ["needs_input"],
		}],
	}

	markup = _rendered(tmp_path, {"Detail": sample})["Detail"]

	assert "Open" in markup, "the item page did not render its status control at all"
	assert "Needs input" not in markup, (
		"the item page rendered a status its project hides, so the roster is not reaching Doing"
	)


def test_which_statuses_a_project_hides_is_found_by_id_or_by_address (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#1029`. The two readers hold different things and neither should have to convert.

	An item's row carries `project_id`; a form's project control carries the whole **address**,
	and has to, because a key stopped identifying a project at `SR#958`.

	**Not found means nothing hidden, deliberately.** The roster is capped and scoped to one
	workspace, so *not here* is *not known* — and failing towards offering everything is the safe
	direction, since an extra option is a shrug and a missing one is a control that cannot say
	what an item is.
	"""

	projects = [
		{"id": "aaa", "key": "substation", "depth": 0, "hidden_statuses": ["needs_input"]},
		{"id": "bbb", "key": "dist", "depth": 1, "hidden_statuses": ["blocked"]},
		{"id": "ccc", "key": "shopping", "depth": 0, "hidden_statuses": []},
	]

	by_id, by_address, nested, unknown, nothing = _views(tmp_path, [
		("notOffered", {"projects": projects, "chosen": "bbb"}),
		("notOffered", {"projects": projects, "chosen": "substation"}),
		("notOffered", {"projects": projects, "chosen": "substation/dist"}),
		("notOffered", {"projects": projects, "chosen": "nowhere"}),
		("notOffered", {"projects": projects, "chosen": None}),
	])

	assert by_id == ["blocked"]
	assert by_address == ["needs_input"]

	# **The whole address, not the bare key** — a child is addressed through its parent, which is
	# the same walk the form's own project control makes.
	assert nested == ["blocked"], "a nested project was not found by its address"

	assert unknown == [] and nothing == []


def _date_fields () -> list[tuple[str, str, str]]:
	"""The browser's three date fields, as `(name, label, hint)`.

	**A trailing flag is allowed and the sentence is not** (`SR#798`): the entry grew a fourth
	member saying whether the control carries a time, and the three that describe the field to a
	reader are still read — and compared — exactly.
	"""

	app = _served_modules()["app.js"]
	found = re.search(r"export const DATE_FIELDS = \[(.*?)\n\];", app, re.S)

	assert found is not None, "DATE_FIELDS is gone, so this is checking nothing"

	fields = re.findall(
		r'\["(\w+)", "([^"]+)",\s*"([^"]+)",?\s*(?:true|false)?\s*\]', found.group(1), re.S
	)

	assert len(fields) == 3, f"{len(fields)} date fields were read, and there are three"

	return fields


def test_the_browser_calls_the_three_dates_what_the_terminal_calls_them () -> None:
	"""`SR#769`. The browser said *Starts*, which is the one reading `snoozed_until` is not.

	Appendix A's ambiguity A4 asked whether it means *work starts then*, *hide until then* or
	*earliest permitted start*, and settled it as a **defer**: not actionable before it, hidden
	by default (§6.5). `subroutine explain dates` has said so since M1.

	So this was a second copy of a vocabulary disagreeing with the first, on the one surface with
	no `explain` to check against — a terminal reader can ask, a browser reader has the label and
	nothing else. `cli/topics.py` is the original, and this makes the browser derive from it
	rather than merely agree with it today.

	**The sentences are compared exactly.** I wrote this to compare load-bearing words first, on
	the grounds that a fixed-width table and a hint under a control want different prose — and
	then it failed, because my own hint for `start` had drifted to two words in common. Reading
	the terminal's three sentences showed they need no adaptation at all: they are already the
	right length and the right voice for a form. So the weaker check was solving a problem that
	did not exist, and equality is what *one* copy of a sentence actually means.

	The labels stay a case-insensitive containment, because the terminal writes them lower case
	inside a table and a control's label is capitalised.
	"""

	said = subroutine.cli.topics.find("dates")

	assert said is not None, "the dates topic is gone, so this is checking nothing"

	terminal = said.body.lower()
	fields = _date_fields()

	for name, label, hint in fields:
		assert label.lower() in terminal, (
			f"the browser calls {name} {label!r} and `subroutine explain dates` has never used "
			f"that phrase — one of the two is now teaching something the other contradicts"
		)

		assert hint in said.body, (
			f"the browser explains {name} as {hint!r}, which appears nowhere in `subroutine "
			f"explain dates` — two surfaces are now teaching two things about one field"
		)

	# **Chronological, and the reason is written down** so it is not reshuffled by taste: when
	# it starts, then when you want to stop being shown it, then when it is due. The middle one
	# is the odd member and is meant to look it — two of these say when the work happens and
	# one says when you want to be bothered about it (`#854`).
	assert [name for name, _label, _hint in fields] == ["starts", "snooze", "due"]


def test_the_priority_scale_says_which_way_it_runs (tmp_path: pathlib.Path) -> None:
	"""`SR#770`. A bare 1 to 5 does not say whether 1 or 5 is the important end.

	Appendix A's ambiguity A1: *1-5, 5 = most important/urgent. Higher is more (§6.3)* — settled
	because every `gte` filter depends on it. A reader who guesses the other way files their
	whole backlog upside down and is never told, which is the failure worth preventing rather
	than the untidiness.

	**The direction is asserted against the domain rather than against this docstring**, so a
	control that ever lists *Very high* against 1 fails here.
	"""

	app = _served_modules()["app.js"]
	found = re.search(r"export const PRIORITIES = \[(.*?)\n\];", app, re.S)

	assert found is not None, "PRIORITIES is gone, so this is checking nothing"

	rungs = re.findall(r'\{ value: (\d), label: "([^"]+)" \}', found.group(1))

	assert [int(value) for value, _label in rungs] == list(
		range(min(subroutine.domain.tasks.PRIORITY_RANGE), max(subroutine.domain.tasks.PRIORITY_RANGE) + 1)
	), "the control offers a different range from the one the service accepts"

	labels = {int(value): label.lower() for value, label in rungs}

	assert "very high" in labels[max(labels)], (
		f"the top of the scale is labelled {labels[max(labels)]!r}, and §6.3 says higher is more"
	)
	assert "very low" in labels[min(labels)], (
		f"the bottom of the scale is labelled {labels[min(labels)]!r}, and §6.3 says higher is more"
	)

	# **Ascending, so the number stays the thing.** `!4/3` is how a priority is written at a
	# terminal, in a captured line and in `Facts`; a control putting 5 at the top would be the
	# one place the scale reads backwards.
	assert [int(value) for value, _label in rungs] == sorted(int(v) for v, _ in rungs)


def test_the_control_and_the_row_say_the_same_thing_about_an_agent (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#1420`. **Two vocabularies for one roster is what this control's own comment objects to.**

	`SR#1414` put *(agent, @si)* on a row and this control went on saying *(agent)* — so a
	reader picking somebody to hand work to was told less about them than the row they had just
	come from. `SR#674` is the shape and `SR#1266` is the guard family.

	**The sigil is the one thing that differs, and deliberately.** On a row `@si` sits beside
	`#ops` and a project path, and the sigil is what makes three addresses tell themselves
	apart; in a control whose every option is an account it distinguishes nothing, and a marker
	on every row is §12.2a's column that says the same thing everywhere. So the assertion is
	that everything *after* the name agrees.
	"""

	[people] = _views(tmp_path, [("people", {"roster": [
		{"user": {
			"username": "gizmo", "is_service_account": True,
			"display_name": None, "answers_to": "morgan",
		}},
	]})])

	[row] = _addressing(tmp_path, [
		("marks", {
			"item": {
				"ref": 1, "title": "Held", "assignee": "gizmo",
				"assignee_is_agent": True, "assignee_answers_to": "morgan",
			},
			"showKind": False, "ordering": None, "place": None,
			"linkable": False, "hideStatus": False,
		}),
	])

	offered = people[0]["label"]
	drawn = next(
		mark["text"] for mark in row if "gizmo" in (mark.get("text") or "")
	)

	assert offered == "gizmo (agent, @morgan)", (
		f"the control offers {offered!r}, which does not say who is accountable for the agent "
		f"a reader is about to hand work to"
	)
	assert drawn == f"@{offered}", (
		f"the control says {offered!r} and the row says {drawn!r} — two vocabularies for one "
		f"roster, which is the thing this control's own comment objects to about '(bot)'"
	)


def test_an_agent_is_not_offered_as_though_it_were_a_colleague (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#770`. The roster publishes `is_service_account` and the app threw it away.

	Measured on this project's own workspace: **one person and four service accounts**, so the
	assignment control offered five names that read as five colleagues.

	That matters more than it looks. `SR#473` made an agent answer to a person and `SR#474` is
	that delegation has never once been used on this instance — handing work to `claude-nuc14`
	believing it is a colleague is the failure the accountability chain exists to prevent.

	**Said in a word** (`SR#102`): nothing may be information only in how it looks, so an icon
	beside four of five names would not do on its own.
	"""

	[said] = _views(tmp_path, [("people", {"roster": [
		{"user": {"username": "si", "is_service_account": False, "display_name": None}},
		{"user": {"username": "claude-nuc14", "is_service_account": True,
			"display_name": "Claude on nuc14"}},
	]})])

	assert said[0] == {"username": "si", "label": "si"}
	assert said[1]["label"] == "claude-nuc14 (agent)", (
		f"an agent is offered as {said[1]['label']!r}, which reads as a person"
	)

	# **The username is the label, not `display_name`.** A reader who picks *Claude on nuc14* and
	# reads `claude-nuc14` back off the item has been shown two names for one account.
	assert said[1]["username"] == "claude-nuc14"


def test_a_sub_project_cannot_be_mistaken_for_a_root_one (tmp_path: pathlib.Path) -> None:
	"""`SR#770`. The projects are a tree and the control was flat.

	On this instance that put `Web UI` — which is inside `Subroutine` — beside `Websites`, which
	is a root, as though they were the same kind of thing.

	**Indented by depth, two spaces per level, because that is what `subroutine project list`
	already does.** One tree rendered the same way on both surfaces rather than each inventing a
	shape for it. `path` orders and `depth` indents; `path` is sortable and not selectable, which
	is why the shape arrives as an order plus a number.
	"""

	[said] = _views(tmp_path, [("filableFor", {"project": None, "projects": [
		{"key": "inbox", "title": "Inbox", "is_inbox": True, "depth": 0},
		{"key": "subroutine", "title": "Subroutine", "depth": 0},
		{"key": "ui", "title": "Web UI", "depth": 1},
		{"key": "websites", "title": "Websites", "depth": 0},
	]})])

	assert [one["label"] for one in said] == [
		"Inbox (default)", "Subroutine", "\u00a0\u00a0Web UI", "Websites",
	]

	# **Non-breaking**, because an `<option>` is the one place a browser may collapse leading
	# whitespace, and the indent is the whole of what is being said.
	assert said[2]["label"].startswith("\u00a0\u00a0"), (
		"the indent is ordinary whitespace, which an `<option>` may collapse"
	)


#: **Two of them are called `Personal`**, which is what the served instance really holds and is
#: the case `SR#979` was found by. A roster of distinct titles cannot tell a control that
#: identifies its destinations from one that merely names them.
SPACES = [
	{"id": "1", "slug": "projects", "title": "Personal"},
	{"id": "2", "slug": "personal", "title": "Personal"},
	{"id": "3", "slug": "acme", "title": "Acme"},
]

#: A workspace's projects as `projectsRequest` receives them: `order=path`, so a pre-order in
#: creation order. Deliberately not alphabetical at either level.
FILABLE = [
	{"key": "substation", "title": "Substation", "depth": 0},
	{"key": "dist", "title": "Packaging", "depth": 1},
	{"key": "inbox", "title": "Inbox", "is_inbox": True, "depth": 0},
	{"key": "alpha", "title": "Alpha", "depth": 0},
]


def test_only_a_query_that_is_nothing_but_a_ref_opens_an_item (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#976`, Simon's, and the sigil is the whole signal.

	**A bare number stays a search.** `8471` is the port `docs/hosting.md` names and `403` and
	`404` are throughout the prose here, so jumping on one would make those unfindable for as
	long as an item happened to hold that number — and invisibly, because the reader would get
	an item, which looks like a search that worked rather than one that never ran.

	The grammar is `refs._TYPED`'s and `parseAddress`'s, so a number means one thing on every
	surface: no leading zero, and nothing outside a ref's range.
	"""

	asked = [
		"#916", "916", "#916 dentist", "dentist #916", "#0916", "# 916", "#", "#916#917",
		"  #916  ", "#0", "#2147483647", "#2147483648", "",
	]
	answered = _views(tmp_path, [("refAsked", one) for one in asked])

	assert dict(zip(asked, answered, strict=True)) == {
		"#916": 916,
		"916": None,
		"#916 dentist": None,
		"dentist #916": None,
		"#0916": None,
		"# 916": None,
		"#": None,
		"#916#917": None,
		"  #916  ": 916,
		"#0": None,
		# `refs.MAX_REF`, so an out-of-range number is an ordinary search rather than a lookup
		# the database refuses.
		"#2147483647": 2147483647,
		"#2147483648": None,
		"": None,
	}


def test_every_entry_is_named_the_way_a_person_reads_it (tmp_path: pathlib.Path) -> None:
	"""`SR#980`, Simon 2026-08-18, reversing `SR#979` the same day.

	`SR#979` labelled workspaces by slug because a title is not unique — and accepted titles for
	the projects underneath, where the same is true of two siblings. What it produced was
	`projects` above `Inbox` and `Web UI`: **two registers in one list**, which is `SR#912`
	verbatim, reintroduced by the fix for something else.

	**Alphabetically**, which the roster is not: `workspaces.readable` orders by `created_at`.
	"""

	[shown] = _views(tmp_path, [("placesToGo", {
		"workspaces": SPACES, "projects": [], "showing": {"agenda": True},
	})])

	assert [one["label"] for one in shown] == [
		"All workspaces", "Acme", "Personal", "Personal",
	]

	# **The value is an address**, so `goTo` reads it back through `parseAddress` and the thing
	# chosen is the thing that ends up in the bar — `SR#959`'s argument for the row labels.
	assert [one["value"] for one in shown] == ["", "/acme", "/personal", "/projects"]

	# On the agenda nothing else is selected, which is what `SR#969` settled: the control says
	# what is showing, and what is showing at `/` is every workspace.
	assert [one["chosen"] for one in shown] == [True, False, False, False]


def test_two_workspaces_with_one_name_are_still_two_destinations (
	tmp_path: pathlib.Path,
) -> None:
	"""What `SR#979` met, and what is accepted about it rather than designed around.

	Two workspaces sharing a title read alike, which is a **data** fault — this instance had one,
	and `SR#981` is that nothing can correct it after creation. What must stay true is that
	neither becomes unreachable: the label is for reading and **the value is an address**, so
	each entry still goes somewhere definite.

	**Asserted on the values, because the labels agreeing is the premise.** A version that
	deduplicated entries, or that let one shadow the other, would leave a workspace with no way
	in — which is the failure worth guarding, where reading alike is a rename away from fixed.
	"""

	[shown] = _views(tmp_path, [("placesToGo", {
		"workspaces": SPACES, "projects": [], "showing": {"agenda": True},
	})])
	alike = [one for one in shown if one["label"] == "Personal"]

	assert len(alike) == 2, "the fixture no longer holds the collision this is about"
	assert sorted(one["value"] for one in alike) == ["/personal", "/projects"], (
		"two workspaces reading alike collapsed into one destination"
	)


def test_only_the_workspace_you_are_in_offers_its_projects (tmp_path: pathlib.Path) -> None:
	"""Simon's answer of 2026-08-17, and it is a measurement rather than a preference.

	Projects arrive one workspace at a time — `scoped()` pins `workspace_id`, and `words(slug)`
	returns early without one — so on the agenda the app holds none, and offering every
	workspace's would cost a request per workspace on load.

	**Two assertions, because one of them proves the wrong thing on its own.** That the current
	workspace has its projects says nothing about whether the others wrongly share them; the
	fixture has three workspaces and one project list, so a version that appended it to each
	would pass the first and fail the second.
	"""

	[inside] = _views(tmp_path, [("placesToGo", {
		"workspaces": SPACES, "projects": FILABLE,
		"showing": {"workspace": "projects", "project": None, "agenda": False},
	})])

	assert [(one["label"], one["depth"]) for one in inside] == [
		("All workspaces", 0),
		("Acme", 0),
		("Personal", 0),
		("Personal", 0),
		("Alpha", 1),
		("Inbox", 1),
		("Substation", 1),
		("Packaging", 2),
	], "one register throughout, workspaces and projects alike — `SR#980`"

	# The tree hangs off the *second* `Personal`, which is the one slugged `projects` — a title
	# may repeat, so which entry is which is settled by the slug and not by where it landed.
	assert inside[3]["value"] == "/projects"

	# **The Inbox sorts by its name here**, unlike in the add form: this control is not choosing
	# a destination, so there is no default to keep in view.
	assert [one["label"] for one in inside[4:7]] == ["Alpha", "Inbox", "Substation"]


def test_a_nested_project_is_offered_by_its_whole_address (tmp_path: pathlib.Path) -> None:
	"""A `key` is unique only among its siblings since `SR#958`, so it cannot address anything.

	`path` would say it exactly and is **not selectable** — measured by `SR#770` when it wrote
	that request — so the address is rebuilt from the ancestry the tree walk already has.

	**The fixture nests a project whose own key is ambiguous**, because a one-level fixture
	passes against an implementation that just uses `key` and never notices.
	"""

	[shown] = _views(tmp_path, [("placesToGo", {
		"workspaces": SPACES,
		"projects": [
			{"key": "substation", "title": "Substation", "depth": 0},
			{"key": "dist", "title": "Packaging", "depth": 1},
			{"key": "websites", "title": "Websites", "depth": 0},
			{"key": "dist", "title": "Handouts", "depth": 1},
		],
		"showing": {"workspace": "projects", "project": "substation/dist", "agenda": False},
	})])
	addressed = {one["label"]: one["value"] for one in shown}

	assert addressed["Packaging"] == "/projects/substation/dist"
	assert addressed["Handouts"] == "/projects/websites/dist"

	# **And the one the reader is inside is the one marked**, which two projects keyed `dist`
	# is exactly the case that separates addressing from naming.
	assert [one["label"] for one in shown if one["chosen"]] == ["Packaging"]


def test_a_project_tree_is_alphabetical_within_each_parent (tmp_path: pathlib.Path) -> None:
	"""`SR#974`, Simon's. What arrives is creation order wearing a tree order's clothes.

	`projectsRequest` asks for `order=path`, and `path` is composed of ancestor **ids** — uuid7,
	which lead with a timestamp. Measured on the live instance before this was built: `Null
	sweep` after `Subroutine` at the root, and `Web UI` before `Release and hosting` beneath it.

	**The fixture is deliberately three levels deep with a deep branch out of order**, because
	the two cheap wrong implementations both pass on anything shallower. A flat `sort` by title
	passes a one-level fixture; a sort that ignores parentage passes a two-level one where the
	children happen to fall after their own parent alphabetically. Only a third level, whose
	names sort *before* their grandparent's siblings, tells a real reassembly from either.
	"""

	# Pre-order as the server sends it, so a parent is immediately followed by its own subtree.
	arriving = [
		{"key": "sub", "title": "Subroutine", "depth": 0},
		{"key": "ui", "title": "Web UI", "depth": 1},
		{"key": "zebra", "title": "Zebra", "depth": 2},
		{"key": "alpha", "title": "Alpha", "depth": 2},
		{"key": "ops", "title": "Release and hosting", "depth": 1},
		{"key": "null", "title": "Null sweep", "depth": 0},
		{"key": "web", "title": "Websites", "depth": 0},
	]

	[ordered] = _views(tmp_path, [("treeOrdered", {"projects": arriving})])

	assert [one["title"] for one in ordered] == [
		"Null sweep",
		"Subroutine",
		"Release and hosting",
		"Web UI",
		"Alpha",
		"Zebra",
		"Websites",
	]

	# **Still a pre-order**, which is what the indentation depends on: a child may never appear
	# before the parent it is indented under. Asserted structurally rather than by reading the
	# list above, because that list is what a wrong implementation would also be edited to say.
	seen: list[str] = []

	for one in ordered:
		assert one["depth"] <= len(seen), f"{one['title']} is indented under nothing"

		seen[one["depth"]:] = [one["title"]]


def test_a_project_list_that_is_not_a_tree_is_left_exactly_as_it_came (
	tmp_path: pathlib.Path,
) -> None:
	"""The premise is `order=path`; when it does not hold, the server's order is the answer.

	A row indented deeper than the one before it can be placed under nothing, so there is no
	tree to rebuild. **Returning it untouched is not defensiveness** — a confidently assembled
	wrong tree would indent items under parents they are not in, which reads as fact.
	"""

	broken = [
		{"key": "a", "title": "Alpha", "depth": 0},
		{"key": "c", "title": "Charlie", "depth": 2},
		{"key": "b", "title": "Bravo", "depth": 0},
	]

	[left] = _views(tmp_path, [("treeOrdered", {"projects": broken})])

	assert [one["title"] for one in left] == ["Alpha", "Charlie", "Bravo"]


def test_the_form_puts_the_inbox_first_however_its_name_sorts (
	tmp_path: pathlib.Path,
) -> None:
	"""Simon's answer of 2026-08-17, asked whether *within its parent* included the Inbox.

	This control chooses where an item goes and the Inbox is what happens if you say nothing, so
	burying the default halfway down an alphabetical list makes the form worse at its one job.

	**The fixture renames it**, because an Inbox called `Inbox` sorts near the front on most
	instances by luck — so a version of this that did nothing at all would pass against the
	seeded name. `Zed` is where an unpinned one would land.
	"""

	[said] = _views(tmp_path, [("filableFor", {"project": None, "projects": [
		{"key": "alpha", "title": "Alpha", "depth": 0},
		{"key": "inbox", "title": "Zed", "is_inbox": True, "depth": 0},
		{"key": "beta", "title": "Beta", "depth": 0},
	]})])

	assert [one["label"] for one in said] == ["Zed (default)", "Alpha", "Beta"]


def test_promoting_the_inbox_takes_anything_filed_under_it (tmp_path: pathlib.Path) -> None:
	"""A project can be re-parented (`SR#44`), so the Inbox may one day have children.

	Lifting a parent out of a pre-order list without them would leave those children indented
	under whatever happened to precede them — which is not a cosmetic fault: the indentation is
	the only thing saying what a project is inside.
	"""

	[said] = _views(tmp_path, [("filableFor", {"project": None, "projects": [
		{"key": "alpha", "title": "Alpha", "depth": 0},
		{"key": "under", "title": "Under alpha", "depth": 1},
		{"key": "inbox", "title": "Zed", "is_inbox": True, "depth": 0},
		{"key": "kept", "title": "Kept", "depth": 1},
	]})])

	assert [one["label"] for one in said] == [
		"Zed (default)", "\u00a0\u00a0Kept", "Alpha", "\u00a0\u00a0Under alpha",
	]


def test_two_projects_with_one_key_are_told_apart (tmp_path: pathlib.Path) -> None:
	"""`SR#977`: a key stopped identifying a project at `SR#958`.

	It is unique among its *siblings* now, so a workspace holding `substation/dist` beside
	`websites/dist` rendered two options carrying the value `dist`. Either one was **refused** —
	`selection.addressed` turns a name matching several projects into a refusal listing the
	candidates, which is `SR#957` working — so the fault was never a misfiled item. It was a
	form that could not file into either project, because it was sending the wrong string.

	**The claim is that the values are distinct and are addresses**, not that the labels differ:
	two projects may legitimately share a title as well, and a label is not what gets sent.

	**No fixture here built this case before**, which is why the defect survived a suite that
	covers this function four other ways — every one of them with keys that happened to be
	unique.
	"""

	projects = [
		{"key": "substation", "title": "Substation", "depth": 0},
		{"key": "dist", "title": "Distribution", "depth": 1},
		{"key": "websites", "title": "Websites", "depth": 0},
		{"key": "dist", "title": "Distribution", "depth": 1},
	]

	offered, chosen = _views(tmp_path, [
		("filableFor", {"projects": projects, "project": None}),
		("filableFor", {"projects": projects, "project": "websites/dist"}),
	])

	assert [one["key"] for one in offered] == [
		"substation", "substation/dist", "websites", "websites/dist",
	]

	values = [one["key"] for one in offered]

	assert len(set(values)) == len(values), "two options carry one value, so neither resolves"

	# **Chosen by address**, and the count matters as much as the flag: comparing against the
	# bare key matches nothing here, and the not-in-the-listing fallback then *prepends* a fifth
	# option — so a version that still reads the key fails on the length before the flag.
	assert [one["chosen"] for one in chosen] == [False, False, False, True]


def test_a_new_item_goes_where_the_address_says (tmp_path: pathlib.Path) -> None:
	"""Simon's requirement, verbatim: *if a project is already selected (in URL), that is default
	project for the item to be added to*.

	`SR#738` had already settled the principle — `/{workspace}/{project}` says where rows come
	from, so it says where a new one goes — so nothing new is parsed and this is only the wire.

	**Pure so it can be checked at all.** It was an expression inside the markup first, and the
	harness flattens attributes, so which option carried `selected` was invisible to every test
	here — a closing condition of the item with nothing behind it. Lifting the decision out is
	`SR#640`'s cheapest route and this is the fifth time it has been the answer.
	"""

	projects = [
		{"key": "inbox", "title": "Inbox", "is_inbox": True},
		{"key": "ui", "title": "Interface"},
	]

	named, none, missing = _views(tmp_path, [
		("filableFor", {"projects": projects, "project": "ui"}),
		("filableFor", {"projects": projects, "project": None}),
		("filableFor", {"projects": projects, "project": "gone"}),
	])

	assert [one["chosen"] for one in named] == [False, True], "the address's project is not chosen"

	# **No project in the address is the Inbox**, which is where an item with no project lands
	# anyway — so the control agrees with what would happen if it were not there.
	assert [one["chosen"] for one in none] == [True, False]

	# **A project the address names and the listing does not hold is added rather than dropped.**
	# Nothing chosen means the browser selects the first option, so the item would file into the
	# Inbox under an address naming somewhere else — wrong, and silent, which is worse than any
	# refusal. Reachable: this asks for 200 projects and a workspace may hold more.
	assert missing[0] == {"key": "gone", "label": "gone", "chosen": True}
	assert [one["chosen"] for one in missing[1:]] == [False, False]


#: Fields of `POST /v1/tasks` the add form deliberately does not offer. **Each says what would
#: make the entry go away**, which is the property every allow-list in this repository is held
#: to — an excuse with no expiry is indistinguishable from an oversight.
NOT_ON_THE_FORM = {
	# The capture line *is* the title, and sending both with one empty is refused by name.
	# Goes away if the line ever stops being the primary path, which §1.4 forbids.
	"title": "the capture line is the title (§1.4)",
	# Sub-tasks are not a browser concept yet, and `SR#17`/`SR#44` are open on what membership
	# even means — whether a parent is `blocks` or `parent_task_id` is undecided.
	"parent_task_id": "sub-tasks are undecided — SR#17, SR#44",
	# **Derived from the value rather than chosen.** Measured: `2026-08-14` is stored as the end
	# of that day and all-day, `2026-08-14T15:00` is stored at 15:00 and not. A checkbox beside
	# each date would be a control whose only effect is to contradict the field next to it.
	"due_is_all_day": "derived from whether the date carries a time",
	"snoozed_is_all_day": "derived from whether the date carries a time",
	"starts_is_all_day": "derived from whether the date carries a time",
	# **A gap rather than a decision, so it is filed** (`SR#1211`, and `SR#1234` is the item).
	# A reminder is set on an existing item far more often than at creation — you file a
	# birthday, then decide you want telling — so the add form is the *less* useful half and the
	# item page is where it belongs. The browser has no per-field control for it either, which
	# is `SR#1218`'s shape rather than a line on this form.
	#
	# **Deleting this entry is what closes `SR#1234`.**
	"reminder": "the browser cannot set or show one at all yet — SR#1234",
	# **A gap rather than a decision, so it is filed** (`SR#576`, and `SR#1238` is the item),
	# and the same shape as `reminder` above: the field landed on the model, both clients, the
	# terminal, MCP and the calendar feed in one commit, and a surface's worth of controls is
	# its own. **The end has no all-day flag to go with it** — decision `SR#1235`, it shares
	# `starts_is_all_day` — so this is one control rather than the pair above.
	#
	# **Deleting this entry is what closes `SR#1238`.**
	"ends": "the browser cannot set or show a span yet — SR#1238",
	# The chain is explicit -> user -> workspace -> instance and null means *not stated* at every
	# level. A form field would be a fourth place to get it wrong, on the one surface that
	# already knows the reader's zone.
	"timezone": "the timezone chain answers this without asking",
	# **A decision now rather than a gap** (`SR#94`). `recurrence` and `recurrence_anchor` are on
	# the form, behind a *Repeats* disclosure and beside the phrase preview
	# `POST /v1/recurrence/parse` exists to serve — the two entries that used to be here went
	# when the control landed, which is what a written excuse naming its own removal is for.
	#
	# `recurrence_trigger` stays off, and for the reason the CLI and MCP left it off too: only
	# `completion` is built, and a control offering one accepted value and one that is refused
	# by name is a control with nothing to decide — `SR#251`'s inert control, drawn. It arrives
	# with `SR#916`, when a date-ranged view gives `time` somewhere to be visible.
	"recurrence_trigger": "SR#94 — only one value is built; a control would have no choice to "
	"offer. Lands with SR#916.",
}


def test_every_control_the_form_draws_is_one_the_body_reads () -> None:
	"""**The rule right, the display right, and no wire between them** — `SR#640`'s exact shape.

	`filed` reads controls by name off the submitted form; `Adding` writes those names into the
	markup. Nothing joins the two, so a control called `plannedFor` beside a rule expecting
	`starts_at` is a field that silently never arrives: the reader fills it in, the item is
	created, and the value is gone. Every fault this app has shipped looked like that, and each
	was found by Simon rather than by the build.

	Both directions, because they fail differently. A name the form draws and the body ignores
	is a dead control; a name the body expects and the form never draws is a rule with nothing
	to apply to.
	"""

	app = _served_modules()["app.js"]
	# **Each form against its own rule** (`SR#761`). A document's fields are a different set
	# from a task's — no priority, no dates, no estimate, no assignee — so one slice spanning
	# both would compare each against the other's list and pass on the union.
	form = app[app.index("export function Fields ("):app.index("export const CAPTURE_HINT")]

	drawn = set(re.findall(r'name="(\w+)"', form)) | set(re.findall(r"name=\$\{(\w+)\}", form))
	# `name=${name}` is a control built by one of the small helpers, so what it draws is whatever
	# its caller passed — read those call sites rather than treating the parameter as a field.
	#
	# **Naming the helpers is a list, and a fourth one would fall off it** — but it falls off in
	# the safe direction: the field then appears in `read` and not in `drawn`, and this fails
	# saying the body reads something the form never draws. It has caught its own extraction
	# falling behind twice — first with two of the three helpers, then when the three dates
	# stopped being call sites and became a table.
	drawn.discard("name")
	drawn |= set(re.findall(r'(?:day|rank|vocabularySelect)\("(\w+)"', form))

	dates = re.search(r"export const DATE_FIELDS = \[(.*?)\n\];", app, re.S)

	assert dates is not None, "DATE_FIELDS is gone, so the dates are not being counted"

	drawn |= set(re.findall(r'\["(\w+)",', dates.group(1)))

	found = re.search(r"export const SAID_AS_WRITTEN = \[(.*?)\];", app, re.S)
	numbers = re.search(r"export const SAID_AS_NUMBERS = \[(.*?)\];", app, re.S)

	assert found and numbers, "the field lists are gone, so this is checking nothing"

	always = re.search(r"export const NEVER_CLEARED = \[(.*?)\];", app, re.S)

	assert always is not None, "NEVER_CLEARED is gone, so `title` is not being counted"

	# **A fourth register, because the rule joining its two names is *both or neither*** (`SR#94`).
	# The anchor qualifies the rule and the service refuses it alone (`SR#918`), so the repeat
	# fields are read by `repeating` rather than by either loop — and a name consumed only
	# inside a function body is invisible here, which is how this guard first met them.
	repeated = re.search(r"export const REPEATED = \[(.*?)\];", app, re.S)

	assert repeated is not None, "REPEATED is gone, so the repeat's controls are not counted"

	read = set(re.findall(
		r'"([^"]+)"',
		found.group(1) + numbers.group(1) + always.group(1) + repeated.group(1),
	))
	read |= {"tags"}
	# `title` and `text` are the naming control, drawn by `Editing` and `Adding` rather than by
	# `Fields`, so they are not in the slice above.
	read -= {"title"}

	assert drawn, "the form draws no named control at all, so this is checking nothing"
	assert drawn == read, (
		f"the form draws {sorted(drawn - read)} that the body ignores, and the body reads "
		f"{sorted(read - drawn)} that the form never draws"
	)

	# **The document form against its own rule** (`SR#761`). Checking it against a task's list
	# would pass on the union and say nothing about either.
	papers = app[
		app.index("export function DocumentFields ("):app.index("export function Adding (")
	]
	on_paper = set(re.findall(r'name="(\w+)"', papers))
	on_paper |= set(re.findall(r'pick\("(\w+)"', papers))
	on_paper.discard("name")

	document = re.search(r"export const DOCUMENT_SAID = \[(.*?)\];", app, re.S)

	assert document is not None, "DOCUMENT_SAID is gone, so the document form checks nothing"

	wanted = set(re.findall(r'"([^"]+)"', document.group(1)))

	assert on_paper, "the document form draws nothing, so this is checking nothing"
	assert on_paper == wanted, (
		f"the document form draws {sorted(on_paper - wanted)} that `written` ignores, and it "
		f"reads {sorted(wanted - on_paper)} that the form never draws"
	)


def test_a_page_can_tell_the_instance_has_been_redeployed_under_it (
	tmp_path: pathlib.Path,
) -> None:
	"""`#785`. The rule, before anything is wired to it.

	**Moved rather than newer**, which is not a shortcut. A rollback changes the served asset
	exactly as a release does, and a page left on the version that was rolled back is the same
	problem pointing the other way — where an ordering over `0.7.6.dev70+g72240d9c8` is not
	something this app can invent.

	**Both halves must be known.** An instance older than `#381` publishes no
	`instance_version`, and `null` against a string is a missing field rather than a release.
	Offering a reload on that would fire on every load against such an instance and never stop,
	which is worse than saying nothing.
	"""

	same, moved, back, unknown, missing = _views(tmp_path, [
		("releaseMoved", {"served": "0.7.6", "reported": "0.7.6"}),
		("releaseMoved", {"served": "0.7.6", "reported": "0.7.7"}),
		("releaseMoved", {"served": "0.7.7", "reported": "0.7.6"}),
		("releaseMoved", {"served": None, "reported": "0.7.6"}),
		("releaseMoved", {"served": "0.7.6", "reported": None}),
	])

	assert same is False
	assert moved is True
	assert back is True, "a rollback serves a different asset and the page is still stale"
	assert unknown is False, "an unknown baseline was read as a release"
	assert missing is False, "an instance that publishes no version was read as a release"


def test_the_release_check_rides_the_poll_about_once_an_hour () -> None:
	"""The cadence, asserted against the poll it rides rather than written down twice.

	The item's own measurement is that one extra request an hour against a 600-a-minute
	allowance costs nothing, and that *a release is not something that happens between two
	glances at a page*. Both fall over if this number and `POLL_MS` drift apart, and neither
	is checked by anything that renders.
	"""

	source = _without_prose(_served_modules()["app.js"])
	poll = re.search(r"const POLL_MS = (\d+);", source)
	every = re.search(r"const RELEASE_CHECK_POLLS = (\d+);", source)

	assert poll is not None and every is not None, "the poll or its release check is gone"

	minutes = int(poll.group(1)) * int(every.group(1)) / 60_000

	assert 45 <= minutes <= 90, f"the release check runs every {minutes:g} minutes"


def test_a_note_can_carry_something_to_do_about_it (tmp_path: pathlib.Path) -> None:
	"""`#785`. News with an action is what a `Note` already is — `undo` is exactly that shape.

	**Not a modal**, which is the house rule for news and the one thing the item asked not to
	build: a sentence beside the work with the action in it. So the release notice is one more
	label on the component that exists rather than a second component that has to agree with it.

	The plain note is rendered beside it, because a button appearing on *every* note would be
	the opposite defect and a test of the new one alone cannot see that.
	"""

	acting, plain = [
		rendered["Note"]
		for rendered in (
			_rendered(tmp_path, {"Note": {"note": {
				"text": "A new version of this page is available.",
				"tone": "good",
				"act": {"label": "Reload"},
			}}}),
			_rendered(tmp_path, {"Note": {"note": {"text": "Saved.", "tone": "good"}}}),
		)
	]

	assert "A new version of this page is available." in acting
	assert "Reload" in acting, f"the note carries no way to act on it: {acting}"
	assert "Reload" not in plain and "Saved." in plain, (
		f"an ordinary note grew a button it was not given: {plain}"
	)


def test_the_page_offers_a_reload_and_never_takes_one () -> None:
	"""`#785`'s other half, and it is asserted over the source for a stated reason.

	The wiring lives in `App`, which uses hooks, so `tests/dom.js` cannot call it at all
	(`#640`) — and the behaviour fires **once an hour**, which is not something a browser test
	can wait for either. So what is checked is that the reload is reachable only from a
	handler: `window.location.reload()` appears exactly once and inside the notice's `act`.

	**Never reloading by itself** is the requirement. Somebody may be halfway through an edit
	form, and `#757` went to some trouble to make sure their typing survives a conflict;
	throwing it away for a version bump is the same loss from a friendlier direction.
	"""

	source = _without_prose(_served_modules()["app.js"])
	reloads = re.findall(r"window\.location\.reload\(\)", source)

	assert len(reloads) == 1, f"the page reloads itself from {len(reloads)} places"

	notice = source[source.index("released && html"):]

	assert "window.location.reload()" in notice[:600], (
		"the only reload is not the one behind the release notice's button"
	)
	assert "releaseMoved" in source, "nothing compares the version that served this page"


def test_the_release_check_is_actually_inside_the_poll () -> None:
	"""**Found by falsifying, and the falsification is why this exists.**

	Replacing the hourly condition with `if (false)` left every other guard in this file green:
	the rule was right, the notice was right, and nothing asked whether anything ever ran it.
	`#640` in its own commit — and `#964` is the item about how often this shape has shipped.

	The interval body is read rather than the file, because `releaseMoved` appearing *somewhere*
	is what the version this replaces already asserted. Four reads, and each is the thing the
	mutation removed: the counter advances, it is compared against the constant, the answer
	goes through the rule, and the rule's `true` reaches the state the notice renders from.
	"""

	source = _without_prose(_served_modules()["app.js"])
	opened = source.index("const tick = setInterval(async () => {")
	body = source[opened:source.index("}, POLL_MS);", opened)]

	for wanted in (
		"polled.current += 1",
		"RELEASE_CHECK_POLLS",
		"releaseMoved(",
		"setReleased(true)",
	):
		assert wanted in body, (
			f"the poll never reaches {wanted!r}, so the release check runs on no schedule"
		)


def test_the_form_can_set_every_field_the_endpoint_accepts () -> None:
	"""`SR#756` is titled *with every field it can have*, so the claim is derived, not asserted.

	`SR#427`'s lesson exactly, applied to the browser rather than to `clients/http.py`: a guard
	comparing *names* misses a capability that is a field on a call both surfaces already make.
	Four defects of that shape shipped before the reach guard compared fields — and the browser
	is a client too, and is in none of it.

	Read off `api.tasks.Create` rather than from a list here, so a twentieth field added to the
	endpoint fails this until somebody has decided whether the form offers it.
	"""

	app = _served_modules()["app.js"]

	def listed (name: str) -> list[str]:
		"""Read one exported array out of the served app, by its name."""

		found = re.search(rf"export const {name} = \[(.*?)\];", app, re.S)

		assert found is not None, f"{name} is no longer declared, so this is checking nothing"

		return re.findall(r'"([^"]+)"', found.group(1))

	# `REPEATED` is read by `repeating` rather than by either loop, because its two names travel
	# together or not at all (`SR#94`, `SR#918`) — but they are controls the form draws and
	# fields the endpoint accepts, which is the only question here.
	offers = set(listed("SAID_AS_WRITTEN")) | set(listed("SAID_AS_NUMBERS"))
	offers |= set(listed("REPEATED"))
	offers |= {"text", "tags", "workspace_id"}

	accepted = set(subroutine.api.tasks.Create.model_fields)
	missing = accepted - offers - set(NOT_ON_THE_FORM)

	assert accepted, "the endpoint declares no fields, so this is checking nothing"
	assert not missing, (
		f"POST /v1/tasks accepts {sorted(missing)} and the browser's form offers no way to set "
		f"them — add a control, or an entry to NOT_ON_THE_FORM saying what would remove it"
	)

	stale = set(NOT_ON_THE_FORM) - accepted

	assert not stale, (
		f"NOT_ON_THE_FORM excuses {sorted(stale)}, which the endpoint no longer accepts"
	)


def test_a_failed_write_says_what_happened_in_words (tmp_path: pathlib.Path) -> None:
	"""`SR#102`: no information exists only in a colour.

	A refused write is the case a reader most needs told, and `.note.bad` is a border. The
	sentence has to carry it — and the element has to interrupt a screen reader, which is what
	separates a failure from a confirmation.
	"""

	rendered = _rendered(tmp_path, {
		"Note": {"note": {"text": "#42 was not changed. Not permitted.", "tone": "bad"}}
	})

	assert "was not changed" in rendered["Note"], "the failure said nothing a reader can read"


def test_undo_restores_the_status_that_was_there (tmp_path: pathlib.Path) -> None:
	"""**Undo puts back what was, rather than writing `open`.**

	A status is workspace vocabulary — `status:write` curates it — so `open` is only the
	seeded default and an item may well have been in something else. Reversing a completion by
	writing a guessed status is a different edit from the one being undone, and it would look
	right on every instance that never renamed anything, which is every instance we own.

	**This used to read the source and now drives the request** (`SR#640`). It said so at the
	time: what would make it behavioural is lifting the request each action makes into a pure
	function, and that has happened — so the body the instance would receive is asserted on here
	rather than a spelling that happens to appear in the file.
	"""

	requests = _built(tmp_path, [
		("completeRequest", [{"ref": 42}, "personal"]),
		("restoreRequest", [{"ref": 42, "status": "in progress"}, "personal"]),
	])

	done, back = requests

	assert done["method"] == "POST" and done["path"].startswith("/tasks/42/complete"), (
		f"completing is no longer the endpoint built for it: {done}"
	)
	assert done.get("body") is None, "completing chose a status; the endpoint decides that"

	assert back["method"] == "PATCH", f"undo is no longer a write to the task: {back}"
	assert back["body"] == {"status": "in progress"}, (
		f"undo sent {back.get('body')} rather than the status it recorded — an item that was "
		f"not in the seeded default would come back as something it never was"
	)


def test_every_write_goes_through_one_request_function () -> None:
	"""One `fetch`, reads and writes together — so the credential rules are written once.

	`credentials: "same-origin"` is what attaches the session, and a second request path would
	be a second place to get that right. The read half already relied on this; the write half
	is when it starts to matter.
	"""

	app = _without_comments(_served_modules()["app.js"])

	assert app.count("fetch(") == 1, "a second request path has appeared"
	assert app.count("credentials:") == 1


# ---- addresses (`SR#638`) ------------------------------------------------------------------


def _addressing (tmp_path: pathlib.Path, calls: list[tuple[str, typing.Any]]) -> list[typing.Any]:
	"""Run the app's own address functions in Node, and return what each answered.

	**Pure functions, so they can be driven directly** — which is `SR#640`'s point applied
	rather than only recorded: the routing *decisions* live outside the component, so the
	harness can check them without a DOM even though it cannot touch `App`.
	"""

	module = _staged(tmp_path)

	return list(_ran(tmp_path, f"""
		import * as app from "{module.as_uri()}";

		const calls = {json.dumps(calls)};

		process.stdout.write(JSON.stringify(calls.map(([name, argument]) =>
			name === "parseAddress" ? app.parseAddress(argument)
			: name === "mentionHref" ? app.mentionHref(argument)(42)
			: name === "encodedPath" ? app.encodedPath(argument)
			: name === "frame" ? app.frame(argument.showing, argument.open)
			: name === "withShowing" ? app.withShowing(argument.path, argument.showing)
			: name === "projectLabel" ? app.projectLabel(argument.item, argument.place)
			: name === "soleStatusIn" ? app.soleStatusIn(
				argument.vocabulary, argument.kind, argument.category)
			: name === "marks" ? app.marks(
				argument.item, argument.showKind, argument.ordering, argument.place,
				argument.linkable, argument.hideStatus, argument.hideAssignee)
			: app.addressOf(argument.item, argument.workspace, argument.place || null))));
	"""))


def test_an_item_has_a_readable_address_and_a_durable_one (tmp_path: pathlib.Path) -> None:
	"""The readable form carries the project; the durable one cannot, because a key is renameable.

	`sr` became `subroutine` across 502 items on 2026-08-08, which is the whole argument: a
	project key is a rendering and a workspace slug is not — the slug cannot be renamed at all
	(`SR#295`), deliberately, because it is the middle of every address anybody wrote down.
	"""

	readable, bare, mention = _addressing(tmp_path, [
		("addressOf", {"item": {"ref": 42, "project_key": "ui"}, "workspace": "projects"}),
		("addressOf", {"item": {"ref": 42, "project_key": None}, "workspace": "projects"}),
		("mentionHref", "projects"),
	])

	assert readable == "/projects/ui/42"
	assert bare == "/projects/42", "an item with no project got something other than the ref"
	assert mention == "/projects/42", "a mention in stored prose used a renameable segment"


def test_a_stale_project_in_an_address_still_finds_the_item (tmp_path: pathlib.Path) -> None:
	"""**The ref is last, so everything before it is decoration.**

	This is what makes the readable form a convenience rather than a liability: a link somebody
	pasted into a message six weeks ago goes on working after a rename, and the app rewrites the
	bar to the current spelling once it has read the item.
	"""

	current, stale, deep = _addressing(tmp_path, [
		("parseAddress", "/projects/ui/42"),
		("parseAddress", "/projects/sr/42"),
		("parseAddress", "/projects/subroutine/ui/42"),
	])

	assert current["ref"] == 42 and current["workspace"] == "projects"
	assert stale["ref"] == 42, "a retired project name broke the address"
	assert deep["ref"] == 42, "extra segments were not ignored, so the path form cannot grow in"

	# **The project is the whole path, and until `SR#958` it was the last segment** (`SR#772`).
	# A key was unique in its workspace, so the last one named a project on its own; it is
	# unique among its siblings now, and `substation/dist` and `websites/dist` are two projects
	# sharing a last segment. `trail` is the same path as segments.
	assert deep["project"] == "subroutine/ui" and deep["trail"] == ["subroutine", "ui"]
	assert current["project"] == "ui" and current["trail"] == ["ui"]


def test_opening_an_item_keeps_the_path_the_reader_is_on (tmp_path: pathlib.Path) -> None:
	"""`SR#772`, Simon 2026-08-10.

	Viewing `/projects/websites/simonholliday-com` and opening the item in it used to leave
	`/projects/simonholliday-com/768` in the bar — the top-level project gone. The address still
	resolved, because everything before the ref is decoration (`SR#638`), so nothing failed and
	the tree the reader had navigated simply disappeared from where they were.

	**And it is the item's own address now, not the reader's route** (`SR#512`). This used to
	rebuild the path out of the one the reader had navigated, keeping it only where its last
	segment matched the item's key — the best available while a row carried a key and nothing
	else. A row states its whole address since `SR#512`, so the borrowed route is second-hand
	information about a fact the row has: it survives every case the old rule covered, and it
	is also right from the agenda, from a whole workspace, and after a mention followed
	somewhere else — the three the old rule had to fall back on.

	**Derived from the row rather than from the project tree**, which is the deciding argument
	and not merely the cheaper one: the tree arrives from a fetch, so a canonical ancestry
	assembled here would make the same click produce a different address depending on whether
	that fetch had landed. Every fault this app has shipped is that shape.
	"""

	nested = {"workspace": "projects", "project": "websites/simonholliday-com",
		"trail": ["websites", "simonholliday-com"], "ref": None}
	item = {"ref": 768, "project_key": "simonholliday-com",
		"project_path": "websites/simonholliday-com"}

	kept, elsewhere, wider, agenda, other = _addressing(tmp_path, [
		("addressOf", {"item": item, "workspace": "projects", "place": nested}),
		# A mention followed into a different project: the path names somewhere this item is not.
		("addressOf", {"item": {"ref": 5, "project_key": "ui", "project_path": "subroutine/ui"},
			"workspace": "projects", "place": nested}),
		# The whole workspace, and then no place at all — the row answers both.
		("addressOf", {"item": item, "workspace": "projects",
			"place": {"workspace": "projects", "project": None, "trail": [], "ref": None}}),
		("addressOf", {"item": item, "workspace": "projects", "place": None}),
		# An agenda row from another workspace addresses its own, and its path travels with it.
		("addressOf", {"item": item, "workspace": "personal", "place": nested}),
	])

	assert kept == "/projects/websites/simonholliday-com/768", (
		"opening an item flattened the tree it is filed in"
	)
	assert elsewhere == "/projects/subroutine/ui/5", (
		"an item borrowed a path naming a project it is not in"
	)
	assert wider == "/projects/websites/simonholliday-com/768", (
		"the whole address is the item's own, so a wider page does not shorten it"
	)
	assert agenda == "/projects/websites/simonholliday-com/768"
	assert other == "/personal/websites/simonholliday-com/768"


def test_an_address_that_names_no_item_is_not_read_as_one (tmp_path: pathlib.Path) -> None:
	"""A path may name a place without naming an item, and the two must not be confused.

	`SR#647` added the first two shapes, and `/projects/ui` is the interesting one: two
	segments, like `/projects/42`, and only the last one being a number tells them apart. Get
	that wrong and a project called `2026` would open item 2026 — which is exactly the reason
	§5.4 forbids a project key that reads as a number, and the reason a ref may not have a
	leading zero.
	"""

	nowhere, workspace, project, item, readable, padded = _addressing(tmp_path, [
		("parseAddress", "/"),
		("parseAddress", "/projects"),
		("parseAddress", "/projects/ui"),
		("parseAddress", "/projects/42"),
		("parseAddress", "/projects/ui/42"),
		("parseAddress", "/projects/007"),
	])

	assert nowhere is None, "the root named a place"

	assert workspace["workspace"] == "projects" and workspace["ref"] is None
	assert workspace["project"] is None

	assert project["project"] == "ui" and project["ref"] is None, (
		"a project was read as an item, so /projects/ui would open something"
	)

	assert item["ref"] == 42 and item["project"] is None
	assert readable["ref"] == 42 and readable["project"] == "ui"

	assert padded["ref"] is None and padded["project"] == "007", (
		"a leading zero was read as a ref; the mention rule forbids one and so does this"
	)


def test_an_address_a_browser_cannot_decode_is_a_miss_rather_than_a_crash (
	tmp_path: pathlib.Path,
) -> None:
	"""A stray percent sign is a typo, and a typo may not take the page down — `SR#681`.

	``decodeURIComponent`` is all or nothing: one malformed escape throws ``URIError`` for the
	whole string. Since `SR#648` **this app is the handler for every address nothing else
	claimed**, so a mistyped URL is its problem rather than the server's — and the throw
	reached the failure page, whose *Retry* re-ran the same parse, or in the ``popstate``
	handler was not caught at all.

	Falling back to the raw segment is safe *because of what it is compared against*: a
	workspace slug maps every non-alphanumeric to ``-`` and a project key is
	``[a-z][a-z0-9]*(?:-[a-z0-9]+)*``, so neither can contain a percent sign. An undecodable
	segment therefore matches nothing, which is a miss — the reader is told the address names
	nowhere, which is true.
	"""

	bare, doubled, trailing, partial = _addressing(tmp_path, [
		("parseAddress", "/%"),
		("parseAddress", "/%zz"),
		("parseAddress", "/personal/100%"),
		("parseAddress", "/personal/%E0%A4%A"),
	])

	assert bare["workspace"] == "%", "a lone percent sign was not returned as written"
	assert doubled["workspace"] == "%zz"
	assert trailing["project"] == "100%" and trailing["workspace"] == "personal"
	assert partial["project"] == "%E0%A4%A"


def test_a_percent_encoded_address_is_still_decoded (tmp_path: pathlib.Path) -> None:
	"""Tolerating a bad escape must not stop a good one being read.

	This is the half a blanket "stop decoding" would have broken, and it is not hypothetical:
	``workspaces.normalize_slug`` keeps anything ``str.isalnum`` accepts, which in Python is
	**Unicode-aware**, so ``Café`` is a legal short name and reaches this function as
	``caf%C3%A9``. The fallback has to be per-address rather than per-application.
	"""

	accented, spaced = _addressing(tmp_path, [
		("parseAddress", "/caf%C3%A9"),
		("parseAddress", "/personal/q3%20plan/42"),
	])

	assert accented["workspace"] == "café", "a legitimate escape stopped being decoded"
	assert spaced["project"] == "q3 plan" and spaced["ref"] == 42


def test_an_undecodable_address_is_reported_rather_than_swapped (tmp_path: pathlib.Path) -> None:
	"""Not crashing is half of it; the other half is saying so.

	The two functions are each already driven — one returns the raw segment, the other refuses
	a workspace nobody has — and **the join between them was not**, which is the shape all four
	of this arc's shipped faults had: the rule right, the display right, and no wire. So this
	runs the real chain rather than asserting the halves again.

	A silent fallback would be the worse failure of the two. `SR#650` was exactly that — an
	address parsed and then ignored — and it reached Simon rather than the build.
	"""

	module = _staged(tmp_path)
	answers = _ran(tmp_path, f"""
		import * as app from "{module.as_uri()}";

		const available = ["projects", "personal"];

		process.stdout.write(JSON.stringify(
			["/100%", "/personal", "/caf%C3%A9"].map((path) =>
				app.chosenWorkspace(app.parseAddress(path), available, "projects"))
		));
	""")

	broken, good, accented = answers

	assert broken == {"slug": "projects", "refused": "100%"}, (
		"a mistyped address was silently swapped for a workspace the reader did not ask for"
	)
	assert good == {"slug": "personal", "refused": None}
	assert accented == {"slug": "projects", "refused": "café"}, (
		"the decoded name did not reach the refusal, so the reader would be shown the escape"
	)


def test_the_app_claims_no_path_it_has_not_been_given () -> None:
	"""**The app answers unmatched addresses, and declares none of them.**

	`SR#648`. The first version declared `/{workspace}`, `/{workspace}/{project}` and two more,
	which claimed paths nothing else had claimed *yet* — and that is the hazard, not an ordering
	mistake:

	* `/{workspace}/{project}` matched `/v1/nothing`, so the API's own 404 became `200
	  text/html`. Five tests in two other modules said so.
	* `/{workspace}` matched `GET /mcp`, which declares only `POST`, replacing a `405` that had
	  been measured against a real client with a page.
	* Routes registered later were shadowed too, including ones `api_support` adds to a built
	  application.

	None of it is fixable by ordering. Answering the 404 inverts the problem: every real route
	wins whenever it was registered, and what is left is by definition unclaimed.
	"""

	declared = {
		path for router in (subroutine.api.web.router,)
		for path in (getattr(route, "path", "") for route in router.routes)
	}

	assert declared == {"/", "/app/{name}"}, (
		f"the app declares {sorted(declared)}; anything beyond its page and its files claims "
		f"addresses nothing else has claimed yet, which is what SR#648 is about"
	)

	# **A catch-all is allowed, and only under an address a workspace can never have.**
	# `/v1/projects/{id_or_key:path}` arrived with `#957` and is fine: it can swallow only
	# things beneath `/v1`, which no browser address reaches. What would recreate `#648` is a
	# catch-all whose *first* segment is spendable, because then it claims a workspace's page.
	#
	# The other half of this — a catch-all swallowing a route registered after it — is
	# `api.routing.swallowed`, which asks it properly rather than banning the converter.
	for _prefix, router in subroutine.api.app.ROUTERS:
		for route in router.routes:
			path = getattr(route, "path", "")

			if ":path}" not in path:
				continue

			first = path.strip("/").split("/")[0]

			assert first in subroutine.addressing.ROUTED_WORKSPACE_WORDS, (
				f"{path} is a catch-all under {first!r}, which a workspace could be named, so "
				f"it would claim that workspace's own page"
			)


def test_only_a_browser_is_given_the_app_for_an_unmatched_address () -> None:
	"""`Accept` is what separates the two readers, and it has to be the literal type.

	A browser navigating asks for `text/html` by name. Every client of this API asks for
	`application/json` — `clients/http.py` sets it, and the app's own `fetch` sets it. `curl`
	sends `*/*`, which would match a looser test and would turn a mistyped path into a page for
	anybody driving the API by hand.
	"""

	assert subroutine.api.web.NAVIGATION == "text/html"

	def asking (accept: str) -> bool:
		"""Say whether a request with this `Accept` header wants the page."""

		request = starlette.requests.Request({
			"type": "http", "method": "GET", "path": "/x", "headers":
				[(b"accept", accept.encode())],
		})

		return subroutine.api.web._navigating(request)

	assert asking("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	assert asking("TEXT/HTML"), "the header is case-insensitive and this reads it literally"
	assert not asking("application/json"), "a client of this API was handed a page"
	assert not asking("*/*"), "curl's default was read as a browser navigation"
	assert not asking(""), "a request with no Accept was read as a browser navigation"


def test_a_mention_becomes_a_link_and_only_a_real_one (tmp_path: pathlib.Path) -> None:
	"""**The pattern is `mentions.REF_PATTERN`, so the browser and the index agree.**

	A `#42` in a description is 2,196 occurrences across 90% of the prose on this instance —
	the highest-traffic link in the product. If the browser underlined things the mention index
	does not know about, the link and the backlink would disagree about what a reference is.
	"""

	module = _staged(tmp_path)
	rendered = _ran(tmp_path, f"""
		import * as markdown from "{(module.parent / "markdown.js").as_uri()}";
		import * as app from "{module.as_uri()}";

		const where = app.mentionHref("projects");
		const sources = {json.dumps([
			"See #42 for why.",
			"`#42` in code is not a link.",
			"#42FF00 is a colour, ##1 is not a ref, issue#1 is not either, and #007 has a zero.",
			"A mention with no workspace to point at.",
		])};

		process.stdout.write(JSON.stringify([
			...sources.slice(0, 3).map((s) => markdown.render(s, where)),
			markdown.render(sources[3] + " #42", null),
		]));
	""")

	assert '<a href="/projects/42" class="mention">#42</a>' in rendered[0]
	assert "<a " not in rendered[1], "a ref inside code became a link"
	assert "<a " not in rendered[2], "something that is not a reference was linked"
	assert "<a " not in rendered[3], "a mention was linked with nowhere to point"


def test_the_browser_and_the_index_share_one_mention_rule () -> None:
	"""Two copies of a rule that must agree is this codebase's signature defect.

	The copy in `markdown.js` is deliberate — the browser cannot import Python — so it is
	compared against the original here rather than trusted to have been kept in step.
	"""

	import subroutine.domain.mentions

	original = subroutine.domain.mentions.REF_PATTERN.pattern
	inside = _served_modules()["markdown.js"]

	# The JavaScript copy is written escaped for `new RegExp`, so it is unescaped before
	# comparing: `\\w` in the source is `\w` in the pattern the engine sees.
	assert original.replace("\\", "\\\\") in inside, (
		f"markdown.js no longer carries {original!r}, so the browser and the mention index "
		f"can disagree about what a reference is"
	)


def test_a_mention_of_something_that_is_not_there_is_a_note (tmp_path: pathlib.Path) -> None:
	"""A dead `#999` must not replace the page a reader is on.

	Prose mentions whatever somebody wrote, and the renderer links every ref without asking
	whether it resolves — checking would be one request per mention on descriptions that carry
	forty. So a link to nothing is reachable by construction, and it has to land somewhere
	survivable. Found by driving: the first version showed the failure page for a typo.

	Source-level for `SR#640`'s reason — the branch is inside `App`, which the harness cannot
	render.
	"""

	app = _without_comments(_served_modules()["app.js"])

	assert "failure.status === 404" in app, "a missing ref stopped being told apart"
	assert app.count("setError(failure)") == 4, (
		"the number of places that replace the whole page changed — each one should be a case "
		"where nothing on screen is worth keeping"
	)

	# **The fourth is the poll, and only for a 401** (`SR#927`'s M-26). Every other failure
	# there is left alone, deliberately — the next poll may work, and replacing a readable page
	# because a background request timed out is worse than being ten seconds stale. A session
	# that has lapsed is the one failure the next poll cannot fix: the page re-rendered the same
	# rows every ten seconds for ever, every control on it refusing, with nothing saying why.
	assert "failure.status === 401" in app, "a lapsed session stopped being told apart"


#: A hook call and the dependency array it ends with, e.g. ``useCallback(…, [load, workspace])``.
#: Matched on the closing bracket rather than the opening paren, because the body between them
#: contains every kind of nesting and this only needs the tail.
_DEPENDENCIES = re.compile(r"\buse(?:Callback|Effect|Memo|LayoutEffect)\(", re.M)


def _deps_after (source: str, start: int) -> tuple[str, int] | None:
	"""Return the dependency array that closes the hook call starting at ``start``."""

	depth = 0

	for index in range(start, len(source)):
		if source[index] == "(":
			depth += 1
		elif source[index] == ")":
			depth -= 1

			if depth == 0:
				tail = source.rfind("[", start, index)

				return (source[tail + 1:source.rfind("]", tail, index)], index) if tail > 0 else None

	return None


def test_nothing_is_named_in_a_dependency_array_before_it_exists () -> None:
	"""**A dependency array is evaluated where it is written, and `const` has a dead zone.**

	This shipped a blank page. The `arrive` effect was written above `show` and listed it as a
	dependency, so the first render threw `ReferenceError: Cannot access 'show' before
	initialization` and the app rendered nothing — no failed request, no 404, nothing in the
	build. The second blank page from this file, and the second one a check of the *served*
	behaviour would have caught.

	The order of the `const`s themselves was checked by hand at the time and was correct. That
	is the trap: the effect is not one of them, so reading the declarations proved nothing about
	the thing that was wrong.

	Narrow on purpose. A name used inside a *body* is fine — that runs later — so only the
	array is examined, which is where the reference is immediate and where this style of
	component puts one every time.
	"""

	source = _served_modules()["app.js"]
	declared: dict[str, int] = {}

	for found in re.finditer(r"^\t*(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=", source, re.M):
		declared.setdefault(found.group(1), found.start())

	problems = []

	for call in _DEPENDENCIES.finditer(source):
		array = _deps_after(source, call.end() - 1)

		if array is None:
			continue

		listed, _closes = array

		for name in re.findall(r"[A-Za-z_$][\w$]*", listed):
			if name in declared and declared[name] > call.start():
				problems.append(f"{name!r} is a dependency of the hook at offset {call.start()}, "
				                f"but is not declared until {declared[name]}")

	assert declared, "no declarations were found, so this is checking nothing"
	assert _DEPENDENCIES.search(source), "no hook calls were found, so this is checking nothing"
	assert not problems, "\n".join(problems)


#: A read of the project filter *itself*. `setProject` is a setter and stable, `asked.project`
#: is a property of a parsed address, and `project_key` is a field on a row — none of them are
#: the state, and none of them belong in a dependency array.
_READS_PROJECT = re.compile(r"(?<![\w$.])project(?![\w$])")


def test_a_hook_that_reads_the_project_filter_declares_it () -> None:
	"""**A list that widens itself ten seconds after it is opened.**

	Open `/{workspace}/{project}` and the page showed that project's seven items, then
	replaced them with the whole workspace — unrelated projects and all — at an address that
	still said the project. It read as data corruption. It was a dependency array.

	`start` calls `setWorkspace` before awaiting the head of the feed and `setProject` after,
	so the filter lands in a later commit than the workspace. The poll effect listed
	`[error, workspace, load]`, so it re-ran on the workspace commit — while the filter was
	still `null` — and the interval it created closed over that `null` permanently. Every
	later poll called `load(workspace, null)`.

	The trap is that nothing is stale on the render. `project` was correct in the markup the
	whole time, which is why the banner still named the project while the list under it did
	not match. Only the callback the render left behind was wrong, and a ten-second timer is
	long enough that the wrong list looks like a considered answer rather than a bug.

	So the rule, and not the one instance of it: read the filter inside a hook and it is a
	dependency of that hook. Written that way it found two more the same day — adding an item
	and asking for more of the list both reloaded the workspace instead of the project.

	Prose is stripped first because this file's own explanations name the filter constantly,
	and so does a message it shows a reader: *"There is no project called …"*. A guard that
	counts its own documentation measures nothing.
	"""

	source = _without_prose(_served_modules()["app.js"])
	problems = []
	reading = 0

	for call in _DEPENDENCIES.finditer(source):
		found = _deps_after(source, call.end() - 1)

		if found is None:
			continue

		listed, closes = found
		body = source[call.end():source.rfind("[", call.end(), closes)]

		if not _READS_PROJECT.search(body):
			continue

		reading += 1

		if not _READS_PROJECT.search(listed):
			problems.append(f"the hook at offset {call.start()} reads the project filter but "
			                f"declares [{listed.strip()}] — its callback will keep whichever "
			                f"filter was current when it last ran")

	assert reading, "no hook reads the project filter, so this is checking nothing"
	assert not problems, "\n".join(problems)


def test_what_is_showing_has_one_writer () -> None:
	"""`SR#766`. What keeps a second copy of a fact from becoming a second answer.

	`load` used to read the selection from `window.location.search`, on `SR#719`'s reasoning: the
	address is written by `go` before any load that changes one, so it cannot lag the way state
	lags the render that calls a callback. Sound while it held.

	**`SR#766` took the premise away.** An item's address carries no query, so while one is open
	the bar says nothing about the listing behind it — and the poll goes on calling `load` every
	ten seconds. Reading the address there would have refetched the default selection under a
	reader who chose another, invisibly, until they closed the item and found the columns they
	asked for empty. That is `SR#744` re-created by the fix for `SR#766`.

	So there is a ref beside the state, and the ref is what a callback reads. Two copies of one
	fact is this codebase's signature defect, and the only thing making it safe is that exactly
	one function writes them — which is what this checks, by finding the writes rather than by
	trusting the sentence above.

	**What this cannot check**, stated rather than implied: the harness calls components as plain
	functions and cannot execute a hook (`SR#640`), so nothing here proves `nowShowing` writes
	the ref *correctly* — only that no other caller writes half the pair. `SR#748` is the item
	for a machine that could answer the rest.
	"""

	source = _without_prose(_served_modules()["app.js"])
	writers = [found.start() for found in re.finditer(r"(?<![\w$.])setShowing\s*\(", source)]

	assert writers, "no write of the showing state was found, so this is checking nothing"

	opens, closes = _braced(source, "const nowShowing = useCallback(")
	inside = source[opens:closes]

	assert "setShowing(" in inside and "shown.current =" in inside, (
		f"nowShowing is meant to write both copies of what is showing; its body is {inside!r}"
	)

	stray = [at for at in writers if not opens <= at < closes]

	assert not stray, (
		f"{len(stray)} call(s) to setShowing sit outside nowShowing, at offsets {stray} — each "
		"one moves the state without moving the ref, so `load` and the render disagree about "
		"which rows this page asked for"
	)


def _braced (source: str, opening: str) -> tuple[int, int]:
	"""The span of the block a declaration opens, found by matching its braces."""

	start = source.index(opening)
	depth = 0

	for index in range(source.index("{", start), len(source)):
		if source[index] == "{":
			depth += 1
		elif source[index] == "}":
			depth -= 1

			if depth == 0:
				return start, index

	raise AssertionError(f"{opening!r} never closes")


def _function_body (source: str, name: str) -> str:
	"""Return the body of one top-level function in the app, by name.

	**The parameter list is skipped rather than walked into, and that is `#860`.** This took
	the first ``{`` after the name, which for a component declared

	    export function Row ({ item, showKind, ... }) {

	is the *destructured parameter list* — so it returned at the brace closing the signature
	and called 114 characters a function body. ``Row`` contributed nothing to the scan below
	for as long as it has existed, while the guard named it first.

	Nothing noticed because the caller's floor asks whether *any* field was found, and the
	other three functions supply sixteen between them. A floor catches a scanner that reads
	nothing and is blind to one that reads most things.
	"""

	start = source.index(f"export function {name} (")
	depth = 0
	cursor = source.index("(", start)

	while True:
		if source[cursor] == "(":
			depth += 1

		elif source[cursor] == ")":
			depth -= 1

			if depth == 0:
				break

		cursor += 1

	depth = 0
	opening = source.index("{", cursor)

	for index in range(opening, len(source)):
		if source[index] == "{":
			depth += 1

		elif source[index] == "}":
			depth -= 1

			if depth == 0:
				return source[opening:index]

	raise AssertionError(f"{name} never closes")


def _item_fields_read (
	source: str, surface: typing.Sequence[str]
) -> tuple[dict[str, str], set[str]]:
	"""Return each named function's body, and every ``item.<field>`` it or a callee reads.

	**One scanner because two guards ask the same question of the same source** — what does the
	browser read off a row — and a second copy would be free to disagree with this one about
	which functions it walked into. `#427`'s method: derive it, do not maintain it.

	The callee walk is one level deep and deliberately so. It is what reaches `overdue`,
	`holding` and `projectLabel`, which is where most of the reads live; anything deeper is a
	helper taking values rather than an item, and following it would be scanning for a shape
	that is not there.
	"""

	bodies = {name: _function_body(source, name) for name in surface}

	for name in surface:
		for called in re.findall(r"\b([a-z][A-Za-z0-9_]*)\s*\(", bodies[name]):
			if called in bodies or f"export function {called} (" not in source:
				continue

			bodies[called] = _function_body(source, called)

	read = set()

	for body in bodies.values():
		read |= set(re.findall(r"\bitem\.([a-z_][a-z0-9_]*)\b", body))

	return bodies, read


#: What `marks` reads that a link's far end deliberately does not carry, and why. A register
#: rather than a silent exclusion, because leaving a field off is a decision about what a reader
#: can judge without opening an item — and the test below refuses an entry naming a field `marks`
#: has stopped reading, so this cannot become a place to park one.
NOT_ON_A_LINK_END = {
	"importance": "the sort value `orderingValue` draws, and an item's links have no ordering "
	"for it to be the value of — `Detail` passes none, so this mark never renders here",
	"urgency": "the other half of the same pair, read by the same function for the same mark",
}


def test_a_links_far_end_carries_every_field_the_marks_read () -> None:
	"""**The list of fields on `views.LinkEnd` is a second copy of what a mark shows.**

	`SR#970`, Simon: *"I cannot look at a task and see whether all of its blockers are complete,
	without looking at each blocker individually."* The fix was to render a link's far end
	through `marks` — the same function a list, a board and the agenda already use — which works
	only while the end carries what that function reads.

	**So the set is derived rather than listed.** A mark added to a row tomorrow fails here
	until the far end can answer it, which is the only shape that keeps four renderings of one
	line together: they had already drifted into four different answers when this was written,
	and `SR#674`'s guard could not see it because it compares an item row against an item row.

	**A name the view models do not have was computed by the app** — `kind` is what this client
	calls `entity_type` and `workspace` is resolved at the merge — so those are excluded by
	*derivation* rather than by being listed, exactly as `SR#860` made the listing guard do it.
	A literal list there grew silently and this would too.
	"""

	source = _served_modules()["app.js"]
	bodies, read = _item_fields_read(source, ["marks"])

	assert read, "no fields were found, so this is checking nothing"
	assert re.search(r"\bitem\.[a-z_]", bodies["marks"]), (
		"marks contributed no field reads of its own, so this guard is scanning something else. "
		"Check what _function_body returned."
	)

	published = set(subroutine.views.Task.model_fields) | set(
		subroutine.views.Document.model_fields
	)
	carried = set(subroutine.views.LinkEnd.model_fields)
	missing = sorted((read & published) - carried - set(NOT_ON_A_LINK_END))

	assert not missing, (
		f"a link's far end cannot answer {missing}, which `marks` reads — so a listing row and "
		f"an item's links would say different things about the same item. Add the field to "
		f"views.LinkEnd, or record in NOT_ON_A_LINK_END why a link line does without it."
	)

	# **The other direction, which is what stops the register above becoming a graveyard.**
	# Every allow-list in this repository has this half (`SR#405`): an entry excusing a field
	# nothing reads any more is a decision recorded about code that has gone, and it reads
	# exactly like a considered one.
	stale = sorted(set(NOT_ON_A_LINK_END) - read)

	assert not stale, (
		f"NOT_ON_A_LINK_END excuses {stale}, which `marks` no longer reads. Delete the entries."
	)


def test_a_listing_asks_for_every_field_its_rows_render () -> None:
	"""**The `fields=` list is a second copy of what a row shows, so it is derived, not trusted.**

	`SR#645`: a whole page of tasks is 287 KB and a whole page of documents is 1.3 MB, because a
	document's body arrives in full. Asking only for what a row renders makes the pair 38 KB.
	The cost of that is a list somebody has to keep in step with `Row`.

	**And forgetting it does not error.** An unrequested field arrives as `null`, and a null
	reads as *not set* — which is the rule `subroutine show` and `Facts` are built on (§12.2c).
	So a row would quietly stop saying an item is blocked, or overdue, or whose it is, and look
	exactly like an item that is none of those.

	Derived from the four functions that are a row's whole surface, **and from anything in this
	module they call** — so adding a field fails here until the request asks for it.

	**That second half was a real hole, met by the change that needed it** (`SR#726`). The scan
	named four functions; moving a decision out of `marks` into `holding` — which is the move
	this project reaches for constantly, because a pure function is the only kind `SR#640`'s
	harness can check — took its `item.` reads out of the scan entirely. So a guard whose whole
	job is to notice a new field stopped noticing, in response to the refactor it should most
	have been watching. One level of resolution closes it, and one level is enough because the
	functions a row's surface calls are pure and shallow by design.

	**Both collections, since `#683`.** Documents render through these same four functions and
	the comparison was against `TASK_FIELDS` alone — `DOCUMENT_FIELDS` appeared nowhere in this
	module. What is missing today is genuinely task-only, so the shipped request was right and
	the gap was here: a field both kinds have would have failed the task half loudly, about a
	defect that was true of both, while the document half went on rendering it as absent.

	**And `Row` was never scanned at all, which is `#860`.** Three of the four functions were,
	and between them they supply sixteen fields — enough for the one floor this test had to
	pass. Fixing it needed a floor *per function*, because that is the only shape that can see a
	scanner which read most things. See :func:`_function_body` for what it was returning.
	"""

	source = _served_modules()["app.js"]
	surface = ["Row", "marks", "when", "overdue"]
	bodies, rendered = _item_fields_read(source, surface)

	assert rendered, "no fields were found, so this is checking nothing"

	# **A floor per function rather than one over the union, which is `#860`.** The single floor
	# above passes on sixteen fields from three functions, so it could not see that `Row` — the
	# one this test is named after — was contributing nothing at all, because the body extractor
	# was returning its parameter list. A floor catches a scanner that read *nothing* and is
	# blind to one that read *most* things; this is that lesson applied to the scanner that
	# taught it. Each of the four genuinely reads the item it is handed, so each must show it.
	silent = sorted(
		name
		for name in surface
		if not re.search(r"\bitem\.[a-z_]", bodies[name])
	)

	assert not silent, (
		f"{silent} contributed no field reads, so {'it is' if len(silent) == 1 else 'they are'} "
		f"named by this guard and not scanned by it. Check what _function_body returned."
	)

	# **Both collections, because both render through these four functions** (`#683`). The
	# comparison was against `TASK_FIELDS` alone and `DOCUMENT_FIELDS` appeared nowhere in this
	# module — so adding a field both kinds have would fail the task half while the document
	# half went on rendering it as absent, which is §12.2c inverted for half the rows.
	# **Collected and reported once rather than asserted per kind**, because a field both
	# collections render is the case this exists for and a sequential assertion would name only
	# the first — which is how the gap looked before `#683`: the task half failing, loudly and
	# alone, about a defect that was true of both.
	missing: dict[str, list[str]] = {}

	for kind, constant, view in (
		("task", "TASK_FIELDS", subroutine.views.Task),
		("document", "DOCUMENT_FIELDS", subroutine.views.Document),
	):
		opening = source.index(f"const {constant} = [")
		asked = set(
			re.findall(r'"([a-z_]+)"', source[opening:source.index('].join(",");', opening)])
		)

		assert asked, f"{constant} was not found, so this is checking nothing"

		# **What this kind of row could ask for, derived rather than listed.** A name the view
		# model does not have is one the app computed for itself after the answer arrived —
		# `kind` says which collection a row came from and `workspace` is resolved from
		# `workspace_id` at the merge — and no endpoint reports either. That was a literal
		# `- {"kind"}` until `#860`, which is a list that grows silently: scanning `Row` for
		# the first time immediately produced a second name for it.
		#
		# It is also what makes the document half honest. Most of what a row renders is
		# task-only, and asking a document's request for `due_at` would be demanding a field
		# that cannot exist — the difference between "has no deadline" and "cannot have one",
		# which is the distinction `DOCUMENT_FIELDS`'s own comment is about.
		reportable = rendered & set(subroutine.api.shaping.selectable(view))
		absent = sorted(reportable - asked)

		if absent:
			missing[f"a {kind} row ({constant})"] = absent

	assert not missing, (
		f"the listing renders fields it does not ask for: {missing} — so every row will show "
		f"them as absent rather than as unknown"
	)


# ---- the list is the whole list, in the same order (`SR#646`) ------------------------------


def test_the_listing_asks_the_same_question_the_command_line_does (
	tmp_path: pathlib.Path
) -> None:
	"""**Two surfaces, one question, and they gave different answers.**

	`subroutine list` sends no `order`, so it gets the API's default: newest first. `app.js`
	asked for `-priority_score`, which §6.3a sorts in three bands with *unranked last* — and an
	item somebody has just captured has neither axis set, so it goes straight to the bottom.
	`SR#642` was 142 of 142 on a page of 100, and its author had been told it was added.

	The rule is not "never sort by priority"; it is that the *default* is one decision and there
	is one of it. A sort control is a different item.

	**An order is a parameter the route declares**, so the guard driving these against a real
	instance cannot see this one — it would be accepted, and answer with the wrong list. That is
	the difference between a request being *legal* and being the *right question*.

	**The satisfier changed with `SR#877` and the intent did not.** This used to assert the
	browser sent *no* order at all, which was the right shape while the command line sent none
	either. Both sink deferred work now, so the question has moved from "does it choose one" to
	"does it choose the same one" — and the answer is read out of the command line rather than
	written down here, because a literal would be a second copy of the very decision this test
	exists to keep single. `-priority_score` still fails it, which is `SR#642`'s half.
	"""

	built = _built(tmp_path, [("listingRequests", ["personal", None, None])])

	assert len(built) == 2, f"expected the two listing requests, found {built}"

	wanted = subroutine.cli.personal._sunk(None)

	assert "priority" not in wanted, (
		f"the command line's default listing order is now {wanted!r}, and SR#642 is what "
		f"happens when a default ranks by priority — this test has stopped checking that"
	)

	for request in built:
		asked = urllib.parse.parse_qs(urllib.parse.urlparse(request["path"]).query).get("order")

		assert asked == [wanted], (
			f"{request['path']} asks for {asked}, and `subroutine list` asks for {wanted!r} — "
			f"so the same question has two answers, and the one that hid SR#642 was this one"
		)


def test_a_listing_that_had_to_stop_says_so (tmp_path: pathlib.Path) -> None:
	"""`has_more` is in every envelope and this app read it nowhere.

	So it showed 100 of 142 and looked complete. The CLI learned to print `…and more` in July;
	the browser was written eleven months later without it. A truncation nobody is told about is
	worse than a short list — it is a *wrong* list that cannot be questioned.
	"""

	rows = [{"ref": 1, "kind": "task", "title": "A task", "status_is_default": True}]

	whole = _rendered(tmp_path, {"Listing": {"items": rows, "more": None}})
	cut = _rendered(tmp_path, {
		"Listing": {"items": rows, "more": {"tasks": "a-cursor", "documents": None}}
	})

	assert "There are more" not in whole["Listing"], "a complete list claimed to be short"
	assert "There are more" in cut["Listing"], "a truncated list said nothing"
	assert "Show more" in cut["Listing"], "there was no way to see the rest"


def test_the_page_size_is_a_page_and_not_a_ceiling (tmp_path: pathlib.Path) -> None:
	"""Whatever was left behind is reachable, so the cursor has to be carried.

	`has_more` without `next_cursor` would be a listing that admits it is short and offers
	nothing — which is the shape §8.4 rejected for `include_total`, arrived at from the other
	direction.
	"""

	# Comments stripped, because this file *documents* why a total is not asked for — and a
	# guard that counts the word in its own explanation measures the prose, not the code.
	source = _without_comments(_served_modules()["app.js"])

	assert "next_cursor" in source, "the cursor stopped being read, so there is no way onwards"
	assert "cursor=" in source, "the cursor is read and never sent"
	assert "include_total" not in source, (
		"a total is being asked for; §8.4 declines it because it costs a second full scan"
	)


def test_a_project_in_the_address_narrows_the_list_and_says_so (tmp_path: pathlib.Path) -> None:
	"""**A filter the reader did not apply has to announce itself.**

	`SR#647`: `/projects/subroutine` shows that project. Nothing on the page put it there — it
	arrived in a link somebody was sent — so a short list with no explanation is indistinguishable
	from an empty backlog, and there is no control for the reader to un-touch. It says what it is
	showing, and offers the whole workspace.

	This is `SR#251`/`SR#303`'s shape read forwards: a filter nobody can see is a control that
	does nothing, from the reader's side.
	"""

	rows = [{"ref": 1, "kind": "task", "title": "A task", "status_is_default": True}]

	whole = _rendered(tmp_path, {"Listing": {"items": rows, "project": None}})
	narrow = _rendered(tmp_path, {
		"Listing": {"items": rows, "project": "ui", "widenTo": "/projects"},
	})

	assert "Showing" not in whole["Listing"], "an unfiltered list claimed to be filtered"
	assert "ui" in narrow["Listing"], "the list did not say what it was narrowed to"
	assert "Show everything" in narrow["Listing"], "there was no way back to the workspace"

	# **It leaves a project for its workspace, so it is an address and so it is a link**
	# (`SR#722`). Found by auditing every remaining `onClick=` rather than from the report,
	# which named the rows, the switcher and the detail page and not this.
	assert 'href="/projects"' in narrow["Listing"], (
		f"the way back to the workspace cannot be opened in a tab: {narrow['Listing']}"
	)


def test_a_project_filter_sends_what_the_route_accepts () -> None:
	"""`SR#320`: `project=` already covers what is under a project, and `subtree` is not it.

	**This test used to assert the opposite and passed while the feature was broken.** It
	checked that the request contained `subtree=true`; it did, and every filtered listing
	answered `422` — *"'subtree' says how much of a parent's tree to return, so it needs a
	parent"* — because `subtree` is about a *task's* children, not a project's. The failure
	reached `setError` and replaced the page.

	A guard that reads a spelling out of the source can only ever confirm the spelling. This one
	now asserts the absence that was measured, and the note is the point: **what actually found
	it was driving a real instance**, and nothing available to this file could have.
	"""

	source = _without_comments(_served_modules()["app.js"])

	assert "project=$" in source, "the listing stopped narrowing by project at all"
	assert "subtree" not in source, (
		"`subtree` is back in a request beside `project`, which the API refuses — it is about a "
		"task's children, and a project already includes its own"
	)


def test_a_project_that_is_gone_does_not_take_the_page_with_it () -> None:
	"""**The case the whole address design exists for.**

	`sr` became `subroutine` on 2026-08-08 across 502 items. A link saved before that names a
	project this instance now refuses with `404` — and the listing's failure would have reached
	`setError` and replaced the page, for an address that still identifies its item perfectly
	well. Measured by driving: `?project=gone` is *"There is no project 'gone' here."*

	The filter is dropped, the workspace is read instead, and the reason is said out loud —
	which is the difference between a link that ages and one that dies.

	Source-level, for `SR#640`'s reason: the branch is inside `App`.
	"""

	app = _without_comments(_served_modules()["app.js"])

	assert "failure.status !== 404 || !key" in app, (
		"a project that no longer exists no longer falls back to the workspace"
	)
	assert "any more" in app, "the reader is no longer told why the list widened"


def test_an_unmatched_address_answers_a_browser_and_a_client_differently (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The fallback, driven rather than read (`SR#648`).

	Both halves matter and they fail in opposite directions. A browser that got a problem
	document for `/personal` would have no deep links at all; a client that got a page for
	`/v1/nothing` would have `200 text/html` where `docs/errors.md` promises
	`application/problem+json` — and that one is silent, because 200 does not look like a
	failure.
	"""

	application = api_support.build_app(api_support.factory_for(session))

	page = api_support.call(
		application, "GET", "/personal", headers={"accept": "text/html,*/*;q=0.8"}
	)
	data = api_support.call(
		application, "GET", "/personal", headers={"accept": "application/json"}
	)
	mistyped = api_support.call(
		application, "GET", "/v1/nothing", headers={"accept": "application/json"}
	)

	assert page.status_code == 200
	assert page.headers["content-type"].startswith("text/html")
	assert "<title>Subroutine</title>" in page.text

	assert data.status_code == 404
	assert data.headers["content-type"].startswith("application/problem+json")

	assert mistyped.status_code == 404
	assert mistyped.json()["code"] == "not_found"
	assert "/v1/nothing" in mistyped.json()["detail"]


def test_the_workspace_in_an_address_is_the_one_that_is_shown (tmp_path: pathlib.Path) -> None:
	"""`SR#650`: it was parsed and then ignored, so `/personal` showed whatever you were in.

	`/projects` looked right the whole time — **because it was the default**, which is the one
	case that cannot tell a working feature from a missing one. That is why the decision is a
	function now: the rule was never wrong, the wire was missing, and only something callable
	can prove the wire exists.

	Four cases, and the last is new since `SR#648`: an address nobody claimed is served the app,
	so `/nonsense` reaches this and a reader who typed it must be told rather than quietly shown
	somebody else's backlog.
	"""

	module = _staged(tmp_path)
	answers = _ran(tmp_path, f"""
		import * as app from "{module.as_uri()}";

		const available = ["projects", "personal"];
		const cases = [
			[{{ workspace: "personal", project: null, ref: null }}, "projects"],
			[{{ workspace: "projects", project: null, ref: null }}, "personal"],
			[null, "projects"],
			[{{ workspace: "nonsense", project: null, ref: null }}, "projects"],
		];

		process.stdout.write(JSON.stringify(
			cases.map(([asked, current]) => app.chosenWorkspace(asked, available, current))
		));
	""")

	named, other, none, unknown = answers

	assert named == {"slug": "personal", "refused": None}, (
		"an address naming a workspace did not select it — which is SR#650 exactly"
	)
	assert other == {"slug": "projects", "refused": None}
	assert none == {"slug": "projects", "refused": None}, "no address should keep you where you are"
	assert unknown == {"slug": "projects", "refused": "nonsense"}, (
		"an unknown workspace was silently swapped for another one"
	)


# ---- what the browser asks for, against what the instance accepts (`SR#640`) ----------------


# **The four faults this arc shipped had one shape**: the rule right, the display right, and no
# wire between them. Three of the four were a *request* — `?limit=` on a route that declares
# none, `&subtree=true` beside a project filter, a list ordered by something the command line
# does not order by — and every one was found by Simon opening the page, because the decision
# about what to ask for lived inside `App`, which the render harness cannot touch.
#
# `SR#640`'s middle option, taken: the requests are pure functions now, so they can be called
# with no DOM and the answers **driven against a real instance**. That is the part that matters.
# Reading the query string and approving of it is what was done for `subtree`, and the test
# passed while every filtered listing 422'd — a spelling can only ever confirm a spelling.


class Instance(typing.NamedTuple):
	"""A real installation holding one of everything an address in this app can name."""

	application: fastapi.FastAPI
	secret: str
	slug: str
	project: str
	task: int
	#: A second task that nothing else in `_calls` writes to, so its version is knowable — an
	#: edit sends `expected_version` (§8.9) and a stale one is a 409, which the driving guard
	#: reads as a failure. `task` is patched three times over by assign, unassign and restore.
	spare: int
	spare_version: int
	#: A repeating task, and its version, so an edit that has to say which occurrences it is
	#: for can be driven against a real one (`SR#1252`). Nothing else here writes to it, for
	#: `spare`'s reason: an edit sends `expected_version` and a stale one is a 409.
	#:
	#: **This is what makes the answer checkable at all.** Against a task that does not repeat
	#: the answer is refused by name, and against one that does, leaving it out is refused —
	#: so a fixture holding only ordinary tasks could drive neither direction.
	repeating: int
	repeating_version: int
	#: A link the fixture made, so removing one can be driven — a DELETE needs an id that
	#: exists, and the ids the calls above create are not threaded back into this list.
	link: str
	#: A link whose *source* is a document, so removing one can be driven from that end too.
	#: `unlinkRequest` branches on the kind exactly as `commentRequest` does, and was driven
	#: from one end only — the coverage gap that let `SR#1419` sit in `statusRequest`.
	document_link: str
	document: int
	#: A second document that nothing else in `_calls` revises, for `spare`'s reason one entity
	#: along: `statusRequest` carries no `expected_version` (`SR#758`) but still moves the
	#: version, and `documentRequest`'s revise below sends the one it was told. Driving a
	#: document's status against `document` would bump it out from under that call, and the
	#: 409 would name the revise rather than the status change that caused it.
	spare_document: int
	#: A document status that is **not** the seeded default, so moving to it is a real move
	#: rather than a write that happens to change nothing. ``archived`` deliberately: it is the
	#: one Simon selected when he met `SR#1419`, so this guard asks the instance the question
	#: the report asked it.
	document_status: str
	username: str
	status: str
	cursor: str
	since: int

	def call (self, method: str, path: str, **kwargs: typing.Any) -> httpx.Response:
		"""Make one request, authenticated the way this app's session cookie would be."""

		return api_support.call(
			self.application, method, path,
			headers={"authorization": f"Bearer {self.secret}"}, **kwargs,
		)


@pytest.fixture
def instance (session: sqlalchemy.orm.Session) -> Instance:
	"""An installation the browser's own requests can be made against.

	Built through the API rather than through the services, so the refs and the cursor are the
	ones a browser would actually be holding — a cursor especially, which is signed and would
	be refused if this invented one.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="The browser"
	)
	session.flush()

	application = api_support.build_app(api_support.factory_for(session))
	secret = issued.value.get_secret_value()

	def call (method: str, path: str, **kwargs: typing.Any) -> httpx.Response:
		"""Make one authenticated request against this instance."""

		return api_support.call(
			application, method, path, headers={"authorization": f"Bearer {secret}"}, **kwargs
		)

	slug = setup.workspace.slug
	scope = f"?workspace_id={slug}"

	# **A create names its workspace in the body; everything else names it in the query**, and
	# this fixture used to append `?workspace_id=` to the three creates below as well. That was
	# discarded unheard, and the workspace they landed in was the fallback for *there is only
	# one* — so the pin these lines appeared to apply had never once been applied. Invisible
	# until `#898` made an undeclared parameter a refusal, and invisible to this fixture in
	# principle, since a second workspace is what it would take to tell the two apart.
	made = call(
		"POST", "/v1/projects", json={"key": "web", "title": "The browser", "workspace_id": slug}
	)
	assert made.status_code == 201, made.text

	# Two tasks, so a page of one has something after it and the cursor below is a real one.
	refs = []

	for title in ("Read the backlog", "Write it down"):
		answer = call(
			"POST", "/v1/tasks", json={"text": f"{title} +web", "workspace_id": slug}
		)
		assert answer.status_code == 201, answer.text
		refs.append(answer.json())

	repeating = call(
		"POST",
		"/v1/tasks",
		json={
			"text": "Stand-up +web",
			"workspace_id": slug,
			"due": "2026-09-01",
			"recurrence": "every week",
		},
	)
	assert repeating.status_code == 201, repeating.text

	document = call(
		"POST",
		"/v1/documents",
		json={"title": "A note", "body": "Prose.", "workspace_id": slug},
	)
	assert document.status_code == 201, document.text

	spare_document = call(
		"POST",
		"/v1/documents",
		json={"title": "A second note", "body": "More prose.", "workspace_id": slug},
	)
	assert spare_document.status_code == 201, spare_document.text

	joined = call(
		"POST", f"/v1/tasks/{refs[0]['ref']}/links{scope}",
		json={"target": refs[1]["ref"], "link_type": "relates_to", "target_type": "task"},
	)
	assert joined.status_code == 201, joined.text

	from_document = call(
		"POST", f"/v1/documents/{spare_document.json()['ref']}/links{scope}",
		json={"target": refs[1]["ref"], "link_type": "relates_to", "target_type": "task"},
	)
	assert from_document.status_code == 201, from_document.text

	# **Fetched in the order the browser asks for, which is not the default** (`SR#877`). A
	# cursor encodes the keys of the order that produced it, so one minted from an unordered
	# page is refused by a request that sinks deferred work — *"the cursor was not issued by
	# this instance"*, which reads like a signing fault and is a disagreement about ordering.
	# Read from the command line rather than written down, so the fixture cannot drift from
	# what a browser would really be holding.
	sunk = urllib.parse.quote(subroutine.cli.personal._sunk(None))
	page = call("GET", f"/v1/tasks{scope}&limit=1&order={sunk}")
	assert page.status_code == 200, page.text

	# **A real seq, read rather than invented.** A literal `1` is below the oldest event the
	# shared PostgreSQL database still holds — earlier tests roll back and leave a gap in the
	# sequence — and is correctly refused with `410 cursor_expired`.
	#
	# Read from the table rather than from the feed, because `GET /v1/changes` carries a
	# deliberate `now() - 1s` watermark (§5.11a): `seq` is allocated at insert and becomes
	# visible at commit, so a resumable reader must not advance past a number that has not
	# landed. Everything above was written in the same second, so the feed reports none of it.
	newest = session.execute(sqlalchemy.text("SELECT max(seq) FROM event")).scalar()

	assert newest is not None, "no events were written, so there is no seq to resume from"

	return Instance(
		application=application,
		secret=secret,
		slug=slug,
		project="web",
		task=refs[0]["ref"],
		spare=refs[1]["ref"],
		spare_version=refs[1]["version"],
		repeating=repeating.json()["ref"],
		repeating_version=repeating.json()["version"],
		link=joined.json()["id"],
		document_link=from_document.json()["id"],
		document=document.json()["ref"],
		spare_document=spare_document.json()["ref"],
		document_status="archived",
		username=setup.user.username,
		status=refs[0]["status"],
		cursor=page.json()["page"]["next_cursor"],
		since=int(newest),
	)


def _builders (source: str) -> set[str]:
	"""Return the name of every request builder the app exports."""

	return set(re.findall(r"\bexport function (\w*Requests?) \(", source))


def _built (
	tmp_path: pathlib.Path, calls: typing.Sequence[tuple[str, list[typing.Any]]]
) -> list[dict[str, typing.Any]]:
	"""Call each named builder with its arguments, in Node, and return what each built.

	Flat, because a builder answers with one request or with several and the caller is checking
	each of them rather than counting.
	"""

	module = _staged(tmp_path)

	return list(_ran(tmp_path, f"""
		import * as app from "{module.as_uri()}";

		const out = [];

		for (const [name, args] of {json.dumps([list(call) for call in calls])}) {{
			const answer = app[name](...args);

			for (const request of Array.isArray(answer) ? answer : [answer]) {{
				out.push({{ ...request, from: name }});
			}}
		}}

		process.stdout.write(JSON.stringify(out));
	"""))


def _view_names () -> list[str]:
	"""The arrangements the app offers, read from `VIEWS` rather than listed here."""

	source = _served_modules()["app.js"]
	found = re.search(r"export const VIEWS = \[([^\]]*)\]", source)

	assert found, "the app's list of views could not be read from app.js"

	return re.findall(r'"([^"]+)"', found.group(1))


def _selections () -> list[dict[str, str]]:
	"""Every selection this app's address grammar admits, derived from `SELECTABLE`.

	**Derived from what is being measured, not from a list kept beside it** (`SR#738`). This used
	to parametrise over `VIEWS`, which was right while a view name carried the selection and
	became a hole the moment it did not: `VIEWS` shrank to two entries and the done path would
	have stopped being driven while every case went on passing. *No cases failed* and *one case
	ran* read identically, which is `test_cli_help`'s recorded trap.

	Singles plus the named presets, and **never every combination**: `status_category=done`
	implies `include_completed`, and sending both is refused by name — a constraint `SELECTABLE`
	cannot express and `SR#710` measured on the live instance.
	"""

	source = _served_modules()["app.js"]
	block = re.search(r"export const SELECTABLE = \{(.*?)\n\};", source, re.S)

	assert block, "the app's selectable parameters could not be read from app.js"

	singles = [
		{name: value}
		for name, values in re.findall(r"\n\t(\w+): \[([^\]]*)\]", block.group(1))
		for value in re.findall(r'"([^"]+)"', values)
	]

	# **A free-text parameter has no values to enumerate, so one is supplied** (`SR#775`). The
	# derivation above reads array entries only, so `q: null` would have fallen out of it
	# silently — every case still passing, and the one new parameter driven by nothing.
	singles += [{name: "backlog"} for name in re.findall(r"\n\t(\w+): null,", block.group(1))]

	assert singles, "no selectable parameter was found, so nothing would be driven"

	# **Every name in `SELECTABLE` is represented**, whatever shape its values take. Without
	# this the derivation is only as complete as the shapes somebody thought to match, which is
	# how `q` would have arrived undriven.
	declared = set(re.findall(r"\n\t(\w+): ", block.group(1)))
	covered = {name for one in singles for name in one}

	assert declared == covered, (
		f"{sorted(declared - covered)} is selectable and is driven by nothing, and "
		f"{sorted(covered - declared)} is driven and is not selectable"
	)

	return [{}] + singles + [_preset(source, name) for name in ("EVERYTHING", "ONLY_FINISHED")]


def _preset (source: str, name: str) -> dict[str, str]:
	"""One of the selections a control writes, read from the app rather than restated here."""

	found = re.search(rf"export const {name} = \{{([^}}]*)\}};", source)

	assert found, f"the app's {name} selection could not be read from app.js"

	return dict(re.findall(r'(\w+): "([^"]+)"', found.group(1)))


def _calls (place: Instance) -> list[tuple[str, list[typing.Any]]]:
	"""Every request this app can make, with arguments naming things that exist.

	One entry per *shape* rather than per builder: a listing narrowed to a project and a listing
	that is not are different requests, and the narrowing is where the last two faults were.

	**A selection is a shape, so the selections are derived rather than listed** (`SR#738`). Each
	sends a different query and each is a chance to send something the route refuses — which is
	precisely how `SR#718` reached a reader: nothing had ever asked what a *board* fetches.
	"""

	return [
		("listingRequests", [place.slug, None, None, selection])
		for selection in _selections()
	] + [
		("identityRequest", []),
		("headRequest", []),
		# **Driven last of the reads, because it ends the session it is driven with.** The
		# endpoint has existed since `SR#248` and nothing on the page reached it, so the only
		# way to stop being signed in on a machine was to wait or to clear a cookie by hand
		# (`SR#927`'s M-26).
		("signOutRequest", []),
		("pollRequest", [place.slug, place.since]),
		# **The instance nobody has used yet**, which has no events and so no seq to resume
		# from. `SR#656` was exactly this shape, and the poll's own habit of swallowing
		# failures is what made it permanent.
		("pollRequest", [place.slug, None]),
		("rosterRequest", [place.slug]),
		# **The add form's two answers** (`SR#756`). `vocabularyRequest` is the one that has to
		# name the workspace: `/v1/meta` without one answers 200 with `statuses`, `item_types`
		# and `link_types` all empty, so a form built from it offers a type dropdown with no
		# types in it and nothing has failed.
		("vocabularyRequest", [place.slug]),
		("projectsRequest", [place.slug]),
		("listingRequests", [place.slug, None, None]),
		("listingRequests", [place.slug, place.project, None]),
		("listingRequests", [
			place.slug, None, {"tasks": place.cursor, "documents": place.cursor},
		]),
		("itemRequests", ["task", place.task, place.slug]),
		("itemRequests", ["document", place.document, place.slug]),
		("completeRequest", [{"ref": place.task}, place.slug]),
		("restoreRequest", [{"ref": place.task, "status": place.status}, place.slug]),
		("assignRequest", [{"ref": place.task}, place.username, place.slug]),
		("assignRequest", [{"ref": place.task}, None, place.slug]),
		# **Both directions, because clearing is a value rather than an omission** (`SR#986`,
		# §8.3). A `null` that the route read as *leave it alone* would make *Stop prioritising*
		# a button that reports success and changes nothing, which is this codebase's own
		# recorded shape for a control nobody notices is inert.
		("prioritiseRequest", [place.project, place.slug]),
		("prioritiseRequest", [None, place.slug]),
		# **Both shapes of the same write** (`SR#756`): the one-line box on its own, which is what
		# §1.4 guarantees keeps working, and the box with every disclosed field filled in.
		("addRequest", [{"text": "Something new"}, place.slug]),
		("addRequest", [{
			"text": "Something detailed",
			"description": "why it matters",
			"project": place.project,
			# Seeded vocabulary, like `place.status` above, and deliberately **not** the default
			# one: a dropped `type` would still produce a task, so driving the default would pass
			# against a body that never sent the field.
			"type": "bug",
			"status": place.status,
			"assignee": place.username,
			"importance": "4",
			"urgency": "3",
			"estimate": "2h",
			# Fixed days rather than ones computed from today. A deadline in the past is a legal
			# thing to file, so nothing here expires — which is the trap a same-day fixture walks
			# into, passing in the morning and failing in the evening.
			"start": "2026-08-12",
			"starts_at": "2026-08-13",
			"due": "2026-08-14",
			"tags": "health, #admin",
		}, place.slug]),
		# **Every disclosed control left alone**, which is the commonest submission there is and
		# the one that would 422: an untouched control gives `""`, and this endpoint refuses an
		# empty assignee, type, estimate and title by name.
		("addRequest", [{
			"text": "Something plain",
			"description": "", "project": "", "type": "", "status": "", "assignee": "",
			"importance": "", "urgency": "", "estimate": "",
			"starts": "", "snooze": "", "due": "", "tags": "",
		}, place.slug]),
		# **An edit, sending `expected_version`** (`SR#757`, §8.9). Against a task nothing else
		# here writes to, because a stale version is a 409 and this guard reads any 4xx as the
		# page a reader would have got.
		("updateRequest", [{
			"title": "Edited through the form",
			"description": "changed", "project": place.project, "type": "bug",
			"status": place.status, "assignee": place.username,
			"importance": "4", "urgency": "3", "estimate": "90m",
			"starts": "2026-08-13", "snooze": "2026-08-12", "due": "2026-08-14",
			"tags": "health",
		}, {"ref": place.spare, "version": place.spare_version}, place.slug]),
		# **Every clearable control blanked**, which is the half `filed`'s rule would break: an
		# edit must send `null` where creating omits, or clearing a deadline is a silent no-op.
		("updateRequest", [{
			"title": "Cleared through the form",
			"description": "", "project": place.project, "type": "task",
			"status": place.status, "assignee": "",
			"importance": "", "urgency": "", "estimate": "",
			"starts": "", "snooze": "", "due": "", "tags": "",
		}, {"ref": place.spare, "version": place.spare_version + 1}, place.slug]),
		# **A save on something that repeats, which has to say which occurrences it is for**
		# (`SR#1252`, decision `SR#1249`). Both answers, because they are two writes rather than
		# one write and a flag: *just this one* lands on the row in front of the reader and
		# *every one from now on* also reaches the row that persists, and an instance could
		# accept the word and act on neither.
		#
		# **The version moves between them**, which is why the second says `+ 1`. `edited` sends
		# `expected_version` on every save (§8.9), and a stale one is a 409 that this guard
		# would read as the page a reader would have got.
		("updateRequest", [{
			"title": "Just this stand-up",
			"description": "", "project": place.project, "type": "task",
			"status": place.status, "assignee": "",
			"importance": "", "urgency": "", "estimate": "",
			"starts": "", "snooze": "", "due": "2026-09-08", "tags": "",
		}, {"ref": place.repeating, "version": place.repeating_version}, place.slug, "this_one"]),
		("updateRequest", [{
			"title": "Every stand-up",
			"description": "", "project": place.project, "type": "task",
			"status": place.status, "assignee": "",
			"importance": "", "urgency": "", "estimate": "",
			"starts": "", "snooze": "", "due": "2026-09-15", "tags": "",
		}, {
			"ref": place.repeating, "version": place.repeating_version + 1,
		}, place.slug, "from_now_on"]),
		# **Handing a repeating item over asks too** (decision `SR#1249` §1). It is one gesture
		# and one field, which is what makes it look like `statusRequest` below — and it is not,
		# because *who does this one* and *who does it from now on* are different sentences.
		("assignRequest", [
			{"ref": place.repeating}, place.username, place.slug, "from_now_on",
		]),
		# **The quick path** (`SR#758`): one field and no `expected_version`, which is right
		# here and wrong for the form — a single control read and written in one gesture cannot
		# be refused for a field somebody else moved and this reader never saw.
		("statusRequest", [{"ref": place.task, "kind": "task"}, place.status, place.slug]),
		# **And on a document, which is what `SR#1419` was** — reported from the browser against
		# a real note: the Status control offers *Archive*, pressing it answers *"is a document,
		# not a task"*, and nothing changes. The vocabulary above the control asks `item.kind`
		# and the builder did not, so every press of it on a document had always been refused.
		#
		# **This entry fails against the code as shipped**, with the instance's own words, which
		# is what makes it a guard rather than a copy of the fix.
		(
			"statusRequest",
			[
				{"ref": place.spare_document, "kind": "document"},
				place.document_status, place.slug,
			],
		),
		# **Both kinds** (`SR#759`): a document is commented on exactly as a task is, and the
		# collection in the path is the only difference — which is the sort of thing that is
		# right for one and wrong for the other until something drives both.
		("commentRequest", [
			{"ref": place.task, "kind": "task"}, "What happened.", place.slug,
		]),
		("commentRequest", [
			{"ref": place.document, "kind": "document"}, "What happened.", place.slug,
		]),
		# **Both ends of both kinds** (`SR#760`): a task linked to a document, and a document
		# linked to a task. The collection in the path is the source and `target_type` is the
		# other end, and getting one right says nothing about the other.
		("linkRequest", [
			{"ref": place.task, "kind": "task"}, place.document, "relates_to", "document",
			place.slug,
		]),
		("linkRequest", [
			{"ref": place.document, "kind": "document"}, place.spare, "relates_to", "task",
			place.slug,
		]),
		("unlinkRequest", [{"ref": place.task, "kind": "task"}, place.link, place.slug]),
		# **From the document end as well**, for the reason two entries above: this builder
		# chooses its collection from the kind, and driving one end says nothing about the other.
		(
			"unlinkRequest",
			[{"ref": place.spare_document, "kind": "document"}, place.document_link, place.slug],
		),
		# **Writing one and revising one** (`SR#761`), which are one builder and two methods.
		# The revision carries `expected_version`, and `doc edit` is a whole-body replace — so
		# what is at stake is the entire document rather than one field.
		("documentRequest", [
			{"title": "A conclusion", "body": "Prose.", "type": "decision", "status": "active"},
			None, place.slug,
		]),
		("documentRequest", [
			{"title": "Revised", "body": "More prose.", "type": "note", "status": "draft"},
			{"ref": place.document, "version": 1}, place.slug,
		]),
		# **No arguments, and that is the thing being checked** (`SR#652`): the agenda asks
		# across every workspace, so a request that named one would answer a different question
		# and look right doing it.
		("agendaRequest", []),
		# **The phrase preview, both ways it is called** (`SR#94`, §6.7). With a zone, which is
		# what the form sends, and without — because the parameter is optional and the branch
		# that omits it is the one nothing else would drive. It writes nothing, so unlike every
		# other entry here it needs no spare row and can be asked anything.
		("readingRequest", ["every other tuesday", "Europe/London"]),
		("readingRequest", ["every month on the 30th"]),
	]


#: Builders whose request is correct and cannot succeed against a token. One entry, and it
#: names why rather than merely excusing itself: a browser session is the thing being ended, and
#: this harness has none.
_NEEDS_A_BROWSER_SESSION = frozenset({"signOutRequest"})


def test_a_parents_sub_tasks_are_counted_from_what_the_instance_really_sends (
	tmp_path: pathlib.Path, instance: Instance
) -> None:
	"""`SR#1281`, and the reason it is driven against a real instance rather than a fixture.

	Simon, reading a milestone in the browser: *"I see 'Parts (0 of 13 done)'"* — with all
	thirteen finished and the terminal saying **13 of 13**. Neither the count nor the
	strikethrough beside it had ever worked, because both read ``is_complete`` and **a task did
	not carry one**. It was a link end's field; the terminal and the agent's ``show`` each
	re-derived the same fact from ``completed_at``, so three surfaces agreed and the fourth
	asked for the answer and was handed nothing.

    **The test next door could not see it, and that is the finding.** Its fixture writes
	``"is_complete": True`` into the rows by hand — a field the payload does not have — so it
	confirmed the rendering against a shape no server produces. A harness that supplies the
	input under test can only ever check the half that was not broken.

	**So the parts here come off the wire.** A real parent, a real finished child and a real
	unfinished one, fetched through the same request the page builds, and handed to the
	component exactly as it arrives. Nothing in this test knows the field's name, which is what
	makes it survive the field being renamed and fail if it stops being sent.
	"""

	parent = instance.call(
		"POST", "/v1/tasks", json={"text": "A parent", "workspace_id": instance.slug}
	).json()

	made = []

	for title in ("The first piece", "The second piece"):
		child = instance.call(
			"POST",
			"/v1/tasks",
			json={
				"text": title,
				"workspace_id": instance.slug,
				# **The id, not the ref** — the endpoint's own refusal names the field, and
				# taking the spelling from it rather than guessing is why this is one line.
				"parent_task_id": parent["id"],
			},
		)

		assert child.status_code == 201, child.text
		made.append(child.json())

	finished = instance.call(
		"POST", f"/v1/tasks/{made[1]['ref']}/complete?workspace_id={instance.slug}"
	)

	assert finished.status_code == 200, finished.text

	# **The page's own request, not one written for this test.** `partsRequest` is what the
	# browser builds and it carries `include_completed=true` deliberately — a parent showing
	# one of its two children because the other is finished would misreport the thing somebody
	# opened it to see.
	answered = instance.call(
		"GET",
		f"/v1/tasks?parent={parent['ref']}&include_completed=true&order=ref"
		f"&limit=50&workspace_id={instance.slug}",
	)

	assert answered.status_code == 200, answered.text

	parts = answered.json()

	assert len(parts["items"]) == 2, f"the fixture did not produce two children: {parts}"

	shown = _rendered(tmp_path, {"Detail": {
		"item": {"ref": parent["ref"], "title": "A parent", "status": "open", "kind": "task"},
		"links": [], "comments": [], "workspace": instance.slug, "members": [],
		"vocabulary": {"link_types": []},
		"parts": parts,
	}})["Detail"]

	assert "1 of 2 done" in shown, (
		f"a parent with one finished child of two counted something else. The page reads a "
		f"field off each row; if the instance has stopped sending it, every count here is 0 "
		f"and nothing says so. What arrived: {parts['items'][1]}"
	)

	# **The field itself, named**, because the count above would also pass if the page had
	# started counting something else. This is the assertion the two fixtures next door cannot
	# make: theirs write the field in by hand, so they say what the *component* does with it and
	# nothing about whether it arrives.
	assert "is_complete" in parts["items"][1], (
		f"a task on the wire does not carry `is_complete`, so every parts count in the browser "
		f"is 0 and every finished one is drawn unstruck — which is `SR#1281`. What arrived: "
		f"{sorted(parts['items'][1])}"
	)
	assert parts["items"][1]["is_complete"] is True
	assert parts["items"][0]["is_complete"] is False

	# **The strikethrough is the same fact rendered a second way** (`SR#102`: no information may
	# exist only in a colour) and it reads the same field, so it goes red with the assertions
	# above rather than needing one here. It cannot have one here anyway: this harness drops
	# every attribute but `href` (`SR#784`), so a class assertion through it would be asserting
	# on something the instrument discards. `tests/test_browser.py` is where a computed
	# `line-through` is read.


def test_every_request_the_browser_makes_is_one_the_instance_accepts (
	tmp_path: pathlib.Path, instance: Instance
) -> None:
	"""**The whole point of `SR#640`: the request is built here and answered by the real app.**

	`api/query.py` refuses a query parameter a route did not declare, bodies refuse an unknown
	field, and several parameters are refused for what they *mean* rather than for their
	spelling — `subtree` needs a `parent`, and sending it beside `project` is a 422. None of
	those can be checked by reading the source, and all of them shipped.

	A refusal here is not a failing test about HTTP. It is the page a reader would have got.

	**One request cannot succeed here and says so by name.** ``signOutRequest`` ends a *browser
	session*, and this harness authenticates with a bearer token — so the instance answers "this
	request is not signed in with a browser session, so there is nothing to sign out of", which
	is the correct refusal rather than a fault in the request. What this test is for is the
	shape of the request; that it works with a cookie is
	``tests/test_api_sessions.py``'s question and is asked there.
	"""

	for request in _built(tmp_path, _calls(instance)):
		answer = instance.call(
			request["method"], f"/v1{request['path']}",
			**({"json": request["body"]} if request.get("body") is not None else {}),
		)

		if request["from"] in _NEEDS_A_BROWSER_SESSION:
			assert answer.status_code == 404, (
				f"{request['from']} was refused for a reason other than the credential this "
				f"harness carries: {answer.status_code} {answer.text[:200]}"
			)

			continue

		assert answer.status_code < 400, (
			f"{request['from']} builds {request['method']} {request['path']}, and the instance "
			f"answered {answer.status_code}: {answer.text[:400]}"
		)


#: Builders that name an entity and are right to always name a task's, with the reason each is.
#:
#: **Read together with `test_a_task_only_builder_really_is_one`**, which drives every one of
#: these with a document and fails if the path moved. So an entry stops being true the moment
#: somebody teaches one of these to branch, rather than sitting here looking considered — which
#: is the stale-excuse half `SR#405` requires of every allow-list in this repository.
_ALWAYS_A_TASK = {
	"completeRequest": (
		"§6.14 gives a document no `completed_at`, and `completable` hides the control that "
		"calls this from one. There is nothing on a document for this to write."
	),
	"assignRequest": (
		"§6.14 gives a document no assignee — it lists the fields a document deliberately "
		"lacks, and `assignee_id` is among them. Nobody is 'working on' a document."
	),
	"updateRequest": (
		"The task edit form. `documentRequest` is the document one, and they are two builders "
		"on purpose: the field lists do not overlap enough for one to serve both."
	),
	"restoreRequest": (
		"Reachable only from `complete`'s undo, and `complete` is gated by `completable`, "
		"which is `kind === 'task'`. A document can never reach this."
	),
}


def test_every_builder_that_names_an_entity_is_driven_with_both_kinds (
	tmp_path: pathlib.Path, instance: Instance
) -> None:
	"""`SR#1419`: a builder driven with one kind is a path checked for one kind.

	**The coverage test below is a floor and this is the other direction.** It asks whether
	every builder is driven *at all*; this asks whether the ones that name an entity are driven
	with **both** — which is the question that was not being asked when `statusRequest` shipped
	choosing `/tasks/` for everything. It was driven, once, with a task, and a floor cannot tell
	*no case failed* from *one case fewer ran*.

	**The population is derived from what the builders actually emit**, never from a list of
	names: a path is classified by the collection it came out with, so a builder written
	tomorrow is in scope the day it produces one.

	`itemRequests` is the precedent worth knowing — its own comment records that it was
	*"written the other way first and caught by the guard that drives every request against a
	real instance"*. It was caught because it is a **read** and the page builds both kinds. A
	write is built only when somebody presses something, so nothing drove the other half.
	"""

	seen: dict[str, set[str]] = {}

	for request in _built(tmp_path, _calls(instance)):
		found = re.match(r"^/(tasks|documents)/", request["path"])

		if found is not None:
			seen.setdefault(request["from"], set()).add(found.group(1))

	assert seen, "no builder produced an entity path, so this is checking nothing"

	thin = {
		name for name, kinds in seen.items()
		if kinds == {"tasks"} and name not in _ALWAYS_A_TASK
	}

	assert not thin, (
		f"{sorted(thin)} names an entity and is only ever driven with a task, so nothing has "
		f"asked what it writes to when the item is a document — which is exactly how SR#1419 "
		f"shipped. Drive it with both, or record in `_ALWAYS_A_TASK` why a document can never "
		f"reach it."
	)


def test_a_task_only_builder_really_is_one (
	tmp_path: pathlib.Path, instance: Instance
) -> None:
	"""An excuse that has stopped being true must fail, not sit there reading as a decision.

	Each entry above claims a builder can only ever address a task. **Driven, rather than
	believed**: the builder is handed a document and the path it returns must still be a task's.
	Teach one of them to branch and its entry fails here, which is what makes deleting the entry
	the act that closes it.

	**The requests built here are never sent.** The claim is about which collection the path
	names, and half of them would be refused against a real document by design — which is the
	very thing being asserted.
	"""

	assert _ALWAYS_A_TASK, "nothing is excused, so this is checking nothing"

	document = {"ref": instance.spare_document, "kind": "document"}
	probes: list[tuple[str, list[typing.Any]]] = [
		("completeRequest", [document, instance.slug]),
		("assignRequest", [document, instance.username, instance.slug]),
		("restoreRequest", [{**document, "status": instance.document_status}, instance.slug]),
		("updateRequest", [{
			"title": "Driven at a document", "description": "", "project": instance.project,
			"type": "task", "status": instance.status, "assignee": "",
			"importance": "", "urgency": "", "estimate": "",
			"starts": "", "snooze": "", "due": "", "tags": "",
		}, {**document, "version": 1}, instance.slug]),
	]

	assert {name for name, _arguments in probes} == set(_ALWAYS_A_TASK), (
		"every excused builder must be probed here, or an entry is excused and unchecked"
	)

	for request in _built(tmp_path, probes):
		assert request["path"].startswith("/tasks/"), (
			f"{request['from']} is recorded in `_ALWAYS_A_TASK` as never addressing a document, "
			f"and handed one it built {request['path']} — so the entry is out of date and the "
			f"reason beside it no longer holds"
		)


def test_every_request_builder_is_driven_against_the_instance () -> None:
	"""A builder nobody exercises is a request nobody checked.

	The pair above is only worth anything if it covers everything, so the list of calls is
	compared with what the app exports rather than trusted to have kept up. This is the check
	that makes adding a fifth write cost something.
	"""

	declared = _builders(_served_modules()["app.js"])
	place = Instance(
		application=typing.cast(fastapi.FastAPI, None), secret="", slug="w", project="p",
		task=1, spare=3, spare_version=1, repeating=4, repeating_version=1, link="l",
		document=2, spare_document=5, document_status="archived", document_link="dl",
		username="si", status="open", cursor="c", since=1,
	)
	exercised = {name for name, _arguments in _calls(place)}

	assert declared, "no request builders were found, so this is checking nothing"
	assert declared == exercised, (
		f"exercised {sorted(exercised)} and the app exports {sorted(declared)} — a builder that "
		f"is not driven is a request the instance has never been asked to accept"
	)


def test_the_app_reaches_the_network_only_through_a_built_request () -> None:
	"""**One way out, so there is one place a path can be got wrong.**

	The builders are only the app's requests if nothing else makes one. `sent` is the single
	caller of `api`, and every `sent` is handed a builder's answer — so a path assembled inside
	a component would have to be written past both, rather than merely forgotten about.
	"""

	app = _without_comments(_served_modules()["app.js"])
	declared = _builders(_served_modules()["app.js"])

	assert app.count("api(") == 1, (
		"something other than `sent` reaches the network, so a request exists that no builder "
		"built and no test drives"
	)

	handed = set(re.findall(r"\bsent\((\w+)\(", app))
	handed |= set(re.findall(r"\b(\w+)\([^()]*\)\.map\(sent\)", app))

	assert handed, "no request is sent at all, so this is checking nothing"

	invented = handed - declared

	assert not invented, f"{sorted(invented)} is sent and is not an exported request builder"


def _mounted (tmp_path: pathlib.Path) -> dict[str, typing.Any]:
	"""Render the whole app the way a browser would, and report what came back.

	**`App` uses hooks, so calling it as a plain function throws** — which is why the harness
	above renders components without one, and why `App` was in no test at all until `SR#640`.
	`preact-render-to-string` is a renderer with no DOM behind it: hooks work, effects do not
	run, and what comes back is the markup of the first paint.

	Deliberately *not* jsdom, which is ~2 MB of transitive Node closure to look at a page with
	three buttons on it.
	"""

	module = _staged(tmp_path)

	return dict(_ran(tmp_path, f"""
		// Beside the app, because that is where `_staged` puts it and where the served page
		// finds it. This named a `staged/` subdirectory and was the only other thing that
		// knew the layout, which is why flattening it broke here and nowhere else (`#764`).
		import {{ h }} from "{(tmp_path / "preact.js").as_uri()}";
		import {{ renderToString }} from "{(tmp_path / "render-to-string.js").as_uri()}";
		import * as app from "{module.as_uri()}";

		try {{
			process.stdout.write(JSON.stringify(
				{{ html: renderToString(h(app.App, {{}})), threw: null }}
			));
		}} catch (failure) {{
			process.stdout.write(JSON.stringify({{ html: null, threw: failure.message }}));
		}}
	"""))


def test_the_whole_app_renders (tmp_path: pathlib.Path) -> None:
	"""**The two blank pages this arc shipped were `App` throwing, and nothing could see it.**

	`ef8386d` — the import map missing `htm`, so the module never loaded. `2713b5e` — `SR#643`,
	a dependency array naming `show` above its own declaration, so the first render threw. Each
	time the gate was green, no request failed, and nothing was logged anywhere on the server.
	The only evidence either time was a browser console.

	This calls `App`. That is the whole of it, and it is what was missing: a component that
	throws for *any* reason now fails here rather than in front of a reader. The static guard
	below closes one shape of it; this closes the class.

	Falsified against `SR#643` itself — reinstating that effect above `show` produces
	``Cannot access 'show' before initialization``, which is exactly what the console said.
	"""

	answer = _mounted(tmp_path)

	assert answer["threw"] is None, f"the app throws on its first render: {answer['threw']}"

	# Nothing has been fetched — effects do not run without a DOM — so this is the first paint,
	# which is the state a reader sees before any answer arrives. It has to say *something*.
	assert answer["html"], "the app rendered nothing at all"
	assert "Reading" in answer["html"], (
		f"the first paint says nothing while the instance is being asked: {answer['html'][:200]}"
	)


# ---- stored text cannot take the page with it (`SR#679`, `SR#680`) --------------------------


def test_prose_nested_past_any_reason_still_renders (tmp_path: pathlib.Path) -> None:
	"""**`SR#679`: a 3,363-character line of `>` used to throw, and the page went blank.**

	`blocks` recurses once per blockquote and once per list level. Measured by binary search
	before the cap: 3,360 nested blockquotes exhausted the stack, and so did a list 2,000 deep
	and a list of blockquotes alternating — every one of them something a person or an agent can
	put in a description, or in a **comment on somebody else's item**, which is what made it
	worth fixing rather than noting.

	Sizes here are far past the old failure, so this fails loudly if the cap is removed.
	"""

	rendered = _markdown(tmp_path, [
		">" * 20000 + " hi",
		"\n".join(" " * (level * 2) + "- x" for level in range(3000)),
		"\n".join(" " * (level * 2) + "- > x" for level in range(3000)),
		">" * 20000 + " - x",
	])

	assert len(rendered) == 4, "the payloads did not all render"

	for html in rendered:
		assert html, "something nested deeply rendered as nothing at all"


def test_the_depth_cap_leaves_ordinary_prose_alone (tmp_path: pathlib.Path) -> None:
	"""A cap that changed what real prose looks like would be a worse defect than the crash.

	Nothing in this instance nests more than two or three levels; the cap is 32. This is the
	other direction of `SR#679` — the payloads above prove it stops, and this proves it does not
	stop early.
	"""

	source = (
		"- one\n"
		"- two\n"
		"  - nested\n"
		"    - deeper\n"
		"      - deeper still\n"
		"\n"
		"> a quote\n"
		">> and a reply\n"
	)

	html = _markdown(tmp_path, [source])[0]

	assert html.count("<ul>") == 4, f"a real nested list stopped rendering as lists: {html}"
	assert html.count("<blockquote>") == 2, "a quoted reply stopped rendering as a quote"
	assert "deeper still" in html


def test_text_that_cannot_be_rendered_is_shown_rather_than_thrown (
	tmp_path: pathlib.Path
) -> None:
	"""**`SR#680`: the one surface that takes arbitrary input cannot take the page with it.**

	`Prose` is where stored text becomes output, and the text is written by anybody with a
	credential — including on somebody else's item, since a comment renders through here too.
	`SR#679` closed the way it was known to fail; this closes the ones nobody has found.

	**The payload deliberately does not use the recursion.** A test that nested deeply would be
	checking `SR#679`'s cap a second time rather than this fallback, and would stop being able to
	fail the day the cap works — which is the shape of a test that cannot fail.

	Depends on no framework behaviour, which is the reason it exists alongside the boundary: a
	boundary needs a DOM to be exercised and this does not.
	"""

	module = _staged(tmp_path)
	answer = _ran(tmp_path, f"""
		import * as app from "{module.as_uri()}";

		/* `render` calls `String(source)` before anything else. */
		const hostile = {{ toString () {{ throw new Error("deliberate"); }} }};
		const out = {{ threw: null, html: null }};

		try {{
			const node = app.Prose({{ text: hostile, className: "prose" }});

			out.html = node.props.dangerouslySetInnerHTML.__html;
		}} catch (failure) {{
			out.threw = failure.message;
		}}

		process.stdout.write(JSON.stringify(out));
	""")

	assert answer["threw"] is None, (
		f"Prose threw rather than reporting: {answer['threw']} — a description would have taken "
		f"the page with it"
	)
	assert "could not be displayed" in answer["html"], (
		f"the reader was shown nothing about why: {answer['html']!r}"
	)


def test_what_a_reader_is_told_when_something_will_not_render () -> None:
	"""The decision is pure so that it can be checked at all (`SR#680`).

	**`preact-render-to-string` does not run error boundaries** — measured, both
	`componentDidCatch` and `getDerivedStateFromError`, and neither caught. So the harness can
	prove what the boundary *says* and cannot prove that Preact calls it. Lifting the sentence
	out is what makes the half this project owns checkable, which is the move `SR#640` arrived at
	four times.
	"""

	module_source = _served_modules()["app.js"]

	assert "export function unrenderable (" in module_source, (
		"the boundary's decision has gone back inside the component, where nothing can reach it"
	)


def test_the_page_says_which_thing_failed_and_stays_readable (tmp_path: pathlib.Path) -> None:
	"""Driven rather than read: what does a reader actually get told?"""

	module = _staged(tmp_path)
	answers = _ran(tmp_path, f"""
		import * as app from "{module.as_uri()}";

		process.stdout.write(JSON.stringify([
			app.unrenderable(new Error("Maximum call stack size exceeded"), "This text"),
			app.unrenderable(new Error("nope"), null),
		]));
	""")

	named, unnamed = answers

	assert named["said"] == "This text could not be displayed."
	assert named["detail"] == "Maximum call stack size exceeded", (
		"the message was swallowed, so a reader reporting this has nothing to quote"
	)
	assert unnamed["said"].startswith("This could not"), "an unnamed thing lost its sentence"


def test_the_app_is_mounted_inside_something_that_can_catch_it () -> None:
	"""**A boundary inside the thing that throws catches nothing** (`SR#680`).

	Both blank pages this arc shipped were `App` itself failing — an import map missing `htm`,
	and a dependency array naming a value declared below it (`SR#643`). A boundary placed inside
	`App` would have caught neither, so the mount is what has to be wrapped.
	"""

	app = _without_comments(_served_modules()["app.js"])
	mount = app[app.index("render(html`"):]

	assert "Boundary" in mount[:200], (
		f"the app is mounted without a boundary around it: {mount[:160]!r}"
	)


# ---- one list, not two end to end (`SR#660`) ------------------------------------------------


def _ordered (tmp_path: pathlib.Path, rows: list[dict[str, typing.Any]]) -> list[typing.Any]:
	"""Put rows through the app's own merge and return what came back, in order."""

	module = _staged(tmp_path)

	return list(_ran(tmp_path, f"""
		import * as app from "{module.as_uri()}";

		process.stdout.write(JSON.stringify(
			app.newestFirst({json.dumps(rows)}).map((row) => row.ref)
		));
	"""))


def test_a_document_is_ordered_among_the_tasks_not_after_them (
	tmp_path: pathlib.Path
) -> None:
	"""**`SR#660`: a document written a minute ago started at row 101 at best.**

	The list is two requests — tasks and documents are separate collections — and they were
	concatenated, so every document sat below every task. On a project holding 122 tasks against
	a page of 100, that put a document written minutes ago off the end of the page entirely.
	Simon met it on `SR#659`, which is what filed this.

	§6.2 gives both kinds one ref counter precisely so a reader can treat them as one thing; a
	list that then presents them as two blocks gives back the confusion it was merging to avoid.
	"""

	rows = [
		{"ref": 1, "kind": "task", "created_at": "2026-08-08T09:00:00+00:00"},
		{"ref": 2, "kind": "task", "created_at": "2026-08-08T11:00:00+00:00"},
		{"ref": 3, "kind": "document", "created_at": "2026-08-08T10:00:00+00:00"},
		{"ref": 4, "kind": "document", "created_at": "2026-08-08T12:00:00+00:00"},
	]

	assert _ordered(tmp_path, rows) == [4, 2, 3, 1], (
		"the two collections were not interleaved by when they were written"
	)


def _accumulated (
	tmp_path: pathlib.Path,
	held: list[dict[str, typing.Any]],
	arriving: list[dict[str, typing.Any]],
	*,
	appending: bool,
	collections: int,
) -> list[typing.Any]:
	"""Put a page through the app's own accumulation rule and return the refs it produced."""

	module = _staged(tmp_path)
	options = {"appending": appending, "collections": collections}

	return list(_ran(tmp_path, f"""
		import * as app from "{module.as_uri()}";

		process.stdout.write(JSON.stringify(app.accumulated(
			{json.dumps(held)}, {json.dumps(arriving)}, {json.dumps(options)}
		).map((row) => row.ref)));
	"""))


def test_one_collection_keeps_the_order_the_instance_gave_it (tmp_path: pathlib.Path) -> None:
	"""**`SR#706`: the merge that fixes one view silently breaks another.**

	`newestFirst` sorts on `created_at` because that is the key both collections are paged by.
	The done view asks the instance for `-completed_at` and reads one collection, so re-sorting
	client-side would overwrite the server's order with the *creation* order — a page ordered by
	when work was written, under a heading claiming when it was finished.

	Nothing about that page would look wrong. It is the right rows, complete, in an order a
	reader has no way to check, which is why the rule is a function with a test rather than a
	condition inside `App` (`SR#640`).
	"""

	# Deliberately the *reverse* of creation order, which is what finishing order looks like:
	# an old item completed today belongs above a new one completed last week.
	arriving = [
		{"ref": 1, "kind": "task", "created_at": "2026-08-01T09:00:00+00:00"},
		{"ref": 9, "kind": "task", "created_at": "2026-08-08T09:00:00+00:00"},
		{"ref": 5, "kind": "task", "created_at": "2026-08-04T09:00:00+00:00"},
	]

	kept = _accumulated(tmp_path, [], arriving, appending=False, collections=1)

	assert kept == [1, 9, 5], (
		f"a single collection was re-sorted, so the instance's ordering was thrown away: {kept}"
	)

	merged = _accumulated(tmp_path, [], arriving, appending=False, collections=2)

	assert merged == [9, 5, 1], (
		f"two collections must still be merged by when they were written: {merged}"
	)


def test_a_page_is_added_to_what_is_already_held (tmp_path: pathlib.Path) -> None:
	"""The other axis, and the one *Show more* depends on.

	Without it the two branches above could both be right and the page still lose everything
	above the fold on the second load — which is what `appending` is, and it is a separate
	question from whether the result is merged.
	"""

	held = [{"ref": 9, "kind": "task", "created_at": "2026-08-08T09:00:00+00:00"}]
	arriving = [{"ref": 5, "kind": "task", "created_at": "2026-08-04T09:00:00+00:00"}]

	assert _accumulated(tmp_path, held, arriving, appending=True, collections=1) == [9, 5]

	assert _accumulated(tmp_path, held, arriving, appending=False, collections=1) == [5], (
		"a first load must replace what is held rather than growing it — otherwise switching "
		"view or project leaves the previous page's rows underneath"
	)


@pytest.mark.parametrize("order", ["-created_at", "created_at", "title", "-updated_at"])
def test_the_merge_agrees_with_the_server_about_a_tie (
	tmp_path: pathlib.Path, order: str
) -> None:
	"""**`SR#879`. Four spellings of one rule, and two of them were stale.**

	Simon's decision of 2026-08-13 is that age separates rows and says nothing else, so the
	tiebreaker is **always ascending** — oldest first — rather than inheriting the direction of
	whatever key preceded it. `eecbd93` moved two of the four places that state it:
	`domain/ordering.clauses` and `api/pagination.parse_order`. It did not move
	`cli/personal._ordering` or `app.js`'s `inOrder`, **and both of their docstrings went on
	asserting that it had** — which is why nobody looked, and why the review rated this above
	the tie order it changes.

	**This test asserted the old rule**, quoting a sentence that had stopped being true. Intent
	kept — a client must break a tie the way the boundary it is paging across was broken —
	satisfier changed.

	**Parametrised over the direction**, because that is the whole defect: under the old rule a
	descending key gave a descending tiebreak, so a test using one order could not tell the two
	rules apart.
	"""

	rows = [
		{"ref": 7, "created_at": "2026-08-08T09:00:00+00:00",
			"updated_at": "2026-08-09T09:00:00+00:00", "title": "Same"},
		{"ref": 9, "created_at": "2026-08-08T09:00:00+00:00",
			"updated_at": "2026-08-09T09:00:00+00:00", "title": "Same"},
		{"ref": 8, "created_at": "2026-08-08T09:00:00+00:00",
			"updated_at": "2026-08-09T09:00:00+00:00", "title": "Same"},
	]

	assert _views(tmp_path, [("inOrder", {"rows": rows, "order": order})])[0] == [7, 8, 9], (
		f"a tie under {order} must come out oldest first, whichever way the key points"
	)


@pytest.mark.parametrize("order", ["created_at", "-created_at"])
def test_a_row_with_nothing_to_compare_sorts_last_whichever_way_it_was_asked (
	tmp_path: pathlib.Path, order: str
) -> None:
	"""`SR#794`, and the defect is not a row in the wrong place — it is a page in no order.

	`Date.parse(undefined)` is `NaN`, and **`NaN !== NaN` is true**, so the comparator took its
	compare branch and both `NaN < x` and `x < NaN` are false: `compare(a, b)` and
	`compare(b, a)` each answered *after*. A sort given a comparator that contradicts itself may
	produce **any** arrangement, varying with the engine and with how many rows there are, so
	the observable is not a mis-sort somebody could recognise.

	**Last in both directions, which is the server's rule rather than a choice.**
	`domain.ordering.clauses` appends `.nullslast()` to every term, ascending and descending
	alike — asserted here against that function rather than restated, so the two cannot drift.

	**Parametrised over the direction**, because a fix that multiplied by the direction would
	pass one of them and is exactly the mistake the deferred band above it already avoids.

	**Latent today**, since every request asks for the fields an ordering can use — which is why
	this is four lines now and a diagnosis later.
	"""

	rendered = str(
		subroutine.domain.ordering.clauses(
			order,
			allowed=subroutine.domain.ordering.TASK_FIELDS,
			default=subroutine.domain.ordering.DEFAULT_TASK_ORDER,
			tiebreak=subroutine.db.models.work.Task.id,
		)[0]
	).upper()

	assert "NULLS LAST" in rendered, (
		f"the server no longer sorts an absent value last under {order}, so the rule this "
		f"agrees with has moved: {rendered}"
	)

	rows = [
		{"ref": 1, "created_at": "2026-08-08T09:00:00+00:00"},
		{"ref": 2},
		{"ref": 3, "created_at": "2026-08-10T09:00:00+00:00"},
	]

	ordered = _views(tmp_path, [("inOrder", {"rows": rows, "order": order})])[0]

	assert ordered[-1] == 2, (
		f"under {order} the row with no value came out {ordered}, and it belongs last "
		f"whichever way the reader asked — that is what NULLS LAST means"
	)

	assert sorted(ordered) == [1, 2, 3], f"rows were lost or duplicated: {ordered}"


def test_every_spelling_of_the_tiebreak_points_the_same_way () -> None:
	"""**`SR#879`'s guard: the rule exists four times and nothing compared them.**

	Two are the server's — the `ORDER BY` a query is built with and the seek predicate a cursor
	is built from — and two are clients re-sorting rows they have already been given. A
	disagreement between any of them is a page boundary that skips or repeats rows, which looks
	nothing like a sorting fault.

	Read out of each place rather than restated here, so the check is against the code and not
	against a copy of the decision.
	"""

	clauses = subroutine.domain.ordering.clauses(
		"-created_at",
		allowed=subroutine.domain.ordering.TASK_FIELDS,
		default=subroutine.domain.ordering.DEFAULT_TASK_ORDER,
		tiebreak=subroutine.db.models.work.Task.id,
	)

	# Rendered rather than introspected: `UnaryExpression` keeps its direction in a private
	# `modifier`, and reading one is a claim about SQLAlchemy's internals where the SQL is the
	# thing that actually reaches the database.
	rendered = str(clauses[-1]).upper()

	assert "ASC" in rendered and "DESC" not in rendered, (
		f"domain.ordering.clauses breaks a tie with {rendered}"
	)

	keys = subroutine.api.pagination.parse_order(
		"-created_at",
		allowed=subroutine.domain.ordering.TASK_FIELDS,
		default=subroutine.domain.ordering.DEFAULT_TASK_ORDER,
		tiebreak=subroutine.db.models.work.Task.id,
	)

	assert keys[-1].descending is False, f"the cursor breaks a tie with {keys[-1]}"

	terminal = subroutine.cli.personal._ordering("-created_at")[1]

	assert terminal[-1] == ("ref", False), f"the terminal breaks a tie with {terminal[-1]}"

	source = _served_modules()["app.js"]

	assert "return one.ref - other.ref;" in source, (
		"the browser's inOrder no longer breaks a tie by ascending ref, so it disagrees with "
		"the id the server pages on"
	)


def test_showing_more_does_not_append_a_page_below_older_rows () -> None:
	"""**Across pages, appending was worse than within one** (`SR#660`).

	A second page of tasks belongs *above* documents already on screen, so appending made the
	list alternate between the two collections after one *Show more* — in no order at all. The
	whole held set is re-merged rather than extended.

	**Derived rather than spelled.** The first version of this asserted one exact expression was
	present, which is the trap this project keeps meeting — it would have survived any reformat
	being called a defect, and would have missed a *second* `setItems` written the old way. This
	asks the question instead: does every place that adds to the held list put the result through
	the merge?

	The wiring is inside `App`, which the render harness cannot execute (`SR#640`), so this reads
	the source. The *decision* it is checking — `newestFirst` — is pure and driven above.
	"""

	app = _without_comments(_served_modules()["app.js"])
	setting = [
		body for _, body in re.findall(r"setItems\(\(?(\w+)\)? =>\s*(.+?)\);", app, re.S)
	]

	assert setting, "nothing sets the list at all, so this is checking nothing"

	# **The rule moved into `accumulated` and this guard nearly went vacuous with it** (`SR#706`).
	# Its first version skipped any body with no `...` in it, so lifting the concatenation into a
	# pure function left every case `continue`-ing and the test passing while checking nothing.
	# That is the shape it was written to catch, met by the change that was meant to improve it.
	#
	# So it asks the question the other way round: a body that touches the held list at all must
	# delegate, and at least one must — the floor, without which deleting the delegation would
	# read as "no offenders".
	delegating = [body for body in setting if "accumulated" in body]

	assert delegating, (
		"no setItems call goes through `accumulated`, so the merge rule is wired to nothing"
	)

	for body in setting:
		if "..." not in body:
			continue

		assert "accumulated" in body or "newestFirst" in body, (
			f"a page is added to the list without going through the merge rule: "
			f"{body.strip()!r} — a second page of tasks belongs above documents already on screen"
		)


def test_the_listing_asks_for_the_field_it_orders_on () -> None:
	"""A merge key nobody requested arrives as `undefined`, and every row ties.

	`SR#645` shapes both listings to what a row renders, and this is the one field asked for that
	a row does *not* render — so it is the exception that the shaping test cannot derive, and it
	needs saying out loud rather than being left to look like an oversight.
	"""

	source = _served_modules()["app.js"]

	for name in ("TASK_FIELDS", "DOCUMENT_FIELDS"):
		start = source.index(f"const {name} = [")
		block = source[start:source.index('].join(",");', start)]

		assert '"created_at"' in block, (
			f"{name} does not ask for the field the two collections are merged on, so every row "
			f"will have an undefined key and the order will be whatever the two requests were"
		)


#: A component the render harness deliberately does not touch, and why. **Every entry needs
#: something that makes it go away**, which is this repository's rule for an allow-list — see
#: `tests/test_reach.py`'s `NOT_REACHED`. Deleting the entry is what closes the item.
UNRENDERED: dict[str, str] = {
	"App": (
		"Uses hooks, so the harness cannot call it as a plain function — it would throw for "
		"want of a renderer. `SR#640` is the item, and everything below `App` is written "
		"without hooks precisely so it can be checked here."
	),
}


def _components () -> set[str]:
	"""Return every component `app.js` exports, by the capital-letter convention Preact uses."""

	source = _served_modules()["app.js"]

	# **Exported components only, because those are the ones the harness can reach** — it looks
	# them up as `app[name]`. `Boundary` is a class and is deliberately not exported, so it is
	# outside this question entirely rather than excused from it; `SR#680` measured that
	# `preact-render-to-string` does not run error boundaries anyway, which is why the sentence
	# it shows is a pure function checked on its own.
	#
	# `class` is matched as well as `function` so that an exported class component cannot slip
	# past a scan that only ever looked for one of the two spellings.
	return set(re.findall(r"^export (?:function|class) ([A-Z]\w*)", source, re.M))


def test_every_component_the_app_exports_is_rendered_by_the_harness () -> None:
	"""A component nobody renders is one whose template is checked by nothing.

	The failure this is written for is the arc's own: an htm template that is malformed parses
	perfectly and throws when it is *rendered*, which on this project's record is the shape that
	ships and turns into a blank page. `test_every_component_renders` proves that of whatever
	`SAMPLES` names — and until this, nothing said `SAMPLES` named them all.
	"""

	missing = _components() - set(SAMPLES) - set(UNRENDERED)

	assert not missing, (
		f"{sorted(missing)} are exported components with no entry in SAMPLES, so their "
		f"templates are never rendered. Add a sample, or excuse it in UNRENDERED with a reason."
	)


def test_no_component_is_both_rendered_and_excused () -> None:
	"""An excuse that outlived its reason reads exactly like a considered decision.

	`SR#405`'s rule, and the second half of it: an allow-list that only fails when an entry is
	*missing* lets a stale one sit there for ever, still naming an item, still reading as
	deliberate. Three of those were found at once in `test_reach`.
	"""

	both = set(UNRENDERED) & set(SAMPLES)

	assert not both, f"{sorted(both)} are excused from rendering and rendered anyway"

	gone = set(UNRENDERED) - _components()

	assert not gone, f"{sorted(gone)} are excused from rendering and no longer exist"


def _agenda (
	tmp_path: pathlib.Path, calls: typing.Sequence[tuple[str, typing.Any]]
) -> list[typing.Any]:
	"""Drive the agenda's pure functions directly — `SR#652`, and `SR#640`'s point again."""

	module = _staged(tmp_path)

	return list(_ran(tmp_path, f"""
		import * as app from "{module.as_uri()}";

		const calls = {json.dumps(calls)};

		process.stdout.write(JSON.stringify(calls.map(([name, argument]) =>
			name === "agendaBuckets" ? app.agendaBuckets(argument.agenda, argument.workspaces)
			: name === "counted" ? app.counted(argument)
			: app.agendaRequest())));
	"""))


def test_the_agenda_asks_across_every_workspace (tmp_path: pathlib.Path) -> None:
	"""`SR#652`, decision `SR#649`: `/` is the agenda across all of them.

	Measured on the instance rather than assumed: naming `projects` returns 153 unscheduled,
	naming nothing returns 160 and an overdue row the narrower question cannot see. So sending a
	workspace here would quietly answer a different question — and would look right, because a
	shorter agenda is indistinguishable from a lighter day.
	"""

	[asked] = _agenda(tmp_path, [("agendaRequest", None)])

	assert asked["path"].startswith("/agenda")
	assert "workspace" not in asked["path"], "the agenda must not be scoped to one workspace"


def test_the_agenda_asks_for_the_look_ahead (tmp_path: pathlib.Path) -> None:
	"""`SR#985`: without this, any future deadline took a task off the page entirely.

	`GET /v1/agenda` omits `upcoming` unless asked — deliberately, so a client can reason about
	the window it gets — and this page asked for nothing while rendering a `Next 7 days` heading
	it could never be given data for. So dating a task removed it from every bucket, where the
	same edit on the terminal moves it *up*.

	**The number is read off the CLI's rather than written out here.** That is `SR#927`'s H-15
	again, one test along: a literal cannot notice that the thing it claims to mirror has moved,
	and it fails whoever changes the original instead.

	Driven rather than scanned, because the rendering was right throughout — `agendaBuckets` has
	always rendered an `upcoming` array correctly, since its own tests hand it one. The *request*
	was the wrong half, which is `SR#640` for the fifth time.
	"""

	[asked] = _agenda(tmp_path, [("agendaRequest", None)])
	wanted = subroutine.domain.agenda.DEFAULT_HORIZON_DAYS

	assert wanted > 0, "the CLI's look-ahead could not be read"
	assert f"horizon_days={wanted}" in asked["path"]


def test_a_bucket_with_nothing_in_it_is_not_shown (tmp_path: pathlib.Path) -> None:
	"""Nothing overdue is good news, and a heading over an empty list makes a reader hunt.

	**The opposite answer to a board's**, deliberately: a board with no `In progress` column
	reads as broken because the columns are the structure, where an agenda's headings are only
	what is there.
	"""

	[buckets] = _agenda(tmp_path, [(
		"agendaBuckets",
		{
			"agenda": {
				"overdue": [],
				"today": [{"ref": 1, "title": "Today's", "workspace_id": "w1"}],
				"upcoming": [],
				"unscheduled": [],
			},
			"workspaces": [{"id": "w1", "slug": "projects"}],
		},
	)])

	assert [bucket["key"] for bucket in buckets] == ["today"]


def test_the_buckets_keep_the_order_a_day_is_read (tmp_path: pathlib.Path) -> None:
	"""The same words, and the same order, `subroutine agenda` prints.

	§12.2 already decided what the agenda says, and one product answering one question two ways
	is worse than either answer on its own.

	**Read off the terminal's own list rather than written out here** (`#927`'s H-15). This
	asserted a literal four-item list under a docstring claiming it matched the CLI — and by
	the time anybody looked it matched neither: `in_progress` was missing entirely, so an item
	somebody had started and dated nowhere appeared in `subroutine agenda`, in the agent's
	agenda, and on no browser surface at all; and the last section was still `Unscheduled`
	where the CLI had renamed it `Next`. **A test written as a literal cannot notice that the
	thing it claims to mirror has moved** — it fails whoever changes the original instead.

	Driven rather than scanned, so it is the rendering that is compared and not the source: a
	`BUCKETS` entry nothing reads would satisfy a regex over `app.js` and still show nothing.
	"""

	row = {"ref": 1, "title": "A task", "workspace_id": "w1"}
	wanted = subroutine.cli.personal.AGENDA_SECTIONS

	assert len(wanted) > 3, "the CLI's sections could not be read"

	[buckets] = _agenda(tmp_path, [(
		"agendaBuckets",
		{
			# Every section filled, because `agendaBuckets` drops an empty one — so a bucket
			# the browser has lost would be indistinguishable from one that simply had no
			# rows, and this test would pass against exactly the defect it was written for.
			"agenda": {field: [row] for _label, field, _late in wanted},
			"workspaces": [{"id": "w1", "slug": "projects"}],
		},
	)])

	assert [bucket["label"] for bucket in buckets] == [label for label, _f, _l in wanted]
	assert [bucket["key"] for bucket in buckets] == [field for _label, field, _l in wanted]


def test_every_agenda_row_is_told_which_workspace_it_came_from (tmp_path: pathlib.Path) -> None:
	"""The response carries a uuid and nothing readable, so the slug is resolved here.

	**This is the wire, and it is what `SR#640` keeps breaking.** A row opened or completed
	against the switcher's workspace rather than its own is a 404 for an item the reader is
	looking at — the rule right, the display right, and nothing joining them. Resolving the slug
	onto the row is what lets `App` pass it, and this is the half that can be checked.
	"""

	[buckets] = _agenda(tmp_path, [(
		"agendaBuckets",
		{
			"agenda": {
				"overdue": [{"ref": 1, "title": "Elsewhere", "workspace_id": "w2"}],
				"today": [], "upcoming": [], "unscheduled": [],
			},
			"workspaces": [{"id": "w1", "slug": "projects"}, {"id": "w2", "slug": "sandbox"}],
		},
	)])

	assert buckets[0]["items"][0]["workspace"] == "sandbox"
	assert buckets[0]["items"][0]["kind"] == "task", "an agenda holds tasks, and `show` needs it"


def test_a_workspace_nobody_can_name_leaves_the_row_alone (tmp_path: pathlib.Path) -> None:
	"""An id with no slug beside it is null rather than the uuid.

	Printing the uuid would be worse than printing nothing: it reads as a name, it is not one,
	and `SR#638` says an address is `{workspace}/{ref}` — a uuid there resolves to nothing.
	"""

	[buckets] = _agenda(tmp_path, [(
		"agendaBuckets",
		{
			"agenda": {
				"overdue": [{"ref": 1, "title": "Orphan", "workspace_id": "w9"}],
				"today": [], "upcoming": [], "unscheduled": [],
			},
			"workspaces": [{"id": "w1", "slug": "projects"}],
		},
	)])

	assert buckets[0]["items"][0]["workspace"] is None


def test_a_row_carries_its_workspace_wherever_the_agenda_shows_it (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#968`, Simon 2026-08-17: *the workspace should always be shown, if none is selected.*

	**This used to ask whether the rows happened to span workspaces** — §12.2a's *a mark that
	says the same thing on every row says nothing*, which is the terminal's rule, where a
	listing is computed once and read once. Here the page polls, so what a row says would
	change because a stranger filed something elsewhere; decision `SR#957` §4 rules that out
	for this surface, and `SR#966` had just been fixed for one column along.

	**Asserted on what a row renders**, because the question the old rule answered no longer
	exists to be asked. `agendaBuckets` resolving a workspace onto every row is what makes the
	address possible at all, and that is what this now holds.
	"""

	one = {"overdue": [{"ref": 1, "workspace_id": "w1"}], "today": []}
	spaces = [{"id": "w1", "slug": "projects"}]

	[buckets] = _agenda(tmp_path, [("agendaBuckets", {"agenda": one, "workspaces": spaces})])

	assert buckets[0]["items"][0]["workspace"] == "projects", (
		"a row cannot say which workspace it is in, so nothing on the agenda can"
	)


def test_the_agenda_counts_what_is_on_screen (tmp_path: pathlib.Path) -> None:
	"""The footer counts rows across buckets, not the listing's state.

	`items` is the listing's and is empty while the agenda is showing, so reading it there put
	*0 items* under a full day.
	"""

	[total] = _agenda(tmp_path, [("counted", [
		{"key": "overdue", "items": [{"ref": 1}]},
		{"key": "today", "items": [{"ref": 2}, {"ref": 3}]},
	])])

	assert total == 3


def test_a_day_with_nothing_in_it_says_so_once (tmp_path: pathlib.Path) -> None:
	"""Rather than four headings over four empty lists."""

	markup = _rendered(tmp_path, {"Agenda": {"buckets": [], "more": 0}})["Agenda"]

	assert "Nothing is due" in markup
	assert "Overdue" not in markup


def test_an_agenda_row_from_elsewhere_is_addressed_by_its_workspace (
	tmp_path: pathlib.Path,
) -> None:
	"""`sandbox/#1` beside a bare `#589` — the same answer `subroutine agenda` gives.

	It goes in the ref cell rather than in a badge, because it is part of the address rather
	than a fact beside it (`SR#638`).
	"""

	markup = _rendered(tmp_path, {"Agenda": SAMPLES["Agenda"]})["Agenda"]

	assert "projects/#1" in markup and "personal/#2" in markup


def test_the_unscheduled_bucket_says_how_much_it_is_not_showing (
	tmp_path: pathlib.Path,
) -> None:
	"""An exact count, because the endpoint already did the counting.

	Unlike the listing's `…and more`, which declines one: §8.4 will not compute a total for a
	listing, and `unscheduled_total` exists precisely because "an agenda that dumped a 400-item
	backlog would not be an agenda".
	"""

	markup = _rendered(tmp_path, {"Agenda": SAMPLES["Agenda"]})["Agenda"]

	assert "12 more unscheduled" in markup


def test_the_agenda_says_how_much_dated_work_is_past_the_look_ahead (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#997`, Simon's decision of 2026-08-18: the edge stays and gets said.

	A deadline further out than the look-ahead is in **no bucket at all** — `unscheduled`
	requires both dates to be null, so dated work leaves that pile and there is nowhere else
	to go. `unscheduled_total`'s sibling, and it reaches every surface for the same reason:
	a count on one of them is `SR#583`'s shape, which is what `SR#990` exists to prevent.

	**Both counts, and separately.** They have different remedies — one is a cap you can lift,
	the other is a listing — so one total would be a figure with no action attached to it.
	"""

	markup = _rendered(
		tmp_path, {"Agenda": {**SAMPLES["Agenda"], "later": 3}}
	)["Agenda"]

	assert "3 dated further out" in markup
	assert "12 more unscheduled" in markup, "the other count is still its own sentence"


def test_an_agenda_showing_everything_says_nothing_about_what_it_left_out (
	tmp_path: pathlib.Path,
) -> None:
	"""§12.2a: a line that says the same thing on every page says nothing.

	The pair is what makes the count worth having — a *zero* printed beside every agenda would
	be noise on the ordinary day, and the ordinary day is most days: measured on this project's
	own instance, 11 of 170 open tasks carry a deadline at all.
	"""

	markup = _rendered(
		tmp_path, {"Agenda": {**SAMPLES["Agenda"], "more": 0, "later": 0}}
	)["Agenda"]

	assert "further out" not in markup
	assert "unscheduled." not in markup


def test_the_agenda_can_add_something_and_says_where_it_lands (tmp_path: pathlib.Path) -> None:
	"""§1.4: no entity may ever be *required* to create a task, and `/` is where a person lands.

	Before `SR#652` the root was a listing and carried an add box. Moving the agenda in without
	one would have made adding something require choosing a workspace first — which is the rule
	broken by the change that was meant to make the page more welcoming.

	It says where it lands because the agenda spans workspaces and a listing does not. The
	listing deliberately says nothing: a line on every page naming the only workspace there is
	would be §12.2a's column that says the same thing on every row.

	**What this does not prove, measured rather than assumed: that `App` passes `onAdd`.**
	`_rendered` supplies every handler the app uses, derived from the source, so a component
	always receives one here whether or not anything gives it one in the real page. Deleting
	`onAdd=` from `App`'s `Agenda` call leaves all of these green — falsified by doing it. That
	is `SR#640` in its purest form: the component is right, the props are right, and nothing
	checks the wire between them.
	"""

	agenda = _rendered(tmp_path, {"Agenda": SAMPLES["Agenda"]})["Agenda"]
	listing = _rendered(tmp_path, {"Listing": SAMPLES["Listing"]})["Listing"]

	# **The harness flattens to tag names and text**, so an attribute is invisible to it — the
	# placeholder cannot be asserted on and the form and its button can.
	assert "<form><div><input><button>Add" in agenda, (
		"a person landing on `/` cannot add anything"
	)
	assert "Adds to projects." in agenda

	assert "<form><div><input><button>Add" in listing
	assert "Adds to" not in listing


def test_a_day_with_nothing_in_it_can_still_be_added_to (tmp_path: pathlib.Path) -> None:
	"""The empty state is the one most likely to be somebody's first sight of the product."""

	markup = _rendered(
		tmp_path, {"Agenda": {"buckets": [], "more": 0, "where": "projects"}}
	)["Agenda"]

	assert "Nothing is due" in markup
	assert "<form><div><input><button>Add" in markup


def _views (
	tmp_path: pathlib.Path, calls: typing.Sequence[tuple[str, typing.Any]]
) -> list[typing.Any]:
	"""Drive the view and board decisions directly — `SR#651`, `SR#653`, `SR#640`'s point again."""

	module = _staged(tmp_path)

	return list(_ran(tmp_path, f"""
		import * as app from "{module.as_uri()}";

		const calls = {json.dumps(calls)};

		process.stdout.write(JSON.stringify(calls.map(([name, argument]) =>
			name === "viewOf" ? app.viewOf(argument)
			: name === "selectionOf" ? app.selectionOf(argument)
			: name === "showingOf" ? app.showingOf(argument)
			: name === "withShowing" ? app.withShowing(argument.path, argument.showing)
			: name === "chips" ? app.chips(argument.behind, argument.showing)
			: name === "reloads" ? app.reloads(argument.before, argument.after)
			: name === "moment" ? app.moment(argument.value, argument.now)
			: name === "releaseMoved"
				? app.releaseMoved(argument.served, argument.reported)
			: name === "calendarDay" ? app.calendarDay(argument.value, argument.zone)
			: name === "excluded" ? app.excluded(argument.key, argument.selection)
			: name === "listingAddress" ? app.listingAddress(argument)
			: name === "pageTitle" ? app.pageTitle(argument)
			: name === "titlesByPath" ? app.titlesByPath(argument)
			: name === "filed" ? app.filed(argument.values, argument.slug)
			: name === "offered"
				? app.offered(
					argument.vocabulary, argument.kind, argument.hidden, argument.keep
				)
			: name === "notOffered" ? app.notOffered(argument.projects, argument.chosen)
			: name === "filableFor"
				? app.filableFor(argument.projects, argument.project, argument.prioritised)
			: name === "prioritisedHere"
				? app.prioritisedHere(argument.workspaces, argument.workspace)
			: name === "prioritisedSentence" ? app.prioritisedSentence(argument)
			: name === "rankedByPriority" ? app.rankedByPriority(argument)
			: name === "treeOrdered" ? app.treeOrdered(argument.projects)
			: name === "refAsked" ? app.refAsked(argument)
			: name === "placesToGo"
				? app.placesToGo(argument.workspaces, argument.projects, argument.showing)
			: name === "edited" ? app.edited(argument.values, argument.item)
			: name === "fromItem" ? app.fromItem(argument.item)
			: name === "withTime" ? app.withTime(argument.day, argument.time)
			: name === "TIMED" ? app.TIMED
			: name === "timeFor"
				? app.timeFor(argument.value, argument.allDay, argument.zone)
			: name === "conflictIn" ? app.conflictIn(argument.failure)
			: name === "authorOf" ? app.authorOf(argument.comment, argument.members)
			: name === "linkableTypes" ? app.linkableTypes(argument.vocabulary)
			: name === "linkChoices" ? app.linkChoices(argument.vocabulary)
			: name === "written" ? app.written(argument.values, argument.item)
			: name === "permits" ? app.permits(argument.name, argument.value)
			: name === "refused" ? (() => {{
				const made = app.refusal(argument.status, argument.problem);
				return {{ message: made.message, status: made.status,
					conflict: app.conflictIn(made) }};
			}})()
			: name === "people" ? app.people(argument.roster)
			: name === "freshly" ? app.freshly(argument.items, argument.since)
			: name === "touching"
				? app.touching(argument.events, argument.open, argument.page, argument.links)
			: name === "orderedAs" ? app.orderedAs(argument.selection)
			: name === "statusFor"
				? app.statusFor(argument.vocabulary, argument.kind, argument.category)
			: name === "unmovable" ? app.unmovable(argument.because, argument.category)
			: name === "offeredOrders" ? app.offeredOrders().map(([key]) => key)
			: name === "collectionsFor" ? app.collectionsFor(argument)
			: name === "inOrder"
				? app.inOrder(argument.rows, app.ORDERINGS[argument.order]).map((row) => row.ref)
			: name === "deferred" ? app.deferred(argument)
			: name === "sunkOrder" ? app.sunkOrder(argument)
			: name === "mergeOrder" ? app.mergeOrder(argument[0], argument[1])
			: name === "accumulated"
				? app.accumulated(argument[0], argument[1], argument[2])
			: app.columns(argument))));
	"""))


def test_stepping_back_asks_whether_the_selection_changed (tmp_path: pathlib.Path) -> None:
	"""**`SR#767`. Back out of the finished view and the finished rows stayed.**

	`arrive` decided whether to refetch with `agenda !== null || narrowed !== project`. Neither
	is true when only the selection changed, so pressing Back after choosing *done* left the
	reader looking at finished rows under an address saying the ordinary list — with an
	empty-state message that would have read *Nothing here yet.* if they emptied it.

	The branch predates the selection being in the address at all (`SR#738`): when only the
	project could change which rows there are, `narrowed !== project` was the whole question.

	**`reloads` is the decision and it was already lifted out** — `SR#640`'s route — so what was
	missing was not the rule but the *call*, which is that item's other half repeated: lift the
	decision out, then drive the thing that uses it.

	**This guards the wiring rather than the behaviour, and that is a limit worth stating.**
	`_driven` mounts the app at one address and cannot press Back — there is no popstate in the
	harness — so the honest test of the fix is not available without extending it. What is here
	catches the call being deleted and cannot catch it being made with the wrong arguments;
	`SR#767` carries what a real one would need.
	"""

	source = _without_comments(_served_modules()["app.js"])
	opens, closes = _braced(source, "const arrive = () => {")
	inside = source[opens:closes]

	assert "reloads(" in inside, (
		"`arrive` decides whether to refetch without asking `reloads`, so stepping back across "
		"a selection change leaves the rows of the selection the reader has left"
	)

	# **Before `nowShowing`, or it compares the new showing with itself.** `shown.current` is
	# the live copy and `nowShowing` overwrites it, so the order is the whole of the fix.
	assert inside.index("reloads(") < inside.index("nowShowing("), (
		"`reloads` is asked after `nowShowing` has already written the new showing, so it "
		"compares a value with itself and always answers no"
	)


def test_a_bare_address_still_reads_as_the_default_view (tmp_path: pathlib.Path) -> None:
	"""`SR#745`. The half that has to survive a control writing the arrangement out.

	**This test used to assert the opposite** — that the default is the absence of the parameter,
	so it could become per-workspace or per-user without invalidating a saved link (`SR#651`).
	Simon's call on 2026-08-10 narrowed that: a control writes `?view=list`, because `SR#649` says
	the address states what is showing *so what you send somebody is what you were looking at*, and
	an address omitting the arrangement hands its reader their own default instead of the sender's
	page.

	What did not change, and what this now guards, is that **an address typed by hand still falls
	back**. The fallback is what makes an address a person can shorten work at all.

	**The default is the agenda since `SR#1215`**, which is Simon's amendment to `SR#649` and is
	that decision's own unbuilt row — its grammar always paired `?view=list` with an agenda and
	nothing had built the pairing anywhere but the root.
	"""

	plain, empty, board = _views(tmp_path, [
		("viewOf", ""), ("viewOf", "?view="), ("viewOf", "?view=board"),
	])

	assert plain == {"view": "agenda", "refused": None}
	assert empty == {"view": "agenda", "refused": None}
	assert board == {"view": "board", "refused": None}

	[written] = _views(tmp_path, [
		("withShowing", {"path": "/projects", "showing": {"view": "list", "selection": {}}}),
	])

	assert written == "/projects?view=list", (
		"a control must write the arrangement it chose, including the default"
	)


def test_a_view_nobody_has_is_named_rather_than_blanking_the_page (
	tmp_path: pathlib.Path,
) -> None:
	"""The same answer `chosenWorkspace` gives for a workspace nobody can see.

	`api/query.py` refuses a query parameter a route does not declare, and is right to: an
	ignored `fields` returns the whole object and charges for it. But a *person* types a URL,
	and replacing their page with a failure over one wrong word is worse than showing the list
	and saying so. Refused, named, and not fatal.
	"""

	[answered] = _views(tmp_path, [("viewOf", "?view=gantt")])

	assert answered == {"view": "agenda", "refused": "gantt"}


def test_the_arrangement_survives_being_written_into_an_address (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#651`'s *survives navigation*, at the one place an address is written.

	Four places wrote one before this and every one dropped the query, so
	`/projects?view=board` became `/projects` the moment anything was opened — a setting that
	silently expired on the first click.
	"""

	board = {"view": "board", "selection": {}}

	kept, project, chosen, nothing = _views(tmp_path, [
		("withShowing", {"path": "/projects", "showing": board}),
		("withShowing", {"path": "/projects/subroutine", "showing": board}),
		("withShowing", {"path": "/projects", "showing": {
			"view": "list", "selection": {"status_category": "done"},
		}}),
		("withShowing", {"path": "/projects", "showing": {"view": None, "selection": {}}}),
	])

	assert kept == "/projects?view=board"
	assert project == "/projects/subroutine?view=board"

	# **A selection is written out even under the default arrangement** (`SR#738`), because
	# there is no default to fall back to: the absence of one *is* the ordinary selection, so an
	# address that dropped it would show a different set of rows than the one it came from.
	assert chosen == "/projects?view=list&status_category=done"

	# **An address with no arrangement to write is left alone**, which is what keeps a bare
	# `/projects` expressible at all — `listingAddress` builds one before a view is known.
	assert nothing == "/projects"


def test_an_address_naming_one_item_carries_neither (tmp_path: pathlib.Path) -> None:
	"""`SR#766`, found by Simon typing one into the bar.

	`/projects/ui/441` came back as `/projects/ui/441?view=list`. Both halves of a query here
	describe a *set* of rows — which ones arrive and how they are laid out — and an item address
	has one row that is part of no set. The parameter said nothing, in its default, about rows
	that were not there.

	**The app already disagreed with itself**, which is the sharper form of it: every link *to*
	an item that this app renders is `addressOf`, a bare path, in a listing row, in a prose
	mention and in the Links list. Only the rewrite after the click added a query, so the href a
	reader could copy and the address they landed on were two strings for one page.

	Parametrised over the shapes `parseAddress` distinguishes rather than over one example, so
	the durable form and the readable form are both covered — a check written from one of them
	would pass while the other kept its query.
	"""

	showing = {"view": "board", "selection": {"include_completed": "true"}}

	durable, readable, workspace, project, agenda, blank = _views(tmp_path, [
		("withShowing", {"path": "/projects/441", "showing": showing}),
		("withShowing", {"path": "/projects/ui/441", "showing": showing}),
		("withShowing", {"path": "/projects", "showing": showing}),
		("withShowing", {"path": "/projects/ui", "showing": showing}),
		("withShowing", {"path": "/", "showing": showing}),
		("withShowing", {"path": "", "showing": showing}),
	])

	assert durable == "/projects/441", "an item address takes no query, in its durable form"
	assert readable == "/projects/ui/441", "nor in its readable one"

	# **The selection goes too, not only the arrangement.** Dropping the view and keeping
	# `include_completed` would leave an address still describing a set of rows, which is the
	# thing an item address has none of — and it would read as considered rather than as missed.
	assert "include_completed" not in durable
	assert "include_completed" not in readable

	# Every listing address is untouched, which is what makes this a rule about the place rather
	# than a retreat from `SR#651`: what is showing still survives navigation everywhere it means
	# something.
	assert workspace == "/projects?view=board&include_completed=true"
	assert project == "/projects/ui?view=board&include_completed=true"
	assert agenda == "/?view=board&include_completed=true"

	# **`reloads` compares two selections by writing each onto the empty path**, so an empty path
	# reading as an item address would make every selection equal to every other and the app
	# would silently stop refetching when a chip was pressed.
	assert blank == "?view=board&include_completed=true"


def test_a_selection_nobody_can_send_is_refused_by_name_and_by_value (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#738`. What stops the address becoming a passthrough to `api/query.py`.

	That route refuses a parameter it does not declare, so forwarding whatever a person typed
	would replace their page with a 422 over one wrong word — the failure `viewOf` already
	declined for the arrangement, arriving by a second door.

	**A value outside its list is refused as well as an unknown name**, which is the half worth
	having: `status_category` exists and `finished` is not one of its categories, so admitting
	the name and passing the value is the check that reads as validation and is not.
	"""

	known, wrong, unknown, both = _views(tmp_path, [
		("selectionOf", "?status_category=done"),
		("selectionOf", "?status_category=finished"),
		("selectionOf", "?colour=red"),
		("selectionOf", "?status_category=finished&order=-title"),
	])

	assert known == {"selection": {"status_category": "done"}, "refused": []}

	assert wrong == {"selection": {}, "refused": ["status_category=finished"]}, (
		"a value outside the list must be refused, not forwarded"
	)

	assert unknown == {"selection": {}, "refused": []}, (
		"a name the app does not know is not the reader's mistake to be told about — it is "
		"simply not a selection, and every address carries parameters this app has no opinion on"
	)

	assert both["refused"] == ["status_category=finished", "order=-title"], (
		"a reader who mistyped two things needs to be told about both"
	)


def test_an_address_can_ask_for_finished_work_on_either_arrangement (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#738`, and the thing that says the split is real rather than cosmetic.

	While a view name carried the selection, *a list including finished work* and *a board of
	only finished work* could not be expressed at all — there was no name for either, and adding
	one would have meant a fourth and a fifth view. Separating the two parameters makes both
	fall out with nothing built for them.
	"""

	# **Both arrangements in one build**, because the claim is that neither needed anything
	# built for it. This used to read `wide, narrow = _built(...), None` and end with
	# `assert narrow is None` — a tautology under a name suggesting a second case had been
	# checked (`#947`, cold review `#927`'s L-3). The second case is asserted rather than
	# named now.
	built = _built(tmp_path, [
		("listingRequests", ["personal", None, None, {"include_completed": "true"}]),
		("listingRequests", ["personal", None, None, {"status_category": "in_progress"}]),
	])

	tasks = [request for request in built if "/tasks" in request["path"]]

	assert "include_completed=true" in tasks[0]["path"], (
		"a list must be able to include finished work, which no view name could ask for"
	)

	assert [request for request in built if "/documents" in request["path"]], (
		"including finished work says nothing about documents, so both collections stay"
	)

	assert any("status_category=in_progress" in request["path"] for request in tasks), (
		"a board of one category must be expressible too — that is what makes the split real "
		"rather than a rename, and it was the half this test named and never checked"
	)


def test_a_category_selection_reads_tasks_alone_whichever_category_it_is (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#738`. The reason is the filter, not the word *done*.

	`GET /v1/documents` refuses `status_category` outright — 422, measured — because a document
	has no such axis. That holds for `in_progress` exactly as it does for `done`, and keying the
	decision on a view name stated the consequence while hiding the reason: a second category
	would have shipped a page that does not load.
	"""

	progress = _built(tmp_path, [
		("listingRequests", ["personal", None, None, {"status_category": "in_progress"}]),
	])

	assert [request for request in progress if "/tasks" in request["path"]]

	assert not [request for request in progress if "/documents" in request["path"]], (
		"any status_category selection must skip documents, not only the finished one"
	)


def test_the_controls_write_the_addresses_they_are_about_to_navigate_to (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#738` and `SR#722`. Two arrangements and a selection, and each is a real address.

	**Chosen is computed, never remembered**, so an address no control produces highlights
	nothing — which is true rather than tidy, and is what stops the switcher claiming a reader is
	somewhere they are not.

	**The done control keeps whichever arrangement is showing**, which is the split doing its
	job: a board of finished work is reachable and its address says exactly that.

	**Four controls since `SR#1215`**, and the agenda is first because it is the default — a
	switcher whose first option was something else would read as though the page had chosen the
	second. `done` names `list` outright rather than following the default: an agenda holds back
	finished work by construction, so a *done* chip that inherited the new default would produce
	an empty page.
	"""

	plain, board, finished = _views(tmp_path, [
		("chips", {"behind": "/projects", "showing": {"view": "list", "selection": {}}}),
		("chips", {"behind": "/projects", "showing": {
			"view": "board", "selection": {"include_completed": "true"},
		}}),
		("chips", {"behind": "/projects", "showing": {
			"view": "board", "selection": {"status_category": "done"},
		}}),
	])

	assert [chip["href"] for chip in plain] == [
		"/projects?view=agenda",
		"/projects?view=list",
		"/projects?view=board&include_completed=true",
		"/projects?view=list&status_category=done&order=-completed_at",
	]

	assert [chip["name"] for chip in plain if chip["chosen"]] == ["list"]
	assert [chip["name"] for chip in board if chip["chosen"]] == ["board"]

	assert [chip["name"] for chip in finished if chip["chosen"]] == ["done"], (
		"a board narrowed to finished work is on the done control, not on the board one"
	)

	# **The done control shows a list whatever is showing** (`SR#745`). It used to keep the
	# arrangement; driving it gave a board with one populated column and three empty ones, and
	# the reason is that its `order=-completed_at` means nothing on a board — columns discard
	# the sequence rows arrived in.
	assert [chip["href"] for chip in finished][3] \
		== "/projects?view=list&status_category=done&order=-completed_at", (
			"the done control must show a list, since the order it asks for needs one"
		)


def test_every_arrangement_this_app_has_can_be_reached_from_a_control (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#651`'s reason for having controls at all, asked of `VIEWS` rather than of a list here.

	*A reader who has never seen one cannot type a word they have not been told* — so an
	arrangement with no control is a feature nobody finds. `chips` names its three entries
	literally, because two of them are arrangements and one is a selection and the selections
	differ per arrangement, so it cannot be derived; this is what makes that literal safe.

	**Derived from `VIEWS`**, so a third arrangement fails here rather than shipping unreachable.
	"""

	[offered] = _views(tmp_path, [
		("chips", {"behind": "/projects", "showing": {"view": "list", "selection": {}}}),
	])
	named = {chip["name"] for chip in offered}

	assert set(_view_names()) <= named, (
		f"an arrangement this app has is not on any control: {set(_view_names()) - named}"
	)


def test_a_board_never_hides_a_row_it_is_holding (tmp_path: pathlib.Path) -> None:
	"""`SR#744`. Found by Simon on the first board he opened, and it was mine.

	At `?view=board&status_category=done&order=-completed_at` every column read *Nothing* or
	*Not shown* while the footer beneath them read **"Showing 100. There are more."** A hundred
	rows had been fetched, were held in the component, and were rendered by nothing.

	Two mistakes in one expression. It keyed on `include_completed` alone, when
	`status_category=done` is the *other* way of asking for finished work — and it won over the
	row branch entirely, so having rows could not save the column.

	**The row count is the first term now**, and that ordering is the whole fix: `excluded` is a
	model of what the instance did and can be wrong, where the rows are a fact. Being wrong about
	an empty column costs a word; being wrong about a full one costs the page.
	"""

	done = {"ref": 1, "kind": "task", "title": "Finished", "status_category": "done",
		"completed_at": _from_now(hours=-2)}

	narrowed = _rendered(tmp_path, {"Board": {
		"items": [done], "workspace": "projects",
		"selection": {"status_category": "done", "order": "-completed_at"},
	}})["Board"]

	assert "Finished" in narrowed, (
		f"a board holding a row rendered none of it: {narrowed}"
	)

	# **The Done column holds the row and the three the selection excluded say so.** Asserting
	# "Not shown" is simply absent would be wrong and would pass for the wrong reason: the other
	# three columns are genuinely not asked for and saying so is this whole feature.
	held = narrowed.split("Done")[1]

	assert "Not shown" not in held.split("Cancelled")[0], (
		f"the column holding the rows said it was not shown: {narrowed}"
	)

	assert narrowed.count("Not shown") == 3, (
		f"the three categories this selection excluded should each say so, and only those: "
		f"{narrowed}"
	)

	# **The rows and the model disagreeing is the case the ordering is actually for**, and
	# falsifying showed it is the only one that reaches it: with `excluded` correct, dropping
	# the row-count term is invisible to the case above, because a done selection does not
	# exclude the done column. So this is a board holding finished work under a selection that
	# says finished work was not asked for — which is what a changed API default, or a caller
	# passing one selection while another was fetched, looks like from here.
	#
	# `excluded` is a model of what the instance did. The rows are a fact. The fact wins.
	contradicted = _rendered(tmp_path, {"Board": {
		"items": [done], "workspace": "projects", "selection": {},
	}})["Board"]

	assert "Finished" in contradicted, (
		f"a board threw away a row it was holding because its own model of the selection said "
		f"the row should not exist: {contradicted}"
	)


def test_a_board_says_which_columns_a_selection_left_out (tmp_path: pathlib.Path) -> None:
	"""`SR#744`. Three ways a column can be absent, and they are one question.

	**The cancelled column has been lying since the board shipped**, which is `SR#718`'s defect
	in the column beside the one `SR#718` was about. Measured on the served instance: a plain
	listing of this project answers `{'todo': 143}` — no `done` and **no `cancelled`** — so the
	default excludes both finished categories rather than only the completed one, and a board
	without `include_completed` was reporting *Cancelled: Nothing* about work it never asked for.
	"""

	open_row = {"ref": 1, "kind": "task", "title": "Open", "status_category": "todo"}

	plain, everything = (
		_rendered(tmp_path, {"Board": {
			"items": [open_row], "workspace": "projects", "selection": selection,
		}})["Board"]
		for selection in ({}, {"include_completed": "true"})
	)

	assert plain.count("Not shown") == 2, (
		f"a board that did not ask for finished work must say so for *both* finished "
		f"categories, not only for done: {plain}"
	)

	assert "Not shown" not in everything, (
		f"a board that asked for everything still claimed a column was withheld: {everything}"
	)

	assert "Nothing" in everything, (
		"a column that really is empty must still say so — the categories are the structure"
	)


def test_a_row_says_the_time_it_finished_and_names_today_and_yesterday (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#746`, Simon's, from driving `SR#729`.

	> *Everything above the fold shows "done 9 Aug 2026" so the ordering is not apparent until I
	> scroll down to see "done 8 Aug 2026".*

	A page sorted on `completed_at` showed it as a day, so the order it claimed could not be
	checked — `SR#661`'s complaint from the other side.

	**`now` is passed in.** A fixture holding a fixed instant against the wall clock is a test
	that passes in the morning and fails in the evening, which this repository shipped and pushed
	on 2026-08-09 (`SR#737`).

	**Compared as local midnights**, so a clock change cannot turn yesterday into two days ago.
	"""

	now = datetime.datetime(2026, 8, 10, 9, 30, tzinfo=datetime.UTC)
	stamp = int(now.timestamp() * 1000)

	def at (hours: float) -> str:
		"""Return an instant this many hours from now, as the feed writes one."""

		return (now + datetime.timedelta(hours=hours)).isoformat()

	today, yesterday, older = _views(tmp_path, [
		("moment", {"value": at(-2), "now": stamp}),
		("moment", {"value": at(-14), "now": stamp}),
		("moment", {"value": at(-72), "now": stamp}),
	])

	assert today.startswith("today "), f"a completion hours old was not today: {today!r}"
	assert yesterday.startswith("yesterday "), f"the day before was not yesterday: {yesterday!r}"

	assert not older.startswith(("today", "yesterday")), (
		f"three days ago was named rather than dated: {older!r}"
	)

	for said in (today, yesterday, older):
		assert re.search(r"\d{1,2}[:.]\d{2}", said), (
			f"a finished stamp carried no time, so the order it is sorted on cannot be read: "
			f"{said!r}"
		)


def test_a_deadline_stays_a_day (tmp_path: pathlib.Path) -> None:
	"""`SR#746`'s other half, which is what stops the fix being "put a time on everything".

	A deadline and a planned day are dates **somebody chose**. A time on one would be precision
	the writer never supplied — 23:59 on a day meaning *that day*. Only the finished stamp is an
	instant the program recorded.
	"""

	due = {"ref": 1, "kind": "task", "title": "Due", "due_at": _from_now(hours=48)}
	planned = {"ref": 2, "kind": "task", "title": "Planned", "starts_at": _from_now(hours=48)}

	for sample in (due, planned):
		rendered = _rendered(
			tmp_path, {"Row": {"item": sample, "workspace": "projects"}}
		)["Row"]

		assert not re.search(r"\d{1,2}:\d{2}", rendered), (
			f"a date somebody chose was rendered with a time on it: {rendered}"
		)


def test_a_day_is_read_in_the_timezone_that_stored_it (tmp_path: pathlib.Path) -> None:
	"""`SR#773`, and it was live on the served instance when it was found.

	`SR#589` is due all day on **Friday 14 August**, stored as `2026-08-14T23:59:59.999999Z`
	because §6.5 puts an all-day deadline at the last instant of its day in the task's own
	timezone. Rendered in the *reader's* timezone that is Saturday the 15th anywhere east of
	UTC — so `subroutine agenda` printed `(due Fri 14 Aug)` and the browser printed 15 Aug, about
	one item, at one moment.

	**Wrong in both directions, for different fields**, which is why the fix is one function
	rather than one adjustment:

	| field | stored at | wrong for a reader |
	| --- | --- | --- |
	| an all-day `due_at` | the **end** of the day | **east** of the task |
	| an all-day `snoozed_until` | the **beginning** | **west** of the task |
	| `starts_at` | not an instant at all | west of UTC, by being parsed |

	**Sydney rather than London**, for `SR#532`'s reason: its abbreviations are never zone names
	and it is far enough east to cross the end-of-day boundary, so this cannot pass by season
	the way a London test would — UTC in winter, UTC+1 in summer, and CI is always UTC.
	"""

	east, west, plain, none = _views(tmp_path, [
		# The real value off `SR#589`, read in a zone ten hours ahead.
		("calendarDay", {"value": "2026-08-14T23:59:59.999999Z", "zone": "Etc/UTC"}),
		("calendarDay", {"value": "2026-08-14T00:00:00Z", "zone": "Etc/UTC"}),
		# **A bare date is returned untouched**, and that is not an optimisation: `starts_at`
		# has no instant behind it, and `new Date("2026-08-13")` is UTC midnight — so parsing it
		# moves it to the 12th anywhere west of UTC. Not parsing is the only exact answer.
		#
		# **West, and this assertion was Sydney until a surviving mutation corrected it.** UTC
		# midnight is still the 13th anywhere *ahead* of UTC, so the short-circuit it is meant to
		# be checking was doing nothing observable and removing it changed no answer.
		("calendarDay", {"value": "2026-08-13", "zone": "America/Los_Angeles"}),
		("calendarDay", {"value": None, "zone": "Etc/UTC"}),
	])

	assert east == "2026-08-14", "an all-day deadline moved to the next day"
	assert west == "2026-08-14", "an all-day start moved to the previous day"
	assert plain == "2026-08-13", "a calendar date was parsed as an instant and moved"
	assert none is None

	# The zone that stored it decides, so the *same instant* is two different days depending on
	# whose day it was — which is the whole point, and the assertion that fails if the parameter
	# is ever ignored.
	[sydney] = _views(tmp_path, [
		("calendarDay", {"value": "2026-08-14T23:59:59.999999Z", "zone": "Australia/Sydney"}),
	])

	assert sydney == "2026-08-15"


def test_a_row_and_the_terminal_agree_about_a_deadline (tmp_path: pathlib.Path) -> None:
	"""`SR#773`'s other half: the fix has to reach what a reader actually looks at.

	`calendarDay` being right is worth nothing if `Facts` and `Row` go on calling `day` without
	the task's timezone — the rule right, the display right, and no wire between them, which is
	this app's signature fault and the one `SR#640` exists for.

	Driven through `Row`, and the assertion is on the **day number** rather than on a whole
	string, because the month name is the reader's locale and the machine running this is not
	the machine reading it.
	"""

	# **Stored in a zone behind UTC, and that is the whole of what makes this falsifiable.** The
	# fallback when no zone is passed is UTC, so a task whose own zone *is* UTC renders the same
	# either way and dropping the parameter changes nothing — which is exactly what happened when
	# this fixture said `Etc/UTC`, and a mutation that should have failed passed.
	#
	# End of 15 August in Los Angeles is 06:59 UTC on the **16th**, so the task's day and the
	# UTC day are different numbers and only one of them is right.
	rendered = _rendered(tmp_path, {"Row": {"workspace": "projects", "item": {
		"ref": 589, "kind": "task", "title": "A second human has used this instance",
		"due_at": "2126-08-16T06:59:59.999999Z", "timezone": "America/Los_Angeles",
	}}})["Row"]

	# A hundred years out, so it is never overdue and the row renders `due …` rather than the
	# overdue badge — the same shape, without a fixture that expires.
	assert "15" in rendered and "16" not in rendered, (
		f"the row shows the deadline's UTC day rather than the task's own: {rendered}"
	)


#: The fields §6.5 stores as a *day* rather than as an instant, so rendering one needs the
#: timezone that stored it. `updated_at`, `created_at` and `completed_at` are deliberately
#: absent: those are moments the program recorded, and the reader's own zone is the right one.
DAY_SCALE = ("due_at", "starts_at", "snoozed_until")


def test_every_day_scale_date_is_rendered_with_its_timezone () -> None:
	"""`SR#773`, structurally, because one call site per test is a list that falls behind.

	The driven test above proves the rule reaches `Row`. It said nothing about `Facts`, and a
	mutation dropping the timezone there passed the whole suite — which is how this exists: the
	rule right, the display right, and no wire between them, on the third call site.

	**The fallback is what makes this invisible by hand.** `day` with no zone falls back to UTC,
	which is the correct answer on an instance whose timezone is UTC and wrong on any other. So
	a forgotten argument does nothing here and something on somebody else's machine.

	Structural rather than driven, so a fourth reader of one of these fields is covered the day
	it is written rather than the next time somebody notices.
	"""

	source = _without_prose(_served_modules()["app.js"])
	naked = re.findall(rf"day\(\s*\w+\.({'|'.join(DAY_SCALE)})\s*\)", source)

	assert not naked, (
		f"{sorted(set(naked))} are rendered by `day` with no timezone, so they will show the "
		f"day either side of themselves for a reader whose instance is not on UTC"
	)

	dressed = re.findall(rf"day\(\s*\w+\.({'|'.join(DAY_SCALE)}),", source)

	assert len(dressed) >= len(DAY_SCALE), (
		f"only {len(dressed)} day-scale renders were found, so this is checking almost nothing"
	)


def test_only_a_change_of_selection_asks_the_instance_again (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#738`. An arrangement is a rendering of rows already held; a selection is not.

	**Lifted out of `App` to be askable at all** (`SR#640`). A wrong answer here is that
	component's signature fault — the address right, the rule right, the display right, and the
	rows belonging to a selection the page has left.
	"""

	rearranged, narrowed, identical = _views(tmp_path, [
		("reloads", {"before": {"view": "list", "selection": {}},
			"after": {"view": "board", "selection": {}}}),
		("reloads", {"before": {"view": "list", "selection": {}},
			"after": {"view": "list", "selection": {"status_category": "done"}}}),
		("reloads", {"before": {"view": "list", "selection": {"order": "-completed_at",
			"status_category": "done"}},
			"after": {"view": "board", "selection": {"status_category": "done",
				"order": "-completed_at"}}}),
	])

	assert rearranged is False, "rearranging rows already held must not refetch them"
	assert narrowed is True, "a narrower selection is a different set of rows"

	assert identical is False, (
		"two spellings of one selection are one selection — comparing the objects would make "
		"key order significant"
	)


def test_a_board_column_nobody_asked_for_does_not_report_that_it_is_empty (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#738`, and it is `SR#718` arriving through a second door.

	Finished work is a selection now, so a board reached without it is a coherent thing to want —
	and an empty *Done* column under it would be a false statement rather than an empty one. A
	column is exactly where a reader looks to conclude nothing is left, and this project's own
	repeated lesson is that something which works and says something untrue about itself is
	worse than a failure (`SR#564`, `SR#568`, `SR#570`, `SR#572`).

	**Driven rather than built**, because what is being checked is which of two empty states
	`App` hands down — the decision `SAMPLES` cannot reach.
	"""

	bare = _driven(tmp_path, pathname="/projects", search="?view=board")
	asked = _driven(
		tmp_path, pathname="/projects", search="?view=board&include_completed=true",
	)

	assert "Not shown." in bare["said"], (
		f"a column nobody asked for reported on its contents: {bare['said']!r}"
	)

	assert "/projects?view=board&include_completed=true" in bare["links"], (
		f"the column says it is not shown and offers no way to show it: {bare['links']}"
	)

	assert "Not shown." not in asked["said"], (
		f"a board that did ask for finished work still said it had not: {asked['said']!r}"
	)


def test_closing_an_item_returns_to_what_is_behind_it (tmp_path: pathlib.Path) -> None:
	"""`SR#652`'s regression, found by reading `close` while wiring `SR#651` through it.

	It pushed `/` unconditionally — harmless while `/` was the list, and wrong the moment the
	agenda moved there: the address said the agenda while the page went on showing a workspace
	listing, so a reload or a step back gave something the reader had not been looking at.

	**Nothing failed.** An address disagreeing with its page is not a thing any test here can
	see, which is why the decision is a function now.
	"""

	root, space, narrowed = _views(tmp_path, [
		("listingAddress", {"agenda": True, "workspace": "projects", "project": "ui"}),
		("listingAddress", {"agenda": False, "workspace": "projects", "project": None}),
		("listingAddress", {"agenda": False, "workspace": "projects", "project": "ui"}),
	])

	assert root == "/", "the agenda's only address is `/`"
	assert space == "/projects"
	assert narrowed == "/projects/ui", "the filter is part of the address too (`SR#647`)"


def test_a_board_column_is_a_status_category_not_a_status_key (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#653`. Three seeded keys share `todo`, which is the whole argument for the field.

	A board built on keys would show three columns meaning one thing, and break on the first
	installation that renames one. `status_category` is published beside the renameable key
	precisely so a client may branch on it.
	"""

	[arranged] = _views(tmp_path, [("columns", [
		{"ref": 1, "status_category": "todo", "status": "open"},
		{"ref": 2, "status_category": "todo", "status": "blocked"},
		{"ref": 3, "status_category": "todo", "status": "needs_input"},
		{"ref": 4, "status_category": "in_progress", "status": "in_progress"},
	])])

	held = {column["key"]: [item["ref"] for item in column["items"]] for column in arranged}

	assert held["todo"] == [1, 2, 3], "three keys, one column"
	assert held["in_progress"] == [4]


def test_an_empty_task_column_is_shown_and_an_empty_document_column_is_not (
	tmp_path: pathlib.Path,
) -> None:
	"""They look like one rule and are two questions.

	The task categories *are* the structure — a board with no *In progress* reads as broken
	rather than as empty, and an empty column is where a card gets dragged to. Four empty
	document columns on a page holding no documents are §12.2a's column that says the same
	thing on every row, four times over.
	"""

	[tasks_only] = _views(tmp_path, [("columns", [{"ref": 1, "status_category": "todo"}])])

	assert [column["key"] for column in tasks_only] == [
		"todo", "in_progress", "done", "cancelled",
	]

	[mixed] = _views(tmp_path, [("columns", [
		{"ref": 1, "status_category": "todo"},
		{"ref": 2, "status_category": "current"},
	])])

	assert [column["key"] for column in mixed] == [
		"todo", "in_progress", "done", "cancelled", "current",
	]


def test_a_category_this_build_does_not_know_still_gets_a_column (
	tmp_path: pathlib.Path,
) -> None:
	"""A row must not leave the page because a client is older than its instance.

	`SR#345`'s direction: the client is the half that goes stale, and the failure mode worth
	preventing is silent. A column labelled with the raw key is a reader noticing something new;
	a missing one is a task that has vanished.
	"""

	[arranged] = _views(tmp_path, [("columns", [
		{"ref": 1, "status_category": "todo"},
		{"ref": 2, "status_category": "deferred_forever"},
	])])

	held = {column["key"]: [item["ref"] for item in column["items"]] for column in arranged}

	assert held["deferred_forever"] == [2]


def test_a_board_keeps_the_order_the_rows_arrived_in (tmp_path: pathlib.Path) -> None:
	"""And deliberately does not rank by the reported `priority_score`.

	The field a client reads is `importance * urgency`, null unless both are set; the *ordering*
	of that name applies §6.3a's three bands. Sorting a column by the reported field would put a
	part-ranked item below an unranked one — the exact defect the bands were added to fix,
	reintroduced one layer up and only in the board. Ranking is `?order=` on the fetch, where
	the database applies them.
	"""

	[arranged] = _views(tmp_path, [("columns", [
		{"ref": 1, "status_category": "todo", "importance": None, "urgency": None},
		{"ref": 2, "status_category": "todo", "importance": 5, "urgency": None},
		{"ref": 3, "status_category": "todo", "importance": 5, "urgency": 5},
	])])

	assert [item["ref"] for item in arranged[0]["items"]] == [1, 2, 3]


def test_a_board_shows_the_same_rows_the_list_does (tmp_path: pathlib.Path) -> None:
	"""Decision `SR#649`'s whole rule: the path says which rows, the query says how they look.

	The line worth holding is that no row may be lost or invented in the rearranging — a view
	that dropped one would be the query deciding which rows exist, which is §14.10's *scoping
	bug wearing a formatting hat*.
	"""

	rows = [
		{"ref": index, "status_category": category}
		for index, category in enumerate(
			["todo", "in_progress", "done", "cancelled", "current", "unheard_of", None], start=1
		)
	]

	[arranged] = _views(tmp_path, [("columns", rows)])
	placed = [item["ref"] for column in arranged for item in column["items"]]

	assert sorted(placed) == [row["ref"] for row in rows], (
		"every row the listing fetched must appear exactly once on the board"
	)


def test_the_board_asks_for_finished_work_and_the_list_does_not (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#718`. Without it the *Done* column is structurally incapable of holding anything.

	Measured on the served instance the day the board shipped: the same request returned
	`{'todo': 100}` without the parameter and `{'todo': 59, 'done': 41}` with it. So the column
	was not showing a wrong number — it could not work, and the suite could not see it because
	nothing here had ever asked what a *board* fetches.

	**Decision `SR#649` was rewritten over this** (`SR#738`): the parameter is a *selection*, in
	the address beside the arrangement, rather than something a view name implies. What that
	buys is asked below — a list can now be told to include finished work, which was not
	expressible while `board` was the only thing that could say it.

	**Tasks only, and the asymmetry is right.** `GET /v1/documents` does not accept the
	parameter — it answers 422, which the driven-request guard caught before this shipped — and
	needs no equivalent: a document's categories are `draft`, `current`, `superseded` and
	`archived`, none of which means *stop showing me this*, so every document is in the listing
	already. My first version sent it to both and asserted a symmetry that does not exist.
	"""

	plain = _built(tmp_path, [("listingRequests", ["personal", None, None, {}])])
	board = _built(tmp_path, [
		("listingRequests", ["personal", None, None, {"include_completed": "true"}]),
	])

	assert not any("include_completed" in request["path"] for request in plain), (
		"the list must go on hiding finished work — it is the answer to 'what do I have to do'"
	)

	tasks = [request for request in board if "/tasks" in request["path"]]
	documents = [request for request in board if "/documents" in request["path"]]

	assert len(tasks) == 1 and len(documents) == 1

	assert "include_completed=true" in tasks[0]["path"], (
		"without it the Done column is empty by construction, not by chance"
	)

	assert "include_completed" not in documents[0]["path"], (
		"the documents listing refuses it, and needs no equivalent"
	)


def test_a_board_says_when_it_is_showing_only_part_of_the_work (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#718`, and the correction of a claim I made in the same file the day before.

	The comment beside the column tally said *a board is not paged, so the count is exact*. It
	is paged — it renders the rows `load` fetched, and that fetch is capped at `PAGE`, measured
	biting on this project's own board. A per-column number that reads as a total and is a page
	count is worse on a board than a short list is, because a column is exactly where somebody
	looks to conclude that nothing is left.
	"""

	whole = _rendered(tmp_path, {"Board": SAMPLES["Board"]})["Board"]

	assert "There are more" not in whole

	cut = _rendered(tmp_path, {
		"Board": {**SAMPLES["Board"], "more": {"tasks": "cursor", "documents": None}}
	})["Board"]

	assert "There are more" in cut
	assert "Show more" in cut


# ---- driving the app, not only rendering it (`SR#640`) -------------------------------------


def _driven (
	tmp_path: pathlib.Path,
	*,
	pathname: str = "/",
	search: str = "",
	answers: typing.Mapping[str, typing.Any] | None = None,
	ticks: int = 0,
	permissions: typing.Sequence[str] = ("task:write", "comment:write", "task:delete"),
) -> dict[str, typing.Any]:
	"""Mount the real app at one address and report what it asked the instance for.

	**This is the half `_mounted` cannot reach.** `preact-render-to-string` runs `useState` and
	`useCallback` and **not** `useEffect` — measured — so it sees the first paint and nothing
	after it. `App` does all of its work in an effect, so every fetch, every write and every
	decision about *what to ask for* was outside every test until this.

	**The app is imported rather than rendered here**, because `app.js` mounts itself when a
	`document` exists — which is what a browser does, boundary and all. Rendering it a second
	time would be a mount this page never performs.

	**Assert on the requests, not on the markup.** What went wrong five times was which of two
	correct values a component passed, and that is visible in the request and invisible in the
	HTML. Markup is `SAMPLES`' job and needs no DOM at all.

	**The one exception, and it is narrow**: `said` is the mounted page as flat text, and it is
	here for what `App` *decides* rather than for what a component renders — which prop it hands
	down, which sentence it chooses. Those live inside a hook and so are reachable by nothing
	else, which is the other half of `SR#640`. A test asserting on layout, class names or the
	shape of a component's output is in the wrong place and belongs in `SAMPLES`.

	**`ticks` runs the poll by hand**, because `POLL_MS` is ten seconds and a suite cannot wait
	for two of them. `setInterval` is captured rather than left to fire, so a test says *now*
	and reads what the page asked for — which is the only way to pose a question about the
	**second** tick, and `SR#781` was a defect only the second tick could show.

	This substitutes a second mechanism, on top of `fetch`, and the risk `tests/dom.js` names
	applies: a harness that replaces the thing under test confirms only the half that was not
	broken. It is defensible here because the interval is not the subject — what the callback
	*decides* is — and `clearInterval` is honoured, so a stale interval from a re-run effect
	cannot be run by mistake.
	"""

	module = _staged(tmp_path)
	replies = dict(answers or {})
	permitted = json.dumps(list(permissions))

	return dict(_ran(tmp_path, f"""
		import {{ install, text }} from "{(tmp_path / DOM.name).as_uri()}";

		const {{ root, written }} = install(
			{json.dumps({"pathname": pathname, "search": search})}
		);
		const replies = {json.dumps(replies)};
		const asked = [];

		/* **The poll, held rather than left to fire.** `clearInterval` really removes it, so an
		   effect that re-ran leaves no second callback behind to be run by mistake. */
		const running = new Map();
		let handles = 0;

		globalThis.setInterval = (run) => {{
			handles += 1;
			running.set(handles, run);

			return handles;
		}};

		globalThis.clearInterval = (handle) => {{
			running.delete(handle);
		}};

		/* Enough of an answer for the app to carry on, and no more. A fixture that returned
		   real-looking rows would invite assertions about rendering, which is not what this is
		   for and is already covered without a DOM. */
		function answered (path) {{
			for (const [fragment, body] of Object.entries(replies)) {{
				if (path.includes(fragment)) return body;
			}}

			if (path.includes("/me")) {{
				return {{
					user: {{ username: "si", is_service_account: false }},
					workspaces: [{{
						slug: "projects", id: "w1", role: "owner",
						/* **What an owner really has**, because the app reads this now
						   (`SR#927`'s M-25) and an empty list means a reader who may do
						   nothing — which is a different page and not the one most of these
						   tests are about. It was empty when nothing read it. */
						permissions: {permitted},
					}}],
					instance_permissions: [],
					credential: null,
				}};
			}}

			return {{ items: [], page: {{ has_more: false, next_cursor: null, total: null }} }};
		}}

		globalThis.fetch = async (path, options = {{}}) => {{
			asked.push({{ method: (options.method || "GET"), path }});

			return {{
				ok: true,
				status: 200,
				headers: {{ get: () => "application/json" }},
				json: async () => answered(path),
			}};
		}};

		await import("{module.as_uri()}");

		/* Long enough for the mount, its effect, and everything that effect awaits — and far
		   short of `POLL_MS`, so what is recorded is the *first* load rather than a poll
		   quietly correcting it. That distinction is `SR#719`, which presented as the right
		   rows arriving ten seconds late. */
		await new Promise((done) => setTimeout(done, 300));

		/* Each tick is recorded separately, so a test can ask what the *second* one did rather
		   than only what the page has asked for in total. */
		const rounds = [];

		for (let round = 0; round < {ticks}; round += 1) {{
			const before = asked.length;

			for (const run of [...running.values()]) await run();

			await new Promise((done) => setTimeout(done, 50));
			rounds.push(asked.slice(before));
		}}

		/* **Every address the mounted page offers** (`SR#722`). The view switcher and the
		   detail page's controls are built inside `App`, so no component harness can reach
		   them — and whether they are links is precisely what this change is about. Walking
		   for one attribute is not markup assertion; it is the same narrow question `asked`
		   answers about requests. */
		function addresses (of) {{
			const found = of.href ? [of.href] : [];

			return found.concat(...of.childNodes.map(addresses));
		}}

		process.stdout.write(JSON.stringify(
			{{
				asked, written, said: text(root), links: addresses(root), rounds,
				/* **What the tab says** (`SR#1214`). The rule that decides it is pure and asked
				   directly next door; this is the wiring, which is where every fault this arc
				   shipped actually was. `document` is the shim's, so an unassigned title reads
				   as undefined and a page that writes none fails rather than passing quietly. */
				title: globalThis.document.title || null,
			}}
		));

		/* The poll's interval holds the process open, and a test that hangs is worse than one
		   that fails. */
		process.exit(0);
	"""))


def test_arriving_at_a_board_asks_for_finished_work_immediately (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#719`, and the reason this harness exists at all.

	The defect: `load` took the arrangement from *state*, and `setView` does not land in the
	render that calls it — so the first load after arriving at `?view=board` asked for the
	list's rows and the poll asked for the board's ten seconds later. Simon reported it as
	*"the completed column populates after a full 10 second wait"*, which is `POLL_MS` exactly.

	**Nothing in the suite could see it.** The decision was pure and tested; what was wrong was
	which of two correct values a component handed it. Falsified by reinstating the defect,
	which fails this and nothing else.
	"""

	driven = _driven(
		tmp_path, pathname="/projects/subroutine", search="?view=board&include_completed=true",
	)
	tasks = [call for call in driven["asked"] if "/v1/tasks" in call["path"]]

	assert tasks, f"the board asked for no tasks at all: {driven['asked']}"

	assert "include_completed=true" in tasks[0]["path"], (
		"the *first* load must ask for finished work, or the Done column fills a poll later — "
		f"{tasks[0]['path']}"
	)


def test_arriving_at_a_listing_does_not_ask_for_finished_work (
	tmp_path: pathlib.Path,
) -> None:
	"""The other direction, which is what stops the fix being 'always send it'.

	A list answers *what do I have to do*, so it goes on hiding finished work; that asymmetry is
	the qualification of decision `SR#649` recorded on it.
	"""

	driven = _driven(tmp_path, pathname="/projects/subroutine", search="?view=list")
	tasks = [call for call in driven["asked"] if "/v1/tasks" in call["path"]]

	assert tasks, "the listing asked for no tasks at all"
	assert all("include_completed" not in call["path"] for call in tasks)


def test_arriving_at_the_done_view_asks_for_finished_work_immediately (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#706`, and `SR#719`'s trap asked of the view that arrived after it.

	The board's version of this failed because `load` read the arrangement from state; the fix
	was to read it from the address, which is why this passes on arrival rather than a `POLL_MS`
	later. A third view is where a fix like that gets quietly undone, so it is asked again.
	"""

	driven = _driven(tmp_path, pathname="/projects/subroutine", search="?status_category=done&order=-completed_at")
	tasks = [call for call in driven["asked"] if "/v1/tasks" in call["path"]]

	assert tasks, f"the finished selection asked for no tasks at all: {driven['asked']}"

	assert "status_category=done" in tasks[0]["path"], (
		f"the *first* load must narrow to finished work — {tasks[0]['path']}"
	)

	assert "order=-completed_at" in tasks[0]["path"], (
		f"a page claiming 'most recently finished' must ask for that order — {tasks[0]['path']}"
	)


def test_the_done_view_never_asks_documents_a_question_they_refuse (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#706`. The failure this prevents is a page that does not load, not one that is wide.

	`GET /v1/documents` refuses `status_category` outright — 422, measured — because a document
	has no completed axis at all: its categories are `draft`, `current`, `superseded` and
	`archived`, and none of them means *finished*. So the done view reads one collection.

	**Driven rather than built**, because the question is what the mounted app does on arrival:
	the same asymmetry was got wrong once already on the board (`SR#718`), where the guard caught
	`include_completed` being sent to both.
	"""

	driven = _driven(tmp_path, pathname="/projects", search="?status_category=done&order=-completed_at")
	documents = [call for call in driven["asked"] if "/v1/documents" in call["path"]]

	assert not documents, (
		f"the done view asked the documents listing for something it refuses: {documents}"
	)

	plain = _driven(tmp_path, pathname="/projects", search="?view=list")

	assert [call for call in plain["asked"] if "/v1/documents" in call["path"]], (
		"the ordinary list must still read both collections — one ref counter serves them, so "
		"half the numbers a reader has learned would not exist"
	)


def test_each_view_can_be_opened_in_its_own_tab (tmp_path: pathlib.Path) -> None:
	"""`SR#722`. The switcher was buttons, so the board could only replace the list.

	The addresses come from `chips`, which is what `chooseView` is about to write — building them
	here by hand would be a second copy of the rule `SR#651` centralised, and the two would drift
	the first time a default moved.

	**Built inside `App`**, so no component harness can see them; this is what the mounted page
	actually offers.
	"""

	driven = _driven(tmp_path, pathname="/projects")

	wanted = (
		"/projects?view=list",
		"/projects?view=board&include_completed=true",
		"/projects?view=list&status_category=done&order=-completed_at",
	)

	for address in wanted:
		assert address in driven["links"], (
			f"{address} is not something a reader can open in a tab: {driven['links']}"
		)


def test_the_page_behind_an_open_item_is_a_link (tmp_path: pathlib.Path) -> None:
	"""`SR#722`. *All items* was a button, so there was no way back except in this tab.

	It carries the arrangement, so going back from an item opened on the board returns to the
	board rather than to the list — the same address `close` writes, which is why both read it
	from one expression.
	"""

	# **The two narrower paths come first**, because the fixture matches on a fragment being
	# present and `/v1/tasks/42` is inside `/v1/tasks/42/links`. Answering a listing with a bare
	# object leaves `links.items` undefined and the boundary shows its fallback — a page with no
	# links on it at all, which is what this asserts about and would have passed for the wrong
	# reason if the assertion had been the other way round.
	empty = {"items": [], "page": {"has_more": False, "next_cursor": None, "total": None}}

	driven = _driven(
		tmp_path, pathname="/projects/42", search="?view=board",
		answers={
			"/v1/tasks/42/links": empty,
			"/v1/tasks/42/comments": empty,
			"/v1/tasks/42": {"ref": 42, "kind": "task", "title": "Open",
				"status_category": "todo"},
		},
	)

	assert "/projects?view=board" in driven["links"], (
		f"there is no link back to the board an item was opened from: {driven['links']}"
	)


def test_the_finished_view_offers_no_capture_box (tmp_path: pathlib.Path) -> None:
	"""`SR#706`. Adding from here reports success over a page the new item cannot appear on.

	A captured item is open and this view holds only what is over, so the note would say
	*Added #123* and the list would not change. That is `SR#515`'s shape — every step reports
	success and the reader is left confirming the wrong conclusion — and it is the reason the box
	is withheld rather than merely unhelpful.

	**The decision is `App`'s and lives inside a hook**, so `SAMPLES` cannot reach it: `Listing`
	renders whatever it is handed, and is already checked both ways. What is unchecked without
	this is which of the two `App` hands down.
	"""

	done = _driven(tmp_path, pathname="/projects", search="?status_category=done&order=-completed_at")
	plain = _driven(tmp_path, pathname="/projects", search="?view=list")

	assert "Add" not in done["said"], (
		f"the finished view offered a capture box: {done['said']!r}"
	)

	assert "Add" in plain["said"], (
		f"the ordinary list lost its capture box, which is §1.4's primary path: {plain['said']!r}"
	)


def test_an_empty_page_says_which_question_it_answered (tmp_path: pathlib.Path) -> None:
	"""`SR#706`. *Nothing here yet* under the finished view means the opposite of what it says.

	A reader checking whether an agent has been working reads it as an empty workspace, when what
	it means is that nothing has been finished — and those are different facts with different
	next actions. The sentence comes from the caller because the caller is what knows which view
	it asked for.
	"""

	done = _driven(tmp_path, pathname="/projects", search="?status_category=done&order=-completed_at")
	plain = _driven(tmp_path, pathname="/projects", search="?view=list")

	assert "Nothing has been finished here yet." in done["said"], (
		f"an empty finished view did not say what was empty: {done['said']!r}"
	)

	assert "Nothing here yet." in plain["said"], (
		f"an empty list stopped saying it was empty: {plain['said']!r}"
	)


def test_arriving_at_the_root_asks_for_the_agenda_across_every_workspace (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#652`. `/` is the agenda, and naming a workspace would answer a different question.

	Measured on the served instance: `projects` alone returns 153 unscheduled where naming
	nothing returns 160 and an overdue row the narrower question cannot see. A scoped agenda
	would look right — a shorter one is indistinguishable from a lighter day — which is why the
	absence of the parameter is worth a test rather than a comment.
	"""

	driven = _driven(tmp_path)
	agenda = [call for call in driven["asked"] if "/v1/agenda" in call["path"]]

	assert len(agenda) == 1, f"expected one agenda request, got {driven['asked']}"
	assert "workspace" not in agenda[0]["path"], agenda[0]["path"]

	assert not any("/v1/tasks" in call["path"] for call in driven["asked"]), (
		"the root is the agenda, so it must not also fetch a listing"
	)


def test_a_place_opens_on_its_own_agenda_and_view_list_is_obeyed_there (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#1215`, Simon's amendment to `SR#649`, and that decision's own unbuilt row.

	Its grammar always applied `?view=` *"on any of the above"* and its §*Why `/` is the agenda*
	spelled the pairing out — *"`/?view=list` is the backlog"*. Nothing built it: the root showed
	an agenda because the **path** named no workspace, so `?view=list` was dropped everywhere and
	`/projects/subroutine` could only ever be a list.

	**Both directions, because either alone passes against half the change.** A version that made
	the agenda the default and never obeyed `?view=list` satisfies the first assertion; one that
	kept the listing default satisfies the second.
	"""

	space = _driven(tmp_path, pathname="/projects")
	asked = [call["path"] for call in space["asked"] if "/v1/agenda" in call["path"]]

	assert len(asked) == 1, f"a workspace did not open on an agenda: {space['asked']}"
	assert "workspace_id=projects" in asked[0], (
		f"the workspace agenda was not narrowed to the workspace: {asked[0]}"
	)
	assert "project=" not in asked[0], (
		f"a whole workspace was asked about as though it were one project: {asked[0]}"
	)

	filed = _driven(tmp_path, pathname="/projects/subroutine")
	narrowed = [call["path"] for call in filed["asked"] if "/v1/agenda" in call["path"]]

	assert len(narrowed) == 1, f"a project did not open on an agenda: {filed['asked']}"
	assert "workspace_id=projects" in narrowed[0] and "project=subroutine" in narrowed[0], (
		f"a project's agenda was not asked about that project: {narrowed[0]}"
	)

	# **`?project=` needs `workspace_id=` and the endpoint refuses it without one**, so the two
	# are written together or not at all. Driven against a real instance by
	# `test_every_request_this_app_can_make_is_one_the_instance_answers`; asserted here because
	# a request that sent a project alone would be a 422 and an empty page.
	assert narrowed[0].index("workspace_id=") < narrowed[0].index("project="), narrowed[0]

	listed = _driven(tmp_path, pathname="/projects/subroutine", search="?view=list")

	assert any("/v1/tasks" in call["path"] for call in listed["asked"]), (
		f"?view=list at a place is still ignored, which is the row `SR#649` wrote and nobody "
		f"built: {listed['asked']}"
	)
	assert not any("/v1/agenda" in call["path"] for call in listed["asked"]), (
		f"a listing address also fetched an agenda: {listed['asked']}"
	)


def test_a_filter_keeps_a_listing_even_when_no_arrangement_was_named (
	tmp_path: pathlib.Path,
) -> None:
	"""The one edge the agenda-as-default creates, and `SR#1215` answers it rather than leaving it.

	The agenda is a different endpoint with its own question, so it takes no `status_category`,
	no `include_completed` and no `order`. Left alone, `?status_category=done` with no view named
	would have become an agenda that silently ignored the words beside it — an address stating
	something the page is not doing, which is exactly what `SR#649` exists to prevent.

	**Two cases, answered differently because one is a mistake and the other is not.** A filter
	with no view named is somebody asking a question the list answers; nothing was refused and
	nothing is said. A filter beside an explicit `?view=agenda` is a reader asking for two things
	that cannot both happen, and that is named and fallen back exactly as an unknown view is.
	"""

	quiet, loud, plain = _views(tmp_path, [
		("showingOf", "?status_category=done&order=-completed_at"),
		("showingOf", "?view=agenda&status_category=done&order=-completed_at"),
		("showingOf", "?view=agenda"),
	])

	assert quiet["view"] == "list", (
		f"a filter with no arrangement named became an agenda, which cannot honour it: {quiet}"
	)
	assert quiet["refused"] == [], (
		f"falling back to the list is the default doing its job, not a refusal: {quiet}"
	)

	assert loud["view"] == "list", loud
	assert any("agenda" in word for word in loud["refused"]), (
		f"asking for an agenda and a filter together was silently resolved rather than named: "
		f"{loud}"
	)

	# **And the agenda is still reachable when nothing narrows it**, which is what stops the
	# rule above being satisfied by never choosing the agenda at all.
	assert plain == {"view": "agenda", "selection": {}, "refused": []}, plain


def test_every_request_the_app_makes_on_arrival_is_a_declared_builder (
	tmp_path: pathlib.Path,
) -> None:
	"""The static scan below this asserts the same thing by reading the source; this runs it.

	Both are worth having and neither implies the other. A scan sees a builder that is never
	called; this sees a request assembled by hand at a call site, which is the shape a scan
	over exported names is structurally unable to notice.
	"""

	driven = _driven(tmp_path, pathname="/projects", search="?view=board")
	paths = {call["path"].split("?")[0] for call in driven["asked"]}

	assert paths, "the app made no request at all, so this is checking nothing"

	known = {
		"/v1/me", "/v1/meta", "/v1/agenda", "/v1/tasks", "/v1/documents", "/v1/changes",
		"/v1/projects", "/v1/workspaces/projects/members",
	}
	invented = paths - known

	assert not invented, f"{sorted(invented)} is fetched and is not a route the app declares"


# --- The poll: what it resumes from, and what it re-reads --------------------------------


#: An empty collection, in the envelope every listing here uses.
NOTHING = {"items": [], "page": {"has_more": False, "next_cursor": None, "total": None}}


def _feed (events: list[dict[str, typing.Any]], *, more: bool = False) -> dict[str, typing.Any]:
	"""One answer from ``/v1/changes``, in the shape that endpoint actually returns."""

	return {"items": events, "page": {"has_more": more, "next_cursor": None, "total": None}}


def _event (seq: int, ref: int | None = None, kind: str = "task") -> dict[str, typing.Any]:
	"""One event, carrying only the three fields the poll asks for."""

	return {"seq": seq, "item_ref": ref, "workspace_id": "w1", "entity_type": kind}


def _open_item (tmp_path: pathlib.Path) -> dict[str, typing.Any]:
	"""What the fixture answers for one open task, narrowest paths first.

	The order is load-bearing and the reason is `SR#722`'s: the fixture matches on a fragment
	being *present*, and ``/v1/tasks/42`` is inside ``/v1/tasks/42/links``.
	"""

	return {
		"/v1/tasks/42/links": NOTHING,
		"/v1/tasks/42/comments": NOTHING,
		"/v1/tasks/42": {"ref": 42, "kind": "task", "workspace_id": "w1", "version": 1,
			"title": "Open", "status_category": "todo"},
	}


def test_a_poll_that_sees_nothing_new_reloads_nothing (tmp_path: pathlib.Path) -> None:
	"""`SR#781`, and it takes **two** ticks to see, which is why nothing had.

	``?since=`` is inclusive by decision (§5.11): the caller sends back the last seq it dealt
	with and is handed that event again. The poll asked for one row, was given that row, and
	tested ``items.length === 0`` — which could never be true. So the cursor never moved off
	where ``start`` put it and the listing was refetched every ten seconds for ever, while the
	change feed's answer was read only to be discarded.

	**Nothing about that looks wrong from either side.** The endpoint answers correctly, the
	caller reads a non-empty list, and the reader does see changes — ten seconds late, exactly
	as designed. Only the reason was wrong.

	Falsified by having `freshly` return what it was given: both rounds refetch and this fails.
	"""

	driven = _driven(
		tmp_path, pathname="/projects", ticks=2,
		answers={"changes?newest": _feed([_event(7)]), "changes?since": _feed([_event(7)])},
	)

	assert len(driven["rounds"]) == 2, "the poll did not run, so this is checking nothing"

	for number, round in enumerate(driven["rounds"], start=1):
		polls = [call for call in round if "/v1/changes" in call["path"]]
		reloads = [
			call for call in round
			if "/v1/tasks" in call["path"] or "/v1/documents" in call["path"]
			or "/v1/agenda" in call["path"]
		]

		assert polls, f"round {number} did not ask what had changed"
		assert not reloads, (
			f"round {number} refetched {[call['path'] for call in reloads]} although the feed "
			f"reported only the event the page had already dealt with"
		)


def test_a_poll_that_sees_something_new_reloads_the_listing (tmp_path: pathlib.Path) -> None:
	"""The other half, so the test above cannot pass by the poll doing nothing at all.

	A guard that only proves *nothing happened* is satisfied by a page that has stopped
	working, which is the failure this pair exists to tell apart.
	"""

	driven = _driven(
		tmp_path, pathname="/projects", search="?view=list", ticks=1,
		answers={
			"changes?newest": _feed([_event(7)]),
			"changes?since": _feed([_event(7), _event(8, ref=99)]),
		},
	)
	reloaded = [call for call in driven["rounds"][0] if "/v1/tasks" in call["path"]]

	assert reloaded, "an event nobody had seen left the listing unrefreshed"


def test_a_change_to_the_open_item_re_reads_it (tmp_path: pathlib.Path) -> None:
	"""`SR#657`. Simon, watching an agent work with an item's own URL open in the browser.

	The poll refreshed the listing behind the pane and never the pane. A status set by an agent,
	a description revised, and above all **a comment written while somebody is reading the
	item** appeared nowhere until the reader closed it and opened it again — with nothing on
	screen saying it was stale. `SR#638` is what made that ordinary rather than marginal: an
	item is a page somebody can be sent and can sit on.
	"""

	driven = _driven(
		tmp_path, pathname="/projects/42", ticks=1,
		answers={
			"changes?newest": _feed([_event(7)]),
			"changes?since": _feed([_event(7), _event(8, ref=42, kind="comment")]),
			**_open_item(tmp_path),
		},
	)
	reread = [
		call for call in driven["rounds"][0]
		if call["path"].split("?")[0] == "/v1/tasks/42"
	]

	assert reread, (
		"a comment was written on the item this reader has open and the pane was not re-read: "
		f"{[call['path'] for call in driven['rounds'][0]]}"
	)


def test_a_change_to_something_else_leaves_the_open_item_alone (
	tmp_path: pathlib.Path,
) -> None:
	"""The pane is re-read because *it* moved, not because anything did.

	Without this the test above passes for a page that refetches the open item on every tick,
	which is the version of this feature that costs two requests a tick for ever.
	"""

	driven = _driven(
		tmp_path, pathname="/projects/42", ticks=1,
		answers={
			"changes?newest": _feed([_event(7)]),
			"changes?since": _feed([_event(7), _event(8, ref=99)]),
			**_open_item(tmp_path),
		},
	)
	reread = [
		call for call in driven["rounds"][0]
		if call["path"].split("?")[0] == "/v1/tasks/42"
	]

	assert not reread, "something else moved and the open item was re-read anyway"


@pytest.mark.parametrize(
	("events", "since", "kept"),
	[
		# The resumed event comes back every time, and is the whole of `SR#781`.
		([7], 7, []),
		([7, 8, 9], 7, [8, 9]),
		# A first look has nothing to have dealt with already.
		([7], None, [7]),
		# A cursor behind the page — the catch-up case `?since=` exists for.
		([4, 5, 6], 3, [4, 5, 6]),
	],
)
def test_only_what_the_page_has_not_seen_counts_as_a_change (
	tmp_path: pathlib.Path, events: list[int], since: int | None, kept: list[int]
) -> None:
	"""The rule on its own — `SR#781`. The tests above drive the wire to it."""

	answer = _views(tmp_path, [
		("freshly", {"items": [_event(seq) for seq in events], "since": since}),
	])[0]

	assert [one["seq"] for one in answer] == kept


@pytest.mark.parametrize(
	("events", "more", "expected", "why"),
	[
		([(8, 42, "task")], False, True, "the open item was updated"),
		([(8, 99, "task")], False, False, "something else was updated"),
		([(8, 99, "link")], False, True, "a link names one end and either could be this one"),
		([(8, 99, "task")], True, True, "a batch that had to stop may have hidden it"),
	],
)
def test_what_counts_as_touching_the_open_item (
	tmp_path: pathlib.Path,
	events: list[tuple[int, int, str]],
	more: bool,
	expected: bool,
	why: str,
) -> None:
	"""`SR#657`'s decision, including the two cases that are deliberately generous.

	**A link event names its source**, measured on the live instance: `entity_type: "link"`
	carries the source item's ref. So linking #A to #B is invisible to #B, which is the end that
	grew a backlink — and rather than reason about which end a reader is on, any link event
	re-reads. **A truncated batch is treated as yes** for the same reason: the honest answer to
	*I could not see all of it* is to look, and being wrong costs one request.
	"""

	answer = _views(tmp_path, [
		("touching", {
			"events": [_event(seq, ref, kind) for seq, ref, kind in events],
			"open": {"ref": 42, "workspace_id": "w1"},
			"page": {"has_more": more},
		}),
	])[0]

	assert answer is expected, why


@pytest.mark.parametrize(
	("ref", "expected", "why"),
	[
		(7, True, "a blocker of the open item was finished"),
		(9, True, "the other end of any link counts, not only a blocker"),
		(99, False, "an item this one is not joined to is still somebody else's business"),
	],
)
def test_the_far_end_of_a_link_counts_as_touching_the_open_item (
	tmp_path: pathlib.Path,
	ref: int,
	expected: bool,
	why: str,
) -> None:
	"""`SR#1147`: an item's own ref is not the only thing a reader on its page is looking at.

	Simon, driving it: *"I loaded /projects/subroutine/1123 expecting to see the blockers crossed
	off as work progressed, but in fact I had to reload the page to see a change."* Finishing
	`#1112` writes an event whose ``item_ref`` is 1112, and the page open is 1123 — so under the
	old rule nothing in the batch named the open item and nothing re-read, while
	``Links (14 of 14 blockers done)`` went on saying whatever it said when the page loaded.

	**The third case is what stops this being satisfied by re-reading on everything.** A set that
	had quietly become "any ref at all" would pass the first two and reintroduce `SR#781`, which
	was filed to stop exactly that.
	"""

	answer = _views(tmp_path, [
		("touching", {
			"events": [_event(8, ref, "task")],
			"open": {"ref": 42, "workspace_id": "w1"},
			"page": {"has_more": False},
			"links": [
				{"other": {"ref": 7}},
				{"other": {"ref": 9}},
			],
		}),
	])[0]

	assert answer is expected, why


def test_a_ref_in_another_workspace_is_not_this_item (tmp_path: pathlib.Path) -> None:
	"""A ref is unique per workspace (§6.2) and the agenda's poll spans every one of them.

	So the comparison carries the workspace beside the ref. Without it, #42 moving in a
	colleague's workspace re-reads #42 here — harmless, and the same mistake one step further
	along is how a listing comes to show somebody else's rows.
	"""

	answer = _views(tmp_path, [
		("touching", {
			"events": [{"seq": 8, "item_ref": 42, "workspace_id": "w2", "entity_type": "task"}],
			"open": {"ref": 42, "workspace_id": "w1"},
			"page": {"has_more": False},
		}),
	])[0]

	assert answer is False


def test_the_open_item_has_one_writer (tmp_path: pathlib.Path) -> None:
	"""`SR#657`, and the same rule `test_what_is_showing_has_one_writer` holds for `showing`.

	The poll's interval is built once per workspace and holds whatever `open` was then — which
	is nothing, because a reader opens an item long after the page settles. So there is a ref
	beside the state, and two copies of a fact are safe only while exactly one function moves
	them together.

	**What this cannot check**, said rather than implied: it finds the writes, not whether
	`nowOpen` writes them correctly. The tests above drive that.
	"""

	source = _without_prose(_served_modules()["app.js"])
	writers = [found.start() for found in re.finditer(r"(?<![\w$.])setOpen\s*\(", source)]

	assert writers, "no write of the open item was found, so this is checking nothing"

	opens, closes = _braced(source, "const nowOpen = useCallback(")
	inside = source[opens:closes]

	assert "setOpen(" in inside and "held.current =" in inside, (
		f"nowOpen is meant to write both copies of the open item; its body is {inside!r}"
	)

	stray = [at for at in writers if not opens <= at < closes]

	assert not stray, (
		f"{len(stray)} call(s) to setOpen sit outside nowOpen, at offsets {stray} — each one "
		"moves the state without moving the ref, so the poll would re-read the item that was "
		"open when its interval was built rather than the one on screen"
	)


# --- How the list says it is ordered (`SR#661`) ------------------------------------------


def _orderings () -> dict[str, dict[str, str]]:
	"""Every order the app has a sentence for, read from `ORDERINGS` itself."""

	source = _served_modules()["app.js"]
	block = re.search(r"export const ORDERINGS = \{(.*?)\n\};", source, re.S)

	assert block, "the app's orderings could not be read from app.js"

	found = {
		key: dict(re.findall(r"(\w+): (\"[^\"]*\"|true|false)", body))
		for key, body in re.findall(r'"([^"]+)": \{(.*?)\},', block.group(1), re.S)
	}

	assert found, "no ordering was parsed, so anything derived from this checks nothing"

	return found


def test_every_order_an_address_can_carry_says_how_it_reads () -> None:
	"""`SR#661`. An order a reader can reach and the page cannot describe is the defect itself.

	`SELECTABLE.order` is what an address may carry, and `ORDERINGS` is what a listing can say
	about one. Widening the first without the second gives a page that is ordered by something
	and says nothing — which is exactly what Simon reported, arriving by a new route.

	The default is in the set too, and has to be: `listingRequests` sends no `order` at all, so
	the commonest page in the product is the one with nothing in its address to look up.
	"""

	source = _served_modules()["app.js"]
	block = re.search(r"export const SELECTABLE = \{(.*?)\n\};", source, re.S)

	assert block, "the app's selectable parameters could not be read from app.js"

	orders = re.search(r"\n\torder: \[([^\]]*)\]", block.group(1))

	assert orders, "SELECTABLE no longer declares an order, so this is checking nothing"

	reachable = set(re.findall(r'"([^"]+)"', orders.group(1)))
	default = re.search(r'export const DEFAULT_ORDER = "([^"]+)"', source)

	assert default, "the default order could not be read"

	described = set(_orderings())
	silent = (reachable | {default.group(1)}) - described

	assert not silent, (
		f"{sorted(silent)} can be asked for and no sentence describes it, so the list would be "
		f"ordered by it and say nothing"
	)


def test_every_field_an_ordering_names_is_one_the_listing_asks_for () -> None:
	"""`SR#661`, and the guard beside this one is structurally unable to see it.

	*Every field a row renders is asked for* scans for `item.<name>`, and the ordering value is
	read as `item[ordering.field]` — a name that does not exist in the source. So the field
	could quietly leave `TASK_FIELDS`, arrive as null, and the mark would simply never appear:
	a row showing nothing where the sort key should be, which is indistinguishable from a row
	whose sort key is unset and is exactly the confusion this item exists to remove.

	**Both collections**, because the list is tasks *and* documents (§6.2) and an ordering half
	the rows cannot show is an order only half the page can be checked against.
	"""

	source = _served_modules()["app.js"]
	lists = {}

	for name in ("TASK_FIELDS", "DOCUMENT_FIELDS"):
		opens = source.index(f"const {name} = [")
		lists[name] = set(re.findall(r'"([a-z_]+)"', source[opens:source.index('].join(",");', opens)]))

		assert lists[name], f"{name} was not read, so this is checking nothing"

	for order, ordering in _orderings().items():
		both = ordering["both"] == "true"
		# **Two different obligations.** `shows` is what a row renders and is owed by whichever
		# collections receive the ordering; `field` is what `inOrder` merges on and is owed by
		# both — but only when both are asked, since a tasks-only ordering never merges.
		wanted = list(lists) if both else ["TASK_FIELDS"]
		owed = set(ordering["shows"].strip('"').split(",")) | ({ordering["field"].strip('"')} if both else set())

		for name in wanted:
			for field in owed:
				assert field in lists[name], (
					f"the list can be ordered by {order} and {name} does not ask for {field!r}, "
					f"so a row cannot show the value it is sorted on"
				)


def test_every_ordering_says_whether_deferred_work_sinks_in_it () -> None:
	"""`SR#877`. Sinking is a leading key, so it is a claim about *every* ordering.

	**A missing `sinks` is not a safe default, it is an unasked question.** `inOrder` reads the
	flag and a falsy one means *do not sink*, so an ordering added without it would quietly be
	the one arrangement on the page that mixes parked work back in — and the reader would have
	no way to tell that from a defect. Two entries say `false` and both say why, which is what
	makes them decisions rather than omissions.

	**An ordering that sinks owes `snoozed_until` to both collections**, because that is what
	`deferred` reads. The guard above asks the same of `field` and `shows`; a merge key the row
	does not carry arrives as undefined, and `deferred` would then answer *no* for every row —
	an ordering that claims to sink and silently does not.
	"""

	orderings = _orderings()
	source = _served_modules()["app.js"]
	lists = {}

	for name in ("TASK_FIELDS", "DOCUMENT_FIELDS"):
		opens = source.index(f"const {name} = [")
		lists[name] = set(re.findall(r'"([a-z_]+)"', source[opens:source.index('].join(",");', opens)]))

	undeclared = sorted(order for order, one in orderings.items() if "sinks" not in one)

	assert not undeclared, (
		f"{undeclared} do not say whether deferred work sinks in them, so `inOrder` will take "
		f"the silence for 'no' and one arrangement on the page will mix parked work back in"
	)

	sinking = sorted(order for order, one in orderings.items() if orderings[order]["sinks"] == "true")

	assert sinking, "no ordering sinks deferred work, so this checks nothing"

	for order in sinking:
		wanted = list(lists) if orderings[order]["both"] == "true" else ["TASK_FIELDS"]

		for name in wanted:
			# **`DOCUMENT_FIELDS` is exempt and that is the point of naming it here.** A
			# document has no start date at all (§6.14), so `deferred` reads undefined and
			# answers *no* — which is the same answer the server gives it, deliberately.
			if name == "DOCUMENT_FIELDS":
				continue

			assert "snoozed_until" in lists[name], (
				f"the list can be ordered by {order}, which sinks deferred work, and {name} "
				f"does not ask for 'snoozed_until' — so every row would answer 'not deferred'"
			)


def test_every_ordering_renders_by_a_name_the_app_knows () -> None:
	"""`SR#782`. `render` is a name so the table stays readable; a typo would show nothing.

	`orderingValue` returns null for anything it does not recognise, which is the safe half and
	is indistinguishable from `none` — *the row already shows this* — at runtime. So the check
	is here: every name in the table is one that function handles.
	"""

	source = _served_modules()["app.js"]
	body = _function_body(source, "orderingValue")
	handled = set(re.findall(r'ordering\.render === "(\w+)"', body)) | {"none"}

	assert len(handled) > 1, "no render names were found, so this is checking nothing"

	for order, ordering in _orderings().items():
		name = ordering["render"].strip('"')

		assert name in handled, (
			f"{order} renders by {name!r} and orderingValue handles {sorted(handled)} — so its "
			f"row would show nothing, exactly as if the field were unset"
		)


@pytest.mark.parametrize(
	("selection", "sentence"),
	[
		({}, "Newest first"),
		({"status_category": "done", "order": "-completed_at"}, "Most recently finished first"),
		({"order": "-created_at"}, "Newest first"),
	],
)
def test_how_a_list_says_it_is_ordered (
	tmp_path: pathlib.Path, selection: dict[str, str], sentence: str
) -> None:
	"""The rule on its own. An absent `order` is the default rather than no order at all."""

	answer = _views(tmp_path, [("orderedAs", {"selection": selection})])[0]

	assert answer is not None and answer["sentence"] == sentence


def test_a_row_shows_the_value_the_page_is_sorted_on (tmp_path: pathlib.Path) -> None:
	"""`SR#661`'s second half, which is the one Simon put first.

	*"I believe that the fields which are used for ordering may not be displayed. This makes the
	list hard to interpret."* The value is asked for already — it is the merge key `SR#660`
	added — and was read by nothing, which is the cheapest possible version of this defect.
	"""

	stamp = "2026-08-10T14:22:00+00:00"
	written = _rendered(tmp_path, {"Row": {
		"item": {"ref": 7, "kind": "task", "title": "Something", "created_at": stamp},
		"ordering": {"sentence": "Newest first", "field": "created_at", "shows": "created_at",
			"render": "moment", "label": "written", "both": True},
	}})["Row"]

	# **What `moment` says, asked of `moment`** rather than spelled out here. It formats for the
	# reader — 24-hour in London, 12-hour in New York — so a literal `14:22` is this machine's
	# locale written into a test as if it were the product's. It passed here and failed in CI as
	# `02:22 PM`, which is `SR#795`: the local gate and CI were not the same gate.
	said = _views(tmp_path, [("moment", {"value": stamp, "now": None})])[0]

	assert "written" in written and said in written, (
		f"the row does not show what the page is ordered on: {written} (expected {said!r})"
	)


def test_a_row_does_not_say_twice_what_it_already_says_once (tmp_path: pathlib.Path) -> None:
	"""The finished view prints `done <moment>` in its own cell and has since `SR#706`.

	Without `already` the same instant would appear twice on every row of that page — which is
	the shape `SR#582` is about: a guard, or a rule, that fires where the thing it is for is
	already handled.
	"""

	stamp = "2026-08-10T14:22:00+00:00"
	written = _rendered(tmp_path, {"Row": {
		"item": {"ref": 7, "kind": "task", "title": "Something", "status_category": "done",
			"completed_at": stamp},
		"ordering": {"sentence": "Most recently finished first", "field": "completed_at",
			"shows": "completed_at", "render": "none", "label": "finished", "both": False},
	}})["Row"]
	# Asked of `moment` rather than spelled out, for the reason above (`SR#795`).
	said = _views(tmp_path, [("moment", {"value": stamp, "now": None})])[0]

	assert written.count(said) == 1, f"the finished stamp is rendered twice: {written}"


def test_the_list_a_reader_arrives_at_says_how_it_is_ordered (tmp_path: pathlib.Path) -> None:
	"""Driven, because `SR#640` has cost this project six defects that a pure test could not see.

	`orderedAs` being right is worth nothing until something hands its answer to `Listing`.
	"""

	rows = {"items": [{"ref": 7, "kind": "task", "title": "Something",
		"created_at": "2026-08-10T14:22:00+00:00", "status_category": "todo"}],
		"page": {"has_more": False, "next_cursor": None, "total": None}}
	driven = _driven(
		tmp_path, pathname="/projects", search="?view=list",
		answers={"/v1/tasks": rows},
	)

	assert "Newest first" in driven["said"], (
		f"the list does not say how it is ordered: {driven['said'][:400]}"
	)


def test_every_order_a_reader_can_choose_is_one_the_api_can_sort_by (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#782`. The composition, and it is the half a pure test cannot reach.

	`SELECTABLE.order` is a list in a JavaScript file; `SORTABLE` is what the two routes
	actually accept. An order in the first and not the second is a **422 for the whole page**,
	and the reader chose it from a control we drew — the worst version of this, because the
	page they broke is the page they were on.

	`collectionsFor` is what keeps them apart: an order documents cannot answer must read the
	task collection alone, and this asserts the pairing rather than trusting it.
	"""

	sortable = {
		"task": set(subroutine.api.tasks.SORTABLE),
		"document": set(subroutine.api.documents.SORTABLE),
	}
	source = _served_modules()["app.js"]
	block = re.search(r"export const SELECTABLE = \{(.*?)\n\};", source, re.S)

	assert block, "the app's selectable parameters could not be read"

	orders = re.search(r"\n\torder: \[(.*?)\]", block.group(1), re.S)

	assert orders, "SELECTABLE no longer declares an order"

	for order in re.findall(r'"([^"]+)"', orders.group(1)):
		field = order.lstrip("-")
		reads = _views(tmp_path, [("collectionsFor", {"order": order})])[0]

		assert reads, f"{order} reads no collection at all"

		for kind in reads:
			assert field in sortable[kind], (
				f"the browser can be ordered by {order} and GET /v1/{kind}s cannot sort by "
				f"{field!r} — that is a 422 for the whole page, chosen from a control we drew"
			)


def test_a_priority_ordering_is_tasks_alone (tmp_path: pathlib.Path) -> None:
	"""Simon's decision, 2026-08-10 (`SR#782`), and the reason is in the data.

	A document has no importance and no urgency, so a merged list cannot be put in one priority
	order. Of drop, sink and do-not-offer, dropping needs no new machinery: `collectionsFor`
	already drops documents from a selection they cannot answer.
	"""

	both, ranked = _views(tmp_path, [
		("collectionsFor", {}),
		("collectionsFor", {"order": "-priority_score"}),
	])

	assert both == ["task", "document"], "the ordinary list is both kinds (§6.2)"
	assert ranked == ["task"]


@pytest.mark.parametrize(
	("order", "expected"),
	[
		# Written 1st, 2nd, 3rd; titles C, A, B. Refs are the tiebreak and follow the direction.
		("-created_at", [3, 2, 1]),
		("created_at", [1, 2, 3]),
		("title", [2, 3, 1]),
		("-updated_at", [1, 3, 2]),
	],
)
def test_the_merge_follows_the_order_that_was_asked_for (
	tmp_path: pathlib.Path, order: str, expected: list[int]
) -> None:
	"""`SR#782`. The list is two collections and one order, whichever order that is.

	`newestFirst` merged on `created_at` because that is what the API sorts and pages both
	collections by. The moment a reader can choose, a fixed merge key orders the page by one
	thing while the cursor walks another — rows repeating or vanishing at a page boundary,
	which is the defect keyset pagination exists to prevent and which looks nothing like a
	sorting fault.
	"""

	rows = [
		{"ref": 1, "created_at": "2026-08-01T09:00:00+00:00",
			"updated_at": "2026-08-09T09:00:00+00:00", "title": "Carrot"},
		{"ref": 2, "created_at": "2026-08-02T09:00:00+00:00",
			"updated_at": "2026-08-03T09:00:00+00:00", "title": "Apple"},
		{"ref": 3, "created_at": "2026-08-03T09:00:00+00:00",
			"updated_at": "2026-08-07T09:00:00+00:00", "title": "Banana"},
	]

	assert _views(tmp_path, [("inOrder", {"rows": rows, "order": order})])[0] == expected


@pytest.mark.parametrize(
	("order", "expected"),
	[
		# Written 1st, 2nd, 3rd, and #2 is deferred. It leads every one of these orderings on
		# its own merits and comes last in all of them, which is what a *leading* key means.
		("-created_at", [3, 1, 2]),
		("created_at", [1, 3, 2]),
		("title", [1, 3, 2]),
		# Not offered to a reader, and the row that proves the flag is read rather than assumed:
		# finished work is not waiting for anything, so this one leaves the order alone.
		("-completed_at", [2, 3, 1]),
	],
)
def test_the_merge_sinks_deferred_work_under_every_order_that_says_it_does (
	tmp_path: pathlib.Path, order: str, expected: list[int]
) -> None:
	"""`SR#877`. The server sinks the page and the client has to agree, or the merge undoes it.

	**Nothing saw this half.** Removing the leading key from `inOrder` left all 221 browser
	tests green: the request asked for `deferred,<order>`, the server answered correctly, and
	the merge put a deferred task back among the rest — `SR#640`'s shape for the sixth time,
	the rule right and the display right with nothing joining them.

	It is only reachable when both collections are on the page, which is what `accumulated`
	guards: a document has no `snoozed_until` and lands in the first band, so a deferred task
	merging above one is exactly the row that would look wrong to a reader.

	**Not multiplied by the direction**, which is the case `created_at` covers: *oldest first*
	must not mean *deferred first*, and a comparator that followed `way` would say it did.
	"""

	ahead = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365)).isoformat()

	rows = [
		{"ref": 1, "created_at": "2026-08-01T09:00:00+00:00", "title": "Apple",
			"completed_at": "2026-08-04T09:00:00+00:00"},
		{"ref": 2, "created_at": "2026-08-02T09:00:00+00:00", "title": "Banana",
			"completed_at": "2026-08-06T09:00:00+00:00", "snoozed_until": ahead},
		{"ref": 3, "created_at": "2026-08-03T09:00:00+00:00", "title": "Carrot",
			"completed_at": "2026-08-05T09:00:00+00:00"},
	]

	assert _views(tmp_path, [("inOrder", {"rows": rows, "order": order})])[0] == expected


@pytest.mark.parametrize(
	("selection", "expected"),
	[
		# The commonest page in the product, and the one this changes: it used to send no
		# order at all and lean on the API's default.
		({}, "deferred,-created_at"),
		({"order": "title"}, "deferred,title"),
		# **A search nobody has given an order to is left to the server** (`SR#875`), which
		# ranks it — naming an order here would overrule that ranking.
		({"q": "passport"}, None),
		# ...and a reader who *has* chosen one gets it sunk, search or no search: they have
		# said how they want the page arranged and relevance is not it.
		({"q": "passport", "order": "title"}, "deferred,title"),
		# The finished view, which says `sinks: false` and says why.
		({"order": "-completed_at"}, "-completed_at"),
	],
)
def test_the_order_the_browser_asks_for_sinks_deferred_work (
	tmp_path: pathlib.Path, selection: dict[str, str], expected: str | None
) -> None:
	"""`SR#877`. The address carries the reader's choice; the request carries the arrangement.

	**The server has to do the sinking, not the page.** Sorting the rows already fetched would
	sink them *within* a page, and which rows are on a page is decided by the order the query
	ran in — so a first page could be entirely deferred work with everything startable waiting
	behind *Show more*: a plausible, complete, wrong answer.

	Null means *send no order*, which is not the same as sending the default. It is how the
	server's own ranking survives, and `mergeOrder` reads the same situation off the rows that
	come back.
	"""

	assert _views(tmp_path, [("sunkOrder", selection)])[0] == expected


@pytest.mark.parametrize(
	("selection", "sinks"),
	[
		# The ordinary list and board: the request names `deferred,-created_at`, so the merge
		# sinks too.
		({}, True),
		({"order": "title"}, True),
		# **A search nobody has given an order to.** `sunkOrder` sends nothing, the server
		# applies plain `-created_at` without the band, and the merge must not add one.
		({"q": "passport"}, False),
		# ...but a reader who chose an order gets it sunk at both ends.
		({"q": "passport", "order": "title"}, True),
		# The finished view, which says `sinks: false` at both ends for its own reason.
		({"order": "-completed_at"}, False),
	],
)
def test_the_merge_sinks_exactly_when_the_request_asked_the_server_to (
	tmp_path: pathlib.Path, selection: dict[str, str], sinks: bool
) -> None:
	"""**`SR#882`, and it is the pair that had no test between them.**

	`sunkOrder` decides what the server is asked for and `mergeOrder` decides what the merged
	page is put in — and each had a passing test while **nothing compared them**. So a search
	with no explicit order sent no `order` at all, the server ranked or dated it without the
	deferral band, and the client sank rows anyway: re-sorting by a rule the server did not
	use, which is the disagreement keyset pagination exists to prevent (`SR#782`).

	`SR#640`'s shape for the seventh time, and it shipped in the change whose own docstring
	says *"the server has to do the sinking, not the page"*.

	**Both halves read here, from one selection**, so the assertion is about their agreement
	rather than about either one being right on its own.
	"""

	answers = _views(tmp_path, [
		("sunkOrder", selection),
		("mergeOrder", [selection, [{"ref": 1, "created_at": "2026-08-01T09:00:00+00:00"}]]),
	])

	asked, merging = answers

	assert bool(asked and asked.startswith("deferred,")) == sinks, (
		f"the request for {selection} asked for {asked!r}, which does not match the "
		f"expectation that sinking is {sinks}"
	)
	assert bool(merging and merging.get("sinks")) == sinks, (
		f"the merge for {selection} says sinks={merging and merging.get('sinks')} while the "
		f"request asked for {asked!r} — the client and the server would disagree"
	)


def test_a_start_date_that_has_passed_does_not_sink_a_row (tmp_path: pathlib.Path) -> None:
	"""`SR#877`. A defer that has come round is not a defer, and the merge must read it so.

	`ordering.put_off` says the same thing server-side and `deferred` marks the same rows, so
	a row sinking after its instant has passed would be the position and the mark disagreeing
	about one task — the confusion this was built to remove, arriving from the other side.
	"""

	behind = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)).isoformat()

	rows = [
		{"ref": 1, "created_at": "2026-08-01T09:00:00+00:00", "title": "Apple"},
		{"ref": 2, "created_at": "2026-08-02T09:00:00+00:00", "title": "Banana",
			"snoozed_until": behind},
	]

	assert _views(tmp_path, [("inOrder", {"rows": rows, "order": "-created_at"})])[0] == [2, 1]


def test_the_finished_order_is_not_offered_as_a_choice (tmp_path: pathlib.Path) -> None:
	"""Decision `SR#649`: an arrangement never selects.

	`-completed_at` is the *finished view's* order and is reached by the chip that also narrows
	to finished work. Offering it beside the rest would be an ordering that silently changes
	which rows there are — the exact thing `SR#738` took out of the view names.
	"""

	offered = _views(tmp_path, [("offeredOrders", {})])[0]

	assert offered, "no order is offered, so the control would be empty"
	assert "-completed_at" not in offered
	assert "-created_at" in offered and "-priority_score" in offered


def test_choosing_an_order_puts_it_in_the_address (tmp_path: pathlib.Path) -> None:
	"""Driven, because `SR#640` has cost six defects that a pure test could not see.

	Arriving at an address that names an order is the half a reader is *sent*; this asserts the
	page asks the instance for what its address says, rather than for the default it would have
	used anyway.
	"""

	rows = {"items": [{"ref": 7, "kind": "task", "title": "Something",
		"created_at": "2026-08-10T14:22:00+00:00", "status_category": "todo"}],
		"page": {"has_more": False, "next_cursor": None, "total": None}}
	driven = _driven(
		tmp_path, pathname="/projects", search="?view=list&order=title",
		answers={"/v1/tasks": rows},
	)
	tasks = [call for call in driven["asked"] if call["path"].startswith("/v1/tasks?")]
	documents = [call for call in driven["asked"] if call["path"].startswith("/v1/documents?")]

	assert tasks and documents, "the list asked for one collection or none"

	# **The address says `title` and the request says `deferred,title`** (`SR#877`): sinking is
	# a leading key the arrangement adds rather than something a reader chose, so the two are
	# deliberately different strings. What must survive is that the reader's key is still there
	# and still last, which is what decides the arrangement inside each band.
	wanted = urllib.parse.quote("deferred,title")

	assert all(f"order={wanted}" in call["path"] for call in tasks + documents), (
		f"the order in the address did not reach both collections: "
		f"{[call['path'] for call in tasks + documents]}"
	)
	assert "A to Z" in driven["said"], "the page does not say the order it was asked for"


def test_a_ranked_page_asks_for_no_documents_and_says_why (tmp_path: pathlib.Path) -> None:
	"""`SR#782` end to end, and the two halves fail differently.

	Asking `GET /v1/documents` for `-priority_score` is a **422 for the whole page**, so the
	request half is a page that does not load. Not saying so is quieter and worse: a reader who
	orders by priority and finds their specifications gone has been told nothing, and *`SR#503`
	the backlog is not a source of truth about the world* is the lesson about beliefs nobody
	corrects.
	"""

	rows = {"items": [{"ref": 7, "kind": "task", "title": "Something", "importance": 4,
		"urgency": 3, "created_at": "2026-08-10T14:22:00+00:00", "status_category": "todo"}],
		"page": {"has_more": False, "next_cursor": None, "total": None}}
	driven = _driven(
		tmp_path, pathname="/projects", search="?view=list&order=-priority_score",
		answers={"/v1/tasks": rows},
	)

	assert not [call for call in driven["asked"] if call["path"].startswith("/v1/documents?")], (
		"a ranked page asked for documents, which GET /v1/documents refuses outright"
	)
	assert "documents have no importance" in driven["said"], (
		f"the page dropped every document and said nothing about it: {driven['said'][:300]}"
	)
	assert "!4/3" in driven["said"], "a ranked row does not show what it is ranked by"


#: A workspace whose statuses are its own, which is the case a literal list gets wrong.
#: Three in one category, one renamed, and the default is not the first.
RENAMED = {
	"task": [
		{"key": "triage", "label": "Triage", "category": "todo", "is_default": False},
		{"key": "ready", "label": "Ready", "category": "todo", "is_default": True},
		{"key": "doing", "label": "Under way", "category": "in_progress", "is_default": True},
		{"key": "shipped", "label": "Shipped", "category": "done", "is_default": True},
	],
}


@pytest.mark.parametrize(
	("category", "expected"),
	[
		# Not the first in its category — the workspace said which is ordinary.
		("todo", "ready"),
		("in_progress", "doing"),
		("done", "shipped"),
		# Configured with nothing there, so a drop on it is declined rather than sent.
		("cancelled", None),
	],
)
def test_which_status_a_column_means (
	tmp_path: pathlib.Path, category: str, expected: str | None
) -> None:
	"""`SR#711`. A board's columns are categories and the API takes a status.

	The four categories are fixed by the model, which is what lets a board have columns at all
	(`SR#653`); a status is workspace vocabulary, renameable, and there may be several in one
	category — `open`, `blocked` and `needs_input` are all `todo` here. So a drop is a question
	with more than one answer.

	**Answered from `is_default`**, which is what a workspace has already said about *which of
	these is the ordinary one*. Choosing by key would be this app carrying its own vocabulary,
	wrong on the first instance that renames anything and wrong silently.
	"""

	answer = _views(tmp_path, [
		("statusFor", {"vocabulary": RENAMED, "kind": "task", "category": category}),
	])[0]

	assert answer["key"] == expected
	assert answer["because"] == (None if expected else "absent")


def test_a_drop_that_cannot_be_read_says_which_thing_it_looked_at (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#791`, from the review `SR#789`. Two absences reached the reader as one sentence.

	A category genuinely holding no status is a fact about the workspace. **A vocabulary that
	has not arrived is not** — `words` clears it before fetching and treats its own failure as
	survivable (§1.4), so null is a state this app reaches on a failed or in-flight request. The
	drop said *"There is no status here that means in progress"* for both, which is a refusal
	naming a cause it has not established: the rule the CLI already follows, broken here.

	**The remedy is offered only where there is one.** A reader can reload; a reader cannot add
	a status to their workspace from a board.
	"""

	unread, absent, fine = _views(tmp_path, [
		("statusFor", {"vocabulary": None, "kind": "task", "category": "in_progress"}),
		("statusFor", {"vocabulary": {"task": []}, "kind": "task", "category": "in_progress"}),
		("statusFor", {"vocabulary": RENAMED, "kind": "task", "category": "in_progress"}),
	])

	assert unread == {"key": None, "because": "unread"}
	assert absent == {"key": None, "because": "absent"}
	assert fine["key"] == "doing" and fine["because"] is None

	said, otherwise, nothing = _views(tmp_path, [
		("unmovable", {"because": "unread", "category": "in_progress"}),
		("unmovable", {"because": "absent", "category": "in_progress"}),
		("unmovable", {"because": None, "category": "in_progress"}),
	])

	assert "in progress" in said and "Reload" in said, said
	assert "not read" in said, "the unread sentence does not say what was not read"
	assert otherwise == "There is no status here that means in progress."
	assert "read" not in otherwise, "the workspace's own answer blames the page"
	assert nothing is None, "a status that was found needs no sentence"


@pytest.mark.parametrize(
	("day", "time", "expected"),
	[
		# A day alone stays a day, which is what keeps every ordinary deadline from carrying a
		# time somebody had to invent.
		("2026-08-17", "", "2026-08-17"),
		("2026-08-17", "14:00", "2026-08-17T14:00"),
		# A time with no day is nothing: there is no day for it to be on.
		("", "14:00", ""),
		("", "", ""),
	],
)
def test_a_day_and_a_time_become_one_field (
	tmp_path: pathlib.Path, day: str, time: str, expected: str
) -> None:
	"""`SR#798`, Simon driving `SR#755`: *"my appointment starts at 14:00 … but I cannot express
	that via the UI."*

	**The instance was never the limit.** `schedule.interpret` reads both shapes and sets
	`*_is_all_day` from which it was given — measured: `2026-08-17` becomes the last instant of
	that day with the flag true, `2026-08-17T14:00` becomes that minute with it false. So one
	field carries both meanings and the server decides which.

	**Two controls rather than `datetime-local`**, because that one forces a time on every
	deadline and a person writing *by Friday* would have to invent one.
	"""

	assert _views(tmp_path, [("withTime", {"day": day, "time": time})])[0] == expected


def test_a_time_control_starts_empty_unless_the_item_has_one (tmp_path: pathlib.Path) -> None:
	"""`SR#798`. `all_day` is the item's own answer and is a fact rather than an inference.

	Reading the clock and guessing would put `00:00` into the box for an ordinary deadline —
	which is stored at the *last* instant of its day, so in some zones the two are one instant
	— and saving would then write a time nobody chose. That is a display bug becoming data
	loss the moment a form is filled from the same value, which is the trap `fromItem` already
	names for the day half.
	"""

	timed, all_day, absent = _views(tmp_path, [
		("timeFor", {"value": "2026-08-17T13:00:00+00:00", "allDay": False,
			"zone": "Europe/London"}),
		("timeFor", {"value": "2026-08-17T22:59:59.999999+00:00", "allDay": True,
			"zone": "Europe/London"}),
		("timeFor", {"value": None, "allDay": False, "zone": "Europe/London"}),
	])

	assert timed == "14:00", "an appointment does not read back at the time it was written"
	assert all_day == "", "an all-day deadline put a clock in the box"
	assert absent == ""


def test_the_form_offers_a_time_where_a_time_means_something (tmp_path: pathlib.Path) -> None:
	"""`SR#798`, and `#854` widened it: all three dates are instants, so all three take a time.

	**This test used to assert the opposite** — that `starts` was a day and offering a clock
	there would be a promise the field could not keep. That was true of the column and Simon
	read the missing picker as an inconsistency; it was one, in the model rather than the form.
	Kept pointing the other way rather than deleted, because the guard underneath it is what
	matters: whatever the form draws and whatever `TIMED` says must be the same set.
	"""

	markup = _rendered(tmp_path, {"Fields": SAMPLES["Fields"]})["Fields"]

	# **Counted rather than named**, because `_rendered` is a text harness and carries `href`
	# and nothing else through — an attribute is not text. Two inputs where a time is offered,
	# one where it is not, which is the shape this harness can honestly see.
	drawn = set()

	for name, label, _hint in _date_fields():
		at = markup.index(label)

		if markup[at:markup.index("<small>", at)].count("<input>") == 2:
			drawn.add(name)

	assert drawn == {"starts", "snooze", "due"}, (
		f"the form offers a time on {sorted(drawn)} — every date a task carries is an instant "
		f"since `#854`, so each of the three should have a clock beside it"
	)

	# **The two halves compared, which is the invariant rather than the spelling.** `TIMED` is
	# what `filed` and `edited` combine; `drawn` is what the reader is given. A field in one and
	# not the other is a control nobody reads or a value nobody typed — and deriving `TIMED`
	# from `DATE_FIELDS` is what makes them agree, so this is the check that says so.
	assert set(_views(tmp_path, [("TIMED", {})])[0]) == drawn, (
		"the fields the form draws a time on and the fields the body sends one for differ"
	)


#: This instance's own link types, as `/v1/meta` publishes them — four with a direction and
#: one without, which is the case the control has to get right.
LINK_TYPES = {"link_types": [
	{"key": "blocks", "title": "Blocks", "inverse_title": "Blocked by", "is_symmetric": False},
	{"key": "duplicates", "title": "Duplicates", "inverse_title": "Duplicated by",
		"is_symmetric": False},
	{"key": "relates_to", "title": "Relates to", "inverse_title": "Relates to",
		"is_symmetric": True},
]}


def test_both_ends_of_a_directed_link_can_be_chosen (tmp_path: pathlib.Path) -> None:
	"""`SR#799`, Simon driving `SR#755`: *"I cannot select 'blocked by' in the list, only
	'blocked' — this type of link has a direction, I should be able to select both."*

	`/v1/meta` has published `inverse_title` and `is_symmetric` since M1 and the browser read
	neither, so it offered the near end of each. *Blocked by* is usually the one somebody
	opening an item means, because they are looking at the thing that is stuck.

	**`is_symmetric` is what stops `relates_to` appearing twice** saying one thing under one
	label — which is the whole reason the field is published rather than inferred from the two
	titles happening to match.
	"""

	choices = _views(tmp_path, [("linkChoices", {"vocabulary": LINK_TYPES})])[0]

	assert [one["label"] for one in choices] == [
		"Blocks", "Blocked by", "Duplicates", "Duplicated by", "Relates to"
	]
	assert [one["value"] for one in choices] == [
		"blocks", "-blocks", "duplicates", "-duplicates", "relates_to"
	]


def test_a_link_is_always_made_on_the_item_the_reader_has_open (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#799` said the instance learns no inverse; `SR#816` says who made it.

	*#42 blocked by #43* is *#43 blocks #42*, and the row stores it that way round because a
	row records a direction and there is only one of it. **What changed is who the request is
	addressed to.** This used to post to *their* links with the open item as the target —
	correct about the row, and wrong about the event, which names the item a link hangs off.
	So *what did I work on* listed an item the reader never opened.

	Simon's rule, settling `SR#815`'s question 3: **the action occurs on the item which is
	edited to add the link.** Both directions now go to the open item and `direction` says which
	way the link runs.

	**Both halves asserted**, because the path alone would pass against a client that sent no
	direction at all and let every inverse be stored the wrong way round.
	"""

	item = {"ref": 42, "kind": "task"}
	outward, inward = _built(tmp_path, [
		("linkRequest", [item, "43", "blocks", "task", "projects"]),
		("linkRequest", [item, "43", "-blocks", "task", "projects"]),
	])

	for named, built in (("outward", outward), ("inward", inward)):
		assert built["path"].startswith("/tasks/42/links"), (
			f"the {named} link was posted somewhere other than the item the reader has open, "
			f"so its event will name an item nobody was looking at"
		)

		assert built["body"]["target"] == 43 and built["body"]["link_type"] == "blocks", (
			f"the {named} link swapped the ends or invented an inverse type for the instance"
		)

	assert outward["body"]["direction"] == "outgoing"
	assert inward["body"]["direction"] == "incoming", (
		"the inverse is indistinguishable from the ordinary link, so the instance would store "
		"it the wrong way round"
	)


def test_the_top_of_the_page_offers_the_same_things_whatever_is_below_it (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#786`, Simon comparing two addresses on the live instance.

	The header gated its search and its view chips on `!open`, and `.top` is
	`justify-content: space-between` — so opening an item took two of four children away and
	the box pushed the workspace switcher hard right. **Nothing moved; two things vanished.**

	An item's address is a page somebody is *sent* (`SR#638`), so arriving there and finding no
	way to search is `SR#651`'s fault again: an address is not a way to find something.

	Compared as sets rather than asserted one by one, so a control added to the listing's header
	and not to the item's fails this without anybody remembering to extend it.
	"""

	empty = {"items": [], "page": {"has_more": False, "next_cursor": None, "total": None}}
	listing = _driven(tmp_path, pathname="/projects", search="?view=list")
	item = _driven(
		tmp_path, pathname="/projects/42", search="?view=list",
		answers={
			"/v1/tasks/42/links": empty,
			"/v1/tasks/42/comments": empty,
			"/v1/tasks/42": {"ref": 42, "kind": "task", "title": "Open",
				"status_category": "todo"},
		},
	)

	views = {address for address in listing["links"] if "view=" in address}

	assert views, "the listing offers no view chips, so this is checking nothing"

	missing = views - set(item["links"])

	assert not missing, (
		f"the header loses {sorted(missing)} the moment an item is open, and the rest of it "
		f"redistributes to fill the gap"
	)

	assert "Search" in item["said"] or "search" in item["said"].lower(), (
		f"an item page offers no search: {item['said'][:300]}"
	)


def test_a_control_that_writes_a_listing_address_leaves_the_open_item () -> None:
	"""`SR#786`'s other half, and it became reachable only because of the fix above.

	Both controls call `go(listingAddress(…))`. While they were hidden over an open item that
	was harmless; visible, it is an address saying the listing while the page shows the item —
	which is exactly what `close` was fixed for, arriving from a third door.

	**This checks the call, not the outcome**, and says so: choosing a view needs a click, and
	`tests/dom.js` cannot dispatch one by decision. `SR#748` is the item for a machine that
	could. What it buys is that removing the call fails a test rather than nothing.
	"""

	source = _without_prose(_served_modules()["app.js"])

	for name in ("chooseSearch", "chooseView"):
		opens, closes = _braced(source, f"const {name} = useCallback(")
		inside = source[opens:closes]

		assert "listingAddress(" in inside, f"{name} no longer writes a listing address"
		assert "nowOpen(null)" in inside, (
			f"{name} writes a listing address and leaves the item open, so the bar and the "
			f"page would disagree about what the reader is looking at"
		)


def test_a_background_read_stands_off_and_does_not_lose_what_it_skipped () -> None:
	"""`SR#657` and `SR#792`, which are two halves of one rule.

	**Standing off is correctness rather than courtesy.** The edit form carries
	`expected_version` (§8.9), so replacing the open item underneath it would replace the
	version too — and a save that should have been refused with *somebody changed this while
	you were typing* would go through and overwrite them.

	**And the poll advances its cursor before it asks**, so an event skipped that way is
	*consumed*. Saving hides it, because `wrote` re-reads; cancelling did not, and the pane went
	back to showing an item that had moved with nothing saying so — the state `SR#657` exists to
	remove, surviving in the one path it did not cover. So the skip leaves a mark and something
	reads it.

	**This checks the spelling and cannot check the thing**, said out loud because this
	repository has been caught believing otherwise. Opening the editor needs a click and
	`tests/dom.js` cannot dispatch one by decision; `tests/test_browser.py` could, and its ten
	slots are better spent on layout and `axe-core` than on this. `SR#748` is the item. What
	this does buy is that removing either half fails a test rather than nothing at all.
	"""

	source = _without_prose(_served_modules()["app.js"])
	opens, closes = _braced(source, "const refresh = useCallback(")
	inside = source[opens:closes]

	assert "held.current" in inside, "this is not the background read; the scan found the wrong body"

	assert re.search(r"if\s*\(editing\)\s*\{[^}]*missed\.current\s*=\s*true", inside), (
		"the background read no longer stands off while the item is being edited, so a save "
		f"carrying expected_version could overwrite somebody: {inside!r}"
	)

	# **And the other half**: a mark nothing reads is a skip by another name.
	readers = [
		at.start() for at in re.finditer(r"missed\.current", source)
		if not opens <= at.start() < closes
	]

	assert readers, (
		"nothing outside refresh looks at the mark it leaves, so a change made while the form "
		"was open is still consumed and never re-read"
	)


def test_the_shim_is_a_mount_and_not_a_browser () -> None:
	"""`SR#640`'s scope, held by a test rather than by an intention.

	**A harness that substitutes the mechanism under test can only ever confirm the half that
	was not broken** — this repository's own words, about the version of this file that supplied
	`htm` when the served page did not. `dom.js` is a substitute, and it is defensible only
	while it does one narrow thing.

	So the line is drawn where it was decided: the first test that wants to *click* needs a real
	DOM, and this fails rather than letting the file grow into a worse one. There is no npm on
	the development machine today, which is the whole reason a shim was the answer.
	"""

	#: **Comments stripped first**, because the first version of this failed on the word
	#: *click* inside the sentence forbidding it. `SR#546`'s shape at its smallest: a scan that
	#: reads prose is measuring the explanation rather than the thing.
	code = re.sub(r"/\*.*?\*/", "", DOM.read_text(encoding="utf-8"), flags=re.S)
	code = "\n".join(
		line for line in code.splitlines() if line.strip() and not line.strip().startswith("//")
	)

	assert len(code.splitlines()) < 120, (
		f"dom.js is {len(code.splitlines())} lines of code. It is meant to mount the app and "
		f"nothing else; past this it is a bad browser rather than a small harness — jsdom."
	)

	for forbidden, why in (
		("dispatchEvent", "dispatching an event is where a shim stops being honest"),
		(".click", "clicking needs a real DOM, not a larger pretence"),
		("querySelector", "finding nodes by selector is a browser's job, not a mount's"),
		("innerHTML", "parsing HTML is the one thing this must never pretend to do"),
	):
		assert forbidden not in code, f"dom.js implements {forbidden!r}: {why}"


def test_an_image_is_shown_as_it_was_written_rather_than_rendered_as_a_link (
	tmp_path: pathlib.Path,
) -> None:
	"""`#833`. The pattern matches `!?[…](…)` and both branches used to render a link.

	So `![alt](url)` came out as an **anchor labelled with the alt text** — the reader saw a
	link where an author had written an image, and clicking it went wherever the image had
	been. On the refusal path it was worse in a quieter way: `linked` rebuilds `[text](dest)`
	from its parts, so the `!` the author typed was dropped from the escaped fallback too.

	**Images are deliberately unsupported and that is not what this is about.** The module
	measured *images 0* across 291 documents. What it promises for anything it will not render
	is *show what was written*, and a link is not that — so the two halves are asserted
	together here: no anchor, and the original text intact.
	"""

	written = "![a diagram](https://example.com/d.png)"
	rendered = _markdown(tmp_path, [written])[0]

	assert "<a " not in rendered, f"an image became a link: {rendered}"
	assert "href" not in rendered, f"an image kept a destination: {rendered}"

	# The `!` is the half that vanished on the refusal path, so it is asserted by name rather
	# than left to a substring check that would pass without it.
	assert "!" in rendered, f"the exclamation mark was dropped: {rendered}"
	assert "a diagram" in rendered
	assert "https://example.com/d.png" in rendered


def test_an_ordinary_link_still_renders (tmp_path: pathlib.Path) -> None:
	"""The control for the case above, and it is not redundant.

	`#833`'s fix branches on whether the match began with `!`, so the obvious way to get it
	wrong is to send every `[…](…)` down the escaped path — which would close the finding and
	silently stop rendering links at all. Nothing else in this file would notice, because the
	payload tests all assert that something is *absent*.
	"""

	rendered = _markdown(tmp_path, ["[the docs](https://example.com/docs)"])[0]

	assert '<a href="https://example.com/docs"' in rendered, rendered
	assert "the docs" in rendered


def test_the_masthead_says_which_instance_served_the_page (tmp_path: pathlib.Path) -> None:
	"""`#784`, moved to where it is read from by `#1536`.

	The argument is about who reads this page. It has one reader per instance and they are on
	another machine, so every defect arrives as prose — *the dropdown is top-right*, *the
	column populates after ten seconds* — and whether the page being described is the code in
	this tree was unknowable to both ends. I push, they pull, they restart the service, and
	nothing on screen said which of those had happened.

	`#380`/`#393` are the same shape one layer up: a cached plugin predating the feature it was
	installed for, reporting success and changing nothing. A page has every one of those
	properties.

	**It was in the footer until `#1536` and the reason it moved is the same reason it exists.**
	A trial user asked which version they are on should not have to scroll to answer, and the
	answer must be quotable in the message they are already writing.
	"""

	rendered = _rendered(tmp_path, {"Wordmark": SAMPLES["Wordmark"]})["Wordmark"]

	assert "0.6.7" in rendered, "the page does not say which instance answered it"

	# **The name too, because a version alone is not a masthead.** A mutation that dropped the
	# wordmark and kept the build would satisfy the line above.
	assert "Subroutine" in rendered


def test_the_masthead_drops_the_commit_from_the_version_and_not_the_release (
	tmp_path: pathlib.Path,
) -> None:
	"""`#1536`. Simon asked for `0.8.3.dev33`, not `0.8.3.dev33+g7fad4af9d`.

	**Both halves, because either alone passes on a version that says nothing.** Truncating to
	the first three components would satisfy *the sha is gone* while destroying `dev36`, which
	is the part that says how far past the tag this is — and returning the string untouched
	satisfies *the release survives*.

	**A tagged release is asserted to be unchanged rather than special-cased**, which is the
	whole reason this rule is `split("+")` and not a pattern: a tag has no local segment, so
	one rule produces both of the forms asked for and neither is a branch anybody has to
	remember.
	"""

	built = _rendered(tmp_path, {"Wordmark": {"version": "0.8.3.dev36+g7fad4af9d"}})["Wordmark"]

	assert "0.8.3.dev36" in built
	assert "g7fad4af9d" not in built, "the commit is on the title, not on the page"

	tagged = _rendered(tmp_path, {"Wordmark": {"version": "0.8.2"}})["Wordmark"]

	assert "0.8.2" in tagged


def test_the_masthead_says_nothing_about_a_version_it_has_not_been_given (
	tmp_path: pathlib.Path,
) -> None:
	"""An instance that has not answered yet is not an instance with no version.

	``/v1/me`` is the first request this app makes and the masthead renders before it lands, so
	the honest reading of a missing value is *not known yet*. Printing an empty pair of
	parentheses, or the word ``undefined``, would be this project's own defect of reporting the
	absence of a fact as though it were one.

	**Counted rather than read, because both readable versions of this cannot fail.** Preact
	renders ``null`` as nothing, so dropping the guard leaves an empty ``<span>`` and
	*"undefined is not in the output"* is true either way. Asserting on the element's ``title``
	fails too — this harness emits an element's children and its ``href`` and drops the rest,
	so a check on any other attribute is a check on something it never writes. Both of those
	passed against the mutation before this one was counted.
	"""

	silent = _rendered(tmp_path, {"Wordmark": {"version": None}})["Wordmark"]
	told = _rendered(tmp_path, {"Wordmark": {"version": "9.9.9"}})["Wordmark"]

	assert "9.9.9" in told, "the version is not being rendered at all"

	# **Differential rather than an absolute count**, so this keeps meaning what it means
	# whatever else the masthead grows. What it says is *a version adds an element and no
	# version adds none*.
	assert told.count("<span") == silent.count("<span") + 1, (
		f"an empty element is still a claim that there is a version: {silent}"
	)


def test_the_footer_no_longer_carries_the_version (tmp_path: pathlib.Path) -> None:
	"""`#1536` moved it to the masthead, and one fact belongs in one place.

	**The count is asserted beside it**, because *no version in the footer* is equally true of
	a footer that failed to render at all — which is this project's recorded shape for a
	negative assertion that proves nothing.
	"""

	rendered = _rendered(tmp_path, {"Foot": SAMPLES["Foot"]})["Foot"]

	assert "7 items" in rendered
	assert "0.6.7" not in rendered, (
		"the version is in the masthead and the footer, so two places can disagree about it"
	)


def test_a_fact_sheet_shows_the_time_an_item_starts (tmp_path: pathlib.Path) -> None:
	"""`#864`, found by Simon driving the browser with no terminal.

	He captured *Dentist on Monday at 14:00*, which `#797` taught the grammar to read; the item
	stored `snoozed_until` with `snoozed_is_all_day: false`; and the item page said **Starts 17 Aug
	2026**. A field a person can write and cannot read back is `#515`'s shape, and `#797`'s own
	finding was that a time somebody typed must be *reported* rather than guessed.

	**Both halves, because either alone passes for the wrong reason.** Showing the time proves
	nothing if an all-day deadline grows a spurious `00:00` — which is exactly what reading the
	clock instead of the flag would do, and is why `timeFor` refuses to infer.
	"""

	timed, all_day = (
		_rendered(tmp_path, {"Facts": {"item": {
			"ref": 18, "title": "Dentist", "timezone": "Europe/London",
			"snoozed_until": "2026-08-17T13:00:00Z", "snoozed_is_all_day": flag,
		}}})["Facts"]
		for flag in (False, True)
	)

	assert "14:00" in timed, (
		f"an item that starts at an o'clock shows only its day: {timed}"
	)
	assert "17" in timed, f"the day went missing along with the fix: {timed}"

	assert "14:00" not in all_day and "00:00" not in all_day, (
		f"an all-day start was given a time nobody chose: {all_day}"
	)


def test_a_row_marks_both_ends_of_a_dependency (tmp_path: pathlib.Path) -> None:
	"""`#861`, which is `#569` reaching the surface it was reported from.

	`#569` began with an agent reading a **board**: the urgent item carried *Blocked*, and the
	five-minute errand actually holding it up carried nothing — so the only thing worth
	starting looked like the least important row on the page. The terminal row and the agent's
	row were both given the mark and this one was missed, which nothing could see: the field
	guard asks whether every field a row *renders* is requested, and a field the row never
	renders is invisible to it in the direction that matters.

	**A row that is both shows both.** The terminal has one cell and had to choose, keeping
	`blocked` because that is the fact deciding whether you can act; a card has room, and a row
	mid-chain genuinely is both ends of two different links.
	"""

	shown = {
		which: _rendered(tmp_path, {"Row": {"workspace": "projects", "item": {
			"ref": 7, "kind": "task", "title": "Renew the certificate",
			"blocked": which in ("blocked", "both"),
			"blocking": which in ("blocking", "both"),
		}}})["Row"]
		for which in ("neither", "blocked", "blocking", "both")
	}

	assert "Blocker" in shown["blocking"], (
		f"a task holding up unfinished work is unmarked: {shown['blocking']}"
	)
	assert "Blocker" in shown["both"] and "Blocked" in shown["both"], (
		f"a row in the middle of a chain shows only one end: {shown['both']}"
	)

	# The negatives, because a mark that is always there says nothing — and this is the pair
	# `#569` is about, so each has to be absent when it is untrue rather than merely present
	# when it is.
	assert "Blocker" not in shown["blocked"], (
		f"a blocked task is marked as holding something up: {shown['blocked']}"
	)
	assert "Blocker" not in shown["neither"] and "Blocked" not in shown["neither"], (
		f"an ordinary task carries a dependency mark: {shown['neither']}"
	)


def test_a_search_narrows_the_documents_it_asks_for (tmp_path: pathlib.Path) -> None:
	"""**`SR#872`, found by Simon driving the served instance and unmissable once seen.**

	Searching a nonsense string returned rows. The endpoints were both innocent — measured,
	`GET /v1/tasks?q=` and `GET /v1/documents?q=` each answered `[]` — because the browser sent
	`q` to the tasks request and **nothing at all** to the documents one, so a search filtered
	half the list and returned every document there was.

	The tasks-only rule was correct when written, for `status_category` and `include_completed`,
	which `GET /v1/documents` refuses. `q` arrived later (`SR#775`) and inherited it in silence,
	because the rule was implicit — spread across two functions and stated in neither.
	"""

	asked = _built(tmp_path, [("listingRequests", ["personal", None, None, {"q": "quinsy"}])])
	documents = [request for request in asked if "/documents" in request["path"]]

	assert documents, (
		"a search reads documents too — SPEC 6.2 gives both kinds one ref counter, so a search "
		"that skipped documents would be lying about half the numbers"
	)
	assert all("q=quinsy" in request["path"] for request in documents), (
		"every collection a search reads has to be told what the search is, or it returns "
		"everything it has and the reader cannot tell that from a genuine match"
	)


def test_every_selection_parameter_says_which_collections_answer_it (
	tmp_path: pathlib.Path,
) -> None:
	"""The guard that would have caught `SR#872` when `q` was added, rather than a month later.

	`ANSWERED_BY` is the one statement of where a selection parameter goes. A new entry in
	`SELECTABLE` that is not in it would otherwise inherit whatever the request builder happened
	to do — which is exactly how `q` came to be withheld from documents by an omission nobody
	made on purpose.

	Derived from `SELECTABLE` rather than listed, so this cannot fall behind the thing it
	guards.
	"""

	source = _without_comments(_served_modules()["app.js"])

	def declared (name: str) -> set[str]:
		"""Return the keys of a module-level object literal, by name."""

		start = source.index(f"export const {name} = {{")
		body = source[start : source.index("\n};", start)]

		return set(re.findall(r"^\t(\w+):", body, re.M))

	selectable = declared("SELECTABLE")
	answered = declared("ANSWERED_BY")

	assert selectable, "nothing was scanned, so this is asserting about an empty set"

	assert selectable <= answered, (
		f"{sorted(selectable - answered)} can be put in an address and no rule says which "
		f"collections can answer them. That is how SR#872 happened: the request builder did "
		f"something reasonable, and it was wrong for the new parameter."
	)


def test_a_chosen_ordering_survives_the_merge (tmp_path: pathlib.Path) -> None:
	"""**`SR#876`.** `accumulated` merged on `created_at` whatever the reader had chosen.

	`inOrder` was written for this in `SR#782` and was reachable only through `newestFirst`,
	which passes the default — so the general case it exists for had never been reached, while
	a guard elsewhere asserted both collections fetch the ordering's field *"because `field` is
	what `inOrder` merges on"*. The data was fetched for a merge that did not use it.

	**Creation order disagrees with alphabetical order**, which is the only arrangement that can
	tell the two apart. A fixture where they agree passes either way.
	"""

	rows = [
		{"ref": 1, "kind": "task", "title": "Zebra", "created_at": "2026-08-01T00:00:00+00:00"},
		{"ref": 2, "kind": "document", "title": "Apple", "created_at": "2026-08-02T00:00:00+00:00"},
	]

	merged = _views(tmp_path, [(
		"accumulated",
		[[], rows, {"appending": False, "collections": 2, "ordering": None}],
	)])[0]

	assert [row["ref"] for row in merged] == [2, 1], "the default is newest first"

	alphabetical = _views(tmp_path, [(
		"accumulated",
		[[], rows, {"appending": False, "collections": 2, "ordering": {
			"field": "title", "compare": "text", "descending": False,
		}}],
	)])[0]

	assert [row["ref"] for row in alphabetical] == [2, 1], "Apple before Zebra"

	descending = _views(tmp_path, [(
		"accumulated",
		[[], rows, {"appending": False, "collections": 2, "ordering": {
			"field": "title", "compare": "text", "descending": True,
		}}],
	)])[0]

	assert [row["ref"] for row in descending] == [1, 2], "Zebra before Apple"


def test_a_ranked_search_is_merged_by_its_ranking (tmp_path: pathlib.Path) -> None:
	"""**`SR#875`, and Simon's requirement: one order across all four surfaces.**

	The server defaults a search to `-relevance` where a backend can score one, and says so by
	populating the field. `mergeOrder` reads that rather than re-deriving the rule, so the
	browser puts a merged search into the order the API, the CLI and MCP already return.

	**The best match is the oldest row here**, deliberately: under the old merge it would have
	sorted last, which is exactly what happened to a bare ref search — the item somebody typed
	the number of is usually older than everything discussing it.
	"""

	rows = [
		{"ref": 1, "kind": "task", "relevance": 1000.1,
		 "created_at": "2026-08-01T00:00:00+00:00"},
		{"ref": 2, "kind": "document", "relevance": 0.07,
		 "created_at": "2026-08-05T00:00:00+00:00"},
	]

	chosen = _views(tmp_path, [("mergeOrder", [None, rows])])[0]

	assert chosen["field"] == "relevance", chosen
	assert chosen["descending"] is True

	merged = _views(tmp_path, [(
		"accumulated",
		[[], rows, {"appending": False, "collections": 2, "ordering": chosen}],
	)])[0]

	assert [row["ref"] for row in merged] == [1, 2], (
		"the exact match came back below something written later, which is the defect"
	)


def test_an_unranked_listing_is_not_merged_by_relevance (tmp_path: pathlib.Path) -> None:
	"""The other half: nothing changes for a listing the server did not rank.

	`relevance` is null on every listing that is not a ranked search, so reading the data has
	to mean *this was ranked* rather than *this has the field*. Without this the fix would put
	every ordinary list into an order decided by a column of nulls.
	"""

	rows = [
		{"ref": 1, "kind": "task", "relevance": None,
		 "created_at": "2026-08-01T00:00:00+00:00"},
		{"ref": 2, "kind": "document", "relevance": None,
		 "created_at": "2026-08-05T00:00:00+00:00"},
	]

	chosen = _views(tmp_path, [("mergeOrder", [None, rows])])[0]

	assert chosen["field"] == "created_at", chosen


def test_a_deferred_item_says_it_has_been_put_off (tmp_path: pathlib.Path) -> None:
	"""**`SR#862`.** The board showed parked work looking exactly like work nobody had parked.

	`subroutine list` hides deferred items, so the two surfaces disagreed about items somebody
	had deliberately set aside — and the board's reader had no way to tell, which is `SR#12.2c`'s
	rule that a null reads as *not set* rather than as *not asked for*.

	Simon's decision of 2026-08-14: **marked rather than hidden** — *"that way they are not
	invisible, but neither are they confused with non-deferred items"*.
	"""

	shown = {
		which: _rendered(tmp_path, {"Row": {"workspace": "projects", "item": {
			"ref": 7, "kind": "task", "title": "Renew the certificate",
			"snoozed_until": "2099-01-01T09:00:00+00:00" if which == "parked" else None,
			"snoozed_is_all_day": False, "timezone": "UTC",
		}}})["Row"]
		for which in ("parked", "ordinary")
	}

	assert "Deferred" in shown["parked"], (
		f"work somebody put aside looks exactly like work nobody did: {shown['parked']}"
	)
	assert "2099" in shown["parked"], (
		f"the mark does not say when it comes back, which is the only thing a reader can act "
		f"on: {shown['parked']}"
	)
	assert "Deferred" not in shown["ordinary"], (
		f"an item nobody put off is marked as deferred: {shown['ordinary']}"
	)


def test_a_defer_that_has_come_round_is_not_a_mark (tmp_path: pathlib.Path) -> None:
	"""The clock is the whole rule, and a fixture in the past is what proves it is read.

	`readiness.undeferred` treats an instant that has passed as not deferred, and this has to
	agree or the browser would mark work as parked for ever after it came back — the mark would
	then say something false about every item that had ever been deferred.

	**Computed rather than published for exactly this reason**: a boolean fixed when the page
	was fetched goes on saying *deferred* on a page left open past the moment.
	"""

	passed = {"ref": 1, "kind": "task", "title": "Came back", "snoozed_until": "2020-01-01T00:00:00+00:00"}

	assert _views(tmp_path, [("deferred", passed)])[0] is False


#: Controls addressed by class rather than by element, so a scan over selectors cannot find
#: them. **Each is a real control** — a row's action, a listing's view switcher, a banner's way
#: out — and each has a written reason, so the list is a decision rather than a leftover.
CONTROLS_BY_CLASS = {
	".finish": "a row's Complete, which is a button and says so nowhere in its selector",
	".views a": "the view switcher, anchors since `#722` so a middle-click opens a tab",
	".narrowed a.widen": "*show everything*, an anchor for the same reason",
	# **The six roles** (design `#1045`, `#1046`). A button's size lives on its role now rather
	# than on wherever it happened to be written, so these carry every padding this scan
	# existed to count — and the floor below fell to 14 the moment they did.
	".primary": "commits what a form exists to make",
	".action": "changes an item",
	".quiet": "changes nothing — abandons, dismisses, goes back",
	".reveal": "shows or hides more of what is already here",
	".segment": "one control of a segmented toggle",
}


#: The six roles a button may wear — design `#1045`, Simon 2026-08-20. A role says what pressing
#: it does; `#763`'s three sizes say where it is, and the two are orthogonal.
ROLES = ("primary", "action", "quiet", "reveal", "segment", "inline")


def test_every_button_wears_exactly_one_role () -> None:
	"""`#1046`. **Nine treatments had grown and no two of them agreed about anything.**

	*More*, *Cancel* and *Search* were one look doing three unrelated things — reveal, abandon,
	submit — while *Save*, *Complete* and *Remove* were three looks all committing a change.
	**Complete and Show more were byte-identical**, one changing an item and one loading rows.
	Every one was chosen where it was written, because nothing said what a look *meant*.

	**Read off the source rather than listed**, so a button written tomorrow is a case tomorrow
	— which is the whole difference between this and the convention it replaces.

	**Two rather than one**: a button with no role falls back to the browser's own look, and a
	button with two is a rule nobody can read off the page.
	"""

	# **`_without_comments`, never `_without_prose`.** The second blanks the text of a template
	# literal — and in this app the markup *is* that text, so the scan would read nothing and
	# report a clean page. `#776` met the same thing and this is the second instance.
	found = re.findall(r"<button\b[^>]*>", _without_comments(_served_modules()["app.js"]), re.S)

	assert len(found) >= 15, (
		f"only {len(found)} buttons were found, so this scan is broken rather than the page"
	)

	bare, several = [], []

	for one in found:
		# **Read off the whole tag, not off a quoted `class="…"`.** One button's class is a
		# template literal — `class=${`inline${…}`}` — because whether a link end is struck
		# through is decided there, and a scan that only understood the quoted form reported it
		# as wearing no role at all. Word boundaries, so `action` in an `aria-label` would be a
		# false positive and nothing here has one.
		wearing = {name for name in ROLES if re.search(rf"\b{name}\b", one)}

		if not wearing:
			bare.append(one[:70])

		elif len(wearing) > 1:
			several.append(f"{one[:50]} — {sorted(wearing)}")

	assert not bare, (
		f"{len(bare)} buttons wear no role, so each lands on the user agent's own look and on "
		"whichever rule it was written next to:\n  " + "\n  ".join(bare)
	)
	assert not several, (
		"a button cannot both, and a reader has to be able to tell what pressing it does from "
		"how it looks:\n  " + "\n  ".join(several)
	)


def test_only_the_primary_role_spends_the_accent_fill () -> None:
	"""`#1046`, rule 3: the accent is for committing and nothing else wears it.

	*Preview* wore *Save*'s fill while writing nothing — three inches above a control that
	writes. **The fill is the loudest thing this page has**, so what it means has to be one
	thing.

	Asserted on the *stylesheet* rather than in a browser because it is a claim about which
	selector carries `--accent` as a background, which is text. The browser's job is the
	computed colour of what is drawn, and `tests/test_browser.py` asks that.
	"""

	text = _rules_only((ASSETS / "app.css").read_text(encoding="utf-8"))
	filled = [
		" ".join(selector.split())
		for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text)
		if re.search(r"background:\s*var\(--accent\)", body)
	]

	assert filled, "nothing is filled with the accent, so this is checking nothing"

	stray = [one for one in filled if not one.startswith(".primary")]

	assert not stray, (
		f"{stray} fill themselves with the accent. It is spent on committing — one per form — "
		f"and a control that writes nothing wearing it is what `#1045` is about."
	)


def test_a_reveal_says_so_in_a_glyph_as_well_as_in_a_word () -> None:
	"""`#1046`, rule 4, and Simon's *'a button which reveals more content should indicate that'*.

	`aria-expanded` was on both controls before this and **nothing drew it**, so *More* looked
	exactly like *Cancel* and *Search*. The state was already declared; what was missing was
	any way to see it.

	**Both halves**, because either alone is the defect: a caret with no `aria-expanded` says
	nothing to a reader who cannot see it, and `aria-expanded` with no caret is what there was.
	"""

	source = _without_comments(_served_modules()["app.js"])
	reveals = re.findall(r"<button\b[^>]*\bclass=\"[^\"]*\breveal\b[^\"]*\"[^>]*>", source, re.S)

	assert len(reveals) >= 2, f"only {len(reveals)} reveals found, so this scan reads nothing"

	silent = [one[:70] for one in reveals if "aria-expanded" not in one]

	assert not silent, f"a reveal that says nothing to a screen reader: {silent}"

	assert '.reveal[aria-expanded="true"] .icon' in (ASSETS / "app.css").read_text(
		encoding="utf-8"
	), "nothing turns the caret, so the state is declared and drawn by nothing"


def test_a_control_is_one_of_three_sizes () -> None:
	"""**`#763`. Not *are the controls styled* — they were, 44 times, differently.**

	Measured before deciding: 13 distinct paddings across 15 rules, while `border-radius` was
	**one** value across fourteen. The difference is that `--radius` existed and nothing named a
	control's size, which is `#906`'s argument one level up — the thing with a token works, the
	thing without one accretes.

	**Asserts the vocabulary is used, not merely that it exists.** Three tokens nobody applied
	would pass a check that only read `:root`, and the page would look exactly as it did.

	**A link is not a control and is excluded by what it declares rather than by name**:
	`.linked a` is `padding: 0` with no border and no background, which is a deliberate decision
	that the other end of a link reads as text. Anything that draws itself a box is in scope.
	"""

	text = (ASSETS / "app.css").read_text(encoding="utf-8")
	rules = re.findall(r"([^{}]+)\{([^{}]*)\}", _rules_only(text))

	sizes = {"var(--control-field)", "var(--control-button)", "var(--control-tight)"}
	element = re.compile(r"\b(?:button|input|select|textarea)\b")

	wrong, found = [], 0

	for selector, body in rules:
		name = " ".join(selector.split())
		padding = re.search(r"(?<![a-z-])padding:\s*([^;]+)", body)

		if padding is None:
			continue

		by_class = any(one in name for one in CONTROLS_BY_CLASS)

		if not element.search(name) and not by_class:
			continue

		# A control that draws no box is text wearing a tag: `padding: 0`, no border, no fill.
		if padding.group(1).strip() == "0" and "border: 0" in body:
			continue

		# **Counted per selector rather than per rule** (`#1046`). The six roles share one
		# padding declaration — which is the whole point of them — so counting rules made the
		# floor fall as the page got *more* consistent, which is the opposite of what a floor
		# is for. Each comma-separated part is a control that has a size.
		found += len([one for one in name.split(",") if one.strip()])

		if padding.group(1).strip() not in sizes:
			wrong.append(f"{name[:44]} — padding: {padding.group(1).strip()}")

	assert found >= 15, f"only {found} controls found, so the scan is broken rather than the page"

	assert not wrong, (
		f"{len(wrong)} controls are a size of their own. A control is a field, a button or "
		f"tight (`#763`); a fourth is a decision:\n  " + "\n  ".join(sorted(wrong))
	)


def test_every_class_named_as_a_control_is_still_in_the_stylesheet () -> None:
	"""An entry that no longer matches anything is an excuse nobody can delete (`#405`)."""

	text = _rules_only((ASSETS / "app.css").read_text(encoding="utf-8"))

	gone = [one for one in CONTROLS_BY_CLASS if one not in text]

	assert not gone, f"{gone} are named as controls and are in no rule — delete the entries"


def test_a_control_focuses_the_same_way_wherever_it_is () -> None:
	"""**`#763`. The primary button on the page focused differently from everything else.**

	Every control drew `2px solid var(--accent)` except `.adding button`, which drew
	`var(--ink)` — nothing decided that, it is what happens when a value is written out nine
	times. A focus ring is the one piece of styling a keyboard reader depends on, so *the
	odd one out* is the worst thing for it to be.
	"""

	text = _rules_only((ASSETS / "app.css").read_text(encoding="utf-8"))

	rings = set(re.findall(r"(?<![a-z-])outline:\s*([^;]+)", text))

	assert rings, "no outline is declared at all, so this is checking nothing"

	assert rings == {"var(--focus-ring)"}, (
		f"a control focuses differently from the rest: {sorted(rings)}. One ring, named once "
		f"(`#763`) — a keyboard reader should not have to learn a second one"
	)


def test_reduced_motion_has_motion_to_reduce () -> None:
	"""**`#763`, and the failure it guards is an inert control, not a missing one.**

	`#441` called `prefers-reduced-motion` not optional and this item is where it landed, for a
	measured reason: the stylesheet contained **zero** occurrences of `transition`, `animation`,
	`@keyframes` or `scroll-behavior`, so declaring the query then would have been a rule
	governing nothing — the declared-and-does-nothing family for the ninth time after `#247`,
	`#251`, `#303` and `#523`.

	So the assertion is in both directions: **the query exists, and there is something for it to
	suppress.** Either alone passes while the pair is useless.

	**What this cannot see, stated rather than left to be assumed**: whether the query actually
	*wins* in a browser. That is a cascade question and needs one, and `tests/test_browser.py`
	is at its agreed size — the durations carry `!important` precisely so the answer does not
	depend on rule order, which is the closest thing to a proof available from here.
	"""

	rules = _rules_only((ASSETS / "app.css").read_text(encoding="utf-8"))

	moving = re.findall(r"(?<![a-z-])transition:\s*([^;]+)", rules)

	assert moving, (
		"nothing in the stylesheet moves, so the reduced-motion query below governs nothing — "
		"which is a rule that reads as care and is worth none"
	)

	reduced = re.search(
		r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", rules, re.DOTALL
	)

	assert reduced is not None, "`#441` calls this query not optional and there is none"

	for property in ("transition-duration", "animation-duration"):
		assert f"{property}: 0.01ms !important" in reduced.group(1), (
			f"the query does not zero {property}, so motion survives a reader asking for none"
		)


def test_a_type_this_client_has_never_seen_still_gets_a_chip (tmp_path: pathlib.Path) -> None:
	"""**`#764`, and `#906` says to build this half first because it is the untested one.**

	Item types are workspace data (§5.5) and this client's glyph map is one opinion about a
	vocabulary the server owns. `#826` measured that no surface can add or rename a type
	*today*, so mapping the seeded keys is correct now — and the moment `#826` goes the other
	way it stops being correct **silently**: a new type would render as whatever the fallback
	is and nobody would find out from a failure. So the unrecognised path is the one that has
	to be right, and it is the one nothing would exercise on its own.

	**The word is what carries the meaning either way** (`#102`): a chip a reader can read,
	whatever picture sits beside it.
	"""

	unknown = {
		"ref": 9, "kind": "task", "title": "Something new", "type": "escalation",
		"status_is_default": True,
	}

	shown = _rendered(tmp_path, {"Row": dict(SAMPLES["Row"], item=unknown)})["Row"]

	known = _rendered(
		tmp_path, {"Row": dict(SAMPLES["Row"], item=dict(unknown, type="bug"))}
	)["Row"]

	assert "escalation" in shown, (
		f"a type this client does not recognise lost its chip entirely, so a reader learns "
		f"nothing about what the item is: {shown}"
	)

	# **A glyph either way**, so an unrecognised type reads as *something, unspecified* rather
	# than as a row that lost a picture every other row has.
	#
	# **Counted before the row's controls**, because `#1046` gave `Complete` a tick: a count
	# over the whole row would be two either way and would stop saying anything about the type.
	def glyphs (markup: str) -> int:
		"""How many glyphs the row's marks draw, which is this test's subject."""

		return markup[: markup.index("<button")].count("<svg")

	assert glyphs(shown) == glyphs(known) == 1, (
		f"a recognised type draws {glyphs(known)} glyphs and an unrecognised one "
		f"{glyphs(shown)}"
	)


def test_every_seeded_item_type_has_a_glyph () -> None:
	"""The client's map covers the vocabulary the server actually ships (`#764`).

	**Read from `seed.py` rather than listed here**, so a twelfth seeded type fails this rather
	than quietly falling back — the fallback is for a type *somebody else* invented, and using
	it for one we ship ourselves would be the mapping silently going stale.

	This is the half `#826` will change: the moment a workspace can add a type, the seeded set
	stops being the whole set and the fallback carries the rest. It is right until then and
	this is what says when it stops being.
	"""

	source = (ASSETS / "app.js").read_text(encoding="utf-8")
	mapping = re.search(r"export const TYPE_ICONS = \{(.*?)\n\};", source, re.DOTALL)

	assert mapping is not None, "`TYPE_ICONS` has moved, so this is scanning nothing"

	drawn = set(re.findall(r"^\t([a-z_]+):", mapping.group(1), re.M))
	seeded = {one.key for one in subroutine.db.seed.SEEDED_ITEM_TYPES}

	assert len(seeded) >= 10, f"only {seeded} seeded, so this checks almost nothing"

	assert seeded <= drawn, (
		f"{sorted(seeded - drawn)} are seeded item types with no glyph, so they fall back to "
		f"the one meant for a type this client has never heard of"
	)


#: What each seeded type draws today, pinned rather than derived — `SR#1134`, whose own words are
#: that a guard for this is worth more than the feature. Decision `SR#1133` adds a *fallback* and
#: changes nothing a reader already recognises, so the way this goes wrong is a glyph quietly
#: moving while every other test stays green: the map is still complete, every name is still
#: vendored, and the picture on somebody's board has changed.
TODAYS_GLYPHS = {
	"task": "check-square",
	"bug": "bug",
	"feature": "sparkle",
	"chore": "broom",
	"spike": "flask",
	"note": "note",
	"spec": "file-text",
	"design": "compass-tool",
	"decision": "gavel",
	"finding": "magnifying-glass",
	"dead_end": "prohibit",
}


def _unknown_icon () -> str:
	"""Return what this client draws for a type it cannot place, read out of the source.

	Written down here it would be a second copy of a value one line of `app.js` owns, and the
	copy that agrees is the one nothing catches.
	"""

	found = re.search(
		r'export const UNKNOWN_ICON = "([a-z-]+)"',
		(ASSETS / "app.js").read_text(encoding="utf-8"),
	)

	assert found is not None, "`UNKNOWN_ICON` has moved, so this is reading nothing"

	return found.group(1)


UNKNOWN = _unknown_icon()


@pytest.mark.parametrize(
	("type_key", "category", "expected", "why"),
	[
		("bug", "defect", "bug", "a key this client knows wins, and nothing about it moved"),
		("epic", "work", "check-square", "a type it has never seen draws by what kind it is"),
		("epic", "", UNKNOWN, "a type with no category has nothing left to fall through to"),
		("epic", "saga", UNKNOWN, "nor has one whose category this client does not know"),
	],
)
def test_a_type_this_client_has_never_seen_is_drawn_by_what_kind_of_thing_it_is (
	tmp_path: pathlib.Path, type_key: str, category: str, expected: str, why: str
) -> None:
	"""`SR#1134` driven rather than read, which is the half the source scans cannot do.

	The two guards above check that the *maps* are complete and that every name in them was
	vendored. Neither can say the chain is wired: `TYPE_ICONS[…] || CATEGORY_ICONS[…] || …`
	could have its middle term dropped, or read a field the row does not carry, and both would
	stay green.

	**The first case is what stops this being satisfied by always falling through.** `bug` has to
	keep drawing `bug` — decision `SR#1133` adds a fallback and changes nothing a reader already
	recognises — and the last two are what stop it being satisfied by never falling through.
	"""

	drawn = _addressing(tmp_path, [
		("marks", {
			"item": {"ref": 1, "kind": "task", "title": "Something", "type": type_key,
				"type_category": category, "status": "open", "status_is_default": True},
			"showKind": True, "ordering": None, "place": None, "linkable": False,
		}),
	])[0]

	identity = [one for one in drawn if one["family"] == "identity"]

	assert identity, f"no type mark was drawn at all, so this checks nothing: {drawn}"
	assert identity[0]["icon"] == expected, why


def test_the_glyph_each_seeded_type_draws_has_not_changed () -> None:
	"""A snapshot, deliberately, and the only one in this file.

	Derived checks are better than pinned ones almost everywhere, and here they are the thing
	that cannot work: a glyph's *correctness* is somebody's judgement about a picture, so there
	is nothing to derive it from. What can be checked is that it has not moved without anybody
	saying so — and `SR#1134` adds a whole second lookup beside this map, which is exactly the
	kind of change that reshuffles a table by accident.

	**Deleting an entry here is how a deliberate change passes**, which is the point: it costs
	one line and a moment's thought, and the alternative costs a reader their landmarks.
	"""

	source = (ASSETS / "app.js").read_text(encoding="utf-8")
	mapping = re.search(r"export const TYPE_ICONS = \{(.*?)\n\};", source, re.DOTALL)

	assert mapping is not None, "`TYPE_ICONS` has moved, so this is scanning nothing"

	drawn = dict(re.findall(r'^\t([a-z_]+):\s*"([a-z-]+)"', mapping.group(1), re.M))

	assert len(drawn) >= 11, f"only {sorted(drawn)} were read, so this checks almost nothing"
	assert {key: drawn[key] for key in TODAYS_GLYPHS if key in drawn} == TODAYS_GLYPHS


def test_every_category_a_workspace_can_seed_has_a_glyph_to_fall_back_to () -> None:
	"""`SR#1134`. The fallback is only worth having if it answers for every category.

	**Read from the model's vocabulary rather than listed here**, so a seventh category added to
	``db.mixins.ITEM_TYPE_CATEGORIES`` fails this rather than silently drawing the mark for
	*unknown* — which would be the fallback needing a fallback, and no louder than the gap it
	was built to close.

	The sibling above asks the same of the *type* map. Both are the same question one level
	apart, and this one is the level that survives a workspace inventing a type.
	"""

	source = (ASSETS / "app.js").read_text(encoding="utf-8")
	mapping = re.search(r"export const CATEGORY_ICONS = \{(.*?)\n\};", source, re.DOTALL)

	assert mapping is not None, "`CATEGORY_ICONS` has moved, so this is scanning nothing"

	drawn = set(re.findall(r"^\t([a-z_]+):", mapping.group(1), re.M))
	known = set(subroutine.db.mixins.ITEM_TYPE_CATEGORIES)

	assert len(known) >= 6, f"only {sorted(known)} exist, so this checks almost nothing"

	assert known <= drawn, (
		f"{sorted(known - drawn)} are categories with no glyph, so a type this client does not "
		f"recognise falls all the way through to the mark for unknown"
	)


def test_only_the_surface_with_a_column_for_it_drops_the_chip (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#1424` moves who-has-a-row out of the flow on **one** surface, not out of `marks`.

	**A board card is not a row and has no grid to line up against.** `.board .row` is
	`display: block` deliberately: a 4.5rem address column inside a 260px column leaves a title
	about 150px wide, which wraps into nonsense. So the chip is still the right answer there,
	and arranging a board *by* who holds a card is `SR#1425` rather than this.

	**Both directions, because either one alone passes against a mistake.** Defaulting the flag
	the other way would take the fact off the board and the agenda and leave every test of the
	column passing; never reading it would draw the same fact twice on every row of the list,
	which is this codebase's signature defect and is the reason the flag exists at all.
	"""

	drawn, withheld = _addressing(tmp_path, [
		("marks", {
			"item": {
				"ref": 1, "title": "Held by an agent", "assignee": "gizmo",
				"assignee_is_agent": True, "assignee_answers_to": "morgan",
			},
			"showKind": False, "ordering": None, "place": None,
			"linkable": False, "hideStatus": False, "hideAssignee": False,
		}),
		("marks", {
			"item": {
				"ref": 1, "title": "Held by an agent", "assignee": "gizmo",
				"assignee_is_agent": True, "assignee_answers_to": "morgan",
			},
			"showKind": False, "ordering": None, "place": None,
			"linkable": False, "hideStatus": False, "hideAssignee": True,
		}),
	])

	def names_the_holder (marks: list[dict[str, typing.Any]]) -> list[str]:
		"""Return every mark that says who has this."""

		return [mark["text"] for mark in marks if "gizmo" in (mark.get("text") or "")]

	assert names_the_holder(drawn) == ["@gizmo (agent, @morgan)"], (
		f"a surface with no column for it stopped saying who holds the row: {drawn}"
	)
	assert names_the_holder(withheld) == [], (
		f"the list draws who has this as a column *and* as a chip, which is the same fact "
		f"twice on one row: {withheld}"
	)

	# **The rest of the row is untouched**, which is what says the flag is narrow. Without this
	# a version that returned nothing at all when asked to withhold one mark would pass above.
	assert len(withheld) == len(drawn) - 1, (
		f"withholding the holder changed more than the holder: {drawn} against {withheld}"
	)


def test_a_row_draws_a_different_glyph_for_a_person_and_for_an_agent (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#1421`, design `SR#1422`. **The word says it; this is what makes it scannable.**

	`SR#1414` put *(agent, @si)* on the row and Simon's objection was that the information is
	there and cannot be *scanned* — a reader sees a name and has to read to tell a colleague
	from something somebody set running.

	**Both kinds carry one, and that is the assertion that matters.** Marking only agents makes
	the **absence** of a glyph carry the other half, and an absence does not catch an eye on a
	page of fifty rows — so a version that drew a robot and left a person bare would look
	correct in a screenshot of one row and fail at the thing this is for.

	**The glyph never carries it alone** — `SR#102`, and
	`test_every_surface_says_an_agent_is_one_and_who_answers_for_it` is the half that holds the
	*word*. Together they say: the text is sufficient, and the picture is what makes it quick.
	"""

	person, agent = _addressing(tmp_path, [
		("marks", {
			"item": {"ref": 1, "title": "Ordinary", "assignee": "jo"},
			"showKind": False, "ordering": None, "place": None,
			"linkable": False, "hideStatus": False,
		}),
		("marks", {
			"item": {
				"ref": 2, "title": "Held by an agent", "assignee": "gizmo",
				"assignee_is_agent": True, "assignee_answers_to": "morgan",
			},
			"showKind": False, "ordering": None, "place": None,
			"linkable": False, "hideStatus": False,
		}),
	])

	def icon_beside (marks: list[dict[str, typing.Any]], who: str) -> str | None:
		"""Return the glyph on the mark that names this account, or None if it has none."""

		for mark in marks:
			if who in (mark.get("text") or ""):
				return mark.get("icon")

		raise AssertionError(f"no mark named {who!r} at all, so this test asks nothing: {marks}")

	for_person = icon_beside(person, "@jo")
	for_agent = icon_beside(agent, "@gizmo")

	assert for_person, f"a person's row carries no glyph, so an agent's is an absence: {person}"
	assert for_agent, f"an agent's row carries no glyph: {agent}"
	assert for_person != for_agent, (
		f"a person and an agent draw the same glyph {for_person!r}, so the picture says nothing "
		f"and only the word tells them apart — which is the thing SR#1421 was for"
	)


def test_every_glyph_this_client_names_is_one_that_was_vendored () -> None:
	"""`SR#925`. **A name with no path data draws nothing, and says nothing about it.**

	`Icon` returns null for a name it does not have, deliberately and rightly — the type map is
	one client's opinion about a vocabulary a workspace owns, so being handed an unknown name is
	a normal event rather than an error. The cost is that a **typo in this client's own maps** is
	indistinguishable from that: the chip renders, the word is there, and the picture silently is
	not. `SR#251`'s inert control, drawn at 16 pixels.

	The seeded-type guard above asks whether every type *has* a name; this asks whether every
	name *is* one. Both directions, because they fail differently: a type with no entry falls
	back to a glyph meant for a stranger's vocabulary, and an entry naming nothing falls back to
	blank.
	"""

	source = (ASSETS / "app.js").read_text(encoding="utf-8")
	vendored = (
		subroutine.web.vendored.DIRECTORY / "phosphor.js"
	).read_text(encoding="utf-8")

	held = set(re.findall(r'^\t"([a-z-]+)":', vendored, re.M))

	assert len(held) >= 14, f"only {sorted(held)} were vendored, so this checks almost nothing"

	named = set()

	for constant in ("TYPE_ICONS", "MARK_ICONS", "CATEGORY_ICONS"):
		mapping = re.search(rf"export const {constant} = \{{(.*?)\}};", source, re.DOTALL)

		assert mapping is not None, f"`{constant}` has moved, so this is scanning nothing"

		named |= set(re.findall(r':\s*"([a-z-]+)"', mapping.group(1)))

	named |= set(re.findall(r'export const UNKNOWN_ICON = "([a-z-]+)"', source))

	assert named, "no glyph names were found, so this is checking nothing"

	missing = sorted(named - held)

	assert not missing, (
		f"{missing} are named as glyphs and no path data was vendored for them, so every "
		f"chip using one draws no picture at all and nothing else says so"
	)


def test_a_repeat_and_its_anchor_travel_together_or_not_at_all (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#94`, and the rule neither field list can express — so it has its own function.

	The anchor control always holds a value, because a repeat is always measured from
	somewhere. The service refuses an anchor with no rule by name (`SR#918`), so a body that
	copied the controls independently would **refuse every ordinary create**: nothing typed
	into the phrase box, a select still reading *keep to the schedule*, and a 422 about a field
	the reader never opened the disclosure to see.
	"""

	[alone] = _views(tmp_path, [("filed", {"slug": "projects", "values": {
		"text": "buy milk", "recurrence": "", "recurrence_anchor": "schedule",
	}})])

	assert alone == {"workspace_id": "projects", "text": "buy milk"}, (
		f"an untouched disclosure sent {sorted(alone)}, and an anchor without a rule is a 422"
	)

	[together] = _views(tmp_path, [("filed", {"slug": "projects", "values": {
		"text": "water the plants",
		"recurrence": "every 3 days", "recurrence_anchor": "completion",
	}})])

	assert together["recurrence"] == "every 3 days"
	assert together["recurrence_anchor"] == "completion"


def test_clearing_the_repeat_box_stops_the_series (tmp_path: pathlib.Path) -> None:
	"""`SR#94`. **This form's only way to say *stop repeating*, and it is the same word as
	everywhere else on it.**

	Every other control here is nulled when it is blank, because §8.3 makes that the difference
	between *unchanged* and *cleared* — and a repeat reads it identically: the series ends, the
	work in hand keeps its number and its record, and nothing follows it. So a reader who wants
	something to stop coming back empties the box, which is what emptying a box means on every
	other field of the same form.

	**The anchor is not sent alongside**, for the reason above: on the way to a `PATCH` a lone
	anchor is refused exactly as it is on the way to a `POST`.
	"""

	[body] = _views(tmp_path, [("edited", {
		"values": {
			"title": "Water the plants", "status": "open", "type": "task", "project": "inbox",
			"recurrence": "", "recurrence_anchor": "completion",
		},
		"item": {"version": 3},
	})])

	assert body["recurrence"] is None, (
		f"an emptied box sent {body.get('recurrence')!r}, which leaves the series running while "
		f"the form reports success — a silent no-op, and the worst of the three failures"
	)
	assert "recurrence_anchor" not in body, (
		"the anchor went without a rule to qualify, which the service refuses by name"
	)


def test_a_repeating_item_opens_its_form_with_the_words_that_were_written (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#94`. **A box that opened empty would read as *this does not repeat* — and then save
	as *stop repeating*, because that is what blank means here.**

	`recurrence_text` is null when a caller sent an `RRULE` directly, so the rule itself is the
	fallback. It is ugly and it is the truth, and `POST /v1/recurrence/parse` accepts it back —
	which is what stops the fallback being a value the form cannot re-submit.
	"""

	[written] = _views(tmp_path, [("fromItem", {"item": {
		"title": "Water the plants",
		"recurrence_text": "every 3 days",
		"recurrence_rule": "FREQ=DAILY;INTERVAL=3",
		"recurrence_anchor": "completion",
	}})])

	assert written["recurrence"] == "every 3 days"
	assert written["recurrence_anchor"] == "completion"

	[compiled] = _views(tmp_path, [("fromItem", {"item": {
		"title": "Pay the rent",
		"recurrence_text": None,
		"recurrence_rule": "FREQ=MONTHLY;BYMONTHDAY=30",
	}})])

	assert compiled["recurrence"] == "FREQ=MONTHLY;BYMONTHDAY=30", (
		"a rule sent directly left the box empty, so reopening the item and saving would stop it"
	)


def _read_by_python (module: typing.Any, function: str, variable: str) -> set[str]:
	"""Return the view fields one Python renderer reads off the item it is given.

	`SR#427`'s method, and the same extraction `tests/test_mcp.py` uses to compare the terminal's
	renderers with the agent's — reading the renderer rather than listing beside it is the only
	reason such a comparison stays true as either surface grows.
	"""

	tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
	found = set()

	for node in ast.walk(tree):
		if not isinstance(node, ast.FunctionDef) or node.name != function:
			continue

		found |= {
			read.attr
			for read in ast.walk(node)
			if isinstance(read, ast.Attribute)
			and isinstance(read.value, ast.Name)
			and read.value.id == variable
		}

	return found


#: A fact the terminal's listing row carries that a browser row deliberately does not, and why.
#: The same register `tests/test_mcp.py` keeps for the agent's row, asked of the third rendering.
SAID_ANOTHER_WAY: dict[str, str] = {
	# **The same fact through a better field** (`SR#925`). The terminal reads the rule and its
	# anchor and calls `recurrence.describe` on them; a browser cannot, because that function is
	# Python — so it would need a second copy of the grammar, free to disagree in silence. It
	# reads `recurrence_description` instead, which is `describe`'s answer computed once on the
	# server. Two fields there, one here, and the one carries both.
	#
	# **What would remove these**: the terminal reading `recurrence_description` too, which is
	# the tidier end state and is a change to `cli/personal._when` rather than to this file.
	"recurrence_rule": "recurrence_description",
	"recurrence_anchor": "recurrence_description",
}


#: A fact the terminal's listing row carries that a browser row does not report at all, and why.
NOT_ON_A_BROWSER_ROW: dict[str, str] = {
	"timezone": (
		"Read to render a day-scale date in the zone that stored it, which a browser row does "
		"too — through `day(value, item.timezone, …)`, so it is a *use* rather than a fact "
		"drawn. It is on the terminal's list because that renderer names the field directly."
	),
}


def test_a_browser_row_reports_what_the_command_lines_row_reports () -> None:
	"""`SR#925`, Simon: *"nothing indicates that it is a repeating task"*.

	**The third rendering of one row, and nothing compared it to either of the others.** `SR#583`
	put a guard on the terminal's two renderings and `SR#922` on the terminal against the agent;
	both stopped at the surfaces written in Python. So the browser — the surface `SR#755` made a
	person's primary one — carried a repeat nowhere at all, for as long as the feature existed,
	and it was found by Simon opening a page.

	**Why no existing guard could see it.** `NOT_ON_THE_FORM` compares *controls* against what
	`POST /v1/tasks` accepts, which is a question about writing;
	`test_a_listing_asks_for_every_field_its_rows_render` compares what a row draws against what
	the request asks for, which is satisfied when a row draws nothing. Neither asks whether this
	surface says what the others say.
	"""

	source = _served_modules()["app.js"]
	surface = ["Row", "marks", "when", "overdue"]
	bodies = {name: _function_body(source, name) for name in surface}

	browser = set()

	for body in bodies.values():
		browser |= set(re.findall(r"\bitem\.([a-z_][a-z0-9_]*)\b", body))

	terminal = _read_by_python(subroutine.cli.personal, "_when", "task")

	assert browser, "no fields were found in the browser's row, so this is checking nothing"
	assert terminal, "no fields were found at the command line, so this is checking nothing"

	missing = sorted(terminal - browser - set(NOT_ON_A_BROWSER_ROW) - set(SAID_ANOTHER_WAY))

	assert not missing, (
		f"a listing row at the terminal reports {missing} and a browser row does not. Draw "
		f"them, name the field that stands in for each in SAID_ANOTHER_WAY, or record in "
		f"NOT_ON_A_BROWSER_ROW what a reader gets instead."
	)

	# **The substitution is checked, not taken on trust**, and the first version of this guard
	# was inert for want of it: excusing the two recurrence fields as *read another way* made
	# the comparison vacuous, so deleting the chip from `marks` — the exact state Simon found —
	# left it green. An excuse naming a stand-in nobody verifies is this project's own signature
	# defect, written into the guard built to catch it.
	unread = sorted(set(SAID_ANOTHER_WAY.values()) - browser)

	assert not unread, (
		f"SAID_ANOTHER_WAY says a browser row reports {unread} instead of a field the terminal "
		f"draws, and no browser row reads {'it' if len(unread) == 1 else 'them'}. So neither is "
		f"shown and the excuse is the only thing saying otherwise."
	)


def test_every_fact_excused_from_a_browser_row_is_still_read_at_the_command_line () -> None:
	"""So neither register can go on excusing a fact no terminal row renders — `SR#405`'s rule."""

	terminal = _read_by_python(subroutine.cli.personal, "_when", "task")
	unknown = sorted(
		field
		for field in (set(NOT_ON_A_BROWSER_ROW) | set(SAID_ANOTHER_WAY))
		if field not in terminal
	)

	assert not unknown, (
		f"NOT_ON_A_BROWSER_ROW names {unknown}, which a terminal row no longer reads."
	)


def test_a_repeating_row_says_so_and_the_item_page_says_how (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#925`, Simon's own split: an indicator on a row, the whole sentence on the item page.

	**A card is narrow and is scanned**, so *does this come back* is the question a row is being
	asked; *how* is what somebody opens it to check. Both are rendered from
	`recurrence_description`, which the server generates from the stored rule — so the item page
	shows what will happen rather than the phrase somebody typed, which is the whole of his
	objection and of §6.7's read-back rule.
	"""

	[row, facts] = _rendered(tmp_path, {
		"Row": {"item": dict(
			SAMPLES["Row"]["item"], recurrence_description="every other week, on Tuesday"
		)},
		"Facts": {"item": {
			"ref": 42,
			"title": "Water the plants",
			"recurrence_description": "every 3 days, from when it is done",
		}},
	}).values()

	assert "Repeats" in row, row

	# **The sentence is not on the row**, which is the half that says the split was made rather
	# than one of the two simply being forgotten.
	assert "every other week" not in row, row

	assert "Repeats" in facts and "every 3 days, from when it is done" in facts, facts


def test_a_form_opened_on_a_repeat_says_what_is_in_force_before_anybody_types (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#925`. **The check was there while typing and absent while reviewing.**

	Simon: *"I see 'every monday' in the 'How often' box, but this does not tell me how it has
	been parsed"*. The live preview fires on `onInput`, so reopening a repeat to check it showed
	the one thing that confirms nothing — his own string back. The stored sentence is on the
	item, so this needs no request: the disclosure can say what is in force the moment it opens.
	"""

	[opened] = _rendered(tmp_path, {"Repeats": {
		"busy": False,
		"held": {
			"recurrence": "every monday",
			"recurrence_anchor": "schedule",
			"recurrence_description": "every Monday",
		},
	}}).values()

	assert "every Monday" in opened, opened

	# **And a live answer still wins**, because somebody typing has moved on from what is
	# stored — the seed is the resting state, not a value that outranks the server's reply.
	[typing] = _rendered(tmp_path, {"Repeats": {
		"busy": False,
		"held": {"recurrence": "every monday", "recurrence_description": "every Monday"},
		"reading": {"description": "every other week, on Tuesday"},
	}}).values()

	assert "every other week, on Tuesday" in typing, typing
	assert "every Monday" not in typing, typing


def test_a_table_column_says_how_it_is_aligned_without_an_inline_style (
	tmp_path: pathlib.Path,
) -> None:
	"""The one inline style the app produced, blocked by the app's own policy.

	`api/policy` states the measurement `default-src 'self'` was chosen on — the app *"uses no
	inline styles and no ``url()`` in its stylesheet"* — and this renderer emitted
	``style="text-align:center"`` for every aligned column of every Markdown table. So the one
	construct that needed the exception was refused by the policy written on the assumption
	there was none, and a centred column arrived left-aligned with a violation in the console.

	Both halves: the alignment still reaches the page, and it reaches it as a class the
	stylesheet defines — a rendering that names a class nothing styles is the same defect
	wearing the fix.
	"""

	table = "| a | b | c |\n| :-- | :-: | --: |\n| 1 | 2 | 3 |"
	html = _markdown(tmp_path, [table])[0]

	assert "style=" not in html, f"an inline style survives: {html!r}"

	for how in ("left", "center", "right"):
		assert f'class="align-{how}"' in html, f"{how} alignment is not rendered: {html!r}"

	styles = subroutine.api.web.FILES["app.css"][0].decode("utf-8")

	for how in ("left", "center", "right"):
		assert f".align-{how}" in styles, f"the stylesheet does not define align-{how}"


def test_a_reader_who_may_not_write_is_offered_no_control_that_would_refuse (
	tmp_path: pathlib.Path,
) -> None:
	"""``WorkspaceAccess.permissions`` says *"this is the field to act on"* and nothing read it.

	So a member with a read-only role — or anybody holding a narrowed credential, which is
	every agent this product is built for — was shown Edit, Complete, the status control, the
	assignee control, the comment box, the link box and Remove, and every one of them answered
	403 when pressed. `app.js` states the rule against that three times in its own comments: a
	control that refuses when pressed is worse than one that is not there.

	**Both directions, because the empty case is not the interesting one.** A page with no
	controls is also what a broken read produces, so the reader who *may* write is asserted
	first — otherwise this passes against an app that offers nothing to anybody.
	"""

	writing = _driven(tmp_path, pathname="/projects")
	reading = _driven(tmp_path, pathname="/projects", permissions=())

	assert "Add" in writing["said"], "the capture box is §1.4's primary path and must be there"
	assert "Add" not in reading["said"], (
		f"a reader who may not write was offered the capture box: {reading['said']!r}"
	)


def test_every_select_has_a_name_a_screen_reader_can_read () -> None:
	"""`#927`'s L-7. A combo box announced as *"combo box"* and nothing else.

	The workspace switcher was the one bare ``<select>`` in the app — no wrapping ``<label>``,
	no ``aria-label`` — and it is the control that decides which backlog you are looking at.
	Every other one is named, so this was a gap rather than a policy.

	**A guard rather than the fix**, because the fix is one attribute and the next select is
	the one nobody will check. Read out of the source with comments stripped, since a comment
	explaining ``aria-label`` would otherwise satisfy a scan for it — which is `#427`'s recorded
	trap, met three times in this repository.

	**Wrapped in a ``<label>`` counts**, and is preferred: it names the control *and* enlarges
	the target. The masthead has nowhere to put one, which is why the switcher takes the
	attribute instead.
	"""

	source = _without_comments((ASSETS / "app.js").read_text(encoding="utf-8"))
	nameless = []

	for found in re.finditer(r"<select\b([^>]*)>", source):
		attributes = found.group(1)

		if "aria-label" in attributes or "aria-labelledby" in attributes:
			continue

		# A `<label>` opened and not yet closed before this point is one this select sits in.
		before = source[: found.start()]

		if before.count("<label") > before.count("</label>"):
			continue

		nameless.append(source[: found.start()].count("\n") + 1)

	assert not nameless, (
		f"a <select> at line(s) {nameless} has no accessible name — it is in no <label> and "
		f"carries no aria-label, so a screen reader announces a combo box and nothing about "
		f"what it changes"
	)

	assert source.count("<select") > 5, (
		"the scan found almost no selects, so it is reading something other than the app"
	)


def _at_the_clock (
	tmp_path: pathlib.Path, cases: typing.Sequence[tuple[str, dict[str, typing.Any], int]]
) -> list[typing.Any]:
	"""Ask ``overdue`` or ``deferred`` about a row at the moment given with it."""

	module = _staged(tmp_path)

	return list(_ran(tmp_path, f"""
		import * as app from "{module.as_uri()}";

		process.stdout.write(JSON.stringify(
			{json.dumps([list(case) for case in cases])}.map(([mark, item, now]) =>
				app[mark](item, now))
		));
	"""))


def test_a_mark_read_from_the_clock_answers_the_clock_it_is_given (
	tmp_path: pathlib.Path,
) -> None:
	"""`#950`, cold review `#927`'s L-7. The half that can be exact.

	``overdue`` and ``deferred`` read ``new Date()`` and took no argument, so the only thing a
	test could ask was *what do you say right now* — which was never in doubt. They take a
	``now`` like ``holding``, ``moment`` and ``when`` already did, and the interesting question
	becomes answerable: **the same row, two instants, two answers.**

    That is what the page depends on. ``deferred``'s own comment argues the value is computed
	rather than published *because* a published one would go stale on a page left open — so a
	mark that could not be asked about a different moment was a claim nothing could check.
	"""

	noon = int(datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC).timestamp() * 1000)
	evening = int(datetime.datetime(2026, 8, 9, 18, 0, tzinfo=datetime.UTC).timestamp() * 1000)

	parked = {"ref": 1, "kind": "task", "title": "Later", "snoozed_until": "2026-08-09T15:00:00+00:00"}
	due = {"ref": 2, "kind": "task", "title": "Soon", "due_at": "2026-08-09T15:00:00+00:00"}

	assert _at_the_clock(tmp_path, [
		("deferred", parked, noon),
		("deferred", parked, evening),
		("overdue", due, noon),
		("overdue", due, evening),
	]) == [True, False, False, True], (
		"a mark read from the clock gave the same answer at two instants either side of the "
		"moment it is about, so nothing about a page left open could be checked"
	)


def test_the_poll_re_renders_so_those_marks_are_recomputed () -> None:
	"""`#950`. **Guarded at the wiring, and that is stated rather than left to be found.**

	The behaviour — *a mark goes away while somebody watches* — needs a `snoozed_until` that
	passes between two renders, and `_driven` waits a fixed 300ms before ticking. Any instant
	close enough to cross in that window is close enough to have crossed already on a slow
	run, which is a flaky test in the one file that is excluded from the parallel suite for
	load sensitivity. `#767` is the precedent: guarded at the wiring because `_driven` cannot
	press Back, said in the test rather than discovered later.

	So this asserts the tick bumps state, and the test above asserts the marks answer a clock.
	Together they are the claim; neither is on its own.

	**Read with comments stripped**, because the paragraph explaining `retick` names it several
	times and would satisfy a scan for it — `#427`'s trap, met three times in this repository.
	"""

	source = _without_comments((ASSETS / "app.js").read_text(encoding="utf-8"))
	interval = source[source.index("setInterval(async") :]

	assert "retick(" in interval[: interval.index("}, POLL_MS)")], (
		"the poll no longer bumps state, so a page left open stops recomputing `overdue` and "
		"`deferred` — which is what `deferred`'s own comment says computing them is for"
	)


def test_a_label_says_only_what_the_address_did_not (tmp_path: pathlib.Path) -> None:
	"""Decision `SR#957` §4's table, driven row by row.

	> full path -> strip what the URL already said -> show it

	**The workspace leads when the address named none**, which is the agenda at `/`: it spans
	every workspace, so a bare `subroutine/ui` there would name a project in whichever one the
	reader assumed.
	"""

	row = {"project_path": "subroutine/ui", "workspace": "projects"}
	nowhere, workspace, project, exact, elsewhere, none = _addressing(tmp_path, [
		("projectLabel", {"item": row, "place": None}),
		("projectLabel", {"item": row, "place": {"workspace": "projects", "project": None}}),
		("projectLabel", {"item": row,
			"place": {"workspace": "projects", "project": "subroutine"}}),
		("projectLabel", {"item": row,
			"place": {"workspace": "projects", "project": "subroutine/ui"}}),
		# A prefix that is not a whole segment: `ui` must not turn `ui-things/x` into `-things/x`.
		("projectLabel", {"item": {"project_path": "ui-things/x", "workspace": "projects"},
			"place": {"workspace": "projects", "project": "ui"}}),
		("projectLabel", {"item": {"workspace": "projects"}, "place": None}),
	])

	assert nowhere == "projects/subroutine/ui", "the agenda at / named no workspace"
	assert workspace == "subroutine/ui"
	assert project == "ui"
	assert exact == "", "the page is that project, so the label says nothing"
	assert elsewhere == "ui-things/x", "a label was shortened at something that is not a segment"
	assert none == "", "a row with no project claimed one"


def test_a_project_label_is_escaped_a_segment_at_a_time (tmp_path: pathlib.Path) -> None:
	"""`encodeURIComponent` over a whole address escapes its separators too.

	`substation/dist` would become `substation%2Fdist` — one segment, naming a project keyed
	with a slash in it, which is a project that cannot exist. The separators are structure and
	the segments are values; only the second kind is escaped.
	"""

	plain, spaced = _addressing(tmp_path, [
		("encodedPath", "substation/dist"),
		("encodedPath", "a b/c d"),
	])

	assert plain == "substation/dist"
	assert spaced == "a%20b/c%20d"


def test_a_project_label_is_a_link_only_where_something_can_follow_it (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#251`'s rule: a control whose only outcome is nothing happening should not be drawn.

	The label is rendered either way — where a row lives is worth saying on any surface — and
	the anchor is what depends on somebody listening.
	"""

	item = {"ref": 1, "kind": "task", "title": "A task", "status_is_default": True,
		"project_key": "ui", "project_path": "subroutine/ui", "workspace": "projects"}

	linked, plain = _addressing(tmp_path, [
		("marks", {"item": item, "showKind": False, "ordering": None, "projects": None,
			"place": {"workspace": "projects", "project": None}, "linkable": True}),
		("marks", {"item": item, "showKind": False, "ordering": None, "projects": None,
			"place": {"workspace": "projects", "project": None}, "linkable": False}),
	])

	assert [mark["text"] for mark in linked] == ["subroutine/ui"]
	assert linked[0]["href"] == "/projects/subroutine/ui"
	assert [mark["text"] for mark in plain] == ["subroutine/ui"]
	assert plain[0]["href"] is None, "a surface that cannot navigate drew a link anyway"


def test_a_page_is_only_wide_where_a_board_is_on_it (tmp_path: pathlib.Path) -> None:
	"""`SR#963`, Simon 2026-08-17, from the served instance.

	**An open item is a document, whatever view the reader came from.** This asked
	`showing.view === "board"` alone, and opening an item never clears the view — so a board, a
	click, and the item page arrived at the board's uncapped width.

	**Reported as stale CSS and it is not**, which was measured before anything was written:
	`SR#914`'s assets answer `no-cache` with an ETag and a `304` on a match, so a stylesheet
	cannot be stale on any load. That refreshing *fixed* it is evidence against caching — a
	refresh hits the same cache — and for the view falling back to the list, because
	`/projects/subroutine/871` carries no `?view=`.
	"""

	board, reading, listing, item = _addressing(tmp_path, [
		("frame", {"showing": {"view": "board", "selection": {}}, "open": None}),
		("frame", {"showing": {"view": "board", "selection": {}}, "open": {"item": {"ref": 1}}}),
		("frame", {"showing": {"view": "list", "selection": {}}, "open": None}),
		("frame", {"showing": {"view": "list", "selection": {}}, "open": {"item": {"ref": 1}}}),
	])

	assert board == "app wide", "the board no longer gets the screen, which is SR#846"
	assert reading == "app", "an item opened from a board is read at the board's width"
	assert listing == "app"
	assert item == "app"


def test_going_home_writes_an_address_with_no_arrangement (tmp_path: pathlib.Path) -> None:
	"""`SR#962`'s second half: `/` arrived as `/?view=board&include_completed=true`.

	`withShowing` carries the arrangement everywhere (`SR#651`) and is right everywhere but
	here: **an agenda has no board and no completed filter**, so what it carried to `/` was a
	selection that means nothing where it landed. `addressOf` already knows — it returns `"/"`
	for the agenda — and this is the same fact asked of the writer rather than the reader.
	"""

	blank, carried = _addressing(tmp_path, [
		("withShowing", {"path": "/", "showing": {"view": None, "selection": {}}}),
		("withShowing", {"path": "/projects", "showing": {
			"view": "board", "selection": {"include_completed": "true"},
		}}),
	])

	assert blank == "/", f"going home carried an arrangement the agenda has none of: {blank}"
	assert carried == "/projects?view=board&include_completed=true", (
		"a listing stopped carrying its arrangement, which is what SR#651 is for"
	)



def test_no_browser_test_waits_by_evaluating_a_string () -> None:
	"""`SR#1000`: `wait_for_function` takes a string, and the served policy forbids evaluating one.

	`SR#805` gives every response a content security policy with no `unsafe-eval`, so Chromium
	refuses the predicate Playwright injects — **intermittently**, which is worse than always:
	the run that failed and the run that passed were the same file, unchanged, minutes apart.
	A guard that blames the product at random for a policy the product is right to have is the
	shape `SR#998` filed a *slow* guard over.

	**Here rather than in `tests/test_browser.py`**, which is capped at twenty-six tests
	answering what only a browser can (`SR#964`). This needs no browser: it reads a file.

	**Walked as an AST rather than grepped**, so the paragraph in `test_browser.py` explaining
	why the call is not used cannot be read as a use of it — `SR#836`'s trap, where a scan over
	prose reports the sentence that describes the rule.
	"""

	source = pathlib.Path(__file__).parent / "test_browser.py"
	tree = ast.parse(source.read_text(encoding="utf-8"))
	found = [
		node.lineno
		for node in ast.walk(tree)
		if isinstance(node, ast.Call)
		and isinstance(node.func, ast.Attribute)
		and node.func.attr == "wait_for_function"
	]

	assert not found, (
		f"{source.name} evaluates a string to wait, at line(s) {found}. The policy this "
		f"application serves has no 'unsafe-eval', so the wait is refused rather than slow. "
		f"Say the condition in CSS — 'option:nth-child(3)' is 'more than two options' — or "
		f"poll from Python with `_until`."
	)


#: Two workspaces, one with a focus and one without — the shape decision `SR#982` allows and the
#: one a merged agenda actually meets. Deliberately not three prioritised projects: that is the
#: state a single pointer per workspace makes unreachable, so a fixture holding it would be
#: testing something the schema forbids.
FOCUSED = [
	{"id": "1", "slug": "projects", "title": "Subroutine", "prioritised_project": "subroutine"},
	{"id": "2", "slug": "personal", "title": "Personal", "prioritised_project": None},
]


def test_a_page_spanning_workspaces_names_each_prioritised_project_by_its_workspace (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#986`. The agenda spans workspaces, so an unqualified name would identify nothing.

	The terminal's `qualifies_workspace` says the same thing about the same question, and the
	rule is `SR#512`'s: the shortest form that identifies the thing, and no shorter. A listing
	is narrowed to one workspace and asks the narrower question, so it gets the bare address.
	"""

	spanning, narrowed, quiet = _views(tmp_path, [
		("prioritisedHere", {"workspaces": FOCUSED, "workspace": None}),
		("prioritisedHere", {"workspaces": FOCUSED, "workspace": "projects"}),
		("prioritisedHere", {"workspaces": FOCUSED, "workspace": "personal"}),
	])

	assert spanning == ["projects/subroutine"], "a page spanning workspaces has to say which"
	assert narrowed == ["subroutine"], "a page inside one workspace does not"
	assert quiet == [], "and a workspace with no focus contributes nothing"


def test_one_workspace_needs_no_qualifying (tmp_path: pathlib.Path) -> None:
	"""§13.5b's instance is one workspace, and nothing about this may reach its output."""

	[alone] = _views(tmp_path, [("prioritisedHere", {
		"workspaces": [FOCUSED[0]], "workspace": None,
	})])

	assert alone == ["subroutine"]


def test_the_sentence_agrees_with_itself_about_how_many (tmp_path: pathlib.Path) -> None:
	"""Three plurals in one line — the verb, the possessive and the noun — and `SR#986`.

	Two workspaces may each prioritise a project, and *"a, b is prioritised, so its work rises"*
	is the kind of detail that makes a reader distrust every number beside it.
	"""

	nothing, one, two = _views(tmp_path, [
		("prioritisedSentence", []),
		("prioritisedSentence", ["subroutine"]),
		("prioritisedSentence", ["projects/subroutine", "personal/home"]),
	])

	assert nothing is None, "nothing prioritised says nothing at all"
	assert one == "subroutine is prioritised, so its work rises here."
	assert two == (
		"projects/subroutine and personal/home are prioritised, so their work rises here."
	)


def test_the_browser_says_what_is_prioritised_in_the_terminal_s_words (
	tmp_path: pathlib.Path,
) -> None:
	"""One sentence, generated twice, and the two are compared — `SR#986`.

	`SR#925`'s rule is that when a client would need a copy of a grammar to render a field, the
	*rendering* is what gets published. This is smaller than that and the same shape: a person
	moving between the two surfaces should not have to work out that they are being told the
	same thing, and two independently-worded copies drift a word at a time with nothing noticing.
	"""

	[said] = _views(tmp_path, [("prioritisedSentence", ["web/dist"])])

	assert said == subroutine.cli.personal._prioritised_sentence(["web/dist"])

	[several] = _views(tmp_path, [("prioritisedSentence", ["a/b", "c/d"])])

	assert several == subroutine.cli.personal._prioritised_sentence(["a/b", "c/d"])


def test_the_browser_and_the_terminal_agree_which_orders_the_bonus_applies_to (
	tmp_path: pathlib.Path,
) -> None:
	"""A prioritised project changes a ranked page and no other, so both say so or neither does.

	**The failure this prevents is a page announcing an effect it is not showing** — a line a
	reader learns to ignore, which is worse than no line. `SR#986`.
	"""

	asked = ["-priority_score", "priority_score", "-created_at", "due_at,-priority_score", ""]
	answered = _views(tmp_path, [("rankedByPriority", one) for one in asked])

	assert answered == [True, True, False, True, False]
	assert answered == [
		subroutine.cli.personal._ranked_by_priority(one or None) for one in asked
	]


def test_a_project_dropdown_marks_the_one_that_is_prioritised (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#986`, and **only the project itself** — its subtree inherits the bonus, not the label.

	Marking children would read as several prioritised projects, which is the state a single
	pointer makes unreachable. `(default)` on the Inbox is the precedent for saying that an
	entry is not an ordinary one.
	"""

	[marked] = _views(tmp_path, [("filableFor", {
		"projects": FILABLE, "project": None, "prioritised": "substation",
	})])
	labels = [one["label"].strip() for one in marked]

	child = next(one for one in labels if one.startswith("Packaging"))

	assert "Substation (prioritised)" in labels
	assert not child.endswith("(prioritised)"), (
		"the child inherits the bonus and not the label"
	)

	[plain] = _views(tmp_path, [("filableFor", {
		"projects": FILABLE, "project": None, "prioritised": None,
	})])

	assert not [one for one in plain if "(prioritised)" in one["label"]], (
		"nothing prioritised marks nothing — which is most workspaces, every day"
	)


def test_the_masthead_marks_the_prioritised_project_too (tmp_path: pathlib.Path) -> None:
	"""The other dropdown, from the workspace it is already looping over — `SR#986`.

	Decision `SR#982` asks for both, and they are marked from different sources: this one reads
	the workspace beside it, where a form knows its projects and never its workspace.
	"""

	[shown] = _views(tmp_path, [("placesToGo", {
		"workspaces": [{
			"id": "1", "slug": "projects", "title": "Subroutine",
			"prioritised_project": "substation/dist",
		}],
		"projects": FILABLE,
		"showing": {"workspace": "projects"},
	})])
	labels = [one["label"] for one in shown]

	assert "Packaging (prioritised)" in labels, (
		"the masthead marks the project a workspace has prioritised, by its whole address"
	)
	assert "Substation" in labels, "and marks no ancestor of it"


def test_an_item_says_when_its_project_is_the_prioritised_one (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#986`, decision `SR#982` §4 — **and this is the one place an item may say it**.

	A task *row* says nothing, because 84% of this instance's open tasks are in the project most
	likely to be prioritised and §12.2a drops a mark that appears on nearly every row. A fact
	sheet is not a column: it describes one item, the mark appears once, and it answers *why is
	this ranked where it is* at the moment somebody is asking about that item.

	**Compared on the path rather than the key**, since `SR#958` made a key unique only among its
	siblings — two projects keyed `dist` would otherwise mark the wrong one.
	"""

	rendered = _rendered(tmp_path, {
		"Facts": {
			"item": {"ref": 1, "project_key": "dist", "project_path": "web/dist"},
			"prioritised": ["web/dist"],
		},
		"Listing": {
			"items": [{
				"ref": 1, "kind": "task", "title": "A task", "project_key": "dist",
				"project_path": "web/dist", "status_is_default": True,
			}],
			"prioritised": ["web/dist"],
		},
	})

	assert "dist (prioritised)" in rendered["Facts"], (
		f"the item does not say its project is the prioritised one: {rendered['Facts']}"
	)
	assert "(prioritised)" not in rendered["Listing"], (
		f"a row said it, and §12.2a drops a mark that says the same thing on every row: "
		f"{rendered['Listing']}"
	)


def test_an_item_in_another_project_says_nothing (tmp_path: pathlib.Path) -> None:
	"""The mark is about *this* project, which a fixture holding one prioritised cannot show."""

	[said] = _rendered(tmp_path, {
		"Facts": {
			"item": {"ref": 1, "project_key": "ops", "project_path": "ops"},
			"prioritised": ["web/dist"],
		},
	}).values()

	assert "(prioritised)" not in said, said


def test_a_column_that_is_over_starts_shut_and_a_reader_can_open_it (
	tmp_path: pathlib.Path,
) -> None:
	"""`#1008`, Simon 2026-08-18: cancelled and superseded work need not take the width all day.

	**Both directions of an explicit choice are asserted, and `false` is the one that matters.**
	A reader who opens *Cancelled* must have that survive the next render — otherwise the
	default reasserts itself, the column shuts under them, and the control appears inert. That
	is the half a set of collapsed keys cannot express, which is why what is stored is a map
	with two values rather than a list.

	**An empty column is deliberately left open.** `To do` with nothing in it is the answer
	somebody wanted, and `columns()` argues that an empty *In progress* reads as broken rather
	than as absent and is where you drag something to.
	"""

	answers = _ran(tmp_path, f"""
		import {{ collapsedColumns, CLOSED_BY_DEFAULT }} from "{_staged(tmp_path).as_uri()}";

		const board = ["todo", "in_progress", "done", "cancelled", "superseded", "archived"];
		const shut = (chosen) => [...collapsedColumns(board, chosen)].sort();

		console.log(JSON.stringify({{
			byDefault: shut(null),
			opened: shut({{ cancelled: false }}),
			closed: shut({{ todo: true }}),
			bothWays: shut({{ cancelled: false, done: true }}),
			empty: shut({{}}),
			over: [...CLOSED_BY_DEFAULT].sort(),
		}}));
	""")

	assert answers["byDefault"] == ["archived", "cancelled", "superseded"], (
		"work that is over should start shut, and nothing else should"
	)
	assert answers["empty"] == answers["byDefault"], "nothing remembered is not a choice"

	assert answers["opened"] == ["archived", "superseded"], (
		"a reader opened Cancelled and the default shut it again"
	)
	assert answers["closed"] == sorted(answers["byDefault"] + ["todo"])

	assert answers["bothWays"] == ["archived", "done", "superseded"], (
		"one explicit choice was honoured and the other was not"
	)

	assert "done" not in answers["over"], (
		"finished work is a selection nobody has by default, so a reader looking at a Done "
		"column has asked for it — and it is where 'Show finished work' lives"
	)


def test_a_collapsed_column_says_what_is_in_it_without_showing_it (
	tmp_path: pathlib.Path,
) -> None:
	"""`#1008`. Collapsed must not mean blind, or the reader cannot tell whether to open it.

	**The count is what makes the column honest**, and it is load-bearing rather than
	decoration: a shut column is the one place a row is fetched and not rendered, so the number
	is the whole of what says the rows are still there. It says *Not shown* instead where that
	is why the column is empty — a `0` beside a category nobody asked for is the same false
	statement `#742` established under an open one, said sideways.
	"""

	cancelled = {"ref": 7, "kind": "task", "title": "Abandoned experiment",
		"status_category": "cancelled"}
	live = {"ref": 8, "kind": "task", "title": "Still going", "status_category": "todo"}

	rendered = _rendered(tmp_path, {"Board": {
		"items": [cancelled, live], "workspace": "projects",
		"selection": {"include_completed": "true"},
	}})["Board"]

	assert "Still going" in rendered, "an open column stopped rendering its rows"
	assert "Abandoned experiment" not in rendered, (
		f"a column that starts shut rendered its contents: {rendered}"
	)

	shut = rendered.split("Cancelled")[1]

	assert shut.lstrip().startswith("<span>1"), (
		f"the shut column did not say how much it is holding: {rendered}"
	)


def test_a_reader_who_opens_a_column_is_remembered (tmp_path: pathlib.Path) -> None:
	"""`#1008`. `localStorage` per `#908`'s theme precedent, and defensive for its reasons.

	**Anything unrecognised reads as nothing remembered.** A value written by an older version
	or by somebody poking at storage must not put the board into a state no control can get it
	out of — and the failure is worse here than for a theme, because a wrongly shut column
	hides work rather than recolouring it.

	Storage throwing is the same answer rather than an exception: a private window can refuse,
	and a board that will not render because it could not read a preference is a worse bargain
	than one that renders with its defaults.
	"""

	answers = _ran(tmp_path, f"""
		import {{ collapsedChoices, rememberCollapsed }} from "{_staged(tmp_path).as_uri()}";

		const stored = (value) => ({{ getItem: () => value }});
		const broken = {{ getItem: () => {{ throw new Error("no storage here"); }} }};

		let written = null;
		const writable = {{
			getItem: () => written,
			setItem: (_key, value) => {{ written = value; }},
		}};

		rememberCollapsed({{ cancelled: false, superseded: true }}, writable);

		console.log(JSON.stringify({{
			roundTrip: collapsedChoices(writable),
			nothing: collapsedChoices(stored(null)),
			nonsense: collapsedChoices(stored("{{ not json")),
			array: collapsedChoices(stored("[1, 2]")),
			mixed: collapsedChoices(stored('{{"a": true, "b": "yes", "c": 3}}')),
			absent: collapsedChoices(undefined),
			broken: collapsedChoices(broken),
		}}));
	""")

	assert answers["roundTrip"] == {"cancelled": False, "superseded": True}, (
		"what was remembered did not come back, so a reader's choice lasts one render"
	)

	for state in ("nothing", "nonsense", "array", "absent", "broken"):
		assert answers[state] == {}, (
			f"{state} was read as a choice somebody made: {answers[state]}"
		)

	assert answers["mixed"] == {"a": True}, (
		"a value that is not a yes or a no is not an answer, and keeping it would put the "
		"board into a state no control can name"
	)


# ---- what kind of fact a mark is (`SR#1019`) -----------------------------------------------


def _families (marks: list[dict[str, typing.Any]]) -> list[tuple[str, str]]:
	"""Return each mark as ``(family, text)``, which is the pair this change is about."""

	return [(mark.get("family") or "", mark["text"]) for mark in marks]


def test_every_mark_says_which_family_it_belongs_to (tmp_path: pathlib.Path) -> None:
	"""`SR#1019`, Simon: *"it's hard to tell what kind of label each is."*

	**Eleven chips differed only by tone, so the *category* of a mark was carried by nothing.**
	A project address, a tag, a status and a state were one rounded lozenge, and the confusion
	he named — a sub-project sharing a tag's name — had nothing at all to separate the two.

	**Asserted as a family per mark rather than as a rendered class**, because the class is one
	template away and the decision is here: `marks` is the pure function every surface calls,
	so a mark that reaches a row without a family reaches four surfaces without one.

	`SR#102` is why this is reinforcement rather than information: every mark still says its
	word, so a reader in monochrome loses the grouping and nothing else.
	"""

	item = {
		"ref": 1, "kind": "task", "title": "Something", "type": "bug",
		"project_key": "ui", "project_path": "subroutine/ui", "workspace": "projects",
		"assignee": "si", "tags": ["ops", "security"],
		"status": "needs_input", "status_is_default": False,
		"blocked": True, "blocking": True, "recurrence_description": "every monday",
	}

	found = _addressing(tmp_path, [("marks", {"item": item, "showKind": False})])[0]

	assert _families(found) == [
		("identity", "bug"),
		("identity", "needs_input"),
		("state", "Blocked"),
		("state", "Blocker"),
		("state", "Repeats"),
		("address", "projects/subroutine/ui"),
		("address", "#ops"),
		("address", "#security"),
		("address", "@si"),
	], found

	# **Every mark, not most of them.** A family added to nine of ten reads as done and leaves
	# the tenth drawn as whatever `.mark` alone looks like, which is what this replaces.
	assert all(mark.get("family") for mark in found), f"a mark carries no family: {found}"


def test_a_tag_and_an_assignee_carry_the_sigil_a_person_would_type (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#1019`, Simon's decision: `#` for tags and `@` for people, and no `+` on a project.

	**Quick capture already reads all three**, so the chips say what somebody would type to
	reproduce them — which is the argument for sigils over shapes: no new vocabulary, legible
	in monochrome, and it separates a tag from a sub-project of the same name.

	**A project deliberately has no sigil.** Simon gave the reason as *the only linked item
	without one*, and `SR#1020` will make tags and assignees links too — so the reason expires
	and the decision does not: with `#` and `@` in place a bare word is already the third
	distinguishable thing, whatever is clickable.
	"""

	item = {
		"ref": 2, "kind": "task", "title": "Named", "workspace": "projects",
		"project_key": "ui", "project_path": "subroutine/ui",
		"tags": ["ops"], "assignee": "si",
	}

	found = _addressing(tmp_path, [("marks", {"item": item, "showKind": False})])[0]
	said = [mark["text"] for mark in found]

	# The whole address, because nothing was narrowed — `place` is null, so the label keeps
	# the workspace it would otherwise strip (decision `SR#957` §4).
	assert said == ["projects/subroutine/ui", "#ops", "@si"], said


def test_a_claim_says_who_holds_it_now_and_an_expired_one_says_nothing (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#1019`, Simon: *"'claimed by' better represents a current state than 'left it'."*

	**This reverses `SR#726`**, which drew an expired lease deliberately on the argument that
	*started and walked away from* is what somebody watching agents work most wants to see.
	What outweighed it: a chip reads as a property and *left it* is an event, whose home is the
	item's history.

	**It also ends a divergence.** `mcp/tools` reads `views.holder`, which applies the clock, so
	the agent has never shown an expired lease — the browser was the only surface that did, and
	the two now say the same words as well as the same thing.
	"""

	now = datetime.datetime.now(datetime.UTC)
	live = {
		"ref": 3, "kind": "task", "title": "Being done", "claimed_by_id": "u1",
		"claimed_by": "agent",
		"claim_expires_at": (now + datetime.timedelta(hours=1)).isoformat(),
	}
	expired = {**live, "claim_expires_at": (now - datetime.timedelta(hours=1)).isoformat()}

	held, gone = _addressing(tmp_path, [
		("marks", {"item": live, "showKind": False}),
		("marks", {"item": expired, "showKind": False}),
	])

	assert _families(held) == [("state", "claimed by @agent")], held
	assert gone == [], f"an expired lease still drew a mark: {gone}"


def test_a_status_stands_down_where_a_state_already_says_the_word (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#1019`, Simon's decision B. **One word, two facts, and a card said it twice.**

	The seeded status `blocked` means *declared, often outside the system* (`SR#96`); the
	derived state means *an unfinished blocker in the link graph* (`SR#425`). Both reach one
	card, and it read `Blocked Blocked` — two chips, two meanings, and nothing to tell a reader
	they were different things.

	**The state keeps the word** because the status is the workspace's own vocabulary and is not
	ours to rename (§5.5).

	**And it generalises for nothing extra**: a workspace that renames a status to `Deferred` or
	`Overdue` gets the same treatment without anybody adding a case, which is why this compares
	against what the state marks *say* rather than against a list of keys.
	"""

	both = {
		"ref": 4, "kind": "task", "title": "Waiting", "blocked": True,
		"status": "blocked", "status_is_default": False,
	}
	# The same shape, with a status no state mark says — the half that stops this being a rule
	# that hides every status.
	other = {**both, "status": "needs_input"}

	twice, kept = _addressing(tmp_path, [
		("marks", {"item": both, "showKind": False}),
		("marks", {"item": other, "showKind": False}),
	])

	assert _families(twice) == [("state", "Blocked")], twice
	assert _families(kept) == [
		("identity", "needs_input"), ("state", "Blocked"),
	], kept


def test_a_category_holding_one_status_is_what_lets_a_column_stop_repeating_it (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#1019`, Simon's decision A, and the measurement that ruled out the obvious answer.

	*"Items in the done column all have the done label — these seem superfluous."* True of
	**Done**, **Cancelled** and **Superseded**, each the only status in its category. **False of
	To do**, where `seed.py` puts `open`, `blocked` and `needs_input`, and only the first is the
	default — so suppressing the chip on boards wholesale would delete the one thing separating
	three states from the busiest column on the page.

	**Not computed from the rows either.** §12.2a's drop-if-uniform would answer *Done*
	correctly by looking at what is in it — and decision `SR#957` §4 refused that for the
	browser, because the page polls and a chip appearing as a column fills is `SR#966`'s shape.
	Asking the *vocabulary* is stable: it cannot change while nobody edits it.

	**Unknown vocabulary keeps the chip.** `words` clears it before fetching and treats its own
	failure as survivable (§1.4), so null is a state a working page reaches, and hiding a fact
	because a request is in flight is the wrong direction to fail.
	"""

	statuses = {
		"task": [
			{"key": "open", "category": "todo", "is_default": True},
			{"key": "blocked", "category": "todo"},
			{"key": "needs_input", "category": "todo"},
			{"key": "done", "category": "done"},
		],
	}

	answers = _addressing(tmp_path, [
		("soleStatusIn", {"vocabulary": statuses, "kind": "task", "category": "done"}),
		("soleStatusIn", {"vocabulary": statuses, "kind": "task", "category": "todo"}),
		("soleStatusIn", {"vocabulary": None, "kind": "task", "category": "done"}),
		("soleStatusIn", {"vocabulary": statuses, "kind": "task", "category": "invented"}),
	])

	assert answers == [True, False, False, False], answers


def test_a_board_column_that_names_one_status_does_not_repeat_it_on_every_card (
	tmp_path: pathlib.Path,
) -> None:
	"""The wiring, driven — `SR#1019`. The rule above is right and reaches nothing by itself.

	**`SR#640`'s lesson, five times over in this file**: a pure function can be correct, the
	display correct, and nothing joining them. So this renders the board and reads the cards.
	"""

	statuses = {
		"task": [
			{"key": "open", "category": "todo", "is_default": True},
			{"key": "needs_input", "category": "todo"},
			{"key": "done", "category": "done"},
		],
	}
	finished = {
		"ref": 5, "kind": "task", "title": "Over", "status": "done",
		"status_is_default": False, "status_category": "done",
	}
	waiting = {
		"ref": 6, "kind": "task", "title": "Waiting", "status": "needs_input",
		"status_is_default": False, "status_category": "todo",
	}

	rendered = _rendered(tmp_path, {
		"Board": {"items": [finished, waiting], "workspace": "projects", "statuses": statuses},
	})["Board"]

	assert "needs_input" in rendered, (
		f"the To do column dropped a status its own name does not say: {rendered}"
	)
	assert "done" not in rendered.replace("Done", ""), (
		f"the Done column repeated its own name on every card: {rendered}"
	)
