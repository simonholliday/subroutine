"""Reaching §9.6's dotted filters from a listing — item `#815`, decision `#817`.

``GET /v1/tasks?created_at.gte=yesterday&project=subroutine`` — a comparison written as
``field.operator=value``, alongside every flat parameter the endpoint already takes.
:mod:`subroutine.domain.filtering` owns the grammar; this is the seam that lets HTTP reach it.

**The seam is the interesting part, and it is split in two.** Names are resolved by a
*dependency*, so an endpoint that declares one cannot forget to refuse a misspelling; values
are read in the handler, because reading them needs the timezone and that is not known until
the workspace has been resolved. The alternative — doing it all in the handler — would leave
:func:`subroutine.api.query.refuse_unknown` letting dotted names through on faith, and a
listing quietly ignoring ``creatd_at.gte`` is precisely the failure that module exists for.

**Neither side holds a list of the other's names.** ``refuse_unknown`` owns everything without
a separator and asks this module only *whether* a route declares a reader; this owns everything
with one. A route that declares no reader refuses a dotted name as an unknown parameter, which
is the right answer for a listing that cannot filter on dates.
"""

import typing
import uuid

import fastapi
import sqlalchemy.orm
import starlette.requests

import subroutine.db.models.identity
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.filtering
import subroutine.domain.grammar

#: The query parameter a written search line arrives in — `#1806`.
#:
#: **``q``, the one a search box has always sent, rather than a second parameter beside it.**
#: `#1801` §6 says the words matching no term *become* ``q``, which is one input rather than
#: two, and two parameters both about words would make an agent choose between them for no
#: reason. Reinterpreting a shipped parameter is only safe because of the unreadable-term rule:
#: a term naming no field stays in the text verbatim, so every query written before this one
#: behaves exactly as it did.
SEARCH = "q"


class Asked (typing.NamedTuple):
	"""What one request asked about its dates, resolved by name and not yet read."""

	#: Which registry the names came from, so a refusal can say what this endpoint filters on.
	entity: str

	#: One per dotted parameter, in the order they arrived — **and one per term a written
	#: search line carried** (`#1806`), which is what makes the grammar sugar over the registry
	#: rather than a second path to the same rows. By the time an endpoint sees this it cannot
	#: tell which spelling a comparison arrived in, and nothing downstream should.
	comparisons: list[subroutine.domain.filtering.Comparison]

	#: What is left of ``q`` once its terms are taken out: the words to search for, or ``None``.
	#:
	#: **The endpoint uses this rather than its own ``q`` parameter.** Reading the raw one would
	#: search for ``type:bug`` as text on a request that had already been narrowed by it —
	#: every filtered search answering nothing, which is a plausible, complete, wrong answer.
	words: str | None = None

	#: Terms the line carried that named a field and could not be read. See
	#: :attr:`subroutine.views.Page.unread`.
	#:
	#: **A tuple because this is a NamedTuple**: a `[]` default is evaluated once and shared by
	#: every instance, which is a class attribute wearing the look of a per-request field.
	unread: tuple[str, ...] = ()

	def narrowing (
		self, where: subroutine.domain.filtering.Where
	) -> list[typing.Any]:
		"""Return what to narrow the listing with, reading every value in one timezone.

		Empty when nothing was asked, which is why a caller can pass the result straight to
		``statement.where(*…)`` without testing it first.
		"""

		return subroutine.domain.filtering.predicates(self.comparisons, where=where)

	def about (self, field: str) -> bool:
		"""Report whether this request filtered on one named field — `#818`.

		Asked before the values are read, because what it decides — whether the listing
		reaches finished work — narrows the statement these predicates are added to.
		"""

		# **The resolved field rather than the name as written** (`#1017`). An alias such as
		# `due_after` carries no separator, so asking `about` for the name would compare
		# `"due_after"` against `"due_at"` and answer no — a filter that was applied and is
		# invisible to the rule that decides whether the listing reaches finished work. A
		# resolved field has no separator either, so `about` partitions it to itself and needs
		# no change to serve both.
		return subroutine.domain.filtering.about(
			(comparison.field for comparison in self.comparisons), field
		)

	def values_for (self, field: str) -> list[str]:
		"""Return the values this request gave one field — `#1829`.

		:meth:`about` answers *whether*; some rules need *what*. Naming a finished status
		decides whether the listing reaches finished work at all (`#1032`), and a `REFERENCE`
		entry compiling to ``status_id == x`` and nothing else would answer `[]` for every
		finished status — a plausible, complete, wrong answer on a listing's commonest
		narrowing.
		"""

		return subroutine.domain.filtering.values_for(self.comparisons, field)


