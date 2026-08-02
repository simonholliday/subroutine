"""What changed while the caller was away, across everything they can see (SPEC.md §5.11a).

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

*Excluded by default.* The scoping allow-list lives in
:data:`subroutine.domain.scoping.FEED_ENTITY_TYPES`, and an ``entity_type`` absent from it is
invisible here. A feed is the one place in this API where forgetting to add a rule would
publish something rather than hide it.
"""

import datetime
import typing
import uuid

import fastapi
import sqlalchemy
import sqlalchemy.orm

import subroutine.api.dependencies
import subroutine.api.query
import subroutine.api.routing
import subroutine.api.security
import subroutine.api.shaping
import subroutine.config
import subroutine.db.models.activity
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.events
import subroutine.domain.paging
import subroutine.domain.scoping
import subroutine.domain.selection
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1/changes", tags=["changes"], route_class=subroutine.api.routing.Transactional
)

SELECTABLE = subroutine.api.shaping.selectable(subroutine.views.Event)

#: How far behind the clock the newest reportable event sits. §5.11 fixes the value, because
#: it is client-visible: a caller polling more often than this simply sees nothing new, and
#: one that reasons about freshness needs to know the endpoint is deliberately a second stale.
WATERMARK = datetime.timedelta(seconds=1)

#: The only value ``?actor=`` takes. **This credential, not this user** (`#158`): an agent
#: holding a service-account token wants what *it* did, not what the person who issued the
#: token did from a laptop an hour ago.
ACTOR_ME = "me"


@router.get(
	"",
	summary="What changed since you last looked",
	dependencies=[subroutine.api.query.UnknownQueryDep],
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

	# **Checked here rather than with `ge=1` on the parameter**, so the refusal names the field
	# and says what to do instead. It also keeps `since=0` from meaning "before everything",
	# which would be indistinguishable from a pruned cursor and so would answer `410` to a
	# caller who had simply never polled before.
	if since is not None and since < 1:
		raise subroutine.errors.ValidationError(
			f"'since' is a seq and the first one is 1, so {since} names nothing.",
			code="invalid_field_value",
			errors=[
				subroutine.errors.FieldError(
					field="since",
					code="invalid_field_value",
					message="Send the seq of the last event you processed, or omit 'since' "
					"to start from the oldest event still held.",
				)
			],
		)

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

	_refuse_expired_cursor(session, settings, since=since, workspace_ids=workspace_ids)

	return _page(
		session,
		settings,
		actor,
		workspace_ids=workspace_ids,
		since=since,
		mine=actor_filter == ACTOR_ME,
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
	limit: int | None,
	shape: typing.Any,
) -> typing.Any:
	"""Return one page of the feed."""

	model = subroutine.db.models.activity.Event

	# **`mine` with no token is an empty feed, not an unfiltered one.** A session-authenticated
	# caller has no `actor_token_id` on anything they did, so matching on a null token would
	# silently widen `?actor=me` to everything — the failure mode being asked about here is
	# precisely somebody believing they are seeing only their own work.
	token_id = None if actor.token is None else actor.token.id

	if mine and token_id is None:
		return subroutine.api.shaping.response(
			[], subroutine.views.Page(limit=subroutine.domain.paging.size(limit, settings)), shape
		)

	statement = subroutine.domain.events.selected(
		workspace_ids=workspace_ids,
		upper_bound=subroutine.db.types.utcnow() - WATERMARK,
		since=since,
		visible=subroutine.domain.scoping.visible_events(actor, workspace_ids=workspace_ids),
		actor_token_id=token_id if mine else None,
	)

	size = subroutine.domain.paging.size(limit, settings)
	rows = list(session.scalars(statement.order_by(model.seq.asc()).limit(size + 1)))
	has_more = len(rows) > size

	return subroutine.api.shaping.response(
		[subroutine.views.event(row) for row in rows[:size]],
		# **No `next_cursor`, and that is the contract rather than an omission.** This feed
		# resumes on `?since=<seq>` (§5.11a), and handing back an opaque keyset cursor would
		# offer a second way to page that the endpoint does not accept.
		subroutine.views.Page(limit=size, has_more=has_more, total=None),
		shape,
	)


def _refuse_expired_cursor (
	session: sqlalchemy.orm.Session,
	settings: subroutine.config.Settings,
	*,
	since: int | None,
	workspace_ids: typing.Sequence[uuid.UUID],
) -> None:
	"""Refuse a cursor pointing further back than this instance can still account for.

	§5.11 retains events for ``events_retention_days`` and requires ``410 cursor_expired``
	below that floor, so a client resyncs rather than being handed a page that silently omits
	everything pruned in between — the one failure a feed must never have, because it looks
	exactly like nothing having happened.

	**The test is "did events below this point exist and go", not "is this old".** A caller
	resuming from seq 5 on an instance that still holds seq 1 is simply behind, and behind is
	what this endpoint is for.

	**Currently unreachable, and honestly so.** Nothing prunes yet (`#251`), so the oldest
	surviving event is the first one ever written and no cursor can fall below it. The path
	is built and tested by deleting rows, and goes live the day retention does.
	"""

	if since is None:
		return

	oldest = session.scalar(
		sqlalchemy.select(sqlalchemy.func.min(subroutine.db.models.activity.Event.seq)).where(
			subroutine.db.models.activity.Event.workspace_id.in_(workspace_ids)
		)
	)

	if oldest is None or since >= oldest:
		return

	raise subroutine.errors.CursorExpired(
		f"Events before seq {oldest} are no longer held, so what happened since {since} "
		f"cannot be reported in full.",
		hint="Ask again without 'since' to resync from the oldest event still kept.",
	)
