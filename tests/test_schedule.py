"""Tests for the three date fields and the all-day rule, on both backends.

The headline is :func:`test_a_task_due_all_day_friday_is_not_overdue_on_friday_morning`,
which is MVP-PLAN's done-criterion for S2-02 and exists because the naive implementation —
storing an all-day deadline at midnight — passes every test anybody thinks to write and
then makes a user's whole Friday look late.

Everything else here holds the rest of §6.5 in place: deadlines end the day and defers
start it, ``starts_at`` is a date with no time at all, invariant 8 is evaluated on what
the user sees, and the timezone chain resolves the way the specification says.
"""

import ast
import datetime
import pathlib
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
import subroutine.domain.capture
import subroutine.domain.dates
import subroutine.domain.projects
import subroutine.domain.schedule
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors

#: This checkout, so the structural guard at the foot of this file can be handed a tree.
ROOT = pathlib.Path(__file__).resolve().parent.parent

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
	# **The word a caller sends, not the column** — `SR#1317`. `snoozed_until` is not a field
	# any endpoint accepts, so a reader who acted on it was refused a second time.
	assert raised.value.errors[0].field == "snooze"


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
	assert raised.value.errors[0].field == "due", "SR#1317: the word a caller sends"


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


def test_a_word_that_names_a_day_is_stored_as_that_whole_day () -> None:
	"""`#988`: a deadline of ``today`` was the first microsecond of it, and read as overdue.

	§9.3's expressions are a grammar of **instants** — which is why ``start_of_day`` and
	``end_of_day`` both exist — and ``today`` is defined as the former inside it. That
	definition is right and is untouched. What was wrong is where it reached: somebody writing
	``--due today`` means the day, and a deadline stored at midnight has already gone by the
	time anybody reads it.

	**The rule existed, in one of the readers.** ``domain.capture`` knew these words named
	whole days and nothing else did, so ``add "… by today"`` was right while ``--due today``
	and ``{"due": "today"}`` were not — `#149`'s shape rather than two copies disagreeing.

	**The claim is that the word and the date are indistinguishable**, at both boundaries, for
	every word in the shared set — and that the surface which already had the rule agrees with
	the two that have just been given it. Written against the set rather than against literal
	instants so a fourth whole-day word is covered the day somebody adds one, and so this
	cannot drift from the boundary rule it rests on.
	"""

	assert subroutine.domain.dates.WHOLE_DAY_KEYWORDS, "the shared vocabulary could not be read"

	for keyword in sorted(subroutine.domain.dates.WHOLE_DAY_KEYWORDS):
		day = subroutine.domain.schedule.local_date(
			subroutine.domain.dates.resolve(keyword, now=NOW, timezone=LONDON), LONDON
		)

		for boundary in subroutine.domain.schedule.Boundary:
			by_word = subroutine.domain.schedule.interpret(
				keyword, boundary=boundary, timezone=LONDON, now=NOW, field="due_at"
			)
			by_date = subroutine.domain.schedule.interpret(
				day.isoformat(), boundary=boundary, timezone=LONDON, now=NOW, field="due_at"
			)

			assert by_word == by_date, f"'{keyword}' and {day} differ at {boundary.name}"
			assert by_word.is_all_day, keyword

		# The surface that already had the rule, held against the two that now share it.
		captured = subroutine.domain.capture.parse(
			f"Pay the rent by {keyword}", now=NOW, timezone=LONDON
		)

		assert captured.due == day, keyword
		assert captured.due_is_all_day, keyword


def test_a_word_that_names_a_moment_is_still_an_instant () -> None:
	"""The other half of `#988`, and what stops the fix reaching further than the defect.

	``start_of_day`` and ``end_of_day`` exist so that somebody can ask for an instant on a
	given day, and ``now+7d`` is arithmetic. **An offset makes a whole-day word arithmetic
	too** — ``today+2h`` is two in the morning — which is the same line ``domain.capture``
	draws and the reason the inference matches the bare word only.

	Derived from :data:`subroutine.domain.dates.KEYWORDS` rather than listed, so a keyword
	added tomorrow has to declare which kind it is instead of quietly defaulting.
	"""

	for keyword in subroutine.domain.dates.KEYWORDS:
		if keyword in subroutine.domain.dates.WHOLE_DAY_KEYWORDS:
			continue

		moment = subroutine.domain.schedule.interpret(
			keyword,
			boundary=subroutine.domain.schedule.Boundary.END,
			timezone=LONDON,
			now=NOW,
			field="due_at",
		)

		assert not moment.is_all_day, keyword

	offset = subroutine.domain.schedule.interpret(
		"today+2h",
		boundary=subroutine.domain.schedule.Boundary.END,
		timezone=LONDON,
		now=NOW,
		field="due_at",
	)

	assert not offset.is_all_day, "an offset is arithmetic, not a day"


