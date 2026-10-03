"""The recurrence grammar, the rules it stores, and the dates those rules mean.

`#94`. Nothing here touches a database — :mod:`subroutine.domain.recurrence` takes text and
instants in and gives rules and instants out — so these are fast and exhaustive rather than
sampled, which is the trade §6.7's design was chosen for.

**The test worth reading first is the daylight-saving one.** Everything else here would pass
against an implementation that computes in UTC, and that implementation is wrong twice a year
for everybody who does not live in one.
"""

import datetime
import threading
import time
import zoneinfo

import pytest

import subroutine.domain.recurrence
import subroutine.errors

#: A Saturday, so that "every monday" cannot pass by accidentally landing on the same day.
NOW = datetime.datetime(2026, 8, 15, 9, 0, tzinfo=datetime.UTC)

LONDON = "Europe/London"

#: Every phrase in the brief `#94` was written from, and the rule each has to become.
#:
#: **Simon's own wording is in here twice**, forwards and fronted: he asked for "On the 30th of
#: every month", and the grammar was built from `every` outwards and refused exactly that. A
#: phrase the person who asked for the feature writes is the one worth holding.
ASKED_FOR: tuple[tuple[str, str], ...] = (
	("on the 30th of every month", "FREQ=MONTHLY;BYMONTHDAY=30"),
	("every month on the 30th", "FREQ=MONTHLY;BYMONTHDAY=30"),
	("on the last thursday of every month", "FREQ=MONTHLY;BYDAY=-1TH"),
	("every month on the last thursday", "FREQ=MONTHLY;BYDAY=-1TH"),
	("every monday", "FREQ=WEEKLY;BYDAY=MO"),
	("every year on 19 august", "FREQ=YEARLY;BYMONTH=8;BYMONTHDAY=19"),
	("every day", "FREQ=DAILY"),
	("every 14 days", "FREQ=DAILY;INTERVAL=14"),
)


@pytest.mark.parametrize(("text", "expected"), ASKED_FOR, ids=[one[0] for one in ASKED_FOR])
def test_every_shape_the_brief_asked_for_is_read (text: str, expected: str) -> None:
	"""The six examples `#94` was filed with, plus the two word orders of the awkward ones."""

	assert subroutine.domain.recurrence.rule(text).rule == expected


def test_every_published_example_is_one_the_grammar_reads () -> None:
	"""`#821`'s shape: a published vocabulary nothing drives is one that goes quietly wrong.

	``/v1/meta`` carries these, so an agent learns the grammar from this list and from nothing
	else — it does not send a phrase and get corrected, it never sends the phrase at all. An
	example that stops parsing would teach the wrong thing to every reader at once.
	"""

	examples = subroutine.domain.recurrence.published()["examples"]

	assert examples, "the published examples are empty, so this is checking nothing"

	for text in examples:
		read = subroutine.domain.recurrence.rule(text)

		assert read.rule, f"{text!r} is published as an example and does not parse"
		assert subroutine.domain.recurrence.occurrences(
			read.rule, start=NOW, timezone=LONDON, limit=1
		), f"{text!r} parses and then names no dates at all"


def test_a_weekly_time_survives_the_clocks_going_back () -> None:
	"""§6.7: occurrences are computed where the task lives, then converted.

	**The one test here that separates a correct implementation from a plausible one.** London
	is UTC+1 in August and UTC+0 in November, so a series computed in UTC keeps 09:00 UTC and
	drifts the local time to 10:00; computed locally it keeps the local 10:00 and the UTC value
	moves. The second is what somebody with a ten o'clock stand-up means.

    Falsified by computing in UTC instead: every assertion below still passes for August and
    the November one fails, which is exactly the seasonal shape that makes this worth pinning.
	"""

	summer = datetime.datetime(2026, 8, 3, 9, 0, tzinfo=datetime.UTC)
	zone = zoneinfo.ZoneInfo(LONDON)

	assert summer.astimezone(zone).strftime("%H:%M") == "10:00", (
		"the fixture does not start at the local hour it claims to"
	)

	found = subroutine.domain.recurrence.occurrences(
		"FREQ=WEEKLY;BYDAY=MO", start=summer, timezone=LONDON, limit=20
	)

	local = {moment.astimezone(zone).strftime("%H:%M") for moment in found}

	assert local == {"10:00"}, f"the local hour drifted across the year: {sorted(local)}"

	# **And the UTC value really does move**, which is what says the conversion happened rather
	# than the zone being ignored. Without this the test passes on a naive implementation that
	# never converts at all.
	assert {moment.strftime("%H:%M") for moment in found} == {"09:00", "10:00"}


