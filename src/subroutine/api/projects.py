"""Projects over HTTP.

The same shape as tasks and for the same reasons: the service layer decides, this
translates. Addressed by ``{id_or_key}`` — a project key is what people have in front of
them, and requiring an id to open ``SR`` would be a needless round trip (SPEC.md §8.1).
"""

import typing
import uuid

import fastapi
import sqlalchemy
import sqlalchemy.orm
import starlette.requests

import subroutine.api.concurrency
import subroutine.api.dependencies
import subroutine.api.pagination
import subroutine.api.query
import subroutine.api.schemas
import subroutine.api.security
import subroutine.api.shaping
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.authentication
import subroutine.domain.paging
import subroutine.domain.projects
import subroutine.domain.scoping
import subroutine.domain.selection
import subroutine.errors
import subroutine.views

router = fastapi.APIRouter(prefix="/v1/projects", tags=["projects"])

#: What ``?order=`` accepts here. ``key`` is the one people think in.
SORTABLE: dict[str, typing.Any] = {
	"created_at": subroutine.db.models.project.Project.created_at,
	"updated_at": subroutine.db.models.project.Project.updated_at,
	"key": subroutine.db.models.project.Project.key,
	"title": subroutine.db.models.project.Project.title,
	"path": subroutine.db.models.project.Project.path,
}

#: By path, so a listing reads as the tree it is: a parent immediately followed by its
#: children, rather than a flat list the caller has to reassemble.
DEFAULT_ORDER = ("path",)

#: What ``?fields=`` may name, read from the view so the two cannot drift (SPEC.md §14.10).
SELECTABLE = subroutine.api.shaping.selectable(subroutine.views.Project)


class Create(subroutine.api.schemas.RequestModel):
	"""What ``POST /v1/projects`` accepts."""

	key: str
	title: str
	description: str | None = None
	workspace_id: str | None = None
	parent: str | None = None
	template: str = "blank"
	visibility: str = "public"
	owner_id: uuid.UUID | None = None


class Update(subroutine.api.schemas.RequestModel):
	"""What ``PATCH /v1/projects/{id_or_key}`` accepts.

	``key`` is absent on purpose: it is the first half of every ref the project has minted,
	and those are written into commit messages, chat and other people's documents. Renaming
	it here would not rewrite them.
	"""

	title: str | None = None
	description: str | None = None
	visibility: str | None = None
	owner_id: uuid.UUID | None = None

	#: The version this change is based on (SPEC.md §8.9).
	expected_version: int | None = None


class Move(subroutine.api.schemas.RequestModel):
	"""Where a project should sit in the tree.

	``parent: null`` makes it a root, which is why this is a body rather than a query
	parameter — "no parent" and "unchanged" have to be distinguishable (§8.3).

	**And they were not, until 2026-07-30.** The handler read ``body.parent`` directly, so an
	*omitted* parent and an explicit ``null`` both meant "move to root" — and
	``POST /v1/projects/web/move {}`` silently flattened a project and its whole subtree. This
	was the one mutating site in the API that did not use ``model_fields_set``, twenty lines
	below a docstring saying it must. A move is not a field being dropped; it rewrites the
	materialised path of every descendant, and there is no undo.
	"""

	parent: str | None = None

	def requested (self) -> bool:
		"""Report whether the caller actually named a destination."""

		return "parent" in self.model_fields_set


@router.post("", status_code=201, summary="Create a project")
def create (
	body: Create,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Project:
	"""Create a project, optionally inside another."""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=body.workspace_id)
	parent = None if body.parent is None else resolve(session, actor, workspace, body.parent)

	created = subroutine.domain.projects.create(
		session,
		workspace_id=workspace.id,
		key=body.key,
		title=body.title,
		description=body.description,
		parent=parent,
		template=body.template,
		visibility=body.visibility,
		# The creator owns what they create unless they say otherwise, which is also what
		# makes a private project visible to them (SPEC.md §7.3a).
		owner_id=body.owner_id if body.owner_id is not None else actor.user.id,
		actor=actor,
	)

	return _rendered(session, created)


