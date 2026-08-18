"""The three date fields, and the all-day rule that makes them behave.

docs/design.md §6.5 keeps a **deadline** (``due_at``), a **start** (``starts_at``) and a **defer
instant** (``snoozed_until``) apart, because conflating them is what makes an overdue list
meaningless within a month. This module is where user input becomes those columns and where
the rules between them are enforced.

**The middle one used to be two columns and one of them lied** (`#854`). There was a
``planned_for`` date beside a ``start_at`` instant, and ``start_at`` was read as *hide this
until* by every consumer — so an appointment at two o'clock was stored as a defer and
vanished from the list until two o'clock. ``starts_at`` absorbed the planned day, because
*planned for Tuesday* is *starts Tuesday, all day*; ``snoozed_until`` is the old column under
a name that says what it does. **Only one of the three hides a row**, and that is the
distinction the rename exists to make visible.

**The all-day rule is the part that is easy to get wrong and slow to notice.** "Due Friday"
is a date, not an instant, so it has to be stored as one — and the obvious choice, midnight,
makes a task due Friday overdue for the whole of Friday. Deadlines therefore store the
**end** of the day (23:59:59.999999 local, converted to UTC) and defers store the **start**.
A naive implementation passes every test anybody thinks to write, and then a user asks why
their morning is full of things that are not late.

Nothing here touches the database. It takes values in and produces columns, so that the
same rules apply to the CLI, to quick capture and to the API without any of them owning
them.
"""

import dataclasses
import datetime
import enum
import re
import zoneinfo

import subroutine.db.models.identity
import subroutine.db.models.system
import subroutine.db.models.work
import subroutine.domain.dates
import subroutine.errors

#: The fallback when neither the person nor their workspace has said (docs/design.md §6.5).
DEFAULT_TIMEZONE = "UTC"

#: A date with no time — ``2026-08-01``. Matched before the datetime parser, which would
#: otherwise read it as midnight and quietly turn a whole day into an instant.
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: For each stored date column: the name a caller **writes** it under, and the all-day flag
#: beside it. Three spellings of one field, and none of them derives from the others.
#:
#: **This was a suffix rule until `#854` and it was right by coincidence.** With ``due_at``
#: and ``start_at``, stripping ``_at`` gave both the written name and the stem of the flag —
#: so a refusal built its field name by string surgery. ``snoozed_until`` has no ``_at`` to
#: strip and its flag is ``snoozed_is_all_day`` rather than ``snoozed_until_is_all_day``, so
#: the rule started naming a field that does not exist, in a message whose whole job is
#: telling a caller which field to send. A table cannot drift that way: a name that is not in
#: it raises here rather than reaching somebody as advice.
DATE_FIELDS: dict[str, tuple[str, str]] = {
	"due_at": ("due", "due_is_all_day"),
	"starts_at": ("starts", "starts_is_all_day"),
	"snoozed_until": ("snooze", "snoozed_is_all_day"),
}


class Boundary(enum.StrEnum):
	"""Which end of the day an all-day value means.

	The whole of §6.5's all-day rule, in one parameter. A deadline that is "Friday" runs
	out at the end of Friday; a defer that is "Friday" lifts at the start of it.
	"""

	END = "end"
	START = "start"


@dataclasses.dataclass(frozen=True)
class Moment:
	"""An instant, and whether the user meant a whole day rather than a time.

	The pair travels together because either alone is a half-truth: the instant says
	23:59:59.999999 and the flag is what stops a client rendering that at the user.
	"""

	instant: datetime.datetime | None
	is_all_day: bool


def zone_for (
	*,
	user: subroutine.db.models.identity.User | None = None,
	workspace: subroutine.db.models.identity.Workspace | None = None,
	instance: subroutine.db.models.system.Instance | None = None,
	explicit: str | None = None,
) -> str:
	"""Return the timezone dates should be read in: explicit → user → workspace → instance.

	docs/design.md §6.5's chain, in the one place that owns it. Before this existed every caller
	picked a timezone by hand, which is the sort of thing that agrees everywhere until it
	does not.

	**Null means "not stated" at every level**, which is why the workspace column is
	nullable rather than defaulting to UTC: a default would have shadowed the instance for
	every workspace created without an explicit zone, and the chain would have had a step
	nothing could ever reach.

	The instance is the last word because a server has a locality of its own. UTC below it
	is defensive — ``subroutine init`` always sets an instance timezone, so the only way to
	reach it is to call this without one.
	"""

	candidates = (
		explicit,
		None if user is None else user.timezone,
		None if workspace is None else workspace.timezone,
		None if instance is None else instance.timezone,
	)

	for candidate in candidates:
		if candidate:
			return candidate

	return DEFAULT_TIMEZONE


