"""The same scenarios through both transports, asserting the output matches.

SPEC.md §13.7 makes the local database a connection like any other so that ``subroutine
today`` fans out across it and every configured remote through **one** code path — one that
does not know which of its answers arrived over a socket. That claim is only true while the
two clients return the same objects, and it is the kind of claim that stops being true
quietly: a field added to a view, an ordering changed in one listing, an error raised as a
different class.

So both clients are pointed at the **same database** in the same test, and their answers are
compared field by field. The HTTP one goes through the real application over httpx's ASGI
transport; nothing here is stubbed, because a stub would agree with whatever it was written
against.

Every test runs on SQLite and PostgreSQL, since two of the things most likely to diverge —
NULL ordering and datetime awareness — are invisible on one of them.
"""

import datetime
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.clients.base
import subroutine.clients.http
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.db.models.identity
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.tasks
import subroutine.errors
import subroutine.views


class Pair(typing.NamedTuple):
	"""The same installation, reached both ways."""

	local: subroutine.clients.local.Client
	remote: subroutine.clients.http.Client
	session: sqlalchemy.orm.Session
	user: subroutine.db.models.identity.User
	workspace: subroutine.db.models.identity.Workspace

	def both (self) -> tuple[subroutine.clients.local.Client, subroutine.clients.http.Client]:
		"""Return the two clients, for a test that asks each the same thing."""

		return self.local, self.remote


@pytest.fixture
def pair (session: sqlalchemy.orm.Session) -> typing.Iterator[Pair]:
	"""One installation, a local client and an HTTP client, sharing a transaction."""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test Instance"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Equivalence"
	)
	session.flush()

	factory = api_support.factory_for(session)
	settings = subroutine.config.Settings(dev_mode=True)
	application = api_support.build_app(factory)

	local = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"), settings, session_factory=factory
	)
	remote = subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url="https://tasks.example.com"),
		token=issued.value.get_secret_value(),
		transport=api_support.SyncTransport(application),
		base_url=api_support.BASE_URL,
	)

	with local, remote:
		yield Pair(
			local=local,
			remote=remote,
			session=session,
			user=setup.user,
			workspace=setup.workspace,
		)


def make (pair: Pair, text: str) -> subroutine.views.Task:
	"""Add a task through the local client, for the other one to read back."""

	return pair.local.capture(text=text).task


def test_both_report_the_same_instance_and_workspaces (pair: Pair) -> None:
	"""``identity`` is what tells two connections apart, so it has to agree with itself."""

	local, remote = pair.both()

	assert local.identity() == remote.identity()

	instance = local.identity().instance

	assert instance is not None
	assert instance.name == "Test Instance"
	assert [workspace.slug for workspace in local.identity().workspaces] == [
		pair.workspace.slug
	]


def test_both_render_a_task_identically (pair: Pair) -> None:
	"""Every field, not a chosen few — including the ones only one path computes."""

	created = make(pair, "Ship the release by friday !3 ~2h #urgent #work")

	# Urgency has no capture token (§6.13 covers importance only), and this is here to make
	# `priority_score` a *derived* number on both sides rather than a null that would agree
	# by costing nobody anything.
	pair.session.execute(
		sqlalchemy.update(subroutine.db.models.work.Task)
		.where(subroutine.db.models.work.Task.id == created.id)
		.values(urgency=3)
	)
	pair.session.flush()

	local, remote = pair.both()
	from_local = local.task(ref=created.ref)
	from_remote = remote.task(ref=created.ref)

	assert from_local == from_remote
	assert from_local is not None

	# The three fields most likely to be produced by only one of the two paths: a derived
	# value, a batch-loaded relation, and a resolved vocabulary key.
	assert from_local.priority_score == 9
	assert from_local.tags == ["urgent", "work"]
	assert from_local.status_category == "todo"


def test_both_list_the_same_rows_in_the_same_order (pair: Pair) -> None:
	"""Ordering is where a listing diverges without anybody noticing.

	Ten rows created in one transaction share a ``created_at`` on a fast machine, so this is
	also the tie-break test — and the tie-break is a different column on each side unless
	somebody kept them the same.
	"""

	for index in range(10):
		make(pair, f"Task number {index}")

	local, remote = pair.both()

	assert local.tasks() == remote.tasks()
	assert [task.title for task in local.tasks()] == [
		task.title for task in remote.tasks()
	]


def test_both_return_the_same_agenda (pair: Pair) -> None:
	"""Four buckets, a date, a zone and a total, all resolved the same way."""

	make(pair, "Overdue thing by 2020-01-01")
	make(pair, "Today thing by today")
	make(pair, "Someday thing")

	local, remote = pair.both()
	day = datetime.date(2026, 7, 30)

	assert local.agenda(date=day, timezone="Europe/London") == remote.agenda(
		date=day, timezone="Europe/London"
	)
	assert local.agenda(date=day).date == day


