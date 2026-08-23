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
import subroutine.config
import subroutine.db.models.identity
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.claims
import subroutine.domain.filtering
import subroutine.domain.hierarchy
import subroutine.domain.instances
import subroutine.domain.links
import subroutine.domain.ordering
import subroutine.domain.paging
import subroutine.domain.readiness
import subroutine.domain.recurrence
import subroutine.domain.refs
import subroutine.domain.schedule
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

#: What ``?fields=`` may name, read from the view so the two cannot drift (docs/design.md §14.10).
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
	#: The task this one goes underneath, by ref or by id (`#510`). **Widened from a bare
	#: UUID rather than renamed**: the argument two fields below applies here word for
	#: word — a caller holding a UUID for a task is a caller who has already made a
	#: request they should not have had to, and they have `#42`. Renaming it to `parent`,
	#: which is what `Move` calls the same thing, is a breaking wire change and so is
	#: somebody else's to take.
	parent_task_id: subroutine.api.schemas.Reference | None = None

	type: str | None = None
	status: str | None = None
	#: Who is to do this, by username or id (`#493`). **Not `assignee_id`**: a caller holding
	#: a UUID for a person is a caller who has already made a request they should not have had
	#: to, and §6.13's capture line has always taken `@name`. One grammar, both routes in.
	assignee: str | None = None
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

	#: **Two fields where there was one** (`#854`). ``starts`` says when the work begins;
	#: ``snooze`` hides the row until it passes. The old ``start`` and ``planned_for`` are
	#: gone rather than aliased, so a caller that meant either is refused by name instead of
	#: being given the other one's behaviour.
	starts: str | None = None
	starts_is_all_day: bool | None = None
	snooze: str | None = None
	snoozed_is_all_day: bool | None = None

	#: How often this repeats — a phrase like ``every month on the 30th`` or an ``RRULE``
	#: directly. ``recurrence_anchor`` says what the next date is measured from and
	#: ``recurrence_trigger`` what brings it into being; §6.7 and decision `#915`.
	recurrence: str | None = None
	recurrence_anchor: str | None = None
	recurrence_trigger: str | None = None
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
	#: Who is to do this, by username or id (`#493`). **Not `assignee_id`**: a caller holding
	#: a UUID for a person is a caller who has already made a request they should not have had
	#: to, and §6.13's capture line has always taken `@name`. One grammar, both routes in.
	assignee: str | None = None
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

	#: **Two fields where there was one** (`#854`). ``starts`` says when the work begins;
	#: ``snooze`` hides the row until it passes. The old ``start`` and ``planned_for`` are
	#: gone rather than aliased, so a caller that meant either is refused by name instead of
	#: being given the other one's behaviour.
	starts: str | None = None
	starts_is_all_day: bool | None = None
	snooze: str | None = None
	snoozed_is_all_day: bool | None = None

	#: How often this repeats — a phrase like ``every month on the 30th`` or an ``RRULE``
	#: directly. ``recurrence_anchor`` says what the next date is measured from and
	#: ``recurrence_trigger`` what brings it into being; §6.7 and decision `#915`.
	recurrence: str | None = None
	recurrence_anchor: str | None = None
	recurrence_trigger: str | None = None
	timezone: str | None = None

	#: The version this change is based on (docs/design.md §8.9). Optional; ``If-Match`` does the
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
			"importance",
			"urgency",
			"estimate",
			"tags",
			"due",
			"due_is_all_day",
			"starts",
			"starts_is_all_day",
			"snooze",
			"snoozed_is_all_day",
			"recurrence",
			"recurrence_anchor",
			"recurrence_trigger",
		)
		if name in supplied
	}

	if "assignee" in supplied:
		structured["assignee_id"] = (
			None
			if body.assignee is None
			else subroutine.domain.tasks.assignee_for(session, workspace.id, body.assignee).id
		)

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
	response_model=subroutine.views.Collection[subroutine.views.Task],
)
def listing (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	dates: subroutine.api.filters.TaskFilters,
	workspace_id: str | None = fastapi.Query(
		None, description="Which workspace, by id or slug. Needed when you can reach several."
	),
	project: str | None = fastapi.Query(None, description="Restrict to one project, by key or id."),
	status: str | None = fastapi.Query(None, description="Restrict to one status key."),
	status_category: str | None = fastapi.Query(
		None,
		description=(
			"Restrict to one status category: todo, in_progress, done or cancelled. Unlike "
			"'status' this survives an installation renaming its statuses, so it is the handle "
			"a board or a completed-work view should use. Naming a finished category reaches "
			"finished work without also passing include_completed."
		),
		examples=["done"],
	),
	assignee: str | None = fastapi.Query(
		None,
		description="Restrict to one assignee, by username or id. 'me' is the account you are "
		"signed in as — which is not the same as ?actor=me on the change feed, where it means "
		"this credential.",
	),
	claimed_by: str | None = fastapi.Query(
		None,
		description="Restrict to what one account is holding, by username or id. 'me' is the "
		"account you are signed in as. Expired claims are not held, so they are left out.",
	),
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
		None, description="Words to look for in the title or the description. Every one must appear."
	),
	# **A string rather than a `datetime`, which is what stopped a bare date being a 500**
	# (`#1017`). Pydantic reads `2026-08-18` as a naive datetime and `db/types.UtcDateTime`
	# refuses one at execute time, where the refusal becomes `internal_error` and says nothing
	# about the parameter. Read through `domain/filtering.ALIASES` instead, so these take
	# whatever `due_at.lt` and `due_at.gt` take — a date, an instant or a §9.3 expression — and
	# a value that cannot be read is a 422 naming the field.
	due_before: str | None = fastapi.Query(None, description="Due strictly before."),
	due_after: str | None = fastapi.Query(None, description="Due strictly after."),
	include_completed: bool | None = fastapi.Query(
		None,
		description=(
			"Include finished tasks. Left unsaid it is off, unless status_category names a "
			"finished category — asking for finished work and not mentioning completion is not "
			"a request for an empty page."
		),
	),
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
		format=format,
		fields=fields,
		available=SELECTABLE,
		entity="task",
		timezone=subroutine.views.reader_zone(session, actor),
	)

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)

	# Resolved in the domain rather than here, so the local client reaches the same rows for
	# the same query — a narrowing that widened only over HTTP is the divergence S3-07 removed.
	# **Asking *when* something was completed is asking for completed work** (`#818`), the same
	# way naming a finished category is. Without it, the one field that is null on everything
	# unfinished was compared against a set with all of it filtered out — and answered `[]`.
	# **Resolved once, here, because two questions want the same row** (`#1032`): whether this
	# listing reaches finished work, and which status to narrow by. A second lookup below would
	# be the same query twice and a second chance for the two to disagree about an unknown key.
	named = (
		None
		if status is None
		else subroutine.domain.tasks.status_for(session, workspace.id, status)
	)

	completion = subroutine.domain.tasks.completion_wanted(
		status_category,
		include_completed,
		# **Naming the finished status by its key asks for finished work** (`#1032`), as
		# unambiguously as naming the category does. `subroutine list --status done` answered
		# nothing on an instance holding five items finished that fortnight.
		status_named=named,
		about_completion=dates.about(subroutine.domain.filtering.COMPLETION_FIELD),
		about_activity=dates.about(subroutine.domain.filtering.TOUCHED_AT),
		# **The trash is a question about deletion, not about status** (`#900`). Asking what
		# you deleted must reach something you had finished first, which is entirely ordinary
		# — three items here were reachable by `show` and by no listing at all.
		about_deletion=deleted,
		# **A number is a lookup, not a filter** (`#873`). `#867` made an exact ref match find
		# the item; three items in four here are finished, so without this the row was found
		# and then hidden by the rule that a listing shows unfinished work.
		naming_one_item=q is not None
		and subroutine.domain.refs.parse_ref(q) is not None,
	)

	statement = subroutine.domain.scoping.readable_tasks(
		actor,
		workspace_ids=[workspace.id],
		include_completed=completion,
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

	if named is not None:
		statement = statement.where(model.status_id == named.id)

	if status_category is not None:
		statement = statement.where(
			model.status_id.in_(
				subroutine.domain.tasks.statuses_in_category(session, workspace.id, status_category)
			)
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

	# **One instant for the whole request**, which is what `readiness.undeferred` takes it for:
	# the rows this listing hides as deferred, the rows `ready` hides for the same reason, and
	# the rows `?order=deferred` sinks are three readings of one clock, and a listing that read
	# it three times could sink a row it had just decided was startable.
	now = subroutine.db.types.utcnow()

	# **What `-priority_score` means here depends on which project this workspace has
	# prioritised** (`#986`, decision `#982`), so the vocabulary is adjusted before anything is
	# layered on it. The paths are resolved in Python and arrive as literals: reaching the
	# pointer from inside the expression is a correlated subquery in `ORDER BY`, which is
	# `#856`'s defect. Nothing prioritised returns the module's own map unchanged.
	#
	# **Every caller that already sorted by priority gets this without asking**, which is the
	# requirement — a focus nobody would think to opt into is a focus that does nothing.
	focused = subroutine.domain.ordering.prioritising(
		SORTABLE,
		prefixes=subroutine.domain.scoping.prioritised_paths(
			session, actor, workspace_ids=[workspace.id]
		),
	)

	# **`deferred` is added here rather than declared in `SORTABLE`** (`#877`), because the band
	# it sorts by is a fact about that instant rather than about a column. `searching` layers
	# `relevance` on top of this below, so a ranked search can still sink deferred work.
	sortable = subroutine.domain.ordering.sinking(focused, model=model, now=now)

	# `None` unless a search ran and something can rank it, which is what keeps every listing
	# that is not a search on exactly the vocabulary it has always had.
	ranked: dict[str, subroutine.domain.ordering.Sortable] | None = None

	# Applied before `ready`, which subsumes it — the two may be combined and the narrower
	# wins, rather than one silently overriding the other.
	narrowing = subroutine.domain.readiness.deferred(
		model,
		now=now,
		choice=subroutine.domain.readiness.refuse_unknown_deferral(deferred),
	)

	if narrowing is not None:
		statement = statement.where(narrowing)

	if ready:
		statement = statement.where(
			subroutine.domain.readiness.ready(model, now=now, by=actor.user.id)
		)

	# **A username or an id, resolved the way every other identifier here is** (`#501`). This
	# was `assignee_id` and took a UUID only, which made "what is Simon working on" a question
	# you had to already know the answer to part of. Renamed rather than widened, because a
	# parameter called `_id` that takes a name is a third thing to learn — and the rename costs
	# nobody anything, since no client could pass the old one at all.
	if assignee is not None:
		statement = statement.where(
			model.assignee_id
			== subroutine.domain.selection.user(
				session, assignee, caller=actor.user
			).id
		)

	# **Who is holding it, which is a different question from who it is assigned to** (`#1120`).
	# An assignee is somebody's to do; a claim is somebody doing it *now*, so *what is my agent
	# sitting on* and *what did I give it* have different answers and the second was the only
	# one anybody could ask.
	#
	# **An expired claim is not held**, which is §10.7 invariant 10 — an expired claim is
	# treated as absent rather than cleaned up eagerly — so this asks the same question
	# `readiness` does rather than a second version of it. Without that, *what am I holding*
	# would answer with work the lease has already released to somebody else.
	if claimed_by is not None:
		statement = statement.where(
			model.claimed_by_id
			== subroutine.domain.selection.user(session, claimed_by, caller=actor.user).id,
			model.claim_expires_at > now,
		)

	if q:
		# **Title and description, which is what §9.4 always said.** It was the title alone
		# until 2026-07-31 — a search that returns plausible rows and silently drops the ones
		# nobody knew to look for.
		backend = subroutine.domain.search.chosen(session, settings=settings)
		words = subroutine.domain.search.terms(q)
		# **One clause, and the composition is the domain's** (`#892`). This was an `or_`
		# written out here and at three other sites, and the `or_` was the defect: it cost the
		# index on both of its sides. `#83` is the half it adds — a comment is where the
		# running record lives (§5.10), and there are more of them on this instance than there
		# are tasks, so a search that skipped them was answering "nothing matches" about the
		# largest thing it could have looked in.
		statement = statement.where(
			subroutine.domain.search.anywhere(
				q,
				identity=model.id,
				columns=(model.title, model.description),
				ref=model.ref,
				entity_type="task",
				backend=backend,
			)
		)

		# **`relevance` exists for this query and only where something can compute it** (`#823`).
		# The `like` backend has no ranking to offer, so the name stays out of the vocabulary
		# there and `parse_order` refuses it by name with the list of what is available —
		# which is the same refusal any unknown sort field gets, rather than a special case.
		#
		# **Gated on the words rather than on `q`** (`#880`). `q` is a raw string and `" "` is
		# truthy with no words in it — `search.matching` has always answered that correctly and
		# the ranking path, added later, indexed `terms[-1]` and returned 500. One question,
		# asked once, and the answer is passed on rather than recomputed.
		if words and backend == subroutine.domain.search.NATIVE:
			ranked = subroutine.domain.ordering.searching(
				sortable,
				terms=words,
				columns=[model.title, model.description],
				carried_on=model.relevance,
				ref=model.ref,
				numbered=subroutine.domain.refs.parse_ref(q),
			)

	# **§9.6's dotted filters, read here because reading them needs the workspace** (`#815`).
	# The names were resolved before this handler ran; what is left is the values, and they take
	# a timezone — §6.5's chain, with the workspace in it, which is why this cannot be a
	# dependency.
	#
	# **`due_before` and `due_after` are resolved here too and no longer applied above**
	# (`#1017`). They are declared on this signature so `query.refuse_unknown` goes on accepting
	# them and OpenAPI goes on publishing them; what reads them is `filtering.ALIASES`, which
	# turns each into the `due_at` comparison it has always been a synonym for. So the two
	# spellings are one implementation rather than two that agree — and the boundary rule that
	# makes `due_at.lt=2026-08-03` exclude the whole of the 3rd now governs both.
	statement = subroutine.api.filters.narrowed(
		statement, dates, session=session, actor=actor, workspace=workspace
	)

	# **`#884`: a name `/v1/meta` publishes is refused for what it *means*, not as unknown.**
	# `relevance` enters the vocabulary only when there is a search to rank, so without one it
	# was answered "not a field this listing can sort by" — about a field the same instance
	# advertises, and which the README tells a client to rely on.
	subroutine.domain.ordering.refuse_ranking_without_a_search(order, searching=ranked is not None)

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
		allowed=sortable if ranked is None else ranked,
		# **A search defaults to its ranking, and that is what makes `#867` useful** (`#823`).
		# Driven on the served instance beforehand: `815` found `#815` and returned it *sixth*,
		# below the fold of an agent's default page — so "a number finds the item" was true and
		# not yet worth anything. An explicit `?order=` still wins, and a listing that is not a
		# search is untouched.
		default=None if ranked is None else (f"-{subroutine.domain.ordering.RELEVANCE}",),
	)


@router.get(
	"/{id_or_ref}",
	summary="Read one task",
	response_model=subroutine.views.Task,
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
		format=format,
		fields=fields,
		available=SELECTABLE,
		entity="task",
		timezone=subroutine.views.reader_zone(session, actor),
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
	settings: subroutine.api.dependencies.SettingsDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Task:
	"""Change a task. Omitted fields are untouched; nulls clear (docs/design.md §8.3)."""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	task = _resolve(session, actor, workspace, id_or_ref)

	supplied = body.model_fields_set
	changes: dict[str, typing.Any] = {
		name: getattr(body, name)
		for name in (
			"title",
			"description",
			"importance",
			"urgency",
			"estimate",
			"due",
			"starts",
			"snooze",
			# **Declared and discarded until now** (`#94`). `PATCH` advertised these three and
			# the handler never forwarded them, so a caller setting a repeat on an existing
			# task got a 200 having changed nothing — a documented field silently dropped,
			# which is what the `unknown_field` refusal exists to argue against. `test_reach`
			# could not see it: it checks a *client* passes the field, not that the handler
			# does anything with it.
			"recurrence",
			"recurrence_anchor",
			"recurrence_trigger",
			"tags",
		)
		if name in supplied
	}

	if "assignee" in supplied:
		# **Null clears it**, which is how work is handed back to nobody in particular — and it
		# clears `assigned_by_id` with it, because an assigner with no assignee names nobody
		# (`#473`). The service owns that pairing; this only says which of the two was meant.
		changes["assignee_id"] = (
			None
			if body.assignee is None
			else subroutine.domain.tasks.assignee_for(session, workspace.id, body.assignee).id
		)

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
		("starts_is_all_day", "starts_is_all_day"),
		("snoozed_is_all_day", "snoozed_is_all_day"),
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
			# **Carried so an automatic lease renewal uses the lease this instance chose**
			# (`#1113`). Without it a renewal would silently fall back to the built-in default
			# while an explicit `claim` honoured the setting — one number meaning two things
			# depending on which write moved it.
			settings=settings,
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



@router.post("/{id_or_ref}/skip", summary="Let one occurrence of a repeat go by")
def skip (
	request: starlette.requests.Request,
	id_or_ref: subroutine.api.schemas.ItemAddress,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Task:
	"""Cancel this occurrence and bring the next one.

	Cancelled rather than done, deliberately: both are finished and both advance the series,
	and *I did not do this* is a different fact about the month from *I did*.
	"""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	task = _resolve(session, actor, workspace, id_or_ref)

	with subroutine.api.concurrency.reporting(lambda: _rendered(session, task)):
		skipped = subroutine.domain.tasks.skip(
			session,
			task,
			expected_version=subroutine.api.concurrency.expected(request),
			actor=actor,
		)

	return _rendered(session, skipped)


#: How many occurrences one request will compute. A rule with no end runs for ever, so a
#: caller asking *when does this happen* has to be given a stopping point — and this one is
#: generous enough that a year of a weekly meeting fits in a single answer.
MAX_OCCURRENCES = 200


@router.get("/{id_or_ref}/occurrences", summary="When does this come round?")
def occurrences (
	id_or_ref: subroutine.api.schemas.ItemAddress,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	until: str | None = fastapi.Query(
		None, description="Stop here. A date, an instant, or an expression like '+3 months'."
	),
	limit: int = fastapi.Query(
		subroutine.domain.recurrence.AHEAD,
		ge=1,
		le=MAX_OCCURRENCES,
		description="At most this many.",
	),
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Occurrences:
	"""Expand a repeating task's rule into the dates it produces.

	§6.7 reserved this, and the decision behind it is why it exists at all: **one occurrence is real
	and the rest are computed**, so *show me every birthday* is a question about a view rather
	than about the backlog. Nothing is stored and nothing is materialised — a `GET` that wrote
	would break a read-only credential and race two concurrent readers.

	**It answers about the series, from whichever end the caller is holding.** A person is
	always looking at an occurrence, because the template is in no listing; asking an
	occurrence when it next comes round is asking its series.
	"""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	task = _resolve(session, actor, workspace, id_or_ref)
	series = subroutine.domain.tasks.series_of(session, task) or task

	if series.recurrence_rule is None:
		raise subroutine.errors.NotFound(
			f"#{task.ref} does not repeat, so there is nothing to expand.",
			hint="Give it a repeat first — 'recurrence' on this task, or the Repeats section "
			"of its form.",
		)

	zone = subroutine.domain.schedule.zone_for(
		user=actor.user, workspace=workspace, instance=subroutine.domain.instances.get(session)
	)
	# **One more than asked for**, which is how `has_more` is answered without a second pass —
	# the same trick every listing here uses, and the reason it matters more: a rule with no
	# end never runs out, so *there are no more* and *I stopped counting* would otherwise be
	# indistinguishable to a caller drawing a month.
	found = subroutine.domain.recurrence.occurrences(
		series.recurrence_rule,
		start=subroutine.domain.tasks.series_start(series),
		timezone=zone,
		until=(
			None
			if until is None
			# **The end of the day when a bare day is named**, which is what "until August"
			# means to somebody drawing a calendar — the alternative drops an occurrence on the
			# last day and reads as an off-by-one nobody can see the cause of.
			else subroutine.domain.schedule.interpret(
				until,
				boundary=subroutine.domain.schedule.Boundary.END,
				timezone=zone,
				now=subroutine.db.types.utcnow(),
				field="until",
			).instant
		),
		limit=limit + 1,
	)

	return subroutine.views.Occurrences(
		rule=series.recurrence_rule,
		description=subroutine.domain.recurrence.describe(
			series.recurrence_rule, anchor=series.recurrence_anchor
		),
		occurrences=found[:limit],
		has_more=len(found) > limit,
	)


@router.post("/{id_or_ref}/claim", summary="Take a task, so nobody else does")
def take (
	request: fastapi.Request,
	id_or_ref: subroutine.api.schemas.ItemAddress,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	minutes: int | None = fastapi.Query(
		None, description="How long the lease lasts. Defaults to the instance's setting."
	),
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Task:
	"""Take a lease on a task, or renew one you already hold.

	A **lease, not a lock** (docs/design.md §14.11): it expires, and an expired one is ignored rather
	than needing anybody to clear it. Workers die mid-task, and a claim that outlived its
	holder would strand the work permanently.

	Claiming something somebody else holds is a `409` naming who and until when. Claiming
	something you already hold renews it, and keeps the instant you first took it.

	`?ready=true` hides work another worker holds, and never hides your own.
	"""

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	task = _resolve(session, actor, workspace, id_or_ref)

	with subroutine.api.concurrency.reporting(lambda: _rendered(session, task)):
		held = subroutine.domain.claims.claim(
			session,
			task,
			minutes=minutes,
			settings=settings,
			expected_version=subroutine.api.concurrency.expected(request),
			actor=actor,
		)

	return _rendered(session, held)


@router.post("/{id_or_ref}/release", summary="Give a task back")
def give_back (
	request: fastapi.Request,
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
	with subroutine.api.concurrency.reporting(lambda: _rendered(session, task)):
		freed = subroutine.domain.claims.release(
			session,
			task,
			expected_version=subroutine.api.concurrency.expected(request),
			actor=actor,
		)

	return _rendered(session, freed)


class Move(subroutine.api.schemas.RequestModel):
	"""Where a task should sit in the tree.

	``parent: null`` promotes it to a top-level task, which is why this is a body rather than
	a query parameter — "no parent" and "unchanged" have to be distinguishable (§8.3), and
	``POST /v1/projects/{key}/move`` learned that the expensive way: an omitted parent read as
	"move to root" and flattened whole subtrees.
	"""

	parent: str | None = None

	#: The version this move is based on. Optional; ``If-Match`` does the same job for a
	#: client that prefers the header, and sending neither means the check was not asked for.
	expected_version: int | None = None

	def requested (self) -> bool:
		"""Report whether the caller actually named a destination."""

		return "parent" in self.model_fields_set


@router.post("/{id_or_ref}/move", summary="Move a task under another, or to the top level")
def move (
	request: fastapi.Request,
	id_or_ref: subroutine.api.schemas.ItemAddress,
	body: Move,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Task:
	"""Re-parent a task, taking its subtask tree with it.

	**The endpoint §8 reserved**, rather than a field on ``PATCH``. Changing a project is a
	field being wrong and the subtree following is an invariant; changing a parent can be
	refused *for being a cycle*, which is a question about the shape of the tree and cannot be
	answered from this row alone.
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
					"top-level task.",
				)
			],
			hint="An omitted 'parent' would have to mean one of those, and guessing which is "
			"how a subtree gets flattened by accident.",
		)

	workspace = subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
	task = _resolve(session, actor, workspace, id_or_ref)
	# Resolved through the same function as the task being moved, so an unknown parent is
	# refused identically and one in a project the caller cannot see is *absent* rather than
	# forbidden — which is §7.3a, and the reason this is not a bare id lookup.
	parent = None if body.parent is None else _resolve(session, actor, workspace, body.parent)

	with subroutine.api.concurrency.reporting(lambda: _rendered(session, task)):
		subroutine.domain.tasks.move(
			session,
			task,
			parent=parent,
			expected_version=subroutine.api.concurrency.expected(request, body.expected_version),
			actor=actor,
		)

	return _rendered(session, task)


@router.post("/{id_or_ref}/restore", summary="Take a task out of the trash")
def unremove (
	request: starlette.requests.Request,
	id_or_ref: subroutine.api.schemas.ItemAddress,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	workspace_id: str | None = fastapi.Query(None, description="Which workspace, by id or slug."),
) -> subroutine.views.Task:
	"""Restore a soft-deleted task (docs/design.md §6.9).

	**The half that made soft delete soft**, and for a long time it did not exist — §6.9
	promised a deleted item was restorable, a `trash_retention_days` setting was declared, and
	`EventAction.RESTORED` has always been in the vocabulary, with nothing clearing
	`deleted_at`. That setting is gone — nothing ever purged the trash, so it was one more
	place the promise was made.

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
	"""Soft-delete a task. It stays recoverable (docs/design.md §6.9).

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
	would confirm that it exists (docs/design.md §7.3a).

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
	allowed: typing.Mapping[str, subroutine.domain.ordering.Sortable],
	default: typing.Sequence[str] | None = None,
) -> typing.Any:
	"""Order, paginate and render a task query.

	Returns ``Any`` because a shaped response is not a ``Collection[Task]`` — its items are
	lines, or addresses, or partial objects. The endpoint still *declares* the collection, so
	the OpenAPI document describes the default that almost every caller receives.

	``allowed`` is a parameter rather than the module constant because the vocabulary is built
	per request: every listing adds ``deferred`` to it (`#877`) and a search adds ``relevance``
	and makes it the default (`#823`). It is **required**, so a listing cannot fall back to a
	vocabulary narrower than the one it validated its own arguments against; ``default`` still
	falls back, because ``None`` there means the ordinary newest-first.
	"""

	sortable = allowed
	fallback = DEFAULT_ORDER if default is None else default

	keys = subroutine.api.pagination.parse_order(
		order,
		allowed=sortable,
		default=fallback,
		tiebreak=subroutine.db.models.work.Task.id,
	)
	# A sort whose expression reads other rows has no Python half, so its value has to arrive
	# on the row for the cursor to name a page boundary (`#569`). Applied from the same
	# expression the ordering was parsed from, never from a second reading of it.
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
		subroutine.views.edges(
			session,
			subroutine.domain.links.edges(
				session,
				actor,
				workspace_id=workspace_id,
				entity_type="task",
				identifiers=[row.id for row in rows],
			),
		)
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
