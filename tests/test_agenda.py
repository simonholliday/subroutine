"""Tests for the agenda's buckets, on both backends.

Two of these are S2-04's done-criteria and they were once in contradiction: a task with no
dates at all must appear in ``unscheduled``, **and** a task due in four days must appear in
what the CLI renders. The first is what stops quick capture being write-only; the second is
what §13.5b's transcript actually requires. The resolution — the look-ahead is a rendering
decision the client makes, not a window the API widens for itself — is asserted here from
both sides.

The rest hold the exclusions in place. Every one of them is a way for a task to appear in
somebody's morning when it should not: finished, deferred, deleted, a recurrence template,
or in a private project belonging to someone else.
"""

import dataclasses
import datetime
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.agenda
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.claims
import subroutine.domain.links
import subroutine.domain.ordering
import subroutine.domain.projects
import subroutine.domain.scoping
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.views

LONDON = "Europe/London"

#: Thursday 30 July 2026, mid-afternoon UTC.
NOW = datetime.datetime(2026, 7, 30, 14, 0, tzinfo=datetime.UTC)
TODAY = datetime.date(2026, 7, 30)


class World:
	"""A workspace, its owner and a project, with a shortcut for adding tasks."""

	def __init__ (self, session: sqlalchemy.orm.Session) -> None:
		"""Build a seeded workspace whose owner is the principal under test."""

		self.session = session
		self.user = subroutine.domain.users.create(
			session, username=f"si-{uuid.uuid4().hex[:8]}", timezone=LONDON
		)
		self.workspace = subroutine.domain.workspaces.create(
			session,
			slug=f"ws-{uuid.uuid4().hex[:8]}",
			title="Test workspace",
			owner=self.user,
			timezone=LONDON,
		)
		self.project = subroutine.domain.projects.create(
			session,
			workspace_id=self.workspace.id,
			key=f"P{uuid.uuid4().hex[:10].upper()}",
			title="Test project",
		)
		self.principal = subroutine.domain.authentication.Principal(user=self.user)

	def task (self, title: str, **kwargs: typing.Any) -> subroutine.db.models.work.Task:
		"""Add a task, defaulting the clock and timezone to this file's."""

		kwargs.setdefault("now", NOW)
		kwargs.setdefault("timezone", LONDON)
		project = kwargs.pop("project", self.project)

		return subroutine.domain.tasks.create(
			self.session, project=project, title=title, **kwargs
		)

	def agenda (self, **kwargs: typing.Any) -> subroutine.domain.agenda.Agenda:
		"""Build the agenda as the CLI or the API would."""

		kwargs.setdefault("now", NOW)
		kwargs.setdefault("timezone", LONDON)

		return subroutine.domain.agenda.build(
			self.session,
			principal=self.principal,
			workspace_ids=[self.workspace.id],
			**kwargs,
		)



#: The zones a whole-day row is read from in `SR#1296`'s guards, spanning UTC-7 to UTC+12.
#:
#: **London is the zone the rows are *written* in**, so it is the control: its answers are what
#: every other reader must agree with, because a whole day belongs to the day it was labelled
#: with (decision `SR#1088`).
READERS = ("Europe/London", "Etc/UTC", "America/Los_Angeles", "Pacific/Auckland")


def _bucket_of (built: subroutine.domain.agenda.Agenda, title: str) -> str:
	"""Return the bucket one title landed in, or ``none`` — `SR#1296`.

	**Which bucket rather than which rows**, because the defect moves a row between two
	sections that are both plausible, and an assertion naming one section could pass while the
	row sat in another. It also catches the worse symptom, which is a row in *no* bucket.
	"""

	for bucket in subroutine.domain.agenda.BUCKETS:
		if title in _titles(getattr(built, bucket)):
			return bucket

	return "none"


def _read_by_everybody (
	world: World, title: str, *, day: datetime.date
) -> dict[str, str]:
	"""Return which bucket a title lands in for each reader, on **the same** calendar day.

	**The day is pinned, and without that this measures the wrong thing.** Which day *today* is
	genuinely differs by reader — that is decision `SR#1088` working — so leaving it to default
	would confound *what day is it* with *which day does this row belong to*, and the second is
	the question. Met while measuring this: an unpinned probe reported a timed row moving and
	an all-day deadline not, and both readings were artefacts of the day changing underneath.
	"""

	return {
		zone: _bucket_of(world.agenda(timezone=zone, horizon_days=7, date=day), title)
		for zone in READERS
	}


