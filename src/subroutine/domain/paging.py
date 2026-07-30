"""How many rows a listing returns, decided in one place.

A page size is **input**, and this codebase has a rule about that: a CHECK constraint is not
input validation, and neither is a database's own idea of what ``LIMIT -1`` means. The two
backends disagree — SQLite reads a negative limit as *no limit* and PostgreSQL raises
``InvalidRowCountInLimitClause`` — so an unvalidated size is both a wrong answer and a crash,
depending on where it runs (SPEC.md §10.3).

It lives in the domain rather than in the API because **both clients need the same answer**.
``GET /v1/tasks`` declared ``ge=1`` and capped at ``max_page_size``; the local client did
neither, so ``tasks(limit=1000)`` returned 250 rows locally and 200 from the same database over
HTTP — with a test asserting the two clients agree that could not see it, because it passed no
limit. §13.7's whole arrangement rests on the two transports answering identically, and a
listing's length is part of the answer.
"""

import subroutine.config
import subroutine.errors

#: The smallest page anybody may ask for. Zero is refused rather than treated as "the default"
#: or as "none": a caller that asks for nothing has made a mistake, and both readings of it are
#: guesses about which mistake.
MIN_SIZE = 1


def size (limit: int | None, settings: subroutine.config.Settings) -> int:
	"""Return how many rows to fetch, refusing a limit nothing could honour.

	``None`` means "you choose", and is the ordinary case. Anything below :data:`MIN_SIZE` is a
	refusal naming the field and the range; anything above ``max_page_size`` is *capped* rather
	than refused, which is what the API has always done — a caller asking for more than the
	instance will serve is asking a reasonable question and gets as much as there is.
	"""

	if limit is None:
		return min(settings.default_page_size, settings.max_page_size)

	if limit < MIN_SIZE:
		raise subroutine.errors.ValidationError(
			f"A page cannot hold {limit} items.",
			errors=[
				subroutine.errors.FieldError(
					field="limit",
					code="invalid_field_value",
					message=f"'limit' must be at least {MIN_SIZE}.",
					hint=f"Leave it out for the default of {settings.default_page_size}, or "
					f"ask for anything from {MIN_SIZE} to {settings.max_page_size}.",
				)
			],
		)

	return min(limit, settings.max_page_size)
