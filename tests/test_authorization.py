"""Tests for the one permission check, against both backends.

The matrix test is the point of this file: every seeded role against every permission,
asserting that :func:`authorize` agrees with what the role row actually says. The rest
cover the ways a permission system fails open — a sentinel read as an empty set, a token
that widens instead of narrowing, a private project that answers "forbidden" and thereby
confirms it exists.
"""

import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.seed
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.permissions


def _seeded_workspace (
	session: sqlalchemy.orm.Session,
) -> subroutine.db.models.identity.Workspace:
	"""Create a workspace with its full vocabulary, including the system roles."""

	workspace = subroutine.db.models.identity.Workspace(
		slug=f"ws-{uuid.uuid4().hex[:8]}", title="Test workspace"
	)
	subroutine.db.seed.seed_workspace(session, workspace)

	return workspace


def _role (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	key: str,
) -> subroutine.db.models.identity.Role:
	"""Return one of the workspace's seeded roles by key."""

	model = subroutine.db.models.identity.Role

	return session.scalars(
		sqlalchemy.select(model).where(model.workspace_id == workspace.id, model.key == key)
	).one()


def _user (
	session: sqlalchemy.orm.Session, **overrides: object
) -> subroutine.db.models.identity.User:
	"""Create a user."""

	name = f"user-{uuid.uuid4().hex[:8]}"
	fields: dict[str, object] = {"username": name, "username_normalized": name}
	fields.update(overrides)

	user = subroutine.db.models.identity.User(**fields)
	session.add(user)
	session.flush()

	return user


def _member (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	role_key: str,
) -> subroutine.domain.authentication.Principal:
	"""Create a user holding one of the seeded roles, and return them as a principal."""

	user = _user(session)

	session.add(
		subroutine.db.models.identity.WorkspaceMember(
			workspace_id=workspace.id,
			user_id=user.id,
			role_id=_role(session, workspace, role_key).id,
		)
	)
	session.flush()

	return subroutine.domain.authentication.Principal(user=user)


def _with_token (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	**kwargs: typing.Any,
) -> subroutine.domain.authentication.Principal:
	"""Return the same principal holding a token with the given narrowing."""

	token, _issued = subroutine.domain.authentication.issue_token(
		session,
		user=principal.user,
		title="Test token",
		**kwargs,
	)

	return subroutine.domain.authentication.Principal(user=principal.user, token=token)


def _project (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	*,
	key: str = "SR",
	parent: subroutine.db.models.project.Project | None = None,
	visibility: str = "public",
) -> subroutine.db.models.project.Project:
	"""Create a project with a materialised path that includes its own id."""

	status = session.scalars(
		sqlalchemy.select(subroutine.db.models.vocabulary.Status).where(
			subroutine.db.models.vocabulary.Status.workspace_id == workspace.id,
			subroutine.db.models.vocabulary.Status.entity_type == "project",
			subroutine.db.models.vocabulary.Status.key == "active",
		)
	).one()

	identifier = subroutine.db.types.new_uuid()
	prefix = "/" if parent is None else parent.path

	project = subroutine.db.models.project.Project(
		id=identifier,
		workspace_id=workspace.id,
		parent_id=None if parent is None else parent.id,
		visibility=visibility,
		key=f"{key}-{uuid.uuid4().hex[:4]}",
		title="Test project",
		status_id=status.id,
		path=f"{prefix}{identifier}/",
		depth=0 if parent is None else parent.depth + 1,
	)
	session.add(project)
	session.flush()

	return project


@pytest.mark.parametrize("role_key", ["owner", "admin", "member", "contributor", "viewer"])
def test_every_role_against_every_permission (
	session: sqlalchemy.orm.Session, role_key: str
) -> None:
	"""The check agrees with the role row, for all five roles and all seventeen verbs."""

	workspace = _seeded_workspace(session)
	principal = _member(session, workspace, role_key)
	granted = set(_role(session, workspace, role_key).permissions)

	for permission in sorted(subroutine.permissions.ALL):
		allowed = subroutine.domain.authorization.may(
			session, principal, permission, workspace_id=workspace.id
		)

		assert allowed == (permission in granted), f"{role_key} / {permission}"