# ---- every day-scale date names the zone it was stored in (`SR#1093`) -----------------------

#: The three columns that hold a day rather than a moment (decision `SR#1088` §2). Each is an
#: instant at one end of its own day, local to whoever set it, so *which day* it is cannot be
#: read off the stored value without the zone beside it.
DAY_SCALE = frozenset({"due_at", "starts_at", "snoozed_until"})

#: Where a bare ``.date()`` on one of :data:`DAY_SCALE` would be correct, and why.
#:
#: **Empty, and it was written with two entries before it was run.** Both were assumed rather
#: than measured — ``domain/schedule.py`` and ``domain/agenda.py`` looked like obvious
#: exceptions and neither truncates a day-scale *attribute* at all: the conversions there read
#: ``instant.astimezone(zone).date()``, where the thing being truncated is already local. The
#: stale-entry check below refused both on this guard's first run, which is the only reason
#: this comment is true rather than plausible.
CONVERTS_ELSEWHERE: dict[str, str] = {}


def _day_scale_truncations (tree: pathlib.Path) -> dict[str, list[str]]:
	"""Return every ``<expr>.<day-scale field>.date()`` in ``tree``, by file.

	**Read structurally rather than as text**, which `SR#1092` settled the hard way: a scan
	over source counts a name inside this very docstring as a use, and one over *tokens* gives
	a different answer on Python 3.11 than on 3.12. An attribute chain is an attribute chain on
	every version, and a docstring is an ``ast.Constant`` that is never a call.

	**It takes the tree as an argument** (`SR#405`), so a test can hand it one built to contain
	a known offender — no offenders found and nothing to find are otherwise the same answer.

	**It deliberately under-reports.** A day-scale value assigned to a local first —
	``when = task.due_at`` and then ``when.date()`` — is invisible here, because following that
	would mean type inference rather than a shape. All six sites this family has had were the
	direct chain, and a scan nobody can turn off must not cry wolf.
	"""

	found: dict[str, list[str]] = {}

	for path in sorted(tree.rglob("*.py")):
		if "versions" in path.parts:
			continue

		for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
			# `x.due_at.date()` — a call, on the attribute `date`, of an attribute chain whose
			# own attribute is one of the three. `.date` with no call is a column reference.
			if not isinstance(node, ast.Call):
				continue

			if not isinstance(node.func, ast.Attribute) or node.func.attr != "date":
				continue

			inner = node.func.value

			if isinstance(inner, ast.Attribute) and inner.attr in DAY_SCALE:
				name = str(path.relative_to(tree.parent))
				found.setdefault(name, []).append(f"{inner.attr} at line {node.lineno}")

	return found


