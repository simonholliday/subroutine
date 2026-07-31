"""Free-text search, in the one form both transports have to agree on (SPEC.md §9.4).

§9.4 says ``q`` searches **title and description**, and until 2026-07-31 it searched the title
alone — on both entities, on both transports, with the endpoint's own OpenAPI description
saying "Match this text in the title" and so documenting the defect rather than the intent.
Nobody could see it: a search that returns fewer rows than it should returns *plausible* rows,
and the ones it drops are the ones nobody knew to look for.

That mattered here more than it would in most projects. This instance holds its own planning,
and its reasoning lives in descriptions and document bodies — searching this backlog for
"pagination" returned nothing at all, while four items explained the cursor defect at length.

**The v1 implementation is deliberately ``ILIKE '%…%'`` and that is a decision, not an
oversight.** §9.4 designs the interface for replacement — SQLite FTS5 and PostgreSQL
``tsvector`` in v2, chosen by configuration, with ``-relevance`` becoming a sort key only when
a real backend is active. What that buys now is honesty about the cost: a substring match
cannot use an index (§10.4 lists it as one of the two predicates that cannot), so this is
adequate at personal scale and will not stay adequate. Writing it down is what stops the
default ossifying into a decision nobody remembers taking.
"""

import typing

import sqlalchemy
import sqlalchemy.orm


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


def matching (term: str, *columns: Searchable) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching this text anywhere in any of these columns.

	``ilike`` rather than ``like``, always and on every column: SQLite's ``LIKE`` is
	case-insensitive for ASCII and PostgreSQL's is not, so an unqualified one is a filter that
	quietly behaves differently depending on where it runs (§10.3).

	**A null column contributes nothing rather than swallowing the row.** ``description`` and
	``body`` are both nullable, and ``NULL ILIKE '%x%'`` is NULL, not false — inside an ``OR``
	that is harmless, which is the reason this is spelled as one and worth knowing before
	somebody rewrites it as a ``NOT``.
	"""

	pattern = f"%{escaped(term)}%"

	return sqlalchemy.or_(*[column.ilike(pattern, escape="\\") for column in columns])
