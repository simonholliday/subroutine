"""Signing a browser in and out — item `#248`, decision `#364`.

Two routes, and the asymmetry between them is the design:

``GET /signin`` is **public**, because somebody arriving with a link has no credential yet.
That is the whole of what makes it different from every other route in this application, and
the whole of what it costs: §7.7's rate limiting lives inside the principal dependency, so a
route with no principal has none of it unless it asks. This one asks.

``DELETE /v1/session`` needs a credential like everything else, because signing out is
something a signed-in person does.

**There is deliberately no route here that mails anything.** `#599` carries that, and it is
where the danger `#364` §3 enumerates actually lives — a public endpoint that sends mail to
an address a stranger chooses, and answers differently depending on whether the account
exists. A link handed over at a terminal needs none of it.
"""

import urllib.parse

import fastapi
import pydantic
import starlette.requests
import starlette.responses
import starlette.status

import subroutine.api.dependencies
import subroutine.api.routing
import subroutine.api.security
import subroutine.domain.selection
import subroutine.domain.sessions
import subroutine.errors
import subroutine.views

router = fastapi.APIRouter(tags=["identity"], route_class=subroutine.api.routing.Transactional)

#: Registered after the users router, like the link and comment sub-resources: its path
#: extends `/v1/users/{username}` and `routing.check` is what says the two cannot shadow
#: each other, rather than anybody's reading of it.
user_sessions = fastapi.APIRouter(
	tags=["identity"], route_class=subroutine.api.routing.Transactional
)

#: Where a signed-in browser is sent.
#:
#: **`/v1/me` until `#597` serves a page at `/`, and that is a deliberate choice rather than a
#: placeholder.** Landing on `/` today is a 404 — every step of signing in reports success and
#: the reader's actual question, *am I in*, is answered with an error page. `/v1/me` is plain
#: JSON and answers it: their username, their workspaces, and the credential they are holding.
#: `test_signing_in_lands_somewhere_that_answers` is what will notice when this moves.
#:
#: The web UI is served from this origin when it arrives, which is what lets the cookie work at
#: all: a second port would be cross-origin, needing `allow_credentials=True` and exactly the
#: CORS exposure `#364` warns about.
LANDING = "/v1/me"


class SignInLinkRequest(pydantic.BaseModel):
	"""Who to issue a sign-in link for."""

	model_config = pydantic.ConfigDict(extra="forbid")

	#: Unset means the caller themselves, which needs no permission. Naming somebody else is
	#: administering their access and is gated like issuing a credential for them.
	username: str | None = None


@router.post(
	"/v1/login-links", status_code=201, summary="Issue a sign-in link for somebody"
)
def issue (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	request: starlette.requests.Request,
	body: SignInLinkRequest,
) -> subroutine.views.SignInLink:
	"""Mint a single-use sign-in link and return it, once.

	**This is the route `#248` names as what makes browser sign-in safe to ship at all.** A
	self-hoster whose mail relay is misconfigured would otherwise be locked out of their own
	instance with no way back in, which is §12.4's recovery property applied to a login: the
	console has to be a way in when the ordinary path is broken.
	"""

	for_whom = (
		subroutine.domain.selection.user(session, body.username)
		if body.username
		else actor.user
	)

	link, secret = subroutine.domain.sessions.mint_link(
		session, user=for_whom, actor=actor
	)

	return subroutine.views.SignInLink(
		url=_address(settings, request, secret),
		username=for_whom.username,
		expires_at=link.expires_at,
	)


