"""A person can be marked as having left, and the instance says what that stops.

`#475`. ``is_active`` was enforced in four places and written in none: ``authentication``
refuses an inactive account with its own ``USER_INACTIVE`` failure, three queries narrow by it,
and nothing anywhere could produce one. So the state existed, was load-bearing, and was
unreachable — the mirror of this codebase's inert-control defect, and invisible to
``test_api_writability``, which walks ``Task`` and ``Document`` only (`#443`).

Decision `#473` rests on it: when a person leaves, the agents answerable to them stop. That
cannot be true until leaving is something somebody can record.
"""

import uuid

import pytest
import sqlalchemy.orm

import api_support
import subroutine.clients.base
import subroutine.clients.http
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.db.models.identity
import subroutine.domain.accountability
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.users
import subroutine.errors


def _acting (
	user: subroutine.db.models.identity.User,
) -> subroutine.domain.authentication.Principal:
	"""Return an unnarrowed principal, so a scope refusal cannot be mistaken for these rules."""

	return subroutine.domain.authentication.Principal(user=user, token=None)


def _superuser (
	session: sqlalchemy.orm.Session, name: str = "admin"
) -> subroutine.db.models.identity.User:
	"""Create a person who can administer the instance."""

	return subroutine.domain.users.create(
		session, username=f"{name}-{uuid.uuid4().hex[:8]}", is_superuser=True
	)


def _agent (
	session: sqlalchemy.orm.Session,
	answering_to: subroutine.db.models.identity.User,
	name: str = "agent",
) -> subroutine.db.models.identity.User:
	"""Create a service account answerable to somebody."""

	return subroutine.domain.users.create(
		session,
		username=f"{name}-{uuid.uuid4().hex[:8]}",
		is_service_account=True,
		actor=_acting(answering_to),
	)


def _agent_holding_the_permission (
	session: sqlalchemy.orm.Session,
	answering_to: subroutine.db.models.identity.User,
	name: str = "wide",
) -> subroutine.db.models.identity.User:
	"""Create an agent that gets *past* the permission check, so the refusal under test is `#487`.

	Instance verbs are carried by ``is_superuser`` alone — a role may never hold one, since
	``seed.py`` builds roles from ``permissions.WORKSPACE_LEVEL`` — so an agent that can reach
	``set_active`` at all is a superuser service account. Anything narrower is refused by
	``authorize_instance`` first, and a test built on one would pass against the original code
	while measuring authorisation instead of this rule.
	"""

	return subroutine.domain.users.create(
		session,
		username=f"{name}-{uuid.uuid4().hex[:8]}",
		is_service_account=True,
		is_superuser=True,
		actor=_acting(answering_to),
	)


def test_somebody_can_be_marked_as_having_left (session: sqlalchemy.orm.Session) -> None:
	"""The state the product could not reach until now."""

	admin = _superuser(session)
	leaver = subroutine.domain.users.create(
		session, username=f"leaver-{uuid.uuid4().hex[:8]}"
	)

	assert leaver.is_active

	subroutine.domain.users.set_active(session, leaver, active=False, actor=_acting(admin))

	assert not leaver.is_active


def test_bringing_somebody_back_is_the_same_operation (session: sqlalchemy.orm.Session) -> None:
	"""One function both ways, so the two directions cannot come to disagree."""

	admin = _superuser(session)
	returning = subroutine.domain.users.create(
		session, username=f"back-{uuid.uuid4().hex[:8]}"
	)

	subroutine.domain.users.set_active(session, returning, active=False, actor=_acting(admin))
	subroutine.domain.users.set_active(session, returning, active=True, actor=_acting(admin))

	assert returning.is_active


def test_deactivating_says_which_agents_it_stops (session: sqlalchemy.orm.Session) -> None:
	"""`project rename`'s precedent: name what stops working before doing it.

	Returned rather than counted, because "3 agents will stop" is not something anybody can act
	on and "these three will stop" is.
	"""

	admin = _superuser(session)
	leaver = _superuser(session, "leaver")
	first = _agent(session, leaver, "first")
	second = _agent(session, leaver, "second")
	_unrelated = _agent(session, admin, "unrelated")

	stopping = subroutine.domain.users.set_active(
		session, leaver, active=False, actor=_acting(admin)
	)

	assert {row.id for row in stopping} == {first.id, second.id}
	assert [row.username for row in stopping] == sorted(
		[first.username, second.username]
	), "named in a stable order, so a caller can print them without sorting again"


def test_an_agent_of_an_agent_is_named_too (session: sqlalchemy.orm.Session) -> None:
	"""The chain is a tree, so what stops is everything downstream and not only the first level."""

	admin = _superuser(session)
	leaver = _superuser(session, "leaver")
	middle = subroutine.domain.users.create(
		session, username=f"middle-{uuid.uuid4().hex[:8]}",
		is_service_account=True, is_superuser=True, actor=_acting(leaver),
	)
	below = _agent(session, middle, "below")

	stopping = subroutine.domain.users.set_active(
		session, leaver, active=False, actor=_acting(admin)
	)

	assert {row.id for row in stopping} == {middle.id, below.id}


