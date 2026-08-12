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
#: **Only at the very end of the line** (:data:`_BARE_DAY`), and the end is measured once
#: the sigils have been taken out — see :func:`_collect_bare_days`. "Buy milk tomorrow"
#: plans, and so does "Buy milk tomorrow !3"; "Remember what happened today" does not, and
#: neither does "Ask about tomorrow-ish plans". Mid-sentence these words are almost always
#: prose, and reading them as a field both sets a date nobody asked for and takes a word out
#: of the title.
BARE_PLANNED_WORDS = ("today", "tomorrow")

#: The largest and smallest an importance may be (SPEC.md §6.3).
IMPORTANCE_RANGE = (1, 5)

#: **Every sigil must start a word.** Without this, ``Email bob@example.com`` assigns the
#: task to "example.com" and leaves "Email bob about it" as the title — data lost, exactly
#: what rule 1 forbids. Measured, not theorised: it was the first thing tried.
_STARTS_A_WORD = r"(?<![^\s])"

#: Recurrence is M7. Until the RRULE parser exists this is recognised only well enough to
#: be *left alone* — publishing a grammar the installation does not implement is worse than
#: publishing a smaller one, so `/v1/meta` omits the row and the text stays in the title.
#:
#: **It has to match the whole phrase, because the phrase is quoted back** (`#206`). This was
#: ``every\s+\S+``, so "Water plants every 2 days" reserved ``every 2`` and the preview said
#: *"Left as written: every 2"* — a sentence about what somebody typed that misquotes them, on
#: the one surface whose job is confirming their words survived. The title was always right;
#: the report of it was not.
#:
#: The count and ``other`` are the two things that come between ``every`` and its unit, so
#: they are what the pattern has to reach past. It stays deliberately loose about the unit
#: itself — "every fortnight" is reserved and reported exactly like "every monday", because
#: the point here is to *decline* a phrase rather than to understand one.
_EVERY = re.compile(
	rf"{_STARTS_A_WORD}every\s+(?:other\s+)?(?:\d+\s+)?\S+", re.IGNORECASE
)

_WEEKDAY_ALTERNATION = "|".join(
	sorted(subroutine.domain.dates.WEEKDAYS, key=len, reverse=True)
)
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

#: A time of day — ``18:30``, ``9:05``, ``2pm``, ``2:30 pm`` — optionally introduced by ``at``
#: (`#797`).
#:
#: **Two alternatives rather than one, because a 24-hour clock needs the colon and a meridiem
#: does not.** ``2pm`` is a time and a bare ``2`` is not, so the meridiem is what licenses the
#: minute-less form; requiring two digits after the colon is what keeps ``1:1`` out, which is
#: how a weekly one-to-one is written and is not 1 minute past one.
#:
#: **A signal is required, and that is this module's whole philosophy applied to clocks.** The
#: date vocabulary is closed because a library that guessed read ``may`` and ``march`` as dates;
#: a rule that read every ``3pm`` would do the same to ``Email Bob re: 3pm``, which
#: `tests/test_capture.py` has guarded since the grammar existed — *"there is no rule that looks
#: at a bare time of day and hopes"*. So a time is read only when the writer signalled it: either
#: introduced by ``at``, or **immediately following a date phrase this grammar already read**.
#: ``from monday 09:00`` qualifies on the second; ``Dentist appointment Monday 14:00`` qualifies
#: on neither, because a bare weekday is not read — so the time is reported rather than guessed,
#: and whether *that* should change is `#797`'s open question about weekdays.
#:
#: **A range is deliberately not matched.** ``14:00-15:00`` is an appointment with an end, and
#: an end has nowhere to go (`#798` records that ``estimate`` is a duration rather than a
#: finish). Matching the first half would silently keep the start and drop the finish, so the
#: lookahead refuses the whole thing and it is reported instead — which is `#778`'s rule, that
#: the grammar says when it saw something it could not use.
_TIME = re.compile(
	rf"{_STARTS_A_WORD}(?P<at>at\s+)?(?:"
	r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>[ap]m)"
	r"|"
	r"(?P<hour24>\d{1,2}):(?P<minute24>\d{2})(?!\s*[ap]m)"
	# The en dash is written as an escape rather than typed: ruff flags the literal as
	# confusable with a hyphen, and it is — which is the reason both are in the class.
	r")(?!\d)(?!\s*[-\u2013]\s*\d)(?!\w)",
	re.IGNORECASE,
)

