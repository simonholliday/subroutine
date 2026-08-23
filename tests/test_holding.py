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

	row = session.get(subroutine.db.models.work.Task, uuid.UUID(held["id"]))

	assert row is not None
	row.claim_expires_at = subroutine.db.types.utcnow() - datetime.timedelta(minutes=1)
	session.flush()

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
