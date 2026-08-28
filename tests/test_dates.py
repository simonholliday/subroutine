"""Tests for relative date expressions, including the two daylight saving cases.

A table over every keyword published in ``/v1/meta``, then the arithmetic rules that are
easy to state and easy to get wrong: elapsed units measured in UTC, calendar units measured
on the wall clock, and month arithmetic that clamps rather than overflows (docs/design.md §9.3).

Timezone is `Europe/London` throughout, because it is the one this was built in and because
its transitions are the ones a bug here would be noticed on.
"""

import datetime
import zoneinfo

import pytest

import subroutine.domain.dates
import subroutine.errors

LONDON = "Europe/London"

#: A Wednesday, mid-morning, in British Summer Time. Chosen so that every keyword lands
#: somewhere distinguishable — a Monday would make `today` and `start_of_week` agree and
#: hide a bug in one of them.
WEDNESDAY = datetime.datetime(2026, 7, 15, 10, 30, tzinfo=datetime.UTC)


def _london (expression: str, *, now: datetime.datetime = WEDNESDAY) -> datetime.datetime:
	"""Resolve an expression in London, returning the local rendering for legibility."""

	resolved = subroutine.domain.dates.resolve(expression, now=now, timezone=LONDON)

	assert resolved.tzinfo is datetime.UTC

	return resolved.astimezone(zoneinfo.ZoneInfo(LONDON))


def _elapsed (earlier: datetime.datetime, later: datetime.datetime) -> datetime.timedelta:
	"""Return real time between two instants, whatever timezones they are rendered in.

	Python subtracts two aware datetimes that share a ``tzinfo`` by wall clock rather than
	by elapsed time, so ``later - earlier`` is not the answer to this question across a
	clock change. Converting both to UTC first is.
	"""

	return later.astimezone(datetime.UTC) - earlier.astimezone(datetime.UTC)


@pytest.mark.parametrize(
	("expression", "expected"),
	[
		# 10:30 UTC is 11:30 in British Summer Time.
		("now", "2026-07-15 11:30:00.000000"),
		("today", "2026-07-15 00:00:00.000000"),
		("start_of_day", "2026-07-15 00:00:00.000000"),
		("tomorrow", "2026-07-16 00:00:00.000000"),
		("yesterday", "2026-07-14 00:00:00.000000"),
		("end_of_day", "2026-07-15 23:59:59.999999"),
		# The 15th is a Wednesday; the week runs Monday the 13th to Sunday the 19th.
		("start_of_week", "2026-07-13 00:00:00.000000"),
		("end_of_week", "2026-07-19 23:59:59.999999"),
		("start_of_month", "2026-07-01 00:00:00.000000"),
		("end_of_month", "2026-07-31 23:59:59.999999"),
		# The offset forms from §9.3.
		("now+7d", "2026-07-22 11:30:00.000000"),
		("now-2h", "2026-07-15 09:30:00.000000"),
		("today+1w", "2026-07-22 00:00:00.000000"),
		("end_of_week+3d", "2026-07-22 23:59:59.999999"),
		# Chained offsets, which the grammar allows and the specification does not
		# enumerate.
		("today+1w+12h", "2026-07-22 12:00:00.000000"),
	],
)
def test_every_published_expression_resolves_as_documented (
	expression: str, expected: str
) -> None:
	"""The table docs/design.md §9.3 publishes, asserted one row at a time."""

	assert _london(expression).strftime("%Y-%m-%d %H:%M:%S.%f") == expected


def test_every_keyword_in_the_published_list_is_resolvable () -> None:
	"""``/v1/meta`` publishes this list, so nothing in it may be unimplemented.

	The table above asserts what each one means; this asserts that the list and the
	implementation cannot drift apart, which is the failure that would publish a grammar
	the server does not honour.
	"""

	for keyword in subroutine.domain.dates.KEYWORDS:
		resolved = subroutine.domain.dates.resolve(keyword, now=WEDNESDAY, timezone=LONDON)

		assert resolved.tzinfo is datetime.UTC


def test_the_result_is_always_utc_whatever_the_caller_uses () -> None:
	"""Storage is UTC and timezone-aware, never naive (docs/design.md §6.5)."""

	for timezone in ("Europe/London", "America/Los_Angeles", "Australia/Sydney", "UTC"):
		resolved = subroutine.domain.dates.resolve("today", now=WEDNESDAY, timezone=timezone)

		assert resolved.tzinfo is datetime.UTC


