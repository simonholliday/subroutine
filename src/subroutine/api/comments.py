"""Comments over HTTP (docs/design.md §5.10, §8.6).

Hung off the three things that take them, so the address says what is being discussed:
``/v1/tasks/{ref}/comments``, and the same for projects and documents. Editing and deleting go
through ``/v1/comments/{id}`` instead — by then the comment is the subject, and requiring the
caller to remember which task it was on would be a lookup for nothing.

**A comment is what happened; a document is what you concluded.** The API does not enforce that
— it cannot — but the guide says it and the shapes encourage it: a comment takes a body and
nothing else, where a document takes a title, a type and a project.
"""

import typing
import uuid

import fastapi
import sqlalchemy.orm
import starlette.requests

import subroutine.api.concurrency
import subroutine.api.dependencies
import subroutine.api.pagination
import subroutine.api.routing
import subroutine.api.schemas
import subroutine.api.security
import subroutine.api.shaping
import subroutine.api.subjects
import subroutine.config
import subroutine.db.models.activity
import subroutine.domain.authentication
import subroutine.domain.comments
import subroutine.domain.paging
import subroutine.views

#: One router per subject, so each sits beside the entity it extends and ``routing.check`` can
#: see that none of them shadows a literal path.
task_comments = fastapi.APIRouter(
	prefix="/v1/tasks",
	tags=["comments"],
	route_class=subroutine.api.routing.Transactional,
)
project_comments = fastapi.APIRouter(
	prefix="/v1/projects",
	tags=["comments"],
	route_class=subroutine.api.routing.Transactional,
)
document_comments = fastapi.APIRouter(
	prefix="/v1/documents",
	tags=["comments"],
	route_class=subroutine.api.routing.Transactional,
)

#: Editing and deleting address the comment itself.
router = fastapi.APIRouter(
	prefix="/v1/comments",
	tags=["comments"],
	route_class=subroutine.api.routing.Transactional,
)

SELECTABLE = subroutine.api.shaping.selectable(subroutine.views.Comment)


class Create(subroutine.api.schemas.RequestModel):
	"""What ``POST /v1/{entity}/{ref}/comments`` accepts.

	Only a body. No title, no type, no project — a comment that needed those would be a
	document, and offering them here would blur the one distinction §5.10 is about.
	"""

	body: str


class Update(subroutine.api.schemas.RequestModel):
	"""What ``PATCH /v1/comments/{id}`` accepts."""

	body: str | None = None

	#: The version this change is based on (docs/design.md §8.9).
	expected_version: int | None = None


def _rendered (comment: subroutine.db.models.activity.Comment) -> subroutine.views.Comment:
	"""Render one comment."""

	return subroutine.views.comment(comment)


def _page (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	settings: subroutine.config.Settings,
	*,
	entity_type: str,
	entity_id: uuid.UUID,
	limit: int | None,
	cursor: str | None,
	shape: typing.Any,
) -> typing.Any:
	"""Return one page of an item's comments, oldest first."""

	model = subroutine.db.models.activity.Comment
	statement = subroutine.domain.comments.listing(
		session, entity_type=entity_type, entity_id=entity_id, actor=actor
	)

	keys = subroutine.api.pagination.parse_order(
		None,
		allowed={"created_at": model.created_at},
		default=("created_at",),
		tiebreak=model.id,
	)
	size = subroutine.domain.paging.size(limit, settings)

	if cursor is not None:
		statement = statement.where(
			subroutine.api.pagination.after(
				keys,
				subroutine.api.pagination.decode(settings.require_secret_key(), keys, cursor),
			)
		)

	rows = list(session.scalars(statement.limit(size + 1)))
	has_more = len(rows) > size
	rows = rows[:size]

	return subroutine.api.shaping.response(
		[_rendered(row) for row in rows],
		subroutine.views.Page(
			limit=size,
			has_more=has_more,
			next_cursor=(
				subroutine.api.pagination.encode(settings.require_secret_key(), keys, rows[-1])
				if has_more and rows
				else None
			),
			total=None,
		),
		shape,
	)


