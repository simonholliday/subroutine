"""Who is holding an item, and who acted — the reads that make a handover a loop (`#1120`).

Three claim columns and an actor have been stored and reported on every row since the lease
was built, and reachable by no filter and no sort. So the claim discipline — four commands
around every piece of work — could be followed and not *seen*: an agent could not ask what it
was holding, and a person could not ask what their agent had done.

**One absence with three faces**, which is why they are one module: `?claimed_by=` on a
listing, `claimed_at` in the sortable and filterable sets, and `?actor=<username>` on the
change feed. Every one is a read on §14's hand-work-over-and-find-out-what-came-of-it loop.
"""

import datetime
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.claims
import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_changes
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP."""

	return test_api_tasks._world(session)


def _task (world: test_api_tasks.World, title: str = "The work") -> dict[str, typing.Any]:
	"""Make a task."""

	response = world.call("POST", "/v1/tasks", json={"title": title})

	assert response.status_code == 201, response.text

	return typing.cast(dict[str, typing.Any], response.json())


def _claim (world: test_api_tasks.World, ref: int) -> dict[str, typing.Any]:
	"""Take a lease on one."""

	response = world.call("POST", f"/v1/tasks/{ref}/claim")

	assert response.status_code in (200, 201), response.text

	return typing.cast(dict[str, typing.Any], response.json())


def _refs (world: test_api_tasks.World, **query: typing.Any) -> list[int]:
	"""List tasks and return their refs."""

	response = world.call("GET", "/v1/tasks", params=query)

	assert response.status_code == 200, response.text

	return [one["ref"] for one in response.json()["items"]]


def _lease_ends (
	session: sqlalchemy.orm.Session, task: dict[str, typing.Any], *, minutes: int
) -> None:
	"""Move a task's lease expiry, in SQL rather than through the loaded object.

	`claims.claim` writes with a bare ``UPDATE`` — the condition has to live in the ``WHERE``
	clause or two workers both take the task (`#354`) — so the object a test is holding is
	stale afterwards and assigning to it raises ``StaleDataError`` rather than doing anything.
	"""

	session.execute(
		sqlalchemy.update(subroutine.db.models.work.Task)
		.where(subroutine.db.models.work.Task.id == uuid.UUID(task["id"]))
		.values(
			claim_expires_at=subroutine.db.types.utcnow()
			+ datetime.timedelta(minutes=minutes)
		)
	)
	session.flush()
	session.expire_all()


def test_a_listing_narrows_to_what_one_account_is_holding (
	world: test_api_tasks.World,
) -> None:
	"""The question an agent following the claim discipline could not ask about itself."""

	held = _task(world, "Being worked on")
	_task(world, "Nobody has this")
	_claim(world, held["ref"])

	assert _refs(world, claimed_by="me") == [held["ref"]]
	assert _refs(world, claimed_by=world.user.username) == [held["ref"]]


def test_an_expired_claim_is_not_being_held (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""§10.7 invariant 10: an expired claim is treated as absent rather than cleaned up.

	This is the reading `readiness` already takes, and taking a second one here would mean
	*what am I holding* answered with work the lease has already offered to somebody else —
	which is the whole failure a lease exists to prevent, reported as a fact.
	"""

	held = _task(world, "Taken and forgotten")
	_claim(world, held["ref"])

	assert _refs(world, claimed_by="me") == [held["ref"]]

	_lease_ends(session, held, minutes=-1)

	assert _refs(world, claimed_by="me") == []
	assert world.call("GET", f"/v1/tasks/{held['ref']}").json()["claimed_by"] is not None, (
		"the row still records who took it, which is the history; it is the *holding* that ended"
	)


