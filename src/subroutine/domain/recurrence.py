"""Repeating work: the phrases people write, the rule that gets stored, and the dates it means.

§6.7 — the specification is in the instance, under the SPEC project — stores recurrence
as an **RFC 5545 ``RRULE``** and treats natural language as an
input convenience that is parsed into one. That is not a preference for standards: an
``RRULE`` is what every calendar application, every feed and every language's date library
already reads, so a stored rule is portable and a hand-rolled grammar would not be.

**The phrase grammar is closed, and refuses rather than guesses** (§6.13 rule 1). This project
removed ``dateparser`` for reading ``"a"``, ``"may"`` and ``"sat"`` as dates, and the same
argument applies harder here: a misread deadline is one wrong day, where a misread recurrence
is a wrong day *for ever*, arriving silently, on a task the writer has stopped looking at.
Anything this cannot read is refused by name with the forms that would have worked.

**Occurrences are computed in the task's own timezone and then converted to UTC** (§6.7), so
"every Friday at 09:00" stays 09:00 across a daylight saving boundary rather than drifting to
08:00 for half the year. Computing in UTC and converting afterwards gets this wrong in a way
nobody notices until the clocks go back.

Nothing here touches the database. It takes text and instants in and produces rules and
instants out, so the same reading applies to the API, to quick capture and to the CLI.
"""

import dataclasses
import datetime
import re
import typing

import dateutil.rrule

import subroutine.domain.dates
import subroutine.errors

#: How often a series repeats, and the only frequencies a *task* may use.
#:
#: **Deliberately no ``HOURLY``, ``MINUTELY`` or ``SECONDLY``**, which RFC 5545 defines and
#: this refuses. A task repeating every minute is a mistake somebody is about to make at
#: scale — every occurrence materialised is a row, a ref off the workspace counter and an
#: event — and the honest place to say no is before the first one is written.
FREQUENCIES: dict[str, int] = {
	"DAILY": dateutil.rrule.DAILY,
	"WEEKLY": dateutil.rrule.WEEKLY,
	"MONTHLY": dateutil.rrule.MONTHLY,
	"YEARLY": dateutil.rrule.YEARLY,
}

#: The ``RRULE`` parts this understands. A rule carrying anything else is refused rather than
#: stored and silently half-honoured — ``BYSETPOS`` and ``BYWEEKNO`` are real and expressible
#: and would come back as occurrences nobody predicted.
PARTS: frozenset[str] = frozenset({
	"FREQ", "INTERVAL", "COUNT", "UNTIL", "WKST",
	"BYDAY", "BYMONTHDAY", "BYMONTH",
})

#: How many dates to show back. **Five, following §6.7's own wording**, and the number is a
#: judgement about confirmation rather than about pagination: enough to see a weekly rule
#: land on the right weekday and a monthly one skip February, few enough to read at a glance.
AHEAD = 5


#: What a caller may write instead of a rule, in the order somebody would reach for them.
#: Published through every refusal, so the shapes that work are named where the failure is.
PHRASE_HINT = (
	"Try 'every day', 'every 14 days', 'every other tuesday', 'every month on the 30th', "
	"'every month on the last thursday' or 'every year on 19 august'."
)

#: Where a clock belongs, said wherever a repeat is handed one. The rule says *how often*
#: and the task says *when* — an appointment at two o'clock recurring weekly is one rule
#: and one `starts_at`, and folding the time into the rule would be a second place to
#: store it (`#854`).
_A_TIME_GOES_ELSEWHERE = "A time of day goes on the item itself, not on the repeat."

#: Two-letter weekday codes, in RFC 5545's order, indexed the way ``date.weekday()`` counts.
_CODES: tuple[str, ...] = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

