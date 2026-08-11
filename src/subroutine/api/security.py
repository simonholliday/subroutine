"""Working out who is calling.

One dependency, several ways of proving identity. Today there is exactly one — a bearer
token — but the shape is what matters: a *resolver* is asked to find a credential of its
own kind and turn it into a principal, and adding cookie sessions later (SPEC.md §7.5) is
a new resolver in :data:`RESOLVERS` rather than an edit to every endpoint.

A resolver returns ``None`` when it finds no credential of its kind, and raises when it
finds one it cannot accept. That distinction is the whole protocol: "there is no cookie
here" must not stop the bearer resolver from looking, while "this cookie is forged" must
not fall through to a cheerful 401 about how to authenticate.

**Authentication is a dependency, not middleware.** Middleware with a list of exempt paths
is how an endpoint ends up unprotected: the list lives somewhere other than the route, and
nothing fails when the two disagree. A dependency is declared on the route itself — and
because *forgetting* it is then the failure mode, ``tests/test_api_authentication.py``
walks every registered route and fails the build for any that neither requires a principal
nor appears in an explicit public list.
"""

import datetime
import typing
import urllib.parse

import fastapi
import sqlalchemy.orm
import starlette.requests
import starlette.responses

import subroutine.api.dependencies
import subroutine.auth
import subroutine.config
import subroutine.domain.authentication
import subroutine.domain.sessions
import subroutine.errors

#: The one authentication scheme this API accepts. Compared case-insensitively, as
#: RFC 9110 requires; presented in its canonical form in ``WWW-Authenticate``.
BEARER_SCHEME = "Bearer"

#: Query parameters a caller might reasonably, and wrongly, put a token in. Public because
#: `api/logs.py` derives what to keep out of the access log from it — a token sent this way is
#: refused *and the refusal says to treat it as compromised*, so writing it down afterwards is
#: the same mistake one layer on (`#806`). Tokens are
#: never accepted from a query string — they end up in access logs, browser history and
#: referrer headers (SPEC.md §7.4) — but silently ignoring one leaves the caller staring
#: at a 401 with a credential they can plainly see in the URL, so the refusal says so.
TOKEN_PARAMETERS = ("token", "api_key", "apikey", "access_token", "auth")

#: A resolver: find a credential of one kind and identify its holder. ``None`` means "not
#: my kind of credential"; raising means "my kind, and not acceptable".
class Resolver(typing.Protocol):
	"""One way of proving identity — §7.5's seam, and what a second credential type adds.

	A protocol rather than a ``Callable`` alias so that ``record_use`` can be keyword-only:
	every resolver has to accept it, because it is the caller's statement that this request
	has already been counted (`#565`), and a resolver that quietly ignored it would
	reintroduce the double write on whichever credential type it handles.
	"""

	def __call__ (
		self,
		session: sqlalchemy.orm.Session,
		request: starlette.requests.Request,
		*,
		record_use: bool = True,
	) -> subroutine.domain.authentication.Principal | None:
		"""Return the principal this credential names, or ``None`` if it is not present."""


def from_bearer_token (
	session: sqlalchemy.orm.Session,
	request: starlette.requests.Request,
	*,
	record_use: bool = True,
) -> subroutine.domain.authentication.Principal | None:
	"""Identify the caller from ``Authorization: Bearer sr_…``."""

	header = request.headers.get("authorization")

	if header is None:
		return None

	scheme, _, credential = header.partition(" ")

	if scheme.lower() != BEARER_SCHEME.lower():
		raise subroutine.errors.Unauthenticated(
			f"This API does not accept the {scheme!r} authentication scheme."
			if scheme
			else "The Authorization header could not be read.",
			hint=f"Send 'Authorization: {BEARER_SCHEME} sr_…'.",
		)

	# A calendar feed's credential is deliberately not a token: separate table, separate
	# `sr_cal_` prefix, read-only, and refused here (SPEC.md §7.4, §20). It fails the token
	# grammar as it stands, so this costs nothing but says why rather than reporting a
	# mistyped token.
	if credential.strip().startswith(f"{subroutine.auth.TOKEN_SCHEME}_cal_"):
		raise subroutine.errors.Unauthenticated(
			"That is a calendar feed credential, which cannot be used to call the API.",
			hint="Calendar credentials are read-only and work only in their own feed URL. "
			"Create an API token with 'subroutine token create'.",
		)

	_refuse_a_credential_of_another_kind(credential)

	return subroutine.domain.authentication.authenticate(
		session, credential, record_use=record_use
	)