def test_today_means_today_where_the_caller_is () -> None:
	"""The reason the timezone travels with the task at all (docs/design.md §6.5).

	At 00:30 UTC the calendar date is not the same in London as it is in Los Angeles, and
	"what is due today" has to mean the asker's today or the answer is nonsense.
	"""

	early = datetime.datetime(2026, 7, 15, 0, 30, tzinfo=datetime.UTC)

	london = subroutine.domain.dates.resolve("today", now=early, timezone=LONDON)
	los_angeles = subroutine.domain.dates.resolve(
		"today", now=early, timezone="America/Los_Angeles"
	)

	assert london.astimezone(zoneinfo.ZoneInfo(LONDON)).date() == datetime.date(2026, 7, 15)
	assert los_angeles.astimezone(
		zoneinfo.ZoneInfo("America/Los_Angeles")
	).date() == datetime.date(2026, 7, 14)

	assert london != los_angeles


def test_a_calendar_day_keeps_the_time_of_day_across_a_clock_change () -> None:
	"""``+1d`` is a calendar day, so 09:00 stays 09:00 when the clocks go forward.

	The UK moved to British Summer Time at 01:00 on 29 March 2026. Crossing that boundary,
	a calendar day is twenty-three hours of elapsed time — and it is the wall clock the
	person cares about, because their meeting is still at nine.
	"""

	saturday = datetime.datetime(2026, 3, 28, 9, 0, tzinfo=zoneinfo.ZoneInfo(LONDON))

	resolved = _london("now+1d", now=saturday.astimezone(datetime.UTC))

	assert resolved.strftime("%Y-%m-%d %H:%M") == "2026-03-29 09:00"

	# Subtracted in UTC on purpose. Two aware datetimes sharing one `tzinfo` subtract by
	# wall clock, not by elapsed time — the same convenience-that-changes-meaning the
	# implementation navigates, and it made the first version of this test assert 24 hours.
	assert _elapsed(saturday, resolved) == datetime.timedelta(hours=23)


def test_elapsed_hours_are_elapsed_hours_across_a_clock_change () -> None:
	"""``+2h`` is two hours of real time, so it may land three hours later on the clock.

	The counterpart to the test above, and the reason the two unit families are handled
	differently: "in two hours" must not skip an hour every spring.
	"""

	before = datetime.datetime(2026, 3, 29, 0, 30, tzinfo=zoneinfo.ZoneInfo(LONDON))

	resolved = _london("now+2h", now=before.astimezone(datetime.UTC))

	assert _elapsed(before, resolved) == datetime.timedelta(hours=2)

	# Three hours later on the clock, because one of them was the hour that did not happen.
	assert resolved.strftime("%H:%M") == "03:30"


def test_month_arithmetic_clamps_rather_than_overflowing () -> None:
	"""31 January plus one month is 28 February, not 3 March.

	The rule MVP-PLAN's S2-01 asked to be stated. Rolling over would put a task due at the
	end of one month into the middle of the next, silently.
	"""

	january = datetime.datetime(2026, 1, 31, 12, 0, tzinfo=datetime.UTC)

	assert _london("now+1M", now=january).date() == datetime.date(2026, 2, 28)

	leap = datetime.datetime(2028, 1, 31, 12, 0, tzinfo=datetime.UTC)

	assert _london("now+1M", now=leap).date() == datetime.date(2028, 2, 29)


def test_a_year_from_a_leap_day_clamps_too () -> None:
	"""29 February plus one year is 28 February, by the same rule."""

	leap_day = datetime.datetime(2028, 2, 29, 12, 0, tzinfo=datetime.UTC)

	assert _london("now+1y", now=leap_day).date() == datetime.date(2029, 2, 28)


def test_the_end_of_a_month_is_the_end_of_that_month () -> None:
	"""Month lengths are read from the calendar, including February in a leap year."""

	for when, expected in (
		(datetime.datetime(2026, 2, 10, 12, 0, tzinfo=datetime.UTC), "2026-02-28"),
		(datetime.datetime(2028, 2, 10, 12, 0, tzinfo=datetime.UTC), "2028-02-29"),
		(datetime.datetime(2026, 4, 10, 12, 0, tzinfo=datetime.UTC), "2026-04-30"),
	):
		assert _london("end_of_month", now=when).strftime("%Y-%m-%d") == expected


