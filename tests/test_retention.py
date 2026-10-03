"""Events past a retention floor move to an archive, and nothing is deleted - `#251`, decision `#4233`.

The change feed reads the live table, so its floor moves and a cursor at or below the highest
``seq`` moved is answered ``410 cursor_expired``; everything that reads history reads both tables.
Each test ages the events it wants moved, rather than waiting for them to age.
"""

import datetime
import pathlib
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.event
import sqlalchemy.orm
import typer.testing

import api_support
import subroutine.cli.main
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.db.models.activity
import subroutine.db.models.system
import subroutine.db.types
import subroutine.domain.events
import subroutine.domain.retention
import subroutine.domain.workspaces
import subroutine.errors
import test_api_tasks

#: The floor every test here sets.
DAYS = 30

#: Well behind the floor, so nothing depends on the hour the suite runs at.
LONG_AGO = subroutine.db.types.utcnow() - datetime.timedelta(days=90)

LIVE = typing.cast(sqlalchemy.Table, subroutine.db.models.activity.Event.__table__)
ARCHIVE = subroutine.db.models.activity.ARCHIVE


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def _filed (world: test_api_tasks.World, title: str) -> dict[str, typing.Any]:
	"""File a task over HTTP and return it."""

	made = world.call("POST", "/v1/tasks", json={"title": title})

	assert made.status_code == 201, made.text

	return typing.cast(dict[str, typing.Any], made.json())


def _aged (session: sqlalchemy.orm.Session, *, by: datetime.timedelta | None = None) -> None:
	"""Move every live event back: behind the floor, or by ``by`` past the feed's watermark."""

	for event in session.scalars(sqlalchemy.select(subroutine.db.models.activity.Event)):
		event.created_at = LONG_AGO if by is None else event.created_at - by

	session.flush()


def _archived (world: test_api_tasks.World) -> subroutine.domain.retention.Archived:
	"""Move what is past the floor, as ``db archive`` and a serving instance both do."""

	world.session.flush()
	archived = subroutine.domain.retention.run(
		api_support.factory_for(world.session), days=DAYS, now=subroutine.db.types.utcnow()
	)
	world.session.expire_all()

	return archived


def _count (session: sqlalchemy.orm.Session, table: sqlalchemy.Table) -> int:
	"""Return how many rows a table holds."""

	return int(session.scalar(sqlalchemy.select(sqlalchemy.func.count()).select_from(table)) or 0)


def test_events_past_the_floor_move_and_none_is_lost (world: test_api_tasks.World) -> None:
	"""The move: old events leave the feed for the archive, every one arrives, and the floor is kept."""

	_filed(world, "Fix the deploy script")
	_filed(world, "Find the white rabbit")
	_aged(world.session)
	_filed(world, "Take the red pill")

	held = _count(world.session, LIVE)
	archived = _archived(world)

	assert archived.moved > 0
	assert _count(world.session, ARCHIVE) == archived.moved
	assert _count(world.session, LIVE) + archived.moved == held, "an event was lost on the way"
	assert archived.through == world.session.scalar(sqlalchemy.select(sqlalchemy.func.max(ARCHIVE.c.seq)))
	assert world.session.scalar(sqlalchemy.select(sqlalchemy.func.min(LIVE.c.seq))) > archived.through

	instance = world.session.scalar(sqlalchemy.select(subroutine.db.models.system.Instance))

	assert instance is not None
	assert instance.events_archived_through == archived.through, "the floor a cursor is refused by"

	again = _archived(world)

	assert again.moved == 0, "a second run moved what had already gone"


def test_the_newest_event_stays_however_old (world: test_api_tasks.World) -> None:
	"""An emptied table would make SQLite number the next event 1 again, which the archive holds."""

	_filed(world, "Feed the cat")
	_aged(world.session)

	newest = world.session.scalar(sqlalchemy.select(sqlalchemy.func.max(LIVE.c.seq)))
	_archived(world)

	assert world.session.scalars(sqlalchemy.select(LIVE.c.seq)).all() == [newest]


