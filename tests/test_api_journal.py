"""Reading what happened rather than what changed — item `#1430`, decision `#1429`.

`tests/test_api_changes.py` drives the audit log: raw, cheap, resumable, and what a client
polling should read. This drives the **join**, which is the whole of the difference — and the
measurement that produced it is worth restating, because every case here is one of its numbers.

One real day on this instance, 450 events: **130 of them `comment.created` carrying no body at
all**, 51 field-changes whose values were bare UUIDs, and an actor column that was a UUID on
every single row. So *"run me through what we did on Friday"* was answerable only by reading
every comment individually — which is not a feature, it is a list of things to go and look up.
"""

import datetime
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.domain.users
import test_api_tasks

#: Long enough ago that nothing here depends on the hour the suite runs at.
LONG_AGO = datetime.datetime(2026, 8, 1, 9, 0, tzinfo=datetime.UTC)

#: What somebody wrote, with enough in it that a truncation would be obvious.
SAID = "Reproduced on 3.11 only. The fix in the other one does not apply here."


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation with a history to read."""

	return test_api_tasks._world(session)


def _settled (session: sqlalchemy.orm.Session, *, seconds: int = 2) -> None:
	"""Move every event past the feed's watermark, which the journal inherits.

	The same helper `tests/test_api_changes.py` carries and for its reason: the rule under test
	is not *how* a row got to be a second old.
	"""

	shift = datetime.timedelta(seconds=seconds)

	for event in session.scalars(sqlalchemy.select(subroutine.db.models.activity.Event)):
		event.created_at = event.created_at - shift

	session.flush()


def _entries (world: test_api_tasks.World, **query: typing.Any) -> list[dict[str, typing.Any]]:
	"""Read the journal and return its entries, failing loudly on anything but a 200."""

	answered = world.call("GET", "/v1/journal", params=query)

	assert answered.status_code == 200, answered.text

	entries: list[dict[str, typing.Any]] = answered.json()["items"]

	return entries


def _a_task_with_a_comment (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> dict[str, typing.Any]:
	"""File a task, write on it, finish it — one small day of work to read back."""

	made = world.call("POST", "/v1/tasks", json={"title": "Fix the deploy script"})

	assert made.status_code == 201, made.text

	ref = made.json()["ref"]
	wrote = world.call("POST", f"/v1/tasks/{ref}/comments", json={"body": SAID})

	assert wrote.status_code == 201, wrote.text

	finished = world.call("POST", f"/v1/tasks/{ref}/complete")

	assert finished.status_code == 200, finished.text

	session.flush()
	_settled(session)

	return {"ref": ref, "comment": wrote.json()["id"]}


def test_the_journal_says_what_a_comment_actually_said (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""**The one thing the feed omits, and 29% of a real day's events are it** — `#1430`.

	A `comment.created` event names the comment as its entity and carries nothing about its
	contents, so the audit log reports only that somebody wrote something. That is correct for
	an audit and useless for a report, and it is the reason this route exists at all.
	"""

	_a_task_with_a_comment(world, session)

	said = [entry["said"] for entry in _entries(world, limit=200) if entry["said"]]

	assert SAID in said, f"the journal did not carry what was written: {said}"

	# **And the audit log still does not**, which is the other half of decision `#1429`: two
	# reads of one store, and only one of them joins. A journal that worked by changing what is
	# *written* would show up here.
	feed = world.call("GET", "/v1/changes", params={"limit": 200})

	assert feed.status_code == 200, feed.text
	assert SAID not in feed.text, (
		"the comment's body reached the audit log, so this was built by writing more rather "
		"than by joining — which is `#578`'s bug made worse on 29% of the feed"
	)


