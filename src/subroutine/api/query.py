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

**A single-entity read shapes too, and this module excluded them for three months on the
grounds that one "wastes nothing"** — which was reasoned rather than measured, and is wrong
(`#676`). Every single read declares ``fields`` and ``format``, so a typo costs the whole
object there exactly as it does on a listing: measured against this instance,
``/v1/tasks/676`` went from 137 bytes to 3,150, and ``/v1/documents/4`` from 59 bytes to
99,746 — **1,690 times the payload for one wrong letter**, answered `200`.

**Attached per route, and what carries it is not a rule anybody could state** — which is how
five shaping routes came to be without it and how three collections still are (`#897`). The
one guarantee is the derived half: every route declaring ``fields`` or ``format`` has this,
held by ``test_every_route_that_shapes_refuses_a_parameter_it_does_not_declare``. Everything
beyond that is a route somebody remembered, so do not read a list of what is covered off this
paragraph — the sentence that used to be here claimed the collections and was wrong about
three of them for months. ``#898`` is whether the default should flip.

What would be excluded under any rule: the health checks, the sign-in page, the browser's own
pages and the two prose documents, all of which are reached by monitors and mail clients that
append parameters of their own. Refusing those would turn somebody's uptime graph red to
protect nothing.

The forward-compatibility argument this module used to make — that refusing would break a
client which had started sending a parameter early — is the one ``#379`` overturned on the
neighbouring surface, and for the reason that decides it here: what a swallowed parameter
produces is *a plausible, complete, wrong answer*, and all it takes is a client newer than
its server, which is the ordinary state of a fleet.

**The accepted names are read from the route that matched**, never from a second list.
Starlette puts the resolved route in the request scope, and FastAPI's ``dependant`` knows every
query parameter it declared — so this cannot drift from the signature, and a parameter added to
an endpoint is accepted the moment it exists. A hand-maintained allow-list here would be the
same defect this module exists to prevent, one level up.
"""

import typing

import fastapi
import starlette.requests

import subroutine.api.filters
import subroutine.api.security
import subroutine.domain.filtering
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

	unknown = sorted(_asked_about(request) - accepted)

	if not unknown:
		return

	# **An endpoint that declares none is a real case and needs its own sentence** —
	# ``GET /v1/me`` takes nothing at all, and "It accepts: ." reads as a truncated message
	# rather than as an answer.
	listed = (
		f"It accepts: {', '.join(sorted(accepted))}."
		if accepted
		else "It takes no query parameters at all."
	)

	raise subroutine.errors.ValidationError(
		f"This endpoint does not accept {unknown[0]!r}.",
		code="unknown_field",
		errors=[
			subroutine.errors.FieldError(
				field=name,
				code="unknown_field",
				message=f"{name!r} is not a parameter of this endpoint.",
				hint=listed,
			)
			for name in unknown
		],
		hint="Refused rather than ignored, because a request that quietly ignores 'fields' "
		"returns the whole object and charges you for it.",
	)


def _asked_about (request: starlette.requests.Request) -> set[str]:
	"""Return the parameter names this function is responsible for.

	**Everything except §9.6's dotted form, and only where a route declares a reader for it**
	(`#815`). The two halves are deliberately blind to each other's vocabulary: this owns the
	flat names, :mod:`subroutine.api.filters` owns the ones carrying a separator, and neither
	holds a list of the other's — which is what stops them drifting into disagreement.

	A listing that declares no reader is unchanged, so ``?created_at.gte=`` on an endpoint
	that cannot filter on dates is still refused by name rather than ignored.
	"""

	# **A misplaced credential is not this function's to report** (`#899`). It is unknown here
	# by any measure, and answering "'token' is not a parameter of this endpoint" reads as a
	# typo — so the caller corrects the spelling and never revokes the secret now sitting in
	# an access log. `security.principal` refuses it by name, before authenticating, and says
	# to treat it as compromised. Left in place this ran first and swallowed that.
	names = set(request.query_params) - set(subroutine.api.security.TOKEN_PARAMETERS)

	if subroutine.api.filters.declared_by(request.scope.get("route")) is None:
		return names

	return {name for name in names if subroutine.domain.filtering.SEPARATOR not in name}


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
