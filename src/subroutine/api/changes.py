"""What changed while the caller was away, across everything they can see (docs/design.md §5.11a).

The second reader of the ``event`` table, and the one that answers a *resumption* question:
an agent whose context ended on Tuesday asks what has moved since, in one call, without
naming a subject. The per-entity histories answer a comprehension question — what happened to
*this* — and §5.11a records at length why these are two endpoints and not one with a filter.

**Why this is worth an endpoint at all.** An agent writes durably here and, before this,
could not resume incrementally: every session either re-read the backlog defensively or
missed things, and nothing marked a belief stale. The concrete case is on `#13` — an item was
closed, and I would have gone on reporting it open, confidently, because a context window is
a snapshot that does not decay.

**Three properties are load-bearing and each is easy to remove by accident.**

*The watermark.* ``seq`` is allocated at insert and becomes visible at commit, which are not
the same moment: transaction A can take 100 while B takes 101 and commits first, so a reader
polling at 99 sees 101, advances, and never sees 100. Nothing above ``now() - 1s`` is
reported, which gives the slower transaction time to land. **SQLite hides this completely**
by having one writer, so it is a defect that would be found in production and not in a test —
which is why ``tests/test_api_changes.py`` proves it on PostgreSQL specifically.

*Inclusive ``?since=``.* §5.11 fixes cursors as "inclusive-with-dedupe". The client sends
back the last ``seq`` it saw and receives it again, which costs one row per poll and makes a
client that persists its cursor before it finishes processing a page correct rather than
lossy. Every event carries a stable ``id`` to dedupe on.

*Excluded by default.* :func:`subroutine.domain.scoping.visible_events` composes one clause
per kind of event it knows how to narrow, so a kind nobody wrote a clause for matches none of
them and is invisible here. A feed is the one place in this API where forgetting to add a rule
would publish something rather than hide it, and this is the arrangement that means it cannot.

There is no list to keep in step — ``tests/test_events_scoping.py`` reads the kinds out of the
calls that emit them and measures each one against a real feed (`#303`).
"""

import typing
import uuid

import fastapi
import sqlalchemy.orm

import subroutine.api.dependencies
import subroutine.api.routing
import subroutine.api.security
import subroutine.api.shaping
import subroutine.config
import subroutine.domain.authentication
import subroutine.domain.events
import subroutine.domain.paging
import subroutine.domain.selection
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1/changes", tags=["changes"], route_class=subroutine.api.routing.Transactional
)

SELECTABLE = subroutine.api.shaping.selectable(subroutine.views.Event)

#: The only value ``?actor=`` takes. **This credential, not this user** (`#158`): an agent
#: holding a service-account token wants what *it* did, not what the person who issued the
#: token did from a laptop an hour ago.
ACTOR_ME = "me"


@router.get(
	"",
	summary="What changed since you last looked",
	response_model=subroutine.views.Collection[subroutine.views.Event],
	name="list_changes",
)
def listing (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	since: int | None = fastapi.Query(
		None,
		description="Resume from this seq, inclusive. Send back the seq of the last event "
		"you processed; you will see it again and should ignore what you already have.",
	),
	workspace_id: str | None = fastapi.Query(
		None, description="Narrow to one workspace, by id or slug. The default spans all of them."
	),
	actor_filter: str | None = fastapi.Query(
		None,
		alias="actor",
		description="'me' for what this credential itself did. Omit for everything you can see.",
	),
	newest: bool = fastapi.Query(
		False,
		description="Start at the newest events rather than the oldest. For a first look at "
		"a long history; the page still reads forwards. Ignored when 'since' is given.",
	),
	limit: int | None = fastapi.Query(None, description="How many to return."),
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""Return what has happened, oldest first, that this caller is entitled to know about.

	**Resuming is `?since=`, not a cursor.** Take the `seq` of the last event you dealt with
	and send it back; you will receive it again and everything after it. `has_more` says
	whether another page is waiting immediately — when it is false you are caught up, and
	polling again will return only what happens next.

	Ordered oldest first, because a feed is read forwards. The per-item histories run the
	other way, because "what happened to this" is a question about the recent past.
	"""

	# Deliberately *all* readable workspaces unless one is named. The question is "what have I
	# missed", and an agent working across two workspaces that had to ask twice would resume
	# one and silently fall behind on the other.
	if workspace_id is None:
		workspaces = subroutine.domain.workspaces.readable(session, actor)

	else:
		workspaces = [
			subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
		]

	workspace_ids = [workspace.id for workspace in workspaces]

	if actor_filter is not None and actor_filter != ACTOR_ME:
		raise subroutine.errors.ValidationError(
			f"'actor' takes {ACTOR_ME!r} or nothing.",
			code="invalid_field_value",
			errors=[
				subroutine.errors.FieldError(
					field="actor",
					code="invalid_field_value",
					message=f"Send actor={ACTOR_ME} for what this credential did, or omit it.",
				)
			],
		)

	# Both cursor refusals, in the domain so that this transport and `clients.local` cannot
	# answer differently — which is what they were doing for `since=0` (`#309`).
	subroutine.domain.events.refuse_unusable_cursor(
		session, since=since, workspace_ids=workspace_ids
	)

	return _page(
		session,
		settings,
		actor,
		workspace_ids=workspace_ids,
		since=since,
		mine=actor_filter == ACTOR_ME,
		newest=newest,
		limit=limit,
		shape=subroutine.api.shaping.wanted(
			format=format, fields=fields, available=SELECTABLE, entity="event"
		),
	)


def _page (
	session: sqlalchemy.orm.Session,
	settings: subroutine.config.Settings,
	actor: subroutine.domain.authentication.Principal,
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	since: int | None,
	mine: bool,
	newest: bool,
	limit: int | None,
	shape: typing.Any,
) -> typing.Any:
	"""Return one page of the feed."""

	size = subroutine.domain.paging.size(limit, settings)
	shown, has_more = subroutine.domain.events.page(
		session,
		actor,
		workspace_ids=workspace_ids,
		size=size,
		since=since,
		mine=mine,
		newest=newest,
	)
	described = subroutine.domain.events.descriptions(session, shown)

	return subroutine.api.shaping.response(
		[subroutine.views.event(row, described) for row in shown],
		# **No `next_cursor`, and that is the contract rather than an omission.** This feed
		# resumes on `?since=<seq>` (§5.11a), and handing back an opaque keyset cursor would
		# offer a second way to page that the endpoint does not accept.
		subroutine.views.Page(limit=size, has_more=has_more, total=None),
		shape,
	)
