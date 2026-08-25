"""Serving the browser app — item `#597`.

**Ordinary routes rather than a static-files mount, and that is the whole design decision
here.** A ``StaticFiles`` mount is attached to the application rather than to a router, so it
would appear in none of the walks this project's guards are built on: ``routing.check`` reads
``ROUTERS``, ``test_api_authentication`` walks the same list to prove every route either needs
a credential or is written down as public, and ``test_reach`` classifies every route as reached
or excused. A mount is invisible to all three. Two routes that the guards can see are worth
more than a mount that is one line shorter.

**The assets are read once, at import, into a frozen map.** So there is no filesystem access
per request and no path a caller supplies ever reaches a filesystem call — which is the whole
of the directory-traversal question, answered by construction rather than by sanitising.

**Public, necessarily.** The page has to load before anybody can sign in; the *data* it then
asks for needs the cookie like everything else. Nothing here is workspace-scoped, personal, or
derived from a database — it is the same bytes for every caller, signed in or not.
"""

import hashlib
import pathlib
import typing

import fastapi
import starlette.exceptions
import starlette.requests
import starlette.responses

import subroutine.api.problems
import subroutine.api.routing
import subroutine.errors
import subroutine.web.vendored

router = fastapi.APIRouter(tags=["app"], route_class=subroutine.api.routing.Transactional)

#: Where the app's own files live.
ASSETS = pathlib.Path(subroutine.web.vendored.__file__).resolve().parent / "assets"

#: What each extension is served as. A short map rather than ``mimetypes``, because the set is
#: closed and known: a browser handed ``text/plain`` for a module refuses to execute it, and
#: the platform's own table is a thing that varies by machine.
TYPES = {
	".css": "text/css; charset=utf-8",
	".html": "text/html; charset=utf-8",
	# **The two bitmap kinds are the mark's, and until `#1286` neither was here** — so
	# dropping a PNG into ``assets`` served nothing and nothing failed, because
	# :func:`_collected` skips an unknown suffix in silence. That is the shape this codebase
	# calls a control that grants nothing, met from the other side: a *closed* map is only
	# safe while somebody notices what it closes out.
	#
	# ``image/x-icon`` rather than the registered ``image/vnd.microsoft.icon``: it is what
	# every browser has read for twenty years, and the registered name is refused by some of
	# them. This is exactly the case the comment above is about — the platform's table would
	# have answered differently on different machines.
	".ico": "image/x-icon",
	".js": "text/javascript; charset=utf-8",
	".png": "image/png",
	".svg": "image/svg+xml",
}

#: What every page this instance serves puts in its head to declare the mark (`#1286`).
#:
#: **Declared once because two pages carry it** — the app shell and the sign-in page, which is
#: the first thing anybody handed a login link ever sees. They were two copies of one
#: ``<link>``, so changing the mark on one left the old one on the surface a new user meets
#: first: `#583`/`#674`'s defect, on a line nobody would think to compare.
#:
#: **The sign-in page interpolates this; ``index.html`` is a static file and cannot**, so
#: ``tests/test_web.py`` reads both and fails if they part company. One authored copy and one
#: checked copy is what is available here, and it is enough.
#:
#: **``-on-black``, which is what *white on black* names.** The transparent ``-inverted`` files
#: are a white mark on nothing and disappear against the light chrome most people run; a solid
#: tile reads on any tab colour. Both sets are in the tree.
#:
#: **The ``.ico`` first and the SVG second, deliberately.** A browser takes the last one it
#: understands, so the vector wins wherever it is supported and the bitmap is what is left for
#: anything that does not — which is the order the exporter wrote and the opposite of the
#: order that would work.
ICON_LINKS = (
	'<link rel="icon" href="/app/favicon-on-black.ico" sizes="16x16 32x32 48x48">\n'
	'<link rel="icon" href="/app/favicon-on-black.svg" type="image/svg+xml">\n'
	'<link rel="apple-touch-icon" href="/app/apple-touch-icon.png">'
)

