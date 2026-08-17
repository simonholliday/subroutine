"""Tests for the one permission check, against both backends.

The matrix test is the point of this file: every seeded role against every permission,
asserting that :func:`authorize` agrees with what the role row actually says. The rest
cover the ways a permission system fails open — a sentinel read as an empty set, a token
that widens instead of narrowing, a private project that answers "forbidden" and thereby
confirms it exists.
"""

import ast
import pathlib
import re
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.event
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.seed
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.documents
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

	for permission in sorted(subroutine.permissions.WORKSPACE_LEVEL):
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
	"""docs/design.md §7.3's sentinel, stated as its own test because it is the failure mode."""

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
	"""Otherwise a leaked admin-owned agent token would be unbounded (docs/design.md §7.3)."""

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

	parent = _project(session, workspace, key="par")
	child = _project(session, workspace, key="chi", parent=parent)
	unrelated = _project(session, workspace, key="oth")

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
	"""docs/design.md §7.3a: a direct fetch answers 404, not 403."""

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
	# reporting it as forbidden, which would confirm the project is there. Read through a
	# widened local because mypy can now prove the two classes are disjoint, and would
	# otherwise call the assertion unreachable rather than let it run.
	raised: Exception = error.value

	assert not isinstance(raised, subroutine.domain.authorization.AuthorizationError)


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


