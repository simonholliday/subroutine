"""Signing a browser in and out — item `#248`, decision `#364`.

The asymmetry between the routes is the design:

``GET /signin`` is **public**, because somebody arriving with a link has no credential yet.
That is the whole of what makes it different from every other route in this application, and
the whole of what it costs: §7.7's rate limiting lives inside the principal dependency, so a
route with no principal has none of it unless it asks. This one asks.

``DELETE /v1/session`` needs a credential like everything else, because signing out is
something a signed-in person does.

``POST /v1/session`` is the same coin as that, and it exists because of what being public
costs (`#803`). A public route resolves no principal, so the one state-changing ``GET`` in this
application is the one write `#639`'s origin check can never see — and a browser already signed
in as somebody else would silently become somebody new. So the ``GET`` stops and asks whenever
that is what a link would do, and the answering is a ``POST`` that **requires the standing
session**: the confirmation cannot be submitted from another site, because ``SameSite=lax``
withholds the cookie from a cross-site ``POST``, and it inherits the origin check and the
limiters that the ``GET`` had to do without.

``POST /signin`` is that confirmation for a browser signed in as nobody (`#4393`, R2-L34 of the
cold review of 2026-10-04, as decided with Simon that day). Opening a link used to sign such a
browser straight in, so a link handed to somebody put them in its account, and a chat app's
preview spent a link before the person it was meant for opened it. **No session stands behind
this one**, so the origin check is the whole of its defence, asked by the route itself.

**There is deliberately no route here that mails anything.** `#599` carries that, and it is
where the danger `#364` §3 enumerates actually lives — a public endpoint that sends mail to
an address a stranger chooses, and answers differently depending on whether the account
exists. A link handed over at a terminal needs none of it.
"""

import html
import typing
import urllib.parse

import fastapi
import pydantic
import starlette.requests
import starlette.responses
import starlette.status

import subroutine.api.dependencies
import subroutine.api.routing
import subroutine.api.security
import subroutine.api.web
import subroutine.db.models.identity
import subroutine.domain.authentication
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
#: **The app itself, since `#597` serves one there.** It was `/v1/me` for exactly as long as `/`
#: was a 404 — a sign-in whose every step reports success and whose last step is an error page
#: answers the reader's actual question, *am I in*, with the wrong word.
#: `test_signing_in_lands_somewhere_that_answers` follows the redirect rather than checking that
#: one was sent, which is why moving this needed no change to it.
#:
#: The app is served from this origin, which is what lets the cookie work at all: a second port
#: would be cross-origin, needing `allow_credentials=True` and exactly the CORS exposure `#364`
#: warns about.
LANDING = "/"

#: Where the confirmation page posts to. Named once, because the page names it as a form
#: action and the route declares it, and a page posting at an address nothing answers is a
#: button that reports nothing when pressed.
#:
#: **Under ``/v1`` beside ``DELETE /v1/session``**, because it is the same noun: one replaces
#: this browser's session and the other ends it. ``/signin`` stays the address a *link* opens,
#: which is the thing a person is handed and the thing that has to look like a sign-in.
SWITCH = "/v1/session"

#: Where a sign-in link is opened, and where its page posts the link back when the browser is
#: signed in as nobody (`#4393`). One address for both, so the button sends the link to the
#: place the link itself named.
SIGN_IN = "/signin"