def test_the_day_a_row_is_scheduled_on_is_the_same_answer_in_all_three_spellings (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1321`. One rule, three necessary spellings, and nothing had compared them.

	``scheduled_for`` has to exist as SQL for the query, as a reader over a **loaded row** for a
	cursor, and as a reader over a **rendered view** for a merged listing. That is the shape
	``priority_score`` already has, and the reason this file compares those too: three
	expressions of one rule agree until somebody edits one, and the disagreement shows up as a
	page boundary that skips or repeats rows.

	**And it must agree with the rule stated elsewhere in the domain** —
	:func:`subroutine.domain.tasks.grid_date` says *a deadline wins over a start* for a repeat's
	slot, and this is that sentence again. A fourth copy is what this asserts against.

	Three shapes, because each isolates one branch: a deadline only, a start only, and both.
	"""

	world = World(session)
	rows = [
		world.task("A deadline only", due=datetime.date(2026, 9, 4)),
		world.task("A start only", starts=datetime.date(2026, 9, 2)),
		world.task("Both", due=datetime.date(2026, 9, 6), starts=datetime.date(2026, 9, 1)),
	]
	session.flush()

	field = subroutine.domain.ordering.scheduling(
		subroutine.domain.ordering.TASK_FIELDS
	)[subroutine.domain.ordering.SCHEDULED_FOR]

	for row in rows:
		in_sql = session.scalar(
			sqlalchemy.select(field.expression).where(
				subroutine.db.models.work.Task.id == row.id
			)
		)
		off_the_row = field.read(row)
		# **Off a rendered view, and that is the whole point of the third spelling**
		# (`SR#1333`). This read :data:`VIEW_READERS` and handed it the **ORM row**, and
		# ``scheduling`` builds its ``Derived`` with the same function object — so the
		# comparison was ``scheduled_on(row) == scheduled_on(row)`` and could not fail, on the
		# one of the three that a merged listing actually uses. ``scheduled_on`` reaches its
		# columns through ``getattr(..., None)``, so a field a view stopped carrying comes back
		# ``None`` in silence and orders a whole section by the other date.
		off_the_view = subroutine.domain.ordering.VIEW_READERS[
			subroutine.domain.ordering.SCHEDULED_FOR
		](subroutine.views.task(row, subroutine.views.Vocabulary.for_tasks(session, [row])))

		assert in_sql == off_the_row == off_the_view, (
			f"{row.title!r}: the database says {in_sql}, a loaded row says {off_the_row} and a "
			f"rendered view says {off_the_view}"
		)
		assert off_the_row == subroutine.domain.tasks.grid_date(row), (
			f"{row.title!r} disagrees with the rule domain.tasks states for a repeat's slot"
		)


def test_the_look_ahead_is_one_run_of_dates_and_not_two (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1321`. *Next 7 days* listed everything with a deadline, then everything without.

	Measured on a clean instance: **28 Aug, 31 Aug, 1 Sep, 27 Aug, 29 Aug, 2 Sep** — two
	chronological runs under a heading that *is* a time window, with the date printed on every
	row so the disagreement was on the page.

	``ORDERS["upcoming"]`` was two keys. Everything carrying a deadline sorted by it; everything
	with only a start had a null one, and ``clauses`` states ``NULLS LAST`` in both directions
	deliberately (§10.3), so the whole start-only group sank below the whole deadline group.
	**Neither key was wrong and the pair was.**

	**The dates interleave on purpose.** A fixture whose starts all fall after its deadlines
	would come out in the right order under either rule, and prove nothing.
	"""

	world = World(session)
	made = {
		1: ("A deadline on the 2nd", {"due": TODAY + datetime.timedelta(days=2)}),
		2: ("A start on the 1st", {"starts": TODAY + datetime.timedelta(days=1)}),
		3: ("A deadline on the 4th", {"due": TODAY + datetime.timedelta(days=4)}),
		4: ("A start on the 3rd", {"starts": TODAY + datetime.timedelta(days=3)}),
		5: ("A start on the 5th", {"starts": TODAY + datetime.timedelta(days=5)}),
	}

	for title, fields in made.values():
		world.task(title, **fields)

	shown = _titles(world.agenda(horizon_days=7).upcoming)

	assert shown == [
		"A start on the 1st",
		"A deadline on the 2nd",
		"A start on the 3rd",
		"A deadline on the 4th",
		"A start on the 5th",
	], f"the look-ahead is not in date order: {shown}"


def test_a_whole_day_row_is_in_the_same_section_whoever_is_reading (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1296`. *Get paid* was under *Next 7 days* in the terminal and *Happening* in the browser.

	Simon met it as two surfaces disagreeing; they were two **accounts**, an hour apart, each
	answering its own question correctly. A whole-day row is stored at an edge of *its own*
	local day (§6.5), so reading it as an instant against somebody else's day boundary asks
	about clocks when the question is about dates — and decision `SR#1088` settles that a day
	is a label and belongs to the day it was labelled with, on every clock.

	**Three shapes and both columns**, because the item names one and the measurement found
	more: a whole-day start moves between *Happening*, *Today* and *Next 7 days*, and a
	whole-day **deadline** moves between *Overdue* and *Today* — which is the row a person is
	meant to act on first, silently a day out.
	"""

	world = World(session)
	tomorrow = TODAY + datetime.timedelta(days=1)
	yesterday = TODAY - datetime.timedelta(days=1)

	world.task("An event tomorrow", type_key="event", starts=tomorrow, starts_is_all_day=True)
	world.task("An event today", type_key="event", starts=TODAY, starts_is_all_day=True)
	world.task("Something starting tomorrow", starts=tomorrow, starts_is_all_day=True)
	world.task("Due today", due=TODAY, due_is_all_day=True)
	world.task("Due yesterday", due=yesterday, due_is_all_day=True)

	expected = {
		"An event tomorrow": "upcoming",
		"An event today": "occasions",
		"Something starting tomorrow": "upcoming",
		"Due today": "today",
		"Due yesterday": "overdue",
	}

	for title, belongs in expected.items():
		landed = _read_by_everybody(world, title, day=TODAY)

		assert set(landed.values()) == {belongs}, (
			f"{title!r} was written in {LONDON} and belongs in {belongs!r}, and readers "
			f"disagree: {landed}"
		)


def test_a_whole_day_event_on_today_does_not_vanish_for_a_reader_further_west (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1296`'s worst symptom, and worse than the item reports.

	**Measured: the event was in no bucket at all** for a reader west of the zone it was
	written in. Its start is the first instant of its own day, which is *before* a westward
	reader's day begins, so the overlap test dropped it; ``today`` excludes occasions by
	design; and ``upcoming`` wants a start after tonight. A row landing in the wrong section is
	a nuisance, and a row landing in none is the agenda quietly not mentioning something.

	Kept apart from the guard above because *disagreement* and *absence* are different
	failures, and an assertion that the readers agree would be satisfied by all of them losing
	it.
	"""

	world = World(session)
	world.task("Anna's birthday", type_key="event", starts=TODAY, starts_is_all_day=True)

	landed = _read_by_everybody(world, "Anna's birthday", day=TODAY)

	assert "none" not in landed.values(), (
		f"a whole-day event on today is in no section at all for some reader: {landed}"
	)


def test_a_whole_day_defer_hides_a_row_from_everybody_or_from_nobody (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1296`, one layer up from the buckets and arguably worse.

	A defer says *not until Tuesday*. It is stored at the first instant of its own local day
	(§6.5), and the agenda hid a row by reading that instant against the **reader's** day —
	so *not yet* was honoured for one person and broken for another an hour away. Measured
	before the fix: a defer to tomorrow was visible in UTC and in Los Angeles, hidden in London
	and in Auckland.

	**Found by asking what else compares a whole-day value**, rather than by the report, which
	is only about which section a row lands in. A fix that corrected the sections and left this
	would have made the agenda put every row under the right heading and still show the wrong
	set of rows.

	``readiness.undeferred`` is unchanged in meaning and takes the boundary as an expression, so
	``?ready=`` goes on asking *can I start this now* — which is honestly an instant.
	"""

	world = World(session)
	tomorrow = TODAY + datetime.timedelta(days=1)

	world.task("Not until tomorrow", snooze=tomorrow, snoozed_is_all_day=True)
	world.task("Back from today", snooze=TODAY, snoozed_is_all_day=True)

	seen = {
		zone: sorted(
			row.title
			for bucket in subroutine.domain.agenda.BUCKETS
			for row in getattr(
				world.agenda(timezone=zone, horizon_days=7, date=TODAY), bucket
			)
		)
		for zone in READERS
	}

	assert list(seen.values()).count(["Back from today"]) == len(READERS), (
		f"a whole-day defer came round on different days for different readers: {seen}"
	)


def test_a_row_with_a_time_still_belongs_to_the_reader_s_own_clock (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The other half of decision `SR#1088`, and what stops `SR#1296`'s fix over-reaching.

	*A day is a label, a moment is a point in time.* A meeting at 22:00 UTC genuinely has not
	begun yet where it is already tomorrow, so it **must** move between sections as the reader
	changes — and a fix that made every date behave like a label would have taken that away
	while every assertion about whole days went on passing.

	The instant is chosen to sit inside one reader's day and outside another's, because a time
	in the middle of the afternoon is the same section for everybody and would prove nothing.
	"""

	world = World(session)
	world.task(
		"A meeting at the edge of the day",
		type_key="event",
		starts=datetime.datetime(2026, 7, 30, 22, 0, tzinfo=datetime.UTC),
		starts_is_all_day=False,
	)

	landed = _read_by_everybody(world, "A meeting at the edge of the day", day=TODAY)

	assert len(set(landed.values())) > 1, (
		f"a timed row answered the same for every reader, so an instant has been made to "
		f"behave like a label: {landed}"
	)
	assert landed["Pacific/Auckland"] == "upcoming", (
		f"22:00 UTC has not begun where it is already tomorrow: {landed}"
	)


def test_a_whole_day_row_dated_somewhere_neither_reader_is (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The zone branch itself, which the guards above reach only for the reader's own zone.

	A row carries the zone **it** was written in, which is whoever filed it — so the interesting
	case is a third zone belonging to neither the writer of the other rows nor the reader. It is
	also what falsifies a fix that special-cased *the reader's zone against London*.
	"""

	world = World(session)
	world.task(
		"A holiday booked in Tokyo",
		type_key="event",
		starts=TODAY,
		starts_is_all_day=True,
		timezone="Asia/Tokyo",
	)

	landed = _read_by_everybody(world, "A holiday booked in Tokyo", day=TODAY)

	assert set(landed.values()) == {"occasions"}, (
		f"a whole day labelled with today is not today for everybody: {landed}"
	)


def test_an_agenda_can_be_narrowed_to_a_project_and_everything_under_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#1218`'s sibling: `#1215`, and the half `GET /v1/agenda` could not answer at all.

	The endpoint took a workspace and nothing narrower, so the browser could point an agenda at
	a workspace and never at a project — which is the scope a person actually works in.

	**The sub-project is what makes this falsifiable.** A named project means that area of work
	and not that one node (`#320`), so comparing ``project_id`` to a single id would pass every
	assertion here except the one about the child — and a parent whose agenda excluded its own
	sub-projects would answer *nothing due today* about a tree full of deadlines.
	"""

	world = World(session)
	under = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key=f"C{uuid.uuid4().hex[:10].upper()}",
		title="A sub-project",
		parent=world.project,
	)
	elsewhere = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key=f"E{uuid.uuid4().hex[:10].upper()}",
		title="Another area of work",
	)

	world.task("In the project")
	world.task("In the sub-project", project=under)
	world.task("Somewhere else entirely", project=elsewhere)

	assert sorted(_titles(world.agenda().unscheduled)) == [
		"In the project", "In the sub-project", "Somewhere else entirely"
	], "the unnarrowed agenda no longer spans the workspace"

	narrowed = world.agenda(project=world.project)

	assert sorted(_titles(narrowed.unscheduled)) == ["In the project", "In the sub-project"], (
		"an agenda narrowed to a project either misses its sub-projects or leaks another "
		"project's work"
	)

	# **The total follows the narrowing.** It is counted off the same select, so a version that
	# filtered the rows and not the count would report *and 1 more* about work that is not in
	# this project at all — the shape a cap gets wrong when it is bolted on afterwards.
	assert narrowed.unscheduled_total == 2, (
		f"the total counts rows the narrowing excluded: {narrowed.unscheduled_total}"
	)

	assert _titles(world.agenda(project=elsewhere).unscheduled) == [
		"Somewhere else entirely"
	], "narrowing to a leaf project does not narrow"


def test_the_agenda_accounts_for_every_row_the_listing_at_that_scope_holds (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#1215`, Simon's decision of 2026-08-24, and the guard the footer is worth nothing without.

	The agenda now sits beside ``?view=list`` at the same address, so a reader can flip between
	two answers about one place and see different numbers of rows. `#649`'s amendment is that an
	arrangement drawing its rows from another endpoint must say what it left behind — and a
	*sentence* saying so decays, where this cannot: the four counts are added to the rows the
	agenda actually shows and compared against the listing at the same scope.

	**So a fifth exclusion added later is impossible to add silently.** It stops adding up and
	this fails, naming the residual. That is the property, and it is why the compact one-line
	footer Simon chose is not a compromise: what makes the accounting trustworthy is the
	arithmetic, not the number of lines it is printed on.

	**Every exclusion is represented, deliberately including one that hides nothing here.** A
	fixture where a cause contributes zero cannot tell *this count is right* from *this count is
	never read*, which is the shape this file has met before.

	**It has already caught one** (`SR#1236`): an occasion that has gone by leaves the agenda
	with nobody acting on it, and a listing at this scope still shows it, so the sum stopped
	adding up until ``passed_total`` existed to say so.
	"""

	world = World(session)
	asleep = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key=f"H{uuid.uuid4().hex[:10].upper()}",
		title="Put down for now",
	)

	# **Its status is what makes it not running**, read off the category rather than the key
	# (`#983`) — a workspace may rename `on_hold`, and `#1157` is what that costs when a rule
	# compares the label.
	subroutine.domain.projects.update(
		session, project=asleep, status_key="on_hold", actor=world.principal
	)

	world.task("Ordinary undated work")
	world.task("Also undated")
	world.task("Overdue", due=datetime.date(2026, 7, 27))
	world.task("Beyond the window", due=datetime.date(2026, 11, 30))
	world.task("Not until next month", snooze=datetime.date(2026, 9, 30))
	# **And one deferred to tomorrow, which is the only place the defect lives** (`SR#1328`).
	# `_visible` hides a row past the start of the shown day in the row's own zone and
	# `_deferred` counted it past the end of that day in the **reader's**, so the gap between
	# the two boundaries is under a day wide. A defer two months out is on the far side of
	# both and cannot see it — measured, by putting the old boundary back and watching this
	# guard stay green with only the row above in it.
	world.task("Not until tomorrow", snooze=TODAY + datetime.timedelta(days=1))
	world.task("In the project nobody is running", project=asleep)
	# **The fifth exclusion, represented like the other four** (`SR#1236`). A passed event is
	# not *completed*, so the listing below still holds it and this view does not — which is
	# precisely the residual this arithmetic exists to make impossible to leave unreported.
	world.task(
		"A birthday in March",
		type_key="event",
		starts=datetime.date(2026, 3, 14),
		starts_is_all_day=True,
	)

	# **The second cap, represented for the first one's reason** (`SR#1285`). Two rows more
	# than the limit, so the cap bites and the arithmetic has to account for what it hid — a
	# fixture with an empty `blocked_by_others` cannot tell *this count is right* from *this
	# count is never read*, and until this existed every test in this file left it at nought.
	other = _somebody_else(world)

	for number in range(subroutine.domain.agenda.DEFAULT_BLOCKED_LIMIT + 2):
		theirs = world.task(f"Somebody else's {number}")
		theirs.assignee_id = other.id
		_blocks(world, theirs, world.task(f"Held up {number}"))

	session.flush()

	# The listing at the same scope: live, unfinished work, which is what `?view=list` shows
	# with no selection — the page a reader flips to.
	listed = session.scalars(
		subroutine.domain.scoping.readable_tasks(
			world.principal, workspace_ids=[world.workspace.id], include_completed=False
		)
	).all()

	# **Read from every zone, on the same pinned day** (`SR#1328`). The rows are written in
	# London and the arithmetic has to hold for somebody reading them from anywhere: this
	# guard built and read in one zone, where every per-row boundary `SR#1296` introduced
	# collapses onto the reader's own, so it could not see a row hidden by one boundary and
	# counted against another. That is exactly what happened to the whole-day defer, and the
	# zone tests beside this one assert on *which rows are visible* and never on the totals —
	# so between them the two guards covered both halves and neither covered the join.
	#
	# **The day is pinned for `_read_by_everybody`'s reason**: which day *today* is genuinely
	# differs by reader, and leaving it to default would confound that with what is measured.
	for zone in READERS:
		agenda = world.agenda(
			timezone=zone, horizon_days=7, unscheduled_limit=1, date=TODAY
		)

		shown = sum(
			len(getattr(agenda, bucket)) for bucket in subroutine.views.AGENDA_BUCKETS
		)
		accounted = (
			shown
			+ max(0, agenda.unscheduled_total - len(agenda.unscheduled))
			+ max(0, agenda.blocked_by_others_total - len(agenda.blocked_by_others))
			+ agenda.later_total
			+ agenda.deferred_total
			+ agenda.paused_total
			+ agenda.passed_total
			# **The sixth, and this guard is what demanded it exist** (`SR#1265`, decision
			# `SR#1267` §1). The assignee narrowing landed in `_scoped` and seven readable
			# rows left the page with nothing saying so — 15 accounted against 22 listed,
			# reported here before any surface had drawn it. The alternative on offer was to
			# narrow `listed` by assignee too, which would have made the guard agree with
			# the change by definition and blinded it to the next silent exclusion.
			+ agenda.assigned_elsewhere_total
		)

		assert accounted == len(listed), (
			f"read from {zone}, the agenda accounts for {accounted} rows and the listing at "
			f"the same scope holds {len(listed)}. Something is being held back that nothing "
			f"reports — every exclusion has to be a count a reader can see, which is `#649`'s "
			f"amendment and the whole reason this arithmetic exists."
		)

		# **And each count is non-zero**, so the equality above cannot be satisfied by a scan
		# that reads nothing. `unscheduled_limit=1` forces the cap to bite on two undated rows.
		assert agenda.deferred_total == 2, (zone, agenda.deferred_total)
		assert agenda.paused_total == 1, (zone, agenda.paused_total)
		assert agenda.later_total == 1, (zone, agenda.later_total)
		assert agenda.passed_total == 1, (zone, agenda.passed_total)
		assert agenda.unscheduled_total > len(agenda.unscheduled), (
			zone, agenda.unscheduled_total
		)
		assert agenda.blocked_by_others_total > len(agenda.blocked_by_others), (
			zone, agenda.blocked_by_others_total
		)


def test_an_occasion_gets_its_own_section_and_leaves_it_when_the_day_has_gone (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1236`, decision `SR#1235` §4 — the measured defect, and the three shapes it has.

	A birthday planned to a date that has passed sat in **Today**, every day, for ever: the
	bucket's clause is ``starts_at <= day_end`` and a past start is kept there deliberately
	(`SR#927` M-18), which is right for work you meant to begin and wrong for a day that went
	by. Driven on a disposable instance on 2026-08-25 against a birthday dated 14 March, it was
	in Today, in ``--ready``, and the agenda's own tip read ``subroutine done 2``.

	**All three of Simon's shapes are here, because each is over at a different moment**: a
	single all-day date, an all-day span, and a timed span. A fixture holding only the first
	cannot tell a rule about ``starts_at`` from a rule about the whole period.

	**And an ordinary task with a start in the past is asserted to stay in Today**, which is
	what makes this falsifiable in the other direction: excluding by *dates* rather than by
	*type* would pass every assertion above and quietly undo `SR#927` M-18.
	"""

	world = World(session)

	birthday = world.task(
		"Anna's birthday", type_key="event", starts=TODAY, starts_is_all_day=True
	)
	world.task(
		"Anna's birthday last March",
		type_key="event",
		starts=datetime.date(2026, 3, 14),
		starts_is_all_day=True,
	)
	world.task(
		"A fortnight off",
		type_key="event",
		starts=datetime.date(2026, 7, 27),
		ends=datetime.date(2026, 8, 7),
		starts_is_all_day=True,
	)
	world.task(
		"Code freeze",
		type_key="event",
		starts=datetime.datetime(2026, 7, 29, 17, 0, tzinfo=datetime.UTC),
		ends=datetime.datetime(2026, 7, 31, 8, 0, tzinfo=datetime.UTC),
	)
	world.task("Meant to start it on Monday", starts=datetime.date(2026, 7, 27))

	session.flush()

	agenda = world.agenda(horizon_days=7)

	assert sorted(_titles(agenda.occasions)) == [
		"A fortnight off", "Anna's birthday", "Code freeze"
	], f"the occasions section holds {_titles(agenda.occasions)}"

	assert _titles(agenda.today) == ["Meant to start it on Monday"], (
		f"Today holds {_titles(agenda.today)} — an occasion is in it, or an ordinary task with "
		f"a start in the past has been thrown out with them"
	)

	# **In no bucket at all, with nobody having acted.** That is `SR#1235` §3 — a passed event
	# is derived rather than written, so `completed_at` is still null and no scheduler ran.
	assert birthday.completed_at is None
	assert "Anna's birthday last March" not in [
		title
		for bucket in subroutine.views.AGENDA_BUCKETS
		for title in _titles(getattr(agenda, bucket))
	], "a birthday five months past is still on the agenda somewhere"

	assert agenda.passed_total == 1, (
		f"{agenda.passed_total} occasions reported as already happened — a listing at this "
		f"scope still shows the March birthday, so a day that drops it silently is the "
		f"unexplained difference `SR#649`'s amendment forbids"
	)

	# **Not counted as *further out*, which is the word that would have been false.** It is
	# dated and unshown, which is `later_total`'s whole predicate, so the two counts have to
	# partition those rows rather than both claim them.
	assert agenda.later_total == 0, (
		f"{agenda.later_total} reported as dated further out, and the only candidate is a "
		f"birthday five months behind"
	)


def test_an_occasion_leaves_the_section_the_morning_after_and_not_before (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The boundary, on the shape whose boundary is easiest to get wrong.

	An all-day start is stored at the **first** instant of its day (§6.5), so the obvious
	predicate — *is its end behind now* — calls somebody's birthday passed at one minute past
	midnight on their birthday. The agenda answers by overlap with the day being shown, which is
	exact; :func:`subroutine.domain.readiness.passed` answers against the clock and subtracts a
	day for this case, and the two have to agree about which day a birthday belongs to.

	**Both sides asserted, because one of them is the good news.** A version that dropped it a
	day early and a version that kept it a day late are the same one-line mistake with opposite
	signs, and asserting only the disappearance would pass the first.
	"""

	world = World(session)

	world.task("Anna's birthday", type_key="event", starts=TODAY, starts_is_all_day=True)
	session.flush()

	assert _titles(world.agenda().occasions) == ["Anna's birthday"], "it is not there on the day"

	assert _titles(world.agenda(date=TODAY - datetime.timedelta(days=1)).occasions) == [], (
		"it is on the agenda the day before it happens"
	)

	assert _titles(world.agenda(date=TODAY + datetime.timedelta(days=1)).occasions) == [], (
		"it is still on the agenda the morning after"
	)


def _titles (tasks: tuple[subroutine.db.models.work.Task, ...]) -> list[str]:
	"""Return the titles in a bucket, for readable assertions."""

	return [task.title for task in tasks]


def test_a_task_with_no_dates_at_all_appears_in_unscheduled (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Half of S2-04's done-criterion, and the bucket quick capture depends on.

	Most personal tasks are captured with no date. Without this bucket they would never
	appear in the agenda at any point, ever, and `subroutine add` would be a write-only
	feature — the single easiest way to build a to-do list nobody can use (docs/design.md §8.6).
	"""

	world = World(session)
	world.task("Buy milk")

	agenda = world.agenda()

	assert _titles(agenda.unscheduled) == ["Buy milk"]
	assert agenda.unscheduled_total == 1


def test_a_task_due_in_four_days_appears_in_the_look_ahead (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The other half, and the one the review caught the specification getting wrong.

	§12.1's transcript captures "Call the dentist before Sunday" — which sets `due_at` and
	nothing else — and then shows it. By the bucket definitions that task is neither
	`today` nor `unscheduled`; it is `upcoming`, which the API hides by default. A CLI that
	faithfully rendered the API's default would have printed "Nothing due today." and
	stopped, and §13.5b's fourth command would have had nothing to address.
	"""

	world = World(session)
	world.task("Call the dentist", due=datetime.date(2026, 8, 2))

	# What the API returns by default: the task is nowhere.
	api = world.agenda()

	assert api.is_empty

	# What the CLI renders: the same query, with the look-ahead asked for.
	cli = world.agenda(horizon_days=subroutine.domain.agenda.DEFAULT_HORIZON_DAYS)

	assert _titles(cli.upcoming) == ["Call the dentist"]


def test_the_two_criteria_hold_at_the_same_time (session: sqlalchemy.orm.Session) -> None:
	"""They were in contradiction once. Asserting them together is what keeps them honest."""

	world = World(session)
	world.task("Buy milk")
	world.task("Call the dentist", due=datetime.date(2026, 8, 2))

	agenda = world.agenda(horizon_days=7)

	assert _titles(agenda.unscheduled) == ["Buy milk"]
	assert _titles(agenda.upcoming) == ["Call the dentist"]


def test_each_bucket_holds_what_the_specification_says (
	session: sqlalchemy.orm.Session,
) -> None:
	"""docs/design.md §8.6's table, in one pass."""

	world = World(session)
	world.task("Late", due=datetime.date(2026, 7, 28))
	world.task("Due today", due=TODAY)
	world.task("Planned today", starts=TODAY)
	world.task("Planned yesterday", starts=datetime.date(2026, 7, 29))
	world.task("Next week", due=datetime.date(2026, 8, 4))
	world.task("Far off", due=datetime.date(2026, 12, 25))
	world.task("No dates")

	agenda = world.agenda(horizon_days=7)

	assert _titles(agenda.overdue) == ["Late"]
	assert sorted(_titles(agenda.today)) == ["Due today", "Planned today", "Planned yesterday"]
	assert _titles(agenda.upcoming) == ["Next week"]
	assert _titles(agenda.unscheduled) == ["No dates"]


def test_a_task_appears_in_exactly_one_bucket (session: sqlalchemy.orm.Session) -> None:
	"""Overdue *and* planned for today is one task, and the more urgent truth wins.

	The buckets are disjoint by priority. Showing the same task twice in a five-line
	summary is how a five-line summary stops being read.
	"""

	world = World(session)
	world.task("Late and planned", due=datetime.date(2026, 7, 28), starts=TODAY)

	agenda = world.agenda(horizon_days=7)

	assert _titles(agenda.overdue) == ["Late and planned"]
	assert agenda.today == ()
	assert agenda.upcoming == ()


def test_an_all_day_deadline_today_lands_in_today (session: sqlalchemy.orm.Session) -> None:
	"""The boundaries here and in §6.5 must be the same, or it falls outside by a microsecond."""

	world = World(session)
	world.task("Due all day today", due=TODAY)

	agenda = world.agenda()

	assert _titles(agenda.today) == ["Due all day today"]
	assert agenda.overdue == ()


@pytest.mark.parametrize(
	("label", "kwargs"),
	[
		("deferred", {"snooze": datetime.date(2026, 9, 1)}),
		("planned but deferred", {"starts": TODAY, "snooze": datetime.date(2026, 9, 1)}),
	],
)
def test_a_deferred_task_is_hidden_from_every_bucket (
	session: sqlalchemy.orm.Session, label: str, kwargs: dict[str, typing.Any]
) -> None:
	""""Don't show me the renewal form until March" has to actually hide it (docs/design.md §6.5)."""

	world = World(session)
	world.task(label, **kwargs)

	agenda = world.agenda(horizon_days=7)

	assert agenda.is_empty


def test_a_defer_that_has_already_lifted_does_not_hide_anything (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The other side of the same rule: a past `snoozed_until` is not a filter."""

	world = World(session)
	world.task("Now actionable", snooze=datetime.date(2026, 7, 1), starts=TODAY)

	assert _titles(world.agenda().today) == ["Now actionable"]


def test_an_appointment_later_today_is_on_today_s_agenda (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#771`, and the flagship command was wrong about the flagship question.

	Simon's dentist appointment at 14:00 was missing from his morning. Every bucket narrowed by
	``snoozed_until <= now``, so it was hidden from **all four at once** — which is why a workspace
	holding one open task reported ``unscheduled_total`` of zero.

	**The capture grammar makes it systematic rather than rare.** ``Dentist appointment,
	2pm-3pm`` sets a ``snoozed_until`` of 14:00 as well as the deadline and the day, so every
	appointment written with a time was invisible until it began. That is §1.4's audience on
	§1.4's path, and the module's own docstring names the dentist as its example.

	**A defer hides something until a day, not until an o'clock**: ``starts_at`` of today is
	the reader saying *this belongs to this day*, and a defer inside that day may not overrule
	it.

	**No existing test could see this**, and the reason is worth more than the fix. Both sides
	of the rule were held — deferred to September is hidden, lifted in July is shown — and both
	put the defer in a *different day* from the clock. The defect lives strictly inside one day,
	so a fixture that never builds one cannot reach it: *one of a thing* in its calendar form.
	"""

	world = World(session)

	# 17:00 London on the day `NOW` falls in, which is two hours after `NOW` and still today.
	world.task(
		"Dentist appointment",
		starts=TODAY,
		snooze=datetime.datetime(2026, 7, 30, 16, 0, tzinfo=datetime.UTC),
	)

	assert _titles(world.agenda().today) == ["Dentist appointment"], (
		"an appointment later today is hidden from today's agenda until it begins"
	)


def test_a_defer_into_tomorrow_still_hides_a_task_planned_for_today (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The bound on `SR#771`, without which the fix is "show everything".

	The horizon is the end of the day being shown, so it moves from an instant to a day and no
	further. A quarter of an hour past midnight is the case that separates the two — a defer
	the reader means for tomorrow, on a task they planned for today.
	"""

	world = World(session)

	# 00:15 London on the 31st, which is 23:15 UTC on the 30th — so a check comparing UTC
	# calendar days rather than local ones would call this today and let it through.
	world.task(
		"Not until tomorrow",
		starts=TODAY,
		snooze=datetime.datetime(2026, 7, 30, 23, 15, tzinfo=datetime.UTC),
	)

	assert world.agenda().is_empty, "a defer into tomorrow no longer hides anything"


def test_a_finished_task_is_gone_from_the_agenda (session: sqlalchemy.orm.Session) -> None:
	"""An agenda that keeps showing completed work is one nobody reads."""

	world = World(session)
	task = world.task("Already done", starts=TODAY)

	subroutine.domain.tasks.update(session, task, status_key="done", now=NOW)

	assert world.agenda(horizon_days=7).is_empty


def test_a_recurrence_template_never_appears (session: sqlalchemy.orm.Session) -> None:
	"""Templates are excluded from every list, search, agenda and rollup (docs/design.md §6.7)."""

	world = World(session)
	task = world.task("Template", starts=TODAY)
	task.is_template = True
	session.flush()

	assert world.agenda().is_empty


def test_a_task_in_someone_elses_private_project_is_not_shown (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§7.3a reaches the agenda too, and a workspace owner does not get to look.

	The agenda is exactly where a leak like this would be least noticed — a title in a
	list, among the caller's own work.
	"""

	world = World(session)
	private = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="secret",
		title="Private project",
		visibility="private",
	)
	world.task("Confidential", project=private, starts=TODAY)
	world.task("Ordinary", starts=TODAY)

	assert _titles(world.agenda().today) == ["Ordinary"]


def test_a_private_project_is_shown_to_its_members (session: sqlalchemy.orm.Session) -> None:
	"""The membership row is what makes a private project reachable (docs/design.md §7.3a)."""

	world = World(session)
	private = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="secret",
		title="Private project",
		visibility="private",
	)
	session.add(
		subroutine.db.models.project.ProjectMember(
			project_id=private.id, user_id=world.user.id, workspace_id=world.workspace.id
		)
	)
	session.flush()
	world.task("Confidential", project=private, starts=TODAY)

	assert _titles(world.agenda().today) == ["Confidential"]


def test_another_workspace_is_never_included (session: sqlalchemy.orm.Session) -> None:
	"""Every query is scoped by workspace, and the agenda is not an exception."""

	mine = World(session)
	theirs = World(session)

	theirs.task("Not mine", starts=TODAY)
	mine.task("Mine", starts=TODAY)

	assert _titles(mine.agenda().today) == ["Mine"]


def test_the_unscheduled_bucket_is_capped_and_says_so (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A person with two hundred captured tasks wants the reminder, not the pile."""

	world = World(session)

	for index in range(8):
		world.task(f"Undated {index}")

	agenda = world.agenda(unscheduled_limit=3)

	assert len(agenda.unscheduled) == 3
	assert agenda.unscheduled_total == 8


def test_overdue_runs_oldest_first (session: sqlalchemy.orm.Session) -> None:
	"""The most overdue thing is the one to look at first."""

	world = World(session)
	world.task("Two days late", due=datetime.date(2026, 7, 28))
	world.task("A month late", due=datetime.date(2026, 6, 30))
	world.task("One day late", due=datetime.date(2026, 7, 29))

	assert _titles(world.agenda().overdue) == [
		"A month late",
		"Two days late",
		"One day late",
	]


def test_today_orders_deadlines_before_undated_plans (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Explicit NULLS LAST, because the two backends disagree about the default.

	SQLite sorts NULLs first and PostgreSQL sorts them last, so without saying which we
	want, a planned-but-undated task appears at opposite ends of this list depending on
	which backend answered (docs/design.md §10.3). This test runs on both, which is the only
	reason it means anything.
	"""

	world = World(session)
	world.task("Planned, no deadline", starts=TODAY)
	world.task("Due today", due=TODAY)

	assert _titles(world.agenda().today) == ["Due today", "Planned, no deadline"]


def test_the_horizon_is_a_boundary_not_a_suggestion (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Seven days means seven days, inclusive of the last one."""

	world = World(session)
	world.task("Inside", due=datetime.date(2026, 8, 2))
	world.task("On the edge", due=datetime.date(2026, 8, 6))
	world.task("Outside", due=datetime.date(2026, 8, 7))

	agenda = world.agenda(horizon_days=7)

	assert sorted(_titles(agenda.upcoming)) == ["Inside", "On the edge"]


def test_the_day_is_computed_where_the_caller_is (session: sqlalchemy.orm.Session) -> None:
	""""Today" is the asker's today (docs/design.md §8.6, §9.3).

	At 15:00 UTC on the 30th it is still the 30th in London and already the 31st in Sydney,
	so a task planned for the 31st is on their agenda and not on London's.

	The first draft of this used 23:30 UTC, which proves nothing: London is on British
	Summer Time in July, so 23:30 UTC is already the 31st there too and both agendas agreed.
	"""

	world = World(session)
	world.task("Tomorrow in London", starts=datetime.date(2026, 7, 31))

	late = datetime.datetime(2026, 7, 30, 15, 0, tzinfo=datetime.UTC)

	assert world.agenda(now=late).today == ()
	assert _titles(world.agenda(now=late, timezone="Australia/Sydney").today) == [
		"Tomorrow in London"
	]


def test_the_agenda_can_be_narrowed_to_one_workspace (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§8.6: every other listing took ``workspace_id`` and the agenda did not.

	The gap showed up the first time this project used itself — a personal to-do list and a
	project backlog in one instance, and seven undated project tasks above "buy salad". Spanning
	everything stays the default; naming a workspace is how you ask for half.
	"""

	first = subroutine.domain.bootstrap.initialise(
		session, username=f"a-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	second = subroutine.domain.workspaces.create(
		session, slug=f"work-{uuid.uuid4().hex[:6]}", title="Work", owner=first.user
	)
	session.flush()

	subroutine.domain.tasks.create_from_text(
		session, workspace=first.workspace, text="Personal thing", actor=None
	)
	# A bare workspace has no Inbox — that is `bootstrap.initialise`'s doing — so this names
	# its project, which is also the parameter the switch found missing from capture.
	elsewhere = subroutine.domain.projects.create(
		session,
		workspace_id=second.id,
		key="wrk",
		title="Work",
		owner_id=first.user.id,
	)
	subroutine.domain.tasks.create_from_text(
		session, workspace=second, text="Work thing", project=elsewhere, actor=None
	)
	session.flush()

	principal = subroutine.domain.authentication.Principal(user=first.user, token=None)
	everywhere = subroutine.domain.agenda.build(
		session,
		principal=principal,
		workspace_ids=[first.workspace.id, second.id],
		now=subroutine.db.types.utcnow(),
		timezone="Europe/London",
	)
	narrowed = subroutine.domain.agenda.build(
		session,
		principal=principal,
		workspace_ids=[first.workspace.id],
		now=subroutine.db.types.utcnow(),
		timezone="Europe/London",
	)

	assert {task.title for task in everywhere.unscheduled} == {
		"Personal thing",
		"Work thing",
	}
	assert {task.title for task in narrowed.unscheduled} == {"Personal thing"}
	assert narrowed.unscheduled_total == 1, "the total must narrow with the rows"


def test_every_started_item_is_on_the_agenda_however_many_there_are (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#888`, and this records a decision rather than guarding a defect.**

	The cold review of 2026-08-14 raised the `in_progress` bucket as unbounded, which it is —
	`unscheduled` takes a limit and this does not. Simon's decision is that it stays that way:

	> a user viewing their own agenda should see all in-progress items. Hiding some risks
	> misleading the user. They may start others instead of finishing items we didn't show them.

	**Measured before deciding**: 2 in-progress against 179 unscheduled on the served instance,
	and the two are bounded by different things — a backlog has no ceiling, where started work
	is bounded by how many workers there are times how much each holds at once.

	**More rows than `unscheduled_limit`**, so a cap applied by copying that bucket's shape
	fails here rather than passing on a fixture too small to notice.
	"""

	world = World(session)
	started = subroutine.domain.tasks.status_for(session, world.workspace.id, "in_progress")

	for number in range(subroutine.domain.agenda.DEFAULT_UNSCHEDULED_LIMIT + 5):
		task = world.task(f"Started {number}")
		task.status_id = started.id

	session.flush()

	agenda = world.agenda()

	assert len(agenda.in_progress) == subroutine.domain.agenda.DEFAULT_UNSCHEDULED_LIMIT + 5, (
		f"the agenda showed {len(agenda.in_progress)} of "
		f"{subroutine.domain.agenda.DEFAULT_UNSCHEDULED_LIMIT + 5} started items"
	)


def _blocks (world: World, blocker: typing.Any, blocked: typing.Any) -> None:
	"""Say that one task holds another up, through the domain a client would reach."""

	def end (task: typing.Any) -> subroutine.domain.links.End:
		"""Return one end of a link, as a client's resolved form of this task."""

		return subroutine.domain.links.End(
			entity_type="task",
			id=task.id,
			ref=task.ref,
			title=task.title,
			project_id=task.project_id,
		)

	subroutine.domain.links.create(
		world.session,
		workspace_id=world.workspace.id,
		source=end(blocker),
		target=end(blocked),
		link_type_key="blocks",
	)
	world.session.flush()


def _somebody_else (world: World) -> subroutine.db.models.identity.User:
	"""Add a second account to this workspace, so a blocker can belong to somebody."""

	other = subroutine.domain.users.create(
		world.session, username=f"other-{uuid.uuid4().hex[:8]}", timezone=LONDON
	)

	subroutine.domain.workspaces.add_member(
		world.session, workspace=world.workspace, user=other, role_key="member"
	)
	world.session.flush()

	return other


def test_work_held_up_by_somebody_elses_item_gets_its_own_section (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1285`, decision `SR#1267` §3.** The other kind of waiting.

	`waiting` is a question somebody parked for you. This is your work held up by their row,
	and `#96`'s reason for not tracking it — *a `blocks` link resolves itself* — **is a claim
	about a single worker**. When the blocker is somebody else's it resolves when they act,
	and nothing told you it had been sitting there.
	"""

	world = World(session)
	other = _somebody_else(world)

	theirs = world.task("Their bit")
	theirs.assignee_id = other.id
	mine = world.task("My bit")

	_blocks(world, theirs, mine)

	agenda = world.agenda()

	assert _titles(agenda.blocked_by_others) == ["My bit"]

	# **The blocker is on nobody's agenda but its own assignee's** (`SR#1265`, decision
	# `SR#1267` §1). Until the assignee rule landed this asserted `["Their bit"]` — the
	# blocker sat in the reader's *Next* pile, ranked among the work they could pick up,
	# which is the opposite of what this section says about it two lines above. One row,
	# described two ways on one page.
	assert _titles(agenda.unscheduled) == [], (
		"somebody else's work is not offered to this reader as something to start"
	)
	assert agenda.assigned_elsewhere_total == 1, (
		"and it is not silently gone either — a listing at this scope still holds it"
	)


def _agenda_of (
	world: World, user: subroutine.db.models.identity.User
) -> subroutine.domain.agenda.Agenda:
	"""Build the agenda somebody else would see, in the same workspace.

	``World.agenda`` passes its own principal, so a test about two readers cannot go through
	it — and two readers is the whole of what `SR#1774` needs.
	"""

	return subroutine.domain.agenda.build(
		world.session,
		principal=subroutine.domain.authentication.Principal(user=user),
		workspace_ids=[world.workspace.id],
		now=NOW,
		timezone=LONDON,
	)


def test_a_question_nobody_owns_is_on_nobodys_waiting_list (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1774`, found by Simon in the first hour of the first shared instance.

	Two of his own decisions, parked and assigned to nobody, were addressed to a colleague
	under a heading reading *Waiting on you*. What put them there is
	`readiness.yours_to_act_on`'s middle clause — *assigned to nobody* — which is right for
	every other bucket and wrong for this one: `BUCKETS` says in its own comment that every
	other bucket is work the reader could pick up and this one is work they are holding up.

	`#96` is why the row cannot answer for itself. With no fifth status category,
	``needs_input`` records that an answer is owed and never by whom, so reading that silence
	as *everybody* is the product inventing a fact.

	**Two readers, because one cannot tell the halves apart.** Until a second person existed
	on any instance, *assigned to nobody* and *mine* selected the same rows — which is why
	every fixture in this file used an unassigned task and nothing here ever had to choose.

	**And it asserts where the row went, not only where it did not go.** Declining it is only
	right because the buckets subtract in order and it falls through to `unscheduled`; an
	assertion that stopped at the empty bucket would pass just as well against a rule that
	dropped the row off the page.
	"""

	world = World(session)
	other = _somebody_else(world)

	unowned = world.task("Which way round?")
	_waiting(world, unowned, on=None)

	theirs = world.task("Their question")
	_waiting(world, theirs, on=other.id)

	mine = world.agenda()

	assert _titles(mine.waiting) == [], (
		"a question nobody has been given is not one this reader is holding up"
	)
	assert "Which way round?" in _titles(mine.unscheduled), (
		"and it is relabelled rather than hidden — it falls through to what could be started"
	)

	seen = _agenda_of(world, other)

	assert _titles(seen.waiting) == ["Their question"], (
		"the other reader is holding up their own question and not the unowned one"
	)
	assert "Which way round?" in _titles(seen.unscheduled), (
		"which reaches them the same way it reaches everybody: as work nobody has taken"
	)


def test_a_question_you_are_holding_is_yours_to_answer (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The clause that keeps `SR#1774` from being ``assigned_to_me``, falsified on its own.

	Somebody holding a live lease on a parked question is the person acting on it, so
	`readiness.yours_to_answer` keeps :func:`yours_to_act_on`'s third clause and drops only
	its middle one. Without this the narrowing would recreate exactly the failure that third
	clause exists to prevent — work vanishing from the agenda of the one person who has
	started it (`#1267` §1, and :func:`unclaimed` one axis along).

	**Separate from the test above because one of them could not tell these apart.** A single
	test asserting an unowned question is absent would pass against a rule that read the
	assignee alone, which is the shape a later reader would reach for.
	"""

	world = World(session)

	unowned = world.task("Which way round?")
	_waiting(world, unowned, on=None)

	assert _titles(world.agenda().waiting) == [], "nobody has been given it yet"

	subroutine.domain.claims.claim(world.session, unowned, now=NOW, actor=world.principal)
	world.session.flush()

	assert _titles(world.agenda().waiting) == ["Which way round?"], (
		"taking a lease on it makes it the reader's to answer, with no assignee involved"
	)


def _held_up (
	built: subroutine.domain.agenda.Agenda, title: str
) -> list[str] | None:
	"""Return the titles of what is holding one row up, or ``None`` where nobody asked."""

	for row in built.blocked_by_others:
		if row.title == title:
			held = built.blockers.get(row.id)

			return None if held is None else [one.title for one in held]

	raise AssertionError(f"{title!r} is not in the section")


def test_the_section_says_what_is_holding_each_row_up (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1287`, Simon's decision of 2026-08-27.** *Blocked* with no name is a guess.

	The heading says somebody else has to move first. Decision `SR#1267` §3c is what asked
	for this and says why a mark could not carry it: *"a mark cannot carry the thing that
	makes this useful, which is who you are waiting on."*
	"""

	world = World(session)
	other = _somebody_else(world)

	theirs = world.task("Their bit")
	theirs.assignee_id = other.id
	mine = world.task("My bit")

	_blocks(world, theirs, mine)

	built = world.agenda()

	assert _held_up(built, "My bit") == ["Their bit"]

	held = built.blockers[mine.id]

	assert [one.ref for one in held] == [theirs.ref], (
		"the ref is what makes the far end something a reader can act on"
	)
	assert [one.assignee_id for one in held] == [other.id], (
		"and the assignee is who they chase — Simon's rule, and never the claimant"
	)


def test_it_names_every_live_blocker_and_not_only_the_one_somebody_holds (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1287`.** Chasing everybody named must not promise more than it can deliver.

	A row is in this section because *some* blocker is assigned to somebody else. It may be
	held by an unassigned one as well — and naming only the first would say that chasing that
	person releases the work, when finishing their item leaves the row exactly where it was.

	**The opposite reading is the tempting one**, because the bucket's own predicate is the
	narrow one (`SR#1285`, decision `SR#1267` §3a) and reusing it here is one fewer rule. What
	it would produce is a page that is right about why the row is listed and wrong about what
	to do next.
	"""

	world = World(session)
	other = _somebody_else(world)

	theirs = world.task("Their bit")
	theirs.assignee_id = other.id
	nobodys = world.task("Nobody's job")
	mine = world.task("My bit")

	_blocks(world, theirs, mine)
	_blocks(world, nobodys, mine)

	assert _held_up(world.agenda(), "My bit") == sorted(
		["Their bit", "Nobody's job"],
		key=lambda title: {"Their bit": theirs.ref, "Nobody's job": nobodys.ref}[title],
	)


def test_a_finished_blocker_is_not_named (session: sqlalchemy.orm.Session) -> None:
	"""**`SR#1287`.** What is named is what is holding the row up, not what once did.

	The liveness rule is ``readiness._live_blocks_edge``'s, read the same direction
	``unblocked`` reads it — so a row this names nothing for is a row nothing marks blocked,
	which is the property that stops the section and the mark disagreeing.
	"""

	world = World(session)
	other = _somebody_else(world)

	theirs = world.task("Their bit")
	theirs.assignee_id = other.id
	done = world.task("Already done")
	done.assignee_id = other.id
	mine = world.task("My bit")

	_blocks(world, theirs, mine)
	_blocks(world, done, mine)

	subroutine.domain.tasks.complete(world.session, task=done, actor=world.principal)
	world.session.flush()

	assert _held_up(world.agenda(), "My bit") == ["Their bit"]


def test_a_blocker_the_reader_may_not_see_is_not_named (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1287`, and it is the whole of why naming a far end needed a decision.**

	``readiness.blocked_among`` is deliberately *not* narrowed by visibility, because whether
	work is blocked is a fact about the work — what it discloses is bounded at *something
	unseen holds this up*. Naming the far end says *what*, so it takes the rule that governs
	naming a far end anywhere else: ``domain.links`` drops an end the caller cannot see, and
	this drops the same ones.

	**So the row stays in the section with nothing named against it**, which is the honest
	answer and not a bug: it says *somebody you cannot see*. Both assertions are needed —
	dropping the row instead would hide work the reader is genuinely waiting on, and `SR#856`
	is what happens when the end is named anyway.
	"""

	world = World(session)
	other = _somebody_else(world)

	hidden = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key=f"H{uuid.uuid4().hex[:10].upper()}",
		title="Somewhere else",
		visibility="private",
		owner_id=other.id,
	)
	session.flush()

	theirs = world.task("Their bit", project=hidden)
	theirs.assignee_id = other.id
	mine = world.task("My bit")

	_blocks(world, theirs, mine)

	built = world.agenda()

	assert _titles(built.blocked_by_others) == ["My bit"], (
		"the reader is still waiting, and a section that dropped the row would say they are not"
	)
	assert _held_up(built, "My bit") == [], (
		"and nothing about the item holding it up is disclosed"
	)


def test_a_blocked_row_is_told_who_is_holding_it_up_wherever_it_lands (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1847`, Simon 2026-09-04. The exception is about a row, not about a section.**

	*A listing says that and a detail view says what* is written on ``views.Task.blocking`` and
	is what `SR#856` cost. Decision `SR#1267` §3c carves an exception out of it, and `SR#1287`
	applied that to the one bucket whose subject is the far end.

	**`SR#1846` then moved `overdue` above that bucket**, and the buckets are disjoint in list
	order — so a task both blocked and late landed under *Overdue* carrying the **Blocked** mark
	and *not* the line naming who. The mark says the half a reader cannot act on.

	§3c argues from *"a mark cannot carry the thing that makes this useful, which is who you are
	waiting on"*, which is a statement about a **blocked row**. The section was only ever the
	carrier because it was the bucket that needed it first.

	**A row nothing holds up is still absent from the mapping** — ``None`` rather than an empty
	list, because *nobody asked* and *nothing holds this up* are two answers, and that half of
	`SR#1287` is untouched.

	Falsify by narrowing the set back to ``rows["blocked_by_others"]``, which is the shipped
	behaviour: the late row drops out of the mapping and keeps a mark it cannot explain.
	"""

	world = World(session)
	other = _somebody_else(world)

	theirs = world.task("Their bit")
	theirs.assignee_id = other.id
	mine = world.task("My bit")

	_blocks(world, theirs, mine)

	# **Late as well as blocked, which is the row `SR#1846` moved.** `overdue` sits above
	# `blocked_by_others` and the buckets subtract what their predecessors took, so this lands
	# under *Overdue* and never reaches the section the exception used to belong to.
	late = world.task("Late and held up")
	late.due_at = NOW - datetime.timedelta(days=2)
	blocking_the_late_one = world.task("What is holding the late one up")
	blocking_the_late_one.assignee_id = other.id

	_blocks(world, blocking_the_late_one, late)

	loose = world.task("Something to pick up")

	built = world.agenda()

	assert late.id in {row.id for row in built.overdue}, (
		"the fixture did not put the blocked row under Overdue, so this asserts nothing about "
		"the case it was written for"
	)

	assert late.id in built.blockers, (
		"a blocked row outside the Waiting on somebody else section was marked blocked and not "
		"told who by, which is the mark carrying the half a reader cannot act on"
	)
	assert {row.id for row in built.blockers[late.id]} == {blocking_the_late_one.id}

	assert mine.id in built.blockers, "the original section still resolves its own rows"

	assert loose.id not in built.blockers, (
		"a row nothing holds up appeared in the mapping, so 'nobody asked' and 'nothing holds "
		"this up' have stopped being two answers"
	)


def test_a_blocker_of_your_own_is_not_somebody_else (session: sqlalchemy.orm.Session) -> None:
	"""**`SR#1285`, decision `SR#1267` §3a — the narrow reading, and it is Simon's.**

	*Blocked by anything* floods a solo instance, which is most instances, and a solo
	instance's blockers are its own work. `#96`'s argument still holds there, so it has to go
	on holding here: work you are blocking yourself on is work, not a thing to chase somebody
	about.
	"""

	world = World(session)

	first = world.task("Do this first")
	first.assignee_id = world.user.id
	second = world.task("Then this")

	_blocks(world, first, second)

	assert world.agenda().blocked_by_others == ()


def test_a_blocker_nobody_is_assigned_to_is_not_somebody_else (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1285`.** Nobody is holding it, so there is nobody to chase.

	The honest thing to say about an unclaimed blocker is that it is unclaimed work, which is
	what ``--ready`` already says one axis along. Naming it *waiting on somebody else* would
	invite a reader to chase a person who does not exist.
	"""

	world = World(session)

	_blocks(world, world.task("Nobody's job"), world.task("Mine"))

	assert world.agenda().blocked_by_others == ()


def test_a_finished_blocker_of_somebody_elses_releases_the_work (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1285`.** The section empties itself when they act, with nobody touching this row.

	The edges are :func:`readiness.unblocked`'s, so this inherits every rule about what makes
	a ``blocks`` link live — and it is worth one test of its own here, because a section that
	only ever fills up is one people stop reading.
	"""

	world = World(session)
	other = _somebody_else(world)

	theirs = world.task("Their bit")
	theirs.assignee_id = other.id
	mine = world.task("My bit")

	_blocks(world, theirs, mine)

	assert _titles(world.agenda().blocked_by_others) == ["My bit"]

	subroutine.domain.tasks.complete(session, theirs, now=NOW, actor=world.principal)
	session.flush()

	assert world.agenda().blocked_by_others == ()
	assert "My bit" in _titles(world.agenda().unscheduled)


def test_work_held_up_by_somebody_else_is_reported_as_late_once_its_deadline_passes (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1285`, reversed by `SR#1846`, and this is the half with a consequence.**

	`SR#1267` §3 put this bucket above ``overdue`` on `SR#1116`'s reasoning: *you are late* is
	not the useful sentence about work nobody has let you start, because chasing the other
	person is the only move available and this is the section that says so. `SR#1846` weighs
	that against the composition — three such pairwise decisions between them put *Overdue*
	below the fold — and reverses it.

	**The row kept its `blocked` mark and lost the far end, and that was `SR#1847`.** This
	asserted the loss so it could not recur in silence; Simon decided on 2026-09-04 that the
	exception belongs to the **row** rather than to the section, so the assertion is inverted
	here rather than deleted. `SR#1267` §3c argues from *"a mark cannot carry the thing that
	makes this useful, which is who you are waiting on"* — about a blocked row, not a heading —
	and the section only ever held the carve-out because it was the bucket that needed it first.

	**The reordering's own consequence is unchanged**, which is what this test is still for: the
	row leaves ``blocked_by_others`` and reports under *Overdue*. What it no longer loses is the
	line saying who by.
	"""

	world = World(session)
	other = _somebody_else(world)

	theirs = world.task("Their bit")
	theirs.assignee_id = other.id
	mine = world.task("My bit", due=datetime.date(2026, 7, 20))

	_blocks(world, theirs, mine)

	agenda = world.agenda()

	assert _titles(agenda.overdue) == ["My bit"]
	assert agenda.blocked_by_others == (), "the buckets are not disjoint"

	assert mine.id in agenda.blockers, (
		"SR#1847: a row that left the blocked section lost the line naming who is holding it "
		"up, so the mark is carrying the half a reader cannot act on"
	)
	assert {row.id for row in agenda.blockers[mine.id]} == {theirs.id}

	# **And work held up with no deadline still gets the section it was written for**, which is
	# what stops this reordering emptying the bucket it moved past.
	waited = world.task("Not late, still stuck")
	_blocks(world, theirs, waited)

	assert _titles(world.agenda().blocked_by_others) == ["Not late, still stuck"]


def test_the_blocked_section_is_capped_and_says_how_much_it_is_holding_back (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1285`, decision `SR#1267` §3b.** A bar, not a dump — and a cap must say it is one.

	Simon's qualifier was *"if those items would ordinarily be urgent/important enough to be
	included"*. A threshold read off ``priority_score`` cannot be honest, because the score is
	null unless both axes are set and most of a backlog would fall under any bar in silence;
	ordering by rank and capping says the same thing and reports what it left out.

	**More rows than the cap**, so a limit copied from somewhere else fails here rather than
	passing on a fixture too small to notice it.
	"""

	world = World(session)
	other = _somebody_else(world)

	for number in range(subroutine.domain.agenda.DEFAULT_BLOCKED_LIMIT + 3):
		theirs = world.task(f"Their bit {number}")
		theirs.assignee_id = other.id
		_blocks(world, theirs, world.task(f"My bit {number}", importance=5, urgency=5))

	agenda = world.agenda()

	assert len(agenda.blocked_by_others) == subroutine.domain.agenda.DEFAULT_BLOCKED_LIMIT
	assert agenda.blocked_by_others_total == (
		subroutine.domain.agenda.DEFAULT_BLOCKED_LIMIT + 3
	), agenda.blocked_by_others_total


def test_a_row_a_cap_hides_does_not_reappear_under_a_later_heading (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1285`, and this was a real defect found by the arithmetic above rather than read.**

	A cap decides how many rows are *drawn*. It must not decide which bucket owns them — the
	first version pushed the limit into the query, so what `blocked_by_others` hid never
	reached `seen` and fell through into `unscheduled`, where it was offered under *Next* as
	something to pick up. It is the one thing it is not: nobody has let the reader start it.

	**Only the last bucket may cap in the query**, because nothing follows it. This is the
	guard on that, driven rather than asserted about the code.
	"""

	world = World(session)
	other = _somebody_else(world)

	for number in range(subroutine.domain.agenda.DEFAULT_BLOCKED_LIMIT + 3):
		theirs = world.task(f"Their bit {number}")
		theirs.assignee_id = other.id
		_blocks(world, theirs, world.task(f"Held up {number}"))

	agenda = world.agenda()
	elsewhere = [
		title for title in _titles(agenda.unscheduled) if title.startswith("Held up")
	]

	assert elsewhere == [], (
		f"{len(elsewhere)} rows the blocked section capped were offered under another "
		f"heading: {elsewhere}"
	)
	assert agenda.blocked_by_others_total == (
		subroutine.domain.agenda.DEFAULT_BLOCKED_LIMIT + 3
	), "the count has to cover what the cap hid, or the footer under-reports it"


def test_the_blocked_section_shows_the_most_important_of_what_it_holds_back (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1285`.** The cap takes the top of the rank, which is what makes the bar a bar.

	A cap that took whichever rows the database handed back first would hide the item worth
	chasing about behind five that are not, and the count beneath it would read as reassurance.
	"""

	world = World(session)
	other = _somebody_else(world)

	for number in range(subroutine.domain.agenda.DEFAULT_BLOCKED_LIMIT + 1):
		theirs = world.task(f"Their bit {number}")
		theirs.assignee_id = other.id
		_blocks(
			world,
			theirs,
			world.task(f"My bit {number}", importance=1, urgency=1),
		)

	theirs = world.task("The one that matters to them")
	theirs.assignee_id = other.id
	_blocks(world, theirs, world.task("The one that matters", importance=5, urgency=5))

	shown = _titles(world.agenda().blocked_by_others)

	assert "The one that matters" in shown, shown


def test_the_order_the_sections_are_shown_in_is_the_order_they_are_computed_in (
	session: sqlalchemy.orm.Session,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""**`SR#1244`, and it is driven rather than compared.**

	The buckets are disjoint *in the order they are computed*, and the headings are drawn in
	the order they are *shown*. Those were two declarations that happened to agree until
	2026-08-25, and every guard read only the displayed one — so moving `in_progress` to the
	front of the page left a started, overdue task under *Overdue*, below a heading that said
	*In progress* came first. The suite stayed green.

	There is one declaration now, so this asserts the consequence rather than the equality:
	move `in_progress` back above `overdue` in :data:`subroutine.domain.agenda.BUCKETS` and the
	row must move with it. A `build` that has gone back to a written-out sequence answers
	`overdue` both times.

	**The example inverted with `SR#1846`**, which is the point rather than an edit: the shipped
	order and the mutation swapped places, and this went red for exactly that reason. A guard
	whose mutation is somebody's next decision is the shape worth keeping.
	"""

	world = World(session)
	started = subroutine.domain.tasks.status_for(session, world.workspace.id, "in_progress")

	task = world.task("Started and late", due=datetime.date(2026, 7, 28))
	task.status_id = started.id
	session.flush()

	assert _titles(world.agenda().overdue) == ["Started and late"], (
		"a started, overdue task belongs to the first of the two buckets that claims it"
	)
	assert world.agenda().in_progress == ()

	monkeypatch.setattr(
		subroutine.domain.agenda,
		"BUCKETS",
		_moved(subroutine.domain.agenda.BUCKETS, "in_progress", above="overdue"),
	)

	assert _titles(world.agenda().in_progress) == ["Started and late"], (
		"the computation order did not follow BUCKETS, so it is declared somewhere else too"
	)
	assert world.agenda().overdue == ()


def test_the_sections_a_surface_draws_are_the_ones_the_agenda_computed (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1244`.** One tuple, reached by two names — never a copy.

	This is identity rather than equality on purpose. Two equal literals are exactly the state
	this item was filed about: correct for as long as nobody moved either, with nothing able to
	see the disagreement afterwards.
	"""

	assert subroutine.views.AGENDA_BUCKETS is subroutine.domain.agenda.BUCKETS


def test_every_bucket_declared_is_one_the_agenda_reports (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1244`.** A bucket that is computed and never returned disappears in silence.

	Every other way of getting this wrong is loud — a name missing from the predicates or from
	:data:`subroutine.domain.agenda.ORDERS` raises on the next call. Forgetting the field on
	:class:`subroutine.domain.agenda.Agenda` is the quiet one: the bucket is still computed, it
	still takes its rows away from the buckets below it, and then nothing carries it to a page.
	"""

	fields = {field.name for field in dataclasses.fields(subroutine.domain.agenda.Agenda)}

	for bucket in subroutine.domain.agenda.BUCKETS:
		assert bucket in fields, f"{bucket} is computed and there is nowhere to report it"
		assert bucket in subroutine.domain.agenda.ORDERS, f"{bucket} has no declared order"


def _moved (buckets: tuple[str, ...], bucket: str, *, above: str) -> tuple[str, ...]:
	"""Return the buckets with one of them lifted to just before another."""

	assert bucket in buckets and above in buckets

	rest = [name for name in buckets if name != bucket]

	return tuple(
		name for other in rest for name in ((bucket, other) if other == above else (other,))
	)


def _hold (world: World) -> None:
	"""Put the world's project on hold, through the domain that a client would reach."""

	subroutine.domain.projects.update(
		world.session, world.project, status_key="on_hold", actor=world.principal
	)


def test_a_project_on_hold_loses_the_bucket_that_says_what_to_do_next (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#983`. Putting a project down is an answer to *what should I work on*, so this is the
	bucket it changes — the one `#853` made the agenda's real content, and the one a person
	reads when nothing is dated."""

	world = World(session)
	world.task("Redesign the header")

	assert _titles(world.agenda().unscheduled) == ["Redesign the header"]

	_hold(world)

	assert _titles(world.agenda().unscheduled) == []


def test_a_project_on_hold_keeps_its_dated_work_on_the_agenda (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**The half that is a decision rather than a consequence, so it is pinned here.**

	OmniFocus and Things both drop dated items from a paused project. This does not, on
	`#857`'s reasoning that dates are answered by *when* and are kept out of the machinery
	that answers *what matters* — and on the plainer ground that a deadline is usually a
	commitment to somebody else, which pausing your own work does not cancel. The cost of
	the other choice is a deadline passing in silence months after somebody put a project
	down; the cost of this one is a row you have to ignore.

	If this is ever reversed, it is one clause moving from `agenda.build` into
	`scoping.readable_tasks`, and this test is what should be made to fail first.
	"""

	world = World(session)
	world.task("Renew the certificate", due=TODAY - datetime.timedelta(days=1))
	world.task("File the return", due=TODAY)

	_hold(world)

	agenda = world.agenda()

	assert _titles(agenda.overdue) == ["Renew the certificate"]
	assert _titles(agenda.today) == ["File the return"]


def test_dated_work_past_the_look_ahead_is_counted_rather_than_lost (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#997`, Simon's decision of 2026-08-18: the edge stays and gets said.

	**A deadline further out than the look-ahead is in no bucket at all.** ``unscheduled``
	requires *both* dates to be null, so dated work leaves that pile and there is nowhere else
	to go — it disappears from the view whose whole job is *what is coming*, and reappears
	seven days before it is due.

	The agenda stays a day view (§8.6) and a listing already answers *what is due this
	quarter*, so the defect was never the edge: it was that nothing told a reader one existed.
	``unscheduled_total`` is the worked precedent for *there is more, here is how much*.
	"""

	world = World(session)
	world.task("File the return", due=TODAY + datetime.timedelta(days=30))
	world.task("Buy milk")

	agenda = world.agenda(horizon_days=7)

	assert _titles(agenda.upcoming) == [], "thirty days out is past a seven-day look-ahead"
	assert _titles(agenda.unscheduled) == ["Buy milk"], "dated work is not in the undated pile"
	assert agenda.later_total == 1, "and the count is the only thing that says it exists"


def test_the_count_covers_everything_dated_when_no_look_ahead_was_asked_for (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**Written against "dated and not shown" rather than "past the horizon"** — `#997`.

	``GET /v1/agenda`` omits ``upcoming`` unless it is asked for, deliberately, so a caller
	that does not ask is shown *nothing* dated beyond today. A predicate written against the
	horizon would report zero on that call, which is the answer that looks like good news:
	the one caller who sees least would be told there is nothing more.
	"""

	world = World(session)
	world.task("Due on Friday", due=TODAY + datetime.timedelta(days=2))
	world.task("File the return", due=TODAY + datetime.timedelta(days=30))

	unasked = world.agenda()

	assert _titles(unasked.upcoming) == [], "the bucket is omitted unless asked for"
	assert unasked.later_total == 2, "so both are unshown, and both are counted"

	asked = world.agenda(horizon_days=7)

	assert _titles(asked.upcoming) == ["Due on Friday"]
	assert asked.later_total == 1, "asking for the week moves one of them into view"


def test_work_that_is_shown_is_never_also_counted_as_missing (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The claim that makes the number worth printing beside the buckets.

	A count that included rows the reader can already see would read as *there is more* when
	there is not — the failure mode `#818` records for a different question, where a plausible,
	complete, wrong answer is worse than a refusal.
	"""

	world = World(session)
	world.task("Renew the certificate", due=TODAY - datetime.timedelta(days=1))
	world.task("File the return", due=TODAY)
	world.task("Due on Friday", due=TODAY + datetime.timedelta(days=2))

	agenda = world.agenda(horizon_days=7)

	assert _titles(agenda.overdue) == ["Renew the certificate"]
	assert _titles(agenda.today) == ["File the return"]
	assert _titles(agenda.upcoming) == ["Due on Friday"]
	assert agenda.later_total == 0, "every dated task is on the page, so nothing is missing"


def test_a_deadline_set_to_today_is_on_the_setter_s_today (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#1014`. The commonest thing anybody does with a to-do list, and it did not work.

	**What it looked like**: six items set ``due: "today"`` answered ``today: 0``, and every
	one of them sat under *Next 7 days* while its own row rendered *due Wed 19 Aug*. One
	screen, saying two things about the same date, with nothing to suggest anything was wrong.

	**The cause was one word.** ``update`` resolved dates through
	``_timezone(..., explicit=timezone or task.timezone)`` — and ``explicit`` is the *top* of
	§6.5's chain, so the zone a task was created in outranked the zone of everybody who
	touched it afterwards. Creation always records one, so the user step below it was
	unreachable for every task that has ever existed.

	That contradicted two things the code already said about itself: ``zone_for``'s docstring
	calls itself *the one place that owns* the chain, and it has no such step; and the column's
	own comment calls it *the zone the dates were authored in*, which is a record of a past
	write rather than an input to the next one.

	**Why the zone has to be written back too**, which is the half that is easy to miss:
	resolving in the caller's zone and leaving the old one behind moves the contradiction
	instead of removing it. The instant would land inside the reader's day while the rendering,
	which reads the stored zone (`#773`), went on naming a different one — the same defect
	pointing the other way, and this fixture is built to catch that: assert the day as well as
	the bucket, or the second half is unfalsifiable.

	**The fixture cannot be in one zone.** `Etc/UTC` against `Europe/London` in July is an
	hour apart, so a whole-day deadline lands either side of midnight depending on which zone
	resolved it. A test written in one zone passes against both implementations.
	"""

	world = World(session)

	# Authored in UTC, which is what every task filed before its owner set a timezone looks
	# like. The divergence is the whole fixture: nothing here can fail in a single zone.
	task = world.task("Return the library books", timezone="Etc/UTC")

	assert task.timezone == "Etc/UTC"

	subroutine.domain.tasks.update(
		session, task, due="today", now=NOW, actor=world.principal
	)

	# End of 30 July in London, which is 22:59:59.999999Z in BST. The other implementation
	# stores 23:59:59.999999Z — an hour later, and one hour outside the reader's day.
	assert task.due_at == datetime.datetime(
		2026, 7, 30, 22, 59, 59, 999999, tzinfo=datetime.UTC
	)
	assert task.due_is_all_day

	# The zone the date was authored in, so that what a reader is shown agrees with the
	# bucket it was put in rather than being computed from a zone nobody used.
	assert task.timezone == LONDON

	agenda = world.agenda()

	assert _titles(agenda.today) == ["Return the library books"]
	assert _titles(agenda.upcoming) == []


def test_a_date_left_alone_does_not_take_the_zone_with_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#1014`'s other side: editing a title from another zone authors no date.

	Rewriting the column on the way past would silently re-render every date on the task —
	the same defect as the one above, caused by the fix for it. So the write is conditional
	on a date having actually moved, and this is what holds that condition in place.
	"""

	world = World(session)
	task = world.task(
		"Renew the passport", due=datetime.date(2026, 7, 30), timezone="Etc/UTC"
	)

	before = task.due_at

	subroutine.domain.tasks.update(
		session, task, title="Renew the passport today", now=NOW, actor=world.principal
	)

	assert task.timezone == "Etc/UTC"
	assert task.due_at == before


#: Stands for *the person whose agenda this is*, so ``on=None`` can mean **nobody** rather
#: than *not stated* — the distinction `#1774` turns on.
_THE_READER = uuid.UUID(int=0)


def _waiting (
	world: World,
	task: subroutine.db.models.work.Task,
	*,
	on: uuid.UUID | None = _THE_READER,
) -> None:
	"""Park a question on a task, the way an agent would, and say who owes the answer.

	**``on`` defaults to the reader because that is what *Waiting on you* means** (`#1774`).
	These fixtures were written when this instance had one person, so an unassigned row and
	the reader's own row were the same thing and nothing here had to choose. They are not the
	same thing on a shared instance, and the heading names a person.

	Pass ``on=None`` for a question nobody has been given, which is the case
	``test_a_question_nobody_owns_is_on_nobodys_waiting_list`` is about.
	"""

	subroutine.domain.tasks.update(
		world.session,
		task,
		status_key=subroutine.domain.agenda.WAITING_STATUS,
		assignee_id=world.user.id if on is _THE_READER else on,
		now=NOW,
		actor=world.principal,
	)


def test_something_waiting_on_a_person_is_the_first_thing_they_see (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#1116`. A status seeded since M1, reachable at every layer, and used zero times.

	It was published in `/v1/meta`, settable through every client, filterable and rendered by
	the board — and nothing had ever set it, because nothing put it in front of the person who
	could answer. The mechanism was never the missing part.
	"""

	world = World(session)
	world.task("Ordinary work")
	asked = world.task("Which way round should the flag read?")
	_waiting(world, asked)

	built = world.agenda()

	assert _titles(built.waiting) == ["Which way round should the flag read?"]
	assert "Which way round should the flag read?" not in _titles(built.unscheduled)


def test_a_deadline_that_has_passed_outranks_the_question_holding_it_up (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The one decision inside this, and it was taken the other way round until `SR#1846`.

	`SR#1116` put `waiting` above `overdue`: *you owe an answer* is more actionable than *this
	is late*, because the lateness is a consequence of the question and nobody can move the
	task until it is answered. That is a good argument about the pair and it is not what went
	wrong. What went wrong is that three such arguments were taken separately and `overdue`
	ended up fifth — a screen and a half down, where Simon reported the section as missing.

	So the answer is reversed and the reason is prior to the argument above: being seen at all
	comes before which of two true sentences is the more useful. **A task that is both is
	reported as late.**
	"""

	world = World(session)
	late = world.task("Late and stuck", due="2026-07-01")
	_waiting(world, late)

	built = world.agenda()

	assert _titles(built.overdue) == ["Late and stuck"]
	assert _titles(built.waiting) == [], "the buckets are not disjoint"

	# **And a question with no deadline is still a question**, which is what stops this
	# reordering emptying the section it moved past.
	asked = world.task("Stuck and not late")
	_waiting(world, asked)

	assert _titles(world.agenda().waiting) == ["Stuck and not late"]


def test_a_question_that_has_been_answered_leaves_the_bucket (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Which is the whole loop, and the half that decides whether it closes.

	Answering is moving the status back — there is no second act to remember, which is what
	`#1113` measured as the thing that reliably does not happen.
	"""

	world = World(session)
	asked = world.task("Which way round?")
	_waiting(world, asked)

	assert _titles(world.agenda().waiting) == ["Which way round?"]

	subroutine.domain.tasks.update(
		session, asked, status_key="open", now=NOW, actor=world.principal
	)

	assert world.agenda().waiting == ()
	assert _titles(world.agenda().unscheduled) == ["Which way round?"]


def test_a_finished_question_is_not_still_waiting (session: sqlalchemy.orm.Session) -> None:
	"""The exclusions every bucket shares apply here too, and this is the one that could slip.

	`waiting` reads a *key* where every other bucket reads dates or a category, so it does not
	inherit the finished-work exclusion from the shape of its own clause — it inherits it from
	`_visible`, and that is worth an assertion rather than a reading.
	"""

	world = World(session)
	asked = world.task("Answered by giving up on it")
	_waiting(world, asked)
	subroutine.domain.tasks.update(
		session,
		asked,
		status_key=subroutine.domain.tasks.finished_status_key(session, world.workspace.id),
		now=NOW,
		actor=world.principal,
	)

	assert world.agenda().waiting == ()


def test_an_empty_agenda_is_still_empty_with_the_new_bucket (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`is_empty` has to know about every bucket, and it is the one place that lists them all.

	A bucket missing from it makes an agenda holding only that bucket report itself as having
	nothing in it — and the CLI prints its *nothing to do* line off exactly this.
	"""

	world = World(session)

	assert world.agenda().is_empty

	asked = world.task("Which way round?")
	_waiting(world, asked)

	assert not world.agenda().is_empty


def test_a_whole_day_event_re_dated_from_another_zone_stays_in_its_bucket (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1327`. `SR#1296`'s worst symptom, through the door `SR#1296` did not close.

	`SR#1014` relabels ``task.timezone`` to the zone a date was authored in, and leaves the
	instants already on the row where they are. That cost was only a rendering an hour out
	until `SR#1296` made a whole-day row's bucket depend on comparing it against the edge of
	the day *its own zone column* names — after which a row stored at London midnight and
	labelled New York matches no day's edge at all.

	**Measured before the fix**: an event on today, correct for all four readers, was in **no**
	bucket for any of them after a colleague five hours away made an unrelated dated edit, and
	was counted under *dated further out* — the agenda saying an event happening today is
	something to look at next week.

	The edit driven here is deliberately not to the start itself. Clearing a defer the row
	never had changes nothing a person would notice, and is enough to relabel the zone.
	"""

	world = World(session)
	birthday = world.task(
		"Anna's birthday", type_key="event", starts=TODAY, starts_is_all_day=True
	)

	assert "none" not in _read_by_everybody(world, "Anna's birthday", day=TODAY).values(), (
		"this row was already broken before the edit, so the edit is not what is measured"
	)

	subroutine.domain.tasks.update(
		session,
		birthday,
		snooze=None,
		timezone="America/New_York",
		now=NOW,
	)
	session.flush()

	assert birthday.timezone == "America/New_York", (
		"the zone was not relabelled, so this drives none of what it is about"
	)

	landed = _read_by_everybody(world, "Anna's birthday", day=TODAY)

	assert "none" not in landed.values(), (
		f"a whole-day event relabelled to another zone is in no section for some reader: "
		f"{landed}"
	)
	assert len(set(landed.values())) == 1, (
		f"the readers disagree about which section it belongs in: {landed}"
	)


def test_relabelling_a_row_moves_its_whole_day_dates_and_leaves_its_timed_ones (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1327`, and the half that says what the repair may **not** do.

	A whole day is a label and a time is a point (decision `SR#1088`). So relabelling the zone
	moves a whole-day date onto the same *day* in the new zone — the row goes on meaning the
	day it always meant — and must leave a timed date exactly where it is, because a timed
	date is a moment and `SR#1014`'s promise is that it keeps it.

	Without the second half, re-dating one field from a plane would silently walk every other
	appointment on the row across the clock.
	"""

	row = World(session).task(
		"Two dates",
		starts=datetime.datetime(2026, 8, 20, 9, 0, tzinfo=datetime.UTC),
		due=datetime.date(2026, 8, 25),
	)
	stood_at = row.starts_at

	assert row.due_is_all_day is True and row.starts_is_all_day is False, (
		"this fixture no longer holds one date of each kind"
	)

	subroutine.domain.tasks.update(
		session, row, snooze=None, timezone="Pacific/Auckland", now=NOW
	)
	session.flush()

	assert row.starts_at == stood_at, "a timed date was walked across the clock"

	assert row.due_at == subroutine.domain.tasks.whole_day_for(
		datetime.date(2026, 8, 25),
		field="due_at",
		timezone="Pacific/Auckland",
		now=NOW,
	).instant, (
		f"the whole-day deadline is not the end of the 25th in the zone the row now names: "
		f"{row.due_at}"
	)
