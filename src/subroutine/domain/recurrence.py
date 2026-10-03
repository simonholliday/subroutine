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

import dateutil.parser
import dateutil.rrule

import subroutine.domain.dates
import subroutine.domain.schedule
import subroutine.domain.text
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

#: **How far apart a rule's occurrences may be: about a hundred years** (`#3997`). An interval of
#: ten thousand years, or of a hundred billion days, stored and came round once, and then its next
#: date lay beyond any calendar this can read. Written per frequency so a phrase and a rule
#: sent directly are held to one number.
_AT_MOST_APART: dict[str, int] = {
	"DAILY": 36_500,
	"WEEKLY": 5_200,
	"MONTHLY": 1_200,
	"YEARLY": 100,
}

#: One ``BYDAY`` entry: an optional count from the start or the end, then a weekday.
_A_WEEKDAY = re.compile(r"(?P<count>[+-]?\d+)?(?P<day>MO|TU|WE|TH|FR|SA|SU)")


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


#: One weekday as a repeat names it, longest first so ``thurs`` is not read as ``thu``.
_A_WEEKDAY_WRITTEN = "|".join(sorted(subroutine.domain.dates.WEEKDAYS, key=len, reverse=True))

#: One day of a month as a repeat names it, with the ``the`` somebody may write before it.
_A_DAY_OF_THE_MONTH = r"(?:the\s+)?\d{1,2}(?:st|nd|rd|th)"

#: **Which of a weekday in its month each ordinal in a list names** (`#4318`): *the 1st and 3rd
#: monday*, *the first and last friday*. To the fifth, where the one-day grammar stops at the
#: fourth, because ``BYDAY=5MO`` is a rule this stores and naming one is all this is for.
_ORDINALS_WRITTEN: dict[str, int] = {
	**_ORDINALS, "fifth": 5, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5,
}

#: One of them as a list writes it, with the ``the`` somebody may write before it.
_AN_ORDINAL_WRITTEN = rf"(?:the\s+)?(?:{'|'.join(_ORDINALS_WRITTEN)})"


def _listed (item: str) -> str:
	"""Return a pattern for two or more of ``item``, each joined by a comma, ``and`` or both.

	**Each, not only the last** (`#4318`): *every monday and thursday and friday* was refused as a
	time handed to a repeat, and in a line said nothing at all.
	"""

	return rf"(?:{item})(?:(?:\s*,\s*|\s*,?\s+and\s+)(?:{item}))+"


#: **A repeat on several days, recognised in order to name it and never read as a rule**
#: (decision `#4148`). Weekdays in a list - *every monday and thursday*, *every monday, wednesday
#: and friday* - and days of the month in either order - *every month on the 1st and 15th*, *on
#: the 1st and 15th of every month*. Capture leaves such a phrase in the title and says why, and
#: :func:`phrase` refuses it naming the rule that does repeat so. Reading lists would have widened
#: the end-of-line rule's risk of reading the object of a sentence to every list of days.
#:
#: **And however often it comes round, and by which of a weekday in its month** (`#4318`, of the
#: cold review of 2026-10-03): *every other monday and thursday*, *every 2 weeks on monday and
#: thursday*, *every 2 months on the 1st and 15th*, *on the 1st and 3rd monday of every month* and
#: *every first and third monday*. Each was refused for a reason that was not the one, and in a
#: line some were read in part: *on the 1st and 3rd monday of every month* repeated monthly with
#: *of* ending the title, and *every 2 weeks on monday and thursday* started on the Monday. **The
#: ordinal forms come before the days of the month**, which would otherwise take *the 1st and 3rd*
#: and leave the weekday behind.
SEVERAL_DAYS = re.compile(
	rf"""
	(?<![^\s])
	(?:
		every\s+(?:other\s+)?{_listed(_A_WEEKDAY_WRITTEN)}
		|every\s+(?:other\s+|\d+\s+)?weeks?\s+on\s+{_listed(_A_WEEKDAY_WRITTEN)}
		|every\s+{_listed(_AN_ORDINAL_WRITTEN)}\s+(?:{_A_WEEKDAY_WRITTEN})
			(?:\s+of\s+(?:the|every)\s+month)?
		|every\s+(?:other\s+|\d+\s+)?months?\s+on\s+{_listed(_AN_ORDINAL_WRITTEN)}
			\s+(?:{_A_WEEKDAY_WRITTEN})
		|every\s+(?:other\s+|\d+\s+)?months?\s+on\s+{_listed(_A_DAY_OF_THE_MONTH)}
		|on\s+{_listed(_AN_ORDINAL_WRITTEN)}\s+(?:{_A_WEEKDAY_WRITTEN})\s+of\s+every\s+month
		|on\s+{_listed(_A_DAY_OF_THE_MONTH)}\s+of\s+every\s+month
	)
	(?!\w)
	""",
	re.IGNORECASE | re.VERBOSE,
)