def test_a_journal_entry_names_who_did_it (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""`actor_user_id` is a UUID on every event, so a journal of them says a UUID did everything.

	Rendered through `principal_named`, which is the same function a row, a listing, `show` and
	the browser's roster use — `#1266`'s family, so an agent reads here exactly as it reads
	beside a task.
	"""

	_a_task_with_a_comment(world, session)

	actors = {entry["actor"] for entry in _entries(world, limit=200)}

	assert actors, "no entries at all, so this asserts nothing"
	assert f"@{world.user.username}" in actors, (
		f"the journal did not name who acted: {actors}"
	)

	# **Null is a real answer and not a failure to look**, which is why this is a subset rather
	# than an equality — and the first version of this test asserted equality and was wrong
	# about the design. `event.actor_user_id` is nullable precisely so an action the instance
	# took itself can say it had no person behind it, and bootstrapping a workspace is one:
	# `domain.bootstrap` runs before any principal exists, which is the single legitimate
	# `actor=None` in this codebase. A journal that invented a name for those would be
	# attributing work to somebody who did not do it.
	assert actors <= {f"@{world.user.username}", None}, (
		f"somebody other than the one account in this fixture is named: {actors}"
	)


def test_a_change_says_what_it_moved_between_and_not_which_rows (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""45 of 51 id-valued changes in one real day were `status_id` — `#1430`.

	`views.field_in_words` already maps the *column* to a phrase, so the feed can say *si changed
	how it is going*. **It does not resolve the values**, so what a reader is shown is
	`019fad98-431... -> 019fad98-431...`: two ids that are visibly different and mean nothing,
	on the single commonest change there is.
	"""

	_a_task_with_a_comment(world, session)

	moves = [
		change
		for entry in _entries(world, limit=200)
		for change in entry["changed"]
		if change["field"] == "status_id"
	]

	assert moves, "nothing changed status, so this asserts nothing"

	for move in moves:
		assert move["said"] == "how it is going", move
		assert move["before"] and move["after"], (
			f"a status change named neither side, so a reader cannot tell what happened: {move}"
		)
		assert move["before"] != move["after"], move


def test_no_journal_entry_ever_renders_an_identifier (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""**The guard for the whole design** — `#1430`, decision `#1429`.

	The rule taken is that a value we cannot name renders as **nothing** rather than as sixteen
	bytes of hex: the phrase alone — *changed how it repeats* — is shorter than a UUID and just
	as honest, where an id is noise a reader has to learn to skip.

	**So this catches a column nobody has declared a lookup for, on the day it is added**, which
	no test naming the columns we happen to resolve today could do. It is deliberately a
	property of the rendered answer rather than of `NAMED_BY`: a register can be complete and
	the renderer still leak, and the reader is who this is for.
	"""

	_a_task_with_a_comment(world, session)

	entries = _entries(world, limit=200)

	assert entries, "no entries at all, so this asserts nothing"

	shown = [
		value
		for entry in entries
		for value in [entry["actor"]]
		+ [side for change in entry["changed"] for side in (change["before"], change["after"])]
		if isinstance(value, str)
	]

	assert shown, "no values were rendered at all, so this could not fail"

	for value in shown:
		try:
			uuid.UUID(value)

		except ValueError:
			continue

		raise AssertionError(
			f"a journal entry rendered a bare identifier: {value!r}. A column with no lookup "
			f"in `domain.journal.NAMED_BY` must render its phrase and no value."
		)


def test_a_deleted_comment_is_absent_rather_than_quoted (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""Deletion is soft, so the body is still in the table — `#1430`.

	Without this the journal would be the one surface where a retracted paragraph is still
	readable, which is the same answer the mention index gives: a deleted comment stops
	mentioning anything, because a backlink to a sentence nobody can read is worse than none.
	"""

	made = _a_task_with_a_comment(world, session)
	removed = world.call("DELETE", f"/v1/comments/{made['comment']}")

	assert removed.status_code in (200, 204), removed.text

	session.flush()
	_settled(session)

	entries = _entries(world, limit=200)

	assert entries, "no entries at all, so this asserts nothing"
	assert not any(entry["said"] == SAID for entry in entries), (
		"a deleted comment is still quoted in the journal"
	)

	# **The entry itself stays**, which is the half that makes the assertion above mean
	# something rather than describing an empty answer: *somebody wrote and withdrew a comment*
	# is a real thing that happened, and an audit that dropped the row would be hiding it.
	assert any(entry["entity_type"] == "comment" for entry in entries), (
		"the whole entry vanished with the body, so the journal is now hiding that anything "
		"was written at all"
	)


def test_the_journal_takes_the_same_period_the_feed_does (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""One grammar, two routes — `#1431`, and the question Simon asked this for.

	*What did we do on Friday* is a period, and both readings of the store take it in the same
	spelling. Driven here as well as against the feed because the two compile it through
	different seams, and a route that declared no reader would answer 200 having ignored it.
	"""

	made = _a_task_with_a_comment(world, session)

	session.execute(
		sqlalchemy.update(subroutine.db.models.activity.Event).values(created_at=LONG_AGO)
	)
	session.flush()

	inside = _entries(world, limit=200, **{"created_at.gte": "2026-07-01"})
	outside = _entries(world, limit=200, **{"created_at.gte": "2026-08-15"})

	assert any(entry["item_ref"] == made["ref"] for entry in inside), (
		"a period containing the work found none of it"
	)
	assert not outside, f"a period after all of it found {len(outside)} entries"


def test_the_journal_says_what_it_covers (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""`#1085`'s rule, which the journal needs exactly as much as the feed.

	A credential narrowed away from a kind gets a journal of what it may read. Without this,
	*nothing happened on Friday* and *I am not shown that* are the same sentence — and the
	second is the one somebody would act on.
	"""

	_a_task_with_a_comment(world, session)

	answered = world.call("GET", "/v1/journal", params={"limit": 5})

	assert answered.status_code == 200, answered.text
	assert answered.json()["covers"], "the journal does not say which kinds it is a journal of"
