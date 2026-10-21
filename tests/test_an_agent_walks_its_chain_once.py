"""An agent's request walks its accountability chain once, and a change to it is read at once - `SR#5174`.

**Decided in `#5097`**, "The accountability chain is walked once per request": every check an
agent's request asked walked the chain again, one fresh select per level, so measured over HTTP a
journal page cost a person 19-21 statements, their agent 38-47 and an agent's agent 71-72. The
guards in `tests/test_query_cost.py` ran as a person inside the test's own session, which already
held every row, and read the same for anybody. **Here each request has a session of its own with
nothing in it**, as the application makes one per request, and an agent and an agent's agent are
held to what their person asks, plus the one walk.
"""

import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.db.models.identity
import subroutine.domain.accountability
import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import test_api_tasks

User = subroutine.db.models.identity.User

#: What an agent may ask beyond its person: the one walk up its chain, a select per level.
PER_LEVEL = 1


def _agent (session: sqlalchemy.orm.Session, name: str, above: User) -> User:
	"""Make an agent that answers to ``above``, a person or another agent."""

	return subroutine.domain.users.create(
		session,
		username=f"{name}-{uuid.uuid4().hex[:6]}",
		is_service_account=True,
		responsible_user_id=above.id,
	)


def _chained (world: test_api_tasks.World) -> dict[str, str]:
	"""Give the world's person an agent and that agent one of its own, all three in its workspace,
	and return a secret for each, by how far down the chain it is."""

	agent = _agent(world.session, "drone", world.user)
	deeper = _agent(world.session, "probe", agent)
	secrets = {"person": world.secret}

	for name, account in (("agent", agent), ("agent's agent", deeper)):
		subroutine.domain.workspaces.add_member(
			world.session, world.workspace, account, role_key="member"
		)
		_row, issued = subroutine.domain.authentication.issue_token(
			world.session, user=account, title=f"{name}'s"
		)
		secrets[name] = issued.value.get_secret_value()

	world.session.flush()

	return secrets


def _asked (world: test_api_tasks.World, secret: str, path: str) -> int:
	"""Return how many statements one request sent, failing loudly if it was refused."""

	statements: list[str] = []

	def count (
		conn: typing.Any,
		cursor: typing.Any,
		statement: str,
		parameters: typing.Any,
		context: typing.Any,
		executemany: bool,
	) -> None:
		"""Note one statement the request sent."""

		statements.append(statement)

	bind = world.session.connection()
	sqlalchemy.event.listen(bind, "before_cursor_execute", count)

	try:
		answer = api_support.call(
			world.application, "GET", path, headers={"authorization": f"Bearer {secret}"}
		)

	finally:
		sqlalchemy.event.remove(bind, "before_cursor_execute", count)

	assert answer.status_code == 200, answer.text

	return len(statements)


def _busy (world: test_api_tasks.World) -> None:
	"""File work enough for a journal page and an agenda: each task changed, written on and due."""

	for number in range(12):
		made = world.call("POST", "/v1/tasks", json={"title": f"Water the plants, round {number}"})

		assert made.status_code == 201, made.text

		ref = made.json()["ref"]
		answers = [
			world.call(
				"PATCH", f"/v1/tasks/{ref}", json={"status": "in_progress", "due": "2020-01-01"}
			),
			world.call("POST", f"/v1/tasks/{ref}/comments", json={"body": "Started on it."}),
		]

		for answer in answers:
			assert answer.status_code in (200, 201), answer.text


@pytest.mark.parametrize("path", ["/v1/journal?limit=10", "/v1/agenda"])
def test_an_agent_asks_what_its_person_asks_plus_one_walk (
	session: sqlalchemy.orm.Session, path: str
) -> None:
	"""The journal's and the agenda's statement guards, for an agent and an agent's agent over HTTP."""

	world = test_api_tasks._world(session)
	_busy(world)
	secrets = _chained(world)

	# **Each credential used once first**: its first use records when it was used, which one used a
	# moment ago does not, so a fresh agent's credential would be charged a write its person's is not.
	for secret in secrets.values():
		_asked(world, secret, path)

	asked = {name: _asked(world, secret, path) for name, secret in secrets.items()}

	assert asked["agent"] <= asked["person"] + PER_LEVEL, asked
	assert asked["agent's agent"] <= asked["person"] + 2 * PER_LEVEL, asked


