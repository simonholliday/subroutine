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

import ast
import pathlib
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
		# **A stopword narrows nothing under the index** — `#885`. `plainto_tsquery('english',
		# 'of')` is the empty tsquery and `empty && x` is `x`, so this asks for `cursor` alone;
		# `like` requires the substring and the row has no "of" in it. Chosen for that: "the"
		# and "and" both appear in the fixture's own prose, so either would be found by both
		# and prove nothing.
		("cursor of", True, False),
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

	**And a stopword narrows nothing under the index** (`#885`), which is the fourth difference
	and was the one nowhere written down — so the module claimed twice that every term is
	required while this backend quietly dropped some. Accepted rather than refused, Simon's
	decision of 2026-08-14; the row below is what stops it being accepted *silently*.
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


#: The tree the guard below walks. **Absolute, derived from this file**, because an autouse
#: fixture chdirs every test into a temporary directory (`tests/conftest.py`) — a relative
#: `Path("src")` finds nothing there, and a scan that reads nothing passes exactly like one
#: that found nothing. That is how the first version of this guard shipped inert.
SOURCE = pathlib.Path(__file__).resolve().parent.parent / "src"


def _resolving_the_backend (root: pathlib.Path) -> tuple[list[str], list[str]]:
	"""Return every ``search.chosen()`` call under ``root``, split into with and without.

	**Takes the tree as an argument so a synthetic offender can reach the real code** — `#405`.
	A can-fire test that re-implements the rule inline proves the rule and leaves the *scan*
	unchecked, which is that item's recorded blind spot and is what the first version of this
	did.

	Read from the AST rather than by grepping, so a call spread over several lines is seen.
	"""

	passing: list[str] = []
	bare: list[str] = []

	for path in sorted(root.rglob("*.py")):
		for node in ast.walk(ast.parse(path.read_text())):
			if not isinstance(node, ast.Call):
				continue

			if not isinstance(node.func, ast.Attribute) or node.func.attr != "chosen":
				continue

			where = f"{path.name}:{node.lineno}"
			found = passing if any(k.arg == "settings" for k in node.keywords) else bare
			found.append(where)

	return passing, bare


def test_every_caller_asks_which_backend_with_its_own_settings () -> None:
	"""**`#883`. A call that omits `settings=` resolves from the ambient environment.**

	`search.chosen()` falls back to `config.load_settings()`, which builds a fresh `Settings`
	from the environment and the config file every time — it is not cached. So a call site that
	omits it answers with whatever the *process* is configured for rather than with what *this
	application* was built with, and an instance can then use two backends at once: three of the
	six sites did, and tasks ranked while documents did not.

	**A structural guard rather than a behavioural one, deliberately.** The behaviour is only
	observable where an application's settings differ from the environment, which is a shape
	only the suite produces — so a test per site would be a list that falls behind, where this
	covers the seventh call before anybody writes it. `test_actor_discipline` is the precedent:
	the check is that the argument is *passed*, at every site, by walking the tree.

	**The floor is the half that makes it worth anything.** Without it a scan that read nothing
	reports no offenders, which is byte-identical to a clean tree — and that is not a
	hypothetical here, it is how this guard was written the first time.
	"""

	passing, bare = _resolving_the_backend(SOURCE)

	assert len(passing) + len(bare) >= 6, (
		f"only {len(passing) + len(bare)} calls to search.chosen() were found under {SOURCE}, "
		f"and there are at least six — so this walked the wrong tree and is checking nothing"
	)

	assert not bare, (
		f"{bare} call search.chosen() without settings, so they resolve the backend from the "
		f"environment rather than from the application — and one instance can then answer with "
		f"two backends at once"
	)


def test_the_backend_guard_can_see_a_call_that_omits_settings (tmp_path: pathlib.Path) -> None:
	"""The guard above, fed a defect **through its own scanner** — `#405`.

	Both shapes in one file, so what is proved is that the walk separates them rather than that
	a rule written out again in this test does.
	"""

	tmp_path.joinpath("offender.py").write_text(
		"import subroutine.domain.search\n"
		"subroutine.domain.search.chosen(session)\n"
		"subroutine.domain.search.chosen(\n\tsession, settings=settings\n)\n"
	)

	passing, bare = _resolving_the_backend(tmp_path)

	assert len(bare) == 1, f"the walk did not find the offending call: {bare}"
	assert len(passing) == 1, f"the walk did not find the correct call: {passing}"


