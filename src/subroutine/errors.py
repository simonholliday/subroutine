"""What goes wrong, said in a way a program can act on.

Every failure the API reports is one of the exceptions here, and every one of them carries
a ``code`` from the registry below. The codes are **part of the public contract** and
covered by semantic versioning (SPEC.md §8.8): a client may branch on them, so renaming
one is a breaking change and removing one is a major version.

Three things make an error useful to an agent rather than merely accurate:

* a stable ``code`` it can compare, instead of prose it has to match;
* the offending **field**, so it knows which part of its request to change;
* a **hint** naming the valid alternatives, so its next attempt is informed rather than
  another guess.

This module deliberately knows nothing about HTTP frameworks. It maps a failure to a
status code and a document; turning that into a response is the API layer's job, and
keeping them apart is what lets the CLI report the same failures without one.
"""

import dataclasses
import typing

#: Base for the ``type`` URI of a problem document. One URI per code, so it resolves to
#: the registry entry describing it. RFC 9457 does not require this to be dereferenceable;
#: it is far more useful if it is.
ERROR_TYPE_BASE = "https://subroutine.dev/errors"


@dataclasses.dataclass(frozen=True)
class ErrorDefinition:
	"""One entry in the public error registry."""

	code: str
	status: int
	title: str
	description: str

	@property
	def type_uri (self) -> str:
		"""Return the ``type`` URI identifying this kind of problem."""

		return f"{ERROR_TYPE_BASE}/{self.code.replace('_', '-')}"


def _define (code: str, status: int, title: str, description: str) -> ErrorDefinition:
	"""Build a registry entry."""

	return ErrorDefinition(code=code, status=status, title=title, description=description)


#: The public registry. Additions are a minor version; renames and removals are major.
#: Served at ``/v1/meta`` and rendered into ``docs/errors.md`` so the two cannot disagree.
REGISTRY: dict[str, ErrorDefinition] = {
	definition.code: definition
	for definition in (
		_define(
			"malformed_request",
			400,
			"Malformed request",
			"The request could not be read at all — bad JSON, a broken header, or a "
			"parameter that is not of the shape the endpoint accepts.",
		),
		_define(
			"unauthenticated",
			401,
			"Not authenticated",
			"No credential was presented, or the one presented is not valid. Every reason "
			"reports identically: an unknown token, a revoked one and an expired one are "
			"indistinguishable from outside on purpose.",
		),
		_define(
			"forbidden",
			403,
			"Not permitted",
			"The credential is valid but does not carry the permission this action needs. "
			"The permission is named, so a caller can ask for a token that has it.",
		),
		_define(
			"not_found",
			404,
			"Not found",
			"There is no such thing, or it is not visible to this caller. The two are "
			"deliberately not distinguished: saying 'forbidden' about a private project "
			"would confirm it exists (SPEC.md §7.3a).",
		),
		_define(
			"method_not_allowed",
			405,
			"Method not allowed",
			"The path exists but does not answer to that HTTP method. The methods it does "
			"answer to are listed in the 'Allow' header.",
		),
		_define(
			"version_conflict",
			409,
			"Version conflict",
			"The entity changed since the version the caller sent. The response carries "
			"both versions and the current entity, so the caller can merge rather than "
			"refetch and start again.",
		),
		_define(
			"duplicate_key",
			409,
			"Already exists",
			"Something with that identifying value is already here — a project key, a "
			"username, a tag name.",
		),
		_define(
			"cycle_detected",
			409,
			"Cycle detected",
			"The change would make something its own ancestor, in a project tree, a task "
			"hierarchy or a chain of blocking links.",
		),
		_define(
			"payload_too_large",
			413,
			"Too large",
			"A field or the request body exceeds the configured limit. The limit is "
			"reported rather than the value being silently truncated (SPEC.md §6.10).",
		),
		_define(
			"invalid_field_value",
			422,
			"Invalid field value",
			"The request was well-formed but a field's value cannot be used. The field is "
			"named and, where the valid values are a known set, they are listed.",
		),
		_define(
			"missing_field",
			422,
			"Missing field",
			"A field this endpoint requires was not supplied.",
		),
		_define(
			"unknown_field",
			422,
			"Unknown field",
			"The request body carried a field this endpoint does not accept. Rejected "
			"rather than ignored, because silently dropping a typo is how a caller comes "
			"to believe it set something it did not (SPEC.md §8.1).",
		),
		_define(
			"invalid_status",
			422,
			"Invalid status",
			"No status with that key exists for this entity type in this workspace. The "
			"valid keys are listed; an installation may rename them freely, so they are "
			"read from the workspace rather than assumed.",
		),
		_define(
			"rate_limited",
			429,
			"Too many requests",
			"The caller is going faster than the configured limit allows. The response "
			"says when to try again.",
		),
		_define(
			"internal_error",
			500,
			"Internal error",
			"Something failed that should not have. The detail is deliberately vague; the "
			"request id is what ties the response to the log entry that explains it.",
		),
		_define(
			"service_unavailable",
			503,
			"Not ready",
			"The instance is running but cannot serve requests yet — most often its "
			"database is unreachable, or its schema has not been brought up to date. "
			"Reported by the readiness check so that a deployment holds traffic back "
			"rather than serving errors.",
		),
	)
}


def definition (code: str) -> ErrorDefinition:
	"""Return the registry entry for a code, or raise if it is not registered."""

	try:
		return REGISTRY[code]

	except KeyError:
		valid = ", ".join(sorted(REGISTRY))

		raise ValueError(f"Unregistered error code {code!r}. Registered codes are: {valid}.") from None


