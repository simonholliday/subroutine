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
the scoping statements check, and ``comment:read`` for comments, ``workspace:read`` for who
belongs to the workspace and ``project:read`` for who is shared into a project, which those
listings check for themselves and so this does too.

**Left out on purpose**: a withdrawn link and a deleted comment, which cannot be put back and are
recorded as events; roles, which every workspace is seeded with and no route makes; and every
credential, which no view carries.
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
import subroutine.domain.projects
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


def _workspace (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID
) -> sqlalchemy.Select[typing.Any]:
	"""Return the workspace's own row, as ``GET /v1/workspaces/<slug>`` reads it to a member.

	Nothing to refuse beyond what that route refuses, which is anybody outside the workspace, and
	an export is made only of a workspace its reader belongs to.
	"""

	model = subroutine.db.models.identity.Workspace

	return sqlalchemy.select(model).where(model.id == workspace_id)


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
		include_beneath_trash=True,
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
		include_beneath_trash=True,
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


def _tags (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID
) -> sqlalchemy.Select[typing.Any]:
	"""Return the tags the reader may see, as ``GET /v1/tags`` lists them (decision `#4094`)."""

	model = subroutine.db.models.vocabulary.Tag

	return sqlalchemy.select(model).where(
		model.workspace_id == workspace_id,
		subroutine.domain.scoping.tags_seen_by(reader, workspace_ids=[workspace_id]),
	)


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


def _members (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID
) -> sqlalchemy.Select[typing.Any]:
	"""Return who belongs to the workspace, as ``GET /v1/workspaces/<slug>/members`` lists them.

	**Refused without ``workspace:read``**, as that listing refuses it, and an account marked
	deleted is left out, as it leaves one out.
	"""

	subroutine.domain.authorization.authorize(
		session, reader, subroutine.permissions.WORKSPACE_READ, workspace_id=workspace_id
	)

	model = subroutine.db.models.identity.WorkspaceMember
	account = subroutine.db.models.identity.User

	return (
		sqlalchemy.select(model)
		.join(account, account.id == model.user_id)
		.where(model.workspace_id == workspace_id, account.deleted_at.is_(None))
	)


def _project_members (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID
) -> sqlalchemy.Select[typing.Any]:
	"""Return who is shared into each project this export holds.

	**Through the projects it holds**, so a private project the reader is not in adds nothing,
	and refused as ``GET /v1/projects/<key>/members`` refuses: ``project:read``, which the
	project statement checks against a token and this checks against a role.
	"""

	subroutine.domain.authorization.authorize(
		session, reader, subroutine.permissions.PROJECT_READ, workspace_id=workspace_id
	)

	model = subroutine.db.models.project.ProjectMember
	project = subroutine.db.models.project.Project
	held = _projects(session, reader, workspace_id).with_only_columns(project.id)

	return sqlalchemy.select(model).where(
		model.workspace_id == workspace_id, model.project_id.in_(held)
	)


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
		# **Every event held, archived ones included** (`#251`): leaving is taking the history.
		everything=True,
	)


def _named (
	session: sqlalchemy.orm.Session, identifiers: typing.Iterable[uuid.UUID | None]
) -> subroutine.views.Vocabulary:
	"""Return a vocabulary that can name every account these ids hold, loaded once."""

	return subroutine.views.Vocabulary(
		session, user_ids=[identifier for identifier in identifiers if identifier is not None]
	)


def _by_id (
	session: sqlalchemy.orm.Session, model: typing.Any, identifiers: typing.Iterable[uuid.UUID]
) -> dict[uuid.UUID, typing.Any]:
	"""Return the rows of one table these ids name, loaded in one query."""

	wanted = set(identifiers)

	if not wanted:
		return {}

	found: list[typing.Any] = list(
		session.scalars(sqlalchemy.select(model).where(model.id.in_(wanted)))
	)

	return {row.id: row for row in found}


