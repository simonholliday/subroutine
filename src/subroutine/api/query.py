"""Refusing a query parameter this endpoint does not accept.

Request *bodies* have refused unknown fields since S3-01 — ``extra="forbid"`` on every
request model — for the reason §8.1 gives: "silently dropping a typo is how a caller comes to
believe it set something it did not". Query strings did not, because that is FastAPI's
default and the conventional behaviour for an HTTP API.

**On a listing, that convention costs too much.** ``?fieldz=ref,title`` returns full objects
and a `200`; the caller asked for a tenth of the payload, got all of it, paid ten times the
tokens, and was told nothing. Context economy is a first-order cost for an agent (§14.10 is
an entire section about it), so a listing is the one place where an ignored parameter is
expensive rather than merely untidy. ``?include=backlinks`` is the same shape: it is specified
in §8.5, it is not built, and it used to return `200` with nothing.

Applied to the **collection** endpoints and the agenda only, and deliberately not to
everything. A single-entity read taking an unknown parameter wastes nothing, and refusing
unknown parameters wholesale would make adding one a breaking change for any client that had
started sending it early.

**The accepted names are read from the route that matched**, never from a second list.
Starlette puts the resolved route in the request scope, and FastAPI's ``dependant`` knows every
query parameter it declared — so this cannot drift from the signature, and a parameter added to
an endpoint is accepted the moment it exists. A hand-maintained allow-list here would be the
same defect this module exists to prevent, one level up.
"""

import typing

import fastapi
import starlette.requests

import subroutine.errors


def refuse_unknown (request: starlette.requests.Request) -> None:
	"""Refuse any query parameter the matched endpoint did not declare.

	A no-op when the route cannot be introspected. That is not laziness: this is a *courtesy*
	check, and a version of FastAPI that renamed ``dependant`` should cost a lost refusal rather
	than every listing returning a 500.
	"""

	accepted = _accepted(request)

	if accepted is None:
		return

	unknown = sorted(set(request.query_params) - accepted)

	if not unknown:
		return

	listed = ", ".join(sorted(accepted))

	raise subroutine.errors.ValidationError(
		f"This endpoint does not accept {unknown[0]!r}.",
		code="unknown_field",
		errors=[
			subroutine.errors.FieldError(
				field=name,
				code="unknown_field",
				message=f"{name!r} is not a parameter of this endpoint.",
				hint=f"It accepts: {listed}.",
			)
			for name in unknown
		],
		hint="Refused rather than ignored, because a listing that quietly ignores 'fields' "
		"returns the whole object and charges you for it.",
	)


def _accepted (request: starlette.requests.Request) -> frozenset[str] | None:
	"""Return the query-parameter names the matched route declares, or ``None``.

	Reads the route out of the request scope rather than out of ``app.routes``, which
	``include_router`` leaves full of opaque ``_IncludedRouter`` objects with no path at all
	(the same trap ``api/routing.check`` exists to work around).
	"""

	route: typing.Any = request.scope.get("route")
	dependant = getattr(route, "dependant", None)

	if dependant is None:
		return None

	declared = getattr(dependant, "query_params", None)

	if declared is None:
		return None

	return frozenset(
		alias for field in declared if (alias := getattr(field, "alias", None)) is not None
	)


#: Declared on every collection endpoint and on the agenda. A dependency rather than a call in
#: each handler, so it runs before the body of the endpoint and cannot be forgotten half-way
#: down one.
UnknownQueryDep = fastapi.Depends(refuse_unknown)


#: What ``?include=`` may name, per entity. Short on purpose: every entry is a promise that
#: the extra costs a bounded number of queries, and an include that fans out per row is the
#: N+1 this parameter exists to remove, moved inside the server where the caller cannot see
#: it. ``backlinks`` is specified in §8.5 and is **not** here, because nothing implements it —
#: a name accepted and ignored is exactly the failure this module was written for.
INCLUDABLE: dict[str, frozenset[str]] = {
	"task": frozenset({"links"}),
	"document": frozenset({"links"}),
}

#: Declared once so both listings describe it identically.
INCLUDE_QUERY = fastapi.Query(
	None,
	description=(
		"Extras to return beside the items, comma-separated. `links` adds a `links` array "
		"of the links among this page's items — each one `{id, link_type, label, source, "
		"target}`, reported once however many of its ends are on the page. Absent unless "
		"asked for, and unaffected by `fields`."
	),
)


def includes (requested: str | None, name: str, *, entity: str) -> bool:
	"""Return whether ``?include=`` asked for one named extra, refusing anything unknown.

	Refused rather than ignored, for the reason in this module's docstring: a caller who
	writes ``?include=link`` and is told nothing will believe they received a link graph and
	find an empty one, which is a harder bug to see than a 422.
	"""

	if not requested:
		return False

	available = INCLUDABLE.get(entity, frozenset())
	asked = [part.strip() for part in requested.split(",") if part.strip()]

	for part in asked:
		if part not in available:
			raise subroutine.errors.ValidationError(
				f"{part!r} is not something this endpoint can include.",
				errors=[
					subroutine.errors.FieldError(
						field="include",
						code="invalid_field_value",
						message=f"Unknown include {part!r}.",
						hint=(
							f"This endpoint includes: {', '.join(sorted(available))}."
							if available
							else "This endpoint has nothing to include."
						),
					)
				],
			)

	return name in asked
