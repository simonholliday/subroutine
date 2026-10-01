"""What an export holds: every kind of row a reader may take away, a page at a time.

Decision ``#4049``: an export writes what its credential can read, each line the object the API
already returns for that row. This module is the one answer to *which rows, in what order, shown
how*, and both transports page through it - ``GET /v1/export/<kind>`` signs a cursor around a
page, and the local client asks for the next one itself. **Outside ``domain/``, as ``views.py``
is**, because it renders views and nothing under ``domain/`` may import them.

**Every narrowing is ``domain/scoping.py``'s**, never restated. Tasks, documents and projects come
from the statements every listing starts at, with the trash, archived rows and recurrence
templates switched on; comments, links and verifications are narrowed to the rows
:func:`~subroutine.domain.scoping.readable_identifiers` says the reader may know of; events are
the change feed's own page. A second copy of who may read what is the defect
``tests/test_scoping.py`` exists to refuse.

**And every refusal is the one its own listing makes**, so an export is never a wider door to
the same rows: ``task:read`` for tasks and documents and ``project:read`` for projects, which
the scoping statements check, and ``comment:read`` for comments, which the comment listing
checks for itself and so this does too.

**Left out on purpose**: a withdrawn link and a deleted comment, which cannot be put back and are
recorded as events; memberships and roles; and every credential, which no view carries.
"""

import dataclasses
import typing
import uuid

import pydantic
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.saved
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.domain.accountability
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.events
import subroutine.domain.links
import subroutine.domain.saved
import subroutine.domain.scoping
import subroutine.domain.users
import subroutine.permissions
import subroutine.views

Principal = subroutine.domain.authentication.Principal
Rows = typing.Sequence[typing.Any]

#: Which rows of one kind a reader may export, as a statement not yet ordered or cut.
Statement = typing.Callable[
	[sqlalchemy.orm.Session, Principal, uuid.UUID], sqlalchemy.Select[typing.Any]
]

#: Up to ``size`` rows after a value of the kind's order, and whether there were more.
Fetch = typing.Callable[
	[sqlalchemy.orm.Session, Principal, uuid.UUID, typing.Any, int],
	tuple[list[typing.Any], bool],
]


@dataclasses.dataclass(frozen=True)
class Kind:
	"""One kind of row an export holds: which rows, in what order, and how each is shown."""

	name: str
	view: type[pydantic.BaseModel]

	#: The column a page is ordered by and resumed after. An id for every kind but the event
	#: log, whose order is ``seq`` - the number a caller of the change feed already holds.
	order: typing.Any

	#: Return up to ``size`` rows after ``after``, and whether there were more.
	fetch: Fetch

	#: Render one page of rows as the views the API returns for them.
	render: typing.Callable[
		[sqlalchemy.orm.Session, Principal, uuid.UUID, Rows], typing.Sequence[pydantic.BaseModel]
	]


@dataclasses.dataclass(frozen=True)
class Page:
	"""One page of one kind, rendered, and where the next one starts."""

	items: list[pydantic.BaseModel]
	has_more: bool

	#: The order value of the last row *fetched*, which the next page resumes after. **Not read
	#: off the last item**, because a link whose far end the reader cannot see is dropped as it
	#: is rendered: a page can come back shorter than it was asked for, even empty, with more to
	#: follow, and a cursor taken from what was shown would end the walk there.
	resume: typing.Any


def page (
	session: sqlalchemy.orm.Session,
	reader: Principal,
	*,
	workspace_id: uuid.UUID,
	kind: str,
	after: typing.Any,
	size: int,
) -> Page:
	"""Return the page of one kind that follows ``after``, rendered for ``reader``.

	``after`` is the value of the kind's :attr:`Kind.order` on the last row of the page before,
	or ``None`` for the first.
	"""

	chosen = KINDS[kind]
	found, has_more = chosen.fetch(session, reader, workspace_id, after, size)

	return Page(
		items=list(chosen.render(session, reader, workspace_id, found)),
		has_more=has_more,
		resume=getattr(found[-1], chosen.order.key) if found else after,
	)


