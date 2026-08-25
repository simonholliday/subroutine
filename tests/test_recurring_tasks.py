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
import threading
import typing
import uuid
import zoneinfo

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.scoping
import subroutine.domain.tasks
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

	assert template.snoozed_until is not None, "the fixture did not snooze anything"

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
		first, subroutine.views.Vocabulary.for_tasks(session, [first])
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
		first, subroutine.views.Vocabulary.for_tasks(session, [first])
	)

	assert shown.recurrence_rule == "FREQ=MONTHLY;BYMONTHDAY=30", "the state this moves off"

	subroutine.domain.tasks.update(session, first, recurrence=None, now=NOW)

	stopped = subroutine.views.task(
		first, subroutine.views.Vocabulary.for_tasks(session, [first])
	)

	assert stopped.recurrence_rule is None
	assert stopped.recurrence_anchor is None
	assert stopped.recurrence_text is None

	# **The backlink survives**, deliberately: *this came from that series* stays true after it
	# ends, and it is how anybody reaches what happened before.
	assert stopped.recurrence_template_ref is not None


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
		second, subroutine.views.Vocabulary.for_tasks(session, [second])
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