def _render_workspace (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID, rows: Rows
) -> list[subroutine.views.Workspace]:
	"""Render the workspace with the address of the project it has prioritised, as its route does."""

	focused = subroutine.domain.projects.prioritised_addresses(
		session, reader, workspace_ids=[row.id for row in rows]
	)

	return [subroutine.views.workspace(row, prioritised=focused.get(row.id)) for row in rows]


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


def _render_members (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID, rows: Rows
) -> list[subroutine.views.Member]:
	"""Render a page of memberships as the members listing does, loading what they join once."""

	accounts = _by_id(session, subroutine.db.models.identity.User, [row.user_id for row in rows])
	roles = _by_id(session, subroutine.db.models.identity.Role, [row.role_id for row in rows])
	within = session.scalars(
		sqlalchemy.select(subroutine.db.models.identity.Workspace).where(
			subroutine.db.models.identity.Workspace.id == workspace_id
		)
	).one()
	focused = subroutine.domain.projects.prioritised_addresses(
		session, reader, workspace_ids=[workspace_id]
	)
	answerable = subroutine.domain.accountability.answerable_for_many(session, list(accounts))
	parents = subroutine.domain.accountability.account_parents_for_many(
		session, list(accounts.values())
	)

	return [
		subroutine.views.member(
			row,
			account=accounts[row.user_id],
			role=roles[row.role_id],
			within=within,
			prioritised=focused.get(workspace_id),
			answers_to=answerable.get(row.user_id),
			account_parent=parents.get(row.user_id),
		)
		for row in rows
	]


