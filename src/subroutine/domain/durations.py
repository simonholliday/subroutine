"""How long a piece of work takes, written the way a person would write it.

``90``, ``"90m"``, ``"1h30m"``, ``"2d"``, ``"1w"`` all mean a number of minutes, and
:func:`parse` turns any of them into one. :func:`humanize` goes back the other way, because
every response carries both ``estimate_minutes`` and ``estimate_human`` (docs/design.md §6.4) —
an agent should not have to divide by sixty to tell a person what it found.

**Conversions here are calendar-free and fixed**: ``1h = 60m``, ``1d = 1440m``,
``1w = 10080m``. A day is twenty-four hours, *not* a working day, and there is no month or
year unit at all because neither has a fixed length. This is deliberately **not** the same
``d`` as the one in :mod:`subroutine.domain.dates`, where ``now+1d`` means the same
wall-clock time tomorrow and may be twenty-three or twenty-five hours. Two grammars, two
jobs: an estimate measures elapsed effort, a date expression lands on a calendar. Both
publish their units in ``/v1/meta``.
"""

import re

import subroutine.errors

#: Minutes in each unit. Order matters: it is the order :func:`humanize` emits and the
#: descending order :func:`parse` requires, so every duration has exactly one spelling.
UNITS: tuple[tuple[str, int], ...] = (
	("w", 10080),
	("d", 1440),
	("h", 60),
	("m", 1),
)

#: The largest estimate that fits the column it is stored in. Checked here rather than
#: left to the database, because PostgreSQL refuses a 32-bit overflow and SQLite stores
#: the number happily — the same divergence :mod:`subroutine.domain.text` exists for.
MAX_MINUTES = 2_147_483_647

#: One ``<number><unit>`` term. Anchored by :func:`_terms`, never used on its own.
_TERM = re.compile(r"(\d+)([a-zA-Z]+)")

_UNIT_MINUTES = dict(UNITS)
_UNIT_ORDER = {unit: index for index, (unit, _minutes) in enumerate(UNITS)}
_VALID_UNITS = ", ".join(f"`{unit}`" for unit, _minutes in UNITS)


def parse (value: int | str, *, field: str = "estimate_minutes") -> int:
	"""Return a number of minutes, from an integer or a duration string.

	A bare number means minutes, so ``90`` and ``"90"`` and ``"90m"`` are the same thing.
	Compound strings run from the largest unit to the smallest — ``"1h30m"``, not
	``"30m1h"`` — and no unit may appear twice. That is stricter than it needs to be to
	work, and it is on purpose: a published grammar with one spelling per value is one an
	agent can round-trip, and a mis-ordered string is far more often a typo than an
	intention.

	``field`` names the field being parsed, so the error points at the right one when a
	request carries both an estimate and a spent time.
	"""

	if isinstance(value, bool):
		# ``bool`` is an ``int`` subclass, and ``True`` meaning "one minute" is nobody's
		# intention. Caught before the integer path swallows it.
		raise _invalid(value, field, "A duration is a number of minutes or a string like '1h30m'.")

	if isinstance(value, int):
		return _checked(value, value, field)

	# Whitespace between terms is dropped rather than refused, so that :func:`humanize`'s
	# output feeds straight back in. An agent that reads ``estimate_human`` as "1h 30m" and
	# sends it back should not be told that its own field is unparseable.
	text = re.sub(r"\s+", "", value)

	if not text:
		raise _invalid(value, field, "A duration cannot be empty.")

	if text.isdigit():
		return _checked(int(text), value, field)

	return _checked(_sum_terms(text, value, field), value, field)


def humanize (minutes: int) -> str:
	"""Return a duration as a person would say it — ``90`` becomes ``"1h 30m"``.

	Zero is ``"0m"`` rather than an empty string: a task estimated at nothing is a
	deliberate statement, and rendering it as blank makes it look unset.
	"""

	if minutes < 0:
		raise ValueError(f"Cannot render a negative duration: {minutes}.")

	if minutes == 0:
		return "0m"

	parts = []
	remaining = minutes

	for unit, size in UNITS:
		count, remaining = divmod(remaining, size)

		if count:
			parts.append(f"{count}{unit}")

	return " ".join(parts)


def _sum_terms (text: str, original: int | str, field: str) -> int:
	"""Add up a compound duration, refusing anything the grammar does not allow."""

	terms = list(_TERM.finditer(text))

	# Every character must belong to a term. Without this, "1h junk" would parse as an
	# hour and the junk would be silently dropped.
	if not terms or "".join(match.group(0) for match in terms) != text:
		raise _invalid(
			original,
			field,
			f"Write a duration as a number of minutes, or as digits and units — {_VALID_UNITS}. "
			"For example: 90, '90m', '1h30m', '2d'.",
		)

	total = 0
	previous = -1

	for match in terms:
		count, unit = int(match.group(1)), match.group(2)

		if unit not in _UNIT_MINUTES:
			raise _invalid(original, field, _unit_hint(unit))

		position = _UNIT_ORDER[unit]

		if position <= previous:
			raise _invalid(
				original,
				field,
				f"Units must run from largest to smallest and appear once each — "
				f"'1h30m', not '30m1h'. Valid units are {_VALID_UNITS}.",
			)

		previous = position
		total += count * _UNIT_MINUTES[unit]

	return total


def _unit_hint (unit: str) -> str:
	"""Explain an unrecognised unit, naming the mistake where it is a known one."""

	if unit in {"M", "y"}:
		return (
			f"'{unit}' is not a duration unit, because a month and a year have no fixed "
			f"length. Use {_VALID_UNITS}, or a date expression like 'now+1{unit}' if you "
			"meant a point in time rather than an amount of work."
		)

	if unit.lower() in _UNIT_MINUTES:
		return (
			f"Units are lower case: write '{unit.lower()}', not '{unit}'. Case matters "
			"because 'M' means months in a date expression and 'm' means minutes."
		)

	return f"'{unit}' is not a duration unit. Valid units are {_VALID_UNITS}."


def _checked (minutes: int, original: int | str, field: str) -> int:
	"""Refuse a duration that is negative or larger than the column can hold."""

	if minutes < 0:
		raise _invalid(original, field, "A duration cannot be negative.")

	if minutes > MAX_MINUTES:
		raise _invalid(
			original,
			field,
			f"The longest duration this can store is {MAX_MINUTES} minutes.",
		)

	return minutes


def _invalid (value: int | str, field: str, message: str) -> subroutine.errors.ValidationError:
	"""Build the refusal, naming the field and saying what a valid duration looks like."""

	return subroutine.errors.ValidationError(
		f"{value!r} is not a duration this understands.",
		code="invalid_field_value",
		hint=message,
		errors=[
			subroutine.errors.FieldError(
				field=field, code="invalid_field_value", message=message
			)
		],
	)