#: What the sign-in link's secret is called, wherever it travels — the query parameter
#: :func:`signin` declares, the field the confirmation form posts, and the name
#: :mod:`subroutine.api.logs` keeps out of the access log (`#806`).
#:
#: **Named because three places have to agree and one of them is a security control.** A
#: redaction list naming a parameter this route stopped using would go on passing while
#: writing credentials down; ``tests/test_api_logs.py`` asserts this is a parameter ``/signin``
#: really declares, rather than trusting the constant to be true.
LINK_PARAMETER = "link"


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

	**This is what makes browser sign-in safe to ship at all.** A
	self-hoster whose mail relay is misconfigured would otherwise be locked out of their own
	instance with no way back in. The console has to be a way in when the ordinary path is
	broken, as it is for every other kind of recovery.
	"""

	for_whom = (
		subroutine.domain.selection.user(session, body.username)
		if body.username
		else actor.user
	)

	link, secret = subroutine.domain.sessions.mint_link(
		session, user=for_whom, actor=actor
	)

	url, assumed = _address(settings, request, secret)

	return subroutine.views.SignInLink(
		url=url,
		username=for_whom.username,
		expires_at=link.expires_at,
		address_assumed=assumed,
	)


@router.get(
	SIGN_IN,
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
	"""Ask before a link signs this browser in, unless it is signed in as that account already.

	**Opening a link signs nobody in by itself.** The page this answers names the account the link
	is for, and its button posts the link back to be spent - so a person handed somebody else's
	link is not put in that account unseen, and a chat app's preview or a mail scanner fetching
	the address leaves it usable. A browser already signed in as the link's account is signed in
	again at once, since there is nothing to choose.

	**303 rather than 200 wherever a link is spent, so it leaves the address bar.** A URL holding a credential
	that stays on screen is one somebody bookmarks, screenshots or pastes into a chat - and
	although this one is already spent by the time the redirect is followed, a person cannot
	tell a spent secret from a live one by looking at it.

	**A secret in a query string is exactly what this application refuses elsewhere**, and
	:data:`subroutine.api.security.TOKEN_PARAMETERS` names the three reasons: access logs,
	browser history and referrer headers. All three were measured here rather than argued about,
	and the answers differ:

	* **Referrer: never.** Every request the browser makes after signing in carries
	  ``Referer: <root>/`` - a redirect keeps the *original* referrer rather than the redirecting
	  URL, and the document that ends up loaded is the landing page. The 303 is what makes that
	  true, so ``test_a_link_is_exchanged_for_a_cookie_and_a_redirect`` is what holds it.
	* **History: no**, closed by the same 303, which is why it is a 303.

	**Both of those are about the path that redeems, and the confirmation page is not it.**
	That page is a 200 carrying the link in its own URL, so it stays in the address bar and in
	the history - a deliberate trade, since it does not spend the link. Its referrer is closed separately, by
	:func:`_ask` sending ``strict-origin``.
	* **Access log: yes**, in full. :mod:`subroutine.api.logs` keeps it out of the one this
	  process writes; an operator's proxy is theirs, and ``docs/hosting.md`` says so.

	**And the confirmation below made a dead secret into a live one.** Before it, the logged
	value was always spent by the time the line was written, because the line is written on
	response. A confirmation deliberately does *not* spend the link - so this route can now log a
	credential that still works, on exactly the path somebody meets when a link arrives that they
	did not expect. That is the reason the redaction exists rather than a note saying it did not
	matter.
	"""

	standing = _who_is_already_here(session, request)
	becoming = subroutine.domain.sessions.would_sign_in(session, link)

	# **A browser signed in as nobody is asked too** (`#4393`, R2-L34 of the cold review of
	# 2026-10-04, as decided with Simon that day). Only one signed in as the link's account is not.
	if becoming is not None and (standing is None or becoming.id != standing.user.id):
		return _ask(None if standing is None else standing.user, becoming, link)

	return _spent(request, session, settings, link)


def _spent (
	request: starlette.requests.Request,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	link: str,
) -> starlette.responses.Response:
	"""Spend a link, hand the browser the session it buys and send it to the app.

	Shared by opening a link as the account already signed in and by confirming one (`#4393`), so
	both count a failure and word a refusal alike: an unknown link and a spent one must read the
	same whichever way they arrive.
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


#: What a browser sends when it submits a form with no script involved.
FORM_ENCODING = "application/x-www-form-urlencoded"


