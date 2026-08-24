"""Curating the words a workspace uses — `#826`, docs/design.md §5.5.

§5.5's endpoint table named these and nothing implemented them, so a workspace's vocabulary was
whatever ``db.seed`` wrote at ``init`` and stayed that way for ever. The visible cost was three
published permissions — ``status:write``, ``tag:write``, ``link_type:write`` — that gated
nothing at all, which is a claim about what a credential cannot do made to every reader of
``/v1/me``.

**Item types are deliberately absent**, and §5.5's table does not list them either. `#906` left
`#826` a question nobody has answered — *what are the fixed categories of an item type* — and a
type no client can branch on is worse than no type. `#524` waits on the same answer.

**Two routes here are not in §5.5's table**: ``PATCH`` and ``DELETE`` on a link type. Built
anyway, because a create with no matching remove is `#704`'s shape — a row somebody can add and
never be rid of — and the asymmetry is cheaper to avoid than to discover.
"""

import typing
import uuid

import fastapi
import sqlalchemy
import sqlalchemy.orm

import subroutine.api.dependencies
import subroutine.api.routing
import subroutine.api.schemas
import subroutine.api.security
import subroutine.db.models.identity
import subroutine.db.models.vocabulary
import subroutine.domain.authentication
import subroutine.domain.selection
import subroutine.domain.vocabulary
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1",
	tags=["vocabulary"],
	route_class=subroutine.api.routing.Transactional,
)

WORKSPACE = fastapi.Query(
	None, description="Which workspace, by id or short name. Needed when you can reach several."
)


class CreateStatus(subroutine.api.schemas.RequestModel):
	"""What ``POST /v1/statuses`` accepts."""

	#: What it applies to — a task, a project or a document.
	entity_type: str

	#: What a caller sends back. Yours to choose and yours to rename later.
	key: str

	#: What a person reads.
	label: str

	#: **The fixed meaning, and it cannot be changed afterwards.** A task's categories and a
	#: document's are different sets, because a superseded specification is not "done".
	category: str

	is_default: bool = False

	#: Where it sorts. Left out, it goes after everything seeded.
	position: int | None = None


class UpdateStatus(subroutine.api.schemas.RequestModel):
	"""What ``PATCH /v1/statuses/{id}`` accepts.

	**No ``category``**: it is what every client branches on, so moving a status between
	categories would change the meaning of every item already in it rather than its wording.
	"""

	key: str | None = None
	label: str | None = None
	is_default: bool | None = None
	position: int | None = None


class CreateLinkType(subroutine.api.schemas.RequestModel):
	"""What ``POST /v1/link-types`` accepts."""

	key: str
	title: str
	inverse_title: str

	#: What every rule about this relation reads — decision `#1157`. Required, because a workspace
	#: adding a relation is saying what it *means*, and a default would pick that for them: the
	#: migration's fallback is `describing` and choosing it silently for a new `precedes` is the
	#: one thing `#1154` is about.
	category: str

	#: Whether it reads the same from both ends. Set once, for the reason a status category is:
	#: it decides how every edge already stored reads, not how it is worded.
	is_symmetric: bool = False


class UpdateLinkType(subroutine.api.schemas.RequestModel):
	"""What ``PATCH /v1/link-types/{id}`` accepts."""

	key: str | None = None
	title: str | None = None
	inverse_title: str | None = None

	#: Changeable, unlike a status category, and the asymmetry is deliberate. A status category
	#: decides how every row already stored reads; a link category decides what the *program*
	#: concludes from an edge, and getting that wrong is exactly the state `#1157`'s migration
	#: leaves a workspace's own relation in — `describing`, until somebody says otherwise.
	category: str | None = None


class CreateTag(subroutine.api.schemas.RequestModel):
	"""What ``POST /v1/tags`` accepts.

	A tag is still made by being *used* (§5.8) — this is the other door, for declaring one in
	advance and saying what it means here.
	"""

	name: str
	description: str | None = None


class UpdateTag(subroutine.api.schemas.RequestModel):
	"""What ``PATCH /v1/tags/{id}`` accepts."""

	name: str | None = None
	description: str | None = None


