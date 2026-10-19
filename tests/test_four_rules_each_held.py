"""Four rules of the permission model, each held by a test of its own - `SR#5124`.

M-22 of the cold review of 2026-10-17. Each of these could be broken with every test passing, the
permission matrix included, so each is pinned here as the rule it states:

* **Whoever an account answers to stops it, however far down**: a person stops their agent's own
  agent, and stands in for it as its owner; somebody it does not answer to does neither.
* **The permission is asked before the trash**: a caller who may not change an item is told that,
  not that it is in the trash, which is what one who may is told.
* **Nobody holds what nobody owns**: an owner column with nobody in it names no account to match.
* **A person manages what their agents own only while those can act**: an agent marked as having
  left is not managed as its person's own.
"""

import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.bootstrap
import subroutine.domain.projects
import subroutine.domain.saved
import subroutine.domain.stewardship
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors

Principal = subroutine.domain.authentication.Principal


class Chain:
	"""MetaCortex: carrieanne answers for claudia, who answers for scout; hugo answers for nobody's
	agents here, and joe is a viewer. Laurence founded it."""

	def __init__ (self, session: sqlalchemy.orm.Session) -> None:
		"""Make the accounts and the workspace, through the domain."""

		self.session = session
		suffix = uuid.uuid4().hex[:6]
		setup = subroutine.domain.bootstrap.initialise(
			session, username=f"laurence-{suffix}", instance_name="MetaCortex"
		)
		self.laurence = setup.user
		self.workspace = setup.workspace
		self.carrieanne = subroutine.domain.users.create(session, username=f"carrieanne-{suffix}")
		self.hugo = subroutine.domain.users.create(session, username=f"hugo-{suffix}")
		self.joe = subroutine.domain.users.create(session, username=f"joe-{suffix}")
		self.claudia = subroutine.domain.users.create(
			session,
			username=f"claudia-{suffix}",
			is_service_account=True,
			responsible_user_id=self.carrieanne.id,
		)
		self.scout = subroutine.domain.users.create(
			session,
			username=f"scout-{suffix}",
			is_service_account=True,
			responsible_user_id=self.claudia.id,
		)

		for member, role in (
			(self.carrieanne, "member"),
			(self.hugo, "member"),
			(self.joe, "viewer"),
			(self.claudia, "member"),
			(self.scout, "member"),
		):
			subroutine.domain.workspaces.add_member(session, self.workspace, member, role_key=role)

		session.flush()


@pytest.mark.parametrize(("who", "may"), [("carrieanne", True), ("hugo", False)])
def test_a_person_stops_their_agent_s_own_agent (
	session: sqlalchemy.orm.Session, who: str, may: bool
) -> None:
	"""Decision `#4516`: whoever an account answers to may stop it, through other agents too."""

	here = Chain(session)
	actor = Principal(user=getattr(here, who))

	if may:
		subroutine.domain.users.set_active(session, here.scout, active=False, actor=actor)

		assert here.scout.is_active is False

		return

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.users.set_active(session, here.scout, active=False, actor=actor)


@pytest.mark.parametrize(("who", "may"), [("carrieanne", True), ("hugo", False)])
def test_a_person_stands_in_for_their_agent_s_own_agent_as_owner (
	session: sqlalchemy.orm.Session, who: str, may: bool
) -> None:
	"""Decision `#4516`: and stands in for it as the owner of what it owns, two levels down."""

	here = Chain(session)
	view = subroutine.domain.saved.create(
		session,
		workspace_id=here.workspace.id,
		title="Scouting",
		arrangement="list",
		shared=True,
		actor=Principal(user=here.scout),
	)
	actor = Principal(user=getattr(here, who))

	if may:
		subroutine.domain.saved.delete(session, view, actor=actor)

		return

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.saved.delete(session, view, actor=actor)


@pytest.mark.parametrize(
	("who", "told"),
	[
		pytest.param("joe", subroutine.domain.authorization.AuthorizationError, id="a viewer, who may not"),
		pytest.param("hugo", subroutine.errors.ValidationError, id="a member, who may"),
	],
)
def test_the_permission_is_asked_before_the_trash (
	session: sqlalchemy.orm.Session, who: str, told: type[subroutine.errors.SubroutineError]
) -> None:
	"""Decisions `#4548` and `#4532`: a caller who may not change an item is told that first."""

	here = Chain(session)
	web = subroutine.domain.projects.create(
		session, workspace_id=here.workspace.id, key=f"web{uuid.uuid4().hex[:4]}", title="Web"
	)
	task = subroutine.domain.tasks.create(session, project=web, title="Fix the deploy script")
	subroutine.domain.tasks.delete(session, task)

	with pytest.raises(told) as refused:
		subroutine.domain.tasks.update(
			session, task, title="Fix it again", actor=Principal(user=getattr(here, who))
		)

	assert ("in the trash" in str(refused.value.detail)) is (told is subroutine.errors.ValidationError)


def test_nobody_holds_what_nobody_owns (session: sqlalchemy.orm.Session) -> None:
	"""An owner column with nobody in it names no account: a person holds it no more than anybody.

	Only an internal caller, which presents nobody, passes - as it does every check.
	"""

	here = Chain(session)

	assert subroutine.domain.stewardship.holds(Principal(user=here.hugo), None) is False
	assert subroutine.domain.stewardship.holds(Principal(user=here.hugo), here.hugo.id) is True
	assert subroutine.domain.stewardship.holds(None, None) is True

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.stewardship.refuse_unless_author(
			Principal(user=here.hugo), None, detail="Somebody else wrote this.", hint="Ask them."
		)


def test_a_person_manages_their_agents_only_while_they_can_act (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Decision `#4525`: whoever an agent answers to manages what it owns while it can act there.

	carrieanne manages what claudia and scout own as her own; once claudia is marked as having left,
	neither is managed as hers - scout stops with claudia - and what they own falls to whoever stands
	in, as a departed owner's things do.
	"""

	here = Chain(session)
	carrieanne = Principal(user=here.carrieanne)

	def managed () -> set[uuid.UUID]:
		"""Return whose things carrieanne manages as her own in MetaCortex."""

		return subroutine.domain.stewardship.managed_owners(
			session, carrieanne, workspace_id=here.workspace.id
		)

	assert managed() == {here.carrieanne.id, here.claudia.id, here.scout.id}

	subroutine.domain.users.set_active(session, here.claudia, active=False, actor=carrieanne)

	assert managed() == {here.carrieanne.id}
