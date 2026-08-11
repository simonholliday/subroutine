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


# ---- a write is refused unless the page making it is ours (`SR#639`) -----------------------


#: Where this instance is served.
INSTANCE = "https://subroutine.example.test"

#: A different host on the same registrable domain. **`SameSite` compares sites, not origins**,
#: so a page here is same-site and the browser attaches the session cookie to a form it posts.
#: That is the whole of `SR#639`, and it is not hypothetical: the real instance's domain runs
#: other services on a box that also runs the proxy.
SIBLING = "https://photos.example.test"


def _served (session: sqlalchemy.orm.Session, user: subroutine.db.models.identity.User) -> tuple[
	fastapi.FastAPI, str
]:
	"""An instance that knows where it is served, and a live session cookie for it.

	The session is minted directly rather than through ``/signin``, because ``public_url`` being
	an ``https://`` address is what makes the cookie ``Secure`` — and a client talking to
	``http://testserver`` would then be right to refuse to store it. What is under test is the
	write, not the handout.
	"""

	_row, secret = subroutine.domain.sessions.redeem(session, _link(session, user))

	return (
		api_support.build_app(api_support.factory_for(session), public_url=INSTANCE),
		secret,
	)


def test_a_sibling_subdomain_cannot_act_as_a_signed_in_browser (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""**`SR#639`: the case `SameSite=lax` does not cover, because a sibling is same-site.**

	``POST /v1/users/{username}/signout`` is the sharpest of the seven routes that take no
	request body — a plain HTML form reaches it with no scripting and no preflight, it needs no
	knowledge of any ref, and its effect is to lock somebody out of the browser they are sitting
	in front of.
	"""

	application, held = _served(session, setup.user)

	answer = api_support.call(
		application,
		"POST",
		f"/v1/users/{setup.user.username}/signout",
		cookies={subroutine.api.security.SESSION_COOKIE: held},
		headers={"origin": SIBLING},
	)

	assert answer.status_code == 403, answer.text
	assert SIBLING in answer.json()["detail"], "the refusal did not say what it refused"

	# **And the session is untouched**, which is the half worth checking: a refusal that signed
	# the reader out anyway would be the attack succeeding through the defence.
	after = api_support.call(
		application, "GET", "/v1/me", cookies={subroutine.api.security.SESSION_COOKIE: held}
	)

	assert after.status_code == 200, "the refused write ended the session it was refusing"


def test_a_page_this_instance_serves_may_write (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""The boundary has to be a boundary rather than a wall — this is the app's own page."""

	application, held = _served(session, setup.user)

	answer = api_support.call(
		application,
		"POST",
		f"/v1/users/{setup.user.username}/signout",
		cookies={subroutine.api.security.SESSION_COOKIE: held},
		headers={"origin": INSTANCE},
	)

	assert answer.status_code == 200, answer.text


def test_a_caller_that_states_no_origin_may_write (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""**Absent means allow**, and getting that backwards is what would break every caller.

	Only a browser attaches this header without being asked. A request without one is `curl`, a
	script or an agent — measured on the MCP surface before this rule was first written, and the
	same reasoning applies to the same header here.
	"""

	application, held = _served(session, setup.user)

	answer = api_support.call(
		application,
		"POST",
		f"/v1/users/{setup.user.username}/signout",
		cookies={subroutine.api.security.SESSION_COOKIE: held},
	)

	assert answer.status_code == 200, answer.text


def test_reading_is_not_restricted_by_where_the_page_was (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""No state change is reachable by GET — measured for decision `#364`, all 38 mutating
	routes are POST, PATCH or DELETE — so a read from anywhere changes nothing.

	Refusing them as well would break an ordinary embed for no gain, and a browser cannot read
	the answer cross-origin anyway without CORS permitting it.
	"""

	application, held = _served(session, setup.user)

	answer = api_support.call(
		application,
		"GET",
		"/v1/me",
		cookies={subroutine.api.security.SESSION_COOKIE: held},
		headers={"origin": SIBLING},
	)

	assert answer.status_code == 200, answer.text


def test_a_token_is_not_restricted_by_where_the_page_was (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""**The one that breaks every agent if the scoping is got wrong.**

	A bearer token is not attached by a browser without being asked, so it cannot be ridden —
	whoever sent it meant to. The check therefore lives inside the *cookie* resolver rather than
	on the routes, which is §7.5's shape: a credential type brings its own handling.
	"""

	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="An agent"
	)
	session.flush()

	application, _held = _served(session, setup.user)

	answer = api_support.call(
		application,
		"POST",
		f"/v1/users/{setup.user.username}/signout",
		headers={
			"authorization": f"Bearer {issued.value.get_secret_value()}",
			"origin": SIBLING,
		},
	)

	assert answer.status_code == 200, answer.text


@pytest.mark.parametrize(
	("written", "expected"),
	[
		("https://subroutine.example.test", "https://subroutine.example.test"),
		# A trailing slash, which `public_url` is ordinarily written with.
		("https://subroutine.example.test/", "https://subroutine.example.test"),
		# Capitals, which a browser never sends and an operator may well type.
		("HTTPS://Subroutine.Example.Test", "https://subroutine.example.test"),
		# The default port, which a browser leaves out.
		("https://subroutine.example.test:443", "https://subroutine.example.test"),
		("http://localhost:8151", "http://localhost:8151"),
		# A path, because `public_url` is a URL rather than an origin.
		("https://subroutine.example.test/app/", "https://subroutine.example.test"),
		("", None),
		(None, None),
		("not a url", None),
		("https://subroutine.example.test:notaport", None),
	],
)
def test_an_origin_is_compared_as_an_origin (written: str | None, expected: str | None) -> None:
	"""**An origin is not a prefix of a URL**, and comparing the configured text would fail
	closed — a capital letter or a `:443` somebody typed would stop the browser app working on
	the instance whose operator was most careful about writing their address out."""

	assert subroutine.api.security.origin_of(written) == expected


def test_reaching_the_instance_directly_still_writes (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""**An instance is reachable at more than one address, and both are its own.**

	`public_url` names where a proxy serves it; opening it on a LAN address or on loopback,
	past the proxy, is an ordinary thing to do and works today. Comparing against `public_url`
	alone would have refused every write from such a browser — a change that fails *closed*, so
	the symptom is buttons that stop working with a message about origins, on the deployment
	whose operator did configure their address correctly.

	This is the address the request arrived at, which for a cookie is a fact rather than a
	claim: the cookie is host-only, so a browser only sends it to this instance's own name.
	"""

	application, held = _served(session, setup.user)

	answer = api_support.call(
		application,
		"POST",
		f"/v1/users/{setup.user.username}/signout",
		cookies={subroutine.api.security.SESSION_COOKIE: held},
		headers={"origin": api_support.BASE_URL},
	)

	assert answer.status_code == 200, answer.text


# ---- a browser is never silently made somebody else (`SR#803`) --------------


def _second_person (
	session: sqlalchemy.orm.Session,
) -> subroutine.db.models.identity.User:
	"""Somebody else with an account, which is all this attack needs the attacker to have."""

	return subroutine.domain.users.create(
		session, username=f"other-{uuid.uuid4().hex[:8]}", display_name="Somebody Else"
	)


def _unspent (session: sqlalchemy.orm.Session) -> int:
	"""How many sign-in links are still usable, which is what *asking* must not change."""

	model = subroutine.db.models.identity.LoginLink

	return len(
		session.scalars(
			sqlalchemy.select(model).where(model.redeemed_at.is_(None))
		).all()
	)


def test_a_link_for_somebody_else_asks_instead_of_switching (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""**`SR#803`, and it was measured in a real browser before it was written here.**

	`GET /signin` is public, so it resolves no principal, so `SR#639`'s origin check — which
	lives inside the cookie resolver — never sees it. That makes the one state-changing `GET` in
	this application the one write no guard covers, and Chromium confirmed the consequence: a
	*click* on a cross-site link replaced the session, and the reader carried on typing into
	somebody else's account.
	"""

	held = _cookie(setup.application, _link(session, setup.user))
	other = _second_person(session)
	theirs = _link(session, other)

	before = _unspent(session)

	answer = api_support.call(
		setup.application,
		"GET",
		f"/signin?link={theirs}",
		cookies={subroutine.api.security.SESSION_COOKIE: held},
		follow_redirects=False,
	)

	assert answer.status_code == 200, "it switched instead of asking"
	assert "text/html" in answer.headers["content-type"]
	assert setup.user.username in answer.text, "the page did not say who is signed in"
	assert other.username in answer.text, "the page did not say who the link is for"

	# **Nothing happened**, which is the whole property. A reader who says no must not be left
	# holding a link that was spent by being asked about — and must still be themselves.
	assert _unspent(session) == before, "asking spent the link"

	still = api_support.call(
		setup.application,
		"GET",
		"/v1/me",
		cookies={subroutine.api.security.SESSION_COOKIE: held},
	)

	assert still.json()["user"]["username"] == setup.user.username


def test_signing_in_again_as_yourself_does_not_ask (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""The ordinary path is untouched, and *this* is the test that says so.

	A confirmation shown to somebody opening their own second link would be a question with one
	answer, on the screen a person meets when their session has lapsed.
	"""

	held = _cookie(setup.application, _link(session, setup.user))

	answer = api_support.call(
		setup.application,
		"GET",
		f"/signin?link={_link(session, setup.user)}",
		cookies={subroutine.api.security.SESSION_COOKIE: held},
		follow_redirects=False,
	)

	assert answer.status_code == 303, answer.text


def test_a_dead_cookie_is_the_same_as_no_cookie (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""Somebody whose session lapsed overnight is signing in, not switching.

	Asking them to confirm a move away from an account they are no longer in would be a question
	about nothing — so every refusal the standing cookie can raise is swallowed.
	"""

	held = _cookie(setup.application, _link(session, setup.user))
	principal = subroutine.domain.sessions.authenticate(session, held)

	assert principal.session is not None
	subroutine.domain.sessions.sign_out(principal.session)

	other = _second_person(session)

	answer = api_support.call(
		setup.application,
		"GET",
		f"/signin?link={_link(session, other)}",
		cookies={subroutine.api.security.SESSION_COOKIE: held},
		follow_redirects=False,
	)

	assert answer.status_code == 303, answer.text


def test_a_link_that_does_not_work_is_refused_rather_than_offered (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""**The reading half can never be the reason a message differs.**

	`would_sign_in` answers `None` for every link that would not work — unknown, expired, spent,
	suspended — and the caller falls through to `redeem`, which raises the one refusal all of
	those share. A confirmation page rendered for a bad link would say *this link signs you in
	as somebody* about a link that does not.
	"""

	held = _cookie(setup.application, _link(session, setup.user))
	other = _second_person(session)
	theirs = _link(session, other)

	subroutine.domain.sessions.redeem(session, theirs)

	answer = api_support.call(
		setup.application,
		"GET",
		f"/signin?link={theirs}",
		cookies={subroutine.api.security.SESSION_COOKIE: held},
		follow_redirects=False,
	)

	assert answer.status_code == 401, answer.text
	assert "does not work" in answer.json()["detail"]


def test_confirming_switches_and_ends_the_session_it_replaced (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""Saying yes does what the page said it would, and tidies up after itself.

	Replacing the cookie alone would leave a live session belonging to somebody who is no longer
	at this browser — a credential nobody is holding, and not what *sign out, then sign in* would
	have left behind.
	"""

	held = _cookie(setup.application, _link(session, setup.user))
	other = _second_person(session)

	answer = api_support.call(
		setup.application,
		"POST",
		subroutine.api.sessions.SWITCH,
		content=f"link={_link(session, other)}",
		headers={"content-type": subroutine.api.sessions.FORM_ENCODING},
		cookies={subroutine.api.security.SESSION_COOKIE: held},
		follow_redirects=False,
	)

	assert answer.status_code == 303, answer.text
	assert answer.headers["location"] == subroutine.api.sessions.LANDING

	became = answer.cookies[subroutine.api.security.SESSION_COOKIE]

	who = api_support.call(
		setup.application,
		"GET",
		"/v1/me",
		cookies={subroutine.api.security.SESSION_COOKIE: became},
	)

	assert who.json()["user"]["username"] == other.username

	gone = api_support.call(
		setup.application,
		"GET",
		"/v1/me",
		cookies={subroutine.api.security.SESSION_COOKIE: held},
	)

	assert gone.status_code == 401, "the session it replaced is still working"


def test_confirming_needs_the_session_it_is_replacing (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""**This is the control, and the page is only its wrapper.**

	A page that merely warned would stop nobody: whoever can make a browser follow one link can
	make it follow two. What stops the attack is that answering requires the *standing* cookie —
	and `SameSite=lax` withholds a cookie from a cross-site `POST`, so a hostile page cannot
	supply one.
	"""

	other = _second_person(session)

	answer = api_support.call(
		setup.application,
		"POST",
		subroutine.api.sessions.SWITCH,
		content=f"link={_link(session, other)}",
		headers={"content-type": subroutine.api.sessions.FORM_ENCODING},
		follow_redirects=False,
	)

	assert answer.status_code == 401, answer.text


def test_confirming_from_a_sibling_subdomain_is_refused (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""**The origin check the public `GET` could never have, inherited rather than invented.**

	`SameSite` compares sites, so a sibling subdomain's page is same-site and its `POST` would
	carry the cookie. Requiring the standing session is what puts this route behind
	`PrincipalDep`, and `SR#639`'s check lives inside the resolver that dependency runs.
	"""

	application, held = _served(session, setup.user)
	other = _second_person(session)

	answer = api_support.call(
		application,
		"POST",
		subroutine.api.sessions.SWITCH,
		content=f"link={_link(session, other)}",
		headers={
			"content-type": subroutine.api.sessions.FORM_ENCODING,
			"origin": SIBLING,
		},
		cookies={subroutine.api.security.SESSION_COOKIE: held},
		follow_redirects=False,
	)

	assert answer.status_code == 403, answer.text
	assert SIBLING in answer.json()["detail"]


def test_confirming_with_an_api_token_is_refused_rather_than_pretended (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""A token is not a browser, and there is nothing for this endpoint to replace.

	The same refusal as signing out with one, in the same words, because it is the same fact.
	"""

	other = _second_person(session)
	_row, token = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="An agent"
	)

	answer = api_support.call(
		setup.application,
		"POST",
		subroutine.api.sessions.SWITCH,
		content=f"link={_link(session, other)}",
		headers={
			"content-type": subroutine.api.sessions.FORM_ENCODING,
			"authorization": f"Bearer {token.value.get_secret_value()}",
		},
		follow_redirects=False,
	)

	assert answer.status_code == 404, answer.text
	assert "browser session" in answer.json()["detail"]


def test_reading_a_link_does_not_spend_it (
	session: sqlalchemy.orm.Session, setup: Setup
) -> None:
	"""The reading half, on its own, because the page depends on it entirely.

	Falsified by making `would_sign_in` call `redeem`: this fails, and so does the page test
	above — which is the point of asking it twice, once at each level.
	"""

	secret = _link(session, setup.user)

	assert subroutine.domain.sessions.would_sign_in(session, secret) is setup.user
	assert subroutine.domain.sessions.would_sign_in(session, secret) is setup.user

	opened, _held = subroutine.domain.sessions.redeem(session, secret)

	assert opened.user_id == setup.user.id
	assert subroutine.domain.sessions.would_sign_in(session, secret) is None
