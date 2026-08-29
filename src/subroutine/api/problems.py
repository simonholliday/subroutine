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

import dataclasses
import logging
import typing

import fastapi
import fastapi.dependencies.utils
import fastapi.exceptions
import sqlalchemy.exc
import starlette.exceptions
import starlette.requests
import starlette.responses

import subroutine.api.dependencies
import subroutine.api.middleware
import subroutine.api.routing
import subroutine.api.security
import subroutine.db.failures
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

#: Pydantic's coercion failures, and what to say about each instead — `SR#1569`, L-4.
#:
#: **Keyed on pydantic's own type strings**, which is a dependency on somebody else's constant
#: and is why the fallback below is the original message: an entry that stops matching leaves
#: the refusal exactly as it was rather than losing it. Measured on the version in use —
#: `bool_parsing` for a word that is not true or false, `int_parsing` for both `abc` and `1.5`.
#:
#: **The canonical pair rather than every alias.** Pydantic also takes `yes`, `on`, `y`, `t` and
#: their inverses, and listing nine spellings answers a question nobody asked; what a caller
#: needs is one that works.
_COERCIONS: dict[str, tuple[str, str]] = {
	"bool_parsing": ("a true or false value", "Use 'true' or 'false'."),
	"bool_type": ("a true or false value", "Use 'true' or 'false'."),
	"int_parsing": ("a whole number", "Send a whole number, with no decimal point."),
	"int_type": ("a whole number", "Send a whole number, with no decimal point."),
}


def respond (
	request: starlette.requests.Request,
	error: subroutine.errors.SubroutineError,
	*,
	headers: typing.Mapping[str, str] | None = None,
) -> starlette.responses.Response:
	"""Render a failure as the response the caller receives."""

	if request.scope.get(subroutine.api.middleware.TOO_LARGE):
		# **The body ran past the limit while it was being read**, and what the framework made
		# of the truncated remainder is not the answer (`#927`'s M-2). FastAPI wraps reading a
		# body, so anything *raised* in there arrives as "There was an error parsing the body";
		# a body cut short instead arrives as a missing field. Neither says why, and both are
		# the same request. Answered here rather than in one handler, because it can reach more
		# than one of them.
		error = subroutine.errors.PayloadTooLarge(
			"That request body is larger than this instance reads.",
			hint="Send less in one request — a listing takes 'limit', and a document's body "
			"is the one field that is meant to be long.",
		)

	# **Said here because this is the one place every problem document is built** (`#1315`).
	# A field error can be raised by the domain, which does not know a transport, or by
	# Pydantic, which has already named the location it read — and both arrive through this
	# function with the matched route in hand.
	error.errors = _where_it_goes(request, error.errors)

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
		response.headers["Retry-After"] = str(waiting)

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
	headers = dict(exception.headers or {})

	if exception.status_code == 404:
		# The default detail is the single word "Not Found", which tells a caller nothing
		# about whether it got the path wrong or the identifier.
		detail = f"There is nothing at {request.url.path}."
		hint = "See /v1/openapi.json for the paths this instance serves."

	elif exception.status_code == 405:
		allowed = _accepted_at(request) or headers.get("Allow")
		detail = f"{request.method} is not accepted at {request.url.path}."
		hint = None if allowed is None else f"This path accepts {allowed}."

		if allowed is not None:
			headers["Allow"] = allowed

	error = _rebuild(definition.status, code, detail, hint=hint)

	return respond(request, error, headers=headers)


def _accepted_at (request: starlette.requests.Request) -> str | None:
	"""Return every method this instance accepts at the requested path, as an ``Allow`` value.

	Starlette answers a 405 out of the first route whose *path* matched, and FastAPI registers
	one route per method — so ``PUT /v1/tasks`` was told ``Allow: POST`` and "This path accepts
	POST", with the ``GET`` beside it unmentioned. RFC 9110 requires the header to list what the
	path accepts, and a caller mapping the surface from its refusals is exactly who reads it.

	``None`` where the application was assembled without the map — a test building one by hand —
	so the framework's own answer is used rather than none at all. A missing header must never be
	the reason a request fails.
	"""

	declared = getattr(request.app.state, "declared_routes", None)

	if declared is None:
		return None

	return ", ".join(sorted(subroutine.api.routing.accepted(declared, request.url.path))) or None


