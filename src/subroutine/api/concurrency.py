"""Reading the version a caller expects, from wherever it chose to put it.

docs/design.md §8.9 offers two places on purpose: ``If-Match``, which is what HTTP says and what a
cache or a proxy understands, and ``expected_version`` in the body, "for clients that find
headers awkward" — which, in practice, means anything driving the API from a shell script or
a notebook. Neither is required, and sending both is fine as long as they agree.

The header is quoted (``If-Match: "7"``), because that is what an entity tag is; the quotes
are stripped rather than demanded, since a caller that sends ``If-Match: 7`` meant the same
thing and refusing it would teach nobody anything.

**No response carries an ``ETag``, deliberately.** The tag a caller sends back is the
``version`` field, which every entity already publishes and every client here already reads —
so an ``ETag`` header would be a second copy of it, and one carrying cache semantics this API
does not implement: nothing here honours ``If-None-Match`` and no entity response is
cacheable. There was an ``etag()`` helper for a header nothing sent, which is the inert
control this project keeps finding, and `#303`'s answer to one of those is usually to delete
rather than wire. The agent guide and §8.9 both say to send the version, which is what the
version is.
"""

import contextlib
import typing

import pydantic
import starlette.requests

import subroutine.errors

#: Weak validators (``W/"7"``) mean "semantically equivalent", which is not a claim this
#: can check. Accepted and treated as the strong form, because the version *is* the
#: entity's identity here — §8.9 calls it a concurrency token, not a cache validator.
_WEAK_PREFIX = "W/"


def expected (
	request: starlette.requests.Request, body_value: int | None = None
) -> int | None:
	"""Return the version the caller expects this entity to be at, or ``None``.

	``None`` means the caller did not ask for the check — not that it asked and passed.
	"""

	header = _from_header(request)

	if header is not None and body_value is not None and header != body_value:
		raise subroutine.errors.ValidationError(
			"This request expects two different versions.",
			errors=[
				subroutine.errors.FieldError(
					field="expected_version",
					code="invalid_field_value",
					message=f"The If-Match header says {header} and the body says {body_value}.",
					hint="Send one or the other, or make them agree.",
				)
			],
		)

	return header if header is not None else body_value


def _from_header (request: starlette.requests.Request) -> int | None:
	"""Read ``If-Match``, or ``None`` when it is absent."""

	raw = request.headers.get("if-match")

	if raw is None:
		return None

	value = raw.strip()

	if value.startswith(_WEAK_PREFIX):
		value = value[len(_WEAK_PREFIX) :]

	value = value.strip('"')

	# `If-Match: *` means "any current version", which is what sending nothing already
	# means here. Honoured rather than refused, since a client using it is asking for
	# something weaker than the check, not something this cannot do.
	if value == "*":
		return None

	try:
		return int(value)

	except ValueError:
		raise subroutine.errors.ValidationError(
			f"{raw!r} is not a version this API can compare against.",
			errors=[
				subroutine.errors.FieldError(
					field="If-Match",
					code="invalid_field_value",
					message="An entity tag here is the version number, as reported in the "
					"'version' field of the entity.",
					hint='Send If-Match: "7", or omit the header.',
				)
			],
		) from None


@contextlib.contextmanager
def reporting (render: typing.Callable[[], typing.Any]) -> typing.Iterator[None]:
	"""Attach the current entity to a version conflict raised inside this block.

	§8.9 promises the 409 carries "the current entity, so the caller can merge rather than
	refetch", and that is the difference between an error a program can act on and one that
	only tells it to try again. The service layer cannot supply it — rendering is the API's
	job and the domain has no business knowing about views — so the router adds it here.

	``render`` is a callable rather than a value because it must not run on the ordinary
	path: rendering costs a vocabulary lookup, and paying for it on every successful write
	to serve an error that did not happen would be a poor trade. The entity is unchanged
	when this fires, since :func:`subroutine.domain.versions.require` runs before a service
	assigns anything.
	"""

	try:
		yield

	except subroutine.errors.SubroutineError as error:
		if error.code == "version_conflict":
			current = render()

			# Reduced to plain JSON types here rather than handed over as a model.
			# ``subroutine.errors`` knows nothing about frameworks by design, and its
			# extensions are documented as members of the problem *document* — so anything
			# put there has to already be something JSON can carry.
			error.extensions["current"] = (
				current.model_dump(mode="json")
				if isinstance(current, pydantic.BaseModel)
				else current
			)

			# **And say so, because only here is it true** (`#1698`). The hint used to carry
			# *the current one is in this response* from the domain, where it was false for
			# every caller that does not come through a router — a terminal reader, and an
			# agent on a local connection, were told to look in a response they never had.
			# The sentence belongs to whoever attaches the entity.
			error.hint = (
				f"{error.hint} The current one is in this response, under 'current'."
				if error.hint
				else "The current one is in this response, under 'current'."
			)

		raise
