"""Checking that user-supplied text fits the column it is going into.

This exists because the two backends disagree about what happens when it does not.
PostgreSQL refuses an over-length value with ``StringDataRightTruncation``; SQLite does not
enforce ``VARCHAR`` lengths at all and stores it in full. So the same input succeeds on a
laptop and fails in production, which is precisely the class of divergence the dual-backend
rule exists to catch (SPEC.md §10.3).

SPEC.md §6.10 already says what should happen instead: limits are "enforced with a clear
error code rather than a truncation". Truncating silently would be worse than either
backend's behaviour — the user would not be told that the end of their sentence is gone.
"""

import subroutine.errors


def fit (
	value: str,
	*,
	field: str,
	limit: int,
	label: str | None = None,
) -> str:
	"""Return ``value`` stripped, or refuse it for being longer than the column allows."""

	cleaned = value.strip()

	if len(cleaned) <= limit:
		return cleaned

	name = label or field

	raise subroutine.errors.PayloadTooLarge(
		f"That {name} is {len(cleaned)} characters, and the limit is {limit}.",
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="payload_too_large",
				message=f"A {name} is limited to {limit} characters.",
			)
		],
	)


def require (value: str, *, field: str, label: str | None = None) -> str:
	"""Return ``value`` stripped, or refuse it for being empty."""

	cleaned = value.strip()

	if cleaned:
		return cleaned

	name = label or field

	raise subroutine.errors.ValidationError(
		f"A {name} is required.",
		code="missing_field",
		errors=[
			subroutine.errors.FieldError(
				field=field, code="missing_field", message=f"A {name} is required."
			)
		],
	)
