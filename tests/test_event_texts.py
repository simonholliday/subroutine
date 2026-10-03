"""A long description or body is kept once, and every reader is handed it whole - `#578`.

Decision `#4233`, with `#670` §4 as decided on `#1202`: an edit recorded the whole text before
and after it, so the history held two copies of a document per revision. The text is kept once per
workspace under its hash and the event carries the hash; what a reader is handed is unchanged.
"""

import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.event
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.db.models.work
import subroutine.domain.events
import subroutine.domain.tasks
import subroutine.errors
import subroutine.views
import test_api_tasks

#: Long enough to be kept once, and three of them, so a return to the first is a revert.
FIRST = "Restart the deploy by hand, then clear the cache. " * 8
SECOND = "Clear the cache first, then restart the deploy. " * 8

TEXTS = subroutine.db.models.activity.TEXTS
EVENT = subroutine.db.models.activity.Event


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def _described (world: test_api_tasks.World, ref: int, text: str) -> None:
	"""Give a task a description over HTTP."""

	changed = world.call("PATCH", f"/v1/tasks/{ref}", json={"description": text})

	assert changed.status_code == 200, changed.text


def _stored (world: test_api_tasks.World, entity_id: str) -> list[typing.Any]:
	"""Return each description change as stored, oldest first."""

	world.session.flush()
	world.session.expire_all()

	stored: list[typing.Any] = list(
		world.session.scalars(
			sqlalchemy.select(EVENT.changes)
			.where(EVENT.entity_id == uuid.UUID(entity_id), EVENT.action == "updated")
			.order_by(EVENT.seq)
		)
	)

	return [changes["description"] for changes in stored if changes and "description" in changes]


def test_a_long_text_is_kept_once_however_often_it_is_written (world: test_api_tasks.World) -> None:
	"""Three edits, two distinct texts: two rows kept, and the events carry hashes, not text."""

	made = world.call("POST", "/v1/tasks", json={"title": "Fix the deploy script"}).json()

	for text in (FIRST, SECOND, FIRST):
		_described(world, made["ref"], text)

	stored = _stored(world, made["id"])

	assert len(stored) == 3
	assert all(isinstance(side, dict) or side is None for change in stored for side in change.values())
	assert world.session.scalar(sqlalchemy.select(sqlalchemy.func.count()).select_from(TEXTS)) == 2


def test_a_kept_text_is_asked_for_by_its_workspace_so_its_key_serves_it (
	world: test_api_tasks.World,
) -> None:
	"""`SR#4294`, M14 of the cold review of 2026-10-03: by hash alone, the whole table was scanned.

	Measured with 60,000 texts: 11.5 ms by a scan on PostgreSQL and 13.5 ms on SQLite, against
	0.04 ms and 0.12 ms by the key. **Asserted on the statement rather than on a plan**, which a
	table this small would answer with a scan either way.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Fix the deploy script"}).json()
	_described(world, made["ref"], FIRST)

	for event in world.session.scalars(sqlalchemy.select(EVENT)):
		event.created_at = event.created_at.replace(year=event.created_at.year - 1)

	world.session.flush()
	asked: list[str] = []
	engine = world.session.get_bind()

	def heard (_connection: typing.Any, _cursor: typing.Any, statement: str, *_: typing.Any) -> None:
		"""Keep every statement that reads the kept texts."""

		if "event_text" in statement and statement.lstrip().upper().startswith("SELECT"):
			asked.append(" ".join(statement.split()))

	sqlalchemy.event.listen(engine, "before_cursor_execute", heard)

	try:
		assert world.call("GET", "/v1/changes").status_code == 200

	finally:
		sqlalchemy.event.remove(engine, "before_cursor_execute", heard)

	assert asked, "the feed never read a kept text, so this proves nothing"
	assert all("event_text.workspace_id IN" in one for one in asked), asked


def test_a_short_text_stays_where_it_was (world: test_api_tasks.World) -> None:
	"""Below the threshold a hash and a row cost more than the text does."""

	made = world.call("POST", "/v1/tasks", json={"title": "Feed the cat"}).json()
	_described(world, made["ref"], "Before nine.")

	assert _stored(world, made["id"]) == [{"from": None, "to": "Before nine."}]


def test_every_reader_is_handed_the_whole_text (world: test_api_tasks.World) -> None:
	"""The change feed, the item's history and an export, all as they always were."""

	made = world.call("POST", "/v1/tasks", json={"title": "Fix the deploy script"}).json()
	_described(world, made["ref"], FIRST)
	_described(world, made["ref"], SECOND)

	for event in world.session.scalars(sqlalchemy.select(EVENT)):
		event.created_at = event.created_at.replace(year=event.created_at.year - 1)

	world.session.flush()

	wanted = {"from": FIRST, "to": SECOND}
	feed = world.call("GET", "/v1/changes").json()["items"]
	history = world.call("GET", f"/v1/tasks/{made['ref']}/events").json()["items"]
	exported = world.call("GET", "/v1/export/events").json()["items"]

	for name, items in (("the feed", feed), ("the history", history), ("an export", exported)):
		said = [
			item["changes"]["description"]
			for item in items
			if item["changes"] and "description" in item["changes"]
		]

		assert wanted in said, f"{name} was not handed the whole text: {said}"

	shown = world.call("GET", f"/v1/tasks/{made['ref']}").json()

	assert shown["revisions"]["count"] == 1, "a rewrite still counts as one"


