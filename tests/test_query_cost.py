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
import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.session
import subroutine.domain.agenda
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.hierarchy
import subroutine.domain.ordering
import subroutine.domain.readiness
import subroutine.domain.scoping
import subroutine.domain.search
import subroutine.domain.users
import subroutine.views

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

#: One task in this many is filed under the one before it.
#:
#: **Every row carried ``path = ""`` until `SR#1800`, and that is not a shape any task has** —
#: :func:`subroutine.domain.hierarchy.build_path` gives a root ``/<id>/``. An empty path makes
#: ``model.path LIKE ancestor.path || '%'`` into ``'' LIKE '%'``, which is **true of every pair
#: of rows**, so `SR#1610`'s ancestor rule matched everything: measured on this fixture, all
#: 2,000 tasks were ``under_a_blocked_ancestor`` and ``unblocked`` returned **zero**. So
#: :func:`_ready` was timing an empty page and reporting it fast, from the day that rule
#: shipped.
#:
#: A fifth is nested one level, which is far more than the served instance carries — six of
#: 521 open items have a parent — because this is the branch the guard exists to reach, and a
#: fixture at the real proportion would leave it exercised by almost nothing.
NESTED_IN = 5

#: How many comments each task carries.
#:
#: **The real ratio, not a round number.** This instance holds 780 comments against 695 tasks
#: (`#825`), so roughly one apiece — and comments are the largest body of prose here after the
#: event feed, which is the whole argument for `#83` searching them. A fixture with none would
#: measure a join that never matches, which is `BLOCKED_IN`'s lesson one table along.
COMMENTS_PER_TASK = 1

#: One comment in this many is soft-deleted.
#:
#: A search must not surface prose nobody can open, which `#825` names as the one genuine
#: visibility rule inherited here — and a fixture where nothing is deleted cannot tell a query
#: that honours that from one that ignores it.
DELETED_COMMENT_IN = 10

#: One page, as every listing here serves one.
#:
#: **A hundred, because that is what the browser asks for** — ``PAGE`` and ``POLL_PAGE`` in
#: ``web/assets/app.js`` are both 100, on every listing and on every poll. It was 50, and
#: `SR#1800` is what that cost: measured on this fixture, a page of 50 estimates at **491,514**
#: and a page of 100 at over 500,000, which is PostgreSQL's ``jit_inline_above_cost`` and
#: ``jit_optimize_above_cost``. So the guard sat **1.7% under the cliff** it existed to notice,
#: and the same statement it called 87 ms is 801 ms for the page a person actually loads.
#:
#: A guard measuring half the page the product serves is measuring something nobody does.
PAGE = 100

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

#: Listings measured as over the ceiling on purpose, each naming the item that will bring it
#: back under. **Deleting the entry is what closes that item**, which is the rule every excuse
#: list in this repository follows.
#:
#: **The ceiling is not raised for these, deliberately.** A threshold moved to accommodate a
#: defect is a threshold nothing can reach — `#303`'s shape — and here the number *is* the
#: finding: the whole argument for `#823` is that an index makes a search cheap, so a
#: measurement saying it makes half of one dearer has to go on being printed rather than being
#: made to pass.
KNOWN_EXPENSIVE: dict[str, str] = {}

#: Subjects the *ratio* does not fit, each with what measures it instead (`#1295`).
#:
#: **Not the same thing as :data:`KNOWN_EXPENSIVE`, and the difference is the point.** That
#: register is for a listing genuinely over the ceiling, where the number *is* the finding and
#: has to go on being printed. This one is for a subject where the ratio measures something
#: other than what the ceiling is about — so leaving it in would not keep a finding visible, it
#: would be a red build that says nothing true.
#:
#: **The ratio is a proxy for an N+1**, which is what this file's own sentence says it watches:
#: *a listing that consults rows other than the ones it returns, once per candidate*. For one
#: query returning one page that proxy is good. The agenda is **fourteen statements returning
#: 184 rows** at :data:`TASKS`, so its ratio is dominated by per-statement and per-row overhead
#: — measured at 17.3x on a workstation and 30.9x on a CI runner **on the same commit**, where
#: the baseline moved only 1.45x between the two.
#:
#: **And the row count is a decision rather than a cost to remove**: 79 overdue and 80 today,
#: both uncapped by `#888` — *"hiding work misleads the reader into starting something else."*
#:
#: Each entry is still measured by :data:`CEILING_MS`, which it passes with room, **and** by
#: :func:`test_a_composite_view_asks_a_bounded_number_of_questions`, which is the N+1 measured
#: directly and cannot be moved by a busy machine.
#: **`ready` was in here for one day and should never have been** — `SR#1827`, and it is the
#: entry worth learning from rather than the two that remain. It was excused at **36.5x on
#: SQLite**, over the 25x ceiling, on the argument that the ratio reports a backend's strategy
#: for a deliberate correlated ``EXISTS``. The ratio was right and the argument was a way of not
#: looking: the predicate was scanning every ancestor for every row including the four in five
#: that have no parent at all, and one clause took it to **8.4x and 30 ms**. The absolute
#: ceiling caught on CI the next day what the ratio had said the day before.
#:
#: **So an excuse written on the day a number first crosses a ceiling is the shape to
#: distrust**, and :func:`test_nothing_is_excused_from_the_ratio_that_is_not_composite` is what
#: makes that mechanical rather than remembered.
MEASURED_ANOTHER_WAY: dict[str, str] = {
	"agenda": "`#1295` — fourteen statements against a one-statement baseline, so the ratio "
	"measures the machine. A bounded statement count is the guard instead.",
	"marks": "`SR#1800` — a dozen statements against a one-statement baseline, which is the "
	"agenda's argument one view along. **35 ms and 9.9x on PostgreSQL against 36 ms and 9.8x "
	"on SQLite** (`SR#1827`, re-measured after the fix that took SQLite from 110 ms). "
	":data:`COMPOSITE_CEILING_MS` is the bound, and it is the one that catches the defect this "
	"was written for: 803 ms against 500.",
}