def _ordered (order: typing.Any, rows: Statement) -> Fetch:
	"""Return a fetch that pages ``rows`` by ``order``, asking for one more than it keeps."""

	def fetch (
		session: sqlalchemy.orm.Session,
		reader: Principal,
		workspace_id: uuid.UUID,
		after: typing.Any,
		size: int,
	) -> tuple[list[typing.Any], bool]:
		"""Return up to ``size`` rows after ``after``, and whether there were more."""

		statement = rows(session, reader, workspace_id)

		if after is not None:
			statement = statement.where(order > after)

		found = list(session.scalars(statement.order_by(order).limit(size + 1)))

		return found[:size], len(found) > size

	return fetch


def _projects (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID
) -> sqlalchemy.Select[typing.Any]:
	"""Return the projects the reader may see, the trash and archived ones included."""

	return subroutine.domain.scoping.readable_projects(
		reader, workspace_ids=[workspace_id], include_deleted=True, include_archived=True
	)


def _tasks (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID
) -> sqlalchemy.Select[typing.Any]:
	"""Return every task the reader may see: done, archived, in the trash, and templates."""

	return subroutine.domain.scoping.readable_tasks(
		reader,
		workspace_ids=[workspace_id],
		include_deleted=True,
		include_deleted_projects=True,
		include_completed=True,
		include_archived=True,
		include_templates=True,
	)


def _documents (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID
) -> sqlalchemy.Select[typing.Any]:
	"""Return every document the reader may see, the trash and archived ones included."""

	return subroutine.domain.scoping.readable_documents(
		reader,
		workspace_ids=[workspace_id],
		include_deleted=True,
		include_deleted_projects=True,
		include_archived=True,
	)


def _readable_end (
	reader: Principal, workspace_id: uuid.UUID, kind: typing.Any, identifier: typing.Any
) -> sqlalchemy.ColumnElement[bool]:
	"""Return a condition that an item named by kind and id is one the reader may know of.

	A kind the credential may not read is absent from what ``readable_identifiers`` returns, so a
	row naming one is left out rather than refused - the export is of what can be read.
	"""

	readable = subroutine.domain.scoping.readable_identifiers(reader, workspace_ids=[workspace_id])

	if not readable:
		return sqlalchemy.false()

	return sqlalchemy.or_(
		*[
			sqlalchemy.and_(kind == entity_type, identifier.in_(identifiers))
			for entity_type, identifiers in readable.items()
		]
	)


def _comments (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID
) -> sqlalchemy.Select[typing.Any]:
	"""Return the comments on items the reader may see. A deleted one cannot be put back.

	**Refused without ``comment:read``**, as ``GET /v1/tasks/<ref>/comments`` refuses it - the
	comment table has its own permission where the other kinds here take their subject's.
	"""

	subroutine.domain.authorization.authorize(
		session, reader, subroutine.permissions.COMMENT_READ, workspace_id=workspace_id
	)

	model = subroutine.db.models.activity.Comment

	return sqlalchemy.select(model).where(
		model.workspace_id == workspace_id,
		model.deleted_at.is_(None),
		_readable_end(reader, workspace_id, model.entity_type, model.entity_id),
	)


def _links (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID
) -> sqlalchemy.Select[typing.Any]:
	"""Return the links whose two ends the reader may both see. A withdrawn one is not listed."""

	model = subroutine.db.models.work.Link

	return sqlalchemy.select(model).where(
		model.workspace_id == workspace_id,
		model.deleted_at.is_(None),
		_readable_end(reader, workspace_id, model.source_type, model.source_id),
		_readable_end(reader, workspace_id, model.target_type, model.target_id),
	)


def _verifications (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID
) -> sqlalchemy.Select[typing.Any]:
	"""Return what was recorded as checked, on tasks the reader may see."""

	model = subroutine.db.models.work.Verification
	readable = subroutine.domain.scoping.readable_identifiers(reader, workspace_ids=[workspace_id])

	return sqlalchemy.select(model).where(
		model.workspace_id == workspace_id,
		model.task_id.in_(readable["task"]) if "task" in readable else sqlalchemy.false(),
	)


