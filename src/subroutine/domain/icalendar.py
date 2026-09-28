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

import calendar
import dataclasses
import datetime
import typing
import uuid

import dateutil.rrule

import subroutine.db.models.work
import subroutine.domain.calendars
import subroutine.domain.dates
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

	# **A timed repeat is kept on the clock of the zone it was set in** (`#1078`), which is Simon's
	# decision of 2026-08-22: *a scheduled time is wall-clock time in the setter's zone*. A rule
	# over a UTC start repeats at a fixed UTC hour, so a weekly 09:00 in London became 08:00 on a
	# subscriber's calendar when the clocks went back. Each zone so named is described once, ahead
	# of the events that name it.
	described = _described(occasions, now=now)

	for zone in sorted(described):
		changes, until = described[zone]
		lines.extend(_vtimezone(zone, changes, until=until))

	for occasion in occasions:
		lines.extend(
			_event(
				occasion, instance_id=instance_id, now=now, url_for=url_for, described=described
			)
		)

	lines.append("END:VCALENDAR")

	# **CRLF, and a trailing one.** RFC 5545 §3.1 makes the line break part of the content
	# line rather than a separator between them, so a file whose last line is unterminated is
	# malformed — and is accepted by enough clients to ship unnoticed.
	return "".join(f"{folded}\r\n" for line in lines for folded in _fold(line))


#: Minutes in a day, for writing a reminder as days rather than as a large number of minutes.
_MINUTES_A_DAY = 24 * 60

#: How far either side of a timed repeat its zone's clock changes are read (`#1078`): a year
#: before the earliest start, so each rule a client needs has begun before the first occurrence,
#: and a year past the feed's own window, so every occurrence it is shown falls under one.
_ZONE_MARGIN = datetime.timedelta(days=366)

#: **And eight years at least**, so a yearly rule is read off enough of them to tell *the last
#: Sunday* from *the fourth*, which are the same day in most years.
_ZONE_SPAN = datetime.timedelta(days=366 * 8)

#: How often a zone's clock is read while looking for a change, before narrowing to the second.
#: **Once a day, and measured to be enough**: over all 599 zones this system knows, 2025 to 2035,
#: reading hourly found no change that reading daily missed.
_A_DAY_IN_SECONDS = 24 * 60 * 60

#: RFC 5545's weekdays, in :meth:`datetime.date.weekday` order.
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

#: **The last moment a zone is read up to, two years short of the calendar's end** (`#3750`), so
#: a reading an offset later, and a rule expanded to the end of its window, stay inside the years
#: Python can count.
_LAST_READABLE = datetime.datetime(9998, 1, 1, tzinfo=datetime.UTC)


@dataclasses.dataclass(frozen=True)
class _Change:
	"""One moment a zone's clocks change: the instant, the offsets either side, and its new name."""

	onset: datetime.datetime
	before: datetime.timedelta
	after: datetime.timedelta
	daylight: bool
	name: str

	@property
	def wall (self) -> datetime.datetime:
		"""Return the onset as the clock read it just before, which is how RFC 5545 dates one."""

		return (self.onset + self.before).replace(tzinfo=None)


