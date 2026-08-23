"""Workspaces over HTTP (docs/design.md §8.6).

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
import subroutine.api.routing
import subroutine.api.schemas
import subroutine.api.security
import subroutine.api.shaping
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.paging
import subroutine.domain.projects
import subroutine.domain.selection
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1/workspaces",
	tags=["workspaces"],
	route_class=subroutine.api.routing.Transactional,
)

#: What ``?order=`` accepts. ``slug`` is the one people think in.
SORTABLE: dict[str, subroutine.api.pagination.Sortable] = {
	"created_at": subroutine.db.models.identity.Workspace.created_at,
	"updated_at": subroutine.db.models.identity.Workspace.updated_at,
	"slug": subroutine.db.models.identity.Workspace.slug,
	"title": subroutine.db.models.identity.Workspace.title,
}

#: By short name, because a workspace list is a menu of addresses rather than a feed.
DEFAULT_ORDER = ("slug",)

#: What ``?fields=`` may name, read from the view so the two cannot drift (docs/design.md §14.10).
SELECTABLE = subroutine.api.shaping.selectable(subroutine.views.Workspace)

#: The same, for the membership sub-resource below.
MEMBER_FIELDS = subroutine.api.shaping.selectable(subroutine.views.Member)


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

	``slug`` **may be changed** as of `#295`. It was absent on the grounds that it lives "in
	other people's notes, in shell history and in ``config.toml`` on other machines" — and the
	last of those is not true: no connection and no setting names a workspace. What is left is
	the same exposure a project key has, which `#176` decided is acceptable when the caller is
	told what stops working first.

	Validated exactly as creation validates one, so a rename cannot arrive at a short name
	nobody could have chosen.
	"""

	slug: str | None = None
	title: str | None = None
	description: str | None = None
	timezone: str | None = None

	#: What this workspace is configured with, merged **per key** into whatever is there
	#: (`#1025`). Every project in it inherits these unless it sets its own — see
	#: `domain/settings.py`, which holds the registry and the merge rule.
	settings: dict[str, typing.Any] | None = None

	#: The one project whose work rises in this workspace's ranked listings (decision ``#982``),
	#: named the way a caller addresses one — its key or its whole path. Null clears it.
	#:
	#: **On the workspace rather than on the project, deliberately.** There is one such fact and
	#: it belongs here, so choosing a second project unsets the first in the same write with no
	#: clearing logic. A route on the project would read as a per-project flag, which is the
	#: mental model that lets a boost accumulate on four projects until the ordering means
	#: nothing — the failure decision ``#982`` is built to refuse.
	prioritised_project: str | None = None

	#: The version this change is based on (docs/design.md §8.9).
	expected_version: int | None = None


def rendered (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	rows: typing.Sequence[subroutine.db.models.identity.Workspace],
) -> list[subroutine.views.Workspace]:
	"""Render workspaces, resolving what each has prioritised in one query (`#986`).

	**A helper rather than four call sites**, because ``views.workspace`` requires the answer
	rather than defaulting it: a renderer that quietly reported *nothing prioritised* when the
	caller forgot to look is the inert-control shape this codebase keeps finding, and a required
	argument is what makes forgetting impossible instead of unlikely.
	"""

	focused = subroutine.domain.projects.prioritised_addresses(
		session, actor, workspace_ids=[row.id for row in rows]
	)

	return [
		subroutine.views.workspace(row, prioritised=focused.get(row.id)) for row in rows
	]


def _focus (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	row: subroutine.db.models.identity.Workspace,
) -> str | None:
	"""Return which project this one workspace has prioritised, or ``None``."""

	return subroutine.domain.projects.prioritised_addresses(
		session, actor, workspace_ids=[row.id]
	).get(row.id)


def one (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	row: subroutine.db.models.identity.Workspace,
) -> subroutine.views.Workspace:
	"""Render one workspace, the same way a listing renders each of them."""

	return rendered(session, actor, [row])[0]