def test_one_text_kept_twice_in_one_transaction_is_one_row (session: sqlalchemy.orm.Session) -> None:
	"""The insert does nothing for a text already kept, rather than failing the write."""

	world = test_api_tasks._world(session)

	for title in ("Fix the deploy script", "Fix the deploy script again"):
		made = world.call("POST", "/v1/tasks", json={"title": title}).json()
		task = session.get(subroutine.db.models.work.Task, uuid.UUID(made["id"]))

		assert task is not None

		subroutine.domain.tasks.update(session, task, description=FIRST)

	session.flush()

	assert session.scalar(sqlalchemy.select(sqlalchemy.func.count()).select_from(TEXTS)) == 1


def test_an_event_shown_without_its_texts_is_refused_rather_than_published (
	world: test_api_tasks.World,
) -> None:
	"""`SR#4297`: a renderer handed rows nobody restored would have published a hash.

	No reader does that today. Both renderers refuse it, and render the whole text once the
	texts are put back.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Fix the deploy script"}).json()
	_described(world, made["ref"], FIRST)
	world.session.flush()
	world.session.expire_all()

	rows = list(
		world.session.scalars(
			sqlalchemy.select(EVENT).where(
				EVENT.entity_id == uuid.UUID(made["id"]), EVENT.action == "updated"
			)
		)
	)

	assert rows, "no event recorded the description"

	vocabulary = subroutine.views.Vocabulary(world.session)
	renders: tuple[typing.Callable[[], typing.Any], ...] = (
		lambda: subroutine.views.event(rows[0]),
		lambda: subroutine.views.journal_entry(rows[0], vocabulary=vocabulary),
	)

	for render in renders:
		with pytest.raises(subroutine.errors.InternalError):
			render()

	described = subroutine.domain.events.descriptions(world.session, rows)
	shown = subroutine.views.event(rows[0], described)

	assert shown.changes is not None and shown.changes["description"]["to"] == FIRST


def test_restored_puts_back_only_what_it_names () -> None:
	"""The pure half, which the merge script shares with the readers.

	The kept-texts migration's downgrade keeps a copy of its own (`SR#4297`), as a migration
	declares what it needs.
	"""

	kept = {"from": {"sha256": "a" * 64}, "to": "short"}
	found = subroutine.domain.events.restored(
		{"description": kept, "title": {"from": "a", "to": "b"}}, {"a" * 64: FIRST}.get
	)

	assert found == {"description": {"from": FIRST, "to": "short"}, "title": {"from": "a", "to": "b"}}
