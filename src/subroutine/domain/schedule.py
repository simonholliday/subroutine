"""The four date fields, and the all-day rule that makes them behave.

docs/design.md §6.5 keeps a **deadline** (``due_at``), a **start** (``starts_at``) and a **defer
instant** (``snoozed_until``) apart, because conflating them is what makes an overdue list
meaningless within a month. Decision `#1235` added an **end** (``ends_at``) beside the start,
so a fortnight off and a code freeze are one row rather than two. This module is where user
input becomes those columns and where the rules between them are enforced.

**The end is the only one of the four that is meaningless alone**, and that asymmetry is worth
knowing before reading :func:`check_span`: a deadline, a start and a defer each say something
on their own, where an end with no start names no period at all.

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
import typing
import zoneinfo

import subroutine.db.models.identity
import subroutine.db.models.system
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
	# **Names the *start's* flag, deliberately.** An end has none of its own — it is the far
	# side of one span — so a refusal about the shape of an end has to point at the field a
	# caller can actually send.
	"ends_at": ("ends", "starts_is_all_day"),
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


def day_in (instant: datetime.datetime, timezone: str | None) -> datetime.date:
	"""Return the calendar day one instant fell on, where it was stored (`#773`).

	**A day-scale date is a fact about a place**, so reading it anywhere else makes it a
	different day. A deadline is stored as the last microsecond of its day and a plan as the
	first, both in the writer's zone — so taking ``.date()`` off the UTC instant reports a
	deadline a day late for everybody west of Greenwich and a plan a day early for everybody
	east of it, London in summer included. Neither is a rounding error: it is the wrong day,
	on the field whose whole content is which day.

	**One function because the rule had four readers and two of them had it wrong.** The
	terminal and the browser converted; the calendar feed (`#1063`) and the agent surface
	(`#1064`) took ``.date()`` on the stored instant. Both were written by somebody who had
	read the rule elsewhere, which is what says the rule needed a home rather than a
	restatement.

	``None`` falls back to :data:`DEFAULT_TIMEZONE` rather than refusing, because a row that
	was never scheduled carries no zone and asking for its day is a legitimate question.
	"""

	return instant.astimezone(
		subroutine.domain.dates.zone(timezone or DEFAULT_TIMEZONE)
	).date()


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
	"1 September or 2026-08-01, or an offset like '+2w' or 'today+2w'."
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


def interpret_written_day_only (
	value: str,
	*,
	timezone: str,
	now: datetime.datetime,
	field: str = "starts_at",
) -> datetime.date | None:
	"""Read a day, and **refuse** a written time rather than discarding it (`#1299`).

	The third of these, and the axis the three differ on is what becomes of a clock:
	:func:`interpret_written_moment` keeps one, :func:`interpret_written_day` drops one, and
	this refuses one. **Which is right depends on the destination, not on the vocabulary.**

	- Dropping is correct where the question *is* a day: ``GET /v1/agenda?date=`` names the day
	  a page is about, so an instant sent there means that day and always has.
	- Refusing is correct where the value lands in a column that carries a clock. ``plan`` and
	  its ``--until`` write ``starts_at`` and ``ends_at``, and both store an instant — so a time
	  somebody wrote is something the field could have held, and dropping it is §6.13 rule 1's
	  exact forbidden outcome: a value read, discarded and not mentioned.

	**It was silent in the worst possible place.** ``plan 1 tomorrow --until
	'2026-08-27T11:30:00'`` kept the date, threw away the 11:30 and answered *"Starts Thu 27
	Aug"* — and because no terminal surface renders a time on either column (`#1298`), the
	output was identical to the one a working command would print.

	``field`` is the **column**, so the refusal names something a caller can send — which is
	`#1311`'s rule, met here at the first new refusal written after it.
	"""

	moment = interpret_written_moment(value, timezone=timezone, now=now, field=field)

	if not isinstance(moment, datetime.datetime):
		return moment

	local = moment.astimezone(subroutine.domain.dates.zone(timezone, field))
	name, _flag = DATE_FIELDS.get(field, (field, ""))

	raise subroutine.errors.ValidationError(
		f"{value!r} names a time of day, and this takes a day.",
		code="invalid_field_value",
		hint=(
			f"Write just the day — {local.date().isoformat()}. Planning names days and keeps "
			f"whatever time of day the item already carries."
		),
		errors=[
			subroutine.errors.FieldError(
				field=name,
				code="invalid_field_value",
				message=f"A time of day ({local:%H:%M}) was given where a day was expected.",
			)
		],
	)


def on_the_day (
	day: datetime.date,
	*,
	keeping: datetime.datetime | None,
	all_day: bool,
	timezone: str,
	field: str = "starts_at",
) -> datetime.date | datetime.datetime:
	"""Move a field to a named day, carrying the time of day it already held (`#1299`).

	**``plan`` names days and must not touch the clock.** It sent a bare
	:class:`datetime.date`, which means *the whole of that day* everywhere it is stored — so
	planning a doctor's appointment for tomorrow re-snapped its 14:00 start to midnight and
	flagged it all-day. The time was read by ``add``, stored correctly, and destroyed by the
	obvious next command.

	**A field that never had a clock still gets a whole day**, which is nearly everything: *plan
	it for Tuesday* means the whole of Tuesday, and a version that simply stopped snapping would
	leave every ordinary planned task sitting at midnight with its flag off.

	**Returned without a zone on purpose.** ``interpret`` reads a naive datetime in the
	account's timezone, which is what puts the same *wall clock* time on the landing day —
	across a clock change, 09:00 stays 09:00 rather than becoming 08:00.
	"""

	if keeping is None or all_day:
		return day

	local = keeping.astimezone(subroutine.domain.dates.zone(timezone, field))

	return datetime.datetime.combine(day, local.time())


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

	offset = _with_a_left_operand(value.strip())

	try:
		if _is_expression(offset):
			return interpret_day(offset, timezone=timezone, now=now, field=field)

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


def check_span (
	*,
	starts_at: datetime.datetime | None,
	starts_is_all_day: bool,
	ends_at: datetime.datetime | None,
	ends_is_all_day: bool,
	timezone: str,
) -> None:
	"""Enforce the three things a start and an end have to agree about, or refuse.

	Decision `#1235`. A span is *begins here, is over there*, and there are exactly three ways
	to write one that means nothing:

	* **an end with no start** — it names no period, only a moment already spelled ``due_at``;
	* **an end before its start** — a fortnight off that finishes before it begins;
	* **one end all-day and the other timed** — *starts all-day, ends at three* is not
	  something anybody means, and rendering it would have to pick one and discard the other.

	**In the service rather than in a CHECK constraint**, per the house rule: the database
	cannot name the field or say which of the two to move, and on SQLite the third of these
	would not fire at all.

	**All-day pairs are compared as dates**, which is :func:`check_order`'s reasoning and the
	same trap: an all-day start is stored as the first microsecond of its day and an all-day
	end as the last, so comparing instants is right by accident and stops being right the
	moment either boundary moves. A holiday that begins and ends on one day is legitimate —
	a public holiday is exactly that — so the comparison has to allow equality on the *day*,
	which the instants do not express.

	**Every field named below is the one a caller can send, read off :data:`DATE_FIELDS`
	rather than written out** (`#1311`, beside `#1310` and `#1312`). All three of
	these refusals named columns: ``ends_at`` where the request field is ``ends``, and
	``ends_is_all_day``, which no surface accepts at all because an end has no flag of its own.
	That is `#1259`'s defect in the one message whose whole job is saying which field to move,
	and it is what :data:`DATE_FIELDS` was built to prevent one caller along.
	"""

	written_start, _ = DATE_FIELDS["starts_at"]
	written_end, shape = DATE_FIELDS["ends_at"]

	if ends_at is None:
		return

	if starts_at is None:
		raise subroutine.errors.ValidationError(
			"An end needs a beginning.",
			code="invalid_field_value",
			hint="Give it a start as well, or use a deadline if you mean one moment.",
			errors=[
				subroutine.errors.FieldError(
					field=written_end,
					code="invalid_field_value",
					message=f"`{written_end}` cannot be set without `{written_start}`.",
				)
			],
		)

	if starts_is_all_day != ends_is_all_day:
		raise subroutine.errors.ValidationError(
			"Something is either a whole day or a time, not one at each end.",
			code="invalid_field_value",
			hint="Give both ends a time, or give both a date with no time.",
			errors=[
				subroutine.errors.FieldError(
					field=shape,
					code="invalid_field_value",
					message=(
						f"`{shape}` covers both ends of a span, so `{written_start}` and "
						f"`{written_end}` have to be the same shape."
					),
				)
			],
		)

	if starts_is_all_day and ends_is_all_day:
		zone = subroutine.domain.dates.zone(timezone, "ends_at")

		if starts_at.astimezone(zone).date() <= ends_at.astimezone(zone).date():
			return

	elif starts_at <= ends_at:
		return

	raise subroutine.errors.ValidationError(
		"It cannot finish before it starts.",
		code="invalid_field_value",
		hint="Move the end later, or the start earlier.",
		errors=[
			subroutine.errors.FieldError(
				field=written_end,
				code="invalid_field_value",
				message=f"`{written_end}` must not be earlier than `{written_start}`.",
			)
		],
	)


class Dated(typing.Protocol):
	"""Anything carrying the two columns :func:`is_overdue` reads.

	**A protocol rather than the model, so a rendered view can be asked the same question**
	(`#1243`). The terminal marks a late row from a :class:`subroutine.views.Task` and the
	domain marks one from a mapped row; naming the model here would have left the terminal to
	write ``due_at < now`` again, which is the two-copies defect in the one rule §6.5 exists to
	get right. ``views`` cannot be imported here — it imports this module.
	"""

	@property
	def due_at (self) -> datetime.datetime | None:
		"""When it has to be finished by, if anybody said."""

	@property
	def completed_at (self) -> datetime.datetime | None:
		"""When it was finished, if it has been."""


def is_overdue (task: Dated, *, now: datetime.datetime) -> bool:
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


def _with_a_left_operand (written: str) -> str:
	"""Return ``+1d`` as ``today+1d``, so an offset can be written on its own — `#1005`.

	**A default rather than a grammar.** §9.3's expressions have always taken a keyword and an
	offset; this fills in the keyword people mean when they leave it out, which is why it is
	here and not in :func:`interpret_day` — that one serves *programs*, which have a calendar
	and should say what they mean.

	**Only ``+``, deliberately.** Typer reads a leading ``-`` as an option, so ``subroutine
	agenda -1d`` is an unknown-option error before any parser sees it; a spelling that works in
	one position and not another is worse than not having it. A past day is ``yesterday`` or a
	date.

	**And only here, which is the command line and not a captured line.** ``+`` is quick
	capture's project sigil, so ``add "Something +1d"`` leaves it in the title as prose —
	measured. Teaching it as a general date form would be a promise one surface silently
	ignores, which is `#778`'s shape; ``subroutine explain dates`` marks it as the command
	line's alone.
	"""

	return f"today{written}" if written.startswith("+") else written


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
