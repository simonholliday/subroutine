"""Deciding whether a principal may do a particular thing in a particular place.

One entry point, because a permission system with several is a permission system with
several answers. Everything that needs to know goes through :func:`authorize` or
:func:`refusal`, and both are built on the same private decision.

docs/design.md §7.3 states the rule as set intersection::

    effective = role_permissions(user, workspace)
              ∩ token_scopes           (if the token narrows them)
              ∩ token_project_scope    (restricts which rows, not which verbs)

with one exception that has to be stated in the code as loudly as in the spec: **an empty
``scopes`` list and a null ``project_scope`` mean "no narrowing", not "no permissions"**.
Read as literal set algebra, the formula gives every ordinary token nothing at all — which
is the single easiest way to ship an API where everything is refused.

**And every act lands on a place** (`#4558`, decision `#4527`): a read needs its place among the
credential's read places, which reach decides, and any other act needs it among its write places
as well - its project, or its workspace where no project is named, which a credential narrowed to
some projects never holds. So such a credential administers nothing beyond its projects, whatever
the verb, and the installation not at all (decision `#3802`).

The instance tier (docs/design.md §7.1) has its own entry point, :func:`authorize_instance`, for
the acts that have no workspace to be checked against — creating a workspace, creating an
account. It is a separate function rather than the same one called with a placeholder
workspace, because a sentinel id would be a value every future query has to remember to
exclude.
"""

import dataclasses
import enum
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.domain.accountability
import subroutine.domain.authentication
import subroutine.domain.hierarchy
import subroutine.domain.trash
import subroutine.errors
import subroutine.permissions


class AuthorizationFailure(enum.StrEnum):
	"""Why an action was refused.

	Recorded for the log, and used by the API to choose a status code. Only one of these
	is ever reported to the caller in any detail — see :attr:`conceals_existence`.
	"""

	OUT_OF_REACH = "out_of_reach"
	ROLE_LACKS_PERMISSION = "role_lacks_permission"
	OUT_OF_TOKEN_SCOPE = "out_of_token_scope"
	BEYOND_ITS_PLACES = "beyond_its_places"
	PINNED_TO_A_WORKSPACE = "pinned_to_a_workspace"
	NOT_A_SUPERUSER = "not_a_superuser"
	READ_ONLY = "read_only"

	@property
	def conceals_existence (self) -> bool:
		"""Report whether this refusal must be reported as "not found".

		**Anything out of reach answers ``404`` rather than ``403``** (docs/design.md §7.3a, §8.7;
		`#4674`, P-1 of the cold review of 2026-10-05): telling someone they are forbidden confirms
		the thing is there, which is what a private project, a workspace they are not in and a
		project outside their credential each mean they should not learn.
		"""

		return self is AuthorizationFailure.OUT_OF_REACH


#: What to tell the caller for each refusal. Every one of these is safe to say to the
#: person it is said to: it describes their own role and their own token, which they may
#: already inspect, and never anything about what they were reaching for.
_EXPLANATIONS: dict[AuthorizationFailure, str] = {
	AuthorizationFailure.ROLE_LACKS_PERMISSION: (
		"This needs the {permission} permission, which your role here does not include."
	),
	AuthorizationFailure.OUT_OF_TOKEN_SCOPE: (
		"This needs the {permission} permission. Your role allows it, but the token you "
		"used is scoped to a narrower set."
	),
	# **One sentence wherever an act lands beyond the places a credential may change** (`#4558`,
	# decision `#4527`): a project outside its write set, the workspace, and the installation. It
	# names what the credential may change rather than what it reaches, so it holds where it
	# reads the place and may not change it, and where it does neither.
	AuthorizationFailure.BEYOND_ITS_PLACES: (
		"This needs the {permission} permission, and acts beyond the projects the token you "
		"used may change."
	),
	AuthorizationFailure.NOT_A_SUPERUSER: (
		"This affects the whole installation, and needs the {permission} permission. "
		"Only an administrator of this instance holds it."
	),
	AuthorizationFailure.PINNED_TO_A_WORKSPACE: (
		"This needs the {permission} permission, and acts on the whole installation, beyond the "
		"one workspace the token you used is pinned to."
	),
	AuthorizationFailure.READ_ONLY: subroutine.domain.authentication.READ_ONLY,
}

