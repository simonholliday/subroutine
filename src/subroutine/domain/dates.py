"""Relative date expressions — ``now``, ``end_of_week``, ``today+1w`` — resolved to instants.

The point of these is agent errors that never happen (docs/design.md §9.3). Without them, "what is
due this week" requires a client to know today's date, work out which day the week ends on,
format it correctly and get the timezone right; four chances to be wrong, and the failure is
silent because a well-formed wrong date returns a well-formed wrong answer.

Everything resolves **in the caller's timezone** and returns a timezone-aware UTC instant,
because that is what the columns hold (docs/design.md §6.5). "Due today" means today where the
person is, which is the whole reason the timezone travels with the task.

**The units are not the units in :mod:`subroutine.domain.durations`, and the difference
matters.** Here ``+1d`` means *the same wall-clock time tomorrow*, which across a daylight
saving boundary is twenty-three or twenty-five hours; there, ``1d`` is a flat 1440 minutes
of effort. Here ``m`` is minutes and ``M`` is months, so **case is significant** — getting
it wrong is a factor of about forty-three thousand, which is why an unrecognised unit is
refused with a message rather than guessed at.
"""

import calendar
import datetime
import re
import zoneinfo

import dateutil.relativedelta

import subroutine.errors

#: The last microsecond of a day. The same value §6.5 stores for an all-day deadline, so
#: that ``end_of_day`` and "due Friday" mean the same instant rather than nearly the same.
LAST_MICROSECOND = datetime.time(23, 59, 59, 999999)

#: Monday, per ISO 8601 and the locale this was built in. Filed in Appendix A: a workspace
#: setting is the right home for this the moment somebody wants Sunday, and moving it later
#: is a one-line change here plus a lookup at the call site.
WEEK_STARTS_ON = 0

#: Every keyword an expression may start with, published in ``/v1/meta``. ``today`` and
#: ``start_of_day`` are the same instant — both are offered because both get written, and
#: refusing one to keep the list tidy would be a puzzle rather than a simplification.
KEYWORDS: tuple[str, ...] = (
	"now",
	"today",
	"tomorrow",
	"yesterday",
	"start_of_day",
	"end_of_day",
	"start_of_week",
	"end_of_week",
	"start_of_month",
	"end_of_month",
)

#: Offset units. ``m``/``h`` are **elapsed time** and are added in UTC; ``d``/``w``/``M``/``y``
#: are **calendar** units and are added to the local wall clock, so ``now+1d`` stays at the
#: same time of day across a daylight saving change.
UNITS: tuple[str, ...] = ("m", "h", "d", "w", "M", "y")

_ELAPSED = frozenset({"m", "h"})
_CALENDAR = frozenset({"d", "w", "M", "y"})

#: Weekday names and their abbreviations, mapped to ``datetime.date.weekday()`` numbers.
#:
#: **Not part of `resolve`, and that is a decision rather than an omission** (`#167`). This is
#: the vocabulary of somebody *typing* — a capture line, or a day named on the command line —
#: where "friday" is what a person writes and the tool is expected to work out which one. The
#: expression grammar above serves programs, which have a calendar and should send a date;
#: `subroutine explain dates` states the split in those terms, so it is published rather than
#: incidental.
#:
#: It lives here, beside the grammar it is deliberately not part of, so that there is one
#: definition of what "friday" means. There were two readings of that in the product for a
#: while: capture resolved it and the CLI's own `plan` refused it.
WEEKDAYS: dict[str, int] = {
	"monday": 0, "mon": 0,
	"tuesday": 1, "tue": 1, "tues": 1,
	"wednesday": 2, "wed": 2,
	"thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
	"friday": 4, "fri": 4,
	"saturday": 5, "sat": 5,
	"sunday": 6, "sun": 6,
}

#: Month names and their abbreviations, mapped to month numbers — `#1210`.
#:
#: **Here beside :data:`WEEKDAYS` and deliberately not part of `resolve`**, for that table's
#: reason exactly: this is the vocabulary of somebody *typing*, and a program has a calendar and
#: should send an ISO date. Both are published by `subroutine explain dates`, which states the
#: split rather than leaving it to be discovered.
#:
#: **``sept`` is in here and is the one abbreviation that is not three letters.** People write
#: it, and a table that took every other month's short form and refused this one would be wrong
#: in the way nobody thinks to test.
MONTHS: dict[str, int] = {
	"january": 1, "jan": 1,
	"february": 2, "feb": 2,
	"march": 3, "mar": 3,
	"april": 4, "apr": 4,
	"may": 5,
	"june": 6, "jun": 6,
	"july": 7, "jul": 7,
	"august": 8, "aug": 8,
	"september": 9, "sep": 9, "sept": 9,
	"october": 10, "oct": 10,
	"november": 11, "nov": 11,
	"december": 12, "dec": 12,
}

