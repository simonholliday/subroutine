"""The one definition of what a row's searchable text is (§9.4, item `#823`).

**An expression index is only used when the query asks for the same expression, character for
character.** A difference of one `coalesce` or one space produces an index PostgreSQL builds,
maintains and never consults — the inert-control defect this codebase has met four times, in a
form where nothing fails and the only symptom is that the thing is slow. So the expression is
written once, here, and both the index declaration and the predicate are built from it.

This module exists rather than the expression living in :mod:`subroutine.domain.search` because
the model declares the index and ``search`` imports models — putting it there would be a cycle.
It holds no policy: *whether* to use a native backend is
:data:`subroutine.config.Settings.search_backend`, and *when it is available* is
:func:`subroutine.domain.search.chosen`.

**PostgreSQL only, and that is a decision rather than a gap** — `#871`, taken with Simon on
2026-08-14. FTS5 is a virtual table, so keeping it in step needs the triggers `#823` chose
against, and §12.6b puts a SQLite database on local disk and refuses a network filesystem, so a
SQLite instance is one person's machine — the scale `#823` measured `ILIKE` as adequate for.

**`ddl_if` is what makes that safe rather than fatal.** Declared without it, ``create_all``
against SQLite fails outright with ``no such function: to_tsvector`` — measured — which would
break every SQLite instance and the whole suite on that backend. With it, the index is built on
PostgreSQL and skipped elsewhere, which also means the ordinary parameterised fixtures exercise
it on the backend that has one, rather than it existing only in a migrated database.
"""

import typing

import sqlalchemy

# **Imported for its side effect, and the side effect is load-bearing.** SQLAlchemy registers
# the argument and return types of ``to_tsvector`` and friends when this module is imported,
# and refuses to compile one constructed before that with a message naming this import. It
# happened to work in every probe because creating a PostgreSQL engine imports it first; under
# the test suite the models are built before any engine exists, and every PostgreSQL test
# errored. Nothing here references the name, which is why it is written down.
import sqlalchemy.dialects.postgresql

import subroutine.errors

#: The text search configuration, and it decides what a search *finds* rather than how fast.
#:
#: ``english`` stems, so ``seeded``, ``seed`` and ``seeding`` are one word. Measured against
#: this project's own vocabulary before it was chosen: ``priority_score`` indexes as
#: ``prioriti`` and ``score`` so either word finds it, ``domain/search.matching`` survives whole
#: as a single lexeme, ``semi-join`` is indexed three ways, and ``#867`` keeps its number.
#: Nothing that is addressed or named here is damaged by it.
CONFIGURATION = "english"

#: How an index built by this module is recognised again — by
#: :func:`subroutine.db.migrate.schema_differences`, which has to leave these alone.
#:
#: **Alembic cannot compare an expression index at all** and says so in a warning: it reflects
#: one it cannot match against the declared one, so it reports removing and re-adding the same
#: index on every run. Measured on a database migrated from scratch: four entries, two indexes.
#:
#: A marker rather than a list of names, so the exclusion is *derived* from what is declared
#: and cannot fall behind it. :func:`names` is the reader.
MARKER = "fulltext"


def names () -> frozenset[str]:
	"""Return the names of every full-text index the models declare.

	Read from the metadata rather than written out, so adding a fourth searchable table needs
	no second edit — and so the drift-check exclusion cannot name something that has stopped
	existing.
	"""

	import subroutine.db.base

	return frozenset(
		index.name
		for table in subroutine.db.base.Base.metadata.tables.values()
		for index in table.indexes
		if index.info.get(MARKER) and index.name is not None
	)


def _configuration () -> typing.Any:
	"""Return the ``regconfig`` argument, as a literal both DDL and a query render alike.

	:func:`sqlalchemy.text` rather than :func:`sqlalchemy.literal_column`, and the difference
	is not cosmetic — see :func:`document`.
	"""

	return sqlalchemy.text(f"'{CONFIGURATION}'")


