"""Turning a line of typing into a task, without ever losing what was typed.

``"Call the dentist before Sunday !3 ~15m #health"`` becomes a title, a deadline, an
importance, an estimate and a tag (docs/design.md §6.13). The feature is a convenience, and the
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
import subroutine.domain.recurrence
import subroutine.domain.schedule
import subroutine.errors

#: Which field a leading word assigns to (docs/design.md §6.13's table).
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

#: The words that close a span of whole days after its first date (`#2687`, Simon 2026-09-15).
#:
#: **A span is told from a defer by what follows the first date, not by a word of its own.**
#: English opens one with ``from`` - *Holiday in Dawlish from 2nd October to 12th October* -
#: and ``from`` was already a defer, the only date that hides an item. So ``from X to Y`` and
#: ``from X until Y`` are spans and a bare ``from X`` stays a defer: one rule a reader can
#: predict from the line. ``on`` opens one the same way, and a dash between two written dates
#: needs no word at all.
#:
#: **The same words join two times of day** (`#675`), which is what ``til`` and ``till``
#: are doing here: *at 2pm til 3pm* is the line that item was filed from, and a word that
#: joins two clocks and not two days would be a seam a reader meets by accident.
SPAN_WORDS = ("to", "until", "til", "till")

#: The words a worded span may open with: the defer's and the planned day's own.
SPAN_OPENING_WORDS = ("from", "on")

#: **Every sigil must start a word.** Without this, ``Email bob@example.com`` assigns the
#: task to "example.com" and leaves "Email bob about it" as the title — data lost, exactly
#: what rule 1 forbids. Measured, not theorised: it was the first thing tried.
_STARTS_A_WORD = r"(?<![^\s])"

#: The characters that begin a field in a captured line: a tag, an assignee, a priority, an
#: estimate and a project, in :func:`_collect_sigils`' order. Named once so a phrase that is
#: not one of them can refuse to swallow one, and held to the patterns below by a test rather
#: than trusted, since each of those spells its own sigil.
SIGILS = "#@!~+"

#: One word of a phrase that may run on — and that stops short of a sigil (`#2490`). A repeat's
#: optional tail took *any* next word, so ``every grid on the page +superconductor`` reserved the
#: project with the rest of a phrase nothing could read, and the task was filed into no project
#: with ``+superconductor`` left in its title. A word beginning a field is never part of a date.
_PLAIN_WORD = rf"(?![{re.escape(SIGILS)}])\S+"

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
#: **It is read rather than merely reserved since `#94`, and it stays deliberately greedy.**
#: A qualifier can follow the unit — *every month on the 30th*, *every year on 19 august* —
#: and ``on`` is a planned-day word, so a pattern stopping at the unit would hand "on the 30th"
#: to the date grammar and set a start from inside a phrase that belongs to the repeat. So this
#: takes as much as it plausibly can and :func:`_repeat_in` trims from the right until
#: :mod:`subroutine.domain.recurrence` accepts it — which keeps the grammar in one place rather
#: than being restated here as a second pattern that has to agree.
#: **Both word orders, and the fronted one is not optional.** *On the last Thursday of every
#: month* anchored on ``every`` matched only ``every month``: the rule came out as a plain
#: monthly repeat — the wrong schedule — and *"on the last thursday of"* was left in the title.
#: A wrong date and a mangled title from one line is §6.13 rule 1's exact failure, and it is
#: the phrasing the brief was written in.
_EVERY = re.compile(
	rf"{_STARTS_A_WORD}(?:"
	rf"on\s+the\s+{_PLAIN_WORD}(?:\s+{_PLAIN_WORD})?\s+of\s+every\s+{_PLAIN_WORD}"
	rf"|every\s+(?:other\s+)?(?:\d+\s+)?{_PLAIN_WORD}"
	rf"(?:\s+on\s+(?:the\s+)?{_PLAIN_WORD}(?:\s+{_PLAIN_WORD})?)?"
	rf")",
	re.IGNORECASE,
)

#: How many words a repeat's phrase may be trimmed back by before this gives up on it. Three
#: covers every qualifier the grammar has — ``on the last thursday`` is the longest.
_TRIM = 3

_WEEKDAY_ALTERNATION = "|".join(
	sorted(subroutine.domain.dates.WEEKDAYS, key=len, reverse=True)
)
#: Every keyword but ``now``, which is a phrase only with an offset (`#2827`) - see :data:`_PHRASE`.
_KEYWORD_ALTERNATION = "|".join(
	sorted(
		(keyword for keyword in subroutine.domain.dates.KEYWORDS if keyword != "now"),
		key=len,
		reverse=True,
	)
)
#: Month names, longest first for the same reason the others are — `#1210`. ``september`` has to
#: be offered before ``sep``, or the alternation takes the short branch and leaves ``tember``
#: behind in the title.
_MONTH_ALTERNATION = "|".join(
	sorted(subroutine.domain.dates.MONTHS, key=len, reverse=True)
)

#: One date phrase. Ordered longest-form-first, because Python's alternation takes the
#: first branch that matches rather than the longest.
_PHRASE = (
	r"(?:"
	rf"next\s+(?:{_WEEKDAY_ALTERNATION})"
	#: **``now`` only with an offset** (`#2827`). On its own it names a moment that has passed by
	#: the time anybody reads the item, so no deadline, start or defer a person writes means it -
	#: and English is full of it: *from now on*, *by now*. Read as a date, *from now on* deferred a
	#: line to the moment it was filed, said nothing, and took the words out of the title.
	#: ``now+2h`` goes on working, and so does ``--due now``, which is a field rather than a sentence.
	r"|now(?:[+-]\d+[a-zA-Z]+)+"
	rf"|(?:{_KEYWORD_ALTERNATION})(?:[+-]\d+[a-zA-Z]+)*"
	r"|\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?"
	#: **A written calendar date, both ways round** (`#1210`) — ``by 1 september``,
	#: ``due Sept 1``. Above the bare weekday because neither can match the other, and below
	#: the ISO form because that is the unambiguous one.
	#:
	#: **A day number is required on both sides, which is what keeps a bare month out.** ``by
	#: september`` names no day, and reading it as the first would be inventing one — where
	#: this whole grammar's rule is that an unreadable phrase stays in the title and says so.
	#: It is also what stops *"the September release"* being eaten: there is no preposition in
	#: front of it, and `_PHRASE` is only ever reached through one.
	rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTH_ALTERNATION})"
	rf"|(?:{_MONTH_ALTERNATION})\s+\d{{1,2}}(?:st|nd|rd|th)?"
	#: **A weekday in front of a written date, consumed as one phrase** (`#2116`). *on Friday
	#: 18th September* is ordinary English and both halves name the same day, so reading only
	#: the first left the row dated a week early with the right date still in the title.
	#:
	#: **Above the bare weekday, and that is the whole of what makes it work.** Both alternatives
	#: match at the same position — the phrase begins with the weekday either way — and Python
	#: takes the first that matches, so the longer reading has to be offered first or it is
	#: never reached. Reordering the two below it would change nothing, because neither can
	#: match text beginning with a weekday.
	#:
	#: ``dates.day_named`` is what decides whether the pair agree; a phrase that matches here
	#: and disagrees there is left in the title, exactly as an unreadable one is.
	rf"|(?:{_WEEKDAY_ALTERNATION}),?\s+\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTH_ALTERNATION})"
	rf"|(?:{_WEEKDAY_ALTERNATION}),?\s+(?:{_MONTH_ALTERNATION})\s+\d{{1,2}}(?:st|nd|rd|th)?"
	rf"|(?:{_WEEKDAY_ALTERNATION})"
	r")"
)

#: What a line of whole days is joined by: a closing word, or a dash of any length (`#2687`).
_SPAN_JOINT = rf"(?:\s+(?P<word>{'|'.join(SPAN_WORDS)})\s+|\s*[-\u2013\u2014]\s*)"

_ORDINAL = r"(?:st|nd|rd|th)?"

#: A calendar date written out, either way round, or an ISO day — the two forms that name a day
#: without needing a preposition in front to be believed.
_WRITTEN_DAY = (
	rf"(?:\d{{1,2}}{_ORDINAL}\s+(?:{_MONTH_ALTERNATION})"
	rf"|(?:{_MONTH_ALTERNATION})\s+\d{{1,2}}{_ORDINAL}"
	r"|\d{4}-\d{2}-\d{2}(?![T ]?\d))"
)

#: ``from 2nd October to 12th October``, ``on Monday until Wednesday``, ``from 2 October -
#: 12 October``: an opening word, a date, a joint and a date.
_WORDED_SPAN = re.compile(
	rf"{_STARTS_A_WORD}(?:{'|'.join(SPAN_OPENING_WORDS)})\s+(?P<start>{_PHRASE})"
	rf"{_SPAN_JOINT}(?P<end>{_PHRASE})(?![\w'])",
	re.IGNORECASE,
)

#: ``2-12 October``, ``from 2 to 12 October``, ``October 2-12``: two days of one month with the
#: month written once. **Without an opening word only a dash joins them**, because *pages 2 to
#: 12 October* is not somebody's holiday and *2-12 October* nearly always is.
_DAYS_OF_A_MONTH = re.compile(
	rf"{_STARTS_A_WORD}(?:(?P<opening>{'|'.join(SPAN_OPENING_WORDS)})\s+)?(?:"
	rf"(?P<first>\d{{1,2}}){_ORDINAL}{_SPAN_JOINT}(?P<last>\d{{1,2}}){_ORDINAL}"
	rf"\s+(?P<month>{_MONTH_ALTERNATION})"
	rf"|(?P<month_first>{_MONTH_ALTERNATION})\s+(?P<first_after>\d{{1,2}}){_ORDINAL}"
	rf"{_SPAN_JOINT.replace('(?P<word>', '(?P<word_after>')}(?P<last_after>\d{{1,2}}){_ORDINAL}"
	r")(?![\w'])",
	re.IGNORECASE,
)

#: ``2 October - 12 October``, ``30 September-2 October``: two written dates and a dash. **A
#: weekday or a keyword is not enough here**, so *Standup Monday-Friday* keeps its title; they
#: need an opening word, which is :data:`_WORDED_SPAN`.
_DATES_AND_A_DASH = re.compile(
	rf"{_STARTS_A_WORD}(?P<start>{_WRITTEN_DAY})\s*[-\u2013\u2014]\s*(?P<end>{_WRITTEN_DAY})"
	r"(?![\w'])",
	re.IGNORECASE,
)

#: Every shape a span of days is written in, for :func:`_collect_spans` to read and for
#: :func:`explain` to recognise what it gave back.
_SPANS = (_WORDED_SPAN, _DAYS_OF_A_MONTH, _DATES_AND_A_DASH)

#: A time of day as it is written beside a day in a span: ``09:00``, ``9am``, ``9:30 pm``.
_CLOCK = r"(?:\d{1,2}(?::\d{2})?\s*[ap]m|\d{1,2}:\d{2})"

#: The same clock in its parts, for :func:`_clock_at` to read one out of a span pattern
#: that matched it whole. ``at`` is allowed in front because every caller has a match that
#: may carry it, and stripping it in each of them is the copy this avoids.
_ONE_CLOCK = re.compile(
	r"^(?:at\s+)?(?:(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>[ap]m)"
	r"|(?P<hour24>\d{1,2}):(?P<minute24>\d{2}))$",
	re.IGNORECASE,
)

#: Whether a phrase holds a time of day at all, an ISO day's ``T09:00`` included.
_A_CLOCK = re.compile(rf"(?<![\d:]){_CLOCK}(?!\d)", re.IGNORECASE)

#: A span whose **two days each carry a time** - `#2894`, and Simon's decision of 2026-09-19
#: to hold one back and say so. Two days joined as :data:`_WORDED_SPAN` joins them, with a
#: time beside either. Each is a line the date rules read as a defer, because ``from`` is
#: one: *Workshop from 2 October 09:00 to 12 October 17:00* was hidden until the 2nd with
#: the rest of the line in its title, and nothing said.
#:
#: **One day and two times is read now and is not here** (`#675`): *from Monday 9am to 5pm*
#: is :data:`_CLOCKED_DAY`, and *Standup on Monday at 9am - 10am* is a date the rules read
#: with a range beside it. What is left here is the span this grammar still cannot write -
#: two days, timed at both ends - and it says so in those words.
_CLOCKED_SPAN = re.compile(
	rf"{_STARTS_A_WORD}"
	rf"(?:{'|'.join(SPAN_OPENING_WORDS)})\s+{_PHRASE}(?:\s+(?:at\s+)?{_CLOCK})?"
	rf"{_SPAN_JOINT.replace('(?P<word>', '(?:')}{_PHRASE}(?:\s+(?:at\s+)?{_CLOCK})?"
	rf"(?![\w'])",
	re.IGNORECASE,
)

#: A day and the two times an appointment on it runs between, opened with ``from``:
#: *Workshop from Monday 9am to 5pm* (`#675`). **Read here rather than by the time rules**
#: because ``from`` is a defer, so the words have to be claimed whole before the date rules
#: see them - which is the same reason a span of whole days is read here.
#:
#: ``on`` is absent deliberately: *Standup on Monday 2pm-3pm* is a date this grammar
#: already reads with a range written beside it, and claiming it here would take the
#: reading away from the rule that does it properly.
#:
#: **An ISO day may carry its first time with a ``T``** (`#3157`): *from 2026-10-02T09:00 to
#: 17:00* is the same appointment as *from 2 October 09:00 to 17:00*, and without this it fell
#: to the date rules as a hidden defer with *to 17:00* left in its title - `#2894` again.
_CLOCKED_DAY = re.compile(
	rf"{_STARTS_A_WORD}from\s+(?P<day>{_PHRASE})(?:\s+(?:at\s+)?|(?<=\d)T)(?P<first>{_CLOCK})"
	rf"{_SPAN_JOINT.replace('(?P<word>', '(?:')}(?:at\s+)?(?P<last>{_CLOCK})"
	rf"(?![\w'])",
	re.IGNORECASE,
)

#: Two times of day and the word joining them: ``2pm-3pm``, ``at 9am til 5pm``, ``14:00 to
#: 15:00`` (`#675`). Signalled exactly as a single time is - after ``at``, or straight after
#: a date already read - so a range in prose stays prose, which is what keeps *Email Bob re:
#: 3pm* untouched whether or not somebody writes a second time after it.
_TIME_RANGE = re.compile(
	rf"{_STARTS_A_WORD}(?P<at>at\s+)?(?P<first>{_CLOCK})"
	rf"{_SPAN_JOINT.replace('(?P<word>', '(?:')}(?:at\s+)?(?P<last>{_CLOCK})"
	rf"(?!\d)(?!\w)",
	re.IGNORECASE,
)


#: A date preposition and the phrase after it — ``by friday``, ``due 2026-08-19``.
#:
#: **Both edges are guarded against ``\b`` and neither was** (`#929`). ``\b`` sits between any
#: word character and any non-word character, which is not the same question as *does a word
#: start here*:
#:
#: - At the front it matched **inside** a hyphenated word, so ``Ship the add-on tomorrow`` read
#:   the ``on`` of ``add-on`` as a date preposition and filed a task called ``Ship the add-``.
#:   Every other pattern in this module uses ``_STARTS_A_WORD`` and this one did not.
#: - At the back it matched **before an apostrophe**, so ``Ship it by tomorrow's deadline``
#:   became ``Ship it 's deadline``. That is the defect recorded three lines below as the
#:   reason ``_BARE_DAY`` is anchored — found, written down, and fixed in one pattern of the
#:   two it was true of.
#:
#: Both are §6.13 rule 1's forbidden outcome: a word vanished and no field gained it. The
#: trailing guard refuses a word character *or* an apostrophe, so ``due friday,`` and
#: ``due friday.`` still read, which is what an ordinary sentence looks like.
_DATED = re.compile(
	rf"{_STARTS_A_WORD}(?P<word>{'|'.join((*DEADLINE_WORDS, *PLANNED_WORDS, *DEFER_WORDS))})"
	rf"\s+(?P<phrase>{_PHRASE})(?![\w'])",
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
#: **A range is deliberately not matched here.** ``14:00-15:00`` is an appointment with an
#: end, and :data:`_TIME_RANGE` reads one whole (`#675`) before this pattern is asked. The
#: lookahead is what stops this one taking half of a range the other declined - a bare
#: ``2pm-3pm`` in prose, or a range beside a deadline - which would keep the start and drop
#: the finish in silence, where `#778`'s rule is that the grammar says what it could not use.
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

#: The same punctuation, asserted rather than matched: a sigil ends where its value ends,
#: and whatever sentence punctuation follows belongs to the sentence.
#:
#: **A lookahead because every call site claims ``match.span()``**, and claiming a character
#: is what deletes it from the title. Written as a class *inside* the match until #2261,
#: which kept the full stop out of ``+web.``'s key correctly and took it out of the title
#: too — so ``Ship it #ops. Then rest`` was captured as ``Ship it Then rest``, one sentence
#: with a capital stranded in the middle of it. §6.13's rule is that the grammar does not
#: touch what it did not read, and it had read a tag, not a full stop.
_FOLLOWED = r"(?=[,.;:!?)\]]*(?:\s|$))"

#: Punctuation that attaches to the word before it, so a removal must not leave a space in
#: front of one. A `frozenset` rather than a string because ``"" in ",.;:!?"`` is true, and
#: the empty string is what the end of the line looks like.
_CLOSES_A_CLAUSE = frozenset(",.;:!?")

#: Punctuation that joins one part of a sentence to another, so a cut that took everything on
#: one side of it leaves it holding nothing. ``.!?`` are deliberately absent: a title ending
#: in a full stop is a finished sentence rather than a dangling one.
_JOINS_A_CLAUSE = ",;:"

#: A tag is anything after a ``#`` that is not *entirely* digits, because an all-digit one
#: is a reference to an item (docs/design.md §6.15) and the two share the sigil. So ``Fix issue
#: #12`` keeps its number and gains no tag named "12", while ``#3d-printing`` and ``#2fa``
#: are ordinary tags.
#:
#: The digit test is applied to the match rather than written into the pattern: excluding
#: an all-digit run with a lookahead is possible and unreadable, and the loop has to skip
#: the match *without claiming its span* anyway, so that the text stays in the title for the
#: mention index to find.
_TAG = re.compile(
	rf"{_STARTS_A_WORD}#(?P<value>[^\s#]+?){_TRAILING}{_FOLLOWED}"
)
_ASSIGNEE = re.compile(rf"{_STARTS_A_WORD}@(?P<value>[^\s@]+?){_TRAILING}{_FOLLOWED}")
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
	rf"{_STARTS_A_WORD}!(?P<value>[1-5])(?:/(?P<urgency>[1-5]))?{_FOLLOWED}"
)

#: **An estimate must carry a unit**, so ``~90m`` and ``~2h`` parse and ``~5`` does not.
#: The duration grammar reads a bare number as minutes (§6.4) and that is right there; here
#: it is wrong, because in prose ``~5`` means "about five" — ``Invite ~5 people`` would
#: otherwise become a five-minute task to invite people.
_ESTIMATE = re.compile(
	rf"{_STARTS_A_WORD}~(?P<value>\d+[a-zA-Z][a-zA-Z0-9]*){_FOLLOWED}"
)
#: One project key as a capture line may spell it — ``projects.KEY_PATTERN``, written to accept
#: either case because ``projects.normalize_key`` folds it. Named so the address form below is
#: visibly the key form repeated, rather than a second copy of the same shape.
_KEY = r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*"

#: ``+key`` — which project it goes in. **Hyphens inside, never at an edge** (`#508`), so
#: ``+web-sales`` reads as one key and ``+web.`` still drops the full stop. Without the
#: alternation a hyphenated key parsed as ``+web`` and left ``-sales`` in the title, which is
#: §6.13 rule 1's forbidden outcome: a word may only vanish if a field was set.
#:
#: **And separators between them since decision `#957`**, so ``+substation/dist`` is one
#: address. The same argument as the hyphen, one character along: without it that reads as
#: ``+substation`` with ``/dist`` left in the title — filed in the wrong project, with the
#: evidence sitting in the title where nobody looks. It would at least be *reported*, because
#: `#778` compares what was claimed against what a ``+`` was left holding.
#:
#: **The context is not consulted here and never will be** (`#957` §3, Simon's answer). ``+dist``
#: means the same thing wherever it is typed; a `.subroutine` marker supplies the project when a
#: line names none, and giving that one mechanism a second job would make a misfiled line silent
#: — both readings succeed. Both letter cases are still accepted, because
#: ``projects.normalize_key`` is the one place that decides the stored form.
_PROJECT = re.compile(
	rf"{_STARTS_A_WORD}\+(?P<value>{_KEY}(?:/{_KEY})*){_FOLLOWED}"
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


@dataclasses.dataclass(frozen=True)
class Capture:
	"""What a line of text would become, without having become it yet.

	Returned by :func:`parse` for both the preview endpoint and the create path, so the
	thing a user is shown is by construction the thing that will happen.
	"""

	title: str
	due: datetime.date | str | None = None
	due_is_all_day: bool | None = None
	starts_at: datetime.datetime | datetime.date | str | None = None
	starts_is_all_day: bool | None = None

	#: The last day of a span, from ``from 2 to 12 October`` (`#2687`), or the instant an
	#: appointment ends, from ``at 2pm til 3pm`` (`#675`). One flag describes both ends, as
	#: decision `#1235` §2 has it do: whole days at both, or a time at both, never one of each.
	ends_at: datetime.datetime | datetime.date | None = None
	snooze: datetime.date | str | None = None
	snoozed_is_all_day: bool | None = None

	#: How often this repeats, as a stored ``RRULE``, and the words it was written as (`#94`).
	#: **Parsed here rather than reserved**, which is what `every 14 days` used to be: the span
	#: was claimed so the date grammar could not steal `monday` out of it, and the words were
	#: reported as unread. A phrase this still cannot read is reported exactly as it was.
	recurrence: str | None = None
	recurrence_text: str | None = None
	importance: int | None = None
	urgency: int | None = None
	estimate_minutes: int | None = None

	#: How long the work will take, and the token it was written as (`#1614`) — the same pair
	#: as ``recurrence``/``recurrence_text`` two fields up, for the same reason. ``humanize``
	#: re-spells a duration in the largest units that fit, so ``~40h`` came back as ``~1d 16h``:
	#: a different sentence, and one this grammar cannot read, because ``_ESTIMATE`` takes no
	#: space and would stop at ``1d`` and leave ``16h`` in the title.
	estimate_text: str | None = None
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

	# **Three things ended up here and there were two buckets** (`#929`), and there are more of
	# both now, below. A `+` nobody could parse, a repeat phrased in a way the grammar does not
	# know, **and a time that was read and deliberately given back** — which was being reported as a failed repeat, so
	# `explain capture`'s own worked example, `Email Bob re: 3pm`, answered *"not a repeat this
	# understands"* about a string nobody offered as one.
	#
	# Sorted by asking `_EVERY`, which is the pattern that put the repeat here in the first
	# place, rather than by a second description of what a repeat looks like.
	said = [one for one in unparsed if one.startswith("+")]
	rest = [one for one in unparsed if not one.startswith("+")]
	every = [one for one in rest if _EVERY.match(one)]
	over = [one for one in rest if not _EVERY.match(one)]

	# **A fourth bucket, because the third was about to assert a cause it had not
	# established** (`#2116`). Everything that was not a project and not a repeat used to be
	# called a time and told how to write one — which is exactly the mistake the comment above
	# records for repeats, one token along: *Monday 18th September* is not a time, and
	# advising `at 2pm` about it is a refusal explaining something the reader did not do.
	#
	# **Told apart by asking `dates.day_named`, which is the function that refused it**, rather
	# than by a second description of what a contradiction looks like. It is the same move the
	# `mid` bucket makes with `_repeat_in`, and for the same reason.
	# **A span it would not read, told apart by asking the patterns that found it** (`#2886`).
	# A span has two causes to be left as written - it runs backwards, or it names a day there
	# is not - and was falling through to *timed*, so a writer who typed *12-2 October* was
	# told how to write a time. The patterns are :func:`_collect_spans`'s own, so this is one
	# description of what a span looks like rather than two.
	# **And a span with times of day, told apart the same way** (`#2894`): held back whole, and
	# the reason is that its times are not read yet - not that its days are out of order.
	# **Two of those now, because one of them is read** (`#675`): a phrase naming one day and
	# two times is an appointment this grammar writes, so the only way back here is a day or a
	# pair of times it could not read - a different sentence from *two timed days are not read*.
	clocked = [one for one in over if _CLOCKED_SPAN.fullmatch(one) and _A_CLOCK.search(one)]
	hours = [one for one in over if one not in clocked and _CLOCKED_DAY.fullmatch(one)]
	spans = [
		one for one in over
		if one not in clocked and one not in hours
		and any(pattern.fullmatch(one) for pattern in _SPANS)
	]
	contradicted = [
		one for one in over
		if one not in spans and one not in clocked and one not in hours
		and subroutine.domain.dates.day_named(one, today=datetime.date.min) is None
		and one.partition(" ")[0].rstrip(",").lower() in subroutine.domain.dates.WEEKDAYS
	]
	# **And a range, which is a third thing to be told** (`#675`). A range reaches here only
	# when nothing could hold it - beside a deadline, beside a span already read, or beside a
	# day this grammar does not read - and the time sentence would advise `at`, which is no
	# help for any of the three and wrong for the first two.
	ranges = [
		one for one in over
		if one not in contradicted and one not in spans and one not in clocked
		and one not in hours and _TIME_RANGE.fullmatch(one)
	]
	timed = [
		one for one in over
		if one not in contradicted and one not in spans and one not in clocked
		and one not in hours and one not in ranges
	]

	# **Two reasons a repeat is left as written, told apart by asking the function that
	# decided** (`#1401`). A phrase this grammar cannot read and one it read out of the middle
	# of a sentence are textually identical, so nothing about the token separates them — and
	# offering the *phrase this* hint for a sentence that was never meant as a repeat is a
	# refusal asserting a cause it has not established.
	#
	# **Read back off the token rather than carried in a second field**, which is the rule
	# this module already states about ``unparsed``: a second field would have to be widened
	# through ``clients.base.Captured`` and both transports before either reporting surface
	# could see it. :func:`_repeat_in` is the same function :func:`parse` used, so this is
	# one description of what a repeat looks like rather than two.
	mid = [one for one in every if _repeat_in(one) is not None]
	#: **Only the ones with nothing after them reach here at all** (`#1408`). :func:`parse`
	#: drops an unreadable phrase with words following it before it becomes a token, so this
	#: bucket no longer holds every sentence that happens to contain the word *every*.
	repeats = [one for one in every if _repeat_in(one) is None]

	clauses = []

	if mid:
		clauses.append(
			f"Left as written: {', '.join(mid)} - read as part of the sentence rather than "
			f"as a repeat, because words follow it. Put it at the end to make it one."
		)

	if repeats:
		# **The reason changed when the feature landed** (`#94`). It said *"recurring tasks are
		# not supported yet"*, which was true of every unread token here and is now true of
		# none of them: a repeat this grammar cannot read is a repeat *phrased* in a way it
		# does not know, and pointing at the forms that work is what a reader can act on.
		clauses.append(
			f"Left as written: {', '.join(repeats)} - not a repeat this understands. "
			f"{subroutine.domain.recurrence.PHRASE_HINT}"
		)

	if spans:
		clauses.append(
			f"Left as written: {', '.join(spans)} - a span needs its first day before its "
			f"last, and both of them days there are, so neither was set."
		)

	if clocked:
		clauses.append(
			f"Left as written: {', '.join(clocked)} - a span with a time on each of two days "
			f"is not read yet, so nothing was set. Times on one day are read, as in 'from "
			f"monday 9am to 5pm', and a span of whole days is read without them."
		)

	if hours:
		clauses.append(
			f"Left as written: {', '.join(hours)} - an appointment on one day needs a day this "
			f"understands and two different times, as in 'from monday 9am to 5pm', so nothing "
			f"here was set."
		)

	if ranges:
		clauses.append(
			f"Left as written: {', '.join(ranges)} - two times are an appointment's two ends, "
			f"so they go on a start and need a day this understands. A deadline and a deferral "
			f"are each one moment, and take one time rather than two."
		)

	if contradicted:
		# **Names both halves rather than picking one** (`#2116`). The phrase says a weekday
		# and a date and they are different days; this grammar cannot know which the writer
		# meant, and choosing would be the confident wrong answer the fix replaced.
		clauses.append(
			f"Left as written: {', '.join(contradicted)} - the day and the date name "
			f"different days, so neither was used. Write one or the other."
		)

	if timed:
		# **Says what would have made it a date**, because the rule is not guessable from the
		# outcome: a time is read after `at`, or straight after a day that has already been
		# read. Without one it stays in the title, which is `#797`'s decision and is why this
		# is a note rather than a refusal.
		clauses.append(
			f"Left as written: {', '.join(timed)} - a time is read after 'at', or straight "
			f"after a day, as in 'Dentist on Monday at 2pm'."
		)

	if said:
		clauses.append(
			f"Left as written: {', '.join(said)} - a project is named like '+web': letters and "
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
		# **The token, not the duration re-spelled** (`#1614`). This is the one part of the
		# echo that was going through a renderer, and it broke both halves of the promise
		# above: ``~40h`` came back as ``~1d 16h``, which is not what was written and — because
		# the estimate token takes no space — is not something that can be written again.
		# Typing it produces a one-day estimate and a title ending in ``16h``.
		#
		# **``humanize`` behind it, for a caller that built a ``Capture`` by hand.** Nothing in
		# the tree does; a summary is only ever made from a parse. But the alternative is
		# reporting an estimate as unread when one was set, which is the defect this whole
		# function exists to prevent.
		parts.append(
			f"~{capture.estimate_text or subroutine.domain.durations.humanize(capture.estimate_minutes)}"
		)

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

	# **Recurrence first, because the phrase contains words the date grammar wants.** Claiming
	# the span is what stops `every monday` being read as a planned day and `every month on the
	# 30th` being read as one — and since `#94` the phrase is *read* rather than only reserved,
	# so the words leave the title when they became a rule and stay in it when they did not.
	reserved: list[tuple[int, int]] = []
	#: Where each repeat that *was* read landed, so :func:`_mid_sentence` can ask afterwards
	#: whether anything unclaimed follows it. Recorded here because this is the only place
	#: those spans exist, and a second list built later would be a second copy to keep in step.
	repeated: list[tuple[int, int]] = []
	#: Where each repeat this grammar could **not** read landed, for the same question asked of
	#: the readable ones and answered in the same place. Recorded rather than reported here
	#: because *what follows it* is not knowable until every other rule has taken what it wants.
	unread: list[tuple[int, int]] = []

	for match in _EVERY.finditer(text):
		read = _repeat_in(match.group(0))

		if read is None:
			# Reserved now, and reported below only where nothing unclaimed follows it
			# (`#1408`). "every fortnight" is not a rule this knows and inventing one is what
			# §6.13 rule 1 forbids, so the words stay in the title either way.
			unread.append(match.span())
			reserved.append(match.span())

			continue

		rule, words = read

		if "recurrence" not in fields:
			fields["recurrence"] = rule
			fields["recurrence_text"] = words

		# **Only the words that were used**, so a trailing phrase this did not read stays in
		# the title and can still be claimed by another rule — `every 14 days by friday` keeps
		# its deadline.
		start, _end = match.span()
		claimed.append((start, start + len(words)))
		repeated.append((start, start + len(words)))

	# **Where each date landed, and which field it set** (`#2855`), so a time can be recognised
	# as belonging to one date and then be put on that date's field. Each rule writes it on the
	# line where it claims the span, the one moment both facts are in hand. This was a slice of
	# `claimed`, which knew where the dates were and not what any of them had set.
	placed: dict[tuple[int, int], str] = {}

	_collect_spans(
		text, claimed, reserved, fields, unparsed, placed, today=today, now=now, timezone=timezone
	)

	deadline = _collect_dates(
		text, claimed, reserved, fields, unparsed, placed, today=today, now=now, timezone=timezone
	)

	_collect_sigils(text, claimed, reserved, fields, tags)

	# **Before the bare day, and that ordering is the whole fix** (`#797`). `_collect_bare_days`
	# searches the line with claimed spans blanked out, so a time claimed here turns
	# `Solar eclipse today at 18:30` into `Solar eclipse today` for its purposes — and the
	# end-anchor that makes `today` mean something, which is deliberate and well argued, needs
	# no change at all. Reading the time was the missing half; the anchor was never the defect.
	clock = _collect_times(text, claimed, reserved, unparsed, after=list(placed))

	_collect_bare_days(text, claimed, reserved, fields, placed, today=today)

	# **After every start is read and before a clock is put on anything**: a start can come from
	# a span, a date or a bare day, and the comparison is between two days.
	_counted_from_when_it_begins(fields, deadline, now=now, timezone=timezone)

	used = _apply_time(
		fields,
		None if clock is None else clock.at,
		until=None if clock is None else clock.until,
		beside=None if clock is None else _beside(text, clock.span, placed, claimed),
		today=today,
		unread_day=bool(_UNREAD_DAY.search(_blanked(text, claimed))),
		now=now,
		timezone=timezone,
	)

	# **A time that is read and then not used has to go back into the title** (§6.13 rule 1).
	# Claiming it is what lets the bare day be seen as last, and the decision about where it
	# belongs cannot be taken until after that — so the claim is provisional, and this is where
	# it is either kept or given back. Written after driving `Dentist appointment Monday 14:00`
	# and finding the title had lost `14:00` while no field had gained it, which is precisely
	# the outcome this module exists to make impossible.
	if clock is not None and not used:
		claimed.remove(clock.span)
		unparsed.append(text[clock.span[0]:clock.span[1]])

	# **A repeat is read only where nothing unclaimed follows it** (`#1401`), which is §6.13's
	# existing rule for a bare ``today`` applied to the grammar that shipped after it — see
	# :func:`_mid_sentence`. Run here because *unclaimed* is only knowable once every other
	# rule has taken what it wanted: `every 14 days by friday` keeps both.
	for span in _mid_sentence(text, claimed, repeated):
		claimed.remove(span)
		unparsed.append(text[span[0]:span[1]])

		if fields.get("recurrence_text") == text[span[0]:span[1]]:
			fields.pop("recurrence", None)
			fields.pop("recurrence_text", None)

	# **And a phrase this grammar cannot read at all is reported only there too** (`#1408`).
	# The advice differs from the rule above precisely because the phrase does not parse: for
	# a readable repeat *"put it at the end to make it one"* is true and actionable, and for
	# an unreadable one it is false — putting `every fortnight` at the end will not make a
	# repeat either. So the two signals that somebody meant a rule are its shape and its
	# position, and where both are absent there is nothing to report: nothing was taken,
	# nothing was changed, and every word is still in the title.
	#
	# **Asked after the loop above, against the settled ``claimed``**, because a repeat given
	# back a moment ago is part of the sentence now and this has to see it that way.
	settled = _blanked(text, claimed)

	unparsed.extend(
		text[start:end] for start, end in unread if _nothing_follows(settled, (start, end))
	)

	# **A `+` nobody claimed** (`#778`). This runs last because it asks what the rules above
	# took: `_PROJECT` claims the span it read, so anything still unclaimed is a project name
	# the grammar could not parse — `+web_sales`, whose underscore is in no key, or `+dist/`,
	# which names a place inside something and then does not say what.
	#
	# **The worked example here used to be `+subroutine/UI`**, which the pattern could not
	# reach past. It is an address now (decision `#957`) and is read in full; what this
	# reports is the shape the *next* widening will leave behind, which is what it is for.
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
	unparsed: list[str],
	placed: dict[tuple[int, int], str],
	*,
	today: datetime.date,
	now: datetime.datetime,
	timezone: str,
) -> str | None:
	"""Consume ``before Sunday``-style phrases, first one per field winning.

	Returns the words that set the deadline, if one was set, so it can be read again from a
	start this line names anywhere in it (:func:`_counted_from_when_it_begins`). Records in
	``placed`` which field each phrase it claimed set, for a time written beside it (`#2855`).
	"""

	deadline: str | None = None

	for match in _DATED.finditer(text):
		if _overlaps(match.span(), claimed) or _overlaps(match.span(), reserved):
			continue

		word = match.group("word").lower()
		phrase = match.group("phrase")
		value, all_day = _read_phrase(phrase, today=today, now=now, timezone=timezone)

		if value is None:
			# **Read and then not used, so it is reported** (`#778`, `#2116`) — the same rule
			# and the same shape as a time given back to the title a few lines below in
			# :func:`parse`. Today this can only be a weekday contradicting a date in one
			# phrase: every other unreadable phrase is handed on rather than refused here.
			#
			# **The phrase rather than the whole match**, because the preposition is not what
			# failed: *on* was understood perfectly and `Friday 18th September` is the part
			# that says two different days.
			unparsed.append(phrase)

			continue

		if word in PLANNED_WORDS:
			if "starts_at" in fields:
				continue

			# Kept as a bare day so ``_apply_time`` can put a clock on it — ``on monday at
			# 2pm`` is one fact written in two tokens, and reading the day to an instant here
			# would leave that function combining a time with something already resolved.
			fields["starts_at"] = _as_date(value, now=now, timezone=timezone)
			fields["starts_is_all_day"] = True
			placed[match.span()] = "starts_at"

		elif word in DEADLINE_WORDS:
			if "due" in fields:
				continue

			fields["due"], fields["due_is_all_day"] = value, all_day
			deadline = phrase
			placed[match.span()] = "due"

		else:
			if "snooze" in fields:
				continue

			fields["snooze"], fields["snoozed_is_all_day"] = value, all_day
			placed[match.span()] = "snooze"

		claimed.append(match.span())

	return deadline


def _counted_from_when_it_begins (
	fields: dict[str, typing.Any], phrase: str | None, *, now: datetime.datetime, timezone: str
) -> None:
	"""Count a deadline from the day the line begins on, where from today it would come first.

	**Each written date means the soonest such date counting today**, so a line naming two read
	them apart (`#1239`): *on 20 July by 5 August*, said on 30 July, started next July and was due
	this August, eleven months before its own start, and nothing said so. A span's end has been
	counted from its start since `#2687`, and this is that rule for a deadline, Simon's of
	2026-09-17. **Where the words sit makes no difference**: *by 5 August on 20 July* is the
	same line.

	**Only where the deadline would otherwise come first.** For a weekday or a written date that
	is no restriction - the soonest Friday from the start and from today are one day whenever
	today's is not the earlier - but it keeps a start already past from pulling a deadline back
	into the past with it, and *next friday* said from today where it already follows the start.

	**A deadline that is not a search is left as written.** *Tomorrow*, an ISO date and an
	expression each name one day whatever sits beside them, so reading one again gives the day it
	gave, and one before its start is what the writer said: overdue work planned for next week is
	ordinary. So is *friday 31 july* beside a start in October, since 31 July next year is not
	a Friday - the weekday pins the date, and the phrase no longer reads from there.

	**A defer is the day it begins on where there is no start** (`#2854`, Simon 2026-09-20).
	``from`` is the other word that says when an item begins - it begins to be *visible* - and
	the same two dates read apart the same way: *from 15 September by 30 September*, said on
	the 17th, was hidden for a year while going overdue in a fortnight, and *from sunday by
	friday* was due before it could be seen. Neither is anything a writer means.

	**A start wins where a line names both**, because a deadline belongs to the work rather
	than to the hiding: *on monday from friday by sunday* is work that starts on Monday, and
	what the deadline follows is the start.

	**Days are compared, however the beginning is held** (the cold review of 2026-09-21,
	`#3136` and `#3157`). An appointment read by :func:`_collect_spans` begins at an instant,
	and an ISO defer is still the string it was written as. Comparing the first with a date
	raised - ``datetime`` is a ``date``, so no ``isinstance`` guard could see it - and filing
	*Workshop from Monday 9am to 5pm by friday* was a 500. The second was left out, so *from
	2026-10-01 by friday* was due before anybody could see it.
	"""

	begins = _the_day_of(
		fields["starts_at"] if fields.get("starts_at") is not None else fields.get("snooze"),
		now=now,
		timezone=timezone,
	)
	due = fields.get("due")

	# **A deadline written as an ISO time is left as it was written**, as the docstring says of
	# every deadline that is not a search: it is still its string here, because `parse` puts
	# times on anything only after this runs.
	ends = _the_day_of(due, now=now, timezone=timezone) if isinstance(due, datetime.date) else None

	if phrase is None or begins is None or ends is None or ends >= begins:
		return

	value, all_day = _read_phrase(phrase, today=begins, now=now, timezone=timezone)

	if isinstance(value, datetime.date):
		fields["due"], fields["due_is_all_day"] = value, all_day


def _the_day_of (
	value: typing.Any, *, now: datetime.datetime, timezone: str
) -> datetime.date | None:
	"""Return the calendar day a start, a defer or a deadline falls on, or ``None``.

	**Whatever it is held as at this point in** :func:`parse`: a day, the instant an
	appointment begins (`#675`), or the string of an ISO date or time, which is resolved where
	the writer is. ``None`` is a value that names no day, and a caller comparing days has
	nothing to compare.
	"""

	if isinstance(value, datetime.datetime):
		if value.tzinfo is None:
			return value.date()

		return subroutine.domain.schedule.local_date(value, timezone)

	if isinstance(value, datetime.date):
		return value

	if not isinstance(value, str):
		return None

	try:
		named = subroutine.domain.schedule.interpret_written_moment(
			value, timezone=timezone, now=now
		)

	except subroutine.errors.SubroutineError:
		return None

	return _the_day_of(named, now=now, timezone=timezone) if named is not None else None


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
			# picks it up from there (docs/design.md §6.15).
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

		# Kept *after* the parse, so a token that was not an estimate never becomes one.
		fields["estimate_text"] = match.group("value")

		claimed.append(match.span())


class _Clock(typing.NamedTuple):
	"""A time of day a line named, the end of it where a range was written, and where it sits."""

	#: When it starts.
	at: datetime.time

	#: When it ends, from ``at 2pm til 3pm`` (`#675`), or ``None`` where one time was written.
	until: datetime.time | None

	#: Where the words are, so the claim can be given back if nothing can hold them.
	span: tuple[int, int]


def _clock_at (written: str) -> datetime.time | None:
	"""Return the time of day a written clock names, or ``None`` where it names none.

	One reading for every rule that has to turn ``9am``, ``09:00`` or ``9:30 pm`` into a time:
	:func:`_collect_times` had it inline and :func:`_collect_spans` would have been a second
	copy, which is this codebase's signature defect written small.

	**``12am`` is midnight and ``12pm`` is noon**, which is the one place a modulus is needed
	rather than an addition - ``12 + 12`` is 24 and there is no such hour.
	"""

	matched = _ONE_CLOCK.match(written.strip())

	if matched is None:
		return None

	hour = int(matched.group("hour") or matched.group("hour24"))
	minute = int(matched.group("minute") or matched.group("minute24") or 0)
	meridiem = (matched.group("meridiem") or "").lower()

	if meridiem:
		hour = hour % 12 + (12 if meridiem == "pm" else 0)

	if not (0 <= hour <= 23 and 0 <= minute <= 59):
		return None

	return datetime.time(hour=hour, minute=minute)


def _range_of (first: str, last: str) -> tuple[datetime.time, datetime.time] | None:
	"""Return the two times a range names, or ``None`` where it names none to be sure of.

	**One reading for both places a range is written** - beside a date (:func:`_collect_times`)
	and inside *from Monday 9am to 5pm* (:func:`_clocked_day`) - for :func:`_clock_at`'s
	reason: two copies of one rule are this codebase's signature defect.

	**A meridiem written once is read at both ends where it fits** (the cold review of
	2026-09-21, `#3138`). *7:30-9:30pm* is how an evening is ordinarily written, and reading
	each end alone stored it as 07:30 to 21:30, fourteen hours, without a word. So an end
	written with none takes the other's where that keeps the start before the end:
	*11:00-1:00pm* keeps its 11:00, since 23:00 would come after the end.

	**An end earlier than its start is the next morning only where the line says which clock
	it is on** (Simon, 2026-09-20: *9pm til 1am*): a meridiem on the start, or a start written
	as a twelve-hour clock never would be - a leading zero, or an hour past twelve. *22:00-1:30*
	is plainly the small hours. *12:30-1:30* is lunch to nearly everybody and a thirteen-hour
	one on a twenty-four-hour clock, and nothing on the line says which, so it is not read: the
	line does what it did before `#675` read ranges, and the words are reported (§6.13 rule 1).

	**An end equal to its start is not a range** either, and ``None`` gives it the same
	fallback.
	"""

	one = _ONE_CLOCK.match(first.strip())
	other = _ONE_CLOCK.match(last.strip())
	at = _clock_at(first)
	until = _clock_at(last)

	if one is None or other is None or at is None or until is None:
		return None

	said = (one.group("meridiem") or "").lower()
	said_after = (other.group("meridiem") or "").lower()

	if said_after and _on_either_clock(one):
		carried = _clock_at(f"{one.group('hour24')}:{one.group('minute24')}{said_after}")

		if carried is not None and carried < until:
			at = carried

	if said and _on_either_clock(other):
		carried = _clock_at(f"{other.group('hour24')}:{other.group('minute24')}{said}")

		if carried is not None and carried > at:
			until = carried

	if at == until or (until < at and _on_either_clock(one)):
		return None

	return at, until


def _on_either_clock (written: re.Match[str]) -> bool:
	"""Say whether a clock with no meridiem reads the same on a twelve-hour clock - `#3138`.

	``7:30`` does and ``07:30`` and ``19:30`` do not: nobody writing a twelve-hour clock puts
	a zero in front of the hour or counts past twelve.
	"""

	hour = written.group("hour24")

	return hour is not None and not hour.startswith("0") and 1 <= int(hour) <= 12


def _signalled (
	text: str, match: re.Match[str], *, after: typing.Sequence[tuple[int, int]]
) -> bool:
	"""Say whether a time was written where this grammar reads one (`#797`).

	**Signalled, or attached to a date already read.** Without one of the two this is a bare
	number in prose - *Email Bob re: 3pm* - and reading it is exactly the guessing the closed
	date vocabulary exists to refuse. One answer for a single time and for a range (`#675`),
	because a range written in prose is prose as much as one time is.

	**Attached means nothing between them, on whichever side the date is** (the cold review of
	2026-09-21, `#3138`). This sliced from the date's end to the time's start, which is empty
	when the date comes *after* the time - so *Summarise the 2pm-3pm call by friday* counted a
	deadline four words on as the range's signal, and invented an appointment today.
	"""

	return match.group("at") is not None or any(
		(end <= match.start() and not text[end:match.start()].strip())
		or (match.end() <= start and not text[match.end():start].strip())
		for start, end in after
	)


def _collect_times (
	text: str,
	claimed: list[tuple[int, int]],
	reserved: list[tuple[int, int]],
	unparsed: list[str],
	*,
	after: list[tuple[int, int]],
) -> _Clock | None:
	"""Consume a time of day or a range of two, and report anything time-shaped left over.

	**A range is read first and whole** (`#675`), because every one of its halves is a time
	this would otherwise read on its own: *at 2pm til 3pm* would become a 2pm start with the
	end dropped into the title, which is the silent half-reading §6.13 rule 1 forbids.

	**The first readable one wins**, matching every other field here: a second range, or a
	third time, is reported rather than read.

	Returns the times rather than writing a field, because where they belong depends on what
	the *rest* of the line said and the bare day has not been read yet. :func:`_apply_time`
	decides, and refuses where nothing can hold a range.
	"""

	found: _Clock | None = None

	for match in _TIME_RANGE.finditer(text):
		if _overlaps(match.span(), claimed) or _overlaps(match.span(), reserved):
			continue

		if found is not None or not _signalled(text, match, after=after):
			continue

		# **A range this cannot be sure of falls back to what the line did before this rule
		# existed**: the start is read where one time would be, and the rest is reported. An
		# end equal to its start is one - read as a span it would be a zero-length appointment
		# or, counted backwards, a whole day - and :func:`_range_of` names the others.
		read = _range_of(match.group("first"), match.group("last"))

		if read is None:
			continue

		found = _Clock(at=read[0], until=read[1], span=match.span())

		claimed.append(match.span())

	for match in _TIME.finditer(text):
		if _overlaps(match.span(), claimed) or _overlaps(match.span(), reserved):
			continue

		if found is not None or not _signalled(text, match, after=after):
			continue

		at = _clock_at(match.group(0))

		if at is None:
			continue

		found = _Clock(at=at, until=None, span=match.span())

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


def _named_day (value: typing.Any, *, now: datetime.datetime, timezone: str) -> datetime.date | None:
	"""Return the day a still-unresolved date value names, or ``None`` if it names more.

	**Only a value that names a day and no clock**, so a literal ``2026-08-20T17:00`` — which
	has said its own time — comes back ``None`` and is left alone. Anything the grammar cannot
	read at all does too, rather than raising: this is being asked *can a time go here*, and
	*no* is a complete answer to that.
	"""

	if not isinstance(value, str):
		return None

	try:
		named = subroutine.domain.schedule.interpret_written_moment(
			value, timezone=timezone, now=now
		)

	except subroutine.errors.SubroutineError:
		return None

	if isinstance(named, datetime.datetime) or not isinstance(named, datetime.date):
		return None

	return named


def _beside (
	text: str,
	span: tuple[int, int],
	placed: typing.Mapping[tuple[int, int], str],
	claimed: typing.Sequence[tuple[int, int]],
) -> str | None:
	"""Return the field of the date a time was written beside, or ``None`` - `#2855`.

	**The date it follows, and otherwise the one it comes before.** *On monday at 2pm* and *at
	2pm on monday* are one fact written in two orders. In *on monday at 2pm by friday* the time
	sits between two dates and belongs to the one it follows, because that is how a time is
	written onto a day, and ``by friday 17:00`` has always been read that way.

	**Beside means nothing between them once everything claimed is blanked**, as a bare day is
	found last (:func:`_collect_bare_days`): a tag between a date and its time does not part
	them, and a word does. The nearest date wins on each side, since a date between two others
	is blanked too.

	``None`` is a time beside no date, and :func:`_apply_time` keeps its own order for that.
	"""

	start, end = span
	blank = _blanked(text, claimed)

	followed = [
		(date_end, field)
		for (_date_start, date_end), field in placed.items()
		if date_end <= start and not blank[date_end:start].strip()
	]

	if followed:
		return max(followed)[1]

	preceded = [
		(date_start, field)
		for (date_start, _date_end), field in placed.items()
		if date_start >= end and not blank[end:date_start].strip()
	]

	return min(preceded)[1] if preceded else None


def _written_on (
	fields: dict[str, typing.Any],
	field: str,
	flag: str,
	day: datetime.date,
	*,
	at: datetime.time,
	until: datetime.time | None,
) -> bool:
	"""Write a time, and the end of a range where one was written, onto a day - `#675`.

	**An end earlier than its start is the next morning** (Simon, 2026-09-20). On one named
	day *at 9pm til 1am* can mean nothing else, and an evening that runs past midnight is the
	ordinary case rather than the exotic one. An end *equal* to its start names no span at
	all, and :func:`_collect_times` declines that before it reaches here.
	"""

	fields[field] = datetime.datetime.combine(day, at)
	fields[flag] = False

	if until is not None:
		ending = datetime.datetime.combine(day, until)
		fields["ends_at"] = ending if until > at else ending + datetime.timedelta(days=1)

	return True


def _apply_time (
	fields: dict[str, typing.Any],
	at: datetime.time | None,
	*,
	until: datetime.time | None = None,
	beside: str | None,
	today: datetime.date,
	unread_day: bool,
	now: datetime.datetime,
	timezone: str,
) -> bool:
	"""Attach a time of day to whichever date the line established, or to today.

	**A preposition wins, because the writer said which field they meant.** ``due today at
	17:00`` is a deadline with a time; ``from friday 09:00`` is a defer with one.

	**And the date the time was written beside decides between two** (`#2855`). This tried the
	deadline, then the defer, then the start, and put the clock on the first holding a day, so
	*Dentist on monday at 2pm by friday* moved the appointment's time onto the deadline and the
	start lost it, silently, since both dates were still printed. ``beside`` is
	:func:`_beside`'s answer. Where that date cannot take a clock, being a span of days or an
	instant already written, the time goes back into the title rather than onto another date.
	The order below is kept for a time beside no date: *Call Bob at 3pm about it by friday*.

	**A bare day plus a time is simply a start with a time on it** (`#854`). It used to be a
	*defer*: ``starts_at`` was a date that could not hold a clock, so the day was popped off
	and rewritten into the only column that could — which happened to be the one that **hides
	the row**. ``Dentist on Monday at 2pm`` therefore vanished from the list until two o'clock
	on Monday. Now the time lands on the field the preposition already chose, and nothing
	moves between columns.

	**With no day at all the time is today's**, never tomorrow's. A start already past is
	harmless — it is a thing that has begun — where guessing forward invents a date the
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

	Left alone where the field already carries an instant: a literal ``2026-08-20T17:00`` has
	said its own time.

	**A value still held as a string is asked what it names** (`#1239`), which it was not, and
	the cost was the sharpest kind of silence. A weekday, ``today`` and ``tomorrow`` are all
	resolved to a day by the time this runs, so the clock landed on them — but an **ISO date
	stays a string**, so ``by 2026-09-02 at 17:00`` fell past every field and invented a
	``starts_at`` of *today at 17:00*, a date the writer never gave, on a line whose only date
	was a deadline. Measured beside ``by 2026-09-02 17:00``, which was and is correct: the same
	sentence written two ways gave two different answers, and the wrong one is the one with the
	word *at* in it.

	**And a line that established a date is never given an invented one** — the second half,
	and the one that closes the shape rather than the instance. Falling through to *today*
	is right for ``Dentist at 3pm``, which named no day at all; it is never right where a day
	was named and the clock simply could not be attached to it. The time goes back into the
	title instead, which is what the caller does with a ``False`` and is §6.13 rule 1's answer.
	"""

	if at is None:
		return False

	# **A range belongs to a start and to nothing else** (`#675`). A deadline and a defer are
	# each one instant - *by friday 17:00* is the moment it is late, not an hour of lateness -
	# so a line putting a range on one is not read at all rather than read halfway. The words
	# go back into the title and are said, which is what ``False`` means to the caller.
	if until is not None and beside is not None and beside != "starts_at":
		return False

	named_a_day = False

	for field, flag in (
		("due", "due_is_all_day"),
		("snooze", "snoozed_is_all_day"),
		("starts_at", "starts_is_all_day"),
	):
		if beside is not None and field != beside:
			continue

		value = fields.get(field)

		if value is not None:
			named_a_day = True

		# **Asked after the line is known to have named a day, not before** (the cold review of
		# 2026-09-21, `#3138`). A range skips the deadline and the defer, and skipping them
		# first left ``named_a_day`` false, so *Call Bob at 2pm-3pm about it from friday* fell
		# through to today and made an appointment beside a defer to Friday.
		if until is not None and field != "starts_at":
			continue

		# **A span already read takes no clock** (`#2687`). A span of whole days has one flag
		# describing both of its ends (decision `#1235` §2), so a time on the start alone would
		# make it say two things; a span already timed at both ends has been given its times.
		# Either way the time goes back into the title and is said.
		if field == "starts_at" and "ends_at" in fields:
			continue

		if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
			return _written_on(fields, field, flag, value, at=at, until=until)

		day = _named_day(value, now=now, timezone=timezone)

		if day is not None:
			return _written_on(fields, field, flag, day, at=at, until=until)

	if unread_day or named_a_day:
		return False

	return _written_on(fields, "starts_at", "starts_is_all_day", today, at=at, until=until)


def _collect_spans (
	text: str,
	claimed: list[tuple[int, int]],
	reserved: list[tuple[int, int]],
	fields: dict[str, typing.Any],
	unparsed: list[str],
	placed: dict[tuple[int, int], str],
	*,
	today: datetime.date,
	now: datetime.datetime,
	timezone: str,
) -> None:
	"""Read the first span of whole days in a line as a start and an end — `#2687`.

	**Before the dates, because both of its dates would otherwise be read on their own**, and
	wrongly: the first as a defer, since ``from`` is one, and the second not at all.

	**The end is resolved from the start, never on its own.** Each written date means the
	soonest such date counting today, so *from 20 July to 5 August*, said on 30 July, would
	start next July and end this August - `#1239`'s defect, a start and an end resolving
	independently, in a new field. Read from the start, it ends the August after.

	**A span that cannot be read is said, and nothing else may take it.** A range running
	backwards, or a day its month has not got, stays in the title and is reported - and it is
	held back from the date rules too, or ``from`` would quietly become the defer the writer
	was trying not to set.

	**One day and two times is an appointment, and is read** (`#675`): *Workshop from Monday
	9am to 5pm* is a start and an end on that day. It is read here, rather than by the time
	rules that read *Standup on Monday 2pm-3pm*, because ``from`` is a defer and the words
	have to be claimed before the date rules see them.

	**A span whose two days each carry a time is still held back and said** (`#2894`). Its
	ends are two different days, which is the span this grammar cannot write yet, and left to
	the rules it met before it became a defer that hid the item.
	"""

	# **Found by their own patterns, and ahead of a span of days starting at the same place**,
	# because the days' patterns read the ISO form of one as days and would drop its times.
	timed = list(_CLOCKED_DAY.finditer(text))
	clocked = [match for match in _CLOCKED_SPAN.finditer(text) if _A_CLOCK.search(match.group(0))]
	found = sorted(
		(*timed, *clocked, *(match for pattern in _SPANS for match in pattern.finditer(text))),
		key=lambda match: (match.start(), match not in timed, match not in clocked),
	)

	for match in found:
		if _overlaps(match.span(), claimed) or _overlaps(match.span(), reserved):
			continue

		if match in timed:
			hours = _clocked_day(match.groupdict(), today=today, now=now, timezone=timezone)

			# Kept whole and reported where it cannot be read, for the reason below: a day this
			# grammar does not read, or two times that name no span, must not leave ``from``
			# behind to become the defer the writer was not asking for.
			if hours is None:
				reserved.append(match.span())
				unparsed.append(match.group(0).strip())

				return

			fields["starts_at"], fields["ends_at"] = hours
			fields["starts_is_all_day"] = False
			claimed.append(match.span())
			placed[match.span()] = "starts_at"

			return

		# Kept whole and reported, as a span this cannot read is below - `#2894`. Its two days
		# cannot both carry a time yet, and the date rules would have made its start a defer.
		if match in clocked:
			reserved.append(match.span())
			unparsed.append(match.group(0).strip())

			return

		groups = match.groupdict()

		# **A worded joint needs an opening word**: only a dash stands on its own.
		if groups.get("first") is not None or groups.get("first_after") is not None:
			worded = groups.get("word") or groups.get("word_after")

			if worded is not None and groups.get("opening") is None:
				continue

		days = _span_days(groups, today=today, now=now, timezone=timezone)

		if days is None:
			reserved.append(match.span())
			unparsed.append(match.group(0).strip())

			return

		fields["starts_at"], fields["ends_at"] = days
		fields["starts_is_all_day"] = True
		claimed.append(match.span())
		placed[match.span()] = "starts_at"

		return


def _clocked_day (
	groups: dict[str, str | None],
	*,
	today: datetime.date,
	now: datetime.datetime,
	timezone: str,
) -> tuple[datetime.datetime, datetime.datetime] | None:
	"""Return the two instants *from Monday 9am to 5pm* names, or ``None`` - `#675`.

	**A side it cannot read makes the whole phrase unreadable**, as a span of days does and
	for the same reason: the writer wrote one thing, and reading half of it would set a field
	the line did not say while leaving ``from`` to hide the item.

	The end takes the start's day, and the next one where it is earlier, which is
	:func:`_written_on`'s rule read from the other end of the grammar.
	"""

	day = _span_day(groups.get("day") or "", today=today, now=now, timezone=timezone)
	read = _range_of(groups.get("first") or "", groups.get("last") or "")

	if day is None or read is None:
		return None

	at, until = read
	starting = datetime.datetime.combine(day, at)
	ending = datetime.datetime.combine(day, until)

	return starting, ending if until > at else ending + datetime.timedelta(days=1)


def _span_days (
	groups: dict[str, str | None],
	*,
	today: datetime.date,
	now: datetime.datetime,
	timezone: str,
) -> tuple[datetime.date, datetime.date] | None:
	"""Return the first and last day a span names, or ``None`` where it cannot be read as one.

	**A side it cannot read makes the whole span unreadable**, rather than handing that side to
	the date rules: both sides matched a date's shape, so the writer wrote a span, and reading
	half of it would set a field the line did not say.

	**Whether the end's year was written or counted is asked of the end itself**, by reading
	it again from well before the start: a written year answers the same, a counted one does
	not. :func:`_a_real_span` needs to know, because only a counted year can have been
	rolled forward to hide a backwards span.
	"""

	if groups.get("first") is not None or groups.get("first_after") is not None:
		month = groups.get("month") or groups.get("month_first") or ""
		first = int(groups.get("first") or groups.get("first_after") or 0)
		last = int(groups.get("last") or groups.get("last_after") or 0)

		if first > last:
			return None

		start = subroutine.domain.dates.written_date(f"{first} {month}", today=today)

		if start is None:
			return None

		end = subroutine.domain.dates.written_date(f"{last} {month}", today=start)

		# Neither side of this form can carry a year, so the end's is always counted.
		if end is None or not _a_real_span(start, end, counted=True):
			return None

		return start, end

	start = _span_day(groups.get("start") or "", today=today, now=now, timezone=timezone)

	if start is None:
		return None

	phrase = groups.get("end") or ""
	end = _span_day(phrase, today=start, now=now, timezone=timezone)

	if end is None:
		return None

	# **No further back than the calendar goes** (`#3157`): *from 0005-01-02 to 0005-01-05* is
	# absurd and typeable, and nine years before it raised `OverflowError` - a 500 on capture.
	# Clamped rather than refused, since a year written out answers the same from anywhere.
	reach = min(_FAR_ENOUGH_BACK, (start - datetime.date.min).days)
	earlier = start - datetime.timedelta(days=reach)
	counted = _span_day(phrase, today=earlier, now=now, timezone=timezone) != end

	return (start, end) if _a_real_span(start, end, counted=counted) else None


#: **Far enough back that any counted date answers differently from the start**, which is
#: what :func:`_span_days` asks: the soonest such day counting from here is always an earlier
#: one than the soonest counting from the start. **Nine years rather than one, because of a
#: 29 February**: counted from a year back it can still find the same leap day the start did,
#: which read a counted year as a written one - measured, on the first run of this - and two
#: leap days can be eight years apart across a century that is not a leap year.
_FAR_ENOUGH_BACK = 366 * 9


def _a_real_span (start: datetime.date, end: datetime.date, *, counted: bool) -> bool:
	"""Say whether ``start`` to ``end`` is a span somebody meant, rather than a slip - `#2884`.

	**Resolving the end from the start is right, and it hides two mistakes.** It is right
	because *from 20 July to 5 August*, said on 30 July, starts next July and has to end the
	August after (`#1239`). But the same counting makes a backwards span impossible to see:
	*from 12 October to 2 October* finds the 2 October after the 12th, **eleven months on**,
	so ``end < start`` can never be true of it. And a day that does not come round within a
	year of the start - a 29 February - is found in the next leap year instead.

	**So a counted end is refused where it could only be a slip**: in the start's own month a
	year later, which is a span running backwards within one month, or past the start's own
	anniversary, which is a day the months between them have not got. A year-end
	*28 December to 3 January* passes, and so does a long *1 September to 30 June*.

	**A written year is taken as written**, since *2026-10-12 to 2027-10-02* is a year the
	writer chose and nothing here can know better. The review's own first remedy - read the
	end from today as well, and refuse it before the start - was measured against `#1239`'s
	case and would have refused it, which is why the rule is about the counted year instead.
	"""

	if end < start:
		return False

	if not counted:
		return True

	if end.year > start.year and end.month == start.month:
		return False

	return end < _anniversary(start)


def _anniversary (day: datetime.date) -> datetime.date:
	"""Return the same day a year later, with a 29 February kept to the last day of February."""

	try:
		return day.replace(year=day.year + 1)

	except ValueError:
		return day.replace(year=day.year + 1, day=28)


def _span_day (
	phrase: str, *, today: datetime.date, now: datetime.datetime, timezone: str
) -> datetime.date | None:
	"""Return the calendar day one side of a span names, counting from ``today``, or ``None``.

	**``None`` for a day that does not exist, never a refusal** (`#2883`). Every reader in this
	module answers ``None`` for *leave the words in the title*, and :func:`_collect_spans`
	promises exactly that for *a day its month has not got*. ``interpret_day`` refuses such a
	day instead, so *from 1 April to 31 April* refused the whole capture - the product's first
	way in, closed by a typo - where the numeric form beside it was reported.
	"""

	value, _all_day = _read_phrase(phrase, today=today, now=now, timezone=timezone)

	if value is None or isinstance(value, datetime.date):
		return value

	try:
		return subroutine.domain.schedule.interpret_day(value, timezone=timezone, now=now)

	except subroutine.errors.ValidationError:
		return None


def _collect_bare_days (
	text: str,
	claimed: list[tuple[int, int]],
	reserved: list[tuple[int, int]],
	fields: dict[str, typing.Any],
	placed: dict[tuple[int, int], str],
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

	if "starts_at" in fields:
		return

	for match in _BARE_DAY.finditer(_blanked(text, claimed)):
		if _overlaps(match.span(), reserved):
			continue

		offset = 1 if match.group("phrase").lower() == "tomorrow" else 0
		fields["starts_at"] = today + datetime.timedelta(days=offset)
		fields["starts_is_all_day"] = True
		claimed.append(match.span())
		# **The word rather than the match** (`#2855`): the match runs on to the end of the line
		# through everything blanked, so it would sit on top of a time written after the day.
		placed[match.span("phrase")] = "starts_at"

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

	# **A phrase beginning with a weekday is this grammar's own and is never handed on**
	# (`#2116`). A bare weekday always resolves, so the only way to reach here with one in
	# front is the compound form — *Friday 18th September* — with the two halves naming
	# different days. `schedule` would be guessing at that, and the fall-through below exists
	# for §9.3 expressions and ISO values rather than for English somebody wrote.
	#
	# ``None`` sends the whole token back to the title, which is what the caller does with
	# anything it cannot read, so the phrase is reported as unread rather than silently
	# becoming a date nobody named.
	if lowered.partition(" ")[0].rstrip(",") in subroutine.domain.dates.WEEKDAYS:
		return None, None

	# The shared vocabulary, not a copy of it (`#988`). This branch survives the move
	# because it does two things the far end cannot: it matches case-insensitively,
	# so `by Today` reads, and it hands on a `date` rather than a word.
	if lowered in subroutine.domain.dates.WHOLE_DAY_KEYWORDS:
		return subroutine.domain.schedule.local_date(
			subroutine.domain.dates.resolve(lowered, now=now, timezone=timezone), timezone
		), True

	# Everything else — a §9.3 expression or an ISO value — is handed to `schedule`, which
	# already knows how to read both and how to infer all-day from the form.
	return written, None



def _repeat_in (phrase: str) -> tuple[str, str] | None:
	"""Return the longest readable repeat in a claimed phrase, and the words it used.

	**Trimmed from the right rather than matched exactly**, because :data:`_EVERY` is greedy
	on purpose: it has to swallow a following ``on …`` so the date grammar cannot take half of
	one phrase, and what it swallows is sometimes not part of the repeat at all. *Water the
	plants every 14 days on friday* has no meaning as a rule, and the honest answer is the rule
	for ``every 14 days`` with the rest left in the title rather than a refusal of the whole.

	``None`` when nothing in it parses, which is what keeps *every fortnight* reported rather
	than swallowed — the words stay in the title and the writer is told they were not read.
	"""

	words = phrase.split()

	for taken in range(len(words), max(len(words) - _TRIM, 1) - 1, -1):
		candidate = " ".join(words[:taken])

		try:
			read = subroutine.domain.recurrence.phrase(candidate)

		except subroutine.errors.SubroutineError:
			continue

		return read, candidate

	return None


#: What may sit between a repeat and the end of the line without making it mid-sentence.
#: Whitespace, and the punctuation somebody ends a sentence with — the same allowance
#: :data:`_BARE_DAY` makes with ``[.!?]*\s*$``, written as a set here because this checks a
#: slice rather than matching a pattern. A comma is **not** in it: *every day, and bread* has
#: prose after the repeat, which is exactly what this is looking for.
_ENDS_A_LINE = " \t\r\n.!?"


def _nothing_follows (blanked: str, span: tuple[int, int]) -> bool:
	"""Report whether a span is the last unclaimed thing on the line.

	**One description of §6.13's test, because two rules ask it** — :func:`_mid_sentence` of a
	repeat that was read, and :func:`parse` of one that could not be. The two act on opposite
	answers and must not come to disagree about the question.

	``blanked`` is the line with every claimed span already blanked out, taken as an argument
	rather than computed here because a caller asking about several spans would otherwise
	rebuild it once per span.
	"""

	return not blanked[span[1]:].strip(_ENDS_A_LINE)


def _mid_sentence (
	text: str,
	claimed: typing.Sequence[tuple[int, int]],
	repeated: typing.Sequence[tuple[int, int]],
) -> list[tuple[int, int]]:
	"""Return the repeats that were read out of the middle of a sentence — `#1401`.

	**§6.13's rule for a bare day, applied to the grammar that shipped after it.** That rule
	is written down and settled: *a bare ``today``/``tomorrow`` plans only as the last token
	of the line, measured after the sigils are removed* — because *mid-sentence these words
	are almost always prose, and reading one as a field both sets a date nobody asked for and
	takes a word out of the title*. ``every …`` was M7 and did not inherit it, so filing *"A
	view somebody uses every day can be saved and shared"* produced a **daily repeating task
	due today** with the words gone from the title, in two rows because a repeat is two rows
	(`#1247`).

	**Not a narrowing of the grammar.** *Buy milk every day* goes on working, and so does
	every phrase with only claimed text after it: `every 14 days by friday` keeps its
	deadline, `every month +home !3` keeps its sigils, and `Standup every weekday at 9am`
	keeps its time. That is the whole reason this runs last rather than beside the repeat
	pass — *unclaimed* is not knowable until every other rule has taken what it wanted, and a
	check written earlier would have refused all three.

	**Blanking rather than slicing, for :func:`_collect_bare_days`' reason**: every span
	recorded addresses the original text, so the offsets have to survive.

	The asymmetry with a leading *Every day, buy milk* is inherited rather than chosen —
	``_BARE_DAY`` has read *Buy milk tomorrow* and not *Tomorrow buy milk* since the grammar
	existed, and one rule reading both ways would be two rules.
	"""

	blanked = _blanked(text, claimed)

	return [span for span in repeated if not _nothing_follows(blanked, span)]


def _as_date (
	value: datetime.date | str, *, now: datetime.datetime, timezone: str
) -> datetime.date:
	"""Return a phrase's value as a calendar date, before any clock is added to it."""

	if isinstance(value, datetime.date):
		return value

	return subroutine.domain.schedule.interpret_day(value, timezone=timezone, now=now) or now.date()


