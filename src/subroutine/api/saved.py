"""Saved views over HTTP — item `#1402`, docs/design.md §18.

``/v1/views``, addressed by the name somebody gave it rather than by an id: a view exists to be
typed, sent and clicked, and an address nobody can say is one nobody will use. That is why this
is the one entity here whose ``{key}`` is a word rather than a ref or a UUID.

**A literal under ``/v1`` sharing a prefix with nothing**, so nothing can shadow it and it can
shadow nothing — ``api/routing.check`` is what says so rather than this sentence.

**Not ``/v1/saved-views``.** The product word is *view*; the module is called ``saved`` only
because :mod:`subroutine.views` already means the response models, and a URL should not carry a
Python package's naming problem.
"""

import typing
import uuid

import fastapi
import sqlalchemy
import sqlalchemy.orm

import subroutine.api.concurrency
import subroutine.api.dependencies
import subroutine.api.routing
import subroutine.api.schemas
import subroutine.api.security
import subroutine.db.models.identity
import subroutine.db.models.saved
import subroutine.domain.authentication
import subroutine.domain.saved
import subroutine.domain.selection
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1/views",
	tags=["views"],
	route_class=subroutine.api.routing.Transactional,
)

WORKSPACE = fastapi.Query(
	None, description="Which workspace, by id or short name. Needed when you can reach several."
)


class CreateView(subroutine.api.schemas.RequestModel):
	"""What ``POST /v1/views`` accepts."""

	#: What to call it. The address is derived from this, so a caller sends one name and not
	#: two — and ``key`` is deliberately not accepted, because a name and an address that can
	#: disagree are two facts about one thing.
	title: str

	#: How it is drawn: agenda, list or board.
	arrangement: str

	#: The whole of what is narrowed, in the search grammar. Omit it for a view of everything.
	q: str | None = None

	#: How the rows are sorted, e.g. '-created_at'. A leading '-' reverses.
	order: str | None = None

	#: Which axis a board draws columns on, e.g. 'status_category'.
	group_by: str | None = None

	#: Whether everybody in the workspace can see it. False unless said, which is the decision:
	#: a view is mine by default and shared on purpose.
	shared: bool = False


class UpdateView(subroutine.api.schemas.RequestModel):
	"""What ``PATCH /v1/views/{key}`` accepts.

	**Every field here is read from whether it was sent, never from its value**, because
	``null`` is a value on three of them: clearing a view's grouping and not mentioning it are
	different instructions, and a body that could not tell them apart would make *ungroup this*
	unaskable. So omitting a field leaves it alone and sending ``null`` clears it.
	"""

	title: str | None = None
	arrangement: str | None = None
	q: str | None = None
	order: str | None = None
	group_by: str | None = None
	shared: bool | None = None
	expected_version: int | None = None


def _chosen (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	requested: str | None,
) -> subroutine.db.models.identity.Workspace:
	"""Return the workspace this request is about, refusing with the alternatives named."""

	return subroutine.domain.selection.workspace(session, actor, requested=requested)


def _named (
	session: sqlalchemy.orm.Session, owners: typing.Iterable[uuid.UUID]
) -> dict[uuid.UUID, str]:
	"""Return the username of each of these accounts, in one query.

	One query for the whole page rather than one per row, which is `#39`'s N+1 and is the door
	a renderer reaching for ``row.owner`` would walk straight through.
	"""

	wanted = set(owners)

	if not wanted:
		return {}

	model = subroutine.db.models.identity.User

	return dict(
		session.execute(
			sqlalchemy.select(model.id, model.username).where(model.id.in_(wanted))
		).tuples().all()
	)


def _rendered (
	session: sqlalchemy.orm.Session, row: subroutine.db.models.saved.SavedView
) -> subroutine.views.SavedView:
	"""Render one saved view, with its owner's name resolved.

	**The chokepoint for a single view**, exactly as ``api/tasks._rendered`` is for a task:
	every single-item response passes here and no listing does, so a field added to one read
	cannot be missing from another.
	"""

	return subroutine.views.saved_view_seen(
		row, owner=_named(session, [row.owner_id]).get(row.owner_id)
	)


