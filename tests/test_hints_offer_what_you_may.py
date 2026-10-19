"""A refusal's hint offers only an act this caller may take - `SR#5106`.

M-23 of the cold review of 2026-10-17 (`#5096`), its *Hints offer refused acts* bullet, against
decision `#4521`: *the hint offers only what this caller can do*. Three did not. Handing work to
somebody a private project hides it from offered the share to a credential that may not share;
removing the last viewer of a private project offered to make it public to a credential narrowed to
some projects, which may not; and a ring of links through something unseen offered to withdraw a
link with an unseen end, which is not the caller's to withdraw.
"""

import typing
import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.vocabulary
import subroutine.domain.authentication
import subroutine.domain.links
import subroutine.domain.projects
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors

users = subroutine.domain.users
workspaces = subroutine.domain.workspaces
projects = subroutine.domain.projects

User = subroutine.db.models.identity.User
Principal = subroutine.domain.authentication.Principal

#: What a credential holds that may file and change work and read projects, and share none.
WORKING = ["task:read", "task:write", "project:read"]


class Workspace:
	"""MetaCortex: keanu founded it, and carrieanne, hugo and joe are members.

	* keanu's ``vault`` is private and shared with nobody else.
	* joe's ``den`` is private and shared with carrieanne alone; joe has left, so carrieanne, who
	  can see it, stands in as its steward.
	"""

	def __init__ (self, session: sqlalchemy.orm.Session) -> None:
		"""Make the workspace, its members and the two private projects, through the domain."""

		self.session = session
		self.suffix = uuid.uuid4().hex[:6]
		self.keanu = users.create(session, username=f"keanu-{self.suffix}")
		self.carrieanne = users.create(session, username=f"carrieanne-{self.suffix}")
		self.hugo = users.create(session, username=f"hugo-{self.suffix}")
		self.joe = users.create(session, username=f"joe-{self.suffix}")
		self.metacortex = workspaces.create(
			session, slug=f"metacortex-{self.suffix}", title="Metacortex", owner=self.keanu
		)

		for member in (self.carrieanne, self.hugo, self.joe):
			workspaces.add_member(session, self.metacortex, member, role_key="member")

		self.vault = projects.create(
			session,
			workspace_id=self.metacortex.id,
			key=f"vault{self.suffix}",
			title="The vault",
			visibility="private",
			owner_id=self.keanu.id,
		)
		self.den = projects.create(
			session,
			workspace_id=self.metacortex.id,
			key=f"den{self.suffix}",
			title="The den",
			visibility="private",
			owner_id=self.joe.id,
		)
		projects.share(session, self.den, self.carrieanne)
		session.flush()
		users.set_active(session, self.joe, active=False)
		session.flush()

	def presenting (self, user: User, **narrowing: typing.Any) -> Principal:
		"""Return the principal an instance builds from a fresh credential of ``user``'s, narrowed so."""

		_row, issued = subroutine.domain.authentication.issue_token(
			self.session, user=user, title=f"{user.username}'s {uuid.uuid4().hex[:6]}", **narrowing
		)
		self.session.flush()

		return subroutine.domain.authentication.authenticate(
			self.session, issued.value.get_secret_value(), record_use=False
		)


@pytest.mark.parametrize("scoped", [False, True], ids=["holding everything", "unable to share"])
def test_handing_work_offers_the_share_only_to_a_credential_that_may_share (
	session: sqlalchemy.orm.Session, scoped: bool
) -> None:
	"""keanu assigns a task in his vault to hugo, who cannot see it: offered the share only if he may."""

	here = Workspace(session)
	keanu = here.presenting(here.keanu, scopes=WORKING) if scoped else here.presenting(here.keanu)
	task = subroutine.domain.tasks.create(
		session, project=here.vault, title="Count the gold", actor=keanu
	)
	session.flush()

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.tasks.update(session, task, assignee_id=here.hugo.id, actor=keanu)

	hint = refused.value.errors[0].hint

	if scoped:
		assert hint == "Choose somebody who can see it.", hint

	else:
		assert hint == (
			f"'subroutine project share {here.vault.key} {here.hugo.username}' first, or choose "
			"somebody who can see it."
		), hint


@pytest.mark.parametrize("narrowed", [False, True], ids=["holding everything", "narrowed to it"])
def test_removing_the_last_viewer_offers_publishing_only_to_a_credential_that_may (
	session: sqlalchemy.orm.Session, narrowed: bool
) -> None:
	"""carrieanne, standing in for joe, removes herself from his den, and would strand it.

	**A credential narrowed to some projects may share one and not make it public**, since who sees a
	project is decided at the workspace's place, so the hint offers it only the share.
	"""

	here = Workspace(session)
	carrieanne = (
		here.presenting(here.carrieanne, project_scope=[str(here.den.id)])
		if narrowed
		else here.presenting(here.carrieanne)
	)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		projects.unshare(session, here.den, here.carrieanne, actor=carrieanne)

	if narrowed:
		assert refused.value.hint == "Share it with somebody else first.", refused.value.hint

	else:
		assert refused.value.hint == (
			f"'subroutine project update {here.den.key} --public' first, or share it with somebody "
			"else."
		), refused.value.hint


@pytest.mark.parametrize("key", ["supersedes", "includes", "blocks", "precedes"])
def test_a_ring_nobody_here_may_break_offers_no_withdrawal (
	session: sqlalchemy.orm.Session, key: str
) -> None:
	"""Each kind of ring, where no link in it has both ends in sight, says who may break it."""

	here = Workspace(session)
	link_type = session.scalars(
		sqlalchemy.select(subroutine.db.models.vocabulary.LinkType).where(
			subroutine.db.models.vocabulary.LinkType.workspace_id == here.metacortex.id,
			subroutine.db.models.vocabulary.LinkType.key == key,
		)
	).one()

	for withdrawable in (False, True):
		said = subroutine.domain.links._why_a_ring_is_wrong(
			session,
			link_type,
			workspace_id=here.metacortex.id,
			stuck=key == "blocks",
			withdrawable=withdrawable,
		)

		assert ("Withdraw a link in that chain" in said) is withdrawable, said
		assert ("can see both ends of a link in that chain" in said) is not withdrawable, said