#: The port each scheme uses by default, which a browser leaves out of an ``Origin``.
DEFAULT_PORTS = {"http": 80, "https": 443}

#: Methods that cannot change anything, so a browser sending one from anywhere is harmless.
#: Everything else is treated as a write, including a verb nobody has heard of — the unknown
#: case has to fail closed, since the whole point is that this list is not the route's.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def origin_of (url: str | None) -> str | None:
	"""Return the origin a URL belongs to — scheme, host and port — or ``None``.

	**An origin is not a prefix of a URL**, which is what makes this worth a function. It drops
	any path, lower-cases the scheme and host, and omits a port the scheme uses by default,
	because that is what a browser does before it puts one in a header. Comparing configured
	text instead would refuse a legitimate page over a capital letter in ``public_url`` or a
	``:443`` somebody typed — and it fails *closed*, so the symptom is a browser that stops
	working on the instance whose operator was most careful about writing their address out.

	``None`` for anything that is not an absolute URL, so a caller can discard it rather than
	comparing against a value that means "unset".
	"""

	parsed = urllib.parse.urlsplit((url or "").strip())

	try:
		port = parsed.port
	except ValueError:
		return None

	if not parsed.scheme or not parsed.hostname:
		return None

	scheme = parsed.scheme.lower()

	if port is None or port == DEFAULT_PORTS.get(scheme):
		return f"{scheme}://{parsed.hostname}"

	return f"{scheme}://{parsed.hostname}:{port}"


def refuse_an_unanswered_origin (
	request: starlette.requests.Request, *, allowed: typing.Iterable[str | None], hint: str
) -> None:
	"""Refuse a request from a browser page this instance does not answer.

	**Absent is allowed and present-but-unknown is refused**, and that asymmetry is the whole
	rule: only a browser attaches this header, so only a browser can be the threat, and a
	request without one is a CLI, an agent or ``curl`` — the callers that must keep working
	unchanged. Both users of this have measured that rather than assumed it.

	**What counts as answered is the caller's**, deliberately, and the two differ for a reason
	worth reading before making them one list. ``mcp`` must *not* trust the address the request
	arrived at, because DNS rebinding is precisely a request arriving at a name the attacker
	chose; a session cookie is host-only, so a request carrying one arrived at this instance's
	own name and comparing against it is sound. Merging the sets would quietly hand the MCP
	endpoint back the attack its check exists for.
	"""

	stated = request.headers.get("origin")

	if stated is None:
		return

	known = {origin for origin in allowed if origin is not None}

	if "*" in known or origin_of(stated) in known:
		return

	raise subroutine.errors.Forbidden(
		f"This instance does not answer requests from {stated!r}.", hint=hint
	)


#: The cookie a browser signs in with (`#248`, decision `#364`). Prefixed with the program's
#: name because a person may be running several things on one host during development, and an
#: unprefixed ``session`` is the commonest cookie name there is.
SESSION_COOKIE = "subroutine_session"


def from_session_cookie (
	session: sqlalchemy.orm.Session,
	request: starlette.requests.Request,
	*,
	record_use: bool = True,
) -> subroutine.domain.authentication.Principal | None:
	"""Identify the caller from the browser session cookie.

	**Registered after the bearer resolver, and that order is a decision** (`#364`): an
	explicit ``Authorization`` header beats a cookie the same browser happens to be holding.
	Somebody testing an agent's narrow credential from a signed-in browser gets the narrow
	credential, which is what they asked for — the alternative silently answers as them.

	**A write is refused unless the page making it is one this instance serves** (`#639`).
	``SameSite=lax`` on the cookie was measured to be present and explicit, and it closes the
	unrelated-site case — but ``SameSite`` compares *sites*, not origins, so every sibling
	subdomain of the instance's own domain is same-site and the browser attaches the cookie to
	a form it posts. Seven mutating routes take no request body, so a plain form reaches them
	with no scripting and no preflight; ``POST /v1/users/{username}/signout`` is the sharpest,
	needing only a username and ending every session that person holds.

	**Here rather than on the routes**, which is §7.5's shape: a credential type brings its own
	handling. Only a cookie is attached by a browser without anybody asking, so only a cookie
	can be ridden — a bearer token reaching this instance was put there deliberately, and is
	deliberately not touched. Getting that scoping wrong is what would break every agent.
	"""

	presented = request.cookies.get(SESSION_COOKIE)

	if presented is None:
		return None

	if request.method.upper() not in SAFE_METHODS:
		_refuse_a_write_from_elsewhere(request)

	return subroutine.domain.sessions.authenticate(
		session, presented, record_use=record_use
	)


