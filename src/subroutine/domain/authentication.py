"""Issuing credentials, and deciding who is holding one.

Answers exactly one question — *who is this* — and deliberately not the next one, *may
they*. Keeping them apart means an endpoint cannot accidentally treat "authenticated" as
"allowed", which is the single most common way permission systems fail open. Authorisation
is docs/design.md §7.3.

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
import subroutine.db.models.project
import subroutine.db.types
import subroutine.domain.accountability
import subroutine.domain.hierarchy
import subroutine.domain.text
import subroutine.errors
import subroutine.permissions

#: How often a token's ``last_used_at`` is allowed to be written. Every authenticated
#: request would otherwise turn a read into a write, which on SQLite means taking the
#: write lock on the hottest path in the application to record something nobody reads to
#: the second (docs/design.md §7.4).
LAST_USED_INTERVAL = datetime.timedelta(minutes=1)

#: How many times to re-roll a token prefix that is already taken. A collision needs tens
#: of thousands of live tokens before it is likely at all, and re-rolling is cheaper than
#: explaining an integrity error to whoever hit it.
PREFIX_ATTEMPTS = 5

#: The doors a request can arrive through — decision `SR#1426` §2, and `SR#1415` is the item.
#:
#: **The one thing about a request that nobody asserts.** A credential's name is typed once by
#: a human and never changes; a client's name is announced by the program on every connection.
#: Both are worth recording and both are claims. This is neither: it is what we *observed* about
#: where the request came in, so it is the fact an audit can lean on when the other two are
#: disputed.
#:
#: **The CLI is not one of these, and that is the correction `SR#1426` §2 makes to its own
#: question.** Talking to a served instance the CLI makes ordinary HTTP requests with a bearer
#: token, indistinguishable at the door from any other API client. What identifies a *program*
#: is the ``Subroutine-Program`` header, which is `SR#839`.
MCP = "mcp"
API = "api"
BROWSER = "browser"
FEED = "feed"

#: No request at all — ``clients/local.py`` opening the database directly (§12.1a).
#:
#: **A real value rather than an absence to paper over.** It is the most privileged path in the
#: product, because §12.4's recovery property depends on it working when the service will not,
#: and it is therefore the origin an operator most wants named afterwards. An event that could
#: not say *this happened at the machine* would be missing exactly that one.
LOCAL = "local"

#: Every door, for the guards that have to check one against the set.
INTERFACES = frozenset({MCP, API, BROWSER, FEED, LOCAL})

#: Which credential each door implies, where a door implies one at all.
#:
#: A bearer token arrives through two doors — ``/mcp`` and ``/v1`` — so the token is absent from
#: this map in both directions: it is the one kind that does not name its own interface, which
#: is why :func:`authenticate` has to be told.
_CREDENTIAL_OF = {BROWSER: "session", FEED: "feed"}


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

	#: A browser's credential, when that is what was presented (`#364`). It carries no
	#: scopes, no project scope and no workspace pin, because a person signed in to the web
	#: UI is themselves rather than a delegation — so every narrowing property below reports
	#: "not narrowed" for a session, and says so rather than arriving there by omission.
	session: subroutine.db.models.identity.WebSession | None = None

	#: A calendar feed's credential, when the caller is a poller holding a URL (`#916`,
	#: docs/design.md §20.2). **The third kind, and the first that is narrower than its owner rather
	#: than equal to them** — it may read tasks, in one scope, and do nothing else.
	#:
	#: It exists as a field for the reason `#364` gave about the one above it: five behaviours
	#: read "no token" as §12.1a's *maximum trust*, so a credential that arrived without
	#: filling a slot here would be handed a person-at-the-terminal's authority by omission,
	#: silently, at every one of them. :attr:`is_local` is a named question precisely so a
	#: kind that did not exist when it was written can be answered correctly.
	feed: subroutine.db.models.identity.CalendarFeed | None = None

	#: Which door this request came in through — `SR#1415`, decision `SR#1426`.
	#:
	#: **Null means *not stated*, never *unknown*** (§12.3's rule about the timezone chain,
	#: which `SR#1426` restates for this field). The difference matters the moment anybody
	#: filters on it: an instance a release behind, a caller this code has not been taught
	#: about, and *we could not tell* are one answer, and *it arrived over MCP* is another.
	#:
	#: **Never an authorisation input.** :func:`authenticate` already checks revocation,
	#: expiry, activity and the whole accountability chain on every request; this is what
	#: lets somebody answer *what actually did this* afterwards, which is what an
	#: accountability chain is for once a person disputes it.
	interface: str | None = None

	def __post_init__ (self) -> None:
		"""Refuse a principal that claims to hold more than one credential at once.

		Nothing constructs one, and that is the point: the properties below would have to
		decide which of them narrows, and any answer to that is a rule in a second place.

		**And refuse a door that disagrees with the credential presented at it.** A stated
		interface is meant to be the one fact here nobody asserts, so a principal saying
		``browser`` while holding an API token would make it an assertion like any other —
		quietly, in the audit trail, where nothing else would ever compare the two.
		"""

		self._refuse_a_door_that_disagrees()

		held = [
			name
			for name, value in (
				("an API token", self.token),
				("a browser session", self.session),
				("a calendar feed", self.feed),
			)
			if value is not None
		]

		if len(held) > 1:
			raise ValueError(
				f"A principal presents one credential. {' and '.join(held)} together would "
				f"leave it ambiguous which one bounds the caller."
			)

	def _refuse_a_door_that_disagrees (self) -> None:
		"""Refuse a stated interface that the presented credential contradicts.

		**A door is where a request arrived, not what it presented, and the two are easy to
		conflate.** The first version of this refused ``local`` for any principal holding a
		credential — reading :attr:`is_local` as the definition — and that is wrong in the
		commonest agent setup there is: an agent on a personal machine holds a token in
		``SUBROUTINE_TOKEN_LOCAL`` and reaches the database through ``clients/local.py``, which
		opens the file directly. There is no request, so the door is ``local``; the token
		narrows what it may do and says nothing about where it is.

		That is `#364`'s lesson inverted. Its warning was that *the absence of a token stops
		meaning the absence of a credential*; the mistake here was making ``local`` mean *no
		credential* when `SR#1426` §2 defines it as *no request at all*.

		**So what each door genuinely implies**: a browser session arrives at the browser and a
		feed credential at the feed, both ways round; ``api`` and ``mcp`` are reached with a
		bearer token, because a cookie at ``/v1`` is built as ``browser`` before this is ever
		asked; and ``local`` is compatible with a token and with nothing, and with neither of
		the two credentials that only exist over a transport.
		"""

		if self.interface is None:
			return

		if self.interface not in INTERFACES:
			raise ValueError(
				f"{self.interface!r} is not a door a request can arrive through. "
				f"Expected one of: {', '.join(sorted(INTERFACES))}."
			)

		if self.interface == LOCAL and (self.session is not None or self.feed is not None):
			raise ValueError(
				"A local caller opens the database directly, so there is no request — and a "
				"browser session and a calendar feed only exist over one."
			)

		wanted = _CREDENTIAL_OF.get(self.interface)

		if wanted is not None and getattr(self, wanted) is None:
			raise ValueError(
				f"A principal that arrived at the {self.interface} door presents "
				f"{'a browser session' if wanted == 'session' else 'a calendar feed'}, and "
				f"this one does not."
			)

		if self.interface in (API, MCP) and self.token is None:
			raise ValueError(
				f"A request at the {self.interface} door authenticates with a token, and this "
				f"principal presents none."
			)

	@property
	def is_local (self) -> bool:
		"""Report whether this caller presented no credential at all.

		**This is §12.1a and nothing else: somebody at a terminal holding the database
		file.** The filesystem permission is the authentication, so no check here narrows
		them — a lock on a door in a field.

		It exists as a name because it used to be spelled ``token is None``, and decision
		`#364` measured what that costs the moment a second kind of credential arrives: the
		absence of a *token* stops meaning the absence of a *credential*, silently, at every
		site that had made the two synonymous. A named question can be answered correctly
		for a kind of credential that did not exist when it was written; a sentinel cannot.
		"""

		return self.token is None and self.session is None and self.feed is None

	@property
	def expires_at (self) -> datetime.datetime | None:
		"""Return when the presented credential stops working, or ``None`` for never.

		A browser session always has one and an API token need not, which is why this is
		asked of the principal rather than of the token: `#356`'s rule is that a credential
		may not issue one that outlives it, and that rule is about the *presenter*, whatever
		kind of thing it happens to be.
		"""

		if self.token is not None:
			return self.token.expires_at

		if self.feed is not None:
			return self.feed.expires_at

		return self.session.expires_at if self.session is not None else None

	@property
	def credential_prefix (self) -> str | None:
		"""Return the public half of whatever credential was presented, or ``None`` if none.

		This is §7.7's rate-limit key. It is safe as a key because *we* mint the prefix: a
		caller cannot manufacture a fresh allowance by inventing one, which is the property
		that made the failure limiter key on the address instead.
		"""

		if self.token is not None:
			return self.token.token_prefix

		if self.feed is not None:
			return self.feed.token_prefix

		return self.session.token_prefix if self.session is not None else None

	@property
	def scopes (self) -> list[str]:
		"""Return the permissions this credential narrows to, empty meaning no narrowing.

		**A calendar feed narrows to reading, and says so in this vocabulary rather than in a
		special case** (`#916`). §20.2 makes it read-only and valid on one endpoint, and the
		honest way to express that is the list every other check already reads — so a feed's
		principal reaching a write would be refused by :func:`~subroutine.domain.authorization.authorize`
		on the ordinary path, rather than by nothing having pointed it there.

		It also makes :attr:`narrows` derive correctly instead of being told: a feed *is*
		bounded, and `#829`'s lesson is that a credential which cannot say so is one that gets
		traded for something wider on the route nobody checked.
		"""

		if self.feed is not None:
			return [subroutine.permissions.TASK_READ]

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
	def narrows (self) -> bool:
		"""Whether this credential is bounded more tightly than its owner's own authority.

		The four axes a token can be narrowed on, asked as one question. A session and a
		person at a terminal both report ``False``, which is the truth about each: neither
		carries a narrowing at all.

		**What this is for is deciding what a credential may be traded in for** (`#829`).
		Anything that hands back authority the presenter cannot express — a browser session
		has no scopes, no project scope and no workspace pin — has to be able to ask whether
		the presenter was bounded, and it is not free to answer that its own way.
		"""

		return narrowing(
			scopes=self.scopes,
			project_scope=self.project_scope,
			project_write_scope=self.project_write_scope,
			workspace_id=self.pinned_workspace_id,
		)

	@property
	def is_superuser (self) -> bool:
		"""Report whether this user bypasses role checks.

		Bypasses roles, never token scopes: a leaked admin-owned agent token would
		otherwise be unbounded, which defeats the point of scoping it (§7.3).
		"""

		return self.user.is_superuser


#: The width ``api_token.title`` holds, declared beside the function that writes it.
#:
#: **Moved down here from ``domain.tokens`` by `SR#1571`**, whose guard drove this column and
#: found nothing refusing it: the check sat in ``tokens.issue`` and this is what stores the
#: row, so a second caller would have gone round it — which is exactly how `#1555`'s
#: vocabulary gap arose one module along.
#:
#: **The derived default passes through it now and the decision behind that is unchanged.**
#: ``{username}'s token`` is ours rather than the caller's and a username is bounded at 64, so
#: it cannot reach this limit and is never refused; what changed is where the rule is applied,
#: not which values it turns down.
MAX_TITLE_LENGTH = 128


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

	title = subroutine.domain.text.fit(title, field="title", limit=MAX_TITLE_LENGTH)

	if actor is not None:
		_refuse_amplification(
			session,
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

	_refuse_a_write_set_outside_the_reach(session, project_scope, project_write_scope)

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


def narrowing (
	*,
	scopes: typing.Sequence[str],
	project_scope: typing.Sequence[str] | None,
	project_write_scope: typing.Sequence[str] | None,
	workspace_id: uuid.UUID | None,
) -> bool:
	"""Whether these four values bound a credential below its owner's own authority.

	**One definition, because the rule was written twice and is now asked in three places.**
	``views.py`` computed it identically at two sites for the token view and the token row,
	and `#829`'s fix needed it a third time — which is the point at which two copies that
	happen to agree become this codebase's signature defect rather than a near miss.

	The emptiness of each side means opposite things and that is the whole difficulty: ``[]``
	scopes and a ``None`` project scope are *no narrowing*, so a truthiness test is right for
	the first and an identity test for the other three.

	**Expiry is deliberately not one of the axes.** It bounds how long a credential lasts
	rather than what it reaches, `#356` handles it with a one-sided comparison that needs two
	expiries to compare, and this is published as ``narrows`` on the token view — so adding a
	fifth axis here would change an answer callers already read.
	"""

	return (
		bool(scopes)
		or project_scope is not None
		or project_write_scope is not None
		or workspace_id is not None
	)


def _refuse_amplification (
	session: sqlalchemy.orm.Session,
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

	**That exemption is asked for by name** (`#248`). It used to be spelled ``token is None``,
	which a browser session would have satisfied — and the early return skips more than the
	scope comparison it looks like it skips: it also skips the check above requiring
	``instance:user_create`` to issue for somebody else. A signed-in browser inheriting it
	could have issued a credential **for any account on the instance**, from the one screen
	where issuing credentials belongs.
	"""

	if actor.is_local:
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

	if actor.project_scope is not None:
		allowed = set(actor.project_scope)
		asked_reach = (
			None if project_scope is None else _canonical_project_scope(project_scope)
		)

		if asked_reach is None or _outside(session, allowed, asked_reach):
			raise subroutine.errors.Forbidden(
				"A token cannot reach more projects than the one that asked for it.",
				errors=[
					subroutine.errors.FieldError(
						field="project_scope",
						code="forbidden",
						message="The credential you presented reaches: "
						f"{_named(session, allowed)}.",
						hint="Issue a token reaching the same projects or fewer — each one "
						"named there, or filed under something that is.",
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
	held_writes = actor.project_write_scope
	bounds = held_writes if held_writes is not None else actor.project_scope

	if bounds is not None:
		asked = (
			project_write_scope
			if project_write_scope is not None
			else project_scope
		)
		allowed = set(bounds)

		if asked is None or _outside(session, allowed, _canonical_project_scope(asked)):
			raise subroutine.errors.Forbidden(
				"A token cannot write in more projects than the one that asked for it.",
				errors=[
					subroutine.errors.FieldError(
						field="project_write_scope",
						code="forbidden",
						message="The credential you presented writes in: "
						f"{_named(session, allowed)}.",
						hint="Issue a token writing in the same projects or fewer — each one "
						"named there, or filed under something that is.",
					)
				],
			)

	# **A browser session participates here, and that is a decision rather than a
	# consequence** (`#248`). A session is time-bounded so that a stolen cookie stops
	# working; a permanent API token minted from one would end that property in a single
	# call, which is `#356`'s escalation arriving through a door `#356` could not see.
	held_until = actor.expires_at

	if held_until is not None and (expires_at is None or expires_at > held_until):
		# **The instant, not the day it falls on** (`#1091`). A moment has no day until
		# somebody names a zone, and there is none to name here: this runs in the domain,
		# below any workspace, and §6.5's chain needs a session this function does not take.
		# Saying the instant is not a way round that — it is the better answer. A caller told
		# "expires on 2026-09-01 or sooner" who then asks for the end of that day is refused a
		# second time, because the bound was never a day.
		until = held_until.isoformat()

		raise subroutine.errors.Forbidden(
			"A token cannot outlive the one that asked for it.",
			errors=[
				subroutine.errors.FieldError(
					# `POST /v1/tokens` accepts `expires`, and so does the flag on
					# `token create` (`#1534`).
					field="expires",
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
	record_use: bool = True,
	interface: str | None = None,
) -> Principal:
	"""Resolve a presented token into the principal holding it.

	Raises :class:`AuthenticationError` for every kind of refusal. An unknown prefix and a
	wrong secret raise the same one on purpose — distinguishing them would tell an
	attacker when they had guessed half of a credential.

	**``record_use=False`` is for resolving the same credential a second time inside one
	request** — `#565`. A request that authenticates twice used to write ``last_used_at``
	twice, in two sessions, on one row, and the second write blocked on the lock the first was
	holding until the handler returned. One request, deadlocked against itself, with no
	concurrency involved.

	Recording it once per request is not a compromise reached to avoid that: a request is one
	use, and counting it twice was always wrong. The deadlock is what made it visible.

	**``interface`` is the one thing this function cannot work out for itself** — `SR#1415`. A
	bearer token arrives at ``/mcp`` and at ``/v1`` alike, and which of them it was is a fact
	about the transport, which the domain deliberately knows nothing about. So the caller that
	holds the request says. ``None`` is *not stated* rather than *unknown*, and every other
	construction site spells its own door as a literal because it has only one.
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

	_refuse_an_agent_nobody_answers_for(session, user, prefix=prefix)

	if record_use:
		_record_use(token, moment)


	return Principal(user=user, token=token, interface=interface)


def _refuse_an_agent_nobody_answers_for (
	session: sqlalchemy.orm.Session,
	user: subroutine.db.models.identity.User,
	*,
	prefix: str | None,
) -> None:
	"""Refuse an agent whose accountability chain does not reach an *active* person — `#479`.

	Decision `#473`: somebody gave an agent permission to work, and when that somebody leaves,
	so does the permission. The check above asks whether *this* account is active; this asks the
	same question of everybody it answers to, which is the half that makes marking a leaver
	inactive mean anything.

	**Fails safe, and that is the whole argument.** An agent acting with nobody on the hook is
	what the model exists to prevent, so a chain that cannot be resolved — broken, circular, or
	naming nobody — is a refusal rather than a shrug. `domain.accountability` already refuses
	those on the way in; reaching one here means a database somebody edited, or the row a
	migration deliberately left when it declined to guess between two administrators.

	A person is not walked at all: they answer for themselves, and the check above has already
	asked whether they are active.
	"""

	if not user.is_service_account:
		return

	try:
		walked = subroutine.domain.accountability.chain(session, user)

	except subroutine.errors.ValidationError as broken:
		raise AuthenticationError(
			AuthenticationFailure.USER_INACTIVE, prefix=prefix
		) from broken

	# Everybody in the chain, not only the person at the end: an intermediate agent that has
	# been deactivated is a link somebody deliberately cut, and honouring only the far end
	# would walk straight past it.
	for entry in walked[1:]:
		if not entry.is_active or entry.deleted_at is not None:
			raise AuthenticationError(AuthenticationFailure.USER_INACTIVE, prefix=prefix)


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
	session: sqlalchemy.orm.Session,
	project_scope: list[str] | None,
	project_write_scope: list[str] | None,
) -> None:
	"""Refuse a write set naming a project the credential cannot even see — item ``#371``.

	**The two lists are not independent, and letting them be would make the narrower one a
	lie.** A credential that reaches `SUBSAMPLE` and claims a write set of `SR` would report
	itself as able to write in a project every read of which returns nothing — a control that
	is present, exercised and meaningless, which is this codebase's second signature defect.

	**"Inside the reach" means what it means everywhere else, which took a second attempt**
	(`#413`). This was a flat set subset, and the reach it was guarding is subtree-inclusive:
	``--project SR --write WEB``, with ``WEB`` filed under ``SR``, was refused by the sentence
	*"a project it cannot read"* — about a project it reads and writes perfectly well. That is
	the ordinary shape of decision `#370`'s ``collaborator``, so the profile built for it could
	not express it on any tree deeper than one level. :func:`subroutine.domain.hierarchy.within`
	is now the one implementation, shared with ``authorization._covers``.

	**So this does ask the database, and only about the write set.** The ids are not validated
	for *existence* — :func:`_canonical_project_scope`'s reason still holds, and a credential
	may name a project its issuer cannot see or one created later. What is fetched is the
	``path`` needed to place a project in the tree, and a project with no row is covered only by
	being named outright: unknown means unplaceable, never unwelcome. It is deliberately not
	narrowed by visibility, because this is a question about the shape of the tree rather than
	about who may look at it — and ``_refuse_amplification`` has already bounded the caller to
	its own reach before this runs.
	"""

	if project_scope is None or project_write_scope is None:
		return

	reach = set(project_scope)
	placed = _projects_by_id(session, project_write_scope)
	outside = []

	for identifier in project_write_scope:
		found = placed.get(identifier)

		if not subroutine.domain.hierarchy.within(
			reach, identifier=identifier, path=None if found is None else found.path
		):
			# **The key where there is one** (`#203`). Somebody typed `WEB`; reading it back as
			# a UUID sends them to look up what they just wrote, in the one message they meet
			# while still holding the command they meant to type.
			outside.append(identifier if found is None else found.key)

	if not outside:
		return

	raise subroutine.errors.ValidationError(
		"A credential cannot be given write access to a project it cannot read.",
		errors=[
			subroutine.errors.FieldError(
				field="project_write_scope",
				code="invalid_field_value",
				message=f"Not inside this credential's reach: {', '.join(sorted(outside))}.",
				hint="The write set has to be inside the projects the credential can reach — "
				"each one named there, or filed under something that is. Widen project_scope, "
				"or drop these from the write set.",
			)
		],
	)


def _projects_by_id (
	session: sqlalchemy.orm.Session, identifiers: typing.Sequence[str]
) -> dict[str, subroutine.db.models.project.Project]:
	"""Return the projects among these ids that exist, keyed by the id as it was written.

	Keyed by the string rather than by the :class:`uuid.UUID` so that the caller compares the
	same values it was handed — :func:`_canonical_project_scope` has already settled what a
	canonical id looks like, and re-deriving it here would be a second opinion on it.
	"""

	model = subroutine.db.models.project.Project
	found = session.scalars(
		sqlalchemy.select(model).where(
			model.id.in_([uuid.UUID(identifier) for identifier in identifiers])
		)
	)

	return {str(row.id): row for row in found}


def _outside (
	session: sqlalchemy.orm.Session,
	allowed: typing.Collection[str],
	asked: typing.Sequence[str],
) -> bool:
	"""Report whether anything asked for falls outside a credential's own bounds — `#344`.

	**Two rules about one thing, and they disagreed.** ``authorization._within_project_scope``
	decides what a credential may *reach* and honours the subtree, in its own words because
	*"restricting an agent to a project and then refusing it the sub-projects underneath would
	make the restriction useless for any tree deeper than one level"*. This one decides what it
	may *hand on*, and compared ids by flat set membership — so a credential restricted to a
	parent could read a child perfectly well and could not delegate it. This codebase's
	signature defect, in the security layer, where both copies read as correct on their own.

	**It errs safe, which is why it waited**: the failure is a refusal rather than an
	escalation. What it blocks is the ordinary act of week one — granting a teammate or a
	sub-agent access inside a project subtree.

	**`#413` had already put the predicate in the tree** and this is the third caller of it, so
	*"this project and everything under it"* is one implementation rather than three opinions.
	`#423` is the same fix on ``project_write_scope``, which was a second flat comparison two
	clauses down and is why this takes the bounds as an argument rather than reading a
	principal.

	A project with no row is covered only by being named outright, exactly as in
	:func:`_refuse_a_write_set_outside_the_reach`: unknown means unplaceable, never unwelcome.
	The lookup is deliberately not narrowed by visibility — this is a question about the shape
	of the tree rather than about who may look at it.
	"""

	placed = _projects_by_id(session, asked)

	return any(
		not subroutine.domain.hierarchy.within(
			allowed,
			identifier=identifier,
			path=None if placed.get(identifier) is None else placed[identifier].path,
		)
		for identifier in asked
	)


def _named (session: sqlalchemy.orm.Session, identifiers: typing.Collection[str]) -> str:
	"""Render a set of project ids as the keys somebody typed — `#344`, `#203`.

	**The one place in this API a person was shown a raw UUID and asked to work out what it
	was.** Every other surface resolves an id to its key, and this sentence is met while
	somebody is still holding the command they meant to run — so reading it back as a UUID
	sends them to look up what they had just written.

	An id with no row is printed as it stands rather than dropped. A credential may name a
	project its issuer cannot see or one created later, and a bound that quietly omitted such
	a project would understate what the credential actually carries.
	"""

	placed = _projects_by_id(session, list(identifiers))

	return ", ".join(
		sorted(
			identifier if placed.get(identifier) is None else placed[identifier].key
			for identifier in identifiers
		)
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
