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
	def project_write_scope (self) -> list[str] | None:
		"""Return where this credential may *change* things, or ``None`` for its whole reach.

		**Not the same question as :attr:`project_scope`, which is what it can see** (`#371`).
		A credential that reads a related tree and writes into one project of it is the
		ordinary shape for an agent working alongside others, and one list cannot say it.

		``None`` falls back to the reach, so a credential issued before this existed is
		unchanged — and a caller must never read this as "everywhere", because for a scoped
		credential it is not.
		"""

		return None if self.token is None else self.token.project_write_scope

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
	project_write_scope: typing.Sequence[str] | None = None,
	expires_at: datetime.datetime | None = None,
	created_by: uuid.UUID | None = None,
	actor: Principal | None = None,
) -> tuple[subroutine.db.models.identity.ApiToken, subroutine.auth.IssuedToken]:
	"""Mint a token for ``user`` and store its hash, returning both halves.

	The returned :class:`~subroutine.auth.IssuedToken` is the only time the secret is
	readable — show it once and let it go. Nothing recovers it afterwards, including this
	code.

	``scopes`` are checked against the known permissions and rejected if any is not one,
	so a typo becomes an error rather than a silently inert restriction. They are *not*
	checked against what the user can actually do: a token spans workspaces where its
	owner may hold different roles, and §7.3 intersects the two at every check anyway.

	**A credential may not mint a wider credential, and nothing enforced that until
	2026-07-30.** This function took no actor at all, so an agent holding a token scoped to
	``task:read`` could issue itself one with no scope restriction — the scoping refusal it had
	just met was correct, well-worded, and one command away from being irrelevant. §7.4's whole
	least-privilege story rests on a narrow token *staying* narrow, and a credential that can
	widen itself is not a restriction, it is a formality. :func:`_refuse_amplification` is the
	rule; ``actor=None`` remains the unauthenticated internal caller, which for this function
	means ``subroutine init`` and a script holding the database file.
	"""

	if actor is not None:
		_refuse_amplification(
			actor,
			user=user,
			workspace_id=workspace_id,
			scopes=scopes,
			project_scope=project_scope,
			project_write_scope=project_write_scope,
			expires_at=expires_at,
		)

	unknown = subroutine.permissions.unknown(scopes)

	if unknown:
		valid = ", ".join(sorted(subroutine.permissions.ALL))

		# **A refusal, not a programming error** (`#209`). This was a `ValueError`, which is
		# neither of the two things a caller can be given: over HTTP it became a 500 with a
		# request id and nothing to act on, and on the CLI a Rich traceback. Both surfaces can
		# send this — a `--scope` typo is the obvious way — and CLAUDE.md's rule is that
		# anything a client can send is checked where the message can name the field.
		raise subroutine.errors.ValidationError(
			f"Unknown permission(s) in scopes: {', '.join(sorted(unknown))}.",
			errors=[
				subroutine.errors.FieldError(
					field="scopes",
					code="invalid_field_value",
					message=f"Not a permission this instance has: {', '.join(sorted(unknown))}.",
					hint=f"Valid permissions are: {valid}.",
				)
			],
		)

	project_scope = None if project_scope is None else _canonical_project_scope(project_scope)
	project_write_scope = (
		None
		if project_write_scope is None
		else _canonical_project_scope(project_write_scope, field="project_write_scope")
	)

	_refuse_a_write_set_outside_the_reach(project_scope, project_write_scope)

	issued = _mint_unused_token(session)

	token = subroutine.db.models.identity.ApiToken(
		user_id=user.id,
		workspace_id=workspace_id,
		title=title,
		token_prefix=issued.prefix,
		token_hash=issued.token_hash,
		scopes=list(scopes),
		project_scope=None if project_scope is None else list(project_scope),
		project_write_scope=(
			None if project_write_scope is None else list(project_write_scope)
		),
		expires_at=expires_at,
		created_by=created_by,
	)
	session.add(token)
	session.flush()

	return token, issued


