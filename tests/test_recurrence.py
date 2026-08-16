"""The recurrence grammar, the rules it stores, and the dates those rules mean.

`#94`. Nothing here touches a database — :mod:`subroutine.domain.recurrence` takes text and
instants in and gives rules and instants out — so these are fast and exhaustive rather than
sampled, which is the trade §6.7's design was chosen for.

**The test worth reading first is the daylight-saving one.** Everything else here would pass
against an implementation that computes in UTC, and that implementation is wrong twice a year
for everybody who does not live in one.
"""

import datetime
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
