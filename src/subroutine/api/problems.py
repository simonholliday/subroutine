"""Turning failures into RFC 9457 problem documents.

Every error this API returns has the same shape, whether it came from a service refusing a
change, a body that would not parse, a path that does not exist or a bug. A client that
learns to read one failure can read all of them, which is the whole point of the registry
in :mod:`subroutine.errors` — and a caller meeting a bare FastAPI ``{"detail": …}`` for a
404 has met a second, undocumented error format.

This module is where HTTP is allowed to know about ``subroutine.errors`` and nowhere else
is. The error classes themselves stay ignorant of frameworks so the CLI can report the
same failures without one.
"""

import logging
import typing

import fastapi
import fastapi.exceptions
import starlette.exceptions
import starlette.requests
import starlette.responses

import subroutine.api.middleware
import subroutine.api.security
import subroutine.domain.versions
import subroutine.errors

#: Problem documents are ``application/problem+json``, not ``application/json``. A client
#: can branch on the content type alone to tell a failure from a result.
PROBLEM_MEDIA_TYPE = "application/problem+json"

_logger = logging.getLogger("subroutine.api")

#: The statuses Starlette raises on its own behalf, mapped onto the public registry so
#: they arrive in the same envelope as everything else. A status not listed here is
#: reported as an internal error, which is the truth: this application raised an HTTP
#: exception it never taught anybody to expect.
_STATUS_CODES: dict[int, str] = {
	400: "malformed_request",
	401: "unauthenticated",
	403: "forbidden",
	404: "not_found",
	405: "method_not_allowed",
	413: "payload_too_large",
	422: "invalid_field_value",
	429: "rate_limited",
	500: "internal_error",
	503: "service_unavailable",
}

#: How Pydantic names a failure, and which of ours it is. Anything unlisted is a field
#: whose value cannot be used, which is what the great majority of them are.
_FIELD_CODES: dict[str, str] = {
	"extra_forbidden": "unknown_field",
	"missing": "missing_field",
}


def respond (
	request: starlette.requests.Request,
	error: subroutine.errors.SubroutineError,
	*,
	headers: typing.Mapping[str, str] | None = None,
) -> starlette.responses.Response:
	"""Render a failure as the response the caller receives."""

	response = starlette.responses.JSONResponse(
		status_code=error.status,
		content=subroutine.errors.problem_document(
			error,
			instance=request.url.path,
			request_id=subroutine.api.middleware.request_id(request),
		),
		media_type=PROBLEM_MEDIA_TYPE,
		headers=headers,
	)

	# RFC 9110 requires a 401 to say what would be accepted. Set here rather than at each
	# place that refuses a credential, so a new resolver cannot forget it and produce a
	# response that a standards-abiding client treats as malformed.
	if error.status == 401 and "www-authenticate" not in response.headers:
		response.headers["WWW-Authenticate"] = subroutine.api.security.BEARER_SCHEME

	# And a 429 has to say when to come back (§7.7), for the same reason and in the same
	# place. The value rides as an extension member because a caller needs to *act* on it;
	# this turns it into the header a standard client already knows how to obey.
	waiting = error.extensions.get("retry_after")

	if error.status == 429 and waiting is not None and "retry-after" not in response.headers:
		response.headers["Retry-After"] = str(int(waiting))

	subroutine.api.middleware.apply_headers(request, response)

	return response


def handle_subroutine_error (
	request: starlette.requests.Request, exception: Exception
) -> starlette.responses.Response:
	"""Report a failure the service layer raised, exactly as it described itself."""

	assert isinstance(exception, subroutine.errors.SubroutineError)

	return respond(request, exception)


def handle_http_exception (
	request: starlette.requests.Request, exception: Exception
) -> starlette.responses.Response:
	"""Report a failure the framework raised, in this application's own envelope."""

	assert isinstance(exception, starlette.exceptions.HTTPException)

	code = _STATUS_CODES.get(exception.status_code, "internal_error")
	definition = subroutine.errors.definition(code)

	detail = str(exception.detail) if exception.detail else definition.title
	hint = None

	if exception.status_code == 404:
		# The default detail is the single word "Not Found", which tells a caller nothing
		# about whether it got the path wrong or the identifier.
		detail = f"There is nothing at {request.url.path}."
		hint = "See /v1/openapi.json for the paths this instance serves."

	elif exception.status_code == 405:
		allowed = (exception.headers or {}).get("Allow")
		detail = f"{request.method} is not accepted at {request.url.path}."
		hint = None if allowed is None else f"This path accepts {allowed}."

	error = _rebuild(definition.status, code, detail, hint=hint)

	return respond(request, error, headers=exception.headers)