def _event (
	occasion: subroutine.domain.calendars.Occasion,
	*,
	instance_id: uuid.UUID,
	now: datetime.datetime,
	url_for: typing.Callable[[subroutine.db.models.work.Task], str] | None,
	described: typing.Collection[str] = (),
) -> list[str]:
	"""Return the lines of one ``VEVENT``, on its zone's clock where ``described`` names the zone."""

	task = occasion.task
	when = getattr(task, occasion.field)
	all_day = getattr(task, _ALL_DAY[occasion.field], False)
	zone = _series_zone(occasion)
	local = zone if zone in described else None

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

	# **Only a start has a far end** — a deadline is a moment and `ends_at` says nothing about
	# one. Read once here so both branches below ask the same question.
	finish = task.ends_at if occasion.field == "starts_at" else None

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
		#
		# **And the last day is `ends_at`'s where there is one** (`#1235`). Until then this
		# was `started + 1 day` unconditionally, so a fortnight booked off rendered as a
		# single day — the case that made the field necessary. A `VALUE=DATE` span is what
		# every client draws as a banner across the top of those days rather than as a block
		# covering their hours, which is the convention this reads as intended.
		started = subroutine.domain.schedule.day_in(when, task.timezone)
		last = started if finish is None else subroutine.domain.schedule.day_in(
			finish, task.timezone
		)

		lines.append(f"DTSTART;VALUE=DATE:{_basic(started)}")
		lines.append(f"DTEND;VALUE=DATE:{_basic(last + datetime.timedelta(days=1))}")

	else:
		lines.append(_moment("DTSTART", when, local))

		# **An end where one was given, an estimate where one was not** — decision `#1235`
		# over decision `#972` §2, and the fallback is deliberate rather than left behind.
		# `ends_at` is what somebody said the span *is*; `estimate_minutes` is how long the
		# work takes, which is the best available guess at occupancy and is what `at 2pm ~1h`
		# has parsed to since `#797`. Quick capture still cannot set an end (`#1235` §5), so
		# dropping the fallback would take the span off every appointment ever captured.
		#
		# A deadline is an instant and takes no time; a start with neither is something whose
		# length we do not know. Both render with no `DTEND` rather than an invented hour.
		minutes = task.estimate_minutes if occasion.field == "starts_at" else None

		if finish is not None:
			lines.append(_moment("DTEND", finish, local))

		elif minutes:
			lines.append(_moment("DTEND", when + datetime.timedelta(minutes=minutes), local))

	if occasion.rule:
		lines.append(f"RRULE:{occasion.rule}")

		# **The holes in the grid, said out loud** (`#1248`). A client expands the rule and
		# draws every slot it describes, so one whose occurrence has been moved or deleted is
		# an appointment in somebody's calendar that is not happening — and it is the one that
		# looks normal, because the moved occurrence appears beside it as an ordinary event.
		#
		# **The value type has to match `DTSTART`'s** (RFC 5545 §3.8.5.1) or the exclusion
		# matches nothing, so this branches exactly as the start above did rather than picking
		# a format. Several dates go on one line, comma separated, which `_fold` then wraps.
		if occasion.emptied:
			if all_day:
				days = [
					_basic(subroutine.domain.schedule.day_in(one, task.timezone))
					for one in occasion.emptied
				]

				lines.append("EXDATE;VALUE=DATE:" + ",".join(days))

			elif local is None:
				lines.append("EXDATE:" + ",".join(_instant(one) for one in occasion.emptied))

			else:
				lines.append(
					f"EXDATE;TZID={local}:" + ",".join(_clock(one, local) for one in occasion.emptied)
				)

	if url_for is not None:
		# **Not escaped, because `URL` is a URI value rather than a TEXT one** (RFC 5545
		# §3.3.13). Running it through `_escaped` would put a backslash in front of every
		# comma and semicolon in a query string — which our own addresses do not contain, so
		# it would have been correct for the values we happen to produce and wrong for the
		# first one somebody else's instance generated.
		lines.append(f"URL:{url_for(task)}")

	# **A reminder, as an alarm hanging off this event** (`#1211`, and `#577`'s conclusion that
	# a relative reminder beats an absolute one). The client expands it against the `RRULE`
	# itself, so "two weeks before my sister's birthday" reminds two weeks before **every**
	# occurrence and nothing here computes a date per year or stores one.
	#
	# **Relative to the event rather than to a field**, which is how this needs none of what
	# `#577` is still open on: an occasion is already one date, so the alarm is relative to
	# *that* and nothing has to decide whether a reminder is a nudge or a warning.
	#
	# **`DISPLAY` with a `DESCRIPTION`, because RFC 5545 §3.6.6 requires both** for that action
	# — an alarm missing either is malformed, and a client's response to malformed is its own
	# business rather than something we get to predict.
	if task.reminder_minutes:
		lines.extend(
			[
				"BEGIN:VALARM",
				"ACTION:DISPLAY",
				f"TRIGGER:-{_duration(task.reminder_minutes)}",
				f"DESCRIPTION:{_escaped(PREFIXES[occasion.field] + task.title)}",
				"END:VALARM",
			]
		)

	lines.append("END:VEVENT")

	return lines


