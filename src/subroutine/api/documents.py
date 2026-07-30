"""Documents and the links between work items, over HTTP.

This is the endpoint set that makes the project able to hold its own planning: a
specification goes in as a document, and the work comes out of it as tasks joined by
``derives_from``. Everything else in slice 3 is machinery for this.

Links live here rather than in their own module because they are addressed as a
sub-resource of the thing they hang off — ``/v1/tasks/{id_or_ref}/links`` and
``/v1/documents/{id_or_ref}/links`` — and splitting one small concern across two files
would put the two halves of "what may I link" in different places.
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
import subroutine.api.routing
import subroutine.api.schemas
import subroutine.api.security
import subroutine.api.shaping
import subroutine.api.tasks
import subroutine.db.models.identity
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.documents
import subroutine.domain.links
import subroutine.domain.paging
import subroutine.domain.refs
import subroutine.domain.scoping
import subroutine.domain.selection
import subroutine.errors
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1/documents",
	tags=["documents"],
	route_class=subroutine.api.routing.Transactional,
)

#: Links hang off both entities, so the router carrying them is mounted twice — once under
#: tasks and once under documents — with the entity type bound at registration.
task_links = fastapi.APIRouter(
	prefix="/v1/tasks",
	tags=["links"],
	route_class=subroutine.api.routing.Transactional,
)
document_links = fastapi.APIRouter(
	prefix="/v1/documents",
	tags=["links"],
	route_class=subroutine.api.routing.Transactional,
)

SORTABLE: dict[str, subroutine.api.pagination.Sortable] = {
	"created_at": subroutine.db.models.work.Document.created_at,
	"updated_at": subroutine.db.models.work.Document.updated_at,
	"title": subroutine.db.models.work.Document.title,
	"ref": subroutine.db.models.work.Document.ref,
}

DEFAULT_ORDER = ("-created_at",)

#: What ``?fields=`` may name, read from the view so the two cannot drift (SPEC.md §14.10).
SELECTABLE = subroutine.api.shaping.selectable(subroutine.views.Document)


class Create(subroutine.api.schemas.RequestModel):
	"""What ``POST /v1/documents`` accepts."""

	title: str
	body: str | None = None
	workspace_id: str | None = None
	project: str | None = None
	parent: subroutine.api.schemas.Reference | None = None
	type: str | None = None
	status: str | None = None
	owner_id: uuid.UUID | None = None
	supersedes: subroutine.api.schemas.Reference | None = None


class Update(subroutine.api.schemas.RequestModel):
	"""What ``PATCH /v1/documents/{id_or_ref}`` accepts.

	Omitted is unchanged; null clears (§8.3). Setting ``supersedes`` moves the document it
	names to the status that says so — the two are one fact, and a superseded document still
	reading as active is one somebody will act on.
	"""

	title: str | None = None
	body: str | None = None
	status: str | None = None
	owner_id: uuid.UUID | None = None
	supersedes: subroutine.api.schemas.Reference | None = None

	#: The version this change is based on (SPEC.md §8.9).
	expected_version: int | None = None


class LinkRequest(subroutine.api.schemas.RequestModel):
	"""What ``POST /…/links`` accepts.

	``target`` is a ref or an id; ``target_type`` says which table to look in and defaults
	to a task, which is what most links point at.

	A ref is an integer in every response this API sends, so ``42`` is accepted as well as
	``"42"`` — a client should be able to send back what it was given without converting
	it. An id arrives as a string, since a UUID is not a number.
	"""

	target: subroutine.api.schemas.Reference
	link_type: str
	target_type: str = "task"


@router.post("", status_code=201, summary="Write a document")
def create (
	body: Create,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Document:
	"""Create a document — a spec, a design, a note, a decision, a finding or a dead end."""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=body.workspace_id)

	created = subroutine.domain.documents.create(
		session,
		project=subroutine.api.tasks._project(session, actor, workspace, body.project),
		title=body.title,
		body=body.body,
		type_key=body.type or "note",
		status_key=body.status,
		parent=(
			None if body.parent is None else _resolve(session, actor, workspace, str(body.parent))
		),
		owner_id=body.owner_id if body.owner_id is not None else actor.user.id,
		supersedes=(
			None
			if body.supersedes is None
			else _resolve(session, actor, workspace, str(body.supersedes))
		),
		actor=actor,
	)

	return _rendered(session, created)


@router.get(
	"",
	summary="List documents",
	dependencies=[subroutine.api.query.UnknownQueryDep],
	response_model=subroutine.views.Collection[subroutine.views.Document],
)
def listing (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
	project: str | None = fastapi.Query(None, description="Restrict to one project."),
	type: str | None = fastapi.Query(None, description="Restrict to one document type key."),
	status: str | None = fastapi.Query(None, description="Restrict to one status key."),
	q: str | None = fastapi.Query(None, description="Match this text in the title."),
	order: str | None = fastapi.Query(None, description="Comma-separated sort fields."),
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
	"""List the documents this caller can see."""

	shape = subroutine.api.shaping.wanted(
		format=format, fields=fields, available=SELECTABLE, entity="document"
	)
	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	statement = subroutine.domain.scoping.readable_documents(
		actor, workspace_ids=[workspace.id]
	)

	model = subroutine.db.models.work.Document

	if project is not None:
		statement = statement.where(
			model.project_id
			== subroutine.api.tasks._project(session, actor, workspace, project).id
		)

	if type is not None:
		statement = statement.where(
			model.type_id
			== subroutine.domain.documents.item_type_for(session, workspace.id, type).id
		)

	if status is not None:
		statement = statement.where(
			model.status_id
			== subroutine.domain.documents.status_for(session, workspace.id, status).id
		)

	if q:
		statement = statement.where(
			model.title.ilike(f"%{subroutine.api.tasks._escaped(q)}%", escape="\\")
		)

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

	vocabulary = subroutine.views.Vocabulary.for_documents(session, rows)

	return subroutine.api.shaping.response(
		[subroutine.views.document(row, vocabulary) for row in rows],
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
	"/{id_or_ref}", summary="Read one document", response_model=subroutine.views.Document
)
def read (
	id_or_ref: subroutine.api.schemas.ItemAddress,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""Return one document, by id or by ref."""

	shape = subroutine.api.shaping.wanted(
		format=format, fields=fields, available=SELECTABLE, entity="document"
	)
	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)

	return subroutine.api.shaping.single(
		_rendered(session, _resolve(session, actor, workspace, id_or_ref)), shape
	)