def test_a_span_is_stored_from_both_ends_and_each_edge_takes_its_own_boundary (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A fortnight off is fifteen whole days, and the two edges are not symmetrical — `SR#1235`.

	**The start is the first microsecond of its day and the end is the last**, which is §6.5's
	all-day rule applied to a span: a holiday beginning on the 14th begins as the 14th does, and
	one ending on the 28th is over when the 28th is rather than as it starts. Getting the second
	one wrong loses a day off every holiday anybody books, silently, and it looks correct in the
	database.
	"""

	task = _task(
		session, starts="2026-08-14", ends="2026-08-28", title="Away"
	)

	london = zoneinfo.ZoneInfo(LONDON)

	assert _instant(task.starts_at).astimezone(london).date() == datetime.date(2026, 8, 14)
	assert _instant(task.ends_at).astimezone(london).date() == datetime.date(2026, 8, 28)

	assert task.starts_is_all_day is True
	assert _instant(task.starts_at).astimezone(london).time() == datetime.time(0, 0)
	assert _instant(task.ends_at).astimezone(london).time() == datetime.time(
		23, 59, 59, 999999
	), "an end on its own last day stops as it begins, which loses the day"


def test_a_span_that_could_not_mean_anything_is_refused_by_name (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The three ways to write an end that says nothing — `SR#1235`, `schedule.check_span`.

	**In the service rather than in a CHECK constraint**, per this project's rule: the database
	can refuse the row and cannot name the field, say which of the two to move, or fire at all
	on SQLite for the third of these.

	**The shapes are compared after interpretation, not as they were typed.** ``2026-08-28`` is a
	whole day and ``2026-08-28T15:00:00Z`` is a time, and it is what they *became* that has to
	agree — so this drives the service rather than the parser.

	**Each ``field`` is asserted as a field, not as a substring of the message** (`SR#1311`).
	The previous version asked whether ``"starts_is_all_day"`` appeared anywhere in
	``str(errors)``, under a comment saying the refusal has to name a field somebody can send —
	and it passed while ``field`` was ``ends_is_all_day``, because the *message* contained the
	other name. It could not fail for the thing it was about.
	"""

	written_start, _ = subroutine.domain.schedule.DATE_FIELDS["starts_at"]
	written_end, shape = subroutine.domain.schedule.DATE_FIELDS["ends_at"]

	with pytest.raises(subroutine.errors.ValidationError) as alone:
		_task(session, ends="2026-08-28", title="An end and no beginning")

	assert [error.field for error in alone.value.errors] == [written_end]
	assert written_start in alone.value.errors[0].message

	with pytest.raises(subroutine.errors.ValidationError) as backwards:
		_task(session, starts="2026-08-28", ends="2026-08-14", title="Finishes first")

	assert [error.field for error in backwards.value.errors] == [written_end]

	with pytest.raises(subroutine.errors.ValidationError) as mixed:
		_task(
			session,
			starts="2026-08-14",
			ends="2026-08-28T15:00:00Z",
			title="A day at one end and a time at the other",
		)

	assert [error.field for error in mixed.value.errors] == [shape], (
		"the refusal has to name a field somebody can actually send, and an end has no flag"
	)

	# **One whole day at both ends is legitimate and must not be caught by the ordering rule.**
	# A public holiday is exactly that, and comparing the stored instants would pass it by
	# accident — midnight against the last microsecond — which is why the comparison is on days.
	same = _task(session, starts="2026-08-31", ends="2026-08-31", title="Bank holiday")

	assert same.ends_at is not None


def test_moving_one_edge_of_a_span_is_checked_against_the_other (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A caller who mentions one end is still held to the one already stored — `SR#1235`.

	The rule invariant 8 states for a defer and a deadline, and it bites harder here: a change
	that names only the start is checked against nothing unless the stored end is read back, so
	a booked fortnight could be pushed past its own finish by an edit that never mentioned it.
	"""

	task = _task(session, starts="2026-08-14", ends="2026-08-28", title="Away")

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.tasks.update(session, task, starts="2026-09-04", now=NOW)

	# **And the end alone, the other way round**, because a rule aimed at one direction of a
	# symmetric problem never fires for the other.
	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.tasks.update(session, task, ends="2026-08-01", now=NOW)

	subroutine.domain.tasks.update(session, task, ends="2026-09-04", now=NOW)

	assert _instant(task.ends_at).astimezone(zoneinfo.ZoneInfo(LONDON)).date() == (
		datetime.date(2026, 9, 4)
	)


def test_no_day_scale_date_is_truncated_without_its_zone () -> None:
	"""The Python twin of `SR#773`'s browser guard, which is the half that was missing.

	``tests/test_web.py`` has held this rule on ``app.js`` for weeks and has never been the
	thing that broke. Nothing held it on Python — so of the six sites this family had, **five
	were found one at a time by a reviewer reading the code** and the sixth (`SR#1090`) survived
	a fix that named three of its siblings.

	**A deadline is right by 3,600 seconds and that is why nobody meets this.** A day-scale
	deadline stores the *end* of its day, so in a zone ahead of UTC it lands at ``22:59:59Z``
	and a missing conversion still reports the right day. A start or a defer stores the
	*beginning*, so the same zone puts it at ``23:00:00Z`` the day before. The two boundaries
	fail in opposite directions and UTC is the only zone where both are right — which is this
	machine, every CI job, and every test anybody has written here.

	**Measured on the live instance the day this was written**: 47 all-day values, every one
	rendering correctly, and every one for an accidental reason — 27 London deadlines saved by
	the end-of-day rule, and the only three all-day starts in existence all authored in UTC.
	The exposure was nil and loaded.
	"""

	found = _day_scale_truncations(ROOT / "src" / "subroutine")
	offenders = {
		path: where for path, where in found.items() if path not in CONVERTS_ELSEWHERE
	}

	assert not offenders, (
		"a day-scale date is truncated with no zone, so it will report the day either side of "
		f"itself for anybody not on UTC: {offenders} — route it through `schedule.day_in`, or "
		"record why it is right in CONVERTS_ELSEWHERE"
	)


def test_the_day_scale_scan_finds_a_truncation_and_ignores_a_conversion (
	tmp_path: pathlib.Path,
) -> None:
	"""Driven against a tree built to hold one of each, which is the only way to know.

	`SR#405`: a check that cannot be handed its subject can only confirm the arrangement it was
	written from. The floor beside it is the same lesson — a scan whose glob has come adrift
	reports a clean tree in exactly the words a clean tree produces.

	**The conversion case is the one that matters.** `schedule.day_in` ends in ``.date()`` too,
	one call deeper, so a scan that merely looked for the four characters would flag every
	correct site and be turned off within a week.
	"""

	source = tmp_path / "subroutine"
	source.mkdir()
	(source / "bad.py").write_text(
		"def render (task):\n\treturn task.starts_at.date().isoformat()\n", encoding="utf-8"
	)
	(source / "good.py").write_text(
		"def render (task):\n"
		"\treturn schedule.day_in(task.starts_at, task.timezone).isoformat()\n",
		encoding="utf-8",
	)

	found = _day_scale_truncations(source)

	assert set(found) == {"subroutine/bad.py"}, found


def test_every_excused_file_still_exists_and_still_truncates () -> None:
	"""An excuse whose reason has expired reads exactly like a considered decision.

	Both directions, because only one of them is the usual mistake: a file that has stopped
	truncating no longer needs excusing, and one that has gone entirely takes its reason with
	it. This is the shape `SR#405` went round the repository adding.
	"""

	found = _day_scale_truncations(ROOT / "src" / "subroutine")
	stale = sorted(set(CONVERTS_ELSEWHERE) - set(found))

	assert not stale, (
		f"{stale} are excused from converting a day-scale date and no longer truncate one — "
		"delete the entry"
	)


# ---- every moment shown as a day names the zone it is read in (`SR#1091`) --------------------

#: The surfaces that render a stored moment for somebody to read, relative to the repository
#: root. Not the whole tree: ``domain/`` computes with dates and legitimately truncates one it
#: has already converted, so a rule aimed at rendering has to be aimed at the renderers.
RENDERS_FOR_A_READER = ("src/subroutine/views.py", "src/subroutine/cli", "src/subroutine/mcp")

#: Where a rendering surface may take a day off a value itself, and why.
#:
#: **Empty, and measured rather than assumed.** Every site that needed one turned out to be
#: better written the other way — the two remaining conversions went through
#: ``schedule.day_in`` and read *more* clearly for it, and the one refusal that had no zone in
#: reach (``domain/authentication``) was more correct naming the instant than a day.
READS_A_LOCAL_MOMENT: dict[str, str] = {}


def _moments_read_without_a_zone (tree: pathlib.Path, within: tuple[str, ...]) -> dict[str, list[str]]:
	"""Return every ``.date()`` and every argument-less ``.astimezone()`` under ``within``.

	**Two spellings of one mistake, which is why one scan finds both.** ``x.date()`` asks what
	day a moment fell on *in the zone it is stored in*, which is UTC — the server's. A bare
	``x.astimezone()`` asks the same question of ``/etc/localtime`` — the machine's, which for
	every relayed connection since `SR#539` is the server's too. Neither is anybody's zone, and
	both have shipped: `SR#1091` found nine of the first and two of the second.

	**It takes the tree and the reach as arguments** (`SR#405`), so a test can hand it one built
	to hold a known offender. No offenders found and nothing to find are otherwise the same
	answer, and that is how a scan goes quietly inert.

	**A correct conversion is not a special case here, it is a different function.**
	``schedule.day_in`` and ``views.moment_day`` both end in ``.date()`` one call deeper, in
	``domain/``, which is outside this reach — so the rule is *call the function* rather than
	*call it and then explain yourself*, and there is nothing for a register to hold.
	"""

	found: dict[str, list[str]] = {}
	targets: list[pathlib.Path] = []

	for named in within:
		where = tree / named

		targets.extend([where] if where.is_file() else sorted(where.rglob("*.py")))

	for path in targets:
		for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
			if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
				continue

			if node.args or node.keywords:
				continue

			if node.func.attr in ("date", "astimezone"):
				name = str(path.relative_to(tree))
				found.setdefault(name, []).append(f"{node.func.attr}() at line {node.lineno}")

	return found


def test_no_moment_is_shown_as_a_day_without_a_zone () -> None:
	"""The mirror of the guard above, and decision `SR#1088` is why they are two rules.

	**A day is a label and a moment is a point in time.** A day renders in the zone that set
	it and never converts, so the guard above says *reach for the value's own stored zone*. A
	moment has no day at all until somebody names one, so this says *reach for the reader's* —
	the account's per §6.5, which is published on ``/v1/me`` and ``/v1/meta`` precisely so no
	client has to hold a copy of the chain.

	**Do not close this by giving ``created_at`` a stored zone.** That is the obvious-looking
	fix and it answers the wrong question: *what day was that?* depends on who is asking, not
	on who wrote it.

	Thirteen sites when this was written — a credential's last use and expiry, a feed's last
	poll, a comment's day and an event's day at the terminal and for an agent — and two of
	them were the machine's zone rather than the server's, including the *heading* an event
	listing groups by, which put two days' work under one date and called it by the earlier
	name.
	"""

	found = _moments_read_without_a_zone(ROOT, RENDERS_FOR_A_READER)
	offenders = {
		path: where for path, where in found.items() if path not in READS_A_LOCAL_MOMENT
	}

	assert not offenders, (
		"a rendering surface is taking a day off a moment in the server's or the machine's "
		f"zone rather than the reader's: {offenders} — route it through `views.moment_day` "
		"or `schedule.day_in` with the account's zone, or record why it is right in "
		"READS_A_LOCAL_MOMENT"
	)


def test_the_moment_scan_finds_both_spellings_and_ignores_a_conversion (
	tmp_path: pathlib.Path,
) -> None:
	"""Driven against a tree holding one of each, because a floor is not a fixture.

	The conversion case is the one that decides whether this is usable: an ``.astimezone(zone)``
	*with* an argument is the correct spelling and appears beside the wrong one constantly, so
	a scan that could not tell them apart would be turned off within the week.
	"""

	source = tmp_path / "src" / "subroutine"
	source.mkdir(parents=True)
	(source / "shown.py").write_text(
		"def render (event, zone):\n"
		"\ta = event.created_at.date()\n"
		"\tb = event.created_at.astimezone()\n"
		"\tc = event.created_at.astimezone(zone)\n"
		"\td = views.moment_day(event.created_at, zone)\n"
		"\treturn a, b, c, d\n",
		encoding="utf-8",
	)

	found = _moments_read_without_a_zone(tmp_path, ("src/subroutine/shown.py",))

	assert set(found) == {"src/subroutine/shown.py"}, found
	assert found["src/subroutine/shown.py"] == ["date() at line 2", "astimezone() at line 3"]


def test_every_surface_the_moment_scan_reads_is_one_that_exists () -> None:
	"""A reach naming a path that has moved reads exactly like a clean tree.

	`SR#405`'s floor, in the form this scan needs: it walks three named places rather than the
	whole tree, so a rename is the way it goes silently inert. Both halves — the places exist,
	and excusing one that no longer offends is an expired reason left standing.
	"""

	missing = [named for named in RENDERS_FOR_A_READER if not (ROOT / named).exists()]

	assert not missing, f"{missing} no longer exist, so this scan reads less than it names"

	found = _moments_read_without_a_zone(ROOT, RENDERS_FOR_A_READER)
	stale = sorted(set(READS_A_LOCAL_MOMENT) - set(found))

	assert not stale, (
		f"{stale} are excused from naming a zone and no longer read a moment as a day — "
		"delete the entry"
	)