#: A written calendar date, either way round: ``1 september``, ``1 Sep``, ``Sept 1``, ``14
#: March``. An optional ordinal suffix, because ``1st september`` is what a person types.
#:
#: **No year, deliberately.** A year makes it an ISO date's job — `2027-03-14` is unambiguous and
#: is what somebody writes when the year matters. What this spelling is *for* is the case a year
#: makes worse: a bill in September, a birthday in March, where the reader means the next one.
_WRITTEN_DATE = re.compile(
	r"^(?:"
	r"(?P<day_first>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month_after>[a-z]+)"
	r"|"
	r"(?P<month_first>[a-z]+)\s+(?P<day_after>\d{1,2})(?:st|nd|rd|th)?"
	r")$"
)


#: How many years ahead :func:`written_date` looks for the day somebody named — *inclusive*, so
#: the year at the far end is tried. One is enough for every date but the 29th of February;
#: eight is the widest gap between two of those, which a century that is not a leap year
#: produces. Written exclusive first, which put the search one year short of the only case it
#: was widened for — measured from 2096, where the answer is 2104.
_LEAP_SEARCH = 8


_TERM = re.compile(r"([+-])(\d+)([a-zA-Z]+)")

#: The keywords that name a **day** rather than a moment. Everything else in
#: :data:`KEYWORDS` names an instant, which is what §9.3's grammar is for — it is why
#: ``start_of_day`` and ``end_of_day`` both exist, and why ``today`` is defined as the
#: former inside it.
#:
#: But somebody writing ``--due today`` means the day, not its first microsecond, and a
#: deadline stored at midnight has already passed by the time anybody reads it. So the
#: value carries its own scale and every reader gets the same answer: quick capture knew
#: this and nothing else did, which made ``add "… by today"`` right and every other
#: surface wrong (`#988`).
#:
#: **The bare word only.** An offset is arithmetic and lands where it lands, so
#: ``today+2h`` is an instant.
WHOLE_DAY_KEYWORDS = frozenset({"today", "tomorrow", "yesterday"})

_VALID_KEYWORDS = ", ".join(f"`{keyword}`" for keyword in KEYWORDS)
_VALID_UNITS = "`m` minutes, `h` hours, `d` days, `w` weeks, `M` months, `y` years"


def day_named (written: str, *, today: datetime.date) -> datetime.date | None:
	"""Return the day a weekday name means, or ``None`` if it is not one.

	A bare ``friday`` is the soonest Friday **counting today**, because "by Friday" said on a
	Friday means today — the other reading makes a task due today impossible to say. ``next
	friday`` is the Friday of the following week, which is what the words mean to a person and
	is the one place these two differ.
	"""

	lowered = written.strip().lower()

	if lowered.startswith("next "):
		name = lowered[len("next "):].strip()

		if name not in WEEKDAYS:
			return None

		return _next_week(name, today=today)

	if lowered in WEEKDAYS:
		return _soonest(lowered, today=today)

	return written_date(lowered, today=today)


def written_date (written: str, *, today: datetime.date) -> datetime.date | None:
	"""Return the day a written calendar date means, or ``None`` if it is not one — `#1210`.

	``1 september``, ``1 Sep``, ``Sept 1``, ``14 March``, ``1st September``. Both orders,
	because both are ordinary English and a grammar that took one would be right for whichever
	half of its readers happened to match it.

	**The soonest such date counting today, exactly as a weekday is.** "By 1 September" said in
	October means next year's, and the alternative — a date in the past, silently — is the
	answer nobody wants: a deadline that has already gone renders as overdue the moment it is
	set, which reads as a defect rather than as a year having been assumed.

	**Counting today, so "by 1 September" said on 1 September is today**, for the reason
	:func:`day_named` gives about Friday: the other reading makes a task due today impossible to
	say in this grammar.

	**A day the month does not have is not a date**, and it comes back as ``None`` rather than
	being rounded to the 28th or rolled into March. The caller's refusal then names the whole
	grammar, which is more use than a confident wrong answer — and ``31 february`` is a typo
	rather than a request.
	"""

	found = _WRITTEN_DATE.match(written.strip().lower())

	if found is None:
		return None

	name = found.group("month_after") or found.group("month_first")
	number = found.group("day_first") or found.group("day_after")

	if name not in MONTHS:
		return None

	month = MONTHS[name]
	day = int(number)

	# **Eight years, and the number is the leap day rather than caution.** Every other date is
	# found in this year or the next; the 29th of February is a real date in a leap year and not
	# otherwise, so it needs the search to keep going — and *eight* rather than four because a
	# century that is not a leap year puts the gap at eight (2096 to 2104). Written the short way
	# first, with a comment claiming it meant 2028, and it returned nothing at all.
	for year in range(today.year, today.year + _LEAP_SEARCH + 1):
		try:
			candidate = datetime.date(year, month, day)

		except ValueError:
			continue

		if candidate >= today:
			return candidate

	return None