@router.get(
	"",
	summary="List projects",
	dependencies=[subroutine.api.query.UnknownQueryDep],
	response_model=subroutine.views.Collection[subroutine.views.Project],
)
def listing (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
	parent: str | None = fastapi.Query(None, description="Only projects directly inside this one."),
	visibility: str | None = fastapi.Query(None, description="'public' or 'private'."),
	include_archived: bool = fastapi.Query(False, description="Include archived projects."),
	order: str | None = fastapi.Query(None, description="Comma-separated sort fields, '-' reverses."),
	limit: int | None = fastapi.Query(
		None,
		# **No `ge=1` here, deliberately.** `domain.paging.size` is the one arbiter, so that
		# this endpoint and the local client refuse an impossible page identically — with
		# `limit` as the field, not FastAPI's `query.limit`. Two copies of the rule produced
		# two different refusals for the same mistake.
		description="How many to return. At least 1; capped at the instance's max_page_size.",
	),
	cursor: str | None = fastapi.Query(None, description="Continue after a previous page."),
	include_total: bool = fastapi.Query(False, description="Count the whole result."),
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""List the projects this caller can see."""

	shape = subroutine.api.shaping.wanted(
		format=format, fields=fields, available=SELECTABLE, entity="project"
	)

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	statement = subroutine.domain.scoping.readable_projects(
		actor, workspace_ids=[workspace.id], include_archived=include_archived
	)

	model = subroutine.db.models.project.Project

	if parent is not None:
		statement = statement.where(
			model.parent_id == resolve(session, actor, workspace, parent).id
		)

	if visibility is not None:
		statement = statement.where(model.visibility == visibility)

	keys = subroutine.api.pagination.parse_order(
		order, allowed=SORTABLE, default=DEFAULT_ORDER, tiebreak=model.id
	)
	# One definition of a page size, shared with the local client (SPEC.md §13.7): the two
	# transports disagreed about limit until 2026-07-30 because each had its own copy.
	size = subroutine.domain.paging.size(limit, settings)
	total = None

	if include_total:
		total = session.scalar(
			sqlalchemy.select(sqlalchemy.func.count()).select_from(statement.subquery())
		)

	if cursor is not None:
		statement = statement.where(
			subroutine.api.pagination.after(
				keys,
				subroutine.api.pagination.decode(settings.require_secret_key(), keys, cursor),
			)
		)

	rows = list(
		session.scalars(statement.order_by(*[key.ordering() for key in keys]).limit(size + 1))
	)
	has_more = len(rows) > size
	rows = rows[:size]

	vocabulary = subroutine.views.Vocabulary.for_projects(session, rows)

	return subroutine.api.shaping.response(
		[subroutine.views.project(row, vocabulary) for row in rows],
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


@router.get(
	"/{id_or_key}", summary="Read one project", response_model=subroutine.views.Project
)
def read (
	id_or_key: str,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""Return one project, by id or by key."""

	shape = subroutine.api.shaping.wanted(
		format=format, fields=fields, available=SELECTABLE, entity="project"
	)
	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)

	return subroutine.api.shaping.single(
		_rendered(session, resolve(session, actor, workspace, id_or_key)), shape
	)


@router.patch("/{id_or_key}", summary="Change a project")
def change (
	request: starlette.requests.Request,
	id_or_key: str,
	body: Update,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Project:
	"""Change a project. Omitted fields are untouched; nulls clear (SPEC.md §8.3)."""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	project = resolve(session, actor, workspace, id_or_key)

	supplied = body.model_fields_set
	changes: dict[str, typing.Any] = {
		name: getattr(body, name)
		for name in ("title", "description", "owner_id")
		if name in supplied
	}

	if "visibility" in supplied and body.visibility is not None:
		changes["visibility"] = body.visibility

	with subroutine.api.concurrency.reporting(lambda: _rendered(session, project)):
		updated = subroutine.domain.projects.update(
			session,
			project,
			expected_version=subroutine.api.concurrency.expected(request, body.expected_version),
			actor=actor,
			**changes,
		)

	return _rendered(session, updated)


@router.post("/{id_or_key}/move", summary="Move a project in the tree")
def move (
	id_or_key: str,
	body: Move,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Project:
	"""Reparent a project, taking its whole subtree with it."""

	if not body.requested():
		raise subroutine.errors.ValidationError(
			"A move has to say where to.",
			code="missing_field",
			errors=[
				subroutine.errors.FieldError(
					field="parent",
					code="missing_field",
					message="Send 'parent' with a project key or id, or 'parent': null to make "
					"this a root project.",
				)
			],
			hint="An omitted 'parent' used to mean 'move to root', which flattened whole "
			"subtrees by accident.",
		)

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	project = resolve(session, actor, workspace, id_or_key)
	parent = None if body.parent is None else resolve(session, actor, workspace, body.parent)

	subroutine.domain.projects.move(session, project, parent=parent, actor=actor)

	return _rendered(session, project)


@router.delete("/{id_or_key}", summary="Move a project to the trash")
def remove (
	request: starlette.requests.Request,
	id_or_key: str,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Project:
	"""Soft-delete a project. Its tasks leave the visible world with it, and return with it."""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	project = resolve(session, actor, workspace, id_or_key)

	with subroutine.api.concurrency.reporting(lambda: _rendered(session, project)):
		removed = subroutine.domain.projects.delete(
			session,
			project,
			expected_version=subroutine.api.concurrency.expected(request),
			actor=actor,
		)

	return _rendered(session, removed)


def resolve (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	workspace: subroutine.db.models.identity.Workspace,
	id_or_key: str,
) -> subroutine.db.models.project.Project:
	"""Find one project by id or key, or report that there is no such thing.

	Through the scoping helper, so a private project the caller is not a member of is
	reported as absent rather than forbidden — saying "forbidden" would confirm it exists.
	"""

	model = subroutine.db.models.project.Project
	wanted = id_or_key.strip()
	statement = subroutine.domain.scoping.readable_projects(
		actor, workspace_ids=[workspace.id], include_archived=True
	)

	try:
		found = session.scalars(statement.where(model.id == uuid.UUID(wanted))).first()

	except ValueError:
		found = session.scalars(
			statement.where(model.key == subroutine.domain.projects.normalize_key(wanted))
		).first()

	if found is None:
		raise subroutine.errors.NotFound(
			f"There is no project {id_or_key!r} here.",
			errors=[
				subroutine.errors.FieldError(
					field="id_or_key",
					code="not_found",
					message=f"No project in {workspace.slug} answers to {id_or_key!r}.",
					hint="Use a project key like 'SR' or a project id. GET /v1/projects lists "
					"what you can see.",
				)
			],
		)

	return found


def _rendered (
	session: sqlalchemy.orm.Session, row: subroutine.db.models.project.Project
) -> subroutine.views.Project:
	"""Render one project, loading the vocabulary it names."""

	return subroutine.views.project(
		row, subroutine.views.Vocabulary.for_projects(session, [row])
	)
