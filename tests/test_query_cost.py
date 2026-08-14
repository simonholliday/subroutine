"""Measure what a listing costs at a realistic size, on both backends — `#855`.

**Written because a correctness suite is structurally incapable of seeing a query plan.**
`#569` shipped eight passing tests and a listing that took 5.4 seconds: 2.03 ms plain against
5,432.76 ms ranked on PostgreSQL, for one page of fifty at 200 tasks, while SQLite did the
same work in 8.43 ms. Every test was green on both backends throughout, in milliseconds,
because a fixture holds a handful of rows. That is *one of a thing* applied to volume rather
than to plurality, and it is a blind spot the whole suite shares.

**The guarded quantity is a ratio, not a duration.** Everything is measured against the same
page fetched with no ``ORDER BY`` and no narrowing at all, on the same machine, in the same
process, against the same rows — so processor speed, disk, and the difference between an
in-process SQLite and a PostgreSQL over a socket all cancel. A wall-clock ceiling does none
of that and would make a gate that fails on somebody else's laptop.

`#855` asked for the ratio of *PostgreSQL to SQLite* for the same query, and **this measures
a different ratio on purpose**. That one is the shape the defect was found in, and it is the
weaker instrument: the two engines have genuinely different fixed costs — an in-process
SQLite against a PostgreSQL over a socket — so their ratio starts somewhere other than 1 and
moves with the machine, which is a noisy baseline for a regression to have to climb out of.
Anything compared with an unordered page *on its own backend* starts at 1 by construction.
Nothing here computes a cross-backend figure; each backend is measured in its own right and
the two are visible side by side in what a failure prints. :data:`CEILING_MS` is the absolute
half the item also asked for.

**The three listings the item named are all here**: every published ordering, ``--ready``,
and the agenda — plus the search that `#823` is about to replace, so that work has a standing
before-and-after rather than a benchmark run once and quoted from memory. What one size can
and cannot say about that last one is :data:`GROWS_WITH_ROWS`.

**The vocabulary is read rather than listed** (`#661`). Every entry in
``api.tasks.SORTABLE`` is measured, so a sort field added later is covered by having been
declared, and a guard cannot fall behind the thing it guards.

**And the measurement can fail**, which is the half that usually goes missing:
:func:`test_a_quadratic_ordering_is_caught` feeds a deliberately correlated ordering through
:func:`_measured` — the same entry point, not a copy of its rule — and asserts the ceiling is
crossed. `#405`'s method, because a benchmark that has never gone red is a benchmark nobody
knows the sign of.

Seeding is a bulk insert on purpose, and `#855` recorded why before this file existed: the
throwaway probe that produced `#856`'s numbers went through ``tasks.create`` first and spent
nine minutes on permission checks, ref allocation and per-row flushes for a thousand rows
without ever reaching the measurement. A row put there by an ``INSERT`` has the same query
plan as one put there by the service.
"""

import datetime
import functools
import math
import time
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import conftest
import subroutine.api.tasks
import subroutine.db.migrate
import subroutine.db.models.identity
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.session
import subroutine.domain.agenda
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.ordering
import subroutine.domain.readiness
import subroutine.domain.scoping
import subroutine.domain.search

#: How many tasks to measure against. Chosen as roughly ten times this project's own open
#: backlog, which is the size at which the `#569` defect was unmistakable rather than merely
#: present — it was already 5.4 seconds at 200.
TASKS = 2_000

#: One task in this many blocks another.
#:
#: **Not decoration.** ``readiness.unblocked`` is a correlated ``EXISTS`` over the link table
#: and ``views.Task.blocking`` added a second one per page (`#569`); against a workspace with
#: no links at all both are answered from an empty index and this file would be measuring a
#: shape it never exercises. Ten per cent is close to what `#851` measured on the real
#: instance — 16 of 172 open tasks block anything, across 20 live edges.
BLOCKED_IN = 10

#: One page, as every listing here serves one.
PAGE = 50

