"""Answering one listing as several, each group capped and paged on its own — `#1790`.

**One runner, because there are two listings.** Tasks and documents both draw board columns and
both hit the same defect, and writing the loop twice is this codebase's signature failure: two
copies of a rule, correct while they agree, and nothing comparing them. What differs between
them is how a row is rendered and which namespace its cursor belongs to, so those are arguments.

The vocabulary this reads — which axes exist, what keys each has, and what a group may hold —
is :mod:`subroutine.domain.grouping`, where both transports can reach it.
"""

import itertools
import typing
import uuid

import fastapi.responses
import pydantic
import sqlalchemy
import sqlalchemy.orm

import subroutine.api.pagination
import subroutine.api.shaping
import subroutine.domain.grouping
import subroutine.errors
import subroutine.views


def refuse_a_cursor (cursor: str | None, *, axis: str) -> None:
	"""Refuse a cursor sent beside a grouping, naming what to send instead.

	**Refused rather than ignored**, which is `#1484`'s rule. There is no one position in a
	grouped answer to continue from — the groups do not share a sequence — so a cursor here
	could only mean something invented. Each group reports its own instead, and that one is
	valid on an ordinary listing narrowed to the group, which is how a column pages without any
	new machinery.
	"""

	if cursor is None:
		return

	raise subroutine.errors.ValidationError(
		"A grouped listing has no single place to continue from.",
		errors=[
			subroutine.errors.FieldError(
				field="cursor",
				code="invalid_field_value",
				message="'cursor' and 'group_by' cannot be sent together.",
				hint=(
					f"Each group carries its own next_cursor, which is valid on a listing "
					f"narrowed to that group — send {axis}=<key> and the cursor together."
				),
			)
		],
	)


def answer (
	session: sqlalchemy.orm.Session,
	statement: sqlalchemy.Select[typing.Any],
	*,
	secret_key: str,
	keys: typing.Sequence[subroutine.api.pagination.SortKey],
	axis: str,
	kind: str,
	status_column: typing.Any,
	workspace_id: uuid.UUID,
	limit: int | None,
	include_total: bool,
	shape: subroutine.api.shaping.Shape,
	render: typing.Callable[
		[sqlalchemy.orm.Session, typing.Sequence[typing.Any]], list[typing.Any]
	],
	collection: str,
) -> fastapi.responses.JSONResponse:
	"""Run one query once per group and return the groups, each with its own page.

	``statement`` is the caller's whole query, ordered but not yet limited — so every filter,
	every search and every readiness rule they asked for applies to all of the groups
	unchanged. That is why grouping is a parameter on the listing rather than a route of its
	own: a second route would have had to redeclare twenty narrowings and then keep them in
	step for ever.
	"""

	size = subroutine.domain.grouping.size(limit)

	clauses = subroutine.domain.grouping.narrowings(
		session, workspace_id=workspace_id, axis=axis, kind=kind, status_column=status_column
	)

	ordered = [key.ordering() for key in keys]

	#: Every group's rows and its account of itself, gathered before anything is rendered.
	#:
	#: **Rendered once for the whole answer rather than once per group** — the first version did
	#: it inside the loop, which is four vocabulary loads where a listing does one. A
	#: `Vocabulary` is not a lookup table: it carries the readiness of the rows it was built
	#: for, so `blocked_among`, `blocking_among` and `finished_underneath_among` were each run
	#: per group. Measured at 39 statements against an ungrouped listing's 23, and 25 of the
	#: difference was this.
	found: list[tuple[str, list[typing.Any], bool, int | None]] = []

	for group in subroutine.domain.grouping.keys_for(axis, kind=kind):
		within = statement.where(clauses[group])

		# **Counted before the cap, and only where it was asked for**, exactly as an ungrouped
		# listing does it. ``has_more`` is what a column heading actually needs — *20, and more
		# hidden* — and it costs one extra row; a total costs a scan per group, which is why it
		# stays behind the flag that already governs that trade (§8.4).
		total = (
			session.scalar(
				sqlalchemy.select(sqlalchemy.func.count()).select_from(within.subquery())
			)
			if include_total
			else None
		)

		# One more than asked for, which answers *is there another page* without a count. The
		# same trick the ungrouped path uses, per group.
		rows = list(session.scalars(within.order_by(*ordered).limit(size + 1)))
		has_more = len(rows) > size
		rows = rows[:size]

		found.append((group, rows, has_more, total))

	#: **Rendered once, shaped per group**, which is two decisions rather than one.
	#:
	#: Rendering is where the vocabulary is loaded, so doing it once is the saving above.
	#: *Shaping* is per group on purpose: `format=compact` aligns its columns across the rows
	#: it is handed, and a board's columns are read one at a time — one set of widths across
	#: the whole answer would pad every column to the widest row anywhere in it.
	rendered = iter(render(session, [row for _, rows, _, _ in found for row in rows]))

	groups = []

	for group, rows, has_more, total in found:
		groups.append(
			{
				"key": group,
				# **Dumped here rather than handed back as models**, because this response is a
				# plain document: a shaped item is a line or an address or a partial object,
				# none of which is the entity the route declares.
				#
				"items": [
					item.model_dump(mode="json") if isinstance(item, pydantic.BaseModel) else item
					for item in subroutine.api.shaping.applied(
						list(itertools.islice(rendered, len(rows))), shape
					)
				],
				"page": subroutine.views.Page(
					limit=size,
					has_more=has_more,
					next_cursor=(
						subroutine.api.pagination.encode(
							secret_key, keys, rows[-1], collection=collection
						)
						if has_more and rows
						else None
					),
					total=total,
				).model_dump(mode="json"),
			}
		)

	# **A ``JSONResponse``, so FastAPI passes it through rather than validating it against the
	# route's declared collection** — the same door :func:`subroutine.api.shaping.response`
	# uses for a shaped answer, and for the same reason: this genuinely is not a collection.
	# The route goes on declaring one so the OpenAPI document describes the ordinary case,
	# which is what almost every caller receives.
	return fastapi.responses.JSONResponse(content={"group_by": axis, "groups": groups})
