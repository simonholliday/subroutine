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

#: Query parameters a caller might reasonably, and wrongly, put a token in. Tokens are
#: never accepted from a query string — they end up in access logs, browser history and
#: referrer headers (SPEC.md §7.4) — but silently ignoring one leaves the caller staring
#: at a 401 with a credential they can plainly see in the URL, so the refusal says so.
_TOKEN_PARAMETERS = ("token", "api_key", "apikey", "access_token", "auth")

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
	"""

	presented = request.cookies.get(SESSION_COOKIE)

	if presented is None:
		return None

	return subroutine.domain.sessions.authenticate(
		session, presented, record_use=record_use
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

	misplaced = [name for name in _TOKEN_PARAMETERS if name in request.query_params]

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