#: What a single measurement may cost outright, in milliseconds, on either backend — the
#: unordered page included.
#:
#: **It catches the failure the ratio cannot**, which is why one number is not enough: if the
#: *baseline* becomes slow — a visibility predicate that starts consulting other rows, say —
#: then everything else is still cheap relative to it and every ratio stays near 1. Applied to
#: the unordered page as well, because that is the query such a regression would be hiding in.
#:
#: **Re-measured 2026-08-31, because the sentence here was three and a half times out of date
#: and its reassurance was what stopped anybody looking.** It read *"nothing real crossed 17 ms
#: in the table above, so this is ~15x headroom"*. On this workstation the agenda measures
#: **60 ms** with nobody assigned anything and **78 ms** on the fixture as it now stands — so
#: the headroom was ~3x, and a CI runner is about 3.3x slower than here, which put the real
#: margin at **20%**. It crossed at 257 ms on Python 3.11, twice, and the number in this
#: comment is why nobody expected it. `#1724`.
#:
#: **So this bounds a *single-statement* listing**, which is what it can be honest about, and
#: the agenda has its own — see :data:`COMPOSITE_CEILING_MS`.
CEILING_MS = 250.0

#: What a composite view may cost outright, in milliseconds, on either backend.
#:
#: **The agenda is not one query and cannot be held to one query's number.** It issues
#: seventeen statements against a one-statement baseline, which is exactly why `#1295` excused
#: it from the *ratio* — *"the ratio measures the machine"* — and an absolute millisecond
#: figure measures the machine harder still. What that item did not do was give it a ceiling of
#: its own, so it kept the one written for a single page and quietly used up its margin.
#:
#: **Measured rather than chosen**: 78 ms here, 257 ms on a contended two-core CI runner. Five
#: hundred is about 2x the worst reading we have and 6x the local one — tight enough that an
#: order-of-magnitude regression fails, loose enough that a busy runner does not.
#:
#: **It is not the only guard on this view, which is what makes it affordable to be generous.**
#: :data:`AGENDA_STATEMENTS` bounds the question count and is a fact about the code rather than
#: about the machine, so an N+1 fails there by three orders of magnitude whatever this says.
COMPOSITE_CEILING_MS = 500.0

#: Which measurements are composite views rather than one query, and so take the ceiling above.
#:
#: **A register rather than a flag on the measurement**, because what makes something composite
#: is a fact about the work it does and not about the timing — and an entry here has to name the
#: statement count that justifies it, which is what stops this becoming somewhere to park
#: anything that went red.
COMPOSITE: dict[str, str] = {
	"agenda": "seventeen statements — eight buckets, six counts, the prioritised-project "
	"lookup, the zone lookup and the blocker lookup. `#1295` and `AGENDA_STATEMENTS`.",
	"marks": "a dozen statements — statuses, types, projects, parents and tags for the page, "
	"then the three readiness scans (blocked, blocking, finished underneath). `SR#1800`.",
}

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