def _render_project_members (
	session: sqlalchemy.orm.Session, reader: Principal, workspace_id: uuid.UUID, rows: Rows
) -> list[subroutine.views.ProjectMember]:
	"""Render a page of project memberships as a project's members listing does."""

	accounts = _by_id(session, subroutine.db.models.identity.User, [row.user_id for row in rows])
	projects = _by_id(
		session, subroutine.db.models.project.Project, [row.project_id for row in rows]
	)
	answerable = subroutine.domain.accountability.answerable_for_many(session, list(accounts))
	parents = subroutine.domain.accountability.account_parents_for_many(
		session, list(accounts.values())
	)

	return [
		subroutine.views.project_member(
			row,
			account=accounts[row.user_id],
			within=projects[row.project_id],
			answers_to=answerable.get(row.user_id),
			account_parent=parents.get(row.user_id),
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


_Workspace = subroutine.db.models.identity.Workspace
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
_WorkspaceMember = subroutine.db.models.identity.WorkspaceMember
_ProjectMember = subroutine.db.models.project.ProjectMember

#: Every kind an export holds, by the name its route and its file take. **The order is the order
#: a reader meets them in**: the workspace and where the work lives in it, the work, what was said
#: and joined about it, then the words it is described in, who is named, and who belongs where.
KINDS: dict[str, Kind] = {
	kind.name: kind
	for kind in (
		Kind(
			"workspace", subroutine.views.Workspace, _Workspace.id,
			_ordered(_Workspace.id, _workspace), _render_workspace,
		),
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
			_ordered(_Tag.id, _tags), _each(subroutine.views.tag_entry),
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
		Kind(
			"members", subroutine.views.Member, _WorkspaceMember.id,
			_ordered(_WorkspaceMember.id, _members), _render_members,
		),
		Kind(
			"project_members", subroutine.views.ProjectMember, _ProjectMember.id,
			_ordered(_ProjectMember.id, _project_members), _render_project_members,
		),
	)
}


#: **What each kind's line leaves out of its view** - decision ``#4049``: an export keeps what was
#: stored and drops what the system works out, since that is true only of the moment and the
#: reader it was worked out for. So: a count, a state read off other rows, a ranking, a rendering
#: for a reader, and the walk of whom an agent answers to.
#:
#: **A name for something the row stores is kept** - a key, a label, a username, a number -
#: because that is what lets one line be read without the rest of the export, and the ids beside
#: them are what join it to the rest. ``tests/test_export.py`` holds every field of every kind to
#: one side or the other, so a field added to a view has to be placed before it can be exported.
COMPUTED: dict[str, frozenset[str]] = {
	"workspace": frozenset(),
	"projects": frozenset({"hidden_statuses"}),
	"tasks": frozenset({
		"size_bytes",
		"project_colour",
		"blocked",
		"blocking",
		"sub_tasks_done",
		"included_done",
		"included_count",
		"included_done_count",
		"included_unseen",
		"blocked_by",
		"blocks_others",
		"revisions",
		"beneath",
		"priority_score",
		"rank",
		"relevance",
		"recurrence_description",
		"estimate_human",
		"reminder_human",
		"is_complete",
		"claimed_by_answers_to",
		"assignee_answers_to",
	}),
	"documents": frozenset({
		"size_bytes", "project_colour", "sub_documents", "revisions", "relevance"
	}),
	"comments": frozenset(),
	"links": frozenset(),
	"verifications": frozenset(),
	"events": frozenset({"item_title"}),
	"tags": frozenset(),
	"statuses": frozenset(),
	"item_types": frozenset(),
	"link_types": frozenset(),
	"saved_views": frozenset({"about_the_reader"}),
	"users": frozenset({"answers_to"}),
	"members": frozenset(),
	"project_members": frozenset(),
}

#: **What an end of a link keeps**: which item it is, and what it is called. The rest of a
#: :class:`~subroutine.views.LinkEnd` is that item's own state, which its own file holds - and
#: holds as it was stored, where an end reports it worked out for the reader.
END_KEPT = frozenset({"entity_type", "id", "ref", "title"})

#: **What an account keeps where a line names one inside it**: which account, and the name a
#: person reads. The rest is the account's own row, which ``users`` holds as it was stored, or
#: whom it answers to, worked out for the reader.
ACCOUNT_KEPT = frozenset({"id", "username"})

#: **What each object nested in a line keeps**, by kind and field, for :data:`END_KEPT`'s reason.
#: A membership names its workspace by id and short name, since the workspace's own file holds it.
NESTED_KEPT: dict[str, dict[str, frozenset[str]]] = {
	"links": {"source": END_KEPT, "target": END_KEPT},
	"members": {"user": ACCOUNT_KEPT, "workspace": frozenset({"id", "slug"})},
	"project_members": {"user": ACCOUNT_KEPT},
}

#: What an export never holds, said in its manifest so that nobody takes a quiet absence for a
#: complete record.
NEVER_IN_IT = (
	"Anything the credential could not read: a private project it is not a member of, and "
	"everything in one.",
	"The last second of the event log, which the change feed holds back so a write still "
	"being saved is never skipped.",
	"A withdrawn link or a deleted comment, which cannot be put back; the event log records each.",
	"Any password, token, sign-in link, session or calendar feed address.",
	"What the system works out rather than stores - a count, a ranking, a state read off other "
	"rows; each kind's own list is under 'fields_left_out'.",
)

#: How the version the manifest names is to be read.
VERSIONING = (
	"The version of Subroutine that wrote these lines. An export written by 1.x is read by "
	"anything written for 1.y: a minor release may add fields, and only a major release removes "
	"or renames one."
)


def line (kind: str, item: pydantic.BaseModel) -> dict[str, typing.Any]:
	"""Return one row as its line in an export: the view, less what the system works out.

	An object nested in it keeps only what :data:`NESTED_KEPT` names, for the reasons given there.
	"""

	left_out: dict[str, typing.Any] = dict.fromkeys(COMPUTED[kind], True)

	for name, kept in NESTED_KEPT.get(kind, {}).items():
		nested = type(getattr(item, name)).model_fields
		left_out[name] = {field for field in nested if field not in kept}

	return item.model_dump(mode="json", exclude=left_out)
