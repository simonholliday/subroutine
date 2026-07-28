"""Deciding whether a principal may do a particular thing in a particular place.

One entry point, because a permission system with several is a permission system with
several answers. Everything that needs to know goes through :func:`authorize` or
:func:`may`, and both are built on the same private decision.

SPEC.md §7.3 states the rule as set intersection::

    effective = role_permissions(user, workspace)
              ∩ token_scopes           (if the token narrows them)
              ∩ token_project_scope    (restricts which rows, not which verbs)

with one exception that has to be stated in the code as loudly as in the spec: **an empty
``scopes`` list and a null ``project_scope`` mean "no narrowing", not "no permissions"**.
Read as literal set algebra, the formula gives every ordinary token nothing at all — which
is the single easiest way to ship an API where everything is refused.
"""

import dataclasses
import enum
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.authentication
import subroutine.errors
import subroutine.permissions

#: Separates the segments of a project's materialised path.
PATH_SEPARATOR = "/"


class AuthorizationFailure(enum.StrEnum):
	"""Why an action was refused.

	Recorded for the log, and used by the API to choose a status code. Only one of these
	is ever reported to the caller in any detail — see :attr:`conceals_existence`.
	"""

	WORKSPACE_MISMATCH = "workspace_mismatch"
	NOT_A_MEMBER = "not_a_member"
	ROLE_LACKS_PERMISSION = "role_lacks_permission"
	OUT_OF_TOKEN_SCOPE = "out_of_token_scope"
	OUT_OF_PROJECT_SCOPE = "out_of_project_scope"
	PROJECT_INVISIBLE = "project_invisible"

	@property
	def conceals_existence (self) -> bool:
		"""Report whether this refusal must be reported as "not found".

		A private project answers ``404`` rather than ``403`` (SPEC.md §7.3a, §8.7):
		telling someone they are forbidden confirms the thing is there, which is precisely
		what private means they should not learn.
		"""

		return self is AuthorizationFailure.PROJECT_INVISIBLE


#: What to tell the caller for each refusal. Every one of these is safe to say to the
#: person it is said to: it describes their own role and their own token, which they may
#: already inspect, and never anything about what they were reaching for.
_EXPLANATIONS: dict[AuthorizationFailure, str] = {
	AuthorizationFailure.WORKSPACE_MISMATCH: (
		"The token you used is pinned to a different workspace."
	),
	AuthorizationFailure.NOT_A_MEMBER: "You are not a member of this workspace.",
	AuthorizationFailure.ROLE_LACKS_PERMISSION: (
		"This needs the {permission!r} permission, which your role here does not include."
	),
	AuthorizationFailure.OUT_OF_TOKEN_SCOPE: (
		"This needs the {permission!r} permission. Your role allows it, but the token you "
		"used is scoped to a narrower set."
	),
	AuthorizationFailure.OUT_OF_PROJECT_SCOPE: (
		"The token you used is scoped to a different set of projects."
	),
}

_HINTS: dict[AuthorizationFailure, str] = {
	AuthorizationFailure.OUT_OF_TOKEN_SCOPE: (
		"Use a token that includes {permission!r}, or one with no scope restriction at all."
	),
	AuthorizationFailure.WORKSPACE_MISMATCH: (
		"Use a token issued without a workspace, or one issued for this workspace."
	),
}


class AuthorizationError(subroutine.errors.Forbidden):
	"""Raised when a principal may not do what they asked to do."""

	def __init__ (
		self,
		failure: AuthorizationFailure,
		*,
		permission: str,
		workspace_id: uuid.UUID,
		project_id: uuid.UUID | None = None,
	) -> None:
		"""Record what was refused, and where, and say something useful about it."""

		hint = _HINTS.get(failure)

		super().__init__(
			_EXPLANATIONS[failure].format(permission=permission),
			hint=None if hint is None else hint.format(permission=permission),
		)

		self.failure = failure
		self.permission = permission
		self.workspace_id = workspace_id
		self.project_id = project_id


