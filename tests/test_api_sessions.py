"""Signing in and out over HTTP — item `#248`, decision `#364`.

Driven end to end rather than asserted about, because most of what makes browser sign-in
safe is *where* things happen rather than what they compute: which resolver wins when two
credentials arrive, whether a route with no principal is rate-limited, and whether a cookie
carries the attributes that make it a CSRF defence rather than a liability.
"""

import typing
import uuid

import fastapi
import pytest
import sqlalchemy.orm

import api_support
import subroutine.api.limits
import subroutine.api.security
import subroutine.api.sessions
import subroutine.config
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.sessions
import subroutine.domain.users
import subroutine.domain.workspaces


class Setup(typing.NamedTuple):
	"""Somebody who could sign in, and an application to do it through."""

	application: fastapi.FastAPI
	user: subroutine.db.models.identity.User


@pytest.fixture
def setup (session: sqlalchemy.orm.Session) -> Setup:
	"""Create a person with a workspace, and an app bound to the test's transaction."""

	user = subroutine.domain.users.create(
		session, username=f"caller-{uuid.uuid4().hex[:8]}", display_name="The Caller"
	)
	subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Work", owner=user
	)

	return Setup(
		application=api_support.build_app(api_support.factory_for(session)), user=user
	)


def _link (
	session: sqlalchemy.orm.Session, user: subroutine.db.models.identity.User
) -> str:
	"""Mint a sign-in link's secret directly, as `subroutine login link` would."""

	_row, secret = subroutine.domain.sessions.mint_link(session, user=user)

	return secret


def _cookie (application: fastapi.FastAPI, secret: str) -> str:
	"""Sign in and return the session cookie the browser was handed."""

	answer = api_support.call(
		application, "GET", f"/signin?link={secret}", follow_redirects=False
	)

	assert answer.status_code == 303, answer.text

	return answer.cookies[subroutine.api.security.SESSION_COOKIE]