def _refuse_a_write_from_elsewhere (request: starlette.requests.Request) -> None:
	"""Refuse a cookie-authenticated write from a page this instance does not serve.

	**The address the request arrived at is trustworthy here, and this is the one place that
	is true.** A session cookie carries no ``Domain``, so it is host-only: a browser sends it
	to this instance's own name and nowhere else, which means ``base_url`` *is* this instance
	whenever a cookie is present. That is the opposite of what :func:`subroutine.api.mcp` may
	assume, and the difference is what stops these two being one list.

	**Both it and ``public_url``, rather than one preferred over the other.** ``public_url`` is
	needed because TLS terminates at a proxy and the application sees plain HTTP on a connection
	that reached it over HTTPS — the same reason the ``Secure`` flag is derived from it. But
	preferring it would refuse every write from a browser reaching the instance *directly*, on a
	LAN address or on loopback, which is an ordinary thing to do and works today. The union
	closes nothing: a sibling subdomain's page still posts to this instance's own host, so its
	``Origin`` matches neither.

	**``cors_origins`` is honoured**, because an operator who named an origin has already said
	a browser there may make credentialed requests and read the replies; refusing its writes
	would break a configuration somebody deliberately made. ``*`` is honoured too and gives
	this up entirely — which it already does for reads, since the middleware is built with
	``allow_credentials=True``.
	"""

	settings: subroutine.config.Settings = request.app.state.settings
	answered: set[str | None] = {
		origin_of(one) or one.strip() for one in settings.cors_origins
	}

	answered.add(origin_of(settings.public_url))
	answered.add(origin_of(str(request.base_url)))

	refuse_an_unanswered_origin(
		request,
		allowed=answered,
		hint=(
			"A page in a browser sent this write, from somewhere this instance is not served. "
			"A session cookie is only accepted for a write made by this instance's own pages; "
			"an API token is not restricted this way and is what a script or an agent should "
			"send."
		),
	)


#: Every way of proving identity, tried in order. The first to find a credential of its kind
#: decides — §7.5's claim that a new credential type is "a new resolver, not a change to every
#: endpoint" was checked rather than believed when the second one was built, and it held.
RESOLVERS: tuple[Resolver, ...] = (from_bearer_token, from_session_cookie)


def resolve (
	session: sqlalchemy.orm.Session,
	request: starlette.requests.Request,
	*,
	record_use: bool = True,
) -> subroutine.domain.authentication.Principal:
	"""Identify the caller, or refuse the request.

	The first resolver to find a credential of its kind decides the outcome — accepting it
	or raising. Only when none of them found anything at all is this a request with no
	credential, which is the one case that needs explaining rather than reporting.

	**``record_use=False`` says this request has already been counted** (`#565`). It is passed
	by the one caller that resolves the same credential a second time, in a second session, to
	act as the requester rather than to authenticate them.
	"""

	for resolver in RESOLVERS:
		found = resolver(session, request, record_use=record_use)

		if found is not None:
			return found

	raise subroutine.errors.Unauthenticated(
		"This endpoint needs a credential.", hint=_how_to_authenticate(request)
	)


def principal (
	request: starlette.requests.Request, session: subroutine.api.dependencies.SessionDep
) -> subroutine.domain.authentication.Principal:
	"""Return the principal making this request, refusing it if there is none.

	**Also where §7.7's two limiters run**, and that placement is the reason they need no
	exempt-path list: this dependency is declared on every route that takes a credential and
	on none of the public ones, so a health check a load balancer polls is not counted and a
	limiter cannot be forgotten. The same property `tests/test_api_authentication.py` already
	enforces for authentication carries rate limiting for free.

	The order matters. A *failed* credential is counted against the address it came from,
	before anything else; a *successful* one is counted against its own token. A caller whose
	credential works is therefore never held back by somebody else's failures — which is what
	makes the address key safe behind a proxy, where every request appears to come from one
	place.
	"""

	limits = getattr(request.app.state, "limits", None)

	try:
		found = resolve(session, request)

	except subroutine.errors.Unauthenticated:
		if limits is not None:
			limits.count_a_failure(request)

		raise

	# Asked of the principal rather than of its token, so a browser session is counted like
	# anything else (`#248`). Reading `found.token` here would have left the one credential
	# type a stranger can obtain as the one nothing rate-limits.
	counted = found.credential_prefix

	if limits is not None and counted is not None:
		limits.count_a_request(counted)

	return found


