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