def test_the_last_thursday_is_not_the_fourth_one () -> None:
	"""Months have four Thursdays or five, and a fixed count silently means a different week."""

	found = subroutine.domain.recurrence.occurrences(
		"FREQ=MONTHLY;BYDAY=-1TH", start=NOW, timezone=LONDON, limit=6
	)
	days = [moment.astimezone(zoneinfo.ZoneInfo(LONDON)).day for moment in found]

	assert all(day >= 22 for day in days), f"one of these is not a last Thursday: {days}"

	# July 2026 has five Thursdays, so a rule meaning "the fourth" would answer the 23rd where
	# this answers the 30th. Named rather than left to the reader to work out.
	fifth = subroutine.domain.recurrence.occurrences(
		"FREQ=MONTHLY;BYDAY=-1TH",
		start=datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.UTC),
		timezone=LONDON,
		limit=1,
	)

	assert fifth[0].astimezone(zoneinfo.ZoneInfo(LONDON)).day == 30


def test_an_exhausted_series_has_nothing_left_rather_than_failing () -> None:
	"""§6.7 honours ``COUNT`` and ``UNTIL``; running out is an answer, not a fault."""

	spent = subroutine.domain.recurrence.occurrences(
		"FREQ=DAILY;COUNT=3", start=NOW, timezone=LONDON, limit=10
	)

	assert len(spent) == 3

	assert (
		subroutine.domain.recurrence.following(
			"FREQ=DAILY;COUNT=3", start=NOW, after=spent[-1], timezone=LONDON
		)
		is None
	)

	# **And ``UNTIL``, which this said it honoured and never asked** (`SR#3765`). RFC 5545 writes
	# one in UTC beside a start with a zone, and the rule is walked on local time from a start
	# with none, so every rule ending on a date raised - and completing an occurrence of one
	# answered 500.
	last = NOW + datetime.timedelta(days=2)
	ending = f"FREQ=DAILY;UNTIL={last:%Y%m%dT%H%M%S}Z"
	ended = subroutine.domain.recurrence.occurrences(ending, start=NOW, timezone=LONDON, limit=10)

	assert ended == [NOW + datetime.timedelta(days=days) for days in range(3)], ended
	assert (
		subroutine.domain.recurrence.following(ending, start=NOW, after=ended[-1], timezone=LONDON)
		is None
	)


def test_an_until_in_any_spelling_is_stored_in_utc_and_read_on_the_starts_clock () -> None:
	"""`SR#3897`, M-9 (b) of the cold review of 2026-09-28: one spelling of ``UNTIL`` was mended.

	dateutil reads an ``UNTIL`` written many ways, and only ``YYYYMMDDTHHMMSSZ`` was put on the
	start's clock, so a rule ending *20261210T0000Z* was saved and then answered 500 when an
	occurrence was completed. **Stored in that one spelling now, and every spelling read**, because
	rows already hold the others; a year no clock can move it into is refused where it is written,
	and one a row holds already is read rather than raised.
	"""

	spellings = ("20261210T0000Z", "20261210T000000+0000", "20261210T010000+0100")
	start = datetime.datetime(2026, 12, 8, 9, 0, tzinfo=datetime.UTC)

	for spelling in spellings:
		stored = subroutine.domain.recurrence.rule(f"FREQ=DAILY;UNTIL={spelling}").rule

		assert stored == "FREQ=DAILY;UNTIL=20261210T000000Z", (spelling, stored)

		walked = subroutine.domain.recurrence.occurrences(
			f"FREQ=DAILY;UNTIL={spelling}", start=start, timezone=LONDON, limit=10
		)

		assert walked == [start, start + datetime.timedelta(days=1)], (spelling, walked)

	for spelling in ("99991231T230000Z", "00010101T000000Z"):
		with pytest.raises(subroutine.errors.ValidationError) as refused:
			subroutine.domain.recurrence.rule(f"FREQ=DAILY;UNTIL={spelling}")

		assert "first or last day" in refused.value.errors[0].message, refused.value.errors

	far = subroutine.domain.recurrence.occurrences(
		"FREQ=YEARLY;UNTIL=99991231T230000Z",
		start=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
		timezone="Asia/Tokyo",
		limit=2,
	)

	assert len(far) == 2, far