async def _submitted_link (request: starlette.requests.Request) -> str:
	"""Return the link a confirmation form submitted — `#803`.

	**Parsed here rather than declared as ``fastapi.Form()``**, which reads better and costs a
	runtime dependency: FastAPI's form support imports ``python-multipart`` when the route is
	*declared*, whatever encoding actually arrives. This endpoint takes one field in the one
	encoding a plain ``<form>`` sends, and :mod:`urllib.parse` has read that since Python 1.

	**Async so the endpoint can stay synchronous**, exactly as :func:`subroutine.api.mcp._raw_body`
	is and for the same reason: reading a body is asynchronous and everything behind it is
	SQLAlchemy, which is not.

	The refusals name the field, because §13 says a refusal says what to do next — and the only
	caller that can get this wrong is somebody driving the endpoint by hand.
	"""

	kind = request.headers.get("content-type", "").split(";")[0].strip().lower()

	if kind != FORM_ENCODING:
		raise subroutine.errors.ValidationError(
			f"This endpoint is submitted by a form, so it expects {FORM_ENCODING!r}.",
			hint="It is posted by the page a sign-in link shows before it signs a browser in. "
			"Open the link instead of calling this directly.",
		)

	fields = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
	link = fields.get(LINK_PARAMETER, [""])[0]

	if not link:
		raise subroutine.errors.ValidationError(
			"This request carried no sign-in link to act on.",
			errors=[
				subroutine.errors.FieldError(
					field=LINK_PARAMETER,
					code="missing_field",
					message="A sign-in link is what says which session to open.",
				)
			],
		)

	return link


#: The submitted link. See :func:`_submitted_link` for why it is not a declared form field.
SubmittedDep = typing.Annotated[str, fastapi.Depends(_submitted_link)]


def _who_is_already_here (
	session: subroutine.api.dependencies.SessionDep,
	request: starlette.requests.Request,
) -> subroutine.domain.authentication.Principal | None:
	"""Return the browser session this request already carries, or ``None`` — never raising.

	**A cookie that no longer works is the same as no cookie here**, which is why every refusal
	is swallowed. Somebody whose session expired last night and who is opening a fresh link is
	signing in normally, and a page asking them to confirm a switch away from an account they
	are no longer in would be a question about nothing.

	``record_use=False`` because this is not the request's authentication — the route is public
	and stays public. Counting the old session's use while replacing it would also be `#565`'s
	shape, a second write to a row this request is about to leave behind.
	"""

	try:
		return subroutine.api.security.from_session_cookie(
			session, request, record_use=False
		)

	except subroutine.errors.SubroutineError:
		return None


