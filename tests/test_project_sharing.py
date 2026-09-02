"""Letting somebody see a private project, and refusing to do it in the four ways that lie.

The defect this covers is not a wrong answer anywhere. §7.3a grants sight of a private
project to holders of a ``project_member`` row, the enforcement is correct, and the owner's
own row is written correctly — and until ``projects.share`` existed nothing wrote a second
one, on any surface. A project marked private was a project of one, permanently, and nothing
said so.

**Every refusal here is a way of doing nothing that looks like doing something.** A row for
somebody outside the workspace is written and grants nothing; a row on a project whose parent
is private is written and grants nothing; a row on a public project grants nothing because
everybody could already see it. Each is silent, so each is refused by name.

The fixtures come from ``test_authorization`` rather than being written again — that
file already builds a seeded workspace, a member holding a named role and a project with a
materialised path, which is the whole of what this needs.
"""

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.projects
import subroutine.domain.scoping
import subroutine.errors
import subroutine.permissions
import test_authorization


def _can_see (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	project: subroutine.db.models.project.Project,
) -> bool:
	"""Report whether a principal reaches a project through the statement every listing uses.

	Asked of ``scoping.readable_projects`` rather than of ``ProjectMember`` directly, because
	the row existing is not the claim being made — the claim is that the person can now see
	the project, and every listing in the application derives what it shows from here.
	"""

	rows = session.scalars(
		subroutine.domain.scoping.readable_projects(
			principal, workspace_ids=[project.workspace_id]
		)
	)

	return project.id in {row.id for row in rows}


def _outsider (
	session: sqlalchemy.orm.Session, workspace: subroutine.db.models.identity.Workspace
) -> subroutine.domain.authentication.Principal:
	"""Return somebody in the workspace who is not in any private project in it."""

	return test_authorization._member(session, workspace, "member")


def _owned (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	owner: subroutine.domain.authentication.Principal,
	*,
	parent: subroutine.db.models.project.Project | None = None,
	visibility: str = "private",
) -> subroutine.db.models.project.Project:
	"""Create a project owned by one principal, with their membership row written.

	That row is what ``projects.create`` writes for a real creator, so a test that omitted it
	would be measuring against a project its own owner cannot see.
	"""

	project = test_authorization._project(
		session, workspace, parent=parent, visibility=visibility
	)
	project.owner_id = owner.user.id

	session.add(
		subroutine.db.models.project.ProjectMember(
			workspace_id=workspace.id,
			project_id=project.id,
			user_id=owner.user.id,
			role_id=None,
		)
	)
	session.flush()

	return project