@router.patch("/{id_or_ref}", summary="Change a document")
def change (
	request: starlette.requests.Request,
	id_or_ref: subroutine.api.schemas.ItemAddress,
	body: Update,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Document:
	"""Change a document. Omitted fields are untouched; nulls clear (SPEC.md §8.3)."""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	document = _resolve(session, actor, workspace, id_or_ref)

	supplied = body.model_fields_set
	changes: dict[str, typing.Any] = {
		name: getattr(body, name) for name in ("title", "body", "owner_id") if name in supplied
	}

	if "status" in supplied and body.status is not None:
		changes["status_key"] = body.status

	if "supersedes" in supplied:
		changes["supersedes"] = (
			None
			if body.supersedes is None
			else _resolve(session, actor, workspace, str(body.supersedes))
		)

	with subroutine.api.concurrency.reporting(lambda: _rendered(session, document)):
		updated = subroutine.domain.documents.update(
			session,
			document,
			expected_version=subroutine.api.concurrency.expected(request, body.expected_version),
			actor=actor,
			**changes,
		)

	return _rendered(session, updated)


@router.delete("/{id_or_ref}", summary="Move a document to the trash")
def remove (
	request: starlette.requests.Request,
	id_or_ref: subroutine.api.schemas.ItemAddress,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Document:
	"""Soft-delete a document. It stays recoverable (SPEC.md §6.9)."""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	document = _resolve(session, actor, workspace, id_or_ref)

	with subroutine.api.concurrency.reporting(lambda: _rendered(session, document)):
		removed = subroutine.domain.documents.delete(
			session,
			document,
			expected_version=subroutine.api.concurrency.expected(request),
			actor=actor,
		)

	return _rendered(session, removed)


def _links_for (entity_type: str) -> typing.Any:
	"""Build the two link handlers for one entity type.

	The endpoints are identical apart from which table the *near* end lives in, so they are
	made rather than written twice — two copies of "may I link these" is the pair that comes
	to disagree.
	"""

	def listing (
		id_or_ref: subroutine.api.schemas.ItemAddress,
		actor: subroutine.api.security.PrincipalDep,
		session: subroutine.api.dependencies.SessionDep,
		workspace_id: str | None = fastapi.Query(None, description="Which workspace."),
	) -> subroutine.views.Collection[subroutine.views.Link]:
		"""Return every link touching this item, labelled from its point of view.

		Enveloped like every other collection (§8.4), and returned whole: an item's links are
		bounded by how many somebody typed, so there is nothing to page through. ``has_more``
		is therefore always false — which is a *statement* the caller can rely on, and is the
		reason this is worth an envelope rather than a bare array. Until 2026-07-30 it was a
		bare array, and a caller had no way to tell a complete set from a truncated one.
		"""

		workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
		near = _near(session, actor, workspace, entity_type, id_or_ref)

		found = [
			subroutine.views.link(related)
			for related in subroutine.domain.links.around(
				session,
				actor,
				workspace_id=workspace.id,
				entity_type=entity_type,
				identifier=near.id,
			)
		]

		return subroutine.views.Collection(
			items=found,
			page=subroutine.views.Page(limit=len(found), has_more=False, total=len(found)),
		)

	def create (
		id_or_ref: subroutine.api.schemas.ItemAddress,
		body: LinkRequest,
		actor: subroutine.api.security.PrincipalDep,
		session: subroutine.api.dependencies.SessionDep,
		workspace_id: str | None = fastapi.Query(None, description="Which workspace."),
	) -> subroutine.views.Link:
		"""Join this item to another one."""

		workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
		near = _near(session, actor, workspace, entity_type, id_or_ref)
		far = _near(session, actor, workspace, body.target_type, str(body.target))

		created = subroutine.domain.links.create(
			session,
			workspace_id=workspace.id,
			source=near,
			target=far,
			link_type_key=body.link_type,
			actor=actor,
		)

		for related in subroutine.domain.links.around(
			session, actor, workspace_id=workspace.id, entity_type=entity_type, identifier=near.id
		):
			if related.id == created.id:
				return subroutine.views.link(related)

		raise subroutine.errors.InternalError("The link was created but cannot be read back.")

	def remove (
		id_or_ref: subroutine.api.schemas.ItemAddress,
		link_id: uuid.UUID,
		actor: subroutine.api.security.PrincipalDep,
		session: subroutine.api.dependencies.SessionDep,
		workspace_id: str | None = fastapi.Query(None, description="Which workspace."),
	) -> fastapi.Response:
		"""Withdraw a link."""

		workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
		near = _near(session, actor, workspace, entity_type, id_or_ref)

		model = subroutine.db.models.work.Link
		found = session.scalars(
			sqlalchemy.select(model).where(
				model.id == link_id,
				model.workspace_id == workspace.id,
				model.deleted_at.is_(None),
				sqlalchemy.or_(
					sqlalchemy.and_(
						model.source_type == entity_type, model.source_id == near.id
					),
					sqlalchemy.and_(
						model.target_type == entity_type, model.target_id == near.id
					),
				),
			)
		).first()

		if found is None:
			raise subroutine.errors.NotFound(
				f"There is no such link on {subroutine.domain.refs.format_ref(near.ref)}.",
				hint="GET this item's /links to see the ones there are.",
			)

		subroutine.domain.links.remove(session, found, actor=actor)

		return fastapi.Response(status_code=204)

	return listing, create, remove


def _register (target: fastapi.APIRouter, entity_type: str) -> None:
	"""Mount the link endpoints for one entity type."""

	listing, create, remove = _links_for(entity_type)
	noun = "task" if entity_type == "task" else "document"

	target.add_api_route(
		"/{id_or_ref}/links",
		listing,
		methods=["GET"],
		name=f"{noun}_links",
		summary=f"List a {noun}'s links",
	)
	target.add_api_route(
		"/{id_or_ref}/links",
		create,
		methods=["POST"],
		status_code=201,
		name=f"{noun}_link_create",
		summary=f"Link a {noun} to something",
	)
	target.add_api_route(
		"/{id_or_ref}/links/{link_id}",
		remove,
		methods=["DELETE"],
		status_code=204,
		name=f"{noun}_link_delete",
		summary="Withdraw a link",
	)


_register(task_links, "task")
_register(document_links, "document")


def _near (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	workspace: subroutine.db.models.identity.Workspace,
	entity_type: str,
	id_or_ref: str,
) -> subroutine.domain.links.End:
	"""Resolve one end of a link from a ref or an id, refusing an unknown entity type."""

	if entity_type not in subroutine.domain.links.LINKABLE:
		raise subroutine.errors.ValidationError(
			f"{entity_type!r} is not something that can be linked.",
			errors=[
				subroutine.errors.FieldError(
					field="target_type",
					code="invalid_field_value",
					message=f"Unknown entity type {entity_type!r}.",
					hint=f"Linkable types are: {', '.join(subroutine.domain.links.LINKABLE)}.",
				)
			],
		)

	if entity_type == "task":
		row: typing.Any = subroutine.api.tasks._resolve(session, actor, workspace, id_or_ref)

	else:
		row = _resolve(session, actor, workspace, id_or_ref)

	return subroutine.domain.links.End(
		entity_type=entity_type,
		id=row.id,
		ref=row.ref,
		title=row.title,
		project_id=row.project_id,
	)


def _resolve (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	workspace: subroutine.db.models.identity.Workspace,
	id_or_ref: str,
) -> subroutine.db.models.work.Document:
	"""Find one document by id or ref, or report that there is no such thing."""

	model = subroutine.db.models.work.Document
	wanted = id_or_ref.strip()
	statement = subroutine.domain.scoping.readable_documents(
		actor, workspace_ids=[workspace.id], include_deleted=True, include_archived=True
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
			f"There is no document {id_or_ref!r} here.",
			errors=[
				subroutine.errors.FieldError(
					field="id_or_ref",
					code="not_found",
					message=f"No document in {workspace.slug} answers to {id_or_ref!r}.",
					hint="Use a ref like '42' or a document id. GET /v1/documents lists "
					"what you can see. Note tasks and documents share one ref space, so a "
					"ref that exists may name a task instead.",
				)
			],
		)

	return found


def _rendered (
	session: sqlalchemy.orm.Session, row: subroutine.db.models.work.Document
) -> subroutine.views.Document:
	"""Render one document, loading the vocabulary it names."""

	return subroutine.views.document(
		row, subroutine.views.Vocabulary.for_documents(session, [row])
	)
