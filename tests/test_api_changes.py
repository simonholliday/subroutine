"""The change feed over HTTP — docs/design.md §5.11a, items ``#13`` and ``#158``.

The second reader of the ``event`` table, and the one that answers a *resumption* question.
Three of these tests are the done-criteria rather than coverage:

* **The watermark hides an event written this instant**, which is the exact opposite of the
  history's guarantee and the reason §5.11a insists these are two endpoints.
* **A private project's events are absent from the feed.** The histories get their permission
  check free by resolving a subject; the feed has no subject and composes the predicates
  itself, so this is the test that proves the composition happens.
* **The ``seq`` visibility trap, on PostgreSQL specifically.** Two concurrent transactions,
  the later committing first. SQLite has one writer and hides it by construction, so an
  implementation without the watermark passes the whole suite on SQLite and loses events in
  production.

**Events are back-dated rather than slept past.** The watermark is one second, and a suite
that waited it out per test would pay it dozens of times for nothing. ``_settled`` moves the
rows back instead, which tests the query rather than the clock — and the one test that must
*not* back-date is named for it.
"""

import datetime
import os
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import conftest
import subroutine.db.migrate
import subroutine.db.models.activity
import subroutine.db.session
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.events
import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation with a feed to read."""

	return test_api_tasks._world(session)


def _settled (session: sqlalchemy.orm.Session, *, seconds: int = 2) -> None:
	"""Move every event far enough into the past that the watermark lets it through.

	The alternative is ``time.sleep(1)`` in every test here, which buys nothing: the rule
	under test is "``created_at`` is at least a second old", and which side of it a row falls
	on is what matters rather than how it got there.

	Shifted row by row in Python rather than by one ``UPDATE … SET created_at = created_at -
	interval``: ``UtcDateTime`` normalises every bound value to an aware datetime, so it meets
	the timedelta on the right-hand side and asks it for a ``tzinfo``. A handful of rows per
	test is not worth teaching the column type about intervals.
	"""

	shift = datetime.timedelta(seconds=seconds)

	for event in session.scalars(sqlalchemy.select(subroutine.db.models.activity.Event)):
		event.created_at = event.created_at - shift

	session.flush()


def _feed (world: test_api_tasks.World, **query: typing.Any) -> list[dict[str, typing.Any]]:
	"""Read the feed and return its items, failing loudly on anything but a 200."""

	answered = world.call("GET", "/v1/changes", params=query)

	assert answered.status_code == 200, answered.text

	items: list[dict[str, typing.Any]] = answered.json()["items"]

	return items


def test_the_feed_reports_what_happened (world: test_api_tasks.World) -> None:
	"""The whole point, in one call and without naming a subject."""

	task = world.call("POST", "/v1/tasks", json={"title": "Fix the parser"}).json()
	_settled(world.session)

	events = _feed(world)
	mine = [item for item in events if item["entity_id"] == task["id"]]

	assert [item["action"] for item in mine] == ["created"]


def test_an_event_written_this_instant_is_withheld (world: test_api_tasks.World) -> None:
	"""**The watermark, and the reason a history may not inherit it** (docs/design.md §5.11).

	``seq`` is allocated at insert and becomes visible at commit. A reader that advances its
	cursor past a number still uncommitted never sees that event again, so the feed reports
	nothing newer than ``now() - 1s`` and gives the slower transaction time to land.

	Deliberately **not** back-dated. This is the one test whose subject is the clock.
	"""

	task = world.call("POST", "/v1/tasks", json={"title": "Written just now"}).json()

	assert [item for item in _feed(world) if item["entity_id"] == task["id"]] == []

	_settled(world.session)

	assert [item for item in _feed(world) if item["entity_id"] == task["id"]] != []


def test_the_feed_runs_oldest_first (world: test_api_tasks.World) -> None:
	"""A feed is read forwards, unlike a history — a cursor only goes one way."""

	for index in range(4):
		world.call("POST", "/v1/tasks", json={"title": f"Task {index}"})

	_settled(world.session)

	sequences = [item["seq"] for item in _feed(world)]

	assert sequences == sorted(sequences)


def test_since_is_inclusive (world: test_api_tasks.World) -> None:
	"""§5.11 fixes cursors as inclusive-with-dedupe, and it is client-visible either way.

	One duplicate row per poll is the price of a client that persists its cursor before it
	has finished processing the page being correct rather than lossy.
	"""

	world.call("POST", "/v1/tasks", json={"title": "First"})
	world.call("POST", "/v1/tasks", json={"title": "Second"})
	_settled(world.session)

	everything = _feed(world)
	resumed = _feed(world, since=everything[-1]["seq"])

	assert resumed[0]["seq"] == everything[-1]["seq"]


def test_a_since_below_the_first_seq_is_refused (world: test_api_tasks.World) -> None:
	"""``since=0`` is not "before everything" — it names nothing, and says so.

	Left to mean "from the start" it would be indistinguishable from a cursor whose events
	have been pruned, so a caller who had simply never polled before would meet a ``410``.
	"""

	answered = world.call("GET", "/v1/changes", params={"since": 0})

	assert answered.status_code == 422
	assert answered.json()["errors"][0]["field"] == "since"


def test_the_feed_refuses_a_parameter_it_does_not_declare (
	world: test_api_tasks.World,
) -> None:
	"""A typo'd filter returns the whole feed and charges the caller for it otherwise."""

	answered = world.call("GET", "/v1/changes", params={"actorr": "me"})

	assert answered.status_code == 422


