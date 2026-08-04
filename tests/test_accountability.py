"""An agent answers to a person, and the chain that says so cannot be faked or broken.

Decision `#473`: an agent is not a principal anybody can blame — somebody gave it permission to
work, and that somebody is accountable. The rules under test are the two that make the column
worth having rather than decorative: **the chain terminates at a person**, and **it is inherited
rather than chosen**.

The second is the security-shaped one. If an agent creating a sub-agent could name who answers
for it, accountability launders in one call and the trace ends at somebody who authorised none of
it. That is the shape ``_refuse_amplification`` exists for, one layer over.
"""

import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.domain.accountability
import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.errors


def _person (
	session: sqlalchemy.orm.Session, name: str = "person"
) -> subroutine.db.models.identity.User:
	"""Create a person who answers for themselves, and may create accounts.

	A superuser because creating any account needs ``instance:user_create``, which only
	``is_superuser`` carries (§7.1) — so a plain person could not reach the rules under test at
	all, and a test built on one would be measuring the wrong refusal.
	"""

	return subroutine.domain.users.create(
		session, username=f"{name}-{uuid.uuid4().hex[:8]}", is_superuser=True
	)


def _acting (
	user: subroutine.db.models.identity.User,
) -> subroutine.domain.authentication.Principal:
	"""Return a principal with no token, which §12.1a treats as maximum trust.

	Right for these tests: the rules under test are about *who answers for whom*, not about what
	a credential narrows, and using an unnarrowed actor keeps a scope refusal from being mistaken
	for an accountability one.
	"""

	return subroutine.domain.authentication.Principal(user=user, token=None)


def test_a_person_answers_for_themselves (session: sqlalchemy.orm.Session) -> None:
	"""A person's chain is one entry long, whatever the column happens to hold."""

	person = _person(session)

	assert subroutine.domain.accountability.chain(session, person) == [person]
	assert subroutine.domain.accountability.answers_for(session, person) is person
	assert person.responsible_user_id is None


def test_an_agent_is_answerable_to_whoever_created_it (session: sqlalchemy.orm.Session) -> None:
	"""Creating an agent as a person makes that person accountable, without being asked."""

	person = _person(session)

	agent = subroutine.domain.users.create(
		session, username=f"agent-{uuid.uuid4().hex[:8]}",
		is_service_account=True, actor=_acting(person),
	)

	assert agent.responsible_user_id == person.id
	assert subroutine.domain.accountability.answers_for(session, agent) is person


def test_an_agents_own_agent_inherits_rather_than_choosing (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A sub-agent answers to *its creator*, and walking on from there reaches the person.

	The chain records the delegation path rather than collapsing it. Both forms answer "who is
	accountable" — ``answers_for`` walks to the end either way — but only this one also answers
	"who handed this down", and it is what makes deactivating an intermediate agent stop
	everything below it.

	The creating agent is a superuser because **creating an account needs
	``instance:user_create`` and no role may carry an instance verb** (§7.1) — so an agent that
	spawns sub-agents must hold that grant itself. Worth seeing rather than working around: it
	means the delegating-agent pattern currently costs a very wide credential.
	"""

	person = _person(session)
	agent = subroutine.domain.users.create(
		session, username=f"agent-{uuid.uuid4().hex[:8]}",
		is_service_account=True, is_superuser=True, actor=_acting(person),
	)

	sub = subroutine.domain.users.create(
		session, username=f"sub-{uuid.uuid4().hex[:8]}",
		is_service_account=True, actor=_acting(agent),
	)

	assert sub.responsible_user_id == agent.id
	assert subroutine.domain.accountability.answers_for(session, sub) is person
	assert subroutine.domain.accountability.chain(session, sub) == [sub, agent, person]


def test_an_agent_cannot_name_someone_else_as_answerable (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The laundering case, refused. `#473`, and the shape `#356` found one layer over.

	Without this an agent creates a sub-agent answerable to somebody who never authorised it,
	the sub-agent does something wrong, and every row involved resolves perfectly.
	"""

	person = _person(session, "authorised")
	stranger = _person(session, "stranger")

	agent = subroutine.domain.users.create(
		session, username=f"agent-{uuid.uuid4().hex[:8]}",
		is_service_account=True, is_superuser=True, actor=_acting(person),
	)

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.users.create(
			session, username=f"sub-{uuid.uuid4().hex[:8]}", is_service_account=True,
			responsible_user_id=stranger.id, actor=_acting(agent),
		)

	assert "cannot choose" in str(refusal.value)


def test_a_person_may_name_someone_else (session: sqlalchemy.orm.Session) -> None:
	"""A person delegating to another person is the act the model is *for*, so it is allowed."""

	creating = _person(session, "creating")
	answering = _person(session, "answering")

	agent = subroutine.domain.users.create(
		session, username=f"agent-{uuid.uuid4().hex[:8]}", is_service_account=True,
		responsible_user_id=answering.id, actor=_acting(creating),
	)

	assert subroutine.domain.accountability.answers_for(session, agent) is answering


def test_an_agent_with_nobody_answering_for_it_is_refused (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A null chain is a refusal, not a default — the state the migration deliberately leaves.

	Reached by clearing the column directly rather than through a service, because no service
	may produce it: this is the row an installation with two administrators is left holding
	after the backfill declines to guess.
	"""

	person = _person(session)
	agent = subroutine.domain.users.create(
		session, username=f"agent-{uuid.uuid4().hex[:8]}",
		is_service_account=True, actor=_acting(person),
	)

	agent.responsible_user_id = None
	session.flush()

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.accountability.chain(session, agent)

	assert "No one is accountable" in str(refusal.value)


def test_a_chain_that_runs_in_a_circle_is_refused (session: sqlalchemy.orm.Session) -> None:
	"""Two agents answering to each other reach nobody, and every row resolves.

	Built by hand for the same reason as the test above: the write-time rule refuses this, so
	the only way to have it is to put it there. What is under test is that the *read* refuses it
	too — a guard that only runs on the way in leaves the database as the thing that has to have
	been right.
	"""

	person = _person(session)
	first = subroutine.domain.users.create(
		session, username=f"first-{uuid.uuid4().hex[:8]}",
		is_service_account=True, actor=_acting(person),
	)
	second = subroutine.domain.users.create(
		session, username=f"second-{uuid.uuid4().hex[:8]}",
		is_service_account=True, actor=_acting(person),
	)

	first.responsible_user_id = second.id
	second.responsible_user_id = first.id
	session.flush()

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.accountability.chain(session, first)

	assert "in a circle" in str(refusal.value)


def test_an_agent_cannot_be_created_with_no_actor_at_all (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Bootstrap creates the first *person*; it may not create an agent answerable to nobody."""

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.users.create(
			session, username=f"agent-{uuid.uuid4().hex[:8]}", is_service_account=True
		)

	assert "without a person to answer for it" in str(refusal.value)


def test_naming_an_account_that_does_not_exist_is_refused (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A dangling responsible id would be a chain that resolves to nothing at read time."""

	person = _person(session)

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.users.create(
			session, username=f"agent-{uuid.uuid4().hex[:8]}", is_service_account=True,
			responsible_user_id=uuid.uuid4(), actor=_acting(person),
		)
