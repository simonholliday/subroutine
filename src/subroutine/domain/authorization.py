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

The instance tier (SPEC.md §7.1) has its own entry point, :func:`authorize_instance`, for
the acts that have no workspace to be checked against — creating a workspace, creating an
account. It is a separate function rather than the same one called with a placeholder
workspace, because a sentinel id would be a value every future query has to remember to
exclude.
"""

import dataclasses
import enum
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.authentication
import subroutine.domain.hierarchy
import subroutine.errors
import subroutine.permissions


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
	OUT_OF_PROJECT_WRITE_SCOPE = "out_of_project_write_scope"
	PROJECT_INVISIBLE = "project_invisible"
	NOT_A_SUPERUSER = "not_a_superuser"

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
	# Says which of the two restrictions stopped it, because the remedy is different: this
	# credential can *read* here and the caller can see that it can, so a message about the
	# project scope would send them looking for a restriction that is not the one biting.
	AuthorizationFailure.OUT_OF_PROJECT_WRITE_SCOPE: (
		"The token you used can read this project but may only write in another."
	),
	AuthorizationFailure.NOT_A_SUPERUSER: (
		"This affects the whole installation, and needs the {permission!r} permission. "
		"Only an administrator of this instance holds it."
	),
}

_HINTS: dict[AuthorizationFailure, str] = {
	AuthorizationFailure.OUT_OF_TOKEN_SCOPE: (
		"Use a token that includes {permission!r}, or one with no scope restriction at all."
	),
	AuthorizationFailure.WORKSPACE_MISMATCH: (
		"Use a token issued without a workspace, or one issued for this workspace."
	),
	AuthorizationFailure.NOT_A_SUPERUSER: (
		"Ask whoever runs this instance to do it, or to make your account an administrator."
	),
}


class AuthorizationError(subroutine.errors.Forbidden):
	"""Raised when a principal may not do what they asked to do."""

	def __init__ (
		self,
		failure: AuthorizationFailure,
		*,
		permission: str,
		workspace_id: uuid.UUID | None = None,
		project_id: uuid.UUID | None = None,
	) -> None:
		"""Record what was refused, and where, and say something useful about it.

		``workspace_id`` is absent for an instance-level refusal, which is not about any one
		workspace (SPEC.md §7.1).
		"""

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
	"""Return the effective permissions along with where they came from.

	Agrees with :func:`authorize` for every permission, by construction: it asks the same
	decision function. Anything the decision refuses to grant is absent here, so an agent
	reading its own permissions is told the truth rather than a superset it will be refused
	on later.

	A refusal that would conceal a project's existence returns an empty grant rather than
	raising. The caller is asking "what may I do here", and "nothing" is an honest answer
	that discloses nothing — the route that resolved the project id is where a 404 belongs.
	"""

	membership = _project_membership(session, principal, project)
	role = _role_for(session, principal, workspace_id, membership=membership)

	if role is None:
		return Grant(permissions=frozenset(), from_role=None, narrowed_by_token=False)

	title, granted = role
	scopes = principal.scopes
	narrowed = bool(scopes) or principal.project_scope is not None or (
		principal.pinned_workspace_id is not None
	)

	# The sentinel. An empty list narrows nothing; it does not deny everything.
	candidates = granted if not scopes else granted & frozenset(scopes)

	# Ask the real decision about each candidate rather than reproducing its checks here.
	# Duplicating them is how the two answers drifted apart in the first place.
	permitted = frozenset(
		permission
		for permission in candidates
		if _refusal(
			session,
			principal,
			permission,
			workspace_id=workspace_id,
			project=project,
			known_role=role,
		)
		is None
	)

	return Grant(permissions=permitted, from_role=title, narrowed_by_token=narrowed)


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


def instance_permissions (
	principal: subroutine.domain.authentication.Principal,
) -> frozenset[str]:
	"""Return what this principal may do to the installation itself.

	Empty for everyone but a superuser, and narrowed by the token's scopes even then — so
	an agent holding a scoped token is told the truth about what it can do rather than
	discovering it by being refused (SPEC.md §7.1, §13.1).
	"""

	if not principal.is_superuser:
		return frozenset()

	scopes = principal.scopes

	# The sentinel again: an empty list narrows nothing.
	if not scopes:
		return subroutine.permissions.INSTANCE_LEVEL

	return subroutine.permissions.INSTANCE_LEVEL & frozenset(scopes)


def may_instance (
	principal: subroutine.domain.authentication.Principal, permission: str
) -> bool:
	"""Report whether a principal may do this to the installation, without raising."""

	return _instance_refusal(principal, permission) is None


def authorize_instance (
	principal: subroutine.domain.authentication.Principal, permission: str
) -> None:
	"""Permit an action on the installation itself, or raise explaining why not.

	Takes no workspace and no session, because neither has anything to say about it:
	creating the second workspace happens outside every existing one, and creating an
	account happens before that account belongs anywhere (SPEC.md §7.1).

	Only :data:`subroutine.permissions.INSTANCE_LEVEL` verbs may be asked here. Passing a
	workspace permission is a programming error rather than a refusal, and says so.
	"""

	failure = _instance_refusal(principal, permission)

	if failure is None:
		return

	raise AuthorizationError(failure, permission=permission)


def _instance_refusal (
	principal: subroutine.domain.authentication.Principal, permission: str
) -> AuthorizationFailure | None:
	"""Return why an instance-level action is refused, or ``None`` if it is permitted."""

	if permission not in subroutine.permissions.INSTANCE_LEVEL:
		valid = ", ".join(sorted(subroutine.permissions.INSTANCE_LEVEL))

		raise ValueError(
			f"Unknown instance permission {permission!r}. Valid permissions are: {valid}. "
			"Workspace permissions go through authorize, which takes a workspace."
		)

	if not principal.is_superuser:
		return AuthorizationFailure.NOT_A_SUPERUSER

	scopes = principal.scopes

	# A superuser bypasses roles, never token scopes — otherwise a leaked agent token
	# belonging to an administrator would be unbounded (SPEC.md §7.3).
	if scopes and permission not in scopes:
		return AuthorizationFailure.OUT_OF_TOKEN_SCOPE

	return None


def _refusal (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	permission: str,
	*,
	workspace_id: uuid.UUID,
	project: subroutine.db.models.project.Project | None,
	known_role: tuple[str, frozenset[str]] | None = None,
) -> AuthorizationFailure | None:
	"""Return why the action is refused, or ``None`` if it is permitted.

	``known_role`` is the caller's already-resolved role, and means *not yet looked up*
	when absent — never *no role*, which is reported by :func:`_role_for` returning
	``None``. It exists for :func:`explain`, which asks about every permission in turn and
	would otherwise re-read the same role row once per permission: seventeen queries per
	workspace to answer a question whose input it computed before the loop started. The
	decision itself is untouched; only the lookup of one of its inputs is skipped.
	"""

	if permission not in subroutine.permissions.WORKSPACE_LEVEL:
		valid = ", ".join(sorted(subroutine.permissions.WORKSPACE_LEVEL))

		raise ValueError(
			f"Unknown workspace permission {permission!r}. Valid permissions are: {valid}. "
			"Instance permissions go through authorize_instance, which takes no workspace."
		)

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
	# identically no matter what else is or is not true. Asks about ancestors as well:
	# privacy inherits down the tree (SPEC.md §7.3a).
	if project is not None and not is_visible(session, principal, project):
		return AuthorizationFailure.PROJECT_INVISIBLE

	role = known_role or _role_for(session, principal, workspace_id, membership=membership)

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

	# **Reach and write are two questions, asked in that order** (`#371`). Everything above
	# has established that this credential can *see* the project; this asks whether it may
	# change anything there. Only the verbs that land inside a project are narrowed, and they
	# are named rather than derived — see `permissions.WRITES_INSIDE_A_PROJECT`.
	if (
		project is not None
		and permission in subroutine.permissions.WRITES_INSIDE_A_PROJECT
		and not _within_write_scope(principal, project)
	):
		return AuthorizationFailure.OUT_OF_PROJECT_WRITE_SCOPE

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

	# The workspace tier only. What a superuser may do *to the installation* is
	# :func:`_instance_refusal`'s business, and a role is never the answer there.
	if principal.is_superuser:
		return "superuser", subroutine.permissions.WORKSPACE_LEVEL

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


def visible_projects (
	principal: subroutine.domain.authentication.Principal,
) -> sqlalchemy.ColumnElement[bool]:
	"""Return a predicate selecting the projects this principal may see (SPEC.md §7.3a).

	**Privacy inherits down the tree.** A project is hidden when it is private, *or when any
	ancestor of it is*, unless the principal holds a ``project_member`` row on the private
	one. Restricting visibility to a single row would make "private" useless one level down:
	somebody would mark a project private, create a sub-project inside it, and quietly
	publish its titles to the whole workspace.

	This is the same reasoning :func:`_within_project_scope` applies to a token's project
	restriction, and the two disagreeing was a finding in the slice-2 review. They agree now
	— both read the materialised ``path``, which is what makes an ancestor test a string
	comparison rather than a recursive query.

	Written as a predicate rather than a function of one project so that the agenda, search
	and every future listing narrow with the same rule instead of reimplementing it.
	"""

	project = subroutine.db.models.project.Project
	ancestor = sqlalchemy.orm.aliased(subroutine.db.models.project.Project)
	membership = subroutine.db.models.project.ProjectMember

	hidden = (
		sqlalchemy.select(ancestor.id)
		.where(
			ancestor.visibility == "private",
			# `path` holds every ancestor's id, so a prefix match *is* the ancestor test.
			# It includes the project itself, whose path is trivially its own prefix.
			#
			# `like(... || '%')` rather than `startswith(autoescape=True)`, which requires a
			# literal and cannot take a column. Unescaped is safe here for the reason the
			# slice-1 review already recorded about the other path queries: a path is
			# lowercase hex, hyphens and slashes, and contains no `%` or `_` to be read as a
			# wildcard. **Not** a range comparison — that is wrong under a non-byte-wise
			# collation, measured and recorded in `hierarchy.subtree`.
			project.path.like(ancestor.path.concat("%")),
			ancestor.id.not_in(
				sqlalchemy.select(membership.project_id).where(
					membership.user_id == principal.user.id
				)
			),
		)
		.exists()
	)

	return sqlalchemy.not_(hidden)


def is_visible (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	project: subroutine.db.models.project.Project,
) -> bool:
	"""Report whether one project is visible to this principal, ancestors included."""

	model = subroutine.db.models.project.Project

	found = session.scalar(
		sqlalchemy.select(model.id).where(model.id == project.id, visible_projects(principal))
	)

	return found is not None


def _project_membership (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	project: subroutine.db.models.project.Project | None,
) -> subroutine.db.models.project.ProjectMember | None:
	"""Return this principal's membership row for a project, if there is one.

	Looks for a row on *this* project only. Visibility of an ancestor is a separate question
	answered by :func:`visible_projects`, and a role override (§7.3) is deliberately not
	inherited — being given `contributor` on a parent project does not silently make you a
	contributor on everything under it.
	"""

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

	return _covers(principal.project_scope, project)


def _within_write_scope (
	principal: subroutine.domain.authentication.Principal,
	project: subroutine.db.models.project.Project,
) -> bool:
	"""Report whether a credential may change things in this project — item ``#371``.

	**``None`` means "wherever it can reach", not "everywhere".** That is what keeps every
	credential issued before this column existed behaving exactly as it did: the reach check
	has already run and passed by the time this is asked, so falling through here grants
	nothing the reach did not already allow. Spelling the default as a copy of
	``project_scope`` would have been the same behaviour and a worse record — a credential
	would then carry a write set nobody chose, indistinguishable from one somebody did.

	Subtree-inclusive for :func:`_within_project_scope`'s reason: a write set of ``SR`` that
	refused ``SR/WEB`` would be useless on any tree deeper than one level.
	"""

	writable = principal.project_write_scope

	if writable is None:
		return True

	return _covers(writable, project)


def _covers (
	allowed: list[str] | None, project: subroutine.db.models.project.Project
) -> bool:
	"""Report whether a list of project ids covers this project or an ancestor of it.

	Shared by the two restrictions above so that "reaches" and "may write in" cannot come to
	mean subtly different things about the same tree — which is the divergence this codebase
	finds more often than any other.

	**And the rule itself lives in `hierarchy`, one level further out** (`#413`). Two copies
	were not enough: the check that refuses a write set outside the reach was a third reader of
	"is this project inside that one", written as a flat set subset, and it refused a child of a
	project the credential could read. A rule with one implementation cannot do that.
	"""

	# The sentinel again: no list means no restriction.
	if allowed is None:
		return True

	return subroutine.domain.hierarchy.within(
		allowed, identifier=str(project.id), path=project.path
	)
