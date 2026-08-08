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
	".js": "text/javascript; charset=utf-8",
	".svg": "image/svg+xml",
}

#: How long a browser may keep an asset. Short, and deliberately: these files change with every
#: release and there is no content hash in their names to invalidate them, so a long cache is a
#: user looking at last week's app with no way to know it. `#380` is the same defect one layer
#: out — a cached copy of a plugin that predated the feature it was installed for.
CACHE_SECONDS = 300


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


@router.get(
	"/",
	summary="The browser app",
	response_class=starlette.responses.HTMLResponse,
	include_in_schema=False,
)
def shell () -> starlette.responses.Response:
	"""Serve the page a person opens.

	``include_in_schema=False`` because the OpenAPI document describes an API for programs, and
	an HTML page in it is a row every generated client has to be told to ignore.
	"""

	body, kind = FILES[SHELL]

	return starlette.responses.Response(content=body, media_type=kind)


@router.get(
	"/app/{name}",
	summary="One of the browser app's files",
	include_in_schema=False,
)
def asset (name: str) -> starlette.responses.Response:
	"""Serve one asset by name, or refuse a name that is not one of them."""

	found = FILES.get(name)

	if found is None:
		raise subroutine.errors.NotFound(
			f"This instance serves no file called {name!r}.",
			hint="The browser app's files are fixed at build time; a missing one usually "
			"means a page was cached from an older release. Reload without the cache.",
		)

	body, kind = found

	return starlette.responses.Response(
		content=body,
		media_type=kind,
		headers={"cache-control": f"public, max-age={CACHE_SECONDS}"},
	)


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