def _soonest (name: str, *, today: datetime.date) -> datetime.date:
	"""Return the soonest date with this weekday name, counting today."""

	return today + datetime.timedelta(days=(WEEKDAYS[name] - today.weekday()) % 7)


def _next_week (name: str, *, today: datetime.date) -> datetime.date:
	"""Return the day with this name in the week after the one ``today`` falls in.

	**Counted from the start of the week, not from the soonest such day**, and at the weekend
	those are a week apart. Saying "next Friday" on a Saturday used to mean thirteen days
	away: the soonest Friday is already six days off — in the week the speaker is calling
	*next* — and adding seven to it skipped that week entirely. Sunday was twelve.

	Weeks begin on a Monday, which is what decides Sunday. On a Sunday "next Friday" is five
	days away rather than twelve, because a Sunday is the end of the week a person has just
	had rather than the start of the one they are talking about.
	"""

	monday = today - datetime.timedelta(days=today.weekday())

	return monday + datetime.timedelta(days=7 + WEEKDAYS[name])


def resolve (
	expression: str,
	*,
	now: datetime.datetime,
	timezone: str,
	field: str = "date",
) -> datetime.datetime:
	"""Return the instant an expression names, as timezone-aware UTC.

	``now`` is passed in rather than read from the clock so that a whole request resolves
	against one instant — otherwise ``start_of_day`` and ``end_of_day`` in the same filter
	can land on different days, once a day, for one microsecond, which is not a bug anybody
	would ever reproduce.
	"""

	resolved = zone(timezone, field)
	text = expression.strip()

	if not text:
		raise _invalid(expression, field, f"Write a date expression starting with one of: {_VALID_KEYWORDS}.")

	terms = list(_TERM.finditer(text))
	keyword = text[: terms[0].start()] if terms else text

	# Every character must belong to the keyword or to a term, or "today tomorrow" would
	# quietly resolve as "today".
	consumed = keyword + "".join(match.group(0) for match in terms)

	if consumed != text or keyword not in KEYWORDS:
		raise _invalid(
			expression,
			field,
			f"Start with one of {_VALID_KEYWORDS}, optionally followed by offsets like "
			f"'+7d' or '-2h'. Units are {_VALID_UNITS}.",
		)

	local = _base(keyword, now.astimezone(resolved))

	for match in terms:
		local = _offset(local, match, resolved, expression, field)

	return local.astimezone(datetime.UTC)


def _base (keyword: str, local: datetime.datetime) -> datetime.datetime:
	"""Return the instant a keyword names, in the caller's timezone."""

	if keyword == "now":
		return local

	if keyword in {"today", "start_of_day"}:
		return _at(local, datetime.time.min)

	if keyword == "tomorrow":
		return _at(local + datetime.timedelta(days=1), datetime.time.min)

	if keyword == "yesterday":
		return _at(local - datetime.timedelta(days=1), datetime.time.min)

	if keyword == "end_of_day":
		return _at(local, LAST_MICROSECOND)

	if keyword == "start_of_week":
		return _at(local - datetime.timedelta(days=_days_into_week(local)), datetime.time.min)

	if keyword == "end_of_week":
		remaining = 6 - _days_into_week(local)

		return _at(local + datetime.timedelta(days=remaining), LAST_MICROSECOND)

	if keyword == "start_of_month":
		return _at(local.replace(day=1), datetime.time.min)

	_first, length = calendar.monthrange(local.year, local.month)

	return _at(local.replace(day=length), LAST_MICROSECOND)


