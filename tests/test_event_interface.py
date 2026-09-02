"""An event says which door the request came in through — `SR#1415`, decision `SR#1426`.

**The guard here is the audit, not the columns**, and `SR#1426` §6 says why: *a test asserting
three fields are non-null would pass on a version that recorded the same origin for everybody.*
So the driven tests below touch one item through more than one door and read the item's own
event list back, which is the thing an operator would actually look at.

**What makes this fact worth recording at all is that nobody asserts it.** A credential's name
is typed once by a human and never changes; a client's name is announced by the program on
every connection. Both are claims. The door is observed — so it is what an audit can lean on
when the other two are disputed, and it is the one thing here that cannot be misreported.

**It is never an authorisation input.** ``authenticate`` already checks revocation, expiry,
activity and the whole accountability chain on every request (`SR#1395`); nothing in this file
should ever grow an assertion that a door decides what somebody may do.
"""

import json
import typing

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.api.mcp
import subroutine.api.security
import subroutine.db.models.activity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.events
import subroutine.domain.local
import subroutine.domain.sessions
import subroutine.domain.tasks
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def _interfaces_on (session: sqlalchemy.orm.Session, entity_id: typing.Any) -> list[str | None]:
	"""Return the door recorded against every event about one item, oldest first."""

	model = subroutine.db.models.activity.Event

	return list(
		session.scalars(
			sqlalchemy.select(model.actor_interface)
			.where(model.entity_id == entity_id)
			.order_by(model.seq)
		)
	)


def _inbox (
	session: sqlalchemy.orm.Session, world: test_api_tasks.World
) -> subroutine.db.models.project.Project:
	"""Return the workspace's Inbox, which is where a task with no project goes."""

	model = subroutine.db.models.project.Project

	return session.scalars(
		sqlalchemy.select(model).where(
			model.workspace_id == world.workspace.id, model.key == "inbox"
		)
	).one()


def test_one_item_touched_through_two_doors_says_which_was_which (
	world: test_api_tasks.World,
) -> None:
	"""The audit `SR#1426` §6 asks for: two doors, one item, and the history tells them apart.

	**This is the assertion that a version recording one value for everybody would fail.** A
	test that only checked the column is populated would pass on exactly that version, which
	is why the decision names this shape rather than the field.

	The same credential is presented at both doors on purpose. What is being separated here is
	the *door*, not the caller — so holding the caller constant is what makes the reading mean
	only one thing.
	"""

	created = world.call("POST", "/v1/tasks", json={"text": "Touched from two places"})

	assert created.status_code == 201, created.text
	ref = created.json()["ref"]

	answered = world.call(
		"POST",
		subroutine.api.mcp.PATH,
		content=json.dumps(
			{
				"jsonrpc": "2.0",
				"id": 1,
				"method": "tools/call",
				"params": {
					"name": "subroutine_update",
					"arguments": {"ref": ref, "title": "Touched from two places, twice"},
				},
			}
		),
		headers={"content-type": "application/json"},
	)

	assert answered.status_code == 200, answered.text

	entity_id = world.session.scalars(
		sqlalchemy.select(subroutine.db.models.work.Task.id).where(
			subroutine.db.models.work.Task.ref == ref
		)
	).one()

	recorded = _interfaces_on(world.session, entity_id)

	assert recorded[0] == subroutine.domain.authentication.API, (
		"the create arrived at /v1 and is the API"
	)
	assert subroutine.domain.authentication.MCP in recorded, (
		"and the update arrived at /mcp, which the history has to be able to say"
	)

	# The point of the whole item, stated as the property rather than as two values: one item,
	# one credential, and the record still distinguishes where each write came from.
	assert len(set(recorded)) > 1, (
		f"two doors were used and the history recorded {recorded} — a single value here is a "
		f"version that writes the same interface for everybody, which is what `#1426` §6 "
		f"warns a column-shaped test cannot see"
	)