def test_the_instant_asked_from_is_not_answered_with_itself () -> None:
	""""What comes next" must not answer with the occurrence you are standing on.

	The materialisation loop asks this of the occurrence it has just completed, so an inclusive
	answer would mint the same date for ever — a series that never advances and never errors.
	"""

	monday = datetime.datetime(2026, 8, 17, 9, 0, tzinfo=datetime.UTC)

	assert subroutine.domain.recurrence.following(
		"FREQ=WEEKLY;BYDAY=MO", start=monday, after=monday, timezone=LONDON
	) == datetime.datetime(2026, 8, 24, 9, 0, tzinfo=datetime.UTC)

	# And the inclusive reading is available for the *first* occurrence, where standing on the
	# start date and being told the next one is a week away would skip a week.
	[first] = subroutine.domain.recurrence.occurrences(
		"FREQ=WEEKLY;BYDAY=MO", start=monday, timezone=LONDON, limit=1
	)

	assert first == monday


#: A phrase, and the words its refusal has to contain. **Every one is a sentence somebody could
#: plausibly write**, rather than nonsense chosen to be easy to refuse.
REFUSED: tuple[tuple[str, str], ...] = (
	("every fortnight", "fortnight"),
	("every 3 sausages", "3 sausages"),
	("every", "every what"),
	("next tuesday", "starts with 'every'"),
	("every 0 days", "at least one"),
	("every year on 31 february", "no day 31"),
	("every month on the 32nd", "1 to 31"),
	("", "cannot be empty"),
)


@pytest.mark.parametrize(("text", "said"), REFUSED, ids=[one[0] or "empty" for one in REFUSED])
def test_a_refusal_names_the_half_that_actually_failed (text: str, said: str) -> None:
	"""A refusal must not assert a cause it has not established.

	**"every fortnight" was answered with "a repeat starts with 'every'"**, which is true, useless
	and about a word the writer got right — so they check the half that worked and learn nothing
	about the half that did not. Each of these names the part that was unreadable.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.recurrence.rule(text)

	assert said in refused.value.errors[0].message, (
		f"{text!r} was refused with {refused.value.errors[0].message!r}, "
		f"which does not mention {said!r}"
	)

	assert refused.value.errors[0].hint, "and a refusal says what would have worked"


def test_a_time_of_day_is_refused_and_told_where_it_goes () -> None:
	"""`#854`: the rule says how often and the item says when.

	Folding a clock into the rule would be a second place to store the thing `starts_at` holds,
	which is the duplication that item spent a day removing.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.recurrence.rule("every monday at 12:00")

	assert "on the item itself" in refused.value.errors[0].message


def test_a_rule_carrying_a_part_this_does_not_store_is_refused () -> None:
	"""``dateutil`` reads far more of RFC 5545 than §6.7 stores.

	A rule accepted whole and honoured in part is the worst available outcome: it saves, it
	round-trips, and it produces occurrences on days nobody asked for. Checked part by part
	rather than handed to the parser and trusted.
	"""

	for stored in ("FREQ=MONTHLY;BYSETPOS=2", "FREQ=WEEKLY;BYWEEKNO=3"):
		with pytest.raises(subroutine.errors.ValidationError):
			subroutine.domain.recurrence.rule(stored)

	# **Frequencies finer than a day are refused too**, because every occurrence is a row, a ref
	# off the workspace counter and an event.
	for stored in ("FREQ=HOURLY", "FREQ=MINUTELY", "FREQ=SECONDLY"):
		with pytest.raises(subroutine.errors.ValidationError):
			subroutine.domain.recurrence.rule(stored)


