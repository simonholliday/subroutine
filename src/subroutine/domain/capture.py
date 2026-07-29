"""Turning a line of typing into a task, without ever losing what was typed.

``"Call the dentist before Sunday !3 ~15m #health"`` becomes a title, a deadline, an
importance, an estimate and a tag (SPEC.md §6.13). The feature is a convenience, and the
two rules that keep a convenience from becoming a liability are both structural:

1. **Parsing never loses data.** Anything that does not parse stays in the title exactly as
   written. There is no path here that produces a task with a wrong date *and* a title with
   the evidence removed.
2. **It is previewable.** :func:`parse` is a pure function of text, a clock and a timezone.
   It touches no database and creates nothing, so a client can show what would happen and
   an agent can check itself before committing.

**The date vocabulary is closed, and that is the whole design.** It would have been easy to
hand the phrase after ``before`` to a natural-language date library, and the first version
of this was going to. Measured against the strings this grammar actually meets, that
library reads ``"a"`` as a date — and ``"may"``, and ``"march"``, and ``"sat"``. ``before a
meeting`` would have become a task due the 29th of January titled ``meeting``, which is
rule 1's exact failure mode. So the vocabulary here is enumerated, published in
``/v1/meta`` verbatim, and anything outside it simply stays in the title.
"""

import dataclasses
import datetime
import re
import typing

import subroutine.domain.dates
import subroutine.domain.durations
import subroutine.domain.schedule
import subroutine.errors

#: Which field a leading word assigns to (SPEC.md §6.13's table).
DEADLINE_WORDS = ("before", "by", "due")
PLANNED_WORDS = ("on",)
DEFER_WORDS = ("from", "defer")

#: Bare words that plan a task without needing a preposition. Deliberately only these two:
#: they are unambiguous and overwhelmingly common, and every further one is a word somebody
#: wanted in their title.
#:
#: **Only at the very end of the line** (:data:`_BARE_DAY`). "Buy milk tomorrow" plans;
#: "Remember what happened today" does not, and neither does "Ask about tomorrow-ish
#: plans". Mid-sentence these words are almost always prose, and reading them as a field
#: both sets a date nobody asked for and takes a word out of the title.
BARE_PLANNED_WORDS = ("today", "tomorrow")

#: Weekday names and their common abbreviations, mapped to Python's Monday-is-zero.
WEEKDAYS: dict[str, int] = {
	"monday": 0, "mon": 0,
	"tuesday": 1, "tue": 1, "tues": 1,
	"wednesday": 2, "wed": 2,
	"thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
	"friday": 4, "fri": 4,
	"saturday": 5, "sat": 5,
	"sunday": 6, "sun": 6,
}

#: The largest and smallest an importance may be (SPEC.md §6.3).
IMPORTANCE_RANGE = (1, 5)

#: Recurrence is M7. Until the RRULE parser exists this is recognised only well enough to
#: be *left alone* — publishing a grammar the installation does not implement is worse than
#: publishing a smaller one, so `/v1/meta` omits the row and the text stays in the title.
_EVERY = re.compile(r"\bevery\s+\S+", re.IGNORECASE)

_WEEKDAY_ALTERNATION = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_KEYWORD_ALTERNATION = "|".join(
	sorted(subroutine.domain.dates.KEYWORDS, key=len, reverse=True)
)

#: One date phrase. Ordered longest-form-first, because Python's alternation takes the
#: first branch that matches rather than the longest.
_PHRASE = (
	r"(?:"
	rf"next\s+(?:{_WEEKDAY_ALTERNATION})"
	rf"|(?:{_KEYWORD_ALTERNATION})(?:[+-]\d+[a-zA-Z]+)*"
	r"|\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?"
	rf"|(?:{_WEEKDAY_ALTERNATION})"
	r")"
)

_DATED = re.compile(
	rf"\b(?P<word>{'|'.join((*DEADLINE_WORDS, *PLANNED_WORDS, *DEFER_WORDS))})"
	rf"\s+(?P<phrase>{_PHRASE})\b",
	re.IGNORECASE,
)

#: **Every sigil must start a word.** Without this, ``Email bob@example.com`` assigns the
#: task to "example.com" and leaves "Email bob about it" as the title — data lost, exactly
#: what rule 1 forbids. Measured, not theorised: it was the first thing tried.
_STARTS_A_WORD = r"(?<![^\s])"

#: A bare planning word, anchored to the end of the line and required to be a whole word.
#:
#: The end-anchor is the decision above. The ``(?<![^\s])`` guard is a defect fix: ``\b``
#: sits between ``w`` and ``'``, so ``tomorrow's party`` matched ``tomorrow`` and left
#: ``'s party`` as the title — a mangled title *and* a date set from the wreckage, which is
#: exactly what §6.13 rule 1 forbids.
_BARE_DAY = re.compile(
	rf"{_STARTS_A_WORD}(?P<phrase>{'|'.join(BARE_PLANNED_WORDS)})[.!?]*\s*$",
	re.IGNORECASE,
)

