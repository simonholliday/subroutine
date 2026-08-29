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
import subroutine.api.filters
import subroutine.api.pagination
import subroutine.api.routing
import subroutine.api.security
import subroutine.api.shaping
import subroutine.config
import subroutine.domain.authentication
import subroutine.domain.events
import subroutine.domain.paging
import subroutine.domain.scoping
import subroutine.domain.selection
import subroutine.domain.workspaces
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1/changes", tags=["changes"], route_class=subroutine.api.routing.Transactional
)

SELECTABLE = subroutine.api.shaping.selectable(subroutine.views.Event)

#: What ``?actor=`` means when the caller means themselves. **This credential, not this user**
#: (`#158`): an agent holding a service-account token wants what *it* did, not what the person
#: who issued the token did from a laptop an hour ago.
#:
#: **Any other value is a username, and that is the same question one grain coarser** (`#1120`)
#: — *what did that account do*, through whatever credential. Not a second question in one
#: parameter: the coarse grain is the only one that is useful about somebody else, because
#: nobody knows another credential's id, and the fine one is the only one that is useful about
#: yourself, because your account may hold several.
ACTOR_ME = "me"


@router.get(
	"",
	summary="What changed since you last looked",
	response_model=subroutine.views.Changes,
	name="list_changes",
)
def listing (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	# **Declared rather than read in the handler** — `#815`'s split, and the reason this
	# route cannot quietly ignore `creatd_at.gte`: a dotted name a route declares no reader
	# for is refused as an unknown parameter, and one it does declare is resolved here.
	dated: subroutine.api.filters.EventFilters,
	since: int | None = fastapi.Query(
		None,
		description="Resume from this seq, inclusive. Send back the seq of the last event "
		"you processed; you will see it again and should ignore what you already have.",
	),
	before: int | None = fastapi.Query(
		None,
		description="Read only events earlier than this seq, exclusive. With 'newest' it is "
		"how you walk back through a long history: send the seq of the earliest event you "
		"already have. Composes with 'since', and together they are a range.",
	),
	workspace_id: str | None = fastapi.Query(
		None, description="Narrow to one workspace, by id or slug. The default spans all of them."
	),
	actor_filter: str | None = fastapi.Query(
		None,
		alias="actor",
		description="'me' for what this credential itself did, or a username for everything "
		"that account did through any of its credentials. Omit for everything you can see.",
	),
	newest: bool = fastapi.Query(
		False,
		description="Start at the newest events rather than the oldest. For a first look at "
		"a long history; the page still reads forwards. Ignored when 'since' is given.",
	),
	limit: int | None = fastapi.Query(None, description=subroutine.api.pagination.LIMIT_DESCRIPTION),
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""Return what has happened, oldest first, that this caller is entitled to know about.

	**A period is `?created_at.gte=`, and it is a different question from `?since=`.** A cursor
	resumes where you left off and is inclusive-with-dedupe, so it is what a client that polls
	should send; a period is a statement about a stretch of time and is not resumable. Somebody
	asking what happened on a particular day has no cursor to offer, and a client polling has no
	date in mind. Both are accepted and they compose.

	**Resuming is `?since=`, not a cursor.** Take the `seq` of the last event you dealt with
	and send it back; you will receive it again and everything after it. `has_more` says
	whether another page is waiting immediately — when it is false you are caught up, and
	polling again will return only what happens next.

	**Going the other way is `?before=`.** With `newest` set, `has_more` means there are
	*earlier* events, and `since` is a floor — so walking back through a long history is
	`?before=<the earliest seq you hold>`, exclusive. Every answer still reads oldest first.

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

	# **A username is resolved through the same selector every other "who" here uses**
	# (`#1120`), so an account that does not exist is refused by name and with the members
	# listed — which is what the enumerated refusal this replaces could not do for anybody but
	# itself.
	by = (
		None
		if actor_filter is None or actor_filter == ACTOR_ME
		else subroutine.domain.selection.user(session, actor_filter, caller=actor.user).id
	)

	# Both cursor refusals, in the domain so that this transport and `clients.local` cannot
	# answer differently — which is what they were doing for `since=0` (`#309`).
	subroutine.domain.events.refuse_unusable_cursor(
		session, since=since, workspace_ids=workspace_ids
	)
	subroutine.domain.events.refuse_a_bound_that_names_nothing(before)

	return _page(
		session,
		settings,
		actor,
		workspace_ids=workspace_ids,
		# **The zone is the reader's own, because a feed spans workspaces** — see
		# `filters.across`. Read here rather than in `_page` so the per-item histories, which
		# have a workspace and do not take a period, are not handed a decision they never make.
		narrowing=subroutine.api.filters.across(
			dated, session=session, actor=actor, workspace_ids=workspace_ids
		),
		since=since,
		before=before,
		mine=actor_filter == ACTOR_ME,
		by=by,
		newest=newest,
		limit=limit,
		shape=subroutine.api.shaping.wanted(
			format=format,
			fields=fields,
			available=SELECTABLE,
			entity="event",
			timezone=subroutine.views.reader_zone(session, actor),
		),
	)


def _page (
	session: sqlalchemy.orm.Session,
	settings: subroutine.config.Settings,
	actor: subroutine.domain.authentication.Principal,
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	since: int | None,
	before: int | None,
	mine: bool,
	by: uuid.UUID | None,
	newest: bool,
	limit: int | None,
	shape: typing.Any,
	narrowing: typing.Sequence[typing.Any] = (),
) -> typing.Any:
	"""Return one page of the feed."""

	size = subroutine.domain.paging.size(limit, settings)
	shown, has_more = subroutine.domain.events.page(
		session,
		actor,
		workspace_ids=workspace_ids,
		size=size,
		since=since,
		before=before,
		mine=mine,
		by=by,
		newest=newest,
		narrowing=narrowing,
	)
	described = subroutine.domain.events.descriptions(session, shown)

	return subroutine.api.shaping.response(
		[subroutine.views.event(row, described) for row in shown],
		# **No `next_cursor`, and that is the contract rather than an omission.** This feed
		# resumes on `?since=<seq>` (§5.11a), and handing back an opaque keyset cursor would
		# offer a second way to page that the endpoint does not accept.
		subroutine.views.Page(limit=size, has_more=has_more, total=None),
		shape,
		# **Said on every answer, narrowed or not** (`#1085`). A credential that may read only
		# some of these kinds now gets a feed of those rather than a refusal about one it never
		# asked about — so the answer has to say what it is a feed *of*, or the difference
		# between "nothing happened" and "I cannot see that" is invisible.
		covers=subroutine.domain.scoping.readable_event_kinds(actor),
	)
