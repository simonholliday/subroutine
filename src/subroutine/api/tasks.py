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
import subroutine.api.query
import subroutine.api.routing
import subroutine.api.schemas
import subroutine.api.security
import subroutine.api.shaping
import subroutine.config
import subroutine.db.models.identity
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.claims
import subroutine.domain.hierarchy
import subroutine.domain.links
import subroutine.domain.ordering
import subroutine.domain.paging
import subroutine.domain.readiness
import subroutine.domain.refs
import subroutine.domain.scoping
import subroutine.domain.search
import subroutine.domain.selection
import subroutine.domain.tasks
import subroutine.errors
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1/tasks",
	tags=["tasks"],
	route_class=subroutine.api.routing.Transactional,
)

#: How many rows a listing returns when the caller does not say. Mirrors
#: ``Settings.default_page_size``; the hard ceiling is ``max_page_size``.
DEFAULT_LIMIT = 50

#: What ``?order=`` accepts, read from the domain so that both transports offer the same
#: fields and mean the same thing by them (§6.3a). It lived here until 2026-07-30, which is
#: why a local listing could not be ordered at all: `clients/local.py` may not import this
#: module, since it imports FastAPI.
SORTABLE = subroutine.domain.ordering.TASK_FIELDS

#: Newest first, which is what "what have I got" means for a to-do list.
DEFAULT_ORDER = subroutine.domain.ordering.DEFAULT_TASK_ORDER

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

	#: Tag names, without the ``#`` — ``["health", "admin"]``. The same words a captured line
	#: applies with ``#health``, and refused on the same rule: a name of only digits is a
	#: reference, not a tag (§6.2).
	tags: list[str] | None = None

	#: How long the work is expected to take, in §6.4's grammar — ``"4h"``, ``"1h30m"``, or
	#: a bare number of minutes. The same values ``~4h`` accepts in a captured line.
	estimate: int | str | None = None

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
	type: str | None = None
	assignee_id: uuid.UUID | None = None
	importance: int | None = None
	urgency: int | None = None
	estimate: int | str | None = None

	#: Move the task to another project in the same workspace, by key or id (#43). Its parts
	#: go with it. **Not nullable**, unlike most fields here: every task is in a project, and
	#: `null` would have to mean the Inbox — a destination somebody should have to name.
	project: str | None = None

	#: The task's tags, **replacing** whatever it had (§8.3, like every other field here).
	#: ``[]`` clears them, which is how a mistyped tag is removed; omitting the field leaves
	#: them alone.
	tags: list[str] | None = None
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
			"estimate",
			"tags",
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
		# **Only when it was sent.** `selection.project` defaults to the Inbox, so passing its result
		# unconditionally would override a `+KEY` in the captured line with the Inbox — turning
		# one silent misfiling into another. `project` was missing from the structured fields
		# above, so `POST /v1/tasks {"text": …, "project": "SR"}` was accepted, returned 201, and
		# filed the task in the Inbox with nothing to say it had.
		created, _capture = subroutine.domain.tasks.create_from_text(
			session,
			workspace=workspace,
			text=body.text,
			timezone=body.timezone,
			project=(
				subroutine.domain.selection.project(session, actor, workspace, body.project)
				if body.project is not None
				else None
			),
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
		project=subroutine.domain.selection.project(session, actor, workspace, body.project),
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
	# Refuses a query parameter this endpoint does not declare (SPEC.md §8.1). On a
	# listing an ignored `fields` costs the caller the whole object.
	dependencies=[subroutine.api.query.UnknownQueryDep],
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
	parent: str | None = fastapi.Query(
		None,
		description=(
			"Restrict to the children of one task, by ref or id. Use with subtree=true for "
			"everything beneath it rather than one level."
		),
	),
	subtree: bool = fastapi.Query(
		False, description="With parent: include the whole subtree, not only direct children."
	),
	q: str | None = fastapi.Query(
		None, description="Match this text in the title or the description."
	),
	due_before: datetime.datetime | None = fastapi.Query(None, description="Due strictly before."),
	due_after: datetime.datetime | None = fastapi.Query(None, description="Due strictly after."),
	include_completed: bool = fastapi.Query(False, description="Include finished tasks."),
	deferred: str = fastapi.Query(
		subroutine.domain.readiness.DEFAULT_DEFERRAL,
		description=(
			"How to treat work deferred to a future date: 'include' (the default, and "
			"unchanged), 'exclude' to hide it, or 'only' to see just what is parked."
		),
		examples=["exclude"],
	),
	deleted: bool = fastapi.Query(
		False,
		description=(
			"Show *only* what is in the trash, rather than including it. A mixed list would "
			"be the one place a caller cannot tell a live item from a deleted one."
		),
	),
	ready: bool = fastapi.Query(
		False,
		description=(
			"Only tasks that can actually be started: nothing unfinished blocks them and "
			"they are not deferred to a future date. Does not yet consider a task's own "
			"status — one marked 'blocked' by hand is still returned, because that is a "
			"declared block rather than a tracked dependency (see §5.5)."
		),
	),
	order: str | None = fastapi.Query(
		None, description="Comma-separated sort fields, '-' for descending: '-importance,due_at'."
	),
	limit: int | None = fastapi.Query(
		None,
		# **No `ge=1` here, deliberately.** `domain.paging.size` is the one arbiter, so that
		# this endpoint and the local client refuse an impossible page identically — with
		# `limit` as the field, not FastAPI's `query.limit`. Two copies of the rule produced
		# two different refusals for the same mistake.
		description="How many to return. At least 1; capped at the instance's max_page_size.",
	),
	cursor: str | None = fastapi.Query(None, description="Continue after a previous page."),
	include_total: bool = fastapi.Query(
		False, description="Count the whole result. Costs a second scan; off by default."
	),
	include: str | None = subroutine.api.query.INCLUDE_QUERY,
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""List tasks, narrowed by whatever the query string asks for."""

	shape = subroutine.api.shaping.wanted(
		format=format, fields=fields, available=SELECTABLE, entity="task"
	)

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	statement = subroutine.domain.scoping.readable_tasks(
		actor,
		workspace_ids=[workspace.id],
		include_completed=include_completed,
		include_deleted=deleted,
	)

	model = subroutine.db.models.work.Task

	# **Only the trash, not the trash as well.** `include_deleted` widens; this narrows to what
	# was widened for. A mixed list is the one place a caller cannot tell a live item from a
	# deleted one, since nothing in a compact line says which.
	if deleted:
		statement = statement.where(model.deleted_at.is_not(None))

	if project is not None:
		chosen = subroutine.domain.selection.project(session, actor, workspace, project)
		# The project *and everything under it* (`#320`) — a named project means that area of
		# work, and a parent whose listing excluded its own children made the tree decorative.
		statement = statement.where(subroutine.domain.scoping.within_project(chosen))

	if status is not None:
		statement = statement.where(
			model.status_id == subroutine.domain.tasks.status_for(session, workspace.id, status).id
		)

	if type is not None:
		statement = statement.where(
			model.type_id == subroutine.domain.tasks.item_type_for(session, workspace.id, type).id
		)

	if parent is not None:
		# Resolved through `_resolve`, so a parent the caller cannot see is "no such task"
		# rather than an empty list — an empty listing would say the subtree is empty, which
		# is a different and false claim (§7.3a).
		above = _resolve(session, actor, workspace, parent)

		statement = (
			statement.where(
				subroutine.domain.hierarchy.subtree(model, above), model.id != above.id
			)
			if subtree
			else statement.where(model.parent_task_id == above.id)
		)

	elif subtree:
		raise subroutine.errors.ValidationError(
			"'subtree' says how much of a parent's tree to return, so it needs a parent.",
			errors=[
				subroutine.errors.FieldError(
					field="subtree",
					code="invalid_field_value",
					message="'subtree' has no meaning without 'parent'.",
					hint="Pass parent=<ref> as well, or drop subtree.",
				)
			],
		)

	# Applied before `ready`, which subsumes it — the two may be combined and the narrower
	# wins, rather than one silently overriding the other.
	narrowing = subroutine.domain.readiness.deferred(
		model,
		now=subroutine.db.types.utcnow(),
		choice=subroutine.domain.readiness.refuse_unknown_deferral(deferred),
	)

	if narrowing is not None:
		statement = statement.where(narrowing)

	if ready:
		statement = statement.where(
			subroutine.domain.readiness.ready(
				model, now=subroutine.db.types.utcnow(), by=actor.user.id
			)
		)

	if assignee_id is not None:
		statement = statement.where(model.assignee_id == assignee_id)

	if q:
		# **Title and description, which is what §9.4 always said.** It was the title alone
		# until 2026-07-31 — a search that returns plausible rows and silently drops the ones
		# nobody knew to look for.
		statement = statement.where(
			subroutine.domain.search.matching(q, model.title, model.description)
		)

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
		actor=actor,
		workspace_id=workspace.id,
		with_links=subroutine.api.query.includes(include, "links", entity="task"),
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
			"estimate",
			"due",
			"planned_for",
			"start",
			"tags",
		)
		if name in supplied
	}

	# None of these four is patchable-to-null: a status and a type are required, and the two
	# all-day flags are booleans on a NOT NULL column, so `null` has nothing to mean. Passed
	# only when given and not null.
	#
	# **The flags do reach the service on their own, and until `#195` the service dropped
	# them.** They were plain arguments there rather than patch sentinels, so one sent without
	# its date was consulted by nothing and the request returned 200 having changed nothing.
	# This loop was always right; it is named here because reading it is what suggests
	# otherwise.
	for name, parameter in (
		("status", "status_key"),
		("type", "type_key"),
		("due_is_all_day", "due_is_all_day"),
		("start_is_all_day", "start_is_all_day"),
	):
		if name in supplied and getattr(body, name) is not None:
			changes[parameter] = getattr(body, name)

	# Resolved through the domain, so `SR` names one project and an unknown key is refused
	# the same way whichever transport asked. Only when sent and not null: `selection.project`
	# answers `None` with the Inbox, so passing it through unconditionally would file every
	# ordinary edit into the Inbox — the misfiling `#23` produced, with a 200 instead of a 201.
	if body.project is not None:
		changes["project"] = subroutine.domain.selection.project(
			session, actor, workspace, body.project
		)

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


@router.post("/{id_or_ref}/claim", summary="Take a task, so nobody else does")
def take (
	id_or_ref: subroutine.api.schemas.ItemAddress,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	minutes: int | None = fastapi.Query(
		None, description="How long the lease lasts. Defaults to the instance's setting."
	),
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Task:
	"""Take a lease on a task, or renew one you already hold.

	A **lease, not a lock** (SPEC.md §14.11): it expires, and an expired one is ignored rather
	than needing anybody to clear it. Workers die mid-task, and a claim that outlived its
	holder would strand the work permanently.

	Claiming something somebody else holds is a `409` naming who and until when. Claiming
	something you already hold renews it, and keeps the instant you first took it.

	`?ready=true` hides work another worker holds, and never hides your own.
	"""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	task = _resolve(session, actor, workspace, id_or_ref)
	held = subroutine.domain.claims.claim(
		session, task, minutes=minutes, settings=subroutine.config.load_settings(), actor=actor
	)

	return _rendered(session, held)


@router.post("/{id_or_ref}/release", summary="Give a task back")
def give_back (
	id_or_ref: subroutine.api.schemas.ItemAddress,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Task:
	"""Give a task back, so somebody else can take it.

	Releasing something nobody holds is not an error and records nothing — a worker tidying up
	after itself should not have to check first.

	**Anybody who may change the task may release it**, not only the holder. The case this
	exists for is a worker that died holding a lease, and requiring its credential would put
	the remedy in the hands of the one principal that cannot act.
	"""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	task = _resolve(session, actor, workspace, id_or_ref)
	freed = subroutine.domain.claims.release(session, task, actor=actor)

	return _rendered(session, freed)


@router.post("/{id_or_ref}/restore", summary="Take a task out of the trash")
def unremove (
	request: starlette.requests.Request,
	id_or_ref: subroutine.api.schemas.ItemAddress,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Task:
	"""Restore a soft-deleted task (SPEC.md §6.9).

	**The half that made soft delete soft**, and it did not exist until `#140` — §6.9 promised
	a deleted item was restorable, `trash_retention_days` has always been a setting, and
	`EventAction.RESTORED` has always been in the vocabulary, with nothing clearing
	`deleted_at`.

	Registered before the parameterised deletes below it for `routing.check`'s reason, and
	`POST` rather than `DELETE ?restore=` because it is not a deletion of anything.
	"""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	# `_resolve` already sees the trash — "a reference to something in the trash is more useful
	# than a dangling one", decided long before there was anything to restore it with. Which is
	# the whole of what this endpoint needed from it.
	task = _resolve(session, actor, workspace, id_or_ref)

	with subroutine.api.concurrency.reporting(lambda: _rendered(session, task)):
		back = subroutine.domain.tasks.restore(
			session,
			task,
			expected_version=subroutine.api.concurrency.expected(request),
			actor=actor,
		)

	return _rendered(session, back)


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
		instead = subroutine.domain.scoping.the_other_kind(
			session, actor, workspace_id=workspace.id, ref=ref, asked_for="task"
		)

		if instead is not None:
			# `#488`. Saying "there is no task 480" about a document the caller has just listed
			# is a refusal naming a cause it has not established, and it is the one an agent
			# meets when it tries to revise a conclusion — which is how `#293`'s reporter came
			# to believe documents were immutable and stopped filing them at all.
			raise subroutine.errors.NotFound(
				f"{subroutine.domain.refs.format_ref(instead.ref)} is a document, not a task "
				f"— {instead.title}",
				errors=[
					subroutine.errors.FieldError(
						field="id_or_ref",
						code="not_found",
						message=f"{id_or_ref!r} names a document in {workspace.slug}.",
						hint=f"Read it at GET /v1/documents/{instead.ref}, or revise it with "
						f"PATCH /v1/documents/{instead.ref}.",
					)
				],
			)

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
	actor: subroutine.domain.authentication.Principal,
	workspace_id: uuid.UUID,
	with_links: bool = False,
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
	# One definition of a page size, shared with the local client (SPEC.md §13.7): the two
	# transports disagreed about limit until 2026-07-30 because each had its own copy.
	size = subroutine.domain.paging.size(limit, settings)
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

	# Three queries for the whole page, not one per row — `links.edges` gathers every end
	# these links reach before looking any of them up. The point of the parameter is to
	# remove an N+1 from the caller, so doing one here would be a joke at their expense.
	links = (
		[
			subroutine.views.edge(found)
			for found in subroutine.domain.links.edges(
				session,
				actor,
				workspace_id=workspace_id,
				entity_type="task",
				identifiers=[row.id for row in rows],
			)
		]
		if with_links
		else None
	)

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
		links,
	)


def _rendered (
	session: sqlalchemy.orm.Session, row: subroutine.db.models.work.Task
) -> subroutine.views.Task:
	"""Render one task, loading the vocabulary it names."""

	return subroutine.views.task(
		row, subroutine.views.Vocabulary.for_tasks(session, [row])
	)