def test_the_week_starts_on_monday () -> None:
	"""ISO 8601, and the locale this was built in. Filed in Appendix A as a future setting."""

	for day in range(13, 20):
		when = datetime.datetime(2026, 7, day, 12, 0, tzinfo=datetime.UTC)

		assert _london("start_of_week", now=when).date() == datetime.date(2026, 7, 13)
		assert _london("end_of_week", now=when).date() == datetime.date(2026, 7, 19)


@pytest.mark.parametrize("name", sorted(set(subroutine.domain.dates.WEEKDAYS.values())))
def test_next_names_one_day_however_far_through_the_week_it_is_said (name: int) -> None:
	"""Seven people saying "next Friday" in one week mean the same Friday.

	That is the whole property, and stating it that way is what makes it checkable: the
	implementation took the *soonest* such day and added seven, so on any day later in the
	week than the one being named, the soonest was already in the week the speaker meant and
	the addition skipped it. "Next Monday" said on a Tuesday was a fortnight away.

	**Wrong in 21 of the 49 combinations of name and day**, measured — the review found two
	of them, both about Friday at a weekend, which is the shape where a person notices.
	"""

	spelling = next(
		word for word, number in sorted(subroutine.domain.dates.WEEKDAYS.items()) if number == name
	)
	monday = datetime.date(2026, 8, 17)
	answers = {
		subroutine.domain.dates.day_named(
			f"next {spelling}", today=monday + datetime.timedelta(days=offset)
		)
		for offset in range(7)
	}

	assert len(answers) == 1, f"'next {spelling}' named {sorted(str(a) for a in answers)}"

	answered = answers.pop()

	assert answered is not None
	assert answered.weekday() == name, "and it is that day of the week"
	assert monday + datetime.timedelta(days=7) <= answered < monday + datetime.timedelta(days=14), (
		"in the week after the one it was said in"
	)


def test_a_bare_weekday_at_a_weekend_agrees_with_next () -> None:
	"""Because on a Saturday the soonest Friday *is* next week's, and both should say so.

	The two readings differ for as long as there is a day of that name still to come this
	week, and stop differing once there is not. An implementation where they still differ at
	the weekend is one that has counted a week twice.
	"""

	saturday = datetime.date(2026, 8, 22)

	assert subroutine.domain.dates.day_named("friday", today=saturday) == datetime.date(
		2026, 8, 28
	)
	assert subroutine.domain.dates.day_named("next friday", today=saturday) == datetime.date(
		2026, 8, 28
	)


def test_end_of_day_matches_what_an_all_day_deadline_stores () -> None:
	"""``end_of_day`` and "due Friday" must be the same instant, not nearly (docs/design.md §6.5)."""

	resolved = _london("end_of_day")

	assert resolved.time() == subroutine.domain.dates.LAST_MICROSECOND


@pytest.mark.parametrize(
	"expression",
	[
		"",
		"   ",
		"nonsense",
		"today tomorrow",
		"+7d",
		"now+",
		"now+7",
		"now+7q",
		"NOW",
		"Today",
		"now++7d",
		"now+7d junk",
	],
)
def test_an_expression_outside_the_grammar_is_refused (expression: str) -> None:
	"""Refused with the valid keywords named, rather than guessed at."""

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		subroutine.domain.dates.resolve(expression, now=WEDNESDAY, timezone=LONDON, field="due")

	assert raised.value.status == 422
	assert raised.value.code == "invalid_field_value"
	# **Named back exactly as it was given, and it is given the *sendable* name** — `SR#1317`.
	# This module holds no mapping and needs none: `field` reaches it for messages alone, so
	# `schedule` translates the column before it crosses the boundary. Passing a column here
	# would test an input production no longer supplies.
	assert raised.value.errors[0].field == "due"