def on_several_days (written: str) -> str | None:
	"""Return the rule a repeat on several days would be, or ``None`` when it is not one.

	For naming, never for setting: decision `#4148` keeps a written repeat on one day, and this is
	what the refusal and the note can point at instead, as ``FREQ=WEEKLY;BYDAY=MO,TH``.
	"""

	if SEVERAL_DAYS.fullmatch(written.strip()) is None:
		return None

	lowered = written.lower()
	# **How often, where it says** (`#4318`): *every other* is two, as it is in a one-day repeat.
	often = re.search(r"\bevery\s+(other|\d+)\s", lowered)
	interval = 1 if often is None else 2 if often.group(1) == "other" else int(often.group(1))
	named = [
		subroutine.domain.dates.WEEKDAYS[one]
		for one in re.findall(rf"(?<![\w])(?:{_A_WEEKDAY_WRITTEN})(?![\w])", lowered)
	]
	ordinals = re.findall(rf"(?<![\w])(?:{'|'.join(_ORDINALS_WRITTEN)})(?![\w])", lowered)

	# **A weekday with ordinals is which of it in its month**, *the 1st and 3rd monday*, counted from
	# the front and then from the end; a weekday alone is every week; and no weekday is days of the
	# month, whose numbers are ordinals too.
	if named and ordinals:
		counts = sorted({_ORDINALS_WRITTEN[one] for one in ordinals}, key=lambda count: (count < 0, count))
		parts = ["FREQ=MONTHLY", "BYDAY=" + ",".join(f"{count}{_CODES[named[0]]}" for count in counts)]

	elif named:
		parts = ["FREQ=WEEKLY", "BYDAY=" + ",".join(_CODES[index] for index in sorted(set(named)))]

	else:
		days = sorted({int(day) for day in re.findall(r"(\d{1,2})(?:st|nd|rd|th)", lowered)})
		parts = ["FREQ=MONTHLY", "BYMONTHDAY=" + ",".join(str(day) for day in days)]

	if interval != 1:
		parts.insert(1, f"INTERVAL={interval}")

	return ";".join(parts)


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