def test_a_cursor_behind_the_archive_is_expired_and_one_after_it_is_not (
	world: test_api_tasks.World,
) -> None:
	"""§5.11: a client resuming from a moved event is told to start again, not handed a page with a hole.

	**A cursor naming the last event moved is answered** (`SR#4293`, decision `#4305`): ``since`` is
	inclusive, so the client processed that event and nothing after it is lost. This test refused
	it until then, which is the spurious half of one number for the whole instance.
	"""

	_filed(world, "Fix the deploy script")
	_filed(world, "Collect the package from reception")
	_aged(world.session)
	_filed(world, "Take the red pill")
	archived = _archived(world)

	assert archived.through is not None

	refused = world.call("GET", "/v1/changes", params={"since": archived.through - 1})

	assert refused.status_code == 410, refused.text
	assert refused.json()["code"] == "cursor_expired"
	assert str(archived.through) in refused.json()["detail"], "it says how far the archive reaches"

	# The same refusal locally, where `clients.local` asks the same function.
	with pytest.raises(subroutine.errors.CursorExpired):
		subroutine.domain.events.refuse_an_expired_cursor(
			world.session, workspace_ids=[world.workspace.id], since=archived.through - 1
		)

	_aged(world.session, by=datetime.timedelta(seconds=2))

	at_the_floor = world.call("GET", "/v1/changes", params={"since": archived.through})

	assert at_the_floor.status_code == 200, at_the_floor.text
	answered = world.call("GET", "/v1/changes", params={"since": archived.through + 1})

	assert answered.status_code == 200, answered.text
	assert answered.json()["items"], "the feed after the archive is still read"


def test_a_period_the_feed_no_longer_holds_is_refused_naming_the_journal (
	world: test_api_tasks.World,
) -> None:
	"""`SR#4292`, M2 of the cold review of 2026-10-03, decision `#4305`.

	A period behind the floor answered ``200 []``, which reads as nothing having happened, while
	the journal read the same period; so did a walk back past the floor. Both are refused now,
	naming the journal, on both transports. **And the controls**: the journal reads it, and a
	period the feed still holds whole is answered as before.
	"""

	_filed(world, "Fix the deploy script")
	_aged(world.session)
	_filed(world, "Take the red pill")
	archived = _archived(world)
	_aged(world.session, by=datetime.timedelta(seconds=2))

	assert archived.through is not None

	then = (LONG_AGO + datetime.timedelta(days=1)).date().isoformat()

	for asked in (
		{"created_at.lt": then},
		{"created_at.lt": then, "newest": "true"},
		{"newest": "true", "before": str(archived.through + 1)},
	):
		refused = world.call("GET", "/v1/changes", params=asked)

		assert refused.status_code == 410, f"{asked} answered {refused.status_code}: {refused.text}"
		assert refused.json()["code"] == "period_archived" and "/v1/journal" in refused.text

	read = world.call("GET", "/v1/journal", params={"created_at.lt": then})

	assert read.status_code == 200 and read.json()["items"], read.text

	kept = world.call("GET", "/v1/changes", params={"created_at.gte": "yesterday"})

	assert kept.status_code == 200 and kept.json()["items"], kept.text

	local = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True),
		session_factory=api_support.factory_for(world.session),
	)

	with local, pytest.raises(subroutine.errors.PeriodArchived):
		local.changes(dated=[("created_at.lt", then)])