@pytest.mark.parametrize("count", ["0", "-2"])
def test_a_repeat_that_comes_round_no_times_is_refused (count: str) -> None:
	"""`SR#3935`, L-4 of the cold review of 2026-09-28: ``COUNT=0`` was stored as *0 times*.

	dateutil reads it as a rule with nothing in it, so a new task was refused for having no dates,
	while a change to an existing one answered 200 and a preview answered with no occurrences.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.recurrence.rule(f"FREQ=DAILY;COUNT={count}")

	assert "COUNT starts at 1" in refused.value.errors[0].message, refused.value.errors


NEVER_COMES_ROUND = [
	("FREQ=DAILY;INTERVAL=0", "at least one unit apart"),
	("FREQ=WEEKLY;INTERVAL=0;BYDAY=MO", "at least one unit apart"),
	("FREQ=MONTHLY;INTERVAL=0", "at least one unit apart"),
	("FREQ=YEARLY;INTERVAL=0", "at least one unit apart"),
	("FREQ=DAILY;INTERVAL=-1", "at least one unit apart"),
	("FREQ=DAILY;INTERVAL=99999999999", "at most 36,500 days"),
	("FREQ=YEARLY;INTERVAL=10000", "at most 100 years"),
	("FREQ=MONTHLY;BYMONTHDAY=0", "1 to 31"),
	("FREQ=MONTHLY;BYMONTHDAY=32", "1 to 31"),
	("FREQ=MONTHLY;BYMONTHDAY=-32", "1 to 31"),
	("FREQ=YEARLY;BYMONTH=13", "1 to 12"),
	("FREQ=YEARLY;BYMONTH=0;BYMONTHDAY=1", "1 to 12"),
	("FREQ=DAILY;INTERVAL=1;INTERVAL=0", "INTERVAL is given twice"),
	("FREQ=DAILY;UNTIL=20261231T000000Z;UNTIL=20270101T000000Z", "UNTIL is given twice"),
	("FREQ=DAILY;COUNT=3;UNTIL=20261231T000000Z", "not both"),
	("FREQ=WEEKLY;BYDAY=1MO", "only a monthly or yearly repeat"),
	("FREQ=DAILY;BYDAY=-1FR", "only a monthly or yearly repeat"),
	("FREQ=MONTHLY;BYDAY=6MO", "past the 5"),
	("FREQ=YEARLY;BYDAY=54MO", "past the 53"),
	("FREQ=YEARLY;BYMONTH=6;BYDAY=6MO", "past the 5"),
	("FREQ=DAILY;UNTIL=20261231T000000+2400", "not a time any clock reads"),
]


@pytest.mark.parametrize(
	("rule", "said"), NEVER_COMES_ROUND, ids=[one[0] for one in NEVER_COMES_ROUND]
)
def test_a_rule_part_no_calendar_reaches_is_refused_by_name (rule: str, said: str) -> None:
	"""`SR#3997`, H-1 of the cold review of 2026-09-30: stored, then walked for ever or misread.

	``FREQ=DAILY;INTERVAL=0`` was stored, and completing its occurrence never returned, holding a
	worker and its connection. The same validator let through every value here. **Each is
	refused by name**, as a ``ValidationError`` rather than whatever dateutil raises.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.recurrence.rule(rule)

	assert said in refused.value.errors[0].message, refused.value.errors[0].message


@pytest.mark.parametrize(
	"rule",
	[
		"FREQ=MONTHLY;BYDAY=-1FR",
		"FREQ=MONTHLY;BYDAY=5MO",
		"FREQ=YEARLY;BYMONTH=6;BYDAY=1MO",
		"FREQ=YEARLY;BYDAY=53MO",
		"FREQ=MONTHLY;BYMONTHDAY=31",
		"FREQ=MONTHLY;BYMONTHDAY=-31",
		"FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29",
		"FREQ=DAILY;INTERVAL=36500",
		"FREQ=YEARLY;INTERVAL=100",
		"FREQ=WEEKLY;BYDAY=MO,TH",
	],
)
def test_the_edges_of_what_a_calendar_reaches_are_still_accepted (rule: str) -> None:
	"""The other side of `SR#3997`'s refusals: the last Friday, the fifth Monday, the 31st."""

	assert subroutine.domain.recurrence.rule(rule).rule == rule