#: Punctuation that ends a sentence rather than belonging to the value beside it. Trimmed
#: from every sigil, because ``#hashtag,`` created a tag literally named "hashtag," — a
#: permanent piece of litter, since tags are auto-created and never reviewed — and
#: ``@bob,`` failed its lookup with "there is nobody called 'bob,'".
_TRAILING = r"(?<![,.;:!?)\]])"

#: A tag begins with a letter, so ``Fix issue #12`` keeps its issue number: in this
#: project's own domain a ``#`` followed by digits is far more often a reference than a
#: label, and a tag named "12" helps nobody.
_TAG = re.compile(rf"{_STARTS_A_WORD}#(?P<value>[A-Za-z][^\s#]*?){_TRAILING}[,.;:!?)\]]*(?=\s|$)")
_ASSIGNEE = re.compile(rf"{_STARTS_A_WORD}@(?P<value>[^\s@]+?){_TRAILING}[,.;:!?)\]]*(?=\s|$)")
_IMPORTANCE = re.compile(rf"{_STARTS_A_WORD}!(?P<value>[1-5])[,.;:!?)\]]*(?=\s|$)")

#: **An estimate must carry a unit**, so ``~90m`` and ``~2h`` parse and ``~5`` does not.
#: The duration grammar reads a bare number as minutes (§6.4) and that is right there; here
#: it is wrong, because in prose ``~5`` means "about five" — ``Invite ~5 people`` would
#: otherwise become a five-minute task to invite people.
_ESTIMATE = re.compile(
	rf"{_STARTS_A_WORD}~(?P<value>\d+[a-zA-Z][a-zA-Z0-9]*)[,.;:!?)\]]*(?=\s|$)"
)
_PROJECT = re.compile(
	rf"{_STARTS_A_WORD}\+(?P<value>[A-Za-z][A-Za-z0-9]*)[,.;:!?)\]]*(?=\s|$)"
)

#: Whole-day phrases. Anything else names an instant, so it is not all-day.
_WHOLE_DAY_KEYWORDS = frozenset({"today", "tomorrow", "yesterday"})


@dataclasses.dataclass(frozen=True)
class Capture:
	"""What a line of text would become, without having become it yet.

	Returned by :func:`parse` for both the preview endpoint and the create path, so the
	thing a user is shown is by construction the thing that will happen.
	"""

	title: str
	due: datetime.date | str | None = None
	due_is_all_day: bool | None = None
	planned_for: datetime.date | None = None
	start: datetime.date | str | None = None
	start_is_all_day: bool | None = None
	importance: int | None = None
	estimate_minutes: int | None = None
	tags: tuple[str, ...] = ()
	assignee: str | None = None
	project_key: str | None = None

	#: Tokens that look like grammar and were deliberately left in the title — today, only
	#: ``every …``. Carried so a preview can say *why* something was not parsed rather than
	#: leaving a user to wonder whether it was seen at all.
	unparsed: tuple[str, ...] = ()


def parse (
	text: str, *, now: datetime.datetime, timezone: str = subroutine.domain.schedule.DEFAULT_TIMEZONE
) -> Capture:
	"""Read a line of text into the fields it names, leaving everything else in the title.

	Pure: no session, no clock, no writes. ``now`` and ``timezone`` are supplied so that a
	preview and the create that follows it resolve identically.
	"""

	today = subroutine.domain.schedule.local_date(now, timezone)
	claimed: list[tuple[int, int]] = []
	fields: dict[str, typing.Any] = {}
	tags: list[str] = []
	unparsed: list[str] = []

	# Recurrence first, and only to reserve it: claiming the span stops `every monday` from
	# being read as a planned day, while leaving the words in the title (M7).
	for match in _EVERY.finditer(text):
		unparsed.append(match.group(0))

	reserved = [match.span() for match in _EVERY.finditer(text)]

	_collect_dates(text, claimed, reserved, fields, today=today, now=now, timezone=timezone)
	_collect_sigils(text, claimed, reserved, fields, tags)
	_collect_bare_days(text, claimed, reserved, fields, today=today)

	return Capture(
		title=_remaining(text, claimed),
		tags=tuple(tags),
		unparsed=tuple(unparsed),
		**fields,
	)


def _collect_dates (
	text: str,
	claimed: list[tuple[int, int]],
	reserved: list[tuple[int, int]],
	fields: dict[str, typing.Any],
	*,
	today: datetime.date,
	now: datetime.datetime,
	timezone: str,
) -> None:
	"""Consume ``before Sunday``-style phrases, first one per field winning."""

	for match in _DATED.finditer(text):
		if _overlaps(match.span(), claimed) or _overlaps(match.span(), reserved):
			continue

		word = match.group("word").lower()
		phrase = match.group("phrase")
		value, all_day = _read_phrase(phrase, today=today, now=now, timezone=timezone)

		if value is None:
			continue

		if word in PLANNED_WORDS:
			if "planned_for" in fields:
				continue

			# `planned_for` is a date and nothing else, so an instant is read as the day it
			# falls on where the user is.
			fields["planned_for"] = _as_date(value, now=now, timezone=timezone)

		elif word in DEADLINE_WORDS:
			if "due" in fields:
				continue

			fields["due"], fields["due_is_all_day"] = value, all_day

		else:
			if "start" in fields:
				continue

			fields["start"], fields["start_is_all_day"] = value, all_day

		claimed.append(match.span())