def test_the_last_person_who_can_administer_cannot_leave (
	session: sqlalchemy.orm.Session,
) -> None:
	"""An instance nobody can administer cannot be repaired from inside — and stops every agent.

	Under decision `#473` every chain terminates at a person, so deactivating the last one
	would refuse every credential on the instance at once, including the one needed to undo it.
	"""

	only = _superuser(session, "only")

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.users.set_active(session, only, active=False, actor=_acting(only))

	assert "only person who can administer" in str(refusal.value)
	assert only.is_active


def test_the_last_administrator_may_leave_once_there_is_another (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The refusal is about the *last* one, not about superusers in general.

	Falsifies the guard from the other side: a rule that refused every superuser would pass the
	test above just as happily, and would be wrong.
	"""

	first = _superuser(session, "first")
	second = _superuser(session, "second")

	subroutine.domain.users.set_active(session, first, active=False, actor=_acting(second))

	assert not first.is_active


def test_an_agent_does_not_count_as_somebody_who_can_administer (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A superuser service account is not a person, so it cannot be the one left holding this.

	Otherwise the last human could leave whenever an agent happened to hold a wide credential,
	which is the arrangement decision `#473` exists to make impossible.
	"""

	only = _superuser(session, "only")
	subroutine.domain.users.create(
		session, username=f"wide-{uuid.uuid4().hex[:8]}",
		is_service_account=True, is_superuser=True, actor=_acting(only),
	)

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.users.set_active(session, only, active=False, actor=_acting(only))


def test_deactivating_twice_does_not_move_the_version (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Running it again is not a second act, so §8.9's number must not move for one."""

	admin = _superuser(session)
	leaver = subroutine.domain.users.create(
		session, username=f"leaver-{uuid.uuid4().hex[:8]}"
	)

	subroutine.domain.users.set_active(session, leaver, active=False, actor=_acting(admin))
	after = leaver.version

	subroutine.domain.users.set_active(session, leaver, active=False, actor=_acting(admin))

	assert leaver.version == after


def test_an_agent_cannot_mark_a_person_as_having_left (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#487`. The permission was the only check, and an agent can hold it.

	Found by reading `#484`'s deny-list rather than the code: ``transfer`` refuses a service
	account outright and this, the same rule, did not. Under decision `#473` every agent answers
	to a person, which makes this the one act that can stop *the caller* — an agent deactivating
	the person it answers to revokes itself and its siblings in a call nothing undoes.
	"""

	person = _superuser(session)
	agent = _agent_holding_the_permission(session, person)
	leaver = _superuser(session, "leaver")

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		subroutine.domain.users.set_active(
			session, leaver, active=False, actor=_acting(agent)
		)

	assert "person's act" in str(refused.value)
	assert leaver.is_active, "the refusal has to come before the write, not instead of a commit"


def test_an_agent_cannot_bring_somebody_back (session: sqlalchemy.orm.Session) -> None:
	"""Reactivation is the same act, so it is the same rule.

	``set_active`` is deliberately one function for both directions, and a check written on the
	deactivating branch alone would leave the other open — restoring an account is how a
	deactivated one becomes useful again, so it is no less a decision about somebody's standing.
	"""

	person = _superuser(session)
	agent = _agent_holding_the_permission(session, person)
	away = _superuser(session, "away")

	subroutine.domain.users.set_active(session, away, active=False, actor=_acting(person))

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.users.set_active(session, away, active=True, actor=_acting(agent))

	assert not away.is_active


def test_an_agent_cannot_stop_another_agent (session: sqlalchemy.orm.Session) -> None:
	"""Why the refusal reads the *caller* and not the target.

	The narrower rule — refuse only when the target is a person — protects the headline case and
	still lets an agent stop its siblings, which is the same harm by a shorter route. It would
	also make this refusal depend on the target where ``transfer``'s depends on the caller: two
	rules that happen to agree rather than one rule, which is what `#487` was filed to avoid.
	"""

	person = _superuser(session)
	agent = _agent_holding_the_permission(session, person)
	sibling = _agent(session, person, "sibling")

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.users.set_active(
			session, sibling, active=False, actor=_acting(agent)
		)

	assert sibling.is_active


@pytest.mark.parametrize("transport", ["local", "remote"])
def test_neither_transport_lets_an_agent_do_it (
	session: sqlalchemy.orm.Session, transport: str
) -> None:
	"""Proved on both surfaces rather than reasoned about from where the check sits.

	The precedent is ``read_only``, which was enforced in the local client for weeks while a
	test named for the remote one passed. A rule below both surfaces *should* be inherited by
	both, and that is exactly the sentence nobody re-checks — §13.7's whole argument for
	parameterising this kind of test rather than trusting the layering.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	agent = _agent_holding_the_permission(session, setup.user)
	leaver = _superuser(session, "leaver")

	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=agent, title="The agent's own"
	)
	session.flush()

	factory = api_support.factory_for(session)
	client: subroutine.clients.base.Client

	if transport == "local":
		client = subroutine.clients.local.Client(
			subroutine.connections.Connection(name="local"),
			subroutine.config.Settings(dev_mode=True),
			session_factory=factory,
			token=issued.value.get_secret_value(),
		)

	else:
		client = subroutine.clients.http.Client(
			subroutine.connections.Connection(name="work", url="https://employer.example.com"),
			token=issued.value.get_secret_value(),
			transport=api_support.SyncTransport(api_support.build_app(factory)),
			base_url=api_support.BASE_URL,
		)

	with client, pytest.raises(subroutine.errors.Forbidden):
		client.set_active(username=leaver.username, active=False)

	assert leaver.is_active
