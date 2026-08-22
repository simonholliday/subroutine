"""Turning what a feed shows into the bytes a calendar application reads (RFC 5545).

A pure function from data to a string, deliberately, and for the reason ``markdown.js`` is
one: the whole of what this produces can be fed to a test and compared byte for byte, with
nothing standing between the assertion and the thing being asserted about.

**Three details of the format are easy to get subtly wrong and are not decoration.** Lines
end ``CRLF``; a line longer than 75 octets is *folded* rather than truncated, and it is
octets rather than characters, so an em dash counts three; and four characters have to be
escaped inside a text value. Getting any of them wrong produces a file that most clients
open and one client rejects, which is the worst way to find out.
"""

import datetime
import typing
import uuid

import subroutine.db.models.work
import subroutine.domain.calendars
import subroutine.domain.schedule

#: What this program calls itself in the files it produces. RFC 5545 wants a globally unique
#: identifier for the software; the shape is conventional rather than parsed.
PRODUCT_ID = "-//Subroutine//Calendar Feed//EN"

#: RFC 5545 §3.1: a line is folded when it exceeds 75 **octets**, not characters, and the
#: continuation begins with a single space. Counting characters would leave a line of em
#: dashes three times over the limit while looking correct.
FOLD_AT = 75

#: What a text value has to escape, in this order — the backslash first, or escaping the
#: others would then escape the backslashes this put in.
ESCAPES = (("\\", "\\\\"), (";", "\\;"), (",", "\\,"), ("\n", "\\n"))

#: What each field a task can be dated by is called on the calendar. A deadline says so,
#: because *Pay the rent* on the 30th and *due: Pay the rent* on the 30th are different
#: claims and a calendar showing the first would be asserting you had planned to.
PREFIXES = {"due_at": "Due: ", "starts_at": ""}


def render (
	occasions: typing.Sequence[subroutine.domain.calendars.Occasion],
	*,
	name: str,
	instance_id: uuid.UUID,
	now: datetime.datetime,
	url_for: typing.Callable[[subroutine.db.models.work.Task], str] | None = None,
) -> str:
	"""Return the whole of one ``.ics`` document.

	``url_for`` is how an event gets a link back to the item, and is a callback because the
	address depends on the instance's ``public_url`` — which the domain does not read and
	must not (§13.5). ``None`` renders no ``URL`` property, which is what a feed served by an
	instance that does not know its own address should do rather than guess one.
	"""

	lines = [
		"BEGIN:VCALENDAR",
		"VERSION:2.0",
		f"PRODID:{_escaped(PRODUCT_ID)}",
		"CALSCALE:GREGORIAN",
		"METHOD:PUBLISH",
		# **Not in RFC 5545, and every major client reads it.** There is no standard property
		# for a subscribed calendar's name, so Apple's `X-WR-CALNAME` is what Google, Apple
		# and Outlook all use — a feed without one is listed under its URL, which is a secret.
		f"X-WR-CALNAME:{_escaped(name)}",
	]

	for occasion in occasions:
		lines.extend(_event(occasion, instance_id=instance_id, now=now, url_for=url_for))

	lines.append("END:VCALENDAR")

	# **CRLF, and a trailing one.** RFC 5545 §3.1 makes the line break part of the content
	# line rather than a separator between them, so a file whose last line is unterminated is
	# malformed — and is accepted by enough clients to ship unnoticed.
	return "".join(f"{folded}\r\n" for line in lines for folded in _fold(line))