def test_both_capture_the_same_line_the_same_way (pair: Pair) -> None:
	"""Including what the grammar declined to read, which each works out differently."""

	local, remote = pair.both()

	from_local = local.capture(text="Water the plants every monday")
	from_remote = remote.capture(text="Water the plants every monday")

	assert from_local.unparsed == from_remote.unparsed == ("every monday",)

	# §6.13 rule 1: nothing is lost, so the words stay in the title on both paths.
	assert from_local.task.title == from_remote.task.title == "Water the plants every monday"


def test_both_complete_a_task_the_same_way (pair: Pair) -> None:
	"""And both do it unconditionally, so "already done" stays the caller's decision."""

	first = make(pair, "Finish this one")
	second = make(pair, "And this one")

	local, remote = pair.both()
	by_local = local.complete(ref=first.ref)
	by_remote = remote.complete(ref=second.ref)

	assert by_local.completed_at is not None
	assert by_remote.completed_at is not None
	assert by_local.status == by_remote.status
	assert by_local.status_category == by_remote.status_category == "done"


def test_both_schedule_a_task_the_same_way (pair: Pair) -> None:
	"""And both tell an omitted field from a null one (§8.3)."""

	first = make(pair, "Plan this one")
	second = make(pair, "And this one")
	day = datetime.date(2026, 8, 3)

	local, remote = pair.both()

	assert local.schedule(ref=first.ref, planned_for=day).planned_for == day
	assert remote.schedule(ref=second.ref, planned_for=day).planned_for == day

	# Setting the other field must leave the first alone — the difference between "not
	# mentioned" and "cleared", which is the bug §8.3 exists to prevent.
	assert local.schedule(ref=first.ref, start=day).planned_for == day
	assert remote.schedule(ref=second.ref, start=day).planned_for == day

	assert local.schedule(ref=first.ref, planned_for=None).planned_for is None
	assert remote.schedule(ref=second.ref, planned_for=None).planned_for is None


def test_both_say_nothing_is_there_the_same_way (pair: Pair) -> None:
	"""A missing task is ``None`` on both sides, not a refusal on one of them.

	This is what makes resolving an address across several connections possible at all: the
	client asks every connection and expects most of them to say no.
	"""

	local, remote = pair.both()

	assert local.task(ref=9999) is None
	assert remote.task(ref=9999) is None


def test_a_remote_refusal_arrives_as_the_local_exception (pair: Pair) -> None:
	"""A problem document is read back into the class that would have raised it.

	Otherwise a client fanning out has two vocabularies of failure, and every message it
	prints has to say which kind it was before saying what went wrong.
	"""

	local, remote = pair.both()

	with pytest.raises(subroutine.errors.NotFound) as locally:
		local.complete(ref=4242)

	with pytest.raises(subroutine.errors.NotFound) as remotely:
		remote.complete(ref=4242)

	assert locally.value.code == remotely.value.code == "not_found"
	assert locally.value.status == remotely.value.status == 404


def test_a_remote_refusal_keeps_its_field_errors (pair: Pair) -> None:
	"""The part an agent acts on is the field name, and it has to survive the wire.

	Written as a live call rather than a hand-built problem document, so that a change to
	either end of the envelope breaks it.
	"""

	local, remote = pair.both()
	too_long = "x" * (subroutine.domain.tasks.MAX_TITLE_LENGTH + 1)

	with pytest.raises(subroutine.errors.PayloadTooLarge) as locally:
		local.capture(text=too_long)

	with pytest.raises(subroutine.errors.PayloadTooLarge) as remotely:
		remote.capture(text=too_long)

	assert locally.value.code == remotely.value.code
	assert locally.value.detail == remotely.value.detail
	assert [field.field for field in remotely.value.errors] == [
		field.field for field in locally.value.errors
	] == ["title"]


def test_a_read_only_connection_refuses_a_write_before_it_leaves (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Client-side enforcement, which is the only place it can be (§13.7).

	Pointing an agent at a company instance for context while forbidding it to write there
	is a reasonable posture, and it is not one the company's server can arrange on the
	agent-owner's behalf.
	"""

	subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)

	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local", read_only=True),
		subroutine.config.Settings(dev_mode=True),
		session_factory=api_support.factory_for(session),
	)

	with client, pytest.raises(subroutine.errors.Forbidden) as raised:
		client.capture(text="This should not be written")

	assert "read-only" in raised.value.detail
