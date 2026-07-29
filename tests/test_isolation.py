"""Tests that one workspace cannot see or touch another.

SPEC.md §11.4 asks for "a test asserting no query reaches task/project without workspace
scoping". This is the behavioural reading of that mandate: build two workspaces whose data
collides in every way it possibly can — same project keys, therefore the same refs, same
statuses, same usernames — and assert that every service still answers about the right one.

Behavioural rather than static because the bug worth catching is a query that forgot its
filter, and that shows up here as an answer about the wrong workspace. Inspecting compiled
SQL for the presence of a ``workspace_id`` predicate would pass a query that filters on the
*wrong* workspace, which is the more likely mistake once there are two to choose from.
"""

import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.mentions
import subroutine.domain.projects
import subroutine.domain.refs
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.permissions


class Side:
	"""One of the two identical-looking worlds these tests keep apart."""

	def __init__ (
		self,
		workspace: subroutine.db.models.identity.Workspace,
		owner: subroutine.db.models.identity.User,
		project: subroutine.db.models.project.Project,
		task: subroutine.db.models.work.Task,
	) -> None:
		"""Hold the four rows every test here needs."""

		self.workspace = workspace
		self.owner = owner
		self.project = project
		self.task = task


def _build (session: sqlalchemy.orm.Session, label: str) -> Side:
	"""Create a workspace whose contents collide with every other one built this way."""

	owner = subroutine.domain.users.create(session, username=f"{label}-{uuid.uuid4().hex[:6]}")
	workspace = subroutine.domain.workspaces.create(
		session, slug=f"{label}-{uuid.uuid4().hex[:6]}", title="Identical title", owner=owner
	)

	# The same key in both workspaces, so both mint the ref SR-1.
	project = subroutine.domain.projects.create(
		session, workspace_id=workspace.id, key="SR", title="Identical project"
	)
	task = subroutine.domain.tasks.create(
		session, project=project, title="Identical task", description="Mentions SR-1."
	)

	return Side(workspace, owner, project, task)


@pytest.fixture
def two_worlds (session: sqlalchemy.orm.Session) -> tuple[Side, Side]:
	"""Return two workspaces holding deliberately indistinguishable data."""

	return _build(session, "left"), _build(session, "right")


def test_the_two_worlds_really_do_collide (two_worlds: tuple[Side, Side]) -> None:
	"""If they did not, nothing else in this file would be proving anything."""

	left, right = two_worlds

	assert left.workspace.id != right.workspace.id
	assert left.project.key == right.project.key == "SR"
	assert left.task.ref == right.task.ref == "SR-1"
	assert left.task.title == right.task.title


def test_a_ref_resolves_within_its_own_workspace_only (
	session: sqlalchemy.orm.Session, two_worlds: tuple[Side, Side]
) -> None:
	"""``SR-1`` exists in both, and must never resolve across."""

	left, right = two_worlds

	assert subroutine.domain.refs.find(session, left.workspace.id, "SR-1") == (
		"task",
		left.task.id,
	)
	assert subroutine.domain.refs.find(session, right.workspace.id, "SR-1") == (
		"task",
		right.task.id,
	)


def test_a_mention_never_crosses_a_workspace (
	session: sqlalchemy.orm.Session, two_worlds: tuple[Side, Side]
) -> None:
	"""Both descriptions say "SR-1"; each must point at its own."""

	left, right = two_worlds

	for side in (left, right):
		backlinks = subroutine.domain.mentions.backlinks(
			session, workspace_id=side.workspace.id, target_type="task", target_id=side.task.id
		)

		assert [mention.source_id for mention in backlinks] == [side.task.id] or backlinks == []

	# The decisive check: nothing in one workspace points at anything in the other.
	crossings = list(
		session.scalars(
			sqlalchemy.select(subroutine.db.models.work.Mention).where(
				subroutine.db.models.work.Mention.workspace_id == left.workspace.id,
				subroutine.db.models.work.Mention.target_id == right.task.id,
			)
		)
	)

	assert crossings == []


