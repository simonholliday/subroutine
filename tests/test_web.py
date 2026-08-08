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

import json
import pathlib
import re
import shutil
import subprocess
import textwrap
import typing
import uuid

import fastapi
import httpx
import pytest
import sqlalchemy
import sqlalchemy.orm
import starlette.requests

import api_support
import subroutine.api.app
import subroutine.api.routing
import subroutine.api.web
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.web.vendored

ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSETS = subroutine.api.web.ASSETS

#: Where a copied file that is **only ever run by a test** lives.
#:
#: Not beside the served ones, and that is the point: ``api/web._collected`` *walks* the vendor
#: directory, so a file dropped in there is served to every reader — 9 KB of something the page
#: never imports, inside the set a reader is invited to audit.
TEST_VENDOR = pathlib.Path(__file__).resolve().parent / "vendor"

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
	),
)

#: Sample props for every component that takes them, shaped like the API's real answers —
#: which were read off a live instance rather than invented, because a component fed a shape
#: nobody serves is a test of a shape nobody serves.
SAMPLES: dict[str, dict[str, typing.Any]] = {
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
	},
	"Listing": {
		"items": [
			{"ref": 1, "kind": "task", "title": "A task", "status_is_default": True},
			{"ref": 2, "kind": "document", "title": "A document", "status_is_default": True},
		]
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
	},
	"Failed": {"error": {"status": 500, "message": "Something went wrong."}},
	"Adding": {"busy": False},
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
	"""Return a JavaScript runtime, or skip.

	**Skipped rather than faked.** A stub that pretended to render would be a test that cannot
	fail, which is the shape this project has been bitten by most; a skip at least says out
	loud that nothing was checked here.
	"""

	found = shutil.which("node")

	if found is None:
		pytest.skip("no JavaScript runtime on PATH, so the app cannot be rendered")

	return found


def _staged (tmp_path: pathlib.Path) -> pathlib.Path:
	"""Lay the served app out so Node can import it, and return its entry module.

	The bare specifiers are rewritten to file paths, which is exactly what the import map in
	``index.html`` does for a browser — so what runs is the file that is served, with its
	imports resolved the same way rather than transformed.
	"""

	vendor = subroutine.web.vendored.DIRECTORY
	staged = tmp_path / "staged"
	staged.mkdir(exist_ok=True)

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

			return typeof node.type === "string" ? `<${{node.type}}>${{inner}}` : inner;
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