class ProjectNotVisible(subroutine.errors.NotFound):
	"""Raised when a private project is not this caller's to know about.

	Deliberately *not* a subclass of :class:`AuthorizationError`. A caller catching "the
	permission check said no" and logging "permission denied" would be reporting the one
	thing §7.3a exists to conceal; making it a different exception means that mistake has
	to be made on purpose.
	"""

	def __init__ (
		self,
		*,
		permission: str,
		workspace_id: uuid.UUID,
		project_id: uuid.UUID | None = None,
	) -> None:
		"""Record what was refused, while saying only that it is not there."""

		super().__init__("No project with that id, or none that you can see.")

		self.failure = AuthorizationFailure.PROJECT_INVISIBLE
		self.permission = permission
		self.workspace_id = workspace_id
		self.project_id = project_id


@dataclasses.dataclass(frozen=True)
class Grant:
	"""What a principal may do in one place, and why it came out that way.

	Returned by :func:`explain` for ``/v1/meta`` and for diagnosing a refusal. An agent
	that can read its own permissions in one call does not have to discover them by
	being refused things (SPEC.md §13.1).
	"""

	permissions: frozenset[str]
	from_role: str | None
	narrowed_by_token: bool


def effective_permissions (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	workspace_id: uuid.UUID,
	*,
	project: subroutine.db.models.project.Project | None = None,
) -> frozenset[str]:
	"""Return everything this principal may do here, after every narrowing is applied."""

	return explain(session, principal, workspace_id, project=project).permissions


def explain (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	workspace_id: uuid.UUID,
	*,
	project: subroutine.db.models.project.Project | None = None,
) -> Grant:
	"""Return the effective permissions along with where they came from."""

	membership = _project_membership(session, principal, project)
	role = _role_for(session, principal, workspace_id, membership=membership)

	if role is None:
		return Grant(permissions=frozenset(), from_role=None, narrowed_by_token=False)

	title, granted = role
	scopes = principal.scopes

	# The sentinel. An empty list narrows nothing; it does not deny everything.
	if not scopes:
		return Grant(permissions=granted, from_role=title, narrowed_by_token=False)

	return Grant(
		permissions=granted & frozenset(scopes), from_role=title, narrowed_by_token=True
	)


def may (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	permission: str,
	*,
	workspace_id: uuid.UUID,
	project: subroutine.db.models.project.Project | None = None,
) -> bool:
	"""Report whether a principal may do this, without raising.

	For the places that need to *ask* rather than *demand* — filtering a list to what the
	caller can see, or deciding whether to offer an action.
	"""

	return (
		_refusal(session, principal, permission, workspace_id=workspace_id, project=project)
		is None
	)


def authorize (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	permission: str,
	*,
	workspace_id: uuid.UUID,
	project: subroutine.db.models.project.Project | None = None,
) -> None:
	"""Permit the action, or raise explaining why not.

	Raises :class:`AuthorizationError` (403) for an ordinary refusal, and
	:class:`ProjectNotVisible` (404) where saying "forbidden" would confirm that a private
	project exists.

	Returns nothing on success on purpose. A function that returned ``True`` could have
	its result dropped and the call would still read as a check; this one cannot be
	ignored without ignoring an exception.
	"""

	failure = _refusal(
		session, principal, permission, workspace_id=workspace_id, project=project
	)

	if failure is None:
		return

	project_id = None if project is None else project.id

	if failure.conceals_existence:
		raise ProjectNotVisible(
			permission=permission, workspace_id=workspace_id, project_id=project_id
		)

	raise AuthorizationError(
		failure, permission=permission, workspace_id=workspace_id, project_id=project_id
	)


