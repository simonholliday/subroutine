"""Whoever an account answers to stops it, and manages what it owns, through the agent tools too.

`SR#5123`, M-21 of the cold review of 2026-10-17, and `SR#5183`, found building it. **Decision
`#4516` stands as built** (`#5097`): whoever an account answers to may stop it - revoke its
credentials and feeds, sign it out, mark it as having left - from every door, and starting anything
for it stays with the administrator of accounts. **Decision `#4525`**: what an account owns is
managed by it, by whoever it answers to, and by its place's administrator. ``docs/permissions.md``
said the first never went through the agent tools and the second only on one's own; the chain's
path asks nothing the tools refuse, and only the administrators' paths ask what they do. Each is
driven here as the agent's person through the agent tools, with an administrator at both doors.
"""

import typing
import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.calendars
import subroutine.domain.comments
import subroutine.domain.projects
import subroutine.domain.saved
import subroutine.domain.sessions
import subroutine.domain.tasks
import subroutine.domain.tokens
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors

User = subroutine.db.models.identity.User
Principal = subroutine.domain.authentication.Principal


class Chain:
	"""MetaCortex: laurence founded it and administers the installation, carrieanne administers the
	workspace, and hugo is a member, with claude, an agent answering to him and a member too."""

	def __init__ (self, session: sqlalchemy.orm.Session) -> None:
		"""Make the four accounts and the workspace, through the domain."""

		self.session = session
		suffix = uuid.uuid4().hex[:6]
		setup = subroutine.domain.bootstrap.initialise(
			session, username=f"laurence-{suffix}", instance_name="MetaCortex"
		)
		self.laurence = setup.user
		self.workspace = setup.workspace
		self.carrieanne = subroutine.domain.users.create(session, username=f"carrieanne-{suffix}")
		self.hugo = subroutine.domain.users.create(session, username=f"hugo-{suffix}")
		self.claude = subroutine.domain.users.create(
			session,
			username=f"claude-{suffix}",
			is_service_account=True,
			responsible_user_id=self.hugo.id,
		)

		for member, role in ((self.carrieanne, "admin"), (self.hugo, "member"), (self.claude, "member")):
			subroutine.domain.workspaces.add_member(session, self.workspace, member, role_key=role)

		session.flush()

	def at (self, name: str, *, tools: bool) -> Principal:
		"""Return one of the four acting, through the agent tools or at a terminal."""

		return Principal(user=getattr(self, name), through_the_agent_tools=tools)


#: Who acts on claude, through which door, and whether they may: hugo, whom claude answers to,
#: through the agent tools; and an administrator, at a terminal and through the agent tools.
STOPPERS = [
	pytest.param("hugo", True, True, id="hugo through the agent tools"),
	pytest.param("laurence", False, True, id="the administrator of accounts at a terminal"),
	pytest.param("laurence", True, False, id="the administrator of accounts through the agent tools"),
	pytest.param("carrieanne", False, False, id="the workspace's administrator at a terminal"),
]


def _stop (here: Chain, act: str, actor: Principal) -> None:
	"""Stop claude one way, as ``actor``."""

	session = here.session

	if act == "revoke its credential":
		token, _issued = subroutine.domain.authentication.issue_token(
			session, user=here.claude, title="claude's own"
		)
		subroutine.domain.tokens.revoke(session, token, actor=actor)

	elif act == "revoke its feed":
		feed, _issued = subroutine.domain.calendars.create(
			session, Principal(user=here.claude), workspace_id=here.workspace.id, title="Diary"
		)
		subroutine.domain.calendars.revoke(session, feed, actor=actor)

	elif act == "sign it out":
		subroutine.domain.sessions.sign_out_everywhere(session, user=here.claude, actor=actor)

	else:
		subroutine.domain.users.set_active(session, here.claude, active=False, actor=actor)


@pytest.mark.parametrize("act", ["revoke its credential", "revoke its feed", "sign it out", "mark it as having left"])
@pytest.mark.parametrize(("who", "tools", "may"), STOPPERS)
def test_whoever_an_agent_answers_to_stops_it_through_the_agent_tools (
	session: sqlalchemy.orm.Session, act: str, who: str, tools: bool, may: bool
) -> None:
	"""`SR#5123`: *"Through the agent tools, only by whoever it answers to."*"""

	here = Chain(session)

	if may:
		_stop(here, act, here.at(who, tools=tools))

		return

	with pytest.raises(subroutine.errors.Forbidden):
		_stop(here, act, here.at(who, tools=tools))


