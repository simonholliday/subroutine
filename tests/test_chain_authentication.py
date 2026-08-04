"""An agent stops working when the person answerable for it leaves — `#479`.

Decision `#473`, decision 3: somebody gave an agent permission to work, and when that somebody
goes, so does the permission. ``authentication`` already asked whether the presenting account is
active; this is the same question asked of everybody it answers to, which is the half that makes
marking a leaver inactive mean anything at all.

**The refusal is deliberately indistinguishable from every other one.** §7.4's rule: telling an
unauthenticated caller *why* is a fact they had no way to learn. So these tests assert the
credential stops working, never the reason — the reason is what ``AuthenticationFailure`` records
for the log.
"""

import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.users


def _acting (
	user: subroutine.db.models.identity.User,
) -> subroutine.domain.authentication.Principal:
	"""Return an unnarrowed principal, so a scope refusal cannot be mistaken for this rule."""

	return subroutine.domain.authentication.Principal(user=user, token=None)


def _person (
	session: sqlalchemy.orm.Session, name: str = "person"
) -> subroutine.db.models.identity.User:
	"""Create a person who can administer the instance."""

	return subroutine.domain.users.create(
		session, username=f"{name}-{uuid.uuid4().hex[:8]}", is_superuser=True
	)


def _credentialled (
	session: sqlalchemy.orm.Session, user: subroutine.db.models.identity.User
) -> str:
	"""Issue a token for somebody and return the secret, which is readable exactly once.

	``get_secret_value()`` rather than an attribute, deliberately on ``auth.IssuedToken``'s part:
	the secret is wrapped so printing the object or letting it reach a traceback cannot disclose
	it, and reading it is something somebody has to type.
	"""

	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=user, title="For a test"
	)
	session.flush()

	return issued.value.get_secret_value()


def _resolves (session: sqlalchemy.orm.Session, secret: str) -> bool:
	"""Report whether a credential is still accepted."""

	try:
		subroutine.domain.authentication.authenticate(session, secret)

	except subroutine.domain.authentication.AuthenticationError:
		return False

	return True


def test_an_agent_works_while_its_person_is_here (session: sqlalchemy.orm.Session) -> None:
	"""The control case, and the one a rule that refused everything would fail."""

	person = _person(session)
	agent = subroutine.domain.users.create(
		session, username=f"agent-{uuid.uuid4().hex[:8]}",
		is_service_account=True, actor=_acting(person),
	)
	secret = _credentialled(session, agent)

	assert _resolves(session, secret)


def test_an_agent_stops_when_its_person_leaves (session: sqlalchemy.orm.Session) -> None:
	"""The whole of decision `#473`'s third decision, in one assertion.

	Falsified against the original: the credential resolves before the deactivation and not
	after, so the check under test is the only thing that changed.
	"""

	other = _person(session, "other")
	leaving = _person(session, "leaving")
	agent = subroutine.domain.users.create(
		session, username=f"agent-{uuid.uuid4().hex[:8]}",
		is_service_account=True, actor=_acting(leaving),
	)
	secret = _credentialled(session, agent)

	assert _resolves(session, secret), "the agent must work before anybody leaves"

	subroutine.domain.users.set_active(session, leaving, active=False, actor=_acting(other))

	assert not _resolves(session, secret)


def test_a_person_is_unaffected_by_anybody_elses_absence (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A person answers for themselves, so the walk must not touch them at all."""

	first = _person(session, "first")
	second = _person(session, "second")
	secret = _credentialled(session, first)

	subroutine.domain.users.set_active(session, second, active=False, actor=_acting(first))

	assert _resolves(session, secret)


def test_a_cut_link_partway_up_stops_the_agent_below_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Every link is checked, not only the person at the far end.

	An intermediate agent that has been deactivated is a link somebody deliberately cut.
	Honouring only the far end would walk straight past it — which is the shape of defect this
	project keeps finding, a rule applied to one part of a pair.
	"""

	person = _person(session)
	middle = subroutine.domain.users.create(
		session, username=f"middle-{uuid.uuid4().hex[:8]}",
		is_service_account=True, is_superuser=True, actor=_acting(person),
	)
	below = subroutine.domain.users.create(
		session, username=f"below-{uuid.uuid4().hex[:8]}",
		is_service_account=True, actor=_acting(middle),
	)
	secret = _credentialled(session, below)

	assert _resolves(session, secret)

	subroutine.domain.users.set_active(session, middle, active=False, actor=_acting(person))

	assert not _resolves(session, secret)


def test_an_agent_nobody_answers_for_is_refused (session: sqlalchemy.orm.Session) -> None:
	"""The row a migration leaves when it declines to guess between two administrators.

	Reached by clearing the column directly, because no service will produce it — which is the
	point: the check must not assume the database was written by code that was already correct.
	"""

	person = _person(session)
	agent = subroutine.domain.users.create(
		session, username=f"agent-{uuid.uuid4().hex[:8]}",
		is_service_account=True, actor=_acting(person),
	)
	secret = _credentialled(session, agent)

	agent.responsible_user_id = None
	session.flush()

	assert not _resolves(session, secret)


def test_a_circular_chain_is_refused_rather_than_looping (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A cycle reaches nobody, and looping for ever inside authentication is the worse failure."""

	person = _person(session)
	first = subroutine.domain.users.create(
		session, username=f"first-{uuid.uuid4().hex[:8]}",
		is_service_account=True, actor=_acting(person),
	)
	second = subroutine.domain.users.create(
		session, username=f"second-{uuid.uuid4().hex[:8]}",
		is_service_account=True, actor=_acting(person),
	)
	secret = _credentialled(session, first)

	first.responsible_user_id = second.id
	second.responsible_user_id = first.id
	session.flush()

	assert not _resolves(session, secret)


def test_the_refusal_says_no_more_than_any_other (session: sqlalchemy.orm.Session) -> None:
	"""§7.4: every refusal reads the same to the caller, whatever the reason was.

	Worth pinning, because a check added later is exactly where a helpful message that names
	somebody's departure would get written.
	"""

	other = _person(session, "other")
	leaving = _person(session, "leaving")
	agent = subroutine.domain.users.create(
		session, username=f"agent-{uuid.uuid4().hex[:8]}",
		is_service_account=True, actor=_acting(leaving),
	)
	secret = _credentialled(session, agent)

	subroutine.domain.users.set_active(session, leaving, active=False, actor=_acting(other))

	with pytest.raises(subroutine.domain.authentication.AuthenticationError) as refusal:
		subroutine.domain.authentication.authenticate(session, secret)

	assert leaving.username not in str(refusal.value)
	assert "not accepted" in str(refusal.value)