def _in_workspace (model: typing.Any) -> Statement:
	"""Return the rows of one vocabulary table that belong to the workspace."""

	def rows (
		session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID
	) -> sqlalchemy.Select[typing.Any]:
		"""Return the workspace's rows of this table."""

		return sqlalchemy.select(model).where(model.workspace_id == workspace_id)

	return rows


def _saved_views (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID
) -> sqlalchemy.Select[typing.Any]:
	"""Return the saved views the reader sees: their own, and the ones shared with everybody."""

	return subroutine.domain.saved.readable(session, reader, workspace_id=workspace_id)


def _users (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID
) -> sqlalchemy.Select[typing.Any]:
	"""Return the accounts ``GET /v1/users`` lists, which is every one, to anybody signed in."""

	return subroutine.domain.users.readable(session, actor=reader)


def _events (
	session: sqlalchemy.orm.Session,
	reader: Principal,
	workspace_id: uuid.UUID,
	after: typing.Any,
	size: int,
) -> tuple[list[typing.Any], bool]:
	"""Return the change feed's own page, oldest first, from the event after ``after``.

	**The feed's page and not a second query**, so the event log is narrowed exactly as
	``/v1/changes`` narrows it. ``since`` includes the event it names, so the next page starts
	one past the last.
	"""

	return subroutine.domain.events.page(
		session,
		reader,
		workspace_ids=[workspace_id],
		size=size,
		since=None if after is None else after + 1,
	)


def _named (
	session: sqlalchemy.orm.Session, identifiers: typing.Iterable[uuid.UUID | None]
) -> subroutine.views.Vocabulary:
	"""Return a vocabulary that can name every account these ids hold, loaded once."""

	return subroutine.views.Vocabulary(
		session, user_ids=[identifier for identifier in identifiers if identifier is not None]
	)


def _render_projects (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID, rows: Rows
) -> list[subroutine.views.Project]:
	"""Render a page of projects with one vocabulary."""

	vocabulary = subroutine.views.Vocabulary.for_projects(session, rows)

	return [subroutine.views.project(row, vocabulary) for row in rows]


def _render_tasks (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID, rows: Rows
) -> list[subroutine.views.Task]:
	"""Render a page of tasks with one vocabulary, for the reader."""

	vocabulary = subroutine.views.Vocabulary.for_tasks(session, reader, rows)

	return [subroutine.views.task(row, vocabulary) for row in rows]


def _render_documents (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID, rows: Rows
) -> list[subroutine.views.Document]:
	"""Render a page of documents with one vocabulary."""

	vocabulary = subroutine.views.Vocabulary.for_documents(session, rows)

	return [subroutine.views.document(row, vocabulary) for row in rows]


def _render_comments (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID, rows: Rows
) -> list[subroutine.views.Comment]:
	"""Render a page of comments, naming every author in one query."""

	vocabulary = _named(session, [row.author_id for row in rows])

	return [subroutine.views.comment(row, vocabulary) for row in rows]


def _render_links (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID, rows: Rows
) -> list[subroutine.views.Edge]:
	"""Render a page of links as edges, each named by both its ends."""

	return subroutine.views.edges(
		session,
		reader,
		subroutine.domain.links.edges_of(session, reader, workspace_id=workspace_id, links=rows),
	)


def _render_verifications (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID, rows: Rows
) -> list[subroutine.views.Verification]:
	"""Render a page of verifications, with the number of each task they record, in one query."""

	task = subroutine.db.models.work.Task
	refs = dict(
		session.execute(
			sqlalchemy.select(task.id, task.ref).where(task.id.in_({row.task_id for row in rows}))
		).all()
	)
	vocabulary = _named(session, [row.created_by for row in rows])

	return [
		subroutine.views.verification(
			row,
			ref=refs[row.task_id],
			recorded_by=subroutine.views.username_in(vocabulary, row.created_by),
		)
		for row in rows
	]


def _render_events (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID, rows: Rows
) -> list[subroutine.views.Event]:
	"""Render a page of events as the change feed does, described in one batch."""

	described = subroutine.domain.events.descriptions(session, rows)

	return [subroutine.views.event(row, described) for row in rows]


