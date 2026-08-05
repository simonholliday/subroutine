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
	feature — the single easiest way to build a to-do list nobody can use (SPEC.md §8.6).
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
	"""SPEC.md §8.6's table, in one pass."""

	world = World(session)
	world.task("Late", due=datetime.date(2026, 7, 28))
	world.task("Due today", due=TODAY)
	world.task("Planned today", planned_for=TODAY)
	world.task("Planned yesterday", planned_for=datetime.date(2026, 7, 29))
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
	world.task("Late and planned", due=datetime.date(2026, 7, 28), planned_for=TODAY)

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
		("deferred", {"start": datetime.date(2026, 9, 1)}),
		("planned but deferred", {"planned_for": TODAY, "start": datetime.date(2026, 9, 1)}),
	],
)
def test_a_deferred_task_is_hidden_from_every_bucket (
	session: sqlalchemy.orm.Session, label: str, kwargs: dict[str, typing.Any]
) -> None:
	""""Don't show me the renewal form until March" has to actually hide it (SPEC.md §6.5)."""

	world = World(session)
	world.task(label, **kwargs)

	agenda = world.agenda(horizon_days=7)

	assert agenda.is_empty


def test_a_defer_that_has_already_lifted_does_not_hide_anything (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The other side of the same rule: a past `start_at` is not a filter."""

	world = World(session)
	world.task("Now actionable", start=datetime.date(2026, 7, 1), planned_for=TODAY)

	assert _titles(world.agenda().today) == ["Now actionable"]


def test_a_finished_task_is_gone_from_the_agenda (session: sqlalchemy.orm.Session) -> None:
	"""An agenda that keeps showing completed work is one nobody reads."""

	world = World(session)
	task = world.task("Already done", planned_for=TODAY)

	subroutine.domain.tasks.update(session, task, status_key="done", now=NOW)

	assert world.agenda(horizon_days=7).is_empty


def test_a_recurrence_template_never_appears (session: sqlalchemy.orm.Session) -> None:
	"""Templates are excluded from every list, search, agenda and rollup (SPEC.md §6.7)."""

	world = World(session)
	task = world.task("Template", planned_for=TODAY)
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
	world.task("Confidential", project=private, planned_for=TODAY)
	world.task("Ordinary", planned_for=TODAY)

	assert _titles(world.agenda().today) == ["Ordinary"]


def test_a_private_project_is_shown_to_its_members (session: sqlalchemy.orm.Session) -> None:
	"""The membership row is what makes a private project reachable (SPEC.md §7.3a)."""

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
	world.task("Confidential", project=private, planned_for=TODAY)

	assert _titles(world.agenda().today) == ["Confidential"]


def test_another_workspace_is_never_included (session: sqlalchemy.orm.Session) -> None:
	"""Every query is scoped by workspace, and the agenda is not an exception."""

	mine = World(session)
	theirs = World(session)

	theirs.task("Not mine", planned_for=TODAY)
	mine.task("Mine", planned_for=TODAY)

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
	which backend answered (SPEC.md §10.3). This test runs on both, which is the only
	reason it means anything.
	"""

	world = World(session)
	world.task("Planned, no deadline", planned_for=TODAY)
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
	""""Today" is the asker's today (SPEC.md §8.6, §9.3).

	At 15:00 UTC on the 30th it is still the 30th in London and already the 31st in Sydney,
	so a task planned for the 31st is on their agenda and not on London's.

	The first draft of this used 23:30 UTC, which proves nothing: London is on British
	Summer Time in July, so 23:30 UTC is already the 31st there too and both agendas agreed.
	"""

	world = World(session)
	world.task("Tomorrow in London", planned_for=datetime.date(2026, 7, 31))

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
