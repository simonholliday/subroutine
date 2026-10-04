"""A task that repeats: the template, the one live instance, and what advances it.

`#94`, and the half of it that touches the database — :mod:`tests.test_recurrence` covers the
grammar and the dates, which are pure. This is the template/instance machinery §6.7 designed
and decision `#915` sharpened.

**The shape to hold while reading**: a rule-bearing row is a *template*, it is not worked on
and it appears in no listing; exactly one *instance* is live at a time; and finishing an
instance is what brings the next one into being.
"""

import concurrent.futures
import datetime
import functools
import threading
import typing
import uuid
import zoneinfo

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.cli.personal
import subroutine.config
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.readiness
import subroutine.domain.recurrence
import subroutine.domain.refs
import subroutine.domain.scoping
import subroutine.domain.tags
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.versions
import subroutine.errors
import subroutine.views
import test_schedule

LONDON = test_schedule.LONDON

#: A Saturday in August, so a weekly rule cannot pass by landing on the day it started.
NOW = datetime.datetime(2026, 8, 15, 9, 0, tzinfo=datetime.UTC)


def _repeating (
	session: sqlalchemy.orm.Session, **kwargs: typing.Any
) -> subroutine.db.models.work.Task:
	"""Create a repeating task and return the instance, as ``create`` does."""

	kwargs.setdefault("title", "Water the plants")
	kwargs.setdefault("now", NOW)
	kwargs.setdefault("due", datetime.date(2026, 8, 31))

	return test_schedule._task(session, **kwargs)


def _template (
	session: sqlalchemy.orm.Session, instance: subroutine.db.models.work.Task
) -> subroutine.db.models.work.Task:
	"""Return the template an instance came from, asserting there is one."""

	assert instance.recurrence_template_id is not None, "this instance has no template"

	found = session.get(subroutine.db.models.work.Task, instance.recurrence_template_id)

	assert found is not None

	return found


def _next_live (
	session: sqlalchemy.orm.Session, template: subroutine.db.models.work.Task
) -> subroutine.db.models.work.Task:
	"""Return the one unfinished occurrence of a series, asserting there is exactly one."""

	live = session.scalars(
		sqlalchemy.select(subroutine.db.models.work.Task).where(
			subroutine.db.models.work.Task.recurrence_template_id == template.id,
			subroutine.db.models.work.Task.completed_at.is_(None),
		)
	).all()

	assert len(live) == 1, f"expected one live occurrence, found {len(live)}"

	return live[0]


def _held (row: subroutine.db.models.work.Task, column: str) -> typing.Any:
	"""Read one column off a row through a call, so asserting on it narrows nothing.

	mypy carries an ``assert row.x is not None`` past a call that clears it, and reports what
	follows a later ``assert row.x is None`` as unreachable - ``test_schedule._instant``'s trap.
	"""

	return getattr(row, column)


def _predicate (
	session: sqlalchemy.orm.Session,
	task: subroutine.db.models.work.Task,
	predicate: typing.Callable[..., typing.Any],
) -> bool | None:
	"""Ask one of ``domain.readiness``'s predicates about one row."""

	model = subroutine.db.models.work.Task

	return session.scalar(
		sqlalchemy.select(predicate(model, now=NOW))
		.select_from(model)
		.where(model.id == task.id)
	)