def test_a_quiet_workspaces_cursor_is_not_refused_for_what_moved_elsewhere (
	world: test_api_tasks.World,
) -> None:
	"""`SR#4293`, M13 of the cold review of 2026-10-03: one number for the whole instance.

	A quiet workspace's cursor on its own last event, with none of its own events moved after it,
	was refused because another workspace's events, numbered after it, had been.
	"""

	quiet = subroutine.domain.workspaces.create(
		world.session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Quiet", owner=world.user
	)
	world.session.flush()
	said = world.call("POST", "/v1/tasks", json={"title": "Water the plants", "workspace_id": quiet.slug})

	assert said.status_code == 201, said.text

	own = world.session.scalar(
		sqlalchemy.select(sqlalchemy.func.max(LIVE.c.seq)).where(LIVE.c.workspace_id == quiet.id)
	)

	def filed (title: str) -> None:
		"""File a task in the busy workspace, named now that there are two."""

		made = world.call(
			"POST", "/v1/tasks", json={"title": title, "workspace_id": str(world.workspace.slug)}
		)

		assert made.status_code == 201, made.text

	for title in ("Fix the deploy script", "Take the red pill", "Ring the dentist"):
		filed(title)

	# **Every event so far is old**, the quiet workspace's too: it moves with the busy one's, and
	# nothing of its own is moved after its last.
	_aged(world.session)
	filed("Feed the cat")
	archived = _archived(world)
	_aged(world.session, by=datetime.timedelta(seconds=2))

	assert archived.through is not None and own is not None and own < archived.through

	answered = world.call(
		"GET", "/v1/changes", params={"since": own, "workspace_id": quiet.slug}
	)

	assert answered.status_code == 200, f"refused for another workspace's events: {answered.text}"