@pytest.mark.parametrize(
	"written",
	[
		"every\x1cday",
		"FREQ=WEEKLY;BYDAY=MO\x1c",
		"FREQ=DAILY;INTERVAL=\uff12",
		"FREQ=DAILY;INTERVAL=\u0662",
		"FREQ=MONTHLY;BYMONTHDAY=\uff11",
		"FREQ=DAILY;COUNT=\uff13",
	],
	ids=["separator", "separator after a part", "fullwidth", "arabic-indic", "day", "count"],
)
def test_a_repeat_holding_what_no_rule_is_written_in_is_refused (written: str) -> None:
	"""`SR#4320`: a separator read as a space, and digits that are not ASCII stored as sent.

	``rule("every\\x1cday")`` was accepted and read back with the separator in it, and an
	``INTERVAL`` of a fullwidth two was described as *every other day* and written into a calendar's
	feed as sent.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.recurrence.rule(written)

	assert refused.value.errors[0].field == "recurrence", refused.value.errors


@pytest.mark.parametrize(
	"written",
	[
		"FREQ=MONTHLY;BYMONTHDAY=1;BYDAY=2MO",
		"FREQ=MONTHLY;BYMONTHDAY=8;BYDAY=1MO",
		"FREQ=MONTHLY;BYMONTHDAY=1;BYDAY=2MO;COUNT=3",
	],
)
def test_a_rule_that_never_comes_round_is_refused_saying_so (written: str) -> None:
	"""`SR#4320`: walked to the year 9999 before anything said it had no dates.

	The first of a month is never its second Monday, nor the eighth its first. A create refused
	it afterwards with a sentence about dates that had passed.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.recurrence.rule(written)

	assert "never come round" in refused.value.errors[0].message, refused.value.errors


@pytest.mark.parametrize(
	"written",
	[
		"FREQ=YEARLY;BYMONTH=2;BYDAY=5MO",
		"FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29;BYDAY=MO",
		"FREQ=MONTHLY;BYMONTHDAY=13;BYDAY=FR",
		"FREQ=DAILY;INTERVAL=7;BYDAY=TU",
	],
)
def test_a_rule_that_comes_round_rarely_is_accepted (written: str) -> None:
	"""The control: a fifth Monday in February, a leap day on a Monday, Friday the 13th.

	**And one that comes round from some starts only**: every seventh day is a Tuesday from a
	Tuesday. Whether it does from the series' own start is asked where that start is known.
	"""

	assert subroutine.domain.recurrence.rule(written).rule == written


@pytest.mark.parametrize("phrase", ["every 0 days", "every 101 years", "every 36501 days"])
def test_a_phrase_is_held_to_the_same_intervals (phrase: str) -> None:
	"""`SR#3997`: a phrase never passes through the rule check, so it shares the interval's."""

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.recurrence.rule(phrase)


@pytest.mark.parametrize("stored", ["FREQ=DAILY;INTERVAL=0", "FREQ=WEEKLY;INTERVAL=0;BYDAY=MO,TU"])
def test_a_rule_stored_before_the_check_that_never_moves_on_still_returns (stored: str) -> None:
	"""`SR#3997`: a row stored with ``INTERVAL=0`` before the check still exists somewhere.

	dateutil answers it with the same moments for ever, so asking for what comes after its first
	never returned. **The walk stops where the rule stops moving forward**: whatever it named up
	to there, and nothing after.
	"""

	start = datetime.datetime(2026, 10, 5, 9, 0, tzinfo=datetime.UTC)
	answered: list[list[datetime.datetime]] = []
	walker = threading.Thread(
		target=lambda: answered.append(
			subroutine.domain.recurrence.occurrences(
				stored, start=start, timezone="Europe/London", after=start + datetime.timedelta(days=2)
			)
		),
		daemon=True,
	)

	walker.start()
	walker.join(timeout=10)

	assert not walker.is_alive(), "the walk never returned"
	assert answered == [[]], answered


def test_a_rule_stored_before_the_check_that_walks_backwards_is_refused_by_name () -> None:
	"""`SR#3997`: ``INTERVAL=-1`` was a 500 when its occurrence was completed. **A refusal.**"""

	start = datetime.datetime(2026, 10, 31, 9, 0, tzinfo=datetime.UTC)

	try:
		found = subroutine.domain.recurrence.occurrences(
			"FREQ=MONTHLY;INTERVAL=-1", start=start, timezone="Europe/London", after=start, limit=1
		)

	except subroutine.errors.ValidationError:
		return

	assert found == [], found


def test_a_date_ends_a_rule_as_the_whole_of_its_day () -> None:
	"""`SR#3935`, NEW-1 of the verification of 2026-09-28: ``UNTIL=20261210`` was refused.

	RFC 5545 writes a series' end as a date beside a start that is one, so an all-day rule copied
	from a calendar carried the form this refused, with dateutil's sentence about zones. **Stored
	as written, and read as the whole of its day on the series' clock**, so a slot at the day's
	first second, a deadline at its last and a meeting between are all on it.
	"""

	stored = subroutine.domain.recurrence.rule("FREQ=DAILY;UNTIL=20261210").rule

	assert stored == "FREQ=DAILY;UNTIL=20261210"

	zone = zoneinfo.ZoneInfo(LONDON)

	for start in (
		datetime.datetime(2026, 12, 8, 0, 0, tzinfo=zone),
		datetime.datetime(2026, 12, 8, 23, 59, 59, 999999, tzinfo=zone),
		datetime.datetime(2026, 12, 8, 9, 0, tzinfo=zone),
	):
		found = subroutine.domain.recurrence.occurrences(
			stored, start=start.astimezone(datetime.UTC), timezone=LONDON
		)

		assert len(found) == 3, (start, found)


