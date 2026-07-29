"""``GET /v1/me`` — who the caller is, and exactly what they may do.

The point of this endpoint is that an agent should not have to discover its own authority
by being refused things (SPEC.md §13.1). So it reports the *answer* rather than the inputs
to it: each workspace carries the permissions that actually apply there, already
intersected with the role, the token's scopes and its project scope. Nothing here needs
the caller to reproduce §7.3's resolution for itself.

That matters most for the empty-list sentinel. ``scopes: []`` means the token narrows
nothing — read as set algebra it would mean the opposite, and an agent that got it backwards
would conclude it could do nothing at all. The field is reported as it is stored, next to a
``narrows`` boolean that says what it means, and the permissions that settle it.
"""

import datetime
import uuid

import fastapi
import pydantic
import sqlalchemy.orm

import subroutine
import subroutine.api.dependencies
import subroutine.api.security
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.workspaces

router = fastapi.APIRouter(prefix="/v1", tags=["identity"])


class User(pydantic.BaseModel):
	"""The account the caller is acting as."""

	id: uuid.UUID
	username: str
	display_name: str | None
	email: str | None
	timezone: str | None
	is_superuser: bool
	is_service_account: bool


class Credential(pydantic.BaseModel):
	"""The credential presented, and how far it narrows its owner's authority.

	Never the secret, and never anything from which it could be reconstructed: ``prefix``
	is the public half a token is looked up by and is safe to quote in a log.
	"""

	kind: str
	id: uuid.UUID
	title: str
	prefix: str

	#: Empty means **no narrowing**, not "no permissions" (SPEC.md §7.3).
	scopes: list[str]

	#: Null means every project, for the same reason.
	project_scope: list[str] | None

	#: Set when the token may only be used in one workspace.
	workspace_id: uuid.UUID | None

	#: Whether this credential restricts its owner at all. Spelled out so that reading
	#: ``scopes: []`` the wrong way round is not the only thing standing between an agent
	#: and a wrong conclusion.
	narrows: bool

	expires_at: datetime.datetime | None
	last_used_at: datetime.datetime | None


class WorkspaceAccess(pydantic.BaseModel):
	"""One workspace the caller can reach, and what they may do in it."""

	id: uuid.UUID
	slug: str
	title: str
	timezone: str | None

	#: The role this caller holds here, before the credential narrowed anything.
	role: str | None

	#: What they may actually do, after every narrowing in §7.3 has been applied. This is
	#: the field to act on; the others explain how it came out this way.
	permissions: list[str]

	narrowed_by_credential: bool


class Me(pydantic.BaseModel):
	"""The answer to "who am I and what may I do here?", in one round trip."""

	api_version: str
	user: User

	#: Absent in local mode, where the CLI acts as a user with no credential at all.
	credential: Credential | None

	#: Permissions over the installation itself — creating workspaces and accounts. Held
	#: only by a superuser, and narrowed by the credential even then (SPEC.md §7.1).
	instance_permissions: list[str]

	workspaces: list[WorkspaceAccess]


@router.get("/me", summary="Who am I, and what may I do?")
def me (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> Me:
	"""Report the caller's identity, credential and effective permissions."""

	workspaces = [
		_access(session, actor, workspace)
		for workspace in subroutine.domain.workspaces.readable(session, actor)
	]

	return Me(
		api_version=subroutine.API_VERSION,
		user=_user(actor.user),
		credential=_credential(actor),
		instance_permissions=sorted(
			subroutine.domain.authorization.instance_permissions(actor)
		),
		workspaces=workspaces,
	)


def _user (user: subroutine.db.models.identity.User) -> User:
	"""Describe the account, without anything that authenticates it."""

	return User(
		id=user.id,
		username=user.username,
		display_name=user.display_name,
		email=user.email,
		timezone=user.timezone,
		is_superuser=user.is_superuser,
		is_service_account=user.is_service_account,
	)


def _credential (
	actor: subroutine.domain.authentication.Principal,
) -> Credential | None:
	"""Describe the presented credential, or ``None`` when there was none."""

	token = actor.token

	if token is None:
		return None

	return Credential(
		kind="api_token",
		id=token.id,
		title=token.title,
		prefix=token.token_prefix,
		scopes=sorted(token.scopes),
		project_scope=token.project_scope,
		workspace_id=token.workspace_id,
		narrows=bool(token.scopes)
		or token.project_scope is not None
		or token.workspace_id is not None,
		expires_at=token.expires_at,
		last_used_at=token.last_used_at,
	)


def _access (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	workspace: subroutine.db.models.identity.Workspace,
) -> WorkspaceAccess:
	"""Describe what this caller may do in one workspace."""

	grant = subroutine.domain.authorization.explain(session, actor, workspace.id)

	return WorkspaceAccess(
		id=workspace.id,
		slug=workspace.slug,
		title=workspace.title,
		timezone=workspace.timezone,
		role=grant.from_role,
		permissions=sorted(grant.permissions),
		narrowed_by_credential=grant.narrowed_by_token,
	)
