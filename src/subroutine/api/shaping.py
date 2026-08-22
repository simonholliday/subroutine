"""Making a response small enough to put in a context window (docs/design.md §14.10).

Response size is a first-order constraint for an agent and almost none for a person. A
verbose task is 400-600 tokens; fifty of them is a substantial fraction of a working
context, spent mostly on fields the agent did not ask for. So this is a product feature and
not an optimisation, which is why it lives in its own module with its own rules rather than
as a flag threaded through the routers.

Two mechanisms, and they are alternatives rather than layers:

* ``?fields=ref,title,due_at`` keeps the JSON shape and drops everything unasked for.
* ``?format=compact`` replaces the object with one aligned line; ``?format=ids`` replaces it
  with the address alone.

**The envelope survives all three.** §14.10 sketches compact output as bare lines, and this
returns ``{"items": ["…", "…"], "page": {…}}`` instead — a deliberate divergence. Bare lines
would put ``next_cursor`` somewhere new, which means pagination, ``include_total`` and
cursors all grow a second convention that exists for one format. The envelope costs a
handful of tokens against a saving of ninety-odd per cent, and a client looks for the cursor
in the place it already looks.
"""

import typing

import fastapi
import fastapi.responses
import pydantic

import subroutine.errors

#: What ``?format=`` accepts. ``full`` is the default and is what every response was before
#: this existed, so an unshaped request is unchanged.
FORMATS = ("full", "compact", "ids")


class Shape(typing.NamedTuple):
	"""What a caller asked a response to look like."""

	format: str = "full"
	fields: tuple[str, ...] | None = None

	@property
	def is_default (self) -> bool:
		"""Report whether this asks for anything at all."""

		return self.format == "full" and self.fields is None


def wanted (
	*,
	format: str | None,
	fields: str | None,
	available: frozenset[str],
	entity: str,
) -> Shape:
	"""Read the two query parameters, or refuse with what would have worked.

	``fields`` together with a non-default ``format`` is **refused rather than resolved**.
	Both name the shape of the response, so a request carrying both has asked for two
	different things and there is no reading of it that honours the caller's intent — which
	is §8.1's rule applied to a pair of parameters rather than to one unknown field.
	"""

	chosen = (format or "full").strip().lower()

	if chosen not in FORMATS:
		raise subroutine.errors.ValidationError(
			f"{format!r} is not a response format.",
			errors=[
				subroutine.errors.FieldError(
					field="format",
					code="invalid_field_value",
					message=f"The formats are: {', '.join(FORMATS)}.",
					hint="'full' is the default and needs no format at all.",
				)
			],
		)

	selected = _fields(fields, available=available, entity=entity)

	if selected is not None and chosen != "full":
		raise subroutine.errors.ValidationError(
			"'fields' and 'format' both describe the response, so only one of them can.",
			errors=[
				subroutine.errors.FieldError(
					field="fields",
					code="invalid_field_value",
					message=f"'fields' selects from the full representation; "
					f"'format={chosen}' replaces it.",
					hint="Drop 'format' to choose fields, or drop 'fields' to take the "
					"compact rendering as it comes.",
				)
			],
		)

	return Shape(format=chosen, fields=selected)


def _fields (
	fields: str | None, *, available: frozenset[str], entity: str
) -> tuple[str, ...] | None:
	"""Split and check a field selection, or return ``None`` when none was asked for.

	Names are the *view's* own, flat. §14.10 sketches ``status.key``, and this takes
	``status`` — because the view deliberately flattens the vocabulary to a key rather than
	nesting it (§8.5, and the note at the top of ``views``). Building nested selection for a
	shape with no nesting in it would be inventing a grammar to fit an example.
	"""

	if fields is None:
		return None

	names = tuple(name.strip() for name in fields.split(",") if name.strip())

	if not names:
		raise subroutine.errors.ValidationError(
			"'fields' was given with nothing in it.",
			errors=[
				subroutine.errors.FieldError(
					field="fields",
					code="invalid_field_value",
					message="Name at least one field, comma-separated.",
					hint=f"For example: fields=ref,title. Every {entity} field is listed in "
					f"GET /v1/meta.",
				)
			],
		)

	unknown = [name for name in names if name not in available]

	if unknown:
		raise subroutine.errors.ValidationError(
			f"{', '.join(repr(name) for name in unknown)} "
			f"{'is not a field' if len(unknown) == 1 else 'are not fields'} of a {entity}.",
			errors=[
				subroutine.errors.FieldError(
					field="fields",
					code="invalid_field_value",
					message=f"The fields of a {entity} are: {', '.join(sorted(available))}.",
					hint="GET /v1/meta lists these too, so this can be checked without "
					"guessing.",
				)
			],
		)

	# Deduplicated, order preserved. `fields=ref,ref` is somebody generating a list, not a
	# request for the field twice.
	return tuple(dict.fromkeys(names))