class Reader:
	"""Resolves one entity's dotted parameters, as a dependency a route declares.

	A class rather than a function because the entity has to be bound at declaration time, and
	because :func:`declared_by` finds it by type — which is what keeps the two guards derived
	from the routes rather than from a list somebody maintains.
	"""

	def __init__ (self, entity: str) -> None:
		"""Bind this reader to one filter registry."""

		self.entity = entity

	def __call__ (self, request: starlette.requests.Request) -> Asked:
		"""Resolve every dotted parameter this request carried, and every term ``q`` held.

		**One place, so no endpoint has to remember** (`#1806`). The grammar is sugar over the
		registry, so a term compiles to a comparison indistinguishable from one somebody typed
		in dotted form — which is what stops it becoming a second road to the same rows.

		**Read here rather than in the endpoint because the dependency has the request.** The
		endpoint still declares ``q`` so it is documented and refused when misspelled; what it
		must use afterwards is :attr:`Asked.words`.
		"""

		line = subroutine.domain.grammar.read(
			request.query_params.get(SEARCH), entity=self.entity
		)

		return Asked(
			entity=self.entity,
			comparisons=subroutine.domain.filtering.understood(
				list(request.query_params.multi_items()) + line.parameters,
				entity=self.entity,
			),
			words=line.words,
			unread=tuple(line.unread),
		)


def declared_by (route: typing.Any) -> Reader | None:
	"""Return the reader a route declares, or ``None``.

	Walks the route's dependency tree rather than a registry of paths, so this cannot fall
	behind a listing that gains or loses date filters — the same reasoning as
	``query._accepted`` reading its accepted names off the matched route.
	"""

	dependant = getattr(route, "dependant", None)

	if dependant is None:
		return None

	pending = list(getattr(dependant, "dependencies", []))

	while pending:
		found = pending.pop()

		if isinstance(getattr(found, "call", None), Reader):
			call: Reader = found.call

			return call

		pending.extend(getattr(found, "dependencies", []))

	return None


def narrowed (
	statement: typing.Any,
	asked: Asked,
	*,
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	workspace: subroutine.db.models.identity.Workspace,
) -> typing.Any:
	"""Narrow a listing by whatever its dotted parameters asked, in the caller's timezone.

	The zone comes from :func:`subroutine.domain.filtering.timezone_for`, which the local client
	calls too — so a listing answers the same question the same way whichever transport asked.

	A request that asked nothing narrows by nothing, so every listing calls this unconditionally
	rather than testing first.
	"""

	return statement.where(
		*asked.narrowing(
			subroutine.domain.filtering.Where(
				now=subroutine.db.types.utcnow(),
				timezone=subroutine.domain.filtering.timezone_for(session, actor, workspace),
				session=session,
				principal=actor,
				workspace_ids=[workspace.id],
				workspace=workspace,
			)
		)
	)


def across (
	asked: Asked,
	*,
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	workspace_ids: typing.Sequence[uuid.UUID],
) -> list[typing.Any]:
	"""Return what to narrow a *feed* by, for a request that spans workspaces — `#1431`.

	**Predicates rather than a narrowed statement, which is where this differs from**
	:func:`narrowed`. A feed's statement is built by :func:`subroutine.domain.events.selected`,
	the one builder both readers of that table share (§5.11a). Handing it what to apply keeps it
	the one builder; narrowing here would make this a second place a feed's statement is
	assembled, and the two would agree until somebody changed one.

	**And no workspace in the chain, which is the substantive difference.** A listing is always
	inside one workspace and reads its dates in that workspace's zone. `/v1/changes` answers
	across every workspace a caller can read, so there is no one workspace whose zone is the
	right one — and taking whichever sorted first would read *yesterday* in a colleague's zone
	without saying so. ``timezone_for`` takes ``None`` and the chain becomes user to instance.

	A request that asked nothing narrows by nothing, so a caller passes the result on without
	testing it first.
	"""

	return asked.narrowing(
		subroutine.domain.filtering.Where(
			now=subroutine.db.types.utcnow(),
			timezone=subroutine.domain.filtering.timezone_for(session, actor, None),
			session=session,
			principal=actor,
			workspace_ids=workspace_ids,
		)
	)


#: Declared on the listing rather than passed to it, so that a route accepting dotted names and
#: a route refusing them differ by a line in the signature and nothing else. Each is its own
#: alias because the entity is what decides which fields exist.
TaskFilters = typing.Annotated[Asked, fastapi.Depends(Reader("task"))]
DocumentFilters = typing.Annotated[Asked, fastapi.Depends(Reader("document"))]
ProjectFilters = typing.Annotated[Asked, fastapi.Depends(Reader("project"))]

#: What the change feed and the journal accept — `#1431`, decision `#1429`. Both routes declare
#: it, because a period is the same question whichever of the two readings answers it.
EventFilters = typing.Annotated[Asked, fastapi.Depends(Reader("event"))]
