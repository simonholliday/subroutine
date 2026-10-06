"""An agent can be handed to somebody else, and the handover cannot break the chain — `#478`.

Agents stop when the person answerable for them leaves (`#479`), so handing one over is the
only way to keep it running. That makes this **part of the leaver path** rather than a
refinement of it: without it, marking somebody inactive costs you their agents, and a control
that expensive is one people work around instead of using.

Two rules, and the second is the interesting one. Only a person may hand an agent over, to a
person or to an agent whose own chain ends at one — an agent that could move accountability
could move it off itself, and holding one grants nothing over it (decision `#4159`). And the
chain must still terminate afterwards, which is what stops somebody handing an agent to one of
its own descendants: every foreign key resolves and nobody answers for anything.
"""

import typing
import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.domain.accountability
import subroutine.domain.authentication
import subroutine.domain.sessions
import subroutine.domain.tokens
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

	The move makes one exactly when the target already answers to the agent being moved, so that
	is what is asked, by name, before anything is assigned (`SR#3939`).
	"""

	person = _person(session)
	parent = _agent(session, person, "parent", may_create=True)
	child = _agent(session, parent, "child")

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.users.transfer(session, parent, to=child, actor=_acting(person))

	assert "already answers to" in str(refusal.value)


def test_handing_an_agent_to_itself_is_refused (session: sqlalchemy.orm.Session) -> None:
	"""`SR#4030`, gap 3 of L-10 of the cold review of 2026-09-30: the loop of one was never asked.

	``agents_answering_to`` never includes the agent itself, so without its own clause a transfer to
	itself found nothing below, passed the depth check, and wrote an agent answering to itself.
	"""

	person = _person(session)
	agent = _agent(session, person)

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.users.transfer(session, agent, to=agent, actor=_acting(person))

	assert "already answers to" in str(refusal.value)
	assert agent.responsible_user_id == person.id


def test_a_refused_transfer_changes_nothing (session: sqlalchemy.orm.Session) -> None:
	"""A refused transfer leaves the agent answering to whoever it answered to before.

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