def test_a_stranger_to_the_workspace_may_do_nothing (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Membership is the floor; there is no ambient access."""

	workspace = _seeded_workspace(session)
	principal = subroutine.domain.authentication.Principal(user=_user(session))

	with pytest.raises(subroutine.domain.authorization.AuthorizationError) as error:
		subroutine.domain.authorization.authorize(
			session, principal, subroutine.permissions.TASK_READ, workspace_id=workspace.id
		)

	assert (
		error.value.failure is subroutine.domain.authorization.AuthorizationFailure.NOT_A_MEMBER
	)
	assert error.value.status == 403
	assert error.value.code == "forbidden"


def test_an_empty_scope_list_narrows_nothing (session: sqlalchemy.orm.Session) -> None:
	"""SPEC.md §7.3's sentinel, stated as its own test because it is the failure mode."""

	workspace = _seeded_workspace(session)
	principal = _with_token(session, _member(session, workspace, "member"))

	assert principal.scopes == []

	granted = set(_role(session, workspace, "member").permissions)

	for permission in sorted(granted):
		assert subroutine.domain.authorization.may(
			session, principal, permission, workspace_id=workspace.id
		), f"the empty-scope sentinel denied {permission}"


def test_a_scoped_token_narrows_to_its_scopes (session: sqlalchemy.orm.Session) -> None:
	"""A non-empty list is a genuine restriction."""

	workspace = _seeded_workspace(session)
	principal = _with_token(
		session,
		_member(session, workspace, "member"),
		scopes=[subroutine.permissions.TASK_READ],
	)

	assert subroutine.domain.authorization.may(
		session, principal, subroutine.permissions.TASK_READ, workspace_id=workspace.id
	)

	with pytest.raises(subroutine.domain.authorization.AuthorizationError) as error:
		subroutine.domain.authorization.authorize(
			session, principal, subroutine.permissions.TASK_WRITE, workspace_id=workspace.id
		)

	assert (
		error.value.failure
		is subroutine.domain.authorization.AuthorizationFailure.OUT_OF_TOKEN_SCOPE
	)


def test_a_token_cannot_widen_its_owner (session: sqlalchemy.orm.Session) -> None:
	"""Scoping is an intersection, so naming a permission the role lacks grants nothing."""

	workspace = _seeded_workspace(session)
	principal = _with_token(
		session,
		_member(session, workspace, "viewer"),
		scopes=[subroutine.permissions.TASK_READ, subroutine.permissions.TASK_WRITE],
	)

	assert subroutine.domain.authorization.may(
		session, principal, subroutine.permissions.TASK_READ, workspace_id=workspace.id
	)
	assert not subroutine.domain.authorization.may(
		session, principal, subroutine.permissions.TASK_WRITE, workspace_id=workspace.id
	)


def test_a_superuser_bypasses_roles_but_not_token_scopes (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Otherwise a leaked admin-owned agent token would be unbounded (SPEC.md §7.3)."""

	workspace = _seeded_workspace(session)
	root = subroutine.domain.authentication.Principal(user=_user(session, is_superuser=True))

	# No membership row anywhere, and still permitted.
	assert subroutine.domain.authorization.may(
		session, root, subroutine.permissions.WORKSPACE_DELETE, workspace_id=workspace.id
	)

	scoped = _with_token(session, root, scopes=[subroutine.permissions.TASK_READ])

	assert subroutine.domain.authorization.may(
		session, scoped, subroutine.permissions.TASK_READ, workspace_id=workspace.id
	)
	assert not subroutine.domain.authorization.may(
		session, scoped, subroutine.permissions.WORKSPACE_DELETE, workspace_id=workspace.id
	)


def test_a_token_pinned_to_one_workspace_cannot_reach_another (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Pinning restricts where a credential works, whatever its owner may do there."""

	home = _seeded_workspace(session)
	elsewhere = _seeded_workspace(session)

	principal = _member(session, home, "owner")

	session.add(
		subroutine.db.models.identity.WorkspaceMember(
			workspace_id=elsewhere.id,
			user_id=principal.user.id,
			role_id=_role(session, elsewhere, "owner").id,
		)
	)
	session.flush()

	pinned = _with_token(session, principal, workspace_id=home.id)

	assert subroutine.domain.authorization.may(
		session, pinned, subroutine.permissions.TASK_READ, workspace_id=home.id
	)

	with pytest.raises(subroutine.domain.authorization.AuthorizationError) as error:
		subroutine.domain.authorization.authorize(
			session, pinned, subroutine.permissions.TASK_READ, workspace_id=elsewhere.id
		)

	assert (
		error.value.failure
		is subroutine.domain.authorization.AuthorizationFailure.WORKSPACE_MISMATCH
	)


def test_a_project_from_another_workspace_is_refused (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Passing a mismatched pair must not quietly check the wrong one."""

	home = _seeded_workspace(session)
	elsewhere = _seeded_workspace(session)
	principal = _member(session, home, "owner")
	foreign = _project(session, elsewhere)

	with pytest.raises(subroutine.domain.authorization.AuthorizationError) as error:
		subroutine.domain.authorization.authorize(
			session,
			principal,
			subroutine.permissions.TASK_READ,
			workspace_id=home.id,
			project=foreign,
		)

	assert (
		error.value.failure
		is subroutine.domain.authorization.AuthorizationFailure.WORKSPACE_MISMATCH
	)


def test_a_null_project_scope_restricts_nothing (session: sqlalchemy.orm.Session) -> None:
	"""The second sentinel: no list means every project the owner can reach."""

	workspace = _seeded_workspace(session)
	principal = _with_token(session, _member(session, workspace, "member"))
	project = _project(session, workspace)

	assert principal.project_scope is None
	assert subroutine.domain.authorization.may(
		session,
		principal,
		subroutine.permissions.TASK_READ,
		workspace_id=workspace.id,
		project=project,
	)


def test_a_project_scope_carries_the_whole_subtree (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Restricting to a project and then refusing its children would be useless."""

	workspace = _seeded_workspace(session)
	principal = _member(session, workspace, "member")

	parent = _project(session, workspace, key="PAR")
	child = _project(session, workspace, key="CHI", parent=parent)
	unrelated = _project(session, workspace, key="OTH")

	scoped = _with_token(session, principal, project_scope=[str(parent.id)])

	for permitted in (parent, child):
		assert subroutine.domain.authorization.may(
			session,
			scoped,
			subroutine.permissions.TASK_READ,
			workspace_id=workspace.id,
			project=permitted,
		)

	with pytest.raises(subroutine.domain.authorization.AuthorizationError) as error:
		subroutine.domain.authorization.authorize(
			session,
			scoped,
			subroutine.permissions.TASK_READ,
			workspace_id=workspace.id,
			project=unrelated,
		)

	assert (
		error.value.failure
		is subroutine.domain.authorization.AuthorizationFailure.OUT_OF_PROJECT_SCOPE
	)


def test_a_private_project_conceals_its_existence (session: sqlalchemy.orm.Session) -> None:
	"""SPEC.md §7.3a: a direct fetch answers 404, not 403."""

	workspace = _seeded_workspace(session)
	principal = _member(session, workspace, "owner")
	private = _project(session, workspace, visibility="private")

	with pytest.raises(subroutine.domain.authorization.ProjectNotVisible) as error:
		subroutine.domain.authorization.authorize(
			session,
			principal,
			subroutine.permissions.TASK_READ,
			workspace_id=workspace.id,
			project=private,
		)

	assert error.value.status == 404
	assert error.value.code == "not_found"
	assert "project" in error.value.detail.lower()

	# The mistake this guards against is a caller catching "the check said no" and
	# reporting it as forbidden, which would confirm the project is there.
	assert not isinstance(error.value, subroutine.domain.authorization.AuthorizationError)


def test_a_project_member_can_see_a_private_project (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The membership row is what makes a private project reachable at all."""

	workspace = _seeded_workspace(session)
	principal = _member(session, workspace, "member")
	private = _project(session, workspace, visibility="private")

	session.add(
		subroutine.db.models.project.ProjectMember(
			workspace_id=workspace.id, project_id=private.id, user_id=principal.user.id
		)
	)
	session.flush()

	assert subroutine.domain.authorization.may(
		session,
		principal,
		subroutine.permissions.TASK_WRITE,
		workspace_id=workspace.id,
		project=private,
	)


def test_a_project_role_replaces_the_workspace_role_there (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``project_member.role_id`` is documented as overriding; check that it does."""

	workspace = _seeded_workspace(session)
	principal = _member(session, workspace, "viewer")
	project = _project(session, workspace)

	assert not subroutine.domain.authorization.may(
		session,
		principal,
		subroutine.permissions.TASK_WRITE,
		workspace_id=workspace.id,
		project=project,
	)

	session.add(
		subroutine.db.models.project.ProjectMember(
			workspace_id=workspace.id,
			project_id=project.id,
			user_id=principal.user.id,
			role_id=_role(session, workspace, "member").id,
		)
	)
	session.flush()

	assert subroutine.domain.authorization.may(
		session,
		principal,
		subroutine.permissions.TASK_WRITE,
		workspace_id=workspace.id,
		project=project,
	)

	# And nowhere else: the override is scoped to the project it was granted on.
	assert not subroutine.domain.authorization.may(
		session, principal, subroutine.permissions.TASK_WRITE, workspace_id=workspace.id
	)


def test_an_unknown_permission_is_a_programming_error (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A typo must not read as a permission nobody happens to hold."""

	workspace = _seeded_workspace(session)
	principal = _member(session, workspace, "owner")

	with pytest.raises(ValueError) as error:
		subroutine.domain.authorization.may(
			session, principal, "task:reed", workspace_id=workspace.id
		)

	assert "task:reed" in str(error.value)
	assert subroutine.permissions.TASK_READ in str(error.value)


def test_explain_reports_where_the_answer_came_from (
	session: sqlalchemy.orm.Session,
) -> None:
	"""An agent should be able to read its own permissions rather than discover them."""

	workspace = _seeded_workspace(session)
	principal = _member(session, workspace, "contributor")

	plain = subroutine.domain.authorization.explain(session, principal, workspace.id)

	assert plain.from_role == "Contributor"
	assert not plain.narrowed_by_token
	assert subroutine.permissions.TASK_WRITE in plain.permissions
	assert subroutine.permissions.PROJECT_WRITE not in plain.permissions

	scoped = subroutine.domain.authorization.explain(
		session,
		_with_token(session, principal, scopes=[subroutine.permissions.TASK_READ]),
		workspace.id,
	)

	assert scoped.narrowed_by_token
	assert scoped.permissions == {subroutine.permissions.TASK_READ}


def test_effective_permissions_is_empty_for_a_stranger (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Nothing, rather than an error, so a caller can render an empty menu."""

	workspace = _seeded_workspace(session)
	stranger = subroutine.domain.authentication.Principal(user=_user(session))

	assert (
		subroutine.domain.authorization.effective_permissions(session, stranger, workspace.id)
		== frozenset()
	)