def _render_saved_views (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID, rows: Rows
) -> list[subroutine.views.SavedView]:
	"""Render a page of saved views, naming every owner in one query."""

	vocabulary = _named(session, [row.owner_id for row in rows])

	return [
		subroutine.views.saved_view_seen(
			row, owner=subroutine.views.username_in(vocabulary, row.owner_id)
		)
		for row in rows
	]


def _render_users (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID, rows: Rows
) -> list[subroutine.views.User]:
	"""Render a page of accounts, walking whom each answers to once for the page."""

	answerable = subroutine.domain.accountability.answerable_for_many(
		session, [row.id for row in rows]
	)
	parents = subroutine.domain.accountability.account_parents_for_many(session, rows)

	return [
		subroutine.views.user(
			row, answers_to=answerable.get(row.id), account_parent=parents.get(row.id)
		)
		for row in rows
	]


def _each (
	render: typing.Callable[[typing.Any], pydantic.BaseModel],
) -> typing.Callable[
	[sqlalchemy.orm.Session, Principal, uuid.UUID, Rows], list[pydantic.BaseModel]
]:
	"""Return a page renderer for a kind whose rows need nothing loaded beside them."""

	def rendered (
		session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID, rows: Rows
	) -> list[pydantic.BaseModel]:
		"""Render each row on its own."""

		return [render(row) for row in rows]

	return rendered


_Project = subroutine.db.models.project.Project
_Task = subroutine.db.models.work.Task
_Document = subroutine.db.models.work.Document
_Comment = subroutine.db.models.activity.Comment
_Link = subroutine.db.models.work.Link
_Verification = subroutine.db.models.work.Verification
_Event = subroutine.db.models.activity.Event
_Tag = subroutine.db.models.vocabulary.Tag
_Status = subroutine.db.models.vocabulary.Status
_ItemType = subroutine.db.models.vocabulary.ItemType
_LinkType = subroutine.db.models.vocabulary.LinkType
_SavedView = subroutine.db.models.saved.SavedView
_User = subroutine.db.models.identity.User

#: Every kind an export holds, by the name its route and its file take. **The order is the order
#: a reader meets them in**: where the work lives, the work, what was said and joined about it,
#: then the words it is described in and who is named.
KINDS: dict[str, Kind] = {
	kind.name: kind
	for kind in (
		Kind(
			"projects", subroutine.views.Project, _Project.id,
			_ordered(_Project.id, _projects), _render_projects,
		),
		Kind("tasks", subroutine.views.Task, _Task.id, _ordered(_Task.id, _tasks), _render_tasks),
		Kind(
			"documents", subroutine.views.Document, _Document.id,
			_ordered(_Document.id, _documents), _render_documents,
		),
		Kind(
			"comments", subroutine.views.Comment, _Comment.id,
			_ordered(_Comment.id, _comments), _render_comments,
		),
		Kind("links", subroutine.views.Edge, _Link.id, _ordered(_Link.id, _links), _render_links),
		Kind(
			"verifications", subroutine.views.Verification, _Verification.id,
			_ordered(_Verification.id, _verifications), _render_verifications,
		),
		Kind("events", subroutine.views.Event, _Event.seq, _events, _render_events),
		Kind(
			"tags", subroutine.views.TagEntry, _Tag.id,
			_ordered(_Tag.id, _in_workspace(_Tag)), _each(subroutine.views.tag_entry),
		),
		Kind(
			"statuses", subroutine.views.Status, _Status.id,
			_ordered(_Status.id, _in_workspace(_Status)), _each(subroutine.views.status),
		),
		Kind(
			"item_types", subroutine.views.ItemType, _ItemType.id,
			_ordered(_ItemType.id, _in_workspace(_ItemType)), _each(subroutine.views.item_type),
		),
		Kind(
			"link_types", subroutine.views.LinkType, _LinkType.id,
			_ordered(_LinkType.id, _in_workspace(_LinkType)), _each(subroutine.views.link_type),
		),
		Kind(
			"saved_views", subroutine.views.SavedView, _SavedView.id,
			_ordered(_SavedView.id, _saved_views), _render_saved_views,
		),
		Kind("users", subroutine.views.User, _User.id, _ordered(_User.id, _users), _render_users),
	)
}