@router.get(
	"/signin",
	summary="Exchange a sign-in link for a browser session",
	status_code=starlette.status.HTTP_303_SEE_OTHER,
	response_class=starlette.responses.RedirectResponse,
)
def signin (
	request: starlette.requests.Request,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	link: str,
) -> starlette.responses.Response:
	"""Spend a sign-in link, set the browser's session cookie and send it to the app.

	**303 rather than 200, so the link leaves the address bar.** A URL holding a credential
	that stays on screen is one somebody bookmarks, screenshots or pastes into a chat — and
	although this one is already spent by the time the redirect is followed, a person cannot
	tell a spent secret from a live one by looking at it.
	"""

	limits = getattr(request.app.state, "limits", None)

	try:
		opened, secret = subroutine.domain.sessions.redeem(session, link)

	except subroutine.errors.Forbidden:
		if limits is not None:
			limits.count_a_failure(request)

		raise

	except subroutine.errors.Unauthenticated:
		# Counted here rather than by the dependency, because there is no principal on this
		# route for §7.7's limiter to live inside — which is exactly the gap `#364` predicted
		# a login endpoint would inherit, on the one route a stranger can reach.
		if limits is not None:
			limits.count_a_failure(request)

		# **Re-worded, because the reader is different.** The refusal underneath says the
		# token may be mistyped, revoked or expired — correct, uniform across every reason,
		# and addressed to somebody holding an API token. A person who has just clicked a
		# link in a browser has no token, did not type anything, and is told nothing they can
		# act on. Found by clicking one twice rather than by reading the code.
		#
		# **Still one sentence for every reason**, which is the property the original has and
		# the one worth keeping: an unknown link and a spent one must not be distinguishable,
		# or a prefix somebody guessed is confirmed for them.
		raise subroutine.errors.Unauthenticated(
			"This sign-in link does not work.",
			hint="A link is good for half an hour and can be used once, so this one has "
			"probably expired or been opened already. Ask for a new one.",
		) from None

	answer = starlette.responses.RedirectResponse(
		LANDING, status_code=starlette.status.HTTP_303_SEE_OTHER
	)

	subroutine.api.security.set_session_cookie(
		answer, secret, settings=settings, expires_at=opened.expires_at
	)

	return answer


@router.delete(
	"/v1/session",
	summary="Sign this browser out",
	status_code=starlette.status.HTTP_204_NO_CONTENT,
)
def sign_out (
	actor: subroutine.api.security.PrincipalDep,
	settings: subroutine.api.dependencies.SettingsDep,
) -> starlette.responses.Response:
	"""Revoke the browser session this request presented, and clear its cookie.

	**Refused for a caller holding an API token**, rather than quietly doing nothing. A token
	is revoked with ``subroutine token revoke``, and a route that answered "signed out" to
	somebody whose credential still works would be a false statement about the thing they
	most need to be true.
	"""

	if actor.session is None:
		# **A 404 rather than a new error code.** There genuinely is no session on this
		# request, which is what `not_found` says, and error codes are a semver'd contract
		# (§13.4) — minting one so that a wrong-credential case could have its own name would
		# be a public promise made for one refusal.
		raise subroutine.errors.NotFound(
			"This request is not signed in with a browser session, so there is nothing to "
			"sign out of.",
			hint="An API token is stopped with 'subroutine token revoke' instead."
			if actor.token is not None
			else "This caller reached the database directly, which needs no credential.",
		)

	subroutine.domain.sessions.sign_out(actor.session)

	answer = starlette.responses.Response(
		status_code=starlette.status.HTTP_204_NO_CONTENT
	)

	subroutine.api.security.clear_session_cookie(answer, settings=settings)

	return answer


@user_sessions.post(
	"/v1/users/{username}/signout",
	summary="Sign somebody out of every browser they are signed in on",
)
def sign_out_everywhere (
	username: str,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.SignedOut:
	"""Revoke every live session and unspent link belonging to one account.

	This is what a lost laptop needs, and revocation being a row rather than a wait is the
	property `#364` chose an opaque cookie for — a self-describing signed credential would
	have kept working until it expired, whatever anybody did about it.

	**Unspent links go too.** A link is a session that has not happened yet, so stopping the
	sessions and leaving the links would be a control that reads as complete and is not.
	"""

	for_whom = subroutine.domain.selection.user(session, username)
	stopped = subroutine.domain.sessions.sign_out_everywhere(
		session, user=for_whom, actor=actor
	)

	return subroutine.views.SignedOut(username=for_whom.username, sessions_ended=stopped)


def _address (
	settings: subroutine.api.dependencies.SettingsDep,
	request: starlette.requests.Request,
	secret: str,
) -> str:
	"""Build the address somebody opens to sign in.

	``public_url`` decides it wherever it is set, because behind a TLS-terminating proxy the
	request's own URL is the *internal* one — a link built from it would name a host and a
	scheme that only work from inside the machine, and would look entirely correct in the
	response. Falling back to the request's base URL is the loopback case, where the two are
	the same thing.
	"""

	root = (settings.public_url or "").strip() or str(request.base_url)

	return f"{root.rstrip('/')}/signin?link={urllib.parse.quote(secret, safe='')}"