def test_a_title_match_outranks_a_body_match (session: sqlalchemy.orm.Session) -> None:
	"""`#624`. The indexed text was one string, so ranking was frequency and density alone.

	Measured on this project's own instance before this: searching for ``seeded`` put the item
	titled *A search for 'seeded' finds 'seed'* **fifth**, below three body matches and a 97 KB
	specification. A term in a title is the stronger signal and the index could not say so.

	**The fixture has to make the two genuinely disagree**, which is `#1013`'s recorded trap:
	equal scores fall through to the ``ref`` tiebreak, so a seed where the title match happens
	to have the lower ref would satisfy this either way. So the body match is written first —
	it takes the lower ref — and mentions the term four times, which is what wins under
	frequency alone and loses under weights.
	"""

	_postgresql_only(session)

	setup = subroutine.domain.bootstrap.initialise(session, username="si", instance_name="Test")
	wordy = subroutine.domain.tasks.create(
		session,
		project=setup.inbox,
		title="Notes from the migration",
		description="pagination pagination pagination pagination",
		actor=None,
	)
	named = subroutine.domain.tasks.create(
		session,
		project=setup.inbox,
		title="Pagination resumes from the wrong cursor row",
		actor=None,
	)

	session.flush()

	assert wordy.ref < named.ref, "the tiebreak favours the body match, or this proves nothing"

	model = subroutine.db.models.work.Task
	scored = subroutine.db.fulltext.rank(
		["pagination"], model.title, model.description, ref=None, numbered=None
	)
	ordered = list(
		session.execute(
			sqlalchemy.select(model.ref, scored.label("relevance"))
			.where(
				subroutine.domain.search.matching(
					"pagination",
					model.title,
					model.description,
					ref=None,
					backend=subroutine.domain.search.NATIVE,
				)
			)
			.order_by(sqlalchemy.desc("relevance"), model.ref)
		)
	)

	assert [row.ref for row in ordered] == [named.ref, wordy.ref], (
		f"a body mentioning it four times outranks the title it is about: {ordered}"
	)


def test_weighting_changes_the_order_and_not_which_rows_match (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The half that makes `#624` safe to ship, and it is why no data is rewritten.

	``@@`` ignores weights entirely, so the same search finds the same items and only their
	order moves. Without this the migration would be a change to what a search *answers*, which
	is a different and much larger claim — the kind `#871` says belongs in the changelog rather
	than arriving as a performance note.
	"""

	_postgresql_only(session)

	setup = subroutine.domain.bootstrap.initialise(session, username="si", instance_name="Test")

	for title, description in (
		("Pagination resumes wrongly", None),
		("Notes", "pagination is the thing"),
		("Nothing to do with it", "something else entirely"),
	):
		subroutine.domain.tasks.create(
			session,
			project=setup.inbox,
			title=title,
			description=description,
			actor=None,
		)

	session.flush()

	model = subroutine.db.models.work.Task
	found = session.scalars(
		sqlalchemy.select(model.ref).where(
			subroutine.domain.search.matching(
				"pagination",
				model.title,
				model.description,
				ref=None,
				backend=subroutine.domain.search.NATIVE,
			)
		)
	).all()

	assert len(found) == 2, f"the weighted expression changed which rows match: {found}"


def test_every_search_index_names_its_title_first () -> None:
	"""What holds `#624`'s weighting, because it is decided by **position**.

	:func:`subroutine.db.fulltext.vector` labels the first column ``A`` and the rest ``B``, so
	the rule is *the first column is what the row is called*. Every declaration obeys it today
	and nothing said so — an unwritten rule cannot be re-asked of the next table somebody makes
	searchable, and getting it backwards would rank a body above the title silently.

	A comment is the exception and is allowed one column: its body is indexed alone, so the
	label is unobservable — a weight decides something only where two of them meet in one
	vector.
	"""

	source = SOURCE / "subroutine" / "db" / "models"
	declared: list[tuple[str, list[str]]] = []

	for module in sorted(source.glob("*.py")):
		tree = ast.parse(module.read_text(encoding="utf-8"))

		for node in ast.walk(tree):
			if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
				continue

			if node.func.attr != "index":
				continue

			named = node.args[0]

			if not isinstance(named, ast.Constant):
				continue

			declared.append((
				str(named.value),
				[
					argument.attr
					for argument in node.args[1:]
					if isinstance(argument, ast.Attribute)
				],
			))

	assert len(declared) >= 3, f"no search index was found to check: {declared}"

	for name, columns in declared:
		if len(columns) < 2:
			continue

		assert columns[0] == "title", (
			f"{name} weights {columns[0]!r} as the title, because it is declared first"
		)
