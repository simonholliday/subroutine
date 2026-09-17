"""What a browser is told it may do with a page from here — item `SR#805`.

**The policy's own correctness is checked in a browser, not here.** `tests/test_browser.py`
serves every page under the real headers and fails on a violation, because whether a directive
is too narrow for what the app does is a question only a browser answers — and the symptom of
getting it wrong is a blank page, which reads like nothing at all.

What this file checks is the half a browser cannot see: that the headers reach **every**
response, including the ones no route produced, and that the hash is derived from the page
rather than written down beside it.
"""

import pytest
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


def test_a_changed_file_reaches_a_browser_that_holds_the_old_one (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#914`. **Every file the app is made of, not the stylesheet Simon asked about.**

	It was `public, max-age=300` with no validator, which is the worst of both: for five minutes
	a browser did not ask, so a restarted server served current bytes to a page that would not
	request them — and after five minutes it re-downloaded the whole file, because with nothing
	to compare there was no `304` available.

	**Driven per file rather than on one of them.** The stylesheet is what a reader notices, and
	a stale `app.js` is a blank page — the failure `#643` reached Simon by another route — so a
	rule that covered the CSS alone would have left the worse half in place.
	"""

	application = api_support.build_app(api_support.factory_for(session))

	for name in ("index.html", "app.css", "app.js", "preact.js"):
		path = "/" if name == subroutine.api.web.SHELL else f"/app/{name}"

		fresh = api_support.call(application, "GET", path)

		assert fresh.status_code == 200, f"{path} did not answer"
		assert fresh.headers["cache-control"] == "no-cache", (
			f"{path} may be used without asking, so a change to it cannot arrive"
		)

		tag = fresh.headers.get("etag")

		assert tag, f"{path} carries no validator, so a revalidation must re-send the whole file"

		again = api_support.call(application, "GET", path, headers={"if-none-match": tag})

		assert again.status_code == 304, f"{path} re-sent a file the caller already held"
		assert not again.content, f"{path} sent a body with its 304"

		# **A cache may weaken a tag it stored**, and a comparison against the raw header would
		# then answer *no* for a browser holding exactly the right file — wasteful, correct, and
		# indistinguishable from the header working.
		weak = api_support.call(application, "GET", path, headers={"if-none-match": f"W/{tag}"})

		assert weak.status_code == 304, f"{path} ignored a weakened form of its own tag"

		stale = api_support.call(
			application, "GET", path, headers={"if-none-match": '"something-else"'}
		)

		assert stale.status_code == 200, f"{path} withheld itself from a caller holding an old copy"


def test_a_browser_that_takes_gzip_is_sent_gzip (session: sqlalchemy.orm.Session) -> None:
	"""`SR#2509`: 940 KB of files went out raw where 380 KB would do.

	**Measured against the served instance before this was written**: no `Content-Encoding` came
	back even when the request offered `gzip, br`, and the proxy in front compresses nothing
	either. On a local network that is invisible — a cold page is half a second — and from
	anywhere else it is the difference between that and several seconds.

	**Driven per file, like `SR#914`'s revalidation above.** The app is eighteen modules and a
	stylesheet, so a rule proved on `app.js` alone would leave most of the bytes where they were.
	"""

	application = api_support.build_app(api_support.factory_for(session))

	for name in ("index.html", "app.css", "app.js", "preact.js"):
		path = "/" if name == subroutine.api.web.SHELL else f"/app/{name}"

		packed = api_support.call(application, "GET", path, headers={"accept-encoding": "gzip"})
		plain = api_support.call(application, "GET", path, headers={"accept-encoding": "identity"})

		assert packed.headers.get("content-encoding") == "gzip", f"{path} was sent raw"

		assert plain.headers.get("content-encoding") is None, (
			f"{path} was compressed for a caller that asked for it unencoded"
		)

		# The client decodes what it is told the encoding of, so this compares the *file*; the
		# line below it is what compares the wire.
		assert packed.content == plain.content, f"{path} does not decode to the file itself"

		assert int(packed.headers["content-length"]) < len(plain.content), f"{path} grew"

		assert "accept-encoding" in packed.headers.get("vary", "").lower(), (
			f"{path} has two representations and does not say so, so a shared cache may hand "
			f"the compressed copy to a caller that cannot read it"
		)


def test_a_refusal_of_gzip_is_a_refusal_on_the_app_s_own_files (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#2630`: the route honoured ``gzip;q=0`` and the middleware compressed the file anyway.

	Two layers read one header two ways - the route parsed the quality, the middleware searched
	for the word - so a caller who refused gzip was sent it, under the file's own tag. **And
	``*`` stands for the codings a header does not name** (RFC 9110 §12.5.3), so ``gzip;q=0, *``
	refuses gzip too, where the route's parser had read the ``*`` after the refusal as yes.
	"""

	application = api_support.build_app(api_support.factory_for(session))

	for name in (subroutine.api.web.SHELL, "app.js"):
		path = "/" if name == subroutine.api.web.SHELL else f"/app/{name}"

		for refusing in ("gzip;q=0", "gzip;q=0, *", "*;q=0", "br, gzip; q=0.0"):
			answer = api_support.call(
				application, "GET", path, headers={"accept-encoding": refusing}
			)

			assert answer.headers.get("content-encoding") is None, (
				f"{path} was sent gzipped to a caller whose header said {refusing!r}"
			)
			assert answer.headers["etag"] == subroutine.api.web.TAGS[name], (
				f"{path} was not sent as the file itself for {refusing!r}"
			)

		accepting = api_support.call(application, "GET", path, headers={"accept-encoding": "*"})

		assert accepting.headers.get("content-encoding") == "gzip", (
			f"{path} was sent raw to a caller that takes any encoding"
		)

	# **And the page a deep link is answered with**, which is served outside ``/app/`` - so the
	# compressor does see it, and must reach the decision the page's own reading reaches.
	deep = "/projects/some/deep/address"

	for refusing in ("gzip;q=0", "gzip;q=0, *"):
		page = api_support.call(
			application, "GET", deep, headers={"accept": "text/html", "accept-encoding": refusing}
		)

		assert page.status_code == 200 and "<html" in page.text.lower(), "no page for a deep link"
		assert page.headers.get("content-encoding") is None, (
			f"a deep link's page was sent gzipped to a caller whose header said {refusing!r}"
		)

	taken = api_support.call(
		application, "GET", deep, headers={"accept": "text/html", "accept-encoding": "gzip"}
	)

	assert taken.headers.get("content-encoding") == "gzip", "a deep link's page was sent raw"


def test_a_file_held_only_unencoded_is_sent_unencoded_under_its_own_tag (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#2630`: an icon went out gzipped by the middleware, carrying the tag of its raw bytes.

	`api/web` keeps no compressed copy of a picture, so the route sends the file with the file's
	tag - and the middleware then compressed it, so one strong tag named two sets of bytes and a
	cache revalidating either was told it held the other. The middleware stays off the app's own
	paths now, and each route negotiates for itself.
	"""

	application = api_support.build_app(api_support.factory_for(session))

	# **Picked rather than named** (`SR#2864`). This listed three files, and the day the app's
	# mark changed two of them fell under the threshold - a three-bar glyph compresses to under
	# a kilobyte - so the test failed on its own premise rather than on the rule. What it needs
	# is any picture the middleware would want to compress.
	pictures = [
		name
		for name, (body, kind) in sorted(subroutine.api.web.FILES.items())
		if kind.startswith("image/")
		and len(body) >= subroutine.api.web.SMALLEST_WORTH_COMPRESSING
	]

	assert len(pictures) >= 3, f"too few pictures to say anything: {pictures}"

	for name in pictures:
		body = subroutine.api.web.FILES[name][0]
		answer = api_support.call(
			application, "GET", f"/app/{name}", headers={"accept-encoding": "gzip"}
		)

		assert answer.headers.get("content-encoding") is None, f"{name} was compressed on the way out"
		assert answer.headers["etag"] == subroutine.api.web.TAGS[name]
		assert answer.content == body


def test_every_route_serving_the_app_s_files_negotiates_for_itself () -> None:
	"""The paths the compressor leaves alone are the paths `api/web` serves, and no others.

	**Read off the router**, so a route added to `api/web` outside ``/app/`` is caught here rather
	than compressed a second time, and the API is checked to be outside the rule.
	"""

	for route in subroutine.api.web.router.routes:
		path = getattr(route, "path", "")

		assert subroutine.api.web.negotiates_for_itself(path), (
			f"{path} is served by api/web and the compressor would not leave it alone"
		)

	for path in ("/v1/openapi.json", "/healthz", "/signin", "/application"):
		assert not subroutine.api.web.negotiates_for_itself(path), (
			f"{path} would be left uncompressed as though it were one of the app's files"
		)


@pytest.mark.parametrize(
	("header", "takes"),
	[
		("gzip", True),
		("GZip;Q=0.5", True),
		("gzip; q=0", False),
		("gzip;q=0.0", False),
		("*", True),
		("*;q=0", False),
		("br, deflate", False),
		("", False),
		("gzip;q=0, *", False),
		("*, gzip;q=0", False),
		("*;q=0, gzip", True),
		("gzip;q=high", True),
	],
)
def test_what_an_accept_encoding_header_says_about_gzip (header: str, takes: bool) -> None:
	"""The parser `SR#2630` made the one reading for every answer, over the shapes a header takes.

	A coding named outright outranks ``*`` in either order, as RFC 9110 §12.5.3 has it. **And a
	quality that is not a number reads as acceptance** (`SR#2636`, a branch nothing ran): wrong
	that way, a browser still decodes what it is sent.
	"""

	assert subroutine.api.web.takes_gzip(header) is takes


def test_a_picture_is_not_held_in_two_copies (session: sqlalchemy.orm.Session) -> None:
	"""An already-compressed file is not worth *keeping* twice (`SR#2509`).

	**This asked the response until `SR#2535`, and that was the wrong level.** The rule is about
	what `api/web` holds: a second copy of every file, for the life of the process, made at
	import. A response is now also seen by a compressor that decides per request, so asking the
	wire made this a test of two mechanisms and a statement about neither.

	**And the reason it gave was wrong, which the move exposed.** It said gzipping a picture
	makes it larger; measured, these icons go to 91 and 98 per cent — flat colour compresses even
	after PNG has had it. So compressing one on the way out is a small win, and holding one for
	ever to save 2 per cent of a file nobody re-fetches is not.

	What decides is still the content type rather than a list of suffixes, since a second list
	would have to agree with `TYPES` for ever.
	"""

	for name in ("favicon-on-black.ico", "icon-512-on-black.png", "apple-touch-icon.png"):
		assert name in subroutine.api.web.FILES, f"{name} is not served at all any more"

		assert name not in subroutine.api.web.COMPRESSED, (
			f"{name} is already compressed and a copy of it is being held at import anyway"
		)


def test_the_two_encodings_do_not_share_a_tag (session: sqlalchemy.orm.Session) -> None:
	"""`SR#914`'s revalidation is per representation, and `SR#2509` gave every file a second one.

	A caller holding the compressed copy revalidates with its tag; one holding the file itself
	revalidates with the other. A server that answered `304` to both would be telling one of
	them that what it holds is current when it is the other encoding — and a shared cache is
	where that becomes somebody's blank page, which is what `Vary` and two tags keep apart.
	"""

	application = api_support.build_app(api_support.factory_for(session))

	packed = api_support.call(
		application, "GET", "/app/app.js", headers={"accept-encoding": "gzip"}
	)
	plain = api_support.call(
		application, "GET", "/app/app.js", headers={"accept-encoding": "identity"}
	)

	assert packed.headers["etag"] != plain.headers["etag"], (
		"the compressed copy answers with the file's own tag, so a cache holding one of them "
		"believes it holds the other"
	)

	held = api_support.call(application, "GET", "/app/app.js", headers={
		"accept-encoding": "gzip", "if-none-match": packed.headers["etag"],
	})

	assert held.status_code == 304, "a browser holding the current compressed copy re-fetched it"

	crossed = api_support.call(application, "GET", "/app/app.js", headers={
		"accept-encoding": "gzip", "if-none-match": plain.headers["etag"],
	})

	assert crossed.status_code == 200, (
		"a caller holding the raw file was told its compressed copy was what it already had"
	)


def test_a_compressed_copy_carries_no_clock () -> None:
	"""The tag is the bytes, so the bytes may not carry the time (`SR#2509`).

	`gzip.compress` stamps the current time into its header unless told not to. With it, an
	unchanged file would answer with a different tag after every restart — every browser
	re-downloading the whole app because the server had been bounced, and two workers
	disagreeing about what they are holding. The fourth to eighth bytes of a gzip member are
	that field, and reading them is what makes this a measurement rather than a second copy of
	the call being checked.
	"""

	for name, packed in subroutine.api.web.COMPRESSED.items():
		stamp = int.from_bytes(packed[4:8], "little")

		assert stamp == 0, (
			f"{name}'s compressed copy carries the clock, so its tag moves on every restart"
		)


def test_two_files_that_differ_do_not_share_a_tag (session: sqlalchemy.orm.Session) -> None:
	"""The tag is the content, which is what makes it need no maintenance (`#914`).

	**A constant would pass every check above.** One value returned for every file revalidates
	correctly, answers `304` on a match and `200` otherwise — and would serve a stale stylesheet
	for ever, because it never changes when the file does. That is the failure this replaced,
	wearing the new mechanism, so it is asserted rather than assumed.
	"""

	tags = subroutine.api.web.TAGS

	assert len(tags) >= 5, f"only {sorted(tags)} — the app is not made of that few files"

	assert len(set(tags.values())) == len(tags), (
		f"two of the app's files share a tag, so a change to one cannot reach a browser "
		f"holding the other: {tags}"
	)

	# The tag is derived, so proving it *is* the content needs the content changed rather than
	# a second file compared: same bytes, same tag; one byte different, different tag.
	body = subroutine.api.web.FILES["app.css"][0]

	assert subroutine.api.web._tag(body) == tags["app.css"]
	assert subroutine.api.web._tag(body + b"\n") != tags["app.css"]