def document (*columns: typing.Any) -> typing.Any:
	"""Return the text of a row: its searchable columns, joined by a space.

	**Nullable throughout.** ``description`` and ``body`` are both nullable, and one NULL in a
	concatenation makes the whole thing NULL — which would silently empty the searchable text
	of every item nobody had described. ``coalesce`` per column rather than around the join, so
	one missing field costs that field and not the row.

	**Literals rather than bind parameters**, which is not a style choice: an index stores a
	rendered expression, and a query carrying ``$1`` where the index carries ``''`` is a
	different expression to the planner. The index would be built and never used.

	**And they are :func:`sqlalchemy.text`, never :func:`sqlalchemy.literal_column`**, which
	cost an afternoon. A ``literal_column`` is a ``ColumnClause`` belonging to no table, and an
	``Index`` infers which table it is on by looking at the columns in its expression — so one
	anywhere in the tree makes that inference return nothing, the index attaches to no table,
	and ``create_all`` builds it nowhere. **Nothing fails.** The search goes on working and
	goes on being slow, which is this codebase's inert-control defect in the one form no test
	can see: only ``EXPLAIN`` can tell an index that is missing from one that is unused.
	"""

	empty = sqlalchemy.text("''")
	separator = sqlalchemy.text("' '")
	parts: list[typing.Any] = []

	for column in columns:
		if parts:
			parts.append(separator)

		parts.append(sqlalchemy.func.coalesce(column, empty))

	joined: typing.Any = parts[0]

	for part in parts[1:]:
		joined = joined + part

	return joined


def vector (*columns: typing.Any) -> typing.Any:
	"""Return the indexed form of a row's text — what the index stores and the query asks for."""

	return sqlalchemy.func.to_tsvector(_configuration(), document(*columns))


def query (terms: typing.Sequence[str]) -> typing.Any:
	"""Return the query every term must satisfy, with the last one matched as a prefix.

	**Every term is required**, which is `#620`'s rule unchanged: a search that widened to "any
	word" would answer most of the backlog and stop being usable for the question it exists to
	answer.

	**The last term is a prefix and the rest are whole words.** That is the search-as-you-type
	convention every reader already knows from somewhere else, and it is what recovers the half
	of the old behaviour worth keeping: ``curs`` finds ``cursor``, measured. What it does not
	recover is *mid*-word matching — ``ursor`` will never find ``cursor`` again — and §10.4 is
	why: substring is one of the two predicates no index can serve, so that is the trade rather
	than a shortcoming.

	Only the last term, because a prefix in the middle of a query is almost always a whole word
	somebody has finished typing, and treating it as a prefix widens the answer for nothing.

	``plainto_tsquery`` is deliberately not used: it cannot express a prefix at all, and it
	would silently drop the stopwords this builds around instead.

	**No terms is a programming error here rather than an empty search** (`#880`). A caller
	with nothing to look for has nothing to rank, so there is no expression to return that is
	not a claim: matching nothing would make a whitespace query answer *no results* under this
	backend and *every result* under ``like``, which is a divergence rather than a fix. So this
	refuses, and :func:`subroutine.domain.search.terms` is what every caller must ask before
	getting here — ``if q:`` is a truthiness test on the raw string, and ``" "`` is truthy.
	"""

	if not terms:
		raise subroutine.errors.InternalError(
			"A search ranking was asked for with no words to rank against.",
			hint="Test search.terms(q) rather than q — a query of spaces is truthy and has "
			"no words in it.",
		)

	lexemes = [sqlalchemy.func.plainto_tsquery(_configuration(), term) for term in terms[:-1]]
	last = sqlalchemy.func.to_tsquery(
		_configuration(),
		sqlalchemy.func.quote_literal(terms[-1]).op("||")(sqlalchemy.literal_column("':*'")),
	)

	combined: typing.Any = lexemes[0] if lexemes else last

	for lexeme in lexemes[1:]:
		combined = combined.op("&&")(lexeme)

	return combined if not lexemes else combined.op("&&")(last)


