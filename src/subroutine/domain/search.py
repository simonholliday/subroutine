"""Free-text search, in the one form both transports have to agree on (SPEC.md §9.4).

§9.4 says ``q`` searches **title and description**, and until 2026-07-31 it searched the title
alone — on both entities, on both transports, with the endpoint's own OpenAPI description
saying "Match this text in the title" and so documenting the defect rather than the intent.
Nobody could see it: a search that returns fewer rows than it should returns *plausible* rows,
and the ones it drops are the ones nobody knew to look for.

That mattered here more than it would in most projects. This instance holds its own planning,
and its reasoning lives in descriptions and document bodies — searching this backlog for
"pagination" returned nothing at all, while four items explained the cursor defect at length.

**A query is a set of words, all of which must appear** — item `#620`, and until 2026-08-08 it
was one contiguous ordered substring instead. ``vocabulary entries`` matched because those two
words happened to be adjacent and in that order; ``vocabulary seeded`` found nothing, though
both words were four words apart in the same item, and ``entries vocabulary`` found nothing
because it was reversed.

**It failed in the direction that costs something.** An empty result is indistinguishable from
"this does not exist", so the caller does what the empty result implies and files a duplicate —
on the one path that exists to prevent duplicates. The skill sends an agent here *before*
creating anything and promises that "a half-remembered phrase from a description will find it";
half-remembered reproduces the right words, not their adjacency and order, which was precisely
the input that returned nothing. Two of those duplicates were nearly filed against this backlog
by an agent that did exactly as it was told.

The tool schema had said ``"Words to look for"`` since it was written. One rule stated in two
places, agreeing in prose and disagreeing in code, which is this codebase's signature defect.

**The v1 implementation is deliberately ``ILIKE '%…%'`` and that is a decision, not an
oversight.** §9.4 designs the interface for replacement — SQLite FTS5 and PostgreSQL
``tsvector`` in v2, chosen by configuration, with ``-relevance`` becoming a sort key only when
a real backend is active. What that buys now is honesty about the cost: a substring match
cannot use an index (§10.4 lists it as one of the two predicates that cannot), so this is
adequate at personal scale and will not stay adequate. Writing it down is what stops the
default ossifying into a decision nobody remembers taking.

Two things it deliberately still does not do, both filed rather than half-built: ranking a
title match above a body one, and stemming so that ``seeded`` and ``seed`` agree. The first
changes an ordering that keyset pagination has to be able to resume from; the second needs the
v2 backend §9.4 already designs.
"""

import typing

import sqlalchemy
import sqlalchemy.orm

import subroutine.errors


def escaped (value: str) -> str:
	"""Escape a caller's text for use inside a LIKE pattern.

	Without this, a search for ``50%`` matches everything and a search for ``a_b`` matches
	``axb`` — surprising, and on a large table an accidental full scan.
	"""

	return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


#: A column a search may look in. The union rather than ``ColumnElement`` alone, for the same
#: reason ``ordering.Sortable`` is one: a mapped attribute is a ``ColumnElement`` at runtime,
#: and mypy will not accept ``InstrumentedAttribute[str]`` where ``ColumnElement[Any]`` is
#: asked for, because the parameter is invariant.
Searchable = sqlalchemy.orm.InstrumentedAttribute[typing.Any] | sqlalchemy.ColumnElement[typing.Any]


#: The most words one query may ask for. Each term is its own unindexable substring scan
#: (§10.4), so a pasted paragraph is real work per row — and refusing is better than quietly
#: using the first few, because a search that silently narrows differently from what was asked
#: is `#620` again in the other direction.
MAX_TERMS = 16


def terms (query: str) -> list[str]:
	"""Split a search into the words it is asking for.

	``split()`` with no argument, so any run of whitespace separates and leading or trailing
	space contributes nothing — which means a query of spaces alone is *no words*, and the
	caller can tell that from an empty list rather than by inspecting the string itself.
	"""

	return query.split()


def matching (query: str, *columns: Searchable) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching every word of this query, across these columns.

	**Every term must appear; each may appear in any of the columns.** So a query naming one
	word from the title and one from the description finds the row — which is the ordinary
	shape of half-remembering something, and the case `#620` found returning nothing.

	``ilike`` rather than ``like``, always and on every column: SQLite's ``LIKE`` is
	case-insensitive for ASCII and PostgreSQL's is not, so an unqualified one is a filter that
	quietly behaves differently depending on where it runs (§10.3).

	**A null column contributes nothing rather than swallowing the row.** ``description`` and
	``body`` are both nullable, and ``NULL ILIKE '%x%'`` is NULL, not false — inside an ``OR``
	that is harmless, which is the reason this is spelled as one and worth knowing before
	somebody rewrites it as a ``NOT``.

	**A query with no words in it narrows nothing**, rather than searching for the empty
	string. Before this it searched for whatever was typed, so ``q=" "`` was a real filter
	matching every row containing a space — a filter nobody asked for, answering a question
	nobody put.
	"""

	wanted = terms(query)

	if not wanted:
		return sqlalchemy.true()

	if len(wanted) > MAX_TERMS:
		raise subroutine.errors.ValidationError(
			f"That search asks for {len(wanted)} words, and {MAX_TERMS} is the most it can "
			f"look for at once.",
			errors=[
				subroutine.errors.FieldError(
					field="q",
					code="invalid_field_value",
					message=f"{len(wanted)} words were given.",
					hint="Every word has to appear, so a longer search finds less rather "
					"than more. Use the few most distinctive words instead.",
				)
			],
			hint="Every word has to appear, so a longer search finds less rather than more. "
			"Use the few most distinctive words instead.",
		)

	return sqlalchemy.and_(
		*[
			sqlalchemy.or_(
				*[
					column.ilike(f"%{escaped(term)}%", escape="\\")
					for column in columns
				]
			)
			for term in wanted
		]
	)