#: How many times each measurement runs, with the best kept. A benchmark measures a floor:
#: noise only ever adds, so the minimum is the honest statistic and the mean is the one that
#: drifts with whatever else the machine is doing.
RUNS = 5

#: What a listing may cost as a multiple of the same page fetched unordered and unnarrowed.
#:
#: **Measured before it was chosen**, at 2,000 tasks and 200 blocking edges, on both backends:
#:
#: ===================  ==========  =============
#: measurement          SQLite      PostgreSQL
#: ===================  ==========  =============
#: unordered page       2.37 ms     2.60 ms
#: every published sort 1.0x to 1.8x 1.1x to 1.9x
#: ``--ready``          1.7x        2.1x
#: the agenda           6.0x        6.5x
#: search, no match     1.6x        6.3x
#: quadratic control    >400x       >400x
#: ===================  ==========  =============
#:
#: Twenty-five is comfortably above everything real and far below the control, which is what a
#: ceiling wants to be — raising it is meant to be an act, with the new measurement written
#: in, exactly as the MCP tool budget and the browser-test cap already work.
#:
#: **Some work legitimately costs more than an unordered page and always will.**
#: ``priority_score`` is a ``CASE`` (§6.3a) and can never be served by a plain index, so it
#: sorts the whole candidate set; the agenda runs five queries rather than one; and §10.4 says
#: no index can serve ``ILIKE '%…%'`` at all. Those are known and accepted costs. What this
#: number watches for is something that consults rows *other than* the ones it returns, once
#: per candidate.
#:
#: **One ceiling rather than one per listing, and the search is why that changed.** A separate
#: and much larger figure was written for it first, on the reasoning that an unindexable scan
#: deserves its own allowance — and then measuring put the scan at 6.3x, well inside this
#: number, so the special case was a threshold nothing could ever reach. `#303`'s shape, met
#: while writing the guard rather than years later.
RATIO_CEILING = 25.0

#: What a single measurement may cost outright, in milliseconds, on either backend — the
#: unordered page included.
#:
#: **It catches the failure the ratio cannot**, which is why one number is not enough: if the
#: *baseline* becomes slow — a visibility predicate that starts consulting other rows, say —
#: then everything else is still cheap relative to it and every ratio stays near 1. Applied to
#: the unordered page as well, because that is the query such a regression would be hiding in.
#:
#: Nothing real crossed 17 ms in the table above, so this is ~15x headroom: generous enough
#: for a contended CI runner, tight enough that half a second of regression fails rather than
#: passing quietly under a figure chosen to be safe.
CEILING_MS = 250.0

#: **This measures one size, and the thing search does is grow.** `#823` measured the same
#: no-match search at 20 ms per 1,000 rows, 181 ms at 10,000 and 898 ms at 50,000, against
#: 0.1 ms with ``tsvector`` and GIN — so a ratio taken at 2,000 rows says search is fine and
#: says nothing whatever about 50,000. Sizing this file up is the obvious next move and it is
#: not free: seeding is most of the runtime, and a gate step that takes minutes is one people
#: stop running. Recorded here so the limit is known rather than discovered.
GROWS_WITH_ROWS = ("search (no match)",)

#: A search that finds nothing, which is the case that costs the most and the one that
#: matters. `#823` measured this and its first attempt was wrong: benchmarked with a common
#: word the search looked flat at every scale, because ``LIMIT`` short-circuits as soon as
#: fifty rows match — so it timed how fast fifty matches are found rather than how long a scan
#: takes. *Does this already exist* is the duplicate check the skill sends every agent
#: through, and it is a scan of everything.
UNFINDABLE = "quinsy fenestration marmoreal"

#: The words a seeded description is built from. Ordinary vocabulary from this project's own
#: backlog, and deliberately none of :data:`UNFINDABLE`.
VOCABULARY = (
	"listing",
	"ordering",
	"deadline",
	"workspace",
	"credential",
	"agenda",
	"instance",
	"migration",
	"document",
	"backlog",
	"predicate",
	"cursor",
	"transport",
	"principal",
	"vocabulary",
	"boundary",
	"measurement",
)

