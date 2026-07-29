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

import typing

import fastapi
import sqlalchemy.orm
import starlette.requests

import subroutine.api.dependencies
import subroutine.auth
import subroutine.domain.authentication
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
Resolver = typing.Callable[
	[sqlalchemy.orm.Session, starlette.requests.Request],
	subroutine.domain.authentication.Principal | None,
]


def from_bearer_token (
	session: sqlalchemy.orm.Session, request: starlette.requests.Request
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

	return subroutine.domain.authentication.authenticate(session, credential)


#: Every way of proving identity, tried in order. One today; §7.5 adds the next.
RESOLVERS: tuple[Resolver, ...] = (from_bearer_token,)


def resolve (
	session: sqlalchemy.orm.Session, request: starlette.requests.Request
) -> subroutine.domain.authentication.Principal:
	"""Identify the caller, or refuse the request.

	The first resolver to find a credential of its kind decides the outcome — accepting it
	or raising. Only when none of them found anything at all is this a request with no
	credential, which is the one case that needs explaining rather than reporting.
	"""

	for resolver in RESOLVERS:
		found = resolver(session, request)

		if found is not None:
			return found

	raise subroutine.errors.Unauthenticated(
		"This endpoint needs a credential.", hint=_how_to_authenticate(request)
	)


def principal (
	request: starlette.requests.Request, session: subroutine.api.dependencies.SessionDep
) -> subroutine.domain.authentication.Principal:
	"""Return the principal making this request, refusing it if there is none."""

	return resolve(session, request)


#: Declared as an annotation so an endpoint reads ``actor: PrincipalDep``. Its presence on
#: a route is what the authentication test looks for, so it is the single spelling.
PrincipalDep = typing.Annotated[
	subroutine.domain.authentication.Principal, fastapi.Depends(principal)
]


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