def test_a_listing_sorts_by_when_a_claim_was_taken (world: test_api_tasks.World) -> None:
	"""*What has been held longest* is the question a person asks when an agent goes quiet."""

	first = _task(world, "Taken first")
	second = _task(world, "Taken second")
	_task(world, "Never taken")
	_claim(world, first["ref"])
	_claim(world, second["ref"])

	assert _refs(world, order="claimed_at")[:2] == [first["ref"], second["ref"]]
	assert _refs(world, order="-claimed_at")[:2] == [second["ref"], first["ref"]]

	# **Unclaimed last in both directions**, which is `estimate_minutes`' rule rather than an
	# analogy to it: ascending is *held longest* and an unclaimed task has not been held at
	# all; descending is *taken most recently* and it was not taken.
	assert _refs(world, order="claimed_at")[-1] == _refs(world, order="-claimed_at")[-1]


def test_a_listing_narrows_by_when_a_claim_was_taken (world: test_api_tasks.World) -> None:
	"""The measurement this item opened with: `?claimed_at.gte=` answered 422."""

	held = _task(world, "Being worked on")
	_task(world, "Nobody has this")
	_claim(world, held["ref"])

	assert _refs(world, **{"claimed_at.gte": "today"}) == [held["ref"]]
	assert _refs(world, **{"claimed_at.lt": "today"}) == []


def test_the_change_feed_narrows_to_what_one_account_did (
	session: sqlalchemy.orm.Session,
) -> None:
	"""*What did it do while I was away* — the human's commonest question about the record.

	On this project's own instance the person writes 2.2% of the events, so nearly everything
	worth asking about is somebody else's — and `?actor=me` answers only about the credential
	asking, which is the acts you already know about.
	"""

	world = test_api_tasks._world(session)
	agent = subroutine.domain.users.create(session, username=f"agent-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, agent, role_key="member")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=agent, title="the agent"
	)
	session.flush()
	theirs = world._replace(secret=issued.value.get_secret_value())

	mine = _task(world, "What the person filed")
	yours = _task(theirs, "What the agent filed")

	# **Back-dated rather than slept past**, which is `tests/test_api_changes.py`'s own device:
	# the feed withholds the last second so that a `seq` allocated at insert cannot be paged
	# over before it is visible at commit.
	test_api_changes._settled(session)

	def acted (caller: test_api_tasks.World, **query: typing.Any) -> set[str]:
		"""Return the ids of the tasks whose events this narrowing returns."""

		response = caller.call("GET", "/v1/changes", params=query)

		assert response.status_code == 200, response.text

		return {
			one["entity_id"]
			for one in response.json()["items"]
			if one["entity_type"] == "task"
		}

	assert acted(world, actor=agent.username) == {yours["id"]}
	assert acted(world, actor=world.user.username) == {mine["id"]}

	# **And `me` still means this credential**, which is the finer grain and the one that is
	# only useful about yourself: an account may hold several, and nobody knows another
	# credential's id.
	assert acted(theirs, actor="me") == {yours["id"]}

	# **The unnarrowed feed still holds both**, which is what says the two narrowings above
	# were narrowings rather than a feed that happened to be empty. An absence two behaviours
	# produce is not evidence for either.
	assert acted(world) == {mine["id"], yours["id"]}


def test_an_account_that_does_not_exist_is_refused_by_name (
	world: test_api_tasks.World,
) -> None:
	"""And the refusal lists who there is, which the enumerated one it replaces could not."""

	refused = world.call("GET", "/v1/changes", params={"actor": "nobody-here"})

	# **404 rather than the 422 this parameter used to answer**, and the change is required
	# rather than incidental: the old refusal said *'actor' takes 'me' or nothing*, which is
	# now false. What answers instead is the selector every other "who" here goes through, so
	# `?actor=` and `?assignee=` refuse an unknown account the same way and with the same hint.
	assert refused.status_code == 404, refused.text
	assert "nobody-here" in refused.text, refused.text

	held = world.call("GET", "/v1/tasks", params={"claimed_by": "nobody-here"})

	assert held.status_code == 404, held.text