def test_a_run_between_the_check_and_the_page_cannot_hide_rows (
	world: test_api_tasks.World, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#4293`, NEW-C-1 of the cold review of 2026-10-03: asked before the page, a run in between
	moved rows the page then never read, and nothing said so. Asked after it, the run is seen.
	"""

	first = _filed(world, "Fix the deploy script")
	_filed(world, "Take the red pill")
	_aged(world.session)
	_filed(world, "Ring the dentist")
	since = world.session.scalar(
		sqlalchemy.select(sqlalchemy.func.min(LIVE.c.seq)).where(LIVE.c.entity_id == uuid.UUID(first["id"]))
	)
	reading = subroutine.domain.events.page

	def raced (*arguments: typing.Any, **named: typing.Any) -> typing.Any:
		"""Let a retention run commit just before the page is read."""

		_archived(world)

		return reading(*arguments, **named)

	monkeypatch.setattr(subroutine.domain.events, "page", raced)
	answered = world.call("GET", "/v1/changes", params={"since": since})

	assert answered.status_code == 410, f"rows moved under the page unsaid: {answered.text}"


def test_a_run_moves_only_old_events_whatever_their_numbers (
	world: test_api_tasks.World,
) -> None:
	"""`SR#4296`, decision `#4305`: an old event above a recent one leaves the recent one live.

	A merge brings old events in at high numbers. A run moved everything at or below the highest
	old ``seq``, recent events included, emptying the feed of what happened before the merge.
	"""

	recent = _filed(world, "Ring the dentist")
	carried = _filed(world, "Set up the build")
	_filed(world, "Take the red pill")

	for event in world.session.scalars(sqlalchemy.select(subroutine.db.models.activity.Event)):
		if str(event.entity_id) == carried["id"]:
			event.created_at = LONG_AGO

	world.session.flush()
	_archived(world)

	entities: list[uuid.UUID] = list(world.session.scalars(sqlalchemy.select(LIVE.c.entity_id)))
	kept = {str(entity) for entity in entities}

	assert recent["id"] in kept, "a recent event moved because an old one had a higher number"
	assert carried["id"] in kept, "the old event was behind a recent one and is held with it"


def test_the_floor_never_goes_back_whatever_a_run_read (world: test_api_tasks.World) -> None:
	"""`SR#4295`, M15 of the cold review of 2026-10-03: a run that read the row before another moved
	further wrote its own lower number over it. Here the row is read, then moved past directly.
	"""

	for title in ("Fix the deploy script", "Take the red pill", "Ring the dentist"):
		_filed(world, title)

	system = typing.cast(sqlalchemy.Table, subroutine.db.models.system.Instance.__table__)
	held = world.session.scalar(sqlalchemy.select(subroutine.db.models.system.Instance))

	assert held is not None

	low = world.session.scalar(sqlalchemy.select(sqlalchemy.func.min(LIVE.c.seq)))
	world.session.connection().execute(sqlalchemy.update(system).values(events_archived_through=10**9))

	archived = subroutine.domain.retention.move(world.session, through=low)

	assert archived.moved == 1 and archived.through == 10**9, archived


def test_a_retention_too_long_for_the_calendar_moves_nothing_and_says_nothing_went_wrong (
	world: test_api_tasks.World, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#4295`: an overflow ended `db archive` in a traceback and killed the background thread.

	**And the thread says whatever stops it in the server's log**, which it did only for a database's
	own failures.
	"""

	_filed(world, "Fix the deploy script")
	now = subroutine.db.types.utcnow()

	assert subroutine.domain.retention.due(world.session, days=10**12, now=now) is None

	def broken (*_: typing.Any, **__: typing.Any) -> typing.Any:
		"""Fail as nothing this run expected would."""

		raise ValueError("the clock went backwards")

	monkeypatch.setattr(subroutine.domain.retention, "run", broken)
	keeper = subroutine.domain.retention.Keeper(days=30, factory=api_support.factory_for(world.session))

	with caplog.at_level("ERROR", logger="subroutine.retention"):
		keeper._archive(now)

	assert "stopped unexpectedly" in caplog.text and "the clock went backwards" in caplog.text


def test_an_event_committed_during_a_move_is_neither_copied_nor_lost (
	world: test_api_tasks.World,
) -> None:
	"""`SR#4295`: copied by number and deleted by range, an event landing in between was deleted.

	Played by taking one event out of the live table before the move and putting it back the
	moment the copy has run, which is what a writer committing then looks like to the move.
	"""

	for title in ("Fix the deploy script", "Take the red pill", "Ring the dentist"):
		_filed(world, title)

	world.session.flush()
	ordered = list(world.session.execute(sqlalchemy.select(LIVE).order_by(LIVE.c.seq)).mappings())
	late = dict(ordered[1])
	through = ordered[-2]["seq"]
	connection = world.session.connection()
	connection.execute(sqlalchemy.delete(LIVE).where(LIVE.c.seq == late["seq"]))

	done: list[bool] = []

	def landed (conn: typing.Any, clause: typing.Any, *_: typing.Any) -> None:
		"""Commit the late event just after the copy into the archive."""

		if getattr(clause, "table", None) is ARCHIVE and not done:
			done.append(True)
			conn.execute(sqlalchemy.insert(LIVE).values(**late))

	sqlalchemy.event.listen(connection, "after_execute", landed)

	try:
		subroutine.domain.retention.move(world.session, through=through)

	finally:
		sqlalchemy.event.remove(connection, "after_execute", landed)

	kept = world.session.scalar(
		sqlalchemy.select(sqlalchemy.func.count()).select_from(LIVE).where(LIVE.c.seq == late["seq"])
	)
	moved = world.session.scalar(
		sqlalchemy.select(sqlalchemy.func.count()).select_from(ARCHIVE).where(ARCHIVE.c.seq == late["seq"])
	)

	assert done, "the copy never ran, so nothing landed during it"
	assert (kept or 0) + (moved or 0) == 1, "the event that landed mid-move is in neither table"


def test_what_reads_history_still_reads_what_moved (world: test_api_tasks.World) -> None:
	"""Decision `#4233`: the feed's floor takes nothing from the record.

	An item's history and journal, *revised N times*, the ``touched_at`` filter, the period journal
	and an export, each asked about an item every event of which was moved.
	"""

	old = _filed(world, "Fix the deploy script")
	ref = old["ref"]

	for plan in ("Restart it by hand.", "Clear the cache first."):
		changed = world.call("PATCH", f"/v1/tasks/{ref}", json={"description": plan})

		assert changed.status_code == 200, changed.text

	_aged(world.session)
	newer = _filed(world, "Take the red pill")["ref"]
	_archived(world)

	assert _count(world.session, LIVE) > 0
	assert not world.session.scalar(
		sqlalchemy.select(sqlalchemy.func.count()).where(LIVE.c.entity_id == uuid.UUID(old["id"]))
	), "the item's events were meant to have moved"

	history = world.call("GET", f"/v1/tasks/{ref}/events").json()["items"]

	assert {item["action"] for item in history} >= {"created", "updated"}, history

	assert world.call("GET", f"/v1/tasks/{ref}/journal").json()["items"], "the item's journal"

	shown = world.call("GET", f"/v1/tasks/{ref}").json()

	assert shown["revisions"]["count"] == 1, shown["revisions"]

	before = (LONG_AGO + datetime.timedelta(days=1)).date().isoformat()
	touched = world.call("GET", "/v1/tasks", params={"touched_at.lt": before}).json()["items"]

	assert [item["ref"] for item in touched] == [ref], touched

	journal = world.call("GET", "/v1/journal", params={"created_at.lt": before}).json()["items"]
	named = {entry["item_ref"] for entry in journal}

	assert ref in named and newer not in named, journal

	exported = world.call("GET", "/v1/export/events").json()["items"]

	assert any(item["entity_id"] == old["id"] for item in exported), "an export left them behind"


def test_a_serving_instance_moves_them_in_the_background_at_most_once_a_day (
	session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Built only with a floor, and started by somebody using the instance - never by a timer."""

	ran: list[int] = []

	def counted (
		factory: typing.Any, *, days: int, now: datetime.datetime
	) -> subroutine.domain.retention.Archived:
		"""Stand in for the move, so this is about when it starts."""

		ran.append(days)

		return subroutine.domain.retention.Archived(moved=0, through=None)

	monkeypatch.setattr(subroutine.domain.retention, "run", counted)

	assert test_api_tasks._world(session).application.state.retention is None, (
		"an instance keeping every event built something that could move one"
	)

	world = test_api_tasks._world(session, instance={"events_retention_days": DAYS})
	keeper = world.application.state.retention

	assert isinstance(keeper, subroutine.domain.retention.Keeper)

	for _ in range(2):
		assert world.call("GET", "/v1/me").status_code == 200

		if keeper.moving is not None:
			keeper.moving.join(timeout=10)

	assert ran == [DAYS], "it should have run once, for the floor set"


def test_the_terminal_moves_them_on_demand_and_says_when_there_is_no_floor (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""``subroutine db archive``, for an instance nobody serves, or a timer."""

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(tmp_path / variable.lower()))

	runner = typer.testing.CliRunner()

	def run (*arguments: str) -> str:
		"""Run one command, which must succeed, and return what it printed."""

		result = runner.invoke(subroutine.cli.main.app, list(arguments))

		assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"

		return result.output

	run("init", "--workspace", "Metacortex")
	run("add", "Fix the deploy script")
	run("add", "Take the red pill")

	assert "every event stays in the change feed" in run("db", "archive")

	# **One day is a day** (`SR#4299`): it said *older than 1 days*, and *1 events*.
	monkeypatch.setenv("SUBROUTINE_EVENTS_RETENTION_DAYS", "1")

	assert "is older than 1 day, so nothing moved" in run("db", "archive")
	assert subroutine.cli.main._counted(1, "event") == "1 event"
	assert subroutine.cli.main._counted(1_200, "event") == "1,200 events"

	database = tmp_path / "xdg_data_home" / "subroutine" / "subroutine.db"
	engine = sqlalchemy.create_engine(f"sqlite:///{database}")

	try:
		with engine.begin() as connection:
			connection.execute(sqlalchemy.update(LIVE).values(created_at=LONG_AGO))

	finally:
		engine.dispose()

	monkeypatch.setenv("SUBROUTINE_EVENTS_RETENTION_DAYS", str(DAYS))

	assert "events older than 30 days" in run("db", "archive")
	assert "nothing moved" in run("db", "archive"), "a second run found something to move"