def matches (
	terms: typing.Sequence[str], *columns: typing.Any
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate this index can serve: does this row's text answer this query?"""

	return typing.cast(
		sqlalchemy.ColumnElement[bool], vector(*columns).op("@@")(query(terms))
	)


#: What an exact identifier match is worth, against everything else.
#:
#: ``ts_rank`` returns a small float — under 1 for ordinary prose — so any number comfortably
#: above that puts the named item first and nothing else can reach it. **This is where `#867`'s
#: "the exact hit comes first" actually lives**, and it belongs here rather than as an ordering
#: prefix: in a search backend an exact identifier match is not a special case, it is simply
#: the best possible hit. Driven on the served instance before this existed, `815` returned
#: `#815` **sixth**, below the fold of an agent's default page.
EXACT_MATCH_RANK = 1000.0


def rank (
	terms: typing.Sequence[str], *columns: typing.Any, ref: typing.Any = None, numbered: int | None = None
) -> typing.Any:
	"""Return how well this row answers the query, for ``-relevance`` to sort by.

	**Two terms, and only one of them is about text.** ``ts_rank`` scores the prose; the ref
	comparison is what makes a query that *is* a number answer with that item rather than with
	whatever happens to mention those digits. `#867` shipped the predicate and deliberately left
	the ordering to this item, because a per-query sort value a keyset cursor can resume from is
	one piece of machinery and building it twice was the thing to avoid.

	``numbered`` is ``None`` for a query that is not a ref, which is the ordinary case, and the
	whole term drops out rather than being compared against nothing.
	"""

	# **Cast to double precision, and this is not tidiness — it is what makes a cursor work.**
	# ``ts_rank`` returns ``float4``. A keyset cursor carries the sort value out to the client
	# and compares it on the way back, so the value has to survive that round trip *exactly*:
	# psycopg renders the float4 as text, Python parses that decimal into a float8, and the
	# nearest float8 to the decimal `0.075990885` is **not** the float4 promoted to float8. So
	# ``relevance = <what the cursor carried>`` is false, the seek predicate matches nothing,
	# and the second page of every search comes back **empty**.
	#
	# Measured exactly that way before this cast existed: page one returned three rows, page
	# two returned none, and `has_more` said there were more. In float8 both sides are the same
	# width and the round trip is lossless. `#46`'s defect in a new disguise — a sort value the
	# cursor cannot name — and the reason the test for this pages past the limit.
	scored = sqlalchemy.cast(
		sqlalchemy.func.ts_rank(vector(*columns), query(terms)), sqlalchemy.Double
	)

	if ref is None or numbered is None:
		return scored

	return scored + sqlalchemy.case((ref == numbered, EXACT_MATCH_RANK), else_=0.0)


def index (
	name: str, *columns: typing.Any
) -> sqlalchemy.Index:
	"""Return the GIN index over these columns, built on PostgreSQL and skipped elsewhere.

	``ddl_if`` rather than a dialect check in a migration, so that ``create_all`` — which is how
	every test builds its schema — produces the index on PostgreSQL. Without that the native
	backend would exist only in a migrated database and no ordinary test could reach it.

	**The attachment is asserted rather than assumed**, and that check is the whole reason this
	is a function. An ``Index`` works out which table it belongs to from the columns in its
	expression; get one element of that expression wrong and it belongs to none, so nothing
	ever builds it — and *nothing fails*, because the search still works and is merely slow.
	One line here turns the afternoon that cost into an import-time error.
	"""

	built = sqlalchemy.Index(
		name, vector(*columns), postgresql_using="gin", info={MARKER: True}
	).ddl_if(dialect="postgresql")

	if built.table is None:
		raise ValueError(
			f"{name} attached to no table, so nothing will ever build it. An expression "
			f"containing a literal_column has no table for an Index to infer one from; use "
			f"sqlalchemy.text for literals instead."
		)

	return built
