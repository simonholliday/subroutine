"""The history of one item — what happened to it, newest first (docs/design.md §5.11a).

The first of the two readers of the ``event`` table, which five domain modules have been
writing to since M1 with nothing reading them. The other, ``GET /v1/changes``, is the feed:
"what changed while I was away", across everything a caller can see.

**A history is not the feed with a filter**, and building it as one produces a specific bug.
The feed carries a ``now() - 1s`` watermark because it is resumable — ``seq`` is allocated at
insert and becomes visible at commit, so a reader that advances its cursor past an
uncommitted number never sees that event again. A history is not resumable: ask again and the
row is still there. Inheriting the watermark would mean commenting on ``#42`` and immediately
reading its history shows **nothing**, which somebody meets in the first minute and reads as
a lost write. There is a test named for it.

**Paged with the ordinary keyset cursor and deliberately not ``?since=``.** Every other
collection here pages that way, so an agent already knows how — and a ``?since=`` on a
history would invite treating it as resumable, which is how the watermark problem would
arrive per entity having been solved once globally.

**Histories before the feed** was the substance of the decision rather than a preference:
this builds the per-``entity_type`` dispatch one entity at a time, each with a small blast
radius, and it does it by *resolving the subject* — which is the permission check for the item.
**Which events on it a reader may see is the feed's predicate**, reused through
``events.history`` rather than written here: a link hanging off a visible item can name one the
reader may not see, which resolving the item alone let through.

**And one item's journal is the same rows read the other way** (`#2729`, decision `#1429`):
``/v1/tasks/{ref}/journal`` beside ``/events``, rendered through ``views.journal_entries`` as
``/v1/journal`` is, so it names who did each thing and what a change moved between and carries
no whole text. A sibling rather than a flag, for the reason ``/v1/journal`` is its own route:
a caller has to be able to discover that there are two readings. A task and a document have
one; a project does not, because nothing reads a project's journal yet.
"""

import typing

import fastapi
import sqlalchemy.orm
import starlette.requests

import subroutine.api.dependencies
import subroutine.api.pagination
import subroutine.api.routing
import subroutine.api.security
import subroutine.api.shaping
import subroutine.api.subjects
import subroutine.config
import subroutine.db.models.activity
import subroutine.domain.authentication
import subroutine.domain.events
import subroutine.domain.paging
import subroutine.views

#: One router per subject, mounted under the entity it extends, so ``routing.check`` can see
#: that none of them shadows a literal path.
task_events = fastapi.APIRouter(
	prefix="/v1/tasks",
	tags=["events"],
	route_class=subroutine.api.routing.Transactional,
)
project_events = fastapi.APIRouter(
	prefix="/v1/projects",
	tags=["events"],
	route_class=subroutine.api.routing.Transactional,
)
document_events = fastapi.APIRouter(
	prefix="/v1/documents",
	tags=["events"],
	route_class=subroutine.api.routing.Transactional,
)

SELECTABLE = subroutine.api.shaping.selectable(subroutine.views.Event)

#: What an item's journal lets a caller select, which is a journal entry's fields.
JOURNAL_SELECTABLE = subroutine.api.shaping.selectable(subroutine.views.JournalEntry)

#: What ``?order=`` accepts. One field, because ``seq`` is the only ordering an event log
#: has that means anything: it is the order things happened in, and it is monotonic.
SORTABLE = {"seq": subroutine.db.models.activity.Event.seq}

#: Newest first, which is what "what happened to this" means when you are looking at it now.
#: The opposite of the feed, which runs forwards because a cursor goes forwards.
DEFAULT_ORDER = ("-seq",)


def _page (
	session: sqlalchemy.orm.Session,
	settings: subroutine.config.Settings,
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_id: typing.Any,
	entity_type: str,
	entity_id: typing.Any,
	order: str | None,
	limit: int | None,
	cursor: str | None,
	shape: typing.Any,
	render: typing.Callable[
		[list[subroutine.db.models.activity.Event]], typing.Sequence[typing.Any]
	],
) -> typing.Any:
	"""Return one page of an item's history, rendered by ``render``.

	**The rows and the cursor are one thing and the rendering is the caller's** (`#2729`): the
	history renders events and the item's journal renders journal entries, over the same page.
	"""

	model = subroutine.db.models.activity.Event
	statement = subroutine.domain.events.history(
		principal, workspace_id=workspace_id, entity_type=entity_type, entity_id=entity_id
	)

	keys = subroutine.api.pagination.parse_order(
		order,
		allowed=SORTABLE,
		default=DEFAULT_ORDER,
		# `seq` is the primary key and strictly monotonic, so the ordering is already total
		# and the appended tiebreak is the same column. Harmless, and cheaper than a second
		# pagination path for the one table that does not need one.
		tiebreak=model.seq,
	)
	size = subroutine.domain.paging.size(limit, settings)

	if cursor is not None:
		statement = statement.where(
			subroutine.api.pagination.after(
				keys,
				subroutine.api.pagination.decode(
					settings.require_secret_key(), keys, cursor, collection="changes"
				),
			)
		)

	ordered = statement.order_by(*[key.ordering() for key in keys])
	rows = list(session.scalars(ordered.limit(size + 1)))
	has_more = len(rows) > size
	rows = rows[:size]

	return subroutine.api.shaping.response(
		render(rows),
		subroutine.views.Page(
			limit=size,
			has_more=has_more,
			next_cursor=(
				subroutine.api.pagination.encode(
					settings.require_secret_key(), keys, rows[-1], collection="changes"
				)
				if has_more and rows
				else None
			),
			total=None,
		),
		shape,
	)


