"""What goes wrong, said in a way a program can act on.

Every failure the API reports is one of the exceptions here, and every one of them carries
a ``code`` from the registry below. The codes are **part of the public contract** and
covered by semantic versioning (docs/design.md §8.8): a client may branch on them, so renaming
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

import contextlib
import dataclasses
import typing

#: Base for the ``type`` URI of a problem document. One URI per code, so it resolves to the
#: registry entry describing it. RFC 9457 does not require this to be dereferenceable; it is
#: far more useful if it is, and this one is.
#:
#: **The repository, not a product domain** (Simon, 2026-08-01, `#163`). It was
#: ``https://subroutine.dev/errors`` until then — a domain the project does not own, serving a
#: placeholder page from 2022, with ``/errors`` returning 404. So every problem document from
#: every self-hosted instance pointed its readers at a third party who could serve them
#: anything. This resolves today, is owned, and is where the registry actually lives; a
#: product domain can replace it later, which is a major version because §8.8 makes these a
#: public contract.
ERROR_TYPE_BASE = (
	"https://github.com/simonholliday/subroutine/blob/main/docs/errors.md"
)


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

		# A fragment rather than a path segment, because the registry is one page. GitHub's
		# slugger keeps underscores, so a heading of the bare code anchors to the bare code.
		return f"{ERROR_TYPE_BASE}#{self.code}"


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
			"The request could not be read at all — bad JSON, or a header this API has to "
			"parse and could not. A parameter of the wrong shape is a different answer: the "
			"request was read, so it is 422 'invalid_field_value' naming the parameter.",
		),
		_define(
			"unsupported_protocol_version",
			400,
			"Unsupported protocol version",
			"A client announced an MCP revision this server does not speak, which the "
			"Streamable HTTP transport requires be refused rather than answered as though "
			"it were understood. The revision this server does speak is named, so a client "
			"can decide whether to continue. Distinct from 'malformed_request' because the "
			"request was read perfectly well.",
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
			"The credential is valid but does not permit this. Where the refusal turns on "
			"a permission the caller lacks, that permission is named so they can ask for a "
			"token carrying it — but several do not: a token pinned to another workspace, a "
			"caller who is not a member, and a project scope narrower than the project "
			"reached are each about reach rather than about a verb.",
		),
		_define(
			"not_found",
			404,
			"Not found",
			"There is no such thing, or it is not visible to this caller. The two are "
			"deliberately not distinguished: saying 'forbidden' about a private project "
			"would confirm it exists (docs/design.md §7.3a).",
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
			"in_use",
			409,
			"Still in use",
			"The thing being removed is still referenced — a status some tasks are in, a "
			"link type some links use. The message says how many, so the caller can move "
			"them rather than guess. Removing a *tag* is deliberately not this: taking a "
			"label off the things it is on is what deleting a label means.",
		),
		_define(
			"cycle_detected",
			409,
			"Cycle detected",
			"The change would make something its own ancestor, in a project tree, a task "
			"hierarchy or a chain of blocking links.",
		),
		_define(
			"schema_mismatch",
			409,
			"Schema mismatch",
			"A database schema does not match the one this build expects. An older schema "
			"can be migrated forward and the refusal says so; a *newer* one cannot, because "
			"this version cannot interpret data it does not know the shape of and a partial "
			"read is worse than a clear failure. Two things answer with it: a backup being "
			"put back (docs/design.md §12.6), and any write against a live database that has "
			"not been migrated yet (§12.4a) — reads are still served, so an instance mid-deploy "
			"stays readable and refuses to be changed. /readyz reports the same condition as "
			"503 service_unavailable rather than this, because a load balancer has to read the "
			"instance as not ready rather than as arguing.",
		),
		_define(
			"cursor_expired",
			410,
			"Cursor expired",
			"A change-feed cursor names a point older than the events this instance still "
			"holds, so the gap between there and now cannot be reported (docs/design.md §5.11). "
			"The client resyncs from the beginning rather than being handed a page that "
			"silently omits everything pruned in between.",
		),
		_define(
			"payload_too_large",
			413,
			"Too large",
			"A field or the request body exceeds the configured limit. The limit is "
			"reported rather than the value being silently truncated (docs/design.md §6.10).",
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
			"The request carried a field or query parameter this endpoint does not "
			"accept. Rejected rather than ignored, because silently dropping a typo is how "
			"a caller comes to believe it set something it did not (docs/design.md §8.1).",
		),
		_define(
			"invalid_status",
			422,
			"Invalid status",
			"The status asked for cannot be used here. Usually no status with that key "
			"exists for this entity type in this workspace, and then the valid keys are "
			"listed — an installation may rename them freely, so they are read from the "
			"workspace rather than assumed. It also reports a workspace with no default "
			"status at all, where there are no keys to list and the answer is to seed it.",
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
			"request_timed_out",
			503,
			"Timed out",
			"The database work behind this request ran longer than 'request_timeout_seconds' "
			"allows, and was given up on. Distinct from 'service_unavailable', which says "
			"the instance cannot serve anything yet: this instance is serving, and it was "
			"this request that did not finish. The detail names what was being waited for "
			"where the database said, and retrying may work.",
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
	"""One thing wrong with one field, and what to do about it.

	**``code`` has to be one the registry defines** (`#948`, cold review `#927`'s L-6). These
	reach a caller inside the ``errors`` array of a problem document, ``docs/errors.md`` calls
	them a public semver'd contract, and one site had invented ``"required"`` — which is in no
	registry, so :func:`from_problem` dropped it and this project's own client disagreed with
	its own wire about a code it publishes as stable.

	Checked here rather than left to a guard over the tree, because the wrong code is a value
	somebody passes and the check costs a dictionary lookup at the point of the mistake.
	"""

	field: str
	code: str
	message: str
	hint: str | None = None

	def __post_init__ (self) -> None:
		"""Refuse a code no registry defines."""

		if self.code not in REGISTRY:
			raise ValueError(
				f"{self.code!r} is not a registered error code, so a caller reading it would "
				f"be reading something this API does not publish. Registered codes are: "
				f"{', '.join(sorted(REGISTRY))}."
			)

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


class UnsupportedProtocolVersion(SubroutineError):
	"""A client announced an MCP revision this server does not speak."""

	CODE = "unsupported_protocol_version"


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


class InUse(SubroutineError):
	"""Something still points at what the caller asked to remove.

	**The database already refuses this** — the foreign keys into the vocabulary are
	``ondelete="RESTRICT"`` — so this is not the safety. It is the difference between a
	sentence naming what is in the way and an ``IntegrityError`` reaching a caller as a 500
	naming a constraint, which is `#46`'s shape.
	"""

	CODE = "in_use"


class SchemaMismatch(SubroutineError):
	"""A backup was taken on a schema this installation cannot put back."""

	CODE = "schema_mismatch"


class PayloadTooLarge(SubroutineError):
	"""Something exceeds a configured limit."""

	CODE = "payload_too_large"


class CursorExpired(SubroutineError):
	"""A change-feed cursor points further back than this instance can still report."""

	CODE = "cursor_expired"


class ValidationError(SubroutineError):
	"""The request was well-formed but cannot be carried out as written."""

	CODE = "invalid_field_value"


class RateLimited(SubroutineError):
	"""The caller is going too fast."""

	CODE = "rate_limited"


class InternalError(SubroutineError):
	"""Something failed that should not have."""

	CODE = "internal_error"


class RequestTimedOut(SubroutineError):
	"""The database work behind this request was given up on."""

	CODE = "request_timed_out"


class ServiceUnavailable(SubroutineError):
	"""The instance is running but not yet able to serve requests."""

	CODE = "service_unavailable"


def no_instance_yet () -> "ServiceUnavailable":
	"""Return the refusal for a machine where nobody has run ``init`` yet (`#165`, `#698`).

	**One sentence because more than one surface says it.** The command line has answered this
	since `#165` and the API raised instead — so the commonest first contact there, *install the
	plugin, ask a question, no instance yet*, filled an editor's log with 190 lines of traceback
	per message for a state with a known one-line answer. An operator reading that concludes the
	database is broken, which is `#573`'s worst category: a thing that works and says something
	false about itself.

	**It opens in lower case deliberately.** The command line prints the connection's name in
	front of it, and capitalising here would read *"workshop: No Subroutine instance…"*.
	"""

	return ServiceUnavailable(
		"no Subroutine instance has been set up here yet.",
		hint="Run 'subroutine init' to create one. It takes no arguments.",
	)


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


#: Which class reports each status. The **fallback**, for a code no class names — four codes
#: share 409 and four share 422, so a status alone cannot say which exception was raised.
_BY_STATUS: dict[int, type[SubroutineError]] = {
	400: BadRequest,
	401: Unauthenticated,
	403: Forbidden,
	404: NotFound,
	405: MethodNotAllowed,
	409: Conflict,
	410: CursorExpired,
	413: PayloadTooLarge,
	422: ValidationError,
	429: RateLimited,
	500: InternalError,
	503: ServiceUnavailable,
}

#: Which class reports each *code*, which is what :func:`from_problem` asks first.
#:
#: **Derived from the classes rather than written out** (`#948`, cold review `#927`'s L-4), so
#: an exception added above cannot be left out of it. Written out, this is a second list that
#: has to agree with the first, and the defect it fixes is exactly a disagreement of that kind.
#:
#: **Keying only on status was wrong and looked right.** `SchemaMismatch` raised locally came
#: back over HTTP as `Conflict` — the code survived, so every message read correctly, and only
#: `except SchemaMismatch` could tell. Latent for that one, since no route emits it; not latent
#: for `unsupported_protocol_version`, which shares 400 with `malformed_request` and which
#: `POST /mcp` emits.
_BY_CODE: dict[str, type[SubroutineError]] = {
	found.CODE: found
	for found in globals().values()
	if isinstance(found, type)
	and issubclass(found, SubroutineError)
	and found is not SubroutineError
}

#: The members :func:`problem_document` writes itself. Anything else in a document is an
#: RFC 9457 extension member and is carried back as one.
_DOCUMENT_MEMBERS = frozenset(
	{"type", "title", "status", "detail", "code", "instance", "request_id", "hint", "errors"}
)


def from_problem (
	document: typing.Mapping[str, typing.Any], *, status: int | None = None
) -> SubroutineError:
	"""Read a problem document back into the exception that would have raised it.

	The inverse of :func:`problem_document`, and it lives beside it so the two cannot drift.
	This is what lets a connection over HTTP refuse in exactly the words a local one does
	(docs/design.md §13.7): a client fanning out across a local database and a remote server must
	not have two vocabularies of failure, or every message it prints has to say which kind of
	failure it was before it says what went wrong.

	Nothing here trusts the document. A ``code`` that is not in the registry falls back to
	the HTTP status, and a status that is not a failure we publish becomes an internal error
	— which is the truth, because something answered in a shape this program does not
	define.
	"""

	code = document.get("code")
	registered = REGISTRY.get(code) if isinstance(code, str) else None

	if registered is not None:
		reported = registered.status

	else:
		code = None
		reported = _status(document.get("status"), status)

	# **The code decides, and the status is the fallback** (`#948`). Several codes share a
	# status, so choosing by number alone rebuilt the wrong class for four of the nine that
	# collide — and the *code* came through intact, so nothing in the message gave it away.
	failure = _BY_CODE.get(code or "") or _BY_STATUS.get(reported, InternalError)
	detail = document.get("detail")
	hint = document.get("hint")

	return failure(
		str(detail) if isinstance(detail, str) and detail else f"HTTP {reported}.",
		code=code,
		errors=_field_errors(document.get("errors")),
		hint=str(hint) if isinstance(hint, str) else None,
		extensions={
			name: value
			for name, value in document.items()
			if name not in _DOCUMENT_MEMBERS
		},
	)


def _status (reported: typing.Any, actual: int | None) -> int:
	"""Read a problem document's ``status`` member, trusting nothing about it.

	``int(document.get("status") or …)`` was here until 2026-07-30, which raised ``ValueError``
	on ``"abc"`` and ``TypeError`` on ``[500]`` — from inside the function whose whole job is to
	*translate* a remote failure into a local one. Neither is a :class:`SubroutineError`, so a
	half-conformant remote crashed the client's fan-out instead of being named and skipped, and
	this function's docstring claimed "nothing here trusts the document" while trusting exactly
	this member.

	The HTTP status the response really carried wins over anything the body says about itself,
	because a body can be forged, cached or rewritten by a proxy and the status line cannot be
	misread the same way. A body value is used only when there is no real status to hand — which
	is the case when this is called on a document read from a file or a queue.
	"""

	if actual is not None:
		return actual

	if isinstance(reported, bool):
		return 500

	if isinstance(reported, int):
		return reported

	if isinstance(reported, str):
		with contextlib.suppress(ValueError):
			return int(reported.strip())

	return 500


def _field_errors (value: typing.Any) -> tuple[FieldError, ...]:
	"""Read the ``errors`` array of a problem document, skipping anything malformed."""

	if not isinstance(value, list):
		return ()

	found: list[FieldError] = []

	for item in value:
		if not isinstance(item, dict):
			continue

		field, code, message = item.get("field"), item.get("code"), item.get("message")

		if not isinstance(field, str) or not isinstance(message, str):
			continue

		hint = item.get("hint")

		found.append(
			FieldError(
				field=field,
				# A field error's code is reported as sent when it is one we publish, and as
				# the generic one otherwise. It is display text here, not a decision.
				code=code if isinstance(code, str) and code in REGISTRY else "invalid_field_value",
				message=message,
				hint=hint if isinstance(hint, str) else None,
			)
		)

	return tuple(found)


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
		"The `type` URI of a problem document links to this page's entry for that code, so",
		"following one lands on the section describing it.",
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

	# **A section per code as well as the row**, because the table has no anchors and the
	# `type` URI has to land on the entry rather than on the top of the page. The table is
	# what somebody scans; these are what a link resolves to.
	for code in sorted(REGISTRY):
		entry = REGISTRY[code]

		lines.extend(
			[
				"",
				f"## {entry.code}",
				"",
				f"**{entry.title}** — HTTP {entry.status}.",
				"",
				" ".join(entry.description.split()),
			]
		)

	return "\n".join(lines) + "\n"
