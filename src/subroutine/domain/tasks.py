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

import subroutine.db.mixins
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
import subroutine.domain.recurrence
import subroutine.domain.refs
import subroutine.domain.schedule
import subroutine.domain.selection
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


def _assigner (
	actor: subroutine.domain.authentication.Principal | None,
	assignee_id: uuid.UUID | None,
) -> uuid.UUID | None:
	"""Return who to record as having assigned this, given who is acting (`#477`).

	Null when nobody is assigned, because an assigner with no assignee names nobody — and null
	when there is no actor, which is an internal caller with no principal to credit. Neither is
	a gap to be filled in later: an unattributed assignment is better than one attributed to
	whoever happened to be convenient.
	"""

	if assignee_id is None or actor is None:
		return None

	return actor.user.id


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


#: What a repeat defaults to when a caller gives a rule and says nothing else.
#:
#: **``completion`` rather than ``time``**, because the caller is filing a *task*: something
#: they intend to finish, which is what puts an occurrence in the list ahead of time. A series
#: nobody ever closes is the other kind and has to be asked for.
DEFAULT_TRIGGER = "completion"
DEFAULT_ANCHOR = "schedule"


def _repeat (
	rule: str | None,
	*,
	anchor: str | None,
	trigger: str | None,
) -> subroutine.domain.recurrence.Repeat | None:
	"""Read a repeat and the two things that qualify it, or refuse the combination.

	Returns ``None`` when no rule was given, which is every ordinary task.
	"""

	if rule is None:
		return None

	read = subroutine.domain.recurrence.rule(rule)

	chosen_anchor = anchor or DEFAULT_ANCHOR
	chosen_trigger = trigger or DEFAULT_TRIGGER

	for value, allowed, field in (
		(chosen_anchor, subroutine.db.mixins.RECURRENCE_ANCHORS, "recurrence_anchor"),
		(chosen_trigger, subroutine.db.mixins.RECURRENCE_TRIGGERS, "recurrence_trigger"),
	):
		if value not in allowed:
			raise subroutine.errors.ValidationError(
				f"{value!r} is not a way for something to repeat.",
				errors=[
					subroutine.errors.FieldError(
						field=field,
						code="invalid_field_value",
						message=f"{field} is one of {', '.join(allowed)}.",
					)
				],
			)

	# **The one combination that cannot mean anything** (`#915`). A `completion` anchor
	# measures the next date from the instant the last one was finished, and a `time` series
	# is never finished — so there is nothing for it to measure from. Refused here rather
	# than by a CHECK constraint, because the message has to name which of the two to change.
	if chosen_trigger == "time" and chosen_anchor == "completion":
		raise subroutine.errors.ValidationError(
			"A repeat that nobody finishes cannot be measured from when it was finished.",
			code="invalid_field_value",
			hint="Use the 'schedule' anchor with a 'time' trigger, or the 'completion' "
			"trigger if this is work you close.",
			errors=[
				subroutine.errors.FieldError(
					field="recurrence_anchor",
					code="invalid_field_value",
					message="'completion' needs a trigger of 'completion'.",
				)
			],
		)

	# **Not built yet, and refused by name rather than accepted and ignored** (`#94`). A
	# `time` series materialises nothing, so until a date-ranged view expands it — the agenda
	# and `#916`'s feed — filing one would store a rule that is visible nowhere at all. That
	# is the silence §6.13 rule 1 forbids, so it says so instead.
	if chosen_trigger == "time":
		raise subroutine.errors.ValidationError(
			"A repeat that happens whether or not you act is not built yet.",
			code="invalid_field_value",
			hint="Repeats that you finish work exactly as described; this is the calendar "
			"half and lands with the calendar.",
			errors=[
				subroutine.errors.FieldError(
					field="recurrence_trigger",
					code="invalid_field_value",
					message="Only 'completion' is built. See #94.",
				)
			],
		)

	return subroutine.domain.recurrence.Repeat(
		rule=read.rule, text=read.text, anchor=chosen_anchor, trigger=chosen_trigger
	)



def series_start (
	template: subroutine.db.models.work.Task,
) -> datetime.datetime:
	"""Return the instant a repeating series is anchored to, or refuse a rule with no date.

	**A deadline wins over a start**, because "the 30th of every month" is overwhelmingly a
	thing that is *due* then — and where both are set the gap between them is preserved, so
	an occurrence that starts on the Monday and is due on the Friday keeps its four days.

	A rule with no date at all is refused rather than anchored to the moment it was filed:
	"every month" from an arbitrary instant means the day somebody happened to type it, which
	is a date they did not choose and will not remember choosing.
	"""

	rule = template.recurrence_rule or ""
	anchor = template.due_at or template.starts_at

	# **A rule that names its own day needs no date beside it** (`#94`, found by driving).
	# "On the 30th of every month" was refused for not saying when, which is exactly what it
	# does say — and the refusal arrived on the phrasing the brief was written in. Anchored on
	# the moment it was filed, which for a self-anchoring rule invents nothing: the first
	# occurrence is the next 30th either way.
	if anchor is None and subroutine.domain.recurrence.names_its_own_day(rule):
		return template.created_at or subroutine.db.types.utcnow()

	if anchor is None:
		raise subroutine.errors.ValidationError(
			"A repeat needs a date to repeat from.",
			code="invalid_field_value",
			hint="Give it a deadline or a start — 'every month' says how often, not when.",
			errors=[
				subroutine.errors.FieldError(
					field="recurrence",
					code="invalid_field_value",
					message="Send 'due' or 'starts' alongside the repeat.",
				)
			],
		)

	return anchor