def _collect_sigils (
	text: str,
	claimed: list[tuple[int, int]],
	reserved: list[tuple[int, int]],
	fields: dict[str, typing.Any],
	tags: list[str],
) -> None:
	"""Consume ``#tag``, ``@name``, ``!3``, ``~15m`` and ``+KEY``."""

	for match in _TAG.finditer(text):
		if not _overlaps(match.span(), claimed) and not _overlaps(match.span(), reserved):
			name = match.group("value").lower()

			# `#a #b #a` is one person typing quickly, not three tags. `tags.ensure` would
			# collapse it anyway; collapsing here keeps the preview honest about what will
			# happen.
			if name not in tags:
				tags.append(name)

			claimed.append(match.span())

	for pattern, name in ((_ASSIGNEE, "assignee"), (_PROJECT, "project_key")):
		for match in pattern.finditer(text):
			if name in fields or _overlaps(match.span(), claimed) or _overlaps(match.span(), reserved):
				continue

			value = match.group("value")
			fields[name] = value.upper() if name == "project_key" else value
			claimed.append(match.span())

	for match in _IMPORTANCE.finditer(text):
		if "importance" in fields or _overlaps(match.span(), claimed):
			continue

		fields["importance"] = int(match.group("value"))
		claimed.append(match.span())

	for match in _ESTIMATE.finditer(text):
		if "estimate_minutes" in fields or _overlaps(match.span(), claimed):
			continue

		try:
			fields["estimate_minutes"] = subroutine.domain.durations.parse(match.group("value"))

		except subroutine.errors.SubroutineError:
			# Rule 1. `~soon` is not an estimate, so it stays in the title rather than
			# failing the whole capture over one token.
			continue

		claimed.append(match.span())


def _collect_bare_days (
	text: str,
	claimed: list[tuple[int, int]],
	reserved: list[tuple[int, int]],
	fields: dict[str, typing.Any],
	*,
	today: datetime.date,
) -> None:
	"""Consume a bare ``today`` or ``tomorrow``, which plans rather than deadlines."""

	if "planned_for" in fields:
		return

	for match in _BARE_DAY.finditer(text):
		if _overlaps(match.span(), claimed) or _overlaps(match.span(), reserved):
			continue

		offset = 1 if match.group("phrase").lower() == "tomorrow" else 0
		fields["planned_for"] = today + datetime.timedelta(days=offset)
		claimed.append(match.span())

		return


def _read_phrase (
	phrase: str,
	*,
	today: datetime.date,
	now: datetime.datetime,
	timezone: str,
) -> tuple[datetime.date | str | None, bool | None]:
	"""Return what a date phrase means, and whether it names a whole day.

	``None`` means "not something we parse", which sends the whole token back to the title.
	"""

	written = phrase.strip()
	lowered = written.lower()

	if lowered.startswith("next"):
		name = lowered.split(maxsplit=1)[1]

		# "next Friday" is the Friday of the following week, not this week's.
		return _weekday(name, today=today) + datetime.timedelta(days=7), True

	if lowered in WEEKDAYS:
		return _weekday(lowered, today=today), True

	if lowered in _WHOLE_DAY_KEYWORDS:
		return subroutine.domain.schedule.local_date(
			subroutine.domain.dates.resolve(lowered, now=now, timezone=timezone), timezone
		), True

	# Everything else — a §9.3 expression or an ISO value — is handed to `schedule`, which
	# already knows how to read both and how to infer all-day from the form.
	return written, None


def _weekday (name: str, *, today: datetime.date) -> datetime.date:
	"""Return the soonest date with this weekday name, counting today.

	"by Friday" said on a Friday means today, which is what somebody means by it. The other
	reading — always the next one — makes a task due today impossible to express.
	"""

	ahead = (WEEKDAYS[name] - today.weekday()) % 7

	return today + datetime.timedelta(days=ahead)


def _as_date (
	value: datetime.date | str, *, now: datetime.datetime, timezone: str
) -> datetime.date:
	"""Return a phrase's value as a calendar date, for ``planned_for``."""

	if isinstance(value, datetime.date):
		return value

	return subroutine.domain.schedule.interpret_day(value, timezone=timezone, now=now) or now.date()


def _overlaps (span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
	"""Report whether a span collides with one already taken."""

	start, end = span

	return any(start < taken_end and taken_start < end for taken_start, taken_end in spans)


def _remaining (text: str, claimed: list[tuple[int, int]]) -> str:
	"""Return the text with every consumed span removed and the gaps closed up.

	Only whitespace is normalised, and only where a removal left it doubled. Punctuation and
	capitalisation inside what is left are untouched — a title is what the person typed.
	"""

	kept = []
	cursor = 0

	for start, end in sorted(claimed):
		kept.append(text[cursor:start])
		cursor = max(cursor, end)

	kept.append(text[cursor:])

	return re.sub(r"\s+", " ", "".join(kept)).strip()
