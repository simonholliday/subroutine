"""Tasks over HTTP.

Thin, deliberately. Every rule about what may happen — permissions, date interpretation,
ref allocation, the completed-at invariant — lives in ``subroutine.domain.tasks``, which
the CLI calls too. What is here is the translation: HTTP in, service call, representation
out, and the resolution of the two things a URL leaves implicit — which workspace (§8.2)
and which task (``{id_or_ref}``, §8.1).

**Nothing here filters for visibility itself.** Both the listing and the single-task lookup
start from ``domain.scoping.readable_tasks``, so a task in a private project is not found
rather than forbidden, and a token's project scope narrows a listing exactly as it narrows
a write.
"""

import datetime
import typing
import uuid

import fastapi
import sqlalchemy
import sqlalchemy.orm
import starlette.requests

import subroutine.api.concurrency
import subroutine.api.dependencies
import subroutine.api.pagination
import subroutine.api.schemas
import subroutine.api.security
import subroutine.api.shaping
import subroutine.config
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.patch
import subroutine.domain.projects
import subroutine.domain.refs
import subroutine.domain.scoping
import subroutine.domain.selection
import subroutine.domain.tasks
import subroutine.errors
import subroutine.views

router = fastapi.APIRouter(prefix="/v1/tasks", tags=["tasks"])

#: How many rows a listing returns when the caller does not say. Mirrors
#: ``Settings.default_page_size``; the hard ceiling is ``max_page_size``.
DEFAULT_LIMIT = 50

#: Fields ``?order=`` accepts, and the columns they mean. Deliberately a short list: every
#: entry is a promise about an index, and a sort the database cannot serve cheaply is worse
#: than no sort at all.
SORTABLE: dict[str, typing.Any] = {
	"created_at": subroutine.db.models.work.Task.created_at,
	"updated_at": subroutine.db.models.work.Task.updated_at,
	"due_at": subroutine.db.models.work.Task.due_at,
	"planned_for": subroutine.db.models.work.Task.planned_for,
	"importance": subroutine.db.models.work.Task.importance,
	"urgency": subroutine.db.models.work.Task.urgency,
	# §6.3's derived ordering key, so an agent has one sensible sort without inventing it.
	# NULL when either axis is unset, which the NULLS LAST in every ordering then handles.
	"priority_score": (
		subroutine.db.models.work.Task.importance * subroutine.db.models.work.Task.urgency
	),
	"ref": subroutine.db.models.work.Task.ref,
	"title": subroutine.db.models.work.Task.title,
}

#: Newest first, which is what "what have I got" means for a to-do list.
DEFAULT_ORDER = ("-created_at",)

#: What ``?fields=`` may name, read from the view so the two cannot drift (SPEC.md §14.10).
SELECTABLE = subroutine.api.shaping.selectable(subroutine.views.Task)


class Create(subroutine.api.schemas.RequestModel):
	"""What ``POST /v1/tasks`` accepts.

	Either ``text`` — one captured line, parsed per §6.13 — or the structured fields, or
	both: **anything given explicitly wins over what the text said**, so a client that wants
	no magic simply sends structured fields and no text.
	"""

	text: str | None = None
	title: str | None = None
	description: str | None = None

	workspace_id: str | None = None
	project: str | None = None
	parent_task_id: uuid.UUID | None = None

	type: str | None = None
	status: str | None = None
	assignee_id: uuid.UUID | None = None
	importance: int | None = None
	urgency: int | None = None

	due: str | None = None
	due_is_all_day: bool | None = None
	planned_for: str | None = None
	start: str | None = None
	start_is_all_day: bool | None = None
	timezone: str | None = None


class Update(subroutine.api.schemas.RequestModel):
	"""What ``PATCH /v1/tasks/{id_or_ref}`` accepts.

	**A field left out is unchanged; a field sent as ``null`` is cleared** (§8.3). The two
	are told apart by ``model_fields_set``, never by comparing against a default — that is
	what makes "clear the due date" expressible at all.
	"""

	title: str | None = None
	description: str | None = None
	status: str | None = None
	assignee_id: uuid.UUID | None = None
	importance: int | None = None
	urgency: int | None = None
	due: str | None = None
	due_is_all_day: bool | None = None
	planned_for: str | None = None
	start: str | None = None
	start_is_all_day: bool | None = None
	timezone: str | None = None

	#: The version this change is based on (SPEC.md §8.9). Optional; ``If-Match`` does the
	#: same job for a client that prefers the header.
	expected_version: int | None = None