def _offset (
	local: datetime.datetime,
	match: re.Match[str],
	zone: zoneinfo.ZoneInfo,
	expression: str,
	field: str,
) -> datetime.datetime:
	"""Apply one ``+7d``-style term, by the clock or by the calendar as the unit requires."""

	sign, count, unit = match.group(1), int(match.group(2)), match.group(3)
	amount = -count if sign == "-" else count

	if unit not in _ELAPSED and unit not in _CALENDAR:
		raise _invalid(expression, field, _unit_hint(unit))

	# **A date has a last day, and the arithmetic has to say so rather than crash** (`SR#1562`).
	# `today+99999y` left the 1..9999 years a `datetime` can hold and raised out of the domain
	# as a 500 — on the captured line as well as the structured field, which is the path an
	# agent uses most and the shape a generated value takes. The grammar around this was always
	# sound: `today+1`, `today+1x` and `now++1d` are all refused by name, and `durations`
	# bounds its own numbers. This was a bound present on one grammar and absent on its
	# neighbour.
	#
	# **Three exception types, from both halves.** `relativedelta` raises `ValueError` for a
	# year out of range, `timedelta` raises `OverflowError` for a span too large to hold, and a
	# count too large for a C int raises `OverflowError` from inside the conversion. The item
	# reported the calendar units alone; the elapsed ones do it too, so the guard goes around
	# both rather than around the site that was measured.
	try:
		if unit in _ELAPSED:
			# Elapsed time, so it is added in UTC. Adding to an aware local datetime would add
			# to the wall clock instead, and "in two hours" would skip an hour every spring.
			elapsed = (
				datetime.timedelta(minutes=amount)
				if unit == "m"
				else datetime.timedelta(hours=amount)
			)

			return (local.astimezone(datetime.UTC) + elapsed).astimezone(zone)

		# Calendar arithmetic on the wall clock, so the time of day survives a daylight saving
		# change. `relativedelta` also clamps a month or year that would overflow: 31 January
		# plus one month is 28 February, not 3 March.
		step = _step(unit, amount)

		return (local.replace(tzinfo=None) + step).replace(tzinfo=zone)

	except (ValueError, OverflowError) as why:
		raise _invalid(
			expression,
			field,
			f"{expression!r} lands outside the years a date can hold, which are 1 to 9999.",
		) from why


def _step (unit: str, amount: int) -> dateutil.relativedelta.relativedelta:
	"""Return the calendar step for one unit, spelled out rather than unpacked.

	The obvious `relativedelta(**{name: amount})` cannot be type-checked, and its first
	positional parameter is a date — so a wrong key name would be a runtime surprise in the
	one place that must not have any.
	"""

	if unit == "d":
		return dateutil.relativedelta.relativedelta(days=amount)

	if unit == "w":
		return dateutil.relativedelta.relativedelta(weeks=amount)

	if unit == "M":
		return dateutil.relativedelta.relativedelta(months=amount)

	return dateutil.relativedelta.relativedelta(years=amount)


def _at (local: datetime.datetime, moment: datetime.time) -> datetime.datetime:
	"""Return the same local date at a given time of day, keeping the timezone."""

	return datetime.datetime.combine(local.date(), moment, tzinfo=local.tzinfo)


def _days_into_week (local: datetime.datetime) -> int:
	"""Return how far into the week this date is, counting from :data:`WEEK_STARTS_ON`."""

	return (local.weekday() - WEEK_STARTS_ON) % 7


def zone (timezone: str, field: str = "timezone") -> zoneinfo.ZoneInfo:
	"""Return a timezone by IANA name, or refuse an identifier the system does not know.

	Public because three modules need exactly this and exactly this error message; a private
	copy in each is three chances for them to disagree about what an unknown zone does.
	"""

	try:
		return zoneinfo.ZoneInfo(timezone)

	except (zoneinfo.ZoneInfoNotFoundError, ValueError):
		raise subroutine.errors.ValidationError(
			f"{timezone!r} is not a timezone this system knows.",
			code="invalid_field_value",
			hint="Use an IANA identifier such as 'Europe/London' or 'UTC'.",
			errors=[
				subroutine.errors.FieldError(
					field="timezone",
					code="invalid_field_value",
					message=f"Unknown timezone {timezone!r}, resolving {field}.",
				)
			],
		) from None


def _unit_hint (unit: str) -> str:
	"""Explain an unrecognised offset unit, naming the mistake where it is a known one."""

	if unit.lower() == "m" or unit.upper() == "M":
		return (
			"Case matters here: 'm' is minutes and 'M' is months. Units are "
			f"{_VALID_UNITS}."
		)

	if unit.lower() in {"d", "w", "h", "y"}:
		return f"Units are lower case except 'M' for months: write '{unit.lower()}'."

	return f"'{unit}' is not an offset unit. Units are {_VALID_UNITS}."


def _invalid (value: str, field: str, message: str) -> subroutine.errors.ValidationError:
	"""Build the refusal, naming the field and what a valid expression looks like."""

	return subroutine.errors.ValidationError(
		f"{value!r} is not a date expression this understands.",
		code="invalid_field_value",
		hint=message,
		errors=[
			subroutine.errors.FieldError(
				field=field, code="invalid_field_value", message=message
			)
		],
	)
