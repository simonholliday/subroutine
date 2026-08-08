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
import starlette.responses

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