def test_a_link_is_exchanged_for_a_cookie_and_a_redirect (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""303 rather than 200, so the credential leaves the address bar.

	A URL holding a secret that stays on screen is one somebody bookmarks or screenshots, and
	a person cannot tell a spent secret from a live one by looking at it.
	"""

	answer = api_support.call(
		setup.application,
		"GET",
		f"/signin?link={_link(session, setup.user)}",
		follow_redirects=False,
	)

	assert answer.status_code == 303
	assert answer.headers["location"] == subroutine.api.sessions.LANDING
	assert subroutine.api.security.SESSION_COOKIE in answer.cookies


def test_the_cookie_authenticates_the_next_request (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""§7.5's claim that a credential type is "a new resolver, not a change to every
	endpoint" was checked rather than believed. This is the check."""

	held = _cookie(setup.application, _link(session, setup.user))

	answer = api_support.call(
		setup.application,
		"GET",
		"/v1/me",
		cookies={subroutine.api.security.SESSION_COOKIE: held},
	)

	assert answer.status_code == 200
	assert answer.json()["user"]["username"] == setup.user.username
	assert answer.json()["credential"]["kind"] == "web_session"


def test_the_cookie_is_httponly_and_samesite_lax (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""``SameSite`` is the actual CSRF defence here, and this is what asserts it exists.

	Decision `#364` measured why it is needed now and was not before: 14 mutating routes take
	no request body and **six of them are POSTs**, which a cross-site form can send with no
	preflight — so CORS never sees them and the cookie's own attribute is the whole defence.

	``Secure`` is absent because this instance has no ``public_url``; marking it on a loopback
	development instance would make signing in impossible with nothing to say why.
	"""

	answer = api_support.call(
		setup.application,
		"GET",
		f"/signin?link={_link(session, setup.user)}",
		follow_redirects=False,
	)

	written = answer.headers["set-cookie"].lower()

	assert "httponly" in written
	assert "samesite=lax" in written
	assert "secure" not in written


def test_the_cookie_is_marked_secure_where_the_instance_is_served_over_https (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Followed from ``public_url``, not from the request.

	TLS is terminated at a proxy, so the application sees plain HTTP on a connection that
	reached it over HTTPS — deriving this from the socket would leave the cookie unmarked on
	every proxied instance, which is every real deployment.
	"""

	user = subroutine.domain.users.create(
		session, username=f"caller-{uuid.uuid4().hex[:8]}", display_name="The Caller"
	)
	subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Work", owner=user
	)
	application = api_support.build_app(
		api_support.factory_for(session), public_url="https://work.example.com"
	)

	answer = api_support.call(
		application, "GET", f"/signin?link={_link(session, user)}", follow_redirects=False
	)

	assert "secure" in answer.headers["set-cookie"].lower()


def test_a_bearer_token_beats_a_cookie_in_the_same_browser (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""Resolver order is a decision, not an accident of the tuple's literal order (`#364`).

	Somebody testing an agent's narrow credential from a signed-in browser gets the narrow
	credential, which is what they asked for. The alternative silently answers as them, and
	every refusal they were checking for stops happening.
	"""

	held = _cookie(setup.application, _link(session, setup.user))

	other = subroutine.domain.users.create(
		session, username=f"agent-{uuid.uuid4().hex[:8]}", display_name="An Agent"
	)
	_row, secret = subroutine.domain.authentication.issue_token(
		session, user=other, title="A token"
	)

	answer = api_support.call(
		setup.application,
		"GET",
		"/v1/me",
		headers={"Authorization": f"Bearer {secret.value.get_secret_value()}"},
		cookies={subroutine.api.security.SESSION_COOKIE: held},
	)

	assert answer.status_code == 200
	assert answer.json()["user"]["username"] == other.username


def test_a_session_sent_as_a_bearer_token_is_refused_by_name (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""It fails the token grammar anyway, and would be reported as mistyped.

	That sends somebody looking for a typo in a string they pasted correctly. Naming what
	they are holding is the difference between a minute and an afternoon — the same treatment
	§7.4 already gives a calendar feed's credential.
	"""

	held = _cookie(setup.application, _link(session, setup.user))

	answer = api_support.call(
		setup.application,
		"GET",
		"/v1/me",
		headers={"Authorization": f"Bearer {held}"},
	)

	assert answer.status_code == 401
	assert "browser session" in answer.json()["detail"]


def test_a_sign_in_link_sent_as_a_bearer_token_is_refused_by_name (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""The other half of the same rule, on the credential somebody is likelier to paste."""

	answer = api_support.call(
		setup.application,
		"GET",
		"/v1/me",
		headers={"Authorization": f"Bearer {_link(session, setup.user)}"},
	)

	assert answer.status_code == 401
	assert "sign-in link" in answer.json()["detail"]


def test_signing_in_is_rate_limited_although_it_has_no_principal (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**The gap `#364` predicted a login endpoint would inherit, closed and proved.**

	§7.7's limiter lives inside the principal dependency, which is what lets it need no
	exempt-path list — and means a route with no principal has no limiter at all unless it
	asks. This is the one route a stranger can reach, so it asks.

	Driven rather than asserted about: the claim is that the counting *happens* on this route,
	which is exactly the shape of failure that reading the code cannot see.
	"""

	user = subroutine.domain.users.create(
		session, username=f"caller-{uuid.uuid4().hex[:8]}", display_name="The Caller"
	)
	subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Work", owner=user
	)

	application = api_support.build_app(api_support.factory_for(session))

	# Turned on as an operator's configuration would, the same way `test_api_limits` does it:
	# the default follows the bind, and a test application binds nowhere.
	#
	# **The *failures* allowance, which is a separate setting from the requests one.** A bad
	# link is a failed authentication, so `rate_limit_per_minute` is not what bounds it — a
	# version of this test that set the other one passed six bad links in a row and reported
	# that nothing was counting.
	application.state.limits = subroutine.api.limits.Limits(
		subroutine.config.Settings(
			dev_mode=True, rate_limit=True, rate_limit_failures_per_minute=3
		),
		host="0.0.0.0",
	)

	seen = [
		api_support.call(
			application, "GET", "/signin?link=sr_lnk_abcdef01_nope", follow_redirects=False
		).status_code
		for _ in range(6)
	]

	assert 401 in seen, seen
	assert 429 in seen, f"nothing counted the failures on a route with no principal: {seen}"


def test_a_good_link_still_works_beside_a_limiter (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""The floor under the test above: it must fail because of *failures*, not because
	the route refuses everything."""

	answer = api_support.call(
		setup.application,
		"GET",
		f"/signin?link={_link(session, setup.user)}",
		follow_redirects=False,
	)

	assert answer.status_code == 303


def test_signing_out_revokes_the_session_and_clears_the_cookie (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""Revocation is immediate, and the cookie is deleted with the attributes it was set
	with — otherwise the browser keeps sending one the instance has already refused."""

	held = _cookie(setup.application, _link(session, setup.user))
	sent = {subroutine.api.security.SESSION_COOKIE: held}

	answer = api_support.call(setup.application, "DELETE", "/v1/session", cookies=sent)

	assert answer.status_code == 204
	assert subroutine.api.security.SESSION_COOKIE in answer.headers["set-cookie"]

	after = api_support.call(setup.application, "GET", "/v1/me", cookies=sent)

	assert after.status_code == 401


def test_signing_out_with_an_api_token_is_refused_rather_than_pretended (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""Answering "signed out" to somebody whose credential still works would be a false
	statement about the thing they most need to be true."""

	_row, secret = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="A token"
	)

	answer = api_support.call(
		setup.application,
		"DELETE",
		"/v1/session",
		headers={"Authorization": f"Bearer {secret.value.get_secret_value()}"},
	)

	assert answer.status_code == 404
	assert "token revoke" in answer.json()["hint"]


def test_a_link_names_the_public_address_rather_than_the_internal_one (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Behind a proxy the request's own URL is the internal one.

	A link built from it would name a host that only works from inside the machine, and would
	look entirely correct in the response — which is the failure somebody debugs for an hour.
	"""

	user = subroutine.domain.users.create(
		session, username=f"caller-{uuid.uuid4().hex[:8]}", display_name="The Caller"
	)
	subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Work", owner=user
	)
	application = api_support.build_app(
		api_support.factory_for(session), public_url="https://work.example.com/"
	)
	_row, secret = subroutine.domain.authentication.issue_token(
		session, user=user, title="A token"
	)

	answer = api_support.call(
		application,
		"POST",
		"/v1/login-links",
		json={},
		headers={"Authorization": f"Bearer {secret.value.get_secret_value()}"},
	)

	assert answer.status_code == 201
	assert answer.json()["url"].startswith("https://work.example.com/signin?link=sr_lnk_")
	assert answer.json()["username"] == user.username


def test_issuing_a_link_for_somebody_else_needs_permission_to_administer_accounts (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""A link signs in *as* whoever it names, so handing one out is handing out their access.

	Gated exactly as issuing them a token is — the same authority, so refusing here and
	allowing there would be a boundary that exists on one surface only, which is `#487`.
	"""

	somebody = subroutine.domain.users.create(
		session, username=f"other-{uuid.uuid4().hex[:8]}", display_name="Somebody"
	)
	_row, secret = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="A token"
	)

	answer = api_support.call(
		setup.application,
		"POST",
		"/v1/login-links",
		json={"username": somebody.username},
		headers={"Authorization": f"Bearer {secret.value.get_secret_value()}"},
	)

	assert answer.status_code == 403

	setup.user.is_superuser = True
	session.flush()

	allowed = api_support.call(
		setup.application,
		"POST",
		"/v1/login-links",
		json={"username": somebody.username},
		headers={"Authorization": f"Bearer {secret.value.get_secret_value()}"},
	)

	assert allowed.status_code == 201
	assert allowed.json()["username"] == somebody.username


def test_signing_in_lands_somewhere_that_answers (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""**Following the redirect, not merely checking that one was sent.**

	Every step of signing in can report success while the reader's actual question — *am I
	in* — is answered by an error page. That is the shape this project keeps meeting: an
	install reports on itself rather than on the outcome, so the ordinary failure is a
	confident sequence ending in a 404.

	This follows the whole flow, which is also what will notice when `#597` moves `LANDING`
	to a page it serves: the assertion is about the destination working, not about its path.
	"""

	answer = api_support.call(
		setup.application,
		"GET",
		f"/signin?link={_link(session, setup.user)}",
		follow_redirects=True,
	)

	assert answer.status_code == 200, f"{subroutine.api.sessions.LANDING}: {answer.text}"


@pytest.mark.parametrize(
	("what", "presented"),
	[
		("never existed", "sr_lnk_abcdef01_nope"),
		("is not a link at all", "not-a-credential"),
	],
)
def test_a_link_that_does_not_work_says_so_in_the_reader_s_terms (
	session: sqlalchemy.orm.Session, setup: Setup, what: str, presented: str
) -> None:
	"""A person who clicked a link has no token and did not type anything.

	The refusal underneath is correct, uniform and addressed to somebody holding an API
	token — *"the token may be mistyped, revoked, or expired"*. Handing that to a browser
	tells the reader nothing they can act on, and names two things they are not holding.

	**Found by clicking a link twice against a real server**, not by reading the code, which
	is where every message defect in this project has come from.
	"""

	answer = api_support.call(
		setup.application, "GET", f"/signin?link={presented}", follow_redirects=False
	)

	assert answer.status_code == 401, what
	assert answer.json()["detail"] == "This sign-in link does not work."
	assert "used once" in answer.json()["hint"]
	assert "token" not in answer.json()["detail"].lower()


def test_a_spent_link_is_not_distinguishable_from_one_that_never_existed (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""The property the original refusal had, and the one worth keeping through a rewording.

	Distinguishing them would confirm a guessed prefix for whoever guessed it. One sentence
	for every reason is what makes that impossible, and rewording for a friendlier audience
	is exactly the change that quietly loses it.
	"""

	secret = _link(session, setup.user)

	api_support.call(
		setup.application, "GET", f"/signin?link={secret}", follow_redirects=False
	)

	spent = api_support.call(
		setup.application, "GET", f"/signin?link={secret}", follow_redirects=False
	)
	unknown = api_support.call(
		setup.application,
		"GET",
		"/signin?link=sr_lnk_abcdef01_nope",
		follow_redirects=False,
	)

	assert spent.status_code == unknown.status_code
	assert spent.json()["detail"] == unknown.json()["detail"]
	assert spent.json()["hint"] == unknown.json()["hint"]