def test_working_on_what_you_hold_keeps_the_lease_alive (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""`#1113`. The lease default is 30 minutes and the work is measured in hours.

	`#1091` is the worked example: claimed at 18:42, expired at 19:12, finished at 19:22, and
	nothing said so. A longer default trades one wrong answer for another — it is still a
	guess about how long work takes, and it strands a dead worker's claim for longer. Renewing
	on activity is deterministic: the worker that stopped stops renewing.

	**Driven from a lease about to run out**, which is the state the defect produced: a claim
	is expired for most of its life, so the interesting write is the one that lands while a
	minute is left.
	"""

	held = _task(world, "Long work")
	_claim(world, held["ref"])
	_lease_ends(session, held, minutes=1)

	nearly = world.call("GET", f"/v1/tasks/{held['ref']}").json()["claim_expires_at"]

	world.call("PATCH", f"/v1/tasks/{held['ref']}", json={"importance": 3})
	after = world.call("GET", f"/v1/tasks/{held['ref']}").json()["claim_expires_at"]

	assert after > nearly, "a write by the holder did not push the lease out"


def test_an_expired_lease_is_not_brought_back_by_a_write (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""It renews a lease; it does not resurrect one.

	An expired claim is treated as absent (§10.7 invariant 10), which is what makes the work
	available to somebody else — so a write by the last holder must not silently take it back
	from whoever the listing has already offered it to.
	"""

	held = _task(world, "Abandoned")
	_claim(world, held["ref"])
	_lease_ends(session, held, minutes=-1)

	assert _refs(world, claimed_by="me") == [], "the lease has not expired, so nothing is proved"

	world.call("PATCH", f"/v1/tasks/{held['ref']}", json={"importance": 3})

	assert _refs(world, claimed_by="me") == []


def test_a_write_by_somebody_else_does_not_move_the_lease (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Writing to what somebody else holds is allowed and must not quietly take it.

	`release` says why anybody with ``task:write`` may act on held work: the case it exists
	for is a worker that died. What must not happen is the lease silently changing hands, or
	the holder's own expiry being extended by an act that was not theirs.
	"""

	world = test_api_tasks._world(session)
	other = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, other, role_key="member")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=other, title="the other one"
	)
	session.flush()
	theirs = world._replace(secret=issued.value.get_secret_value())

	held = _task(world, "Mine")
	_claim(world, held["ref"])
	_lease_ends(session, held, minutes=1)

	shortened = world.call("GET", f"/v1/tasks/{held['ref']}").json()
	changed = theirs.call("PATCH", f"/v1/tasks/{held['ref']}", json={"importance": 4})

	assert changed.status_code == 200, changed.text

	after = world.call("GET", f"/v1/tasks/{held['ref']}").json()

	assert after["claim_expires_at"] == shortened["claim_expires_at"], (
		"somebody else's write moved the lease"
	)
	assert after["claimed_by_id"] == shortened["claimed_by_id"], (
		"somebody else's write took the lease"
	)


def test_a_write_to_something_nobody_holds_takes_no_lease (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""Taking work is an act. A renewal is a side effect, and must not become one."""

	free = _task(world, "Unclaimed")
	world.call("PATCH", f"/v1/tasks/{free['ref']}", json={"importance": 2})
	after = world.call("GET", f"/v1/tasks/{free['ref']}").json()

	assert after["claimed_by_id"] is None
	assert after["claim_expires_at"] is None


def test_finishing_gives_the_lease_back (world: test_api_tasks.World) -> None:
	"""`#1113`, and it is `#726`'s reasoning applied to the arrow it did not consider.

	`#726` settled that *releasing* must not set a status, because release has four
	destinations and cannot tell them apart. **None of its four cases is this one.** Once the
	status is finished the lease protects nothing: the task is not startable by anybody, and a
	name on the row saying somebody is holding it is simply false.

	Measured on the live instance when this was filed: about thirty tasks carrying a claim by
	the agent, most of them on work that was finished and shipped.
	"""

	held = _task(world, "Nearly done")
	_claim(world, held["ref"])

	assert _refs(world, claimed_by="me") == [held["ref"]]

	world.call("POST", f"/v1/tasks/{held['ref']}/complete")
	finished = world.call("GET", f"/v1/tasks/{held['ref']}").json()

	assert finished["claimed_by_id"] is None, "finishing left the claim behind"
	assert finished["claim_expires_at"] is None


def test_cancelling_gives_the_lease_back_too (world: test_api_tasks.World) -> None:
	"""Wider than the item asked for, and the same argument.

	§10.7 invariant 5 makes `completed_at` non-null for a `done` *and* a `cancelled` status,
	and a cancelled task is exactly as unstartable as a completed one. `skip` cancels an
	occurrence of a repeat, so an agent skipping one should not be left holding it either.
	"""

	held = _task(world, "Not going to happen")
	_claim(world, held["ref"])
	world.call("PATCH", f"/v1/tasks/{held['ref']}", json={"status": "cancelled"})

	assert world.call("GET", f"/v1/tasks/{held['ref']}").json()["claimed_by_id"] is None


def test_finishing_something_somebody_else_holds_gives_their_lease_back (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The lease is about the work, not about who ends it.

	Anybody with ``task:write`` may release, for the reason `release` gives — the case it
	exists for is a worker that died. Completing somebody else's held task is the same
	situation arriving by a different verb, and leaving the lease behind would put a holder's
	name on finished work nobody is doing.
	"""

	world = test_api_tasks._world(session)
	other = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, other, role_key="member")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=other, title="the other one"
	)
	session.flush()
	theirs = world._replace(secret=issued.value.get_secret_value())

	held = _task(world, "Handed over unfinished")
	_claim(world, held["ref"])
	theirs.call("POST", f"/v1/tasks/{held['ref']}/complete")

	assert world.call("GET", f"/v1/tasks/{held['ref']}").json()["claimed_by_id"] is None


def test_finishing_twice_records_one_release (world: test_api_tasks.World) -> None:
	"""Idempotent, because `release` returns early when nobody holds it.

	A retry is ordinary — `#723` is the item about completion being made idempotent — and a
	second release event about a lease that was already given back would be noise in the one
	place the record is meant to be read.
	"""

	held = _task(world, "Finished twice")
	_claim(world, held["ref"])
	world.call("POST", f"/v1/tasks/{held['ref']}/complete")
	world.call("POST", f"/v1/tasks/{held['ref']}/complete")

	history = world.call("GET", f"/v1/tasks/{held['ref']}/events").json()["items"]
	released = [one for one in history if one["action"] == "released"]

	assert len(released) == 1, f"{len(released)} releases for one finish"


def test_commenting_on_what_you_hold_keeps_the_lease_alive (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""The other half of *activity*, and the half a rule about writes alone would miss.

	Writing about the work is working on it — and it is what an agent does most of on this
	instance, where a comment is where the running record lives (§5.10). A renewal that
	counted only edits to the row would leave somebody who spent forty minutes writing up what
	they found holding an expired lease.
	"""

	held = _task(world, "Being written up")
	_claim(world, held["ref"])
	_lease_ends(session, held, minutes=1)

	nearly = world.call("GET", f"/v1/tasks/{held['ref']}").json()["claim_expires_at"]
	world.call(
		"POST", f"/v1/tasks/{held['ref']}/comments", json={"body": "Reproduced on 3.11 only."}
	)

	assert world.call("GET", f"/v1/tasks/{held['ref']}").json()["claim_expires_at"] > nearly


def test_commenting_on_something_nobody_holds_takes_no_lease (
	world: test_api_tasks.World,
) -> None:
	"""And a comment on a document or a project reaches no claim at all, having none."""

	free = _task(world, "Unclaimed")
	world.call("POST", f"/v1/tasks/{free['ref']}/comments", json={"body": "A note."})

	assert world.call("GET", f"/v1/tasks/{free['ref']}").json()["claimed_by_id"] is None

	made = world.call(
		"POST", "/v1/documents", json={"title": "A document", "body": "Words."}
	).json()
	commented = world.call(
		"POST", f"/v1/documents/{made['ref']}/comments", json={"body": "A note."}
	)

	assert commented.status_code == 201, commented.text