#: What a browser is told about keeping these files (`#914`).
#:
#: **``no-cache`` does not mean do not store — it means revalidate before use**, which with the
#: tag below is exactly the promise wanted: an unchanged file costs a couple of hundred bytes
#: and a changed one arrives on the next load, with no hard refresh and nothing to remember.
#:
#: **It replaced ``public, max-age=300``, which was the worst of both.** For five minutes a
#: browser did not ask, so a restarted server served current bytes to a page that would not
#: request them; after five minutes it re-downloaded the *whole file*, because with no validator
#: a ``304`` was impossible. `CACHE_SECONDS` is deleted rather than left unread — `#303`'s
#: precedent, and its own comment already named this risk: *a long cache is a user looking at
#: last week's app with no way to know it*.
#:
#: **A query parameter on the stylesheet's ``href`` was the other candidate and is not this**
#: (Simon's suggestion, and the reasoning is on `#914`). A version busts only what the shell
#: names — the import map's modules keep their own copies — and it does not move when a file
#: changes without a commit, which is most of development. A tag derived from the bytes cannot
#: have either fault.
REVALIDATE = "no-cache"


def _tag (body: bytes) -> str:
	"""Return a strong entity tag for exactly these bytes.

	Truncated because a tag is an opaque identity rather than a checksum — sixteen bytes of
	SHA-256 is far past the point where two of this app's six files could collide, and the whole
	value travels on every request and every revalidation.
	"""

	return f'"{hashlib.sha256(body).hexdigest()[:32]}"'


def _collected () -> dict[str, tuple[bytes, str]]:
	"""Read every file the app is made of, once, at import.

	Both directories are walked rather than listed, so adding an asset is dropping a file in —
	and :mod:`subroutine.web.vendored` is what says which of them we did not write.
	"""

	found: dict[str, tuple[bytes, str]] = {}

	for directory in (ASSETS, subroutine.web.vendored.DIRECTORY):
		for path in sorted(directory.iterdir()):
			if not path.is_file() or path.suffix not in TYPES:
				continue

			found[path.name] = (path.read_bytes(), TYPES[path.suffix])

	return found


#: Name to bytes and content type. Frozen at import: a caller's path is *looked up* here and
#: never joined to a directory, so ``/app/../../etc/passwd`` is a miss rather than a traversal.
FILES: typing.Final[dict[str, tuple[bytes, str]]] = _collected()

#: The page itself. Named here so the route below and the tests agree on one spelling.
SHELL = "index.html"

#: A tag per file, computed once from the same bytes that are served.
#:
#: **Derived rather than declared, which is the whole point** (`#914`): the tag *is* the content,
#: so it changes when a file changes and never when it does not. Nothing to bump, nothing to
#: forget, and no version that could be right about the package while wrong about the file.
TAGS: typing.Final[dict[str, str]] = {name: _tag(body) for name, (body, _) in FILES.items()}


def _asked_for (request: starlette.requests.Request, tag: str) -> bool:
	"""Whether the caller already holds this exact version.

	``If-None-Match`` is a list, and a cache is allowed to weaken a tag it stored — so the
	comparison strips ``W/`` and reads every entry. Comparing the raw header would answer *no*
	for a browser holding the right file, which is a correct-but-wasteful answer that looks
	exactly like the header working.
	"""

	offered = request.headers.get("if-none-match", "")

	if offered.strip() == "*":
		return True

	return any(one.strip().removeprefix("W/") == tag for one in offered.split(","))


def _served (name: str, request: starlette.requests.Request) -> starlette.responses.Response:
	"""Answer with one of the app's files, or with ``304`` if the caller has it already."""

	body, kind = FILES[name]
	tag = TAGS[name]

	headers = {"cache-control": REVALIDATE, "etag": tag}

	# **A `304` carries the validators and no body**, which is what makes revalidation cheap
	# enough to do on every load — the alternative to this whole arrangement was a five-minute
	# window in which a changed file could not arrive at all.
	if _asked_for(request, tag):
		return starlette.responses.Response(status_code=304, headers=headers)

	return starlette.responses.Response(content=body, media_type=kind, headers=headers)