def test_the_feed_refuses_an_actor_it_does_not_understand (
	world: test_api_tasks.World,
) -> None:
	"""``me`` or nothing. A username here would read as working and filter nothing."""

	answered = world.call("GET", "/v1/changes", params={"actor": "simon"})

	assert answered.status_code == 422
	assert answered.json()["errors"][0]["field"] == "actor"


def test_a_private_project_stays_out_of_the_feed (session: sqlalchemy.orm.Session) -> None:
	"""**The leak this endpoint makes newly possible** (docs/design.md §7.3a).

	A history resolves its subject and gets the permission check for free. The feed has no
	subject, composes the predicates itself, and is therefore the one place where forgetting
	a rule publishes rather than hides. Everything about the private project must be absent —
	the project's own events and the task's alike.
	"""

	world = test_api_tasks._world(session)

	world.call(
		"POST",
		"/v1/projects",
		json={"key": "secret", "title": "Secret", "visibility": "private"},
	)
	hidden = world.call(
		"POST", "/v1/tasks", json={"title": "Acquire the rival company", "project": "secret"}
	).json()
	_settled(session)

	assert [item for item in _feed(world) if item["entity_id"] == hidden["id"]] != []

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, outsider, role_key="member")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=outsider, title="outsider"
	)
	session.flush()

	nosy = world._replace(secret=issued.value.get_secret_value())
	seen = _feed(nosy)

	assert [item for item in seen if item["entity_id"] == hidden["id"]] == []
	assert not [item for item in seen if item["entity_type"] == "project" and _secret(item)]


def _secret (item: dict[str, typing.Any]) -> bool:
	"""Whether an event's recorded changes mention the private project by key."""

	return "secret" in str(item.get("changes"))


def test_a_projects_deletion_reaches_the_feed (session: sqlalchemy.orm.Session) -> None:
	"""A deletion is the event most worth reporting, and it was the one being dropped (`#307`).

	The feed reached a project through ``readable_projects``, which had no ``include_deleted``
	at all — so the row went, and with it the only notice anybody polling would ever get.
	"""

	world = test_api_tasks._world(session)

	world.call("POST", "/v1/projects", json={"key": "doomed", "title": "Doomed"})
	world.call("DELETE", "/v1/projects/DOOMED")
	_settled(session)

	assert [
		item
		for item in _feed(world)
		if item["entity_type"] == "project" and item["action"] == "deleted"
	] != []


