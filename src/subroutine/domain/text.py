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

#: How much of a title fits on one line of a compact listing before it is cut. Sixty
#: characters is what leaves room for an address, a date and a priority inside eighty.
ONE_LINE_LIMIT = 60

#: How much prose makes an item worth announcing before somebody opens it (`#595`).
#:
#: **Anchored on something rather than chosen round.** The whole MCP tool surface — every
#: schema an agent carries in every session, budgeted and held by a test — is a little over ten
#: thousand bytes. An item whose prose alone exceeds that costs more to read once than the
#: tools cost all day, which is the point at which a reader is owed the fact before deciding.
#:
#: **Here rather than on either surface that uses it.** The command line and the agent's tools
#: both mark a large item, and nothing in `mcp` may import `cli` — a served instance need not
#: have been started through the command line at all, which is the same argument that moved
#: `is_loopback` into `config`. Two thresholds agreed separately would drift into one surface
#: warning where the other did not, and nothing would look wrong on either side.
LARGE_PROSE = 10_000


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


def require (value: str | None, *, field: str, label: str | None = None) -> str:
	"""Return ``value`` stripped, or refuse it for being empty.

	**``None`` is refused exactly as ``""`` is**, and that is the fix for one defect with
	nine call sites. §8.3 says an omitted field is unchanged and a null one clears, so a
	caller following the convention sends ``{"title": null}`` — and every request model
	declares `title: str | None = None` in order to express "omitted", so the null arrives
	here as ``None`` and ``None.strip()`` raised. That was a **500 on tasks, documents and
	projects alike**, on the commonest field there is, and it survived two reviews.

	Refusing here rather than at each router is what makes the answer the same everywhere:
	a title that cannot be cleared is a missing title, and "A title is required." naming the
	field is already the right sentence. Every required string in this system passes through
	this function, so the CLI and the MCP adapter are covered by the same change.
	"""

	cleaned = (value or "").strip()

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


def truncated (text: str, limit: int = ONE_LINE_LIMIT) -> str:
	"""Shorten text for a one-line rendering, marking that something was cut.

	Nothing is refused and nothing is stored — this is how a title is *printed* in a
	compact listing or an aligned column, which is why it lives here rather than beside
	:func:`fit`. The ellipsis is the whole point: a line that has quietly lost its end reads
	as the whole title.
	"""

	collapsed = " ".join(text.split())

	if len(collapsed) <= limit:
		return collapsed

	# A limit below 1 has no honest answer, and `collapsed[: limit - 1]` turned into a negative
	# slice — returning *more* characters than the limit asked for, which is the one outcome the
	# function exists to prevent. Every caller uses the default today; the parameter is public,
	# and the obvious next caller is a computed column width.
	if limit < 1:
		return "…"

	return f"{collapsed[: limit - 1]}…"
