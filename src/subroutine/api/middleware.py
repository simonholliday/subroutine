"""The two headers every response carries, and the request id that ties a failure to a log.

``X-Request-Id`` is the only thread between a 500 a caller saw and the traceback that
explains it: the response says nothing about what went wrong, deliberately, so the id is
what makes the failure diagnosable at all (docs/design.md §8.1). ``X-Subroutine-Api-Version``
tells a client which wire contract it is talking to without a round trip to ``/v1/meta``.
"""

import json
import re
import typing

import starlette.datastructures
import starlette.middleware.gzip
import starlette.requests
import starlette.responses
import uuid6

import subroutine
import subroutine.api.routing
import subroutine.errors

REQUEST_ID_HEADER = "X-Request-Id"
API_VERSION_HEADER = "X-Subroutine-Api-Version"

#: A caller may bring its own correlation id, which is how a request keeps one identity
#: across a proxy, a queue and this service. It is echoed back, so it is accepted only if
#: it is plausibly an id: an unbounded value is free amplification, and one containing a
#: newline is a response-splitting attempt. Anything else is quietly replaced rather than
#: refused — the caller asked for a task, not a debate about its header.
_PLAUSIBLE_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def new_request_id () -> str:
	"""Return a fresh request id.

	Time-ordered (UUIDv7), so ids sort into the order the requests arrived and a log
	sorted by id reads chronologically.
	"""

	return str(uuid6.uuid7())


def request_id (request: starlette.requests.Request) -> str:
	"""Return this request's id, assigning one if it has not been through the middleware.

	The fallback matters: an error raised before or outside the middleware still has to
	produce a problem document, and one without an id is a failure nobody can look up.
	"""

	existing = getattr(request.state, "request_id", None)

	if isinstance(existing, str):
		return existing

	supplied = request.headers.get(REQUEST_ID_HEADER)
	assigned = supplied if supplied and _PLAUSIBLE_ID.fullmatch(supplied) else new_request_id()
	request.state.request_id = assigned

	return assigned


def apply_headers (
	request: starlette.requests.Request, response: starlette.responses.Response
) -> None:
	"""Stamp the correlation and version headers onto a response.

	Called from the middleware for ordinary responses and again from the error handlers,
	because a response produced by the outermost handler — the one that catches what
	nothing else did — never passes back through the middleware that would have stamped
	it. The 500 is the response that needs its id most.

	**§7's policy headers ride here for exactly that reason** (`#805`). Four things serve HTML —
	the shell, the 404 fallback, the app's own assets and `#803`'s confirmation — and a helper
	each of them had to remember would be forgotten by the fifth. This function is already the
	one place every response passes through, including the ones no route produced.

	**Read off the application rather than imported**, which is what keeps the import graph
	acyclic: the policy is derived from the served page, so a module here that computed it would
	import ``api.web``, which imports ``api.problems``, which imports this. ``api.app`` builds it
	once and puts it on the state, the same arrangement as the rate limiter's. Absent — an
	application assembled by a test without it — leaves a response unstamped rather than raising,
	because a missing header must never be the reason a request fails.
	"""

	response.headers[REQUEST_ID_HEADER] = request_id(request)
	response.headers[API_VERSION_HEADER] = subroutine.API_VERSION

	# **Defaults, so a response that set one of these deliberately keeps it.** They were
	# assigned unconditionally, which meant the sign-in confirmation — the one page whose own
	# URL carries a live credential — could not narrow its `Referrer-Policy` below the
	# instance's `same-origin` (`#927`'s M-27). Narrowing is the only direction anything here
	# goes: nothing in this application widens one, and `tests/test_api_policy.py` drives every
	# page under the real headers.
	for name, value in getattr(request.app.state, "policy_headers", {}).items():
		if name.lower() not in response.headers:
			response.headers[name] = value

	# **Nothing here is cacheable, and only the assets said so** (`#927`'s M-9). RFC 9111 lets
	# a shared cache store a response with no explicit directive, and its one built-in
	# protection is for requests carrying `Authorization` — which the browser's do not, because
	# `#248` authenticates it with a cookie. So a proxy in front of an instance was free to
	# store one person's agenda and hand it to the next reader, and `docs/hosting.md`
	# recommends putting a proxy in front of an instance.
	#
	# **Left alone where the response set its own**, which is `api/web`'s files: those really
	# are cacheable, carry a validator derived from their bytes (`#914`), and are the same for
	# everybody. Everything else is one person's work.
	if "cache-control" not in response.headers:
		response.headers["Cache-Control"] = "no-store"

		# What the answer depends on, for anything that stores it anyway. Both credential
		# kinds, because two of them reach the same routes and a cache keyed on one would
		# serve an agent's answer to a browser.
		#
		# **And the encoding, since `#2535`.** A compressed answer and a whole one are two
		# representations of one address. The compressor adds that itself, further in — and
		# this line is an assignment rather than an append, so it overwrote it. Measured, not
		# reasoned about: the header arrived as `Cookie, Authorization` with a gzipped body
		# behind it, which is precisely the mix a shared cache hands to the wrong caller.
		response.headers["Vary"] = "Cookie, Authorization, Accept-Encoding"