def test_deleting_a_project_does_not_erase_its_contents_past (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**A feed may not rewrite what it has already reported** (docs/design.md §5.11a, `#307`).

	The worse half, and the one no permission test could see. A task is reached through a join
	to its project, so deleting the project removed every event about everything inside it —
	and a client polling afterwards was told those items had never existed. That is the single
	failure a resumable feed cannot have, because it is indistinguishable from nothing having
	happened.

	Asserted across the deletion rather than after it: the point is not that the events are
	present, it is that the same question gets the same answer twice.
	"""

	world = test_api_tasks._world(session)

	world.call("POST", "/v1/projects", json={"key": "doomed", "title": "Doomed"})
	inside = world.call(
		"POST", "/v1/tasks", json={"title": "Filed in it", "project": "doomed"}
	).json()
	_settled(session)

	# Identified by ``seq`` rather than compared whole: ``_settled`` back-dates every row it
	# finds, so calling it twice moves the ``created_at`` of events already reported. The claim
	# is that the same events are still there, not that their timestamps never move.
	before = _about(world, inside["id"])

	assert before != []

	world.call("DELETE", "/v1/projects/DOOMED")
	_settled(session)

	assert _about(world, inside["id"])[: len(before)] == before


def _about (world: test_api_tasks.World, entity_id: str) -> list[tuple[int, str]]:
	"""Return the feed's events for one entity, as the pairs that identify them."""

	return [
		(item["seq"], item["action"])
		for item in _feed(world)
		if item["entity_id"] == entity_id
	]


def test_a_comment_reaches_the_feed_through_what_it_was_written_on (
	world: test_api_tasks.World,
) -> None:
	"""A comment's event names the comment; its *subject* is what decides who may see it."""

	task = world.call("POST", "/v1/tasks", json={"title": "Fix the parser"}).json()
	world.call(
		"POST", f"/v1/tasks/{task['ref']}/comments", json={"body": "Tried the tokeniser first"}
	)
	_settled(world.session)

	comments = [item for item in _feed(world) if item["entity_type"] == "comment"]

	assert len(comments) == 1
	assert comments[0]["subject_id"] == task["id"]


def test_a_comment_on_a_private_task_stays_out_of_the_feed (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The subject is the whole of the check, so a comment inherits its item's privacy.

	Worth its own test rather than trusting the one above: a comment's own ``entity_id`` is
	the comment row, which no visibility rule covers, so an implementation matching only on
	``entity_id`` would report the *prose* of a private item to a non-member.
	"""

	world = test_api_tasks._world(session)

	world.call(
		"POST",
		"/v1/projects",
		json={"key": "secret", "title": "Secret", "visibility": "private"},
	)
	hidden = world.call(
		"POST", "/v1/tasks", json={"title": "Acquire the rival", "project": "secret"}
	).json()
	world.call(
		"POST", f"/v1/tasks/{hidden['ref']}/comments", json={"body": "Board approved it"}
	)
	_settled(session)

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, outsider, role_key="member")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=outsider, title="outsider"
	)
	session.flush()

	nosy = world._replace(secret=issued.value.get_secret_value())

	assert [item for item in _feed(nosy) if item["entity_type"] == "comment"] == []


def test_a_link_event_reaches_the_feed_through_the_item_it_hangs_off (
	world: test_api_tasks.World,
) -> None:
	"""`#252`. Link events were excluded entirely, and this test asserted the exclusion.

	The exclusion was the right way round while it stood — an event whose ``entity_id`` names
	a link row cannot be scoped, and under-reporting is the recoverable half of that choice.
	It stops being needed once a link records *what it hangs off*, which is the pair a comment
	has always carried: the feed then narrows it with a rule that knows nothing about links.

	**The residual is `#302` and is deliberately not asserted away here**: the subject is the
	link's source, so an event whose source is visible still reports the far end's ref in its
	``changes``. That is the conjunction one subject cannot express, and it is a schema
	question rather than a line.
	"""

	first = world.call("POST", "/v1/tasks", json={"title": "Build the endpoint"}).json()
	second = world.call("POST", "/v1/tasks", json={"title": "Design it"}).json()

	# **Asserted, because the version of this test that guarded the exclusion was vacuous.**
	# It sent `type` where the model declares `link_type`, so the request was refused, no link
	# event was ever written, and it passed by having nothing to exclude — it went on passing
	# with the whole scoping predicate removed, which is how it was caught.
	linked = world.call(
		"POST",
		f"/v1/tasks/{first['ref']}/links",
		json={"target_type": "task", "target": second["ref"], "link_type": "blocks"},
	)

	assert linked.status_code == 201, linked.text

	_settled(world.session)

	reported = [item for item in _feed(world) if item["entity_type"] == "link"]

	assert reported, "a link event is no longer excluded from the feed"

	# Scoped through the source, and named to the reader as that item — which is what makes
	# the row readable rather than a bare identifier.
	assert reported[0]["subject_type"] == "task"
	assert reported[0]["item_ref"] == first["ref"]

	# **The source and not the target, said as its own assertion** (`#783`). `views.Event`
	# published the opposite — *a link names nothing* — for as long as it took somebody to read
	# the schema and believe it, which is the whole cost of a docstring that is also a contract.
	# The line above already pinned this by equality; this one says *which* fact it is pinning,
	# so a reader of either the schema or the test finds the same sentence.
	assert reported[0]["item_ref"] != second["ref"], (
		"a link event named its target, so a client watching an item would see links made to it"
	)


def test_actor_me_reports_only_this_credential (session: sqlalchemy.orm.Session) -> None:
	"""`#158`, and **the credential rather than the user** is the whole of the distinction.

	An agent holding a service-account token wants what it did, not what the person who
	issued the token did from a laptop an hour ago — so two tokens belonging to the *same*
	user must not see each other's work under ``?actor=me``.
	"""

	world = test_api_tasks._world(session)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=world.user, title="agent"
	)
	session.flush()

	agent = world._replace(secret=issued.value.get_secret_value())

	ours = world.call("POST", "/v1/tasks", json={"title": "Written by the person"}).json()
	theirs = agent.call("POST", "/v1/tasks", json={"title": "Written by the agent"}).json()
	_settled(session)

	seen = {item["entity_id"] for item in _feed(agent, actor="me")}

	assert theirs["id"] in seen
	assert ours["id"] not in seen

	# And unfiltered, the same credential sees both — otherwise the test above would pass on
	# a feed that had simply lost an event.
	everything = {item["entity_id"] for item in _feed(agent)}

	assert {ours["id"], theirs["id"]} <= everything


def test_a_cursor_below_what_is_still_held_is_refused (
	world: test_api_tasks.World,
) -> None:
	"""``410 cursor_expired`` — the one failure a feed must never have silently (§5.11).

	A page that quietly omits everything pruned between the cursor and the oldest surviving
	event looks exactly like nothing having happened, which is the belief this endpoint exists
	to prevent.

	**Unreachable in production today**, because nothing prunes (`#251`); the rows are deleted
	here to exercise the path before the feature that will produce it.
	"""

	world.call("POST", "/v1/tasks", json={"title": "Long ago"})
	_settled(world.session)

	model = subroutine.db.models.activity.Event
	oldest = world.session.scalar(sqlalchemy.select(sqlalchemy.func.min(model.seq)))

	assert oldest is not None

	world.session.execute(sqlalchemy.delete(model).where(model.seq <= oldest + 1))
	world.session.flush()

	answered = world.call("GET", "/v1/changes", params={"since": oldest})

	assert answered.status_code == 410
	assert answered.json()["code"] == "cursor_expired"


@pytest.fixture
def own_database (tmp_path: typing.Any) -> typing.Iterator[str]:
	"""A PostgreSQL database this test owns outright, for real concurrent connections.

	The shared session fixture wraps every test in a transaction it rolls back, which is
	exactly what cannot be used here: the trap under test is about two transactions
	*committing*, and committing into the suite's database would leave rows behind — a
	documented way to make ten unrelated tests fail much later.
	"""

	reason = conftest._postgres_unavailable_reason()

	if reason is not None:
		if conftest.REQUIRE_POSTGRES:
			pytest.fail(reason)

		pytest.skip(reason)

	name = f"subroutine_changes_{os.getpid()}_{abs(hash(tmp_path)) % 100000}"
	admin = sqlalchemy.create_engine(conftest.POSTGRES_ADMIN_URL, isolation_level="AUTOCOMMIT")

	try:
		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{name}"'))
			connection.execute(sqlalchemy.text(f'CREATE DATABASE "{name}"'))

		yield conftest.with_database(conftest.POSTGRES_ADMIN_URL, name)

		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{name}"'))

	finally:
		admin.dispose()


def test_an_event_committed_late_is_never_skipped (own_database: str) -> None:
	"""**The done-criterion for `#13`, and it can only fail on PostgreSQL** (docs/design.md §5.11).

	Transaction A takes ``seq`` 100 and holds it; B takes 101 and commits first. A reader
	polling at that instant sees 101, advances its cursor, and loses 100 for ever — because
	``seq`` is allocated at insert and becomes visible at commit, which are not the same
	moment.

	This asserts both halves. First that the hazard is **real**: with no watermark, B's event
	is visible while A's lower number is not, which is the moment a cursor would jump the gap.
	Then that the watermark **closes it**: nothing that recent is reportable, so the cursor
	does not move until A has landed and both are delivered in order.

	SQLite serialises writers and cannot produce the state at all, which is why this test owns
	a PostgreSQL database rather than running on the parameterised engine.
	"""

	subroutine.db.migrate.upgrade(own_database)
	engine = subroutine.db.session.create_engine(own_database)

	try:
		factory = subroutine.db.session.create_session_factory(engine)

		with factory() as setup:
			bootstrapped = subroutine.domain.bootstrap.initialise(
				setup, username="si", instance_name="Test"
			)
			workspace_id = bootstrapped.workspace.id
			setup.commit()

		slow = factory()
		quick = factory()

		try:
			held = subroutine.domain.events.record(
				slow,
				workspace_id=workspace_id,
				entity_type="task",
				entity_id=uuid.uuid4(),
				action="created",
			)
			slow.flush()

			raced = subroutine.domain.events.record(
				quick,
				workspace_id=workspace_id,
				entity_type="task",
				entity_id=uuid.uuid4(),
				action="created",
			)
			quick.flush()

			# The allocation order is what makes this a trap rather than a curiosity.
			assert held.seq < raced.seq

			quick.commit()

			with factory() as reader:
				visible = set(
					reader.scalars(
						subroutine.domain.events.selected(workspace_ids=[workspace_id])
					)
				)
				numbers = {event.seq for event in visible}

				# The hazard, demonstrated rather than asserted about: the later number is
				# readable and the earlier one is not.
				assert raced.seq in numbers
				assert held.seq not in numbers

				# And the watermark refuses to report either, so no cursor can jump the gap.
				watermarked = reader.scalars(
					subroutine.domain.events.selected(
						workspace_ids=[workspace_id],
						upper_bound=subroutine.db.types.utcnow()
						- subroutine.domain.events.WATERMARK,
					)
				)

				assert raced.seq not in {event.seq for event in watermarked}

			slow.commit()

			with factory() as after:
				delivered = [
					event.seq
					for event in after.scalars(
						subroutine.domain.events.selected(
							workspace_ids=[workspace_id]
						).order_by(subroutine.db.models.activity.Event.seq)
					)
				]

			assert held.seq in delivered
			assert delivered == sorted(delivered)

		finally:
			slow.close()
			quick.close()

	finally:
		engine.dispose()


def test_the_shipped_watermark_is_more_than_nothing () -> None:
	"""§5.11 fixes this value, and it is now the only thing that says so — item ``#404``.

	**The hole a monkeypatch opens.** The tests that exercise the feed's withholding set
	``WATERMARK`` rather than racing the clock, which is what makes them deterministic — and
	means none of them would notice the shipped value going to zero. A guard that can be
	switched off by the thing it guards is no guard, so the constant itself is asserted here.

	Zero is the value that would actually be reached: it is what somebody sets while debugging
	"why does my event not appear", and it is the state the feed's whole resumability argument
	depends on not being in. `seq` is allocated at insert and becomes visible at commit, so a
	feed with no watermark hands out a cursor past rows that were still uncommitted when it
	was read, and those rows are never reported to anybody.
	"""

	assert datetime.timedelta(0) < subroutine.domain.events.WATERMARK