#: Roughly how many words a seeded description carries.
#:
#: **The first version of this file had none, and the search measured cheaper than an
#: unordered page.** Every ``description`` was NULL and every title was nineteen characters,
#: so ``ILIKE '%quinsy%'`` failed on the first column of the first term and the scan §10.4
#: says cannot be indexed came out at 1.1x. That is a fixture structurally unable to show the
#: difference this guard exists to watch — the same shape as a correctness fixture holding
#: four rows, one column along. `#851` measured this instance's real items in the low
#: kilobytes; a hundred words is a few hundred bytes, which is enough for the scan to be real
#: without making the seed one.
PROSE_WORDS = 100


class Measured(typing.NamedTuple):
	"""One backend's answer: what an unordered page cost, and what each measurement cost."""

	#: Which backend, for a failure message that says where.
	backend: str

	#: Milliseconds for one page with no ``ORDER BY`` and no narrowing.
	baseline: float

	#: Milliseconds per named measurement.
	timings: dict[str, float]

	def ratio (self, name: str) -> float:
		"""Return what one measurement cost as a multiple of the unordered page."""

		return self.timings[name] / self.baseline

	def report (self) -> str:
		"""Return every timing, for a failure to print in full."""

		lines = [f"  {self.backend}: unordered page {self.baseline:.2f} ms, {TASKS} tasks"]

		for name in sorted(self.timings):
			lines.append(
				f"    {name:<24} {self.timings[name]:8.2f} ms  {self.ratio(name):.1f}x"
			)

		return "\n".join(lines)


class Context(typing.NamedTuple):
	"""What every measurement needs in order to build the query a listing would build."""

	session: sqlalchemy.orm.Session
	principal: subroutine.domain.authentication.Principal
	workspace_id: uuid.UUID


#: One thing to time: given a context, do what a listing does.
Work = typing.Callable[[Context], typing.Any]


