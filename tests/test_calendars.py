"""Tests for calendar feeds: the credential, what a feed shows, and the bytes it produces.

`SR#916`, against decision `SR#972`. Three things are worth knowing before adding to this file.

**The renderer is driven directly**, because it is a pure function from data to a string —
the same reason `markdown.js` was written that way. A hostile or awkward value can be fed
straight through the entry point and the exact bytes a calendar application would receive can
be asserted on, with no HTTP and no database in between.

**The window is asserted with dates either side of it**, never with one inside. A fixture
whose every row is in range cannot tell a window from no window at all.

**The clock is injected.** A fixture holding a fixed instant against the wall clock is a test
that passes in the morning and fails in the evening, which this repository shipped and pushed
on 2026-08-09 (`SR#737`).
"""

import datetime
import typing
import uuid
import zoneinfo

import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.api.calendars
import subroutine.auth
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.calendars
import subroutine.domain.icalendar
import subroutine.domain.projects
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.permissions
import subroutine.views
import test_api_tasks

NOW = datetime.datetime(2026, 8, 17, 9, 0, tzinfo=datetime.UTC)


def _world (
	session: sqlalchemy.orm.Session,
) -> tuple[subroutine.db.models.identity.Workspace, subroutine.db.models.identity.User]:
	"""Create a seeded workspace and return it with its founder."""

	owner = subroutine.domain.users.create(
		session, username=f"owner-{uuid.uuid4().hex[:8]}"
	)
	workspace = subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Test workspace", owner=owner
	)

	return workspace, owner


def _project (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	**kwargs: typing.Any,
) -> subroutine.db.models.project.Project:
	"""Create a project to file tasks in."""

	kwargs.setdefault("key", f"p{uuid.uuid4().hex[:10]}")
	kwargs.setdefault("title", "Test project")

	return subroutine.domain.projects.create(session, workspace_id=workspace.id, **kwargs)


def _task (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	owner: subroutine.db.models.identity.User,
	**kwargs: typing.Any,
) -> subroutine.db.models.work.Task:
	"""File one task, with whatever dates the caller cares about."""

	kwargs.setdefault("title", "A task")

	return subroutine.domain.tasks.create(
		session,
		project=project,
		actor=subroutine.domain.authentication.Principal(user=owner),
		**kwargs,
	)


def _feed (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	owner: subroutine.db.models.identity.User,
	**kwargs: typing.Any,
) -> tuple[subroutine.db.models.identity.CalendarFeed, subroutine.auth.IssuedToken]:
	"""Mint a feed as somebody at a terminal, which no check narrows (§12.1a).

	**The owner is the actor and there is no way to say otherwise**, which is why this takes
	one argument where it used to take two: a feed renders with its owner's sight, so an owner
	a caller could name would be somebody else's work handed to whoever asked.
	"""

	kwargs.setdefault("title", "My calendar")

	return subroutine.domain.calendars.create(
		session,
		subroutine.domain.authentication.Principal(user=owner),
		workspace_id=workspace.id,
		now=NOW,
		**kwargs,
	)


