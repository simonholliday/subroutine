"""What a browser is told it may do with a page from here — item `#806`'s neighbour, `#805`.

**This exists because of one line in the app**, and the line is worth naming: ``Prose`` is the
only ``dangerouslySetInnerHTML`` in the browser app, and what it renders is text **anybody with
a credential wrote** — including on somebody else's item, since a comment goes through it too.
``markdown.js`` is ours, is audited, and held against all 25 hostile payloads `#677` built. A
policy is what stands between a future defect in it and a reader's session.

**It is unusually cheap here, and that was measured rather than hoped.** The app loads nothing
from another host, uses no inline styles and no ``url()`` in its stylesheet, so ``default-src
'self'`` is satisfied as the app already stands. The single exception is the import map, which
is inline by necessity — nothing rewrites the files on the way to the browser — and is allowed
by **hash**, derived from the served bytes.

**A hash rather than a nonce**, deliberately. A nonce has to be minted per response and written
into the HTML, which would end §2.2's *served as written* promise: `#677` verified on a built
artefact that the served files are byte-identical to the ones in the repository, and a page
rewritten per request cannot be. A hash keeps the bytes static and moves the work here.

**Applied to every response rather than to the HTML ones.** Four things serve HTML now — the
shell, the 404 fallback, the app's own assets and `#803`'s confirmation page — and a helper each
of them had to remember to call would be forgotten by the fifth. ``nosniff`` earns its place on
a JSON response anyway, and the rest cost nothing there.
"""

import base64
import hashlib
import re

import subroutine.api.web

#: The inline script the shell carries, and the only one. Matched on the opening tag rather
#: than on the whole document so that the *text content* is what gets hashed — a CSP hash is
#: over exactly what lies between the tags, whitespace included, and a pattern that swept up
#: the tags would produce a hash no browser ever computes.
_INLINE_SCRIPT = re.compile(
	r"<script type=\"importmap\">(?P<body>.*?)</script>", re.DOTALL
)

#: Headers that are the same for every response and every deployment.
#:
#: **``X-Frame-Options`` is deliberately absent.** ``frame-ancestors`` below supersedes it, and
#: the browsers that need the older header cannot run this app at all — it is served as ES
#: modules with an import map, which is a *newer* baseline than CSP framing control. Shipping
#: both would be two spellings of one rule, which is this codebase's signature defect in its
#: smallest form.
#:
#: ``same-origin`` rather than the browser's own default, because an item's address is in the
#: URL (`#638`) and the app's footer links off-site: a reader opening *Source* should not hand
#: another host the address of the item they were looking at.
STATIC_HEADERS = {
	"X-Content-Type-Options": "nosniff",
	"Referrer-Policy": "same-origin",
}


def inline_script_hashes (shell: bytes) -> list[str]:
	"""Return a CSP source expression for every inline script in the page.

	Derived from the bytes that are served rather than from a value written down beside them.
	A hash somebody pasted would be right until the import map changed, and the symptom of a
	stale one is a **blank page** — the script never runs, so nothing renders and nothing says
	why. `#643` is that failure arriving by a different route, and it reached Simon rather than
	the build.
	"""

	found = _INLINE_SCRIPT.finditer(shell.decode("utf-8"))

	return [
		f"'sha256-{base64.b64encode(hashlib.sha256(one.group('body').encode()).digest()).decode()}'"
		for one in found
	]


def content_security_policy (shell: bytes) -> str:
	"""Return the policy for a page from this instance.

	Each directive is here because something would otherwise fall back to ``default-src`` and be
	*wider* than it needs to be, or because there is no sensible default at all:

	* ``base-uri 'none'`` — nothing here sets a ``<base>``, and an injected one silently
	  re-points every relative URL on the page, including the ones this app fetches from.
	* ``object-src 'none'`` — plugins are a scripting surface with no use here.
	* ``frame-ancestors 'none'`` — depth rather than a fix: measured in Chromium during review
	  `#807`, the app inside a cross-site frame is **signed out**, because ``SameSite=lax``
	  withholds the cookie from a cross-site subresource document. There is nothing to click
	  through to; this makes sure of it if that ever changes.
	* ``form-action 'self'`` — `#803`'s confirmation is a real ``<form>``, so where a form may
	  post is now a question with an answer worth pinning.
	* ``script-src`` names the import map by hash. Everything else the app loads is a module
	  from ``/app/``, which ``'self'`` already covers.
	"""

	scripts = " ".join(["'self'", *inline_script_hashes(shell)])

	return "; ".join((
		"default-src 'self'",
		"base-uri 'none'",
		"object-src 'none'",
		"frame-ancestors 'none'",
		"form-action 'self'",
		f"script-src {scripts}",
	))


def headers (shell: bytes | None = None) -> dict[str, str]:
	"""Return every policy header a response should carry.

	``shell`` is an argument so a test can hand in a page of its own and watch the policy follow
	it — a scanner that cannot be given its subject can only confirm the arrangement it was
	written from (`#405`). Unset means the page this instance actually serves.
	"""

	page = shell if shell is not None else subroutine.api.web.FILES[
		subroutine.api.web.SHELL
	][0]

	return {**STATIC_HEADERS, "Content-Security-Policy": content_security_policy(page)}