def _correlated_prioritised () -> subroutine.domain.ordering.Derived:
	"""Return the prioritised-project bonus written the tempting way — `#986`.

	**This is the version somebody will reach for**, because passing the paths in as literals
	looks like a detour: it asks the workspace which project is prioritised from *inside* the
	ordering expression, which makes it a correlated subquery in ``ORDER BY`` and therefore
	computes a sort key per row rather than per page.

	It exists to be measured against the literal, and measuring it is what corrected the item:
	`#986` and decision ``#982`` both say this reproduces `#856` exactly, and it does not. See
	:func:`test_reaching_the_pointer_from_inside_the_ordering_costs_more`.
	"""

	task = subroutine.db.models.work.Task
	project = subroutine.db.models.project.Project
	workspace = subroutine.db.models.identity.Workspace

	pointed_at = (
		sqlalchemy.select(project.path)
		.join(workspace, workspace.prioritised_project_id == project.id)
		.where(workspace.id == task.workspace_id)
		.correlate(task)
		.scalar_subquery()
	)

	return subroutine.domain.ordering.Derived(
		expression=sqlalchemy.case(
			(project.path.like(sqlalchemy.func.coalesce(pointed_at, "") + "%"), 3), else_=0
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

		# **Somebody else holds the blockers, so the agenda's *Waiting on somebody else*
		# section is populated** (`SR#1287`). Without an assignee on the far end that bucket is
		# empty on every measurement here, the statement it costs is never issued, and
		# :data:`AGENDA_STATEMENTS` would be counting an agenda this instance cannot produce —
		# a fixture that has quietly stopped being representative of the subject.
		held_by = subroutine.domain.users.create(
			session, username="jo", timezone="Etc/UTC"
		)

		for index in range(0, len(rows) - 1, BLOCKED_IN):
			rows[index]["assignee_id"] = held_by.id

		session.execute(sqlalchemy.insert(subroutine.db.models.work.Task), rows)
		session.execute(
			sqlalchemy.insert(subroutine.db.models.work.Link), list(_links(session, setup, rows))
		)
		session.execute(
			sqlalchemy.insert(subroutine.db.models.activity.Comment),
			list(_comments(setup, rows)),
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

	previous: dict[str, typing.Any] | None = None

	for number in range(TASKS):
		identifier = uuid.uuid4()

		# **Filed under the row before it, which is a root** — so nothing here is deeper than
		# one level and the chain cannot run away. See :data:`NESTED_IN` for why any of it is
		# nested at all.
		parent = previous if (number % NESTED_IN == 1 and previous is not None) else None

		previous = {
			"id": identifier,
			"workspace_id": setup.workspace.id,
			"project_id": setup.inbox.id,
			"status_id": status.id,
			"type_id": kind.id,
			"ref": number + 1,
			"title": f"Measured task {number:05d}",
			"description": _prose(number),
			"parent_task_id": None if parent is None else parent["id"],
			"depth": 0 if parent is None else 1,
			"path": subroutine.domain.hierarchy.build_path(
				None if parent is None else parent["path"], identifier
			),
			"importance": None if number % 4 == 0 else (number % 5) + 1,
			"urgency": None if number % 3 == 0 else (number % 5) + 1,
			"due_at": epoch + datetime.timedelta(days=number) if number % 3 == 0 else None,
			"starts_at": (epoch + datetime.timedelta(days=number))
			if number % 3 == 1
			else None,
			"created_at": epoch + datetime.timedelta(minutes=number),
			"updated_at": epoch + datetime.timedelta(minutes=number),
		}

		yield previous


def _comments (
	setup: subroutine.domain.bootstrap.Bootstrap, rows: list[dict[str, typing.Any]]
) -> typing.Iterator[dict[str, typing.Any]]:
	"""Return the comment rows to insert, :data:`COMMENTS_PER_TASK` on every task.

	**Prose of the same length as a description**, for :func:`_prose`'s reason: a join whose
	inner rows are nineteen characters long measures a scan that finishes before it starts.
	Offset so that no comment repeats the text of the task it hangs off — a search matching the
	description *and* the comment would be answered from the cheaper of the two and would say
	nothing about the join.

	One in every :data:`COMMENTS_PER_TASK` group is soft-deleted, because the deletion filter
	is the one genuine visibility rule search inherits here (`#825`) and a fixture with nothing
	deleted cannot tell a query that honours it from one that does not.
	"""

	epoch = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

	for index, row in enumerate(rows):
		for repeat in range(COMMENTS_PER_TASK):
			number = index * COMMENTS_PER_TASK + repeat

			yield {
				"id": uuid.uuid4(),
				"workspace_id": setup.workspace.id,
				"entity_type": "task",
				"entity_id": row["id"],
				"author_id": setup.user.id,
				"body": _prose(number + TASKS),
				"created_at": epoch + datetime.timedelta(minutes=number),
				"updated_at": epoch + datetime.timedelta(minutes=number),
				"deleted_at": epoch if number % DELETED_COMMENT_IN == 0 else None,
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


def _marks (context: Context) -> typing.Any:
	"""Build the vocabulary a page of rows is rendered through — what every listing then does.

	**The half `#1764` says nothing here was measuring.** Every other entry in this file stops
	at the statement that fetches the rows; a listing then hands them to
	:class:`subroutine.views.Vocabulary`, which is where readiness is answered for the whole
	page. So the most expensive statement any listing issued had no standing measurement at
	all, and `SR#1800` was found by a person loading a page rather than by this file.

	Composite by construction — see :data:`COMPOSITE` for the statement count — so it takes
	:data:`COMPOSITE_CEILING_MS` for the agenda's reason.
	"""

	rows = context.session.execute(_base(context).limit(PAGE)).unique().scalars().all()

	return subroutine.views.Vocabulary.for_tasks(context.session, rows)


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


def _searched (context: Context, backend: str = subroutine.domain.search.LIKE) -> typing.Any:
	"""Run a search that matches nothing, which is the expensive and the useful case.

	**Takes the backend** (`#887`), because the module promised *"the search `#823` is about to
	replace, so that work has a standing before-and-after"* — and the *after* was never added.
	The whole performance argument for `#823` is 119 ms against 1 ms, and nothing was watching
	the half it argued for.
	"""

	statement = _base(context).where(
		subroutine.domain.search.matching(
			UNFINDABLE,
			subroutine.db.models.work.Task.title,
			subroutine.db.models.work.Task.description,
			ref=subroutine.db.models.work.Task.ref,
			backend=backend,
		)
	)

	return context.session.execute(statement.limit(PAGE)).unique().scalars().all()


def _searched_including_comments (
	context: Context, backend: str = subroutine.domain.search.LIKE
) -> typing.Any:
	"""Run the same search with `#83`'s comment half, as the endpoints build it.

	**Measured before it was built, because the shape is the one this file exists to watch.**
	A comment is a join rather than a column, so "does any comment on this item match" is a
	correlated ``EXISTS`` evaluated once per candidate row — `#856`'s shape exactly — and the
	inner predicate is an ``ILIKE`` that §10.4 says no index can serve. Two unindexable scans
	multiplied together is the arrangement that produced a 5.4-second listing once already.

	**It calls `search.anywhere` now rather than building the clause here** (`#887`). The
	hand-built version was correct when written — that function did not exist yet, and the
	docstring said *"in the shape it would be built"*. It does exist, and a guard measuring a
	copy of the thing it is guarding is this codebase's signature defect inside the file written
	to catch a different one.

	**It earned itself immediately.** Calling the real composition is what surfaced `#892`: the
	`or_` those four call sites spelled out cost the index on both of its sides, and this file
	measured 63.7x for a query the endpoints were actually running. A copy would have gone on
	measuring the shape somebody wrote down here instead.
	"""

	task = subroutine.db.models.work.Task

	statement = _base(context).where(
		subroutine.domain.search.anywhere(
			UNFINDABLE,
			identity=task.id,
			columns=(task.title, task.description),
			ref=task.ref,
			entity_type="task",
			backend=backend,
		)
	)

	return context.session.execute(statement.limit(PAGE)).unique().scalars().all()


def _agenda (context: Context) -> typing.Any:
	"""Build the whole agenda, which is six queries and what ``subroutine agenda`` runs.

	Five buckets and two counts, less one: ``upcoming`` and the two totals are three
	statements against five bucket queries, and ``in_progress`` shares its base with the rest.
	The number is here because it is the thing this file exists to notice moving — `#997`
	added the second count, and a count per agenda is the cost that was weighed against
	silently leaving dated work out of the view.
	"""

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
	"""Return everything that took longer than the ceiling for its kind, the baseline included.

	The unordered page is in here deliberately: it is the one query that a ratio *against* the
	unordered page can never report on.

	**Two ceilings, because one number could only ever be honest about one kind of work**
	(`#1724`). A single-statement listing and a view that issues seventeen statements are not
	the same measurement, and holding both to a figure derived from the first is what let the
	agenda spend its margin unnoticed until a CI runner failed on it.
	"""

	costs = {"(unordered)": measured.baseline} | measured.timings

	return {
		name: cost for name, cost in costs.items() if cost > _allowed(name)
	}


def _allowed (name: str) -> float:
	"""Return the ceiling one measurement is held to, in milliseconds."""

	return COMPOSITE_CEILING_MS if name in COMPOSITE else CEILING_MS


def _crossing (over: typing.Mapping[str, float]) -> str:
	"""Say which ceiling each measurement crossed, because there are two of them now.

	**Naming the wrong number is worse than naming none** (`#1724`): the message said *crossed
	250 ms* whatever had been exceeded, so a composite view failing at 501 ms would have sent a
	reader to the wrong constant, and to the paragraph arguing for a figure that was not the one
	that fired.
	"""

	return ", ".join(
		f"{name} over {_allowed(name):.0f} ms" for name in sorted(over)
	)


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
		and name not in KNOWN_EXPENSIVE
		and name not in MEASURED_ANOTHER_WAY
	}

	assert not _too_slow(measured), (
		f"On {backend}, one page of {PAGE} at {TASKS} tasks was too slow — "
		f"{_crossing(_too_slow(measured))}:\n{measured.report()}"
	)

	assert not expensive, (
		f"On {backend}, {sorted(expensive)} cost more than {RATIO_CEILING:.0f}x an unordered "
		f"page. An ordering that reads rows other than the one it returns is the shape to "
		f"look for — aggregate them once per statement with a LEFT JOIN, never once per "
		f"candidate row.\n{measured.report()}"
	)


#: How many statements a whole agenda may issue, at any number of rows (`#1295`).
#:
#: **Seventeen measured**, at :data:`TASKS`, and the same on both backends: eight bucket
#: queries — one is skipped when no look-ahead is asked for — and six counts, plus the
#: prioritised-project lookup the ordering resolves once, the zone lookup `#1296` added and the
#: blocker lookup `#1287` added. The allowance above that is deliberately small, because the
#: thing this catches is not a statement or two: an N+1 here would be two thousand.
#:
#: **It was fourteen until `#1296`, fifteen until `#1265` and sixteen until `#1287`**, and the
#: number is restated each time rather than left, because a comment that says *measured* has to
#: say what was measured. `#1296` added a lookup asking which zones the whole-day rows in scope
#: were dated in, so each can be compared as a date rather than against somebody else's
#: midnight. `#1265` added the sixth count: an agenda is one person's now, so the work it leaves
#: out because it belongs to somebody else has to be counted on the scope *before* that rule,
#: which is a question the agenda's own select can no longer be asked. `#1287` added one
#: statement naming what is holding the *Waiting on somebody else* rows up — **and needed the
#: fixture changed to be seen at all**, because that bucket was empty here and the lookup
#: returns without asking anything when it is. See ``_fill``.
#:
#: **This is the guard the ratio was a proxy for**, and unlike the ratio it is a fact about the
#: code rather than about the machine it ran on.
AGENDA_STATEMENTS = 18


def test_nothing_is_excused_from_the_ratio_that_the_ratio_never_measures (
	seeded: tuple[sqlalchemy.engine.Engine, str]
) -> None:
	"""**What makes an entry in :data:`MEASURED_ANOTHER_WAY` go away** — this file's own rule.

	An excuse naming a subject nothing measures is an excuse for a thing that no longer exists,
	and it reads as a considered decision for as long as nobody checks. Every register in this
	repository is asked what removes its entries; this is that question for the newest one.

	**The work is every excused subject, and the name that used to be subtracted is gone.**
	This ran one measurement and then took ``agenda`` off the difference, so it could only ever
	have checked an entry that was not the one it measured — and with one entry in the register
	that was no check at all. `SR#1800` added two more and they were excused by nothing.
	"""

	engine, backend = seeded
	measured = _measured(
		engine, work={"agenda": _agenda, "marks": _marks, "ready": _ready}
	)

	unmeasured = set(MEASURED_ANOTHER_WAY) - set(measured.timings)

	assert not unmeasured, (
		f"{sorted(unmeasured)} is excused from the ratio on {backend} and is measured by "
		f"nothing here, so the entry is describing a subject that has gone"
	)


def test_nothing_is_excused_from_the_ratio_that_is_not_composite () -> None:
	"""**A single statement has no business being excused from a ratio over statements** —
	`SR#1827`, and this guard exists because the excuse it refuses was written and shipped.

	:data:`RATIO_CEILING` asks one question: *does this consult rows other than the ones it
	returns, once per candidate row?* There is exactly one honest answer to a subject that
	exceeds it — *this is not one statement, so the ratio is measuring the machine rather than
	the query*, which is `SR#1295`'s argument for the agenda and `SR#1800`'s for the marks. A
	subject that **is** one statement has no such defence, and excusing one is not a judgement
	about instruments; it is declining to look.

	**That is not hypothetical.** ``ready`` was measured at 36.5x on SQLite, over the ceiling,
	and excused on the day the number was taken with an argument about correlated ``EXISTS``
	evaluation strategies. It was one statement doing eight times the work it needed to, the
	absolute ceiling caught it on CI the next morning, and one clause took it to 8.4x — inside
	the ratio that had been reporting it correctly all along.

	**Derived from :data:`COMPOSITE` rather than checked against a second list**, so a subject
	that becomes composite is excusable the day it is declared and a subject that stops being
	composite loses the excuse in the same breath.
	"""

	single = sorted(set(MEASURED_ANOTHER_WAY) - set(COMPOSITE))

	assert not single, (
		f"{single} is excused from the {RATIO_CEILING:.0f}x ratio and is not in COMPOSITE, so "
		f"it is one statement being let off a measurement of statements. Either it really is "
		f"composite — declare it, with the statement count — or the ratio is telling you "
		f"something and the excuse is how you are not hearing it."
	)


def test_nothing_is_called_composite_that_this_file_never_measures (
	seeded: tuple[sqlalchemy.engine.Engine, str]
) -> None:
	"""**What makes an entry in :data:`COMPOSITE` go away** — `SR#1724`, and this file's own rule.

	That register buys a measurement a ceiling twice the ordinary one, so it is exactly the
	shape that becomes somewhere to park anything that went red. An entry naming a subject
	nothing measures is an entry for a thing that no longer exists, and it reads as a considered
	decision for as long as nobody checks.

	**The reason each entry carries is a statement count**, which is what keeps this honest: a
	view is composite because of the work it does, and :data:`AGENDA_STATEMENTS` is what holds
	that claim. A measurement that stopped issuing seventeen statements would fail there rather
	than here — the two guards are the pair, not one with a spare.

	**The work is every composite this file knows how to run**, rather than the one that
	happened to exist when this was written. Naming a single view here made the register
	half-checked: adding ``marks`` for `SR#1800` failed this test on its first run, correctly,
	because a second entry was excused by a guard that only ever looked at the first.
	"""

	engine, backend = seeded
	measured = _measured(engine, work={"agenda": _agenda, "marks": _marks})

	unmeasured = sorted(set(COMPOSITE) - set(measured.timings))

	assert not unmeasured, (
		f"{unmeasured} takes the composite ceiling on {backend} and is measured by nothing "
		f"here, so the entry is describing a subject that has gone"
	)


def test_a_composite_view_asks_a_bounded_number_of_questions (
	seeded: tuple[sqlalchemy.engine.Engine, str]
) -> None:
	"""**`SR#1295`.** The N+1 measured directly, for a subject the ratio does not fit.

	This file watches for *a listing that consults rows other than the ones it returns, once per
	candidate*, and uses cost against an unordered page as the proxy for it. That proxy is good
	for one query returning one page and poor for the agenda, which is legitimately fourteen
	statements returning 184 rows — so its ratio measured 17.3x on a workstation and 30.9x on a
	CI runner **on the same commit**, while the baseline moved 1.45x.

	**Counting the statements cannot move like that.** Fourteen is a property of the code. A
	query per candidate at :data:`TASKS` rows would be two thousand, so this fails by three
	orders of magnitude rather than by a margin somebody has to judge.

	**Falsified by putting a query inside the loop over a bucket's rows**, and the result is a
	stronger argument than the one this was written from: **198 questions for 184 rows, and the
	ratio test passed**. A real N+1, invisible to the instrument that exists to catch it,
	because on an unloaded machine two hundred trivial lookups cost less than the margin the
	ceiling allows. This fails it by an order of magnitude.
	"""

	engine, backend = seeded
	asked: list[str] = []

	def count (
		conn: typing.Any,
		cursor: typing.Any,
		statement: str,
		parameters: typing.Any,
		context: typing.Any,
		executemany: bool,
	) -> None:
		"""Note one statement the agenda sent."""

		asked.append(statement)

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

		sqlalchemy.event.listen(engine, "before_cursor_execute", count)

		try:
			built = _agenda(context)

		finally:
			sqlalchemy.event.remove(engine, "before_cursor_execute", count)

	shown = sum(len(getattr(built, bucket)) for bucket in subroutine.domain.agenda.BUCKETS)

	# **The rows are asserted too, and that is what stops this passing by measuring nothing.**
	# A build that returned an empty agenda would issue few statements and sail through — the
	# floor every scanner in this repository needs, and the one this file's own `_measured`
	# already has in the shape of a baseline.
	assert shown > PAGE, (
		f"the agenda returned {shown} rows on {backend}, which is too few for this to be "
		f"measuring a page's worth of work at all"
	)

	assert len(asked) <= AGENDA_STATEMENTS, (
		f"On {backend}, one agenda asked {len(asked)} questions for {shown} rows, against an "
		f"allowance of {AGENDA_STATEMENTS}. A query per candidate is what this catches, and it "
		f"is the thing `#1295` records the ratio as no longer able to see."
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
	work: dict[str, typing.Callable[[Context], typing.Any]] = {
		"ready": _ready,
		"agenda": _agenda,
		"marks": _marks,
		"search (no match)": _searched,
		"search with comments (no match)": _searched_including_comments,
	}

	# **The backend `#823` built, measured beside the one it replaces** (`#887`). The module
	# promises a standing before-and-after and only the *before* was here, so the whole
	# performance argument for the feature had no guard at all. Skipped where it cannot exist,
	# which is the same rule `test_search_backend.py` applies for `#871`'s reason.
	if backend == "postgresql":
		work["search, indexed (no match)"] = functools.partial(
			_searched, backend=subroutine.domain.search.NATIVE
		)
		work["search with comments, indexed (no match)"] = functools.partial(
			_searched_including_comments, backend=subroutine.domain.search.NATIVE
		)

	measured = _measured(engine, work=work)
	expensive = {
		name: measured.ratio(name)
		for name in measured.timings
		if measured.ratio(name) > RATIO_CEILING
		and name not in KNOWN_EXPENSIVE
		and name not in MEASURED_ANOTHER_WAY
	}

	assert not _too_slow(measured), (
		f"On {backend}, a listing was too slow at {TASKS} tasks — "
		f"{_crossing(_too_slow(measured))}:\n{measured.report()}"
	)

	assert not expensive, (
		f"On {backend}, {sorted(expensive)} cost more than {RATIO_CEILING:.0f}x an unordered "
		f"page. A listing that consults rows other than the ones it returns, once per "
		f"candidate, is the shape to look for.\n{measured.report()}"
	)


def test_no_excused_listing_has_quietly_come_back_under_the_ceiling (
	seeded: tuple[sqlalchemy.engine.Engine, str]
) -> None:
	"""**What makes an entry in :data:`KNOWN_EXPENSIVE` go away** — the question every excuse
	list in this repository is required to answer (`#405`).

	An excuse that outlives its defect is worse than none: it reads as a considered decision,
	the number stops being looked at, and the item it names stays open because nobody notices it
	is finished. Three of those were found at once when this rule was first applied.

	So a listing that is excused and is *inside* the ceiling fails here, naming the entry to
	delete. **It has already done that once**: `#892` was excused at 63.7x on the afternoon it
	was found and fixed the same day, and this is what would have refused to let the entry
	outlive it.

	**The list is empty now, and the mechanism stays.** An excuse arriving without its guard is
	how three stale ones survived here before, so the pair is kept together rather than the
	whole thing being deleted with its last entry.

	**Skipped where nothing being excused can run.** Every current entry is a PostgreSQL
	measurement; on SQLite the names are absent from the timings and there is nothing to check.
	"""

	engine, backend = seeded

	if not KNOWN_EXPENSIVE:
		pytest.skip("nothing is excused, so there is no entry that could have gone stale")

	if backend != "postgresql":
		pytest.skip("every excused listing is measured on PostgreSQL only")

	measured = _measured(
		engine,
		work={
			"search, indexed (no match)": functools.partial(
				_searched, backend=subroutine.domain.search.NATIVE
			),
			"search with comments, indexed (no match)": functools.partial(
				_searched_including_comments, backend=subroutine.domain.search.NATIVE
			),
		},
	)

	excused = set(KNOWN_EXPENSIVE) & set(measured.timings)

	assert excused, (
		f"none of {sorted(KNOWN_EXPENSIVE)} was measured here, so the excuses are being kept "
		f"for listings this test cannot see and nothing will ever retire them"
	)

	settled = {
		name: measured.ratio(name) for name in excused if measured.ratio(name) <= RATIO_CEILING
	}

	assert not settled, (
		f"On {backend}, {sorted(settled)} now cost under {RATIO_CEILING:.0f}x — delete the "
		f"entry from KNOWN_EXPENSIVE, and close the item it names.\n{measured.report()}"
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
		comment = subroutine.db.models.activity.Comment
		live = session.scalar(
			sqlalchemy.select(sqlalchemy.func.count())
			.select_from(comment)
			.where(comment.deleted_at.is_(None))
		)
		deleted = session.scalar(
			sqlalchemy.select(sqlalchemy.func.count())
			.select_from(comment)
			.where(comment.deleted_at.is_not(None))
		)

	assert tasks == TASKS, f"On {backend} the fixture holds {tasks} tasks, not {TASKS}."

	assert shortest == min(len(_prose(number)) for number in range(TASKS)), (
		f"On {backend} the shortest description is {shortest} characters. The search is a "
		f"scan of this prose, so a workspace seeded without it measures nothing and passes."
	)

	# Stated before the count below it, which subtracts it: a soft-deleted comment is the only
	# thing that tells a query honouring the deletion filter from one ignoring it.
	assert deleted, (
		f"On {backend} no comment is deleted, so a query that honours the deletion filter and "
		f"one that ignores it return the same rows and measure the same thing."
	)

	assert live == TASKS * COMMENTS_PER_TASK - deleted, (
		f"On {backend} the fixture holds {live} readable comments. The comment half of the "
		f"search is a correlated EXISTS over that table, so with none of them it is answered "
		f"from an empty index and costs nothing to get wrong."
	)

	assert edges == len(range(0, TASKS - 1, BLOCKED_IN)), (
		f"On {backend} the fixture holds {edges} live blocking edges. ``--ready`` and "
		f"``blocking`` are correlated EXISTS clauses over that table, so with none of them "
		f"they are answered from an empty index and cost nothing to get wrong."
	)


def test_a_prioritised_project_costs_about_what_an_unordered_page_costs (
	seeded: tuple[sqlalchemy.engine.Engine, str]
) -> None:
	"""Decision ``#982``'s bonus is a prefix test on a column already joined — `#986`.

	`#856` is the reason this has a measurement at all rather than an argument: the objection
	that killed graph inheritance was ``ORDER BY <correlated subquery>``, and the whole claim
	here is that this term is not that shape. ``scoping.readable_tasks`` already joins
	``project`` to express the visibility rules, so the ordering compares ``project.path``
	against a literal resolved once in Python.

	**Measured against the same ordering with nothing prioritised**, which is the honest
	comparison: what is being asked is what the *feature* costs, not what sorting by priority
	costs. The prefix is the fixture's own project, so the ``LIKE`` matches every row rather
	than none — a term that matches nothing is the cheap case and would be the wrong one to
	report.
	"""

	engine, backend = seeded
	factory = subroutine.db.session.create_session_factory(engine)

	with subroutine.db.session.session_scope(factory) as session:
		prefix = session.scalars(
			sqlalchemy.select(subroutine.db.models.project.Project.path)
		).first()

	assert prefix is not None, "the fixture did not create a project"

	measured = _measured(
		engine,
		work={
			"priority_score": _ordering("priority_score", subroutine.api.tasks.SORTABLE),
			"prioritised": _ordering(
				"priority_score",
				subroutine.domain.ordering.prioritising(
					subroutine.api.tasks.SORTABLE, prefixes=(prefix,)
				),
			),
		},
	)
	ratio = measured.ratio("prioritised")

	assert not _too_slow(measured), (
		f"On {backend}, one page of {PAGE} at {TASKS} tasks was too slow with a project "
		f"prioritised — {_crossing(_too_slow(measured))}:\n{measured.report()}"
	)

	assert ratio <= RATIO_CEILING, (
		f"On {backend}, a ranked page with a prioritised project cost {ratio:.1f}x an "
		f"unordered page, over the {RATIO_CEILING:.0f}x ceiling. The term must stay a prefix "
		f"test against a literal — reaching workspace.prioritised_project_id from inside the "
		f"expression makes it correlated, which is `#856`.\n{measured.report()}"
	)


def test_neither_way_of_reaching_the_prioritised_project_is_expensive (
	seeded: tuple[sqlalchemy.engine.Engine, str]
) -> None:
	"""Neither spelling is `#856`, and that is the whole of what can be measured (`#986`, `#1013`).

	**This test has now refused two claims in turn, and the second refusal was CI's.** `#986`
	said to *"falsify by making the term correlated and watching the ratio guard fire; if it
	does not, the guard is the thing to fix first"*. It does not fire — 2.2x against a 25x
	ceiling — so the first version asserted the weaker claim that the literal is the *cheaper*
	spelling. That failed on three Pythons of four the first time CI reached it: the margin is
	**1-3% and its sign flips between machines**, which is a stopwatch reading rather than a
	difference.

	**It contradicted the paragraph below it**, which is the tell worth keeping. Having
	established that the two are indistinguishable, it asserted a strict inequality between
	them; five local runs passed and proved nothing, because a quiet machine is biased
	consistently and a shared runner is not.

	**`#856`'s objection does not transfer, and the difference is what the subquery does.**
	That one aggregated over the *link* table for every candidate row — a join and a count, 972
	ms at a thousand tasks. This one is a primary-key lookup of one ``workspace`` row, which
	both planners serve from an index at a cost the page barely notices. Two things that are
	both "a correlated subquery in ``ORDER BY``" and one of them is a catastrophe while the
	other is a rounding error, which is worth knowing before the next term is written.

	**So what is asserted is what a shared runner can see**: both spellings sit an order of
	magnitude under the ceiling. The reason the design passes paths in as literals is the
	*shape* — a correlated subquery is evaluated per candidate row in principle, and `#856` is
	where that bites — rather than a measured difference at this size.

	**Not fixed by widening a tolerance**, deliberately: a margin loose enough to survive the
	noise here would be loose enough to admit the catastrophe :data:`RATIO_CEILING` already
	catches, which is a second and weaker copy of a guard that works.
	"""

	engine, backend = seeded
	factory = subroutine.db.session.create_session_factory(engine)

	with subroutine.db.session.session_scope(factory) as session:
		prefix = session.scalars(
			sqlalchemy.select(subroutine.db.models.project.Project.path)
		).first()

	assert prefix is not None, "the fixture did not create a project"

	measured = _measured(
		engine,
		work={
			"literal": _ordering(
				"priority_score",
				subroutine.domain.ordering.prioritising(
					subroutine.api.tasks.SORTABLE, prefixes=(prefix,)
				),
			),
			"correlated": _ordering(
				"correlated", {"correlated": _correlated_prioritised()}
			),
		},
	)

	for spelling in ("literal", "correlated"):
		assert measured.ratio(spelling) < RATIO_CEILING, (
			f"On {backend}, the {spelling} spelling of the prioritised-project ordering costs "
			f"{measured.ratio(spelling):.1f}x an unordered page, past the {RATIO_CEILING}x "
			f"ceiling. That is `#856`'s shape arriving here after all, and the fix is the one "
			f"`#856` took: aggregate once per statement rather than once per candidate "
			f"row.\n{measured.report()}"
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
