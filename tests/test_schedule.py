"""Tests for the three date fields and the all-day rule, on both backends.

The headline is :func:`test_a_task_due_all_day_friday_is_not_overdue_on_friday_morning`,
which is MVP-PLAN's done-criterion for S2-02 and exists because the naive implementation —
storing an all-day deadline at midnight — passes every test anybody thinks to write and
then makes a user's whole Friday look late.

Everything else here holds the rest of §6.5 in place: deadlines end the day and defers
start it, ``starts_at`` is a date with no time at all, invariant 8 is evaluated on what
the user sees, and the timezone chain resolves the way the specification says.
"""

import datetime
import typing
import uuid
import zoneinfo

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.system
import subroutine.db.models.work
import subroutine.domain.bootstrap
import subroutine.domain.projects
import subroutine.domain.schedule
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors

LONDON = "Europe/London"

#: Thursday 30 July 2026, mid-afternoon UTC. Every date in this file is relative to it.
NOW = datetime.datetime(2026, 7, 30, 14, 0, tzinfo=datetime.UTC)


def _workspace (
	session: sqlalchemy.orm.Session, *, timezone: str = LONDON
) -> subroutine.db.models.identity.Workspace:
	"""Create a seeded workspace whose owner has no timezone opinion of their own."""

	owner = subroutine.domain.users.create(
		session, username=f"founder-{uuid.uuid4().hex[:8]}", timezone=timezone
	)

	return subroutine.domain.workspaces.create(
		session,
		slug=f"ws-{uuid.uuid4().hex[:8]}",
		title="Test workspace",
		owner=owner,
		timezone=timezone,
	)


def _project (
	session: sqlalchemy.orm.Session, workspace: subroutine.db.models.identity.Workspace
) -> subroutine.db.models.project.Project:
	"""Create a project to hang tasks off."""

	return subroutine.domain.projects.create(
		session,
		workspace_id=workspace.id,
		key=f"P{uuid.uuid4().hex[:10].upper()}",
		title="Test project",
	)


def _task (
	session: sqlalchemy.orm.Session, **kwargs: typing.Any
) -> subroutine.db.models.work.Task:
	"""Create a task with the shared clock and timezone, overridable per call."""

	workspace = _workspace(session, timezone=kwargs.pop("workspace_timezone", LONDON))

	kwargs.setdefault("title", "Test task")
	kwargs.setdefault("now", NOW)
	kwargs.setdefault("timezone", LONDON)

	return subroutine.domain.tasks.create(
		session, project=_project(session, workspace), **kwargs
	)


def _instant (value: datetime.datetime | None) -> datetime.datetime:
	"""Return a date column's value, asserting it is set.

	Every date column is nullable, so reading one is a ``datetime | None``. Going through
	here rather than asserting in place also sidesteps a mypy trap this file walked into:
	an ``assert task.due_at is not None`` narrows the attribute for the rest of the
	function, including across a call that clears it, and everything after the next
	``assert task.due_at is None`` is then reported as unreachable.
	"""

	assert value is not None

	return value


def _local (instant: datetime.datetime | None, timezone: str = LONDON) -> str:
	"""Render an instant where the user is, for legible assertions."""

	return _instant(instant).astimezone(zoneinfo.ZoneInfo(timezone)).strftime(
		"%Y-%m-%d %H:%M:%S.%f"
	)


def test_a_task_due_all_day_friday_is_not_overdue_on_friday_morning (
	session: sqlalchemy.orm.Session,
) -> None:
	"""S2-02's done-criterion, and the reason the all-day rule exists at all.

	Storing "due Friday" at midnight would make this task overdue from the instant Friday
	began — every all-day task in the system permanently late by up to a day, which nobody
	notices until a month of them have piled up.
	"""

	task = _task(session, due=datetime.date(2026, 7, 31))

	assert task.due_is_all_day
	assert _local(task.due_at) == "2026-07-31 23:59:59.999999"

	friday_morning = datetime.datetime(2026, 7, 31, 8, 0, tzinfo=zoneinfo.ZoneInfo(LONDON))
	friday_late = datetime.datetime(2026, 7, 31, 23, 30, tzinfo=zoneinfo.ZoneInfo(LONDON))
	saturday = datetime.datetime(2026, 8, 1, 0, 30, tzinfo=zoneinfo.ZoneInfo(LONDON))

	assert not subroutine.domain.schedule.is_overdue(task, now=friday_morning)
	assert not subroutine.domain.schedule.is_overdue(task, now=friday_late)
	assert subroutine.domain.schedule.is_overdue(task, now=saturday)