def test_whoever_an_agent_answers_to_starts_nothing_for_it (session: sqlalchemy.orm.Session) -> None:
	"""`SR#5123`: *"Only an administrator of accounts does this for another account."*"""

	here = Chain(session)

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.authentication.issue_token(
			session, user=here.claude, title="A spare", actor=here.at("hugo", tools=False)
		)

	subroutine.domain.authentication.issue_token(
		session, user=here.claude, title="A spare", actor=here.at("laurence", tools=False)
	)


#: Who manages what claude owns, through which door, and whether they may.
MANAGERS = [
	pytest.param("hugo", True, True, id="hugo through the agent tools"),
	pytest.param("carrieanne", False, True, id="the workspace's administrator at a terminal"),
	pytest.param("carrieanne", True, False, id="the workspace's administrator through the agent tools"),
]


def _manage (here: Chain, thing: str) -> typing.Callable[[Principal], object]:
	"""Make something claude owns, and return how to act on it as somebody else."""

	session = here.session
	claude = Principal(user=here.claude)

	if thing == "forget its shared view":
		view = subroutine.domain.saved.create(
			session,
			workspace_id=here.workspace.id,
			title="Deploys",
			arrangement="list",
			shared=True,
			actor=claude,
		)

		return lambda actor: subroutine.domain.saved.delete(session, view, actor=actor)

	notes = subroutine.domain.projects.create(
		session,
		workspace_id=here.workspace.id,
		key=f"notes{uuid.uuid4().hex[:4]}",
		title="Notes",
		owner_id=here.claude.id,
	)

	if thing == "make its project private":
		return lambda actor: subroutine.domain.projects.update(
			session, notes, visibility="private", actor=actor
		)

	task = subroutine.domain.tasks.create(session, project=notes, title="Book the venue", actor=claude)
	comment = subroutine.domain.comments.create(
		session, entity_type="task", entity_id=task.id, body="Booked for Friday.", actor=claude
	)

	return lambda actor: subroutine.domain.comments.delete(session, comment, actor=actor)


@pytest.mark.parametrize("thing", ["forget its shared view", "delete its comment", "make its project private"])
@pytest.mark.parametrize(("who", "tools", "may"), MANAGERS)
def test_whoever_an_owner_answers_to_manages_what_it_owns_through_the_agent_tools (
	session: sqlalchemy.orm.Session, thing: str, who: str, tools: bool, may: bool
) -> None:
	"""`SR#5183`: what an agent owns is its person's to manage through the agent tools too.

	The page said *"only on one's own"* of every act on somebody's own thing. ``manages`` and
	``stewards`` ask the owner's chain first and nothing of the door; only the administrator's half
	asks ``workspace:admin``, which the agent tools never hold.
	"""

	here = Chain(session)
	act = _manage(here, thing)

	if may:
		act(here.at(who, tools=tools))

		return

	with pytest.raises(subroutine.errors.Forbidden):
		act(here.at(who, tools=tools))


def test_a_private_project_s_stand_in_acts_through_the_agent_tools (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#5183`: *never as an administrator*, and not *only by its owner or whoever they answer to*.

	When a private project's owner can no longer act, anybody who can see it stands in (`SR#5099`),
	and nothing in that asks the door: keanu shares ``kp`` with hugo and leaves, and hugo makes it
	public through the agent tools. Before keanu left, hugo was refused.
	"""

	here = Chain(session)
	keanu = subroutine.domain.users.create(session, username=f"keanu-{uuid.uuid4().hex[:6]}")
	subroutine.domain.workspaces.add_member(session, here.workspace, keanu, role_key="member")
	kp = subroutine.domain.projects.create(
		session,
		workspace_id=here.workspace.id,
		key=f"kp{uuid.uuid4().hex[:4]}",
		title="Kp",
		visibility="private",
		owner_id=keanu.id,
	)
	subroutine.domain.projects.share(session, kp, here.hugo, actor=Principal(user=keanu))

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.projects.update(
			session, kp, visibility="public", actor=here.at("hugo", tools=True)
		)

	subroutine.domain.workspaces.remove_member(session, here.workspace, keanu)
	subroutine.domain.projects.update(session, kp, visibility="public", actor=here.at("hugo", tools=True))

	assert kp.visibility == "public"