#: A day the writer named that this grammar does not read on its own — a bare weekday, or a
#: `today` that is not last. Used only to stop :func:`_apply_time` inventing *today* beside a
#: word that says otherwise; nothing reads a date from it.
_UNREAD_DAY = re.compile(
	rf"{_STARTS_A_WORD}(?:{_WEEKDAY_ALTERNATION}|{'|'.join(BARE_PLANNED_WORDS)})(?!\w)",
	re.IGNORECASE,
)

#: What a time-shaped thing that could not be read is called back to the writer. Named here
#: rather than inline so the refusal and the test cannot drift.
_TIME_LOOKS_LIKE = re.compile(
	rf"{_STARTS_A_WORD}(?:at\s+)?\d{{1,2}}(?::\d{{2}})?\s*(?:[ap]m|:\d{{2}})", re.IGNORECASE
)

#: Punctuation that ends a sentence rather than belonging to the value beside it. Trimmed
#: from every sigil, because ``#hashtag,`` created a tag literally named "hashtag," — a
#: permanent piece of litter, since tags are auto-created and never reviewed — and
#: ``@bob,`` failed its lookup with "there is nobody called 'bob,'".
_TRAILING = r"(?<![,.;:!?)\]])"

#: A tag is anything after a ``#`` that is not *entirely* digits, because an all-digit one
#: is a reference to an item (SPEC.md §6.15) and the two share the sigil. So ``Fix issue
#: #12`` keeps its number and gains no tag named "12", while ``#3d-printing`` and ``#2fa``
#: are ordinary tags.
#:
#: The digit test is applied to the match rather than written into the pattern: excluding
#: an all-digit run with a lookahead is possible and unreadable, and the loop has to skip
#: the match *without claiming its span* anyway, so that the text stays in the title for the
#: mention index to find.
_TAG = re.compile(
	rf"{_STARTS_A_WORD}#(?P<value>[^\s#]+?){_TRAILING}[,.;:!?)\]]*(?=\s|$)"
)
_ASSIGNEE = re.compile(rf"{_STARTS_A_WORD}@(?P<value>[^\s@]+?){_TRAILING}[,.;:!?)\]]*(?=\s|$)")
#: §6.3 has *two* axes and this used to reach one. ``!4`` sets importance; ``!4/2`` sets
#: both. Spelled exactly as the listing renders it back, so what you read is what you can
#: type — and needing no second sigil, since the plausible ones are either cryptic (``!!4``)
#: or collide with ordinary words (``u4``).
#:
#: **Urgency alone is not expressible here**, deliberately: ``!/2`` reads as a typo more
#: readily than as a field. The structured ``urgency`` on ``POST /v1/tasks`` covers it, and
#: a captured line is for the common case.
#:
#: Why it matters more than a missing convenience: ``priority_score`` is null unless both
#: axes are set and every ordering is NULLS LAST, so a task captured ``!4`` scored null and
#: sank below everything ranked, looking exactly like something judged unimportant. Anybody
#: typing ``!4`` reached that. Found by #26's priority column rendering the missing axis as
#: ``?`` rather than as a blank.
_IMPORTANCE = re.compile(
	rf"{_STARTS_A_WORD}!(?P<value>[1-5])(?:/(?P<urgency>[1-5]))?[,.;:!?)\]]*(?=\s|$)"
)