def test_an_agents_views_cost_what_its_persons_cost_per_view (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``saved.may`` asks each act's own refusal for each view, which no longer walks the chain.

	Each account lists views it saved itself, since a view is its saver's until it is shared.
	"""

	world = test_api_tasks._world(session)
	secrets = _chained(world)

	def saved (secret: str, count: int) -> None:
		"""Save ``count`` more views as whoever holds ``secret``."""

		for _number in range(count):
			body = {"title": f"Queue {uuid.uuid4().hex[:6]}", "arrangement": "board"}
			made = api_support.call(
				world.application,
				"POST",
				"/v1/views",
				json=body,
				headers={"authorization": f"Bearer {secret}"},
			)

			assert made.status_code == 201, made.text

	def listed (more: int) -> dict[str, int]:
		"""Have each account save ``more`` views, then count what listing its own asks."""

		for secret in secrets.values():
			saved(secret, more)

		return {name: _asked(world, secret, "/v1/views") for name, secret in secrets.items()}

	one = listed(1)
	four = listed(3)
	per_view = {name: (four[name] - one[name]) / 3 for name in secrets}

	assert per_view["agent"] <= per_view["person"], (one, four)
	assert per_view["agent's agent"] <= per_view["person"], (one, four)


def test_a_change_to_a_chain_is_read_by_the_next_check_in_the_same_session (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Transferring, deactivating and reactivating each forget the walk the session remembered."""

	accountability = subroutine.domain.accountability
	first = subroutine.domain.users.create(session, username=f"neo-{uuid.uuid4().hex[:6]}")
	second = subroutine.domain.users.create(session, username=f"trinity-{uuid.uuid4().hex[:6]}")
	agent = _agent(session, "drone", first)

	assert accountability.answers_for(session, agent).id == first.id

	subroutine.domain.users.transfer(session, agent, to=second)

	assert accountability.answers_for(session, agent).id == second.id

	subroutine.domain.users.set_active(session, second, active=False)

	assert not accountability.can_act(session, agent)

	subroutine.domain.users.set_active(session, second, active=True)

	assert accountability.can_act(session, agent)


def test_a_walk_undone_by_a_rollback_is_forgotten (session: sqlalchemy.orm.Session) -> None:
	"""A transfer rolled back leaves the walk as it was, not as the session last read it."""

	accountability = subroutine.domain.accountability
	first = subroutine.domain.users.create(session, username=f"neo-{uuid.uuid4().hex[:6]}")
	second = subroutine.domain.users.create(session, username=f"trinity-{uuid.uuid4().hex[:6]}")
	agent = _agent(session, "drone", first)
	session.flush()

	saved = session.begin_nested()
	agent.responsible_user_id = second.id

	assert accountability.answers_for(session, agent).id == second.id

	saved.rollback()

	assert accountability.answers_for(session, agent).id == first.id


def test_a_chain_broken_after_it_was_walked_is_refused_each_time (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A walk that raises is not remembered, so nothing stands in for the refusal."""

	accountability = subroutine.domain.accountability
	person = subroutine.domain.users.create(session, username=f"neo-{uuid.uuid4().hex[:6]}")
	agent = _agent(session, "drone", person)
	below = _agent(session, "probe", agent)

	assert accountability.answers_for(session, below).id == person.id

	agent.responsible_user_id = below.id

	for _attempt in range(2):
		with pytest.raises(subroutine.errors.ValidationError, match="runs in a circle"):
			accountability.chain(session, below)