def materialise (
	session: sqlalchemy.orm.Session,
	template: subroutine.db.models.work.Task,
	*,
	now: datetime.datetime,
	after: datetime.datetime | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Task | None:
	"""Bring the next occurrence of a series into being, or report that it is spent.

	``after`` is the cursor — the occurrence just finished — and ``None`` asks for the first,
	which is what creation wants. **The anchor and the cursor are separate arguments for the
	reason ``domain.recurrence`` keeps them apart**: ``COUNT`` and ``UNTIL`` are measured from
	the series' own start, so passing the cursor as the anchor spends the count on occurrences
	nobody asked about.

	Returns ``None`` when ``COUNT`` is exhausted or ``UNTIL`` has passed, and marks the
	template finished on the way out — a series with nothing left is over, and leaving the
	template open would be a rule that can never fire again sitting in the workspace for ever.
	"""

	if template.recurrence_rule is None:
		raise ValueError("materialise() was given a task that carries no repeat.")

	anchor = series_start(template)
	zone = template.timezone or subroutine.domain.schedule.DEFAULT_TIMEZONE

	occurrence: datetime.datetime | None

	if after is None:
		# The first one, and inclusive: a series starting on the 30th has its first occurrence
		# on the 30th, and asking for what comes *after* it would skip a month.
		found = subroutine.domain.recurrence.occurrences(
			template.recurrence_rule, start=anchor, timezone=zone, limit=1
		)
		occurrence = found[0] if found else None

	elif template.recurrence_anchor == "completion":
		# **Measured from when the work actually happened** — water the plants fourteen days
		# after you last did, not fourteen days after you meant to. The series is re-anchored
		# on the completion instant, which is what that anchor means.
		occurrence = subroutine.domain.recurrence.following(
			template.recurrence_rule, start=now, after=now, timezone=zone
		)

	else:
		# **The grid holds however late anybody was — but the answer still has to be ahead.**
		# The cursor clears both the occurrence just finished *and* the moment it was finished,
		# because a daily series closed a month late would otherwise mint yesterday's, then
		# the day before's, one overdue row per press until the backlog caught up. "The 1st is
		# the 1st" is about which dates the series falls on, not about handing somebody a
		# fortnight of arrears.
		occurrence = subroutine.domain.recurrence.following(
			template.recurrence_rule, start=anchor, after=max(after, now), timezone=zone
		)

	if occurrence is None:
		# Finished rather than deleted: what repeated, and until when, is a fact worth keeping.
		if template.completed_at is None:
			complete(session, template, now=now, actor=actor)

		return None

	shift = occurrence - anchor

	instance = subroutine.db.models.work.Task(
		id=subroutine.db.types.new_uuid(),
		workspace_id=template.workspace_id,
		project_id=template.project_id,
		parent_task_id=template.parent_task_id,
		type_id=template.type_id,
		ref=subroutine.domain.refs.allocate(session, template.workspace_id),
		title=template.title,
		description=template.description,
		status_id=status_for(session, template.workspace_id, None).id,
		assignee_id=template.assignee_id,
		assigned_by_id=template.assigned_by_id,
		importance=template.importance,
		urgency=template.urgency,
		estimate_minutes=template.estimate_minutes,
		due_at=None if template.due_at is None else template.due_at + shift,
		due_is_all_day=template.due_is_all_day,
		starts_at=None if template.starts_at is None else template.starts_at + shift,
		starts_is_all_day=template.starts_is_all_day,
		# **Deliberately not carried.** A snooze is somebody saying "not yet" about one
		# occurrence; repeating it would hide every future one for the same reason, which
		# nobody asked for.
		snoozed_until=None,
		snoozed_is_all_day=False,
		timezone=template.timezone,
		recurrence_template_id=template.id,
		occurrence_at=occurrence,
		is_template=False,
		path="",
		depth=0,
		created_by=None if actor is None else actor.user.id,
	)
	subroutine.domain.hierarchy.place(instance, None, max_depth=subroutine.domain.hierarchy.DEFAULT_MAX_DEPTH)

	session.add(instance)
	session.flush()

	subroutine.domain.mentions.synchronize(
		session,
		workspace_id=instance.workspace_id,
		source_type="task",
		source_id=instance.id,
		texts=(instance.title, instance.description),
	)

	subroutine.domain.events.record(
		session,
		workspace_id=instance.workspace_id,
		entity_type="task",
		entity_id=instance.id,
		action=subroutine.domain.events.EventAction.CREATED,
		changes={
			"ref": {"from": None, "to": instance.ref},
			"title": {"from": None, "to": instance.title},
		},
		actor=actor,
	)
	session.flush()

	return instance


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
	starts: datetime.datetime | datetime.date | str | None = None,
	starts_is_all_day: bool | None = None,
	snooze: datetime.datetime | datetime.date | str | None = None,
	snoozed_is_all_day: bool | None = None,
	recurrence: str | None = None,
	recurrence_anchor: str | None = None,
	recurrence_trigger: str | None = None,
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

	``starts`` and ``snooze`` are the two halves of what was one ``start`` argument (`#854`),
	and they are deliberately spelled nothing like each other: one says when the work begins
	and the other hides the row until it passes. A caller that meant the second and reached
	for the first now gets an item on the list rather than an item that disappeared.
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

	repeat = _repeat(recurrence, anchor=recurrence_anchor, trigger=recurrence_trigger)

	deadline = subroutine.domain.schedule.interpret(
		due,
		boundary=subroutine.domain.schedule.Boundary.END,
		timezone=zone,
		now=instant,
		all_day=due_is_all_day,
		field="due_at",
	)
	defer = subroutine.domain.schedule.interpret(
		snooze,
		boundary=subroutine.domain.schedule.Boundary.START,
		timezone=zone,
		now=instant,
		all_day=snoozed_is_all_day,
		field="snoozed_until",
	)
	beginning = subroutine.domain.schedule.interpret(
		starts,
		boundary=subroutine.domain.schedule.Boundary.START,
		timezone=zone,
		now=instant,
		all_day=starts_is_all_day,
		field="starts_at",
	)

	# **The defer only** — `schedule._ORDERED_BEFORE_DUE` carries why `starts_at` is exempt.
	subroutine.domain.schedule.check_order(
		instant=defer.instant,
		is_all_day=defer.is_all_day,
		due_at=deadline.instant,
		due_is_all_day=deadline.is_all_day,
		timezone=zone,
		field="snoozed_until",
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
		assigned_by_id=_assigner(actor, assignee_id),
		importance=_priority(importance, field="importance"),
		urgency=_priority(urgency, field="urgency"),
		estimate_minutes=estimated,
		due_at=deadline.instant,
		due_is_all_day=deadline.is_all_day,
		starts_at=beginning.instant,
		starts_is_all_day=beginning.is_all_day,
		snoozed_until=defer.instant,
		snoozed_is_all_day=defer.is_all_day,
		# **The row a caller creates *is* the template when they gave a rule** (§6.7), rather
		# than a third thing built beside it: it already carries the title, the project, the
		# dates and the priorities somebody typed, which is exactly what each occurrence
		# inherits. The first instance is minted from it below.
		recurrence_rule=None if repeat is None else repeat.rule,
		recurrence_text=None if repeat is None else repeat.text,
		recurrence_anchor=None if repeat is None else repeat.anchor,
		recurrence_trigger=None if repeat is None else repeat.trigger,
		is_template=repeat is not None,
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
		subroutine.domain.tags.apply_to(
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

	if repeat is None:
		return task

	# **The instance is what the caller gets back**, because it is the thing they act on
	# (§6.7). The template is addressable by its own ref and readable, and is excluded from
	# every listing — it is a rule, not work.
	first = materialise(session, task, now=instant, actor=actor)

	if first is None:
		# **Refused rather than answered with a finished template.** A rule whose every date
		# is already behind us — `UNTIL` in the past — is a mistake somebody wants told about
		# now, not one they discover by the item never appearing.
		raise subroutine.errors.ValidationError(
			"That repeat names no dates that have not already passed.",
			code="invalid_field_value",
			hint="Check the UNTIL or COUNT on the rule, and the date it repeats from.",
			errors=[
				subroutine.errors.FieldError(
					field="recurrence",
					code="invalid_field_value",
					message="A repeat has to have at least one occurrence still to come.",
				)
			],
		)

	return first


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
		# **The default is asked for rather than assumed, and that is the whole of `#374`.**
		# This reached for the Inbox itself, which was a second copy of a rule `selection` also
		# holds — and the two came apart the moment `#369` taught one of them that a bounded
		# credential cannot file there. The captured line is the path a person and an agent
		# both actually use, so the copy that stayed wrong was the one that mattered.
		# `actor=None` is the unauthenticated internal caller — bootstrap and the tests — which
		# holds no credential and so has no scope to be narrowed by (§12.1a). The Inbox is what
		# `selection` would answer for it anyway; asking would just mean passing a principal
		# that does not exist.
		project = (
			(
				subroutine.domain.selection.project(session, actor, workspace, None)
				if actor is not None
				else subroutine.domain.bootstrap.inbox_for(session, workspace)
			)
			if captured.project_key is None
			else _project_by_key(session, workspace.id, captured.project_key)
		)

	if project is None:
		raise subroutine.errors.NotFound(
			f"There is no project {captured.project_key!r} in this workspace.",
			errors=[
				subroutine.errors.FieldError(
					field="text",
					code="not_found",
					message=f"The captured line files this under "
					f"{captured.project_key!r}, and no project here answers to it.",
					hint="Use a project key that exists, or leave the +KEY off to file it "
					"where this credential ordinarily would.",
				)
			],
		)

	fields: dict[str, typing.Any] = {
		"title": captured.title,
		"due": captured.due,
		"due_is_all_day": captured.due_is_all_day,
		"starts": captured.starts_at,
		"starts_is_all_day": captured.starts_is_all_day,
		# **`recurrence_text` is not passed**: `create` derives it from what it parsed, and a
		# captured line and a structured field must not disagree about the words somebody wrote.
		"recurrence": captured.recurrence,
		"snooze": captured.snooze,
		"snoozed_is_all_day": captured.snoozed_is_all_day,
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


def assignee_for (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, given: str
) -> subroutine.db.models.identity.User:
	"""Return the member this names, whether it was named by username or by id — `#493`.

	**Workspace-scoped, and deliberately not the same function as
	:func:`subroutine.domain.selection.user`.** That one resolves across the instance because a
	*filter* must not refuse in the workspaces somebody is not a member of — asking what is
	assigned to Jo is a fair question everywhere. Assigning work to Jo is only a fair act where
	Jo is a member, so this narrows and :func:`_user_by_name` already refuses by name with the
	members listed. **The same grammar, two questions**, and collapsing them would let a task be
	handed to somebody who cannot see it.

	A value that parses as a UUID is taken as an id, matching ``id_or_ref`` and ``id_or_key``
	and :func:`subroutine.domain.selection.user`; anything else is a username.
	"""

	try:
		identifier = uuid.UUID(given)

	except ValueError:
		return _user_by_name(session, workspace_id, given)

	member = subroutine.db.models.identity.WorkspaceMember
	user = subroutine.db.models.identity.User
	found = session.scalars(
		sqlalchemy.select(user)
		.join(member, member.user_id == user.id)
		.where(
			member.workspace_id == workspace_id,
			user.id == identifier,
			user.deleted_at.is_(None),
		)
	).one_or_none()

	if found is not None:
		return found

	raise subroutine.errors.ValidationError(
		f"There is nobody with the id {given!r} in this workspace.",
		errors=[
			subroutine.errors.FieldError(
				field="assignee",
				code="not_found",
				message=f"No member of this workspace has the id {given!r}.",
				hint="Name them by username instead — 'subroutine user list' shows them.",
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
	due_is_all_day: bool | None = subroutine.domain.patch.UNSET,
	starts: datetime.datetime | datetime.date | str | None = subroutine.domain.patch.UNSET,
	starts_is_all_day: bool | None = subroutine.domain.patch.UNSET,
	snooze: datetime.datetime | datetime.date | str | None = subroutine.domain.patch.UNSET,
	snoozed_is_all_day: bool | None = subroutine.domain.patch.UNSET,
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

	finished_now = False

	deadline: typing.Any = _rescheduled(
		task.due_at,
		given=due,
		all_day=due_is_all_day,
		boundary=subroutine.domain.schedule.Boundary.END,
		zone=zone,
		now=instant,
		field="due_at",
	)
	defer: typing.Any = _rescheduled(
		task.snoozed_until,
		given=snooze,
		all_day=snoozed_is_all_day,
		boundary=subroutine.domain.schedule.Boundary.START,
		zone=zone,
		now=instant,
		field="snoozed_until",
	)
	beginning: typing.Any = _rescheduled(
		task.starts_at,
		given=starts,
		all_day=starts_is_all_day,
		boundary=subroutine.domain.schedule.Boundary.START,
		zone=zone,
		now=instant,
		field="starts_at",
	)

	# Invariant 8 is checked against what the task *will* look like, not against what was
	# passed in: moving only the deadline still has to be consistent with the defer that is
	# already there, and the caller did not mention it.
	unchanged = defer is subroutine.domain.patch.UNSET

	subroutine.domain.schedule.check_order(
		instant=task.snoozed_until if unchanged else defer.instant,
		is_all_day=task.snoozed_is_all_day if unchanged else defer.is_all_day,
		due_at=task.due_at if deadline is subroutine.domain.patch.UNSET else deadline.instant,
		due_is_all_day=(
			task.due_is_all_day
			if deadline is subroutine.domain.patch.UNSET
			else deadline.is_all_day
		),
		timezone=zone,
		field="snoozed_until",
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
		#
		# **It records when the task became finished, and finishing it again is not a second
		# time** (`#723`). This stamped `utcnow()` on every write of a finished status, so
		# completing something already complete moved the record by however long had passed —
		# measured at 51 seconds on a throwaway, and a `POST /v1/tasks/{ref}/complete` on
		# finished work is a 200 that silently edits history. An ordinary retry does it, and
		# so does the *Complete* button that used to sit on every card in the board's *Done*
		# column (`#724`).
		#
		# **The reasoning was already written out one function below, about `deleted_at`**:
		# *"deleting twice is not an error and does not move the timestamp — when something
		# was thrown away is a fact worth not overwriting, and a caller retrying a request
		# should not change it."* Every word of it applies here and only one of the two
		# columns had it, which is this codebase's signature defect — one rule applied to one
		# side of a pair.
		#
		# **`completed_at is not None` is the test for "was it already finished", and that is
		# not a shortcut**: it is the reading `readiness`, `scoping`, `links` and `schedule`
		# all already apply, and this assignment is the only thing in the program that writes
		# the column, so the invariant it maintains is the invariant it may rely on.
		#
		# `cancelled` to `done` therefore keeps the original instant. Both are finished, the
		# work stopped when it stopped, and a column that moved on a change of *which kind* of
		# finished would be reporting when the status last changed — which is `updated_at`.
		if status.category not in FINISHED_CATEGORIES:
			task.completed_at = None
		elif task.completed_at is None:
			# **The call's own instant, not the wall clock** (`#94`). This read `utcnow()`,
			# so a caller supplying `now` — every test, and every service resolving a batch
			# against one moment — got a different instant recorded than the one it was
			# working from. The docstring above already promises the opposite: *"``now`` is
			# supplied so that every relative expression in one call resolves against a single
			# instant"*. Found by a `completion`-anchored repeat measuring its next occurrence
			# from a clock the caller had explicitly overridden.
			task.completed_at = instant

			# **Noted here and acted on at the end**, because this is the one place in the
			# program that decides a task has *become* finished. Hanging the next occurrence
			# off `complete()` instead would miss every other way a done status is set —
			# `update(status=…)`, the board's drag, the browser's status control — and a
			# repeat that advances on one surface and not the others is worse than none.
			finished_now = True

	if moving:
		moved_from = task.project_id
		task.project_id = project.id

		# **The parts go with it, because the invariant says they must.** Their own version
		# is bumped: a client holding one and sending it back under §8.9 has a stale view of
		# where that task lives, which is exactly what the check exists to catch.
		#
		# **And each one says so in its own history** (`#200`). The version moved and nothing
		# recorded why, so a subtask's history read `created` and nothing else while its ETag
		# had changed underneath a client — a 409 with no account of itself, which is §10.7's
		# invariant 9 broken on the commonest multi-row write in the product. An event per
		# descendant rather than a count on the parent, because the history somebody reads is
		# the *child's*: a number on another item's event is not an answer to "what happened to
		# this one". They are already loaded, so this writes no rows the move did not imply.
		for descendant in descendants:
			descendant.project_id = project.id
			descendant.version += 1

			subroutine.domain.events.record(
				session,
				workspace_id=descendant.workspace_id,
				entity_type="task",
				entity_id=descendant.id,
				action=subroutine.domain.events.EventAction.MOVED,
				changes={
					"project_id": {"from": moved_from, "to": project.id},
					# Which move this was part of. Without it the event says a task changed
					# project and not that it was carried, and "why did this move?" has no
					# answer but the timestamps.
					"moved_with": {"from": None, "to": task.ref},
				},
				actor=actor,
			)

	if wanted_tags is not subroutine.domain.patch.UNSET:
		# **Replaces, so an empty list clears.** Every other field on a PATCH is assigned
		# rather than merged, and a `tags` that merged would be the only one a caller could
		# not use to remove anything — which is how a mistyped tag became permanent.
		subroutine.domain.tags.set_on(session, task, wanted_tags)
		touches_content = True

	if assignee_id is not subroutine.domain.patch.UNSET:
		# **Only when it actually changes.** Re-sending the same assignee is not a fresh act of
		# delegation, and rewriting the assigner on it would let a passing `PATCH` quietly take
		# somebody else's name off the record.
		if assignee_id != task.assignee_id:
			task.assigned_by_id = _assigner(actor, assignee_id)

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

	if beginning is not subroutine.domain.patch.UNSET:
		task.starts_at = beginning.instant
		task.starts_is_all_day = beginning.is_all_day

	if defer is not subroutine.domain.patch.UNSET:
		task.snoozed_until = defer.instant
		task.snoozed_is_all_day = defer.is_all_day

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

	# **After the event, so the order reads the way it happened**: this one was finished, then
	# the next one appeared. The new instance records its own creation, so a change feed shows
	# both rather than one write that mysteriously produced two rows.
	if finished_now and task.recurrence_template_id is not None:
		template = session.get(
			subroutine.db.models.work.Task, task.recurrence_template_id
		)

		if template is not None and template.recurrence_rule is not None:
			materialise(
				session,
				template,
				now=task.completed_at or subroutine.db.types.utcnow(),
				after=task.occurrence_at or task.completed_at,
				actor=actor,
			)

	return task


def move (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	*,
	parent: subroutine.db.models.work.Task | None,
	max_depth: int = subroutine.domain.hierarchy.DEFAULT_MAX_DEPTH,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> int:
	"""Re-parent a task and everything under it, returning how many rows were rewritten.

	``parent=None`` promotes it to a top-level task, which is half of what `#44` was filed
	for: a subtask could be created and then never moved again, in either direction.

	**Its own operation rather than a field on ``update``**, which §8's *"an explicit verb
	sub-resource where something is genuinely not CRUD"* reserved this endpoint for. The
	distinction is not ceremony: changing a project is a field being wrong and the subtree
	following is an invariant, where changing a parent can be *refused for being a cycle* —
	a question about the shape of the tree rather than about this row, and one that cannot be
	answered without walking it.
	"""

	filed_in = session.get(subroutine.db.models.project.Project, task.project_id)

	_permitted(
		session,
		actor,
		subroutine.permissions.TASK_WRITE,
		project=filed_in,
		workspace_id=task.workspace_id,
	)

	if parent is not None and parent.project_id != task.project_id:
		# **Refused rather than carried, and this is the decision worth reading** (`#44`).
		# A subtask belongs to its parent's project — `create` enforces it and `update`
		# refuses to move a subtask out from under its parent for the same reason. So a
		# parent elsewhere leaves two coherent answers: refuse, or move this subtree into
		# the parent's project as a side effect of re-parenting it.
		#
		# Refused, because the second changes a task's project without the caller ever
		# naming the project — and `update` already does that move, explicitly, when asked.
		# The cost is that reaching across projects is two commands; the refusal says which.
		# **Whether the invariant should hold at all is `#17`'s**, not this one's: a release
		# whose contents are sub-tasks would span projects, and that is a decision about what
		# a sub-task means rather than about how one is moved.
		destination = session.get(subroutine.db.models.project.Project, parent.project_id)

		# Both keys named, never one. "It is in the wrong project" leaves a reader looking up
		# two things before they can act, and the second lookup is the one they will get wrong.
		here = "another project" if filed_in is None else f"'{filed_in.key}'"
		there = None if destination is None else destination.key

		raise subroutine.errors.ValidationError(
			"A subtask belongs to the same project as its parent.",
			errors=[
				subroutine.errors.FieldError(
					field="parent",
					code="invalid_field_value",
					message=(
						f"#{task.ref} is in {here} and #{parent.ref} is in "
						f"{'another project' if there is None else repr(there)}."
					),
					hint=(
						f"Move it there first, with 'subroutine update {task.ref} "
						f"--project {there}', then put it under #{parent.ref}."
						if there is not None
						else f"Move it into that project first, then put it under #{parent.ref}."
					),
				)
			],
		)

	previous_parent = task.parent_task_id
	previous_path = task.path

	moved = subroutine.domain.hierarchy.reparent(
		session, subroutine.db.models.work.Task, task, parent, max_depth=max_depth
	)

	if moved == 0:
		return 0

	task.parent_task_id = None if parent is None else parent.id

	# `version` is the ETag (§8.9), so anything a client can read has to move it — and
	# `reparent` rewrote `path` and `depth` on every descendant with one Core UPDATE, which
	# sets no version. Without the second statement a client holding an ETag for a child
	# cannot tell that the child's path changed. `projects.move` carries the same pair.
	task.version += 1
	task.updated_by = None if actor is None else actor.user.id

	model = subroutine.db.models.work.Task
	session.execute(
		sqlalchemy.update(model)
		.where(
			model.workspace_id == task.workspace_id,
			subroutine.domain.hierarchy.subtree(model, task),
			model.id != task.id,
		)
		.values(version=model.version + 1, updated_by=task.updated_by)
		.execution_options(synchronize_session=False)
	)
	session.expire_all()
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=task.workspace_id,
		entity_type="task",
		entity_id=task.id,
		action=subroutine.domain.events.EventAction.MOVED,
		changes={
			"parent_task_id": {"from": previous_parent, "to": task.parent_task_id},
			"path": {"from": previous_path, "to": task.path},
			"descendants_rewritten": {"from": None, "to": moved - 1},
		},
		actor=actor,
	)
	session.flush()

	return moved


def status_key_in (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, category: str
) -> str:
	"""Return the key of a status in a category, whatever this workspace calls it.

	Statuses are data — an installation renames and adds them freely (§5.5) — so nothing may
	hard-code ``"done"``. This asks for the first status in the *category*, which is what
	keeps "mark it finished" working after somebody renames it to "Shipped".
	"""

	model = subroutine.db.models.vocabulary.Status

	found = session.scalars(
		sqlalchemy.select(model)
		.where(
			model.workspace_id == workspace_id,
			model.entity_type == "task",
			model.category == category,
		)
		.order_by(model.position)
	).first()

	if found is None:
		raise subroutine.errors.InternalError(
			f"This workspace has no status meaning {category!r}.",
			hint="Its vocabulary is incomplete; restore it, or start again from an empty "
			"database.",
		)

	return found.key


def finished_status_key (session: sqlalchemy.orm.Session, workspace_id: uuid.UUID) -> str:
	"""Return the key of a status meaning finished. See :func:`status_key_in`."""

	return status_key_in(session, workspace_id, "done")


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



def skip (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	*,
	now: datetime.datetime | None = None,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Task:
	"""Let one occurrence of a repeat go by, and bring the next one (§6.7).

	**Cancelled rather than done, and that distinction is the whole verb.** Both categories
	are finished, so both advance the series — but "I did not do this" and "I did this" are
	different facts about the same month, and a series recorded entirely as `done` cannot
	answer *how often do I actually skip this*. `#574` is what wants that answer: a habit
	skipped leaves no trace, and the absence of evidence is the whole problem.

	**A task that is not part of a repeat is refused by name**, rather than quietly cancelled.
	Skipping means *this one, not the series*, and on something that happens once that is
	simply cancelling it — which the caller can already say, and which they should say
	deliberately.
	"""

	if task.recurrence_template_id is None:
		raise subroutine.errors.ValidationError(
			"That is not one of a repeating series, so there is nothing to skip to.",
			code="invalid_field_value",
			hint="Cancel it instead, by setting a cancelled status.",
			errors=[
				subroutine.errors.FieldError(
					field="ref",
					code="invalid_field_value",
					message="Only an occurrence of a repeat can be skipped.",
				)
			],
		)

	return update(
		session,
		task,
		status_key=status_key_in(session, task.workspace_id, "cancelled"),
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
	a deleted item is "restorable for a configurable retention period", a
	``trash_retention_days`` setting was declared from the beginning, and ``EventAction.RESTORED``
	has been in the vocabulary just as long — with nothing anywhere setting ``deleted_at`` back to null. So the
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


def _rescheduled (
	stored: datetime.datetime | None,
	*,
	given: typing.Any,
	all_day: typing.Any,
	boundary: subroutine.domain.schedule.Boundary,
	zone: str,
	now: datetime.datetime,
	field: str,
) -> typing.Any:
	"""Work out a date column's new value from whichever half of the pair was sent.

	The pair is a date and a flag saying whether it names a whole day, and **either may be
	changed without the other** (`#195`). The flag used to be a plain argument rather than a
	patch sentinel, so it was consulted only when the date beside it was also being set — which
	meant ``PATCH {"due_is_all_day": false}`` was accepted with a ``200``, changed nothing, and
	left ``version`` where it was. A declared, documented field, silently discarded: exactly
	what the ``unknown_field`` refusal exists to argue against, and worse, because a correctly
	spelled field gives a caller no reason to doubt it.

	Changing the flag alone re-reads the date the task already has. ``interpret`` takes a
	``datetime`` and returns it untouched when the flag is off, or snapped to the boundary of
	its local day when the flag is on — so the two directions are the two answers a person
	means: "this is a day, not a time", and "no, I meant that exact instant".

	**A flag with no date to describe is refused**, rather than stored against a null. It is
	the one combination that cannot mean anything, and accepting it would put the silence back.
	"""

	if given is subroutine.domain.patch.UNSET and all_day is subroutine.domain.patch.UNSET:
		return subroutine.domain.patch.UNSET

	if given is subroutine.domain.patch.UNSET and stored is None:
		# **Looked up rather than derived from the column name** — `schedule.DATE_FIELDS`
		# carries why, and it is the mistake this refusal is about, made about itself.
		written, flag = subroutine.domain.schedule.DATE_FIELDS[field]

		raise subroutine.errors.ValidationError(
			f"There is no {written} date for that to describe.",
			errors=[
				subroutine.errors.FieldError(
					field=flag,
					code="invalid_field_value",
					message="Whether something is a whole day or a time says nothing on its own.",
					hint=f"Send '{written}' as well, with the day or the instant you mean.",
				)
			],
		)

	return subroutine.domain.schedule.interpret(
		stored if given is subroutine.domain.patch.UNSET else given,
		boundary=boundary,
		timezone=zone,
		now=now,
		all_day=None if all_day is subroutine.domain.patch.UNSET else all_day,
		field=field,
	)


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
		"tags": subroutine.domain.tags.names_on(session, task),
		"description": task.description,
		"status_id": task.status_id,
		"type_id": task.type_id,
		"assignee_id": task.assignee_id,
		"importance": task.importance,
		"urgency": task.urgency,
		"estimate_minutes": task.estimate_minutes,
		"due_at": task.due_at,
		"due_is_all_day": task.due_is_all_day,
		"starts_at": task.starts_at,
		"snoozed_until": task.snoozed_until,
		"snoozed_is_all_day": task.snoozed_is_all_day,
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


def statuses_in_category (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, category: str
) -> list[uuid.UUID]:
	"""Return the ids of every task status in one category, for a listing to narrow by.

	**A category rather than a key, and that is the whole point of the filter** (`#710`). A
	status key is per-workspace and renameable, so a board or a completed-work view keyed on
	``done`` stops working on the first installation that renames it. ``category`` is the fixed
	field :class:`subroutine.db.models.vocabulary.Status` publishes beside the key precisely so
	that a client may branch on it.

	A document's categories are refused here by name. They are a different vocabulary for a
	different reason — a superseded specification is not "done" — and passing one to a task
	listing is a mistake worth being told about rather than an empty page.
	"""

	if category not in subroutine.db.mixins.TASK_STATUS_CATEGORIES:
		known = ", ".join(subroutine.db.mixins.TASK_STATUS_CATEGORIES)

		raise subroutine.errors.ValidationError(
			f"{category!r} is not a status category a task can be in.",
			errors=[
				subroutine.errors.FieldError(
					field="status_category",
					code="invalid_field_value",
					message=f"No task status category called {category!r}.",
					hint=f"A task is in one of: {known}.",
				)
			],
		)

	model = subroutine.db.models.vocabulary.Status

	return list(
		session.scalars(
			sqlalchemy.select(model.id).where(
				model.workspace_id == workspace_id,
				model.entity_type == "task",
				model.category == category,
			)
		)
	)


def completion_wanted (
	category: str | None,
	asked: bool | None,
	*,
	about_completion: bool = False,
	about_activity: bool = False,
	naming_one_item: bool = False,
) -> bool:
	"""Say whether a listing should reach finished work.

	**Here rather than in the router, because both transports have to agree** — the same reason
	:mod:`subroutine.domain.ordering` exists. A rule applied on one side would make
	``status_category="done"`` return the finished work over HTTP and an empty list locally.

	``asked`` is three-valued: ``None`` means the caller did not say, which is what lets a
	narrowing supersede a default without overriding a decision. Asking for a finished category
	and *not* mentioning completion is an unambiguous request for finished work, so the rows
	are reached rather than filtered away — the trap being ``?status_category=done`` answering
	``[]`` on an instance full of finished work, which is a plausible, complete, wrong answer.

	Saying both, and disagreeing, is refused rather than resolved. There is no reading of
	"only cancelled work, and no finished work" that means anything, and this codebase's rule
	on a listing is that a contradiction is named rather than quietly settled in one
	parameter's favour.

	**``about_completion`` is the same rule reaching a second spelling** (`#818`). A caller
	filtering on ``completed_at`` is asking about finished work as unambiguously as one naming
	a finished category — the column is null on everything else — so the paragraph above
	applied to it word for word and did not reach it, because this function knew about
	categories and not about filters. Measured on a fresh instance:
	``list --filter completed_at.gte=today`` said *nothing on your list* the same minute a task
	was completed.

	**``about_activity`` is the same argument with a different ending** (`#815`). Asking
	``touched_at.gte=today`` — *what did I work on today* — must reach something finished
	today, because Simon's own wording of the question names *completed* among the things that
	count, and decision `#817`'s rule for this filter is that the failure direction is too many
	rows rather than work that is silently missing. Found by driving the five questions on a
	real instance: the finished task was the only one absent.

	**But it is not a contradiction to say no**, which is where the two part company. *What did
	I work on today that is not finished yet* is an ordinary question, so
	``include_completed=false`` is honoured here rather than refused — where beside
	``completed_at`` it asks for finished work and no finished work, which means nothing.

	**``naming_one_item`` is the third spelling, and it is the one that hurt** (`#873`). `#867`
	made a search that is exactly a ref match the item with that number — and 548 of this
	instance's 721 tasks are finished, so for **three items in four** the lookup found the row
	and the listing then hid it. Every other selection *narrows a set*; a ref names **one item**
	and the reader has already decided which, so answering "nothing" about something
	``subroutine show`` reads happily is `#700`'s divergence between a lookup and a listing.

	`#818`'s own sentence is the lesson and this is its third instance: *a rule written down in
	one vocabulary does not reach the next one.* It knew about categories, then about filters,
	and not about a lookup.

	**It widens the whole listing rather than exempting one row**, which is a choice worth
	stating. Exempting only the matched row would mean pushing the ref down into
	``scoping.readable_tasks``, and the result set for a bare number is small anyway — four to
	twenty-one rows, measured — so what widening actually adds is the *finished* items that
	mention that number, which is a fair reading of what somebody typing a ref is asking.
	``include_completed=false`` is still honoured, as with ``about_activity``, because *the open
	item numbered 815* is a coherent question.
	"""

	wants_finished = about_completion or (
		category is not None and category in FINISHED_CATEGORIES
	)

	if not wants_finished:
		# Not `bool(asked)`: three-valued, so *did not say* means include and *said no* means
		# exclude. Collapsing them would make the answer ignore a caller who was explicit.
		return asked is not False if (about_activity or naming_one_item) else bool(asked)

	if asked is False:
		raise subroutine.errors.ValidationError(
			_excluding_all_of_it(category, about_completion),
			errors=[
				subroutine.errors.FieldError(
					field="include_completed",
					code="invalid_field_value",
					message=(
						f"{_asking_for_it(category, about_completion)} asks only for finished "
						"work and include_completed=false excludes all of it."
					),
					hint="Drop include_completed — asking about finished work implies it.",
				)
			],
		)

	return True


def _asking_for_it (category: str | None, about_completion: bool) -> str:
	"""Name whichever half of the request asked for finished work."""

	if category is not None and category in FINISHED_CATEGORIES:
		return f"status_category={category!r}"

	return "a filter on completed_at"


def _excluding_all_of_it (category: str | None, about_completion: bool) -> str:
	"""Say what the contradiction was, in the caller's own terms."""

	if category is not None and category in FINISHED_CATEGORIES:
		return f"{category!r} is finished work, so excluding finished work leaves nothing."

	return (
		"completed_at is only ever set on finished work, so excluding finished work "
		"leaves nothing."
	)