@pytest.mark.parametrize(
	("stored", "whole_day", "written"),
	[
		("FREQ=DAILY;UNTIL=20260825T225959Z", True, "FREQ=DAILY;UNTIL=20260825"),
		("FREQ=DAILY;UNTIL=20260825", True, "FREQ=DAILY;UNTIL=20260825"),
		("FREQ=DAILY;UNTIL=20260825", False, "FREQ=DAILY;UNTIL=20260825T225959Z"),
		("FREQ=DAILY;UNTIL=20260825T225959Z", False, "FREQ=DAILY;UNTIL=20260825T225959Z"),
		("FREQ=WEEKLY;UNTIL=20260825T090000;BYDAY=TU", False, "FREQ=WEEKLY;UNTIL=20260825T080000Z;BYDAY=TU"),
		("FREQ=DAILY;COUNT=3", True, "FREQ=DAILY;COUNT=3"),
	],
)
def test_a_rule_s_end_is_written_for_a_calendar_as_its_start_is (
	stored: str, whole_day: bool, written: str
) -> None:
	"""`SR#3935`: RFC 5545 §3.3.10 makes a rule's ``UNTIL`` and its start the same kind of value.

	A date beside a start that is a date, and a date-time in UTC beside one with a time, each read
	on the series' own clock - London's here, an hour ahead of UTC in August.
	"""

	assert subroutine.domain.recurrence.for_a_calendar(
		stored, whole_day=whole_day, timezone=LONDON
	) == written


@pytest.mark.parametrize(("at_its_end", "day"), [(True, "20261009"), (False, "20261010")])
def test_a_whole_day_series_ends_for_a_calendar_on_the_last_day_it_falls_on (
	at_its_end: bool, day: str
) -> None:
	"""`SR#4026`, L-3 (2) of the cold review of 2026-09-30: the feed wrote a day the series never made.

	A whole-day deadline falls at the end of its day, so ``UNTIL`` at noon on 10 October ends it on
	the 9th, which is what the program makes; the feed wrote the 10th, and a calendar showed it. A
	whole-day start falls at the day's beginning and still falls on the 10th.
	"""

	stored = "FREQ=DAILY;UNTIL=20261010T120000Z"
	written = subroutine.domain.recurrence.for_a_calendar(
		stored, whole_day=True, timezone=LONDON, at_its_end=at_its_end
	)
	start = datetime.datetime(2026, 10, 7, 23, 59, 59) if at_its_end else datetime.datetime(2026, 10, 7)
	made = subroutine.domain.recurrence.occurrences(
		stored, start=start.replace(tzinfo=zoneinfo.ZoneInfo(LONDON)), timezone=LONDON
	)
	last = made[-1].astimezone(zoneinfo.ZoneInfo(LONDON)).date()

	assert written == f"FREQ=DAILY;UNTIL={day}", written
	assert last.strftime("%Y%m%d") == day, made


def test_a_rule_that_names_a_real_part_and_means_nothing_is_still_refused () -> None:
	"""The part list says a name is allowed; only building the rule says the value parses."""

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.recurrence.rule("FREQ=WEEKLY;BYDAY=XX")


def test_a_rule_sent_directly_keeps_no_words_and_a_phrase_keeps_its_own () -> None:
	"""§6.7 stores the rule; the text is a courtesy for whoever wrote a sentence.

	``None`` rather than the rule repeated back, because a reader shown
	``FREQ=WEEKLY;INTERVAL=2;BYDAY=TU`` where they typed it has been told nothing, and a reader
	shown it where they typed *"every other tuesday"* has been told something false.
	"""

	written = subroutine.domain.recurrence.rule("every other tuesday")

	assert written.rule == "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"
	assert written.text == "every other tuesday"

	direct = subroutine.domain.recurrence.rule("FREQ=WEEKLY;INTERVAL=2;BYDAY=TU")

	assert direct.rule == "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"
	assert direct.text is None


