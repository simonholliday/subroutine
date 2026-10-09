"""Every last one is held by one predicate: the people who could repair it - `SR#4569`.

Decision `#4526`, on G9 to G11 of the cold review of 2026-10-05: three "last one" checks counted three
different things. The workspace's administrators counted agents, so its founding owner could be
removed while only an agent administered it (G9); ownership had no rule at all, so the only owner
could step down or leave (G10); and ``unshare`` counted rows, those of people who had left included,
so the last member who could act could strand a private project (G11). Each is driven here as it was
measured, and refused.
"""

import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.projects
import subroutine.domain.repair
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors

workspaces = subroutine.domain.workspaces


def _person (session: sqlalchemy.orm.Session, name: str) -> subroutine.db.models.identity.User:
	"""Make a person with a name nobody else in the run has."""

	return subroutine.domain.users.create(session, username=f"{name}-{uuid.uuid4().hex[:6]}")


def _agent (
	session: sqlalchemy.orm.Session, name: str, person: subroutine.db.models.identity.User
) -> subroutine.db.models.identity.User:
	"""Make an agent that answers to ``person``."""

	return subroutine.domain.users.create(
		session,
		username=f"{name}-{uuid.uuid4().hex[:6]}",
		is_service_account=True,
		responsible_user_id=person.id,
	)


def _metacortex (
	session: sqlalchemy.orm.Session, owner: subroutine.db.models.identity.User
) -> subroutine.db.models.identity.Workspace:
	"""Make a workspace ``owner`` founded, and so owns and administers."""

	return workspaces.create(
		session, slug=f"metacortex-{uuid.uuid4().hex[:6]}", title="Metacortex", owner=owner
	)


def test_an_agent_administering_a_workspace_is_not_somebody_who_could_repair_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""G9: the founding owner was removed while only an agent administered the workspace.

	An agent stops when its person goes, so a workspace it alone administers is one departure from
	nobody. **Removing the last person who administers it is refused**, and so is moving them to a
	role that does not, whichever agent administers it beside them.
	"""

	keanu = _person(session, "keanu")
	workspace = _metacortex(session, keanu)
	dozer = _agent(session, "dozer", keanu)
	workspaces.add_member(session, workspace, dozer, role_key="admin")
	session.flush()

	assert subroutine.domain.repair.repairers(
		session, subroutine.domain.repair.Administration(workspace.id)
	) == {keanu.id}

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		workspaces.remove_member(session, workspace, keanu)

	assert "would be left with nobody who can administer it" in refused.value.detail

	with pytest.raises(subroutine.errors.ValidationError):
		workspaces.set_member_role(session, workspace, keanu, role_key="member")


def test_the_only_owner_who_can_act_makes_somebody_else_an_owner_first (
	session: sqlalchemy.orm.Session,
) -> None:
	"""G10: ownership had no rule, so the only owner could step down or leave, an administrator beside.

	**Refused, saying what to do first**, reversing that part of `#3808`; with another owner who can
	act, both are allowed. An owner who has left already is not somebody who could repair it, so an
	agent owning it beside them counts for nothing.
	"""

	keanu = _person(session, "keanu")
	carrie_anne = _person(session, "carrie-anne")
	workspace = _metacortex(session, keanu)
	workspaces.add_member(session, workspace, carrie_anne, role_key="admin")
	workspaces.add_member(session, workspace, _agent(session, "dozer", keanu), role_key="owner")
	session.flush()

	for leaving in (
		lambda: workspaces.remove_member(session, workspace, keanu),
		lambda: workspaces.set_member_role(session, workspace, keanu, role_key="admin"),
	):
		with pytest.raises(subroutine.errors.ValidationError) as refused:
			leaving()

		assert refused.value.detail.endswith("would be left with no owner who can act."), (
			refused.value.detail
		)
		assert refused.value.hint == "Make somebody else an owner there first."

	workspaces.set_member_role(session, workspace, carrie_anne, role_key="owner")
	workspaces.set_member_role(session, workspace, keanu, role_key="admin")
	session.flush()

	assert subroutine.domain.repair.repairers(
		session, subroutine.domain.repair.Ownership(workspace.id)
	) == {carrie_anne.id}


def test_deactivating_the_only_owner_is_allowed_and_named (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#4155`'s answer for every scope: a departure is allowed, and named before and after."""

	keanu = _person(session, "keanu")
	carrie_anne = _person(session, "carrie-anne")
	workspace = _metacortex(session, keanu)
	workspaces.add_member(session, workspace, carrie_anne, role_key="admin")
	session.flush()

	assert [one.workspace.id for one in workspaces.unowned(session, leaving=keanu)] == [
		workspace.id
	]
	assert workspaces.unowned(session) == []
	assert workspaces.unadministered(session, leaving=keanu) == [], "carrie-anne administers it"

	subroutine.domain.users.set_active(session, keanu, active=False)
	session.flush()

	assert [one.workspace.id for one in workspaces.unowned(session)] == [workspace.id]
	assert workspaces.unowned(session, leaving=keanu) == [], "already so, not his to name again"


def test_unsharing_counts_people_who_can_act_and_not_rows (
	session: sqlalchemy.orm.Session,
) -> None:
	"""G11: three rows, one of a person who left and one of an agent, and only one who could repair it.

	``unshare`` counted rows, so the last member who could act was taken out and the project was
	stranded with two rows nobody could use. **Refused**, and the one who could repair it is named.
	"""

	thomas = _person(session, "thomas")
	gone = _person(session, "gone")
	workspace = _metacortex(session, thomas)
	bot = _agent(session, "bot", thomas)

	for member in (gone, bot):
		workspaces.add_member(session, workspace, member, role_key="member")

	project = subroutine.domain.projects.create(
		session, workspace_id=workspace.id, key="secret", title="Redundancies",
		visibility="private", owner_id=gone.id,
	)

	for member in (thomas, bot):
		subroutine.domain.projects.share(session, project, member)

	gone.is_active = False
	session.flush()

	assert subroutine.domain.repair.repairers(session, project) == {thomas.id}

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.projects.unshare(session, project, thomas)

	assert refused.value.detail == (
		f"{thomas.username} is the only person who can see secret, so removing them would leave "
		"no person able to reach it."
	)


def test_unsharing_a_parent_is_refused_where_it_strands_a_project_inside_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Privacy inherits down the tree, so a row on a private parent is sight of everything in it.

	Jo can still see the parent and cannot see the child, which only Thomas holds a row on, so
	taking Thomas's row on the parent strands the child: refused, naming the child inside it.
	"""

	thomas = _person(session, "thomas")
	jo = _person(session, "jo")
	workspace = _metacortex(session, thomas)
	workspaces.add_member(session, workspace, jo, role_key="member")
	parent = subroutine.domain.projects.create(
		session, workspace_id=workspace.id, key="zion", title="Zion", visibility="private",
		owner_id=jo.id,
	)
	subroutine.domain.projects.share(session, parent, thomas)
	child = subroutine.domain.projects.create(
		session, workspace_id=workspace.id, key="dock", title="The dock", visibility="private",
		owner_id=thomas.id, parent=parent,
	)
	session.flush()

	assert subroutine.domain.repair.repairers(session, child) == {thomas.id}

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.projects.unshare(session, parent, thomas)

	assert f"the only person who can see {child.key}, inside {parent.key}," in refused.value.detail