def _quadratic () -> subroutine.domain.ordering.Derived:
	"""Return an ordering that consults every other row, as the control this guard needs.

	It counts the rows below each candidate, so the database visits N² of them to order N —
	the shape of `#569`'s defect without its particular cause. Deliberately not a copy of
	that query: what is being proved is that :func:`_measured` notices work which reads other
	rows, not that it recognises one specific mistake.
	"""

	other = sqlalchemy.orm.aliased(subroutine.db.models.work.Task)

	return subroutine.domain.ordering.Derived(
		expression=(
			sqlalchemy.select(sqlalchemy.func.count())
			.select_from(other)
			.where(
				other.workspace_id == subroutine.db.models.work.Task.workspace_id,
				other.ref <= subroutine.db.models.work.Task.ref,
			)
			.scalar_subquery()
		),
		# Never called: this ordering exists to be timed, and nothing pages with it.
		read=lambda row: None,
	)


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def own_database (
	request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> typing.Iterator[str]:
	"""Yield a database of this module's own, on each backend in turn.

	**Never the shared session engine.** This seeds two thousand committed rows and runs
	``ANALYZE``; both would follow every other test in the run, and the second is a change to
	the statistics every other query's plan is chosen from.
	"""

	if request.param == "sqlite":
		yield f"sqlite:///{tmp_path_factory.mktemp('cost') / 'cost.db'}"

		return

	reason = conftest._postgres_unavailable_reason()

	if reason is not None:
		if conftest.REQUIRE_POSTGRES:
			pytest.fail(reason)

		pytest.skip(reason)

	name = f"subroutine_cost_{uuid.uuid4().hex[:12]}"
	admin = sqlalchemy.create_engine(conftest.POSTGRES_ADMIN_URL, isolation_level="AUTOCOMMIT")

	try:
		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{name}"'))
			connection.execute(sqlalchemy.text(f'CREATE DATABASE "{name}"'))

		yield conftest.with_database(conftest.POSTGRES_ADMIN_URL, name)

		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{name}"'))

	finally:
		admin.dispose()


@pytest.fixture(scope="module")
def seeded (own_database: str) -> typing.Iterator[tuple[sqlalchemy.engine.Engine, str]]:
	"""Yield an engine over a workspace holding :data:`TASKS` tasks, and its backend's name."""

	engine = subroutine.db.session.create_engine(own_database)

	try:
		subroutine.db.session.create_all(engine)
		subroutine.db.migrate.stamp(own_database)

		_fill(engine)

		yield engine, engine.dialect.name

	finally:
		engine.dispose()


def _fill (engine: sqlalchemy.engine.Engine) -> None:
	"""Bootstrap an installation and bulk-insert its tasks and their blocking links."""

	factory = subroutine.db.session.create_session_factory(engine)

	with subroutine.db.session.session_scope(factory) as session:
		setup = subroutine.domain.bootstrap.initialise(
			session, username="si", instance_name="Cost measurement"
		)
		rows = list(_rows(session, setup))

		session.execute(sqlalchemy.insert(subroutine.db.models.work.Task), rows)
		session.execute(
			sqlalchemy.insert(subroutine.db.models.work.Link), list(_links(session, setup, rows))
		)

	# **Statistics decide a plan, so a measurement taken without them is measuring the
	# planner's guess about an empty table.** PostgreSQL autovacuum has not run on a database
	# this young, and SQLite consults `sqlite_stat1` only if something has written it.
	with engine.begin() as connection:
		connection.execute(sqlalchemy.text("ANALYZE"))


def _rows (
	session: sqlalchemy.orm.Session, setup: subroutine.domain.bootstrap.Bootstrap
) -> typing.Iterator[dict[str, typing.Any]]:
	"""Return the task rows to insert, spread across every state an ordering can see.

	**All three of §6.3a's bands are populated**, because a ranked listing that meets only
	ranked rows never evaluates the ``CASE``'s later branches, and the bands are what
	``priority_score`` costs anything to compute at all. Deadlines and planned days are set on
	a third each for the same reason: an ordering over a column that is null in every row is
	a sort of one value.
	"""

	status = session.scalars(
		sqlalchemy.select(subroutine.db.models.vocabulary.Status)
		.where(subroutine.db.models.vocabulary.Status.workspace_id == setup.workspace.id)
		.order_by(subroutine.db.models.vocabulary.Status.position)
	).first()
	kind = session.scalars(
		sqlalchemy.select(subroutine.db.models.vocabulary.ItemType)
		.where(subroutine.db.models.vocabulary.ItemType.workspace_id == setup.workspace.id)
		.order_by(subroutine.db.models.vocabulary.ItemType.position)
	).first()

	assert status is not None and kind is not None, "the workspace was created unseeded"

	epoch = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

	for number in range(TASKS):
		yield {
			"id": uuid.uuid4(),
			"workspace_id": setup.workspace.id,
			"project_id": setup.inbox.id,
			"status_id": status.id,
			"type_id": kind.id,
			"ref": number + 1,
			"title": f"Measured task {number:05d}",
			"description": _prose(number),
			"path": "",
			"importance": None if number % 4 == 0 else (number % 5) + 1,
			"urgency": None if number % 3 == 0 else (number % 5) + 1,
			"due_at": epoch + datetime.timedelta(days=number) if number % 3 == 0 else None,
			"planned_for": (epoch + datetime.timedelta(days=number)).date()
			if number % 3 == 1
			else None,
			"created_at": epoch + datetime.timedelta(minutes=number),
			"updated_at": epoch + datetime.timedelta(minutes=number),
		}


def _prose (number: int) -> str:
	"""Return a description of :data:`PROSE_WORDS` words, different for every row.

	Different rather than repeated, because one string stored two thousand times is a shape
	no real workspace has and one a database may treat unusually — PostgreSQL compresses a
	wide value before storing it out of line, and identical rows compress alike.
	"""

	return " ".join(
		VOCABULARY[(number + position) % len(VOCABULARY)] for position in range(PROSE_WORDS)
	)


def _links (
	session: sqlalchemy.orm.Session,
	setup: subroutine.domain.bootstrap.Bootstrap,
	rows: typing.Sequence[dict[str, typing.Any]],
) -> typing.Iterator[dict[str, typing.Any]]:
	"""Return ``blocks`` edges over one task in :data:`BLOCKED_IN`, source to target."""

	kind = session.scalars(
		sqlalchemy.select(subroutine.db.models.vocabulary.LinkType).where(
			subroutine.db.models.vocabulary.LinkType.workspace_id == setup.workspace.id,
			subroutine.db.models.vocabulary.LinkType.key == "blocks",
		)
	).one()

	for index in range(0, len(rows) - 1, BLOCKED_IN):
		yield {
			"id": uuid.uuid4(),
			"workspace_id": setup.workspace.id,
			"source_type": "task",
			"source_id": rows[index]["id"],
			"target_type": "task",
			"target_id": rows[index + 1]["id"],
			"link_type_id": kind.id,
			"created_by": setup.user.id,
		}


def _base (context: Context) -> sqlalchemy.Select[tuple[subroutine.db.models.work.Task]]:
	"""Return the statement every listing starts from: what this principal may read."""

	return subroutine.domain.scoping.readable_tasks(
		context.principal, workspace_ids=[context.workspace_id]
	)


def _unordered (context: Context) -> typing.Any:
	"""Run one page with nothing asked of it, which is what everything is measured against."""

	return context.session.execute(_base(context).limit(PAGE)).unique().scalars().all()


def _ordering (
	name: str, orderings: typing.Mapping[str, subroutine.domain.ordering.Sortable]
) -> Work:
	"""Return the work of serving one page under one named ordering.

	Built the way ``api/tasks._page`` builds it — the loader options from the same expression
	the ``ORDER BY`` terms are parsed from, never a second reading of it (`#569`).
	"""

	def run (context: Context) -> typing.Any:
		"""Run one page under this ordering."""

		statement = (
			_base(context)
			.options(
				*subroutine.domain.ordering.options(
					name, allowed=orderings, default=(name,)
				)
			)
			.order_by(
				*subroutine.domain.ordering.clauses(
					name,
					allowed=orderings,
					default=(name,),
					tiebreak=subroutine.db.models.work.Task.id,
				)
			)
			.limit(PAGE)
		)

		return context.session.execute(statement).unique().scalars().all()

	return run


def _ready (context: Context) -> typing.Any:
	"""Run ``list --ready``: nothing unfinished blocks it, undeferred, unclaimed."""

	statement = _base(context).where(
		subroutine.domain.readiness.ready(
			subroutine.db.models.work.Task,
			now=datetime.datetime.now(tz=datetime.UTC),
			by=context.principal.user.id,
		)
	)

	return context.session.execute(statement.limit(PAGE)).unique().scalars().all()


def _searched (context: Context) -> typing.Any:
	"""Run a search that matches nothing, which is the expensive and the useful case."""

	statement = _base(context).where(
		subroutine.domain.search.matching(
			UNFINDABLE,
			subroutine.db.models.work.Task.title,
			subroutine.db.models.work.Task.description,
			ref=subroutine.db.models.work.Task.ref,
		)
	)

	return context.session.execute(statement.limit(PAGE)).unique().scalars().all()


def _agenda (context: Context) -> typing.Any:
	"""Build the whole agenda, which is five queries and what ``subroutine today`` runs."""

	return subroutine.domain.agenda.build(
		context.session,
		principal=context.principal,
		workspace_ids=[context.workspace_id],
		now=datetime.datetime.now(tz=datetime.UTC),
		timezone="UTC",
		horizon_days=subroutine.domain.agenda.DEFAULT_HORIZON_DAYS,
	)


def _measured (
	engine: sqlalchemy.engine.Engine, *, work: typing.Mapping[str, Work]
) -> Measured:
	"""Time each named piece of work, and one page that asks for nothing.

	**The work is an argument rather than a constant**, which is what lets a synthetic
	pathological ordering reach exactly this code (`#405`). A guard whose subject is baked in
	can only ever be falsified by a copy of itself.
	"""

	factory = subroutine.db.session.create_session_factory(engine)

	with subroutine.db.session.session_scope(factory) as session:
		user = session.scalars(sqlalchemy.select(subroutine.db.models.identity.User)).first()

		assert user is not None, "the fixture did not bootstrap a user"

		context = Context(
			session=session,
			principal=subroutine.domain.authentication.Principal(user=user, token=None),
			workspace_id=session.scalars(
				sqlalchemy.select(subroutine.db.models.identity.Workspace.id)
			).one(),
		)

		baseline = _elapsed(functools.partial(_unordered, context))
		timings = {
			name: _elapsed(functools.partial(run, context)) for name, run in work.items()
		}

	return Measured(backend=engine.dialect.name, baseline=baseline, timings=timings)


def _elapsed (run: typing.Callable[[], typing.Any]) -> float:
	"""Return the fastest of :data:`RUNS` executions, in milliseconds.

	The rows are consumed rather than left in the result, because that is what a caller pays.
	**It is not what makes this measurement honest, and the first version of this docstring
	said it was.** Falsifying by dropping the ``.all()`` left every assertion green: neither
	driver streams by default, so the statement has already run by the time ``execute``
	returns and only the fetch of fifty buffered rows is saved. The claim that a lazy result
	would time the planning and none of the work is true of a server-side cursor and false of
	what runs here.

	What actually stops this being an inert stopwatch is
	:func:`test_a_quadratic_ordering_is_caught`, which fails the moment the work stops
	reaching the database.
	"""

	best = math.inf

	for _attempt in range(RUNS):
		started = time.perf_counter()

		run()

		best = min(best, (time.perf_counter() - started) * 1_000)

	return best


def _too_slow (measured: Measured) -> dict[str, float]:
	"""Return everything that took longer than :data:`CEILING_MS`, the baseline included.

	The unordered page is in here deliberately: it is the one query that a ratio *against* the
	unordered page can never report on.
	"""

	costs = {"(unordered)": measured.baseline} | measured.timings

	return {name: cost for name, cost in costs.items() if cost > CEILING_MS}


def test_every_published_ordering_costs_about_what_an_unordered_page_costs (
	seeded: tuple[sqlalchemy.engine.Engine, str]
) -> None:
	"""No sort in the published vocabulary may consult the rows it is not returning."""

	engine, backend = seeded
	measured = _measured(
		engine,
		work={
			name: _ordering(name, subroutine.api.tasks.SORTABLE)
			for name in subroutine.api.tasks.SORTABLE
		},
	)
	expensive = {
		name: measured.ratio(name)
		for name in measured.timings
		if measured.ratio(name) > RATIO_CEILING
	}

	assert not _too_slow(measured), (
		f"On {backend}, one page of {PAGE} at {TASKS} tasks crossed {CEILING_MS:.0f} ms:\n"
		f"{measured.report()}"
	)

	assert not expensive, (
		f"On {backend}, {sorted(expensive)} cost more than {RATIO_CEILING:.0f}x an unordered "
		f"page. An ordering that reads rows other than the one it returns is the shape to "
		f"look for — aggregate them once per statement with a LEFT JOIN, never once per "
		f"candidate row.\n{measured.report()}"
	)


def test_a_narrowed_listing_costs_about_what_an_unordered_page_costs (
	seeded: tuple[sqlalchemy.engine.Engine, str]
) -> None:
	"""``--ready``, the agenda and a search are the three listings a person actually runs.

	All three consult rows other than the ones they return — ``unblocked`` is a correlated
	``EXISTS`` over the link table, and a substring search reads every row's prose — so all
	three are the shape this file exists to watch, and none had a standing measurement before
	`#855`. See :data:`GROWS_WITH_ROWS` for what a single-size measurement cannot say about
	the search.
	"""

	engine, backend = seeded
	measured = _measured(
		engine, work={"ready": _ready, "agenda": _agenda, "search (no match)": _searched}
	)
	expensive = {
		name: measured.ratio(name)
		for name in measured.timings
		if measured.ratio(name) > RATIO_CEILING
	}

	assert not _too_slow(measured), (
		f"On {backend}, a listing crossed {CEILING_MS:.0f} ms at {TASKS} tasks:\n"
		f"{measured.report()}"
	)

	assert not expensive, (
		f"On {backend}, {sorted(expensive)} cost more than {RATIO_CEILING:.0f}x an unordered "
		f"page. A listing that consults rows other than the ones it returns, once per "
		f"candidate, is the shape to look for.\n{measured.report()}"
	)


def test_the_measurement_is_against_the_workspace_it_claims (
	seeded: tuple[sqlalchemy.engine.Engine, str]
) -> None:
	"""Assert the seed, because every ceiling above is one-sided.

	**Both halves of this were surviving mutations**, and neither is about the code being
	measured. Dropping the seeded descriptions takes the search from 6.3x to 1.1x, and
	dropping the links takes ``--ready`` down with it — and every assertion above stays green
	either way, because a ratio ceiling catches a listing getting *more* expensive and can say
	nothing at all about a fixture getting *less* representative. The numbers would go on
	being printed and would mean nothing, which is worse than not measuring.

	So the fixture is checked against what the module says it is, and the expectations are
	derived from the seeding rather than written out a second time.

	**What deriving them costs is worth stating**: an expectation computed from
	:data:`BLOCKED_IN` moves when :data:`BLOCKED_IN` moves, so this catches a *seeding* that
	silently under-fills — falsified at two edges instead of two hundred — and does not catch
	somebody lowering the density on purpose. That is the right trade and the same one every
	written ceiling here makes: a deliberate change is an act, with the reason beside the
	constant, and a guard is for the accident.
	"""

	engine, backend = seeded
	factory = subroutine.db.session.create_session_factory(engine)
	link = subroutine.db.models.work.Link
	kind = subroutine.db.models.vocabulary.LinkType

	with subroutine.db.session.session_scope(factory) as session:
		tasks = session.scalar(
			sqlalchemy.select(sqlalchemy.func.count()).select_from(
				subroutine.db.models.work.Task
			)
		)
		shortest = session.scalar(
			sqlalchemy.select(
				sqlalchemy.func.min(
					sqlalchemy.func.length(subroutine.db.models.work.Task.description)
				)
			)
		)
		edges = session.scalar(
			sqlalchemy.select(sqlalchemy.func.count())
			.select_from(link)
			.join(kind, kind.id == link.link_type_id)
			.where(kind.key == "blocks", link.deleted_at.is_(None))
		)

	assert tasks == TASKS, f"On {backend} the fixture holds {tasks} tasks, not {TASKS}."

	assert shortest == min(len(_prose(number)) for number in range(TASKS)), (
		f"On {backend} the shortest description is {shortest} characters. The search is a "
		f"scan of this prose, so a workspace seeded without it measures nothing and passes."
	)

	assert edges == len(range(0, TASKS - 1, BLOCKED_IN)), (
		f"On {backend} the fixture holds {edges} live blocking edges. ``--ready`` and "
		f"``blocking`` are correlated EXISTS clauses over that table, so with none of them "
		f"they are answered from an empty index and cost nothing to get wrong."
	)


def test_a_quadratic_ordering_is_caught (
	seeded: tuple[sqlalchemy.engine.Engine, str]
) -> None:
	"""The measurement can fail, and this is what failing looks like.

	Without this, :data:`RATIO_CEILING` is a number nobody has ever seen exceeded, and both
	tests above pass equally well when ``_measured`` is timing nothing at all.
	"""

	engine, backend = seeded
	orderings = {"quadratic": _quadratic()}
	measured = _measured(engine, work={"quadratic": _ordering("quadratic", orderings)})
	ratio = measured.ratio("quadratic")

	assert ratio > RATIO_CEILING, (
		f"On {backend} an ordering that counts every row below each candidate cost only "
		f"{ratio:.1f}x an unordered page, which is under the {RATIO_CEILING:.0f}x ceiling. "
		f"Either the ceiling is now too loose to catch a quadratic ordering, or this "
		f"measurement is not running the query it thinks it is.\n{measured.report()}"
	)