def resolve (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	id_or_slug: str,
) -> subroutine.db.models.identity.Workspace:
	"""Find one workspace this caller can reach, by id or short name.

	Searched among the ones they can *read*, so a workspace they are not a member of is
	reported as absent rather than forbidden — saying "forbidden" would confirm it exists
	(§7.3a). A token pinned to one workspace therefore cannot see past its pin here either.

	**Never the trash**, which :func:`unremove` reaches through ``workspaces.for_restore``
	instead. A slug frees when a workspace is deleted, so it can name a deleted workspace and
	a live one at once and this function would have to guess between them.
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

	return one(session, actor, created)


@router.get(
	"",
	summary="List workspaces",
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
		format=format,
		fields=fields,
		available=SELECTABLE,
		entity="workspace",
		timezone=subroutine.views.reader_zone(session, actor),
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
		rendered(session, actor, rows),
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
		format=format,
		fields=fields,
		available=SELECTABLE,
		entity="workspace",
		timezone=subroutine.views.reader_zone(session, actor),
	)

	return subroutine.api.shaping.single(
		one(session, actor, resolve(session, actor, id_or_slug)), shape
	)


@router.patch("/{id_or_slug}", summary="Change a workspace")
def change (
	request: starlette.requests.Request,
	id_or_slug: str,
	body: Update,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Workspace:
	"""Change a workspace. Omitted fields are untouched; nulls clear (docs/design.md §8.3)."""

	found = resolve(session, actor, id_or_slug)
	supplied = body.model_fields_set
	changes: dict[str, typing.Any] = {
		name: getattr(body, name)
		for name in ("slug", "title", "description", "timezone")
		if name in supplied
	}

	# **Resolved here rather than in the domain, so an unknown project is refused by name.**
	# `selection.project` searches the projects this caller can *read* inside this workspace, so
	# one in another workspace is "no such project" rather than forbidden (§7.3a) — and the
	# cross-workspace check in `workspaces.update` is defence behind that rather than the only
	# guard. Read from `model_fields_set` because null is a value here: it clears the priority,
	# where an absent key means *leave it alone* (§8.3).
	# Null means *leave it alone* rather than *clear them all*, for `api/projects.py`'s reason.
	if "settings" in supplied and body.settings is not None:
		changes["settings"] = body.settings

	if "prioritised_project" in supplied:
		changes["prioritised_project"] = (
			None
			if body.prioritised_project is None
			else subroutine.domain.selection.project(
				session, actor, found, body.prioritised_project
			)
		)

	with subroutine.api.concurrency.reporting(
		lambda: one(session, actor, found)
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

	return one(session, actor, updated)


@router.post("/{id_or_slug}/restore", summary="Take a workspace out of the trash")
def unremove (
	request: starlette.requests.Request,
	id_or_slug: str,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Workspace:
	"""Restore a soft-deleted workspace, and everything in it with it.

	``POST`` rather than ``DELETE ?restore=``, matching a project's restore, because it is not
	a deletion of anything. The short name may have been taken while this was in the trash —
	the unique index ignores deleted rows — and that is refused by name with the rename that
	clears it, rather than surfacing as a constraint violation.
	"""

	found = subroutine.domain.workspaces.for_restore(session, actor, id_or_slug)

	with subroutine.api.concurrency.reporting(lambda: one(session, actor, found)):
		back = subroutine.domain.workspaces.restore(
			session,
			found,
			expected_version=subroutine.api.concurrency.expected(request),
			actor=actor,
		)

	return one(session, actor, back)


@router.delete("/{id_or_slug}", summary="Move a workspace to the trash")
def remove (
	request: starlette.requests.Request,
	id_or_slug: str,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Workspace:
	"""Soft-delete a workspace. Everything in it leaves the visible world, and returns with it.

	Needs ``workspace:delete``, which is the *only* verb separating the `owner` and `admin`
	roles — so until this route existed the two were the same role with two descriptions.

	The workspace itself is returned rather than a 204, so a caller can see ``deleted_at`` and
	knows what to hand back to the restore. The last live workspace is refused: an installation
	with none cannot file a task and reports itself as interrupted part-way through setup.
	"""

	found = resolve(session, actor, id_or_slug)

	with subroutine.api.concurrency.reporting(lambda: one(session, actor, found)):
		removed = subroutine.domain.workspaces.delete(
			session,
			found,
			expected_version=subroutine.api.concurrency.expected(request),
			actor=actor,
		)

	return one(session, actor, removed)


# --------------------------------------------------------------------------------------
# Membership — docs/design.md §7.3a, item `#174`
# --------------------------------------------------------------------------------------


class Join(subroutine.api.schemas.RequestModel):
	"""What ``POST /v1/workspaces/{id_or_slug}/members`` accepts."""

	#: By username rather than by id, because the caller has just read a directory of names and
	#: a UUID in a request body is something to go and look up first.
	username: str

	#: Which of the workspace's seeded roles they get. Named rather than defaulted: what
	#: somebody may do in a workspace is exactly the decision being taken here, and a default
	#: would be this function choosing it on the operator's behalf and not saying so.
	role: str


@router.get(
	"/{id_or_slug}/members",
	summary="Who belongs to this workspace",
	response_model=subroutine.views.Collection[subroutine.views.Member],
)
def members (
	id_or_slug: str,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""List this workspace's members and their roles.

	Needs ``workspace:read``: knowing who you are working alongside is part of working
	somewhere, and it is the question anybody about to add or remove somebody asks first.

	Enveloped and unpaginated, like a task's links (§8.4) and for the same reason — a
	workspace's membership is bounded by how many people somebody put in it.
	"""

	shape = subroutine.api.shaping.wanted(
		format=format,
		fields=fields,
		available=MEMBER_FIELDS,
		entity="member",
		timezone=subroutine.views.reader_zone(session, actor),
	)
	found = resolve(session, actor, id_or_slug)
	rows = subroutine.domain.workspaces.members(session, found, actor=actor)
	focus = _focus(session, actor, found)

	return subroutine.api.shaping.response(
		[
			subroutine.views.member(
				row, account=account, role=role, within=found, prioritised=focus
			)
			for row, account, role in rows
		],
		subroutine.views.Page(limit=len(rows), has_more=False, next_cursor=None, total=None),
		shape,
	)