def test_a_feed_url_resolves_to_its_feed_and_records_the_poll (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The round trip, and `last_polled_at` is what makes a stale feed noticeable (§20.3)."""

	workspace, owner = _world(session)
	feed, minted = _feed(session, workspace, owner)

	assert feed.last_polled_at is None, "nothing has polled it yet"

	found = subroutine.domain.calendars.resolve(
		session, minted.value.get_secret_value(), now=NOW
	)

	assert found.id == feed.id
	assert found.last_polled_at == NOW

	# **That the secret is refused as a bearer token is not asserted here**, deliberately:
	# `tests/test_api_authentication.py` drives *every* kind this program mints through the
	# real refusal, so a copy of it for this one kind would be the weaker half of a check that
	# already exists — and it would put an `api` import into a domain test to say it.
	assert minted.value.get_secret_value().startswith(
		f"{subroutine.auth.TOKEN_SCHEME}_{subroutine.auth.CALENDAR_KIND}_"
	), "the secret does not carry the word that lets it be refused by name"


@pytest.mark.parametrize(
	"spoil",
	[
		pytest.param(lambda secret: secret[:-1] + ("a" if secret[-1] != "a" else "b"),
			id="a wrong secret"),
		pytest.param(lambda secret: secret.replace("sr_cal_", "sr_", 1), id="the wrong kind"),
		pytest.param(lambda secret: "nonsense", id="nonsense"),
	],
)
def test_a_url_that_names_no_live_feed_is_refused_the_same_way (
	session: sqlalchemy.orm.Session, spoil: typing.Callable[[str], str]
) -> None:
	"""**Every refusal is the same refusal**, so nothing says when half a guess was right.

	And it is a 404 rather than a 401: the credential *is* the address, so there is no header
	to correct and no challenge a calendar client could answer.
	"""

	workspace, owner = _world(session)
	_row, minted = _feed(session, workspace, owner)

	with pytest.raises(subroutine.errors.NotFound):
		subroutine.domain.calendars.resolve(
			session, spoil(minted.value.get_secret_value()), now=NOW
		)


def test_a_revoked_or_expired_feed_stops_working_and_a_reset_moves_the_url (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The three ways a URL stops resolving, and the one that keeps the feed (§20.3)."""

	workspace, owner = _world(session)
	feed, minted = _feed(session, workspace, owner)
	first = minted.value.get_secret_value()

	# **A reset keeps the row and moves the secret**, which is what makes it different from
	# revoking and creating another: the scope, the audience and `last_polled_at` survive.
	again = subroutine.domain.calendars.reset(session, feed, enabled=True)
	session.flush()

	with pytest.raises(subroutine.errors.NotFound):
		subroutine.domain.calendars.resolve(session, first, now=NOW)

	assert subroutine.domain.calendars.resolve(
		session, again.value.get_secret_value(), now=NOW
	).id == feed.id

	subroutine.domain.calendars.revoke(session, feed, now=NOW)
	session.flush()

	with pytest.raises(subroutine.errors.NotFound):
		subroutine.domain.calendars.resolve(
			session, again.value.get_secret_value(), now=NOW
		)


def test_a_feed_stops_working_when_its_owner_does (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#475`'s rule, checked on every poll rather than at creation.

	A feed borrows one person's sight. When that account is marked as having left there is no
	visibility rule left to apply, and falling back to *something* is exactly how a leaked URL
	survives somebody being offboarded — which nobody would notice, because a feed has no
	login to audit.
	"""

	workspace, owner = _world(session)
	_row, minted = _feed(session, workspace, owner)
	secret = minted.value.get_secret_value()

	assert subroutine.domain.calendars.resolve(session, secret, now=NOW)

	owner.is_active = False
	session.flush()

	with pytest.raises(subroutine.errors.NotFound):
		subroutine.domain.calendars.resolve(session, secret, now=NOW)


def test_a_feed_stops_working_when_its_workspace_is_deleted (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The mirror of the test above, for the container rather than the person — `SR#704`.

	**A feed is the one credential that reaches tasks without going through
	``workspaces.readable``**, which is where every other surface stops at the trash. It holds
	a ``workspace_id`` and hands it to ``readable_tasks``, so the exclusion that covers the
	whole application by construction does not cover this.

	It could not be wrong until a workspace could be deleted, and it was wrong the moment one
	could: the first working version of `SR#704` shipped the delete and left this poll
	answering with the deleted workspace's whole calendar, over a URL nobody has to log in to.
	"""

	workspace, owner = _world(session)
	_row, minted = _feed(session, workspace, owner)
	secret = minted.value.get_secret_value()

	assert subroutine.domain.calendars.resolve(session, secret, now=NOW)

	# A second one, because the last live workspace cannot be deleted — and going through the
	# real service rather than setting the column is what joins the two halves of this.
	_world(session)
	subroutine.domain.workspaces.delete(session, workspace)

	with pytest.raises(subroutine.errors.NotFound):
		subroutine.domain.calendars.resolve(session, secret, now=NOW)


def test_a_bounded_credential_cannot_mint_a_feed (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#829`'s test asked of a fourth credential — `SR#837`, and decision `SR#972`.

	A feed renders with its **owner's** visibility rather than with the narrowing on whatever
	minted it, so a credential scoped to one project could mint a URL reading the whole
	workspace. That is the escalation `SR#829` found on `POST /v1/login-links`, one credential
	kind along, and the answer here is the same blunt one.
	"""

	workspace, owner = _world(session)
	token = subroutine.db.models.identity.ApiToken(
		user_id=owner.id, title="A narrow token", token_prefix="0" * 8,
		token_hash="0" * 64, scopes=["task:read"],
	)
	session.add(token)
	session.flush()

	narrowed = subroutine.domain.authentication.Principal(user=owner, token=token)

	assert narrowed.narrows, "the fixture is not narrowed, so this proves nothing"

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		subroutine.domain.calendars.create(
			session, narrowed, workspace_id=workspace.id, title="Wider than me", now=NOW,
		)

	assert "bounded" in str(refused.value)

	# **And the unnarrowed case works**, which is what stops this being a rule that refuses
	# everything — the shape a refusal test passes for the wrong reason in.
	wide = subroutine.db.models.identity.ApiToken(
		user_id=owner.id, title="An ordinary token", token_prefix="1" * 8,
		token_hash="1" * 64, scopes=[],
	)
	session.add(wide)
	session.flush()

	made, _minted = subroutine.domain.calendars.create(
		session,
		subroutine.domain.authentication.Principal(user=owner, token=wide),
		workspace_id=workspace.id, title="Fine", now=NOW,
	)

	assert made.id is not None


def test_a_feed_may_not_outlive_the_credential_that_asked_for_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#356`'s rule, and only in the amplifying direction.

	A feed's expiry is *optional*, so the common mistake is a permanent URL minted by a token
	that stops working in a month — which is the whole of what this refuses. A credential that
	outlives the feed is not being widened and passes.
	"""

	workspace, owner = _world(session)
	token = subroutine.db.models.identity.ApiToken(
		user_id=owner.id, title="A short token", token_prefix="2" * 8,
		token_hash="2" * 64, scopes=[],
		expires_at=NOW + datetime.timedelta(days=30),
	)
	session.add(token)
	session.flush()

	actor = subroutine.domain.authentication.Principal(user=owner, token=token)

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		subroutine.domain.calendars.create(
			session, actor, workspace_id=workspace.id, title="For ever", now=NOW,
		)

	assert "outlive" in str(refused.value)

	made, _minted = subroutine.domain.calendars.create(
		session, actor, workspace_id=workspace.id, title="Bounded",
		expires_at=NOW + datetime.timedelta(days=7), now=NOW,
	)

	assert made.expires_at is not None


def test_a_feed_shows_the_recent_past_and_stops_at_the_window (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Simon's decision of 2026-08-17, and decision `SR#972` §4.

	**Rows either side of both edges**, because a fixture whose every date is in range cannot
	tell a window from no window — and the past edge is the one he asked for, so a test that
	only proved the future was bounded would say nothing about it.
	"""

	workspace, owner = _world(session)
	project = _project(session, workspace)
	inside = subroutine.domain.calendars.PAST_DAYS
	beyond = subroutine.domain.calendars.FUTURE_DAYS

	wanted = {
		"three days ago": NOW - datetime.timedelta(days=3),
		"tomorrow": NOW + datetime.timedelta(days=1),
		"almost a year": NOW + datetime.timedelta(days=beyond - 1),
	}
	unwanted = {
		"a fortnight ago": NOW - datetime.timedelta(days=inside * 2),
		"past the horizon": NOW + datetime.timedelta(days=beyond + 1),
	}

	for label, when in {**wanted, **unwanted}.items():
		_task(session, project, owner, title=label, due=when)

	session.flush()

	feed, _minted = _feed(session, workspace, owner)
	shown = {
		one.task.title for one in subroutine.domain.calendars.occasions(session, feed, now=NOW)
	}

	assert set(wanted) <= shown, f"the window dropped something inside it: {shown}"
	assert not (set(unwanted) & shown), f"the window let something through: {shown}"


def test_a_feed_keeps_work_somebody_has_finished (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The other half of Simon's decision, and the reason the two were one question.

	Most clients drop an event the moment a feed stops sending it, so excluding finished work
	would mean **ticking a meeting off deletes it from your calendar's history** — a to-do
	list's rule producing a result no calendar has.
	"""

	workspace, owner = _world(session)
	project = _project(session, workspace)
	actor = subroutine.domain.authentication.Principal(user=owner)
	done = _task(
		session, project, owner, title="A meeting that happened",
		due=NOW - datetime.timedelta(days=2),
	)

	subroutine.domain.tasks.complete(session, done, actor=actor)
	session.flush()

	feed, _minted = _feed(session, workspace, owner)
	shown = {
		one.task.title for one in subroutine.domain.calendars.occasions(session, feed, now=NOW)
	}

	assert "A meeting that happened" in shown, (
		"finishing something removed it from the calendar it was already on"
	)


def test_a_repeating_series_is_one_event_on_the_calendar (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A grid and a copy of one of its own occurrences is two events (`SR#1067`).

	A `schedule` series exists as a template carrying the rule **and** as one live instance
	(decision `SR#972` §1). Both were emitted: the template as an `RRULE` covering every
	occurrence, and the instance again as a standalone event on a date the grid already
	carries. The changelog says a repeating item arrives "as a repeating event rather than as
	several hundred copies"; it arrived as a repeating event plus a copy.

	**No test put a series through a feed at all**, which is the reason this shipped — every
	other test in this file files a one-off.
	"""

	workspace, owner = _world(session)
	project = _project(session, workspace)
	actor = subroutine.domain.authentication.Principal(user=owner)

	subroutine.domain.tasks.create(
		session,
		project=project,
		actor=actor,
		title="Standup",
		starts=NOW + datetime.timedelta(days=1),
		recurrence="every monday",
	)
	session.flush()

	feed, _minted = _feed(session, workspace, owner)
	shown = subroutine.domain.calendars.occasions(session, feed, now=NOW)
	standups = [one for one in shown if one.task.title == "Standup"]

	assert len(standups) == 1, (
		f"one weekly series produced {len(standups)} events: "
		f"{[(one.field, getattr(one.task, one.field), one.rule) for one in standups]}"
	)

	assert standups[0].rule, (
		"the one event kept is the standalone occurrence rather than the rule, so a client "
		"sees this Monday and no others"
	)


def test_a_repeat_that_names_its_own_day_reaches_the_calendar (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1208`. The rule was right, the slot was computed, and the item had no date at all.

	**Measured on a fresh 0.8.1 instance** before filing: *"Pay the council tax every month on
	the 1st"* stored a template with `FREQ=MONTHLY;BYMONTHDAY=1` and an occurrence whose
	``occurrence_at`` was the first of next month and whose ``due_at`` and ``starts_at`` were
	both null. `SR#94` lets such a rule anchor itself on the moment it was filed rather than
	refusing it for not saying when — which it does say — and nothing then wrote that day onto
	the occurrence, so `_occasions_of` gated both its branches on a column that was never set.

	**The code already assumed this state was unreachable**: `_is_on_its_grid` compares
	``occurrence_at`` against ``due_at or starts_at``, which cannot hold while one side is null.

	**A whole day, not the minute it was typed.** Anchoring on the filing instant gave the slot
	that instant's time of day, so a client would have drawn a one-minute appointment at
	whatever o'clock somebody happened to be at their desk.
	"""

	workspace, owner = _world(session)
	project = _project(session, workspace)
	actor = subroutine.domain.authentication.Principal(user=owner)

	subroutine.domain.tasks.create(
		session,
		project=project,
		actor=actor,
		title="Pay the council tax",
		recurrence="every month on the 1st",
	)
	session.flush()

	feed, _minted = _feed(session, workspace, owner)
	bills = [
		one
		for one in subroutine.domain.calendars.occasions(session, feed, now=NOW)
		if one.task.title == "Pay the council tax"
	]

	assert bills, (
		"a repeating deadline nobody gave a second date to reaches no calendar at all, which "
		"is every repeat written the way somebody would actually type one"
	)

	# **One, not two.** The rule and its first occurrence are the same day, and the whole of
	# `SR#1067` is that a calendar showing both is showing one thing twice — which is only
	# decidable once the occurrence *has* a date to compare.
	assert len(bills) == 1, (
		f"one monthly series produced {len(bills)} events: "
		f"{[(one.field, getattr(one.task, one.field), one.rule) for one in bills]}"
	)

	[shown] = bills

	assert shown.field == "due_at", (
		f"a bill is a deadline and this is on {shown.field!r} — the calendar prefixes differ, "
		f"so the wrong one writes 'Due:' onto things that are not due"
	)
	assert shown.task.due_is_all_day, (
		"the occurrence is a timed appointment rather than a day, so a client draws a "
		"one-minute event at whatever o'clock it was filed"
	)
	assert shown.rule, "the event kept is the standalone occurrence rather than the series"


def test_a_reminder_rides_on_the_event_so_it_repeats_with_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1211`, Simon: *"for a birthday, I might enable a reminder 2 weeks before the event,
	which should happen every year."*

	**A `VALARM` hanging off the `VEVENT` is the whole mechanism**, and it is why this needed
	neither a scheduler nor `SR#577`'s open questions answered. The client expands the alarm
	against the `RRULE` itself, so nothing here computes a date per occurrence or stores one —
	and the reminding happens in the reader's own calendar application, which is already running
	and already knows how they want to be told.

	**Relative rather than absolute**, which `SR#577` had already concluded is right on a
	different argument — *"one hour before" survives the deadline moving* — and which turns out
	to be the same property that makes a reminder follow a repeat.
	"""

	workspace, owner = _world(session)
	project = _project(session, workspace)
	actor = subroutine.domain.authentication.Principal(user=owner)

	subroutine.domain.tasks.create(
		session,
		project=project,
		actor=actor,
		title="Anna's birthday",
		starts=NOW + datetime.timedelta(days=30),
		starts_is_all_day=True,
		recurrence="every year",
		reminder="2w",
	)
	session.flush()

	feed, _minted = _feed(session, workspace, owner)
	rendered = subroutine.domain.icalendar.render(
		subroutine.domain.calendars.occasions(session, feed, now=NOW),
		name="Work",
		instance_id=uuid.uuid4(),
		now=NOW,
	)

	assert "BEGIN:VALARM" in rendered, (
		f"a reminder somebody set reaches no calendar: {rendered}"
	)

	# **`P14D`, not `PT20160M`.** Both name the same instant and only one of them reads as *two
	# weeks before* when a client shows it to somebody.
	assert "TRIGGER:-P14D" in rendered, (
		f"the reminder is not two weeks before, or is written in a unit nobody reads: "
		f"{[line for line in rendered.split(chr(13) + chr(10)) if 'TRIGGER' in line]}"
	)

	# **Inside the `VEVENT` that carries the rule**, which is the entire reason this repeats:
	# an alarm outside it, or on an event without the `RRULE`, would fire once.
	body = rendered.split("BEGIN:VEVENT", 1)[1]
	alarm = body.index("BEGIN:VALARM")

	assert body.index("RRULE:") < alarm < body.index("END:VEVENT"), (
		f"the alarm is not inside the repeating event, so it would fire once: {rendered}"
	)

	# **RFC 5545 §3.6.6 requires both for a DISPLAY alarm**, and a client's response to a
	# malformed one is its own business rather than something worth predicting.
	assert "ACTION:DISPLAY" in rendered and "DESCRIPTION:Anna" in rendered, rendered


def test_an_item_with_no_reminder_carries_no_alarm (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The other half, so the test above cannot pass by every event carrying an alarm.

	§6.16's rule read from the rendering end: a feed that attached an alarm to everything would
	be a reminder nobody asked for on every task they have, which is worse than none at all.
	"""

	workspace, owner = _world(session)
	project = _project(session, workspace)
	actor = subroutine.domain.authentication.Principal(user=owner)

	subroutine.domain.tasks.create(
		session, project=project, actor=actor, title="Ordinary thing",
		starts=NOW + datetime.timedelta(days=2),
	)
	session.flush()

	feed, _minted = _feed(session, workspace, owner)
	rendered = subroutine.domain.icalendar.render(
		subroutine.domain.calendars.occasions(session, feed, now=NOW),
		name="Work",
		instance_id=uuid.uuid4(),
		now=NOW,
	)

	assert "Ordinary thing" in rendered, "the fixture produced no event to check"
	assert "VALARM" not in rendered, (
		f"an item nobody set a reminder on carries one anyway: {rendered}"
	)


def test_an_occurrence_somebody_moved_is_still_on_the_calendar (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Skipped only when it duplicates, which is not the same as *belongs to a series*.

	The obvious fix for `SR#1067` — drop every instance whose template carries a rule — puts
	the calendar back to showing the date an occurrence *was going to be* on, which is worse
	than the duplicate it removes: a person who moved this week's standup would see the old
	time and nothing else.

	``occurrence_at`` is the slot the series minted the row for, and rescheduling changes the
	date without touching it, so the two parting company is exactly *this has been moved*.
	"""

	workspace, owner = _world(session)
	project = _project(session, workspace)
	actor = subroutine.domain.authentication.Principal(user=owner)

	created = subroutine.domain.tasks.create(
		session,
		project=project,
		actor=actor,
		title="Standup",
		starts=NOW + datetime.timedelta(days=1),
		recurrence="every monday",
	)
	session.flush()

	moved = NOW + datetime.timedelta(days=3)
	subroutine.domain.tasks.update(session, created, starts=moved, actor=actor)
	session.flush()

	feed, _minted = _feed(session, workspace, owner)
	standups = [
		one
		for one in subroutine.domain.calendars.occasions(session, feed, now=NOW)
		if one.task.title == "Standup"
	]

	assert len(standups) == 2, (
		f"a moved occurrence and the rule it left are two things, and this feed shows "
		f"{len(standups)}"
	)

	assert any(
		one.rule is None and getattr(one.task, one.field) == moved for one in standups
	), (
		"the occurrence was moved and the calendar shows only the grid, so a reader is told "
		"the old date"
	)


def test_a_task_with_both_dates_appears_under_both_and_they_are_told_apart (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§20.4, and the correction `SR#916` makes to it.

	*"A task with both appears twice, which is correct: the day you meant to do it and the day
	it is due are different facts"* — and §20.4 then gives the `UID` as `<task-id>@<instance>`,
	so the two would arrive under **one identity** and a client would drop one or read it as an
	override of the other. The field is part of the identity here.
	"""

	workspace, owner = _world(session)
	project = _project(session, workspace)
	_task(
		session, project, owner, title="Both",
		starts=NOW + datetime.timedelta(days=1), due=NOW + datetime.timedelta(days=2),
	)
	session.flush()

	feed, _minted = _feed(session, workspace, owner)
	found = subroutine.domain.calendars.occasions(session, feed, now=NOW)

	assert {one.field for one in found} == {"starts_at", "due_at"}

	rendered = subroutine.domain.icalendar.render(
		found, name="c", instance_id=uuid.uuid4(), now=NOW
	)
	identities = [
		line for line in rendered.split("\r\n") if line.startswith("UID:")
	]

	assert len(identities) == 2 and len(set(identities)) == 2, (
		f"two events arrived under one identity, so a client will drop one: {identities}"
	)


@pytest.mark.parametrize(
	("timezone", "written", "field", "all_day_flag"),
	[
		# A deadline is stored as the last microsecond of its day, so west of Greenwich the
		# UTC instant is already tomorrow.
		("America/Los_Angeles", datetime.time(23, 59, 59, 999999), "due_at", "due_is_all_day"),
		("Pacific/Auckland", datetime.time(23, 59, 59, 999999), "due_at", "due_is_all_day"),
		# A plan is stored as the first, so east of it the UTC instant is still yesterday.
		("Europe/London", datetime.time(0, 0), "starts_at", "starts_is_all_day"),
		("Pacific/Auckland", datetime.time(0, 0), "starts_at", "starts_is_all_day"),
		# The zone the defect cannot be seen in, kept so a fix that simply drops the
		# conversion fails here too rather than passing three cases and looking careful.
		("UTC", datetime.time(0, 0), "starts_at", "starts_is_all_day"),
	],
)
def test_an_all_day_event_lands_on_the_day_its_writer_meant (
	timezone: str, written: datetime.time, field: str, all_day_flag: str
) -> None:
	"""An all-day `DATE` is the writer's day, not the UTC instant's (`SR#1063`).

	**A `DATE` value carries no zone**, so whatever is written is what somebody reads: there
	is no conversion left for a client to get right. That is what makes this the one surface
	where the day has to be correct on the way out, and the one where a wrong day is least
	likely to be questioned.

	Driven per zone rather than once, because the defect is invisible in UTC — which is every
	CI job and every other test in this file.
	"""

	zone = zoneinfo.ZoneInfo(timezone)
	meant = datetime.date(2026, 8, 17)
	stored = datetime.datetime.combine(meant, written, tzinfo=zone).astimezone(datetime.UTC)

	class _Row:
		"""The smallest thing the renderer reads, so this needs no database."""

		id = uuid.UUID("11111111-2222-3333-4444-555555555555")
		title = "Something all day"
		starts_at = None
		due_at = None
		starts_is_all_day = False
		due_is_all_day = False
		estimate_minutes = None
		reminder_minutes = None

	setattr(_Row, field, stored)
	setattr(_Row, all_day_flag, True)
	_Row.timezone = timezone  # type: ignore[attr-defined]

	rendered = subroutine.domain.icalendar.render(
		[subroutine.domain.calendars.Occasion(task=_Row(), field=field)],  # type: ignore[arg-type]
		name="Work", instance_id=uuid.uuid4(), now=NOW,
	)

	assert f"DTSTART;VALUE=DATE:{meant:%Y%m%d}" in rendered, (
		f"{timezone}: {field} written for {meant} was published as "
		f"{[line for line in rendered.split(chr(13) + chr(10)) if 'DTSTART' in line]}"
	)

	# **The end is a calendar day later, which is not twenty-four hours later.** RFC 5545
	# makes `DTEND` exclusive, so an all-day event ending on its own date is zero days long;
	# and adding a day to the *instant* gets that wrong on the night the clocks go back,
	# where local midnight plus 24 hours is 23:00 the same evening.
	after = meant + datetime.timedelta(days=1)

	assert f"DTEND;VALUE=DATE:{after:%Y%m%d}" in rendered


def test_an_all_day_event_spans_one_day_across_a_clock_change () -> None:
	"""The two nights a day is not twenty-four hours long (`SR#1063`).

	The zero-length event the `DTEND` rule exists to prevent, reachable only by arithmetic on
	the instant. This is the case that decided the fix resolves the day *once* and adds a
	calendar day to it, rather than converting an instant that has already had 24 hours added.
	"""

	london = zoneinfo.ZoneInfo("Europe/London")

	for meant in (datetime.date(2026, 10, 25), datetime.date(2026, 3, 29)):
		stored = datetime.datetime.combine(
			meant, datetime.time(0, 0), tzinfo=london
		).astimezone(datetime.UTC)

		class _Row:
			"""One all-day plan on a night the clocks move."""

			id = uuid.UUID("11111111-2222-3333-4444-555555555555")
			title = "Something all day"
			starts_at = stored
			due_at = None
			starts_is_all_day = True
			due_is_all_day = False
			estimate_minutes = None
			reminder_minutes = None
			timezone = "Europe/London"

		rendered = subroutine.domain.icalendar.render(
			[subroutine.domain.calendars.Occasion(task=_Row(), field="starts_at")],  # type: ignore[arg-type]
			name="Work", instance_id=uuid.uuid4(), now=NOW,
		)

		after = meant + datetime.timedelta(days=1)

		assert f"DTSTART;VALUE=DATE:{meant:%Y%m%d}" in rendered, meant
		assert f"DTEND;VALUE=DATE:{after:%Y%m%d}" in rendered, (
			f"{meant}: the event is zero days long, which some clients hide entirely"
		)


def test_the_rendered_document_is_what_a_calendar_will_accept () -> None:
	"""The format's three sharp edges, driven rather than reasoned about.

	Lines end `CRLF`; a line over 75 **octets** is folded rather than truncated; and four
	characters are escaped inside a text value. Getting any of them wrong produces a file most
	clients open and one client rejects, which is the worst way to find out.
	"""

	class _Row:
		"""The smallest thing the renderer reads, so this needs no database."""

		id = uuid.UUID("11111111-2222-3333-4444-555555555555")
		title = "Standup — with a comma, a semicolon; and a title long enough to fold"
		starts_at = datetime.datetime(2026, 8, 21, 14, 0, tzinfo=datetime.UTC)
		due_at = None
		starts_is_all_day = False
		due_is_all_day = False
		estimate_minutes = 60
		reminder_minutes = None

	rendered = subroutine.domain.icalendar.render(
		[subroutine.domain.calendars.Occasion(task=_Row(), field="starts_at")],  # type: ignore[arg-type]
		name="Work", instance_id=uuid.uuid4(), now=NOW,
	)

	assert rendered.endswith("\r\n"), "the last line is unterminated, which is malformed"
	assert "\n" not in rendered.replace("\r\n", ""), "a bare LF survived"

	for line in rendered.split("\r\n"):
		assert len(line.encode("utf-8")) <= subroutine.domain.icalendar.FOLD_AT, (
			f"an unfolded line of {len(line.encode('utf-8'))} octets: {line!r}"
		)

	assert "\\," in rendered and "\\;" in rendered, "a text value went out unescaped"
	assert "—" in rendered, "an em dash was mangled, so folding split a character in half"

	# **The span, which is `SR#576` arriving** — `starts_at` plus `estimate_minutes` is an hour
	# of somebody's day rather than an instant, and this feed is the first thing to read the
	# pair as one (decision `SR#972` §2).
	assert "DTSTART:20260821T140000Z" in rendered
	assert "DTEND:20260821T150000Z" in rendered


def test_the_feed_endpoint_serves_a_calendar_and_revalidates (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The whole round trip over HTTP, driven rather than reasoned about — `SR#916`.

	**The conditional half is the one worth having.** A validator that is present and never
	matches is `SR#914` exactly: correct-looking, always a miss, and indistinguishable from one
	that works — which is what a tag over the whole body would be here, because every event
	carries the moment the document was built.
	"""

	world = test_api_tasks._world(session)
	project = _project(session, world.workspace)
	_task(session, project, world.user, title="Dentist", starts=NOW + datetime.timedelta(days=1))
	feed, minted = subroutine.domain.calendars.create(
		session,
		subroutine.domain.authentication.Principal(user=world.user),
		workspace_id=world.workspace.id,
		title="Mine", now=NOW,
	)
	session.flush()

	prefix, secret = typing.cast(
		tuple[str, str],
		subroutine.auth.parse_token(
			minted.value.get_secret_value(), kind=subroutine.auth.CALENDAR_KIND
		),
	)
	address = f"/v1/calendars/{prefix}/{secret}.ics"

	answered = api_support.call(world.application, "GET", address)

	assert answered.status_code == 200, answered.text
	assert answered.headers["content-type"].startswith("text/calendar")
	assert answered.headers["cache-control"] == subroutine.api.calendars.CACHE_CONTROL
	assert "Dentist" in answered.text
	assert "BEGIN:VCALENDAR" in answered.text

	tag = answered.headers["etag"]

	# **The claim that the tag ignores `DTSTAMP` is asserted on `_etag` directly**, and that is
	# not belt-and-braces — it is the only place it *can* be asserted. Two requests in one test
	# land in the same second, so the round trip below succeeds whether or not the timestamp is
	# excluded: the mutation putting `DTSTAMP` back into the hash **survived** the HTTP half
	# and was caught by nothing until this went in. A test that passes because of when it ran
	# is `SR#737`'s shape, arriving from the other side.
	stamped = "BEGIN:VEVENT\r\nDTSTAMP:{}\r\nSUMMARY:x\r\nEND:VEVENT\r\n"

	assert subroutine.api.calendars._etag(
		stamped.format("20260817T090000Z")
	) == subroutine.api.calendars._etag(stamped.format("20260818T113000Z")), (
		"the validator moves when only the generation time did, so every poll is a miss and "
		"the revalidation is correct, useless and indistinguishable from one that works"
	)

	# **Asked twice**, because the tag has to survive a second render — and a `DTSTAMP` inside
	# it would not, which is the whole reason `_etag` is a function rather than a hash.
	again = api_support.call(
		world.application, "GET", address, headers={"if-none-match": tag}
	)

	assert again.status_code == 304, again.text
	assert again.headers["etag"] == tag

	# And a change moves it, or the tag is a constant and every poll is a false 304.
	_task(session, project, world.user, title="Standup", starts=NOW + datetime.timedelta(days=2))
	session.flush()

	moved = api_support.call(
		world.application, "GET", address, headers={"if-none-match": tag}
	)

	assert moved.status_code == 200, "a changed calendar answered as unchanged"
	assert moved.headers["etag"] != tag

	# The poll was recorded, which is what makes a stale feed noticeable (§20.3).
	#
	# **Refreshed first**, because the request ran in its own session: `api_support.factory_for`
	# shares the test's *connection* rather than its identity map, so the object here holds
	# what it was loaded with and would report `None` however many polls had landed.
	session.refresh(feed)

	assert feed.last_polled_at is not None


def test_an_address_naming_no_feed_and_a_disabled_instance_look_alike (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§20.6's kill switch, and why it is a 404 rather than a refusal that explains itself.

	An operator who has turned feeds off has said they do not want them served. A refusal
	reading *calendars are disabled here* would confirm to whoever holds a leaked URL that it
	named something real — so the switch and a wrong address are deliberately the same answer.
	"""

	world = test_api_tasks._world(session)
	_feed_row, minted = subroutine.domain.calendars.create(
		session,
		subroutine.domain.authentication.Principal(user=world.user),
		workspace_id=world.workspace.id,
		title="Mine", now=NOW,
	)
	session.flush()

	prefix, secret = typing.cast(
		tuple[str, str],
		subroutine.auth.parse_token(
			minted.value.get_secret_value(), kind=subroutine.auth.CALENDAR_KIND
		),
	)
	address = f"/v1/calendars/{prefix}/{secret}.ics"

	assert api_support.call(world.application, "GET", address).status_code == 200

	off = test_api_tasks._world(session, instance={"calendars_enabled": False})
	refused = api_support.call(off.application, "GET", address)
	nowhere = api_support.call(
		off.application, "GET", "/v1/calendars/00000000/nothing.ics"
	)

	assert refused.status_code == 404
	assert nowhere.status_code == 404

	# **Compared on what the refusal *says*, not on the whole document.** A problem document
	# also carries `request_id` and the address that was asked for, which differ by
	# construction — so comparing bodies would fail whatever the wording, and a test that
	# cannot pass is no better than one that cannot fail.
	told = ("type", "title", "status", "code", "detail")

	assert {key: refused.json().get(key) for key in told} == {
		key: nowhere.json().get(key) for key in told
	}, (
		"a disabled instance answers differently from one where the address names nothing, "
		"which tells whoever holds a leaked URL that it was real"
	)


def test_turning_calendar_feeds_off_refuses_to_mint_one_and_still_lets_one_be_ended (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1068`, cold review `SR#1062`'s M-2 — and Simon's decision of 2026-08-22.

	``calendars_enabled`` was read on the feed route alone, so an operator who had turned feeds
	off could still be handed a URL that answered 404 for ever. `docs/connecting.md` told that
	person *"if the command is refused outright, feeds may be turned off on that instance"* —
	naming a symptom that could not occur.

	**It is a kill switch for the feature, not only for serving**, so minting is refused. And
	the two exceptions are the decision rather than an oversight:

	* **Revoking keeps working**, because turning something off must never be a way to trap a
	  live credential. An operator who disables feeds *because* one leaked would otherwise be
	  unable to end it.
	* **Listing keeps working**, one step back from that: you cannot revoke what you cannot see,
	  and a listing discloses nothing to somebody who already owns the instance.

	**Reset counts as minting**, which is the half easiest to miss: it hands back a new working
	URL exactly as creating does, and it is not how you stop a feed — that is revoking, which is
	why the pair sit on opposite sides here.
	"""

	off = test_api_tasks._world(session, instance={"calendars_enabled": False})
	on = test_api_tasks._world(session, instance={"calendars_enabled": True})

	made = on.call("POST", "/v1/calendars", json={"title": "Mine"})
	assert made.status_code == 201, made.json()

	feed = made.json()["id"]

	refused = off.call("POST", "/v1/calendars", json={"title": "Another"})

	assert refused.status_code == 403, refused.json()
	assert "turned off" in refused.json()["detail"]

	# The half that reads as an ending and is not: a new URL, from the same switch's far side.
	reset = off.call("POST", f"/v1/calendars/{feed}/reset")

	assert reset.status_code == 403, reset.json()

	assert off.call("GET", "/v1/calendars").status_code == 200, (
		"a feed nobody can list is a feed nobody can revoke"
	)
	assert off.call("DELETE", f"/v1/calendars/{feed}").status_code in (200, 204), (
		"turning the feature off must not trap a credential that is already in the world"
	)


def test_a_feed_can_be_made_listed_reset_and_revoked_over_http (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§20.3's four verbs, driven end to end against the URL each one produces.

	**Every claim is checked by fetching the address**, never by reading a column. Whether a
	reset really stopped the old URL and whether the new one really works are the two facts
	the whole feature rests on, and both are answerable only by asking.
	"""

	world = test_api_tasks._world(session, instance={"public_url": "https://example.test"})
	project = _project(session, world.workspace)
	_task(session, project, world.user, title="Dentist", starts=NOW + datetime.timedelta(days=1))
	session.flush()

	headers = {"authorization": f"Bearer {world.secret}"}
	made = api_support.call(
		world.application,
		"POST",
		"/v1/calendars",
		json={"title": "My work", "project": project.key},
		headers=headers,
	)

	assert made.status_code == 201, made.text

	first = made.json()["url"]

	assert first.startswith("https://example.test/v1/calendars/"), first
	assert first.endswith(".ics")
	assert made.json()["project_key"] is not None, "the project is reported as an address"

	path = first.removeprefix("https://example.test")

	assert "Dentist" in api_support.call(world.application, "GET", path).text

	# **The listing never carries the secret**, and that is asserted against the whole body
	# rather than against a field: a URL leaking through some *other* key is exactly the
	# mistake a field-by-field check cannot see.
	listed = api_support.call(world.application, "GET", "/v1/calendars", headers=headers)

	assert listed.status_code == 200, listed.text
	assert [row["prefix"] for row in listed.json()["items"]] == [made.json()["prefix"]]
	assert first not in listed.text, "a listing gave back the address"

	again = api_support.call(
		world.application,
		"POST",
		f"/v1/calendars/{made.json()['prefix']}/reset",
		headers=headers,
	)

	assert again.status_code == 200, again.text

	second = again.json()["url"]

	assert second != first
	assert api_support.call(world.application, "GET", path).status_code == 404
	assert api_support.call(
		world.application, "GET", second.removeprefix("https://example.test")
	).status_code == 200

	# **A reset replaces the whole credential, so the reference moves with it.** That is worth
	# asserting rather than discovering: it is one minting path rather than two, and the `id`
	# is what stays stable — which is why both address a feed.
	assert again.json()["prefix"] != made.json()["prefix"]
	assert again.json()["id"] == made.json()["id"]

	stopped = api_support.call(
		world.application, "DELETE", f"/v1/calendars/{again.json()['prefix']}", headers=headers
	)

	assert stopped.status_code == 200, stopped.text
	assert stopped.json()["revoked_at"] is not None
	assert stopped.json()["usable"] is False
	assert api_support.call(
		world.application, "GET", second.removeprefix("https://example.test")
	).status_code == 404

	# **Revoked feeds leave the listing and can still be named**, which is what makes revoking
	# twice say *already stopped* rather than *no such thing*.
	assert api_support.call(
		world.application, "GET", "/v1/calendars", headers=headers
	).json()["items"] == []
	assert api_support.call(
		world.application,
		"GET",
		"/v1/calendars?include_revoked=true",
		headers=headers,
	).json()["items"] != []


def test_a_feed_cannot_be_made_for_somebody_else (
	session: sqlalchemy.orm.Session,
) -> None:
	"""There is no owner field, so the escalation `SR#829` found cannot be expressed.

	A feed renders with its **owner's** sight, so one minted for another person would be their
	work handed to whoever asked — and unlike a sign-in link there is no session to end and
	nothing to audit afterwards.

	**Driven rather than asserted about the model**, because a request model with no such field
	and a handler that quietly ignored one look identical from the outside. What proves it is
	the request being refused by name.
	"""

	world = test_api_tasks._world(session)
	headers = {"authorization": f"Bearer {world.secret}"}
	refused = api_support.call(
		world.application,
		"POST",
		"/v1/calendars",
		json={"title": "Theirs", "owner": "someone-else"},
		headers=headers,
	)

	assert refused.status_code == 422, refused.text
	assert refused.json()["code"] == "unknown_field"
	assert "owner" in refused.text

	made = api_support.call(
		world.application, "POST", "/v1/calendars", json={"title": "Mine"}, headers=headers
	)

	assert made.status_code == 201, made.text

	feed = session.get(
		subroutine.db.models.identity.CalendarFeed, uuid.UUID(made.json()["id"])
	)

	assert feed is not None
	assert feed.owner_id == world.user.id


def test_a_feed_is_not_something_anybody_else_can_read_or_act_on (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§20.6: an inventory of somebody's feeds is the map that makes one worth stealing.

	So a second person — a **superuser**, which is the strongest credential this instance
	issues — sees none of them and cannot reset or revoke one. That is deliberately narrower
	than `GET /v1/tokens`, where an administrator sees everything.
	"""

	world = test_api_tasks._world(session)
	headers = {"authorization": f"Bearer {world.secret}"}
	made = api_support.call(
		world.application, "POST", "/v1/calendars", json={"title": "Mine"}, headers=headers
	)

	assert made.status_code == 201, made.text

	stranger = subroutine.domain.users.create(
		session, username=f"other-{uuid.uuid4().hex[:8]}", is_superuser=True
	)
	subroutine.domain.workspaces.add_member(
		session, workspace=world.workspace, user=stranger, role_key="owner"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=stranger, title="Theirs"
	)
	session.flush()

	theirs = {"authorization": f"Bearer {issued.value.get_secret_value()}"}
	listed = api_support.call(world.application, "GET", "/v1/calendars", headers=theirs)

	assert listed.status_code == 200, listed.text
	assert listed.json()["items"] == [], "somebody else's feeds appeared in this listing"

	# **Absent rather than forbidden**, which is `tokens.mine`'s rule: acting on one discloses
	# no more than the listing does, so refusing with a 403 would confirm it exists.
	for method, address in (
		("POST", f"/v1/calendars/{made.json()['prefix']}/reset"),
		("DELETE", f"/v1/calendars/{made.json()['prefix']}"),
	):
		refused = api_support.call(world.application, method, address, headers=theirs)

		assert refused.status_code == 404, f"{method} {address}: {refused.text}"


def test_a_page_of_feeds_costs_what_one_costs (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1080`. Bounded by how many feeds one person has, and the wrong shape all the same.

	``views.calendar`` resolved the project address and the item-type keys **per row**, one
	query each, on ``calendar list``. Small, because nobody has hundreds of feeds — and the
	opposite of the batch-loading rule every other listing here follows. A small N is why it
	had not bitten, not why it was right, and the shape is what the next reader copies.

	**Counted rather than timed**, this project's rule for `SR#39`-shaped claims: on a fixture
	this size a per-row walk is too fast to measure, so what is asserted is that five feeds cost
	what one does.
	"""

	workspace, owner = _world(session)
	project = _project(session, workspace)
	actor = subroutine.domain.authentication.Principal(user=owner)
	kinds = session.scalars(
		sqlalchemy.select(subroutine.db.models.vocabulary.ItemType).where(
			subroutine.db.models.vocabulary.ItemType.workspace_id == workspace.id
		)
	).all()

	made = [
		_feed(
			session,
			workspace,
			owner,
			title=f"Feed {index}",
			project_id=project.id,
			item_type_ids=[kinds[0].id],
		)[0]
		for index in range(5)
	]
	session.flush()

	counted: list[int] = []

	def watch (*_args: object, **_kwargs: object) -> None:
		"""Count one statement."""

		counted.append(1)

	sqlalchemy.event.listen(session.get_bind(), "before_cursor_execute", watch)

	try:
		counted.clear()
		subroutine.views.calendars(made[:1], session=session, principal=actor)
		one = len(counted)

		counted.clear()
		subroutine.views.calendars(made, session=session, principal=actor)
		five = len(counted)

	finally:
		sqlalchemy.event.remove(session.get_bind(), "before_cursor_execute", watch)

	assert one > 0, "the fixture is not reaching the query path at all"
	assert five == one, (
		f"rendering five feeds cost {five} queries where one cost {one} — this resolves per row"
	)


def test_a_type_filter_that_matches_nothing_is_refused_rather_than_read_as_all (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`[]` means *no types* and `None` means *every type*, so they must not collapse.

	Reading an empty filter as no filter answers *show me nothing* with *show me everything* —
	a plausible, complete, wrong answer on a feed nobody reads closely enough to notice, which
	is `SR#818`'s shape.
	"""

	world = test_api_tasks._world(session)
	headers = {"authorization": f"Bearer {world.secret}"}
	refused = api_support.call(
		world.application,
		"POST",
		"/v1/calendars",
		json={"title": "Nothing at all", "item_types": []},
		headers=headers,
	)

	assert refused.status_code == 422, refused.text
	assert "item_types" in refused.text

	# **A type that does not exist is refused naming the field the caller sent**, which is
	# `SR#547`: being told to correct `type` sends somebody looking for a field this request
	# has not got.
	unknown = api_support.call(
		world.application,
		"POST",
		"/v1/calendars",
		json={"title": "Nonsense", "item_types": ["nosuchtype"]},
		headers=headers,
	)

	assert unknown.status_code == 422, unknown.text
	assert unknown.json()["errors"][0]["field"] == "item_types"

	kept = api_support.call(
		world.application,
		"POST",
		"/v1/calendars",
		json={"title": "Bugs", "item_types": ["bug"]},
		headers=headers,
	)

	assert kept.status_code == 201, kept.text
	assert kept.json()["item_types"] == ["bug"], "the filter reads back as what was typed"


def test_an_instance_that_cannot_address_itself_says_so_rather_than_guessing (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§20.2, and `SR#832`'s reasoning about what an instance may infer about itself.

	The whole URL is the credential, so a host assembled from a request header would send the
	secret wherever a proxy pointed, on every poll, for as long as somebody stays subscribed.
	The feed is still made — it works, once the operator sets `public_url` and resets it.
	"""

	world = test_api_tasks._world(session)
	made = api_support.call(
		world.application,
		"POST",
		"/v1/calendars",
		json={"title": "Mine"},
		headers={"authorization": f"Bearer {world.secret}"},
	)

	assert made.status_code == 201, made.text
	assert made.json()["url"] is None
	assert made.json()["prefix"], "the feed exists and can be reset once there is an address"


def test_somebody_who_may_not_read_the_work_cannot_mint_a_feed_of_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A feed does exactly one thing, and this is the permission that decides it.

	**No seeded role can reach this**, which is the fact worth writing down rather than
	discovering: `viewer` is the narrowest and it carries every read, and somebody who is not a
	member cannot name the workspace at all. So the trigger is a role an installation made
	itself — which §5.5 says it may, since permissions are a JSON list precisely so that a
	custom role is a data change.

	**Not deleted for being hard to reach**, which is the pull `SR#303` records. Without it the
	feed is minted and renders through `scoping.readable_tasks`, which narrows by *membership*
	and project visibility and asks nothing about verbs — so the URL would show work its owner
	is not allowed to read, to a program that polls it every quarter of an hour.
	"""

	workspace, _founder = _world(session)
	quiet = subroutine.domain.users.create(
		session, username=f"quiet-{uuid.uuid4().hex[:8]}"
	)
	blind = subroutine.db.models.identity.Role(
		workspace_id=workspace.id,
		key="no-reading",
		title="No reading",
		permissions=[subroutine.permissions.COMMENT_WRITE],
	)
	session.add(blind)
	session.flush()
	membership = subroutine.db.models.identity.WorkspaceMember(
		workspace_id=workspace.id, user_id=quiet.id, role_id=blind.id
	)
	session.add(membership)
	session.flush()

	actor = subroutine.domain.authentication.Principal(user=quiet)

	with pytest.raises(subroutine.domain.authorization.AuthorizationError):
		subroutine.domain.calendars.create(
			session, actor, workspace_id=workspace.id, title="Not mine to see", now=NOW
		)

	# **And the same person with the ordinary role can**, or this is a rule that refuses
	# everybody and the test above says nothing about which half fired. The membership is
	# moved rather than a second one added: one person holds one role in one workspace.
	membership.role_id = subroutine.domain.workspaces.find_role(
		session, workspace.id, "viewer"
	).id
	session.flush()

	made, _minted = subroutine.domain.calendars.create(
		session, actor, workspace_id=workspace.id, title="Mine to see", now=NOW
	)

	assert made.owner_id == quiet.id