def interpret (
	value: datetime.datetime | datetime.date | str | None,
	*,
	boundary: Boundary,
	timezone: str,
	now: datetime.datetime,
	all_day: bool | None = None,
	field: str,
) -> Moment:
	"""Turn whatever the caller supplied into a stored instant and an all-day flag.

	Accepts a ``date`` (a whole day), a ``datetime`` (an instant, naive ones read in
	``timezone``), a relative expression from §9.3, or an ISO 8601 string. ``all_day``
	overrides the inference — pass it when the caller knows something the value does not
	say, such as quick capture deciding that "before Sunday" means the whole of Sunday.

	Returns ``Moment(None, False)`` for ``None``, which is how a field is cleared.
	"""

	if value is None:
		return Moment(instant=None, is_all_day=False)

	resolved, inferred = _to_instant(value, timezone=timezone, now=now, field=field)
	whole_day = inferred if all_day is None else all_day

	if not whole_day:
		return Moment(instant=resolved.astimezone(datetime.UTC), is_all_day=False)

	local = resolved.astimezone(subroutine.domain.dates.zone(timezone, field))
	moment = (
		subroutine.domain.dates.LAST_MICROSECOND
		if boundary is Boundary.END
		else datetime.time.min
	)
	snapped = datetime.datetime.combine(local.date(), moment, tzinfo=local.tzinfo)

	return Moment(instant=snapped.astimezone(datetime.UTC), is_all_day=True)


def interpret_day (
	value: datetime.date | str | None,
	*,
	timezone: str,
	now: datetime.datetime,
	field: str = "starts_at",
) -> datetime.date | None:
	"""Turn whatever the caller supplied into a calendar date.

	A relative expression is resolved and then read as a date *in the caller's timezone*,
	which is the step that makes "plan it for tomorrow" mean tomorrow where they are.

	**It returns a day rather than a Moment on purpose.** Its callers want the date itself —
	quick capture, which is deciding what a phrase meant, and whoever is about to hand it to
	:func:`interpret` as a whole day. Since `#854` no column stores a bare date, so nothing
	writes this to the database without going through that step.
	"""

	if value is None:
		return None

	if isinstance(value, datetime.datetime):
		return value.astimezone(subroutine.domain.dates.zone(timezone, field)).date()

	if isinstance(value, datetime.date):
		return value

	resolved, _inferred = _to_instant(value, timezone=timezone, now=now, field=field)

	return resolved.astimezone(subroutine.domain.dates.zone(timezone, field)).date()


#: What a *typed* day may look like, in the order somebody would reach for them. Published
#: through every human surface's refusal, so the weekday that `#167` was about is the first
#: thing named rather than absent.
WRITTEN_DAY_HINT = (
	"Try a weekday like 'friday' or 'next friday', 'today', 'tomorrow', a date like "
	"2026-08-01, or an expression like 'today+2w'."
)


def interpret_written_day (
	value: str,
	*,
	timezone: str,
	now: datetime.datetime,
	field: str = "starts_at",
) -> datetime.date | None:
	"""Read a day somebody *typed*, which includes a weekday name (`#167`).

	**The vocabulary a human surface takes, in one place.** ``interpret_day`` is §9.3's
	expression grammar and serves programs, which have a calendar and should send a date;
	this is what a person writes into ``subroutine plan``, and what an agent reading a
	conversation has in front of it when somebody says "next tuesday".

	The two were the same function until `#167`, which is how ``plan 1 friday`` came to be
	promised by five surfaces and refused by the parser while ``add "Something by friday"``
	worked. Having them as two named functions is what stops that recurring: a caller now
	says which grammar it means.

	**The refusal is here too, and that is the point of the function.** ``interpret_day``
	raises §9.3's keyword inventory — ``start_of_month``, ``end_of_week`` — which is what a
	program may send and reads like the HTTP grammar leaking into a surface that takes more
	than it, with no mention of the weekday that would have worked. Saying it once here is
	what keeps a person and an agent from being given two explanations of one refusal.
	"""

	moment = interpret_written_moment(value, timezone=timezone, now=now, field=field)

	if isinstance(moment, datetime.datetime):
		return moment.astimezone(subroutine.domain.dates.zone(timezone, field)).date()

	return moment


