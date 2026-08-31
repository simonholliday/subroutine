"""Creating and editing the thing the whole system exists to hold.

Everything the foundations built meets here: a ref is allocated from the project's
counter, a path is placed in the subtask tree, an event is recorded, and whatever the
description refers to is indexed — all inside one transaction, so a task never exists
without its ref or its history.
"""

import datetime
import inspect
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.config
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
import subroutine.domain.claims
import subroutine.domain.dates
import subroutine.domain.durations
import subroutine.domain.events
import subroutine.domain.hierarchy
import subroutine.domain.instances
import subroutine.domain.mentions
import subroutine.domain.ordering
import subroutine.domain.patch
import subroutine.domain.readiness
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
#: (docs/design.md §10.7 invariant 5). Read from the status row's category rather than its key,
#: because an installation renames and adds statuses freely.
FINISHED_CATEGORIES = frozenset({"done", "cancelled"})

#: docs/design.md §6.10. Enforced here so the message names the field and the limit, rather than
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
		# **Refused rather than dropped** (`#918`). Both of these qualify a rule and neither
		# means anything without one, so a caller who sent an anchor and no rule has said
		# something this cannot act on. It answered 201 and stored nothing until now, which is
		# `#379`'s swallowed argument in a second place: the caller is told it worked, and the
		# only evidence otherwise is a field that reads back null.
		named = [
			field
			for field, value in (
				("recurrence_anchor", anchor), ("recurrence_trigger", trigger)
			)
			if value is not None
		]

		if named:
			# **The prose names no field and the structured half names them all** (`#547`).
			# A refusal saying *send `recurrence`* is unfollowable at a terminal, where the
			# flag is `--repeat`, and MCP's renaming layer reaches tool arguments rather than
			# a domain message. Describing the *thing* instead is true on every surface, and
			# `errors[].field` still carries the wire name for a caller that wants it.
			raise subroutine.errors.ValidationError(
				"That describes how something repeats, and nothing here repeats.",
				code="invalid_field_value",
				hint="Say how often it comes round as well — this only qualifies that.",
				errors=[
					subroutine.errors.FieldError(
						field=field,
						code="invalid_field_value",
						message="This qualifies a repeat, and none was given.",
					)
					for field in named
				],
			)

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



def own_day_field (category: str) -> str:
	"""Return the column a rule that names its own day fills, for an item of this category.

	**Decision `#1235`, and the first thing that answers this question by what the item *is*.**
	`#1208` hardcoded ``due_at`` and said in terms that a birthday wants the opposite — the
	phrasing cannot tell them apart, because *every month on the 1st* and *every year on 14
	March* are one grammar and *the council tax* and *Anna's birthday* are the difference.

	An occasion is Simon's *out of our control, never due or overdue — it just happens*, so a
	deadline is the one thing its date cannot be. Everything else keeps `#1208`'s answer, which
	was taken on his own example: a bill really is due on the 1st.

	**The category and never the key**, so a workspace adding ``holiday`` under ``occasion``
	through `#1129` inherits this without a release.
	"""

	if category == subroutine.domain.readiness.OCCASION:
		return "starts_at"

	return "due_at"


def grid_field_for (due_at: datetime.datetime | None) -> str:
	"""Say which column a row holding this deadline puts its slot on.

	The rule itself, taken apart from the row so that it can also be asked of a **snapshot** —
	:func:`_kept_on_its_grid` has to know which column the slot was on *before* an edit as well
	as which one it is on now, and an edit may move it from one to the other. Written once here
	rather than spelled a second time over a dict, which is `#1302` exactly.
	"""

	return "due_at" if due_at is not None else "starts_at"


def grid_field (row: subroutine.db.models.work.Task) -> str:
	"""Say which of a row's two dates ``occurrence_at`` is a slot on.

	:func:`materialise` mints an occurrence with ``occurrence_at`` equal to
	``due_at or starts_at``, and :func:`~subroutine.domain.calendars._is_on_its_grid` reads
	*has this been moved* off exactly that comparison. So the slot follows one column and the
	other is free to move without it.

	**One rule, because its two readers disagreed** (`#1302`, and `#1304` is the other half of
	the same finding). This module carried the rule as *the first date column that moved*,
	which is only the tracked one when a single date is set — so lengthening a repeating
	meeting moved the slot by the **end's** delta, the row fell off its grid, and the feed drew
	the event twice with an ``EXDATE`` pointing at a time the rule never emits. The calendar
	carried the rule correctly, one module away, and had no way to say so.

	**A series carrying both dates has two grids and one recorded slot**, which is the known
	limitation this names rather than papers over: moving only the *start* of such a series is
	a move nothing here can see, and the feed shows the grid's start rather than the moved one.
	What removes it is ``RECURRENCE-ID`` overrides and a per-field original this schema does
	not keep. Narrow enough to accept and too specific to guess at.
	"""

	return grid_field_for(row.due_at)


def grid_date (row: subroutine.db.models.work.Task) -> datetime.datetime | None:
	"""Return the date ``occurrence_at`` is a slot on, or ``None`` if the row carries neither.

	The value half of :func:`grid_field`, for the callers that want the date rather than the
	name. Written out because ``getattr`` on a column name returns :data:`typing.Any`, and an
	``Any`` on the rule this many readers share is where the next defect would hide.
	"""

	held: datetime.datetime | None = getattr(row, grid_field(row))

	return held


def first_whole_day (
	rule: str, *, timezone: str, now: datetime.datetime, field: str = "due_at"
) -> subroutine.domain.schedule.Moment:
	"""Return the first day a rule that names its own days falls on, as a whole day.

	**"Every month on the 1st" says when, and until `#1208` nothing wrote that down.** `#94`
	lets such a rule anchor itself on the moment it was filed rather than refusing it for not
	saying when — which it does say — and the row was left carrying a rule and no date. Every
	surface that draws a date then had nothing to draw: measured on a fresh 0.8.1 instance, the
	series was invisible to the calendar feed entirely, and the occurrence it minted was too.

	**A whole day, and :func:`whole_day_for` is why** — the grammar names days and never times.

	**Which column is the caller's to say, and :func:`own_day_field` is what says it** (`#1209`).
	It was hardcoded to ``due_at``, which is right for the bill `#1208` was written from and
	wrong for a birthday — *"Due: Anna's birthday"*, yearly, for ever, in somebody's calendar.
	The phrasing cannot tell those apart; the type category can.

	**The edge follows the column.** §6.5 stores an all-day deadline at the last microsecond of
	its day and an all-day start at the first, so a version that chose the column and kept
	``Boundary.END`` would store a birthday's start at the *end* of its day. **Every rendering
	still reads right** — the calendar draws `VALUE=DATE` from the local date either way — and
	the comparisons that take the instant for the beginning of the day are a day out:
	:func:`subroutine.domain.readiness.passed` would keep the birthday current for a day after
	it, because it measures a whole day from the start.

	**Creation is the only caller now**, so the series itself carries a date and can be drawn as
	a repeat. :func:`materialise` computes the occurrence itself and shares the snapping through
	:func:`whole_day_for`; it is what carries a series filed before `#1208` existed.
	"""

	found = subroutine.domain.recurrence.occurrences(rule, start=now, timezone=timezone, limit=1)

	if not found:
		return subroutine.domain.schedule.Moment(instant=None, is_all_day=False)

	return whole_day_for(found[0], field=field, timezone=timezone, now=now)