_HINTS: dict[AuthorizationFailure, str] = {
	AuthorizationFailure.OUT_OF_TOKEN_SCOPE: (
		"Use a token that includes {permission}, or one with no scope restriction at all."
	),
	AuthorizationFailure.BEYOND_ITS_PLACES: "Use a token that is not narrowed to projects.",
	AuthorizationFailure.PINNED_TO_A_WORKSPACE: "Use a token issued without a workspace.",
	AuthorizationFailure.NOT_A_SUPERUSER: (
		"Ask whoever runs this instance to do it, or to make your account an administrator."
	),
	AuthorizationFailure.READ_ONLY: subroutine.domain.authentication.READ_ONLY_HINT,
}


def _named (permission: str) -> str:
	"""Return a permission as a refusal should say it, with what it covers if that is not obvious.

	``'task:write' (tasks and documents)`` rather than ``'task:write'``, because the name is
	about tasks and the permission is not. Only the four verbs in
	:data:`subroutine.permissions.COVERAGE` carry a gloss; everything else says its own name,
	which is already the whole truth about it.
	"""

	covers = subroutine.permissions.COVERAGE.get(permission)

	return f"{permission!r}" if covers is None else f"{permission!r} ({covers})"


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
		workspace (docs/design.md §7.1).
		"""

		hint = _HINTS.get(failure)

		# **Named with what it covers, where the name alone would misdirect** (`SR#1555`'s
		# sweep). There is deliberately no ``document:write`` — a document is written with
		# ``task:write`` — so a caller who asked to create a *document* was told to obtain a
		# *task* permission, with nothing saying the two are one. The gloss is
		# :data:`subroutine.permissions.COVERAGE`, which already existed for exactly this and
		# is what ``whoami`` prints, so this adds no second description to keep in step.
		named = _named(permission)

		super().__init__(
			_EXPLANATIONS[failure].format(permission=named),
			hint=None if hint is None else hint.format(permission=named),
		)

		self.failure = failure
		self.permission = permission
		self.workspace_id = workspace_id
		self.project_id = project_id


class OutOfReach(subroutine.errors.NotFound):
	"""Raised when what was named is beyond the caller's reach, so is not there for them (`#4674`).

	A private project, a workspace they are not in, one their credential is pinned away from, and
	a project outside their credential: **one refusal, said as not found**, since saying forbidden
	confirms the thing exists. The 403s these were before could be met only by a direct domain
	call, because every route finds what it was named through the listings, which already leave
	all of them out.

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

		super().__init__(
			"No workspace with that id, or none that you can reach."
			if project_id is None
			else "No project with that id, or none that you can see."
		)

		self.failure = AuthorizationFailure.OUT_OF_REACH
		self.permission = permission
		self.workspace_id = workspace_id
		self.project_id = project_id


@dataclasses.dataclass(frozen=True)
class Grant:
	"""What a principal may do in one place, and why it came out that way.

	Returned by :func:`explain` for ``/v1/meta`` and for diagnosing a refusal. An agent
	that can read its own permissions in one call does not have to discover them by
	being refused things (docs/design.md §13.1).
	"""

	permissions: frozenset[str]
	from_role: str | None
	narrowed_by_token: bool