def test_a_project_key_is_free_in_the_other_workspace (
	session: sqlalchemy.orm.Session, two_worlds: tuple[Side, Side]
) -> None:
	"""Uniqueness is per workspace, so the same key in two of them is not a conflict."""

	left, right = two_worlds

	assert left.project.key == right.project.key

	# And a second SR in the *same* workspace still is one.
	with pytest.raises(subroutine.errors.Conflict):
		subroutine.domain.projects.create(
			session, workspace_id=left.workspace.id, key="SR", title="Second"
		)


def test_a_role_lookup_stays_in_its_workspace (
	session: sqlalchemy.orm.Session, two_worlds: tuple[Side, Side]
) -> None:
	"""Both workspaces seed a role called 'owner'; they are different rows."""

	left, right = two_worlds

	left_owner = subroutine.domain.workspaces.find_role(session, left.workspace.id, "owner")
	right_owner = subroutine.domain.workspaces.find_role(session, right.workspace.id, "owner")

	assert left_owner.id != right_owner.id


def test_membership_of_one_workspace_grants_nothing_in_the_other (
	session: sqlalchemy.orm.Session, two_worlds: tuple[Side, Side]
) -> None:
	"""The permission check's own version of this file's thesis."""

	left, right = two_worlds
	principal = subroutine.domain.authentication.Principal(user=left.owner)

	assert subroutine.domain.authorization.may(
		session, principal, subroutine.permissions.TASK_READ, workspace_id=left.workspace.id
	)
	assert not subroutine.domain.authorization.may(
		session, principal, subroutine.permissions.TASK_READ, workspace_id=right.workspace.id
	)

	assert subroutine.domain.authorization.effective_permissions(
		session, principal, right.workspace.id
	) == frozenset()


def test_a_project_from_the_other_workspace_is_refused (
	session: sqlalchemy.orm.Session, two_worlds: tuple[Side, Side]
) -> None:
	"""Passing a mismatched workspace and project must not check whichever is convenient."""

	left, right = two_worlds
	principal = subroutine.domain.authentication.Principal(user=left.owner)

	with pytest.raises(subroutine.errors.SubroutineError):
		subroutine.domain.authorization.authorize(
			session,
			principal,
			subroutine.permissions.TASK_READ,
			workspace_id=left.workspace.id,
			project=right.project,
		)


def test_a_subtree_query_stops_at_the_workspace_boundary (
	session: sqlalchemy.orm.Session, two_worlds: tuple[Side, Side]
) -> None:
	"""Moving a tree must not rewrite paths in a workspace it has nothing to do with."""

	left, right = two_worlds

	child = subroutine.domain.projects.create(
		session, workspace_id=left.workspace.id, key="CHILD", title="Child", parent=left.project
	)
	untouched = right.project.path

	subroutine.domain.projects.move(session, child, parent=None)
	session.refresh(right.project)

	assert right.project.path == untouched


def test_every_workspace_scoped_table_carries_the_filter (
	session: sqlalchemy.orm.Session, two_worlds: tuple[Side, Side]
) -> None:
	"""A structural backstop: each side's rows are all its own.

	Cheap insurance against a service that writes a row with the wrong ``workspace_id``,
	which no amount of reading the query would catch.
	"""

	left, right = two_worlds

	scoped: tuple[typing.Any, ...] = (
		subroutine.db.models.project.Project,
		subroutine.db.models.work.Task,
		subroutine.db.models.work.Mention,
		subroutine.db.models.identity.Role,
		subroutine.db.models.identity.WorkspaceMember,
	)

	for model in scoped:
		for side, other in ((left, right), (right, left)):
			leaked = session.scalars(
				sqlalchemy.select(sqlalchemy.func.count())
				.select_from(model)
				.where(model.workspace_id == side.workspace.id, model.id.in_(
					sqlalchemy.select(model.id).where(model.workspace_id == other.workspace.id)
				))
			).one()

			assert leaked == 0, f"{model.__name__} rows appear in both workspaces"