def _duration (minutes: int) -> str:
	"""Return minutes as an RFC 5545 duration — ``P14D``, ``PT1H``, ``PT30M``.

	**Whole days are written as days**, which is what a client shows a reader: `TRIGGER:-P14D`
	reads as *two weeks before* where `-PT20160M` is the same instant and tells them nothing.
	The value is one number in the database either way; this is the rendering of it.

	**Days and minutes, never weeks.** `P2W` is legal and cannot be combined with anything else
	in the same duration, so a rule that reached for it would have to fall back for 15 days —
	two spellings, one of them rare, and no reader is better off for it.
	"""

	days, left = divmod(minutes, _MINUTES_A_DAY)

	if left == 0:
		return f"P{days}D"

	hours, remainder = divmod(left, 60)
	clock = f"{hours}H" if hours else ""
	clock += f"{remainder}M" if remainder else ""

	return f"P{days}DT{clock}" if days else f"PT{clock}"


#: Which flag says whether each dated field carries a time. Read from a table rather than
#: derived with ``removesuffix``, which is `#854`'s recorded trap: that derivation was right
#: by coincidence of the old names and started naming a field that does not exist the moment
#: one of them stopped ending in ``_at``.
_ALL_DAY = {"starts_at": "starts_is_all_day", "due_at": "due_is_all_day"}


def _dated (when: datetime.date, rest: str) -> str:
	"""Return a date in basic format: its year in four digits, then the rest as ``rest`` writes it.

	**The four digits are written out rather than left to** ``%Y`` (`#3788`), which the C library
	pads or not as it pleases: here year 2 came out as ``2``, and 09:00 on 1 March of it as
	``20301T090000``, which no client can read.
	"""

	return f"{when.year:04d}{when.strftime(rest)}"


def _instant (when: datetime.datetime) -> str:
	"""Return one instant as UTC basic format — ``20260817T140000Z``.

	**A single instant is emitted in UTC rather than with a `TZID`**, which needs no `VTIMEZONE`
	block and cannot disagree with one. A client shows it in the reader's own zone, which is
	what a reader wants: §6.5's chain decides what the *server* computes with, and a calendar
	is read wherever the person is.

	**A timed repeat is not an instant** (`#1078`). Its rule is about the clock in the zone it was
	set in, and *every Monday at nine where I am* has no spelling in UTC, so its dates go through
	:func:`_moment` with the zone named and described.
	"""

	return _dated(when.astimezone(datetime.UTC), "%m%dT%H%M%SZ")


def _moment (name: str, when: datetime.datetime, zone: str | None) -> str:
	"""Return one date-time property: in UTC, or on the clock of a zone the document describes."""

	if zone is None:
		return f"{name}:{_instant(when)}"

	return f"{name};TZID={zone}:{_clock(when, zone)}"


def _clock (when: datetime.datetime, zone: str) -> str:
	"""Return an instant as a zone's clock showed it — ``20261019T090000``, with no zone mark."""

	return _wall(when.astimezone(subroutine.domain.dates.zone(zone)).replace(tzinfo=None))


def _wall (clock: datetime.datetime) -> str:
	"""Return a time as a clock shows it, in basic format with nothing to say which clock."""

	return _dated(clock, "%m%dT%H%M%S")


def _series_zone (occasion: subroutine.domain.calendars.Occasion) -> str | None:
	"""Return the zone a timed repeat's clock is read in, or ``None`` for anything else.

	**The zone the server mints its occurrences in** - ``template.timezone`` and its fallback, as
	:func:`subroutine.domain.tasks.materialise` reads them - so a calendar and the agenda cannot
	put one occurrence at two hours. A single timed event is an instant and needs none; an all-day
	one is a day, and a day has none to need.
	"""

	if not occasion.rule or getattr(occasion.task, _ALL_DAY[occasion.field], False):
		return None

	return occasion.task.timezone or subroutine.domain.schedule.DEFAULT_TIMEZONE