@dataclasses.dataclass(frozen=True)
class _Reached:
	"""What one query says of a principal at one place they reach - `#4674`.

	The role they hold in the workspace, and whether the project named, if one was, is inside the
	credential's write set (``True`` where none was named, or the credential has none).
	"""

	title: str
	permissions: frozenset[str]
	writes_here: bool


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

	**With no project named, it says what may be done anywhere in the workspace** (`#4558`, decision
	`#4527` as revised on 2026-10-07): a credential narrowed to some projects holds no place at the
	workspace itself, so the check refuses it every write asked there - and ``/v1/me``, which reads
	this, would tell an agent that files in its projects that it may only read. So a verb that acts
	inside projects is offered where only its place stood in the way, and a verb in
	:data:`subroutine.permissions.WORKSPACE_WIDE`, which acts on nothing smaller, is not (`#4095`).
	"""

	_refuse_a_project_from_another_workspace(workspace_id, project)

	# **Reach is asked once**, as the role was (`#4674`): each verb below is decided against it
	# rather than asking again, which for a project was two queries a verb.
	reached = _reached(session, principal, workspace_id=workspace_id, project=project)

	if reached is None:
		return Grant(permissions=frozenset(), from_role=None, narrowed_by_token=False)

	scopes = principal.scopes
	# **The one definition, asked rather than worked out again** (`#3934`). This spelled out three
	# of the four axes by hand and never learned the fourth, `project_write_scope`, so `/v1/me`
	# called a write-set-only credential not narrowed in each workspace while its own
	# `credential.narrows` said it was, and `/v1/tokens` agreed with the second.
	narrowed = principal.narrows

	# The sentinel. An empty list narrows nothing; it does not deny everything.
	candidates = reached.permissions if not scopes else reached.permissions & frozenset(scopes)

	# Ask the real decision about each candidate rather than reproducing its checks here.
	# Duplicating them is how the two answers drifted apart in the first place.
	permitted = frozenset(
		permission
		for permission in candidates
		if _offered(
			_refusal(
				session,
				principal,
				permission,
				workspace_id=workspace_id,
				project=project,
				known=reached,
			),
			permission,
			anywhere_inside=project is None,
		)
	)

	return Grant(permissions=permitted, from_role=reached.title, narrowed_by_token=narrowed)


def _offered (failure: AuthorizationFailure | None, permission: str, *, anywhere_inside: bool) -> bool:
	"""Report whether :func:`explain` offers a verb, given what the check said of it."""

	if failure is None:
		return True

	return (
		anywhere_inside
		and failure is AuthorizationFailure.BEYOND_ITS_PLACES
		and permission not in subroutine.permissions.WORKSPACE_WIDE
	)


def refusal (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	permission: str,
	*,
	workspace_id: uuid.UUID,
	project: subroutine.db.models.project.Project | None = None,
	own_account: bool = False,
) -> AuthorizationFailure | None:
	"""Say why a principal may not do this, or ``None`` if they may, without raising.

	For the places that need to *ask* rather than *demand* - filtering a list to what the caller can
	see, or deciding whether to offer an action - and for a rule of its own that refuses in its own
	words (`#4020`): the owner rule said *only an owner may* to an owner whose token was not given the
	permission, where the token's own sentence says what to do about it. **One function for both**
	(S16 of the cold review of 2026-10-05): a ``may`` beside it asked the same question and dropped
	the reason.

	``own_account`` is as :func:`authorize` takes it.
	"""

	return _refusal(
		session,
		principal,
		permission,
		workspace_id=workspace_id,
		project=project,
		own_account=own_account,
	)


def authorize (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	permission: str,
	*,
	workspace_id: uuid.UUID,
	project: subroutine.db.models.project.Project | None = None,
	own_account: bool = False,
	instead: str | typing.Callable[[], str] | None = None,
	field: str | None = None,
) -> None:
	"""Permit the action, or raise explaining why not.

	Raises :class:`AuthorizationError` (403) for an ordinary refusal, and :class:`OutOfReach`
	(404) where saying "forbidden" would confirm that what was named exists.

	Returns nothing on success on purpose. A function that returned ``True`` could have
	its result dropped and the call would still read as a check; this one cannot be
	ignored without ignoring an exception.

	**``own_account`` says the act lands on the caller's own account** (`#4558`, decision `#4527`):
	a private saved view changes nothing anybody else sees, and every credential reaches its own
	account, so the place test is not asked while the role, the scopes and a read-only session
	still are - a workspace viewer saves no view (`SR#3149`).

	**``instead`` says what to do where the act lands beyond the credential's places**, as the hint
	of that one refusal: making a project inside one it may change, or keeping a view as one's own.
	The sentence stays the one every such refusal says. A function is called only on refusal, for a
	hint that costs a query. **``field`` names the part of the request that took the act to the
	workspace**, such as ``binds`` or ``shared``, so a caller is told what to change.
	"""

	failure = _refusal(
		session,
		principal,
		permission,
		workspace_id=workspace_id,
		project=project,
		own_account=own_account,
	)

	if failure is None:
		return

	project_id = None if project is None else project.id

	if failure.conceals_existence:
		raise OutOfReach(permission=permission, workspace_id=workspace_id, project_id=project_id)

	refused = AuthorizationError(
		failure, permission=permission, workspace_id=workspace_id, project_id=project_id
	)

	if failure is AuthorizationFailure.BEYOND_ITS_PLACES:
		if instead is not None:
			refused.hint = instead if isinstance(instead, str) else instead()

		if field is not None:
			refused.errors = (
				subroutine.errors.FieldError(
					field=field,
					code="forbidden",
					message="This acts on the whole workspace, and the credential may change only "
					"some of its projects.",
					hint=refused.hint,
				),
			)

	raise refused


def authorize_on (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal | None,
	permission: str,
	item: typing.Any,
	*,
	doing: str | None = "it cannot be changed",
) -> None:
	"""Permit an act on one item, or raise: against its project, and that project's workspace.

	**One check for any item** (`#4544`, decision `#4532`): a project, or anything that names
	one - a task, a document, a link's end. A comment is checked on what it hangs off. Six wrappers
	each spelled this out, and a dozen calls looked the project up by hand. **Never the workspace
	alone** for something in a project: that skips the two rules about the project itself, whether
	it is out of sight and whether a credential's project scope and write set admit it (`#940`).

	``None`` is an unauthenticated internal caller and is not checked: ``domain.bootstrap`` and the
	tests. ``tests/test_actor_discipline.py`` fails the build if a module under ``src`` calls a
	mutating service without an actor, which is what keeps this skip from being a hole.

	**And the trash, which is one gate here** (`#4548`, decision `#4532`): a task or a document in
	the trash, or beneath anything in it, takes no write but restore and delete, whoever asks -
	internal callers included, as each writer's own check refused them. ``doing`` finishes the
	refusal - *#42 is in the trash, so* ``it cannot be changed`` - and ``None`` is for an act the
	trash does not stop, which :mod:`subroutine.domain.trash` names. After the permission, so a
	caller who may not touch the item is told that first.
	"""

	if principal is not None:
		project = (
			item
			if isinstance(item, subroutine.db.models.project.Project)
			else session.get(subroutine.db.models.project.Project, item.project_id)
		)

		if project is None:
			# ``project_id`` is NOT NULL with a foreign key on both backends, so reaching here means
			# the schema is broken. The one thing not to do is check against the workspace alone,
			# which is the permissive answer this exists to stop.
			raise subroutine.errors.NotFound("The project this belongs to could not be read.")

		authorize(
			session, principal, permission, workspace_id=project.workspace_id, project=project
		)

	if (
		doing is not None
		and permission not in subroutine.permissions.READS
		and permission != subroutine.permissions.TASK_DELETE
		and isinstance(item, subroutine.db.models.work.Task | subroutine.db.models.work.Document)
	):
		subroutine.domain.trash.refuse_reaching(session, item, doing=doing)


def instance_permissions (
	principal: subroutine.domain.authentication.Principal,
) -> frozenset[str]:
	"""Return what this principal may do to the installation itself.

	Empty for everyone but a superuser, and narrowed by the token's scopes even then — so
	an agent holding a scoped token is told the truth about what it can do rather than
	discovering it by being refused (docs/design.md §7.1, §13.1).

	**Asked of the decision itself, as :func:`explain` does for a workspace** (`#3812`). This
	restated the superuser and scope rules and not the narrowing decision `#3802` added, so a
	superuser's credential narrowed to some projects would have been offered the installation's
	administration by ``/v1/me`` and then refused it.
	"""

	return frozenset(
		permission
		for permission in subroutine.permissions.INSTANCE_LEVEL
		if _instance_refusal(principal, permission) is None
	)


def may_instance (
	principal: subroutine.domain.authentication.Principal, permission: str, *, reading: bool = False
) -> bool:
	"""Report whether a principal may do this to the installation, without raising.

	``reading`` says the act only reads, as :func:`authorize_instance` takes it.
	"""

	return _instance_refusal(principal, permission, reading=reading) is None


def authorize_instance (
	principal: subroutine.domain.authentication.Principal, permission: str, *, reading: bool = False
) -> None:
	"""Permit an action on the installation itself, or raise explaining why not.

	Takes no workspace and no session, because neither has anything to say about it:
	creating the second workspace happens outside every existing one, and creating an
	account happens before that account belongs anywhere (docs/design.md §7.1).

	Only :data:`subroutine.permissions.INSTANCE_LEVEL` verbs may be asked here. Passing a
	workspace permission is a programming error rather than a refusal, and says so.

	**``reading`` says the act only reads** (decision `#4510`): listing every workspace, the
	backups or the projects nobody can reach. A read-only session is refused every other act asked
	here, and an act left to the default is a write, so forgetting it refuses rather than permits.
	"""

	failure = _instance_refusal(principal, permission, reading=reading)

	if failure is None:
		return

	raise AuthorizationError(failure, permission=permission)


def write_places (
	principal: subroutine.domain.authentication.Principal,
) -> typing.Sequence[str] | None:
	"""Return the projects a credential may change things in, ``None`` being wherever it reaches.

	**Its write set, else its reach** (`#371`, decision `#4527`): ``project_write_scope`` where it
	has one, ``project_scope`` where it does not, and ``None`` for neither. A workspace and the
	installation are places of their own, beyond every project, so **only a credential whose write
	places are ``None`` holds them** - which is how a credential narrowed to some projects
	administers nothing beyond them (decision `#3802`). One spelling, for the check and for what a
	refusal or a filing offers instead, which each wrote out for themselves (`#4558`).
	"""

	if principal.project_write_scope is not None:
		return principal.project_write_scope

	return principal.project_scope


def outside_token_scope (
	principal: subroutine.domain.authentication.Principal, permission: str
) -> bool:
	"""Report whether the credential's own narrowing forbids this verb (§7.3).

	**Empty means no narrowing, not no permission**, which is the trap this rule keeps: a
	token issued with no scopes is as wide as its owner, and reading the list truthily is what
	tells the two apart.

	Lifted out because it was written twice and was about to be written a third time — the two
	refusal paths below, and `#930`'s read check in :mod:`subroutine.domain.scoping`, which
	needs the answer without a session and so cannot go through :func:`authorize`. A rule this
	codebase keeps finding in two places that disagree gets one home the moment there is a
	third caller.
	"""

	scopes = principal.scopes

	return bool(scopes) and permission not in scopes


def _instance_refusal (
	principal: subroutine.domain.authentication.Principal, permission: str, *, reading: bool = False
) -> AuthorizationFailure | None:
	"""Return why an instance-level action is refused, or ``None`` if it is permitted.

	``reading`` says the act only reads. **An instance verb gates reads and writes alike** - the
	listing of every workspace and the backups as well as making either - so the verb alone cannot
	say which side of a read-only session an act is on, and the caller says it instead.
	"""

	if permission not in subroutine.permissions.INSTANCE_LEVEL:
		valid = ", ".join(sorted(subroutine.permissions.INSTANCE_LEVEL))

		raise ValueError(
			f"Unknown instance permission {permission!r}. Valid permissions are: {valid}. "
			"Workspace permissions go through authorize, which takes a workspace."
		)

	# **Before anything else, as for a workspace** (decision `#4510`).
	if principal.read_only and not reading:
		return AuthorizationFailure.READ_ONLY

	if not principal.is_superuser:
		return AuthorizationFailure.NOT_A_SUPERUSER

	# A superuser bypasses roles, never token scopes — otherwise a leaked agent token
	# belonging to an administrator would be unbounded (docs/design.md §7.3).
	if outside_token_scope(principal, permission):
		return AuthorizationFailure.OUT_OF_TOKEN_SCOPE

	# **The installation is a place beyond every project** (decision `#3802`, item `#3812`; `#4558`,
	# decision `#4527`), held only by a credential narrowed to none: ``scopes`` defaults to the
	# owner's whole set, so a superuser's credential narrowed to one project created accounts and
	# revoked other people's credentials and sign-ins (`#3883` M-4). **For reading too, as before**:
	# the installation's listings span every workspace on it, which a credential narrowed even only
	# in what it changes was issued to stop short of. Everything that acts on the installation asks
	# here.
	if write_places(principal) is not None:
		return AuthorizationFailure.BEYOND_ITS_PLACES

	# **Nor a pin to one workspace** (`#4006`, M-9 of the cold review of 2026-09-30, the half of
	# `#3883` M-4 the narrowing above left). A pin says *this credential is for that workspace*
	# (`#344`), and a question about the installation is by construction not about one, so a pin
	# refuses here even with ``instance:admin`` among the scopes. Nothing here asked it once: a
	# superuser's token pinned to one workspace listed and revoked a colleague's credentials, made
	# a superuser, and minted a credential in a colleague's name and then acted as them. **Asked
	# only here** (S16 of the cold review of 2026-10-05): four callers asked it again beside this,
	# and two of those could never run.
	if principal.pinned_workspace_id is not None:
		return AuthorizationFailure.PINNED_TO_A_WORKSPACE

	return None


def _refusal (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	permission: str,
	*,
	workspace_id: uuid.UUID,
	project: subroutine.db.models.project.Project | None,
	known: _Reached | None = None,
	own_account: bool = False,
) -> AuthorizationFailure | None:
	"""Return why the action is refused, or ``None`` if it is permitted.

	``known`` is what :func:`_reached` already said of this place, and means *not yet asked* when
	absent - never *out of reach*, which is :func:`_reached` returning ``None``. It exists for
	:func:`explain`, which asks about every permission in turn and would otherwise ask the same
	question once per permission. The decision itself is untouched; only the lookup of one of its
	inputs is skipped.
	"""

	if permission not in subroutine.permissions.WORKSPACE_LEVEL:
		valid = ", ".join(sorted(subroutine.permissions.WORKSPACE_LEVEL))

		raise ValueError(
			f"Unknown workspace permission {permission!r}. Valid permissions are: {valid}. "
			"Instance permissions go through authorize_instance, which takes no workspace."
		)

	_refuse_a_project_from_another_workspace(workspace_id, project)

	# **A read-only session reads and does nothing else** (decision `#4510`). Asked before anything
	# about the workspace or the project, because it is about the session and says nothing of either.
	if principal.read_only and permission not in subroutine.permissions.READS:
		return AuthorizationFailure.READ_ONLY

	# **An instance administrator administers any live workspace's membership, and deletes it,
	# member or not** (`#4557`, decision `#4519`, `#1418`): the one clause, where a superuser
	# bypassed every role. Asked of the instance tier, so a pin, a narrowing to some projects and a
	# read-only session each refuse it as they refuse any instance act; and the verb itself must be
	# within the credential's scopes, as everywhere. **Before reach, since it is the one way past
	# it, and only with no project named**, as none of its three verbs is ever asked with one.
	if (
		project is None
		and permission in subroutine.permissions.ADMINISTERED_FROM_THE_INSTANCE
		and _instance_refusal(
			principal,
			subroutine.permissions.INSTANCE_ADMIN,
			reading=permission in subroutine.permissions.READS,
		)
		is None
		and not outside_token_scope(principal, permission)
	):
		return None

	# **Reach is one question, and out of reach is not found** (`#4674`, P-1 of the cold review of
	# 2026-10-05): a pin to another workspace, a project in another workspace, a project out of
	# sight, no membership and a project outside the credential were five checks here, three of
	# them refusing as forbidden what the listings leave out.
	reached = known or _reached(session, principal, workspace_id=workspace_id, project=project)

	if reached is None:
		return AuthorizationFailure.OUT_OF_REACH

	if permission not in reached.permissions:
		return AuthorizationFailure.ROLE_LACKS_PERMISSION

	if outside_token_scope(principal, permission):
		return AuthorizationFailure.OUT_OF_TOKEN_SCOPE

	# **Every act but a read lands on a place the credential may change** (`#4558`, decision
	# `#4527`): its project, or its workspace where none is named. Reach has established that it
	# reads the place (`#371`'s first question), and this is the second, whether it may change
	# anything there - asked of every verb that is not a read, where three sets of verbs each
	# decided which of the two questions a verb was asked. The workspace is held only by a credential
	# narrowed to no project, so one narrowed to some administers nothing beyond them (decision
	# `#3802`, `#3744`), makes no project at the top level (`#4095`, Q15 of `#4506`), and puts
	# nothing in front of the whole workspace (`#3151`, `#4134`). An act on the caller's own account
	# lands on that account, which every credential holds.
	if permission in subroutine.permissions.READS or own_account:
		return None

	if project is None:
		return None if write_places(principal) is None else AuthorizationFailure.BEYOND_ITS_PLACES

	return None if reached.writes_here else AuthorizationFailure.BEYOND_ITS_PLACES


def _refuse_a_project_from_another_workspace (
	workspace_id: uuid.UUID, project: subroutine.db.models.project.Project | None
) -> None:
	"""Raise where a check names a project with a workspace it is not in: a programming error.

	**A failure of its own** (`#4558`, A I-7 of the cold review of 2026-10-05), as an unknown verb
	is: it was refused as pinned to a different workspace, said to callers who were not pinned, and
	then as not found. No route can send one - every check is asked of the project's own workspace -
	so only a caller's mistake reaches this, and it should say so rather than read as a refusal.
	"""

	if project is None or project.workspace_id == workspace_id:
		return

	raise ValueError(
		f"Project {project.id} is in workspace {project.workspace_id}, not {workspace_id}. Ask a "
		"check of the project's own workspace."
	)


def visible_projects (
	principal: subroutine.domain.authentication.Principal,
) -> sqlalchemy.ColumnElement[bool]:
	"""Return a predicate selecting the projects this principal may see (docs/design.md §7.3a).

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


