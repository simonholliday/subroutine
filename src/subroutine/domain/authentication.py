"""Issuing credentials, and deciding who is holding one.

Answers exactly one question — *who is this* — and deliberately not the next one, *may
they*. Keeping them apart means an endpoint cannot accidentally treat "authenticated" as
"allowed", which is the single most common way permission systems fail open. Authorisation
is SPEC.md §7.3.

Every refusal here is the same refusal as far as the caller is concerned. The reason is
recorded for the log and for metrics, never returned: telling an unauthenticated stranger
that a token exists but has expired is a fact they had no way to learn otherwise.
"""

import dataclasses
import datetime
import enum
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.auth
import subroutine.db.models.identity
import subroutine.db.types
import subroutine.errors
import subroutine.permissions

#: How often a token's ``last_used_at`` is allowed to be written. Every authenticated
#: request would otherwise turn a read into a write, which on SQLite means taking the
#: write lock on the hottest path in the application to record something nobody reads to
#: the second (SPEC.md §7.4).
LAST_USED_INTERVAL = datetime.timedelta(minutes=1)

#: How many times to re-roll a token prefix that is already taken. A collision needs tens
#: of thousands of live tokens before it is likely at all, and re-rolling is cheaper than
#: explaining an integrity error to whoever hit it.
PREFIX_ATTEMPTS = 5


class AuthenticationFailure(enum.StrEnum):
	"""Why a credential was refused.

	For logs, metrics and tests. The API turns every one of these into the same 401.
	"""

	MALFORMED = "malformed"
	UNKNOWN = "unknown"
	REVOKED = "revoked"
	EXPIRED = "expired"
	USER_INACTIVE = "user_inactive"


class AuthenticationError(subroutine.errors.Unauthenticated):
	"""Raised when a presented credential cannot be accepted.

	Carries the token's public prefix where there is one, because that is the part it is
	safe to write down. The secret never reaches this object, and so never reaches a
	traceback.

	The message is the same for every reason. It lists all of them rather than naming the
	one that applied, which is useful to whoever holds the token and tells anyone who does
	not hold it nothing they could not already guess.
	"""

	def __init__ (self, failure: AuthenticationFailure, *, prefix: str | None = None) -> None:
		"""Record the reason, and the token prefix if one could be read."""

		super().__init__(
			"Your credentials were not accepted. The token may be mistyped, revoked, or "
			"expired, or the account that owns it may be inactive."
		)

		self.failure = failure
		self.prefix = prefix


@dataclasses.dataclass(frozen=True)
class Principal:
	"""Who is acting, and how far the credential they used lets them go.

	A token may narrow its owner's authority but never widen it, so the properties here
	report the *narrowing* and leave the intersection with the owner's role to §7.3. An
	empty ``scopes`` and a null ``project_scope`` mean no narrowing at all — read as
	literal set algebra they would mean the opposite, which is the easiest way to ship an
	API where nothing works.
	"""

	user: subroutine.db.models.identity.User
	token: subroutine.db.models.identity.ApiToken | None = None

	@property
	def scopes (self) -> list[str]:
		"""Return the permissions this credential narrows to, empty meaning no narrowing."""

		return list(self.token.scopes) if self.token is not None else []

	@property
	def project_scope (self) -> list[str] | None:
		"""Return the projects this credential is restricted to, or ``None`` for all."""

		return None if self.token is None else self.token.project_scope

	@property
	def pinned_workspace_id (self) -> uuid.UUID | None:
		"""Return the workspace this credential is pinned to, or ``None`` for any."""

		return None if self.token is None else self.token.workspace_id

	@property
	def is_superuser (self) -> bool:
		"""Report whether this user bypasses role checks.

		Bypasses roles, never token scopes: a leaked admin-owned agent token would
		otherwise be unbounded, which defeats the point of scoping it (§7.3).
		"""

		return self.user.is_superuser