@pytest.mark.parametrize(
	("entry", "directory"),
	[(entry, subroutine.web.vendored.DIRECTORY) for entry in subroutine.web.vendored.CATALOGUE]
	+ [(entry, TEST_VENDOR) for entry in TEST_ONLY],
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
#: The last four are the ones a prefix test on the scheme lets through, which is why the
#: scheme is parsed instead: two spellings of `javascript:` that a browser accepts and a
#: literal check does not, a protocol-relative address that looks like a path, and a `data:`
#: document that is same-origin in some browsers.
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
				index = source.find("\n", index)

				if index < 0:
					break

				continue

			if char == "/" and following == "*":
				index = source.find("*/", index) + 2

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
			index += 2

			continue

		if char == quote:
			quote, kept = None, [*kept, " "]
			index += 1

			continue

		if quote == "`" and char == "$" and following == "{":
			quote, index = None, index + 2
			depth.append(1)
			kept.append(" ")

			continue

		index += 1

	return "".join(kept)


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
			scheme = href.split(":", 1)[0].lower() if ":" in href else ""

			assert scheme in ("", "http", "https", "mailto"), f"{source!r} linked to {href!r}"
			assert not href.startswith("//"), f"{source!r} linked off-origin as {href!r}"


def test_a_refused_link_shows_what_was_written (tmp_path: pathlib.Path) -> None:
	"""A destination we will not follow is rendered as its source, not quietly dropped.

	Dropping the destination and keeping the text would tell the reader there was never a
	link there, which is a false statement about what somebody wrote — and it is the reading
	under which a suspicious link becomes invisible rather than obvious.
	"""

	[rendered] = _markdown(tmp_path, ["[click](javascript:alert(1))"])

	assert "<a " not in rendered
	assert "javascript:alert(1)" in rendered, "the destination stopped being visible"


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


def test_a_completed_task_is_not_asked_to_complete_again (tmp_path: pathlib.Path) -> None:
	"""`status_category` is the fixed field a client may branch on; the status key is renameable."""

	rendered = _rendered(tmp_path, {
		"Doing": {
			"item": {"ref": 42, "kind": "task", "title": "Done already",
				"status_category": "done", "assignee": None},
			"members": ["si"],
		}
	})

	assert rendered["Doing"] == "", "a finished task still offered to be finished"


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
	source = _served_modules()["app.js"]
	placeholder = re.search(r'placeholder="([^"]+)"', source)

	assert placeholder is not None, "the add box stopped saying what can be typed into it"
	assert "+" in placeholder.group(1) and "!" in placeholder.group(1), (
		f"the placeholder {placeholder.group(1)!r} no longer shows any of the grammar"
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
			: app.addressOf(argument.item, argument.workspace))));
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

	for _prefix, router in subroutine.api.app.ROUTERS:
		for route in router.routes:
			path = getattr(route, "path", "")

			assert ":path}" not in path, (
				f"{path} is a catch-all and can shadow whatever is registered after it"
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
	assert app.count("setError(failure)") == 3, (
		"the number of places that replace the whole page changed — each one should be a case "
		"where nothing on screen is worth keeping"
	)


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


def _function_body (source: str, name: str) -> str:
	"""Return the body of one top-level function in the app, by name."""

	start = source.index(f"export function {name} (")
	depth = 0

	for index in range(source.index("{", start), len(source)):
		if source[index] == "{":
			depth += 1
		elif source[index] == "}":
			depth -= 1

			if depth == 0:
				return source[start:index]

	raise AssertionError(f"{name} never closes")


def test_a_listing_asks_for_every_field_its_rows_render () -> None:
	"""**The `fields=` list is a second copy of what a row shows, so it is derived, not trusted.**

	`SR#645`: a whole page of tasks is 287 KB and a whole page of documents is 1.3 MB, because a
	document's body arrives in full. Asking only for what a row renders makes the pair 38 KB.
	The cost of that is a list somebody has to keep in step with `Row`.

	**And forgetting it does not error.** An unrequested field arrives as `null`, and a null
	reads as *not set* — which is the rule `subroutine show` and `Facts` are built on (§12.2c).
	So a row would quietly stop saying an item is blocked, or overdue, or whose it is, and look
	exactly like an item that is none of those.

	Derived from the four functions that are a row's whole surface, so adding a field to any of
	them fails here until the request asks for it.
	"""

	source = _served_modules()["app.js"]
	rendered = set()

	for name in ("Row", "marks", "when", "overdue"):
		rendered |= set(re.findall(r"\bitem\.([a-z_][a-z0-9_]*)\b", _function_body(source, name)))

	asked = set(re.findall(r'"([a-z_]+)"', source[
		source.index("const TASK_FIELDS = ["):source.index("].join(\",\");", source.index("const TASK_FIELDS = ["))
	]))

	assert rendered, "no fields were found, so this is checking nothing"
	assert asked, "the task field list was not found, so this is checking nothing"

	# `kind` is added by the app after the answer arrives — it says which collection a row came
	# from, and no endpoint reports it.
	missing = rendered - asked - {"kind"}

	assert not missing, (
		f"a row renders {sorted(missing)}, and the listing does not ask for {'it' if len(missing) == 1 else 'them'} — "
		f"so every row will show that as absent rather than as unknown"
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
	"""

	built = _built(tmp_path, [("listingRequests", ["personal", None, None])])

	assert len(built) == 2, f"expected the two listing requests, found {built}"

	for request in built:
		assert "order=" not in request["path"], (
			f"{request['path']} chooses an order, and `subroutine list` does not — so the same "
			f"question has two answers, and the one that hid SR#642 was this one"
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
	narrow = _rendered(tmp_path, {"Listing": {"items": rows, "project": "ui"}})

	assert "Showing" not in whole["Listing"], "an unfiltered list claimed to be filtered"
	assert "ui" in narrow["Listing"], "the list did not say what it was narrowed to"
	assert "Show everything" in narrow["Listing"], "there was no way back to the workspace"


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
	document: int
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
		return api_support.call(
			application, method, path, headers={"authorization": f"Bearer {secret}"}, **kwargs
		)

	slug = setup.workspace.slug
	scope = f"?workspace_id={slug}"

	made = call("POST", f"/v1/projects{scope}", json={"key": "web", "title": "The browser"})
	assert made.status_code == 201, made.text

	# Two tasks, so a page of one has something after it and the cursor below is a real one.
	refs = []

	for title in ("Read the backlog", "Write it down"):
		answer = call("POST", f"/v1/tasks{scope}", json={"text": f"{title} +web"})
		assert answer.status_code == 201, answer.text
		refs.append(answer.json())

	document = call("POST", f"/v1/documents{scope}", json={"title": "A note", "body": "Prose."})
	assert document.status_code == 201, document.text

	page = call("GET", f"/v1/tasks{scope}&limit=1")
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
		document=document.json()["ref"],
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


def _calls (place: Instance) -> list[tuple[str, list[typing.Any]]]:
	"""Every request this app can make, with arguments naming things that exist.

	One entry per *shape* rather than per builder: a listing narrowed to a project and a listing
	that is not are different requests, and the narrowing is where the last two faults were.
	"""

	return [
		("identityRequest", []),
		("headRequest", []),
		("pollRequest", [place.slug, place.since]),
		# **The instance nobody has used yet**, which has no events and so no seq to resume
		# from. `SR#656` was exactly this shape, and the poll's own habit of swallowing
		# failures is what made it permanent.
		("pollRequest", [place.slug, None]),
		("rosterRequest", [place.slug]),
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
		("addRequest", ["Something new", place.slug]),
	]


def test_every_request_the_browser_makes_is_one_the_instance_accepts (
	tmp_path: pathlib.Path, instance: Instance
) -> None:
	"""**The whole point of `SR#640`: the request is built here and answered by the real app.**

	`api/query.py` refuses a query parameter a route did not declare, bodies refuse an unknown
	field, and several parameters are refused for what they *mean* rather than for their
	spelling — `subtree` needs a `parent`, and sending it beside `project` is a 422. None of
	those can be checked by reading the source, and all of them shipped.

	A refusal here is not a failing test about HTTP. It is the page a reader would have got.
	"""

	for request in _built(tmp_path, _calls(instance)):
		answer = instance.call(
			request["method"], f"/v1{request['path']}",
			**({"json": request["body"]} if request.get("body") is not None else {}),
		)

		assert answer.status_code < 400, (
			f"{request['from']} builds {request['method']} {request['path']}, and the instance "
			f"answered {answer.status_code}: {answer.text[:400]}"
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
		task=1, document=2, username="si", status="open", cursor="c", since=1,
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
		import {{ h }} from "{(tmp_path / "staged" / "preact.js").as_uri()}";
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


def test_the_merge_agrees_with_the_server_about_a_tie (tmp_path: pathlib.Path) -> None:
	"""The server's tiebreaker follows the last key's direction, so a tie is `ref` descending.

	`domain/ordering.terms` appends it always — *"so that equal values keep one stable order and
	'newest first' stays newest first among rows that tie"*. A client breaking it the other way
	would disagree with the boundary it is paging across.
	"""

	rows = [
		{"ref": 7, "created_at": "2026-08-08T09:00:00+00:00"},
		{"ref": 9, "created_at": "2026-08-08T09:00:00+00:00"},
		{"ref": 8, "created_at": "2026-08-08T09:00:00+00:00"},
	]

	assert _ordered(tmp_path, rows) == [9, 8, 7]


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
	adding = [
		body for holder, body in re.findall(r"setItems\((\w+)\) => ([^;]+)\);", app)
	] + [
		body for holder, body in re.findall(r"setItems\(\((\w+)\) => ([^;]+)\);", app)
	]

	assert adding, "nothing sets the list at all, so this is checking nothing"

	for body in adding:
		if "..." not in body:
			continue

		assert "newestFirst" in body, (
			f"a page is added to the list without being merged into it: {body.strip()!r} — "
			f"a second page of tasks belongs above documents already on screen"
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