def test_explain_never_promises_more_than_authorize_grants (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The two used to disagree in four ways; a second answer is not worth having."""

	workspace = _seeded_workspace(session)
	elsewhere = _seeded_workspace(session)
	owner = _member(session, workspace, "owner")
	project = _project(session, workspace)
	private = _project(session, workspace, visibility="private")
	foreign = _project(session, elsewhere)

	pinned = _with_token(session, owner, workspace_id=workspace.id)
	scoped = _with_token(session, owner, project_scope=[str(project.id)])

	cases = (
		("pinned token, other workspace", pinned, elsewhere.id, None),
		("project outside the token's scope", scoped, workspace.id, private),
		("private project, no membership", owner, workspace.id, private),
		("project from another workspace", owner, workspace.id, foreign),
		("ordinary case", owner, workspace.id, project),
	)

	for label, principal, workspace_id, target in cases:
		granted = subroutine.domain.authorization.effective_permissions(
			session, principal, workspace_id, project=target
		)

		for permission in sorted(subroutine.permissions.WORKSPACE_LEVEL):
			allowed = subroutine.domain.authorization.may(
				session, principal, permission, workspace_id=workspace_id, project=target
			)

			assert (permission in granted) == allowed, f"{label}: {permission}"


def test_explain_does_not_re_read_the_role_for_every_permission (
	session: sqlalchemy.orm.Session,
) -> None:
	"""One question about a workspace costs a handful of queries, not one per verb.

	``explain`` resolves the role and then asks the decision function about each permission
	in turn. Every one of those looked the same role up again — seventeen round trips to
	answer a question whose input was computed before the loop started, and ``/v1/me``
	multiplies that by the number of workspaces the caller belongs to. Measured at 18 per
	workspace before ``known_role`` was threaded through, and 1 after.

	The bound is loose on purpose: this is here to catch the shape coming back, not to pin
	an exact number that a legitimate change would have to keep re-blessing.
	"""

	workspace = _seeded_workspace(session)
	owner = _member(session, workspace, "owner")
	session.flush()

	engine = session.get_bind()
	counted = 0

	def count (*_arguments: typing.Any) -> None:
		"""Tally one statement."""

		nonlocal counted

		counted += 1

	sqlalchemy.event.listen(engine, "before_cursor_execute", count)

	try:
		granted = subroutine.domain.authorization.effective_permissions(
			session, owner, workspace.id
		)

	finally:
		sqlalchemy.event.remove(engine, "before_cursor_execute", count)

	assert granted, "the measurement is worthless if the call did nothing"
	assert counted <= 5, (
		f"explain issued {counted} queries for {len(subroutine.permissions.WORKSPACE_LEVEL)} "
		f"permissions; it should resolve the role once."
	)


def test_a_pinned_or_project_scoped_token_reports_itself_as_narrowing (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Narrowing is not only about the scopes list."""

	workspace = _seeded_workspace(session)
	member = _member(session, workspace, "member")
	project = _project(session, workspace)

	pinned = subroutine.domain.authorization.explain(
		session, _with_token(session, member, workspace_id=workspace.id), workspace.id
	)
	scoped = subroutine.domain.authorization.explain(
		session,
		_with_token(session, member, project_scope=[str(project.id)]),
		workspace.id,
		project=project,
	)

	assert pinned.narrowed_by_token
	assert scoped.narrowed_by_token


def test_a_workspace_owner_holds_nothing_at_instance_level (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Owning a workspace is not owning the installation (docs/design.md §7.2)."""

	workspace = _seeded_workspace(session)
	owner = _member(session, workspace, "owner")

	assert subroutine.domain.authorization.instance_permissions(owner) == frozenset()

	for permission in sorted(subroutine.permissions.INSTANCE_LEVEL):
		assert not subroutine.domain.authorization.may_instance(owner, permission)

		with pytest.raises(subroutine.domain.authorization.AuthorizationError) as raised:
			subroutine.domain.authorization.authorize_instance(owner, permission)

		assert (
			raised.value.failure
			is subroutine.domain.authorization.AuthorizationFailure.NOT_A_SUPERUSER
		)
		assert raised.value.workspace_id is None


def test_a_superuser_holds_every_instance_permission (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The account ``init`` creates can create the second workspace and the second user."""

	superuser = subroutine.domain.authentication.Principal(
		user=_user(session, is_superuser=True)
	)

	assert (
		subroutine.domain.authorization.instance_permissions(superuser)
		== subroutine.permissions.INSTANCE_LEVEL
	)

	for permission in sorted(subroutine.permissions.INSTANCE_LEVEL):
		subroutine.domain.authorization.authorize_instance(superuser, permission)

		assert subroutine.domain.authorization.may_instance(superuser, permission)


def test_a_superuser_token_still_narrows_instance_permissions (
	session: sqlalchemy.orm.Session,
) -> None:
	"""An agent gets what its token says, not what its owner is (docs/design.md §7.3).

	This is what makes it safe to answer "yes, my agent may create workspaces": it may,
	if and only if the token says so.
	"""

	superuser = subroutine.domain.authentication.Principal(
		user=_user(session, is_superuser=True)
	)
	scoped = _with_token(
		session, superuser, scopes=[subroutine.permissions.INSTANCE_WORKSPACE_CREATE]
	)

	assert subroutine.domain.authorization.instance_permissions(scoped) == frozenset(
		{subroutine.permissions.INSTANCE_WORKSPACE_CREATE}
	)

	subroutine.domain.authorization.authorize_instance(
		scoped, subroutine.permissions.INSTANCE_WORKSPACE_CREATE
	)

	with pytest.raises(subroutine.domain.authorization.AuthorizationError) as raised:
		subroutine.domain.authorization.authorize_instance(
			scoped, subroutine.permissions.INSTANCE_USER_CREATE
		)

	assert (
		raised.value.failure
		is subroutine.domain.authorization.AuthorizationFailure.OUT_OF_TOKEN_SCOPE
	)


def test_an_unscoped_superuser_token_narrows_nothing (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The sentinel holds at instance level too: ``scopes == []`` is not "nothing"."""

	superuser = subroutine.domain.authentication.Principal(
		user=_user(session, is_superuser=True)
	)
	unscoped = _with_token(session, superuser, scopes=[])

	assert (
		subroutine.domain.authorization.instance_permissions(unscoped)
		== subroutine.permissions.INSTANCE_LEVEL
	)


def test_the_two_tiers_refuse_each_others_verbs (session: sqlalchemy.orm.Session) -> None:
	"""Asking the wrong entry point is a programming error, not a refusal."""

	workspace = _seeded_workspace(session)
	owner = _member(session, workspace, "owner")

	with pytest.raises(ValueError, match="Unknown instance permission"):
		subroutine.domain.authorization.authorize_instance(
			owner, subroutine.permissions.TASK_READ
		)

	with pytest.raises(ValueError, match="Unknown workspace permission"):
		subroutine.domain.authorization.authorize(
			session,
			owner,
			subroutine.permissions.INSTANCE_USER_CREATE,
			workspace_id=workspace.id,
		)


def test_a_superuser_gets_the_workspace_tier_and_not_more (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Bypassing roles grants the workspace verbs, never the instance ones."""

	workspace = _seeded_workspace(session)
	superuser = subroutine.domain.authentication.Principal(
		user=_user(session, is_superuser=True)
	)

	granted = subroutine.domain.authorization.effective_permissions(
		session, superuser, workspace.id
	)

	assert granted == subroutine.permissions.WORKSPACE_LEVEL


def _document_permissions () -> set[str]:
	"""Return the permissions ``domain/documents.py`` actually checks, read from its source.

	**Derived rather than listed**, which is the whole reason this guard is worth anything. A
	hand-kept copy of "which verbs gate a document" would be a second statement of the rule, and
	this codebase's record is that two copies agree until they do not — most recently eleven
	copies of a project key's normalisation, every one correct while they matched (`SR#508`).
	"""

	source = pathlib.Path(subroutine.domain.documents.__file__).read_text(encoding="utf-8")
	names = set(re.findall(r"subroutine\.permissions\.([A-Z_]+)", source))

	return {
		getattr(subroutine.permissions, name)
		for name in names
		if isinstance(getattr(subroutine.permissions, name, None), str)
	}


def test_every_permission_that_gates_a_document_says_so () -> None:
	"""`SR#703`. There is no `document:*` verb, and nothing said which verb stands in for one.

	**The failure this is written for happened, and it cost real work.** The agent on nuc14 read
	its grants, found no document permission, wrote a substantial measurement up as a *comment*
	rather than as the finding it was, and asked for its credential to be widened. It had held
	the capability throughout: `POST /v1/documents` with that exact credential answered 201.

	That is the worst shape a surface can have — not a refusal, but a true list a careful reader
	draws a false conclusion from. Refusing would have been better, because a refusal is
	something you argue with.
	"""

	gating = _document_permissions()

	assert gating, "read no permissions out of documents.py, so this is checking nothing"

	missing = gating - set(subroutine.permissions.COVERAGE)

	assert not missing, (
		f"{sorted(missing)} gate writes to documents and say nothing about it where an agent "
		f"reads them. Add an entry to permissions.COVERAGE naming what the verb really covers."
	)


def test_nothing_is_described_as_covering_something_it_does_not () -> None:
	"""`SR#405`: an allow-list needs the other direction, or a stale entry reads as a decision.

	Both halves are cheap here and neither is implied by the other — a permission that no longer
	exists, and a note that no longer says anything the name does not.
	"""

	gone = set(subroutine.permissions.COVERAGE) - subroutine.permissions.ALL

	assert not gone, f"{sorted(gone)} are described and are not permissions"

	for name, covers in subroutine.permissions.COVERAGE.items():
		prefix = name.split(":")[0]

		assert covers.replace(" ", "_") != f"{prefix}s", (
			f"{name} is described as {covers!r}, which is what its own name already says — a "
			f"note on every permission is §12.2a's column that says the same thing on every row"
		)


def test_a_described_permission_reads_as_a_permission_and_a_note () -> None:
	"""What a reader is actually handed, rather than what the map holds."""

	said = subroutine.permissions.described(
		[subroutine.permissions.COMMENT_WRITE, subroutine.permissions.TASK_WRITE]
	)

	assert said == ["comment:write", "task:write (tasks and documents)"]


#: The functions that decide whether a principal may do something. A verb reaching one of
#: these is enforced; a verb reaching none of them is a claim nothing stands behind.
_GATES = frozenset(
	{
		"authorize",
		"authorize_instance",
		"_permitted",
		"permitted",
		"_refusal",
		"_instance_refusal",
		"refuse_a_read_out_of_scope",
	}
)


def _verbs_reaching_a_gate (tree: pathlib.Path) -> dict[str, list[str]]:
	"""Return each permission constant passed to a gate, and where.

	Takes the tree as an argument so a synthetic offender can be fed to the real scanner —
	`#405`'s rule, after `test_actor_discipline` was found re-implementing its own detection
	inline and passing over a directory it never read.
	"""

	found: dict[str, list[str]] = {}

	for path in sorted(tree.rglob("*.py")):
		for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
			if not isinstance(node, ast.Call):
				continue

			function = node.func
			name = (
				function.attr
				if isinstance(function, ast.Attribute)
				else getattr(function, "id", "")
			)

			if name not in _GATES:
				continue

			# The verb may be an argument of the call or nested in a keyword, so the whole
			# call is walked rather than only its positional arguments.
			for inner in ast.walk(node):
				if isinstance(inner, ast.Attribute) and inner.attr.isupper():
					found.setdefault(inner.attr, []).append(f"{path.name}:{node.lineno}")

	return found


def _declared_permissions () -> dict[str, str]:
	"""Return every permission constant this module publishes, by attribute name."""

	return {
		name: value
		for name in dir(subroutine.permissions)
		if name.isupper()
		and isinstance(value := getattr(subroutine.permissions, name), str)
		and ":" in value
	}


def test_every_permission_is_enforced_or_says_why_not () -> None:
	"""`#930`. A verb this instance publishes means something, or records that it does not.

	**The census the cold review asked for** (`#927` H-3, and its own highest-leverage
	recommendation). It found eight of twenty verbs reaching no check at all — including
	``task:read`` and ``project:read``, so a token issued ``--scope task:delete`` read
	everything it could reach while ``/v1/me`` reported the one permission it held.

	A permission is a promise made to whoever reads ``/v1/me`` and to whoever types
	``--scope``. This is what stops the next one being added and forgotten.
	"""

	source = pathlib.Path(subroutine.permissions.__file__).parent
	gated = _verbs_reaching_a_gate(source)
	declared = _declared_permissions()

	unenforced = {
		value
		for name, value in declared.items()
		if name not in gated and value not in subroutine.permissions.NOT_ENFORCED
	}

	assert not unenforced, (
		f"These permissions are published and checked by nothing: {sorted(unenforced)}. "
		f"Enforce them, or record why not in permissions.NOT_ENFORCED with what removes "
		f"the entry."
	)


def test_no_permission_is_both_enforced_and_excused () -> None:
	"""The half that makes the list above a record rather than a place to park a verb.

	Every allow-list in this repository has this test, and `#405` is the pass that added them:
	an entry that has quietly become true again reads exactly like a considered decision, and
	nothing else will ever notice. Deleting the entry is what closes the work it names.
	"""

	source = pathlib.Path(subroutine.permissions.__file__).parent
	gated = _verbs_reaching_a_gate(source)
	declared = _declared_permissions()

	by_value = {value: name for name, value in declared.items()}
	stale = {
		value
		for value in subroutine.permissions.NOT_ENFORCED
		if by_value.get(value, "") in gated
	}

	assert not stale, (
		f"These are excused in permissions.NOT_ENFORCED and are now checked: {sorted(stale)}. "
		f"Delete the entry — it names what its own removal means."
	)


def test_the_scanner_reads_something () -> None:
	"""A floor, because a scan that reads nothing makes every entry look enforced.

	`#408`'s recorded pattern: a floor catches a scanner that read *nothing* and is blind to
	one that read most things. The two tests above are both satisfiable by an empty result —
	the first trivially, the second by every verb looking unenforced — so the count is what
	tells a working scan from a broken one.
	"""

	source = pathlib.Path(subroutine.permissions.__file__).parent
	gated = _verbs_reaching_a_gate(source)

	assert len(gated) >= 12, f"the scan found only {len(gated)} enforced permissions"


def test_membership_is_administered_by_the_verb_named_for_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#930`, from `#927` H-3. The verb named for the job gated nothing and another did it.

	``permissions.py`` describes ``user:admin`` as *"managing who belongs to this workspace —
	inviting, removing, changing a member's role"*, and ``COVERAGE`` publishes it as *"who
	belongs to this workspace"*. Both membership services checked ``workspace:admin``.

	**A no-op for every role and not for a token**, which is why it is worth changing rather
	than reworded: ``workspace:admin``, ``user:admin`` and ``token:admin`` are held by ``owner``
	and ``admin`` and by nobody else, so no seeded role changes hands — but a credential scoped
	to ``user:admin`` could not administer membership and one scoped to ``workspace:admin``
	could, which is backwards from both descriptions an operator can read.
	"""

	source = pathlib.Path(subroutine.permissions.__file__).parent
	gated = _verbs_reaching_a_gate(source)

	assert "USER_ADMIN" in gated, "user:admin is published as membership and checked nowhere"

	# The seeded roles must still be indistinguishable here, or this changed who can do what
	# rather than which verb says so.
	holders = {
		verb: {
			seed.key
			for seed in subroutine.db.seed._SYSTEM_ROLES
			if verb in seed.permissions
		}
		for verb in (
			subroutine.permissions.WORKSPACE_ADMIN,
			subroutine.permissions.USER_ADMIN,
		)
	}

	assert (
		holders[subroutine.permissions.WORKSPACE_ADMIN]
		== holders[subroutine.permissions.USER_ADMIN]
	), f"the correction moved a capability between roles: {holders}"