#: A rule, and the sentence it has to read back as.
DESCRIBED: tuple[tuple[str, str], ...] = (
	("FREQ=DAILY", "every day"),
	("FREQ=DAILY;INTERVAL=14", "every 14 days"),
	("FREQ=WEEKLY;BYDAY=MO", "every Monday"),
	("FREQ=WEEKLY;INTERVAL=2;BYDAY=TU", "every other week, on Tuesday"),
	("FREQ=MONTHLY;BYMONTHDAY=30", "every month, on the 30th"),
	("FREQ=MONTHLY;BYDAY=-1TH", "every month, on the last Thursday"),
	("FREQ=YEARLY;BYMONTH=8;BYMONTHDAY=19", "every year, on 19 August"),
	("FREQ=WEEKLY;BYDAY=FR;COUNT=3", "every Friday, 3 times"),
	# **Lists, which are ordinary rules** (`SR#3923`): each answered 500 on every write.
	("FREQ=MONTHLY;BYMONTHDAY=1,15", "every month, on the 1st and 15th"),
	("FREQ=YEARLY;BYMONTH=1,7;BYMONTHDAY=1", "every year, on 1 January and July"),
	# **A day counted from the end** (`SR#3935`), which read back as *on the -1th*.
	("FREQ=MONTHLY;BYMONTHDAY=-1", "every month, on the last day"),
	("FREQ=MONTHLY;BYMONTHDAY=1,-1", "every month, on the 1st and last day"),
	("FREQ=MONTHLY;BYMONTHDAY=-2", "every month, on the 2nd to last day"),
	("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=-1", "every year, on the last day of February"),
	# **Every month by its whole name, and a count of one or two said as a word** (`SR#4026`),
	# which read *on 1 5*, *on 1 Sept* and *1 times*.
	("FREQ=YEARLY;BYMONTH=5;BYMONTHDAY=1", "every year, on 1 May"),
	("FREQ=YEARLY;BYMONTH=9;BYMONTHDAY=1", "every year, on 1 September"),
	("FREQ=DAILY;COUNT=1", "every day, once"),
	("FREQ=DAILY;COUNT=2", "every day, twice"),
	# **A month said without a day of the month too** (`SR#4026`), which read as every month.
	("FREQ=YEARLY;BYMONTH=6;BYDAY=1MO", "every year, on the first Monday, in June"),
	("FREQ=MONTHLY;BYMONTH=6,12;BYDAY=-1FR", "every month, on the last Friday, in June and December"),
	# **An ordinal past the fourth** (`SR#4320`), which read *the 5 Monday* and *the -2 Monday*.
	("FREQ=MONTHLY;BYDAY=5MO", "every month, on the 5th Monday"),
	("FREQ=MONTHLY;BYDAY=-2MO", "every month, on the second to last Monday"),
	("FREQ=MONTHLY;BYDAY=-5MO", "every month, on the 5th to last Monday"),
	("FREQ=YEARLY;BYDAY=20MO", "every year, on the 20th Monday"),
	("FREQ=YEARLY;BYDAY=53MO", "every year, on the 53rd Monday"),
)


@pytest.mark.parametrize(("stored", "said"), DESCRIBED, ids=[one[0] for one in DESCRIBED])
def test_a_rule_reads_back_as_a_sentence (stored: str, said: str) -> None:
	"""§6.7's ``/v1/recurrence/parse`` exists so an agent can confirm before committing.

	**The description is generated from the rule, never echoed from the input.** Echoing would
	confirm nothing: the whole point is that the words come back changed, so a reader can see
	whether the thing understood is the thing they meant.
	"""

	assert subroutine.domain.recurrence.describe(stored) == said


def test_the_description_differs_from_the_words_that_were_typed () -> None:
	"""Which is the property that makes it a check rather than a mirror."""

	written = "on the last thursday of every month"
	read = subroutine.domain.recurrence.rule(written)

	assert subroutine.domain.recurrence.describe(read.rule) != written
	assert "last Thursday" in subroutine.domain.recurrence.describe(read.rule)


def test_a_leap_day_is_accepted_and_skips_the_years_without_one () -> None:
	"""29 February is a real birthday, and RFC 5545 already answers what to do about it."""

	read = subroutine.domain.recurrence.rule("every year on 29 february")

	found = subroutine.domain.recurrence.occurrences(
		read.rule,
		start=datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC),
		timezone=LONDON,
		limit=2,
	)
	years = [moment.astimezone(zoneinfo.ZoneInfo(LONDON)).year for moment in found]

	assert years == [2028, 2032], f"the leap years were not skipped correctly: {years}"