def _refusal (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	permission: str,
	*,
	workspace_id: uuid.UUID,
	project: subroutine.db.models.project.Project | None,
) -> AuthorizationFailure | None:
	"""Return why the action is refused, or ``None`` if it is permitted."""

	if permission not in subroutine.permissions.ALL:
		valid = ", ".join(sorted(subroutine.permissions.ALL))

		raise ValueError(f"Unknown permission {permission!r}. Valid permissions are: {valid}.")

	# A token pinned to one workspace cannot reach into another, whatever its owner may
	# do there.
	if (
		principal.pinned_workspace_id is not None
		and principal.pinned_workspace_id != workspace_id
	):
		return AuthorizationFailure.WORKSPACE_MISMATCH

	if project is not None and project.workspace_id != workspace_id:
		return AuthorizationFailure.WORKSPACE_MISMATCH

	membership = _project_membership(session, principal, project)

	# Checked before anything else about the project, so that a private one refuses
	# identically no matter what else is or is not true.
	if project is not None and project.visibility == "private" and membership is None:
		return AuthorizationFailure.PROJECT_INVISIBLE

	role = _role_for(session, principal, workspace_id, membership=membership)

	if role is None:
		return AuthorizationFailure.NOT_A_MEMBER

	_title, granted = role

	if permission not in granted:
		return AuthorizationFailure.ROLE_LACKS_PERMISSION

	scopes = principal.scopes

	if scopes and permission not in scopes:
		return AuthorizationFailure.OUT_OF_TOKEN_SCOPE

	if project is not None and not _within_project_scope(principal, project):
		return AuthorizationFailure.OUT_OF_PROJECT_SCOPE

	return None


def _role_for (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	workspace_id: uuid.UUID,
	*,
	membership: subroutine.db.models.project.ProjectMember | None = None,
) -> tuple[str, frozenset[str]] | None:
	"""Return the role that applies, as ``(title, permissions)``, or ``None`` for a stranger.

	A superuser bypasses role checks entirely — but not token scopes, which the caller
	applies afterwards. Otherwise the workspace role applies, unless the caller found a
	``project_member`` row naming a different one for this project.
	"""

	if principal.is_superuser:
		return "superuser", subroutine.permissions.ALL

	if membership is not None and membership.role_id is not None:
		project_role = session.get(subroutine.db.models.identity.Role, membership.role_id)

		if project_role is not None:
			return project_role.title, frozenset(project_role.permissions)

	role = subroutine.db.models.identity.Role
	member = subroutine.db.models.identity.WorkspaceMember

	found = session.execute(
		sqlalchemy.select(role.title, role.permissions)
		.join(member, member.role_id == role.id)
		.where(member.workspace_id == workspace_id, member.user_id == principal.user.id)
	).one_or_none()

	if found is None:
		return None

	title, permissions = found

	return title, frozenset(permissions)


def _project_membership (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	project: subroutine.db.models.project.Project | None,
) -> subroutine.db.models.project.ProjectMember | None:
	"""Return this principal's membership row for a project, if there is one."""

	if project is None:
		return None

	model = subroutine.db.models.project.ProjectMember

	return session.scalars(
		sqlalchemy.select(model).where(
			model.project_id == project.id, model.user_id == principal.user.id
		)
	).one_or_none()


def _within_project_scope (
	principal: subroutine.domain.authentication.Principal,
	project: subroutine.db.models.project.Project,
) -> bool:
	"""Report whether a project falls inside the token's project restriction.

	A scoped project brings its whole subtree with it: restricting an agent to a project
	and then refusing it the sub-projects underneath would make the restriction useless
	for any tree deeper than one level. The materialised ``path`` is what makes that an
	ordinary string check rather than a recursive query.
	"""

	allowed = principal.project_scope

	# The sentinel again: no list means no restriction.
	if allowed is None:
		return True

	if str(project.id) in allowed:
		return True

	ancestors = {segment for segment in project.path.split(PATH_SEPARATOR) if segment}

	return any(str(identifier) in ancestors for identifier in allowed)