@router.get("", summary="The views you can see")
def list_views (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = WORKSPACE,
) -> subroutine.views.Collection[subroutine.views.SavedView]:
	"""Return the views you saved, and the ones shared with this workspace.

	**Enveloped with ``has_more`` always false**, which the link listing settled and which is a
	statement rather than a shrug here for the same reason: a workspace's saved views are
	bounded by how many people took the trouble to write one.

	Ordered by name, because a list somebody reads to pick from is read alphabetically and
	*most recently saved* is a question nobody puts to their own furniture.
	"""

	workspace = _chosen(session, actor, workspace_id)
	rows = list(
		session.scalars(
			subroutine.domain.saved.readable(session, actor, workspace_id=workspace.id).order_by(
				subroutine.db.models.saved.SavedView.key
			)
		)
	)
	owners = _named(session, [row.owner_id for row in rows])

	return subroutine.views.Collection[subroutine.views.SavedView](
		items=[
			subroutine.views.saved_view_seen(row, owner=owners.get(row.owner_id))
			for row in rows
		],
		page=subroutine.views.Page(limit=None, has_more=False, total=len(rows)),
	)


@router.post("", status_code=201, summary="Save a view")
def create_view (
	body: CreateView,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = WORKSPACE,
) -> subroutine.views.SavedView:
	"""Save a query and an arrangement under a name, so nobody has to retype it."""

	workspace = _chosen(session, actor, workspace_id)

	return _rendered(
		session,
		subroutine.domain.saved.create(
			session,
			workspace_id=workspace.id,
			title=body.title,
			arrangement=body.arrangement,
			q=body.q,
			order=body.order,
			group_by=body.group_by,
			shared=body.shared,
			actor=actor,
		),
	)


@router.get("/{key}", summary="One saved view")
def read_view (
	key: str,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = WORKSPACE,
) -> subroutine.views.SavedView:
	"""Return one view by name, whether it is yours or shared with this workspace."""

	workspace = _chosen(session, actor, workspace_id)

	return _rendered(
		session,
		subroutine.domain.saved.by_key(session, actor, workspace_id=workspace.id, key=key),
	)


@router.patch("/{key}", summary="Change a saved view")
def update_view (
	key: str,
	body: UpdateView,
	request: fastapi.Request,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = WORKSPACE,
) -> subroutine.views.SavedView:
	"""Change a view you saved. Renaming it changes the address people have.

	**Only the person who saved a view may change it**, however widely it is shared. Sharing
	publishes a view; it does not hand it over. A shared view is somebody's statement of how
	the team's queue is read, and a second person quietly changing what everybody's saved link
	draws would be a rewrite under its author's name. Copy it under your own name instead.
	"""

	workspace = _chosen(session, actor, workspace_id)
	row = subroutine.domain.saved.by_key(session, actor, workspace_id=workspace.id, key=key)
	supplied = body.model_fields_set

	with subroutine.api.concurrency.reporting(lambda: _rendered(session, row)):
		changed = subroutine.domain.saved.update(
			session,
			row,
			title=body.title,
			arrangement=body.arrangement,
			q=body.q,
			order=body.order,
			group_by=body.group_by,
			shared=body.shared,
			expected_version=subroutine.api.concurrency.expected(request, body.expected_version),
			actor=actor,
			given=supplied,
		)

	return _rendered(session, changed)


@router.delete("/{key}", status_code=204, summary="Forget a saved view")
def delete_view (
	key: str,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = WORKSPACE,
) -> None:
	"""Remove a view you saved.

	**Gone rather than trashed**, unlike a task: a view holds no record of anything that
	happened, and a deleted one left in the table would go on holding its name against the next
	person who wants it.
	"""

	workspace = _chosen(session, actor, workspace_id)

	subroutine.domain.saved.delete(
		session,
		subroutine.domain.saved.by_key(session, actor, workspace_id=workspace.id, key=key),
		actor=actor,
	)