def reaches (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	workspace_id: (
		sqlalchemy.ColumnElement[uuid.UUID] | sqlalchemy.orm.InstrumentedAttribute[uuid.UUID]
	),
	*,
	include_deleted: bool = False,
) -> sqlalchemy.ColumnElement[bool]:
	"""Return the predicate saying a principal reaches the workspace a row is in - `#4674`.

	**The one answer to *which workspaces*, for listing and for checking alike** (P-1 of the cold
	review of 2026-10-05): :func:`subroutine.domain.workspaces.readable` lists by it, every listing
	in :mod:`subroutine.domain.scoping` narrows by it (`#4675`), and :func:`_reached` asks it of the
	one workspace a check names. ``workspace_id`` is a column of the query this goes into.

	Membership is what grants reach. A pin narrows it to one workspace (docs/design.md §7.3); a
	credential narrowed to some projects reaches only the workspaces holding them (`#4555`); and an
	agent reaches a workspace only while its chain of accountability stands and the person at the
	top of it is a member there too (`#4546`, decision `#4518`). ``include_deleted`` is for the
	check, which :func:`subroutine.domain.workspaces.restore` asks of a workspace in the trash;
	every listing leaves them out (`#704`).

	**One statement of ids, asked once rather than for every row**, and built of its own aliases so
	a query that already selects members or workspaces cannot correlate it away (`#3922`).
	"""

	member = sqlalchemy.orm.aliased(subroutine.db.models.identity.WorkspaceMember)
	workspace = sqlalchemy.orm.aliased(subroutine.db.models.identity.Workspace)

	statement = (
		sqlalchemy.select(member.workspace_id)
		.join(workspace, workspace.id == member.workspace_id)
		.where(member.user_id == principal.user.id)
	)

	if not include_deleted:
		statement = statement.where(workspace.deleted_at.is_(None))

	if principal.pinned_workspace_id is not None:
		statement = statement.where(member.workspace_id == principal.pinned_workspace_id)

	if principal.project_scope is not None:
		scoped = sqlalchemy.orm.aliased(subroutine.db.models.project.Project)
		statement = statement.where(
			sqlalchemy.exists().where(
				scoped.workspace_id == member.workspace_id,
				scoped.id.in_(_project_ids(principal.project_scope)),
			)
		)

	if principal.user.is_service_account:
		if not subroutine.domain.accountability.can_act(session, principal.user):
			return sqlalchemy.false()

		person = subroutine.domain.accountability.answers_for(session, principal.user)
		seated = sqlalchemy.orm.aliased(subroutine.db.models.identity.WorkspaceMember)
		statement = statement.where(
			sqlalchemy.exists().where(
				seated.workspace_id == member.workspace_id, seated.user_id == person.id
			)
		)

	return workspace_id.in_(statement)