def _as_events (
	session: sqlalchemy.orm.Session, rows: list[subroutine.db.models.activity.Event]
) -> list[subroutine.views.Event]:
	"""Render a page of an item's history as the audit log reports it."""

	described = subroutine.domain.events.descriptions(session, rows)

	return [subroutine.views.event(row, described) for row in rows]


def _attach (group: fastapi.APIRouter, *, entity_type: str, address: str) -> None:
	"""Register the history endpoint for one kind of subject.

	Declared once and applied three times, so the three cannot drift into three slightly
	different history APIs — which is exactly what happened to the *link* sub-resources
	before they were unified.
	"""

	@group.get(
		"/{" + address + "}/events",
		summary=f"What happened to this {entity_type}",
		response_model=subroutine.views.Collection[subroutine.views.Event],
		name=f"list_{entity_type}_events",
	)
	def listing (
		actor: subroutine.api.security.PrincipalDep,
		session: subroutine.api.dependencies.SessionDep,
		settings: subroutine.api.dependencies.SettingsDep,
		request: starlette.requests.Request,
		workspace_id: str | None = fastapi.Query(
			None, description="Which workspace, by id or slug. Needed when you can reach several."
		),
		order: str | None = fastapi.Query(
			None, description="'-seq' (default, newest first) or 'seq' for oldest first."
		),
		limit: int | None = fastapi.Query(None, description=subroutine.api.pagination.LIMIT_DESCRIPTION),
		cursor: str | None = fastapi.Query(None, description="Continue after a page."),
		format: str | None = subroutine.api.shaping.FORMAT_QUERY,
		fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
	) -> typing.Any:
		"""Return this item's history, newest first."""

		# Resolving the subject is the permission check **for the item**: it goes through the
		# entity's own narrowed statement, so one the caller may not see is absent rather than
		# forbidden (§7.3a). Which events on it they may see is `events.history`'s, since a link
		# hanging off it can name an item they may not (`#2769`).
		subject = subroutine.api.subjects.resolve(
			session,
			actor,
			entity_type=entity_type,
			address=request.path_params[subroutine.api.subjects.named(address)],
			workspace_id=workspace_id,
		)

		return _page(
			session,
			settings,
			actor,
			workspace_id=subject.workspace_id,
			entity_type=entity_type,
			entity_id=subject.id,
			order=order,
			limit=limit,
			cursor=cursor,
			shape=subroutine.api.shaping.wanted(
				format=format,
				fields=fields,
				available=SELECTABLE,
				entity="event",
				timezone=subroutine.views.reader_zone(session, actor),
			),
			render=lambda rows: _as_events(session, rows),
		)


def _attach_journal (group: fastapi.APIRouter, *, entity_type: str, address: str) -> None:
	"""Register one item's journal for one kind of subject — `#2729`.

	Beside :func:`_attach` rather than inside it, because a project has a history and no journal.
	"""

	@group.get(
		"/{" + address + "}/journal",
		summary=f"What happened to this {entity_type}, with what was said",
		response_model=subroutine.views.Collection[subroutine.views.JournalEntry],
		name=f"read_{entity_type}_journal",
	)
	def reading (
		actor: subroutine.api.security.PrincipalDep,
		session: subroutine.api.dependencies.SessionDep,
		settings: subroutine.api.dependencies.SettingsDep,
		request: starlette.requests.Request,
		workspace_id: str | None = fastapi.Query(
			None, description="Which workspace, by id or slug. Needed when you can reach several."
		),
		order: str | None = fastapi.Query(
			None, description="'-seq' (default, newest first) or 'seq' for oldest first."
		),
		limit: int | None = fastapi.Query(None, description=subroutine.api.pagination.LIMIT_DESCRIPTION),
		cursor: str | None = fastapi.Query(None, description="Continue after a page."),
		format: str | None = subroutine.api.shaping.FORMAT_QUERY,
		fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
	) -> typing.Any:
		"""Return what happened to this item, newest first, with who did it and what they said.

		**The same entries `/v1/journal` gives about this item**, read from this item's history:
		who did each thing and through which door, what a change moved between, and how a
		comment on it opens. No entry carries a whole text, and `/events` beside this has every
		change whole.
		"""

		subject = subroutine.api.subjects.resolve(
			session,
			actor,
			entity_type=entity_type,
			address=request.path_params[subroutine.api.subjects.named(address)],
			workspace_id=workspace_id,
		)

		return _page(
			session,
			settings,
			actor,
			workspace_id=subject.workspace_id,
			entity_type=entity_type,
			entity_id=subject.id,
			order=order,
			limit=limit,
			cursor=cursor,
			shape=subroutine.api.shaping.wanted(
				format=format,
				fields=fields,
				available=JOURNAL_SELECTABLE,
				entity="journal entry",
				timezone=subroutine.views.reader_zone(session, actor),
			),
			render=lambda rows: subroutine.views.journal_entries(
				session, rows, principal=actor, workspace_ids=[subject.workspace_id]
			),
		)


for _group, _entity in (
	(task_events, "task"),
	(project_events, "project"),
	(document_events, "document"),
):
	_attach(_group, entity_type=_entity, address=subroutine.api.subjects.ADDRESS[_entity])

for _group, _entity in ((task_events, "task"), (document_events, "document")):
	_attach_journal(
		_group, entity_type=_entity, address=subroutine.api.subjects.ADDRESS[_entity]
	)