def test_a_defer_lands_at_the_start_of_its_day (session: sqlalchemy.orm.Session) -> None:
	"""The mirror of the rule above: "not before Monday" means from midnight, not to it."""

	task = _task(session, snooze=datetime.date(2026, 8, 3))

	assert task.snoozed_is_all_day
	assert _local(task.snoozed_until) == "2026-08-03 00:00:00.000000"


def test_a_timed_deadline_is_stored_as_the_instant_it_names (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Not every deadline is a whole day, and a time of day must survive intact."""

	task = _task(session, due="2026-08-01T17:00:00Z")

	assert not task.due_is_all_day
	assert task.due_at == datetime.datetime(2026, 8, 1, 17, 0, tzinfo=datetime.UTC)


def test_a_bare_date_string_means_the_whole_day (session: sqlalchemy.orm.Session) -> None:
	"""``2026-08-01`` is a date. Read as midnight it would silently become an instant."""

	task = _task(session, due="2026-08-01")

	assert task.due_is_all_day
	assert _local(task.due_at) == "2026-08-01 23:59:59.999999"


def test_a_relative_expression_sets_a_date (session: sqlalchemy.orm.Session) -> None:
	"""§9.3's grammar reaches the service layer, which is what `subroutine plan 42 tomorrow` needs."""

	task = _task(session, due="end_of_week", starts="tomorrow")

	# The 30th is a Thursday; the week ends on Sunday the 2nd.
	assert _local(task.due_at) == "2026-08-02 23:59:59.999999"
	assert _local(task.starts_at) == "2026-07-31 00:00:00.000000"


def test_all_day_can_be_stated_rather_than_inferred (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Quick capture knows things the value does not say — "before Sunday" means all of it."""

	task = _task(session, due="tomorrow", due_is_all_day=True)

	assert task.due_is_all_day
	assert _local(task.due_at) == "2026-07-31 23:59:59.999999"


def test_a_start_given_as_a_day_begins_at_the_start_of_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A whole day is stored as its first instant, *where the caller is* (docs/design.md §6.5).

	**This used to assert the opposite** — that the field was a bare date carrying no time and
	no zone. `#854` made it an instant so an appointment can say two o'clock, and the all-day
	flag beside it is what stops a client rendering midnight at somebody. Boundary.START is
	the half that matters: a deadline of "Friday" runs out at the *end* of Friday and a start
	of "Friday" begins at the beginning of it.
	"""

	task = _task(session, starts=datetime.date(2026, 8, 5))

	assert _local(task.starts_at) == "2026-08-05 00:00:00.000000"
	assert task.starts_is_all_day


def test_a_planned_day_is_the_day_where_the_caller_is (
	session: sqlalchemy.orm.Session,
) -> None:
	"""An instant near midnight is a different day in different places, and the user wins."""

	late = datetime.datetime(2026, 7, 30, 23, 30, tzinfo=datetime.UTC)

	sydney = _task(session, starts="today", now=late, timezone="Australia/Sydney")
	london = _task(session, starts="today", now=late, timezone=LONDON)

	assert _local(sydney.starts_at, "Australia/Sydney") == "2026-07-31 00:00:00.000000"
	assert _local(london.starts_at) == "2026-07-31 00:00:00.000000"

	los_angeles = _task(session, starts="today", now=late, timezone="America/Los_Angeles")

	# **Read back where it was written**, which is the whole assertion: the same instant is
	# the 30th in Los Angeles and the 31st in London, and the field means the writer's day.
	assert (
		_local(los_angeles.starts_at, "America/Los_Angeles") == "2026-07-30 00:00:00.000000"
	)


def test_a_defer_after_the_deadline_is_refused (session: sqlalchemy.orm.Session) -> None:
	"""Invariant 8, at creation."""

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		_task(session, due=datetime.date(2026, 8, 1), snooze=datetime.date(2026, 8, 5))

	assert raised.value.status == 422
	assert raised.value.errors[0].field == "snoozed_until"


def test_a_defer_and_a_deadline_on_the_same_day_are_allowed (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Evaluated on the rendered dates when both are all-day (docs/design.md §6.5).

	Both stored instants differ — midnight against the last microsecond — so this passes
	either way today. It is asserted because the comparison must stay meaningful in the
	user's terms if either boundary ever moves.
	"""

	task = _task(session, due=datetime.date(2026, 8, 1), snooze=datetime.date(2026, 8, 1))
	start, due = _instant(task.snoozed_until), _instant(task.due_at)

	assert start < due
	assert subroutine.domain.schedule.local_date(
		start, LONDON
	) == subroutine.domain.schedule.local_date(due, LONDON)


def test_invariant_eight_is_checked_against_the_task_not_the_request (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Moving only the deadline must still agree with the defer already on the task.

	The failure this guards against is checking only the fields the caller mentioned, which
	lets a task end up deferred until after it is due in two valid-looking steps.
	"""

	task = _task(session, due=datetime.date(2026, 8, 10), snooze=datetime.date(2026, 8, 5))

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.tasks.update(session, task, due=datetime.date(2026, 8, 1), now=NOW)

	# Refused, and nothing moved.
	assert subroutine.domain.schedule.local_date(
		_instant(task.due_at), LONDON
	) == datetime.date(2026, 8, 10)


def test_a_date_can_be_cleared_but_omitting_it_leaves_it_alone (
	session: sqlalchemy.orm.Session,
) -> None:
	"""docs/design.md §8.3's distinction, on the fields it was written for."""

	task = _task(session, due=datetime.date(2026, 8, 10), starts=datetime.date(2026, 8, 9))

	subroutine.domain.tasks.update(session, task, title="Renamed", now=NOW)

	assert _instant(task.due_at)
	assert _local(task.starts_at) == "2026-08-09 00:00:00.000000"

	subroutine.domain.tasks.update(session, task, due=None, now=NOW)

	cleared: datetime.datetime | None = task.due_at

	assert cleared is None
	assert not task.due_is_all_day
	assert _local(task.starts_at) == "2026-08-09 00:00:00.000000"


def test_changing_a_date_is_recorded_as_a_change (session: sqlalchemy.orm.Session) -> None:
	"""Dates are in the snapshot, so moving a deadline reaches the change feed."""

	task = _task(session, due=datetime.date(2026, 8, 10))
	before = task.version

	subroutine.domain.tasks.update(session, task, due=datetime.date(2026, 8, 11), now=NOW)

	assert task.version == before + 1
	assert subroutine.domain.schedule.local_date(
		_instant(task.due_at), LONDON
	) == datetime.date(2026, 8, 11)


def test_a_task_records_the_zone_its_dates_were_written_in (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Needed for recurrence across daylight saving and for rendering an all-day date."""

	task = _task(session, due=datetime.date(2026, 8, 1))

	assert task.timezone == LONDON


def test_the_timezone_falls_back_from_the_user_to_the_workspace_to_utc () -> None:
	"""docs/design.md §6.5's chain, asserted directly rather than through a task."""

	zone_for = subroutine.domain.schedule.zone_for

	assert zone_for(explicit="Asia/Tokyo") == "Asia/Tokyo"
	assert zone_for() == "UTC"


def test_a_completed_task_is_never_overdue (session: sqlalchemy.orm.Session) -> None:
	"""An overdue list that includes finished work is an overdue list nobody reads."""

	task = _task(session, due=datetime.date(2026, 7, 1))
	long_after = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)

	assert subroutine.domain.schedule.is_overdue(task, now=long_after)

	subroutine.domain.tasks.update(session, task, status_key="done", now=NOW)

	assert not subroutine.domain.schedule.is_overdue(task, now=long_after)


def test_a_task_with_no_deadline_is_never_overdue (session: sqlalchemy.orm.Session) -> None:
	"""Most tasks have no deadline at all, and none of them are late."""

	task = _task(session)

	assert not subroutine.domain.schedule.is_overdue(
		task, now=datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)
	)


def test_an_all_day_deadline_ends_the_day_where_the_user_is (
	session: sqlalchemy.orm.Session,
) -> None:
	""""Due Friday" in London is not the same instant as "due Friday" in Los Angeles.

	docs/design.md §6.5's opening claim about all-day flags, asserted: without the local snap, a
	task due Friday in London would be due Thursday on the American west coast.
	"""

	london = _task(session, due=datetime.date(2026, 7, 31), timezone=LONDON)
	los_angeles = _task(
		session, due=datetime.date(2026, 7, 31), timezone="America/Los_Angeles"
	)

	assert _instant(los_angeles.due_at) > _instant(london.due_at)

	assert _local(london.due_at) == "2026-07-31 23:59:59.999999"
	assert _local(los_angeles.due_at, "America/Los_Angeles") == "2026-07-31 23:59:59.999999"


@pytest.mark.parametrize("written", ["", "   ", "next friday", "01/08/2026", "2026-13-01"])
def test_a_date_that_is_not_in_any_accepted_form_is_refused (
	session: sqlalchemy.orm.Session, written: str
) -> None:
	"""Refused naming the forms that would have worked, rather than guessed at."""

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		_task(session, due=written)

	assert raised.value.status == 422
	assert raised.value.errors[0].field == "due_at"


def test_the_timezone_chain_runs_user_workspace_instance (
	session: sqlalchemy.orm.Session,
) -> None:
	"""docs/design.md §6.5's chain in full, each level shadowing the one below it.

	Null means *not stated* at every level, which is why the workspace column is nullable:
	a default of UTC there would have shadowed the instance for every workspace created
	without an explicit zone, leaving a step in the chain nothing could reach.
	"""

	zone_for = subroutine.domain.schedule.zone_for

	instance = subroutine.db.models.system.Instance(name="I", timezone="America/New_York")
	workspace = subroutine.db.models.identity.Workspace(slug="w", title="W")
	user = subroutine.db.models.identity.User(username="u", username_normalized="u")

	# Nothing stated anywhere below the instance.
	assert zone_for(user=user, workspace=workspace, instance=instance) == "America/New_York"

	workspace.timezone = "Europe/Berlin"

	assert zone_for(user=user, workspace=workspace, instance=instance) == "Europe/Berlin"

	user.timezone = LONDON

	assert zone_for(user=user, workspace=workspace, instance=instance) == LONDON
	assert zone_for(user=user, workspace=workspace, instance=instance, explicit="UTC") == "UTC"

	# And UTC only when there is nothing at all — which `init` makes unreachable.
	assert zone_for() == "UTC"


def test_init_records_the_machines_timezone_on_the_instance (
	session: sqlalchemy.orm.Session,
) -> None:
	"""An installation has a locality, and it is not necessarily its users'."""

	installed = subroutine.domain.bootstrap.initialise(
		session,
		username=f"si-{uuid.uuid4().hex[:8]}",
		instance_name="Test instance",
		timezone="America/New_York",
	)

	assert installed.instance.timezone == "America/New_York"


def test_a_task_falls_back_to_the_instance_when_nothing_else_says (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The case that motivated the column: a remote instance in another zone.

	A workspace on a New York instance, and a task created there with no user or workspace
	opinion, is authored in New York — so a person in London reading it later has both
	halves of "your 16:00 is their 10:00" rather than a date silently read as UTC.
	"""

	installed = subroutine.domain.bootstrap.initialise(
		session,
		username=f"si-{uuid.uuid4().hex[:8]}",
		instance_name="New York instance",
		timezone="America/New_York",
	)

	# Neither the workspace nor the actor states one.
	installed.workspace.timezone = None
	installed.user.timezone = None
	session.flush()

	task = subroutine.domain.tasks.create(
		session, project=installed.inbox, title="Stand-up", due=datetime.date(2026, 8, 3)
	)

	assert task.timezone == "America/New_York"

	# 23:59:59.999999 in New York is 04:59 the next morning in London — which is exactly
	# the difference a client needs the instance zone to be able to explain.
	assert _local(task.due_at, "America/New_York") == "2026-08-03 23:59:59.999999"
	assert _local(task.due_at, LONDON) == "2026-08-04 04:59:59.999999"


def test_a_workspace_with_no_timezone_follows_the_instance (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Not stated means *follow the installation*, not UTC.

	So moving an instance's timezone moves every workspace that never chose one, and leaves
	alone every workspace that did.
	"""

	installed = subroutine.domain.bootstrap.initialise(
		session,
		username=f"si-{uuid.uuid4().hex[:8]}",
		instance_name="Test instance",
		timezone="Australia/Sydney",
	)
	installed.workspace.timezone = None
	session.flush()

	assert subroutine.domain.schedule.zone_for(
		workspace=installed.workspace, instance=installed.instance
	) == "Australia/Sydney"
