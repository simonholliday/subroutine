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

import pytest
import sqlalchemy.orm

import subroutine.auth
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.calendars
import subroutine.domain.icalendar
import subroutine.domain.projects
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors

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
	"""Mint a feed as somebody at a terminal, which no check narrows (§12.1a)."""

	kwargs.setdefault("title", "My calendar")

	return subroutine.domain.calendars.create(
		session, None, workspace_id=workspace.id, owner=owner, now=NOW, **kwargs
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
	again = subroutine.domain.calendars.reset(session, feed)
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
			session, narrowed, workspace_id=workspace.id, owner=owner,
			title="Wider than me", now=NOW,
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
		workspace_id=workspace.id, owner=owner, title="Fine", now=NOW,
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
			session, actor, workspace_id=workspace.id, owner=owner,
			title="For ever", now=NOW,
		)

	assert "outlive" in str(refused.value)

	made, _minted = subroutine.domain.calendars.create(
		session, actor, workspace_id=workspace.id, owner=owner, title="Bounded",
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