def test_a_browser_write_is_recorded_as_a_browser (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A session cookie is its own door, and it needs no transport to say so.

	``domain/sessions.authenticate`` builds the principal, so the value is a literal at the one
	place that knows it — which is why three of the five doors need nothing threaded through
	from the request at all.
	"""

	world = test_api_tasks._world(session)

	_link, secret = subroutine.domain.sessions.mint_link(session, user=world.user)
	_opened, cookie = subroutine.domain.sessions.redeem(session, secret)

	principal = subroutine.domain.sessions.authenticate(session, cookie)

	assert principal.interface == subroutine.domain.authentication.BROWSER
	assert principal.session is not None, "and it really is the session case"

	made = subroutine.domain.tasks.create(
		session,
		project=_inbox(session, world),
		title="Written from a browser",
		actor=principal,
	)
	session.flush()

	assert _interfaces_on(session, made.id) == [subroutine.domain.authentication.BROWSER]


def test_a_local_caller_is_recorded_as_local (session: sqlalchemy.orm.Session) -> None:
	"""No credential at all is a positive fact, not an absence — §12.1a.

	It is the most privileged path in the product, because §12.4's recovery property depends on
	it working when the service will not, so it is the origin an operator most wants named
	afterwards. Recording it as null would have made it indistinguishable from a system write.
	"""

	world = test_api_tasks._world(session)
	principal = subroutine.domain.local.principal(session)

	assert principal.interface == subroutine.domain.authentication.LOCAL
	assert principal.is_local, "and it really is the no-credential case"

	made = subroutine.domain.tasks.create(
		session,
		project=_inbox(session, world),
		title="Written at the machine",
		actor=principal,
	)
	session.flush()

	assert _interfaces_on(session, made.id) == [subroutine.domain.authentication.LOCAL]


def test_a_write_with_no_principal_says_nothing_rather_than_local (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Null is *nobody said*; ``local`` is a claim that somebody was at the machine.

	**The distinction this test exists for is easy to lose.** Seeding, a migration's data fix
	and ``subroutine init`` all write with no principal, and they are not somebody at a
	terminal — so collapsing the two would put a positive claim about §12.1a's most privileged
	path onto every row a migration ever touched. It is also what an event recorded before
	this shipped says, which is the same answer for the same reason.
	"""

	world = test_api_tasks._world(session)

	recorded = subroutine.domain.events.record(
		session,
		workspace_id=world.workspace.id,
		entity_type="workspace",
		entity_id=world.workspace.id,
		action="workspace.probed",
	)
	session.flush()

	assert recorded.actor_interface is None
	assert recorded.actor_interface != subroutine.domain.authentication.LOCAL


def test_the_interface_reaches_the_reader_of_an_item_s_history (
	world: test_api_tasks.World,
) -> None:
	"""`GET /v1/tasks/{ref}/events` reports it, so the audit is readable without a new view.

	**Reported rather than held back, and the reason is the field beside it**: this response
	already names the *credential* that was presented, which says strictly more about a
	colleague than naming a door does. Who may see an event at all is unchanged — that is
	``scoping.visible_events``.
	"""

	created = world.call("POST", "/v1/tasks", json={"text": "Read my own history"})

	assert created.status_code == 201, created.text

	history = world.call("GET", f"/v1/tasks/{created.json()['ref']}/events")

	assert history.status_code == 200, history.text

	doors = [event["actor_interface"] for event in history.json()["items"]]

	assert doors and all(door == subroutine.domain.authentication.API for door in doors), (
		f"a request made at /v1 should read back as the API, and this said {doors}"
	)


def test_the_mcp_path_this_module_matches_is_the_one_the_app_mounts () -> None:
	"""The literal in ``api/security.py`` is the path ``api/mcp.py`` actually serves.

	**A copy something compares is a copy that cannot rot.** ``api/mcp.py`` imports
	``api/security.py``, so reading the constant there directly would be an import cycle — and
	a path spelled twice with nothing holding the two together is this codebase's signature
	defect. A test can import both, so it does.

	Without this, moving the endpoint would leave every MCP request quietly recorded as having
	arrived at the API door: no error, no failing request, and an audit trail that is wrong in
	the one direction nobody would think to check.
	"""

	assert subroutine.api.security.MCP_PATH == subroutine.api.mcp.PATH


def test_a_principal_refuses_a_door_that_disagrees_with_its_credential (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A stated interface is ours to observe, so it may not contradict what was presented.

	**Otherwise it becomes a claim like any other**, quietly, in the audit trail, where nothing
	else would ever compare the two. The token case is deliberately absent: a bearer token
	really does arrive at two doors, which is the whole asymmetry that makes ``authenticate``
	take the interface as an argument.
	"""

	world = test_api_tasks._world(session)

	_link, secret = subroutine.domain.sessions.mint_link(session, user=world.user)
	opened, _cookie = subroutine.domain.sessions.redeem(session, secret)

	with pytest.raises(ValueError, match="there is no request"):
		subroutine.domain.authentication.Principal(
			user=world.user,
			session=opened,
			interface=subroutine.domain.authentication.LOCAL,
		)

	with pytest.raises(ValueError, match="browser session"):
		subroutine.domain.authentication.Principal(
			user=world.user, interface=subroutine.domain.authentication.BROWSER
		)

	with pytest.raises(ValueError, match="authenticates with a token"):
		subroutine.domain.authentication.Principal(
			user=world.user, interface=subroutine.domain.authentication.API
		)

	with pytest.raises(ValueError, match="not a door"):
		subroutine.domain.authentication.Principal(user=world.user, interface="carrier-pigeon")


def test_every_door_the_product_can_record_is_one_the_register_declares () -> None:
	"""``INTERFACES`` is the set, and nothing may reach an event that is not in it.

	Derived from the module's own constants rather than written out again, so a sixth door
	added tomorrow is covered by the register the day it exists — and a constant added and
	left out of :data:`INTERFACES` fails here rather than silently escaping every check that
	reads the set.
	"""

	authentication = subroutine.domain.authentication
	declared = {
		value
		for name, value in vars(authentication).items()
		if name.isupper() and isinstance(value, str) and name in
		{"MCP", "API", "BROWSER", "FEED", "LOCAL"}
	}

	assert declared == set(authentication.INTERFACES), (
		"a door is declared as a constant and is not in INTERFACES, so every guard that reads "
		"the set is blind to it"
	)

	# Sixteen characters is what the column takes; a longer one would be truncated or refused
	# depending on the backend, which is not a difference anybody should have to discover.
	assert all(len(door) <= 16 for door in authentication.INTERFACES)


def test_an_agent_holding_a_local_token_is_still_local (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A credential narrows what a caller may do; it does not give them a door — `SR#1415`.

	**This is the branch that would otherwise have recorded nothing, and it is the commonest
	agent setup rather than an edge case.** An agent given its own identity on a personal
	machine holds ``SUBROUTINE_TOKEN_LOCAL`` and reaches the database through
	``clients/local.py``, which opens the file directly. `SR#1426` §2's definition of local is
	*there is no request at all*, and that is still true with a token in hand.

	**It also falsifies the first version of the coherence check**, which read
	:attr:`Principal.is_local` as the definition of the door and refused this exact state — a
	person at the terminal recording ``local`` while their own agent, two feet away, recorded
	nothing at all.
	"""

	world = test_api_tasks._world(session)
	principal = subroutine.domain.local.principal(session, token=world.secret)

	assert principal.token is not None, "the token really was accepted"
	assert not principal.is_local, "so this is not the no-credential case"
	assert principal.interface == subroutine.domain.authentication.LOCAL, (
		"and the door is still local, because there was no request"
	)

	made = subroutine.domain.tasks.create(
		session,
		project=_inbox(session, world),
		title="Written by an agent at the machine",
		actor=principal,
	)
	session.flush()

	assert _interfaces_on(session, made.id) == [subroutine.domain.authentication.LOCAL]