def whole_day_for (
	moment: datetime.datetime | datetime.date,
	*,
	field: str,
	timezone: str,
	now: datetime.datetime,
) -> subroutine.domain.schedule.Moment:
	"""Snap a day to the edge of it that ``field`` stores.

	**The pairing of column and edge, in one place** (`#1209`), declared as
	:data:`subroutine.domain.schedule.WHOLE_DAY_EDGE`. §6.5 stores an all-day deadline at the last microsecond of its day
	and an all-day start at the first, so the two travel together — and they were two copies
	before this, one here and one inlined in :func:`materialise`, agreeing only because both
	were hardcoded to ``due_at``. The moment the column became a question they would have had
	to be changed twice, which is `#1156`'s shape and the reason this codebase watches for it.

	**A whole day, always.** A rule computed one — the recurrence grammar has no ``BYHOUR``, so
	anchoring on the filing instant gave each slot that instant's time of day and a client drew
	a one-minute appointment at whatever o'clock somebody was at their desk. A bare
	:class:`datetime.date` is the other caller, `#1303`'s: a series carrying a date whose shape
	changed, where the day is known and the edge is the whole question.
	"""

	return subroutine.domain.schedule.interpret(
		moment,
		boundary=subroutine.domain.schedule.WHOLE_DAY_EDGE[field],
		timezone=timezone,
		now=now,
		all_day=True,
		field=field,
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
	# **The same column the slot is on** (:func:`grid_field`), and it has to be: ``materialise``
	# computes ``occurrence_at`` from this anchor and each date from its own column, so an
	# anchor on one column and a slot on another would mint every occurrence off its own grid.
	anchor = grid_date(template)

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

	# **A finished template is a stopped series** (`#94`). That is what `stop_repeating` does
	# and it is why it needs no column: completing the rule-bearing row says *the series ran,
	# here is what it was, and no more are coming* — where clearing the rule and un-templating
	# would put a second copy of the task into every listing, since the template carries the
	# same title as the live occurrence.
	if template.completed_at is not None:
		return None

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

	# **A series that was never given a date takes the one its own rule computes** (`#1208`).
	#
	# "Pay the council tax every month on the 1st" is a rule that says which day it falls on, so
	# `series_start` anchors it on the moment it was filed rather than refusing it — and every
	# occurrence then arrived with `occurrence_at` set and `due_at` and `starts_at` both null.
	# **Measured on a fresh 0.8.1 instance**: the rule was right, the first slot was computed
	# correctly, and the item was invisible to every calendar feed, because `calendars`
	# gates both of its branches on one of those two columns being set.
	#
	# **The code already assumed this could not happen.** `_is_on_its_grid` compares
	# `occurrence_at` against :func:`grid_date`, which is null while both columns are — so the
	# deduplication that stops a rule and its occurrence appearing twice in a calendar was
	# reasoning about a state the writer had not considered. (It spelled the rule out as
	# `due_at or starts_at` when this was written; `#1302` made it one function, and the
	# quotation is corrected here rather than left as the one place a reader hunting copies of
	# the rule would find one and not know it was a quotation.)
	#
	# **A deadline, and a whole day.** The rule names days and never times — the grammar has no
	# `BYHOUR` — so anchoring on the filing instant gave the slot a time of day nobody chose:
	# 18:27 on the 1st, because that is when somebody happened to type it. Snapped to the whole
	# day it falls in, which is what `interpret` does for every other typed deadline, so the end
	# of the day is the deadline and the calendar draws a date rather than an appointment.
	#
	# **Which column it lands in is decided by what the item is** (`#1209`, decision `#1235`),
	# and it was hardcoded to `due_at`. A council tax bill is due; a birthday is not, and the
	# calendar prefixes differ, so the hardcoding wrote *"Due: Anna's birthday"* into somebody's
	# calendar every year. `own_day_field` is the one answer and `whole_day_for` is the one
	# snapping rule, because this branch and `create`'s have to agree and previously agreed only
	# by both being wrong the same way.
	#
	# **`occurrence_at` moves with it**, because the invariant above is what the grid is read
	# through and the cursor for the *next* slot is this column. Snapping one and not the other
	# would leave every occurrence looking rescheduled.
	dateless = template.due_at is None and template.starts_at is None
	kind = session.get(subroutine.db.models.vocabulary.ItemType, template.type_id)
	own_field = own_day_field("" if kind is None else kind.category)
	whole_day = (
		whole_day_for(occurrence, field=own_field, timezone=zone, now=now).instant
		if dateless
		else None
	)
	# **Reachable only for a series filed before `#1208`**, because creation now gives such a
	# template its own date — and left in deliberately rather than migrated, so a repeat somebody
	# already has starts producing dated occurrences without anybody running anything. What makes
	# it removable is a migration that dates the templates; until then, deleting this puts those
	# series back to minting rows no surface can draw.

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
		# **Carried, unlike the snooze below it** (`#1211`). A reminder is a property of the
		# series — "two weeks before my sister's birthday" is asked once and meant every year —
		# where a defer is somebody saying *not this one*.
		reminder_minutes=template.reminder_minutes,
		due_at=(
			(whole_day if own_field == "due_at" else None)
			if dateless
			else None if template.due_at is None else template.due_at + shift
		),
		due_is_all_day=(
			own_field == "due_at" if dateless else template.due_is_all_day
		),
		starts_at=(
			(whole_day if own_field == "starts_at" else None)
			if dateless
			else None if template.starts_at is None else template.starts_at + shift
		),
		starts_is_all_day=(
			own_field == "starts_at" if dateless else template.starts_is_all_day
		),
		# **Carried and shifted with the start**, because a span belongs to the series: a
		# stand-up that runs 09:00 to 09:15 runs that long every day, and an occurrence carrying
		# a start without its end would be the zero-length event `#1235` exists to prevent.
		ends_at=None if template.ends_at is None else template.ends_at + shift,
		# **Deliberately not carried.** A snooze is somebody saying "not yet" about one
		# occurrence; repeating it would hide every future one for the same reason, which
		# nobody asked for.
		snoozed_until=None,
		snoozed_is_all_day=False,
		timezone=template.timezone,
		recurrence_template_id=template.id,
		occurrence_at=whole_day if dateless else occurrence,
		is_template=False,
		path="",
		depth=0,
		created_by=None if actor is None else actor.user.id,
	)
	subroutine.domain.hierarchy.place(instance, None, max_depth=subroutine.domain.hierarchy.DEFAULT_MAX_DEPTH)

	session.add(instance)
	session.flush()

	# **A tag belongs to the series, so every turn of the wheel carries it** (`#1307`). The
	# columns above are copied one at a time and a join is not a column, so the tag somebody
	# typed stayed on the template — which is excluded from every listing — and the row they
	# were handed had none of it. ``subroutine add "Water the plants #home every monday"``
	# answered *(read #home)* and then showed a row without it, and `search "#home"` found
	# nothing at all.
	#
	# **Here rather than in `create`, because this is the call that mints every occurrence**
	# and not only the first. Decorating the first would put the tag back for one week and
	# lose it again, which reads as correct on the day it is written.
	#
	# **The same argument `reminder_minutes` makes above**, and the opposite of the snooze
	# below it: *#home* says what the task is, where a defer says *not this one*.
	#
	# ``set_on`` replaces rather than merges, so a tag taken off the series is taken off the
	# occurrences it goes on to mint.
	subroutine.domain.tags.set_on(
		session, instance, subroutine.domain.tags.on(session, template)
	)

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
	reminder: int | str | None = None,
	due: datetime.datetime | datetime.date | str | None = None,
	due_is_all_day: bool | None = None,
	starts: datetime.datetime | datetime.date | str | None = None,
	starts_is_all_day: bool | None = None,
	ends: datetime.datetime | datetime.date | str | None = None,
	snooze: datetime.datetime | datetime.date | str | None = None,
	snoozed_is_all_day: bool | None = None,
	recurrence: str | None = None,
	recurrence_anchor: str | None = None,
	recurrence_trigger: str | None = None,
	tags: typing.Sequence[str] | None = None,
	timezone: str | None = None,
	now: datetime.datetime | None = None,
	max_depth: int | None = None,
	settings: subroutine.config.Settings | None = None,
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
	description = subroutine.domain.text.readable(description, field="description")

	if parent is not None and parent.project_id != project.id:
		raise subroutine.errors.ValidationError(
			"A sub-task belongs to the same project as its parent.",
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
	# **The same grammar `~2h` already speaks**, so "two weeks before" is written `2w` here as
	# it is everywhere else (`#1211`). One parser, so a reminder and an estimate cannot come to
	# disagree about what a week is.
	warning = (
		None
		if reminder is None
		else subroutine.domain.durations.parse(reminder, field="reminder")
	)

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
	# **The far end of the day, like a deadline and unlike a start** (`#1235`). A holiday that
	# ends on the 28th is over when the 28th is, not at midnight as it begins — the same
	# reasoning §6.5 applies to `due_at`, arriving at the same boundary for the same reason.
	ending = subroutine.domain.schedule.interpret(
		ends,
		boundary=subroutine.domain.schedule.Boundary.END,
		timezone=zone,
		now=instant,
		# **Inferred from the shape, never sent.** There is no `ends_is_all_day` to pass: a
		# span's all-day-ness is one fact and `starts_is_all_day` holds it, so what a caller
		# writes here decides only whether the two ends *agree* — which `check_span` insists
		# on rather than silently picking one.
		all_day=None,
		field="ends_at",
	)

	subroutine.domain.schedule.check_span(
		starts_at=beginning.instant,
		starts_is_all_day=beginning.is_all_day,
		ends_at=ending.instant,
		ends_is_all_day=ending.is_all_day,
		timezone=zone,
	)

	# **A repeat that names its own days is given the first of them** (`#1208`). Without this the
	# row carries a rule and no date, so nothing that draws a date can draw it — including the
	# calendar, which is where a repeating bill is most of the point. `first_whole_day` holds the
	# reasoning and is the same function `materialise` falls back to.
	#
	# **Which column it lands in is decided by what the item is** (`#1209`, decision `#1235`).
	# A council-tax payment on the 1st is due then; a birthday on 14 March is not due at all,
	# and until this the two were one hardcoded answer — so a yearly birthday reached somebody's
	# calendar as *Due: Anna's birthday*, for ever.
	if (
		repeat is not None
		and deadline.instant is None
		and beginning.instant is None
		and subroutine.domain.recurrence.names_its_own_day(repeat.rule)
	):
		field = own_day_field(item_type.category)
		found = first_whole_day(repeat.rule, timezone=zone, now=instant, field=field)

		if field == "due_at":
			deadline = found
		else:
			beginning = found

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
		reminder_minutes=warning,
		due_at=deadline.instant,
		due_is_all_day=deadline.is_all_day,
		starts_at=beginning.instant,
		starts_is_all_day=beginning.is_all_day,
		ends_at=ending.instant,
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
	subroutine.domain.hierarchy.place(task, parent, max_depth=subroutine.domain.hierarchy.depth_limit(max_depth, settings))

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
	settings: subroutine.config.Settings | None = None,
	**overrides: typing.Any,
) -> tuple[subroutine.db.models.work.Task, subroutine.domain.capture.Capture]:
	"""Create a task from a captured line, resolving the names it mentions.

	Returns the task **and** what was parsed, so a caller can tell the user what it did
	with their sentence rather than making them infer it from the result.

	**Structured fields win over parsed ones** (docs/design.md §6.13): anything in ``overrides``
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
			else subroutine.domain.selection.addressed(
				session, actor, workspace, captured.project_key, field="project"
			)
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
			else subroutine.domain.users.member(
				session, workspace.id, captured.assignee, field="assignee"
			).id
		),
	}
	fields.update(overrides)

	task = create(
		session,
		project=project,
		now=instant,
		timezone=zone,
		actor=actor,
		# **Named rather than left to ``overrides``**, for the reason ``project`` is: it is not
		# a field of the task, it is how the call is answered, and a caller writing
		# ``settings`` into a captured line's overrides would be saying something else.
		settings=settings,
		**fields,
	)

	return task, captured


def assignee_for (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, given: str
) -> subroutine.db.models.identity.User:
	"""Return the member this names, whether it was named by username or by id — `#493`.

	**Workspace-scoped, and deliberately not the same function as
	:func:`subroutine.domain.selection.user`.** That one resolves across the instance because a
	*filter* must not refuse in the workspaces somebody is not a member of — asking what is
	assigned to Jo is a fair question everywhere. Assigning work to Jo is only a fair act where
	Jo is a member, so this narrows and refuses by name with the members listed. **The same
	grammar, two questions**, and collapsing them would let a task be handed to somebody who
	cannot see it.

	The resolution itself is :func:`subroutine.domain.users.member`, because a document's owner
	is the same question and was answered two other ways. This name stays because the argument
	above is about assigning work, and it is where a reader of this module will look for it.
	"""

	return subroutine.domain.users.member(session, workspace_id, given, field="assignee")


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
	reminder: int | str | None = subroutine.domain.patch.UNSET,
	due: datetime.datetime | datetime.date | str | None = subroutine.domain.patch.UNSET,
	due_is_all_day: bool | None = subroutine.domain.patch.UNSET,
	starts: datetime.datetime | datetime.date | str | None = subroutine.domain.patch.UNSET,
	starts_is_all_day: bool | None = subroutine.domain.patch.UNSET,
	ends: datetime.datetime | datetime.date | str | None = subroutine.domain.patch.UNSET,
	snooze: datetime.datetime | datetime.date | str | None = subroutine.domain.patch.UNSET,
	snoozed_is_all_day: bool | None = subroutine.domain.patch.UNSET,
	recurrence: str | None = subroutine.domain.patch.UNSET,
	recurrence_anchor: str | None = subroutine.domain.patch.UNSET,
	recurrence_trigger: str | None = subroutine.domain.patch.UNSET,
	project: subroutine.db.models.project.Project = subroutine.domain.patch.UNSET,
	tags: typing.Sequence[str] | None = subroutine.domain.patch.UNSET,
	applies_to: str | None = None,
	timezone: str | None = None,
	now: datetime.datetime | None = None,
	expected_version: int | None = None,
	settings: subroutine.config.Settings | None = None,
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

	``applies_to`` says which occurrences of a repeating item an edit is for — decision `#1249`,
	and :data:`ANSWERS` carries the two of them. ``None`` means the caller did not say, and lands
	on the row it was given, which is what everything did before `#1247`. **The surfaces are
	where that becomes a question a person is asked** (`#1251`) or a request refused for not
	answering (`#1252`); here it is an argument, because a domain that guessed would be guessing
	on behalf of whichever client happened to call it.
	"""

	# **Which arguments the caller actually named, taken before any local exists** — so this
	# is exactly the parameters, and a patchable argument added later is covered without
	# anybody remembering to list it here. That is `#1268`'s lesson one layer along: a field
	# missing from a hand-written register is invisible to every guard built on the register.
	#
	# **Two statements rather than one, and the reason is `#1272`.** `locals()` inside a
	# comprehension's leftmost iterable is a question about scoping rules that moved in 3.12
	# (PEP 709) and again in 3.13 (PEP 667) — and this machine has neither, so the only place
	# an answer would arrive is a red CI job. Called plainly, in the function's own body, it
	# means the same thing on every version the project supports.
	#
	# Anything that is not a patch argument survives the filter and is harmless: what is asked
	# of this is its intersection with :data:`ASKS_WHICH_OCCURRENCES`, which holds parameter
	# names and nothing else.
	arguments = locals()
	named = frozenset(
		name for name, value in arguments.items() if value is not subroutine.domain.patch.UNSET
	)

	# Permission first, before anything is even read: a caller who may not touch this task
	# should not be able to learn from the error message whether their new title was valid.
	# The version check follows it, for the same reason — a stranger should not learn what
	# version a task is at (docs/design.md §8.9).
	_permitted(
		session,
		actor,
		subroutine.permissions.TASK_WRITE,
		project=session.get(subroutine.db.models.project.Project, task.project_id),
		workspace_id=task.workspace_id,
	)
	subroutine.domain.versions.require(task, expected_version, noun="This task")
	refuse_an_answer_that_means_nothing(task, applies_to)
	refuse_an_edit_that_does_not_say(task, applies_to, named=named)

	# Validation pass. Nothing below this point may raise.
	cleaned_title: typing.Any = subroutine.domain.patch.UNSET if title is subroutine.domain.patch.UNSET else _clean_title(title)
	description = (
		description
		if description is subroutine.domain.patch.UNSET
		else subroutine.domain.text.readable(description, field="description")
	)
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

	# Same shape, same reason: ``None`` clears the reminder rather than being a duration.
	cleaned_reminder: typing.Any = reminder

	if reminder is not subroutine.domain.patch.UNSET and reminder is not None:
		# `reminder`, which is what both request models accept — not the column
		# (`#1534`). The terminal spells its flag `--remind`, which is a near miss
		# rather than a dead end, and is `#1547`'s question rather than this one.
		cleaned_reminder = subroutine.domain.durations.parse(reminder, field="reminder")

	# **§6.5's chain, and `task.timezone` is deliberately not in it** (`#1014`). It used to be
	# passed as `explicit`, which is the chain's *top* step — so the zone a task was created in
	# outranked the zone of everybody who touched it afterwards, for ever. Creation always
	# records one, so the user step was unreachable for every task that exists.
	#
	# That contradicted the column's own comment, which calls it *the zone the dates were
	# authored in* and gives its two purposes as recurrence and rendering. A record of a past
	# write had become an input to the next one.
	zone = _timezone(session, task.workspace_id, actor=actor, explicit=timezone)
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
	ending: typing.Any = _rescheduled(
		task.ends_at,
		given=ends,
		all_day=subroutine.domain.patch.UNSET,
		boundary=subroutine.domain.schedule.Boundary.END,
		zone=zone,
		now=instant,
		field="ends_at",
	)

	# **Both ends resolved against what the task will look like** — the rule the block below
	# states for invariant 8, and it bites harder here: a caller moving only the start of a
	# booked fortnight would otherwise be checked against nothing, and could push the
	# beginning past an end they never mentioned.
	unmoved = beginning is subroutine.domain.patch.UNSET
	unended = ending is subroutine.domain.patch.UNSET

	subroutine.domain.schedule.check_span(
		starts_at=task.starts_at if unmoved else beginning.instant,
		starts_is_all_day=task.starts_is_all_day if unmoved else beginning.is_all_day,
		ends_at=task.ends_at if unended else ending.instant,
		# **The start's flag when the end is not moving**, because there is only one: an end
		# has none of its own and inherits whatever the start already says.
		ends_is_all_day=task.starts_is_all_day if unended else ending.is_all_day,
		timezone=zone,
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
	# `completed_at` follows the status two blocks down. docs/design.md reserves
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
				"A sub-task belongs to the same project as its parent.",
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

	if cleaned_title is not subroutine.domain.patch.UNSET:
		task.title = cleaned_title

	if description is not subroutine.domain.patch.UNSET:
		task.description = description

	if item_type is not subroutine.domain.patch.UNSET:
		task.type_id = item_type.id

	if status is not subroutine.domain.patch.UNSET:
		task.status_id = status.id

		# docs/design.md §10.7 invariant 5: `completed_at` is non-null exactly when the status
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

	if reminder is not subroutine.domain.patch.UNSET:
		task.reminder_minutes = cleaned_reminder

	if deadline is not subroutine.domain.patch.UNSET:
		task.due_at = deadline.instant
		task.due_is_all_day = deadline.is_all_day

	if beginning is not subroutine.domain.patch.UNSET:
		task.starts_at = beginning.instant
		task.starts_is_all_day = beginning.is_all_day

	if ending is not subroutine.domain.patch.UNSET:
		task.ends_at = ending.instant

	if defer is not subroutine.domain.patch.UNSET:
		task.snoozed_until = defer.instant
		task.snoozed_is_all_day = defer.is_all_day

	# **A date rewritten in a new zone carries that zone with it** (`#1014`), which is what the
	# column promises: *the zone the dates were authored in*. Resolving in the caller's zone
	# and leaving the old one behind would move the contradiction rather than remove it — the
	# instant would land inside the reader's day while rendering, which reads the stored zone
	# (`#773`), went on naming a different one.
	#
	# **Only when a date actually moved.** A caller editing a title from another zone has
	# authored no date, and rewriting the column on their way past would silently re-render
	# every date on the task.
	#
	# The cost, which is real and pre-existing: one column serves three date fields, so
	# re-dating one of them re-renders the other two. A per-field zone is a schema change and
	# a decision nobody has taken; this is deliberately not that.
	if any(
		moved is not subroutine.domain.patch.UNSET for moved in (deadline, beginning, defer)
	):
		# **And the dates it leaves behind are moved onto the same days in the new zone**
		# (`#1327`). Relabelling the column without touching the instants leaves a row whose
		# two halves disagree, which since `#1296` puts it in no agenda bucket at all.
		was_written_in = task.timezone
		task.timezone = zone

		_resnapped(
			task,
			was_written_in=was_written_in,
			already_resolved=frozenset(
				column
				for column, moved in (
					("due_at", deadline),
					("starts_at", beginning),
					("ends_at", ending),
					("snoozed_until", defer),
				)
				if moved is not subroutine.domain.patch.UNSET
			),
			now=instant,
		)

	# **Applied before the "nothing changed" return below**, because a repeat lives on the
	# *series* rather than on this row: `changes_between` compares the task with itself and
	# sees nothing, so anything after that return is unreachable for a caller who changed
	# only how something repeats — which is every caller who came to change only that.
	# **Any of the three, not the rule alone** (`#918`). The two qualifiers were readable only
	# once the rule had been named, so *change how this is measured, keep the rule* reached
	# nothing at all — and answered as though it had.
	if (
		recurrence is not subroutine.domain.patch.UNSET
		or recurrence_anchor is not subroutine.domain.patch.UNSET
		or recurrence_trigger is not subroutine.domain.patch.UNSET
	):
		_repeat_changed(
			session,
			task,
			rule=recurrence,
			anchor=(
				None if recurrence_anchor is subroutine.domain.patch.UNSET else recurrence_anchor
			),
			trigger=(
				None
				if recurrence_trigger is subroutine.domain.patch.UNSET
				else recurrence_trigger
			),
			now=instant,
			actor=actor,
		)

	after = _snapshot(session, task)
	changes = subroutine.domain.events.changes_between(before, after)

	if not changes:
		return task

	# `updated_at` moves on any write; `content_updated_at` moves only when the *meaning*
	# changed, so that re-planning something does not read as rewriting it.
	#
	# **Asked of what changed rather than of what was sent** (`#1140`). This was six
	# `touches_content = True` assignments beside the assignment pass above, each guarded by
	# "was this field named in the request" — so a client that reads a task, edits its
	# importance and sends the whole object back re-sent an unchanged title and had its
	# bookkeeping recorded as a change of meaning. Which is most clients.
	#
	# **And the list is `events.CONTENT_FIELDS` rather than written out here**, because
	# `documents.update` needs the same rule and had a second, shorter copy of it (`#1112`).
	if subroutine.domain.events.touches_content("task", changes):
		task.content_updated_at = subroutine.db.types.utcnow()

	# **A worker that is writing to what it holds is still working** (`#1113`). Before the
	# version bump, so one increment covers the write and the renewal rather than two.
	subroutine.domain.claims.renewed(task, actor=actor, now=instant, settings=settings)

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

	# **Finishing gives the lease back** (`#1113`), after the event that says it was finished
	# so the record reads in the order it happened. A lease over work nobody can start protects
	# nothing, and a name on the row saying somebody is holding it is simply false.
	subroutine.domain.claims.released_if_finished(session, task, now=instant, actor=actor)

	# **After this row is settled and recorded, because the other one is a consequence of it**
	# (`#1247`). A change feed then reads in the order it happened: the row somebody edited, and
	# then the row that had to follow — rather than two writes with no way to tell which was
	# asked for.
	if applies_to == FROM_NOW_ON:
		_applied_to_the_series(
			session, task, was=before, now_holds=after, actor=actor, instant=instant
		)

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
	max_depth: int | None = None,
	settings: subroutine.config.Settings | None = None,
	expected_version: int | None = None,
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

	subroutine.domain.versions.require(task, expected_version, noun="task")

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
			"A sub-task belongs to the same project as its parent.",
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
		session, subroutine.db.models.work.Task, task, parent, max_depth=subroutine.domain.hierarchy.depth_limit(max_depth, settings)
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



#: What an edit to a repeating item can apply to — decision `#1249` §2.
#:
#: **Two answers rather than the three every calendar offers**, and structurally so. Google,
#: Apple and Outlook need *this-and-following* because they compute every occurrence from the
#: rule, so *all* would rewrite last March. This materialises one occurrence at a time and keeps
#: the finished ones as real rows the template never re-derives — **there is no past to rewrite**,
#: so *from now on* already means *this one and every one after*.
#:
#: Say it in those words on every surface. *All events* promises something about history that
#: does not happen.
#:
#: **The word for the question is ``applies_to`` and never ``scope``** (`#1275`). A published
#: field is a semver'd contract, and ``scope`` already means *permission narrowing* in this
#: API — a token carries ``scopes`` and a ``project_scope``, and `scoping` is the module that
#: decides which rows a caller may see. One word covering both is the hazard `#1267` recorded
#: about ``assigned_to_me``, met a second time and cheap to avoid before anything ships.
THIS_ONE = "this_one"
FROM_NOW_ON = "from_now_on"

ANSWERS = (THIS_ONE, FROM_NOW_ON)

#: What an answer never carries between a series and its occurrence — decision `#1249` §1.
#:
#: **These are the fields with no second answer**, which is why they are not a rule anybody has
#: to learn: nobody is ever shown a prompt where one of the two options would be meaningless.
#: Completing every future occurrence would end the series, which is what a series *running out*
#: already means (`#94`); the join itself is what tells the two rows apart. A claim is a lease on
#: rows that do not exist yet, and comments and links are not columns — what happened, happened
#: to that one.
NEVER_CARRIED = frozenset({"status_id", "completed_at", "recurrence_template_id"})

#: The columns that move by the same amount rather than to the same value.
#:
#: A series' date and its occurrence's are **meant** to differ — by however many turns of the
#: wheel are between them — so copying one onto the other would drag the whole grid back to the
#: row that happened to be edited. Moving both by the same delta is what "from 3pm from now on"
#: means, and it keeps every other occurrence where the rule puts it.
#:
#: ``occurrence_at`` travels with them and is not optional: it is the slot the series minted the
#: row for, and `#1248` reads *has this been moved* off exactly that comparison. Left behind, a
#: whole series shifting an hour would read as every occurrence individually rescheduled, and the
#: calendar feed would emit an ``EXDATE`` for a slot nothing had left.
MOVED_BY_DELTA = ("due_at", "starts_at", "ends_at", "snoozed_until")

#: Which all-day flag decides whether a moved column is a whole day rather than an instant.
#:
#: ``ends_at`` has none of its own by design (decision `#1235` §2, amended while it was being
#: built): a field constrained to equal another is not a field, so one flag describes both edges
#: of a span.
ALL_DAY_FLAG = {
	"due_at": "due_is_all_day",
	"starts_at": "starts_is_all_day",
	"ends_at": "starts_is_all_day",
	"snoozed_until": "snoozed_is_all_day",
}


def _moved_by (
	was: datetime.datetime, now_holds: datetime.datetime, *, whole_days: bool
) -> datetime.timedelta:
	"""Return how far a date moved, in the units the date is actually written in.

	**A whole-day date has no sub-day meaning, so a sub-day difference is not a move** (`#1291`).
	It reads as one to arithmetic, and that is what walked a repeating deadline a day forward
	on every save: §6.5 stores an all-day deadline at the last microsecond of its day,
	``dateutil`` keeps no microsecond, and the 999999µs it dropped came back as a delta this
	function's caller faithfully carried to the other row.

	**Rounded rather than truncated**, so a day that is 23 or 25 hours long still counts as one:
	the two ends are local day boundaries and a clock change moves one of them.

	**This is the half that protects rows written before the fix.** Their occurrences are still
	stored 999999µs behind their template, and without this the next save that carries a date —
	which the browser's form does on every save — would move the series a day. With it, that
	save quietly corrects the occurrence and carries nothing.
	"""

	moved = now_holds - was

	if not whole_days:
		return moved

	return datetime.timedelta(days=round(moved.total_seconds() / 86_400))


def _changed_shape (
	column: str, *, was: dict[str, typing.Any], now_holds: dict[str, typing.Any]
) -> bool:
	"""Say whether a date column went from a whole day to an instant, or back.

	**A shape change is not a move, and no delta can express it** (`#1303`). §6.5 stores an
	all-day deadline at the last microsecond of its day and an all-day start at the first, so
	the difference between the two instants is the edge as much as the day — 09:00 becoming
	*all day today* looks like a fourteen-hour move and is none.
	"""

	return bool(was.get(ALL_DAY_FLAG[column])) != bool(now_holds.get(ALL_DAY_FLAG[column]))


def _deltas (
	was: dict[str, typing.Any], now_holds: dict[str, typing.Any]
) -> dict[str, datetime.timedelta]:
	"""Return how far each date column moved, for the columns that moved by a knowable amount.

	**One computation, because there were two and both were wrong the same way** (`#1304`).
	:func:`_carried` built a dict and :func:`_applied_to_the_series` built a list from the same
	comprehension, and the linked defect — `#1302` — was in the *use* of both. Two copies that
	agree are invisible; two copies that agree about a mistake are invisible twice.

	A column is absent for three different reasons and they are worth telling apart: it was
	cleared or set from nothing, so there is a value rather than a move; it did not move; or
	its **shape** changed, which :func:`_changed_shape` takes out because the arithmetic is
	meaningless across it. Every caller has to say what it does with each.
	"""

	# **A zero delta is a member, and leaving it out is `SR#1334`.** This filtered on the walrus
	# — ``and (moved := _moved_by(...))`` — so a move that rounds to *no* days was falsy and the
	# column dropped out of the dict entirely. :func:`_carried` then reached its catch-all,
	# which reads an absent column as the *other* reason one can be absent (cleared, or set from
	# nothing) and copies the source's **absolute** value: a live occurrence re-dated in another
	# zone took its template a whole week forward, onto the occurrence's own date.
	#
	# **The three reasons a column is absent are named two paragraphs above and this made a
	# fourth silently.** *It did not move* is now expressed as a delta of zero rather than as
	# absence, so the vocabulary this docstring promises is the vocabulary the code speaks —
	# and applying a zero delta is a no-op, which is what *did not move* should do.
	return {
		column: _moved_by(
			was[column],
			now_holds[column],
			whole_days=bool(now_holds.get(ALL_DAY_FLAG[column])),
		)
		for column in MOVED_BY_DELTA
		if was.get(column) is not None and now_holds.get(column) is not None
		and not _changed_shape(column, was=was, now_holds=now_holds)
	}


def _reshaped (
	held: datetime.datetime,
	*,
	was: datetime.datetime,
	now_holds: datetime.datetime,
	column: str,
	whole_day: bool,
	timezone: str,
	now: datetime.datetime,
) -> datetime.datetime | None:
	"""Carry a date whose shape changed onto the other row of a series (`#1303`).

	The old code moved the date by a delta and copied the flag, so a shape change wrote a row
	claiming to be all-day at 14:00 — and the row it wrote is the **template**, which nothing
	re-derives, so ``materialise`` copied the broken instant into every future occurrence for
	the life of the series. :func:`~subroutine.domain.schedule.is_overdue` compares the stored
	instant and nothing else, so *due all day Wednesday* was then late from 15:00 on Wednesday.

	**Days rather than a duration**, because that is the only part of a shape change that
	survives it: the two rows are a whole number of local days apart, and rounding a timedelta
	gets that wrong at exactly the edges §6.5 stores things at. What lands is the target's own
	day moved by that many, wearing the new shape — the edge from :data:`~subroutine.domain.schedule.WHOLE_DAY_EDGE` when
	the date became a whole day, and the source's own time of day when it stopped being one.
	"""

	zone = subroutine.domain.dates.zone(timezone, column)
	moved = now_holds.astimezone(zone).date() - was.astimezone(zone).date()
	landing = held.astimezone(zone).date() + moved

	if whole_day:
		return whole_day_for(landing, field=column, timezone=timezone, now=now).instant

	# **Naive on purpose**: ``interpret`` reads a datetime without a zone in ``timezone``, which
	# is what puts the same wall-clock time on the landing day across a clock change.
	return subroutine.domain.schedule.interpret(
		datetime.datetime.combine(landing, now_holds.astimezone(zone).time()),
		boundary=subroutine.domain.schedule.WHOLE_DAY_EDGE[column],
		timezone=timezone,
		now=now,
		all_day=False,
		field=column,
	).instant


def _kept_on_its_grid (
	row: subroutine.db.models.work.Task,
	*,
	before: dict[str, typing.Any],
	deltas: dict[str, datetime.timedelta],
) -> None:
	"""Move a row's slot with the one date column it is a slot on (`#1302`).

	``occurrence_at`` follows :func:`grid_field` and nothing else. Moving it by whichever date
	happened to change first put it on no grid at all: a repeating meeting lengthened *from now
	on* moved its slot by the **end's** delta, and
	:func:`~subroutine.domain.calendars._is_on_its_grid` then read a row nobody had touched as
	one somebody had rescheduled by hand — so the feed drew the event twice and excluded a time
	the rule never emits.

	**A column that did not move leaves the slot alone**, which is the ordinary case for a
	series carrying both a start and a deadline.

	**A column whose shape changed has no delta**, so the only honest question left is whether
	the slot was on the grid before: if it was, it still is, and it takes the column's new
	value. A row somebody had already moved by hand stays moved.

	**And an edit can change which column is tracked at all**, which is the third case and the
	one the first version missed (`#1325`): adding a deadline to a series that had only a start
	moves the slot from one grid to the other, and a column set from nothing is in no delta. So
	*was this on the grid* has to be asked of the column the slot was on **before** the edit —
	reading it off the column it is on now asks about a value that was ``None``, which is never
	equal to anything, so the row was left stranded on a grid it is no longer measured against.
	:func:`~subroutine.domain.calendars._is_on_its_grid` then read a row nobody had touched as
	individually rescheduled, and the feed drew it twice. Clearing a deadline is the mirror.
	"""

	if row.occurrence_at is None:
		return

	tracked = grid_field(row)
	was_tracked = grid_field_for(before.get("due_at"))

	if tracked == was_tracked and tracked in deltas:
		row.occurrence_at += deltas[tracked]

		return

	held = grid_date(row)

	if held is not None and row.occurrence_at == before.get(was_tracked):
		row.occurrence_at = held


def _resnapped (
	task: subroutine.db.models.work.Task,
	*,
	was_written_in: str | None,
	already_resolved: frozenset[str],
	now: datetime.datetime,
) -> None:
	"""Move a row's untouched whole-day dates onto the same local day in its new zone (`#1327`).

	`#1014` rewrites ``task.timezone`` whenever a date was authored, and until `#1296` the cost
	of leaving the stored instants behind was only that they rendered an hour out. It decides
	**membership** now: a whole-day row is bucketed by comparing it against the edge of the day
	*its own zone column* names, so a row stored at London midnight and labelled New York
	matches no day's edge at all and falls out of every bucket, for every reader, while being
	counted under *dated further out*. Measured: a birthday re-dated by a colleague five hours
	away vanished from the agenda of all four readers it had been correct for.

	**The row is what is wrong, not the comparison.** Widening the comparison to tolerate it
	would have to tolerate a whole day at each end — the offset between two zones reaches
	twenty-six hours — which is exactly the precision the buckets are made of. So the write is
	where it is repaired: the day the row *meant* is read in the zone it was written in, and
	stored again at the edge of that same day in the zone it is being relabelled to.

	**A timed date is not touched**, because it genuinely is a point and `#1014`'s promise is
	that it keeps its moment. **Nor is a column this very call resolved**, which was already
	interpreted in the new zone and would be read back in the wrong one.

	It repairs one thing on its way past: a whole-day value stored anywhere other than its
	edge — the sub-microsecond drift `#1291` left behind on occurrences minted before it — is
	snapped as it is rewritten, because the day is what is read and the edge is what is
	written.
	"""

	relabelled = task.timezone

	# **A row that never said where it was written has no day of its own to keep**, which is
	# the same answer :func:`~subroutine.domain.agenda._edge` gives it: it takes the reader's
	# zone, so there is nothing here to repair.
	if relabelled is None or was_written_in is None or was_written_in == relabelled:
		return

	written_in = subroutine.domain.dates.zone(was_written_in, "timezone")

	for column in MOVED_BY_DELTA:
		held = getattr(task, column)

		if (
			held is None
			or column in already_resolved
			or not getattr(task, ALL_DAY_FLAG[column])
		):
			continue

		setattr(
			task,
			column,
			whole_day_for(
				held.astimezone(written_in).date(),
				field=column,
				timezone=relabelled,
				now=now,
			).instant,
		)


def _flags_held_back (
	*,
	was: dict[str, typing.Any],
	now_holds: dict[str, typing.Any],
	before: dict[str, typing.Any],
	only_where_unchanged: bool,
) -> frozenset[str]:
	"""Name the all-day flags whose own date column is not being carried (`#1324`).

	**A flag and the date it describes are one fact and have to be decided together.**
	``only_where_unchanged`` holds a column back when the target already differs from the
	source's old value — *somebody moved this one by hand, leave it alone* — and past the first
	turn of a series that is **always** true of the dates, because an occurrence is a whole grid
	shift ahead of its template by construction. The flag is not held back by that test, because
	both rows still agree about it, so a shape change made at the *template* end skipped
	``due_at`` and copied ``due_is_all_day`` on its own. What landed on the live row was an
	all-day deadline stored at 09:00 — the row §6.5 says cannot exist and
	:func:`_reshaped` was written to prevent, written by the function that fixes it.

	**Held back only when every date naming it is**, which is what keeps a span honest:
	:data:`ALL_DAY_FLAG` points both edges at ``starts_is_all_day``, so a flag whose other end
	*is* being reshaped still has to travel — :func:`_reshaped` has already written that shape
	into the instant, and leaving the flag behind would state the opposite.
	"""

	if not only_where_unchanged:
		return frozenset()

	moving = [
		column
		for column in MOVED_BY_DELTA
		if column in now_holds and was.get(column) != now_holds[column]
	]

	carried = {
		ALL_DAY_FLAG[column]
		for column in moving
		if before.get(column) == was.get(column)
	}

	return frozenset(
		{ALL_DAY_FLAG[column] for column in moving if before.get(column) != was.get(column)}
		- carried
	)


def _carried (
	session: sqlalchemy.orm.Session,
	target: subroutine.db.models.work.Task,
	*,
	was: dict[str, typing.Any],
	now_holds: dict[str, typing.Any],
	only_where_unchanged: bool,
	actor: subroutine.domain.authentication.Principal | None,
	instant: datetime.datetime,
) -> None:
	"""Carry one row of a series' change onto the other, and record it there.

	``was`` and ``now_holds`` are the *source* row before and after — snapshots rather than the
	rendered change list, because this compares against live column values and
	:func:`~subroutine.domain.events.changes_between` has already put its own through
	``jsonable``, where a datetime is a string and a UUID is a string.

	**``only_where_unchanged`` is decision `#1249` §3, and it has exactly one direction.** When
	the *series* is edited, the live occurrence takes the change only where it still held the
	series' old value — so a title somebody set on this occurrence alone is not silently undone
	by a later *from now on*. When the *occurrence* is edited there is no such question: the row
	the person is looking at is the one they changed.

	**An override needs no column** and that is the whole point of the rule: a field is
	overridden exactly when it differs from the series, and the old value is in hand because it
	is the row before the update.

	The bookkeeping is written out rather than routed back through :func:`update`, and the
	reason is that this is a **copy** rather than an edit: every value here has already been
	validated on the row it came from, and re-validating would ask the far row's other columns
	questions the caller never answered — a start moved to 3pm being checked against an end that
	is about to move with it.
	"""

	before = _snapshot(session, target)
	deltas = _deltas(was, now_holds)
	held_back = _flags_held_back(
		was=was,
		now_holds=now_holds,
		before=before,
		only_where_unchanged=only_where_unchanged,
	)

	# **Resolved only when something is going to be re-shaped**, which is rare: §6.5's chain
	# reaches the workspace and the instance, and every other carried edit is a copy.
	zone = (
		_timezone(session, target.workspace_id, actor=actor, explicit=None)
		if any(
			_changed_shape(column, was=was, now_holds=now_holds) for column in MOVED_BY_DELTA
		)
		else ""
	)

	for column, value in now_holds.items():
		if column in NEVER_CARRIED or was.get(column) == value:
			continue

		if only_where_unchanged and before.get(column) != was.get(column):
			continue

		# **A flag whose date stayed behind stays with it** (`#1324`), or the row claims a
		# shape its stored instant does not have.
		if column in held_back:
			continue

		if column == "tags":
			subroutine.domain.tags.set_on(
				session,
				target,
				subroutine.domain.tags.ensure(
					session, workspace_id=target.workspace_id, names=value
				),
			)

		elif column in deltas and getattr(target, column) is not None:
			# **Moved, not copied.** The two rows are meant to hold different dates; what they
			# share is the shape of the move.
			setattr(target, column, getattr(target, column) + deltas[column])

		elif (
			column in MOVED_BY_DELTA
			and getattr(target, column) is not None
			and was.get(column) is not None
			# **Both ends of the move, not just the near one** (`#1323`). Clearing a date
			# flips its flag too, so the shape *did* change and there is still nothing to
			# reshape onto — :func:`_reshaped` read ``None.astimezone`` and the call was a
			# 500 rather than one of the typed refusals. The branch below already says the
			# right thing about a cleared column.
			and value is not None
			and _changed_shape(column, was=was, now_holds=now_holds)
		):
			# **A shape change carries as days and a new edge, never as a delta** (`#1303`).
			setattr(
				target,
				column,
				_reshaped(
					getattr(target, column),
					was=was[column],
					now_holds=value,
					column=column,
					whole_day=bool(now_holds.get(ALL_DAY_FLAG[column])),
					timezone=zone,
					now=instant,
				),
			)

		elif column in MOVED_BY_DELTA:
			# A date that was cleared, or set from nothing, has no delta to apply — the value
			# is the only thing there is to say.
			setattr(target, column, value)

		else:
			setattr(target, column, value)

	# **The slot moves with the one date it is a slot for, and never with the others** (`#1302`).
	# `#1248` reads *has this been moved* off ``occurrence_at`` against the row's own date, so
	# leaving it behind would make a series shifting an hour look like every occurrence being
	# individually rescheduled — and the feed would emit an ``EXDATE`` for a slot nothing had
	# left. Read after the loop, so the column asked about is the one the row holds *now*.
	_kept_on_its_grid(target, before=before, deltas=deltas)

	session.flush()

	changes = subroutine.domain.events.changes_between(before, _snapshot(session, target))

	if not changes:
		return

	if subroutine.domain.events.touches_content("task", changes):
		target.content_updated_at = subroutine.db.types.utcnow()

	target.version += 1
	target.updated_by = None if actor is None else actor.user.id
	session.flush()

	if "title" in changes or "description" in changes:
		subroutine.domain.mentions.synchronize(
			session,
			workspace_id=target.workspace_id,
			source_type="task",
			source_id=target.id,
			texts=(target.title, target.description),
		)

	subroutine.domain.events.record(
		session,
		workspace_id=target.workspace_id,
		entity_type="task",
		entity_id=target.id,
		action=subroutine.domain.events.EventAction.UPDATED,
		changes=changes,
		actor=actor,
	)
	session.flush()


def _applied_to_the_series (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	*,
	was: dict[str, typing.Any],
	now_holds: dict[str, typing.Any],
	actor: subroutine.domain.authentication.Principal | None,
	instant: datetime.datetime,
) -> None:
	"""Send an edit to the other row of the series, whichever end the caller was holding.

	**Decision `#1249` §1 and §4 are one mechanism seen from two sides.** A person is not aware
	that there are two rows: for them there is one event that reoccurs. So an edit that says
	*every one from now on* has to reach the row that persists, and an edit made **to** that row
	has to reach the one they are looking at — or `subroutine list` goes on showing the old
	title until the thing next comes round, which is the defect arriving from the other side.
	"""

	# **The row that was edited keeps its own slot in step, and it is not the same statement as
	# the copy below.** When a series moves, its live occurrence has not been *individually*
	# rescheduled — so ``occurrence_at`` has to move with the date or `#1248` reads the whole
	# series shifting an hour as every occurrence being moved by hand, and the feed excludes a
	# slot nothing left. Applied to whichever row was addressed; the other gets it in
	# :func:`_carried`.
	#
	# ``was`` is this row's own before, so it is the grid test :func:`_kept_on_its_grid` needs.
	_kept_on_its_grid(task, before=was, deltas=_deltas(was, now_holds))
	session.flush()

	if task.is_template:
		occurrence = live_occurrence(session, task)

		if occurrence is not None:
			_carried(
				session,
				occurrence,
				was=was,
				now_holds=now_holds,
				only_where_unchanged=True,
				actor=actor,
				instant=instant,
			)

		return

	series = series_of(session, task)

	if series is not None:
		_carried(
			session,
			series,
			was=was,
			now_holds=now_holds,
			only_where_unchanged=False,
			actor=actor,
			instant=instant,
		)


#: Every argument of :func:`update` that patches a field, derived rather than listed.
#:
#: **A patchable parameter is exactly one whose default is the patch sentinel**, so this is a
#: measurement of the signature rather than a copy of it. `#1268` is why: a hand-written
#: register of `update`'s fields was two short, and the two guards built on it were both blind
#: to the gap because each read the register rather than the function.
PATCHABLE = frozenset(
	name
	for name, parameter in inspect.signature(update).parameters.items()
	if parameter.default is subroutine.domain.patch.UNSET
)

#: The patchable fields with only one answer on a repeating item — decision `#1249` §1.
#:
#: **Nobody is ever shown a prompt where one of the two answers would be meaningless**, which
#: is what stops this being a rule a person has to learn. Two reasons, and the second is
#: measured rather than argued:
#:
#: - a status is `#1249` §1's first row — completing every future occurrence would end the
#:   series, which is what a series *running out* already means (`#94`), and starting all of
#:   them means nothing;
#: - the three repeat arguments edit **how this repeats**, which lives on the series and
#:   nowhere else. ``_repeat_changed`` already routes them there whichever row was addressed,
#:   so there is no second row for an answer to choose between.
#:
#: The claim, comments and links are `#1249` §1's other three rows and are not here because
#: they are not fields of an update at all.
#:
#: **Everything else asks, by subtraction rather than by enrolment**, so a field added to
#: :func:`update` asks until somebody writes down why it should not.
NEVER_ASKS = frozenset(
	{"status_key", "recurrence", "recurrence_anchor", "recurrence_trigger"}
)

ASKS_WHICH_OCCURRENCES = PATCHABLE - NEVER_ASKS


def repeats (task: subroutine.db.models.work.Task) -> bool:
	"""Say whether a task is one of a series, from either end of it.

	**Both ends, deliberately.** The row a person is looking at is the occurrence and the row
	that persists is the template, and an edit can arrive addressed to either — `show` names
	the other one now (`#1247`), so the template is reachable and reaching it must not be a
	way round the question.
	"""

	return task.recurrence_template_id is not None or task.is_template


def refuse_an_edit_that_does_not_say (
	task: subroutine.db.models.work.Task,
	applies_to: str | None,
	*,
	named: frozenset[str],
) -> None:
	"""Refuse a change to a repeating item that has not said which occurrences it is for.

	Decision `#1249` §5, and **Simon took the breaking half knowingly**: this answered 200
	yesterday and answers 422 now. The alternative was keeping today's behaviour as the
	default, and he refused it — an agent silently getting *just this one* is the whole
	failure `#1247` reports.

	**The precedent is §12.6a**, where ``db restore`` refuses without ``--recover`` or
	``--as-clone`` because both defaults are wrong half the time and the damage is invisible
	in both directions. This is that, exactly: a title correction that reaches one occurrence
	expires next month, and a time change that reaches the series moves a meeting nobody
	agreed to move.

	**A change that names no asking field is not refused**, so completing something, moving it
	between statuses or altering how it repeats all go through untouched — there is no second
	answer to any of them and being asked would be friction with no decision in it.

	The refusal says what to do and does not name the fields, because the names here are this
	function's arguments rather than the caller's: ``status_key`` and ``assignee_id`` are not
	words anybody sent, and a refusal naming the wrong field is `#1259`'s defect.
	"""

	if applies_to is not None or not repeats(task):
		return

	if not named & ASKS_WHICH_OCCURRENCES:
		return

	raise subroutine.errors.ValidationError(
		"That repeats, so this change has to say which occurrences it is for.",
		code="missing_field",
		hint=f"Say {THIS_ONE} to change this one, or {FROM_NOW_ON} for every one after it too.",
		errors=[
			subroutine.errors.FieldError(
				field="applies_to",
				code="missing_field",
				message=(
					"An edit to a repeating item says whether it is for this one or every "
					"one from now on."
				),
			)
		],
	)


def refuse_an_answer_that_means_nothing (
	task: subroutine.db.models.work.Task, applies_to: str | None
) -> None:
	"""Refuse an answer about something that does not repeat, and an unknown one anywhere.

	**Ignoring it would be the inert control this codebase has found three times** — a setting
	that is accepted, documented and read by nothing. Somebody who says *from now on* about a
	one-off has misunderstood something, and the cheapest moment to say so is the one where they
	said it.
	"""

	if applies_to is None:
		return

	if applies_to not in ANSWERS:
		raise subroutine.errors.ValidationError(
			f"{applies_to!r} is not a way for an edit to apply to a repeat.",
			code="invalid_field_value",
			errors=[
				subroutine.errors.FieldError(
					field="applies_to",
					code="invalid_field_value",
					message=f"The answers are: {', '.join(ANSWERS)}.",
				)
			],
		)

	if not repeats(task):
		raise subroutine.errors.ValidationError(
			"That does not repeat, so there is only one of it to change.",
			code="invalid_field_value",
			hint="Leave it out and the change applies to the one item there is.",
			errors=[
				subroutine.errors.FieldError(
					field="applies_to",
					code="invalid_field_value",
					message="Only an edit to a repeating item says which occurrences it is for.",
				)
			],
		)


def live_occurrence (
	session: sqlalchemy.orm.Session, template: subroutine.db.models.work.Task
) -> subroutine.db.models.work.Task | None:
	"""Return the one unfinished occurrence of a series, or ``None`` if there is none.

	**There is exactly one at a time**, which is what makes decision `#1249` §4's write-through
	well defined: `materialise` mints the next only when the last is finished. ``None`` is an
	ordinary answer rather than a failure — a series whose rule is spent has no live row, and
	neither has a template somebody is holding mid-creation.

	Ordered by the slot it was minted for so that a database which has somehow been left with
	two answers the same question the same way twice, rather than differently each call.
	"""

	task = subroutine.db.models.work.Task

	return session.scalars(
		sqlalchemy.select(task)
		.where(
			task.recurrence_template_id == template.id,
			task.completed_at.is_(None),
			task.deleted_at.is_(None),
		)
		.order_by(task.occurrence_at.asc().nulls_last(), task.id.asc())
		.limit(1)
	).first()


def series_of (
	session: sqlalchemy.orm.Session, task: subroutine.db.models.work.Task
) -> subroutine.db.models.work.Task | None:
	"""Return the rule-bearing row behind a task, whichever end the caller is holding.

	**A person is always looking at the occurrence.** The rule lives on the template, which is
	in no listing and which nobody navigates to — so a change to *how this repeats* arrives
	addressed to the instance and has to be routed. Returning ``None`` for a task that is part
	of no series is what lets a caller tell "not repeating" from "template missing".
	"""

	if task.is_template:
		return task

	if task.recurrence_template_id is None:
		return None

	return session.get(subroutine.db.models.work.Task, task.recurrence_template_id)


def stop_repeating (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	*,
	now: datetime.datetime | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Task:
	"""End a series, leaving the occurrence in hand exactly as it is.

	**Stopping is not deleting and not clearing.** The occurrence somebody is looking at is
	real work with a ref, a history and possibly comments; the series ending says only that
	nothing follows it. So the template is completed — which records what repeated and until
	when — and the live occurrence is untouched.

	Idempotent, for `#723`'s reason: stopping something already stopped is not a second act
	and must not move the record of when it happened.
	"""

	series = series_of(session, task)

	if series is None:
		raise subroutine.errors.ValidationError(
			"That is not part of a repeating series, so there is nothing to stop.",
			code="invalid_field_value",
			hint="Only something that repeats can stop repeating.",
			errors=[
				subroutine.errors.FieldError(
					field="recurrence",
					code="invalid_field_value",
					message="This task does not repeat.",
				)
			],
		)

	if series.completed_at is None:
		complete(session, series, now=now, actor=actor)

	return task



def begin_repeating (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	repeat: subroutine.domain.recurrence.Repeat,
	*,
	now: datetime.datetime,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Task:
	"""Make an existing task the first occurrence of a new series.

	**The task stays the task.** Somebody adding a repeat to something already on their list
	is not asking for it to be replaced — it has a ref they have written down, a history and
	perhaps comments — so a template is created *from* it and the task becomes that template's
	first occurrence. Turning the task itself into the template would take it out of every
	listing and put an identical-looking stranger in its place.
	"""

	template = subroutine.db.models.work.Task(
		id=subroutine.db.types.new_uuid(),
		workspace_id=task.workspace_id,
		project_id=task.project_id,
		parent_task_id=task.parent_task_id,
		type_id=task.type_id,
		ref=subroutine.domain.refs.allocate(session, task.workspace_id),
		title=task.title,
		description=task.description,
		status_id=task.status_id,
		assignee_id=task.assignee_id,
		importance=task.importance,
		urgency=task.urgency,
		estimate_minutes=task.estimate_minutes,
		due_at=task.due_at,
		due_is_all_day=task.due_is_all_day,
		starts_at=task.starts_at,
		starts_is_all_day=task.starts_is_all_day,
		timezone=task.timezone,
		recurrence_rule=repeat.rule,
		recurrence_text=repeat.text,
		recurrence_anchor=repeat.anchor,
		recurrence_trigger=repeat.trigger,
		is_template=True,
		path="",
		depth=0,
		created_by=None if actor is None else actor.user.id,
	)
	subroutine.domain.hierarchy.place(
		template, None, max_depth=subroutine.domain.hierarchy.DEFAULT_MAX_DEPTH
	)

	session.add(template)
	session.flush()

	# **The tags travel too** (`#1326`). Every occurrence inherits the template's tags
	# through :func:`materialise`, and the template built here is a copy of named columns —
	# a tag is a join and was not one of them, so a rule added to a task that already
	# carried tags lost them from the second turn of the wheel onwards. `#1307` fixed the
	# reading end and its tests all build the series through ``create``, where the row the
	# caller's tags landed on *is* the template and there is nothing to copy.
	subroutine.domain.tags.set_on(
		session, template, subroutine.domain.tags.on(session, task)
	)

	# Refused after the template exists rather than before, so the message is the one
	# `series_start` gives — one rule about what a repeat needs a date for, in one place.
	series_start(template)

	task.recurrence_template_id = template.id
	# **The slot this row was minted for**, on the one column :func:`grid_field` names — the
	# rule was written out here too, and a copy that agrees is invisible (`#1302`).
	task.occurrence_at = grid_date(task)

	subroutine.domain.events.record(
		session,
		workspace_id=template.workspace_id,
		entity_type="task",
		entity_id=template.id,
		action=subroutine.domain.events.EventAction.CREATED,
		changes={
			"ref": {"from": None, "to": template.ref},
			"title": {"from": None, "to": template.title},
		},
		actor=actor,
	)
	session.flush()

	return template



def _repeat_changed (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	*,
	rule: str | None,
	anchor: str | None,
	trigger: str | None,
	now: datetime.datetime,
	actor: subroutine.domain.authentication.Principal | None,
) -> None:
	"""Apply a change to how a task repeats, whichever end the caller is holding.

	**Editing a repeat edits the series, not this occurrence** (§6.7). The caller is looking
	at the instance because the template is in no listing, so a rule addressed to the instance
	is addressed to the series — and the alternative, applying it to one occurrence, would be
	a rule on a row that mints nothing and is silently forgotten the moment it is completed.

	``rule`` carries three answers, not two. ``UNSET`` is *leave the rule alone and change
	what qualifies it*, ``None`` stops the series, and a string replaces the rule.
	:func:`stop_repeating` carries why stopping is not clearing a column.
	"""

	series = series_of(session, task)

	# **Changing how an existing repeat is measured, without re-sending the rule** (`#918`).
	# The anchor and the trigger used to be readable only inside the rule's own branch, so
	# naming either alone reached nothing, moved no version and answered *Changed* — the
	# capability was unreachable from every surface and every surface reported success.
	if rule is subroutine.domain.patch.UNSET:
		if series is None:
			raise subroutine.errors.ValidationError(
				"That describes how something repeats, and this does not repeat.",
				code="invalid_field_value",
				hint="Give it a repeat first — this describes one rather than starting one.",
				errors=[
					subroutine.errors.FieldError(
						field=field,
						code="invalid_field_value",
						message="This qualifies a repeat, and this task does not have one.",
					)
					for field, value in (
						("recurrence_anchor", anchor), ("recurrence_trigger", trigger)
					)
					if value is not None
				],
			)

		# The rule the series already carries, so the two qualifiers are re-checked against it
		# — `_repeat` is what refuses a `time` trigger and the pair that cannot mean anything,
		# and routing round it here would be a second opinion about the same combination.
		rule = series.recurrence_rule
		anchor = anchor or series.recurrence_anchor
		trigger = trigger or series.recurrence_trigger

	if rule is None:
		if series is not None:
			stop_repeating(session, task, now=now, actor=actor)

		return

	repeat = _repeat(rule, anchor=anchor, trigger=trigger)

	if repeat is None:
		return

	if series is None:
		begin_repeating(session, task, repeat, now=now, actor=actor)

		return

	series.recurrence_rule = repeat.rule
	series.recurrence_text = repeat.text
	series.recurrence_anchor = repeat.anchor
	series.recurrence_trigger = repeat.trigger
	series.version += 1

	# **Re-opened if it had been stopped**, because setting a rule on a stopped series is
	# somebody restarting it, and a finished template mints nothing.
	if series.completed_at is not None:
		update(session, series, status_key=status_for(session, series.workspace_id, None).key, now=now, actor=actor)

	session.flush()


def delete (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	*,
	now: datetime.datetime | None = None,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.work.Task:
	"""Move a task to the trash, where it stays recoverable (docs/design.md §6.9).

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
	"""Take a task back out of the trash (docs/design.md §6.9).

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
		# `#1268`. Writable since `#1211` and absent from here, so setting a reminder wrote the
		# column, recorded nothing, and — because `update` returns before the bump when nothing
		# differs — **left `version` where it was**. §8.9's guard then compared a number that
		# never moved for this field.
		"reminder_minutes": task.reminder_minutes,
		# `#1268`. What joins a row to its series, and the field that changes when a one-off
		# becomes a repeat. *This now happens every week* is not a small edit, and it was
		# invisible in the feed and in the version for the same reason as the reminder above.
		"recurrence_template_id": task.recurrence_template_id,
		"due_at": task.due_at,
		"due_is_all_day": task.due_is_all_day,
		"ends_at": task.ends_at,
		"starts_at": task.starts_at,
		# `#1016`. Its two siblings were both here and this one was not, so flipping only
		# whether a start is a whole day moved the row, bumped the version, and left no trace
		# — reachable, because an all-day start and a timed midnight are the same instant.
		"starts_is_all_day": task.starts_is_all_day,
		"snoozed_until": task.snoozed_until,
		"snoozed_is_all_day": task.snoozed_is_all_day,
		# `#1014`. Writable since a date rewritten in a new zone carries that zone with it, and
		# a field `update` can write that this dict forgets produces no event at all.
		"timezone": task.timezone,
	}


def _timezone (
	session: sqlalchemy.orm.Session,
	workspace_id: uuid.UUID,
	*,
	actor: subroutine.domain.authentication.Principal | None,
	explicit: str | None,
) -> str:
	"""Return the timezone this task's dates are read in, per docs/design.md §6.5's chain.

	The workspace and the instance are fetched only when the answer is not already settled,
	so the common path — a person with a timezone, editing their own tasks — costs no query.

	**A zone the caller sent is checked here, and used to be taken on trust** (`SR#1561`). The
	identifier was only ever validated incidentally, inside ``schedule.interpret``, which runs
	when a date is supplied — so a task created with ``"Mars/Olympus"`` and no date was stored
	happily and could then never be given one. Every later attempt was refused for the *stored*
	zone, naming a value the caller had not sent, and sending a good zone on its own changes
	nothing (`#1014`), so there was no way back: only deleting the row cleared it.

	The workspace, the user and the instance were each already checked on the way in. This is
	the fourth member of that family and the only one with a column of its own, which is what
	kept it out of the rule — a previous review recorded it as unreachable *because every write
	path resolves the zone through* ``dates.zone`` *first*, true of the other three.
	"""

	if explicit:
		# Resolved and discarded: what is wanted is the refusal, and the column stores the
		# identifier rather than the zone.
		subroutine.domain.dates.zone(explicit, "timezone")

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
	session: sqlalchemy.orm.Session,
	workspace_id: uuid.UUID,
	key: str,
	*,
	field: str = "type",
) -> subroutine.db.models.vocabulary.ItemType:
	"""Return a task type by key, or list the ones this workspace has.

	``field`` names the thing the caller sent, because a refusal has to name a field that
	caller actually has — `#547`'s rule. A calendar feed's type filter is ``item_types``, and
	being told to correct ``type`` sends somebody looking for a field their request has not
	got. The second caller is why this is a parameter rather than a literal.
	"""

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
				field=field,
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


def default_order (
	*,
	status: subroutine.db.models.vocabulary.Status | None = None,
	category: str | None = None,
) -> tuple[str, ...]:
	"""Return what a task listing is ordered by when the caller named no order.

	**A listing that holds only finished work is ordered by when it finished** (`#1150`, Simon:
	*"completed items should always be ordered by their completed date. Importance and urgency
	are no longer factors for ordering, when an item is done"*). Everything else keeps
	newest-first, which is what *what have I got* means for a to-do list.

	**Both ways of narrowing count, and taking them together is why this is a function.**
	``status_category=done`` says it outright; ``status=done`` names one status and that status
	has a category. A caller that checked only the first would leave `subroutine list --status
	done` — the spelling the terminal actually offers, since ``--status`` takes a *key* — reading
	as unnarrowed.

	:data:`FINISHED_CATEGORIES` is read rather than the key, so a workspace that renames ``done``
	to ``shipped`` is covered — and both categories in it carry a ``completed_at`` by §10.7
	invariant 5, so there is nothing null to sort around.

	**Here rather than at the two call sites**, because a default the endpoint applies and the
	terminal does not is the divergence :mod:`subroutine.domain.ordering` exists to prevent —
	and it was already real: the browser's *done* view asked for ``-completed_at`` as a literal
	of its own while `subroutine list --status done` and every board's finished column got
	``-created_at``. One surface of three had the rule, and had it in a place the other two
	could not inherit.

	:func:`completion_wanted` is its neighbour and takes the same two inputs to answer a
	*different* question — whether a listing should **reach** finished work at all, which
	``include_completed=true`` makes true of a listing that is mostly unfinished. Reaching is
	not the same as holding nothing else, and only the second decides an order.

	**Only where the whole listing is finished.** A *mixed* listing — a board, or
	``?include_completed=true&order=-priority_score`` — is the stronger reading of *always*, and
	it wants a fourth band in §6.3a's three. That rule exists twice by necessity and a
	disagreement between the halves is a page boundary that skips rows, so it is `#1152` and a
	decision rather than a continuation of this.
	"""

	# A set rather than a precedence, because there is no right answer when a caller says both
	# contradictory things — `status=done&status_category=todo` is an empty listing whichever
	# order this picks, so the question is not worth a rule.
	narrowed_to = {category, status.category if status is not None else None}

	if narrowed_to & FINISHED_CATEGORIES:
		return tuple(subroutine.domain.ordering.FINISHED_TASK_ORDER)

	return tuple(subroutine.domain.ordering.DEFAULT_TASK_ORDER)


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
	status_named: subroutine.db.models.vocabulary.Status | None = None,
	about_completion: bool = False,
	about_activity: bool = False,
	about_deletion: bool = False,
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

	**``about_deletion`` is the fourth, and it is the trash** (`#900`). ``deleted=true`` asks
	*what did I delete*, and what an item's status happened to be is no part of that question —
	finishing something and then deleting it is entirely ordinary. Measured on the served
	instance: 23 rows against 26 with completion asked for, so three deleted items were
	reachable by ``subroutine show`` and by **no listing at all**. A reader looking for
	something they deleted is told it is not there, and a reader emptying the trash empties
	part of it.

	It is honoured rather than refused, like the two above and unlike ``about_completion``:
	*what is in the trash that I had not finished* is a coherent question, where *finished work
	and no finished work* is not.

	**``naming_one_item`` is the third spelling, and it is the one that hurt** (`#873`). `#867`
	made a search that is exactly a ref match the item with that number — and 548 of this
	instance's 721 tasks are finished, so for **three items in four** the lookup found the row
	and the listing then hid it. Every other selection *narrows a set*; a ref names **one item**
	and the reader has already decided which, so answering "nothing" about something
	``subroutine show`` reads happily is `#700`'s divergence between a lookup and a listing.

	**``status_named`` is the fifth, and it is the plainest of the five** (`#1032`). A caller
	who names the *status key* their workspace uses for finished work is asking for finished
	work as unambiguously as one naming the category, and this function knew only the category.
	Measured on the served instance, same minute, same rows:

	    subroutine list --status done                 ->  0 rows
	    subroutine list --filter completed_at.gte=…   ->  five finished items

	**The resolved status is taken rather than its key**, because the category is the stable
	handle and the key is workspace vocabulary (§5.5): an installation that renames ``done``
	still has a status whose category is ``done``, and a rule matching the word would go quiet
	the moment somebody renamed it — which is exactly `#496`'s shape.

	`#818`'s own sentence is the lesson and this is now its **fifth** instance: *a rule written
	down in one vocabulary does not reach the next one.* It knew about categories, then about
	filters, then about a lookup, then about the trash, and not about the status key sitting
	beside the category in the same query string. Five spellings of one sentence, each found
	separately and none by the guard written for the last — which is the argument for asking,
	of any narrowing added here, whether completion is part of what it asked about.

	**It widens the whole listing rather than exempting one row**, which is a choice worth
	stating. Exempting only the matched row would mean pushing the ref down into
	``scoping.readable_tasks``, and the result set for a bare number is small anyway — four to
	twenty-one rows, measured — so what widening actually adds is the *finished* items that
	mention that number, which is a fair reading of what somebody typing a ref is asking.
	``include_completed=false`` is still honoured, as with ``about_activity``, because *the open
	item numbered 815* is a coherent question.
	"""

	named_finished = (
		status_named is not None and status_named.category in FINISHED_CATEGORIES
	)

	wants_finished = (
		about_completion
		or named_finished
		or (category is not None and category in FINISHED_CATEGORIES)
	)

	if not wants_finished:
		# Not `bool(asked)`: three-valued, so *did not say* means include and *said no* means
		# exclude. Collapsing them would make the answer ignore a caller who was explicit.
		return (
			asked is not False
			if (about_activity or about_deletion or naming_one_item)
			else bool(asked)
		)

	if asked is False:
		raise subroutine.errors.ValidationError(
			_excluding_all_of_it(category, status_named),
			errors=[
				subroutine.errors.FieldError(
					field="include_completed",
					code="invalid_field_value",
					message=(
						f"{_asking_for_it(category, status_named)} asks only for finished "
						"work and include_completed=false excludes all of it."
					),
					hint="Drop include_completed — asking about finished work implies it.",
				)
			],
		)

	return True


def _asking_for_it (
	category: str | None,
	status_named: subroutine.db.models.vocabulary.Status | None,
) -> str:
	"""Name whichever part of the request asked for finished work.

	**In the caller's own spelling, because a refusal naming a parameter they did not send is
	unfollowable** (`#547`). Somebody who wrote ``status=done`` and is told about
	``status_category`` goes looking for a parameter that is not in their request.
	"""

	if category is not None and category in FINISHED_CATEGORIES:
		return f"status_category={category!r}"

	if status_named is not None and status_named.category in FINISHED_CATEGORIES:
		return f"status={status_named.key!r}"

	return "a filter on completed_at"


def _excluding_all_of_it (
	category: str | None,
	status_named: subroutine.db.models.vocabulary.Status | None,
) -> str:
	"""Say what the contradiction was, in the caller's own terms."""

	if category is not None and category in FINISHED_CATEGORIES:
		return f"{category!r} is finished work, so excluding finished work leaves nothing."

	if status_named is not None and status_named.category in FINISHED_CATEGORIES:
		return (
			f"{status_named.key!r} is finished work here, so excluding finished work "
			"leaves nothing."
		)

	return (
		"completed_at is only ever set on finished work, so excluding finished work "
		"leaves nothing."
	)
