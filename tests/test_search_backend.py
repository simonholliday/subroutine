"""The indexed search backend: that it is chosen correctly, and that it is actually used.

**The test that matters here reads a query plan, and it is the only one that can.** `#871`'s
first working version was measured at 524 ms and a sequential scan, because the index attached
to no table and `create_all` had built nothing. Every functional test passed throughout — the
search worked, it was merely slow — which is this codebase's inert-control defect in the form
`#855` was written about: a fixture holds a handful of rows, so nothing about cost is visible
until somebody looks at a plan.

So :func:`test_a_native_search_is_served_by_the_index` is the guard, and the rest of this file
is about the choice between backends and the behaviour that differs between them.
"""

import typing

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.config
import subroutine.db.fulltext
import subroutine.db.migrate
import subroutine.db.models.work
import subroutine.domain.bootstrap
import subroutine.domain.search
import subroutine.domain.tasks


def _postgresql_only (session: sqlalchemy.orm.Session) -> None:
	"""Skip where there is no native backend to test — SQLite, by `#871`'s decision."""

	if session.get_bind().dialect.name != "postgresql":
		pytest.skip("the native backend exists on PostgreSQL only")


def _settings (backend: str) -> subroutine.config.Settings:
	"""Return settings asking for a backend, without touching the machine's own."""

	return subroutine.config.Settings(search_backend=backend)


def test_asking_for_a_backend_that_is_not_there_is_not_an_error (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**A request that cannot be served falls back and is published, rather than refused.**

	§9.4's *"agents learn which is available from /v1/meta"* is only worth anything if a caller
	can be told *no* without being refused — an operator who writes ``native`` into a SQLite
	instance's configuration has not made a mistake, they have asked for something that is not
	there. So this reports what is in force rather than raising.
	"""

	chosen = subroutine.domain.search.chosen(
		session, settings=_settings(subroutine.domain.search.NATIVE)
	)

	if session.get_bind().dialect.name == "postgresql":
		assert chosen == subroutine.domain.search.NATIVE

	else:
		assert chosen == subroutine.domain.search.LIKE, "SQLite has no native backend"


def test_the_default_is_the_implementation_every_instance_already_had (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A fresh instance needs nothing configured and behaves exactly as it always has."""

	assert subroutine.config.Settings().search_backend == subroutine.domain.search.LIKE
	assert (
		subroutine.domain.search.chosen(session, settings=subroutine.config.Settings())
		== subroutine.domain.search.LIKE
	)


def test_a_native_search_is_served_by_the_index (session: sqlalchemy.orm.Session) -> None:
	"""**The guard the afternoon of 2026-08-14 was spent earning.**

	An expression index is used only where the query renders the same expression character for
	character, and an ``Index`` whose expression contains a ``literal_column`` belongs to no
	table and is therefore built by nothing. Both failures produce a working, slow search and
	no test failure anywhere. ``EXPLAIN`` is the only instrument that separates an index that
	is missing from one that is merely unused.
	"""

	_postgresql_only(session)

	model = subroutine.db.models.work.Task
	predicate = subroutine.db.fulltext.matches(
		["quinsy", "fenestration"], model.title, model.description
	)
	statement = sqlalchemy.select(model.id).where(predicate)
	compiled = str(
		statement.compile(session.get_bind(), compile_kwargs={"literal_binds": True})
	)

	# **The question is whether the index *can* serve this query, not whether the planner
	# prefers it here.** A fixture holds a handful of rows, where a sequential scan is genuinely
	# cheaper and PostgreSQL is right to choose one — so without this the guard would assert
	# something about table size and fail on a correct index. Turning sequential scans off
	# makes the planner use the index if it is able to; where the expression does not match, it
	# has no alternative and scans anyway, which is exactly the case being caught.
	session.execute(sqlalchemy.text("SET LOCAL enable_seqscan = off"))

	plan = "\n".join(
		row[0] for row in session.execute(sqlalchemy.text("EXPLAIN " + compiled))
	)

	assert "ix_task_search" in plan, (
		f"The full-text index was not used, so every search is a scan of every row's prose. "
		f"Check the index attaches to a table and that its expression matches the query's "
		f"exactly.\n{plan}"
	)


def test_every_excluded_index_is_one_a_database_really_has (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The stale-entry half of the drift-check exclusion — what makes the entry go away.

	``migrate._include_object`` stops comparing these because Alembic cannot compare an
	expression index at all. That exclusion could otherwise cover an index **nothing builds**,
	which is precisely the failure it would then hide and the one that costs a scan per search.

	Derived from the models rather than listed, so a fourth searchable table is covered by
	having been declared and a removed one stops being excused without anybody remembering.
	"""

	_postgresql_only(session)

	declared = subroutine.db.fulltext.names()

	assert declared, "nothing is excluded, so this test is asserting about an empty set"

	present = {
		row[0]
		for row in session.execute(
			sqlalchemy.text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
		)
	}

	assert declared <= present, (
		f"{sorted(declared - present)} are excused from the drift check and do not exist. "
		f"An exclusion covering an index nothing builds hides the one failure it was written "
		f"to tolerate."
	)


@pytest.fixture
def searchable (session: sqlalchemy.orm.Session) -> typing.Any:
	"""One task whose prose is worth asking different questions about."""

	setup = subroutine.domain.bootstrap.initialise(
		session, username="si", instance_name="Search"
	)
	task = subroutine.domain.tasks.create(
		session,
		project=setup.inbox,
		title="The cursor is decoded wrongly",
		description="Seeded vocabulary, and pagination resumes from the wrong row.",
		actor=None,
	)
	session.flush()

	return task


@pytest.mark.parametrize(
	("query", "native", "like"),
	[
		("cursor", True, True),
		("curs", True, True),
		("ursor", False, True),
		# `like` finds this one by accident rather than by stemming: "seed" is a substring of
		# "Seeded". The pair below is the honest test of stemming, because "paginate" is a
		# substring of nothing in the row.
		("seed", True, True),
		("seeded", True, True),
		("paginate", True, False),
		("cursor pagination", True, True),
		("quinsy", False, False),
	],
)
def test_what_each_backend_finds (
	session: sqlalchemy.orm.Session,
	searchable: typing.Any,
	query: str,
	native: bool,
	like: bool,
) -> None:
	"""**The two do not find the same things, and this is the table of how they differ.**

	Written out as a table rather than described, because `#871` made the trade deliberately
	and the changelog claims it in prose: the native backend **stems**, so ``seed`` finds
	``Seeded`` and ``paginate`` finds ``pagination``; it matches a **trailing prefix**, so
	``curs`` finds ``cursor``; and it cannot match **mid-word**, so ``ursor`` never will
	(§10.4). The ``like`` backend is the mirror of all three.
	"""

	model = subroutine.db.models.work.Task
	expected = {subroutine.domain.search.LIKE: like}

	if session.get_bind().dialect.name == "postgresql":
		expected[subroutine.domain.search.NATIVE] = native

	for backend, wanted in expected.items():
		found = session.scalars(
			sqlalchemy.select(model.id).where(
				subroutine.domain.search.matching(
					query, model.title, model.description, ref=None, backend=backend
				)
			)
		).all()

		assert (searchable.id in found) is wanted, (
			f"{backend} searching {query!r} should "
			f"{'find' if wanted else 'not find'} the row and did not agree"
		)
