"""An owner refused a change to another owner is told the true reason - `SR#5107`.

M-23 of the cold review of 2026-10-17 (`#5096`), its *Wrong reasons* bullet. **Making, demoting or
removing an owner is an owner's**, asked as ``workspace:delete`` (decision `#3808`). Where that was
refused for any reason but the role, the refusal still said *only an owner of metacortex makes,
demotes or removes an owner* - to an owner - and sent them to ask one. An owner working through the
agent tools is refused by the tools' ceiling, which says so and where to go instead.
"""

import dataclasses
import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors

users = subroutine.domain.users
workspaces = subroutine.domain.workspaces

User = subroutine.db.models.identity.User
Principal = subroutine.domain.authentication.Principal
Failure = subroutine.domain.authorization.AuthorizationFailure


class Workspace:
	"""MetaCortex: keanu founded it, carrieanne is a second owner, and hugo administers it."""

	def __init__ (self, session: sqlalchemy.orm.Session) -> None:
		"""Make the workspace and its three, through the domain."""

		self.session = session
		suffix = uuid.uuid4().hex[:6]
		self.keanu = users.create(session, username=f"keanu-{suffix}")
		self.carrieanne = users.create(session, username=f"carrieanne-{suffix}")
		self.hugo = users.create(session, username=f"hugo-{suffix}")
		self.metacortex = workspaces.create(
			session, slug=f"metacortex-{suffix}", title="Metacortex", owner=self.keanu
		)
		workspaces.add_member(session, self.metacortex, self.carrieanne, role_key="owner")
		workspaces.add_member(session, self.metacortex, self.hugo, role_key="admin")
		session.flush()

	def presenting (self, user: User) -> Principal:
		"""Return the principal an instance builds from a fresh credential of ``user``'s."""

		_row, issued = subroutine.domain.authentication.issue_token(
			self.session, user=user, title=f"{user.username}'s {uuid.uuid4().hex[:6]}"
		)
		self.session.flush()

		return subroutine.domain.authentication.authenticate(
			self.session, issued.value.get_secret_value(), record_use=False
		)

	def demote (self, actor: Principal) -> None:
		"""Ask, as ``actor``, to make carrieanne an ordinary member."""

		workspaces.set_member_role(
			self.session, self.metacortex, self.carrieanne, role_key="member", actor=actor
		)


def test_an_owner_through_the_agent_tools_is_refused_by_the_ceiling (
	session: sqlalchemy.orm.Session,
) -> None:
	"""keanu, an owner, demoting carrieanne through the agent tools, hears that the tools refused."""

	here = Workspace(session)
	through = dataclasses.replace(here.presenting(here.keanu), through_the_agent_tools=True)

	with pytest.raises(subroutine.domain.authorization.AuthorizationError) as refused:
		here.demote(through)

	assert refused.value.failure is Failure.ABOVE_THE_AGENT_TOOLS, refused.value.detail
	assert "Only an owner" not in str(refused.value.detail), refused.value.detail


def test_an_owner_on_a_read_only_session_is_refused_as_read_only (
	session: sqlalchemy.orm.Session,
) -> None:
	"""keanu on a read-only session hears that it only reads, never that only an owner may."""

	here = Workspace(session)
	reading = dataclasses.replace(here.presenting(here.keanu), read_only=True)

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		here.demote(reading)

	assert "Only an owner" not in str(refused.value.detail), refused.value.detail


def test_an_administrator_who_is_not_an_owner_still_hears_only_an_owner_may (
	session: sqlalchemy.orm.Session,
) -> None:
	"""hugo administers MetaCortex and is not an owner, so the role is what refused him."""

	here = Workspace(session)

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		here.demote(here.presenting(here.hugo))

	assert str(refused.value.detail).startswith(
		f"Only an owner of {here.metacortex.slug} makes, demotes or removes an owner."
	), refused.value.detail


def test_an_owner_holding_everything_still_may (session: sqlalchemy.orm.Session) -> None:
	"""keanu with a credential nothing narrows demotes carrieanne."""

	here = Workspace(session)
	here.demote(here.presenting(here.keanu))