def _asked (body: typing.Any, names: tuple[str, ...], *, clearable: tuple[str, ...] = ()) -> dict[str, typing.Any]:
	"""Return what the caller actually sent, refusing a `null` that would clear the unclearable.

	**Told apart by ``model_fields_set``, never by comparing against a default** (§8.3) — that
	is what separates *left alone* from *sent as null*.

	The third case is the one worth handling rather than ignoring: a status with no key is not
	a status, so ``{"key": null}`` cannot mean anything. Dropping it silently — which is what a
	plain ``is not None`` test does, and what this codebase does elsewhere for fields that were
	never nullable in a request — tells the caller their change was applied.
	"""

	changes: dict[str, typing.Any] = {}

	for name in names:
		if name not in body.model_fields_set:
			continue

		value = getattr(body, name)

		if value is None and name not in clearable:
			raise subroutine.errors.ValidationError(
				f"{name!r} cannot be cleared.",
				errors=[
					subroutine.errors.FieldError(
						field=name,
						code="invalid_field_value",
						message="Leave it out to keep what is there, or send a new value.",
					)
				],
			)

		changes[name] = value

	return changes


def _chosen (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	requested: str | None,
) -> subroutine.db.models.identity.Workspace:
	"""Return the workspace this request is about, refusing with the alternatives named."""

	return subroutine.domain.selection.workspace(session, actor, requested=requested)


def _status (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	which: uuid.UUID,
) -> subroutine.db.models.vocabulary.Status:
	"""Return one status the caller can reach, or a 404 that says nothing more."""

	found = session.get(subroutine.db.models.vocabulary.Status, which)

	if found is None or found.workspace_id not in _reachable(session, actor):
		raise subroutine.errors.NotFound("There is no status with that id.")

	return found


def _link_type (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	which: uuid.UUID,
) -> subroutine.db.models.vocabulary.LinkType:
	"""Return one link type the caller can reach."""

	found = session.get(subroutine.db.models.vocabulary.LinkType, which)

	if found is None or found.workspace_id not in _reachable(session, actor):
		raise subroutine.errors.NotFound("There is no link type with that id.")

	return found


def _tag (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	which: uuid.UUID,
) -> subroutine.db.models.vocabulary.Tag:
	"""Return one tag the caller can reach."""

	found = session.get(subroutine.db.models.vocabulary.Tag, which)

	if found is None or found.workspace_id not in _reachable(session, actor):
		raise subroutine.errors.NotFound("There is no tag with that id.")

	return found


def _reachable (
	session: sqlalchemy.orm.Session, actor: subroutine.domain.authentication.Principal
) -> set[uuid.UUID]:
	"""Return the workspaces this credential can see at all.

	**A 404 rather than a 403 for a row in a workspace the caller cannot reach**, which is the
	same choice §7.3a makes about a private project: saying "forbidden" would confirm the id
	names something.
	"""

	return {
		workspace.id for workspace in subroutine.domain.workspaces.readable(session, actor)
	}


@router.get("/statuses", summary="The statuses this workspace has")
def list_statuses (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = WORKSPACE,
	entity_type: str | None = fastapi.Query(
		None, description="Narrow to what they apply to: task, project or document."
	),
) -> subroutine.views.Collection[subroutine.views.Status]:
	"""Return this workspace's statuses, in the order a client should show them.

	**Enveloped like every other listing**, with ``has_more`` always false — §5.7's link
	listing settled that a bare array is the one shape a caller cannot tell complete from
	truncated, and *always false* here is a statement rather than a shrug: a workspace's
	vocabulary is bounded by how many somebody wrote.
	"""

	workspace = _chosen(session, actor, workspace_id)
	rows = subroutine.domain.vocabulary.statuses(
		session, workspace_id=workspace.id, entity_type=entity_type
	)

	return subroutine.views.Collection[subroutine.views.Status](
		items=[subroutine.views.status(row) for row in rows],
		page=subroutine.views.Page(limit=len(rows), has_more=False, total=len(rows)),
	)


@router.post("/statuses", status_code=201, summary="Add a status")
def create_status (
	body: CreateStatus,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = WORKSPACE,
) -> subroutine.views.Status:
	"""Add a status to this workspace's vocabulary."""

	workspace = _chosen(session, actor, workspace_id)

	return subroutine.views.status(
		subroutine.domain.vocabulary.create_status(
			session,
			workspace_id=workspace.id,
			entity_type=body.entity_type,
			key=body.key,
			label=body.label,
			category=body.category,
			is_default=body.is_default,
			position=body.position,
			actor=actor,
		)
	)


