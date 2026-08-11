"""The two headers every response carries, and the request id that ties a failure to a log.

``X-Request-Id`` is the only thread between a 500 a caller saw and the traceback that
explains it: the response says nothing about what went wrong, deliberately, so the id is
what makes the failure diagnosable at all (SPEC.md §8.1). ``X-Subroutine-Api-Version``
tells a client which wire contract it is talking to without a round trip to ``/v1/meta``.
"""

import re
import typing

import starlette.requests
import starlette.responses
import uuid6

import subroutine

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

	for name, value in getattr(request.app.state, "policy_headers", {}).items():
		response.headers[name] = value


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