def interpret_written_moment (
	value: str,
	*,
	timezone: str,
	now: datetime.datetime,
	field: str = "starts_at",
) -> datetime.datetime | datetime.date | None:
	"""Read a day somebody typed, **keeping the time of day when they wrote one** (`#858`).

	Same vocabulary as :func:`interpret_written_day`, which is now this function with the
	clock thrown away — one grammar rather than two that agree until somebody edits one.

	**A time has to be written to be honoured**, which is `#797`'s rule about clocks arriving
	at the same answer from the other direction. A weekday, a bare date and a §9.3 expression
	all name a *day*, so they come back as one:

	- ``friday``, ``2026-08-18`` — a day. Whoever stores it decides what midnight means.
	- ``2026-08-18T06:00:00+01:00``, ``2026-08-18 06:00`` — an instant, kept to the minute.
	- ``tomorrow``, ``today+2w`` — a day. **Deliberately, and this is the trap**: an
	  expression resolves against ``now``, so returning its instant would silently store
	  *whatever o'clock it happens to be* — a defer written in days that lands at 14:37
	  because that is when it was typed.

	``subroutine defer`` is why this exists. It read every value through the day grammar, so
	``defer 42 2026-08-14T06:00:00+01:00`` was accepted, echoed as *"Hidden until Fri 14
	Aug"*, and stored as midnight: the six hours parsed, discarded and not mentioned. The
	field carries a clock everywhere else — quick capture writes one, ``PATCH /v1/tasks``
	accepts one, and ``readiness.undeferred`` reads it to the minute — so the command named
	after the field was the one surface that could not.
	"""

	named = subroutine.domain.dates.day_named(
		value, today=local_date(now, timezone)
	)

	if named is not None:
		return named

	try:
		if _is_expression(value.strip()):
			return interpret_day(value, timezone=timezone, now=now, field=field)

		resolved, whole_day = _to_instant(value, timezone=timezone, now=now, field=field)

	except subroutine.errors.SubroutineError:
		raise subroutine.errors.ValidationError(
			f"{value!r} is not a day this understands.",
			errors=[
				subroutine.errors.FieldError(
					field=field,
					code="invalid_field_value",
					message=f"{value!r} is not a day this understands.",
					hint=WRITTEN_DAY_HINT,
				)
			],
			hint=WRITTEN_DAY_HINT,
		) from None

	if whole_day:
		return resolved.astimezone(subroutine.domain.dates.zone(timezone, field)).date()

	return resolved


#: What each field that must not outrun a deadline is called when it does — the sentence a
#: person reads, and the one telling them which way to move things.
#:
#: **``starts_at`` is deliberately absent, and that was measured rather than assumed** (`#854`).
#: When ``start_at`` meant both things, invariant 8 read as one rule; splitting it made the
#: obvious move *checking both*, and a test refused a task that was **overdue and planned for
#: today** — which is not an error, it is what being late looks like, and it is among the
#: commonest states on any real backlog. Hiding work past its deadline guarantees it is never
#: seen in time; starting work after its deadline is just work being late.
_ORDERED_BEFORE_DUE: dict[str, tuple[str, str]] = {
	"snoozed_until": (
		"A task cannot be hidden until after it is due.",
		"Move the hidden-until date earlier, or the deadline later.",
	),
}


def check_order (
	*,
	instant: datetime.datetime | None,
	is_all_day: bool,
	due_at: datetime.datetime | None,
	due_is_all_day: bool,
	timezone: str,
	field: str,
) -> None:
	"""Enforce invariant 8 — this field must not be later than ``due_at`` — or refuse.

	**Evaluated on the rendered dates when both are all-day** (docs/design.md §6.5). Comparing the
	stored instants would be comparing midnight against the last microsecond of the day,
	which is right by accident here and would stop being right the moment either boundary
	moved. Comparing what the user sees is right on purpose.

	``field`` is one of :data:`_ORDERED_BEFORE_DUE`, and is looked up rather than formatted
	into a message: a caller asking this of a field the rule was never written for gets a
	``KeyError`` here instead of a refusal about nothing.
	"""

	summary, hint = _ORDERED_BEFORE_DUE[field]

	if instant is None or due_at is None:
		return

	if is_all_day and due_is_all_day:
		zone = subroutine.domain.dates.zone(timezone, field)

		if instant.astimezone(zone).date() <= due_at.astimezone(zone).date():
			return

	elif instant <= due_at:
		return

	raise subroutine.errors.ValidationError(
		summary,
		code="invalid_field_value",
		hint=hint,
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="invalid_field_value",
				message=f"`{field}` must not be later than `due_at`.",
			)
		],
	)


