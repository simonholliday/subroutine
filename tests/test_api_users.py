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
import sqlalchemy
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


# ---- administering the installation (`SR#701`) ----------------------------------------------


def test_a_second_administrator_can_be_created (session: sqlalchemy.orm.Session) -> None:
	"""``SR#701``. Until this, an instance had exactly the one superuser ``init`` made.

	``is_superuser`` is the **only** source of an instance-tier permission — no role can carry
	one, because ``seed.py`` builds roles from ``permissions.WORKSPACE_LEVEL`` — and it was
	reported by the view, rendered by ``user list`` as *instance admin*, and settable by
	nothing: not the CLI, not ``POST /v1/users``, and by no update path. So an operator could
	not delegate administration, could not keep a second admin against losing the first, and
	could not give an agent the rights to create the accounts it was asked to create.

	The model plainly expected more than one: ``_refuse_deactivating_the_last_administrator``
	counts *other* active superusers before permitting a deactivation, which was a guard
	defending a state nothing could reach.
	"""

	person, token = _instance(session)
	client = _over_http(session, token)

	made = client.create_user(username="deputy", is_superuser=True)

	assert made.is_superuser, "the flag was accepted and did not reach the account"

	# Driven rather than asserted on the field: the point of the flag is the permission.
	stored = session.get(subroutine.db.models.identity.User, made.id)

	assert stored is not None and stored.is_superuser
	assert stored.id != person.id, "it made the caller rather than a second account"


def test_an_ordinary_account_is_not_made_an_administrator_by_accident (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The default has to stay false, or every account created becomes an administrator.

	Worth its own test because the field is new on a request model that already had four
	booleans' worth of defaults, and a wrong default here is the failure nobody would report:
	it grants rather than refuses, so nothing breaks and nobody looks.
	"""

	_person, token = _instance(session)

	made = _over_http(session, token).create_user(username="ordinary")

	assert not made.is_superuser


def test_an_agent_cannot_make_an_administrator (session: sqlalchemy.orm.Session) -> None:
	"""Handing out administration is a person's act — the rule ``set_active`` already states.

	``authorize_instance`` requires the caller to be a superuser, so only an administrator
	reaches this at all. Without the refusal an administering *agent* could make a second, and
	a third, none of which any person agreed to — `SR#356`'s amplification rule at the tier
	above credentials, where it has further to fall.
	"""

	person, _token = _instance(session)

	agent = subroutine.domain.users.create(
		session,
		username="administering-agent",
		is_service_account=True,
		is_superuser=True,
		responsible_user_id=person.id,
	)
	session.flush()

	acting = subroutine.domain.authentication.Principal(user=agent, token=None)

	# It may create an ordinary account: the refusal is about administration, not about agents.
	subroutine.domain.users.create(session, username="fine", actor=acting)

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		subroutine.domain.users.create(
			session, username="another-admin", is_superuser=True, actor=acting
		)

	assert "person's act" in str(refused.value.detail)


def test_a_person_can_say_which_timezone_they_are_in (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#994`. §6.5's user level was settable at creation and by no surface afterwards.

	Driven over HTTP by the client, because that is the gap this file exists for: a route the
	API accepts and no client passes is `#427`'s blind spot, and it is the fifth instance.
	"""

	person, token = _instance(session)

	# **`init` does record one, and `#994` said otherwise** — it measured the served instance,
	# where the founder's is null, and read that as what a fresh install produces. `bootstrap`
	# passes the machine's zone to the account *and* the workspace, so what the item describes
	# is an account made by `POST /v1/users` without one, or a person who has since moved.
	assert person.timezone == "UTC", "init records the machine's zone on the first account"

	with _over_http(session, token) as client:
		changed = client.set_timezone(
			username=person.username, timezone="Australia/Sydney"
		)

	assert changed.timezone == "Australia/Sydney"


def test_clearing_a_timezone_puts_the_reader_back_on_the_workspace_s (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Null is a value here, not a gap — *not stated* is what makes §6.5 a chain.

	The pair matters more than either half: a field that can be set and not unset is one
	somebody is stuck with the first time they get it wrong, which is `#812`'s shape and the
	reason §8.3 pins the distinction rather than leaving it to taste.
	"""

	person, token = _instance(session)

	with _over_http(session, token) as client:
		client.set_timezone(username=person.username, timezone="Australia/Sydney")

		cleared = client.set_timezone(username=person.username, timezone=None)

	assert cleared.timezone is None


def test_nobody_can_set_somebody_else_s_timezone (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Simon's decision of 2026-08-18, and the caller here is the one who could do anything else.

	**The founder is a superuser holding `instance:user_create`**, which is the grant that
	marks somebody as having left and hands an agent over — so this proves the refusal is
	about *identity* rather than about authority, which is the whole of the decision. A test
	using an ordinary account would have passed against a permission check as well.
	"""

	_person, token = _instance(session)
	colleague = subroutine.domain.users.create(
		session, username=f"jo-{uuid.uuid4().hex[:8]}", actor=None
	)
	session.flush()

	with (
		_over_http(session, token) as client,
		pytest.raises(subroutine.errors.Forbidden) as refused,
	):
		client.set_timezone(username=colleague.username, timezone="Europe/London")

	assert colleague.username in str(refused.value), (
		"the refusal names who it is about, since there is no permission to name"
	)
	assert colleague.timezone is None, "nothing was written on the way to being refused"


def test_a_timezone_nobody_has_heard_of_is_refused_where_it_is_typed (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`workspaces.create`'s recorded lesson, asked of the table one along.

	Stored and met later, an unknown zone surfaces as a 422 naming the *request's* timezone on
	some later date computation — a message about the wrong thing, arriving long after the
	mistake, to somebody who is not doing the thing that caused it.
	"""

	person, token = _instance(session)

	with (
		_over_http(session, token) as client,
		pytest.raises(subroutine.errors.ValidationError) as refused,
	):
		client.set_timezone(username=person.username, timezone="Mars/Olympus")

	assert "Mars/Olympus" in str(refused.value)
	assert person.timezone == "UTC", "the row is untouched by a value it refused"


def test_the_agenda_is_counted_from_the_zone_a_person_gave (
	session: sqlalchemy.orm.Session,
) -> None:
	"""What the setting is *for* — decision `#989`, and the reason `#994` blocks `#995`.

	Without this, "the reader's own timezone" resolves to a *workspace's*, which is a server's
	locality rather than a person's. So the value being settable and the value being read are
	one claim, and asserting only the first would leave a control that changes nothing.
	"""

	person, token = _instance(session)

	with _over_http(session, token) as client:
		before = client.agenda()

		client.set_timezone(username=person.username, timezone="Pacific/Auckland")

		after = client.agenda()

	assert before.timezone == "UTC", "a bootstrapped instance counts in its own zone"
	assert after.timezone == "Pacific/Auckland", (
		"the chain reads the account before the workspace (§6.5)"
	)

	# The workspace is untouched, which is what makes the line above a statement about the
	# *chain* rather than about a value that happened to change somewhere.
	spaces = session.scalars(
		sqlalchemy.select(subroutine.db.models.identity.Workspace)
	).all()

	assert [one.timezone for one in spaces] == ["UTC"], (
		"only the account moved, so the agenda followed the account"
	)