def _reached (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_id: uuid.UUID,
	project: subroutine.db.models.project.Project | None,
) -> _Reached | None:
	"""Return the principal's role where they reach this place, or ``None`` where they do not.

	**One query** (`#4674`): the role row, kept only where :func:`reaches` holds of the workspace
	and, with a project named, where that project is in that workspace, in sight
	(:func:`visible_projects`) and inside the credential's project scope; the write set is asked in
	the same statement.
	Deleted workspaces are reached here, as they were, because restoring one is checked.

	**The workspace role applies in every project of the workspace**, a superuser's included
	(`#4557`, decision `#4519`), and no project has a role of its own (`#4547`).
	"""

	role = subroutine.db.models.identity.Role
	member = subroutine.db.models.identity.WorkspaceMember
	model = subroutine.db.models.project.Project

	# **The write set in the same statement** (`#371`): ``None`` means wherever it reaches, which
	# reach has already established, so it is asked only of a project named to a credential that
	# has one.
	writes_here: sqlalchemy.ColumnElement[bool] = sqlalchemy.true()

	if project is not None and principal.project_write_scope is not None:
		writes_here = sqlalchemy.exists().where(
			model.id == project.id,
			subroutine.domain.hierarchy.beneath_any(model, principal.project_write_scope),
		)

	statement = (
		sqlalchemy.select(role.title, role.permissions, writes_here.label("writes_here"))
		.select_from(member)
		.join(role, member.role_id == role.id)
		.where(
			member.workspace_id == workspace_id,
			member.user_id == principal.user.id,
			reaches(session, principal, member.workspace_id, include_deleted=True),
		)
	)

	if project is not None:
		statement = statement.where(
			sqlalchemy.exists().where(
				model.id == project.id,
				model.workspace_id == workspace_id,
				visible_projects(principal),
				subroutine.domain.hierarchy.beneath_any(model, principal.project_scope),
			)
		)

	found = session.execute(statement).one_or_none()

	if found is None:
		return None

	title, permissions, writes = found

	return _Reached(title=title, permissions=frozenset(permissions), writes_here=bool(writes))


def _project_ids (scope: typing.Sequence[str]) -> list[uuid.UUID]:
	"""Return the project ids a credential's scope names, leaving out anything that is not one."""

	found: list[uuid.UUID] = []

	for named in scope:
		try:
			found.append(uuid.UUID(str(named)))

		except ValueError:
			continue

	return found