def _ask (
	standing: subroutine.db.models.identity.User | None,
	becoming: subroutine.db.models.identity.User,
	link: str,
) -> starlette.responses.Response:
	"""Ask a reader whether they meant to become somebody — `#803`, and `#4393` when signed out.

	**Signed in as somebody else**, the page names both accounts and posts to :data:`SWITCH`, which
	ends the session it replaces. **Signed in as nobody**, it names the account the link is for and
	posts to :data:`SIGN_IN`, where the origin check is all that stands between a page elsewhere
	and a browser signed into that page's account.

	**Nothing has happened when this is rendered, and that is the whole point.** The link is
	read rather than spent, so *stay as you are* leaves it usable and a person who was sent here
	by somebody else loses nothing at all.

	**The form posts, and the posting is the security control rather than the page.** A page
	that only warned would stop nobody: an attacker who can make a browser follow a link can
	make it follow two. ``SameSite=lax`` withholds the cookie from a cross-site ``POST`` — that
	is measured, not assumed — so the confirmation cannot be submitted from anywhere but here,
	and :func:`switch` needs the standing session to accept it at all.

	**Plain HTML with no script**, because it has to work before the app does, and because the
	one thing it must not depend on is the thing a reader is in the middle of deciding to trust.
	Its only asset is the app's own stylesheet, which is served from this instance.

	``empty`` is the app's own panel — a raised block with a border — reused rather than styled
	afresh, so this page is legible today without adding a rule that `#763` would then have to
	reconcile. ``asking`` names the thing for when it does.

	**Both usernames are escaped, and the link is escaped into an attribute**, although neither
	can currently carry markup: this only renders for a link that *resolved*, so the value is a
	token this instance minted, and a username is constrained where it is created. Escaping is
	what keeps that true if either of those stops being true somewhere else.

	**``strict-origin`` on this page alone** (`#927`'s M-27). The instance sends ``same-origin``
	everywhere, which is right for the app — an item's address is in the URL and the footer
	links off-site — and this is the one page whose *own* URL carries a live credential. Under
	``same-origin`` the stylesheet request above would carry it in a ``Referer``; it goes to
	this instance, so the exposure is to our own access log rather than to a stranger, and
	``api/logs`` redacts that one. Narrowed anyway, because a header that need not carry a
	secret should not.

	**The origin and nothing more, rather than nothing** (`#3908`). ``no-referrer`` made the
	browser send the form below with ``Origin: null``, and the cookie path refuses a write it
	cannot place (`#3833`), so *Continue as …* ended in raw JSON. ``strict-origin`` sends every
	request's ``Referer`` and the form's ``Origin`` as the bare origin, which the check accepts
	and which carries no link.

	**What stays is the address bar and the history entry**, and that is `#803`'s trade rather
	than an oversight: the page deliberately does not spend the link, so *stay as you are*
	leaves it usable. Nothing here can undo a URL somebody has already been sent.
	"""

	now = html.escape(becoming.username)

	if standing is None:
		action, after = SIGN_IN, ""
		said = [
			f"<p>The link you opened signs this browser in as <strong>{now}</strong>.</p>",
			"<p>If that is not you, do not continue: somebody else may have sent you their link, "
			f"and anything you write here would be recorded as <strong>{now}</strong>. Leaving "
			"this page changes nothing and leaves the link unused.</p>",
		]

	else:
		was = html.escape(standing.username)
		action, after = SWITCH, f'\n\t\t<p><a href="{LANDING}">Stay signed in as {was}</a></p>'
		said = [
			f"<p>This browser is signed in as <strong>{was}</strong>. The link you opened signs in "
			f"as <strong>{now}</strong> instead.</p>",
			"<p>If you did not expect this, somebody else may have sent you the link. Staying as "
			f"<strong>{was}</strong> changes nothing and leaves the link unused.</p>",
		]

	told = "\n\t\t".join(said)

	return starlette.responses.HTMLResponse(
		headers={"Referrer-Policy": "strict-origin"},
		content=f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in as {now}?</title>
{subroutine.api.web.ICON_LINKS}
<link rel="stylesheet" href="/app/app.css">
</head>
<body>
<div class="app">
	<div class="empty asking">
		<h1>Sign in as {now}?</h1>
		{told}
		<form method="post" action="{action}">
			<input type="hidden" name="{LINK_PARAMETER}" value="{html.escape(link)}">
			<button type="submit">Continue as {now}</button>
		</form>{after}
	</div>
</div>
</body>
</html>
""",
		status_code=starlette.status.HTTP_200_OK,
	)


#: What a confirmation posted from anywhere but this instance's own page is told (`#4393`).
_A_SIGN_IN_FROM_ELSEWHERE = (
	"A sign-in link is confirmed with the button on the page the link opens, on this instance. "
	"Open the link in this browser and press it there."
)


@router.post(
	SIGN_IN,
	summary="Spend a sign-in link for a browser signed in as nobody, once its page is answered",
	status_code=starlette.status.HTTP_303_SEE_OTHER,
	response_class=starlette.responses.RedirectResponse,
	include_in_schema=False,
)
def confirm (
	request: starlette.requests.Request,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	submitted: SubmittedDep,
) -> starlette.responses.Response:
	"""Spend a link a browser signed in as nobody opened, once the page it showed is answered.

	**Public, like the link it confirms, so the origin check is the whole of its defence.** A
	browser holding no session has nothing for ``SameSite`` to withhold, so without it a page
	anywhere could post a link of its own here and sign a visitor into its account. Only a form
	on this instance's own page passes - the rule a write made with a session cookie keeps,
	asked by this route because no principal is resolved to ask it.

	**The address the request arrived at counts as this instance**, as it does for a cookie,
	because the cookie this answers with is written for that address: a page that reached this
	instance under a name of its own would sign a browser in at that name, and never at this
	instance's own.

	**A browser signed in as somebody else since its page was shown is asked again**, by the page
	naming both accounts, rather than switched by a button that named one.
	"""

	subroutine.api.security.refuse_a_write_from_elsewhere(
		request,
		unnamed="Confirming a sign-in has to say which page sent it, and this one did not.",
		hint=_A_SIGN_IN_FROM_ELSEWHERE,
	)

	standing = _who_is_already_here(session, request)

	if standing is not None:
		becoming = subroutine.domain.sessions.would_sign_in(session, submitted)

		if becoming is not None and becoming.id != standing.user.id:
			return _ask(standing.user, becoming, submitted)

	return _spent(request, session, settings, submitted)


@router.post(
	SWITCH,
	summary="Replace this browser's session with the one a sign-in link buys",
	status_code=starlette.status.HTTP_303_SEE_OTHER,
	response_class=starlette.responses.RedirectResponse,
	include_in_schema=False,
)
def switch (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	submitted: SubmittedDep,
) -> starlette.responses.Response:
	"""Spend a link for a browser that is already signed in as somebody else.

	**This is the half of the confirmation that does the work, and it is where the defence
	lives.** Everything protecting it is inherited rather than invented:

	* ``PrincipalDep`` means the *standing* session has to authenticate, so a request arriving
	  without one is refused before this body runs. ``SameSite=lax`` withholds the cookie from a
	  cross-site ``POST``, so a hostile page cannot supply it.
	* That same dependency runs the origin check, because this is a write authenticated by
	  cookie - the one control the public ``GET`` could never have.
	* And §7.7's limiters, for the same reason.

	**A form encoding rather than JSON, deliberately, and it is the only route here that takes
	one.** It is submitted by a page this application served and by nothing else, and a form is
	what lets that page work with no script at all - which matters on the one screen whose job
	is to let somebody stop.

	**The standing session is revoked rather than abandoned.** Replacing the cookie alone would
	leave a live session belonging to somebody who is no longer at this browser, which is a
	credential nobody is holding - and *sign out first, then sign in* is what a reader would
	have done by hand.
	"""

	if actor.session is None:
		# The same refusal as signing out, for the same reason: a token is not a browser, and
		# this endpoint exists only to swap what a browser is holding.
		raise subroutine.errors.NotFound(
			"This request is not signed in with a browser session, so there is nothing to "
			"replace.",
			hint="Open the sign-in link in a browser instead."
			if actor.token is not None
			else "This caller reached the database directly, which needs no credential.",
		)

	opened, secret = subroutine.domain.sessions.redeem(session, submitted)

	subroutine.domain.sessions.sign_out(actor.session)

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
	property an opaque cookie was chosen for - a self-describing signed credential would
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
) -> tuple[str, bool]:
	"""Build the address somebody opens to sign in, and say whether it was worked out.

	``public_url`` decides it wherever it is set, because behind a TLS-terminating proxy the
	request's own URL is the *internal* one — a link built from it would name a host and a
	scheme that only work from inside the machine, and would look entirely correct in the
	response. Falling back to the request's base URL is the loopback case, where the two are
	the same thing.

	**The second half of the answer is new** (`#1007`). The fallback has always been here and
	has always been silent, so a caller could not tell an address an operator stated from one
	this function inferred — and those fail differently: the first is right by definition,
	the second is right on a laptop and wrong behind a proxy. Saying which is what lets a
	surface printing the link warn before somebody hands it over.

	Local callers reach the same three-way rule through
	:func:`subroutine.config.browsable_url`; the branch differs here only because a request
	is a better source than the bind when there is one.

	**Guessed here and never for a calendar feed, by one rule** (`#3702`, Simon's decision on
	`#1084`): an address may be worked out from the request where it goes straight back to the
	person who asked and is used at once, as a sign-in link is, and flagged so they are told;
	never where it is kept and used later by another program, as a feed's is - see
	:func:`subroutine.domain.calendars.address`. A feature that needs its own address asks which
	of the two it is.
	"""

	told = (settings.public_url or "").strip()
	root = told or str(request.base_url)

	return (
		f"{root.rstrip('/')}{SIGN_IN}"
		f"?{LINK_PARAMETER}={urllib.parse.quote(secret, safe='')}",
		not told,
	)