def test_an_agent_is_not_handed_where_an_agent_below_it_could_not_act (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#3939`, NEW-L7-1 of the verification of the cold review of 2026-09-28.

	A transfer walked the moved agent's own chain and nothing below it, so an agent with eight
	levels below it could be handed to one ten deep, and its deepest sub-agent was left with a
	chain nineteen accounts long - treated as broken, so it could not act - with nothing said.
	**Measured at the deepest agent below**, and refused before anything moves - one account past
	the limit is refused and a chain exactly as long as allowed is not.
	"""

	person = _person(session)

	def nested (
		top: subroutine.db.models.identity.User, depth: int
	) -> list[subroutine.db.models.identity.User]:
		"""Nest ``depth`` agents under ``top`` by hand, each answering to the one before it."""

		made: list[subroutine.db.models.identity.User] = []
		above = top

		for _level in range(depth):
			agent = _agent(session, person, "level")
			agent.responsible_user_id = above.id
			made.append(agent)
			above = agent

		session.flush()

		return made

	moving = _agent(session, person, "moving")
	deepest = nested(moving, 8)[-1]
	ladder = nested(person, 7)

	# Eight below, the agent itself, and a target whose own chain is eight long: seventeen.
	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.users.transfer(session, moving, to=ladder[-1], actor=_acting(person))

	said = str(refusal.value)

	assert "an agent below it" in said and "17 accounts long" in said, said
	assert moving.responsible_user_id == person.id, "a refused transfer moved the agent"
	assert subroutine.domain.accountability.can_act(session, deepest)

	# One nearer a person, and the deepest chain is exactly as long as allowed.
	subroutine.domain.users.transfer(session, moving, to=ladder[-2], actor=_acting(person))

	assert len(subroutine.domain.accountability.chain(session, deepest)) == (
		subroutine.domain.accountability.MAX_DEPTH
	)


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


def test_an_agent_that_cannot_make_agents_may_hold_one_and_gains_nothing_over_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4158`, decided on `#3953`: the holder need not be able to make agents.

	**That is safe because holding grants nothing**, so the holder's own credential is refused
	every act on what it holds: a credential or a sign-in link for it, an agent beneath it,
	revoking its credential, deactivating it, handing it on, setting its timezone and signing it
	out. The test above passed whichever way this was decided, since its holder may make agents.
	The day holding grants any of these, the decision is taken again, and this is where it shows.
	"""

	person = _person(session)
	holder = _agent(session, person, "holder")
	held = _agent(session, person, "held")
	its_own, _secret = subroutine.domain.authentication.issue_token(
		session, user=held, title="the held agent's own"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=holder, title="the holder's own"
	)
	session.flush()
	as_holder = subroutine.domain.authentication.authenticate(
		session, issued.value.get_secret_value(), record_use=False
	)

	subroutine.domain.users.transfer(session, held, to=holder, actor=_acting(person))

	assert subroutine.domain.accountability.chain(session, held) == [held, holder, person]

	acts: dict[str, typing.Callable[[], object]] = {
		"a credential for it": lambda: subroutine.domain.authentication.issue_token(
			session, user=held, title="taken", actor=as_holder
		),
		"a sign-in link for it": lambda: subroutine.domain.sessions.mint_link(
			session, user=held, actor=as_holder
		),
		"an agent beneath it": lambda: subroutine.domain.users.create(
			session,
			username=f"beneath-{uuid.uuid4().hex[:8]}",
			is_service_account=True,
			responsible_user_id=held.id,
			actor=as_holder,
		),
		"revoking its credential": lambda: subroutine.domain.tokens.revoke(
			session, its_own, actor=as_holder
		),
		"deactivating it": lambda: subroutine.domain.users.set_active(
			session, held, active=False, actor=as_holder
		),
		"handing it on": lambda: subroutine.domain.users.transfer(
			session, held, to=person, actor=as_holder
		),
		"its timezone": lambda: subroutine.domain.users.set_timezone(
			session, held, timezone="Europe/London", actor=as_holder
		),
		"signing it out": lambda: subroutine.domain.sessions.sign_out_everywhere(
			session, user=held, actor=as_holder
		),
	}

	for what, act in acts.items():
		with pytest.raises(subroutine.errors.Forbidden):
			act()

		assert held.is_active, what
		assert held.responsible_user_id == holder.id, what
		assert its_own.revoked_at is None, what


def test_an_agent_is_not_handed_to_somebody_who_cannot_act (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4545`, G8 of the cold review of 2026-10-05: it stopped the moment it moved.

	Handed to a deactivated person, or to an agent nobody active answers for, the agent was
	accepted and then refused on every request, and nothing said so at the hand-over. **Refused
	there, saying which**, and nothing moves.
	"""

	staying = _person(session, "staying")
	agent = _agent(session, staying)
	gone = _person(session, "gone")
	stranded = _agent(session, gone, "stranded")
	subroutine.domain.users.set_active(session, gone, active=False, actor=_acting(staying))

	for to, why in ((gone, "is deactivated"), (stranded, "nobody who can act answers for")):
		with pytest.raises(subroutine.errors.ValidationError, match=why):
			subroutine.domain.users.transfer(session, agent, to=to, actor=_acting(staying))

		assert agent.responsible_user_id == staying.id


def test_an_agent_is_not_made_to_answer_to_somebody_who_cannot_act (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#4545`, G8's other half: an agent made to answer to a leaver could never act."""

	staying = _person(session, "staying")
	gone = _person(session, "gone")
	subroutine.domain.users.set_active(session, gone, active=False, actor=_acting(staying))

	with pytest.raises(subroutine.errors.ValidationError, match="is deactivated"):
		subroutine.domain.users.create(
			session,
			username=f"agent-{uuid.uuid4().hex[:8]}",
			is_service_account=True,
			responsible_user_id=gone.id,
			actor=_acting(staying),
		)


@pytest.mark.parametrize("named", ["username", "service_account"])
def test_no_credential_is_issued_for_an_agent_nobody_active_answers_for (
	session: sqlalchemy.orm.Session, named: str
) -> None:
	"""`SR#4545`, G8: issued, printed, stored, and refused the first time anybody used it.

	**Not found, as a deactivated account is**, with the remedy named: hand it to somebody who can
	act. Both ways of naming the account reached the dead credential.
	"""

	staying = _person(session, "staying")
	gone = _person(session, "gone")
	stranded = _agent(session, gone, "stranded")
	subroutine.domain.users.set_active(session, gone, active=False, actor=_acting(staying))

	with pytest.raises(subroutine.errors.NotFound, match="answers for") as refused:
		subroutine.domain.tokens.issue(
			session,
			actor=_acting(staying),
			title="Dead on arrival",
			**typing.cast(dict[str, typing.Any], {named: stranded.username}),
		)

	assert "user transfer" in str(refused.value.errors[0].hint)
