"""An agent can be handed to somebody else, and the handover cannot break the chain — `#478`.

Agents stop when the person answerable for them leaves (`#479`), so handing one over is the
only way to keep it running. That makes this **part of the leaver path** rather than a
refinement of it: without it, marking somebody inactive costs you their agents, and a control
that expensive is one people work around instead of using.

Two rules, and the second is the interesting one. Only a person may hand an agent over or take
one on — an agent that could move accountability could move it off itself. And the chain must
still terminate afterwards, which is what stops somebody handing an agent to one of its own
descendants: every foreign key resolves and nobody answers for anything.
"""

import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.domain.accountability
import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.errors


def _acting (
	user: subroutine.db.models.identity.User,
) -> subroutine.domain.authentication.Principal:
	"""Return an unnarrowed principal, so a scope refusal cannot be mistaken for these rules."""

	return subroutine.domain.authentication.Principal(user=user, token=None)


def _person (
	session: sqlalchemy.orm.Session, name: str = "person"
) -> subroutine.db.models.identity.User:
	"""Create a person who can administer the instance."""

	return subroutine.domain.users.create(
		session, username=f"{name}-{uuid.uuid4().hex[:8]}", is_superuser=True
	)


def _agent (
	session: sqlalchemy.orm.Session,
	creator: subroutine.db.models.identity.User,
	name: str = "agent",
	*,
	may_create: bool = False,
) -> subroutine.db.models.identity.User:
	"""Create an agent answerable to whoever creates it."""

	return subroutine.domain.users.create(
		session,
		username=f"{name}-{uuid.uuid4().hex[:8]}",
		is_service_account=True,
		is_superuser=may_create,
		actor=_acting(creator),
	)


def test_an_agent_can_be_handed_to_somebody_else (session: sqlalchemy.orm.Session) -> None:
	"""The act the leaver path needs: somebody else agrees to answer for it."""

	leaving = _person(session, "leaving")
	staying = _person(session, "staying")
	agent = _agent(session, leaving)

	subroutine.domain.users.transfer(session, agent, to=staying, actor=_acting(staying))

	assert agent.responsible_user_id == staying.id
	assert subroutine.domain.accountability.answers_for(session, agent) is staying


def test_a_handed_over_agent_survives_its_old_persons_departure (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The whole point, stated as behaviour rather than as a column.

	Without this the two features contradict each other on paper — `#479` stops the agent and
	`#478` claims to save it — and only running both together says which won.
	"""

	leaving = _person(session, "leaving")
	staying = _person(session, "staying")
	agent = _agent(session, leaving)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=agent, title="For a test"
	)
	session.flush()
	secret = issued.value.get_secret_value()

	subroutine.domain.users.transfer(session, agent, to=staying, actor=_acting(staying))
	subroutine.domain.users.set_active(
		session, leaving, active=False, actor=_acting(staying)
	)

	assert subroutine.domain.authentication.authenticate(session, secret).user is agent


def test_an_agent_cannot_hand_over_another_agent (session: sqlalchemy.orm.Session) -> None:
	"""Somebody has to *agree* to be accountable, and that is not something an agent can do.

	The same rule as creation, from the other end: an agent that could move accountability could
	move it off itself, which is the laundering `accountability` refuses one step earlier.
	"""

	person = _person(session)
	stranger = _person(session, "stranger")
	broker = _agent(session, person, "broker", may_create=True)
	agent = _agent(session, person)

	with pytest.raises(subroutine.errors.Forbidden) as refusal:
		subroutine.domain.users.transfer(session, agent, to=stranger, actor=_acting(broker))

	assert "person's act" in str(refusal.value)
	assert agent.responsible_user_id == person.id


def test_a_person_cannot_be_handed_over (session: sqlalchemy.orm.Session) -> None:
	"""A person answers for themselves, so there is nothing to transfer."""

	first = _person(session, "first")
	second = _person(session, "second")

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.users.transfer(session, first, to=second, actor=_acting(second))

	assert "answers for themselves" in str(refusal.value)


def test_handing_an_agent_to_its_own_descendant_is_refused (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A cycle where every foreign key resolves and nobody answers for anything.

	This is why the check runs against the tree as it *will* be rather than as it was: asking
	whether the target is currently reachable would miss the case where the move itself makes
	the loop.
	"""

	person = _person(session)
	parent = _agent(session, person, "parent", may_create=True)
	child = _agent(session, parent, "child")

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.users.transfer(session, parent, to=child, actor=_acting(person))

	assert "already answers to" in str(refusal.value)


def test_a_refused_transfer_changes_nothing (session: sqlalchemy.orm.Session) -> None:
	"""The refusal above assigns before it walks, so it has to put the old answer back.

	Worth its own test rather than trusting the one above: a guard that leaves the row half
	written is `claims._lease`'s recorded defect, where a refused lease length left the loaded
	row carrying a holder it had just declined to give.
	"""

	person = _person(session)
	parent = _agent(session, person, "parent", may_create=True)
	child = _agent(session, parent, "child")

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.users.transfer(session, parent, to=child, actor=_acting(person))

	assert parent.responsible_user_id == person.id
	assert subroutine.domain.accountability.answers_for(session, child) is person


def test_an_agent_may_be_handed_to_another_agent (session: sqlalchemy.orm.Session) -> None:
	"""Nesting is allowed as long as the chain still ends at somebody.

	The rule is that responsibility *terminates* at a person, not that it is one link long — so
	refusing every agent target would be stricter than the model and would break the delegation
	path `#476` records.
	"""

	person = _person(session)
	senior = _agent(session, person, "senior", may_create=True)
	loose = _agent(session, person, "loose")

	subroutine.domain.users.transfer(session, loose, to=senior, actor=_acting(person))

	assert subroutine.domain.accountability.chain(session, loose) == [loose, senior, person]