#: **An estimate must carry a unit**, so ``~90m`` and ``~2h`` parse and ``~5`` does not.
#: The duration grammar reads a bare number as minutes (§6.4) and that is right there; here
#: it is wrong, because in prose ``~5`` means "about five" — ``Invite ~5 people`` would
#: otherwise become a five-minute task to invite people.
_ESTIMATE = re.compile(
	rf"{_STARTS_A_WORD}~(?P<value>\d+[a-zA-Z][a-zA-Z0-9]*)[,.;:!?)\]]*(?=\s|$)"
)
#: ``+key`` — which project it goes in. **Hyphens inside, never at an edge** (`#508`), so
#: ``+web-sales`` reads as one key and ``+web.`` still drops the full stop. Without the
#: alternation a hyphenated key parsed as ``+web`` and left ``-sales`` in the title, which is
#: §6.13 rule 1's forbidden outcome: a word may only vanish if a field was set.
_PROJECT = re.compile(
	rf"{_STARTS_A_WORD}\+(?P<value>[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*)[,.;:!?)\]]*(?=\s|$)"
)

#: A ``+`` that begins a word and could have been a project key — whether or not any rule could
#: read the rest. Compared against what the rules claimed, which is what makes an unreadable
#: project name reportable rather than silent (`#778`).
#:
#: **A letter after the sigil, and that is derived rather than chosen** (`#790`). A key begins
#: with one — ``projects.KEY_PATTERN`` is ``[a-z][a-z0-9]*…`` and input is case-folded by
#: ``normalize_key`` before it is checked — so a ``+`` carrying anything else was never an
#: attempt at one. ``tests/test_capture.py`` holds the two rules against each other rather than
#: trusting this paragraph, and it fails a version narrowed to lower case as well as a widened
#: one.
#:
#: The first version was ``\+\S+`` and reported every ``+`` beginning a word, so *"Call +44
#: 7911 123456"* was answered with *a project is named like '+web'*. The item is filed correctly
#: and the words stay in the title either way, so nothing was lost but the sentence — and a
#: sentence that misdescribes what happened is the failure §6.13 rule 1 exists to prevent,
#: arriving from the side meant to fix it.
_SIGIL_LEFT = re.compile(rf"{_STARTS_A_WORD}\+[A-Za-z]\S*")


def names_a_project (text: str) -> bool:
	"""Whether a captured line says which project it belongs to, with ``+KEY``.

	**Here rather than in the callers**, and both clients need it: a default project from a
	`.subroutine` marker (§13.7a) must not override a `+KEY` somebody typed, and the two
	transports would otherwise each hold a copy of the grammar's own rule. This *is* the rule
	— it asks the same pattern the parser uses.
	"""

	return _PROJECT.search(text) is not None


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
	urgency: int | None = None
	estimate_minutes: int | None = None
	tags: tuple[str, ...] = ()
	assignee: str | None = None
	project_key: str | None = None

	#: Tokens that look like grammar and were left in the title: ``every …``, which is
	#: reserved for M7, and a ``+something`` no project rule could read (`#778`). Carried so a
	#: preview can say *why* something was not parsed rather than leaving a user to wonder
	#: whether it was seen at all.
	#:
	#: **One field for both, and the reason is read back off the token.** A second field would
	#: have to be widened through ``clients.base.Captured`` and both transports before either
	#: reporting surface could see it — for a sentence — and every surface that already carries
	#: this one would have gained nothing. The `+` is not a proxy for the kind: it is the sigil
	#: the writer typed, which is what makes reading it back honest rather than clever.
	unparsed: tuple[str, ...] = ()


