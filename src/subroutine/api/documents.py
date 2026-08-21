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
import subroutine.api.filters
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
import subroutine.domain.mentions
import subroutine.domain.ordering
import subroutine.domain.paging
import subroutine.domain.refs
import subroutine.domain.scoping
import subroutine.domain.search
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

#: Declared in the domain, like a task's, so that ``GET /v1/documents?order=`` and a local
#: client sorting the same rows accept the same names and refuse the same ones. Kept here
#: under the name the router already uses rather than reached through in twelve places.
SORTABLE: dict[str, subroutine.api.pagination.Sortable] = (
	subroutine.domain.ordering.DOCUMENT_FIELDS
)

DEFAULT_ORDER = subroutine.domain.ordering.DEFAULT_DOCUMENT_ORDER

#: What ``?fields=`` may name, read from the view so the two cannot drift (docs/design.md §14.10).
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

	#: Tag names, without the `#` — `["decision", "security"]`. The same words a task takes,
	#: from the same per-workspace vocabulary (`#819`), and refused on the same rule: a name of
	#: only digits is a reference, not a tag (§6.2).
	tags: list[str] | None = None

	supersedes: subroutine.api.schemas.Reference | None = None


class Update(subroutine.api.schemas.RequestModel):
	"""What ``PATCH /v1/documents/{id_or_ref}`` accepts.

	Omitted is unchanged; null clears (§8.3). Setting ``supersedes`` moves the document it
	names to the status that says so — the two are one fact, and a superseded document still
	reading as active is one somebody will act on.
	"""

	title: str | None = None
	body: str | None = None
	type: str | None = None
	status: str | None = None
	owner_id: uuid.UUID | None = None

	#: The document's tags, **replacing** whatever it had (§8.3, like every other field here).
	#: `[]` clears them, which is how a mistyped tag is removed; omitting the field leaves them
	#: alone.
	tags: list[str] | None = None

	#: The project to file it under, by key — `#294`. Accepted on create since M1 and here by
	#: nothing, so a conclusion written before anybody decided where it belonged stayed in the
	#: Inbox for good. A document's project also decides who may read it (§7.3a), which makes
	#: this a permissions field rather than a filing one.
	project: str | None = None

	supersedes: subroutine.api.schemas.Reference | None = None

	#: The version this change is based on (docs/design.md §8.9).
	expected_version: int | None = None