def handle_validation_error (
	request: starlette.requests.Request, exception: Exception
) -> starlette.responses.Response:
	"""Report a request whose body, query or path could not be used.

	Each complaint names its field and, where the valid answers are a known set, lists
	them — an agent that is told only "422" has no better next move than the guess that
	just failed (docs/design.md §8.1).
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


def handle_a_request_that_did_not_finish (
	request: starlette.requests.Request, exception: Exception
) -> starlette.responses.Response:
	"""Report database work this instance stopped waiting for, rather than a bug (`#568`).

	Nothing bounded how long a statement could run, so a row lock or a query that would never
	finish reached the caller as **silence** — which from outside is indistinguishable from a
	deploy, a network fault or a proxy, and was read as exactly that during `#553`.
	``request_timeout_seconds`` turns the wait into a refusal; this turns the refusal into
	something a caller can act on instead of a 500 blaming this program for the caller's query.

	**Every other ``OperationalError`` is handed on unchanged.** That class is most of what a
	database can raise — a connection dropped, a disk full, a database shut down underneath us
	— and none of those is this. Delegating rather than re-raising keeps them logged with their
	request id by the one function that does that.

	**The words are :mod:`subroutine.db.failures`' rather than this module's** (`#1070`). Since
	`#539` the MCP tools run inside this instance on the same bounded session, so an agent meets
	this condition too and was handed SQLAlchemy's raw text for it. One translation, two
	surfaces.
	"""

	answer = subroutine.db.failures.gave_up(
		exception,
		seconds=subroutine.api.dependencies.settings(request).request_timeout_seconds,
	)

	if answer is None:
		return handle_unexpected_error(request, exception)

	return respond(request, answer)


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
	application.add_exception_handler(
		sqlalchemy.exc.OperationalError, handle_a_request_that_did_not_finish
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

	# **A value that never reached our own validation** (`SR#1569`, L-4). Of 110 refusals a
	# cold review provoked across the advertised surface, 98 were in the house voice with a
	# field, a specific message and an actionable hint; the 12 that were not are all `bool` and
	# `int` **query** parameters, which pydantic rejects at coercion — before the endpoint's
	# body runs at all. So `?ready=maybe` said *"Input should be a valid boolean, unable to
	# interpret input"* and named none of the spellings it would have taken, where every
	# sibling refusal names its vocabulary: *"The choices are: include, exclude, only."*
	#
	# **Reworded here rather than by re-declaring the parameters as strings**, which is the
	# other way to reach our own validation and would change what `/v1/openapi.json` says these
	# take — a published contract, changed to improve a sentence.
	elif str(item.get("type")) in _COERCIONS:
		wanted, advice = _COERCIONS[str(item.get("type"))]
		message = f"{item.get('input')!r} is not {wanted}."
		hint = advice

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


def parameter_locations (route: typing.Any) -> dict[str, str]:
	"""Return each name a route reads outside the body, and which half of the request it is in.

	Read the same way :func:`body_fields` reads the other half, and from the same resolved
	route, so the two answers are about one endpoint and cannot describe different ones.

	The dependency tree is flattened first, because a name declared inside a shared
	dependency is still a name the caller writes in the query string — and every listing
	here gets its filters that way.

	**It was ``query_parameters`` and it never returned only the query's** (`#1404`). The flat
	parameters include the path's, so ``GET /v1/tasks/{id_or_ref}`` answered with ``id_or_ref``
	among them — harmless while the one caller was narrowed to routes that take a body, and a
	refusal reading ``query.id_or_ref`` the moment it was not. The location comes back beside
	the name now, so the caller can say where a field really was rather than assuming.

	The value is ``fastapi``'s own word for it — ``query``, ``path``, ``header``, ``cookie`` —
	which is the vocabulary :func:`_field_name` already keeps and
	:data:`subroutine.errors.FIELD_LOCATIONS` already reads.
	"""

	dependant = getattr(route, "dependant", None)

	if dependant is None:
		return {}

	found: dict[str, str] = {}

	for parameter in fastapi.dependencies.utils.get_flat_params(dependant):
		# **Read defensively, like :func:`body_fields` reads its half and for the same reason**:
		# ``in_`` is on ``fastapi``'s ``Param`` subclasses rather than on the ``FieldInfo`` the
		# type says, and it is not a documented interface. Falling back to ``query`` keeps the
		# answer this function gave before it knew the difference, which is the safe direction:
		# a name we cannot place is far more likely to be one.
		where = getattr(getattr(parameter, "field_info", None), "in_", None)
		found[str(parameter.name)] = str(getattr(where, "value", "query"))

	return found


def _where_it_goes (
	request: starlette.requests.Request, fields: typing.Sequence[subroutine.errors.FieldError]
) -> tuple[subroutine.errors.FieldError, ...]:
	"""Qualify a refused field name that names a query parameter rather than a body field.

	`#1315`. ``workspace_id`` is a query parameter on 55 routes and a body field on three, and
	the domain that raises the ambiguous-workspace refusal knows neither — it is raised below
	the transport and read on two of them (`#547`). So it names the field bare, which in this
	API is the spelling for *a field of the body*: a caller who did what it said was refused a
	second time by ``unknown_field``, having spent a round trip finding out.

	``query.limit`` is already what a caller sees when Pydantic refuses a query parameter, so
	this applies one existing convention to the other source of field errors rather than
	inventing a second. It is **derived per route** and never a rename of one word: on ``POST
	/v1/tasks`` the same refusal about the same parameter must stay bare, because that endpoint
	takes ``workspace_id`` in the body and takes no query parameters at all.

	**It reaches every route now, and it used to reach only the ones that take a body**
	(`#1404`, Simon's decision of 2026-08-28). The narrowing was defensible on its own terms —
	a bare name is *ambiguous* only where there is somewhere else to put it — and it left the
	wire contract saying two things about one parameter depending on which layer refused it.
	Measured before the widening: 40 routes take ``workspace_id`` in the query and accept no
	body at all, and about fifteen distinct domain refusals across the API named a query
	parameter bare. It matters as soon as anybody writes a client that branches on ``field``,
	and it cannot be found by testing locally, because this runs on the HTTP path alone.

	**The three readers were taught the location first, and that was the order rather than a
	preference.** ``cli.main.TERMINAL_REMEDIES`` and ``TERMINAL_FIELD_NAMES`` and MCP's
	``_as_this_tool_calls_it`` all match on a field name; keyed on the bare spelling, every one
	of them would have stopped matching the day this widened — silently, on remote connections
	only, costing a person the line that tells them which flag to retype and handing an agent a
	name its own tool does not declare. They ask :func:`subroutine.errors.field_tail` now.

	**And the location is the parameter's real one rather than an assumption.** This said
	``query.`` for everything it touched, over a helper whose flat parameters include the
	path's — so widening it to bodiless routes would have answered ``query.id_or_ref`` about a
	segment of the URL. :func:`parameter_locations` says which half each name is in.

	A name already carrying a location is left alone, so a refusal that has been through
	:func:`_field_name` cannot come out ``query.query.limit``. A name the route takes *both*
	ways is left alone too — there is no such parameter today, and guessing which half a
	caller meant would be the mistake this exists to stop.
	"""

	route = request.scope.get("route")
	body = set(body_fields(route))
	outside = {
		name: where
		for name, where in parameter_locations(route).items()
		if name not in body
	}

	if not outside:
		return tuple(fields)

	return tuple(
		dataclasses.replace(field, field=f"{outside[field.field]}.{field.field}")
		if field.field in outside
		else field
		for field in fields
	)


def _accepted_field_names (request: starlette.requests.Request) -> tuple[str, ...]:
	"""Return the body field names the matched endpoint accepts, if it has a body model.

	Read from the route FastAPI has already resolved rather than from a list maintained
	alongside it, so the hint cannot name a field the endpoint stopped accepting.
	"""

	return body_fields(request.scope.get("route"))
