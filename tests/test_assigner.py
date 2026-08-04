"""A task records who put it in somebody's queue, and only when that actually changed.

`#477`, decision `#473`. The column answers *"who assigned this to me"* — a plain question a
person asks of their own list, and what a hand-back reads. It is deliberately **not** the
history: the event log already carries every assignment change with its actor and its sequence,
and duplicating that here would be two records of one fact, which is this codebase's signature
defect.

The rule worth testing is the narrow one. Re-sending the same assignee is not a fresh act of
delegation, so a `PATCH` that happens to carry an unchanged ``assignee_id`` must leave the
assigner alone — otherwise anybody touching an unrelated field takes somebody else's name off
the record, silently.
"""

import uuid

import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.authentication
import subroutine.domain.projects
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces


def _world (
	session: sqlalchemy.orm.Session,
) -> tuple[
	subroutine.db.models.project.Project,
	subroutine.db.models.identity.Workspace,
	subroutine.db.models.identity.User,
]:
	"""Return a project to file into, its workspace, and the person who owns it.

	Built here rather than taken from a fixture because these tests need the *owner* by name —
	the assigner is the point, so a helper that only handed back a workspace would leave every
	test looking it up again.
	"""

	owner = subroutine.domain.users.create(
		session, username=f"owner-{uuid.uuid4().hex[:8]}", is_superuser=True
	)
	workspace = subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Test workspace", owner=owner
	)
	project = subroutine.domain.projects.create(
		session,
		workspace_id=workspace.id,
		key=f"P{uuid.uuid4().hex[:10].upper()}",
		title="Test project",
		actor=_principal(owner),
	)

	return project, workspace, owner


def _principal (
	user: subroutine.db.models.identity.User,
) -> subroutine.domain.authentication.Principal:
	"""Return an unnarrowed principal, so a scope refusal cannot be mistaken for this rule."""

	return subroutine.domain.authentication.Principal(user=user, token=None)


def test_assigning_at_creation_records_who_did_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Filing something already assigned records the filer as the assigner."""

	project, _workspace, owner = _world(session)

	task = subroutine.domain.tasks.create(
		session, project=project, title="Something for somebody",
		assignee_id=owner.id, actor=_principal(owner),
	)

	assert task.assignee_id == owner.id
	assert task.assigned_by_id == owner.id


def test_an_unassigned_task_names_no_assigner (
	session: sqlalchemy.orm.Session,
) -> None:
	"""An assigner with no assignee names nobody, so it stays null rather than crediting anyone."""

	project, _workspace, owner = _world(session)

	task = subroutine.domain.tasks.create(
		session, project=project, title="Nobody's yet", actor=_principal(owner),
	)

	assert task.assigned_by_id is None


def test_assigning_later_records_the_person_who_assigned_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The assigner is whoever made the change, not the person receiving the work."""

	project, workspace, owner = _world(session)
	other = subroutine.domain.users.create(
		session, username=f"other-{uuid.uuid4().hex[:8]}"
	)
	subroutine.domain.workspaces.add_member(
		session, workspace, other, role_key="contributor", actor=_principal(owner)
	)

	task = subroutine.domain.tasks.create(
		session, project=project, title="Unowned to begin with", actor=_principal(owner)
	)

	subroutine.domain.tasks.update(
		session, task=task, assignee_id=other.id, actor=_principal(owner)
	)

	assert task.assignee_id == other.id
	assert task.assigned_by_id == owner.id


def test_reassigning_moves_the_assigner_too (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A second delegation replaces the first — the column is current, not a history."""

	project, workspace, owner = _world(session)
	second = subroutine.domain.users.create(
		session, username=f"second-{uuid.uuid4().hex[:8]}"
	)
	subroutine.domain.workspaces.add_member(
		session, workspace, second, role_key="contributor", actor=_principal(owner)
	)

	task = subroutine.domain.tasks.create(
		session, project=project, title="Passed along",
		assignee_id=owner.id, actor=_principal(owner),
	)

	subroutine.domain.tasks.update(
		session, task=task, assignee_id=owner.id, actor=_principal(second)
	)

	# Unchanged assignee, so the original delegation stands and `second` has not taken it over.
	assert task.assigned_by_id == owner.id

	subroutine.domain.tasks.update(
		session, task=task, assignee_id=second.id, actor=_principal(second)
	)

	assert task.assigned_by_id == second.id


def test_an_unrelated_change_leaves_the_assigner_alone (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The rule this column most needs, and the one a naive write would get wrong.

	A ``PATCH`` that carries the same ``assignee_id`` alongside a title change is not an act of
	delegation. Rewriting the assigner on it would let anybody touching an unrelated field
	replace somebody else's name on the record without meaning to and without it showing.
	"""

	project, workspace, owner = _world(session)
	passer = subroutine.domain.users.create(
		session, username=f"passer-{uuid.uuid4().hex[:8]}"
	)
	subroutine.domain.workspaces.add_member(
		session, workspace, passer, role_key="contributor", actor=_principal(owner)
	)

	task = subroutine.domain.tasks.create(
		session, project=project, title="Before",
		assignee_id=owner.id, actor=_principal(owner),
	)

	subroutine.domain.tasks.update(
		session, task=task, title="After", assignee_id=owner.id, actor=_principal(passer)
	)

	assert task.title == "After"
	assert task.assigned_by_id == owner.id


def test_clearing_the_assignee_clears_the_assigner (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Nobody assigned it to nobody, so a name left behind would be a fact about no one."""

	project, _workspace, owner = _world(session)

	task = subroutine.domain.tasks.create(
		session, project=project, title="Briefly owned",
		assignee_id=owner.id, actor=_principal(owner),
	)

	subroutine.domain.tasks.update(
		session, task=task, assignee_id=None, actor=_principal(owner)
	)

	assert task.assignee_id is None
	assert task.assigned_by_id is None