@router.post("", status_code=201, summary="Create a task")
def create (
	body: Create,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Task:
	"""Create a task, from structured fields or from a captured line."""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=body.workspace_id)
	supplied = body.model_fields_set

	structured: dict[str, typing.Any] = {
		name: getattr(body, name)
		for name in (
			"description",
			"assignee_id",
			"importance",
			"urgency",
			"due",
			"due_is_all_day",
			"planned_for",
			"start",
			"start_is_all_day",
		)
		if name in supplied
	}

	if body.title is not None:
		structured["title"] = body.title

	if body.type is not None:
		structured["type_key"] = body.type

	if body.status is not None:
		structured["status_key"] = body.status

	if body.parent_task_id is not None:
		structured["parent"] = _resolve(session, actor, workspace, str(body.parent_task_id))

	if body.text is not None:
		created, _capture = subroutine.domain.tasks.create_from_text(
			session,
			workspace=workspace,
			text=body.text,
			timezone=body.timezone,
			actor=actor,
			**structured,
		)

		return _rendered(session, created)

	if not body.title:
		raise subroutine.errors.ValidationError(
			"A task needs a title.",
			code="missing_field",
			errors=[
				subroutine.errors.FieldError(
					field="title",
					code="missing_field",
					message="Send 'title', or send 'text' to have one parsed out of a line.",
				)
			],
		)

	created = subroutine.domain.tasks.create(
		session,
		project=_project(session, actor, workspace, body.project),
		timezone=body.timezone,
		actor=actor,
		**structured,
	)

	return _rendered(session, created)


# ``response_model`` rather than a return annotation, because a shaped response is not a
# ``Collection[Task]`` and returning one would be a lie mypy is right to catch. The model
# still documents the default in OpenAPI and still validates it; a ``JSONResponse`` from the
# shaping path is passed through untouched, which is the documented FastAPI behaviour.
@router.get(
	"",
	summary="List tasks",
	response_model=subroutine.views.Collection[subroutine.views.Task],
)
def listing (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	workspace_id: str | None = fastapi.Query(
		None, description="Which workspace, by id or slug. Needed when you can reach several."
	),
	project: str | None = fastapi.Query(None, description="Restrict to one project, by key or id."),
	status: str | None = fastapi.Query(None, description="Restrict to one status key."),
	assignee_id: uuid.UUID | None = fastapi.Query(None, description="Restrict to one assignee."),
	type: str | None = fastapi.Query(None, description="Restrict to one item type key."),
	q: str | None = fastapi.Query(None, description="Match this text in the title."),
	due_before: datetime.datetime | None = fastapi.Query(None, description="Due strictly before."),
	due_after: datetime.datetime | None = fastapi.Query(None, description="Due strictly after."),
	include_completed: bool = fastapi.Query(False, description="Include finished tasks."),
	order: str | None = fastapi.Query(
		None, description="Comma-separated sort fields, '-' for descending: '-importance,due_at'."
	),
	limit: int | None = fastapi.Query(None, ge=1, description="How many to return."),
	cursor: str | None = fastapi.Query(None, description="Continue after a previous page."),
	include_total: bool = fastapi.Query(
		False, description="Count the whole result. Costs a second scan; off by default."
	),
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""List tasks, narrowed by whatever the query string asks for."""

	shape = subroutine.api.shaping.wanted(
		format=format, fields=fields, available=SELECTABLE, entity="task"
	)

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	statement = subroutine.domain.scoping.readable_tasks(
		actor, workspace_ids=[workspace.id], include_completed=include_completed
	)

	model = subroutine.db.models.work.Task

	if project is not None:
		statement = statement.where(
			model.project_id == _project(session, actor, workspace, project).id
		)

	if status is not None:
		statement = statement.where(
			model.status_id == subroutine.domain.tasks.status_for(session, workspace.id, status).id
		)

	if type is not None:
		statement = statement.where(
			model.type_id == subroutine.domain.tasks.item_type_for(session, workspace.id, type).id
		)

	if assignee_id is not None:
		statement = statement.where(model.assignee_id == assignee_id)

	if q:
		# `ilike` rather than `like`: SQLite's LIKE is case-insensitive for ASCII and
		# PostgreSQL's is not, so an unqualified LIKE is a filter that behaves differently
		# depending on where it runs (SPEC.md §10.3).
		statement = statement.where(model.title.ilike(f"%{_escaped(q)}%", escape="\\"))

	if due_before is not None:
		statement = statement.where(model.due_at < due_before)

	if due_after is not None:
		statement = statement.where(model.due_at > due_after)

	return _page(
		session,
		settings,
		statement,
		order=order,
		limit=limit,
		cursor=cursor,
		include_total=include_total,
		shape=shape,
	)


@router.get(
	"/{id_or_ref}", summary="Read one task", response_model=subroutine.views.Task
)
def read (
	id_or_ref: subroutine.api.schemas.ItemAddress,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""Return one task, by id or by ref."""

	shape = subroutine.api.shaping.wanted(
		format=format, fields=fields, available=SELECTABLE, entity="task"
	)
	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)

	return subroutine.api.shaping.single(
		_rendered(session, _resolve(session, actor, workspace, id_or_ref)), shape
	)