#: Set on the ASGI scope when a body ran past the limit while being read. Named as an
#: extension key rather than put on ``request.state``, because it is set before anything has
#: built a request — and read by ``api/problems``, which cannot import this module.
TOO_LARGE = "subroutine.body_too_large"


class BodyLimit:
	"""Refuse a request body larger than this instance is willing to read.

	**``docs/errors.md`` has described this since the registry was written** — *"a field or the
	request body exceeds the configured limit"* — and there was neither a configuration nor a
	check (`#927`'s M-2). §6.10 bounds each *field* after the body has been parsed, which is a
	different promise and arrives far too late: nothing stopped a caller streaming gigabytes at
	a route that would then try to read them into memory.

	**Counted as it arrives rather than read off ``Content-Length``.** A header is what the
	caller says, a chunked request need not carry one at all, and the check has to hold either
	way — so this is the only rule, and there is no faster path that could disagree with it.

	Raised rather than answered here, so the refusal is built by the ordinary handler and comes
	back as the same problem document as everything else. The exception surfaces where the body
	is awaited, which is inside the route.

	Pure ASGI rather than ``BaseHTTPMiddleware`` because reading the body is exactly what this
	must not do: that class hands the downstream app its own receive channel, so consuming the
	body to measure it would leave the route with nothing to parse.
	"""

	def __init__ (self, app: typing.Any, *, limit: int) -> None:
		"""Wrap an application, refusing anything longer than ``limit`` bytes."""

		self.app = app
		self.limit = limit

	async def __call__ (
		self, scope: typing.Any, receive: typing.Any, send: typing.Any
	) -> None:
		"""Pass the request on, counting the body on its way through."""

		if scope["type"] != "http":
			await self.app(scope, receive, send)

			return

		if self._declared(scope) > self.limit:
			# Refused before the application is called at all, which is the whole point on the
			# request this exists for: a caller announcing a gigabyte is answered without one
			# byte of it being read.
			await self._refuse(scope, send)

			return

		read = 0

		async def counted () -> typing.Any:
			"""Return the next piece of the body, marking the request once there is too much."""

			nonlocal read

			message = await receive()

			if message["type"] == "http.request":
				read += len(message.get("body", b""))

				if read > self.limit:
					# **Marked rather than raised.** FastAPI wraps reading the body and turns
					# anything raised there into *"There was an error parsing the body"* — a
					# 400 that tells the caller nothing about why. The mark is what lets
					# `problems` answer the question that was actually asked; the body is
					# truncated here so nothing further is read.
					scope[TOO_LARGE] = True
					message = {"type": "http.request", "body": b"", "more_body": False}

			return message

		await self.app(scope, counted, send)

	def _declared (self, scope: typing.Any) -> int:
		"""Return the body length the caller announced, or zero when it announced none."""

		for name, value in scope.get("headers", ()):
			if name.lower() == b"content-length" and value.isdigit():
				return int(value)

		return 0

	async def _refuse (self, scope: typing.Any, send: typing.Any) -> None:
		"""Answer with the same problem document every other refusal here produces.

		Built from ``subroutine.errors`` rather than through ``api.problems``, which imports
		this module — the cycle ``apply_headers`` already documents. What is lost by not going
		through it is the request id, and this is the one answer given before a request has
		been looked at.
		"""

		document = subroutine.errors.problem_document(self._too_large())
		body = json.dumps(document, separators=(",", ":")).encode("utf-8")

		await send(
			{
				"type": "http.response.start",
				"status": 413,
				"headers": [
					(b"content-type", b"application/problem+json"),
					(b"content-length", str(len(body)).encode("ascii")),
				],
			}
		)
		await send({"type": "http.response.body", "body": body})

	def _too_large (self) -> subroutine.errors.PayloadTooLarge:
		"""Return the refusal, worded once for both the announced and the counted case."""

		return subroutine.errors.PayloadTooLarge(
			f"That request body is larger than this instance reads "
			f"({self.limit // 1024} KB).",
			hint="Send less in one request - a listing takes 'limit', and a document's body "
			"is the one field that is meant to be long.",
		)