def _refuse_amplification (
	actor: Principal,
	*,
	user: subroutine.db.models.identity.User,
	workspace_id: uuid.UUID | None,
	scopes: typing.Sequence[str],
	project_scope: typing.Sequence[str] | None,
	project_write_scope: typing.Sequence[str] | None,
	expires_at: datetime.datetime | None,
) -> None:
	"""Refuse a token that would grant more than the credential asking for it.

	Four separate ways to amplify, and all four are refused:

	* **Issuing for somebody else.** That is minting authority you do not hold, and it needs
	  the instance permission that creating an account needs — the same authority, since a
	  service account plus a token for it is one act in two steps.
	* **Widening the scopes.** ``[]`` means *no narrowing* (§7.3), so a presenter with any
	  scopes at all may only issue a subset of them. Getting this backwards — treating the
	  empty list as "no permissions" — would refuse every ordinary case, which is why the
	  emptiness of each side is tested explicitly rather than by set algebra alone.
	* **Widening the project scope, or unpinning a pinned workspace.** ``None`` means *every
	  project* for the same reason, and a token pinned to one workspace may not hand out one
	  that reaches them all.
	* **Outliving the credential that asked** (`#356`). This one was missing, and the docstring
	  above it said there were three — a completeness claim nothing checked. An agent holding a
	  credential that expired tomorrow issued itself a permanent one, same scopes, same account,
	  no refusal. That is not an edge: ``--expires now+30d`` is exactly how "a month's work on
	  somebody else's instance" is bounded, and it could be undone on the first day by the
	  credential it bounds.

	**The expiry rule is one-sided, and that is deliberate.** A presenter with no expiry may
	issue any expiry, because issuing something *narrower* than yourself is the whole point.
	Only the absent-or-later direction is amplification.

	A presenter with no credential at all — a person at a terminal with the database file — is
	not narrowed by any of this, which is §12.1a's position: the filesystem permission is the
	authentication, and a check inside a process that already holds the file handle is a lock
	on a door in a field.
	"""

	if actor.token is None:
		return

	if user.id != actor.user.id:
		# Imported here, not at the top: `domain.authorization` imports *this* module for
		# `Principal`, so a module-level import is the circular-import trap CLAUDE.md records.
		# The house style's documented exception for a nested `from X import Y as alias` exists
		# for exactly this, and the alias keeps `subroutine` from being rebound as a local.
		from subroutine.domain import authorization as permits

		permits.authorize_instance(actor, subroutine.permissions.INSTANCE_USER_CREATE)

	held = set(actor.scopes)

	if held and (not scopes or not set(scopes) <= held):
		raise subroutine.errors.Forbidden(
			"A token cannot grant more than the one that asked for it.",
			errors=[
				subroutine.errors.FieldError(
					field="scopes",
					code="forbidden",
					message="The credential you presented is scoped to: "
					f"{', '.join(sorted(held))}.",
					hint="Issue a token scoped to the same permissions or fewer.",
				)
			],
		)

	if actor.token.project_scope is not None:
		allowed = set(actor.token.project_scope)

		if project_scope is None or not set(_canonical_project_scope(project_scope)) <= allowed:
			raise subroutine.errors.Forbidden(
				"A token cannot reach more projects than the one that asked for it.",
				errors=[
					subroutine.errors.FieldError(
						field="project_scope",
						code="forbidden",
						message=f"The credential you presented reaches: {', '.join(sorted(allowed))}.",
					)
				],
			)

	if actor.pinned_workspace_id is not None and workspace_id != actor.pinned_workspace_id:
		raise subroutine.errors.Forbidden(
			"A token pinned to one workspace cannot issue one that is not.",
			errors=[
				subroutine.errors.FieldError(
					field="workspace_id",
					code="forbidden",
					message="The credential you presented is pinned to "
					f"{actor.pinned_workspace_id}.",
				)
			],
		)

	# **The write set is compared against the presenter's, falling back to its reach** (`#371`).
	# A credential that may write only in `SUBSAMPLE` must not issue one that writes across
	# `SR` — and where the presenter has no write set of its own, what bounds it is what it can
	# reach, which is exactly what `_within_write_scope` falls back to at check time. The two
	# fallbacks have to agree or the guard and the enforcement mean different things.
	held_writes = actor.token.project_write_scope
	bounds = held_writes if held_writes is not None else actor.token.project_scope

	if bounds is not None:
		asked = (
			project_write_scope
			if project_write_scope is not None
			else project_scope
		)
		allowed = set(bounds)

		if asked is None or not set(_canonical_project_scope(asked)) <= allowed:
			raise subroutine.errors.Forbidden(
				"A token cannot write in more projects than the one that asked for it.",
				errors=[
					subroutine.errors.FieldError(
						field="project_write_scope",
						code="forbidden",
						message=f"The credential you presented writes in: "
						f"{', '.join(sorted(allowed))}.",
					)
				],
			)

	if actor.token.expires_at is not None and (
		expires_at is None or expires_at > actor.token.expires_at
	):
		until = actor.token.expires_at.date().isoformat()

		raise subroutine.errors.Forbidden(
			"A token cannot outlive the one that asked for it.",
			errors=[
				subroutine.errors.FieldError(
					field="expires_at",
					code="forbidden",
					message=f"The credential you presented stops working on {until}.",
					hint=f"Issue one that expires on {until} or sooner.",
				)
			],
		)


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


