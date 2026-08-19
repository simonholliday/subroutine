"""Tests for the four agenda buckets, on both backends.

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

import datetime
import typing
import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.agenda
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.projects
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces

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
