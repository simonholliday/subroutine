"""An administrator refused by the tools' ceiling or a narrowed credential is told so - `SR#5315`.

A bullet of L-4 of the cold review of 2026-11-11 (`#5284`), *An administrator refused by the
ceiling is given the wrong reason*. **Who decides who sees a project, and who manages what is
owned, includes whoever administers the workspace** (decisions `#4509` and `#4525`), asked as
``workspace:admin``.
Every refusal of that was read as *not an administrator*, so laurence, who administers MetaCortex,
working through the agent tools or on a credential narrowed to the projects in question, was told
*Only the owner of 'hr', or somebody who administers this workspace, may ...*, and to ask keanu or
an administrator. **The refusal that kept him out says so now**, as `SR#5107` made the owner check
say it; hugo, whose role administers nothing, is still told who may.
"""

import collections.abc
import dataclasses
import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.comments
import subroutine.domain.projects
import subroutine.domain.saved
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors

projects = subroutine.domain.projects

User = subroutine.db.models.identity.User
Principal = subroutine.domain.authentication.Principal
Failure = subroutine.domain.authorization.AuthorizationFailure


class Metacortex:
	"""keanu founded MetaCortex and owns hr and web; laurence administers it; two are members.

	hr is private and shared with laurence and hugo, so laurence stewards it by administering the
	workspace and sees it; web is public; vault is laurence's own private project, which moving web
	beneath crosses a privacy boundary. carrieanne saved a view shared with the workspace and
	commented on a task in web.
	"""

	def __init__ (self, session: sqlalchemy.orm.Session) -> None:
		"""Make the workspace, its four people, its projects, a view and a comment."""

		self.session = session
		suffix = uuid.uuid4().hex[:6]
		self.keanu = subroutine.domain.users.create(session, username=f"keanu-{suffix}")
		self.laurence = subroutine.domain.users.create(session, username=f"laurence-{suffix}")
		self.carrieanne = subroutine.domain.users.create(session, username=f"carrieanne-{suffix}")
		self.hugo = subroutine.domain.users.create(session, username=f"hugo-{suffix}")
		self.workspace = subroutine.domain.workspaces.create(
			session, slug=f"metacortex-{suffix}", title="Metacortex", owner=self.keanu
		)
		subroutine.domain.workspaces.add_member(
			session, self.workspace, self.laurence, role_key="admin"
		)
		for member in (self.carrieanne, self.hugo):
			subroutine.domain.workspaces.add_member(
				session, self.workspace, member, role_key="member"
			)

		self.hr = self._project("hr", self.keanu, visibility="private")
		self.web = self._project("web", self.keanu, visibility="public")
		self.vault = self._project("vault", self.laurence, visibility="private")
		projects.share(session, self.hr, self.laurence, actor=self.full(self.keanu))
		projects.share(session, self.hr, self.hugo, actor=self.full(self.keanu))

		self.view = subroutine.domain.saved.create(
			session,
			workspace_id=self.workspace.id,
			title="Launch queue",
			arrangement="list",
			q="is:open",
			shared=True,
			actor=self.full(self.carrieanne),
		)
		task = subroutine.domain.tasks.create(
			session, project=self.web, title="Pick the launch date", actor=self.full(self.keanu)
		)
		self.comment = subroutine.domain.comments.create(
			session,
			entity_type="task",
			entity_id=task.id,
			body="Friday suits the agency.",
			actor=self.full(self.carrieanne),
		)
		session.flush()

	def _project (
		self, key: str, owner: User, *, visibility: str
	) -> subroutine.db.models.project.Project:
		"""Make a project ``owner`` owns, as the domain makes one."""

		return projects.create(
			self.session,
			workspace_id=self.workspace.id,
			key=key,
			title=key.title(),
			visibility=visibility,
			owner_id=owner.id,
		)

	def full (self, user: User) -> Principal:
		"""Return ``user`` acting on a credential nothing narrows."""

		return Principal(user=user)

	def presenting (self, user: User, *, narrowed: bool) -> Principal:
		"""Return the principal an instance builds from a fresh credential of ``user``'s.

		``narrowed`` limits it to the three projects every act here lands on, so the workspace,
		which administering it lands on, is beyond the credential's places.
		"""

		scope = [str(one.id) for one in (self.hr, self.web, self.vault)] if narrowed else None
		_row, issued = subroutine.domain.authentication.issue_token(
			self.session,
			user=user,
			title=f"{user.username}'s {uuid.uuid4().hex[:6]}",
			project_scope=scope,
		)
		self.session.flush()

		return subroutine.domain.authentication.authenticate(
			self.session, issued.value.get_secret_value(), record_use=False
		)