@router.get(
	"/",
	summary="The browser app",
	response_class=starlette.responses.HTMLResponse,
	include_in_schema=False,
)
def shell (request: starlette.requests.Request) -> starlette.responses.Response:
	"""Serve the page a person opens.

	``include_in_schema=False`` because the OpenAPI document describes an API for programs, and
	an HTML page in it is a row every generated client has to be told to ignore.

	**Revalidated like every other file, and it matters most here**: this document is
	what names the assets, so a stale copy of it cannot be corrected by anything downstream.
	Whatever busts a cache must not itself be cached.
	"""

	return _served(SHELL, request)


@router.get(
	"/app/{name}",
	summary="One of the browser app's files",
	include_in_schema=False,
)
def asset (name: str, request: starlette.requests.Request) -> starlette.responses.Response:
	"""Serve one asset by name, or refuse a name that is not one of them."""

	if name not in FILES:
		raise subroutine.errors.NotFound(
			f"This instance serves no file called {name!r}.",
			hint="The browser app's files are fixed at build time; a missing one usually "
			"means a page was cached from an older release. Reload without the cache.",
		)

	return _served(name, request)


#: What a browser sends when it is asking for a page, and no client of this API sends.
#: ``clients/http.py`` sends ``application/json``; the app's own ``fetch`` sends the same;
#: ``curl`` sends ``*/*``. Only a navigation asks for HTML by name, which is why the test is for
#: the literal type rather than for "anything".
NAVIGATION = "text/html"


def _navigating (request: starlette.requests.Request) -> bool:
	"""Whether this is a person's browser asking for a page, rather than a program for data."""

	return NAVIGATION in request.headers.get("accept", "").lower()


def unmatched (
	request: starlette.requests.Request, exception: Exception
) -> starlette.responses.Response:
	"""Answer a request nothing else claimed — with the app, for a browser (`#638`, `#647`).

	**A fallback rather than a route, and that distinction cost a day to arrive at.** The first
	version declared ``/{workspace}``, ``/{workspace}/{project}`` and two more, which is the
	obvious way to do it and is wrong in a way that gets worse quietly:

	* ``/{workspace}/{project}`` matched ``/v1/nothing``, so **the API's 404 stopped being a
	  problem document** and became ``200 text/html``. Five tests said so and they were right —
	  a mistyped path answering with a page is `#379`'s "plausible, complete, wrong answer" on
	  the surface this project's primary audience uses.
	* ``/{workspace}`` matched ``GET /mcp``, which declares only ``POST``, turning a ``405``
	  that had been *measured* against a real client into a page (`#648`).
	* And it shadowed any route registered after it, for ever, which no amount of care survives:
	  ``api_support`` adds routes to a built application, and those went unreachable too.

	None of that is fixable by ordering, because the hazard *is* claiming paths nobody has
	claimed yet. Answering the 404 instead inverts it: every real route wins, always, whenever
	it was registered — and what is left over is by definition unclaimed.

	``Accept`` is what separates the two readers. A browser navigating asks for ``text/html``
	by name; every client of this API asks for ``application/json``. So a program still gets
	the problem document it has always got, byte for byte, and a person gets the page.
	"""

	http = typing.cast(starlette.exceptions.HTTPException, exception)

	if http.status_code == 404 and _navigating(request):
		body, kind = FILES[SHELL]

		return starlette.responses.Response(content=body, media_type=kind)

	return subroutine.api.problems.handle_http_exception(request, http)


def install (application: fastapi.FastAPI) -> None:
	"""Serve the app for any address nothing else claimed.

	**After** :func:`subroutine.api.problems.install`, because Starlette keys handlers by
	exception class and this one replaces that module's — it delegates to it for everything
	except the case above.
	"""

	application.add_exception_handler(starlette.exceptions.HTTPException, unmatched)