def handle_validation_error (
	request: starlette.requests.Request, exception: Exception
) -> starlette.responses.Response:
	"""Report a request whose body, query or path could not be used.

	Each complaint names its field and, where the valid answers are a known set, lists
	them — an agent that is told only "422" has no better next move than the guess that
	just failed (SPEC.md §8.1).
	"""

	assert isinstance(exception, fastapi.exceptions.RequestValidationError)

	raw = exception.errors()

	# A body that is not JSON at all has no fields to complain about, so it is a different
	# failure: the request could not be read, rather than read and found wanting.
	if any(item.get("type") in ("json_invalid", "json_type") for item in raw):
		return respond(
			request,
			subroutine.errors.BadRequest(
				"The request body is not valid JSON.",
				hint="Send a JSON object with 'Content-Type: application/json'.",
			),
		)

	valid_names = _accepted_field_names(request)
	fields = tuple(_field_error(item, valid_names) for item in raw)

	# Whichever kind of mistake it is, it is the one worth naming in the code: an unknown
	# field is a caller using a name this endpoint has never had, and that is more useful
	# to know than that something, somewhere, was invalid.
	code = "invalid_field_value"

	for candidate in ("unknown_field", "missing_field"):
		if any(field.code == candidate for field in fields):
			code = candidate
			break

	detail = (
		fields[0].message
		if len(fields) == 1
		else f"The request could not be accepted: {len(fields)} fields are wrong."
	)

	return respond(
		request,
		subroutine.errors.ValidationError(detail, code=code, errors=fields),
	)


def handle_a_lost_update (
	request: starlette.requests.Request, exception: Exception
) -> starlette.responses.Response:
	"""Report a write the database refused because somebody else got there first.

	`#927`'s H-12. ``VersionMixin`` writes every ``UPDATE`` under ``WHERE version = <what
	this transaction read>``, so a racing writer's statement matches no row and SQLAlchemy
	raises. Untranslated that is a 500 about a caller who did nothing wrong, on the one
	condition §8.9 exists to report.

	**At the application rather than around each service**, because the failure arrives from
	two places: a service's own ``flush`` inside the handler, and ``Transactional``'s commit
	after it returns. A wrapper at either one would cover half the writes and read as though
	it covered them all.

	``api.concurrency.reporting`` is untouched by this and must stay so: it attaches the
	current entity to a :class:`~subroutine.errors.SubroutineError`, and what passes through
	it here is SQLAlchemy's own exception, which it ignores. So the enrichment that needs a
	healthy session never runs against a broken one.
	"""

	return respond(request, subroutine.domain.versions.raced())


def handle_unexpected_error (
	request: starlette.requests.Request, exception: Exception
) -> starlette.responses.Response:
	"""Report a bug without describing it to the caller.

	The detail is deliberately vague and the traceback goes to the log under this
	request's id, which is the only thing the response and the log entry have in common.
	Saying more would be handing an attacker a description of the internals; saying less
	would leave nobody able to look it up.
	"""

	identifier = subroutine.api.middleware.request_id(request)

	_logger.exception(
		"Unhandled error serving %s %s (request %s)",
		request.method,
		request.url.path,
		identifier,
		exc_info=exception,
	)

	return respond(
		request,
		subroutine.errors.InternalError(
			"Something went wrong that should not have.",
			hint=f"Quote request id {identifier} when reporting this.",
		),
	)


def install (application: fastapi.FastAPI) -> None:
	"""Register every handler, so that no failure escapes in another shape."""

	application.add_exception_handler(
		subroutine.errors.SubroutineError, handle_subroutine_error
	)
	application.add_exception_handler(
		starlette.exceptions.HTTPException, handle_http_exception
	)
	application.add_exception_handler(
		fastapi.exceptions.RequestValidationError, handle_validation_error
	)
	application.add_exception_handler(
		subroutine.domain.versions.RACED, handle_a_lost_update
	)

	# The catch-all. Registered last for readability only — Starlette keys handlers by
	# exception class, so the most specific match wins whatever order they arrived in.
	application.add_exception_handler(Exception, handle_unexpected_error)