#: Each act laurence takes as somebody who administers MetaCortex, by what it decides.
ACTS: dict[str, collections.abc.Callable[[Metacortex, Principal], object]] = {
	"making hr public": lambda here, actor: projects.update(
		here.session, here.hr, visibility="public", actor=actor
	),
	"sharing hr": lambda here, actor: projects.share(
		here.session, here.hr, here.carrieanne, actor=actor
	),
	"unsharing somebody else from hr": lambda here, actor: projects.unshare(
		here.session, here.hr, here.hugo, actor=actor
	),
	"changing who owns hr": lambda here, actor: projects.update(
		here.session, here.hr, owner_id=here.laurence.id, actor=actor
	),
	"moving web under a private project": lambda here, actor: projects.move(
		here.session, here.web, parent=here.vault, actor=actor
	),
	"unsharing a view somebody else shared": lambda here, actor: subroutine.domain.saved.update(
		here.session, here.view, shared=False, given={"shared"}, actor=actor
	),
	"deleting somebody else's comment": lambda here, actor: subroutine.domain.comments.delete(
		here.session, here.comment, actor=actor
	),
}


@pytest.mark.parametrize("act", ACTS, ids=list(ACTS))
@pytest.mark.parametrize(
	("how", "failure"),
	[
		("through the agent tools", Failure.ABOVE_THE_AGENT_TOOLS),
		("narrowed", Failure.BEYOND_ITS_PLACES),
	],
	ids=["through the agent tools", "narrowed"],
)
def test_an_administrator_kept_out_by_the_credential_is_told_what_kept_them_out (
	session: sqlalchemy.orm.Session, act: str, how: str, failure: Failure
) -> None:
	"""laurence is refused in the words of what refused him, never sent to ask an administrator."""

	here = Metacortex(session)
	actor = here.presenting(here.laurence, narrowed=how == "narrowed")

	if how == "through the agent tools":
		actor = dataclasses.replace(actor, through_the_agent_tools=True)

	with pytest.raises(subroutine.domain.authorization.AuthorizationError) as refused:
		ACTS[act](here, actor)

	assert refused.value.failure is failure, refused.value.detail
	assert "administers this workspace" not in str(refused.value.detail), refused.value.detail
	assert "administers this workspace" not in str(refused.value.hint), refused.value.hint


@pytest.mark.parametrize("act", ACTS, ids=list(ACTS))
def test_an_administrator_on_a_full_credential_may (
	session: sqlalchemy.orm.Session, act: str
) -> None:
	"""The control: laurence with a credential nothing narrows takes each act."""

	here = Metacortex(session)

	ACTS[act](here, here.presenting(here.laurence, narrowed=False))


def test_a_member_whose_role_administers_nothing_is_still_told_who_may (
	session: sqlalchemy.orm.Session,
) -> None:
	"""hugo sees hr and is not its steward: his role is what refused him, so he hears who may."""

	here = Metacortex(session)

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		ACTS["making hr public"](here, here.presenting(here.hugo, narrowed=False))

	assert str(refused.value.detail) == (
		"Only the owner of 'hr', or somebody who administers this workspace, may make it public."
	), refused.value.detail
	assert refused.value.hint == (
		f"Ask {here.keanu.username}, or somebody who administers this workspace."
	), refused.value.hint
