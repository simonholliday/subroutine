"""The base every request body inherits, and the one rule it exists to hold.

**Unknown fields are refused, never ignored.** A caller that sends ``due`` where the field
is ``due_at`` gets a 422 naming the field and listing the ones that exist, rather than a
201 for a task with no due date and an agent that believes it set one (docs/design.md §8.1).

Pydantic's default is to drop what it does not recognise, so this has to be asked for —
which means it has to be asked for on every model, which means it belongs on a base class
rather than in a habit.
"""

import typing

import fastapi
import pydantic

import subroutine.domain.refs

#: The path segment that names one task or document.
#:
#: Declared once and shared by every addressed endpoint, because the *description* is the
#: point: without it the published schema says only "string", and an agent has no way to
#: learn that a bare number works short of being refused — which is the thing §13.1 exists
#: to prevent.
ItemAddress = typing.Annotated[
	str,
	fastapi.Path(
		description="The item's ref — a plain integer, as returned in `ref` — or its id. "
		"`42` and `019f…` are both accepted. Write `#42` in prose, never in a URL.",
		examples=["42"],
	),
]


def _reference (value: typing.Any) -> typing.Any:
	"""Refuse the values a bare ``int | str`` union would quietly accept.

	Two of them, both found by probing the union rather than by reading it:

	* **A boolean.** ``bool`` is a subclass of ``int`` and pydantic's lax mode coerces it,
	  so ``{"target": true}`` arrived as ref ``1`` and ``false`` as ref ``0``. A client bug
	  became a successful write against the wrong item instead of a refusal.
	* **A number too large to be a ref.** Out of range is refused *here*, with a 422 naming
	  the limit, because a body field is being validated rather than resolved. The same
	  number in a path is a 404 — nothing answers to it — which is why
	  :func:`subroutine.domain.refs.parse_ref` returns ``None`` instead of raising.

	Raising ``ValueError`` rather than building a problem document: pydantic turns it into
	the 422 the rest of the request-validation path already produces, so the shape of the
	error stays the same as every other field's.
	"""

	if isinstance(value, bool):
		raise ValueError("a reference is an item's number or its id, not a boolean")

	# A digit string and an integer mean the same thing and must be bounded the same way,
	# or `{"target": 2147483648}` is refused and `{"target": "2147483648"}` is a 404.
	if isinstance(value, str) and value.strip().lstrip(subroutine.domain.refs.SIGIL).isdigit():
		candidate: int | None = int(value.strip().lstrip(subroutine.domain.refs.SIGIL))

	elif isinstance(value, int):
		candidate = value

	else:
		candidate = None

	if candidate is not None and not 1 <= candidate <= subroutine.domain.refs.MAX_REF:
		raise ValueError(
			f"an item's number is between 1 and {subroutine.domain.refs.MAX_REF}"
		)

	return value


#: A field that names another work item, by ref or by id.
#:
#: A ref is an integer in every response this API sends (docs/design.md §6.2), so an integer is
#: accepted here as well as a string: a client should be able to send back what it was
#: given without converting it, and a rule that says "read a number, quote it, send it" is
#: a rule somebody will get wrong. An id is a UUID and arrives as a string.
Reference = typing.Annotated[int | str, pydantic.BeforeValidator(_reference)]


class RequestModel(pydantic.BaseModel):
	"""Base for every body this API accepts."""

	model_config = pydantic.ConfigDict(extra="forbid")