def explain (unparsed: typing.Sequence[str]) -> str | None:
	"""Return the sentence telling a caller what the grammar declined to read, or ``None``.

	**§6.13 rule 1 is an obligation on every surface, so the sentence lives here.** Text that
	looks like grammar and is not implemented stays in the title verbatim *and the caller is
	told* — otherwise somebody who wrote "every monday" cannot tell whether it was understood,
	ignored, or silently dropped, and the whole point of leaving the words in place is lost.

	One definition because there are three callers and were nearly three sentences: the CLI's
	human path, its ``--json`` path, and the MCP adapter — which had none at all until `#115`,
	and is the surface where it matters most. The CLI's own note says why: an agent is the
	caller most likely to have written something it believes was understood.

	**The second reason arrived on 2026-08-10 and this is where it landed**, as the docstring
	said it would. `#778`: a ``+something`` no project rule could read was left in the title in
	silence, while a well-formed ``+nosuchproject`` was refused by name and an unreadable
	recurrence was reported. The same mistake got the best answer when the key was well formed
	and the worst when it was not — and eight items were filed into the wrong project believing
	otherwise.

	The kind is read back off the sigil rather than carried beside the token; the reason is on
	:class:`Capture`.

	**Neither sentence names a command, and the first draft of the second one did.** It said
	*"'subroutine list --projects' shows the keys here"* — a flag that does not exist, caught by
	running it. Two reasons it stays out even spelled correctly: this string is shared with the
	MCP adapter, whose reader has no shell (`#548`), and the refusal for a project that is
	merely *missing* already lists the real keys, which this cannot do from the domain.
	"""

	if not unparsed:
		return None

	said = [one for one in unparsed if one.startswith("+")]
	timed = [one for one in unparsed if not one.startswith("+")]

	clauses = []

	if timed:
		clauses.append(
			f"Left as written: {', '.join(timed)} — recurring tasks are not supported yet."
		)

	if said:
		clauses.append(
			f"Left as written: {', '.join(said)} — a project is named like '+web': letters and "
			"digits, hyphens inside, and nothing else."
		)

	return " ".join(clauses)


def read_back (summary: str | None) -> str | None:
	"""Return :func:`summarise`'s tokens as something that cannot be read as a title — `#426`.

	**The tokens alone were ambiguous, and a double space was the whole of the separator.**
	``Added: Stop the stamp brokering an introduction  +TERENCE !4/3 #prompt`` gives a reader
	no way to tell where the title ends, which defeats the confirmation `#135` added this for:
	the question being answered is precisely *"was `+TERENCE` understood or left in the
	title?"*, and the answer was rendered so that both readings look the same.

	**Worse on the agent's surface**, where the line already carries the rank: ``!4/3``
	appeared twice, once as the item's priority and once as an echoed token, separated by
	nothing. Reported by an agent that liked the echo and could not parse it.

	Parentheses because the CLI already renders ``(due Sun 2 Aug)`` that way, so this is the
	idiom a reader has met one field earlier rather than a second convention. The word
	``read`` because a group of sigils needs a noun to be a confirmation of anything — and it
	is one of the few available, since §13.5b forbids naming what ``+WEB`` *means* on exactly
	the path that most needs this.

	Beside :func:`explain` and for its reason: three callers, one obligation, and the summary
	half had drifted into two spellings already.
	"""

	if summary is None:
		return None

	return f"(read {summary})"