def test_a_repeat_makes_a_template_and_hands_back_the_instance (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§6.7: the response is the thing you act on, with the rule behind it.

	The row the caller's fields built *becomes* the template rather than a third thing being
	assembled beside it — it already carries the title, project, dates and priorities somebody
	typed, which is exactly what each occurrence inherits.
	"""

	instance = _repeating(session, recurrence="every month on the 30th")
	template = _template(session, instance)

	assert template.is_template
	assert template.recurrence_rule == "FREQ=MONTHLY;BYMONTHDAY=30"
	assert template.recurrence_text == "every month on the 30th"
	assert template.recurrence_anchor == "schedule"
	assert template.recurrence_trigger == "completion"

	assert not instance.is_template
	assert instance.recurrence_rule is None, "an instance carries the rule by reference only"
	assert instance.occurrence_at is not None
	assert instance.title == template.title
	assert instance.ref != template.ref, "each is its own item with its own number"


def test_a_repeat_that_names_its_own_day_is_given_that_day (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1208`. The rule said which day it fell on and nothing wrote that day down.

	`SR#94` lets a self-anchoring rule be filed without a date rather than refusing it for not
	saying when — which it does say. What it did not do is give the row one, so the template
	carried a rule and no date at all and every surface that draws a date had nothing to draw.

	**A whole day, not the minute it was typed.** Anchoring on the filing instant gave each slot
	that instant's time of day, so a client drew a one-minute appointment at whatever o'clock
	somebody was at their desk.
	"""

	instance = _repeating(session, recurrence="every month on the 1st", due=None)
	template = _template(session, instance)

	assert template.due_at is not None, (
		"a rule that names its own days still leaves the series with no date, so nothing that "
		"draws a date can draw it"
	)
	assert template.due_is_all_day, (
		"the series is a timed appointment rather than a day, and its rule names no time"
	)
	assert template.due_at.astimezone(datetime.UTC).day == 1, template.due_at

	# **The occurrence inherits it by the ordinary shift**, which is what makes this a fix at the
	# root rather than a patch on the instance: nothing in `materialise` is special-cased for it.
	assert instance.due_at is not None and instance.due_is_all_day
	assert instance.occurrence_at == instance.due_at, (
		"`_is_on_its_grid` compares these two, so a series whose occurrence parts company with "
		"its own slot looks rescheduled from the day it is filed"
	)


def test_a_repeating_deadline_and_its_series_are_minted_on_the_same_instant (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1291`, and this is the root cause rather than the symptom.**

	§6.5 stores an all-day deadline at the last microsecond of its day, and ``dateutil.rrule``
	builds its time set from ``dtstart``'s hour, minute and second and **keeps no microsecond**.
	So every repeating deadline there has ever been is anchored on exactly the value that engine
	rounds off — and ``materialise`` computes ``occurrence - anchor`` and moves the whole grid by
	it, minting every occurrence 999999µs before its own template.

	**Both rendered as the same date, which is why nobody ever saw it.** It only became visible
	when a save turned that phantom offset into a real move and carried it to the series.

	Asserted on the instants, because the dates agree while the instants do not — a test
	comparing what a person sees passes against the defect.
	"""

	instance = _repeating(
		session, title="Pay council tax", recurrence="every month on the 1st", due=None
	)
	template = _template(session, instance)

	assert template.due_at is not None and instance.due_at is not None
	assert instance.due_at == template.due_at, (
		f"the occurrence was minted at {instance.due_at} and its series holds "
		f"{template.due_at}. They render as the same day and differ by "
		f"{template.due_at - instance.due_at}, which the next save that carries a date turns "
		f"into a real move."
	)
	assert instance.occurrence_at == template.due_at, (
		"the slot the row was minted for has to be the date it was minted on, or `#1248` reads "
		"the series as individually rescheduled"
	)


def test_saving_a_repeating_deadline_without_changing_it_moves_nothing (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1291`, the symptom, measured the way Simon met it.**

	He changed the capitalisation of a title in the browser and his council-tax bill moved from
	the 1st to the 2nd in a real calendar. The browser's form *"sends every control it shows on
	every save"* (`SR#1250`), so a title edit re-sends the date — and re-sending the date an
	item already has was a move.

	**Three saves, because the drift compounded**: feed back the date the product has just shown
	you and it walks a day each time. One save would pass against a version that only drifted on
	the second.
	"""

	instance = _repeating(
		session, title="Pay council tax", recurrence="every month on the 1st", due=None
	)
	template = _template(session, instance)
	settled = template.due_at

	for attempt in range(3):
		subroutine.domain.tasks.update(
			session,
			instance,
			title=f"Pay Council Tax {attempt}",
			due="2026-09-01",
			applies_to=subroutine.domain.tasks.FROM_NOW_ON,
			now=NOW,
			timezone=LONDON,
		)
		session.flush()

		assert template.due_at == settled, (
			f"save {attempt + 1} moved the series to {template.due_at}, from {settled}. "
			f"The calendar feed draws the series, so this is the day the bill is shown on."
		)


def test_a_series_minted_before_the_fix_does_not_drift_on_its_next_save (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1291`, and this is the half the boundary fix does not reach.**

	Every occurrence written before that fix is still stored 999999µs behind its template. The
	next save carrying a date corrects the occurrence — a real move, of a fraction of a second —
	and without this the series takes that move and lands a day out. The browser sends a date on
	every save, so it would have happened to every existing repeating deadline, once, silently.

	**A whole-day date has no sub-day meaning**, so a sub-day difference is not a move. That is
	the rule, and this is it driven against the exact state the old code left behind.
	"""

	instance = _repeating(
		session, title="Pay council tax", recurrence="every month on the 1st", due=None
	)
	template = _template(session, instance)
	settled = template.due_at

	assert instance.due_at is not None and instance.occurrence_at is not None

	# **The state the old code wrote**, reproduced exactly: the occurrence a microsecond short
	# of a second behind the series it belongs to.
	behind = datetime.timedelta(microseconds=999_999)
	instance.due_at -= behind
	instance.occurrence_at -= behind
	session.flush()

	subroutine.domain.tasks.update(
		session,
		instance,
		title="Pay Council Tax",
		due="2026-09-01",
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
		timezone=LONDON,
	)
	session.flush()

	assert template.due_at == settled, (
		f"a row written before the fix moved its series to {template.due_at}, from {settled}"
	)
	assert instance.due_at == settled, (
		"and the save should have quietly put the occurrence back on the boundary it belongs on"
	)


def test_a_repeating_deadline_still_moves_when_somebody_really_moves_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**`SR#1291`'s falsification, and without it the fix is "never carry a date".**

	Ignoring a sub-day difference must not become ignoring a difference. A whole day moved is
	still a whole day moved, and the series has to follow — that is what *every one from now on*
	means.
	"""

	instance = _repeating(
		session, title="Pay council tax", recurrence="every month on the 1st", due=None
	)
	template = _template(session, instance)
	settled = template.due_at

	assert settled is not None

	subroutine.domain.tasks.update(
		session,
		instance,
		due="2026-09-03",
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
		timezone=LONDON,
	)
	session.flush()

	assert template.due_at == settled + datetime.timedelta(days=2), (
		f"the series stayed at {template.due_at} when its occurrence moved two days"
	)


def test_a_repeating_birthday_gets_a_day_it_happens_on_rather_than_a_deadline (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1209`, decision `SR#1235`. The column follows what the item *is*.

	`SR#1208` hardcoded ``due_at`` and said so in the code: *"What a birthday wants is the
	opposite and is `SR#1209`"*. Until this, ``Anna's birthday every year on 14 March`` reached
	somebody's calendar as **``SUMMARY:Due: Anna's birthday``**, yearly, for ever — because the
	feed's wording is decided by which field the date sits in and the phrasing cannot tell a
	birthday from a council-tax payment.

	**The bill is asserted beside it and that is the whole falsification.** One grammar produces
	both; a change that moved every self-dating rule to ``starts_at`` would pass every assertion
	about the birthday and quietly undo Simon's own example from `SR#1208`.

	**The edge is asserted too, and it is the half nothing else catches.** §6.5 stores an all-day
	deadline at the last microsecond of its day and an all-day start at the first; a version that
	picked the column and kept ``Boundary.END`` renders identically everywhere — the calendar
	draws the local date either way — and leaves every comparison that reads the instant as the
	*beginning* of the day out by one. Falsified: with the edge reverted, this test fails and the
	calendar test for the same change still passes.
	"""

	birthday = _repeating(
		session,
		title="Anna's birthday",
		type_key="event",
		recurrence="every year on 14 March",
		due=None,
	)
	bill = _repeating(
		session, title="Pay the council tax", recurrence="every month on the 1st", due=None
	)

	occasion = _template(session, birthday)
	work = _template(session, bill)

	assert occasion.due_at is None, (
		"a birthday was given a deadline, which is what writes `Due: Anna's birthday` into "
		"somebody's calendar every year"
	)
	assert occasion.starts_at is not None and occasion.starts_is_all_day
	assert occasion.starts_at.astimezone(datetime.UTC).day == 14, occasion.starts_at

	# The first microsecond of the day, not the last: a start and a deadline sit at opposite
	# edges of the same date.
	assert occasion.starts_at.astimezone(datetime.UTC).hour == 0, occasion.starts_at

	assert work.due_at is not None and work.starts_at is None, (
		"the bill lost its deadline too, so this moved every self-dating rule rather than the "
		"ones that are not deadlines"
	)

	# **The occurrence follows**, which is what makes this a fix at the root: `_is_on_its_grid`
	# compares `occurrence_at` against `due_at or starts_at`, so a birthday whose slot parted
	# company with its own start would be drawn twice by the calendar.
	assert birthday.starts_at is not None and birthday.due_at is None
	assert birthday.occurrence_at == birthday.starts_at


def test_a_series_filed_before_it_was_dated_still_mints_dated_occurrences (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The compatibility half of `SR#1208`, and it needs its own test to exist at all.

	Creation gives such a template a date now, so the fallback in ``materialise`` is unreachable
	through the ordinary path — which is exactly the shape of a control that is specified,
	documented and inert. **Falsified: with only the creation half in place, the calendar test
	for this passes and this one does not.**

	The state is built the way the old code left it, by clearing the dates the fix now writes.
	That is a template somebody already has on a running instance, and it goes on minting
	occurrences every time one is completed.
	"""

	instance = _repeating(session, recurrence="every month on the 1st", due=None)
	template = _template(session, instance)

	template.due_at = None
	template.due_is_all_day = False
	session.flush()

	minted = subroutine.domain.tasks.materialise(
		session, template, after=instance.occurrence_at, now=NOW
	)

	assert minted is not None, "a series filed before the fix stopped minting anything"
	assert minted.due_at is not None, (
		"an occurrence from an undated series still has no date, so it reaches no calendar — "
		"which is the defect, for every repeat anybody filed before the fix"
	)
	assert minted.due_is_all_day
	assert minted.occurrence_at == minted.due_at, (
		"the slot and the date parted company, so this reads as an occurrence somebody moved"
	)


def test_an_undated_birthday_filed_before_the_fix_mints_a_day_rather_than_a_deadline (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1209`'s half of the compatibility path above, and it is a second copy of one rule.

	``materialise`` computes its own occurrence and snaps it, where ``create`` asks
	:func:`subroutine.domain.tasks.first_whole_day` — so the choice of column exists in two
	places. They agreed for as long as both were hardcoded to ``due_at``, which is exactly the
	condition under which two copies are invisible: nothing compares them and neither is wrong.

	**This is the test that makes them one.** Both now read ``own_day_field`` and snap through
	``whole_day_for``; reverting either half alone fails here or in the sibling above, and
	nothing else in the suite reaches this branch at all — creation dates such a template now,
	so the fallback is only ever exercised by a series somebody already had.
	"""

	instance = _repeating(
		session,
		title="Anna's birthday",
		type_key="event",
		recurrence="every year on 14 March",
		due=None,
	)
	template = _template(session, instance)

	# The state the old code left: a rule, and no date at either end.
	template.starts_at = None
	template.starts_is_all_day = False
	session.flush()

	minted = subroutine.domain.tasks.materialise(
		session, template, after=instance.occurrence_at, now=NOW
	)

	assert minted is not None, "the series stopped minting anything"
	assert minted.due_at is None, (
		"an occurrence of a birthday was given a deadline, so the calendar says `Due:` about a "
		"day nobody owes anybody"
	)
	assert minted.starts_at is not None and minted.starts_is_all_day
	assert minted.starts_at.astimezone(datetime.UTC).day == 14, minted.starts_at
	assert minted.occurrence_at == minted.starts_at, (
		"the slot and the date parted company, so this reads as an occurrence somebody moved"
	)


def test_a_template_is_not_counted_as_an_unfinished_sub_task (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A rule-bearing row does not make its parent unstartable — `SR#2292`.

	``db/models/work.py`` says a template *"is excluded from every list, search, agenda and
	rollup by the default repository filter"*. ``readiness.a_container`` and
	``every_sub_task_is_done`` counted children by ``parent_task_id`` alone, and a template
	inherits that column from the task it was made from and is never completed — so it matched
	every clause for ever.

	**Reproduced as it is actually reachable, which is narrower than it first looks.** While a
	series runs there is always a live occurrence, so the parent is unstartable for a true
	reason and the template's contribution is masked. Measured: an event-shaped repeat does not
	reach it either, because the template carries the same ``ends_at`` and is ``passed``
	alongside its occurrence; nor does cancelling, which mints the next one. What exposes it is
	the template being the **only** live child — here by removing the occurrence — and then the
	parent can never start and `#1615`'s *all sub-tasks are done, somebody can decide* can never
	fire, with every real sub-task finished.

	**Both predicates, because the clause means opposite things in them.** In `a_container` it
	stops a template holding a parent shut; in `every_sub_task_is_done` it stops the question
	being put about a parent whose only child was never a sub-task at all.
	"""

	workspace = test_schedule._workspace(session)
	project = test_schedule._project(session, workspace)

	def made (**kwargs: typing.Any) -> subroutine.db.models.work.Task:
		"""Create one task in the shared project, on the shared clock and timezone."""

		kwargs.setdefault("now", NOW)
		kwargs.setdefault("timezone", LONDON)

		return subroutine.domain.tasks.create(session, project=project, **kwargs)

	parent = made(title="Milestone")
	chore = made(
		title="Weekly chore",
		parent=parent,
		recurrence="every monday",
		due=datetime.date(2026, 8, 31),
	)

	session.flush()
	template = _template(session, chore)

	subroutine.domain.tasks.complete(session, chore, now=NOW)
	session.flush()

	for occurrence in session.scalars(
		sqlalchemy.select(subroutine.db.models.work.Task).where(
			subroutine.db.models.work.Task.recurrence_template_id == template.id,
			subroutine.db.models.work.Task.completed_at.is_(None),
		)
	).all():
		subroutine.domain.tasks.delete(session, occurrence, now=NOW)

	session.flush()

	live = session.scalars(
		sqlalchemy.select(subroutine.db.models.work.Task).where(
			subroutine.db.models.work.Task.parent_task_id == parent.id,
			subroutine.db.models.work.Task.deleted_at.is_(None),
		)
	).all()

	assert sorted(row.is_template for row in live) == [False, True], (
		f"the state this is about was not built: {[(r.title, r.is_template) for r in live]}"
	)

	assert _predicate(session, parent, subroutine.domain.readiness.a_container) is False, (
		"a template held its parent shut, so a milestone whose every real sub-task is finished "
		"can never be started"
	)
	assert (
		_predicate(session, parent, subroutine.domain.readiness.every_sub_task_is_done) is True
	), "nothing put `#1615`'s question, which is the row that feature exists to surface"

	# **And the state that makes the second clause do anything**, which the one above does not:
	# there, the finished occurrence satisfies *has children* whether or not templates are
	# excluded, so the clause in `every_sub_task_is_done` reads as decoration. With no
	# occurrence left, the template is the only child there has ever been — and *all of its
	# sub-tasks are done* is a false thing to say about a parent that never had one.
	subroutine.domain.tasks.delete(session, chore, now=NOW)
	session.flush()

	remaining = session.scalars(
		sqlalchemy.select(subroutine.db.models.work.Task).where(
			subroutine.db.models.work.Task.parent_task_id == parent.id,
			subroutine.db.models.work.Task.deleted_at.is_(None),
		)
	).all()

	assert [row.is_template for row in remaining] == [True], (
		f"the template should be the only child left: {[r.title for r in remaining]}"
	)

	assert (
		_predicate(session, parent, subroutine.domain.readiness.every_sub_task_is_done) is False
	), (
		"`#1615`'s question was put about a parent whose only child is a rule-bearing row, so "
		"a reader is asked to decide about sub-tasks that never existed"
	)


def test_a_template_is_in_no_listing_and_its_instance_is (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§6.7. A template has a ref, a status and a position, so unless filtered it appears
	everywhere — the listing, search, the agenda, `next` and every rollup.

	The exclusion lives in ``scoping.readable_tasks``, which every listing goes through, rather
	than being remembered at each call site. That is what makes it hold for the surfaces
	written after it.
	"""

	instance = _repeating(session, recurrence="every day")
	template = _template(session, instance)

	# Any member will do: the point is the template filter, not who is asking.
	owner = session.scalars(
		sqlalchemy.select(subroutine.db.models.identity.User).limit(1)
	).one()
	principal = subroutine.domain.authentication.Principal(user=owner)

	visible = set(
		session.scalars(
			subroutine.domain.scoping.readable_tasks(
				principal, workspace_ids=[instance.workspace_id]
			)
		)
	)

	assert instance in visible
	assert template not in visible, "a rule is not work and must not be listed as some"

	# **And asking for them brings it back**, which is what says the exclusion is a filter
	# rather than the row being unreachable.
	with_templates = set(
		session.scalars(
			subroutine.domain.scoping.readable_tasks(
				principal,
				workspace_ids=[instance.workspace_id],
				include_templates=True,
			)
		)
	)

	assert template in with_templates


def test_finishing_one_occurrence_brings_the_next (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The whole feature in one test: close it, and the next one is waiting."""

	first = _repeating(
		session, recurrence="every month on the 30th", due=datetime.date(2026, 8, 30)
	)
	template = _template(session, first)

	subroutine.domain.tasks.complete(session, first, now=NOW)

	minted = session.scalars(
		sqlalchemy.select(subroutine.db.models.work.Task).where(
			subroutine.db.models.work.Task.recurrence_template_id == template.id,
			subroutine.db.models.work.Task.completed_at.is_(None),
		)
	).all()

	assert len(minted) == 1, "finishing one occurrence should leave exactly one live"

	assert minted[0].due_at is not None
	assert minted[0].due_at > (first.due_at or NOW)
	assert minted[0].completed_at is None


def test_the_next_one_appears_however_the_status_was_set (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**The reason this hangs off the transition rather than off ``complete()``.**

	A done status is set by the board's drag, the browser's status control and a plain
	``update(status=…)`` as well as by the complete verb. A repeat that advanced on one surface
	and not the others would be worse than none — the failure is silent and the user's evidence
	is an item that simply stopped coming back.
	"""

	first = _repeating(session, recurrence="every day")
	template = _template(session, first)

	subroutine.domain.tasks.update(
		session,
		first,
		status_key=subroutine.domain.tasks.finished_status_key(session, first.workspace_id),
		now=NOW,
	)

	live = session.scalars(
		sqlalchemy.select(subroutine.db.models.work.Task).where(
			subroutine.db.models.work.Task.recurrence_template_id == template.id,
			subroutine.db.models.work.Task.completed_at.is_(None),
		)
	).all()

	assert len(live) == 1


def test_finishing_twice_does_not_mint_twice (session: sqlalchemy.orm.Session) -> None:
	"""A retry is not a second occurrence.

	``completed_at is not None`` is the test for *was it already finished* (`#723`), and the
	same reading gates this — otherwise a caller pressing *Complete* twice, or any client
	retrying, would spend a month of the series per press.
	"""

	first = _repeating(session, recurrence="every day")
	template = _template(session, first)

	subroutine.domain.tasks.complete(session, first, now=NOW)
	subroutine.domain.tasks.complete(session, first, now=NOW)

	made = session.scalars(
		sqlalchemy.select(subroutine.db.models.work.Task).where(
			subroutine.db.models.work.Task.recurrence_template_id == template.id
		)
	).all()

	assert len(made) == 2, f"one press per occurrence, and there were two presses: {len(made)}"


def test_a_schedule_anchor_holds_the_grid_however_late_you_were (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§6.7's first anchor, and the case that tells it from the other one.

	**The rule here is an interval deliberately.** The first version used "every month on the
	30th" and could not fail: re-anchored on a completion of 6 September, the next 30th is
	still 30 September, so both anchors agree and the test passed against an implementation
	that ignored the distinction entirely. Found by falsifying — the mutation that made every
	series behave like `completion` left this green.

	With `every 14 days` from 15 August the grid runs 29 August, 12 September. Finishing on
	the 6th must give the 12th — on the grid, and ahead. Measuring from the completion instead
	would give the 20th, and taking the next grid slot after the *completed* occurrence rather
	than after the completion would give 29 August, which is already behind us.
	"""

	first = _repeating(
		session, recurrence="every 14 days", due=datetime.date(2026, 8, 15)
	)

	late = datetime.datetime(2026, 9, 6, 11, 0, tzinfo=datetime.UTC)
	subroutine.domain.tasks.complete(session, first, now=late)

	occurrence = _next_live(session, _template(session, first)).occurrence_at

	assert occurrence is not None
	assert occurrence.astimezone(zoneinfo.ZoneInfo(LONDON)).date() == datetime.date(
		2026, 9, 12
	)


def test_a_completion_anchor_measures_from_when_it_was_actually_done (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§6.7's second anchor, and the one the first would get wrong.

	"Every 14 days" means fourteen days after you last watered the plants — not fourteen days
	after you meant to. Finishing a week late moves the whole series, which is the point.
	"""

	first = _repeating(
		session,
		recurrence="every 14 days",
		recurrence_anchor="completion",
		due=datetime.date(2026, 8, 15),
	)

	late = datetime.datetime(2026, 9, 1, 11, 0, tzinfo=datetime.UTC)
	subroutine.domain.tasks.complete(session, first, now=late)

	nxt = _next_live(session, _template(session, first))

	assert nxt.occurrence_at is not None

	# Fourteen days after the completion, not after the original deadline — which would have
	# been the 29th of August and is already behind us.
	assert nxt.occurrence_at.date() == datetime.date(2026, 9, 15)


def test_an_exhausted_series_finishes_its_template_rather_than_lingering (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§6.7: exhaustion materialises nothing and marks the template complete.

	Left open, a spent rule sits in the workspace for ever as something that can never fire
	again — reachable by ref, excluded from every listing, and impossible to notice.
	"""

	first = _repeating(session, recurrence="FREQ=DAILY;COUNT=2")
	template = _template(session, first)

	subroutine.domain.tasks.complete(session, first, now=NOW)

	second = _next_live(session, template)

	subroutine.domain.tasks.complete(session, second, now=NOW)

	session.refresh(template)

	assert template.completed_at is not None, "a spent series should not stay open"


def test_a_repeat_with_no_date_is_refused (session: sqlalchemy.orm.Session) -> None:
	""""Every month" says how often, not when.

	Anchored to the moment it was filed, the series would fall on whatever day somebody
	happened to type it — a date they did not choose and will not remember choosing.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		_repeating(session, recurrence="every month", due=None)

	assert "date to repeat from" in str(refused.value)


def test_a_repeat_nobody_finishes_is_refused_by_name (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**The half that is not built, saying so** rather than storing a rule visible nowhere.

	A ``time`` series materialises no instance at all, so until a date-ranged view expands it
	— the agenda, and `#916`'s feed — filing one would be a rule that appears in no listing and
	on no calendar. §6.13 rule 1: refuse rather than accept silently.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		_repeating(session, recurrence="every year on 19 august", recurrence_trigger="time")

	assert "not built yet" in str(refused.value)


def test_the_combination_that_cannot_mean_anything_is_refused (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#915`: a ``completion`` anchor with a ``time`` trigger has nothing to measure from.

	Refused in the service rather than by a CHECK constraint, so the message names which of
	the two to change — a constraint would arrive as a driver error naming no field at all,
	and on SQLite might not fire.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		_repeating(
			session,
			recurrence="every day",
			recurrence_anchor="completion",
			recurrence_trigger="time",
		)

	assert refused.value.errors[0].field == "recurrence_anchor"


def test_a_snooze_is_not_carried_into_the_next_occurrence (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A defer is somebody saying "not yet" about *one* occurrence.

	Repeated, it would hide every future one for a reason that applied once, which is a
	disappearance nobody asked for and `#854`'s defect wearing a new hat.
	"""

	first = _repeating(
		session, recurrence="every day", snooze=datetime.date(2026, 8, 16)
	)
	template = _template(session, first)

	# **On the first occurrence since `SR#3898`** (decision `SR#3915`), the one it was given for,
	# and never on the series row, which nothing reads.
	assert first.snoozed_until is not None, "the fixture did not snooze anything"
	assert template.snoozed_until is None, "the series row was deferred"

	subroutine.domain.tasks.complete(session, first, now=NOW)

	assert _next_live(session, template).snoozed_until is None


def test_a_repeat_can_be_changed_from_the_occurrence_in_hand (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§6.7: editing the template affects future occurrences — and nobody navigates to one.

	**The template is in no listing**, so a person changing *how this repeats* is looking at
	the instance and addressing it. Applying the rule to that one occurrence instead would
	write it onto a row that mints nothing and is forgotten the moment it is completed.
	"""

	first = _repeating(session, recurrence="every day")
	template = _template(session, first)

	subroutine.domain.tasks.update(session, first, recurrence="every monday", now=NOW)

	session.refresh(template)

	assert template.recurrence_rule == "FREQ=WEEKLY;BYDAY=MO"
	assert first.recurrence_rule is None, "the occurrence still carries it by reference only"


def test_a_repeat_can_be_stopped_and_the_occurrence_stays (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Stopping is not deleting: the work in hand is real and keeps its ref and history."""

	first = _repeating(session, recurrence="every day")
	template = _template(session, first)

	subroutine.domain.tasks.update(session, first, recurrence=None, now=NOW)

	session.refresh(template)

	assert template.completed_at is not None, "a stopped series is a finished template"
	assert first.completed_at is None, "the occurrence in hand was not touched"

	# **And nothing follows it**, which is the whole point rather than a side effect.
	subroutine.domain.tasks.complete(session, first, now=NOW)

	live = session.scalars(
		sqlalchemy.select(subroutine.db.models.work.Task).where(
			subroutine.db.models.work.Task.recurrence_template_id == template.id,
			subroutine.db.models.work.Task.completed_at.is_(None),
		)
	).all()

	assert not live, "a stopped series should not mint another occurrence"


def test_an_ordinary_task_can_be_made_to_repeat_and_keeps_its_number (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Adding a repeat to something already on the list must not replace it.

	It has a ref somebody has written down, a history and possibly comments. Turning the task
	itself into the template would take it out of every listing and put an identical-looking
	stranger in its place — which is what a person would see, without being told why.
	"""

	plain = test_schedule._task(
		session, title="Pay the rent", now=NOW, due=datetime.date(2026, 8, 30)
	)
	was = plain.ref

	subroutine.domain.tasks.update(
		session, plain, recurrence="every month on the 30th", now=NOW
	)

	assert plain.ref == was, "the task somebody was looking at kept its number"
	assert not plain.is_template
	assert plain.recurrence_template_id is not None

	template = _template(session, plain)

	assert template.is_template
	assert template.recurrence_rule == "FREQ=MONTHLY;BYMONTHDAY=30"

	# And it now behaves like any other occurrence.
	subroutine.domain.tasks.complete(session, plain, now=NOW)

	assert _next_live(session, template).occurrence_at is not None


def test_stopping_something_that_does_not_repeat_is_refused_by_name (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Rather than answered with a cheerful 200 that changed nothing."""

	plain = test_schedule._task(session, title="One-off", now=NOW)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tasks.stop_repeating(session, plain, now=NOW)

	assert "not part of a repeating series" in str(refused.value)


def test_how_a_repeat_is_measured_can_be_changed_without_re_sending_the_rule (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#918`. **The capability was unreachable and every surface reported success.**

	``recurrence_anchor`` and ``recurrence_trigger`` were readable only inside the rule's own
	branch of ``update``, so naming either alone reached nothing, moved no version and
	answered *Changed*. A caller had to re-send a rule they were not changing in order to
	change the thing beside it.

	The anchor is the field that decides what the *next* date will be, so getting it wrong is
	not cosmetic: on a schedule anchor this comes back every third day whatever you do, and on
	a completion anchor three days after you last finished.
	"""

	first = _repeating(session, recurrence="every 3 days")
	template = _template(session, first)

	assert template.recurrence_anchor == "schedule", "the default this test moves off"

	subroutine.domain.tasks.update(session, first, recurrence_anchor="completion", now=NOW)

	session.refresh(template)

	assert template.recurrence_anchor == "completion"

	# **And the rule it qualifies is untouched**, which is the half a caller was previously
	# forced to re-send — and therefore the half most likely to be sent back slightly wrong.
	assert template.recurrence_rule == "FREQ=DAILY;INTERVAL=3"


def test_naming_only_a_qualifier_on_something_that_does_not_repeat_is_refused (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#918`, the other half — and the two want opposite answers.

	An anchor with a series behind it means *measure it differently*; an anchor with nothing
	behind it describes a repeat that does not exist, so there is nothing to apply it to.
	Accepting it stored nothing and said so nowhere, which is `#379`'s swallowed argument.
	"""

	plain = test_schedule._task(session, title="One-off", now=NOW)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tasks.update(session, plain, recurrence_anchor="completion", now=NOW)

	# **Names the field**, per review dimension 4: a caller told only "invalid" has to guess
	# which of the three it sent is the problem.
	assert "recurrence_anchor" in str(refused.value.errors)


def test_a_qualifier_at_creation_with_no_rule_is_refused (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#918`. ``create`` had the mirror of ``update``'s hole and lost the value just as quietly.

	``_repeat`` returned ``None`` the moment the rule was ``None``, before it read either
	qualifier — so a task was filed, 201 was answered, and the anchor was gone.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		_repeating(session, title="No rule at all", recurrence_anchor="completion")

	assert "recurrence_anchor" in str(refused.value.errors)


def test_an_occurrence_reports_how_it_is_measured_and_not_only_how_often (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#918`'s read half, and the reason one field falling back is worse than none.

	``recurrence_rule`` resolved through to the template and its two qualifiers did not, so an
	occurrence answered *every three days* with ``recurrence_anchor: null`` — which reads as
	*not set* rather than as *not carried on this row*. A caller who had just changed the
	anchor could not read back what they had set.
	"""

	first = _repeating(session, recurrence="every 3 days", recurrence_anchor="completion")

	shown = subroutine.views.task(
		first, subroutine.views.Vocabulary.for_tasks(session, None, [first])
	)

	assert shown.recurrence_rule == "FREQ=DAILY;INTERVAL=3"
	assert shown.recurrence_anchor == "completion"
	assert shown.recurrence_trigger == "completion"

	# The words somebody typed travel with it too — read back from the template, so a form
	# reopening this shows the phrase rather than the rule it compiled to.
	assert shown.recurrence_text == "every 3 days"


def test_a_stopped_series_stops_saying_it_repeats (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#920`. **A claim about the future that is already known to be false.**

	Stopping a repeat completes the template rather than clearing a column, and the occurrence
	in hand goes on pointing at it — so a view reading straight through advertised a rule that
	would never fire again, on the one surface somebody checks to see their *stop* worked.
	"""

	first = _repeating(session, recurrence="every month on the 30th")

	shown = subroutine.views.task(
		first, subroutine.views.Vocabulary.for_tasks(session, None, [first])
	)

	assert shown.recurrence_rule == "FREQ=MONTHLY;BYMONTHDAY=30", "the state this moves off"

	subroutine.domain.tasks.update(session, first, recurrence=None, now=NOW)

	stopped = subroutine.views.task(
		first, subroutine.views.Vocabulary.for_tasks(session, None, [first])
	)

	assert stopped.recurrence_rule is None
	assert stopped.recurrence_anchor is None
	assert stopped.recurrence_text is None

	# **The backlink survives**, deliberately: *this came from that series* stays true after it
	# ends, and it is how anybody reaches what happened before.
	assert stopped.recurrence_template_ref is not None


@pytest.mark.parametrize("applies_to", [None, "this_one", "from_now_on"])
@pytest.mark.parametrize("spelling", ["every month", "FREQ=MONTHLY"])
def test_a_stopped_series_sent_its_own_rule_starts_again (
	spelling: str, applies_to: str | None, session: sqlalchemy.orm.Session
) -> None:
	"""`SR#4404`, R2-M4 of the cold review of 2026-10-04: its own rule, sent back, was no change.

	That shortcut keeps the browser's every save from moving a series' version, and it also ate the
	one save that means something: a stopped series sent its own rule is somebody starting it again,
	and it stayed stopped, on both spellings and every answer to which occurrences.
	"""

	first = _repeating(session, recurrence="every month")
	series = _template(session, first)

	subroutine.domain.tasks.update(session, first, recurrence=None, now=NOW)

	assert _held(series, "completed_at") is not None, "the state this moves off"

	subroutine.domain.tasks.update(
		session, first, recurrence=spelling, now=NOW, applies_to=applies_to
	)

	assert _held(series, "completed_at") is None, "its own rule did not start it again"
	assert _next_live(session, series) is first, "starting again minted beside the open one"


def test_a_series_started_again_by_a_new_rule_has_something_to_come (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4404`, R2-L19 of the cold review of 2026-10-04: started again, it had nothing open.

	Stopped after its last occurrence was done, a series sent a new rule said it repeated - and a
	series mints only when an occurrence is finished, so nothing ever came round. The next one comes
	after the last slot it had, not on it again.
	"""

	first = _repeating(session, recurrence="every month")
	series = _template(session, first)

	subroutine.domain.tasks.update(session, first, recurrence=None, now=NOW)
	subroutine.domain.tasks.complete(session, first, now=NOW)

	subroutine.domain.tasks.update(session, first, recurrence="every week", now=NOW)

	assert series.completed_at is None
	assert series.recurrence_rule == "FREQ=WEEKLY"

	coming = _next_live(session, series)

	assert coming is not first
	assert first.due_at is not None and coming.due_at is not None
	assert coming.due_at.date() == first.due_at.date() + datetime.timedelta(days=7), (
		"it was not counted from the last slot the series had"
	)


def test_a_series_its_own_rule_ended_is_not_started_again_with_nothing_to_come (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4404`: a series ended by its own ``UNTIL``, every occurrence done, sent its rule later.

	Starting a stopped series again by its own rule, alone, reopened this one: it said it repeated
	and its rule had no date left to give. It is asked what a new rule is asked, and refused.
	"""

	rule = "FREQ=DAILY;UNTIL=20260901T235959Z"
	first = _repeating(session, recurrence=rule)
	series = _template(session, first)

	subroutine.domain.tasks.complete(session, first, now=NOW)
	subroutine.domain.tasks.complete(session, _next_live(session, series), now=NOW)

	assert series.completed_at is not None, "the rule ran out, so the series closed"

	later = datetime.datetime(2026, 9, 10, 9, 0, tzinfo=datetime.UTC)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tasks.update(session, first, recurrence=rule, now=later)

	assert refused.value.detail == "That repeat names no dates that have not already passed."
	assert series.completed_at is not None, "it was started again anyway"


def test_an_exhausted_series_stops_saying_it_repeats_too (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#920`, by the other route to *nothing follows this* — and one condition covers both.

	A ``COUNT`` running out closes the template through the same path a deliberate stop takes,
	so a rule keyed on *was this stopped* rather than *is this template finished* would have
	been right about one of the two and confidently wrong about the other.
	"""

	first = _repeating(session, recurrence="FREQ=DAILY;COUNT=2")
	template = _template(session, first)

	subroutine.domain.tasks.complete(session, first, now=NOW)

	second = _next_live(session, template)

	subroutine.domain.tasks.complete(session, second, now=NOW)
	session.refresh(template)

	assert template.completed_at is not None, "the series ran out, so the template closed"

	spent = subroutine.views.task(
		second, subroutine.views.Vocabulary.for_tasks(session, None, [second])
	)

	assert spent.recurrence_rule is None


def test_two_completions_at_once_mint_one_next_occurrence (
	engine: sqlalchemy.engine.Engine,
) -> None:
	"""`#927`'s H-11 — two terminals finishing the same chore left two of the next one.

	``update`` reads the task at the top of its transaction, works out that this write is the
	one that finishes it, and mints the next occurrence. Two transactions that both read the
	unfinished row both concluded they had finished it, so the series advanced twice: two
	occurrences at the same due date, two refs burnt, and a person left to work out which row
	to delete. `README.md` sells *"Run several agents at once without collisions."*

	**Closed by H-12's fix rather than by a second one**, which is worth stating because the
	finding proposes serialising the status write. ``VersionMixin`` now writes every change
	under the version it was read at, so the second completion's ``UPDATE`` matches no row and
	never reaches ``materialise`` — the same condition, in the one place that covers every
	write rather than in the one place somebody remembered. This test exists to hold that
	claim: if the two ever stop being the same fix, it fails here rather than in a backlog.

	Real connections and a barrier, for the reason ``test_api_concurrency`` gives: the shared
	fixture exists to stop tests seeing each other's transactions, and this is entirely about
	what two of them do to one row.
	"""

	factory = sqlalchemy.orm.sessionmaker(bind=engine, expire_on_commit=False)
	workspace_id: uuid.UUID | None = None
	accounts: set[uuid.UUID] = set()

	def _users (session: sqlalchemy.orm.Session) -> set[uuid.UUID]:
		"""Return every account id there is, so the seed's own can be told apart."""

		return set(
			session.scalars(sqlalchemy.select(subroutine.db.models.identity.User.id)).all()
		)

	try:
		with factory() as setup:
			# **Read before and after rather than naming what the helper makes.**
			# `_repeating` reaches `test_schedule._workspace`, which creates a founder — and
			# the first version of this cleanup deleted the workspace and not the account,
			# which is the recorded shape of `test_concurrent_ref_allocation` exactly. It
			# left 247 tests in `test_transport_equivalence` failing, in a full run only.
			before = _users(setup)

			instance = _repeating(setup, recurrence="every day")
			template = _template(setup, instance)

			setup.commit()
			task_id, template_id = instance.id, template.id
			workspace_id = instance.workspace_id
			accounts = _users(setup) - before

		both_read = threading.Barrier(2)

		def finish () -> bool:
			"""Complete the occurrence from an independent connection."""

			with factory() as worker:
				row = worker.get(subroutine.db.models.work.Task, task_id)

				assert row is not None

				both_read.wait(timeout=30)

				try:
					subroutine.domain.tasks.update(worker, row, status_key="done")
					worker.commit()

					return True

				except (subroutine.errors.Conflict, subroutine.domain.versions.RACED):
					worker.rollback()

					return False

		with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
			finished = [
				future.result() for future in [pool.submit(finish) for _ in range(2)]
			]

		assert sum(finished) == 1, "both callers were told they finished the same occurrence"

		with factory() as check:
			series = check.scalars(
				sqlalchemy.select(subroutine.db.models.work.Task).where(
					subroutine.db.models.work.Task.recurrence_template_id == template_id
				)
			).all()

			live = [one for one in series if one.completed_at is None]

			assert len(live) == 1, (
				f"the series advanced once per completion: {len(live)} occurrences are open"
			)

	finally:
		# This test commits, so it owns **everything** it wrote. The workspace cascades to its
		# projects and tasks; an account does not belong to one and has to go separately.
		with factory() as tidy:
			if workspace_id is not None:
				tidy.execute(
					sqlalchemy.delete(subroutine.db.models.identity.Workspace).where(
						subroutine.db.models.identity.Workspace.id == workspace_id
					)
				)

			if accounts:
				tidy.execute(
					sqlalchemy.delete(subroutine.db.models.identity.User).where(
						subroutine.db.models.identity.User.id.in_(accounts)
					)
				)

			tidy.commit()


# --- Which occurrences an edit is for — item `SR#1247`, decision `SR#1249` ------------------


def test_an_edit_that_does_not_say_is_refused (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1252`, and it is the breaking half Simon took knowingly.

	This answered 200 the day before and answers 422 now. The alternative was keeping the old
	behaviour as the default — every edit landing on the occurrence and nothing reaching the
	series — and he refused it, because an agent silently getting *just this one* is the whole
	defect `SR#1247` reports.

	**The refusal names ``applies_to``**, which is the field an HTTP caller sends. Nothing here
	names ``title``: the argument names in this layer are not words anybody typed.
	"""

	made = _repeating(session, recurrence="every week")
	series = _template(session, made)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tasks.update(session, made, title="Only here", now=NOW)

	assert [field.field for field in refused.value.errors] == ["applies_to"]
	assert refused.value.code == "missing_field"

	# **Nothing was assigned**, which is the guarantee `update`'s docstring makes and the
	# reason the refusal sits in the validation pass: the caller holds a live session it may
	# still commit, so a half-applied change would be committed along with whatever else that
	# transaction was doing.
	assert made.title != "Only here", "a refused edit was applied anyway"
	assert series.title != "Only here"


def test_the_series_itself_cannot_be_edited_without_saying_either (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Both ends of a series ask, because `SR#1247` made the other end reachable.

	``show`` names the template's number now, so somebody can address it directly — and if
	editing *that* row skipped the question, the answer to "how do I change every one" would
	be "find the other row", which is the two-rows model this whole story exists to hide.
	"""

	made = _repeating(session, recurrence="every week")
	series = _template(session, made)

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.tasks.update(session, series, title="Only here", now=NOW)


def test_nothing_excused_from_asking_is_a_field_that_has_gone () -> None:
	"""The stale half. An excuse that outlived its reason reads as a considered decision.

	`SR#405`'s rule, and this register is worth it twice over: an entry naming a parameter
	``update`` no longer takes would silently excuse whatever later took the name — and the
	population is derived, so the *other* direction needs no test at all. Anything patchable
	and not excused asks, by subtraction.
	"""

	gone = subroutine.domain.tasks.NEVER_ASKS - subroutine.domain.tasks.PATCHABLE

	assert not gone, f"{sorted(gone)} are excused from asking and `update` no longer takes them"

	assert len(subroutine.domain.tasks.PATCHABLE) > 15, (
		f"only {sorted(subroutine.domain.tasks.PATCHABLE)} were read off the signature, so the "
		"derivation has stopped working and every field would be excused by accident"
	)


def test_an_update_that_names_no_field_is_never_asked_about (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The empty case, which is what the whole mechanism rests on.

	`update` is reached with nothing to change by every caller that sends only a version, a
	timezone or a lease renewal — and it must go through. What decides it is the set of
	arguments the caller actually *named*, read off the frame before any local exists, so a
	patchable argument added tomorrow is covered without anybody remembering. This is the test
	that would notice that reading going wrong: a capture that saw every parameter rather than
	every parameter *given* would refuse here.
	"""

	made = _repeating(session, recurrence="every week")

	subroutine.domain.tasks.update(session, made, now=NOW)


def test_a_change_with_no_asking_field_is_not_asked_about (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Decision `SR#1249` §1's four exemptions, from the side that matters.

	A status has no second answer and neither has the repeat rule itself, so being asked would
	be friction with no decision in it. **This is the test that stops the refusal becoming a
	toll on every edit of a repeating item**, which is most of what a repeating item's life is.
	"""

	made = _repeating(session, recurrence="every week")

	subroutine.domain.tasks.update(
		session, made, recurrence="every month", now=NOW
	)
	session.flush()

	assert _template(session, made).recurrence_rule is not None


def test_from_now_on_reaches_the_row_that_persists (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The defect `SR#1247` was filed for: a correction that lasted one turn of the wheel.

	Measured on a disposable instance — rename the occurrence, complete it, and the next one
	came back with the old title. Nothing said so, which is what made it worth an item rather
	than a note.
	"""

	made = _repeating(session, recurrence="every week", title="Anna's birthday")
	series = _template(session, made)

	subroutine.domain.tasks.update(
		session,
		made,
		title="Anna's birthday, corrected",
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	session.flush()

	assert made.title == "Anna's birthday, corrected"
	assert series.title == "Anna's birthday, corrected"

	# **The next one, for real**, because the requirement is about what comes round rather than
	# about two rows agreeing at one instant.
	subroutine.domain.tasks.complete(session, made, now=NOW)
	session.flush()

	assert _next_live(session, series).title == "Anna's birthday, corrected"


def test_saving_an_occurrence_at_its_own_date_leaves_the_series_where_it_was (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1334`, from the second cold review of ``61c9de9..fd24dfd``. A move of *nothing*.

	``_deltas`` filtered its comprehension on the walrus — ``and (moved := _moved_by(...))`` —
	so a delta of **exactly zero** was falsy and the column fell out of the dict. ``_carried``
	then reached its catch-all, which reads an absent column as the *other* reason one can be
	absent (cleared, or set from nothing) and copies the source's **absolute** value.

	So a ``from_now_on`` save that moved the date by nothing took the template **onto the
	occurrence's own date** — a whole week forward here, on a save that changed no day at all.

	**The item's second reproduction does not reproduce, and the first one is used here.**
	It reads *"with no zone change at all, a legacy occurrence saving its own unchanged date
	does the same"*; driven, it does not — sending a column the value it already holds is not
	a change, so ``update`` returns before anything propagates and the defect is unreachable
	that way. That reproduction depended on *legacy* rows stored 999999µs behind their
	template, which is the state `#1291` fixed, so it cannot be built on an instance this code
	created. The zone route is the one that still works and is the one asserted.

	**The mechanism was pre-existing and the docstring is what made it a defect.** ``_deltas``
	says a column is absent for three distinguishable reasons and that *"every caller has to
	say what it does with each"* — and the falsy filter quietly made a fourth. It is expressed
	as a delta of zero now, which is what *did not move* means, and applying one is a no-op.
	"""

	made = _repeating(session, recurrence="every week")
	series = _template(session, made)

	# **The second occurrence, because the first shares the template's own date** — with both
	# on one day a delta of zero and a copied absolute value are the same number, and the test
	# would pass against the defect. Completing one materialises the next, a week along.
	subroutine.domain.tasks.complete(session, made, now=NOW)
	session.flush()

	later = _next_live(session, series)
	anchored = series.due_at

	assert anchored is not None
	assert later.due_at is not None
	assert later.due_at != anchored, (
		"this test needs the occurrence and its template on different days to say anything"
	)

	# **Re-dated in another zone, which is the reviewer's own reproduction.** §6.5 stores a
	# whole-day deadline at the last microsecond of its day, so reading the same calendar day
	# in a different zone moves the stored instant by hours — and `_moved_by` rounds that to
	# *nothing*, because a whole-day date has no sub-day meaning (`#1291`). The day does not
	# change; the delta is zero; the column disappeared.
	subroutine.domain.tasks.update(
		session,
		later,
		now=NOW,
		due=later.due_at.date(),
		timezone="America/New_York",
		applies_to="from_now_on",
	)
	session.flush()

	# **The day, read in the zone the series carries, rather than the instant** (`SR#1293`). The
	# zone the occurrence was re-dated in travels to the series, so its deadline moves to the edge
	# of the same day in New York, which comparing instants would call a move. What this test is
	# about is the series landing on the occurrence's own date, a week along.
	carried = _template(session, later)

	assert carried.timezone == "America/New_York" and carried.due_at is not None
	assert carried.due_at.astimezone(zoneinfo.ZoneInfo(carried.timezone)).date() == (
		anchored.astimezone(zoneinfo.ZoneInfo(LONDON)).date()
	), "a save that moved the date by nothing carried the occurrence's own date to the series"


def test_a_repeat_sent_back_unchanged_changes_nothing (session: sqlalchemy.orm.Session) -> None:
	"""`SR#4321`, a Low of the cold review of 2026-10-03, and NEW-D-2 of its verification.

	The browser sends the repeat with every save. A rule stored before the stricter checks was
	refused on the way back, and the whole save with it, title and all; and each save of an
	unchanged repeat moved the series' version, so a client holding it for ``If-Match`` was
	answered 409 after somebody merely retitled an occurrence.
	"""

	made = _repeating(session, recurrence="every monday")
	series = _template(session, made)

	# A rule as one was stored before a part could not be named twice (`SR#3997`).
	old = "FREQ=WEEKLY;BYDAY=MO;BYDAY=MO"
	series.recurrence_rule = old
	session.flush()
	version = series.version

	for title in ("Water the ferns", "Water the ferns and the palm", "Water every plant"):
		subroutine.domain.tasks.update(
			session,
			made,
			now=NOW,
			title=title,
			recurrence=old,
			applies_to=subroutine.domain.tasks.THIS_ONE,
		)

	session.flush()
	session.refresh(series)

	assert made.title == "Water every plant"
	assert series.recurrence_rule == old
	assert series.version == version, "a repeat sent back unchanged moved the series' version"

	# **The control**: a rule that is different is still read, and still refused when it cannot be.
	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.tasks.update(
			session, made, now=NOW, recurrence="FREQ=WEEKLY;BYDAY=TU;BYDAY=TU"
		)


def test_an_unchanged_repeat_sent_back_leaves_the_series_version (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4321`, NEW-D-2: three saves of an unchanged repeat took the series from 1 to 4."""

	made = _repeating(session, recurrence="every monday")
	series = _template(session, made)
	version = series.version

	for _ in range(3):
		subroutine.domain.tasks.update(
			session,
			made,
			now=NOW,
			recurrence="every monday",
			applies_to=subroutine.domain.tasks.THIS_ONE,
		)

	session.flush()
	session.refresh(series)

	assert series.version == version, (version, series.version)


def test_a_series_changed_to_a_rule_that_never_comes_round_is_refused (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#3997`, H-1 of the cold review of 2026-09-30: a change stored a rule create refused.

	Every seventh day from a Monday is never a Tuesday. Create refused that rule, because making a
	series makes its first occurrence; a change made none, so it was stored and the next completion
	finished the series without a word. **Refused as create refuses it, and nothing changed.**
	"""

	made = _repeating(session, recurrence="every week")
	series = _template(session, made)
	before = series.recurrence_rule

	assert made.due_at is not None and made.due_at.date().weekday() == 0, "the due date is a Monday"

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tasks.update(
			session,
			made,
			now=NOW,
			recurrence="FREQ=DAILY;INTERVAL=7;BYDAY=TU",
			applies_to="from_now_on",
		)

	# It names no date at all, which is what it says since `SR#4419`, rather than that they passed.
	assert refused.value.detail == "That repeat never comes round from the date it repeats from."

	session.refresh(series)

	assert series.recurrence_rule == before


@pytest.mark.parametrize("rule", ["FREQ=WEEKLY;UNTIL=20260801", "FREQ=WEEKLY;COUNT=3"])
def test_a_series_changed_to_a_rule_whose_dates_have_all_passed_is_refused (
	rule: str, session: sqlalchemy.orm.Session
) -> None:
	"""`SR#4306`, M3 of the cold review of 2026-10-03, decision `#4312`: checked from the start.

	A weekly series running since spring took an `UNTIL` before today, and a `COUNT` long since
	reached, and the next completion ended it without a word. **From now**, and nothing changed.
	"""

	made = _repeating(
		session, recurrence="every week", due="2026-04-06", now=datetime.datetime(2026, 4, 1, tzinfo=datetime.UTC)
	)
	series = _template(session, made)
	before = series.recurrence_rule

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tasks.update(
			session, made, now=NOW, recurrence=rule, applies_to="from_now_on"
		)

	assert refused.value.detail == "That repeat names no dates that have not already passed."

	session.refresh(series)

	assert series.recurrence_rule == before


def test_a_series_made_with_every_date_behind_it_is_refused (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4306`: due in January and `UNTIL` the end of January, made in August.

	Create walked from the first date, made the overdue January occurrence and handed it back, with
	nothing after it. **And the control**: an `UNTIL` still to come is made as before.
	"""

	with pytest.raises(subroutine.errors.ValidationError):
		_repeating(session, recurrence="FREQ=WEEKLY;UNTIL=20260131", due="2026-01-05")

	made = _repeating(session, recurrence="FREQ=WEEKLY;UNTIL=20261231", due="2026-01-05")

	assert made.due_at is not None


def test_a_rule_ending_before_the_occurrence_already_made_is_refused (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4419`, R2-L16 (a) of the cold review of 2026-10-04: a date between let it through.

	A monthly series finished early has its next occurrence on 1 November. A rule ending on 15
	October, sent on 26 September, was accepted because 1 October is after now, and finishing 1
	November then ended the series without a word. **And the control**: ending in December stands.
	"""

	made = _repeating(
		session,
		recurrence="every month",
		due="2026-10-01",
		now=datetime.datetime(2026, 9, 20, 9, 0, tzinfo=datetime.UTC),
	)
	series = _template(session, made)

	subroutine.domain.tasks.complete(
		session, made, now=datetime.datetime(2026, 9, 25, 9, 0, tzinfo=datetime.UTC)
	)

	live = _next_live(session, series)
	sent = datetime.datetime(2026, 9, 26, 9, 0, tzinfo=datetime.UTC)

	assert live.due_at is not None and live.due_at.date() == datetime.date(2026, 11, 1)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tasks.update(
			session, live, recurrence="FREQ=MONTHLY;UNTIL=20261015T235959Z", now=sent
		)

	assert refused.value.detail == "That repeat ends before the occurrence it has already made."

	subroutine.domain.tasks.update(
		session, live, recurrence="FREQ=MONTHLY;UNTIL=20261215T235959Z", now=sent
	)

	assert series.recurrence_rule is not None and "UNTIL=20261215" in series.recurrence_rule


@pytest.mark.parametrize(
	"rule", ["FREQ=WEEKLY;BYDAY=MO;UNTIL=20260817T235959Z", "FREQ=WEEKLY;BYDAY=MO;COUNT=1"]
)
def test_a_series_may_end_on_the_occurrence_in_hand (
	rule: str, session: sqlalchemy.orm.Session
) -> None:
	"""`SR#4419`, as decision `#4312` keeps it: *stop after this one*, asked at or after its slot.

	A weekly series whose live occurrence is Monday 17 August, sent on Saturday 15 August a rule
	whose last date is that Monday, is accepted - and finishing that one ends the series.
	"""

	made = _repeating(session, recurrence="every monday", due="2026-08-17")
	series = _template(session, made)

	subroutine.domain.tasks.update(session, made, recurrence=rule, now=NOW)
	subroutine.domain.tasks.complete(session, made, now=NOW)

	assert series.completed_at is not None, "finishing the last one did not end the series"


def test_a_date_and_a_rule_in_one_edit_are_asked_together (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4419`, R2-L16 (b): the rule was asked about the date the edit was replacing.

	Moved to 21 December from now on with a rule ending on 10 December, a series was accepted and
	had nothing to come. And the series row of one repeating from 30 June, given 4 January 2027 and
	three occurrences in October, was refused, counted from June. **Each is asked on the date the
	edit leaves.**
	"""

	made = _repeating(session, recurrence="every week", due="2026-08-17")
	series = _template(session, made)

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.tasks.update(
			session,
			made,
			now=NOW,
			due="2026-12-21",
			recurrence="FREQ=WEEKLY;UNTIL=20261210T235959Z",
			applies_to="from_now_on",
		)

	assert series.due_at is not None and series.due_at.date() == datetime.date(2026, 8, 17)

	june = _repeating(session, title="Pay the rent", recurrence="every month", due="2026-06-30")
	rent = _template(session, june)

	subroutine.domain.tasks.update(
		session,
		rent,
		now=datetime.datetime(2026, 10, 4, 9, 0, tzinfo=datetime.UTC),
		due="2027-01-04",
		recurrence="FREQ=MONTHLY;COUNT=3",
		applies_to="this_one",
	)

	assert rent.due_at is not None and rent.due_at.date() == datetime.date(2027, 1, 4)


def test_a_series_counted_from_completion_is_asked_from_now (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4419`, R2-L16 (c): checked along a grid it does not follow.

	Every seven days from when it is done, due 10 August, sent on 15 August a seven-day rule ending
	on 20 August: the grid's 17 August let it through, and finishing it that day ended the series,
	since a week from then is the 22nd. **Asked from now, as its next one is counted.**
	"""

	made = _repeating(
		session,
		recurrence="every 7 days",
		due="2026-08-10",
		recurrence_anchor="completion",
		now=datetime.datetime(2026, 8, 1, 9, 0, tzinfo=datetime.UTC),
	)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tasks.update(
			session,
			made,
			now=NOW,
			recurrence="FREQ=DAILY;INTERVAL=7;UNTIL=20260820T235959Z",
			recurrence_anchor="completion",
		)

	assert refused.value.detail == "That repeat names no dates that have not already passed."


@pytest.mark.parametrize(
	"rule", ["FREQ=DAILY;COUNT=1", "FREQ=DAILY;UNTIL=20260815T235959Z", "FREQ=DAILY;UNTIL=20260815"]
)
def test_a_whole_day_series_ending_today_is_made (
	rule: str, session: sqlalchemy.orm.Session
) -> None:
	"""`SR#4419`, R2-L16 (d): its slot is the start of today, behind the clock and not past.

	A whole-day series starting today and ending today was refused for naming no date still to come.
	**Compared by the day.**
	"""

	made = _repeating(session, recurrence=rule, due=None, starts="2026-08-15")

	assert made.starts_at is not None and made.starts_is_all_day


def test_a_rule_naming_no_date_at_all_says_so (session: sqlalchemy.orm.Session) -> None:
	"""`SR#4419`, R2-L16 (e): *no dates that have not already passed*, said of a date to come.

	A whole-day deadline falls at the end of its day, so an ``UNTIL`` at noon that day ends the
	series the day before - as the program and the feed both read it (`SR#4026`) - and the series
	has no date at all. **Refused, saying that.**
	"""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		_repeating(session, recurrence="FREQ=DAILY;UNTIL=20260905T120000Z", due="2026-09-05")

	assert refused.value.detail == "That repeat never comes round from the date it repeats from."


def test_clearing_the_series_own_date_is_refused_before_anything_changes (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4307`, M9 of the cold review of 2026-10-03: the series' own date, cleared ``this_one``.

	Accepted, and every completion after it was then refused, *A repeat needs a date to repeat
	from*, for a date the occurrence already had. **Refused, and the series keeps its date.**
	"""

	made = _repeating(session, recurrence="every 14 days", due="2026-08-17")
	series = _template(session, made)
	before = series.due_at

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tasks.update(session, series, now=NOW, due=None, applies_to="this_one")

	assert refused.value.detail == "A repeat needs a date to repeat from."

	session.refresh(series)

	assert series.due_at == before


def test_clearing_a_deadline_from_now_on_asks_the_series_dates_not_the_occurrences (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4420`, R2-L18 of the cold review of 2026-10-04: the occurrence's own start let it by.

	Given a start for itself alone, then cleared of its deadline from now on, an occurrence passed
	the check on that start, and the series was left with no date: every completion after it was
	refused, *A repeat needs a date to repeat from*. **Refused, and the series keeps its date.**
	"""

	made = _repeating(session, recurrence="every 14 days", due="2026-08-17")
	series = _template(session, made)
	before = series.due_at

	subroutine.domain.tasks.update(
		session, made, now=NOW, starts="2026-08-16", applies_to="this_one"
	)

	assert series.starts_at is None, "the start reached the series, so this tests nothing"

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tasks.update(
			session, made, now=NOW, due=None, applies_to="from_now_on"
		)

	assert refused.value.detail == "A repeat needs a date to repeat from."

	session.refresh(series)

	assert series.due_at == before


def test_a_stopped_series_may_lose_its_date_and_is_asked_for_one_to_start_again (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4420` and `SR#4404`: a stopped series mints nothing, so it was refused for nothing.

	Clearing its deadline from now on was refused for leaving it undated, where the release before
	accepted it. **Accepted, and then the restart is what asks**: sent its rule again, an undated
	series is refused rather than started with nothing to repeat from.
	"""

	made = _repeating(session, recurrence="every 14 days", due="2026-08-17")
	series = _template(session, made)

	subroutine.domain.tasks.update(session, made, recurrence=None, now=NOW)

	# **Unless the same edit starts it again**, which leaves it undated and repeating at once.
	with pytest.raises(subroutine.errors.ValidationError) as both:
		subroutine.domain.tasks.update(
			session,
			made,
			now=NOW,
			due=None,
			recurrence="every 14 days",
			applies_to="from_now_on",
		)

	assert both.value.detail == "A repeat needs a date to repeat from."
	assert series.completed_at is not None and _held(series, "due_at") is not None

	subroutine.domain.tasks.update(
		session, made, now=NOW, due=None, applies_to="from_now_on"
	)

	assert series.due_at is None and series.starts_at is None, "the state this moves off"

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tasks.update(session, made, recurrence="every 14 days", now=NOW)

	assert refused.value.detail == "A repeat needs a date to repeat from."
	assert series.completed_at is not None, "it was started again with no date"


@pytest.mark.parametrize(
	("starts", "due", "made", "finished", "wanted"),
	[
		(
			"2026-10-23T09:00",
			"2026-10-26T17:00",
			"2026-10-01",
			"2026-10-27",
			(datetime.date(2026, 10, 30), datetime.time(9, 0)),
		),
		(
			"2026-10-23",
			"2026-10-26T17:00",
			"2026-10-01",
			"2026-10-27",
			(datetime.date(2026, 10, 30), datetime.time(0, 0)),
		),
		(
			"2026-03-27T09:00",
			"2026-03-30T17:00",
			"2026-03-20",
			"2026-03-31",
			(datetime.date(2026, 4, 3), datetime.time(9, 0)),
		),
	],
	ids=["autumn timed", "autumn all day", "spring timed"],
)
def test_a_series_straddling_a_clock_change_keeps_every_start_on_its_clock (
	starts: str,
	due: str,
	made: str,
	finished: str,
	wanted: tuple[datetime.date, datetime.time],
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4308`, M17 (a) of the cold review of 2026-10-03: carried in UTC, an hour out for good.

	A start and a deadline either side of a change of the clocks: every later start was carried by
	the time between two instants, so a 09:00 start became 08:00 in autumn and 10:00 in spring, and
	an all-day start landed at 23:00 the day before - for the life of the series.
	"""

	def at (day: str) -> datetime.datetime:
		"""Return noon UTC on a day."""

		return datetime.datetime.fromisoformat(f"{day}T12:00:00+00:00")

	first = _repeating(
		session, recurrence="every week", starts=starts, due=due, timezone=LONDON, now=at(made)
	)
	series = _template(session, first)
	subroutine.domain.tasks.complete(session, first, now=at(finished))
	following = _next_live(session, series)

	assert following.starts_at is not None
	local = following.starts_at.astimezone(zoneinfo.ZoneInfo(LONDON))

	assert (local.date(), local.time()) == wanted, local


@pytest.mark.parametrize(
	("dates", "finished", "wanted"),
	[
		(
			{"starts": "2026-03-27T00:45", "due": "2026-03-27T01:30"},
			["2026-03-27T12:00", "2026-03-28T12:00"],
			{"starts_at": "29 00:45 GMT", "due_at": "29 02:30 BST"},
		),
		(
			{"starts": "2026-03-26T18:00", "due": "2026-03-27T01:30"},
			["2026-03-27T12:00", "2026-03-28T12:00"],
			{"starts_at": "28 18:00 GMT", "due_at": "29 02:30 BST"},
		),
		(
			{"starts": "2026-03-27T00:45", "due": "2026-03-27T01:30", "recurrence_anchor": "completion"},
			["2026-03-28T01:30"],
			{"starts_at": "29 00:45 GMT", "due_at": "29 02:30 BST"},
		),
		(
			{"starts": "2026-03-27T01:45", "due": "2026-03-27T02:30"},
			["2026-03-27T12:00", "2026-03-28T12:00"],
			{"starts_at": "29 00:45 GMT", "due_at": "29 02:30 BST"},
		),
		(
			{"type_key": "event", "starts": "2026-03-27T01:45", "ends": "2026-03-27T02:15", "due": None},
			["2026-03-27T12:00", "2026-03-28T12:00"],
			{"starts_at": "29 02:45 BST", "ends_at": "29 03:15 BST"},
		),
		(
			{"type_key": "event", "starts": "2026-03-27T01:15", "ends": "2026-03-27T01:45", "due": None},
			["2026-03-27T12:00", "2026-03-28T12:00"],
			{"starts_at": "29 02:15 BST", "ends_at": "29 02:45 BST"},
		),
		(
			{"starts": "2026-10-23T00:45", "due": "2026-10-23T01:30"},
			["2026-10-23T12:00", "2026-10-24T12:00"],
			{"starts_at": "25 00:45 BST", "due_at": "25 01:30 BST"},
		),
	],
	ids=[
		"deadline in the gap",
		"start the evening before",
		"from completion",
		"start in the gap",
		"event starting in the gap",
		"event wholly in the gap",
		"the night the clocks go back",
	],
)
def test_on_the_night_the_clocks_go_forward_every_date_keeps_its_distance (
	dates: dict[str, typing.Any],
	finished: list[str],
	wanted: dict[str, str],
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4385`, R2-M8 of the cold review of 2026-10-04 and NEW-D-1, decision `#4386`.

	A slot in the skipped hour is read an hour later, and that hour was carried into every other
	date, so a start followed its deadline; read alone instead, a start in the gap beside a deadline
	just after it did the same, and an event starting in it ended before it started. Every date but
	the slot keeps its elapsed distance from the slot when a time does not exist that night, and its
	own clock otherwise. **And the controls**: an event wholly in the gap, and the night the clocks
	go back, keep their times.
	"""

	def at (moment: str) -> datetime.datetime:
		"""Return a moment written in UTC."""

		return datetime.datetime.fromisoformat(f"{moment}:00+00:00")

	made = datetime.datetime.fromisoformat(f"{dates['starts'][:10]}T00:00:00+00:00")
	first = _repeating(
		session,
		recurrence="every day",
		timezone=LONDON,
		now=made - datetime.timedelta(days=1),
		**dates,
	)
	series = _template(session, first)
	live = first

	for moment in finished:
		subroutine.domain.tasks.complete(session, live, now=at(moment))
		live = _next_live(session, series)

	london = zoneinfo.ZoneInfo(LONDON)
	got = {
		column: getattr(live, column).astimezone(london).strftime("%d %H:%M %Z") for column in wanted
	}

	assert got == wanted, got


def test_a_whole_day_deadline_repeating_from_completion_stays_a_whole_day (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4309`, M17 (b) of the cold review of 2026-10-03: minted at the completion's time of day.

	Flagged all-day and stored at 16:23, the next deadline read as overdue that afternoon. It lands
	on its day's last instant, as every all-day deadline is stored.
	"""

	first = _repeating(
		session,
		recurrence="every 14 days",
		recurrence_anchor="completion",
		due="2026-08-17",
		timezone=LONDON,
	)
	series = _template(session, first)
	subroutine.domain.tasks.complete(
		session, first, now=datetime.datetime(2026, 8, 20, 15, 23, tzinfo=datetime.UTC)
	)
	following = _next_live(session, series)

	assert following.due_at is not None and following.due_is_all_day
	local = following.due_at.astimezone(zoneinfo.ZoneInfo(LONDON))

	assert (local.date(), local.time()) == (
		datetime.date(2026, 9, 3),
		datetime.time(23, 59, 59, 999999),
	), local


@pytest.mark.parametrize("finishing", [False, True], ids=["already finished", "finished with it"])
def test_a_repeat_added_to_finished_work_is_refused_naming_its_status (
	finishing: bool, session: sqlalchemy.orm.Session
) -> None:
	"""`SR#4311`, M17 (d) of the cold review of 2026-10-03, decision `#4312`.

	A repeat added to a finished task made a series with a finished status and no ``completed_at``:
	nothing ever came round, and the task still said it repeated. **And the control**: reopened in
	the same call, it repeats.
	"""

	task = test_schedule._task(session, title="Renew the passport", due="2026-08-31", now=NOW)

	if not finishing:
		subroutine.domain.tasks.complete(session, task, now=NOW)

	finished: dict[str, typing.Any] = {"status_key": "done"} if finishing else {}

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tasks.update(
			session, task, now=NOW, recurrence="every year", **finished
		)

	assert refused.value.errors[0].field == "status", refused.value.errors
	assert subroutine.domain.tasks.series_of(session, task) is None, "a series was made anyway"

	if finishing:
		return

	subroutine.domain.tasks.update(
		session, task, now=NOW, recurrence="every year", status_key="open"
	)
	series = subroutine.domain.tasks.series_of(session, task)

	assert series is not None and series.completed_at is None


@pytest.mark.parametrize(
	"mover", [None, "Asia/Tokyo", "America/Los_Angeles"], ids=["here", "from Tokyo", "from LA"]
)
def test_a_timed_series_moved_from_now_on_by_an_all_day_occurrence_keeps_its_time (
	mover: str | None, session: sqlalchemy.orm.Session
) -> None:
	"""`SR#4001`, M-4 of the cold review of 2026-09-30: the series lost its time of day.

	**And from another zone** (`SR#4310`, M17 (c) of the cold review of 2026-10-03, decision
	`#4312`): the series followed the mover's clock and took their zone - Tuesday 02:00 from Tokyo,
	18:00 from Los Angeles. It keeps its own: Tuesday 10:00 London, from anywhere.

	A weekly Monday series at 10:00, due at 17:00. One occurrence is made all-day for itself
	alone, and then moved to Tuesday from now on. The move was read in the occurrence's shape,
	so the series landed at midnight with its deadline at the day's last microsecond, still
	flagged timed. **The series moves a day and keeps its times.**
	"""

	london = zoneinfo.ZoneInfo(LONDON)
	made = _repeating(
		session,
		recurrence="every monday",
		starts=datetime.datetime(2026, 10, 12, 10, 0, tzinfo=london),
		due=datetime.datetime(2026, 10, 12, 17, 0, tzinfo=london),
		timezone=LONDON,
	)
	series = _template(session, made)

	subroutine.domain.tasks.update(
		session,
		made,
		starts="2026-10-12",
		due="2026-10-12",
		applies_to=subroutine.domain.tasks.THIS_ONE,
		now=NOW,
	)
	session.flush()

	assert made.starts_is_all_day and not series.starts_is_all_day, "the occurrence alone is all-day"

	moved: dict[str, typing.Any] = {} if mover is None else {"timezone": mover}
	subroutine.domain.tasks.update(
		session,
		made,
		starts="2026-10-13",
		due="2026-10-13",
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
		**moved,
	)
	session.flush()

	starts = test_schedule._instant(series.starts_at).astimezone(london)
	due = test_schedule._instant(series.due_at).astimezone(london)

	assert not series.starts_is_all_day and not series.due_is_all_day
	assert (starts.date(), starts.time()) == (datetime.date(2026, 10, 13), datetime.time(10, 0)), starts
	assert (due.date(), due.time()) == (datetime.date(2026, 10, 13), datetime.time(17, 0)), due
	assert series.timezone == LONDON, "the series took the zone the day was read in"


@pytest.mark.parametrize(
	("was_in", "now_in", "first", "to"),
	[
		("America/Los_Angeles", "Asia/Tokyo", datetime.date(2026, 10, 12), datetime.date(2026, 10, 13)),
		("America/Los_Angeles", "Asia/Tokyo", datetime.date(2026, 10, 12), datetime.date(2026, 10, 12)),
		("Europe/London", "Pacific/Auckland", datetime.date(2026, 10, 26), datetime.date(2026, 10, 26)),
		("Europe/London", "Pacific/Auckland", datetime.date(2026, 10, 26), datetime.date(2026, 11, 2)),
	],
	ids=["la-to-tokyo-a-day-on", "la-to-tokyo-same-day", "london-to-auckland-same-day", "london-to-auckland-a-week-on"],
)
def test_a_whole_day_series_moved_into_a_far_zone_lands_on_the_day_given (
	session: sqlalchemy.orm.Session,
	was_in: str,
	now_in: str,
	first: datetime.date,
	to: datetime.date,
) -> None:
	"""`SR#4010`, M-16 of the cold review of 2026-09-30: a day off, and then a duplicate.

	A whole-day series moved from now on into a zone more than twelve hours away was moved by the
	time between two instants, rounded to days - eight hours on rounds to none and sixteen hours
	back to one - so it landed a day early, and completing the occurrence minted the next one on
	its own day. **It lands on the day given, and the next one comes after it.**
	"""

	live = _repeating(
		session,
		title="Bins",
		due=None,
		starts=first,
		recurrence="every monday",
		timezone=was_in,
		workspace_timezone=was_in,
	)
	series = _template(session, live)

	subroutine.domain.tasks.update(
		session,
		live,
		starts=to.isoformat(),
		timezone=now_in,
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	session.flush()

	assert series.timezone == now_in

	landed = test_schedule._instant(series.starts_at).astimezone(zoneinfo.ZoneInfo(now_in))

	assert (landed.date(), landed.time()) == (to, datetime.time(0, 0)), landed

	subroutine.domain.tasks.complete(session, live, now=NOW)
	session.flush()

	following = _next_live(session, series)
	after = test_schedule._instant(following.starts_at).astimezone(
		zoneinfo.ZoneInfo(following.timezone or now_in)
	)

	assert after.date() > to, f"the next occurrence came on {after.date()}, not after {to}"


def test_a_reminder_from_now_on_reaches_the_row_the_calendar_draws (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1247`'s second measurement, and it is why `SR#1211` looked broken.

	The feed draws the *series* for a scheduled repeat, so a reminder stored on the occurrence
	emitted no ``VALARM`` at all — the feature worked perfectly, on a row nobody was looking at.
	"""

	made = _repeating(session, recurrence="every week")
	series = _template(session, made)

	subroutine.domain.tasks.update(
		session, made, reminder="2w", applies_to=subroutine.domain.tasks.FROM_NOW_ON, now=NOW
	)
	session.flush()

	assert made.reminder_minutes == 20160
	assert series.reminder_minutes == 20160, (
		"the reminder is on the occurrence only, so no calendar will ever draw it"
	)


def test_from_now_on_moves_the_grid_rather_than_dragging_it_back (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A series' date and its occurrence's are meant to differ, by however many turns are between.

	Copying one onto the other would pull the whole rule back to whichever row was edited. What
	the two share is the *shape* of the move, which is what "from 3pm from now on" says.

	**And the slot moves with it.** `SR#1248` reads *has this been individually moved* off
	``occurrence_at`` against the row's own date, so a series shifting four hours would
	otherwise read as the occurrence being rescheduled by hand — and the feed would exclude a
	slot nothing had left.
	"""

	made = _repeating(
		session, recurrence="every week", due=None, starts=NOW + datetime.timedelta(days=1)
	)
	series = _template(session, made)

	assert made.starts_at is not None and series.starts_at is not None
	was = made.starts_at
	series_was = series.starts_at

	subroutine.domain.tasks.update(
		session,
		made,
		starts=was + datetime.timedelta(hours=4),
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	session.flush()

	assert made.starts_at == was + datetime.timedelta(hours=4)
	assert series.starts_at == series_was + datetime.timedelta(hours=4)
	assert made.occurrence_at == made.starts_at, (
		"the slot was left behind, so a whole series moving reads as one occurrence moved"
	)


def test_a_series_moved_across_a_change_of_the_clocks_keeps_its_hour (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#3764`: a London 09:00 weekly, moved from 19 to 26 October *from now on*.

	The clocks go back on the 25th, so the move is seven days and an hour by the calendar, and the
	series row - on the summer side, at 12 October - took the hour with it: every later occurrence
	was at 10:00, and completing the 26th minted a second one that day, at 10:00. Carried on the
	clock, the series keeps 09:00 and the next one is 2 November at 09:00.
	"""

	london = zoneinfo.ZoneInfo(LONDON)
	monday = datetime.datetime(2026, 10, 12, 9, 0, tzinfo=london)
	made = _repeating(session, recurrence="every monday", due=None, starts=monday)
	series = _template(session, made)

	subroutine.domain.tasks.complete(session, made, now=monday)
	live = _next_live(session, series)

	assert test_schedule._instant(live.starts_at) == monday + datetime.timedelta(days=7)

	subroutine.domain.tasks.update(
		session,
		live,
		starts=datetime.datetime(2026, 10, 26, 9, 0, tzinfo=london),
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=monday,
	)
	session.flush()

	carried = test_schedule._instant(series.starts_at).astimezone(london)

	assert carried.time() == datetime.time(9, 0), f"the series took the hour: {carried}"
	assert live.occurrence_at == live.starts_at, "the slot was left behind"

	subroutine.domain.tasks.complete(session, live, now=monday)
	after = test_schedule._instant(_next_live(session, series).starts_at)

	assert after == datetime.datetime(2026, 11, 2, 9, 0, tzinfo=london), after.astimezone(london)


def test_a_timed_slot_moved_with_its_series_across_a_clock_change_stays_on_the_grid (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#3975`: the timed half of `SR#3930`, found while building it.

	A London 09:00 weekly anchored on 19 October, its first occurrence done, so the live one is
	the 26th. The series is moved *from now on* to 2 November: the occurrence keeps its own date,
	which is decision `SR#1249` §3, and its slot follows the series' grid (`SR#1340`). The slot was
	moved by the series' delta - fourteen days and the hour the clocks went back - so it landed at
	10:00 on 9 November, where the new rule produces 09:00, and the feed's ``EXDATE`` for it
	excluded nothing. **Moved on the clock**, as the series' own date is (`SR#3764`), it lands on
	the grid.
	"""

	london = zoneinfo.ZoneInfo(LONDON)
	monday = datetime.datetime(2026, 10, 19, 9, 0, tzinfo=london)
	made = _repeating(session, recurrence="every monday", due=None, starts=monday)
	series = _template(session, made)

	subroutine.domain.tasks.complete(session, made, now=monday)
	live = _next_live(session, series)
	waiting = datetime.datetime(2026, 10, 26, 9, 0, tzinfo=london)

	assert test_schedule._instant(live.starts_at) == waiting

	subroutine.domain.tasks.update(
		session,
		series,
		starts=datetime.datetime(2026, 11, 2, 9, 0, tzinfo=london),
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=monday,
	)
	session.flush()

	assert test_schedule._instant(live.starts_at) == waiting, "the occurrence's own date moved"

	slot = test_schedule._instant(live.occurrence_at).astimezone(london)

	assert (slot.date(), slot.time()) == (datetime.date(2026, 11, 9), datetime.time(9, 0)), (
		f"the slot left the series' grid: {slot}"
	)


def test_an_edit_that_moves_no_date_leaves_a_timed_slot_where_it_was (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#3975`: a slot is moved on the clock only where the column it tracks moved.

	Every edit carried *from now on* reaches the slot, a new title included, with that column
	moved by nothing. Read back through the clock, nothing is not always nothing: 01:30 on the
	night the clocks go back happens twice, and a clock time read back is the first of them, an
	hour early. **A move of nothing leaves the slot alone**, as adding a delta of nothing did.
	"""

	second = datetime.datetime(2026, 10, 25, 1, 30, tzinfo=datetime.UTC)
	made = _repeating(session, recurrence="every day", due=None, starts=second)

	# **Set here, since expansion never mints the second 01:30** (`SR#3977`). The delta this
	# replaced could leave a slot there, so a row can hold one.
	made.starts_at = made.occurrence_at = second
	session.flush()

	subroutine.domain.tasks.update(
		session,
		made,
		title="Feed the cat",
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	session.flush()

	assert made.occurrence_at == second, f"a new title moved the slot to {made.occurrence_at}"


def test_lengthening_a_repeating_meeting_leaves_its_slot_where_the_start_is (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1302`, H-1 of the cold review, and the case its own guard could not see.

	``occurrence_at`` is a slot on **one** column — the one
	:func:`subroutine.domain.tasks.grid_field` names — and the code moved it by whichever date
	changed first. A stand-up from 09:00 to 09:15, lengthened *from now on* moves only ``ends_at``, so
	the slot moved by the **end's** delta and landed on nothing.

	**Measured before the fix**: slot 09:00 → 09:15 while the start stayed at 09:00, so
	``_is_on_its_grid`` read a row nobody had touched as one somebody had rescheduled by hand.

	The guard that should have caught this uses a series with **only** a start, where the first
	non-zero delta and the tracked column are the same value — so the defect was invisible to
	the one test written over the line that held it.
	"""

	start = NOW + datetime.timedelta(days=1)
	made = _repeating(
		session,
		recurrence="every day",
		due=None,
		starts=start,
		ends=start + datetime.timedelta(minutes=15),
	)
	series = _template(session, made)

	assert made.occurrence_at == made.starts_at, "the fixture is not on its grid to begin with"
	assert made.ends_at is not None

	subroutine.domain.tasks.update(
		session,
		made,
		ends=made.ends_at + datetime.timedelta(minutes=15),
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	session.flush()

	assert made.starts_at == start, "the start moved, and nobody asked for that"
	assert made.ends_at == start + datetime.timedelta(minutes=30), "the edit did not land"
	assert made.occurrence_at == made.starts_at, (
		"the slot moved by the end's delta, so the feed draws this event twice"
	)
	assert series.occurrence_at is None or series.occurrence_at == series.starts_at


def test_moving_only_the_start_of_a_series_that_has_a_deadline_leaves_its_slot (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1302`, and the commoner half: two dates, one recorded slot.

	``occurrence_at`` follows the deadline when there is one. Moving the *start* is a real edit
	and it is not a move of the grid — the row is still due when the rule says it is due.
	"""

	day = NOW + datetime.timedelta(days=1)
	made = _repeating(
		session,
		recurrence="every week",
		due=day.replace(hour=17, minute=0),
		starts=day.replace(hour=9, minute=0),
	)

	assert made.occurrence_at == made.due_at, "the fixture is not on its deadline's grid"
	assert made.starts_at is not None

	subroutine.domain.tasks.update(
		session,
		made,
		starts=made.starts_at + datetime.timedelta(hours=2),
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	session.flush()

	assert made.starts_at == day.replace(hour=11, minute=0), "the edit did not land"
	assert made.due_at == day.replace(hour=17, minute=0), "the deadline moved, unasked"
	assert made.occurrence_at == made.due_at, (
		"the slot left the grid of a column that did not move"
	)


def test_a_date_that_becomes_a_whole_day_carries_as_a_day_and_not_as_a_delta (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1303`, H-2, and the direction that matters is the one nothing re-derives.

	:func:`~subroutine.domain.tasks._carried` moved a date by a delta and **copied** its
	all-day flag, so a change of *shape* wrote a row claiming to be all-day at 14:00. §6.5
	stores an all-day deadline at the last microsecond of its day, and nothing in the schema
	enforces that — this was the write that broke it.

	**The corrupted row is the template**, which nothing re-derives: ``materialise`` computes
	``template.due_at + shift``, so every future occurrence inherits the broken instant for the
	life of the series. :func:`subroutine.domain.schedule.is_overdue` compares the stored
	instant and nothing else, so *due all day Wednesday* is then late from 15:00 **on**
	Wednesday — the exact case that function's docstring says it asserts directly because the
	implementation that gets it wrong looks identical from the outside.

	Driven on the reviewer's own fixture: a weekly *Pay the rent* due 1 September at 14:00,
	with the occurrence moved to *all day, 2 September*.
	"""

	made = _repeating(
		session,
		title="Pay the rent",
		recurrence="every week",
		due=datetime.datetime(2026, 9, 1, 14, 0, tzinfo=datetime.UTC),
	)
	series = _template(session, made)

	# **Read out before it is asserted on**, for the reason ``test_schedule._instant`` gives one
	# column along: ``assert series.due_is_all_day is False`` narrows the *attribute* to
	# ``Literal[False]`` for the rest of the function, and everything past the assertion that it
	# carried is then reported as unreachable.
	shape_before = series.due_is_all_day

	assert shape_before is False, "the fixture is not a timed deadline"

	subroutine.domain.tasks.update(
		session,
		made,
		due=datetime.date(2026, 9, 2),
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	session.flush()

	zone = zoneinfo.ZoneInfo(LONDON)

	assert made.due_is_all_day and series.due_is_all_day, "the flag did not carry"

	for row, which in ((made, "occurrence"), (series, "template")):
		local = test_schedule._instant(row.due_at).astimezone(zone)

		assert local.date() == datetime.date(2026, 9, 2), f"the {which} landed on {local.date()}"
		assert (local.hour, local.minute, local.microsecond) == (23, 59, 999999), (
			f"the {which} claims to be all day at {local.time()}, which §6.5 says it is not"
		)

	following = subroutine.domain.tasks.materialise(
		session, series, now=NOW, after=test_schedule._instant(made.due_at)
	)

	assert following is not None
	assert test_schedule._instant(following.due_at).astimezone(zone).time() == (
		test_schedule._instant(made.due_at).astimezone(zone).time()
	), "the next occurrence inherited the template's edge, which is the whole cost of this"


def test_a_shape_change_carries_the_same_way_from_either_end_of_the_series (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1304`. One computation, because the two copies of it were wrong in the same way.

	:func:`~subroutine.domain.tasks._carried` and
	:func:`~subroutine.domain.tasks._applied_to_the_series` built the same deltas from the same
	comprehension and used them the same way, so the linked defect was in both. Driving the
	same edit from each end is the property that duplication threatened: a person does not know
	there are two rows, so which one they were holding cannot change the answer.
	"""

	def _driven (edit_the_template: bool) -> tuple[datetime.datetime, datetime.datetime]:
		"""Make an identical series, edit it from one end, and return both stored deadlines."""

		made = _repeating(
			session,
			recurrence="every week",
			due=datetime.datetime(2026, 9, 1, 14, 0, tzinfo=datetime.UTC),
		)
		series = _template(session, made)

		subroutine.domain.tasks.update(
			session,
			series if edit_the_template else made,
			due=datetime.date(2026, 9, 2),
			applies_to=subroutine.domain.tasks.FROM_NOW_ON,
			now=NOW,
		)
		session.flush()

		return (
			test_schedule._instant(made.due_at), test_schedule._instant(series.due_at)
		)

	assert _driven(edit_the_template=False) == _driven(edit_the_template=True), (
		"which row the person was holding changed what the edit did"
	)


def test_a_repeating_task_hands_back_a_row_carrying_the_tags_it_was_captured_with (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1307`, and what makes it bad is that the product says it read the tag.

	``subroutine add "Water the plants #home every monday"`` answers *(read #home)* and hands
	back a row without it. `create` applies tags to the row it built — which **is** the
	template when a rule was given (§6.7) — and ``materialise`` copies twenty-nine columns, of
	which the tag join is not one. The only row carrying the tag is excluded from every
	listing, so ``search "#home"`` finds nothing.

	Driven through the captured line rather than the structured field, because that is the
	grammar `explain capture`, the README and the skill all tell people to use.
	"""

	workspace = test_schedule._workspace(session, timezone=LONDON)
	made, captured = subroutine.domain.tasks.create_from_text(
		session,
		workspace=workspace,
		text="Water the plants #home every monday",
		now=NOW,
	)
	session.flush()

	assert list(captured.tags) == ["home"], "the fixture did not capture the tag it is about"
	assert subroutine.domain.tags.names_on(session, made) == ["home"], (
		"the product said it read the tag and handed back a row without it"
	)


def test_every_occurrence_of_a_series_carries_the_tags_and_not_only_the_first (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1307`, and the reason the fix belongs in ``materialise`` rather than in ``create``.

	Every turn of the wheel is minted by the same call, so a fix that only decorated the first
	occurrence would put the tag back for one week and lose it again — which is worse than
	losing it outright, because the listing would look right until nobody was watching.

	**A tag is a property of the series**, the same argument the reminder beside it makes
	(`SR#1211`): *#home* describes what the task is, not which turn of it you are on.
	"""

	workspace = test_schedule._workspace(session, timezone=LONDON)
	made, _captured = subroutine.domain.tasks.create_from_text(
		session,
		workspace=workspace,
		text="Water the plants #home #indoors every monday",
		now=NOW,
	)
	session.flush()

	series = _template(session, made)

	subroutine.domain.tasks.complete(session, made, now=NOW)
	session.flush()

	following = _next_live(session, series)

	assert following.id != made.id, "the fixture did not advance the series"
	assert subroutine.domain.tags.names_on(session, following) == ["home", "indoors"], (
		"the next occurrence came round without the tags the series carries"
	)


def test_a_tag_taken_off_a_series_is_taken_off_the_occurrence_it_mints (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The other direction, and without it the fix passes by only ever adding.

	``set_on`` replaces rather than merges, which is what §8.3 means by a field on a ``PATCH``.
	An occurrence that accumulated every tag the series had ever carried would be the same
	defect with the sign reversed and would read as correct on the day it was written.
	"""

	workspace = test_schedule._workspace(session, timezone=LONDON)
	made, _captured = subroutine.domain.tasks.create_from_text(
		session,
		workspace=workspace,
		text="Water the plants #home every monday",
		now=NOW,
	)
	session.flush()

	series = _template(session, made)

	# **Every edit to a repeating item says which occurrences it is for** (decision `SR#1249`),
	# and a tag is not one of the four that never ask.
	subroutine.domain.tasks.update(
		session,
		series,
		tags=[],
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	subroutine.domain.tasks.complete(session, made, now=NOW)
	session.flush()

	following = _next_live(session, series)

	assert subroutine.domain.tags.names_on(session, following) == [], (
		"the occurrence carries a tag the series no longer has"
	)


def test_an_edit_to_the_series_reaches_the_row_a_person_is_looking_at (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Decision `SR#1249` §4, which is `SR#1247` arriving from the other side.

	The row in every listing is the occurrence, so a rename that touched only the template
	would leave the old title on screen until the thing next came round.
	"""

	made = _repeating(session, recurrence="every week")
	series = _template(session, made)

	subroutine.domain.tasks.update(
		session,
		series,
		title="What it is really called",
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	session.flush()

	assert made.title == "What it is really called"


def test_a_change_made_to_one_occurrence_is_not_undone_by_a_later_series_edit (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Decision `SR#1249` §3, and the consequence nobody had raised when the model was agreed.

	*Just this one* on a title makes the occurrence disagree with its series. A later *every one
	from now on* has to leave that alone — and it needs no column to know: a field is overridden
	exactly when it differs from the series, and the old value is in hand, being the row before
	the update.
	"""

	made = _repeating(session, recurrence="every week", title="Standup")
	series = _template(session, made)

	subroutine.domain.tasks.update(
		session,
		made,
		title="Standup, short one",
		applies_to=subroutine.domain.tasks.THIS_ONE,
		now=NOW,
	)
	session.flush()

	subroutine.domain.tasks.update(
		session,
		series,
		title="Daily standup",
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	session.flush()

	assert series.title == "Daily standup"
	assert made.title == "Standup, short one", (
		"a change somebody made to this occurrence alone was silently undone"
	)

	# **And an untouched field on the same row still follows**, which is what stops the rule
	# being read as "an overridden row stops listening".
	subroutine.domain.tasks.update(
		session,
		series,
		description="Fifteen minutes, standing up",
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	session.flush()

	assert made.description == "Fifteen minutes, standing up"


def test_completion_is_never_carried_to_the_series (
	session: sqlalchemy.orm.Session,
) -> None:
	"""One of decision `SR#1249` §1's four that never ask, and the one with teeth.

	Completing every future occurrence would end the series, which is what a series *running
	out* already means (`SR#94`) — so carrying it would give a second, unmarked route to
	stopping a repeat, reached by finishing one of them.
	"""

	made = _repeating(session, recurrence="every week")
	series = _template(session, made)
	finished = subroutine.domain.tasks.status_key_in(session, made.workspace_id, "done")

	subroutine.domain.tasks.update(
		session,
		made,
		status_key=finished,
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	session.flush()

	assert series.completed_at is None, "finishing one occurrence stopped the whole repeat"
	assert series.status_id != made.status_id


def test_a_scope_on_something_that_does_not_repeat_is_refused (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Ignored, this would be the inert control this codebase has found three times.

	Somebody who says *from now on* about a one-off has misunderstood something, and the
	cheapest moment to say so is the one where they said it.
	"""

	once = test_schedule._task(session, title="Pay the deposit", now=NOW)
	session.flush()

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tasks.update(
			session, once, title="Anything", applies_to=subroutine.domain.tasks.FROM_NOW_ON, now=NOW
		)

	assert "does not repeat" in refused.value.detail

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.tasks.update(session, once, title="Anything", applies_to="all", now=NOW)


def test_an_occurrence_says_which_repeat_it_came_from (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1247`'s third measurement: the number was reachable and printed by nothing.

	``show`` on the template works and says *the repeat itself*; no output anywhere named which
	number that was, so the only way to reach the row that persists — the one a reminder has to
	be set on — was to guess an integer.
	"""

	made = _repeating(session, recurrence="every week")
	series = _template(session, made)

	shown = subroutine.cli.personal._facts(
		subroutine.cli.personal.Located(
			connection="local",
			workspace="here",
			item=subroutine.views.task(
				made, subroutine.views.Vocabulary.for_tasks(session, None, [made])
			),
		)
	)

	# **The whole phrase, not the digits.** A bare ``str(ref)`` is in the priority cell, in a
	# date and in half the other facts on a small instance, so asserting on it passes against
	# the code this was written for — measured, by removing the line and watching it stay green.
	wanted = (
		f"{subroutine.views.FROM_THE_REPEAT} "
		f"{subroutine.domain.refs.format_ref(series.ref)}"
	)

	assert wanted in shown, (
		f"nothing on the occurrence names the repeat it came from: {shown}"
	)

	# And the other end still says which row it is, so this did not just move the confusion.
	held = subroutine.cli.personal._facts(
		subroutine.cli.personal.Located(
			connection="local",
			workspace="here",
			item=subroutine.views.task(
				series, subroutine.views.Vocabulary.for_tasks(session, None, [series])
			),
		)
	)

	assert subroutine.views.THE_SERIES in held
	assert not any(wanted in fact for fact in held), "the series points at itself"



def test_clearing_a_date_on_a_repeat_is_not_an_internal_error (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1323`. The fix for `SR#1303` guarded both ends of a move and not the far one.

	Clearing a date flips its all-day flag as well as emptying the column, so a *cleared*
	column looks exactly like a **shape change** to
	:func:`~subroutine.domain.tasks._changed_shape` — and there is nothing to reshape onto.
	``_reshaped`` reached ``None.astimezone`` and the call came back as a 500 rather than as
	one of the typed refusals, on ``subroutine plan 1 ""``, ``subroutine_update(plan="")`` and
	``PATCH /v1/tasks/{ref} {"due": null, "applies_to": "from_now_on"}``.

	**Driven for both columns**, because the branch is shared and the flags differ.
	"""

	def _clear_the_deadline (row: subroutine.db.models.work.Task) -> None:
		"""Clear a repeat's deadline for every occurrence from now on."""

		subroutine.domain.tasks.update(
			session,
			row,
			due=None,
			applies_to=subroutine.domain.tasks.FROM_NOW_ON,
			now=NOW,
		)

	def _clear_the_start (row: subroutine.db.models.work.Task) -> None:
		"""Clear a repeat's start for every occurrence from now on."""

		subroutine.domain.tasks.update(
			session,
			row,
			starts=None,
			applies_to=subroutine.domain.tasks.FROM_NOW_ON,
			now=NOW,
		)

	# **A rule naming its own day, 1 September 2026 being a Tuesday** (`SR#4026`): clearing the only
	# date of one that does not is refused now, as making one is, and this is about the clearing.
	for cleared, clear in (("due_at", _clear_the_deadline), ("starts_at", _clear_the_start)):
		live = _repeating(
			session,
			recurrence="every tuesday",
			due=datetime.date(2026, 9, 1) if cleared == "due_at" else None,
			starts=datetime.date(2026, 9, 1) if cleared == "starts_at" else None,
		)
		series = _template(session, live)

		clear(live)
		session.flush()

		assert getattr(live, cleared) is None, f"{cleared} was not cleared on the occurrence"
		assert getattr(series, cleared) is None, f"{cleared} was not cleared on the series"


def test_a_shape_change_made_at_the_template_end_never_leaves_a_flag_without_its_date (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1324`, and the shape it needs is **past the first turn of the wheel**.

	``only_where_unchanged`` holds a column back when the target already differs from the
	source's old value — *somebody moved this one by hand* — and after one completed occurrence
	that is true of the dates by construction, because an occurrence is a whole grid shift
	ahead of its template. The **flag** passed the same test, because both rows still agreed
	about it, so an edit made at the template end skipped ``due_at`` and copied
	``due_is_all_day`` on its own: the live row then claimed to be all-day and was stored at
	09:00, which is the row §6.5 says cannot exist and
	:func:`~subroutine.domain.tasks._reshaped` was written to prevent.

	Every test written for `SR#1303` builds through ``create(recurrence=…)`` and edits the
	**first** occurrence, where the shift is zero and the guard never bites. This one completes
	one turn first, which is the only difference and the whole of it.
	"""

	live = _repeating(
		session,
		recurrence="every week",
		due=datetime.datetime(2026, 8, 16, 9, 0, tzinfo=datetime.UTC),
	)
	series = _template(session, live)

	subroutine.domain.tasks.complete(session, live, now=NOW)
	following = _next_live(session, series)

	assert following.due_at != series.due_at, (
		"the occurrence has not drifted from its template, so this drives the first turn again"
	)

	subroutine.domain.tasks.update(
		session,
		series,
		due=datetime.date(2026, 8, 16),
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	session.flush()
	session.refresh(following)

	assert series.due_is_all_day is True, "the row the edit was made on did not take the shape"

	# **The property, and it is about the pair rather than about either half.** A row whose
	# date was held back keeps the flag that describes it; a row that took the date takes both.
	assert following.due_is_all_day is False, (
		"the flag was copied onto a row whose date was held back, so it claims a shape its "
		f"stored instant does not have: {following.due_at} marked all-day"
	)
	# **The date was held back, so the row still means what it meant.** A whole-day flag over
	# this instant would say the deadline is the end of the 23rd, where the row says 09:00 —
	# and :func:`~subroutine.domain.schedule.is_overdue` reads the instant and nothing else, so
	# the lie is silent until it marks the row late in the middle of its own day.
	assert following.due_at == test_schedule._instant(
		datetime.datetime(2026, 8, 23, 9, 0, tzinfo=datetime.UTC)
	), "the occurrence's own deadline moved, so this is no longer testing the held-back case"


def test_adding_a_deadline_to_a_repeat_moves_its_slot_onto_the_grid_it_is_now_measured_by (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1325`. `SR#1302` fixed the slot for a date that **moved** and not for one added.

	:func:`~subroutine.domain.tasks.grid_field` names the column ``occurrence_at`` is a slot
	on, and a deadline wins over a start — so adding a deadline to a series that had only a
	start moves the slot from one grid to the other. A column set from nothing is in no delta,
	and *was this on the grid* was being asked of the column the slot is on **now**, whose old
	value was ``None`` and is never equal to anything. The slot was left on the old grid,
	:func:`~subroutine.domain.calendars._is_on_its_grid` read a row nobody had touched as
	individually rescheduled, and the feed emitted an ``EXDATE`` for a slot nothing had left
	*and* drew the occurrence a second time.

	**Both directions**, because clearing a deadline is the mirror image and the same branch.
	"""

	live = _repeating(
		session,
		recurrence="every week",
		due=None,
		starts=datetime.datetime(2026, 8, 16, 9, 0, tzinfo=datetime.UTC),
	)

	assert live.occurrence_at == live.starts_at, "this series did not begin on its start's grid"

	subroutine.domain.tasks.update(
		session,
		live,
		due=datetime.date(2026, 8, 18),
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	session.flush()

	assert live.occurrence_at == subroutine.domain.tasks.grid_date(live), (
		f"the slot stayed on the start's grid while the row is measured by its deadline: "
		f"occurrence_at {live.occurrence_at}, grid {subroutine.domain.tasks.grid_date(live)}"
	)

	subroutine.domain.tasks.update(
		session,
		live,
		due=None,
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	session.flush()

	assert live.occurrence_at == subroutine.domain.tasks.grid_date(live), (
		"clearing the deadline left the slot on the grid it no longer has"
	)


def test_a_rule_added_to_a_task_that_already_has_tags_keeps_them_on_every_occurrence (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1326`. `SR#1307`'s fix reached one of the two ways a series comes into being.

	:func:`~subroutine.domain.tasks.materialise` copies the template's tags onto each
	occurrence, which is right — and :func:`~subroutine.domain.tasks.begin_repeating`, the path
	taken when a rule is added to a task that already exists, mints the template by copying
	named columns. A tag is a join and was not one of them, so ``materialise`` faithfully
	copied an empty set.

	``subroutine add "Water the plants #home"`` then ``subroutine update 1 --repeat "every
	monday"`` lost the tag from the second turn of the wheel onwards. Every test written for
	`SR#1307` builds through ``create(recurrence=…)``, where the row the caller's tags landed
	on **is** the template and there is nothing to copy.
	"""

	task = _repeating(session, tags=["home"], due=datetime.date(2026, 8, 31))

	subroutine.domain.tasks.update(session, task, recurrence="every week", now=NOW)
	series = _template(session, task)

	assert [tag.name for tag in subroutine.domain.tags.on(session, series)] == ["home"], (
		"the template the rule minted carries none of the task's tags"
	)

	subroutine.domain.tasks.complete(session, task, now=NOW)
	following = _next_live(session, series)

	assert [tag.name for tag in subroutine.domain.tags.on(session, following)] == ["home"], (
		"the tag survived on the series and not on the occurrence minted from it"
	)


def test_a_repeat_given_to_a_task_keeps_its_end_reminder_and_assigner (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#3924`: the other way a series is made copied a hand-picked list of columns, short three.

	A stand-up from 09:00 to 09:15, with a half-hour reminder and somebody who assigned it, given
	*every day* afterwards lost the end, the reminder and the assigner from the series row and
	from every occurrence after the first. The same series filed in one go kept all three, which
	is where `SR#1235` and `SR#1211` were each fixed.
	"""

	task = _repeating(
		session,
		title="Stand-up",
		due=None,
		starts=datetime.datetime(2026, 8, 17, 9, 0, tzinfo=datetime.UTC),
		ends=datetime.datetime(2026, 8, 17, 9, 15, tzinfo=datetime.UTC),
		reminder="30m",
	)
	assigner = subroutine.domain.users.create(session, username=f"morpheus-{uuid.uuid4().hex[:8]}")
	task.assigned_by_id = assigner.id
	session.flush()

	subroutine.domain.tasks.update(session, task, recurrence="every day", now=NOW)
	series = _template(session, task)
	subroutine.domain.tasks.complete(session, task, now=NOW)
	following = _next_live(session, series)

	for row, called in ((series, "the series"), (following, "the next occurrence")):
		length = test_schedule._instant(row.ends_at) - test_schedule._instant(row.starts_at)

		assert length == datetime.timedelta(minutes=15), f"{called} lost its end"
		assert row.reminder_minutes == 30, f"{called} lost its reminder"
		assert row.assigned_by_id == assigner.id, f"{called} lost who assigned it"


def test_a_repeat_given_to_a_sub_task_files_its_series_under_the_same_parent (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#3924`, `SR#2279`'s disagreement again: the series row named the task's parent and sat
	at the root of the tree, where every rule that reads the tree reads the path.
	"""

	parent = _repeating(session, title="Launch the site")
	project = session.get(subroutine.db.models.project.Project, parent.project_id)

	assert project is not None

	child = subroutine.domain.tasks.create(
		session,
		project=project,
		parent=parent,
		title="Rehearse the launch",
		due=datetime.date(2026, 8, 20),
		now=NOW,
	)

	subroutine.domain.tasks.update(session, child, recurrence="every week", now=NOW)
	series = _template(session, child)

	assert series.parent_task_id == parent.id
	assert (series.depth, series.path.startswith(parent.path)) == (child.depth, True), (
		series.path, parent.path
	)


def test_a_repeat_given_to_a_task_deeper_than_the_default_follows_the_instances_limit (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#3924` files the series row under the task's parent, so the depth limit reaches it, and
	it must be the instance's own (`SR#1560`): a hard-coded ten refused a repeat to a task eleven
	deep on an instance that allows twelve.
	"""

	settings = subroutine.config.Settings(max_hierarchy_depth=12)
	below = _repeating(session, title="Level 0")
	project = session.get(subroutine.db.models.project.Project, below.project_id)

	assert project is not None

	for level in range(1, 12):
		below = subroutine.domain.tasks.create(
			session,
			project=project,
			parent=below,
			title=f"Level {level}",
			due=datetime.date(2026, 8, 20),
			now=NOW,
			settings=settings,
		)

	subroutine.domain.tasks.update(
		session, below, recurrence="every week", now=NOW, settings=settings
	)

	assert below.depth == 11
	assert _template(session, below).depth == 11


def test_an_occurrence_deeper_than_the_default_is_minted_at_its_series_depth (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#3935`, L-4 of the cold review of 2026-09-28: the next occurrence was held to ten.

	A repeat eleven deep, on an instance allowing twelve, was refused its next occurrence when the
	first was finished - *raise max_hierarchy_depth*, of a setting already raised. An occurrence
	sits where its series does, so **its series' depth is allowed** whatever the default.
	"""

	settings = subroutine.config.Settings(max_hierarchy_depth=12)
	below = _repeating(session, title="Level 0")
	project = session.get(subroutine.db.models.project.Project, below.project_id)

	assert project is not None

	for level in range(1, 12):
		below = subroutine.domain.tasks.create(
			session,
			project=project,
			parent=below,
			title=f"Level {level}",
			due=datetime.date(2026, 8, 20),
			now=NOW,
			settings=settings,
		)

	subroutine.domain.tasks.update(
		session, below, recurrence="every week", now=NOW, settings=settings
	)
	series = _template(session, below)

	subroutine.domain.tasks.complete(session, below, now=NOW)

	assert _next_live(session, series).depth == 11


def test_an_edit_refused_for_its_repeat_changes_nothing_else (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#3935`: the title was changed before the repeat was read, and stayed changed.

	``update`` promises everything is validated before anything is assigned, and read the repeat
	as it applied it, after the rest - so an in-process caller that caught the refusal and saved
	kept half an edit. **Read in the validation pass**, where a new series' need for a date is
	asked of the dates the task will have, so giving a date and a repeat together still works.
	"""

	dated = _repeating(session, title="Before", recurrence=None)

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.tasks.update(
			session, dated, title="After", recurrence="every blursday", now=NOW
		)

	assert dated.title == "Before"

	dateless = _repeating(session, title="Before", recurrence=None, due=None)

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.tasks.update(
			session, dateless, title="After", recurrence="every week", now=NOW
		)

	assert dateless.title == "Before"

	subroutine.domain.tasks.update(
		session, dateless, due="2026-09-04", recurrence="every week", now=NOW
	)

	assert dateless.recurrence_template_id is not None


def test_a_series_needing_a_date_is_refused_before_anything_is_written (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#3935`: the series row was numbered, placed and added before its date was asked for.

	A number taken from the workspace's counter is a write, so a caller that caught the refusal
	and saved had used one up. **Asked of the task's own dates first**, which the row copies.
	"""

	dateless = _repeating(session, title="Water the plants", recurrence=None, due=None)
	workspace = subroutine.db.models.identity.Workspace
	counter = sqlalchemy.select(workspace.next_ref_number).where(
		workspace.id == dateless.workspace_id
	)
	before = session.scalar(counter)

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.tasks.begin_repeating(
			session,
			dateless,
			subroutine.domain.recurrence.Repeat(
				rule="FREQ=WEEKLY",
				text="every week",
				anchor=subroutine.domain.tasks.DEFAULT_ANCHOR,
				trigger=subroutine.domain.tasks.DEFAULT_TRIGGER,
			),
			now=NOW,
		)

	assert session.scalar(counter) == before


def test_finishing_a_trashed_occurrence_brings_nothing (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#3935`: completing an occurrence in the trash minted the next one, live.

	So a repeat somebody had thrown away came back on its own, the next time anybody finished the
	row they could no longer see. **Refused, as every change to the trash is, and a skip with it.**
	"""

	live = _repeating(session, recurrence="every week")
	series = _template(session, live)

	subroutine.domain.tasks.delete(session, live, now=NOW)

	for act in (subroutine.domain.tasks.complete, subroutine.domain.tasks.skip):
		with pytest.raises(subroutine.errors.ValidationError):
			act(session, live, now=NOW)

	assert subroutine.domain.occurrences.live_occurrence(session, series) is None


def test_the_last_occurrence_of_a_series_in_the_trash_can_still_be_finished (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4026`, L-1 (2) of the cold review of 2026-09-30: a trashed series blocked its last one.

	An older build put a series in the trash, and with nothing to come, finishing its occurrence
	went on to complete the series, which the trash refuses: so the one row the person can see
	could be neither done nor skipped. **Finished, and the series left where it is.**
	"""

	live = _repeating(session, recurrence="FREQ=DAILY;COUNT=1")
	series = _template(session, live)
	series.deleted_at = NOW
	session.flush()

	subroutine.domain.tasks.complete(session, live, now=NOW)

	assert live.completed_at is not None, live
	assert series.completed_at is None and series.deleted_at is not None, series


def test_a_stopped_series_is_refused_a_delete_as_the_record_of_what_it_ran (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4026`, L-1 (3) of the cold review of 2026-09-30: it was told to stop what had stopped."""

	live = _repeating(session, recurrence="every week")
	series = _template(session, live)
	subroutine.domain.tasks.complete(session, series, now=NOW)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tasks.delete(session, series, now=NOW)

	assert "has stopped" in refused.value.detail, refused.value.detail
	assert "to stop it" not in (refused.value.hint or ""), refused.value.hint


@pytest.mark.parametrize(("recurrence", "refused"), [("every 14 days", True), ("every monday", False)])
def test_a_series_is_not_left_with_no_date_to_repeat_from (
	session: sqlalchemy.orm.Session, recurrence: str, refused: bool
) -> None:
	"""`SR#4026`, L-3 (1) of the cold review of 2026-09-30: its only date was cleared from now on.

	*Every 14 days* says how often and not when, and clearing its deadline from now on was accepted,
	so every completion after it was refused until it was dated again. **Refused at the edit**, as
	such a series is refused being made; a rule naming its own day, *every monday*, needs none.
	"""

	live = _repeating(session, recurrence=recurrence, due=datetime.date(2026, 10, 5))
	clearing = functools.partial(
		subroutine.domain.tasks.update,
		session,
		live,
		due=None,
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)

	if refused:
		with pytest.raises(subroutine.errors.ValidationError) as said:
			clearing()

		assert "needs a date" in said.value.detail, said.value.detail

	else:
		clearing()


def test_an_all_day_series_moved_across_a_clock_change_stays_on_its_day (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#3930`: a whole day was carried from one row of a series to the other as hours.

	A London all-day series on Mondays, its live occurrence moved *from now on* from 19 October
	to 2 November, carried the fortnight to the series as 336 hours - and the October clock change
	lies between, so the series landed at 23:00 on Sunday 1 November and the occurrence after
	the moved one was minted for that Monday again. **A timed series was already right**, and
	stays so: this is the whole-day branch alone.
	"""

	live = _repeating(
		session,
		title="Put the bins out",
		due=None,
		starts=datetime.date(2026, 10, 19),
		recurrence="every monday",
	)
	series = _template(session, live)

	subroutine.domain.tasks.update(
		session,
		live,
		starts="2026-11-02",
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	session.flush()
	london = zoneinfo.ZoneInfo(LONDON)
	moved = test_schedule._instant(series.starts_at).astimezone(london)

	assert (moved.date(), moved.time()) == (datetime.date(2026, 11, 2), datetime.time(0, 0)), moved

	subroutine.domain.tasks.complete(session, live, now=NOW)
	following = test_schedule._instant(_next_live(session, series).starts_at).astimezone(london)

	assert (following.date(), following.time()) == (datetime.date(2026, 11, 9), datetime.time(0, 0)), (
		following
	)


def test_an_all_day_series_moved_and_given_a_new_zone_at_once_stays_on_its_day (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#3930` where the move carries a zone too, which is where the two rows part company.

	**The series is relabelled after its dates are moved, and the occurrence before.** So the
	series' day is read in the zone it was written in, and the occurrence's slot in the zone it
	is in now; either read in the other's zone lands a day out, 13 hours from London.
	"""

	live = _repeating(
		session,
		title="Put the bins out",
		due=None,
		starts=datetime.date(2026, 10, 19),
		recurrence="every monday",
	)
	series = _template(session, live)

	subroutine.domain.tasks.update(
		session,
		live,
		starts="2026-11-02",
		timezone="Pacific/Auckland",
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
	)
	session.flush()
	auckland = zoneinfo.ZoneInfo("Pacific/Auckland")

	def day (value: datetime.datetime | None) -> tuple[datetime.date, datetime.time]:
		"""Return a stored instant as the day and time it is in Auckland."""

		local = test_schedule._instant(value).astimezone(auckland)

		return local.date(), local.time()

	monday = (datetime.date(2026, 11, 2), datetime.time(0, 0))

	assert (series.timezone, day(series.starts_at)) == ("Pacific/Auckland", monday)
	assert day(live.occurrence_at) == monday, "the moved occurrence's slot is off its day"

	subroutine.domain.tasks.complete(session, live, now=NOW)

	assert day(_next_live(session, series).starts_at) == (datetime.date(2026, 11, 9), datetime.time(0, 0))


def test_a_move_of_no_days_leaves_the_other_row_as_it_found_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#3930` moves a whole day by days, and **a move of no days is left alone**, not snapped.

	A series written before the edges were settled can hold its deadline at the end of the UTC
	day while it is labelled London - an hour into the next London day. Carrying a sub-day
	correction onto it as *zero days, snapped to the edge* reads that as the next day and moves
	the series a day, where carrying nothing leaves it where it was.
	"""

	instance = _repeating(
		session, title="Pay council tax", recurrence="every month on the 1st", due=None
	)
	template = _template(session, instance)
	day = test_schedule._instant(template.due_at).astimezone(zoneinfo.ZoneInfo(LONDON)).date()
	template.due_at = datetime.datetime.combine(
		day, datetime.time(23, 59, 59, 999_999), tzinfo=datetime.UTC
	)
	held = template.due_at
	behind = datetime.timedelta(microseconds=999_999)
	instance.due_at = test_schedule._instant(instance.due_at) - behind
	session.flush()

	subroutine.domain.tasks.update(
		session,
		instance,
		due=day.isoformat(),
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
		timezone=LONDON,
	)
	session.flush()

	assert template.due_at == held, f"a move of no days moved the series to {template.due_at}"


def _renders (task: subroutine.db.models.work.Task) -> datetime.date:
	"""Return the day a row's deadline falls on in the zone the row itself names."""

	assert task.due_at is not None and task.timezone is not None

	return task.due_at.astimezone(zoneinfo.ZoneInfo(task.timezone)).date()


@pytest.mark.parametrize("edited", ["series", "occurrence"])
def test_a_zone_carried_across_a_series_keeps_both_rows_on_their_day (
	session: sqlalchemy.orm.Session, edited: str
) -> None:
	"""`SR#1293`: re-dating one row of a series in another zone must not move the other row's day.

	The zone was copied onto the other row as a value while its date moved by whole days, so a
	deadline written in UTC and re-dated *from now on* in London left the other row labelled
	London and still stored at the UTC edge of its day, which rendered on the day after. And the
	slot stayed an hour off its date, which the calendar reads as an occurrence moved by hand.

	**Both directions**, because the series and the live occurrence each carry an edit to the
	other, and the first report came from the series side.
	"""

	instance = _repeating(
		session,
		title="Pay council tax",
		recurrence="every month on the 1st",
		due=None,
		timezone="UTC",
	)
	template = _template(session, instance)

	subroutine.domain.tasks.update(
		session,
		template if edited == "series" else instance,
		due="2026-09-01",
		applies_to=subroutine.domain.tasks.FROM_NOW_ON,
		now=NOW,
		timezone=LONDON,
	)
	session.flush()

	assert (template.timezone, instance.timezone) == (LONDON, LONDON)
	assert _renders(template) == datetime.date(2026, 9, 1), template.due_at
	assert _renders(instance) == datetime.date(2026, 9, 1), (
		f"the occurrence is labelled {instance.timezone} and stored at {instance.due_at}"
	)
	assert instance.occurrence_at == instance.due_at, (
		f"the slot {instance.occurrence_at} was left off the date {instance.due_at}, so the "
		f"occurrence reads as moved by hand"
	)


def test_repairing_one_occurrence_in_another_zone_leaves_it_on_its_slot (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1293`'s second cost: the repair a person reaches for must not strand the slot.

	Setting just this one occurrence to its own day in a new zone put its deadline right and left
	``occurrence_at`` at the old zone's edge, an hour away - and since whole-day dates carry no
	sub-day move, nothing could put it back. The slot is re-expressed with the date it is on.
	"""

	instance = _repeating(
		session,
		title="Pay council tax",
		recurrence="every month on the 1st",
		due=None,
		timezone="UTC",
	)

	subroutine.domain.tasks.update(
		session,
		instance,
		due="2026-09-01",
		applies_to=subroutine.domain.tasks.THIS_ONE,
		now=NOW,
		timezone=LONDON,
	)
	session.flush()

	assert _renders(instance) == datetime.date(2026, 9, 1), instance.due_at
	assert instance.occurrence_at == instance.due_at, (
		f"the slot {instance.occurrence_at} stayed at the old zone's edge"
	)