def _described (
	occasions: typing.Sequence[subroutine.domain.calendars.Occasion], *, now: datetime.datetime
) -> dict[str, tuple[list[_Change], datetime.datetime]]:
	"""Return each zone a timed repeat here is kept in, its clock changes, and how far they reach.

	**Every zone but one at UTC's own offset** (`#3749`). A zone without summer time does fall at
	one UTC hour all year, and was left in UTC for that - but a rule names days, and 08:00 on a
	Monday in Tokyo is 23:00 on the Sunday in UTC, so a rule naming Monday repeated a day late
	there, and a month's 1st on the 2nd. A zone at offset zero has UTC's days, so it stays in UTC,
	and so does every series a feed carried before `#1078` in the zone the server falls back to.
	"""

	earliest: dict[str, datetime.datetime] = {}

	for occasion in occasions:
		zone = _series_zone(occasion)

		if zone is None:
			continue

		when = getattr(occasion.task, occasion.field)
		earliest[zone] = min(when, earliest.get(zone, when))

	ahead = now + datetime.timedelta(days=subroutine.domain.calendars.FUTURE_DAYS) + _ZONE_MARGIN
	described: dict[str, tuple[list[_Change], datetime.datetime]] = {}

	for zone, first in earliest.items():
		# **Read over the years somebody will look at** (`#3750`). From a year before a start typed
		# as year 2 was two thousand years of daily readings, 0.7 s a request, and a start before its
		# second day, or in the calendar's last years, ran off one end of it - so the whole feed
		# answered 500. A client draws the occurrences near now, so the first start is taken as no
		# earlier than :data:`_ZONE_SPAN` ago, and no later than leaves the window room to end.
		start = min(max(first, now - _ZONE_SPAN), _LAST_READABLE - _ZONE_SPAN - _ZONE_MARGIN)
		since = start - _ZONE_MARGIN
		until = max(ahead, since + _ZONE_SPAN)
		changes = _changes(zone, since=since, until=until) or _unchanging(zone, since=since)

		if changes:
			described[zone] = (changes, until)

	return described


def _unchanging (name: str, *, since: datetime.datetime) -> list[_Change]:
	"""Return the one observance a zone whose clock never changes needs, or none at offset zero.

	**RFC 5545 §3.6.5 wants a STANDARD or a DAYLIGHT in every VTIMEZONE** (`#3749`), and a block
	with neither gives a client that reads it no offset at all. So a zone whose clock never moves
	is written as one change that changes nothing: its only offset, from the start of the window.
	"""

	offset, _summer, called = _reading(subroutine.domain.dates.zone(name), int(since.timestamp()))

	if not offset:
		return []

	return [_Change(onset=since, before=offset, after=offset, daylight=False, name=called)]


def _changes (name: str, *, since: datetime.datetime, until: datetime.datetime) -> list[_Change]:
	"""Return every moment a zone's clocks change between two instants, oldest first.

	**Found by asking the zone, since nothing publishes its rules.** :mod:`zoneinfo` answers what
	the clock reads at an instant and nothing else, so this reads it once a day and narrows each
	difference to the second, which is as finely as the tz database writes a change.
	"""

	zone = subroutine.domain.dates.zone(name)
	here = int(since.timestamp())
	end = int(until.timestamp())
	was = _reading(zone, here)
	found: list[_Change] = []

	while here < end:
		there = min(here + _A_DAY_IN_SECONDS, end)

		if _reading(zone, there) == was:
			here = there

			continue

		low, high = here, there

		while high - low > 1:
			middle = (low + high) // 2

			if _reading(zone, middle) == was:
				low = middle

			else:
				high = middle

		became = _reading(zone, high)
		found.append(
			_Change(
				onset=datetime.datetime.fromtimestamp(high, datetime.UTC),
				before=was[0],
				after=became[0],
				daylight=became[1],
				name=became[2],
			)
		)
		here, was = high, became

	return found