def summarise (capture: Capture) -> str | None:
	"""Return the sigils the grammar *did* read, or ``None`` if it read none.

	**The mirror of :func:`explain`, and it lives beside it because it is the same
	obligation** (`#135`). Saying what was left as written and not saying what was taken
	leaves the commoner question unanswered: ``subroutine add "Fix the header +WEB"`` filed it
	correctly and confirmed nothing, so somebody who typed ``+WEB`` got back a title with
	``+WEB`` missing and no way to tell whether it had been filed there, dropped, or read as
	part of the sentence. §6.13's rule that a word may only vanish if a field was set is a
	property of the code; it is not something a person can see.

	**Written back as the tokens they were typed**, not as prose. Three reasons, and the third
	is the one that decided it: it is exactly what the user wrote, so it needs no vocabulary
	and no explanation; it is what they would type again; and §13.5b's transcript forbids the
	words ``project``, ``status`` and ``workspace``, so a sentence naming what ``+WEB`` *means*
	could not be printed on the path that most needs it.

	Dates are deliberately absent. They are already rendered in human form beside the title —
	"(due Sun 2 Aug)" is better than echoing "by friday" back, because the useful confirmation
	there is *which day that turned out to be*.
	"""

	parts = []

	if capture.project_key is not None:
		parts.append(f"+{capture.project_key}")

	if capture.importance is not None:
		# Spelled as the grammar accepts it and as a listing renders it: `!4` for importance
		# alone, `!4/2` for both. Urgency alone is not expressible either way (§6.3).
		parts.append(
			f"!{capture.importance}"
			if capture.urgency is None
			else f"!{capture.importance}/{capture.urgency}"
		)

	if capture.estimate_minutes is not None:
		parts.append(f"~{subroutine.domain.durations.humanize(capture.estimate_minutes)}")

	if capture.assignee is not None:
		parts.append(f"@{capture.assignee}")

	parts.extend(f"#{tag}" for tag in capture.tags)

	return " ".join(parts) or None


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

	before = len(claimed)

	_collect_dates(text, claimed, reserved, fields, today=today, now=now, timezone=timezone)

	# Where the date phrases landed, so a time can be recognised as belonging to one. Taken as
	# a slice rather than returned, because `_collect_dates` appends to `claimed` and that is
	# the only place the spans exist — a second list would be a second copy to keep in step.
	dated = list(claimed[before:])

	_collect_sigils(text, claimed, reserved, fields, tags)

	# **Before the bare day, and that ordering is the whole fix** (`#797`). `_collect_bare_days`
	# searches the line with claimed spans blanked out, so a time claimed here turns
	# `Solar eclipse today at 18:30` into `Solar eclipse today` for its purposes — and the
	# end-anchor that makes `today` mean something, which is deliberate and well argued, needs
	# no change at all. Reading the time was the missing half; the anchor was never the defect.
	at = _collect_times(text, claimed, reserved, unparsed, after=dated)

	_collect_bare_days(text, claimed, reserved, fields, today=today)

	used = _apply_time(
		fields,
		None if at is None else at[0],
		today=today,
		unread_day=bool(_UNREAD_DAY.search(_blanked(text, claimed))),
	)

	# **A time that is read and then not used has to go back into the title** (§6.13 rule 1).
	# Claiming it is what lets the bare day be seen as last, and the decision about where it
	# belongs cannot be taken until after that — so the claim is provisional, and this is where
	# it is either kept or given back. Written after driving `Dentist appointment Monday 14:00`
	# and finding the title had lost `14:00` while no field had gained it, which is precisely
	# the outcome this module exists to make impossible.
	if at is not None and not used:
		claimed.remove(at[1])
		unparsed.append(text[at[1][0]:at[1][1]])

	# **A `+` nobody claimed** (`#778`). This runs last because it asks what the rules above
	# took: `_PROJECT` claims the span it read, so anything still unclaimed is a project name
	# the grammar could not parse — `+subroutine/UI`, whose slash the pattern cannot reach past.
	#
	# **Safe by construction rather than by an exclusion list.** `_STARTS_A_WORD` is
	# `(?<![^\s])`, so the `+` has to begin a word: `C++`, `a+b` and `1+1` cannot match, and a
	# bare `+` between spaces has no `\S` after it. Measured rather than reasoned about.
	unparsed.extend(
		match.group(0)
		for match in _SIGIL_LEFT.finditer(text)
		if not any(start < match.end() and match.start() < end for start, end in claimed)
	)

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

			# An all-digit name is a reference, not a label. Left in the text rather than
			# claimed, so `Fix #12` keeps its number in the title and the mention index
			# picks it up from there (SPEC.md §6.15).
			if name.isdigit():
				continue

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

			# **Read as written; the service normalises.** This used to upper-case a project
			# key here, which was a second copy of `projects.normalize_key`'s rule — and when
			# that rule changed to lower case (`#508`) this one did not, so `+secret` was
			# looked up as `SECRET` and refused. Two copies of one rule, disagreeing, which is
			# this codebase's signature defect and was found by a test rather than by reading.
			fields[name] = match.group("value")
			claimed.append(match.span())

	for match in _IMPORTANCE.finditer(text):
		if "importance" in fields or _overlaps(match.span(), claimed):
			continue

		fields["importance"] = int(match.group("value"))

		if match.group("urgency") is not None:
			fields["urgency"] = int(match.group("urgency"))

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


