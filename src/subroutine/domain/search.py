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

**A query that is exactly a ref finds that item too** — `#867`, 2026-08-14. A ref is this
product's primary address: it is in every commit message, every comment and every sentence
anybody writes about an item, and it was the one address the search box could not resolve.
Measured across ten refs before this was built, the item itself was **absent in ten of ten**,
while four to sixty unrelated rows matched the digits as text — because ``7`` appears inside
``17``, inside ``#755`` and inside every ``2026-08-07`` on the instance.

That measurement is also why the *ordering* half is not here. With sixty noise rows, an exact
match that is merely present is not findable, so "the exact hit first" is the feature rather
than a refinement of it — and it cannot be an ``ordering.ORDERINGS`` entry, because those are
a static map from name to ``Sortable`` and this expression depends on the query. It belongs
with ``-relevance`` in `#823`, which has to build a per-query sort value a cursor can resume
from anyway; an exact identifier match is simply the highest-scoring hit, which is what a
search backend does with one. Building a bespoke prefix here and a general one there would be
two implementations of one rule, chosen deliberately against.

Two things it deliberately still does not do, both filed rather than half-built: ranking a
title match above a body one, and stemming so that ``seeded`` and ``seed`` agree. The first
changes an ordering that keyset pagination has to be able to resume from; the second needs the
v2 backend §9.4 already designs.
"""

import typing

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.domain.refs
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


def matching (
	query: str, *columns: Searchable, ref: Searchable | None
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching every word of this query, across these columns.

	**Every term must appear; each may appear in any of the columns.** So a query naming one
	word from the title and one from the description finds the row — which is the ordinary
	shape of half-remembering something, and the case `#620` found returning nothing.

	**A query that is exactly a ref also matches the item with that number** — `#867`, and
	``ref`` is the column to compare against. A ref is how this product addresses everything:
	it is in every commit message, every comment and every sentence anybody writes about an
	item, and until now it was the one address the search box could not resolve. Measured
	across ten refs before this was built, the item itself was **absent in ten of ten**, while
	four to sixty unrelated rows matched the digits as text.

	**Both readings are kept, rather than one replacing the other.** ``862`` may be the item
	and may equally be a number somebody wrote in a description, and neither is the obviously
	intended one. Which of them appears *first* is an ordering question and deliberately not
	answered here — see below.

	``ref`` is **keyword-only and has no default**, so a new caller has to say what it means
	rather than inherit silence. ``None`` is a legitimate answer for anything searched that
	has no ref of its own; passing it is a decision, and omitting it is now impossible.

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

	text = sqlalchemy.and_(
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

	if ref is None:
		return text

	# The whole query, not one of its words. ``parse_ref`` is anchored at both ends, so this
	# asks "is the entire search a ref" — which keeps `862 pagination` a text search rather
	# than turning any query that happens to contain a number into a lookup. It is also the
	# one place the spelling is decided, so `#862` and `862` agree here with every other
	# surface, and `007` resolves nowhere.
	numbered = subroutine.domain.refs.parse_ref(query)

	if numbered is None:
		return text

	return sqlalchemy.or_(text, ref == numbered)


def in_a_comment (
	query: str, *, subject: Searchable, entity_type: str
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching a readable comment written on this item.

	**Comments are the largest body of prose here after the event feed** — 780 of them against
	695 tasks when `#825` measured it — and they are the only place the running record lives,
	which §5.10 makes their whole job. Until `#83` they were unsearched, so two searches for
	sentences that exist only in a comment both answered *nothing matches*, on the one path
	built to stop an agent filing a duplicate.

	**The visibility objection that kept them out was already answered in code.** §9.4 said a
	comment is "a new visibility surface", because *this item matched* would be evidence that a
	sentence exists which the searcher may not be able to read. There is no such sentence: a
	comment has no visibility of its own and is reachable exactly when its subject is, which
	:func:`subroutine.domain.comments.get` and :mod:`subroutine.domain.scoping` both say in as
	many words. So the leak cannot occur — if the item is visible, every comment on it is
	readable. This clause narrows an item statement that is already scoped, and adds no reach.

	**Deletion is the one real rule, and it is inherited rather than invented.** A search that
	matched a soft-deleted comment would surface prose nobody can open, which is exactly what
	``comments.listing`` refuses and what the mention wiring already decided for the same
	reason: a backlink pointing at a sentence nobody can read is worse than none. That filter
	is stated here as well as there, so a test drives both rather than trusting them to agree.

	**A correlated ``EXISTS``, and measured before it was chosen.** At 2,000 tasks each
	carrying a comment, this took a no-match search from 1.6x an unordered page to 3.3x on
	SQLite and from 6.3x to 11.1x on PostgreSQL — roughly double, which is linear in the prose
	added and well inside ``test_query_cost``'s ceiling. It is **not** `#856`'s shape, and the
	difference is worth holding onto: that was a correlated subquery in ``ORDER BY``, which
	must be computed for every row in the table before the database knows which page to return,
	so ``LIMIT`` cannot help. In ``WHERE`` the same syntax short-circuits and both planners
	turn it into a semi-join.

	``ref=None`` on the inner match, deliberately: a comment has no ref of its own, and the
	number in ``#42`` written inside one is a mention rather than an address for it.
	"""

	model = subroutine.db.models.activity.Comment

	return (
		sqlalchemy.select(sqlalchemy.literal(1))
		.select_from(model)
		.where(
			model.entity_type == entity_type,
			model.entity_id == subject,
			model.deleted_at.is_(None),
			matching(query, model.body, ref=None),
		)
		.exists()
	)
