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

**One exception, and it belongs to the indexed backend alone** (`#885`): a stopword — ``the``,
``of``, ``and`` — narrows nothing under ``native``, because PostgreSQL's text search drops them
before the query is built. So ``cursor the`` finds what ``cursor`` finds there and nothing at
all under ``like``. Accepted rather than refused, Simon's decision of 2026-08-14, on the grounds
that refusing would make the two backends differ in a second way; ``db/fulltext.query`` carries
the mechanism and the changelog carries the warning.

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

import subroutine.config
import subroutine.db.fulltext
import subroutine.db.models.activity
import subroutine.domain.refs
import subroutine.errors

#: What answers ``q`` when nothing better is available, and what every instance had until
#: `#823`. Named rather than spelled `"like"` at each site, so the two implementations are a
#: closed vocabulary a guard can read.
LIKE = "like"

#: The indexed implementation. Available on PostgreSQL and nowhere else — `#871`.
NATIVE = "native"


def chosen (session: sqlalchemy.orm.Session, *, settings: "subroutine.config.Settings | None" = None) -> str:
	"""Return which implementation will actually answer, for this session.

	**Two things have to agree and only one of them is a preference.** ``search_backend`` says
	what the operator asked for; the dialect says what is possible. Asking for ``native`` on
	SQLite is **not an error** — there is nothing wrong with the request, the backend simply is
	not there — so it falls back and ``GET /v1/meta`` publishes what is in force. §9.4 designed
	that channel for exactly this: *"agents learn which is available from /v1/meta"*, which is
	only worth anything if a caller can be told *no* without being refused.

	Read from the session rather than from configuration alone, because the two can disagree:
	a served instance may be bound to a database the configured URL does not name, and
	``db/backup.py`` has the same rule for the same reason after branching on ``settings``
	sent ``VACUUM INTO`` at PostgreSQL.
	"""

	wanted = (settings or subroutine.config.load_settings()).search_backend

	if wanted != NATIVE:
		return LIKE

	bind = session.get_bind()

	return NATIVE if bind.dialect.name == "postgresql" else LIKE


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
	query: str, *columns: Searchable, ref: Searchable | None, backend: str = LIKE
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

	# **One expression against the whole row's text, or one substring test per column.** The
	# two agree about which rows match a whole word and disagree about everything else, which
	# is the trade `#871` records: the native one stems and matches a trailing prefix, the
	# `like` one matches any substring and can never be served by an index (§10.4).
	text = (
		subroutine.db.fulltext.matches(wanted, *columns)
		if backend == NATIVE
		else sqlalchemy.and_(
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


def anywhere (
	query: str,
	*,
	identity: Searchable,
	columns: typing.Sequence[Searchable],
	ref: Searchable | None,
	entity_type: str,
	backend: str = LIKE,
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate matching this query in an item's own prose or in a comment on it.

	**One function rather than an ``or_`` written out at four call sites, and `#892` is why
	that mattered.** The four spelled the composition themselves, and the composition was the
	defect: an ``OR`` between an indexable predicate and a subquery costs the index on *both*
	sides. PostgreSQL hashes the subplan and then has no index route for the other branch, so a
	search that reads comments fell back to a sequential scan computing ``to_tsvector`` over
	every row — while the comment half, which is what I first blamed, was index-served and cost
	0.569 ms.

	**Each half is resolved to a set of ids and the two are unioned**, so each uses its own
	index. Read from ``EXPLAIN (ANALYZE)`` at 2,000 tasks rather than reasoned about, and
	strictly better on every backend — there is nothing here to trade:

	========================  ==========  =========
	                          ``or_``     this
	========================  ==========  =========
	PostgreSQL, ``native``    161.79 ms   0.42 ms
	PostgreSQL, ``like``       26.60 ms   25.98 ms
	SQLite                      5.79 ms   3.61 ms
	========================  ==========  =========

	**The ref match stays outside the union**, because it is a primary-key comparison that no
	index needs help with — and because it is about the *query being an address* rather than
	about prose, which is `#867`'s whole distinction.

	**Neither half is narrowed by visibility and neither needs to be.** The statement this
	clause lands in is already scoped by ``domain.scoping``, so an id the caller cannot see
	simply matches nothing; §5.11a makes a comment exactly as visible as its subject. Narrowing
	twice would cost a join per half and change no answer, which `#815` measured and recorded.
	"""

	found = identity.in_(
		sqlalchemy.select(identity)
		.where(matching(query, *columns, ref=None, backend=backend))
		.union(in_a_comment(query, entity_type=entity_type, backend=backend))
	)

	if ref is None:
		return found

	numbered = subroutine.domain.refs.parse_ref(query)

	if numbered is None:
		return found

	return sqlalchemy.or_(found, ref == numbered)


def in_a_comment (
	query: str, *, entity_type: str, backend: str = LIKE
) -> sqlalchemy.Select[tuple[typing.Any]]:
	"""Return the ids of items with a readable comment on them matching this query.

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

	**A set of ids rather than a correlated ``EXISTS``, and `#892` is what changed it.** This
	was an ``EXISTS`` narrowed by ``entity_id``, which :func:`anywhere` then ``OR``-ed with the
	item's own prose — and *that* ``OR`` was the defect: a hashed subplan on one side of one
	stops PostgreSQL using an index for the other, so a search that reads comments fell back to
	a sequential scan computing ``to_tsvector`` over every task. **161.79 ms against 0.42 ms**,
	measured at 2,000 tasks with `EXPLAIN (ANALYZE)` read rather than reasoned about.

	Answering with ids instead lets :func:`anywhere` union the two halves, and each half then
	uses its own index. Nothing here is correlated any more, so this runs **once per statement**
	rather than once per candidate row.

	``ref=None`` on the inner match, deliberately: a comment has no ref of its own, and the
	number in ``#42`` written inside one is a mention rather than an address for it.
	"""

	model = subroutine.db.models.activity.Comment

	return sqlalchemy.select(model.entity_id).where(
		model.entity_type == entity_type,
		model.deleted_at.is_(None),
		matching(query, model.body, ref=None, backend=backend),
	)