@dataclasses.dataclass(frozen=True)
class FieldError:
	"""One thing wrong with one field, and what to do about it."""

	field: str
	code: str
	message: str
	hint: str | None = None

	def as_dict (self) -> dict[str, typing.Any]:
		"""Return this as it appears in the ``errors`` array of a problem document."""

		body: dict[str, typing.Any] = {
			"field": self.field,
			"code": self.code,
			"message": self.message,
		}

		if self.hint is not None:
			body["hint"] = self.hint

		return body


class SubroutineError(Exception):
	"""Base for every failure reported in the application's own terms.

	Subclasses fix the HTTP status; the ``code`` chooses which specific failure within
	that status it is. A subclass refuses a code registered against a different status,
	so ``NotFound(code="version_conflict")`` fails at the point of the mistake rather
	than producing a 404 document claiming to be a 409.
	"""

	#: The code used when the caller does not name a more specific one.
	CODE = "internal_error"

	def __init__ (
		self,
		detail: str,
		*,
		code: str | None = None,
		errors: typing.Sequence[FieldError] = (),
		hint: str | None = None,
		extensions: typing.Mapping[str, typing.Any] | None = None,
	) -> None:
		"""Record what went wrong, in terms the caller can act on.

		``extensions`` are RFC 9457 extension members — additional top-level fields on the
		problem document, for facts a caller needs to *act on* rather than read. A version
		conflict carries the two version numbers this way, because "merge and retry" is a
		thing a program does and parsing them out of a sentence is not.
		"""

		super().__init__(detail)

		self.code = code or self.CODE
		self.detail = detail
		self.errors = tuple(errors)
		self.hint = hint
		self.extensions = dict(extensions or {})

		expected = definition(self.CODE).status
		actual = self.definition.status

		if actual != expected:
			raise ValueError(
				f"{type(self).__name__} reports HTTP {expected}, but error code "
				f"{self.code!r} is registered as HTTP {actual}."
			)

	@property
	def definition (self) -> ErrorDefinition:
		"""Return this error's registry entry."""

		return definition(self.code)

	@property
	def status (self) -> int:
		"""Return the HTTP status this failure maps to."""

		return self.definition.status


class BadRequest(SubroutineError):
	"""The request could not be read."""

	CODE = "malformed_request"


class Unauthenticated(SubroutineError):
	"""No usable credential was presented."""

	CODE = "unauthenticated"


class Forbidden(SubroutineError):
	"""The credential is valid but does not permit this."""

	CODE = "forbidden"


class NotFound(SubroutineError):
	"""There is no such thing, or none this caller may know about."""

	CODE = "not_found"


class MethodNotAllowed(SubroutineError):
	"""The path exists but not for that verb."""

	CODE = "method_not_allowed"


class Conflict(SubroutineError):
	"""The request collides with the current state."""

	CODE = "duplicate_key"


class PayloadTooLarge(SubroutineError):
	"""Something exceeds a configured limit."""

	CODE = "payload_too_large"


class ValidationError(SubroutineError):
	"""The request was well-formed but cannot be carried out as written."""

	CODE = "invalid_field_value"


class RateLimited(SubroutineError):
	"""The caller is going too fast."""

	CODE = "rate_limited"


class InternalError(SubroutineError):
	"""Something failed that should not have."""

	CODE = "internal_error"


class ServiceUnavailable(SubroutineError):
	"""The instance is running but not yet able to serve requests."""

	CODE = "service_unavailable"


def problem_document (
	error: SubroutineError,
	*,
	instance: str | None = None,
	request_id: str | None = None,
) -> dict[str, typing.Any]:
	"""Render an error as an RFC 9457 problem document.

	``instance`` is the path that produced it and ``request_id`` ties it to the log entry
	describing it — both supplied by whatever is handling the request, since this module
	has no idea there is one.
	"""

	entry = error.definition

	document: dict[str, typing.Any] = {
		"type": entry.type_uri,
		"title": entry.title,
		"status": entry.status,
		"detail": error.detail,
		"code": error.code,
	}

	if instance is not None:
		document["instance"] = instance

	if request_id is not None:
		document["request_id"] = request_id

	if error.hint is not None:
		document["hint"] = error.hint

	if error.errors:
		document["errors"] = [field.as_dict() for field in error.errors]

	# Extension members last, and never allowed to overwrite a member RFC 9457 defines: a
	# document whose `status` disagreed with its HTTP status would be worse than one missing
	# whatever the extension wanted to say.
	for name, value in error.extensions.items():
		document.setdefault(name, value)

	return document


def registry_markdown () -> str:
	"""Render the registry as the published ``docs/errors.md``.

	Generated rather than hand-written so the documented contract and the enforced one
	cannot drift apart; a test asserts the file on disk matches this.
	"""

	lines = [
		"# Error codes",
		"",
		"Every error response carries a `code`. These are part of the public contract and",
		"covered by semantic versioning: branch on them freely. Adding a code is a minor",
		"version; renaming or removing one is a major version.",
		"",
		"The `type` URI of a problem document is this page's entry for that code.",
		"",
		"<!-- Generated from subroutine/errors.py. Do not edit by hand. -->",
		"",
		"| Code | HTTP | Title | Meaning |",
		"| --- | --- | --- | --- |",
	]

	for code in sorted(REGISTRY):
		entry = REGISTRY[code]
		meaning = " ".join(entry.description.split())

		lines.append(f"| `{entry.code}` | {entry.status} | {entry.title} | {meaning} |")

	return "\n".join(lines) + "\n"
