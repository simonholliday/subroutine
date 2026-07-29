"""The three date fields, and the all-day rule that makes them behave.

SPEC.md §6.5 keeps a **deadline** (``due_at``), an **intended day** (``planned_for``) and a
**defer instant** (``start_at``) apart, because conflating them is what makes an overdue
list meaningless within a month. This module is where user input becomes those columns and
where the rules between them are enforced.

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
import subroutine.db.models.work
import subroutine.domain.dates
import subroutine.errors

#: The fallback when neither the person nor their workspace has said (SPEC.md §6.5).
DEFAULT_TIMEZONE = "UTC"

#: A date with no time — ``2026-08-01``. Matched before the datetime parser, which would
#: otherwise read it as midnight and quietly turn a whole day into an instant.
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
	explicit: str | None = None,
) -> str:
	"""Return the timezone dates should be read in — explicit, then user, then workspace, then UTC.

	SPEC.md §6.5's chain, in the one place that owns it. Before this existed every caller
	picked a timezone by hand, which is the sort of thing that agrees everywhere until it
	does not.
	"""

	candidates = (
		explicit,
		None if user is None else user.timezone,
		None if workspace is None else workspace.timezone,
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
	field: str = "planned_for",
) -> datetime.date | None:
	"""Turn whatever the caller supplied into a calendar date, for ``planned_for``.

	``planned_for`` is a date and nothing else — no time, no timezone (SPEC.md §6.5). A
	relative expression is resolved and then read as a date *in the caller's timezone*,
	which is the step that makes "plan it for tomorrow" mean tomorrow where they are.
	"""

	if value is None:
		return None

	if isinstance(value, datetime.datetime):
		return value.astimezone(subroutine.domain.dates.zone(timezone, field)).date()

	if isinstance(value, datetime.date):
		return value

	resolved, _inferred = _to_instant(value, timezone=timezone, now=now, field=field)

	return resolved.astimezone(subroutine.domain.dates.zone(timezone, field)).date()


def check_order (
	*,
	start_at: datetime.datetime | None,
	start_is_all_day: bool,
	due_at: datetime.datetime | None,
	due_is_all_day: bool,
	timezone: str,
) -> None:
	"""Enforce invariant 8 — ``start_at <= due_at`` — or refuse with a reason.

	**Evaluated on the rendered dates when both are all-day** (SPEC.md §6.5). Comparing the
	stored instants would be comparing midnight against the last microsecond of the day,
	which is right by accident here and would stop being right the moment either boundary
	moved. Comparing what the user sees is right on purpose.
	"""

	if start_at is None or due_at is None:
		return

	if start_is_all_day and due_is_all_day:
		zone = subroutine.domain.dates.zone(timezone, "start_at")

		if start_at.astimezone(zone).date() <= due_at.astimezone(zone).date():
			return

	elif start_at <= due_at:
		return

	raise subroutine.errors.ValidationError(
		"A task cannot be deferred until after it is due.",
		code="invalid_field_value",
		hint="Move the deferred-until date earlier, or the deadline later.",
		errors=[
			subroutine.errors.FieldError(
				field="start_at",
				code="invalid_field_value",
				message="`start_at` must not be later than `due_at`.",
			)
		],
	)


def is_overdue (task: subroutine.db.models.work.Task, *, now: datetime.datetime) -> bool:
	"""Report whether a task's deadline has passed.

	The test SPEC.md §6.5 exists for: a task due all-day Friday is **not** overdue at nine
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

		return resolved, False

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