def applied (items: typing.Sequence[typing.Any], shape: Shape) -> list[typing.Any]:
	"""Return a list of items shaped as the caller asked.

	Takes already-rendered view models, so shaping never reaches into the database and never
	changes which rows a caller gets. It decides how a row is *reported* and nothing else —
	the same division ``api/views`` keeps, and the reason a listing's narrowing cannot be
	affected by a display parameter.
	"""

	if shape.format == "ids":
		return [item.address() for item in items]

	if shape.format == "compact":
		return aligned([item.columns() for item in items])

	if shape.fields is not None:
		return [item.model_dump(mode="json", include=set(shape.fields)) for item in items]

	return list(items)


def aligned (rows: typing.Sequence[typing.Sequence[str]]) -> list[str]:
	"""Join each row into one line, with the columns lined up down the page.

	Alignment is what makes a compact listing scannable rather than merely short, and it is
	computed across the page because that is the only place the widths are known.

	Two things are deliberately not paid for. The last column — always the title — is not
	padded, since trailing spaces would cost tokens on every line to align nothing. And **a
	column empty in every row is dropped entirely**: a project listing with nothing private
	in it was spending two spaces per row on a visibility column that said nothing, which is
	the exact waste this whole module exists to remove.
	"""

	if not rows:
		return []

	width = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
	keep = [index for index, measure in enumerate(width) if measure > 0]

	return [
		"  ".join(
			row[index] if index == keep[-1] else f"{row[index]:<{width[index]}}"
			for index in keep
		).rstrip()
		for row in rows
	]


#: The two query parameters, declared once so all three routers describe them identically
#: and an agent reading the OpenAPI document meets one wording rather than three.
FORMAT_QUERY = fastapi.Query(
	None,
	description="'full' (default), 'compact' for one aligned line per item, or 'ids' for "
	"the addresses alone. Compact is roughly a twentieth the size of full.",
	examples=["compact"],
)

FIELDS_QUERY = fastapi.Query(
	None,
	description="Comma-separated field names to return instead of the whole item, e.g. "
	"'ref,title,due_at'. GET /v1/meta lists what each entity has. Cannot be combined "
	"with 'format'.",
	examples=["ref,title,due_at"],
)


def response (
	items: typing.Sequence[typing.Any],
	page: typing.Any,
	shape: Shape,
	links: typing.Sequence[typing.Any] | None = None,
	covers: typing.Sequence[str] | None = None,
) -> typing.Any:
	"""Return a collection, shaped, and typed loosely enough for FastAPI to leave it alone.

	A shaped response is deliberately **not** validated against the declared response model:
	``items`` holds strings or integers or partial objects, none of which is a ``Task``.
	Returning the model unchanged on the default path keeps the OpenAPI document honest about
	the ordinary case, which is what almost every caller sees.

	``links`` is the ``?include=links`` sibling (§8.4) and is **omitted entirely** rather than
	sent as null when it was not asked for — a listing that did not ask is byte-for-byte what
	it was. It survives shaping untouched on purpose: ``?fields=`` selects fields *of an item*
	and an edge is not one, so asking for two fields and the link graph gives you both rather
	than an empty graph.

	``covers`` is the change feed's statement of which kinds of thing it can carry (`#1085`).
	Unlike ``links`` it is on that endpoint's declared response model, because it is always
	present there — so the OpenAPI document stays honest about the ordinary case, which is the
	rule the paragraph above is applying in the other direction.
	"""

	if shape.is_default and links is None and covers is None:
		return {"items": list(items), "page": page}

	if shape.is_default and links is None:
		return {"items": list(items), "page": page, "covers": list(covers or ())}

	content: dict[str, typing.Any] = {
		"items": [
			item.model_dump(mode="json") if isinstance(item, pydantic.BaseModel) else item
			for item in applied(items, shape)
		],
		"page": page.model_dump(mode="json"),
	}

	if covers is not None:
		content["covers"] = list(covers)

	if links is not None:
		content["links"] = [edge.model_dump(mode="json") for edge in links]

	return fastapi.responses.JSONResponse(content=content)


def single (item: typing.Any, shape: Shape) -> typing.Any:
	"""Return one entity, shaped. Unenveloped, as §8.4 requires of a single entity."""

	if shape.is_default:
		return item

	shaped = applied([item], shape)[0]

	return fastapi.responses.JSONResponse(
		content=shaped.model_dump(mode="json")
		if isinstance(shaped, pydantic.BaseModel)
		else shaped
	)


def selectable (model: type[pydantic.BaseModel]) -> frozenset[str]:
	"""Return the field names a caller may select from one view model.

	Read from the model rather than listed, so a field added to a view is selectable and
	published in ``/v1/meta`` without anybody remembering to say so twice.
	"""

	return frozenset(model.model_fields)


