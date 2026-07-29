"""Tests for the duration grammar, including the round trip a client actually performs.

Property-based where the property is worth stating — every duration must survive being
rendered for a person and read back — and table-driven where the point is the exact
published spelling (SPEC.md §6.4).
"""

import hypothesis
import hypothesis.strategies
import pytest

import subroutine.domain.durations
import subroutine.errors


@pytest.mark.parametrize(
	("written", "minutes"),
	[
		(90, 90),
		(0, 0),
		("0", 0),
		("90", 90),
		("90m", 90),
		("1h30m", 90),
		("1h", 60),
		("2d", 2880),
		("1w", 10080),
		("1w2d3h4m", 10080 + 2880 + 180 + 4),
		# Whitespace is dropped, because this is what `humanize` produces.
		("1h 30m", 90),
		("  2d  ", 2880),
	],
)
def test_the_published_spellings_all_parse (written: int | str, minutes: int) -> None:
	"""Every example in SPEC.md §6.4 means what the specification says it means."""

	assert subroutine.domain.durations.parse(written) == minutes


@pytest.mark.parametrize(
	("minutes", "rendered"),
	[
		(0, "0m"),
		(1, "1m"),
		(60, "1h"),
		(90, "1h 30m"),
		(1440, "1d"),
		(10080, "1w"),
		(10080 + 2880 + 180 + 4, "1w 2d 3h 4m"),
	],
)
def test_durations_render_the_way_a_person_would_say_them (minutes: int, rendered: str) -> None:
	"""``estimate_human`` exists so nobody has to divide by sixty (SPEC.md §6.4)."""

	assert subroutine.domain.durations.humanize(minutes) == rendered


@hypothesis.given(
	hypothesis.strategies.integers(
		min_value=0, max_value=subroutine.domain.durations.MAX_MINUTES
	)
)
def test_any_duration_survives_being_rendered_and_read_back (minutes: int) -> None:
	"""The round trip a client performs: read ``estimate_human``, send it back unchanged.

	This is the property that made ``parse`` tolerate whitespace. Without it the two
	functions in this module disagreed about their own output, which nothing in a
	table-driven test would have caught — the tables were written from the specification,
	and the specification does not mention spaces.
	"""

	rendered = subroutine.domain.durations.humanize(minutes)

	assert subroutine.domain.durations.parse(rendered) == minutes


@hypothesis.given(
	weeks=hypothesis.strategies.integers(min_value=0, max_value=50),
	days=hypothesis.strategies.integers(min_value=0, max_value=6),
	hours=hypothesis.strategies.integers(min_value=0, max_value=23),
	minutes=hypothesis.strategies.integers(min_value=0, max_value=59),
)
def test_a_compound_duration_is_the_sum_of_its_terms (
	weeks: int, days: int, hours: int, minutes: int
) -> None:
	"""Whatever the units, the answer is arithmetic — no calendar is consulted."""

	written = f"{weeks}w{days}d{hours}h{minutes}m"
	expected = weeks * 10080 + days * 1440 + hours * 60 + minutes

	assert subroutine.domain.durations.parse(written) == expected


@hypothesis.given(hypothesis.strategies.integers(min_value=0, max_value=100000))
def test_a_bare_number_is_always_minutes (minutes: int) -> None:
	"""``90`` and ``"90"`` and ``"90m"`` are one value written three ways."""

	assert subroutine.domain.durations.parse(minutes) == minutes
	assert subroutine.domain.durations.parse(str(minutes)) == minutes
	assert subroutine.domain.durations.parse(f"{minutes}m") == minutes


@pytest.mark.parametrize(
	"written",
	[
		"",
		"   ",
		"abc",
		"1h junk",
		"h30m",
		"1x",
		"-5",
		"1.5h",
		# Out of order and repeated, both refused so that a value has one spelling.
		"30m1h",
		"1h1h",
	],
)
def test_a_duration_that_is_not_in_the_grammar_is_refused (written: str) -> None:
	"""Refused, not guessed at — and the refusal names the field."""

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		subroutine.domain.durations.parse(written, field="estimate_minutes")

	assert raised.value.status == 422
	assert raised.value.code == "invalid_field_value"
	assert raised.value.errors[0].field == "estimate_minutes"


def test_months_and_years_are_refused_with_the_reason () -> None:
	"""The one mistake worth explaining rather than just rejecting.

	Someone writing ``"3M"`` means three months. Parsed case-insensitively that would be
	three *minutes* — a silent error of five orders of magnitude — so the grammar has no
	month unit at all and says why.
	"""

	for written in ("3M", "2y"):
		with pytest.raises(subroutine.errors.ValidationError) as raised:
			subroutine.domain.durations.parse(written)

		assert raised.value.hint is not None
		assert "no fixed length" in raised.value.hint


def test_upper_case_units_are_refused_rather_than_folded () -> None:
	"""Case-insensitivity here would make ``M`` ambiguous with the date grammar's months."""

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		subroutine.domain.durations.parse("1H30M")

	assert raised.value.hint is not None
	assert "lower case" in raised.value.hint


def test_a_negative_duration_is_refused () -> None:
	"""Work takes a non-negative amount of time."""

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.durations.parse(-1)


def test_a_duration_too_large_for_the_column_is_refused_here () -> None:
	"""PostgreSQL would refuse the overflow and SQLite would store it — so we refuse first.

	Exactly the divergence `domain.text` exists for, in a different field (SPEC.md §10.3).
	"""

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		subroutine.domain.durations.parse(subroutine.domain.durations.MAX_MINUTES + 1)

	assert raised.value.code == "invalid_field_value"

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.durations.parse("999999w")


def test_a_boolean_is_not_a_duration () -> None:
	"""``bool`` is an ``int`` subclass, so ``True`` would otherwise mean one minute."""

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.durations.parse(True)
