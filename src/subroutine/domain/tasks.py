"""Creating and editing the thing the whole system exists to hold.

Everything the foundations built meets here: a ref is allocated from the project's
counter, a path is placed in the subtask tree, an event is recorded, and whatever the
description refers to is indexed — all inside one transaction, so a task never exists
without its ref or its history.
"""

import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.bootstrap
import subroutine.domain.capture
import subroutine.domain.durations
import subroutine.domain.events
import subroutine.domain.hierarchy
import subroutine.domain.instances
import subroutine.domain.mentions
import subroutine.domain.patch
import subroutine.domain.refs
import subroutine.domain.schedule
import subroutine.domain.tags
import subroutine.domain.text
import subroutine.domain.users
import subroutine.domain.versions
import subroutine.errors
import subroutine.permissions

#: Status categories that mean a task is finished, and so must carry a ``completed_at``
#: (SPEC.md §10.7 invariant 5). Read from the status row's category rather than its key,
#: because an installation renames and adds statuses freely.
FINISHED_CATEGORIES = frozenset({"done", "cancelled"})

#: SPEC.md §6.10. Enforced here so the message names the field and the limit, rather than
#: arriving as a driver error from PostgreSQL — and arriving not at all on SQLite, which
#: does not enforce VARCHAR lengths.
MAX_TITLE_LENGTH = 512

#: The range §6.3 gives both priority axes, where 5 is highest. There is a CHECK constraint
#: for each on the table, and until 2026-07-29 that was the *only* thing enforcing them — so
#: ``{"importance": 6}`` reached PostgreSQL, violated the constraint and came back as a 500
#: with no field named and nothing a client could act on. Checked here for the reason
#: ``MAX_TITLE_LENGTH`` is: the message should name the field and the range.
PRIORITY_RANGE = (1, 5)


def _priority (value: int | None, *, field: str) -> int | None:
	"""Return a priority axis unchanged, or refuse with the range it has to be inside.

	``None`` passes through: §6.3 is explicit that absence means "not assessed" and is
	distinct from 1, so clearing an axis has to stay expressible.
	"""

	if value is None:
		return value

	low, high = PRIORITY_RANGE

	if low <= value <= high:
		return value

	raise subroutine.errors.ValidationError(
		f"{value} is not a usable {field}.",
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="invalid_field_value",
				message=f"{field.title()} runs from {low} to {high}, where {high} is highest.",
				hint=f"Send a number between {low} and {high}, or null for 'not assessed'.",
			)
		],
	)


def _permitted (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	permission: str,
	*,
	project: subroutine.db.models.project.Project | None = None,
	workspace_id: uuid.UUID | None = None,
) -> None:
	"""Check that an actor may do this, or raise.

	**``actor=None`` is an unauthenticated internal caller and skips the check.** There are
	exactly two: ``domain.bootstrap``, which runs before any principal exists, and the tests.
	Everything reachable from a user — the CLI today, the API at S3-03 — must pass one, and
	``tests/test_actor_discipline.py`` fails the build if any module under ``src`` calls a
	mutating service without doing so.

	That static check is the mechanism, not this default. A missing ``actor=`` here would
	otherwise disable a permission check silently, which is exactly how the slice-2 review
	found the whole layer unenforced: four documents said the check ran and nothing called it.
	"""

	if actor is None:
		return

	scope = workspace_id if project is None else project.workspace_id

	if scope is None:
		raise ValueError("A workspace or a project is needed to check a permission against.")

	subroutine.domain.authorization.authorize(
		session, actor, permission, workspace_id=scope, project=project
	)


def _clean_title (title: str) -> str:
	"""Return a usable task title, or refuse with a reason.

	One rule, applied by both create and update. A task whose title has been blanked is
	not a task anybody can find again, so an update is held to the same standard as a
	create.
	"""

	return subroutine.domain.text.fit(
		subroutine.domain.text.require(title, field="title"),
		field="title",
		limit=MAX_TITLE_LENGTH,
	)