def _collect_times (
	text: str,
	claimed: list[tuple[int, int]],
	reserved: list[tuple[int, int]],
	unparsed: list[str],
	*,
	after: list[tuple[int, int]],
) -> tuple[datetime.time, tuple[int, int]] | None:
	"""Consume a time of day, and report anything time-shaped that could not be read.

	**The first readable one wins**, matching every other field here: a line naming two times
	is naming a range, and a range has no home (`#798`).

	Returns the time rather than writing a field, because where it belongs depends on what the
	*rest* of the line said and the bare day has not been read yet. :func:`_apply_time` decides.
	"""

	found: tuple[datetime.time, tuple[int, int]] | None = None

	for match in _TIME.finditer(text):
		if _overlaps(match.span(), claimed) or _overlaps(match.span(), reserved):
			continue

		if found is not None:
			continue

		# **Signalled, or attached to a date already read.** Without one of the two this is a
		# bare number in prose — `Email Bob re: 3pm` — and reading it is exactly the guessing
		# the closed date vocabulary exists to refuse.
		signalled = match.group("at") is not None or any(
			text[end:match.start()].strip() == "" for _start, end in after
		)

		if not signalled:
			continue

		hour = int(match.group("hour") or match.group("hour24"))
		minute = int(match.group("minute") or match.group("minute24") or 0)
		meridiem = (match.group("meridiem") or "").lower()

		if meridiem:
			# 12am is midnight and 12pm is noon, which is the one place a modulus is needed
			# rather than an addition — `12 + 12` is 24 and there is no such hour.
			hour = hour % 12 + (12 if meridiem == "pm" else 0)

		if not (0 <= hour <= 23 and 0 <= minute <= 59):
			continue

		found = (datetime.time(hour=hour, minute=minute), match.span())

		claimed.append(match.span())

	# **Said out loud when nothing could be read** (`#778`, and `#797`'s own recommendation).
	# A range, a `25:00`, a second time — each looks like an attempt at a time, and silence is
	# what made `#797` cost two sightings before anybody filed it.
	#
	# Reported here rather than at each rejection above, because the two paths overlap: a
	# `25:00` fails the loop *and* matches this scan, and reporting in both put it in the list
	# twice. One scan over what is left is the whole rule.
	for match in _TIME_LOOKS_LIKE.finditer(text):
		if not _overlaps(match.span(), claimed) and not _overlaps(match.span(), reserved):
			unparsed.append(match.group(0))

	return found


