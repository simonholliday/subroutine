"""The user endpoints, driven over HTTP by the client that is supposed to reach them — `#492`.

**Nothing drove `/v1/users` at all until this file.** Measured on 2026-08-04: the only mentions
of that path anywhere in ``tests/`` were the three entries in ``test_reach``'s own maps, which
assert a client *method of that name exists* and cannot assert that calling it works.

It did not work. ``set_active`` and ``transfer_agent`` — the two halves of decision `#473`'s
leaver path, added together for `#475` and `#478` — both passed ``body=`` to ``_json``, which
forwards to ``httpx.Client.request``, where the keyword is ``json=``. Every one of the other
calls in ``clients/http.py`` gets it right. So both methods raised ``TypeError`` before the
request left the process, on the *only* instance that exists, since the day they shipped.

**The lesson is about where the gap was, not about the keyword.** A guard that maps a route to a
method name reads as coverage and is not: it is satisfied by a method that cannot run. The tests
here drive the success path deliberately, because a refusal proves only that the request was
well-formed enough to be refused — it never reaches ``model_validate`` on the answer.
"""

import uuid

import pytest
import sqlalchemy.orm

import api_support
import subroutine.clients.http
import subroutine.connections
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.users
import subroutine.errors
import subroutine.views


def _instance (
	session: sqlalchemy.orm.Session,
) -> tuple[subroutine.db.models.identity.User, str]:
	"""Bring an instance into being and return its first person with a credential."""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Theirs"
	)
	session.flush()

	return setup.user, issued.value.get_secret_value()


def _over_http (
	session: sqlalchemy.orm.Session, token: str
) -> subroutine.clients.http.Client:
	"""Return the real HTTP client, driving the real application against this database."""

	return subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url="https://example.com"),
		token=token,
		transport=api_support.SyncTransport(api_support.build_app(api_support.factory_for(session))),
		base_url=api_support.BASE_URL,
	)


def test_somebody_can_be_marked_as_having_left_over_http (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#492`. This raised ``TypeError`` before the request left, for every caller.

	The assertion is on the *parsed answer* rather than on the database, because the defect
	sat between the two: a test reading the row back would have passed with the response
	discarded, and the response is what a client returns to whoever asked.
	"""

	person, token = _instance(session)
	leaver = subroutine.domain.users.create(
		session, username=f"leaver-{uuid.uuid4().hex[:8]}"
	)
	session.flush()

	with _over_http(session, token) as client:
		answer = client.set_active(username=leaver.username, active=False)

	assert isinstance(answer, subroutine.views.User)
	assert answer.username == leaver.username
	assert not answer.is_active
	assert person.is_active, "the request must act on the account it named"


def test_somebody_can_be_brought_back_over_http (session: sqlalchemy.orm.Session) -> None:
	"""The other direction of the same endpoint, which carries the other body value."""

	_person, token = _instance(session)
	returning = subroutine.domain.users.create(
		session, username=f"back-{uuid.uuid4().hex[:8]}"
	)
	session.flush()

	with _over_http(session, token) as client:
		client.set_active(username=returning.username, active=False)
		answer = client.set_active(username=returning.username, active=True)

	assert answer.is_active


def test_an_agent_can_be_handed_over_http (session: sqlalchemy.orm.Session) -> None:
	"""`#492`, the second of the pair. Same defect, same endpoint, different body field.

	Worth its own test rather than trusting the first: the two methods are separate lines that
	were wrong separately, and a fix applied to one would leave the other exactly as it was.
	"""

	person, token = _instance(session)
	taking = subroutine.domain.users.create(
		session, username=f"taking-{uuid.uuid4().hex[:8]}", is_superuser=True
	)
	agent = subroutine.domain.users.create(
		session,
		username=f"agent-{uuid.uuid4().hex[:8]}",
		is_service_account=True,
		actor=subroutine.domain.authentication.Principal(user=person, token=None),
	)
	session.flush()

	with _over_http(session, token) as client:
		answer = client.transfer_agent(username=agent.username, to=taking.username)

	assert answer.username == agent.username
	assert answer.responsible_user_id == taking.id


def test_a_refusal_arrives_as_a_refusal_rather_than_a_crash (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The failure path of the same route, so a fixed call cannot be a differently-broken one.

	``errors._status`` translates the problem document back into the application's own exception
	(§8.8). If that stopped working, every test above would still pass — they only ever ask the
	endpoint to succeed.
	"""

	_person, token = _instance(session)
	session.flush()

	with _over_http(session, token) as client, pytest.raises(subroutine.errors.NotFound):
		client.set_active(username="nobody-at-all", active=False)