@router.patch("/{id_or_ref}", summary="Change a task")
def change (
	request: starlette.requests.Request,
	id_or_ref: subroutine.api.schemas.ItemAddress,
	body: Update,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Task:
	"""Change a task. Omitted fields are untouched; nulls clear (SPEC.md §8.3)."""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	task = _resolve(session, actor, workspace, id_or_ref)

	supplied = body.model_fields_set
	changes: dict[str, typing.Any] = {
		name: getattr(body, name)
		for name in (
			"title",
			"description",
			"assignee_id",
			"importance",
			"urgency",
			"due",
			"planned_for",
			"start",
		)
		if name in supplied
	}

	# These three are not patchable-to-null in the service — they qualify another field
	# rather than being one — so they are passed only when given.
	for name, parameter in (
		("status", "status_key"),
		("due_is_all_day", "due_is_all_day"),
		("start_is_all_day", "start_is_all_day"),
	):
		if name in supplied and getattr(body, name) is not None:
			changes[parameter] = getattr(body, name)

	with subroutine.api.concurrency.reporting(lambda: _rendered(session, task)):
		updated = subroutine.domain.tasks.update(
			session,
			task,
			timezone=body.timezone,
			expected_version=subroutine.api.concurrency.expected(request, body.expected_version),
			actor=actor,
			**changes,
		)

	return _rendered(session, updated)


@router.post("/{id_or_ref}/complete", summary="Mark a task finished")
def complete (
	request: starlette.requests.Request,
	id_or_ref: subroutine.api.schemas.ItemAddress,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Task:
	"""Mark a task finished, in whatever this workspace calls its finished status."""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	task = _resolve(session, actor, workspace, id_or_ref)

	with subroutine.api.concurrency.reporting(lambda: _rendered(session, task)):
		finished = subroutine.domain.tasks.complete(
			session,
			task,
			expected_version=subroutine.api.concurrency.expected(request),
			actor=actor,
		)

	return _rendered(session, finished)


@router.delete("/{id_or_ref}", summary="Move a task to the trash")
def remove (
	request: starlette.requests.Request,
	id_or_ref: subroutine.api.schemas.ItemAddress,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Task:
	"""Soft-delete a task. It stays recoverable (SPEC.md §6.9).

	The deleted task is returned rather than an empty 204, so a caller can see when it
	happened without asking again — and so an agent can tell a repeat call apart from a
	first one.
	"""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	task = _resolve(session, actor, workspace, id_or_ref)

	with subroutine.api.concurrency.reporting(lambda: _rendered(session, task)):
		removed = subroutine.domain.tasks.delete(
			session,
			task,
			expected_version=subroutine.api.concurrency.expected(request),
			actor=actor,
		)

	return _rendered(session, removed)


def _resolve (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	workspace: subroutine.db.models.identity.Workspace,
	id_or_ref: str,
) -> subroutine.db.models.work.Task:
	"""Find one task by id or ref, or report that there is no such thing.

	Searched **through the scoping helper**, so a task the caller may not see is reported
	as absent rather than forbidden — saying "forbidden" about a task in a private project
	would confirm that it exists (SPEC.md §7.3a).

	Deleted tasks resolve. A reference to something in the trash is more useful than a
	dangling one, and ``deleted_at`` is in the response for the caller to see.
	"""

	model = subroutine.db.models.work.Task
	wanted = id_or_ref.strip()
	statement = subroutine.domain.scoping.readable_tasks(
		actor,
		workspace_ids=[workspace.id],
		include_deleted=True,
		include_archived=True,
		include_templates=True,
	)

	# A ref is all digits and a project key must start with a letter (SPEC.md §6.2), so
	# the two path spaces cannot overlap and the order of these branches is not a guess.
	ref = subroutine.domain.refs.parse_ref(wanted)

	if ref is not None:
		found = session.scalars(statement.where(model.ref == ref)).first()

	else:
		try:
			found = session.scalars(statement.where(model.id == uuid.UUID(wanted))).first()

		except ValueError:
			# Neither a ref nor an id, so nothing can answer to it.
			found = None

	if found is None:
		raise subroutine.errors.NotFound(
			f"There is no task {id_or_ref!r} here.",
			errors=[
				subroutine.errors.FieldError(
					field="id_or_ref",
					code="not_found",
					message=f"No task in {workspace.slug} answers to {id_or_ref!r}.",
					hint="Use a ref like '42' or a task id. GET /v1/tasks lists what you "
					"can see.",
				)
			],
		)

	return found


def _project (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	workspace: subroutine.db.models.identity.Workspace,
	wanted: str | None,
) -> typing.Any:
	"""Find a project by key or id, defaulting to the workspace's Inbox.

	The Inbox default is what makes ``POST /v1/tasks {"title": "…"}`` work without the
	caller knowing that projects exist — §1.4's rule, applied to the API rather than only
	to the CLI.
	"""

	if wanted is None:
		inbox = subroutine.domain.bootstrap.inbox_for(session, workspace)

		if inbox is None:
			raise subroutine.errors.InternalError(
				"This workspace has no Inbox to file a task in.",
				hint="It was interrupted part-way through setup; run 'subroutine init' again.",
			)

		return inbox

	model = subroutine.db.models.project.Project
	statement = subroutine.domain.scoping.readable_projects(
		actor, workspace_ids=[workspace.id], include_archived=True
	)

	try:
		found = session.scalars(statement.where(model.id == uuid.UUID(wanted.strip()))).first()

	except ValueError:
		found = session.scalars(
			statement.where(model.key == subroutine.domain.projects.normalize_key(wanted))
		).first()

	if found is None:
		raise subroutine.errors.NotFound(
			f"There is no project {wanted!r} here.",
			errors=[
				subroutine.errors.FieldError(
					field="project",
					code="not_found",
					message=f"No project in {workspace.slug} answers to {wanted!r}.",
					hint="Use a project key like 'SR' or a project id. GET /v1/projects lists "
					"what you can see.",
				)
			],
		)

	return found


def _page (
	session: sqlalchemy.orm.Session,
	settings: subroutine.config.Settings,
	statement: sqlalchemy.Select[tuple[subroutine.db.models.work.Task]],
	*,
	order: str | None,
	limit: int | None,
	cursor: str | None,
	include_total: bool,
	shape: subroutine.api.shaping.Shape,
) -> typing.Any:
	"""Order, paginate and render a task query.

	Returns ``Any`` because a shaped response is not a ``Collection[Task]`` — its items are
	lines, or addresses, or partial objects. The endpoint still *declares* the collection, so
	the OpenAPI document describes the default that almost every caller receives.
	"""

	keys = subroutine.api.pagination.parse_order(
		order,
		allowed=SORTABLE,
		default=DEFAULT_ORDER,
		tiebreak=subroutine.db.models.work.Task.id,
	)
	size = min(limit or settings.default_page_size, settings.max_page_size)
	total = None

	if include_total:
		total = session.scalar(
			sqlalchemy.select(sqlalchemy.func.count()).select_from(statement.subquery())
		)

	if cursor is not None:
		values = subroutine.api.pagination.decode(
			settings.require_secret_key(), keys, cursor
		)
		statement = statement.where(subroutine.api.pagination.after(keys, values))

	ordered = statement.order_by(*[key.ordering() for key in keys])

	# One more than asked for, which is how "is there another page" is answered without a
	# second query and without a count.
	rows = list(session.scalars(ordered.limit(size + 1)))
	has_more = len(rows) > size
	rows = rows[:size]

	vocabulary = subroutine.views.Vocabulary.for_tasks(session, rows)

	return subroutine.api.shaping.response(
		[subroutine.views.task(row, vocabulary) for row in rows],
		subroutine.views.Page(
			limit=size,
			has_more=has_more,
			next_cursor=(
				subroutine.api.pagination.encode(settings.require_secret_key(), keys, rows[-1])
				if has_more and rows
				else None
			),
			total=total,
		),
		shape,
	)


def _rendered (
	session: sqlalchemy.orm.Session, row: subroutine.db.models.work.Task
) -> subroutine.views.Task:
	"""Render one task, loading the vocabulary it names."""

	return subroutine.views.task(
		row, subroutine.views.Vocabulary.for_tasks(session, [row])
	)


def _escaped (value: str) -> str:
	"""Escape a caller's text for use inside a LIKE pattern.

	Without this, a search for ``50%`` matches everything and a search for ``a_b`` matches
	``axb`` — surprising, and on a large table an accidental full scan.
	"""

	return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