def _rebuild (
	status: int, code: str, detail: str, *, hint: str | None
) -> subroutine.errors.SubroutineError:
	"""Return the error class that reports ``status``, carrying ``code``.

	The registry fixes which status a code means, and the exception classes fix which
	status they report; this picks the one that agrees with both rather than letting a
	framework status and a registry entry drift apart unnoticed.
	"""

	classes: tuple[type[subroutine.errors.SubroutineError], ...] = (
		subroutine.errors.BadRequest,
		subroutine.errors.Unauthenticated,
		subroutine.errors.Forbidden,
		subroutine.errors.NotFound,
		subroutine.errors.MethodNotAllowed,
		subroutine.errors.Conflict,
		subroutine.errors.CursorExpired,
		subroutine.errors.PayloadTooLarge,
		subroutine.errors.ValidationError,
		subroutine.errors.RateLimited,
		subroutine.errors.InternalError,
		subroutine.errors.ServiceUnavailable,
	)

	for candidate in classes:
		if subroutine.errors.definition(candidate.CODE).status == status:
			return candidate(detail, code=code, hint=hint)

	return subroutine.errors.InternalError(detail, hint=hint)


def _field_error (
	item: typing.Mapping[str, typing.Any], valid_names: tuple[str, ...]
) -> subroutine.errors.FieldError:
	"""Translate one of Pydantic's complaints into one of ours."""

	location = tuple(item.get("loc", ()))
	name = _field_name(location)
	code = _FIELD_CODES.get(str(item.get("type")), "invalid_field_value")
	message = str(item.get("msg", "This value cannot be used."))
	hint = None

	if code == "unknown_field":
		message = f"{name!r} is not a field this endpoint accepts."

		# Only offered for a field directly on the body: nested under a list or another
		# model, the endpoint's own field names are the wrong list to suggest.
		if valid_names and len(location) == 2:
			hint = f"Accepted fields are: {', '.join(valid_names)}."

	elif code == "missing_field":
		message = f"{name!r} is required."

	return subroutine.errors.FieldError(field=name, code=code, message=message, hint=hint)


def _field_name (location: tuple[typing.Any, ...]) -> str:
	"""Render Pydantic's location tuple as a field path a caller can act on.

	The leading ``body`` is dropped because that is where fields live unless said
	otherwise, while ``query`` and ``path`` are kept: knowing that ``limit`` was wrong in
	the query string rather than the body is the difference between one fix and two.
	"""

	parts = [str(part) for part in location]

	if parts[:1] == ["body"]:
		parts = parts[1:]

	return ".".join(parts) if parts else "body"


def body_fields (route: typing.Any) -> tuple[str, ...]:
	"""Return the body field names a route accepts, or nothing if it takes no body.

	**One reader of an interface FastAPI does not document.** It has kept the body's model in
	two different places across versions — ``field_info.annotation`` now, ``type_`` before it —
	so both are tried and neither is required. A second copy of that lookup would be a second
	thing to fix on the day it moves again, which is why ``tests/test_reach.py`` calls this
	rather than reaching into a route itself (item ``#427``).

	Failing to find one is never an error. Here it costs a caller a hint; there it means a
	route is not classified, which the guard reports in its own words.
	"""

	body_field = getattr(route, "body_field", None)
	model = getattr(getattr(body_field, "field_info", None), "annotation", None) or getattr(
		body_field, "type_", None
	)
	fields = getattr(model, "model_fields", None)

	if not isinstance(fields, dict):
		return ()

	return tuple(sorted(str(name) for name in fields))


def _accepted_field_names (request: starlette.requests.Request) -> tuple[str, ...]:
	"""Return the body field names the matched endpoint accepts, if it has a body model.

	Read from the route FastAPI has already resolved rather than from a list maintained
	alongside it, so the hint cannot name a field the endpoint stopped accepting.
	"""

	return body_fields(request.scope.get("route"))