@router.post(
	"/{id_or_slug}/members", status_code=201, summary="Add somebody to this workspace"
)
def join (
	id_or_slug: str,
	body: Join,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Member:
	"""Give somebody a role in this workspace.

	Needs ``workspace:admin`` rather than ``workspace:write``: deciding who belongs somewhere is
	not the same act as doing work there, and a member who can add members can grant themselves
	anything the roles allow. That check did not exist at all at first — the service took an
	actor, attributed the event to it, and never asked it anything — and it was found on the
	morning this endpoint was written.
	"""

	found = resolve(session, actor, id_or_slug)
	account = subroutine.domain.users.by_username(session, body.username)
	membership = subroutine.domain.workspaces.add_member(
		session, found, account, role_key=body.role, actor=actor
	)
	role = subroutine.domain.workspaces.find_role(session, found.id, body.role)

	return subroutine.views.member(
		membership,
		account=account,
		role=role,
		within=found,
		prioritised=_focus(session, actor, found),
	)


@router.delete(
	"/{id_or_slug}/members/{username}",
	status_code=204,
	summary="Take somebody out of this workspace",
)
def leave (
	id_or_slug: str,
	username: str,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> None:
	"""Remove somebody's membership of this workspace.

	**Here rather than later**, for the reason that holds of anything that can be added:
	somebody joined by mistake can see private projects they should not, and a membership that
	can only be granted is one whose mistakes are permanent.

	The last account able to administer the workspace cannot be removed — a workspace nobody can
	administer has thrown away the remedy for every later mistake, including that one.
	"""

	found = resolve(session, actor, id_or_slug)
	account = subroutine.domain.users.by_username(session, username)

	subroutine.domain.workspaces.remove_member(session, found, account, actor=actor)