def issue_token (
	session: sqlalchemy.orm.Session,
	*,
	user: subroutine.db.models.identity.User,
	title: str,
	workspace_id: uuid.UUID | None = None,
	scopes: typing.Sequence[str] = (),
	project_scope: typing.Sequence[str] | None = None,
	expires_at: datetime.datetime | None = None,
	created_by: uuid.UUID | None = None,
) -> tuple[subroutine.db.models.identity.ApiToken, subroutine.auth.IssuedToken]:
	"""Mint a token for ``user`` and store its hash, returning both halves.

	The returned :class:`~subroutine.auth.IssuedToken` is the only time the secret is
	readable — show it once and let it go. Nothing recovers it afterwards, including this
	code.

	``scopes`` are checked against the known permissions and rejected if any is not one,
	so a typo becomes an error rather than a silently inert restriction. They are *not*
	checked against what the user can actually do: a token spans workspaces where its
	owner may hold different roles, and §7.3 intersects the two at every check anyway.
	"""

	unknown = subroutine.permissions.unknown(scopes)

	if unknown:
		valid = ", ".join(sorted(subroutine.permissions.ALL))

		raise ValueError(
			f"Unknown permission(s) in scopes: {', '.join(unknown)}. Valid permissions are: {valid}."
		)

	issued = _mint_unused_token(session)

	token = subroutine.db.models.identity.ApiToken(
		user_id=user.id,
		workspace_id=workspace_id,
		title=title,
		token_prefix=issued.prefix,
		token_hash=issued.token_hash,
		scopes=list(scopes),
		project_scope=None if project_scope is None else list(project_scope),
		expires_at=expires_at,
		created_by=created_by,
	)
	session.add(token)
	session.flush()

	return token, issued


def authenticate (
	session: sqlalchemy.orm.Session,
	presented: str,
	*,
	now: datetime.datetime | None = None,
) -> Principal:
	"""Resolve a presented token into the principal holding it.

	Raises :class:`AuthenticationError` for every kind of refusal. An unknown prefix and a
	wrong secret raise the same one on purpose — distinguishing them would tell an
	attacker when they had guessed half of a credential.
	"""

	parsed = subroutine.auth.parse_token(presented)

	if parsed is None:
		raise AuthenticationError(AuthenticationFailure.MALFORMED)

	prefix, secret = parsed
	moment = now if now is not None else subroutine.db.types.utcnow()

	model = subroutine.db.models.identity.ApiToken
	token = session.scalars(
		sqlalchemy.select(model).where(model.token_prefix == prefix)
	).one_or_none()

	if token is None or not subroutine.auth.token_matches(secret, token.token_hash):
		raise AuthenticationError(AuthenticationFailure.UNKNOWN, prefix=prefix)

	if token.revoked_at is not None:
		raise AuthenticationError(AuthenticationFailure.REVOKED, prefix=prefix)

	if token.expires_at is not None and token.expires_at <= moment:
		raise AuthenticationError(AuthenticationFailure.EXPIRED, prefix=prefix)

	user = session.get(subroutine.db.models.identity.User, token.user_id)

	if user is None or not user.is_active or user.deleted_at is not None:
		raise AuthenticationError(AuthenticationFailure.USER_INACTIVE, prefix=prefix)

	_record_use(token, moment)

	return Principal(user=user, token=token)


def revoke_token (
	token: subroutine.db.models.identity.ApiToken,
	*,
	at: datetime.datetime | None = None,
) -> None:
	"""Withdraw a token, effective on the next request that presents it.

	Idempotent, and keeps the first revocation time: when a credential stopped being
	trusted is a fact worth not overwriting.
	"""

	if token.revoked_at is None:
		token.revoked_at = at if at is not None else subroutine.db.types.utcnow()


def _mint_unused_token (session: sqlalchemy.orm.Session) -> subroutine.auth.IssuedToken:
	"""Generate a token whose prefix is not already in use."""

	model = subroutine.db.models.identity.ApiToken

	for _attempt in range(PREFIX_ATTEMPTS):
		issued = subroutine.auth.generate_token()

		taken = session.scalars(
			sqlalchemy.select(model.id).where(model.token_prefix == issued.prefix)
		).first()

		if taken is None:
			return issued

	raise RuntimeError(
		f"Could not find an unused token prefix in {PREFIX_ATTEMPTS} attempts. This "
		f"should be impossible; check that the random number source is working."
	)


def _record_use (
	token: subroutine.db.models.identity.ApiToken, moment: datetime.datetime
) -> None:
	"""Note that a token was used, no more than once per ``LAST_USED_INTERVAL``.

	Written through the ORM rather than as its own statement so that it joins the caller's
	transaction. If that transaction rolls back the timestamp is lost, which is the right
	trade: this is telemetry, and it must never be the reason a request is held open.
	"""

	if token.last_used_at is not None and moment - token.last_used_at < LAST_USED_INTERVAL:
		return

	token.last_used_at = moment
