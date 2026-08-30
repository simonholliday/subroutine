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
import json
import pathlib
import typing
import urllib.parse

import fastapi
import starlette.exceptions
import starlette.requests
import starlette.responses

import subroutine.api.dependencies
import subroutine.api.problems
import subroutine.api.routing
import subroutine.config
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

#: What this product is called wherever it has to name itself to an operating system.
#:
#: The shell's ``<title>`` says the same word, and a test holds the two together — an installed
#: app whose launcher label disagreed with the page it opens would be one product wearing two
#: names on one device.
PRODUCT = "Subroutine"

#: The two addresses an installable app needs, and neither is a file on disk (`#1665`).
#:
#: **Written here rather than only in the shell**, so the page's ``<link>`` and the routes that
#: answer it can be held to one spelling. A manifest a browser cannot find is not an error and
#: reaches no log: the install offer simply never appears, which is the whole of what this item
#: was about.
MANIFEST = "manifest.webmanifest"
WORKER = "sw.js"

#: What a phone paints behind the app while it starts.
#:
#: **This is not the static colour a manifest ``theme_color`` was refused for.** Leaving it out
#: does not avoid choosing one — it accepts the browser's, which is white — so the only question
#: is *which* static value, and the light theme's own ground is a better answer than a generic
#: one. `#908`'s dark reader still meets a light splash for the moment it takes to start, and
#: there is no manifest field that could follow a theme.
#:
#: It is ``--bg-sunken``'s light value, and ``tests/test_web.py`` reads the stylesheet rather
#: than trusting this comment: a colour written out twice is this codebase's signature defect,
#: and the copy nobody renders is the one that goes stale.
SPLASH = "#f6f7f9"


def app_names (settings: subroutine.config.Settings) -> tuple[str, str]:
	"""Return what an installed app calls itself: the full name, then the launcher label.

	**A ``public_url`` decides whether the address appears in the name**, and the conditional is
	this codebase's own rather than a new one. ``diagnosis._the_settings`` reads a set
	``public_url`` as *this instance is reachable by somebody other than the person at the
	keyboard*, and that is exactly the population that has more than one of them — a promoted
	instance (`#1254`), a colleague's, a hosted one beside a self-hosted one. An instance nobody
	else can reach is by construction the only one on the phone that installed it, so it gets
	the clean label.

	**The address rather than ``Instance.name``, and the reason is who may read this.** That
	field exists, is editable, and defaults from ``/etc/hostname`` — so it is the better answer
	on a surface that needs a credential, which is `#1666`. A manifest is fetched by a ``<link>``
	before anybody has signed in, from a route ``PUBLIC_ROUTES`` records as answering to
	everybody; putting the machine's hostname in it would publish to anonymous callers the one
	value `#1344` forbids in a tracked file. The address costs nothing, because whoever is
	reading this typed it to get here.
	"""

	told = (settings.public_url or "").strip()

	try:
		parsed = urllib.parse.urlsplit(told)
		host = parsed.hostname or ""
		port = parsed.port

	# **Not reachable through a loaded ``Settings``**, which validates the address at startup
	# through :func:`subroutine.config.public_url_fault` — and caught anyway, because a label
	# on a home screen is not worth a 500 and this function is handed its argument by callers
	# a validator does not stand in front of.
	except ValueError:
		return (PRODUCT, PRODUCT)

	if not host:
		return (PRODUCT, PRODUCT)

	where = f"{host}:{port}" if port else host

	# **The address is the *short* name**, which is the one a launcher writes under the icon —
	# so the thing that tells two instances apart is put where two icons sit side by side. The
	# full name is what the install prompt and the app switcher show, where there is room to
	# say what the product is as well as which one.
	return (f"{PRODUCT} ({where})", where)


def manifest_for (settings: subroutine.config.Settings) -> dict[str, typing.Any]:
	"""Return what this instance tells a phone about installing its browser app.

	**``display: standalone``** is Simon's decision of 2026-08-30: an installed app opens
	without the browser's chrome, which is what makes it read as an app rather than as a
	bookmark. The cost is stated on `#1665` — the address bar is where a reader could otherwise
	see which instance they are on, which is why :func:`app_names` puts it in the label.

	**The icons declare no ``purpose``, deliberately.** ``assets/favicon.md`` records the tile
	mark sitting at 78-86% of its grid, and a maskable icon's safe zone is the inner *circle* —
	so claiming ``maskable`` would have an adaptive launcher crop the mark's corners on every
	Android home screen. Left unset, the platform puts the tile on its own plate, which is what
	the tile was drawn for.

	**Nothing here is derived from the database**, which keeps the promise this module's own
	docstring makes: the same bytes for every caller, signed in or not.
	"""

	name, short = app_names(settings)

	return {
		"name": name,
		"short_name": short,
		"start_url": "/",
		"scope": "/",
		"display": "standalone",
		"background_color": SPLASH,
		"icons": [
			{"src": "/app/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"},
			{"src": "/app/icon-512-on-black.png", "sizes": "512x512", "type": "image/png"},
		],
	}

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
	SHA-256 is far past the point where two of the files this app serves could collide, and
	the whole value travels on every request and every revalidation.
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
	f"/app/{MANIFEST}",
	summary="What this instance is called when it is installed as an app",
	include_in_schema=False,
)
def manifest (
	request: starlette.requests.Request,
	settings: subroutine.api.dependencies.SettingsDep,
) -> starlette.responses.Response:
	"""Answer with the web app manifest, which is what makes the page installable.

	**Generated rather than a file in ``assets``**, because the name has to say which instance
	this is and only the running process knows its own address. That is the whole reason this
	is a route and its neighbours are bytes read at import.

	``include_in_schema=False`` for the reason :func:`shell` gives: a browser's manifest in the
	OpenAPI document is a row every generated client has to be told to ignore.
	"""

	# Tab-indented so that a person who opens it reads it the way they read everything else
	# this project serves, and a trailing newline for the same reason.
	body = json.dumps(manifest_for(settings), indent="\t").encode("utf-8") + b"\n"
	tag = _tag(body)

	headers = {"cache-control": REVALIDATE, "etag": tag}

	# **Revalidated like every other file** (`#914`). A manifest a browser holds on to is how a
	# home screen comes to carry an icon and a name from a version nobody is running, and it is
	# the one asset a person cannot refresh — reinstalling the app is the only way back.
	if _asked_for(request, tag):
		return starlette.responses.Response(status_code=304, headers=headers)

	return starlette.responses.Response(
		content=body, media_type="application/manifest+json", headers=headers
	)


@router.get(
	f"/app/{WORKER}",
	summary="The browser app's service worker",
	include_in_schema=False,
)
def worker (request: starlette.requests.Request) -> starlette.responses.Response:
	"""Serve the service worker, and say that it may control the whole site.

	**A route of its own for one header.** A worker's default scope is the directory it is
	served from, so ``/app/sw.js`` would control ``/app/`` and not the page at ``/`` — which is
	the page it exists to make installable. ``Service-Worker-Allowed`` is what lets it claim a
	wider scope than its own address, and the registration in ``app.js`` asks for exactly that.

	**The alternative was serving it at ``/sw.js``**, where the default scope would be right and
	no header would be needed. It is not that, because *everything the app is made of is served
	flat at ``/app/<name>``* is an invariant three tests and the stylesheet check already read —
	and one file living somewhere else for a reason nobody would guess is worse than a header
	whose absence fails loudly, with a ``SecurityError`` naming the scope.
	"""

	answer = _served(WORKER, request)
	answer.headers["service-worker-allowed"] = "/"

	return answer


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
