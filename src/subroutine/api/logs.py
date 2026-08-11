"""Keeping credentials out of the access log — item `#806`.

**A request line is written down by the server, not by the application**, which is what makes
this a logging filter rather than middleware: by the time uvicorn logs a request the response
has already gone, and nothing an ASGI app can do reaches that line.

Two kinds of secret arrive in a query string here, and both were measured rather than assumed:

* **A sign-in link.** ``GET /signin?link=sr_lnk_…`` is how a person trades a link for a session
  (`#248`), and it has to be a ``GET`` because it is opened by clicking. It was tolerable while
  the secret was always *spent* by the time the line was written — and `#803` ended that, because
  a browser already signed in as somebody else is now shown a confirmation and the link is
  deliberately left usable. A live credential, for up to half an hour, on exactly the path
  somebody meets when a link arrives that they did not expect.
* **An API token somebody put in the wrong place.**
  :data:`subroutine.api.security.TOKEN_PARAMETERS` exists because callers do this; the request
  is refused *and the refusal tells them to treat that token as compromised*. Writing it into
  our own log afterwards is the same mistake one layer down, and those tokens are long-lived.

**This is half a fix and is labelled as one.** It reaches the log this process writes. An
operator's proxy logs the same request line, on its own retention, and we cannot touch it —
``docs/hosting.md`` carries that half, as advice rather than as code.

**The whole fix is to stop putting the secret in the query string**, which is a redesign of how
a link travels rather than a change to how one is written down.
"""

import copy
import logging
import urllib.parse

import subroutine.api.security
import subroutine.api.sessions

#: What replaces a secret in a logged request line. Obviously deliberate, so that a reader
#: takes it for a decision rather than for a broken client — and **made only of characters a
#: query string leaves alone**, which was measured rather than chosen: the first version was
#: ``<redacted>`` and the line came out reading ``link=%3Credacted%3E``, because the path is
#: rebuilt through :func:`urllib.parse.urlencode` and angle brackets do not survive it.
REDACTED = "REDACTED"

#: Where uvicorn's access logger sends its records. Named because installing on the root logger
#: would filter everything this process ever logs, for the sake of one line.
ACCESS_LOGGER = "uvicorn.access"

#: Which position of that logger's ``args`` holds the path. **Uvicorn's own formatter unpacks
#: five in this order** — client address, method, full path, HTTP version, status — and both
#: its HTTP implementations emit that shape. It is not a documented interface, which is why
#: ``tests/test_api_logs.py`` drives a record through uvicorn's *own* formatter: an upgrade that
#: changes the shape fails the build rather than quietly writing secrets again.
PATH_POSITION = 2


def secret_parameters () -> frozenset[str]:
	"""Every query parameter whose value is a credential.

	**Derived rather than listed**, so a parameter added to either source is covered here
	without anybody remembering: the token names come from the set that already exists to refuse
	them, and the link name from the route that declares it.
	"""

	return frozenset(subroutine.api.security.TOKEN_PARAMETERS) | {
		subroutine.api.sessions.LINK_PARAMETER
	}


def redacted (path: str) -> str:
	"""Return a request path with any credential in its query replaced.

	**The path is rebuilt from its parsed parts rather than patched by a substitution**, so a
	secret containing something that looks like a separator cannot leave a fragment of itself
	behind. Everything not a secret is preserved, because an access log with the parameters
	removed is an access log nobody can debug with.

	A path with no query is returned unchanged and untouched — this runs on every request, and
	the overwhelming majority carry nothing to hide.
	"""

	split = path.find("?")

	if split < 0:
		return path

	secrets = secret_parameters()
	query = urllib.parse.parse_qsl(path[split + 1:], keep_blank_values=True)

	if not any(name in secrets for name, _value in query):
		return path

	kept = [
		(name, REDACTED if name in secrets else value) for name, value in query
	]

	return f"{path[:split]}?{urllib.parse.urlencode(kept)}"


class Redacting (logging.Filter):
	"""Rewrite an access record's path before anything formats it.

	**A copy of the arguments, not the record's own tuple.** A ``logging.Filter`` is handed the
	record every handler will format, and mutating its ``args`` in place would be editing an
	object the caller still owns — which is fine today and is the sort of thing that stops being
	fine when a second handler appears.

	**An unexpected shape is passed through rather than dropped**, and that direction is a
	decision: a log line is a mitigation here and not a boundary, so losing the whole access log
	to a uvicorn upgrade would be the worse failure. What stops that from being silent is the
	test, which drives a record through uvicorn's own formatter.
	"""

	def filter (self, record: logging.LogRecord) -> bool:
		"""Redact the request line, and always keep the record."""

		args = record.args

		if not isinstance(args, tuple) or len(args) <= PATH_POSITION:
			return True

		path = args[PATH_POSITION]

		if not isinstance(path, str):
			return True

		changed = redacted(path)

		if changed != path:
			replaced = list(copy.copy(args))
			replaced[PATH_POSITION] = changed
			record.args = tuple(replaced)

		return True


def redact_access_logs (logger: logging.Logger | None = None) -> None:
	"""Install the filter on the server's access log, once.

	**Installed where the server is started**, because that is the only place that can: the
	filter belongs to a logger in this process, and an operator running the app under their own
	uvicorn or gunicorn gets it only by calling this. ``docs/hosting.md`` says so rather than
	leaving them to find out.

	``logger`` is an argument so a test can hand in its own and watch this take effect — a
	scanner that cannot be given its subject can only confirm the arrangement it was written
	from (`#405`).

	Adding it twice would redact an already-redacted path, which is harmless, and would still be
	an accumulating list of identical filters — so it checks.
	"""

	target = logger if logger is not None else logging.getLogger(ACCESS_LOGGER)

	if any(isinstance(one, Redacting) for one in target.filters):
		return

	target.addFilter(Redacting())