def _canonical_project_scope (
	project_scope: typing.Sequence[str], *, field: str = "project_scope"
) -> list[str]:
	"""Return the project ids in the exact form the permission check compares against.

	Two failures are being closed here, and both are silent without it. A malformed id
	produces a token that is refused on every project for a reason nobody can see; and a
	correctly-typed id in the wrong case does the same, because the check compares strings
	against the lowercase form the path is built from.

	An empty list is refused outright rather than guessed at. Its sibling ``scopes == []``
	means "no narrowing", so one reading of ``project_scope == []`` widens the token to
	every project and the other denies it every project — and picking either on the
	caller's behalf gets a security control wrong in silence.

	The ids are *not* checked against existing projects: a token may legitimately name a
	project its issuer cannot see, or one created later.

	``field`` names the argument in the refusal, because ``project_write_scope`` goes through
	the same rules and a message about the wrong field sends its reader to the wrong flag
	(`#371`).
	"""

	if not project_scope:
		# Reported the way every other bad field is (`#209`). It used to be a `ValueError`,
		# which reaches an HTTP caller as a 500 and a person as a traceback — for a mistake
		# either of them can make in one keystroke.
		raise subroutine.errors.ValidationError(
			f"An empty {field} is ambiguous: it could mean every project or none.",
			errors=[
				subroutine.errors.FieldError(
					field=field,
					code="invalid_field_value",
					message="An empty list says nothing about which projects are meant.",
					hint=f"Name at least one project id, or leave {field} out entirely for a "
					f"credential that is not restricted that way.",
				)
			],
		)

	canonical: list[str] = []

	for entry in project_scope:
		try:
			canonical.append(str(uuid.UUID(str(entry))))

		except (ValueError, AttributeError, TypeError):
			raise subroutine.errors.ValidationError(
				f"{entry!r} is not a project id.",
				errors=[
					subroutine.errors.FieldError(
						field=field,
						code="invalid_field_value",
						message=f"{field} holds project ids; {entry!r} is not one.",
						hint="A project's id is the `id` field of GET /v1/projects — a UUID, "
						"not its key.",
					)
				],
			) from None

	return canonical


def _refuse_a_write_set_outside_the_reach (
	project_scope: list[str] | None, project_write_scope: list[str] | None
) -> None:
	"""Refuse a write set naming a project the credential cannot even see — item ``#371``.

	**The two lists are not independent, and letting them be would make the narrower one a
	lie.** A credential that reaches `SUBSAMPLE` and claims a write set of `SR` would report
	itself as able to write in a project every read of which returns nothing — a control that
	is present, exercised and meaningless, which is this codebase's second signature defect.

	Nothing here checks against *real* projects, for :func:`_canonical_project_scope`'s reason:
	a credential may name a project its issuer cannot see, or one created later. This is the
	relationship between the two fields, which is knowable without asking the database.
	"""

	if project_scope is None or project_write_scope is None:
		return

	outside = sorted(set(project_write_scope) - set(project_scope))

	if not outside:
		return

	raise subroutine.errors.ValidationError(
		"A credential cannot be given write access to a project it cannot read.",
		errors=[
			subroutine.errors.FieldError(
				field="project_write_scope",
				code="invalid_field_value",
				message=f"Not inside this credential's reach: {', '.join(outside)}.",
				hint="The write set is a subset of the projects the credential can reach. "
				"Widen project_scope, or drop these from the write set.",
			)
		],
	)


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