#: Which way a link runs relative to the item in the path — `#816`.
DIRECTIONS: frozenset[str] = frozenset({"outgoing", "incoming"})


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

	#: **Which way round the link runs, from the point of view of the item in the path**
	#: (`#816`). ``outgoing`` — the default, and what every caller sent before this existed —
	#: stores this item as the source. ``incoming`` stores the *other* item as the source and
	#: still records the action against this one, because that is the item somebody was
	#: looking at when they made it.
	#:
	#: **The alternative was the client swapping the ends, and it is what the browser did.**
	#: `#799` gave it both directions and it implemented the inverse by posting to the other
	#: item's links — correct about the row and wrong about who acted, so *what did I work on*
	#: listed an item the reader never opened. A direction here is the same request said
	#: honestly: one endpoint, one link type, and no inverse for the instance to learn.
	direction: str = "outgoing"


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
		project=subroutine.domain.selection.project(session, actor, workspace, body.project),
		title=body.title,
		body=body.body,
		type_key=body.type or "note",
		status_key=body.status,
		parent=(
			None if body.parent is None else _resolve(session, actor, workspace, str(body.parent))
		),
		owner_id=body.owner_id if body.owner_id is not None else actor.user.id,
		tags=body.tags,
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
	response_model=subroutine.views.Collection[subroutine.views.Document],
)
def listing (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	dates: subroutine.api.filters.DocumentFilters,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
	project: str | None = fastapi.Query(None, description="Restrict to one project."),
	type: str | None = fastapi.Query(None, description="Restrict to one document type key."),
	status: str | None = fastapi.Query(None, description="Restrict to one status key."),
	q: str | None = fastapi.Query(
		None, description="Words to look for in the title or the body. Every one must appear."
	),
	deleted: bool = fastapi.Query(
		False, description="Show *only* what is in the trash, rather than including it."
	),
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
	include: str | None = subroutine.api.query.INCLUDE_QUERY,
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""List the documents this caller can see."""

	shape = subroutine.api.shaping.wanted(
		format=format, fields=fields, available=SELECTABLE, entity="document"
	)
	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	statement = subroutine.domain.scoping.readable_documents(
		actor, workspace_ids=[workspace.id], include_deleted=deleted
	)

	model = subroutine.db.models.work.Document

	# Narrowed to what was widened for, as on tasks: `include_deleted` widens and this asks
	# only for the trash.
	if deleted:
		statement = statement.where(model.deleted_at.is_not(None))

	if project is not None:
		# The project and everything under it (`#320`), matching the task listing — a document
		# filed in a sub-project is part of that area of work in exactly the same way.
		statement = statement.where(
			subroutine.domain.scoping.within_project(
				subroutine.domain.selection.project(session, actor, workspace, project)
			)
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

	# **Resolved before the `if`, and from *this application's* settings** (`#883`). It was
	# read inside the branch and used again below under a second `if`, which is correct only by
	# short-circuit and is a name mypy cannot prove is bound. And omitting `settings` sent
	# `chosen` to `config.load_settings()`, which re-reads the environment — so an instance
	# built with `search_backend` injected answered tasks with one backend and documents with
	# the other, which is what kept the whole document half of `#823` out of the suite.
	backend = subroutine.domain.search.chosen(session, settings=settings)
	words = subroutine.domain.search.terms(q or "")

	if q:
		# Title and body, the document's counterpart to a task's description (§9.4). This is
		# where this project's own reasoning lives: `#4` is a specification, and searching for
		# a term it discusses at length found nothing at all.
		# `#892`, and the tasks endpoint says why the composition moved into the domain.
		# `#83` is the half it adds: a document's comments are where it was argued over, which
		# is often where the sentence somebody half-remembers actually is.
		statement = statement.where(
			subroutine.domain.search.anywhere(
				q,
				identity=model.id,
				columns=(model.title, model.body),
				ref=model.ref,
				entity_type="document",
				backend=backend,
			)
		)

	# **§9.6's dotted filters** (`#815`). Documents are here because one ref counter serves both
	# kinds (§6.2), so *what was created yesterday* answered for tasks alone would be wrong
	# about half of what a number can name.
	statement = subroutine.api.filters.narrowed(
		statement, dates, session=session, actor=actor, workspace=workspace
	)

	# **`relevance`, for this query only and only where something can rank it** (`#823`). Built
	# after the filters rather than beside the search predicate so that everything narrowing the
	# statement has happened first and this reads as what it is: a choice about *order*.
	# **`deferred` is here too, and a document is never deferred** (`#877`). §6.14 says a
	# document is not scheduled, so it has no start date to arrive — but a merged listing can
	# only be asked for an order *both* halves accept, and one that documents refused would drop
	# them from the page entirely (`#782`). So it answers, in the one band it can be in.
	sortable: dict[str, subroutine.domain.ordering.Sortable] = (
		subroutine.domain.ordering.sinking(SORTABLE)
	)
	fallback: tuple[str, ...] = tuple(DEFAULT_ORDER)

	# **`words` rather than `q`** (`#880`): a query of spaces is truthy and has no words in
	# it, and the ranking path cannot be built from none — it indexed `terms[-1]` and returned
	# 500. The filter above has always answered that case correctly, which is the asymmetry.
	if words and backend == subroutine.domain.search.NATIVE:
		sortable = subroutine.domain.ordering.searching(
			sortable,
			terms=words,
			columns=[model.title, model.body],
			carried_on=model.relevance,
			ref=model.ref,
			numbered=subroutine.domain.refs.parse_ref(q or ""),
		)
		fallback = (f"-{subroutine.domain.ordering.RELEVANCE}",)

	# `#884`, and the endpoint above says why: a published name refused as unknown.
	subroutine.domain.ordering.refuse_ranking_without_a_search(
		order, searching=subroutine.domain.ordering.RELEVANCE in sortable
	)

	keys = subroutine.api.pagination.parse_order(
		order, allowed=sortable, default=fallback, tiebreak=model.id
	)
	# **The loader option, without which `ordering.scored` raises.** A ranking exists only in
	# SQL, so the value has to be attached to each loaded row for a cursor to name a page
	# boundary. This listing had no computed sort field before and so had no call to
	# `options`; adding the field without adding this would have made every ranked document
	# page fail on the *second* page, which is `#46` exactly.
	statement = statement.options(
		*subroutine.domain.ordering.options(order, allowed=sortable, default=fallback)
	)
	# One definition of a page size, shared with the local client (docs/design.md §13.7): the two
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

	# Same three queries as the task listing, and the same reason: an include that fanned out
	# per row would move the caller's N+1 inside the server rather than remove it.
	links = (
		subroutine.views.edges(
			session,
			subroutine.domain.links.edges(
				session,
				actor,
				workspace_id=workspace.id,
				entity_type="document",
				identifiers=[row.id for row in rows],
			),
		)
		if subroutine.api.query.includes(include, "links", entity="document")
		else None
	)

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
		links,
	)


@router.get(
	"/{id_or_ref}",
	summary="Read one document",
	response_model=subroutine.views.Document,
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
	"""Change a document. Omitted fields are untouched; nulls clear (docs/design.md §8.3)."""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	document = _resolve(session, actor, workspace, id_or_ref)

	supplied = body.model_fields_set
	changes: dict[str, typing.Any] = {
		name: getattr(body, name)
		# **`tags` is in this loop rather than beside it**, because it is one of the fields
		# where `null` means *clear* rather than *leave alone* — and `model_fields_set` is the
		# only thing that tells those apart (§8.3). The three `if … is not None` blocks below
		# are the fields that cannot be cleared.
		for name in ("title", "body", "owner_id", "tags")
		if name in supplied
	}

	if "status" in supplied and body.status is not None:
		changes["status_key"] = body.status

	if "type" in supplied and body.type is not None:
		changes["type_key"] = body.type

	if "project" in supplied and body.project is not None:
		# Resolved here because the service takes a row and the caller has a key, which is
		# what `selection.project` is for and what the task endpoint already does.
		changes["project"] = subroutine.domain.selection.project(
			session, actor, workspace, body.project
		)

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


class Move(subroutine.api.schemas.RequestModel):
	"""Where a document should sit in the tree. ``parent: null`` makes it top-level."""

	parent: str | None = None

	#: The version this move is based on. Optional; ``If-Match`` does the same job for a
	#: client that prefers the header, and sending neither means the check was not asked for.
	expected_version: int | None = None

	def requested (self) -> bool:
		"""Report whether the caller actually named a destination."""

		return "parent" in self.model_fields_set


@router.post(
	"/{id_or_ref}/move", summary="Nest a document under another, or move it to the top level"
)
def move (
	request: starlette.requests.Request,
	id_or_ref: subroutine.api.schemas.ItemAddress,
	body: Move,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Document:
	"""Re-nest a document, taking its sections with it.

	**The half of re-parenting that had no endpoint at all.** ``parent_id`` was reported by this
	view and accepted nowhere — not here, not on create, not on update — so a document could
	be a section of another only by being inserted into the database directly.
	"""

	if not body.requested():
		raise subroutine.errors.ValidationError(
			"A move has to say where to.",
			code="missing_field",
			errors=[
				subroutine.errors.FieldError(
					field="parent",
					code="missing_field",
					message="Send 'parent' with a ref or id, or 'parent': null to make this a "
					"top-level document.",
				)
			],
			hint="An omitted 'parent' would have to mean one of those, and guessing which is "
			"how a subtree gets flattened by accident.",
		)

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	document = _resolve(session, actor, workspace, id_or_ref)
	parent = None if body.parent is None else _resolve(session, actor, workspace, body.parent)

	with subroutine.api.concurrency.reporting(lambda: _rendered(session, document)):
		subroutine.domain.documents.move(
			session,
			document,
			parent=parent,
			expected_version=subroutine.api.concurrency.expected(request, body.expected_version),
			actor=actor,
		)

	return _rendered(session, document)


@router.post("/{id_or_ref}/restore", summary="Take a document out of the trash")
def unremove (
	request: starlette.requests.Request,
	id_or_ref: subroutine.api.schemas.ItemAddress,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Document:
	"""Restore a soft-deleted document — the task endpoint's counterpart (docs/design.md §6.9).

	Both, because one ref counter serves both kinds (§6.2): a restore that worked on half the
	numbers would surprise anybody holding a ref.
	"""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	document = _resolve(session, actor, workspace, id_or_ref)

	with subroutine.api.concurrency.reporting(lambda: _rendered(session, document)):
		back = subroutine.domain.documents.restore(
			session,
			document,
			expected_version=subroutine.api.concurrency.expected(request),
			actor=actor,
		)

	return _rendered(session, back)


@router.delete("/{id_or_ref}", summary="Move a document to the trash")
def remove (
	request: starlette.requests.Request,
	id_or_ref: subroutine.api.schemas.ItemAddress,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Document:
	"""Soft-delete a document. It stays recoverable (docs/design.md §6.9)."""

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

		found = subroutine.views.links(
			session,
			subroutine.domain.links.around(
				session,
				actor,
				workspace_id=workspace.id,
				entity_type=entity_type,
				identifier=near.id,
			),
		)

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

		if body.direction not in DIRECTIONS:
			raise subroutine.errors.ValidationError(
				f"{body.direction!r} is not a direction.",
				errors=[
					subroutine.errors.FieldError(
						field="direction",
						code="invalid_field_value",
						message=f"{body.direction!r} is not a direction a link can run in.",
						hint=f"Use one of: {', '.join(sorted(DIRECTIONS))}.",
					)
				],
			)

		incoming = body.direction == "incoming"

		created = subroutine.domain.links.create(
			session,
			workspace_id=workspace.id,
			source=far if incoming else near,
			target=near if incoming else far,
			link_type_key=body.link_type,
			# **The row and the event name different items on this one path, deliberately**
			# (`#816`). The row says what is true — `#17 blocks #16` — and the event says what
			# somebody did, which happened on `#16`.
			acted_on=near,
			actor=actor,
		)

		for related in subroutine.domain.links.around(
			session, actor, workspace_id=workspace.id, entity_type=entity_type, identifier=near.id
		):
			if related.id == created.id:
				return subroutine.views.links(session, [related])[0]

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

		# **Whichever end the reader was standing on** (`#816`). This route finds a link by
		# either end, so an incoming one is withdrawn from the target — and recording the
		# source would attribute the work to an item nobody opened. The row is unchanged; only
		# what the event says somebody did.
		subroutine.domain.links.remove(session, found, acted_on=near, actor=actor)

		return fastapi.Response(status_code=204)

	return listing, create, remove


def _backlinks_for (entity_type: str) -> typing.Any:
	"""Build the backlink listing for one entity type — `#144`."""

	def listing (
		id_or_ref: subroutine.api.schemas.ItemAddress,
		actor: subroutine.api.security.PrincipalDep,
		session: subroutine.api.dependencies.SessionDep,
		workspace_id: str | None = fastapi.Query(None, description="Which workspace."),
	) -> subroutine.views.Collection[subroutine.views.Backlink]:
		"""Return everything whose prose refers to this item.

		**A sub-resource rather than §8.5's ``?include=backlinks``**, and the departure is
		deliberate. ``INCLUDABLE``'s own rule is that every entry promises a bounded number of
		queries *per page*, and backlinks on a page of fifty is either fifty lookups or a join
		nobody asked for — the N+1 that parameter exists to remove, moved inside the server.
		Every other section ``subroutine show`` renders is already a sub-resource: links,
		comments and history.

		Enveloped like every other collection (§8.4) and returned whole, for the reason the
		links listing gives: what refers to an item is bounded by how much somebody wrote, so
		``has_more`` is a statement rather than a shrug.

		**Narrowed in the domain**, which is where §6.15's rule belongs — a mention from a
		project the reader cannot see is omitted entirely, because *something you cannot see
		mentioned this* discloses that activity exists and explains nothing.
		"""

		workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
		near = _near(session, actor, workspace, entity_type, id_or_ref)

		found = [
			subroutine.views.Backlink(
				kind=one.kind,
				ref=one.ref,
				title=one.title,
				via=one.via,
				created_at=one.at,
			)
			for one in subroutine.domain.mentions.backlinks(
				session,
				principal=actor,
				workspace_id=workspace.id,
				target_type=entity_type,
				target_id=near.id,
			)
		]

		return subroutine.views.Collection(
			items=found,
			page=subroutine.views.Page(limit=len(found), has_more=False, total=len(found)),
		)

	return listing


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
	target.add_api_route(
		"/{id_or_ref}/backlinks",
		_backlinks_for(entity_type),
		methods=["GET"],
		name=f"{noun}_backlinks",
		summary=f"List what refers to a {noun}",
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

	# A ref is all digits and a project key must start with a letter (docs/design.md §6.2), so
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
		instead = subroutine.domain.scoping.the_other_kind(
			session, actor, workspace_id=workspace.id, ref=ref, asked_for="document"
		)

		if instead is not None:
			# `#488`, the mirror. This hint already *said* a ref might name a task instead —
			# which is a hedge, and a hedge is what a refusal offers when it has not looked.
			# Having looked, it can say which.
			raise subroutine.errors.NotFound(
				f"{subroutine.domain.refs.format_ref(instead.ref)} is a task, not a document "
				f"— {instead.title}",
				errors=[
					subroutine.errors.FieldError(
						field="id_or_ref",
						code="not_found",
						message=f"{id_or_ref!r} names a task in {workspace.slug}.",
						hint=f"Read it at GET /v1/tasks/{instead.ref}, or change it with "
						f"PATCH /v1/tasks/{instead.ref}.",
					)
				],
			)

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
