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

**This applies to every route by default, and a route that does not want it says so in
:data:`NOT_REFUSED`** (`#898`, Simon's decision). It was declared per route by hand until
2026-08-15, which is a list, and the list fell behind three times: five shaping routes went
without it (`#676`), and so did three collections this module's own docstring claimed
(`#897`) — each found by accident rather than by anything failing. The mounting loop in
``api/app.create_app`` attaches it to every router now, so a route added tomorrow is covered
without anybody remembering, and the only thing that can go wrong is an exception somebody
wrote down.

**A route-level dependency runs before the endpoint's own**, so this answers before a body is
validated, before a workspace is resolved and before a credential is read. The last of those
is the one worth checking rather than assuming, and it was: an unauthenticated caller now gets
a `422` naming the parameters instead of a `401`. It discloses nothing, because
``/v1/openapi.json`` is public and already publishes all 34 of them — measured, unauthenticated,
on the served instance. The one place the ordering did matter is a *credential* in the query
string, which is why :func:`_asked_about` steps over ``security.TOKEN_PARAMETERS`` (`#899`).

The forward-compatibility argument this module used to make — that refusing would break a
client which had started sending a parameter early — is the one ``#379`` overturned on the
neighbouring surface, and for the reason that decides it here: what a swallowed parameter
produces is *a plausible, complete, wrong answer*, and all it takes is a client newer than
its server, which is the ordinary state of a fleet.

**And it is worth more than the typos it catches.** Flipping the default found a test fixture
that had been appending ``?workspace_id=`` to three creates for months — where a create names
its workspace in the *body* — so a pin those lines appeared to apply had never once applied,
and could not have been noticed on an installation with one workspace.

**The accepted names are read from the route that matched**, never from a second list.
Starlette puts the resolved route in the request scope, and FastAPI's ``dependant`` knows every
query parameter it declared — so this cannot drift from the signature, and a parameter added to
an endpoint is accepted the moment it exists. A hand-maintained allow-list here would be the
same defect this module exists to prevent, one level up.
"""

import collections.abc
import types
import typing

import fastapi
import starlette.requests

import subroutine.api.filters
import subroutine.api.security
import subroutine.domain.filtering
import subroutine.errors

#: Routes that answer whatever they are asked, keyed ``"METHOD path"`` with the reason —
#: the same shape as ``PUBLIC_ROUTES``, and for the same reason: an exception nobody has to
#: write down is an exception nobody can review. Two tests hold it, one saying every other
#: route enforces and one saying no entry here names a route that has gone.
#:
#: **Every entry is a caller we do not control appending something of its own.** That is the
#: whole test for admission: a monitor's cache-buster, a mail client's tracking parameter, a
#: browser's ``?utm_…``. None of these shapes a response, so nothing is silently overpaid for,
#: and refusing would turn an uptime graph red to protect nothing.
NOT_REFUSED: dict[str, str] = {
	"GET /healthz": "polled by monitors, which append cache-busters nobody controls",
	"GET /readyz": "the same, and an orchestrator's probe must not fail on a spelling",
	"GET /": "the browser app's page, reached from links carrying campaign parameters",
	"GET /app/{name}": "its own files, requested by the browser with whatever it appends",
	# The one route reached *from a mail client*, which is the software most likely to
	# rewrite a URL on the way — and being refused here means being unable to sign in.
	"GET /signin": "opened from an email, so the URL is not only ours by the time it arrives",
	# A 405 that reads nothing (`#648`). The method is the caller's actual mistake, and
	# answering 422 about a parameter instead would name the smaller of two problems.
	"GET /mcp": "refuses the method before anything else, and that is the useful answer",
}


def refuse_unknown (request: starlette.requests.Request) -> None:
	"""Refuse any query parameter the matched endpoint did not declare.

	A no-op when the route cannot be introspected. That is not laziness: this is a *courtesy*
	check, and a version of FastAPI that renamed ``dependant`` should cost a lost refusal rather
	than every listing returning a 500.
	"""

	if _excused(request):
		return

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


def _excused (request: starlette.requests.Request) -> bool:
	"""Report whether this route is one of the few that answers whatever it is asked.

	Named the way :data:`NOT_REFUSED` is keyed, off the route that matched rather than off
	``request.url.path`` — the second would compare ``/app/app.js`` against ``/app/{name}``
	and never match, so every exception would silently stop working while every test that
	spelled a *literal* path went on passing.
	"""

	route: typing.Any = request.scope.get("route")
	path = getattr(route, "path", None)

	if path is None:
		return False

	return f"{request.method} {path}" in NOT_REFUSED


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
	#
	# **Compared folded, because the step-over has to reach exactly what the refusal does**
	# (`#946`). This was an exact match while the refusal became case-insensitive, so `?TOKEN=`
	# was stepped over by neither and answered `unknown_field` from here — `#899`'s defect
	# restored by a fix to one of its two halves. **Third site**, and the one that decides:
	# whichever of these runs first wins, and this one is a route dependency.
	names = {
		name
		for name in request.query_params
		if name.lower() not in subroutine.api.security.CREDENTIAL_PARAMETERS
	}

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


def refuse_repeated (request: starlette.requests.Request) -> None:
	"""Refuse a query parameter given more than once, where the route declares one value.

	**Measured 2026-08-28 on the served instance** (`#1484`): `?type=bug&type=spike` answers
	`200` with spikes and `?status=open&status=done` answers `200` with finished work. The
	caller asked two questions and was answered one, with nothing saying which — the shape
	`#1468` has already been fixed once on the other half of a listing, and the shape this
	module's own docstring is about: *a plausible, complete, wrong answer*.

	**Every query parameter this API declares is scalar**, measured across the routes rather
	than assumed, so today this refuses every repetition. It reads the annotation anyway,
	because the day one is declared a sequence is the day repeating it becomes the way to use
	it — and a check that had hardcoded *always refuse* would have to be found and changed by
	whoever adds it, which is the maintained-list defect this module exists to avoid.

	**A dotted date filter is not covered here** and says so rather than being silently
	included: those names are not declared on the route at all (`api/filters` owns them and
	:func:`refuse_unknown` lets them through on faith), so this has nothing to read an
	annotation from. `#1484` records that gap.

	Simon's decision of 2026-08-28: **refuse rather than union**. A union has to reach the
	domain, both clients and the published contract, and five filters share this shape; a
	refusal is one sentence at the door. It is also the forward-compatible direction —
	refusing now does not stop us accepting a union later, and unioning now and refusing later
	would be a break.
	"""

	if _excused(request):
		return

	scalar = _scalar(request)

	if scalar is None:
		return

	seen: dict[str, int] = {}

	for name, _value in request.query_params.multi_items():
		seen[name] = seen.get(name, 0) + 1

	repeated = sorted(name for name, times in seen.items() if times > 1 and name in scalar)

	if not repeated:
		return

	raise subroutine.errors.ValidationError(
		f"{repeated[0]!r} takes one value and was given "
		f"{seen[repeated[0]]}.",
		code="invalid_field_value",
		errors=[
			subroutine.errors.FieldError(
				field=name,
				code="invalid_field_value",
				message=f"{name!r} was given {seen[name]} times: "
				f"{', '.join(repr(value) for key, value in request.query_params.multi_items() if key == name)}.",
				hint="Ask for one of them. Repeating it kept only the last, which answered a "
				"narrower question than you asked without saying so.",
			)
			for name in repeated
		],
	)


def _scalar (request: starlette.requests.Request) -> frozenset[str] | None:
	"""Return the declared query parameters that hold a single value, or ``None``.

	A sequence annotation — ``list[str]``, and ``list[str] | None`` for one that may be
	omitted — is a parameter whose *meaning* is that it repeats, so it is excluded rather than
	refused. None exists today; the rule is read from the signature so that the first one to
	be declared works without anybody remembering this function.
	"""

	route: typing.Any = request.scope.get("route")
	dependant = getattr(route, "dependant", None)
	declared = getattr(dependant, "query_params", None)

	if declared is None:
		return None

	found: set[str] = set()

	for field in declared:
		alias = getattr(field, "alias", None)

		if alias is None:
			continue

		annotation = getattr(getattr(field, "field_info", None), "annotation", None)

		if not _takes_many(annotation):
			found.add(alias)

	return frozenset(found)


def _takes_many (annotation: typing.Any) -> bool:
	"""Report whether an annotation is a sequence, looking inside an optional.

	``list[str] | None`` is two things at once, so the union is walked rather than tested — a
	check that asked only about the outer type would call every optional parameter scalar,
	including the sequences, which is the direction that fails silently.
	"""

	if annotation is None:
		return False

	origin = typing.get_origin(annotation)

	if origin in (typing.Union, types.UnionType):
		return any(_takes_many(one) for one in typing.get_args(annotation))

	return isinstance(origin, type) and issubclass(origin, collections.abc.Sequence) and origin is not str


def _asked_once_and_by_name (request: starlette.requests.Request) -> None:
	"""Both refusals, in the order that gives the better message.

	A name that is unknown *and* repeated is a typo rather than an ambiguity, so
	:func:`refuse_unknown` answers first and says what the endpoint accepts.
	"""

	refuse_unknown(request)
	refuse_repeated(request)


#: Declared on every collection endpoint and on the agenda. A dependency rather than a call in
#: each handler, so it runs before the body of the endpoint and cannot be forgotten half-way
#: down one.
UnknownQueryDep = fastapi.Depends(_asked_once_and_by_name)


#: What ``?include=`` may name, per entity. Short on purpose: every entry is a promise that
#: the extra costs a bounded number of queries, and an include that fans out per row is the
#: N+1 this parameter exists to remove, moved inside the server where the caller cannot see
#: it.
#:
#: **``backlinks`` is specified in §8.5 and is deliberately not here** (`#144`). It is served
#: as a sub-resource — ``GET /v1/tasks/{id_or_ref}/backlinks`` — because on a page of fifty it
#: is either fifty lookups or a join nobody asked for, which is the N+1 this parameter exists
#: to remove rather than to hide. Every other section ``subroutine show`` renders is a
#: sub-resource too: links, comments and history.
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