def test_asking_for_a_window_stops_at_the_end_of_it () -> None:
	"""What a calendar asks: everything between now and the end of the month, and no more."""

	found = subroutine.domain.recurrence.occurrences(
		"FREQ=DAILY",
		start=NOW,
		timezone=LONDON,
		until=datetime.datetime(2026, 8, 20, 23, 59, tzinfo=datetime.UTC),
	)

	ceiling = datetime.datetime(2026, 8, 20, 23, 59, tzinfo=datetime.UTC)

	# **Six, because the anchor is an occurrence too.** A calendar asking for a window means
	# everything in it, including whatever is happening on the first day — an exclusive
	# reading would hide today's stand-up from today's calendar.
	assert len(found) == 6, [moment.isoformat() for moment in found]
	assert found[0] == NOW
	assert all(moment <= ceiling for moment in found)


def test_a_rule_is_stored_as_it_was_checked_rather_than_as_it_was_typed () -> None:
	"""`#929`. Every part of an ``RRULE`` is case-insensitive and only the check knew it.

	``_checked`` upper-cases each part *name* to validate it and then returned the original
	string, so ``freq=weekly;byday=mo`` was accepted by the parser, stored verbatim, and
	described back as ``"every "``.

	**That is the worst place for it to fail.** Reading a rule back in different words is the
	whole reason a phrase or an ``RRULE`` may be handed to this at all — a repeat that cannot
	be confirmed is a repeat nobody can check against what they meant.
	"""

	read = subroutine.domain.recurrence.rule("freq=weekly;byday=mo")

	assert read.rule == "FREQ=WEEKLY;BYDAY=MO"
	assert subroutine.domain.recurrence.describe(read.rule) == "every Monday"

	# A row written before this was fixed is still out there, so `describe` does not assume
	# its argument came from `_checked`.
	assert subroutine.domain.recurrence.describe("freq=weekly;byday=mo") == "every Monday"


@pytest.mark.parametrize(
	"impossible",
	[
		"FREQ=DAILY;BYMONTH=2;BYMONTHDAY=31",
		"FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30",
		"FREQ=YEARLY;BYMONTH=4;BYMONTHDAY=31",
		"FREQ=MONTHLY;BYMONTH=6,9;BYMONTHDAY=31",
	],
)
def test_a_rule_naming_a_date_that_does_not_exist_is_refused (impossible: str) -> None:
	"""Well-formed, legal, and asking for the 31st of February.

	Nothing rejected it. It parses, it stores, ``describe`` renders it as *"every day, on 31
	February"*, and asking for its occurrences sends ``dateutil`` walking the calendar day by
	day until its own internal limit — **2.68 seconds of CPU, synchronously, measured**, for
	one request on an endpoint whose default rate limit is 600 a minute.

	**Refused rather than bounded, and the measurement is why.** A ceiling on the search was
	written first and thrown away: ``dateutil`` applies ``UNTIL`` to candidates it *generates*,
	so a rule that generates none runs to its internal limit whatever ceiling it is given —
	measured at the same 2.66 seconds with one in place, which is an inert control. Refusing
	also answers the better question: this is not a date, rather than a date with nothing on it.
	"""

	started = time.monotonic()

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.recurrence.rule(impossible)

	assert time.monotonic() - started < 1.0, "refused, but only after doing the work anyway"


@pytest.mark.parametrize(
	"possible",
	[
		# A birthday somebody has.
		"FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29",
		# The 31st of whichever months have one, which is what the rule means.
		"FREQ=MONTHLY;BYMONTHDAY=31",
		# February can never be the 31st and March always can, so the pair is satisfiable.
		"FREQ=YEARLY;BYMONTH=2,3;BYMONTHDAY=31",
		# Counting back from the end of a short month is fine.
		"FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=-1",
	],
)
def test_a_rare_date_is_not_an_impossible_one (possible: str) -> None:
	"""The half that makes the refusal above worth having rather than merely strict.

	Each of these looks like the refused shape and comes round: a leap day, a month that has a
	31st only sometimes, a pair where one month can and the other cannot, and a day counted
	back from the end. A check that refused these would be one somebody has to work around.
	"""

	assert subroutine.domain.recurrence.rule(possible).rule