def _attach (
	group: fastapi.APIRouter,
	*,
	entity_type: str,
	address: str,
) -> None:
	"""Register the list and create endpoints for one kind of subject.

	Declared once and applied three times, so the three subjects cannot drift into three
	slightly different comment APIs — which is exactly what happened to the *link*
	sub-resources before they were unified.

	**Every one of them takes ``workspace_id``**, like every other item-addressed endpoint.
	They did not until 2026-07-30, which meant a ref could only ever be resolved in whichever
	workspace ``selection.workspace`` settled on with nothing requested — so an installation
	with two of them could not comment on an item in the second one at all, and the listing
	*refused* the parameter outright because it was not declared. Invisible to the transport
	equivalence tests, which pass ``workspace=None``, and so send nothing.
	"""

	@group.get(
		"/{" + address + "}/comments",
		summary=f"List a {entity_type}'s comments",
		response_model=subroutine.views.Collection[subroutine.views.Comment],
		name=f"list_{entity_type}_comments",
	)
	def listing (
		actor: subroutine.api.security.PrincipalDep,
		session: subroutine.api.dependencies.SessionDep,
		settings: subroutine.api.dependencies.SettingsDep,
		request: starlette.requests.Request,
		workspace_id: str | None = fastapi.Query(
			None, description="Which workspace, by id or slug. Needed when you can reach several."
		),
		limit: int | None = fastapi.Query(None, description="How many to return."),
		cursor: str | None = fastapi.Query(None, description="Continue after a page."),
		format: str | None = subroutine.api.shaping.FORMAT_QUERY,
		fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
	) -> typing.Any:
		"""Return what happened on this item, oldest first."""

		subject = subroutine.api.subjects.resolve(
			session,
			actor,
			entity_type=entity_type,
			address=request.path_params[address],
			workspace_id=workspace_id,
		)

		return _page(
			session,
			actor,
			settings,
			entity_type=entity_type,
			entity_id=subject.id,
			limit=limit,
			cursor=cursor,
			shape=subroutine.api.shaping.wanted(
				format=format, fields=fields, available=SELECTABLE, entity="comment"
			),
		)

	@group.post(
		"/{" + address + "}/comments",
		status_code=201,
		summary=f"Comment on a {entity_type}",
		response_model=subroutine.views.Comment,
		name=f"create_{entity_type}_comment",
	)
	def create (
		body: Create,
		actor: subroutine.api.security.PrincipalDep,
		session: subroutine.api.dependencies.SessionDep,
		request: starlette.requests.Request,
		workspace_id: str | None = fastapi.Query(
			None, description="Which workspace, by id or slug. Needed when you can reach several."
		),
	) -> subroutine.views.Comment:
		"""Record what happened on this item."""

		subject = subroutine.api.subjects.resolve(
			session,
			actor,
			entity_type=entity_type,
			address=request.path_params[address],
			workspace_id=workspace_id,
		)

		return _rendered(
			subroutine.domain.comments.create(
				session,
				entity_type=entity_type,
				entity_id=subject.id,
				body=body.body,
				actor=actor,
			)
		)


@router.patch("/{comment_id}", summary="Edit your own comment")
def change (
	request: starlette.requests.Request,
	comment_id: uuid.UUID,
	body: Update,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Comment:
	"""Edit a comment's text. Only its author may (docs/design.md §5.10)."""

	found = subroutine.domain.comments.get(session, comment_id, actor=actor)
	supplied = body.model_fields_set

	with subroutine.api.concurrency.reporting(lambda: _rendered(found)):
		updated = subroutine.domain.comments.update(
			session,
			found,
			expected_version=subroutine.api.concurrency.expected(
				request, body.expected_version
			),
			actor=actor,
			**({"body": body.body} if "body" in supplied and body.body is not None else {}),
		)

	return _rendered(updated)


@router.delete("/{comment_id}", summary="Delete a comment")
def remove (
	request: starlette.requests.Request,
	comment_id: uuid.UUID,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Comment:
	"""Move a comment to the trash. Its author may; so may a workspace administrator."""

	found = subroutine.domain.comments.get(session, comment_id, actor=actor)

	with subroutine.api.concurrency.reporting(lambda: _rendered(found)):
		return _rendered(
			subroutine.domain.comments.delete(
				session,
				found,
				expected_version=subroutine.api.concurrency.expected(request, None),
				actor=actor,
			)
		)


# One resolver, shared with the history endpoints, because both sub-resources ask the
# same question first: which item is this, and may this caller see it?
for _group, _entity in (
	(task_comments, "task"),
	(project_comments, "project"),
	(document_comments, "document"),
):
	_attach(
		_group,
		entity_type=_entity,
		address=subroutine.api.subjects.ADDRESS[_entity],
	)