def _apply_time (
	fields: dict[str, typing.Any],
	at: datetime.time | None,
	*,
	today: datetime.date,
	unread_day: bool,
) -> bool:
	"""Attach a time of day to whichever date the line established, or to today.

	**A preposition wins, because the writer said which field they meant.** ``due today at
	17:00`` is a deadline with a time; ``from friday 09:00`` is a defer with one.

	**A bare day plus a time becomes a start rather than a plan** — Simon's decision,
	2026-08-12. ``planned_for`` is a date and cannot hold a time, so the two cannot both be
	honoured; and a line carrying a clock time is describing something that *happens* at a
	moment rather than a day's worth of work, which is what ``start_at`` and the agenda's
	appointment bucket are for. So the plan is replaced rather than kept beside it.

	**With no day at all the time is today's**, never tomorrow's. A start already past is
	harmless — it simply means nothing is hidden — where guessing forward invents a date the
	writer did not give.

	**But only when the writer named no day this grammar could not read**, which is the
	correction this function needed and got by driving it. ``Dentist appointment Monday
	14:00`` — `#797`'s original case — has a bare weekday, and a bare weekday needs a
	preposition, so nothing reads it. Falling back to today then set a start of *today* while
	the title still said *Monday*: a date that contradicts the words printed beside it, which
	is worse than the silence being fixed. Where a day is named and unread, the time is
	reported instead and nothing is set.

	**Whether a bare weekday should be read at all is deliberately not decided here.** `#797`
	records it as a genuine trade — it is how people write, and it would make ``Monday`` in an
	ordinary title into a date nobody asked for.

	Left alone where the field already carries an instant or an unresolved expression: a
	literal ``2026-08-20T17:00`` has said its own time, and combining a clock with a keyword
	string is a second grammar nobody asked for.
	"""

	if at is None:
		return False

	for field, flag in (("due", "due_is_all_day"), ("start", "start_is_all_day")):
		value = fields.get(field)

		if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
			fields[field] = datetime.datetime.combine(value, at)
			fields[flag] = False

			return True

	day = fields.pop("planned_for", None)

	if day is None:
		if unread_day:
			return False

		day = today

	fields["start"] = datetime.datetime.combine(day, at)
	fields["start_is_all_day"] = False

	return True


def _collect_bare_days (
	text: str,
	claimed: list[tuple[int, int]],
	reserved: list[tuple[int, int]],
	fields: dict[str, typing.Any],
	*,
	today: datetime.date,
) -> None:
	"""Consume a bare ``today`` or ``tomorrow``, which plans rather than deadlines.

	**Last means last once the sigils are gone.** A bare day only plans when nothing follows
	it, which is what stops ``Discuss tomorrow's plan with Bob`` from setting a date. Read
	against the raw line that rule also caught ``Renew the domain tomorrow !3``, where the
	only thing after the word is a token being removed from the title anyway — so the search
	runs against the line with every claimed span blanked out. Blanking rather than deleting,
	because it keeps every offset where it was and the spans recorded here address the
	original text.

	Spans that are *reserved* rather than claimed are deliberately not blanked: an unparsed
	``every monday`` stays in the title (M7), so a ``tomorrow`` in front of it really is
	mid-sentence.
	"""

	if "planned_for" in fields:
		return

	for match in _BARE_DAY.finditer(_blanked(text, claimed)):
		if _overlaps(match.span(), reserved):
			continue

		offset = 1 if match.group("phrase").lower() == "tomorrow" else 0
		fields["planned_for"] = today + datetime.timedelta(days=offset)
		claimed.append(match.span())

		return


def _blanked (text: str, spans: typing.Sequence[tuple[int, int]]) -> str:
	"""Return ``text`` with each span replaced by spaces of the same width.

	Same length in, same length out, so an index into the result is an index into the
	original. That is the whole reason this blanks rather than deletes.
	"""

	characters = list(text)

	for start, end in spans:
		for position in range(max(start, 0), min(end, len(characters))):
			characters[position] = " "

	return "".join(characters)


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

	named = subroutine.domain.dates.day_named(written, today=today)

	if named is not None:
		return named, True

	if lowered in _WHOLE_DAY_KEYWORDS:
		return subroutine.domain.schedule.local_date(
			subroutine.domain.dates.resolve(lowered, now=now, timezone=timezone), timezone
		), True

	# Everything else — a §9.3 expression or an ISO value — is handed to `schedule`, which
	# already knows how to read both and how to infer all-day from the form.
	return written, None


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