def _overlaps (span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
	"""Report whether a span collides with one already taken."""

	start, end = span

	return any(start < taken_end and taken_start < end for taken_start, taken_end in spans)


def _remaining (text: str, claimed: list[tuple[int, int]]) -> str:
	"""Return the text with every consumed span removed and the gaps closed up.

	Only whitespace is normalised, and only where a removal left it stranded. Punctuation and
	capitalisation inside what is left are untouched — a title is what the person typed.
	"""

	order = sorted(claimed)

	kept = ""
	cursor = 0

	for start, end in order:
		kept += text[cursor:start]
		cursor = max(cursor, end)

		after = text[cursor:]

		# **A seam, not the sentence** (#2245). The space now sitting at the end of `kept`
		# separated what is left from the phrase just removed; the phrase is gone, so the
		# space is the cut's own leftover and `Ship it by friday, then rest` would otherwise
		# be captured as `Ship it , then rest`.
		#
		# **Only ever the whitespace this cut exposed.** The character after the seam has to
		# be the punctuation *itself*, so a space somebody typed before a comma of their own
		# — `Ship it by friday , then rest` — is on the far side of the seam and survives.
		if after[:1] in _CLOSES_A_CLAUSE:
			kept = kept.rstrip()

		# **And where the cut reached the end of the line, the leftover is the punctuation
		# rather than the space** (#2262): `Buy milk, tomorrow` is a title reading
		# `Buy milk,`. Asked here rather than of the finished title because the tail — a full
		# stop the sentence still needs — has not been put back yet, which is what makes
		# `Buy milk, tomorrow.` come out as `Buy milk.` and not `Buy milk,.`
		if not after.strip(_ENDS_A_LINE):
			kept = kept.rstrip().rstrip(_JOINS_A_CLAUSE)

	title = re.sub(r"\s+", " ", kept + text[cursor:]).strip()

	# The same rule at the other end, and after the loop because that is where the whole of
	# what precedes the first cut is known: `by friday, ship it` is a title reading `, ship
	# it`. Both are guarded on a cut actually having reached that end — a line somebody typed
	# with a trailing comma and nothing in it to read keeps the comma, because §6.13's rule
	# is that the grammar does not touch what it did not read.
	if order and not text[:order[0][0]].strip():
		title = title.lstrip(_JOINS_A_CLAUSE).lstrip()

	return title