def _reading (zone: datetime.tzinfo, seconds: int) -> tuple[datetime.timedelta, bool, str]:
	"""Return what a zone's clock says at one instant: its offset, whether it is summer time, its name."""

	moment = datetime.datetime.fromtimestamp(seconds, zone)

	return moment.utcoffset() or datetime.timedelta(0), bool(moment.dst()), moment.tzname() or ""


def _vtimezone (
	name: str, changes: typing.Sequence[_Change], *, until: datetime.datetime
) -> list[str]:
	"""Return one ``VTIMEZONE``: each kind of change a zone makes, and when it makes it.

	**A yearly rule where one says it exactly**, which is how the calendars people use write a zone
	for themselves - the last Sunday of October at 02:00, for as long as the zone keeps to it.
	Where no single rule does, because the zone moved its dates or never kept a pattern, **the
	dates themselves**, as ``RDATE``: longer, and as true for the window they were read over.
	"""

	lines = ["BEGIN:VTIMEZONE", f"TZID:{name}"]
	kinds: dict[tuple[bool, datetime.timedelta, datetime.timedelta, str], list[_Change]] = {}

	for change in changes:
		kinds.setdefault(
			(change.daylight, change.before, change.after, change.name), []
		).append(change)

	for (daylight, before, after, called), made in kinds.items():
		kind = "DAYLIGHT" if daylight else "STANDARD"
		rule = _yearly(made, until=until)

		lines.extend(
			[
				f"BEGIN:{kind}",
				f"DTSTART:{_wall(made[0].wall)}",
				f"TZOFFSETFROM:{_offset(before)}",
				f"TZOFFSETTO:{_offset(after)}",
			]
		)

		if rule is not None:
			lines.append(f"RRULE:{rule}")

		elif len(made) > 1:
			lines.append("RDATE:" + ",".join(_wall(one.wall) for one in made[1:]))

		if called:
			lines.append(f"TZNAME:{_escaped(called)}")

		lines.append(f"END:{kind}")

	lines.append("END:VTIMEZONE")

	return lines


def _yearly (made: typing.Sequence[_Change], *, until: datetime.datetime) -> str | None:
	"""Return the yearly rule these changes follow, or ``None`` where no one rule names them all.

	**Read off the dates and then checked by expanding it.** The month, the weekday and the hour
	have to agree, and either every date is the last of its weekday in the month or every one is
	in the same week of it. The rule is then kept only if it names exactly these dates **and no
	other before the window ends**: a zone that stops changing its clocks must not be written as
	one that goes on.
	"""

	walls = [one.wall for one in made]
	first = walls[0]
	years = [wall.year for wall in walls]

	if len(walls) < 2 or years != list(range(first.year, first.year + len(walls))):
		return None

	if len({(wall.month, wall.weekday(), wall.time()) for wall in walls}) != 1:
		return None

	if all(wall.day + 7 > calendar.monthrange(wall.year, wall.month)[1] for wall in walls):
		week = -1

	elif len({(wall.day - 1) // 7 for wall in walls}) == 1:
		week = (first.day - 1) // 7 + 1

	else:
		return None

	rule = f"FREQ=YEARLY;BYMONTH={first.month};BYDAY={week}{_WEEKDAYS[first.weekday()]}"
	series = dateutil.rrule.rrulestr(f"RRULE:{rule}", dtstart=first)
	last = (until + made[-1].before).replace(tzinfo=None)

	if list(series.between(first, last, inc=True)) != walls:
		return None

	return rule


def _offset (delta: datetime.timedelta) -> str:
	"""Return a UTC offset as RFC 5545 writes one - ``+0100``, ``-0500``, ``+0530``."""

	total = int(delta.total_seconds())
	sign = "-" if total < 0 else "+"
	hours, rest = divmod(abs(total), 3600)
	minutes, seconds = divmod(rest, 60)

	return f"{sign}{hours:02d}{minutes:02d}" + (f"{seconds:02d}" if seconds else "")


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

	return _dated(day, "%m%d")


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