class CompressAnswers:
	"""Compress what the application builds per request, where the caller accepts it (`#2630`).

	**Each answer is negotiated by exactly one layer** (Simon's decision of 2026-09-14). The app's
	own files negotiate for themselves - ``leaves`` names their paths - and this does not touch
	them; everything else is Starlette's ``GZipMiddleware``, at ``compresslevel`` rather than its
	default of 9. Before this, both layers saw the app's files: the route honoured ``gzip;q=0``
	and sent the file unencoded, and the middleware, which searches the header for the word
	``gzip``, compressed it anyway and kept the file's tag on the result.

	**The header the middleware reads is replaced by the parser's answer** - ``accepts``, the one
	the routes use - so a refusal is a refusal on every path, and ``*`` accepts gzip here as it
	does there. Outside ``leaves`` the one reader of ``Accept-Encoding`` is the page served for a
	deep link, and it asks the same parser, so replacing the header with that parser's answer, on
	a copy of the scope, changes nothing it decides.

	**The parser and the paths are handed in** rather than imported from ``api/web``, which
	imports this module by way of ``api/problems``.
	"""

	def __init__ (
		self,
		app: typing.Any,
		*,
		minimum_size: int,
		compresslevel: int,
		accepts: typing.Callable[[str], bool],
		leaves: typing.Callable[[str], bool],
	) -> None:
		"""Wrap an application."""

		self.app = app
		self.accepts = accepts
		self.leaves = leaves
		self.compressing = starlette.middleware.gzip.GZipMiddleware(
			app, minimum_size=minimum_size, compresslevel=compresslevel
		)

	async def __call__ (
		self, scope: typing.Any, receive: typing.Any, send: typing.Any
	) -> None:
		"""Hand an answer to the compressor with the caller's decision, or leave it alone."""

		if scope["type"] != "http" or self.leaves(scope["path"]):
			await self.app(scope, receive, send)

			return

		asked = starlette.datastructures.Headers(scope=scope).get("accept-encoding", "")
		decided = b"gzip" if self.accepts(asked) else b"identity"
		headers = [(name, value) for name, value in scope["headers"] if name != b"accept-encoding"]

		await self.compressing(
			{**scope, "headers": [*headers, (b"accept-encoding", decided)]}, receive, send
		)


class AnswerHeadWithGet:
	"""Let ``HEAD`` reach the ``GET`` at the same path.

	FastAPI's ``APIRoute`` does not pair the two, where Starlette's own ``Route`` does — so
	``/v1/openapi.json``, which FastAPI registers itself, answered ``HEAD`` and every route in
	this application answered 405. RFC 9110 requires a general-purpose server to support both,
	and the caller who meets it first is a load balancer: ``HEAD /healthz`` is the commonest
	default there is, and an instance serving perfectly well is reported as down.

	**Here rather than as ``methods=["GET", "HEAD"]`` on sixty decorators.** A list is a list of
	the routes somebody thought of, and FastAPI documents every method a route declares — so
	that spelling would also put a ``head`` operation on every path in ``/v1/openapi.json``,
	doubling the published contract to say something no reader needs told.

	Rewritten only where a ``GET`` really exists, so a ``HEAD`` at a write-only path is still
	refused as ``HEAD`` rather than as a ``GET`` nobody sent.

	**The rewrite is made on a copy, and that is why this is pure ASGI** (`#2622`). It was a
	``BaseHTTPMiddleware`` that set ``request.scope["method"]``, and that dict is the server's
	own: uvicorn decides whether to send a body by reading the method from it as the response is
	written, so a ``HEAD`` went out with the ``GET``'s whole body - bytes a client expecting none
	reads as the start of the next response, on the probe load balancers send most. This said
	the opposite had been measured. That class calls the rest of the application with the scope
	it was handed, so no copy could be passed through it. Now the server keeps its ``HEAD`` and
	sends the headers and ``Content-Length`` of the ``GET``, and none of its bytes.
	"""

	def __init__ (self, app: typing.Any) -> None:
		"""Wrap an application."""

		self.app = app

	async def __call__ (
		self, scope: typing.Any, receive: typing.Any, send: typing.Any
	) -> None:
		"""Pass a ``HEAD`` on as a ``GET`` where one is declared, and anything else untouched."""

		if scope["type"] != "http" or scope["method"] != "HEAD":
			await self.app(scope, receive, send)

			return

		declared = getattr(scope["app"].state, "declared_routes", None)
		path = starlette.requests.Request(scope).url.path

		if declared is None or "GET" not in subroutine.api.routing.accepted(declared, path):
			await self.app(scope, receive, send)

			return

		# **One ``state`` for both scopes**, made before the copy: the request id is kept there,
		# and an error answered outside this layer has to find the id the inside assigned.
		scope.setdefault("state", {})

		await self.app({**scope, "method": "GET"}, receive, send)


async def correlate (
	request: starlette.requests.Request,
	call_next: typing.Callable[
		[starlette.requests.Request], typing.Awaitable[starlette.responses.Response]
	],
) -> starlette.responses.Response:
	"""Give the request an id before anything can fail, and stamp the response with it."""

	request_id(request)

	response = await call_next(request)
	apply_headers(request, response)

	return response