@router.patch("/statuses/{which}", summary="Rename or reposition a status")
def update_status (
	which: uuid.UUID,
	body: UpdateStatus,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Status:
	"""Change a status without changing what it means."""

	changes = _asked(body, ("key", "label", "is_default", "position"))

	return subroutine.views.status(
		subroutine.domain.vocabulary.update_status(
			session, _status(session, actor, which), actor=actor, **changes
		)
	)


@router.delete("/statuses/{which}", status_code=204, summary="Remove a status")
def delete_status (
	which: uuid.UUID,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> None:
	"""Remove a status nothing is in, and that is not the default."""

	subroutine.domain.vocabulary.delete_status(
		session, _status(session, actor, which), actor=actor
	)


@router.get("/link-types", summary="The ways two items can relate here")
def list_link_types (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = WORKSPACE,
) -> subroutine.views.Collection[subroutine.views.LinkType]:
	"""Return this workspace's link types. Enveloped; see :func:`list_statuses`."""

	workspace = _chosen(session, actor, workspace_id)
	rows = subroutine.domain.vocabulary.link_types(session, workspace_id=workspace.id)

	return subroutine.views.Collection[subroutine.views.LinkType](
		items=[subroutine.views.link_type(row) for row in rows],
		page=subroutine.views.Page(limit=len(rows), has_more=False, total=len(rows)),
	)


@router.post("/link-types", status_code=201, summary="Add a link type")
def create_link_type (
	body: CreateLinkType,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = WORKSPACE,
) -> subroutine.views.LinkType:
	"""Add a way two items can relate."""

	workspace = _chosen(session, actor, workspace_id)

	return subroutine.views.link_type(
		subroutine.domain.vocabulary.create_link_type(
			session,
			workspace_id=workspace.id,
			key=body.key,
			title=body.title,
			inverse_title=body.inverse_title,
			category=body.category,
			is_symmetric=body.is_symmetric,
			actor=actor,
		)
	)


@router.patch("/link-types/{which}", summary="Rename a link type")
def update_link_type (
	which: uuid.UUID,
	body: UpdateLinkType,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.LinkType:
	"""Rename a link type, or reword either end of it."""

	changes = _asked(body, ("key", "title", "inverse_title", "category"))

	return subroutine.views.link_type(
		subroutine.domain.vocabulary.update_link_type(
			session, _link_type(session, actor, which), actor=actor, **changes
		)
	)


@router.delete("/link-types/{which}", status_code=204, summary="Remove a link type")
def delete_link_type (
	which: uuid.UUID,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> None:
	"""Remove a link type nothing is joined by."""

	subroutine.domain.vocabulary.delete_link_type(
		session, _link_type(session, actor, which), actor=actor
	)


@router.get("/tags", summary="The tags this workspace has")
def list_tags (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = WORKSPACE,
) -> subroutine.views.Collection[subroutine.views.TagEntry]:
	"""Return this workspace's tags as things to curate — id, name and what it means.

	**No usage counts here, and `/v1/meta` is where they stay.** §5.5's table says *List (with
	usage counts)* and `/v1/meta` already answers exactly that, narrowed to the tasks this
	caller can see — a tag used only in a private project they are not a member of does not
	appear. Recomputing that beside a curation listing would either duplicate a
	disclosure-sensitive aggregate or publish an unscoped one.
	"""

	workspace = _chosen(session, actor, workspace_id)
	model = subroutine.db.models.vocabulary.Tag
	rows = list(
		session.scalars(
			sqlalchemy.select(model)
			.where(model.workspace_id == workspace.id)
			.order_by(model.name_normalized)
		)
	)

	return subroutine.views.Collection[subroutine.views.TagEntry](
		items=[subroutine.views.tag_entry(row) for row in rows],
		page=subroutine.views.Page(limit=len(rows), has_more=False, total=len(rows)),
	)


@router.post("/tags", status_code=201, summary="Declare a tag")
def create_tag (
	body: CreateTag,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = WORKSPACE,
) -> subroutine.views.TagEntry:
	"""Declare a tag before anybody uses it, and say what it means here."""

	workspace = _chosen(session, actor, workspace_id)
	tag = subroutine.domain.vocabulary.create_tag(
		session,
		workspace_id=workspace.id,
		name=body.name,
		description=body.description,
		actor=actor,
	)

	return subroutine.views.tag_entry(tag)


@router.patch("/tags/{which}", summary="Rename a tag, or say what it means")
def update_tag (
	which: uuid.UUID,
	body: UpdateTag,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.TagEntry:
	"""Rename a tag, or write down what it means in this workspace."""

	# **`description` is the one field here a `null` may clear** — a workspace that wrote down
	# what a label meant has to be able to take it back. A name cannot be.
	changes = _asked(body, ("name", "description"), clearable=("description",))

	return subroutine.views.tag_entry(
		subroutine.domain.vocabulary.update_tag(
			session, _tag(session, actor, which), actor=actor, **changes
		)
	)


@router.delete("/tags/{which}", status_code=204, summary="Remove a tag")
def delete_tag (
	which: uuid.UUID,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> None:
	"""Remove a tag, and with it every application of it."""

	subroutine.domain.vocabulary.delete_tag(session, _tag(session, actor, which), actor=actor)
