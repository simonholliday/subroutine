"""What a browser is told it may do with a page from here — item `SR#805`.

**The policy's own correctness is checked in a browser, not here.** `tests/test_browser.py`
serves every page under the real headers and fails on a violation, because whether a directive
is too narrow for what the app does is a question only a browser answers — and the symptom of
getting it wrong is a blank page, which reads like nothing at all.

What this file checks is the half a browser cannot see: that the headers reach **every**
response, including the ones no route produced, and that the hash is derived from the page
rather than written down beside it.
"""

import sqlalchemy.orm

import api_support
import subroutine.api.policy
import subroutine.api.security
import subroutine.api.web
import subroutine.domain.users
import subroutine.domain.workspaces

#: A page shaped like the one this instance serves, with an inline script of its own. Used to
#: watch the policy follow its subject rather than assert a constant.
ANOTHER_PAGE = b'<html><script type="importmap">{"imports": {}}</script></html>'


def test_the_import_map_is_allowed_by_a_hash_of_what_is_served () -> None:
	"""**A hash somebody pasted would be right until the import map changed.**

	And the symptom of a stale one is a page that never paints: the map does not load, so no
	module resolves, so nothing renders and nothing says why. Deriving it is what makes editing
	`index.html` safe.
	"""

	served = subroutine.api.policy.content_security_policy(
		subroutine.api.web.FILES[subroutine.api.web.SHELL][0]
	)
	other = subroutine.api.policy.content_security_policy(ANOTHER_PAGE)

	assert "sha256-" in served
	assert "sha256-" in other
	assert served != other, "the policy did not follow the page it was built from"


def test_a_page_with_no_inline_script_is_allowed_nothing_extra () -> None:
	"""The permission exists because the shell needs it, and goes when the need does."""

	policy = subroutine.api.policy.content_security_policy(b"<html></html>")

	assert "script-src 'self'" in policy
	assert "sha256-" not in policy
	assert "unsafe-inline" not in policy, "the escape hatch this exists to avoid"


def test_nothing_may_be_loaded_from_another_host () -> None:
	"""`SR#805`'s reason for existing, in one directive.

	`Prose` is the only `dangerouslySetInnerHTML` in the app and it renders text anybody with a
	credential wrote — including on somebody else's item. This is what stands between a future
	defect in `markdown.js` and a reader's session.
	"""

	policy = subroutine.api.policy.headers()["Content-Security-Policy"]

	for directive in (
		"default-src 'self'",
		"base-uri 'none'",
		"object-src 'none'",
		"frame-ancestors 'none'",
		"form-action 'self'",
	):
		assert directive in policy, f"{directive} is not in {policy}"


def test_the_page_carries_them (session: sqlalchemy.orm.Session) -> None:
	"""The page the policy is *about*, which is the one that would be missed by a helper."""

	application = api_support.build_app(api_support.factory_for(session))
	answer = api_support.call(application, "GET", "/")

	assert answer.status_code == 200
	assert "Content-Security-Policy" in answer.headers
	assert answer.headers["X-Content-Type-Options"] == "nosniff"
	assert answer.headers["Referrer-Policy"] == "same-origin"


def test_a_refusal_carries_them_too (session: sqlalchemy.orm.Session) -> None:
	"""**The response no route produced, which is the one a per-route helper cannot reach.**

	A 401 from the authentication dependency never passes back through the middleware that
	would have stamped it — that is why `apply_headers` is called from the error handlers as
	well, and why this asks a refusal rather than an answer.
	"""

	application = api_support.build_app(api_support.factory_for(session))
	answer = api_support.call(application, "GET", "/v1/tasks")

	assert answer.status_code == 401
	assert "Content-Security-Policy" in answer.headers
	assert answer.headers["X-Content-Type-Options"] == "nosniff"


def test_the_app_s_own_files_carry_them (session: sqlalchemy.orm.Session) -> None:
	"""`nosniff` earns its place here in particular: a module served as the wrong type is a
	module a browser refuses to execute, and one *sniffed* into the wrong type is worse."""

	application = api_support.build_app(api_support.factory_for(session))
	answer = api_support.call(application, "GET", "/app/app.js")

	assert answer.status_code == 200
	assert answer.headers["X-Content-Type-Options"] == "nosniff"


def test_the_confirmation_page_carries_them (session: sqlalchemy.orm.Session) -> None:
	"""**The fourth thing to serve HTML, added three days after the other three** (`SR#803`).

	It is the case that makes this middleware rather than a helper: nothing about writing that
	page would have reminded anybody to stamp it, and it is a page a reader meets while deciding
	whether to trust what they are looking at.
	"""

	import test_api_sessions

	setup = api_support.build_app(api_support.factory_for(session))
	user = subroutine.domain.users.create(
		session, username="policy-reader", display_name="A Reader"
	)
	subroutine.domain.workspaces.create(
		session, slug="policy-ws", title="Work", owner=user
	)
	other = subroutine.domain.users.create(
		session, username="policy-other", display_name="Somebody Else"
	)

	held = test_api_sessions._cookie(setup, test_api_sessions._link(session, user))

	answer = api_support.call(
		setup,
		"GET",
		f"/signin?link={test_api_sessions._link(session, other)}",
		cookies={subroutine.api.security.SESSION_COOKIE: held},
		follow_redirects=False,
	)

	assert answer.status_code == 200, "this is not the confirmation page"
	assert "Content-Security-Policy" in answer.headers
	assert "form-action 'self'" in answer.headers["Content-Security-Policy"], (
		"the page whose whole purpose is a form does not say where a form may post"
	)