def test_sharing_a_private_project_lets_somebody_see_it_who_could_not (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The defect itself: before this function existed there was no way to reach this state.

	Measured both sides of the write, through the statement every listing narrows with, so a
	membership row that was written and granted nothing would fail the second assertion.
	"""

	workspace = test_authorization._seeded_workspace(session)
	owner = test_authorization._member(session, workspace, "owner")
	colleague = _outsider(session, workspace)
	project = _owned(session, workspace, owner)

	assert not _can_see(session, colleague, project), "the fixture is not private"

	subroutine.domain.projects.share(session, project, colleague.user, actor=owner)

	assert _can_see(session, colleague, project)


def test_sharing_grants_sight_and_never_authority (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A shared-in person keeps exactly the workspace role they arrived with.

	The row is written with ``role_id=None`` deliberately, and ``authorization._role_for``
	*does* read that column — a project role replaces the workspace one where it is set. So
	writing anything there would quietly re-grade somebody as a side effect of being shown a
	project, which is the conflict that moved roles out of this work altogether.
	"""

	workspace = test_authorization._seeded_workspace(session)
	owner = test_authorization._member(session, workspace, "owner")
	viewer = test_authorization._member(session, workspace, "viewer")
	project = _owned(session, workspace, owner)

	membership = subroutine.domain.projects.share(session, project, viewer.user, actor=owner)

	assert membership.role_id is None

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.authorization.authorize(
			session,
			viewer,
			subroutine.permissions.PROJECT_WRITE,
			workspace_id=workspace.id,
			project=project,
		)


def test_a_project_everybody_can_already_see_is_refused_rather_than_shared (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Sharing a public project writes a row that changes nobody's answer.

	Refused rather than allowed-and-inert, because the caller has said something about who
	should see this and the honest reply is that the question does not arise yet.
	"""

	workspace = test_authorization._seeded_workspace(session)
	owner = test_authorization._member(session, workspace, "owner")
	colleague = _outsider(session, workspace)
	project = _owned(session, workspace, owner, visibility="public")

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.projects.share(session, project, colleague.user, actor=owner)

	assert "visible to everybody" in str(refusal.value)
	assert "--private" in str(refusal.value.hint or "")


def test_a_project_hidden_by_its_parent_names_the_parent_as_the_one_to_share (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The unguessable case, and the one where doing what was asked would achieve nothing.

	Privacy inherits down the tree, so a **public** child of a private parent is hidden — and
	a membership row on the child grants nothing at all while that is true. Naming the
	ancestor is the difference between a refusal and a silent no-op.
	"""

	workspace = test_authorization._seeded_workspace(session)
	owner = test_authorization._member(session, workspace, "owner")
	colleague = _outsider(session, workspace)

	parent = _owned(session, workspace, owner)
	child = _owned(session, workspace, owner, parent=parent, visibility="public")

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.projects.share(session, child, colleague.user, actor=owner)

	assert parent.key in str(refusal.value)
	assert child.key in str(refusal.value)


def test_a_private_subtree_opens_one_deliberate_step_at_a_time (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Sharing the outermost private project does not hand over a private one inside it.

	``hidden_by`` returns the **outermost** private ancestor this person has no row for, so
	after the parent is shared the child is blocked by its own privacy and is offered as the
	next step rather than coming along silently.
	"""

	workspace = test_authorization._seeded_workspace(session)
	owner = test_authorization._member(session, workspace, "owner")
	colleague = _outsider(session, workspace)

	parent = _owned(session, workspace, owner)
	child = _owned(session, workspace, owner, parent=parent)

	subroutine.domain.projects.share(session, parent, colleague.user, actor=owner)

	assert _can_see(session, colleague, parent)
	assert not _can_see(session, colleague, child)

	subroutine.domain.projects.share(session, child, colleague.user, actor=owner)

	assert _can_see(session, colleague, child)


def test_a_private_project_inside_a_private_one_names_the_outer_one (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The case that decides which ancestor ``hidden_by`` returns, and the only one that does.

	**Added because reversing that loop to innermost-first passed every other test here.**
	Where the child is public, or where the parent has already been shared, both orders give
	the same answer — the two only differ when parent *and* child are private and the person
	holds a row on neither. Then outermost is the correct one: sharing the child grants
	nothing at all while the parent is hiding it, so naming the child would be a refusal that
	sends somebody to a command that does nothing.
	"""

	workspace = test_authorization._seeded_workspace(session)
	owner = test_authorization._member(session, workspace, "owner")
	colleague = _outsider(session, workspace)

	parent = _owned(session, workspace, owner)
	child = _owned(session, workspace, owner, parent=parent)

	blocking = subroutine.domain.projects.hidden_by(session, child, colleague.user.id)

	assert blocking is not None, "a private child of a private parent is hidden from them"
	assert blocking.key == parent.key, "the outermost private ancestor, not the innermost"

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.projects.share(session, child, colleague.user, actor=owner)

	assert parent.key in str(refusal.value)


def test_sharing_with_somebody_outside_the_workspace_is_refused_by_name (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``ProjectMember`` has no workspace check, and reach comes from workspace membership.

	So the row would be written, the person would go on seeing nothing, and nothing anywhere
	would say why. The refusal names the command that fixes it, because the remedy is one
	step and it is not this one.
	"""

	workspace = test_authorization._seeded_workspace(session)
	owner = test_authorization._member(session, workspace, "owner")
	stranger = test_authorization._user(session)
	project = _owned(session, workspace, owner)

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.projects.share(session, project, stranger, actor=owner)

	assert stranger.username in str(refusal.value)
	assert "user add" in str(refusal.value.hint or "")


def test_somebody_already_shared_in_is_refused_rather_than_given_a_second_row (
	session: sqlalchemy.orm.Session,
) -> None:
	"""And the refusal must say *they can already see it*, not *it is already visible*.

	The membership is checked before ``hidden_by`` is asked, because somebody already shared
	in is not hidden from the project — so the later question would answer about the workspace
	at large and report that the project is public, which it is not.
	"""

	workspace = test_authorization._seeded_workspace(session)
	owner = test_authorization._member(session, workspace, "owner")
	colleague = _outsider(session, workspace)
	project = _owned(session, workspace, owner)

	subroutine.domain.projects.share(session, project, colleague.user, actor=owner)

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.projects.share(session, project, colleague.user, actor=owner)

	assert "can already see" in str(refusal.value)


def test_unsharing_takes_sight_away_again (session: sqlalchemy.orm.Session) -> None:
	"""A membership that can only be granted is one whose mistakes are permanent."""

	workspace = test_authorization._seeded_workspace(session)
	owner = test_authorization._member(session, workspace, "owner")
	colleague = _outsider(session, workspace)
	project = _owned(session, workspace, owner)

	subroutine.domain.projects.share(session, project, colleague.user, actor=owner)
	assert _can_see(session, colleague, project)

	subroutine.domain.projects.unshare(session, project, colleague.user, actor=owner)
	assert not _can_see(session, colleague, project)


def test_the_owner_cannot_be_removed_from_their_own_project (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``create`` writes the owner's row so that making a project private later does not lock
	them out. Removing it is that sentence undone one command along.
	"""

	workspace = test_authorization._seeded_workspace(session)
	owner = test_authorization._member(session, workspace, "owner")
	colleague = _outsider(session, workspace)
	project = _owned(session, workspace, owner)

	subroutine.domain.projects.share(session, project, colleague.user, actor=owner)

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.projects.unshare(session, project, owner.user, actor=owner)

	assert "owns" in str(refusal.value)
	assert _can_see(session, owner, project)


def test_the_last_person_who_can_see_a_project_cannot_be_removed (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A private project with no member is one nobody can see or make public again.

	Driven as somebody removing **themselves**, which is the only way to reach this state: an
	administrator cannot help, because a private project is invisible to anybody without a row
	whatever their role. The project is left without an owner so the refusal reached is this
	one rather than the owner's, which would pass for the wrong reason.
	"""

	workspace = test_authorization._seeded_workspace(session)
	owner = test_authorization._member(session, workspace, "owner")
	colleague = _outsider(session, workspace)

	project = _owned(session, workspace, owner)
	subroutine.domain.projects.share(session, project, colleague.user, actor=owner)

	# The owner's row goes through the database rather than the guarded verb, so what is left
	# is one membership held by somebody who does not own the project.
	session.execute(
		sqlalchemy.delete(subroutine.db.models.project.ProjectMember).where(
			subroutine.db.models.project.ProjectMember.project_id == project.id,
			subroutine.db.models.project.ProjectMember.user_id == owner.user.id,
		)
	)
	project.owner_id = None
	session.flush()

	with pytest.raises(subroutine.errors.ValidationError) as refusal:
		subroutine.domain.projects.unshare(session, project, colleague.user, actor=colleague)

	assert "only person" in str(refusal.value)
	assert _can_see(session, colleague, project)


def test_unsharing_somebody_who_was_never_shared_in_is_refused_by_name (
	session: sqlalchemy.orm.Session,
) -> None:
	"""And the refusal names the command that would have done it, so the pair turn each other
	down rather than both reporting nothing.
	"""

	workspace = test_authorization._seeded_workspace(session)
	owner = test_authorization._member(session, workspace, "owner")
	colleague = _outsider(session, workspace)
	project = _owned(session, workspace, owner)

	with pytest.raises(subroutine.errors.NotFound) as refusal:
		subroutine.domain.projects.unshare(session, project, colleague.user, actor=owner)

	assert "project share" in str(refusal.value.hint or "")


def test_a_membership_survives_a_project_going_public_and_back (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Privacy is not a one-way door in either direction.

	Sharing is refused on a public project, so if the rows were dropped when one was published
	a person would have to be shared in again afterwards — and nothing would say that had
	happened. ``unshare`` therefore works whatever the visibility is, and publishing touches
	no row.
	"""

	workspace = test_authorization._seeded_workspace(session)
	owner = test_authorization._member(session, workspace, "owner")
	colleague = _outsider(session, workspace)
	project = _owned(session, workspace, owner)

	subroutine.domain.projects.share(session, project, colleague.user, actor=owner)

	subroutine.domain.projects.update(session, project, visibility="public", actor=owner)
	assert _can_see(session, colleague, project)

	subroutine.domain.projects.update(session, project, visibility="private", actor=owner)
	assert _can_see(session, colleague, project), "the membership did not survive"


def test_sharing_needs_permission_to_write_the_project (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Both verbs are gated by ``project:write``, and a viewer does not hold it.

	The gate is the same on both halves deliberately: anybody who can share can already
	publish the whole project by changing its visibility, so they are trusted with its reach
	in both directions. The known cost is that anybody shared in can evict anybody but the
	owner — which is why the viewer here is one who **can** see the project. Somebody who
	cannot gets a different answer, and the test below is about that.
	"""

	workspace = test_authorization._seeded_workspace(session)
	owner = test_authorization._member(session, workspace, "owner")
	viewer = test_authorization._member(session, workspace, "viewer")
	colleague = _outsider(session, workspace)
	newcomer = _outsider(session, workspace)
	project = _owned(session, workspace, owner)

	subroutine.domain.projects.share(session, project, viewer.user, actor=owner)
	subroutine.domain.projects.share(session, project, colleague.user, actor=owner)

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.projects.share(session, project, newcomer.user, actor=viewer)

	with pytest.raises(subroutine.errors.Forbidden):
		subroutine.domain.projects.unshare(session, project, colleague.user, actor=viewer)


def test_somebody_who_cannot_see_the_project_is_told_it_is_not_there (
	session: sqlalchemy.orm.Session,
) -> None:
	"""§7.3a's concealment rule, reaching the new verbs unchanged.

	A refusal naming a permission would confirm the project exists, which is the disclosure
	privacy is for. So the answer to somebody outside a private project is that there is no
	such project — the same answer they get from every listing, and a different exception
	from the one a member without ``project:write`` gets.
	"""

	workspace = test_authorization._seeded_workspace(session)
	owner = test_authorization._member(session, workspace, "owner")
	colleague = _outsider(session, workspace)
	project = _owned(session, workspace, owner)

	with pytest.raises(subroutine.domain.authorization.ProjectNotVisible):
		subroutine.domain.projects.share(session, project, owner.user, actor=colleague)


def test_who_can_see_a_project_is_answerable_and_nothing_else_answers_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The read half. A public project reports its owner, which is honest rather than empty:
	those are the people who would still see it if somebody made it private.
	"""

	workspace = test_authorization._seeded_workspace(session)
	owner = test_authorization._member(session, workspace, "owner")
	colleague = _outsider(session, workspace)
	project = _owned(session, workspace, owner)

	subroutine.domain.projects.share(session, project, colleague.user, actor=owner)

	rows = subroutine.domain.projects.members(session, project, actor=owner)

	assert {account.username for _row, account in rows} == {
		owner.user.username,
		colleague.user.username,
	}

	# **Oldest first**, so the answer reads as the order people were let in rather than as
	# whatever the database happened to return.
	assert next(account.username for _row, account in rows) == owner.user.username