def create (
	session: sqlalchemy.orm.Session,
	*,
	project: subroutine.db.models.project.Project,
	title: str,
	description: str | None = None,
	type_key: str = "task",
	status_key: str | None = None,
	parent: subroutine.db.models.work.Task | None = None,
	assignee_id: uuid.UUID | None = None,
	importance: int | None = None,
	urgency: int | None = None,
	estimate: int | str | None = None,
	due: datetime.datetime | datetime.date | str | None = None,
	due_is_all_day: bool | None = None,
	planned_for: datetime.date | str | None = None,
	start: datetime.datetime | datetime.date | str | None = None,
	start_is_all_day: bool | None = None,
	tags: typing.Sequence[str] | None = None,
	timezone: str | None = None,
	now: datetime.datetime | None = None,
	max_depth: int = subroutine.domain.hierarchy.DEFAULT_MAX_DEPTH,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Task:
	"""Create a task in a project, allocating its ref and recording that it happened.

	Dates are interpreted in ``timezone``, which defaults down §6.5's chain from the actor
	to the workspace to UTC. ``now`` is supplied so that every relative expression in one
	call resolves against a single instant.
	"""

	cleaned_title = _clean_title(title)

	if parent is not None and parent.project_id != project.id:
		raise subroutine.errors.ValidationError(
			"A subtask belongs to the same project as its parent.",
			errors=[
				subroutine.errors.FieldError(
					field="parent_task_id",
					code="invalid_field_value",
					message="That task is in a different project.",
				)
			],
		)

	workspace_id = project.workspace_id

	_permitted(session, actor, subroutine.permissions.TASK_WRITE, project=project)

	item_type = item_type_for(session, workspace_id, type_key)
	status = status_for(session, workspace_id, status_key)

	# Accepts what §6.4's grammar accepts, so `"4h"` works here exactly as `~4h` does in a
	# captured line. Parsed before anything is assigned, like the two priority axes.
	estimated = None if estimate is None else subroutine.domain.durations.parse(estimate)

	zone = _timezone(session, workspace_id, actor=actor, explicit=timezone)
	instant = now or subroutine.db.types.utcnow()

	deadline = subroutine.domain.schedule.interpret(
		due,
		boundary=subroutine.domain.schedule.Boundary.END,
		timezone=zone,
		now=instant,
		all_day=due_is_all_day,
		field="due_at",
	)
	defer = subroutine.domain.schedule.interpret(
		start,
		boundary=subroutine.domain.schedule.Boundary.START,
		timezone=zone,
		now=instant,
		all_day=start_is_all_day,
		field="start_at",
	)
	planned = subroutine.domain.schedule.interpret_day(planned_for, timezone=zone, now=instant)

	subroutine.domain.schedule.check_order(
		start_at=defer.instant,
		start_is_all_day=defer.is_all_day,
		due_at=deadline.instant,
		due_is_all_day=deadline.is_all_day,
		timezone=zone,
	)

	ref = subroutine.domain.refs.allocate(session, workspace_id)

	task = subroutine.db.models.work.Task(
		id=subroutine.db.types.new_uuid(),
		workspace_id=workspace_id,
		project_id=project.id,
		parent_task_id=None if parent is None else parent.id,
		type_id=item_type.id,
		ref=ref,
		title=cleaned_title,
		description=description,
		status_id=status.id,
		assignee_id=assignee_id,
		importance=_priority(importance, field="importance"),
		urgency=_priority(urgency, field="urgency"),
		estimate_minutes=estimated,
		due_at=deadline.instant,
		due_is_all_day=deadline.is_all_day,
		planned_for=planned,
		start_at=defer.instant,
		start_is_all_day=defer.is_all_day,
		# Recorded even when no date was given: recurrence and all-day rendering need to
		# know the zone the task was authored in, and inferring it later is guesswork.
		timezone=zone,
		path="",
		depth=0,
		created_by=None if actor is None else actor.user.id,
	)
	subroutine.domain.hierarchy.place(task, parent, max_depth=max_depth)

	session.add(task)
	session.flush()

	if tags:
		# Applied after the flush, because the join row needs the task's id. `ensure` is what
		# holds §6.2's rule that a name of only digits is a reference and not a tag, however
		# the tag arrived — a captured `#health`, a structured field, or an importer.
		subroutine.domain.tags.apply_to_task(
			session,
			task,
			subroutine.domain.tags.ensure(
				session, workspace_id=workspace_id, names=list(tags)
			),
		)

	subroutine.domain.mentions.synchronize(
		session,
		workspace_id=workspace_id,
		source_type="task",
		source_id=task.id,
		texts=(task.title, task.description),
	)

	subroutine.domain.events.record(
		session,
		workspace_id=workspace_id,
		entity_type="task",
		entity_id=task.id,
		action=subroutine.domain.events.EventAction.CREATED,
		changes={"ref": {"from": None, "to": ref}, "title": {"from": None, "to": task.title}},
		actor=actor,
	)
	session.flush()

	return task


def create_from_text (
	session: sqlalchemy.orm.Session,
	*,
	workspace: subroutine.db.models.identity.Workspace,
	text: str,
	now: datetime.datetime | None = None,
	timezone: str | None = None,
	project: subroutine.db.models.project.Project | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
	**overrides: typing.Any,
) -> tuple[subroutine.db.models.work.Task, subroutine.domain.capture.Capture]:
	"""Create a task from a captured line, resolving the names it mentions.

	Returns the task **and** what was parsed, so a caller can tell the user what it did
	with their sentence rather than making them infer it from the result.

	**Structured fields win over parsed ones** (SPEC.md §6.13): anything in ``overrides``
	replaces what the text said, so a client that wants no magic simply does not send text
	worth parsing. The capture still runs, so the title is still cleaned of tokens the
	caller did supply values for — otherwise passing ``importance`` explicitly would leave
	a stray ``!3`` in the title.

	``project`` is that same rule applied to where the task lands, and it is a **named
	parameter rather than one of the overrides** because this function derives a project of
	its own: an override of that name would have collided with the argument below and raised
	``TypeError`` rather than doing anything useful. Given explicitly it wins over a ``+KEY``
	in the text and over the Inbox default.
	"""

	zone = _timezone(session, workspace.id, actor=actor, explicit=timezone)
	instant = now or subroutine.db.types.utcnow()

	captured = subroutine.domain.capture.parse(text, now=instant, timezone=zone)

	if project is None:
		project = (
			subroutine.domain.bootstrap.inbox_for(session, workspace)
			if captured.project_key is None
			else _project_by_key(session, workspace.id, captured.project_key)
		)

	if project is None:
		raise subroutine.errors.InternalError(
			"This workspace has no Inbox to file a task in.",
			hint="It was interrupted part-way through setup; run 'subroutine init' again.",
		)

	fields: dict[str, typing.Any] = {
		"title": captured.title,
		"due": captured.due,
		"due_is_all_day": captured.due_is_all_day,
		"planned_for": captured.planned_for,
		"start": captured.start,
		"start_is_all_day": captured.start_is_all_day,
		"importance": captured.importance,
		"urgency": captured.urgency,
		# Passed through like every other parsed field rather than assigned after the fact.
		# It used to be written onto the task below, guarded by `"estimate_minutes" not in
		# overrides` — a condition nothing could satisfy, since `create` had no parameter of
		# that name and an override so spelled raised `TypeError` before reaching it. So the
		# rule "structured wins over parsed" was enforced for `estimate` by unreachable code,
		# and now holds by the same mechanism as everything else: `fields.update(overrides)`.
		"estimate": captured.estimate_minutes,
		# **Through `fields`, for exactly the reason above.** These used to be applied after
		# `create` returned, which meant a structured `tags` could not override a captured
		# `#health` — the same shape as `estimate`, one step less broken because nothing
		# guarded it with an unsatisfiable condition. `fields.update(overrides)` is now the
		# single place §6.13's "structured wins over parsed" is decided.
		"tags": captured.tags,
		"assignee_id": (
			None
			if captured.assignee is None
			else _user_by_name(session, workspace.id, captured.assignee).id
		),
	}
	fields.update(overrides)

	task = create(
		session,
		project=project,
		now=instant,
		timezone=zone,
		actor=actor,
		**fields,
	)

	return task, captured


def _project_by_key (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, key: str
) -> subroutine.db.models.project.Project:
	"""Return a project by its key, or say which keys exist.

	``+WEB`` naming nothing is a typo, and filing the task in the Inbox instead would be
	the wrong kind of helpful — the person would not find it where they put it.
	"""

	model = subroutine.db.models.project.Project

	found = session.scalars(
		sqlalchemy.select(model).where(
			model.workspace_id == workspace_id,
			model.key == key,
			model.deleted_at.is_(None),
		)
	).one_or_none()

	if found is not None:
		return found

	available = sorted(
		session.scalars(
			sqlalchemy.select(model.key).where(
				model.workspace_id == workspace_id, model.deleted_at.is_(None)
			)
		)
	)

	raise subroutine.errors.ValidationError(
		f"There is no project called {key!r} here.",
		errors=[
			subroutine.errors.FieldError(
				field="project",
				code="not_found",
				message=f"No project with key {key!r} exists in this workspace.",
				hint=f"Projects here: {', '.join(available)}." if available else None,
			)
		],
	)


def _user_by_name (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, username: str
) -> subroutine.db.models.identity.User:
	"""Return a member of this workspace by username, or say who is here."""

	user = subroutine.db.models.identity.User
	member = subroutine.db.models.identity.WorkspaceMember

	found = session.scalars(
		sqlalchemy.select(user)
		.join(member, member.user_id == user.id)
		.where(
			member.workspace_id == workspace_id,
			user.username_normalized == subroutine.domain.users.normalize(username),
			user.deleted_at.is_(None),
		)
	).one_or_none()

	if found is not None:
		return found

	available = sorted(
		session.scalars(
			sqlalchemy.select(user.username)
			.join(member, member.user_id == user.id)
			.where(member.workspace_id == workspace_id, user.deleted_at.is_(None))
		)
	)

	raise subroutine.errors.ValidationError(
		f"There is nobody called {username!r} in this workspace.",
		errors=[
			subroutine.errors.FieldError(
				field="assignee",
				code="not_found",
				message=f"No member of this workspace is called {username!r}.",
				hint=f"Members here: {', '.join(available)}." if available else None,
			)
		],
	)


def update (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	*,
	title: str = subroutine.domain.patch.UNSET,
	description: str | None = subroutine.domain.patch.UNSET,
	status_key: str = subroutine.domain.patch.UNSET,
	type_key: str = subroutine.domain.patch.UNSET,
	assignee_id: uuid.UUID | None = subroutine.domain.patch.UNSET,
	importance: int | None = subroutine.domain.patch.UNSET,
	urgency: int | None = subroutine.domain.patch.UNSET,
	estimate: int | str | None = subroutine.domain.patch.UNSET,
	due: datetime.datetime | datetime.date | str | None = subroutine.domain.patch.UNSET,
	due_is_all_day: bool | None = None,
	planned_for: datetime.date | str | None = subroutine.domain.patch.UNSET,
	start: datetime.datetime | datetime.date | str | None = subroutine.domain.patch.UNSET,
	start_is_all_day: bool | None = None,
	project: subroutine.db.models.project.Project = subroutine.domain.patch.UNSET,
	tags: typing.Sequence[str] | None = subroutine.domain.patch.UNSET,
	timezone: str | None = None,
	now: datetime.datetime | None = None,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Task:
	"""Change a task, recording only what actually changed.

	Anything left at ``subroutine.domain.patch.UNSET`` is untouched; passing ``None`` clears the field. An update
	that changes nothing writes no event, so the change feed stays a record of changes
	rather than of requests.

	**Everything is validated before anything is assigned.** A rejected update must leave
	the task exactly as it was: the caller holds a live session it may still commit, so a
	half-applied change that raised on the way through would be committed silently along
	with whatever else that transaction was doing.
	"""

	# Permission first, before anything is even read: a caller who may not touch this task
	# should not be able to learn from the error message whether their new title was valid.
	# The version check follows it, for the same reason — a stranger should not learn what
	# version a task is at (SPEC.md §8.9).
	_permitted(
		session,
		actor,
		subroutine.permissions.TASK_WRITE,
		project=session.get(subroutine.db.models.project.Project, task.project_id),
		workspace_id=task.workspace_id,
	)
	subroutine.domain.versions.require(task, expected_version, noun="This task")

	# Validation pass. Nothing below this point may raise.
	cleaned_title: typing.Any = subroutine.domain.patch.UNSET if title is subroutine.domain.patch.UNSET else _clean_title(title)
	status: typing.Any = (
		subroutine.domain.patch.UNSET if status_key is subroutine.domain.patch.UNSET else status_for(session, task.workspace_id, status_key)
	)

	# **What something is becomes clear after it has been looked at** (`#42`). A type was
	# settable at creation and nowhere else, so a task filed as a task could never become a
	# bug — and reclassifying is the normal case, not an edge one. The status is deliberately
	# *not* dragged along with it: a type carries a default status set at creation, and moving
	# a half-finished bug back to "open" because its type changed would be a second, unasked
	# change wearing the first one's clothes.
	item_type: typing.Any = (
		subroutine.domain.patch.UNSET
		if type_key is subroutine.domain.patch.UNSET
		else item_type_for(session, task.workspace_id, type_key)
	)

	# Both axes are range-checked *here*, in the pass that may raise, rather than beside the
	# assignment below. A refusal after a partial assignment would leave the caller holding a
	# session it may still commit, with half the change in it.
	cleaned_importance: typing.Any = (
		subroutine.domain.patch.UNSET
		if importance is subroutine.domain.patch.UNSET
		else _priority(importance, field="importance")
	)
	cleaned_urgency: typing.Any = (
		subroutine.domain.patch.UNSET
		if urgency is subroutine.domain.patch.UNSET
		else _priority(urgency, field="urgency")
	)

	# Same reasoning, and the same pass: `"90x"` must be refused before the task is touched.
	# Written as a guard rather than the ternary above because `durations.parse` has no
	# null case — ``None`` means clear the estimate, so an over-optimistic guess can be
	# withdrawn rather than only replaced, and it must reach the assignment unparsed.
	cleaned_estimate: typing.Any = estimate

	if estimate is not subroutine.domain.patch.UNSET and estimate is not None:
		cleaned_estimate = subroutine.domain.durations.parse(estimate)

	zone = _timezone(
		session, task.workspace_id, actor=actor, explicit=timezone or task.timezone
	)
	instant = now or subroutine.db.types.utcnow()

	deadline: typing.Any = subroutine.domain.patch.UNSET if due is subroutine.domain.patch.UNSET else subroutine.domain.schedule.interpret(
		due,
		boundary=subroutine.domain.schedule.Boundary.END,
		timezone=zone,
		now=instant,
		all_day=due_is_all_day,
		field="due_at",
	)
	defer: typing.Any = subroutine.domain.patch.UNSET if start is subroutine.domain.patch.UNSET else subroutine.domain.schedule.interpret(
		start,
		boundary=subroutine.domain.schedule.Boundary.START,
		timezone=zone,
		now=instant,
		all_day=start_is_all_day,
		field="start_at",
	)
	planned: typing.Any = (
		subroutine.domain.patch.UNSET
		if planned_for is subroutine.domain.patch.UNSET
		else subroutine.domain.schedule.interpret_day(planned_for, timezone=zone, now=instant)
	)

	# Invariant 8 is checked against what the task *will* look like, not against what was
	# passed in: moving only the deadline still has to be consistent with the defer that is
	# already there, and the caller did not mention it.
	subroutine.domain.schedule.check_order(
		start_at=task.start_at if defer is subroutine.domain.patch.UNSET else defer.instant,
		start_is_all_day=task.start_is_all_day if defer is subroutine.domain.patch.UNSET else defer.is_all_day,
		due_at=task.due_at if deadline is subroutine.domain.patch.UNSET else deadline.instant,
		due_is_all_day=task.due_is_all_day if deadline is subroutine.domain.patch.UNSET else deadline.is_all_day,
		timezone=zone,
	)

	# **The move is validated here and applied below, like every other field**, even though
	# it writes more than one row. From a caller's side "this is in the wrong project" is a
	# field being wrong; the subtree following is an *invariant being maintained*, exactly as
	# `completed_at` follows the status two blocks down. SPEC.md reserves
	# `POST /v1/tasks/{id}/move` for re-parenting (#44), which genuinely needs a cycle check
	# and a body of its own.
	moving = project is not subroutine.domain.patch.UNSET and project.id != task.project_id
	descendants: list[subroutine.db.models.work.Task] = []

	if moving:
		if project.workspace_id != task.workspace_id:
			# #30, and much larger: a cross-workspace move rewrites the ref's tenancy, which
			# §6.2 spent real care making stable. Refused by name rather than half-done.
			raise subroutine.errors.ValidationError(
				"A task cannot be moved to a project in another workspace.",
				errors=[
					subroutine.errors.FieldError(
						field="project",
						code="invalid_field_value",
						message=f"{project.key!r} is in a different workspace.",
						hint="Move it to a project in the same workspace, or create it there.",
					)
				],
			)

		# **Both ends, and the new one is checked in the pass that may raise.** A caller who
		# may write here but not there must not be able to move work out of their reach —
		# and must not learn from a half-applied change that the target exists.
		_permitted(session, actor, subroutine.permissions.TASK_WRITE, project=project)

		if task.parent_task_id is not None:
			# The invariant runs both ways: `create` refuses a subtask in a different project
			# from its parent, so moving a child alone would break it from the other side.
			# Naming the parent is what makes this actionable rather than a wall.
			raise subroutine.errors.ValidationError(
				"A subtask belongs to the same project as its parent.",
				errors=[
					subroutine.errors.FieldError(
						field="project",
						code="invalid_field_value",
						message="This task is part of another task, which decides its project.",
						hint="Move the parent instead — its parts go with it.",
					)
				],
			)

		descendants = list(
			session.scalars(
				sqlalchemy.select(subroutine.db.models.work.Task).where(
					subroutine.domain.hierarchy.subtree(subroutine.db.models.work.Task, task),
					subroutine.db.models.work.Task.id != task.id,
					subroutine.db.models.work.Task.deleted_at.is_(None),
				)
			)
		)

	# Resolved in the pass that may raise, because `ensure` refuses a name that is really a
	# reference (§6.2) and creates rows for the rest — a refusal after the first tag was
	# created would leave a tag nobody asked for.
	# **`None` clears, exactly as `[]` does.** §8.3's null means "clear this", and tags are
	# clearable — unlike a title, which is why the two nulls get different answers. Sending
	# `null` used to reach `list(None)` and 500.
	wanted_tags: typing.Any = (
		subroutine.domain.patch.UNSET
		if tags is subroutine.domain.patch.UNSET
		else subroutine.domain.tags.ensure(
			session, workspace_id=task.workspace_id, names=list(tags or ())
		)
	)

	before = _snapshot(session, task)
	touches_content = False

	if cleaned_title is not subroutine.domain.patch.UNSET:
		task.title = cleaned_title
		touches_content = True

	if description is not subroutine.domain.patch.UNSET:
		task.description = description
		touches_content = True

	if item_type is not subroutine.domain.patch.UNSET:
		task.type_id = item_type.id
		touches_content = True

	if status is not subroutine.domain.patch.UNSET:
		task.status_id = status.id
		touches_content = True

		# SPEC.md §10.7 invariant 5: `completed_at` is non-null exactly when the status
		# category is `done` or `cancelled`. Set here rather than by a database trigger,
		# because the category lives on the status row and an installation may rename or
		# add statuses freely.
		task.completed_at = (
			subroutine.db.types.utcnow() if status.category in FINISHED_CATEGORIES else None
		)

	if moving:
		task.project_id = project.id

		# **The parts go with it, because the invariant says they must.** Their own version
		# is bumped: a client holding one and sending it back under §8.9 has a stale view of
		# where that task lives, which is exactly what the check exists to catch.
		for descendant in descendants:
			descendant.project_id = project.id
			descendant.version += 1

	if wanted_tags is not subroutine.domain.patch.UNSET:
		# **Replaces, so an empty list clears.** Every other field on a PATCH is assigned
		# rather than merged, and a `tags` that merged would be the only one a caller could
		# not use to remove anything — which is how a mistyped tag became permanent.
		subroutine.domain.tags.set_on_task(session, task, wanted_tags)
		touches_content = True

	if assignee_id is not subroutine.domain.patch.UNSET:
		task.assignee_id = assignee_id

	if importance is not subroutine.domain.patch.UNSET:
		task.importance = cleaned_importance

	if urgency is not subroutine.domain.patch.UNSET:
		task.urgency = cleaned_urgency

	if estimate is not subroutine.domain.patch.UNSET:
		task.estimate_minutes = cleaned_estimate

	if deadline is not subroutine.domain.patch.UNSET:
		task.due_at = deadline.instant
		task.due_is_all_day = deadline.is_all_day

	if planned is not subroutine.domain.patch.UNSET:
		task.planned_for = planned

	if defer is not subroutine.domain.patch.UNSET:
		task.start_at = defer.instant
		task.start_is_all_day = defer.is_all_day

	changes = subroutine.domain.events.changes_between(before, _snapshot(session, task))

	if not changes:
		return task

	# `updated_at` moves on any write; `content_updated_at` moves only when the *meaning*
	# changed. That distinction is what lets a verification know whether it is stale, and
	# stops a repositioning from invalidating evidence (SPEC.md §6.1).
	if touches_content:
		task.content_updated_at = subroutine.db.types.utcnow()

	task.version += 1
	task.updated_by = None if actor is None else actor.user.id
	session.flush()

	if title is not subroutine.domain.patch.UNSET or description is not subroutine.domain.patch.UNSET:
		subroutine.domain.mentions.synchronize(
			session,
			workspace_id=task.workspace_id,
			source_type="task",
			source_id=task.id,
			texts=(task.title, task.description),
		)

	subroutine.domain.events.record(
		session,
		workspace_id=task.workspace_id,
		entity_type="task",
		entity_id=task.id,
		action=subroutine.domain.events.EventAction.UPDATED,
		changes=changes,
		actor=actor,
	)
	session.flush()

	return task


def finished_status_key (session: sqlalchemy.orm.Session, workspace_id: uuid.UUID) -> str:
	"""Return the key of a status meaning finished, whatever this workspace calls it.

	Statuses are data — an installation renames and adds them freely (§5.5) — so nothing may
	hard-code ``"done"``. This asks for the first status in the ``done`` *category*, which is
	what keeps "mark it finished" working after somebody renames it to "Shipped".
	"""

	model = subroutine.db.models.vocabulary.Status

	found = session.scalars(
		sqlalchemy.select(model)
		.where(
			model.workspace_id == workspace_id,
			model.entity_type == "task",
			model.category == "done",
		)
		.order_by(model.position)
	).first()

	if found is None:
		raise subroutine.errors.InternalError(
			"This workspace has no status meaning 'done'.",
			hint="Its vocabulary is incomplete; restore it, or start again from an empty "
			"database.",
		)

	return found.key


def complete (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	*,
	now: datetime.datetime | None = None,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Task:
	"""Mark a task finished, in whatever this workspace calls its finished status.

	A thin wrapper over :func:`update`, and deliberately so: completion is a status change
	and giving it a second code path would be how the two come to disagree about events,
	permissions or the ``completed_at`` invariant. What it adds is not having to know the
	installation's vocabulary in order to say "done".
	"""

	return update(
		session,
		task,
		status_key=finished_status_key(session, task.workspace_id),
		now=now,
		expected_version=expected_version,
		actor=actor,
	)


def delete (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	*,
	now: datetime.datetime | None = None,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Task:
	"""Move a task to the trash, where it stays recoverable (SPEC.md §6.9).

	Soft, always: ``deleted_at`` is set and the row remains. Deleting twice is not an error
	and does not move the timestamp — when something was thrown away is a fact worth not
	overwriting, and a caller retrying a request should not change it.

	Needs ``task:delete`` rather than ``task:write``. A `member` can close and cancel, which
	covers the ordinary reasons for wanting something gone; deletion is for `admin` and
	`owner` (§7.2).
	"""

	_permitted(
		session,
		actor,
		subroutine.permissions.TASK_DELETE,
		project=session.get(subroutine.db.models.project.Project, task.project_id),
		workspace_id=task.workspace_id,
	)
	subroutine.domain.versions.require(task, expected_version, noun="This task")

	if task.deleted_at is not None:
		return task

	task.deleted_at = now if now is not None else subroutine.db.types.utcnow()

	# **The version moves, because a delete is a change.** §8.9's promise is that a change is
	# based on the state you read, and a version that stands still across a soft delete breaks
	# it silently: read at v3, somebody trashes it, and `expected_version: 3` still passes — so
	# you edit a deleted item believing nothing happened. `projects.delete` did this and the
	# other two did not, which is what kept the gap invisible.
	task.version += 1
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=task.workspace_id,
		entity_type="task",
		entity_id=task.id,
		action=subroutine.domain.events.EventAction.DELETED,
		actor=actor,
	)
	session.flush()

	return task


def restore (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	*,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Task:
	"""Take a task back out of the trash (SPEC.md §6.9).

	**The half of soft delete that made it soft**, and it did not exist until `#140`. §6.9 says
	a deleted item is "restorable for a configurable retention period", ``trash_retention_days``
	has been a setting since the beginning, and ``EventAction.RESTORED`` has been in the
	vocabulary just as long — with nothing anywhere setting ``deleted_at`` back to null. So the
	promise was made in three places and kept in none, and "delete" meant "gone" whatever the
	documentation said.

	It matters more than an undo usually does, because of what deletion is *for* here: the
	commonest reason to remove something from a to-do list is that it was added by mistake, and
	the second commonest is that the wrong one was removed.

	The same permission as deleting, deliberately. Putting something back is the same authority
	over the same row, and a caller who could restore but not delete could resurrect work
	somebody with more rights had thrown away.

	Restoring twice is not an error, symmetrically with deleting twice — and neither moves a
	timestamp that is already where it belongs.
	"""

	_permitted(
		session,
		actor,
		subroutine.permissions.TASK_DELETE,
		project=session.get(subroutine.db.models.project.Project, task.project_id),
		workspace_id=task.workspace_id,
	)
	subroutine.domain.versions.require(task, expected_version, noun="This task")

	if task.deleted_at is None:
		return task

	task.deleted_at = None

	# For `delete`'s reason: a restore is a change, and §8.9's guard compares a number that has
	# to move or it silently passes for a caller reading stale state.
	task.version += 1
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=task.workspace_id,
		entity_type="task",
		entity_id=task.id,
		action=subroutine.domain.events.EventAction.RESTORED,
		actor=actor,
	)
	session.flush()

	return task


def _snapshot (
	session: sqlalchemy.orm.Session, task: subroutine.db.models.work.Task
) -> dict[str, typing.Any]:
	"""Return the fields an update may change, for comparison afterwards.

	**Every field ``update`` can write belongs here, and a missing one is silent.** The
	comparison decides both what the event says *and whether one is written at all* — an
	update whose only change is a field this dict forgets produces no event, so §10.7's
	invariant 9 fails without anything failing. ``urgency`` was missing from 2026-07-29,
	when §6.3's second priority axis was given a column, a constraint, a sort key and a
	compact-line cell, and not a line here: setting it bumped ``version`` and left no
	trace. Found on 2026-07-30 by building the endpoint that reads this table, which is
	the whole argument for building readers early.

	``tests/test_services.py`` now changes each of these in turn and insists an event
	names it, so the next field added is caught by a test rather than by a reader.
	"""

	return {
		"title": task.title,
		"completed_at": task.completed_at,
		"project_id": task.project_id,
		# **Read rather than taken off the row**, which is why this needs a session at all.
		# Tags live in a join table, so there is no attribute to compare; a sorted list of
		# names is what makes "did the tags change" a value comparison.
		"tags": subroutine.domain.tags.names_on_task(session, task),
		"description": task.description,
		"status_id": task.status_id,
		"type_id": task.type_id,
		"assignee_id": task.assignee_id,
		"importance": task.importance,
		"urgency": task.urgency,
		"estimate_minutes": task.estimate_minutes,
		"due_at": task.due_at,
		"due_is_all_day": task.due_is_all_day,
		"planned_for": task.planned_for,
		"start_at": task.start_at,
		"start_is_all_day": task.start_is_all_day,
	}


def _timezone (
	session: sqlalchemy.orm.Session,
	workspace_id: uuid.UUID,
	*,
	actor: subroutine.domain.authentication.Principal | None,
	explicit: str | None,
) -> str:
	"""Return the timezone this task's dates are read in, per SPEC.md §6.5's chain.

	The workspace and the instance are fetched only when the answer is not already settled,
	so the common path — a person with a timezone, editing their own tasks — costs no query.
	"""

	if explicit:
		return explicit

	if actor is not None and actor.user.timezone:
		return actor.user.timezone

	workspace = session.get(subroutine.db.models.identity.Workspace, workspace_id)

	if workspace is not None and workspace.timezone:
		return workspace.timezone

	return subroutine.domain.schedule.zone_for(
		instance=subroutine.domain.instances.get(session)
	)


def item_type_for (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, key: str
) -> subroutine.db.models.vocabulary.ItemType:
	"""Return a task type by key, or list the ones this workspace has."""

	model = subroutine.db.models.vocabulary.ItemType

	found = session.scalars(
		sqlalchemy.select(model).where(
			model.workspace_id == workspace_id, model.entity_type == "task", model.key == key
		)
	).one_or_none()

	if found is not None:
		return found

	available = sorted(
		session.scalars(
			sqlalchemy.select(model.key).where(
				model.workspace_id == workspace_id, model.entity_type == "task"
			)
		)
	)

	raise subroutine.errors.ValidationError(
		f"There is no task type called {key!r} here.",
		errors=[
			subroutine.errors.FieldError(
				field="type",
				code="not_found",
				message=f"No task type with key {key!r} exists in this workspace.",
				hint=f"Types here: {', '.join(available)}." if available else None,
			)
		],
	)


def status_for (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, key: str | None
) -> subroutine.db.models.vocabulary.Status:
	"""Return a task status by key, or the workspace's default when none is named."""

	model = subroutine.db.models.vocabulary.Status

	statement = sqlalchemy.select(model).where(
		model.workspace_id == workspace_id, model.entity_type == "task"
	)

	if key is None:
		found = session.scalars(
			statement.where(model.is_default.is_(True)).order_by(model.position)
		).first()

	else:
		found = session.scalars(statement.where(model.key == key)).one_or_none()

	if found is not None:
		return found

	available = sorted(
		session.scalars(
			sqlalchemy.select(model.key).where(
				model.workspace_id == workspace_id, model.entity_type == "task"
			)
		)
	)

	raise subroutine.errors.ValidationError(
		"This workspace has no default task status."
		if key is None
		else f"There is no task status called {key!r} here.",
		code="invalid_status",
		errors=[
			subroutine.errors.FieldError(
				field="status",
				code="not_found",
				message=f"No task status with key {key!r} exists in this workspace."
				if key is not None
				else "No task status is marked as the default.",
				hint=f"Statuses here: {', '.join(available)}." if available else None,
			)
		],
	)