def is_overdue (task: subroutine.db.models.work.Task, *, now: datetime.datetime) -> bool:
	"""Report whether a task's deadline has passed.

	The test docs/design.md §6.5 exists for: a task due all-day Friday is **not** overdue at nine
	in the morning on Friday. It falls out of storing the deadline at the end of the day
	rather than the start, and it is asserted directly because the implementation that gets
	it wrong looks identical from the outside until somebody complains.
	"""

	if task.due_at is None or task.completed_at is not None:
		return False

	return task.due_at < now


def local_date (instant: datetime.datetime, timezone: str, *, field: str = "date") -> datetime.date:
	"""Return the calendar date an instant falls on, where the caller is."""

	return instant.astimezone(subroutine.domain.dates.zone(timezone, field)).date()


def _to_instant (
	value: datetime.datetime | datetime.date | str,
	*,
	timezone: str,
	now: datetime.datetime,
	field: str,
) -> tuple[datetime.datetime, bool]:
	"""Return the instant a value names, and whether the value itself implied a whole day."""

	zone = subroutine.domain.dates.zone(timezone, field)

	# Checked before `date`, which it subclasses. The other order silently reads every
	# datetime as a whole day and throws away the time.
	if isinstance(value, datetime.datetime):
		aware = value if value.tzinfo is not None else value.replace(tzinfo=zone)

		return aware, False

	if isinstance(value, datetime.date):
		return datetime.datetime.combine(value, datetime.time.min, tzinfo=zone), True

	return _parse(value, zone=zone, timezone=timezone, now=now, field=field)


def _parse (
	text: str,
	*,
	zone: zoneinfo.ZoneInfo,
	timezone: str,
	now: datetime.datetime,
	field: str,
) -> tuple[datetime.datetime, bool]:
	"""Read a date written as a string, in either of the two forms we accept."""

	written = text.strip()

	if not written:
		raise _invalid(text, field, "A date cannot be empty. Clear it with null instead.")

	# A bare date means the whole of that day — the case the all-day rule exists for.
	if _DATE_ONLY.match(written):
		try:
			day = datetime.date.fromisoformat(written)

		except ValueError as error:
			# The shape is right and the value is not: `2026-13-01`. Without this the raw
			# ValueError escapes the service layer and becomes a 500, which tells the
			# caller nothing and puts a user's typo in the error log as a server fault.
			raise _invalid(text, field, f"{error}. Write a date as 2026-08-01.") from None

		return datetime.datetime.combine(day, datetime.time.min, tzinfo=zone), True

	if _is_expression(written):
		resolved = subroutine.domain.dates.resolve(
			written, now=now, timezone=timezone, field=field
		)

		# **A word naming a day is day-scale; a word naming a moment is an instant**
		# (`#988`). ``today`` is ``start_of_day`` within §9.3's grammar and that stays
		# right, but a *deadline* of ``today`` meant the first microsecond of it — so it
		# read as overdue the moment it was set. Quick capture had the rule and nothing
		# else did. The boundary is applied by the caller, so a ``due`` of ``today``
		# becomes the end of it and a ``snoozed_until`` the start.
		return resolved, written in subroutine.domain.dates.WHOLE_DAY_KEYWORDS

	try:
		parsed = datetime.datetime.fromisoformat(written)

	except ValueError:
		raise _invalid(
			text,
			field,
			"Write a date as 2026-08-01, a time as 2026-08-01T17:00:00Z, or use an "
			f"expression like 'tomorrow' or 'now+7d'. Expressions start with one of: "
			f"{', '.join(subroutine.domain.dates.KEYWORDS)}.",
		) from None

	return (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=zone)), False


def _is_expression (written: str) -> bool:
	"""Report whether a string is a §9.3 relative expression rather than an ISO date.

	Decided by the keyword it starts with, because the two forms are otherwise easy to
	confuse: ``2026-08-01T17:00:00-05:00`` and ``end_of_week-1d`` both contain a hyphen
	followed by digits, and only one of them is arithmetic.
	"""

	for keyword in subroutine.domain.dates.KEYWORDS:
		if written == keyword or written.startswith((f"{keyword}+", f"{keyword}-")):
			return True

	return False


def _invalid (value: object, field: str, message: str) -> subroutine.errors.ValidationError:
	"""Build the refusal, naming the field and the forms that would have worked."""

	return subroutine.errors.ValidationError(
		f"{value!r} is not a date this understands.",
		code="invalid_field_value",
		hint=message,
		errors=[
			subroutine.errors.FieldError(
				field=field, code="invalid_field_value", message=message
			)
		],
	)