#: Declared as an annotation so an endpoint reads ``actor: PrincipalDep``. Its presence on
#: a route is what the authentication test looks for, so it is the single spelling.
PrincipalDep = typing.Annotated[
	subroutine.domain.authentication.Principal, fastapi.Depends(principal)
]


#: What each kind of credential is, and where it belongs, for the refusal below. A person
#: holding two opaque strings cannot tell them apart, so the program has to say which is
#: which — the same reasoning as the calendar credential's refusal above, and the reason
#: every kind carries a word in its prefix at all.
_ELSEWHERE: dict[str, tuple[str, str]] = {
	subroutine.auth.SESSION_KIND: (
		"a browser session",
		"It is set as a cookie when you sign in, and the browser sends it for you. "
		"Create an API token with 'subroutine token create' to call the API directly.",
	),
	subroutine.auth.LOGIN_KIND: (
		"a sign-in link",
		"Open it in a browser instead: it is spent once, in exchange for a session, and "
		"it is not a credential this API accepts.",
	),
}


def _refuse_a_credential_of_another_kind (credential: str) -> None:
	"""Refuse a credential that is real but does not belong in this header.

	It would fail the token grammar anyway and be reported as mistyped, which sends somebody
	looking for a typo in a string they pasted correctly. Naming the thing they hold is the
	difference between a minute and an afternoon.
	"""

	for kind, (what, hint) in _ELSEWHERE.items():
		if credential.strip().startswith(f"{subroutine.auth.TOKEN_SCHEME}_{kind}_"):
			raise subroutine.errors.Unauthenticated(
				f"That is {what}, which cannot be sent as a bearer token.", hint=hint
			)


def set_session_cookie (
	response: starlette.responses.Response,
	secret: str,
	*,
	settings: subroutine.config.Settings,
	expires_at: datetime.datetime,
) -> None:
	"""Put a browser session into a response, with the attributes that make it safe.

	Three of the four attributes are doing real work:

	* **``HttpOnly``** keeps the value out of ``document.cookie``, so a script injected into
	  a page cannot read it and send it somewhere.
	* **``SameSite=Lax`` is the actual CSRF defence**, and decision `#364` measured why it is
	  needed here and was not before: 14 mutating routes take no request body and **six of
	  them are POSTs** — ``complete``, ``claim``, ``release`` and three ``restore``s — which a
	  cross-site form can send with no preflight, so CORS never sees them. ``Lax`` withholds
	  the cookie from exactly those while still sending it on a top-level navigation, which is
	  what a sign-in link is.
	* **``Secure``** follows ``public_url``, not the request: TLS is terminated at a proxy, so
	  the application sees plain HTTP on a connection that reached it over HTTPS. Deriving it
	  from what the socket says would leave the cookie unmarked on every proxied instance —
	  every real deployment — and marking it unconditionally would make a loopback development
	  instance unable to sign in at all, with nothing to say why.
	"""

	public = (settings.public_url or "").strip()

	response.set_cookie(
		SESSION_COOKIE,
		secret,
		expires=expires_at,
		path="/",
		httponly=True,
		samesite="lax",
		secure=public.lower().startswith("https://"),
	)


def clear_session_cookie (
	response: starlette.responses.Response, *, settings: subroutine.config.Settings
) -> None:
	"""Remove the browser session cookie.

	The attributes have to match the ones it was set with or the browser deletes nothing and
	keeps sending a cookie the instance has already revoked — which looks, from the outside,
	exactly like signing out not working.
	"""

	public = (settings.public_url or "").strip()

	response.delete_cookie(
		SESSION_COOKIE,
		path="/",
		httponly=True,
		samesite="lax",
		secure=public.lower().startswith("https://"),
	)


def _how_to_authenticate (request: starlette.requests.Request) -> str:
	"""Return a hint telling this particular caller what to do differently."""

	misplaced = [name for name in TOKEN_PARAMETERS if name in request.query_params]

	if misplaced:
		return (
			f"A token was sent as the {misplaced[0]!r} query parameter, which is not "
			f"accepted: query strings are written to access logs, browser history and "
			f"referrer headers. Send 'Authorization: {BEARER_SCHEME} sr_…' instead, and "
			f"treat that token as compromised."
		)

	return (
		f"Send 'Authorization: {BEARER_SCHEME} sr_…'. Create a token with "
		f"'subroutine token create'."
	)
