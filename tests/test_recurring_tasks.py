"""A task that repeats: the template, the one live instance, and what advances it.

`#94`, and the half of it that touches the database — :mod:`tests.test_recurrence` covers the
grammar and the dates, which are pure. This is the template/instance machinery §6.7 designed
and decision `#915` sharpened.

**The shape to hold while reading**: a rule-bearing row is a *template*, it is not worked on
and it appears in no listing; exactly one *instance* is live at a time; and finishing an
instance is what brings the next one into being.
"""

import datetime
import typing
import zoneinfo

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.scoping
import subroutine.domain.tasks
import subroutine.errors
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