def test_minutes_and_months_are_distinguished_by_case () -> None:
	"""``m`` and ``M`` differ by a factor of about forty-three thousand."""

	minutes = _london("now+1m")
	months = _london("now+1M")

	assert minutes.strftime("%Y-%m-%d %H:%M") == "2026-07-15 11:31"
	assert months.strftime("%Y-%m-%d %H:%M") == "2026-08-15 11:30"


def test_an_unknown_timezone_is_refused_by_name () -> None:
	"""The error names the field being resolved, so a caller knows which one to fix."""

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		subroutine.domain.dates.resolve("today", now=WEDNESDAY, timezone="Mars/Olympus")

	assert raised.value.errors[0].field == "timezone"
	assert raised.value.status == 422


def test_one_instant_resolves_the_whole_request () -> None:
	"""``now`` is passed in, so two expressions in one filter cannot straddle midnight.

	Reading the clock inside `resolve` would make `start_of_day` and `end_of_day` land on
	different days for one microsecond a day — a bug nobody would ever reproduce.
	"""

	midnight = datetime.datetime(2026, 7, 15, 22, 59, 59, 999999, tzinfo=datetime.UTC)

	start = subroutine.domain.dates.resolve("start_of_day", now=midnight, timezone=LONDON)
	end = subroutine.domain.dates.resolve("end_of_day", now=midnight, timezone=LONDON)

	assert start < end
	assert (end - start) < datetime.timedelta(days=1)


@pytest.mark.parametrize(
	("written", "expected"),
	[
		("1 september", datetime.date(2026, 9, 1)),
		("1 Sep", datetime.date(2026, 9, 1)),
		("Sept 1", datetime.date(2026, 9, 1)),
		("1st September", datetime.date(2026, 9, 1)),
		("14 March", datetime.date(2027, 3, 14)),
		("Mar 14", datetime.date(2027, 3, 14)),
		("25 december", datetime.date(2026, 12, 25)),
		# **Counting today**, exactly as a weekday does: "by 24 August" said on the 24th means
		# today, and the other reading makes a date today impossible to say in this grammar.
		("24 august", datetime.date(2026, 8, 24)),
		# **Yesterday's date means next year's**, because a deadline already in the past reads
		# as overdue the moment it is set — which looks like a defect rather than like a year
		# having been assumed.
		("23 august", datetime.date(2027, 8, 23)),
		# The leap day, which is the only reason the search runs past next year at all.
		("29 february", datetime.date(2028, 2, 29)),
		# Not dates, and each comes back as nothing rather than as a guess.
		("31 february", None),
		("32 september", None),
		("september", None),
		("14", None),
		("the september release", None),
		("smarch 4", None),
	],
)
def test_a_written_calendar_date_is_read_both_ways_round (
	written: str, expected: datetime.date | None
) -> None:
	"""`SR#1210`. The spelling somebody reaches for when the date is months away.

	A weekday works for this week and a keyword works for this month; a bill due in September or
	a birthday in March is exactly where the grammar stopped and where an ISO date is least
	natural to type. It surfaced through `SR#1208`: a repeat needs a date beside it, and both
	natural ways to give one failed — so the only working spelling for a monthly bill was
	`by 2026-09-01 every month on the 1st`, which nobody would write.

	**A day number is required on both sides.** `by september` names no day and reading it as the
	first would be inventing one, where this grammar's rule is that an unreadable phrase stays in
	the title and says so.

	**A day the month does not have is not a date.** `31 february` comes back as nothing rather
	than rounded to the 28th or rolled into March, so the caller's refusal names the whole
	grammar instead of giving a confident wrong answer.
	"""

	assert subroutine.domain.dates.day_named(
		written, today=datetime.date(2026, 8, 24)
	) == expected


def test_the_leap_day_is_found_across_a_century_that_is_not_a_leap_year () -> None:
	"""The one case that needs the search to run more than a year ahead — `SR#1210`.

	1900 and 2100 are not leap years, so between 2096 and 2104 there is an eight-year gap with
	no 29th of February in it. **Written the short way first**, looking one year ahead with a
	comment claiming it meant 2028, and it returned nothing at all; then written to look eight
	years ahead *exclusive*, which is one short of the only case it was widened for.

	Both were found by driving it at the boundary rather than by reading it.
	"""

	assert subroutine.domain.dates.day_named(
		"29 february", today=datetime.date(2096, 3, 1)
	) == datetime.date(2104, 2, 29)