def _event (
	occasion: subroutine.domain.calendars.Occasion,
	*,
	instance_id: uuid.UUID,
	now: datetime.datetime,
	url_for: typing.Callable[[subroutine.db.models.work.Task], str] | None,
) -> list[str]:
	"""Return the lines of one ``VEVENT``."""

	task = occasion.task
	when = getattr(task, occasion.field)
	all_day = getattr(task, _ALL_DAY[occasion.field], False)

	lines = [
		"BEGIN:VEVENT",
		# **The field is part of the identity, which corrects §20.4** (`#916`). That section
		# says a task with both a plan and a deadline "appears twice, which is correct", and
		# then gives the `UID` as `<task-id>@<instance-id>` — so the two would arrive under
		# one identity, and a client seeing a repeated `UID` either drops one or reads it as
		# an override of the other. Two events need two identities.
		#
		# **Stable across polls and unique across instances**, which is what the id pair is
		# for: a client updates rather than duplicating, and subscribing to two Subroutine
		# instances cannot collide.
		f"UID:{task.id}-{occasion.field}@{instance_id}",
		f"DTSTAMP:{_instant(now)}",
		f"SUMMARY:{_escaped(PREFIXES[occasion.field] + task.title)}",
	]

	if all_day:
		# **A `DATE` value, and `DTEND` is the day *after*** — RFC 5545 makes the end
		# exclusive, so an all-day event ending on its own date is zero days long and
		# disappears in some clients while showing in others.
		#
		# **The day is resolved once and the end is a calendar day after it**, rather than
		# a day added to the instant and converted afterwards. Twenty-four hours is not a
		# day on either night the clocks move: local midnight on 25 October 2026 plus 24
		# hours is 23:00 *the same evening* in London, so `DTEND` would equal `DTSTART` and
		# the event would be the zero-length one this comment exists to prevent.
		started = subroutine.domain.schedule.day_in(when, task.timezone)

		lines.append(f"DTSTART;VALUE=DATE:{_basic(started)}")
		lines.append(f"DTEND;VALUE=DATE:{_basic(started + datetime.timedelta(days=1))}")

	else:
		lines.append(f"DTSTART:{_instant(when)}")

		# **A span only where the pair says one** — decision `#972` §2. `starts_at` plus
		# `estimate_minutes` is an occupied span; a deadline is an instant and takes no time,
		# and a start with no estimate is something we do not know the length of. All three
		# render honestly rather than being given an invented hour.
		minutes = task.estimate_minutes if occasion.field == "starts_at" else None

		if minutes:
			lines.append(f"DTEND:{_instant(when + datetime.timedelta(minutes=minutes))}")

	if occasion.rule:
		lines.append(f"RRULE:{occasion.rule}")

	if url_for is not None:
		# **Not escaped, because `URL` is a URI value rather than a TEXT one** (RFC 5545
		# §3.3.13). Running it through `_escaped` would put a backslash in front of every
		# comma and semicolon in a query string — which our own addresses do not contain, so
		# it would have been correct for the values we happen to produce and wrong for the
		# first one somebody else's instance generated.
		lines.append(f"URL:{url_for(task)}")

	lines.append("END:VEVENT")

	return lines


#: Which flag says whether each dated field carries a time. Read from a table rather than
#: derived with ``removesuffix``, which is `#854`'s recorded trap: that derivation was right
#: by coincidence of the old names and started naming a field that does not exist the moment
#: one of them stopped ending in ``_at``.
_ALL_DAY = {"starts_at": "starts_is_all_day", "due_at": "due_is_all_day"}


def _instant (when: datetime.datetime) -> str:
	"""Return one instant as UTC basic format — ``20260817T140000Z``.

	**Everything is emitted in UTC rather than with a `TZID`**, which needs no `VTIMEZONE`
	block and cannot disagree with one. A client shows it in the reader's own zone, which is
	what a reader wants: §6.5's chain decides what the *server* computes with, and a calendar
	is read wherever the person is.
	"""

	return when.astimezone(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def _basic (day: datetime.date) -> str:
	"""Return one date as basic format — ``20260817``, with no time and no zone.

	**It takes a day rather than an instant** (`#1063`). This was handed the stored UTC
	instant and called ``strftime`` on it, which put a Los Angeles deadline a day late and a
	London plan a day early: an all-day deadline is the last microsecond of its day and an
	all-day plan the first, both local to the writer, so the UTC calendar date is the writer's
	only in UTC. Taking a :class:`datetime.date` is what makes the conversion the caller's and
	therefore impossible to forget — the type refuses the instant.

	A `DATE` value carries no zone, which is what makes this the whole of the correctness:
	there is nowhere for a client to reinterpret it, so whatever is written here is what
	somebody reads.
	"""

	return day.strftime("%Y%m%d")


def _escaped (value: str) -> str:
	"""Return a text value with the four characters RFC 5545 reserves escaped."""

	for character, replacement in ESCAPES:
		value = value.replace(character, replacement)

	return value


def _fold (line: str) -> list[str]:
	"""Split one content line into folded pieces, none longer than 75 octets.

	**Measured in octets and split on a character boundary**, which is the pair that makes
	this worth a function: counting characters overruns on any non-ASCII title, and splitting
	on a byte index would cut a multi-byte character in half and produce a file that is not
	valid UTF-8 at all. This project's own prose is full of em dashes, so neither is theoretical.
	"""

	if len(line.encode("utf-8")) <= FOLD_AT:
		return [line]

	pieces: list[str] = []
	# A continuation line begins with one space, which counts against its own limit — so the
	# marker is part of what is measured rather than something added afterwards.
	current = ""
	started = False

	for character in line:
		if len((current + character).encode("utf-8")) > FOLD_AT:
			pieces.append(current)
			current = " "
			started = True

		current += character

	# **`started` rather than a truthiness or a `strip`**, because a line whose final piece is
	# whitespace is data: `strip()` would drop it, silently, on exactly the input nobody
	# writes a test for. The question being asked is *did anything come after the fold*, and
	# only a flag answers it.
	if current and (not started or current != " "):
		pieces.append(current)

	return pieces