#: The weekday each code names, for reading a rule back as a sentence. Derived from the
#: same tuple the codes come from, so the two orders cannot come to disagree.
_NAMED: dict[str, str] = dict(zip(
	_CODES,
	("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
	strict=True,
))

#: The units a phrase may repeat by, and the frequency each names.
_UNITS: dict[str, str] = {
	"day": "DAILY",
	"days": "DAILY",
	"week": "WEEKLY",
	"weeks": "WEEKLY",
	"month": "MONTHLY",
	"months": "MONTHLY",
	"year": "YEARLY",
	"years": "YEARLY",
}

#: Which occurrence within a month an ordinal names. ``last`` is -1 rather than a count from
#: the front, which is the whole reason "the last Thursday" is worth supporting: months have
#: four or five Thursdays and a fixed number would silently mean a different week.
_ORDINALS: dict[str, int] = {
	"first": 1, "second": 2, "third": 3, "fourth": 4, "last": -1,
}

_MONTHS: dict[str, int] = {
	"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
	"april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
	"august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
	"october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_WEEKDAY_WORDS = "|".join(sorted(subroutine.domain.dates.WEEKDAYS, key=len, reverse=True))
_UNIT_WORDS = "|".join(sorted(_UNITS, key=len, reverse=True))
_ORDINAL_WORDS = "|".join(_ORDINALS)
_MONTH_WORDS = "|".join(sorted(_MONTHS, key=len, reverse=True))

#: ``every`` [``other`` | *n*] (*unit* | *weekday*), optionally followed by a qualifier.
#:
#: **``other`` and a count are the same thing said two ways** and both are here because both
#: get written — "every other Tuesday" is how people speak and "every 2 weeks" is how they
#: type. Refusing either would be a puzzle rather than a simplification.
_EVERY = re.compile(
	rf"""
	^\s*every\s+
	(?:(?P<other>other)\s+|(?P<count>\d+)\s+)?
	(?:(?P<unit>{_UNIT_WORDS})|(?P<weekday>{_WEEKDAY_WORDS}))
	(?P<qualifier>\s+.*)?
	\s*$
	""",
	re.IGNORECASE | re.VERBOSE,
)

#: ``on the 30th`` / ``on the last thursday`` — what narrows a monthly series to one day.
_MONTHLY_DAY = re.compile(
	r"^\s*on\s+the\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\s*$", re.IGNORECASE
)
_MONTHLY_WEEKDAY = re.compile(
	rf"^\s*on\s+the\s+(?P<ordinal>{_ORDINAL_WORDS})\s+(?P<weekday>{_WEEKDAY_WORDS})\s*$",
	re.IGNORECASE,
)

#: ``on the 30th of every month`` — the same rule written the other way round.
#:
#: **Added because it is the phrasing the person who asked for the feature used.** The grammar
#: was built from `every` forwards and refused *"on the 30th of every month"* by name, which is
#: an honest refusal of a sentence somebody actually wrote. It is normalised into the ordinary
#: form rather than given its own parse path, so there is one grammar with two word orders and
#: not two grammars that have to agree.
_FRONTED = re.compile(
	r"^\s*(?P<qualifier>on\s+the\s+.+?)\s+of\s+every\s+(?P<unit>month|year)\s*$",
	re.IGNORECASE,
)

#: ``on 19 august`` / ``on august 19`` — what pins a yearly series to a date.
_YEARLY_DAY = re.compile(
	rf"""^\s*on\s+(?:the\s+)?(?:
		(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_MONTH_WORDS})
		|(?P<month2>{_MONTH_WORDS})\s+(?P<day2>\d{{1,2}})(?:st|nd|rd|th)?
	)\s*$""",
	re.IGNORECASE | re.VERBOSE,
)


@dataclasses.dataclass(frozen=True)
class Recurrence:
	"""A stored rule and the words it came from.

	``text`` is kept because a person who wrote "every other tuesday" should be shown that
	back rather than ``FREQ=WEEKLY;INTERVAL=2;BYDAY=TU`` — and because a phrase this grammar
	widens later would otherwise have nothing to be re-read from. It is ``None`` when the
	caller sent a rule directly, which is the honest answer: nobody wrote a sentence.
	"""

	rule: str
	text: str | None = None


@dataclasses.dataclass(frozen=True)
class Repeat:
	"""A rule with the two things that qualify it, once a caller's defaults have been filled.

	**Separate from :class:`Recurrence` because they answer different questions.** That one is
	what reading a *phrase* produced and knows nothing about anchors; this is what a service
	settled after applying defaults and refusing the combination that means nothing. Collapsing
	them would make the parser look as though it had an opinion about how a series advances.
	"""

	rule: str
	text: str | None
	anchor: str
	trigger: str


def _refuse (value: str, *, field: str, why: str) -> subroutine.errors.ValidationError:
	"""Return the refusal for something this cannot read, naming what would have worked."""

	return subroutine.errors.ValidationError(
		f"{value!r} is not a repeat this understands.",
		errors=[
			subroutine.errors.FieldError(
				field=field, code="invalid_field_value", message=why, hint=PHRASE_HINT
			)
		],
		hint=PHRASE_HINT,
	)


def _interval (match: re.Match[str], value: str, field: str) -> int:
	"""Return how many units apart the occurrences are, refusing nought."""

	if match.group("other"):
		return 2

	if match.group("count") is None:
		return 1

	count = int(match.group("count"))

	# **Refused rather than treated as 1.** "Every 0 days" is not a slip anybody makes twice,
	# but stored as a daily rule it would look exactly like one somebody meant.
	if count < 1:
		raise _refuse(value, field=field, why="A repeat has to be at least one unit apart.")

	return count


def _monthly_qualifier (qualifier: str, value: str, field: str) -> list[str]:
	"""Return the ``BY…`` parts narrowing a monthly series, or refuse the phrase."""

	day = _MONTHLY_DAY.match(qualifier)

	if day is not None:
		number = int(day.group("day"))

		# **28 rather than 31**, because a rule saying the 30th is one a caller can mean and
		# February simply skips it — where 32 is a value no month has and would produce a
		# series that never fires, silently, for ever.
		if not 1 <= number <= 31:
			raise _refuse(value, field=field, why="A day of the month runs from 1 to 31.")

		return [f"BYMONTHDAY={number}"]

	weekday = _MONTHLY_WEEKDAY.match(qualifier)

	if weekday is not None:
		which = _ORDINALS[weekday.group("ordinal").lower()]
		code = _CODES[subroutine.domain.dates.WEEKDAYS[weekday.group("weekday").lower()]]

		return [f"BYDAY={which}{code}"]

	raise _refuse(
		value,
		field=field,
		why=f"{qualifier.strip()!r} does not say which day of the month.",
	)


def _yearly_qualifier (qualifier: str, value: str, field: str) -> list[str]:
	"""Return the ``BY…`` parts pinning a yearly series to a date, or refuse the phrase."""

	found = _YEARLY_DAY.match(qualifier)

	if found is None:
		raise _refuse(
			value, field=field, why=f"{qualifier.strip()!r} does not name a day and a month."
		)

	month = _MONTHS[(found.group("month") or found.group("month2")).lower()]
	day = int(found.group("day") or found.group("day2"))

	# Checked against the month rather than against 31, because "every year on 31 february"
	# is a rule that would be stored happily and then never fire.
	if not 1 <= day <= _DAYS_IN[month]:
		raise _refuse(
			value,
			field=field,
			why=f"There is no day {day} in that month.",
		)

	return [f"BYMONTH={month}", f"BYMONTHDAY={day}"]


#: The longest each month gets, February counted as a leap year so that "every year on 29
#: february" is accepted — it is a real birthday, and RFC 5545 skips the years without one.
_DAYS_IN: dict[int, int] = {
	1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
	7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31,
}


def phrase (value: str, *, field: str = "recurrence") -> str:
	"""Turn a written repeat into an ``RRULE``, or refuse it by name.

	The whole grammar is :data:`_EVERY` plus one optional qualifier, and everything outside it
	is refused. That is the point rather than a limitation: a phrase this reads wrongly becomes
	a rule nobody re-reads, on an item that then arrives on the wrong day indefinitely.
	"""

	fronted = _FRONTED.match(value)
	written = (
		value
		if fronted is None
		else f"every {fronted.group('unit')} {fronted.group('qualifier')}"
	)

	match = _EVERY.match(written)

	if match is None:
		# **Which half failed, rather than one sentence for both.** "every fortnight" *does*
		# start with `every`, so answering it with "a repeat starts with 'every'" is a refusal
		# asserting a cause it has not established — the reader checks the word they already
		# wrote and learns nothing about the one that was actually unreadable.
		leading = re.match(r"^\s*every\b\s*(?P<rest>.*)$", written, re.IGNORECASE)

		if leading is None:
			raise _refuse(value, field=field, why="A repeat starts with 'every'.")

		rest = leading.group("rest").strip()

		raise _refuse(
			value,
			field=field,
			why=f"{rest!r} is not a length of time this repeats by."
			if rest
			else "'every' has to say every what.",
		)

	interval = _interval(match, value, field)
	qualifier = (match.group("qualifier") or "").strip()

	if match.group("weekday") is not None:
		if qualifier:
			raise _refuse(
				value,
				field=field,
				why=f"{qualifier!r} says nothing more about a weekly repeat. "
				f"{_A_TIME_GOES_ELSEWHERE}",
			)

		code = _CODES[subroutine.domain.dates.WEEKDAYS[match.group("weekday").lower()]]
		parts = ["FREQ=WEEKLY", f"BYDAY={code}"]

	else:
		frequency = _UNITS[match.group("unit").lower()]
		parts = [f"FREQ={frequency}"]

		if qualifier and frequency == "MONTHLY":
			parts += _monthly_qualifier(qualifier, value, field)

		elif qualifier and frequency == "YEARLY":
			parts += _yearly_qualifier(qualifier, value, field)

		elif qualifier:
			raise _refuse(
				value,
				field=field,
				why=f"{qualifier!r} only means something after 'every month' or 'every year'.",
			)

	if interval != 1:
		parts.insert(1, f"INTERVAL={interval}")

	return ";".join(parts)


def rule (value: str, *, field: str = "recurrence") -> Recurrence:
	"""Read whatever the caller supplied as a stored rule, phrase or ``RRULE`` alike.

	**Told apart by ``FREQ=``**, which every ``RRULE`` has and no phrase does. One field taking
	two shapes rather than two fields, for the reason ``due`` takes a date, a datetime and an
	expression: a caller with a calendar's rule already in hand should not have to translate it
	into English so that this can translate it back.
	"""

	written = value.strip()

	if not written:
		raise _refuse(value, field=field, why="A repeat cannot be empty.")

	if "FREQ=" in written.upper():
		return Recurrence(rule=_checked(written, field=field), text=None)

	return Recurrence(rule=phrase(written, field=field), text=written)


def _checked (value: str, *, field: str) -> str:
	"""Return an ``RRULE`` this can honour, or refuse the part that stops it.

	**Every part is checked rather than the string being handed to the parser and trusted.**
	``dateutil`` reads far more of RFC 5545 than this stores, so an unchecked rule would be
	accepted, saved, and come back as occurrences on days nobody asked for — which is the
	shape of defect that survives every test written from the accepted cases.
	"""

	written = value.strip()

	if written.upper().startswith("RRULE:"):
		written = written[len("RRULE:"):]

	found: dict[str, str] = {}

	for piece in written.split(";"):
		if not piece:
			continue

		name, _, setting = piece.partition("=")
		name = name.strip().upper()

		if name not in PARTS:
			raise _refuse(
				value,
				field=field,
				why=f"{name!r} is not a rule part this stores. "
				f"It reads {', '.join(sorted(PARTS))}.",
			)

		found[name] = setting.strip()

	if found.get("FREQ", "").upper() not in FREQUENCIES:
		raise _refuse(
			value,
			field=field,
			why=f"A rule repeats {', '.join(sorted(FREQUENCIES)).lower()} — "
			f"anything finer would materialise faster than anybody works.",
		)

	# Proved by building it, because a part this accepts by name can still be unreadable —
	# `BYDAY=XX` passes the check above and means nothing.
	try:
		dateutil.rrule.rrulestr(
			f"RRULE:{written}", dtstart=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
		)

	except (ValueError, TypeError) as unreadable:
		raise _refuse(value, field=field, why=str(unreadable)) from None

	_refuse_a_day_that_never_comes(value, found, field=field)

	# **Stored as it was checked, not as it was typed** (`#929`). Every part of an ``RRULE`` is
	# case-insensitive and this function upper-cases each *name* to validate it — then returned
	# the original string, so ``freq=weekly;byday=mo`` was accepted, stored verbatim, and
	# described back as ``"every "``. The read-back is the whole point of taking a rule this
	# way, so it failing on a rule the parser accepted is the worst available outcome.
	#
	# Safe to upper-case whole: an ``RRULE``'s values are keywords, integers and a UTC
	# timestamp, none of which carries meaning in its case.
	return written.upper()


#: The most days each month can have. February's 29 is a leap year, which is rare and real —
#: "every 29 February" is a birthday somebody has.
_DAYS_IN = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}


def _refuse_a_day_that_never_comes (
	value: str, parts: dict[str, str], *, field: str
) -> None:
	"""Refuse a rule that is well-formed, legal, and names a date that does not exist.

	``FREQ=DAILY;BYMONTH=2;BYMONTHDAY=31`` asks for the 31st of February. Nothing rejected it:
	it parses, it stores, ``describe`` renders it as *"every day, on 31 February"*, and asking
	for its occurrences sends ``dateutil`` walking the calendar day by day until its own
	internal limit — **2.68 seconds of CPU, synchronously, measured**, for one request on an
	endpoint whose default rate limit is 600 a minute.

	**Refused rather than bounded**, and that is the decision. A time limit on the search would
	answer *"no occurrences"* to a question whose real answer is *"that is not a date"*, and
	would leave the rule stored — so the same three seconds would be spent again by every
	listing that expanded it. This is a validity check the rule was always missing, and the
	pathological cost goes with it.

	Only the combination that is decidable from the rule alone: every month it names against
	the longest that month can be. A rule with no ``BYMONTHDAY`` names no impossible day, and
	one whose months include a long enough one is satisfiable somewhere.
	"""

	days = [
		int(piece) for piece in parts.get("BYMONTHDAY", "").split(",")
		if piece.strip().lstrip("-").isdigit()
	]
	months = [
		int(piece) for piece in parts.get("BYMONTH", "").split(",")
		if piece.strip().isdigit()
	]

	if not days or not months:
		return

	# A negative day counts back from the end of the month, so it is possible wherever the
	# month is at least that long — the same comparison, and never impossible for 1 to 28.
	reachable = [
		(month, day) for month in months for day in days
		if abs(day) <= _DAYS_IN.get(month, 31)
	]

	if reachable:
		return

	names = {2: "February", 4: "April", 6: "June", 9: "September", 11: "November"}
	month, day = months[0], days[0]

	raise _refuse(
		value,
		field=field,
		why=f"There is no {abs(day)} {names.get(month, 'th month'.replace('th month', str(month)))} "
		f"in any year, so this would never come round.",
	)


def names_its_own_day (stored: str) -> bool:
	"""Report whether a rule says which day it falls on, without being told a start.

	**"On the 30th of every month" says when; "every 14 days" does not** — fourteen days from
	*what?* — and that is the whole distinction. A rule carrying a ``BY…`` part has named its
	days, and a daily one falls on every day including this one, so both can be anchored on the
	moment they were written without inventing anything. Anything else genuinely needs a date,
	and asking for one is better than picking whichever day somebody happened to type it.
	"""

	parts = {
		piece.split("=", 1)[0].strip().upper()
		for piece in stored.split(";")
		if "=" in piece
	}

	if parts & {"BYDAY", "BYMONTHDAY", "BYMONTH"}:
		return True

	return "FREQ=DAILY" in stored.upper() and "INTERVAL=" not in stored.upper()


def occurrences (
	stored: str,
	*,
	start: datetime.datetime,
	timezone: str,
	after: datetime.datetime | None = None,
	limit: int | None = None,
	until: datetime.datetime | None = None,
) -> list[datetime.datetime]:
	"""Return the occurrences a rule names, in UTC, computed where the task lives.

	``start`` anchors the series and ``after`` is a cursor into it, and **they are two
	arguments because they are two facts** — which this learned by having one. ``COUNT`` and
	``UNTIL`` are measured from the anchor, so asking "what comes after the second occurrence"
	with the cursor as the anchor spends the count on occurrences nobody asked about:
	``FREQ=DAILY;COUNT=3`` answered with two dates, which is the sort of wrong that looks like
	an off-by-one and is really a conflation.

	The pair is also exactly §6.7's two anchors. A ``schedule`` series passes its original
	first occurrence as ``start`` and the one just completed as ``after``, so the grid holds
	however late anybody was. A ``completion`` series passes the completion instant as both,
	so the next one is an interval after the work actually happened.

	**The rule is evaluated on local wall-clock times and converted afterwards** (§6.7). A
	series computed in UTC keeps the UTC hour and moves the local one, so "every Friday at
	09:00" becomes 08:00 for half the year — correct by every test that does not cross a
	daylight saving boundary, and wrong twice a year for everybody.

	An exhausted series — ``COUNT`` spent or ``UNTIL`` passed — returns an empty list rather
	than raising. Nothing left to do is an answer, not a fault.
	"""

	zone = subroutine.domain.dates.zone(timezone)
	anchor = start.astimezone(zone).replace(tzinfo=None)
	cursor = None if after is None else after.astimezone(zone).replace(tzinfo=None)

	series = dateutil.rrule.rrulestr(f"RRULE:{stored}", dtstart=anchor)

	found: list[datetime.datetime] = []
	ceiling = None if until is None else until.astimezone(zone).replace(tzinfo=None)

	for moment in series:
		if cursor is not None and moment <= cursor:
			continue

		if ceiling is not None and moment > ceiling:
			break

		# **Localised one at a time rather than by shifting the whole series**, because the
		# offset is not constant across it: an hour that does not exist on the day the clocks
		# go forward is what this per-occurrence conversion is for.
		found.append(moment.replace(tzinfo=zone).astimezone(datetime.UTC))

		if limit is not None and len(found) >= limit:
			break

	return found


def following (
	stored: str,
	*,
	start: datetime.datetime,
	after: datetime.datetime,
	timezone: str,
) -> datetime.datetime | None:
	"""Return the next occurrence after a cursor, or ``None`` when the series is spent."""

	found = occurrences(stored, start=start, timezone=timezone, after=after, limit=1)

	return found[0] if found else None


def _described_weekdays (setting: str) -> str:
	"""Return ``BYDAY`` as words — ``-1TH`` becomes "the last Thursday"."""

	ordinals = {number: word for word, number in _ORDINALS.items()}

	said = []

	for piece in setting.split(","):
		match = re.fullmatch(r"(?P<which>-?\d+)?(?P<code>[A-Z]{2})", piece.strip().upper())

		if match is None:
			return setting

		day = _NAMED.get(match.group("code"), match.group("code"))
		which = match.group("which")

		said.append(day if which is None else f"the {ordinals.get(int(which), which)} {day}")

	return " and ".join(said)


#: How a completion anchor reads, said once so that no two surfaces word it differently
#: (`#674`). **Only the non-default is ever said**, on `views.status_is_news`'s rule: a
#: schedule anchor is what "every month on the 30th" already sounds like, so naming it would
#: put a clause on every repeating row to tell the reader nothing.
_MEASURED_FROM_COMPLETION = "from when it is done"


def describe (stored: str, *, anchor: str | None = None) -> str:
	"""Return a rule as a sentence somebody can check against what they meant.

	**This exists so an agent can confirm before committing** (§6.7's ``/v1/recurrence/parse``):
	an ambiguous natural-language feature becomes a checkable one the moment the thing it
	understood is read back in different words from the ones that were typed. Echoing the input
	would confirm nothing.

	``anchor`` is what makes that confirmation complete rather than half of one. *Every three
	days* is two different schedules depending on where it is measured from, so a reader
	checking a repeat against what they meant cannot do it from the rule alone — and a caller
	who has just set ``recurrence_anchor`` has nowhere to see that it landed.
	"""

	# **Does not assume its argument was canonicalised** (`#929`). `_checked` upper-cases what
	# it stores now, so everything written since reads back correctly — but this is also handed
	# rules by callers and by rows written before that, and answering `"every "` about a rule
	# the parser accepts is worse than answering slowly.
	stored = stored.upper()

	parts = dict(
		[*piece.split("=", 1), ""][:2] for piece in stored.split(";") if "=" in piece
	)

	frequency = parts.get("FREQ", "").upper()
	interval = int(parts.get("INTERVAL", "1") or 1)
	unit = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}.get(
		frequency, frequency.lower()
	)

	if interval == 1:
		said = f"every {unit}"

	elif interval == 2:
		said = f"every other {unit}"

	else:
		said = f"every {interval} {unit}s"

	if "BYDAY" in parts and frequency == "WEEKLY":
		days = _described_weekdays(parts["BYDAY"])
		said = f"every {days}" if interval == 1 else f"{said}, on {days}"

	elif "BYDAY" in parts:
		said = f"{said}, on {_described_weekdays(parts['BYDAY'])}"

	if "BYMONTH" in parts and "BYMONTHDAY" in parts:
		names = {number: name for name, number in _MONTHS.items() if len(name) > 3}
		month = names.get(int(parts["BYMONTH"]), parts["BYMONTH"]).title()
		said = f"{said}, on {int(parts['BYMONTHDAY'])} {month}"

	elif "BYMONTHDAY" in parts:
		said = f"{said}, on the {_ordinal(int(parts['BYMONTHDAY']))}"

	if "COUNT" in parts:
		said = f"{said}, {int(parts['COUNT'])} times"

	if "UNTIL" in parts:
		said = f"{said}, until {parts['UNTIL']}"

	if anchor == "completion":
		said = f"{said}, {_MEASURED_FROM_COMPLETION}"

	return said


def _ordinal (number: int) -> str:
	"""Return 1 as ``1st``, 22 as ``22nd`` — for reading a day of the month back."""

	if 11 <= number % 100 <= 13:
		return f"{number}th"

	return f"{number}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th') }"


def published () -> dict[str, typing.Any]:
	"""Return what ``/v1/meta`` says about this grammar.

	Published for `#821`'s reason: a vocabulary a client cannot see is one it never sends and
	is never corrected about, so it never learns the word at all.
	"""

	return {
		"frequencies": sorted(FREQUENCIES),
		"parts": sorted(PARTS),
		"examples": [
			"every day",
			"every 14 days",
			"every other tuesday",
			"every month on the 30th",
			"every month on the last thursday",
			"every year on 19 august",
		],
	}
