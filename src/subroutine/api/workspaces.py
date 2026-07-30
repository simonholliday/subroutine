"""Workspaces over HTTP (SPEC.md §8.6).

A workspace is invisible to somebody using Subroutine alone — ``init`` makes one and never
mentions it again (§1.4) — so these endpoints are for the case where there is a *second* one:
a personal list and a project's backlog on the same instance, or a work instance shared with
colleagues.

**This was the blocking half of that.** ``GET /v1/agenda?workspace_id=`` narrows an agenda, and
until now nothing could create a second workspace to narrow to, so the filter was a parameter
with one legal value. Found by using the product on its own plan.

Addressed by ``{id_or_slug}``, because the short name is what people have in front of them and
what §13.7 puts in the middle of every cross-instance address.
"""

import typing

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
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.paging
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1/workspaces",
	tags=["workspaces"],
	route_class=subroutine.api.routing.Transactional,
)

#: What ``?order=`` accepts. ``slug`` is the one people think in.
SORTABLE: dict[str, typing.Any] = {
	"created_at": subroutine.db.models.identity.Workspace.created_at,
	"updated_at": subroutine.db.models.identity.Workspace.updated_at,
	"slug": subroutine.db.models.identity.Workspace.slug,
	"title": subroutine.db.models.identity.Workspace.title,
}

#: By short name, because a workspace list is a menu of addresses rather than a feed.
DEFAULT_ORDER = ("slug",)

#: What ``?fields=`` may name, read from the view so the two cannot drift (SPEC.md §14.10).
SELECTABLE = subroutine.api.shaping.selectable(subroutine.views.Workspace)


class Create(subroutine.api.schemas.RequestModel):
	"""What ``POST /v1/workspaces`` accepts."""

	slug: str
	title: str
	description: str | None = None

	#: Unset means "not stated", so the instance's own zone shows through (§12.3). It is not
	#: defaulted to UTC here, because a default at this level shadows the instance and leaves a
	#: step in the chain nothing can reach — which is what migration ``233f898a2bee`` undid.
	timezone: str | None = None

	settings: dict[str, typing.Any] | None = None


class Update(subroutine.api.schemas.RequestModel):
	"""What ``PATCH /v1/workspaces/{id_or_slug}`` accepts.

	``slug`` is absent on purpose. It is the middle segment of every address this workspace's
	items are written as — ``work/acme/#42`` — and those strings live in other people's notes,
	in shell history and in ``config.toml`` on other machines. Renaming it here would not
	rewrite them, for the same reason a project key cannot be renamed (§5.2).
	"""

	title: str | None = None
	description: str | None = None
	timezone: str | None = None

	#: The version this change is based on (SPEC.md §8.9).
	expected_version: int | None = None


def resolve (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	id_or_slug: str,
) -> subroutine.db.models.identity.Workspace:
	"""Find one workspace this caller can reach, by id or short name.

	Searched among the ones they can *read*, so a workspace they are not a member of is
	reported as absent rather than forbidden — saying "forbidden" would confirm it exists
	(§7.3a). A token pinned to one workspace therefore cannot see past its pin here either.
	"""

	wanted = id_or_slug.strip()
	reachable = subroutine.domain.workspaces.readable(session, actor)

	for found in reachable:
		if found.slug == wanted or str(found.id) == wanted:
			return found

	raise subroutine.errors.NotFound(
		f"There is no workspace {wanted!r} that you can reach.",
		hint="GET /v1/workspaces lists the ones you can.",
	)


@router.post("", status_code=201, summary="Create a workspace")
def create (
	body: Create,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Workspace:
	"""Create a workspace, stocked with its vocabulary, owned by you.

	Needs ``instance:workspace_create``, which is an *instance*-tier verb: it happens outside
	every workspace, so no role can carry it and only a superuser holds it (§7.1). A token still
	narrows it.
	"""

	created = subroutine.domain.workspaces.create(
		session,
		slug=body.slug,
		title=body.title,
		# The creator owns what they create, which is also what makes them able to administer
		# it — a workspace with no owner is not a state worth being able to reach.
		owner=actor.user,
		timezone=body.timezone or "UTC",
		settings=body.settings,
		actor=actor,
	)

	if body.description is not None:
		created.description = body.description
		session.flush()

	return subroutine.views.workspace(created)


@router.get(
	"",
	summary="List workspaces",
	dependencies=[subroutine.api.query.UnknownQueryDep],
	response_model=subroutine.views.Collection[subroutine.views.Workspace],
)
def listing (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	order: str | None = fastapi.Query(
		None, description="Comma-separated sort fields, '-' reverses."
	),
	limit: int | None = fastapi.Query(
		None,
		# No `ge=1`: `domain.paging.size` is the one arbiter, so this and the local client
		# refuse an impossible page identically, naming `limit` rather than `query.limit`.
		description="How many to return. At least 1; capped at the instance's max_page_size.",
	),
	cursor: str | None = fastapi.Query(None, description="Continue after a previous page."),
	include_total: bool = fastapi.Query(False, description="Count the whole result."),
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""List the workspaces this caller can reach."""

	shape = subroutine.api.shaping.wanted(
		format=format, fields=fields, available=SELECTABLE, entity="workspace"
	)

	model = subroutine.db.models.identity.Workspace

	# Narrowed by membership and by a token's pin, through the one helper that owns that rule —
	# `readable` is what `/v1/me` and the agenda both go through, so a workspace hidden from one
	# cannot be visible in another.
	reachable = [found.id for found in subroutine.domain.workspaces.readable(session, actor)]
	statement = sqlalchemy.select(model).where(
		model.id.in_(reachable), model.deleted_at.is_(None)
	)

	keys = subroutine.api.pagination.parse_order(
		order, allowed=SORTABLE, default=DEFAULT_ORDER, tiebreak=model.id
	)
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

	return subroutine.api.shaping.response(
		[subroutine.views.workspace(row) for row in rows],
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
	"/{id_or_slug}",
	summary="Read one workspace",
	response_model=subroutine.views.Workspace,
)
def read (
	id_or_slug: str,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""Return one workspace, by id or by short name."""

	shape = subroutine.api.shaping.wanted(
		format=format, fields=fields, available=SELECTABLE, entity="workspace"
	)

	return subroutine.api.shaping.single(
		subroutine.views.workspace(resolve(session, actor, id_or_slug)), shape
	)


@router.patch("/{id_or_slug}", summary="Change a workspace")
def change (
	request: starlette.requests.Request,
	id_or_slug: str,
	body: Update,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Workspace:
	"""Change a workspace. Omitted fields are untouched; nulls clear (SPEC.md §8.3)."""

	found = resolve(session, actor, id_or_slug)
	supplied = body.model_fields_set
	changes: dict[str, typing.Any] = {
		name: getattr(body, name)
		for name in ("title", "description", "timezone")
		if name in supplied
	}

	with subroutine.api.concurrency.reporting(
		lambda: subroutine.views.workspace(found)
	):
		updated = subroutine.domain.workspaces.update(
			session,
			found,
			expected_version=subroutine.api.concurrency.expected(
				request, body.expected_version
			),
			actor=actor,
			**changes,
		)

	return subroutine.views.workspace(updated)