def _refuse_an_interval (value: str, frequency: str, interval: int, *, field: str) -> None:
	"""Refuse occurrences nought, a negative number or more than about a century apart - `#3997`.

	**Every 0 days never moves on**: dateutil hands back the same moment for ever, so completing
	its occurrence never returned, holding a worker and its database connection. A negative
	interval walks backwards into a day that does not exist, and a vast one past every calendar.
	"""

	if interval < 1:
		raise _refuse(value, field=field, why="A repeat has to be at least one unit apart.")

	most = _AT_MOST_APART[frequency]

	if interval > most:
		unit = {"DAILY": "days", "WEEKLY": "weeks", "MONTHLY": "months", "YEARLY": "years"}[frequency]

		raise _refuse(
			value,
			field=field,
			why=f"A repeat can be at most {most:,} {unit} apart, about a hundred years.",
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

		# **31 rather than 28**, because a rule saying the 30th is one a caller can mean and
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
#:
#: **Declared twice until 2026-08-27** (`#1409`), 167 lines apart, with two comments saying
#: this in different words and **identical values** — so the second silently replaced the
#: first with itself and nothing ever behaved differently. Two copies that agree are
#: invisible, which is why this needed a guard rather than a reading: `tests/test_imports.py`
#: refuses a module-level name assigned twice now.
#:
#: Both readers are below it — the phrase grammar and
#: :func:`_refuse_a_day_that_never_comes` — and each was reading whichever copy Python had
#: last bound.
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

	# **A repeat on several days is refused for being one** (`#4147`, decision `#4148`), naming
	# the rule that does it. It was refused as a time of day handed to a repeat, or as not saying
	# which day of the month, neither of which was what the writer had done.
	several = on_several_days(value)

	if several is not None:
		# **Only a rule this stores is named** (`#4318`): *every 0 weeks on monday and thursday* is
		# refused as any repeat that never moves on is, rather than told to send one that does not.
		named = {name: setting for name, _, setting in (part.partition("=") for part in several.split(";"))}
		_refuse_an_interval(value, named["FREQ"], int(named.get("INTERVAL", "1")), field=field)

		hint = f"Give it as a rule instead: {several}."

		raise subroutine.errors.ValidationError(
			f"{value!r} repeats on several days, and a repeat written in words is on one.",
			errors=[
				subroutine.errors.FieldError(
					field=field,
					code="invalid_field_value",
					message="A repeat written in words is read on one day.",
					hint=hint,
				)
			],
			hint=hint,
		)

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

	_refuse_an_interval(value, parts[0].removeprefix("FREQ="), interval, field=field)

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

	# **Text first, as every other field's is** (`#4320`, of the cold review of 2026-10-03). A
	# pattern's ``\s`` matches the four separators from U+001C, so a repeat holding one was read
	# as though it held a space and stored as sent, and one after a rule part was dropped unsaid.
	subroutine.domain.text.readable(value, field=field, label="repeat")

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

		# **Written as a rule is written** (`#4320`): Python reads a fullwidth or an Arabic-Indic
		# digit as a digit, so a part written in them was stored as sent, described as though it
		# were the ASCII number, and reached a calendar's feed as sent.
		if not setting.isascii():
			raise _refuse(
				value,
				field=field,
				why=f"{name} holds a character no rule is written in. A number is written with 0 to 9.",
			)

		# **A part is named once** (`#3997`). dateutil reads the last of two, and the stored rule kept
		# both: ``INTERVAL=1;INTERVAL=0`` read back as *every 0 days*, and a second ``UNTIL`` was
		# rewritten over the first, so the end somebody wrote first was gone without a word.
		if name in found:
			raise _refuse(value, field=field, why=f"{name} is given twice. A rule names each part once.")

		found[name] = setting.strip()

	if found.get("FREQ", "").upper() not in FREQUENCIES:
		raise _refuse(
			value,
			field=field,
			why=f"A rule repeats {', '.join(sorted(FREQUENCIES)).lower()} - "
			f"anything finer would materialise faster than anybody works.",
		)

	# **An ``UNTIL`` that is a date is a whole day** (`#3935`), which is how RFC 5545 writes the
	# end beside a start that is one, so an all-day rule copied from a calendar carries it. It was
	# refused with dateutil's sentence about zones, since dateutil takes one only beside a start
	# with none - which is what it is built against here.
	# **One end, not two** (`#3997`; RFC 5545 §3.3.10). dateutil warns while it builds the pair
	# that a future version will refuse it, and which of the two it honours today is not a thing a
	# person should have to know - so asked before it is built.
	if "COUNT" in found and "UNTIL" in found:
		raise _refuse(value, field=field, why="A rule ends after COUNT times or at UNTIL, not both.")

	dated = _A_DATE.fullmatch(found.get("UNTIL", "")) is not None
	built_from = datetime.datetime(2026, 1, 1)

	# Proved by building it, because a part this accepts by name can still be unreadable —
	# `BYDAY=XX` passes the check above and means nothing.
	try:
		dateutil.rrule.rrulestr(
			f"RRULE:{written}",
			dtstart=built_from if dated else built_from.replace(tzinfo=datetime.UTC),
		)

	except (ValueError, TypeError) as unreadable:
		raise _refuse(value, field=field, why=str(unreadable)) from None

	# **A repeat that comes round no times is refused** (`#3935`), as *every 0 days* is: dateutil
	# reads ``COUNT=0`` as a rule with nothing in it, so a new task was refused for having no dates
	# while a change to an old one was stored and read back as *0 times*.
	if "COUNT" in found and int(found["COUNT"]) < 1:
		raise _refuse(
			value,
			field=field,
			why=f"It repeats {found['COUNT']} times, which is never. COUNT starts at 1.",
		)

	_refuse_a_part_that_never_comes(value, found, field=field)
	_refuse_a_day_that_never_comes(value, found, field=field)
	_refuse_a_rule_that_never_comes_round(value, written, field=field)

	# **``UNTIL`` stored in the one spelling everything after this reads** (`#3897`). dateutil reads
	# many - *20261210T0000Z*, *20261210T000000+0000* - and only UTC's ``YYYYMMDDTHHMMSSZ`` was put
	# on the start's clock, so the others saved and then answered 500 when an occurrence was
	# completed. A year no clock can move it into is refused here, where it is written.
	# **A date is stored as it was written** (`#3935`), and read as the whole of its day on the
	# series' own clock by :func:`_on_the_clock`, which is where the zone is known.
	if "UNTIL" in found and not dated:
		until = _until_in_utc(value, found["UNTIL"], field=field)
		written = ";".join(
			f"UNTIL={until}" if piece.partition("=")[0].strip().upper() == "UNTIL" else piece
			for piece in written.split(";")
		)

	# **Stored as it was checked, not as it was typed** (`#929`). Every part of an ``RRULE`` is
	# case-insensitive and this function upper-cases each *name* to validate it — then returned
	# the original string, so ``freq=weekly;byday=mo`` was accepted, stored verbatim, and
	# described back as ``"every "``. The read-back is the whole point of taking a rule this
	# way, so it failing on a rule the parser accepted is the worst available outcome.
	#
	# Safe to upper-case whole: an ``RRULE``'s values are keywords, integers and a UTC
	# timestamp, none of which carries meaning in its case.
	return written.upper()


def _until_in_utc (value: str, until: str, *, field: str) -> str:
	"""Return an ``UNTIL`` as UTC writes it, read the way dateutil reads it - `#3897`.

	Aware by the time it gets here: the rule was built beside a start with a zone, and dateutil
	refuses an ``UNTIL`` without one there.
	"""

	# **An offset of a day or more is refused by name** (`#3997`, L-3.4 of the cold review of
	# 2026-09-30): no clock is that far from UTC, and reading one raised past every refusal, a 500.
	try:
		moment = dateutil.parser.parse(until)
		why = subroutine.domain.schedule.beyond_every_clock(moment.isoformat())
		utc = moment.astimezone(datetime.UTC)

	except (ValueError, OverflowError):
		raise _refuse(
			value, field=field, why=f"It ends at {until}, which is not a time any clock reads."
		) from None

	if why is not None:
		raise _refuse(value, field=field, why=f"It ends at {until}: {why}.")

	return f"{subroutine.domain.dates.basic(utc, '%m%dT%H%M%S')}Z"


def _refuse_a_part_that_never_comes (
	value: str, parts: dict[str, str], *, field: str
) -> None:
	"""Refuse a part whose value no calendar reaches, before anything walks it - `#3997`.

	**H-1 of the cold review of 2026-09-30**: dateutil builds each of these, and then either walks
	for ever, walks to the year 9999, or quietly reads them as something else - ``BYMONTHDAY=0``
	came round every day and read back as *on the 0th to last day*. Each is decidable from the
	rule alone, so it is refused here rather than bounded where it is expanded.
	"""

	frequency = parts["FREQ"].upper()

	if "INTERVAL" in parts:
		_refuse_an_interval(value, frequency, int(parts["INTERVAL"]), field=field)

	days = [int(piece) for piece in parts.get("BYMONTHDAY", "").split(",") if piece.strip()]

	if any(day == 0 or abs(day) > 31 for day in days):
		raise _refuse(
			value,
			field=field,
			why="A day of the month is 1 to 31, or -1 to -31 counting back from its last day.",
		)

	months = [int(piece) for piece in parts.get("BYMONTH", "").split(",") if piece.strip()]

	if any(not 1 <= month <= 12 for month in months):
		raise _refuse(value, field=field, why="A month is 1 to 12.")

	# **A count before a weekday means something only where there is more than one of it**: the
	# first Monday of a month or of a year. ``WEEKLY;BYDAY=1MO`` meant every Monday and read back
	# as *every the first Monday*, and the sixth Monday of a month never comes.
	within = 5 if frequency == "MONTHLY" or months else 53

	for entry in parts.get("BYDAY", "").split(","):
		found = _A_WEEKDAY.fullmatch(entry.strip().upper())

		if found is None or found.group("count") is None:
			continue

		count = int(found.group("count"))

		if frequency not in ("MONTHLY", "YEARLY"):
			raise _refuse(
				value,
				field=field,
				why=f"{entry.strip()} counts a weekday, which only a monthly or yearly repeat can do.",
			)

		if count == 0 or abs(count) > within:
			raise _refuse(
				value,
				field=field,
				why=f"{entry.strip()} counts past the {within} there can be in "
				f"{'a month' if within == 5 else 'a year'}.",
			)


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


#: A rule's ``UNTIL``, in whichever spelling it was stored (`#3897`). RFC 5545 writes one in UTC
#: beside a start that has a zone, and rows saved before :func:`_checked` settled on that
#: spelling hold others dateutil reads as well.
_UNTIL = re.compile(r"UNTIL=([^;]+)")

#: An ``UNTIL`` that is a date and no time, as RFC 5545 writes one beside a start that is a date.
_A_DATE = re.compile(r"\d{8}")


def _on_the_clock (stored: str, zone: datetime.tzinfo) -> str:
	"""Return a rule with an ``UNTIL`` that has a zone rewritten on ``zone``'s clock, as its start is (`#3765`)."""

	def local (found: re.Match[str]) -> str:
		"""Return one ``UNTIL`` as the zone's clock read that instant, its year in four digits."""

		# **A date is the whole of its day on this clock** (`#3935`), so a slot at its first second,
		# a deadline at its last and a meeting between are all on it.
		if _A_DATE.fullmatch(found[1]):
			return f"UNTIL={found[1]}T235959"

		try:
			instant = dateutil.parser.parse(found[1])

		except (ValueError, OverflowError):
			return found[0]

		if instant.tzinfo is None:
			return found[0]

		try:
			clock = instant.astimezone(zone)

		# **A year no clock can move it into** was accepted until `#3897` refused it, and a row may
		# hold one still: its own reading is within a day of right, where raising was a 500.
		except OverflowError:
			clock = instant.replace(tzinfo=None)

		return f"UNTIL={subroutine.domain.dates.basic(clock, '%m%dT%H%M%S')}"

	return _UNTIL.sub(local, stored)


def for_a_calendar (
	stored: str, *, whole_day: bool, timezone: str, at_its_end: bool = False
) -> str:
	"""Return a rule with its ``UNTIL`` in the form its start takes, for a calendar - `#3935`.

	**RFC 5545 §3.3.10 makes the two match**: a date beside a start that is a date, and a date-time
	in UTC beside one with a time. A rule stores one ``UNTIL`` whatever its series is, and the feed
	copied it as stored, so an all-day series ending on a day was written with a date-time beside
	its ``VALUE=DATE`` start, which a strict client refuses. The instant is read on ``timezone``'s
	clock, the series' own, as :func:`_on_the_clock` reads it; one that cannot be read is left be.

	**For a whole-day series, the last day it falls on** (`#4026`, L-3 (2) of the cold review of
	2026-09-30), which ``at_its_end`` decides: a whole-day deadline falls at the end of its day,
	so an ``UNTIL`` before that on its own day ends the series the day before. The instant's own
	date was written, so a calendar showed a day the program never makes.
	"""

	zone = subroutine.domain.dates.zone(timezone)

	def written (found: re.Match[str]) -> str:
		"""Return one ``UNTIL`` in the form beside the start, or as it was where it cannot be read."""

		until = found[1]

		if _A_DATE.fullmatch(until):
			if whole_day:
				return found[0]

			until = f"{until}T235959"

		try:
			instant = dateutil.parser.parse(until)
			clock = (
				instant.replace(tzinfo=zone) if instant.tzinfo is None else instant.astimezone(zone)
			)

			if whole_day:
				day = clock.date()

				if at_its_end and clock.time() < _LAST_SECOND:
					day -= datetime.timedelta(days=1)

				return f"UNTIL={subroutine.domain.dates.basic(day, '%m%d')}"

			return (
				f"UNTIL={subroutine.domain.dates.basic(clock.astimezone(datetime.UTC), '%m%dT%H%M%S')}Z"
			)

		except (ValueError, OverflowError):
			return found[0]

	return _UNTIL.sub(written, stored)


#: **How long the calendar takes to repeat itself**: four hundred Gregorian years are exactly
#: 146,097 days, which is 20,871 weeks and 4,800 months, so every pattern of days falls the same
#: way again after it.
CALENDAR_CYCLE_YEARS = 400

#: Where a rule is first walked from to see whether it comes round at all: the year rules are
#: checked against, moved on by whole cycles.
_PROBED_FROM = datetime.datetime(2026, 1, 1)


def _refuse_a_rule_that_never_comes_round (value: str, written: str, *, field: str) -> None:
	"""Refuse a rule no day ever satisfies, walking no further than the calendar's cycle - `#4320`.

	**One cycle of the calendar is far enough**: a pattern naming no day in four hundred years names
	none at all, since the calendar falls the same way again after them. dateutil walked such a
	rule from its start to the year 9999 before saying it had nothing - a third of a second for
	``BYMONTHDAY=1;BYDAY=2MO``, on a create and on every reading of it - and the refusal that
	followed spoke of dates that had passed.

	**The pattern alone, every interval apart.** With an interval, whether a rule comes round can
	turn on its start - every seventh day from a Tuesday is always a Tuesday, and from a Monday
	never - so that is left to the series' own start, and this refuses only what no start reaches.

	**Started late rather than ended early**, because dateutil checks an end only against a day it
	found, and a rule that finds none walks on to the year 9999 whatever its end. So the walk begins
	on a year the calendar falls on as it does in 2026, a cycle or two before 9999.

	**Its own end is set aside**, so this asks whether the pattern ever comes round and not whether
	it comes round again, which :func:`occurrences` asks with the series' own start.
	"""

	spare = datetime.MAXYEAR - _PROBED_FROM.year - CALENDAR_CYCLE_YEARS
	start = _PROBED_FROM.replace(
		year=_PROBED_FROM.year + CALENDAR_CYCLE_YEARS * (spare // CALENDAR_CYCLE_YEARS)
	)
	pattern = ";".join(
		piece
		for piece in written.split(";")
		if piece.strip()
		and piece.partition("=")[0].strip().upper() not in {"COUNT", "UNTIL", "INTERVAL"}
	)

	if next(iter(dateutil.rrule.rrulestr(f"RRULE:{pattern}", dtstart=start)), None) is not None:
		return

	raise _refuse(
		value, field=field, why="No day matches every part of it, so it would never come round."
	)


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

	# **An ``UNTIL`` in UTC is read on the clock the start is walked on** (`#3765`). The rule is
	# walked on local wall-clock time from a start with no zone, and dateutil refuses to compare
	# that with an ``UNTIL`` that has one - so every rule ending on a date answered 500 the moment
	# an occurrence was completed or skipped.
	series = dateutil.rrule.rrulestr(f"RRULE:{_on_the_clock(stored, zone)}", dtstart=anchor)

	found: list[datetime.datetime] = []
	ceiling = None if until is None else until.astimezone(zone).replace(tzinfo=None)
	walked = _walked(series, stored)

	for moment in walked:
		if cursor is not None and moment <= cursor:
			continue

		if ceiling is not None and moment > ceiling:
			break

		# **Localised one at a time rather than by shifting the whole series**, because the
		# offset is not constant across it: an hour that does not exist on the day the clocks
		# go forward is what this per-occurrence conversion is for.
		#
		# **And the anchor's microsecond is put back, because ``dateutil`` does not keep one**
		# (`#1291`). It builds its time set from ``dtstart``'s hour, minute and second and
		# discards anything below — so a series anchored at ``23:59:59.999999`` comes back at
		# ``23:59:59``, silently, every time. §6.5 stores an all-day *deadline* at exactly that
		# instant, so **every repeating deadline there has ever been is anchored on the one
		# value this rounds off**.
		#
		# **Restored here rather than at the callers, because the loss is here.** A caller that
		# compares what it asked for against what came back — ``materialise`` computes
		# ``occurrence - anchor`` and moves a whole grid by it — reads the rounding as a
		# deliberate move, which is how a bill due on the 1st walked a day forward on every
		# save.
		#
		# **The anchor's own microsecond and not the moment's**, which is the whole of what was
		# dropped: the rule grammar has no ``BYHOUR``/``BYMINUTE``/``BYSECOND``, so every slot
		# a rule names is at the anchor's time of day and there is nothing else it could be.
		#
		# **After the cursor comparison above, deliberately.** ``after`` may be a row stored
		# before this fix, a microsecond-truncated copy of the slot it names; restoring first
		# would make that row compare as *earlier* than its own slot and mint it a second time.
		found.append(
			moment.replace(microsecond=anchor.microsecond, tzinfo=zone)
			.astimezone(datetime.UTC)
		)

		if limit is not None and len(found) >= limit:
			break

	return found


def _walked (
	series: typing.Iterable[datetime.datetime], stored: str
) -> typing.Iterator[datetime.datetime]:
	"""Walk a rule's moments, stopping where it stops moving forward - `#3997`.

	**A rule stored before its parts were checked can still hold** ``INTERVAL=0``, which dateutil
	answers with the same moment for ever: every caller skipping the moments at or before a cursor
	then never returned. Every rule this stores yields strictly later moments, so one that is not
	later is the end of what the rule can say. A rule dateutil cannot walk at all is refused by
	name rather than raised past every refusal.
	"""

	latest: datetime.datetime | None = None

	try:
		for moment in series:
			if latest is not None and moment <= latest:
				return

			latest = moment

			yield moment

	except (ValueError, OverflowError) as unreadable:
		raise _refuse(stored, field="recurrence", why=f"It cannot be followed: {unreadable}.") from None


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

		said.append(day if which is None else f"the {_which(int(which), ordinals)} {day}")

	return " and ".join(said)


def _which (number: int, ordinals: dict[int, str]) -> str:
	"""Return which weekday of a month or year a count names, in words - `#4320`.

	**Past the fourth, as a number**: ``5MO`` read as *the 5 Monday* and ``-2MO`` as *the -2
	Monday*, though the rule accepts every one of them. A count from the end is *to last*.
	"""

	if number in ordinals:
		return ordinals[number]

	if number > 0:
		return _ordinal(number)

	return f"{ordinals.get(-number) or _ordinal(-number)} to last"


#: How a completion anchor reads, said once so that no two surfaces word it differently
#: (`#674`). **Only the non-default is ever said**, on `views.status_is_news`'s rule: a
#: schedule anchor is what "every month on the 30th" already sounds like, so naming it would
#: put a clause on every repeating row to tell the reader nothing.
_MEASURED_FROM_COMPLETION = "from when it is done"

#: **The twelve months as a description names them** (`#4026`, L-3 (3) of the cold review of
#: 2026-09-30). They were taken from :data:`_MONTHS` by length, which dropped *may*, three
#: letters long, and let *sept* stand for September.
_MONTH_NAMES = (
	"January", "February", "March", "April", "May", "June",
	"July", "August", "September", "October", "November", "December",
)

#: How a count of one or two is said (`#4026`, L-3 (6)): *1 times* read as a slip.
_TIMES = {1: "once", 2: "twice"}

#: Where on its own day a whole-day deadline falls, as a rule's occurrences carry it: the last
#: second, since the rule keeps no part of one (`#1291`).
_LAST_SECOND = datetime.time(23, 59, 59)


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

	# **Each of these may be a list** (`#3923`) - the 1st and the 15th, January and July - which
	# is an ordinary rule, and reading one as a single number answered 500 on every write. **And a
	# month is said wherever the rule names one** (`#4026`, a3 NEW-3 of the verification of the cold
	# review of 2026-09-30): only beside a day of the month, it read June's first Monday as every
	# first Monday.
	months = (
		" and ".join(
			_MONTH_NAMES[one - 1] if 1 <= one <= 12 else str(one)
			for one in _numbers(parts["BYMONTH"])
		)
		if "BYMONTH" in parts
		else None
	)

	if months is not None and "BYMONTHDAY" not in parts:
		said = f"{said}, in {months}"

	elif months is not None:
		numbers = _numbers(parts["BYMONTHDAY"])

		# **A day counted from the end is said as one** (`#3935`): *on -1 February* was the
		# whole of it before.
		if all(one > 0 for one in numbers):
			days = " and ".join(str(one) for one in numbers)
			said = f"{said}, on {days} {months}"

		else:
			days = " and ".join(_day_of_the_month(one) for one in numbers)
			said = f"{said}, on the {days} of {months}"

	elif "BYMONTHDAY" in parts:
		days = " and ".join(_day_of_the_month(one) for one in _numbers(parts["BYMONTHDAY"]))
		said = f"{said}, on the {days}"

	if "COUNT" in parts:
		count = int(parts["COUNT"])
		said = f"{said}, {_TIMES.get(count, f'{count} times')}"

	if "UNTIL" in parts:
		said = f"{said}, until {parts['UNTIL']}"

	if anchor == "completion":
		said = f"{said}, {_MEASURED_FROM_COMPLETION}"

	return said


def _numbers (setting: str) -> list[int]:
	"""Return a part of a rule that may list several numbers - ``1,15`` - as those numbers."""

	return [int(one) for one in setting.split(",")]


def _day_of_the_month (number: int) -> str:
	"""Return a ``BYMONTHDAY`` as words: 15 as ``15th``, -1 as ``last day`` - `#3935`.

	**A negative day counts back from the end of the month**, and it read back as *the -1th*,
	where :func:`_described_weekdays` already said *the last Thursday* from :data:`_ORDINALS`.
	"""

	if number > 0:
		return _ordinal(number)

	if number == _ORDINALS["last"]:
		return "last day"

	return f"{_ordinal(-number)} to last day"


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
