"""Forwarding a stdio MCP session to whichever instance answers for a connection — `#539`.

Decision `#538`: once an instance serves MCP itself, there are two implementations of what a
tool call *does* — the one in the caller's installed package and the one on the server — and
which answers depends on which package happens to be installed on the calling machine. That is
this project's signature defect at the largest scale it has appeared: `#345`, `#379`, `#380`,
`#393` and the whole plugin-versus-program split are all instances of it.

So ``subroutine mcp`` reads a message, has it answered *there*, and writes the answer back. It
holds no catalogue, builds no server and knows nothing about tools.

Two ways to reach "there", and they differ only in how the request travels:

* **A remote connection** posts to the instance's ``/mcp``, presenting the token §12.3a already
  resolves for that connection.
* **A local connection** drives this application's own ASGI app in process, exactly as
  ``call_api`` does (:mod:`subroutine.api.inprocess`), with :func:`subroutine.domain.local.principal`
  standing in for reading a header. There is no nested event loop to worry about: a stdio server
  has no running loop of its own, which is the difference from doing this inside a request.

**The message is forwarded without being parsed.** A malformed one has to be refused by the far
end, or this adapter becomes the second implementation it exists to remove — the shape `#530`
is about, one layer up.
"""

import json
import re
import shutil
import typing

import httpx
import sqlalchemy.orm

import subroutine
import subroutine.api.inprocess
import subroutine.config
import subroutine.connections
import subroutine.credentials
import subroutine.domain.authentication
import subroutine.errors
import subroutine.mcp.protocol

#: The endpoint an instance serves MCP on. Named once because both halves below reach it, and
#: because a served instance and an in-process one must not be able to disagree about the path.
PATH = "/mcp"

#: The clause the far end writes about which instance is being worked in, and which this side
#: has to correct: the server knows what it calls itself, and the caller knows the private alias
#: they call it (`#330`). Anchored on the words either side of the name so that a change to the
#: sentence fails the test that drives this rather than silently leaving the wrong label.
_THE_LABEL = re.compile(r"(on connection ')([^']*)(')")


def answering (
	connection: subroutine.connections.Connection,
	roster: subroutine.connections.Roster,
	settings: subroutine.config.Settings,
	*,
	workspace: str | None = None,
) -> typing.Callable[[str], dict[str, typing.Any] | None]:
	"""Return something that answers one raw JSON-RPC message from the chosen instance."""

	forward = (
		_in_process(connection, roster, settings, workspace=workspace)
		if connection.is_local
		else _over_http(connection, roster, workspace=workspace)
	)
	elsewhere = tuple(name for name in roster.names if name != connection.name)

	def answer (raw: str) -> dict[str, typing.Any] | None:
		"""Forward one message and return what came back, in this machine's terms."""

		try:
			status, text = forward(raw)

		except subroutine.errors.SubroutineError as failure:
			return _refused(raw, failure.detail, failure.hint)

		if status == 404:
			# **The failure this change creates, so it gets a sentence rather than a shrug.**
			# An instance from before `#516` has no such route and answers with a perfectly
			# clear problem document about a path — which reads, to somebody who has just
			# installed a plugin, as "MCP is broken" rather than as "that server is older than
			# this program". `#250` is the general form and is not this.
			return _refused(
				raw,
				f"{connection.name} does not serve MCP.",
				"That instance is older than this program. Upgrade it to a version that "
				f"serves {PATH}, or point this at one that does.",
			)

		if not text.strip():
			# A notification: answered with 202 and nothing to say, which the transport reports
			# by writing nothing at all.
			return None

		try:
			answered = json.loads(text)

		except json.JSONDecodeError:
			return _refused(
				raw,
				f"{connection.name} answered {status}, and not with a JSON-RPC message.",
				"Check what is serving that address — a proxy or a captive portal answers "
				"like this.",
			)

		# **Parsing is not the question; being a JSON-RPC message is** (`#697`). The check above
		# tested whether the body was *readable*, and a problem document is perfectly readable
		# JSON — so every refusal this API makes was written to the protocol channel verbatim,
		# with no envelope and no id, including for `initialize`. A client cannot match that to
		# anything it sent, so the session never starts and what it reports is not a refusal but
		# a stream of objects that mean nothing.
		#
		# Measured on a machine with no instance yet: three problem documents on stdout and 564
		# lines of traceback on stderr, ending "unable to open database file".
		if not isinstance(answered, dict) or "jsonrpc" not in answered:
			trouble = answered if isinstance(answered, dict) else {}

			# **The commonest cause has a name and a one-command remedy**, and it is asked only
			# here, on a path that has already failed. Checking before the request instead was
			# tried and was wrong: `settings` describes where a database *would* be, and the
			# application being driven need not be built from it — four adapter tests inject
			# their own and were refused outright by a machine that was working perfectly.
			#
			# The same sentence `clients/local.py` gives a person, and the same predicate: a
			# missing SQLite file is a fact, where an unreachable PostgreSQL might be absent,
			# asleep or firewalled, and guessing produces confident bad advice.
			if connection.is_local and settings.has_no_instance_yet():
				return _refused(
					raw,
					"No Subroutine instance has been set up on this machine yet.",
					_how_to_make_one(),
				)

			# **Its own words when it has any.** A problem document already carries a `detail`
			# written for a person and often a `hint` naming the remedy, and those are worth far
			# more than a sentence composed here about a status code.
			return _refused(
				raw,
				trouble.get("detail")
				or f"{connection.name} answered {status}, and not with a JSON-RPC message.",
				trouble.get("hint"),
			)

		return _in_this_machines_terms(answered, connection.label, elsewhere)

	return answer


def _over_http (
	connection: subroutine.connections.Connection,
	roster: subroutine.connections.Roster,
	*,
	workspace: str | None,
) -> typing.Callable[[str], tuple[int, str]]:
	"""Return a forwarder that posts to a served instance."""

	resolved = subroutine.credentials.resolve(connection, default_connection=roster.default)

	if resolved.token is None:
		raise subroutine.errors.Unauthenticated(
			f"Connection {connection.name!r} has no token, so there is no way to identify "
			"this session to it.",
			hint=f"Put one in {subroutine.credentials.credentials_file_path()} under "
			f"[{connection.name}], or export "
			f"{subroutine.credentials.variable_for(connection.name)}.",
		)

	# **One client for the session, not one per message.** A tool call is a request inside a
	# request, and opening a connection for each would add a handshake to every one of them.
	client = httpx.Client(
		base_url=typing.cast(str, connection.url),
		timeout=connection.timeout_seconds,
		headers={
			"Authorization": f"Bearer {resolved.token}",
			"Content-Type": "application/json",
			"Accept": "application/json",
			"User-Agent": f"subroutine/{subroutine.API_VERSION}",
		},
	)

	def forward (raw: str) -> tuple[int, str]:
		"""Post one message and return the status and body."""

		try:
			answered = client.post(
				PATH, params=_asking_for(workspace), content=raw.encode("utf-8")
			)

		except httpx.HTTPError as failure:
			# Every message goes through here, so a server that goes away mid-session must
			# produce an answer rather than a traceback: the client is blocked on this one.
			raise subroutine.errors.ServiceUnavailable(
				f"{connection.name} could not be reached at {connection.url}: {failure}",
				hint="Check that the instance is running and that you are on a network that "
				"can reach it.",
			) from None

		return answered.status_code, answered.text

	return forward


def _in_process (
	connection: subroutine.connections.Connection,
	roster: subroutine.connections.Roster,
	settings: subroutine.config.Settings,
	*,
	workspace: str | None,
) -> typing.Callable[[str], tuple[int, str]]:
	"""Return a forwarder that drives this installation's own application.

	**The same route the served instance answers, in the same code.** A standalone SQLite
	install runs no server, and refusing there would be missing exactly the machine an agent
	meets first — so the application is driven in process rather than a second implementation
	being kept for it.

	**And the same credential, which is `#927`'s H-9.** This resolved none: it called
	``principal`` with a username and nothing else, so ``SUBROUTINE_TOKEN``,
	``SUBROUTINE_TOKEN_<NAME>`` and ``credentials.toml`` were all ignored on a local
	connection — while ``_over_http`` twelve lines up resolved one properly. The same
	``--scope task:read`` service account therefore answered ``claudebot (agent) … Narrowed to
	scopes task:read`` at the terminal and ``si (person) … instance:admin`` here, and a write
	the CLI refuses succeeded. ``plugin.json`` sells that field as *"if you want it to have
	less access than you do"*.
	"""

	# A late import, using the house style's documented exception, exactly as `serve` and
	# `clients/local.py` do: `api` pulls in FastAPI, and a stdio session is a long-lived child
	# process where that costs nothing.
	from subroutine.api import app as api
	from subroutine.domain import local as principals

	application = api.create_app(settings=settings)

	# **Resolved once, outside the closure.** A credential can come from a `token_command` —
	# `pass show`, `gpg` — and asking per message would run it on every tool call and could
	# prompt for a passphrase in the middle of one.
	held = subroutine.credentials.resolve(connection, default_connection=roster.default)

	def resolve (
		session: sqlalchemy.orm.Session,
	) -> subroutine.domain.authentication.Principal:
		"""Answer §12.1a: on this machine the filesystem permission is the authentication.

		The same resolution every other local path takes, so ``SUBROUTINE_TOKEN_<NAME>`` and a
		stored credential narrow a stdio session exactly as they narrow a command — which this
		sentence claimed before anything did it (`#927` H-9).

		**No credential is not an error here, unlike over HTTP.** A standalone install has
		none and is not supposed to: §12.1a says reaching the file is the authentication, so
		the fallthrough to ``local_user`` is the ordinary path and the token is the narrowing
		somebody asks for on top of it.
		"""

		return principals.principal(
			session,
			token=held.token,
			token_source=held.source if held.token else None,
			local_user=settings.local_user,
		)

	def forward (raw: str) -> tuple[int, str]:
		"""Run one message through the application and return the status and body."""

		answered = subroutine.api.inprocess.call(
			application,
			resolve,
			method="POST",
			path=PATH,
			query=_asking_for(workspace),
			content=raw.encode("utf-8"),
		)

		return answered.status_code, answered.text

	return forward


def _asking_for (workspace: str | None) -> dict[str, str] | None:
	"""Return the query the endpoint takes, or nothing when there is none.

	``--workspace`` travels as the query parameter the plugin already uses, so there is one
	spelling of it rather than two (`#539`).
	"""

	return None if workspace is None else {"workspace": workspace}


def _in_this_machines_terms (
	answered: dict[str, typing.Any],
	label: str,
	elsewhere: typing.Sequence[str],
) -> dict[str, typing.Any]:
	"""Correct the parts of an answer that are about *this* machine rather than the instance.

	Only the instructions, and only two clauses of them:

	* **Which instance this is.** The server says what it calls itself; the reader typed a name
	  of their own, and that alias is theirs and private (`#330`).
	* **Which instances it is not reaching** (`#276`). The far end cannot write this at all — it
	  is the caller's roster and the server has never heard of it. Losing it would restore the
	  defect `#276` was filed for: an agent confident it knew where it was, because the sentence
	  it was given was true and suggested nothing else existed.

	**Appended rather than injected**, for the second one. A substitution that finds nothing
	leaves the sentence out and says nothing about it; appending either happens or does not.
	"""

	instructions = answered.get("result", {}).get("instructions")

	if not isinstance(instructions, str):
		return answered

	said = _THE_LABEL.sub(lambda found: f"{found.group(1)}{label}{found.group(3)}", instructions)

	if elsewhere:
		said = (
			f"{said} Other instances are configured on this machine "
			f"({', '.join(elsewhere)}) and this session cannot reach them; one server "
			f"reaches one."
		)

	answered["result"]["instructions"] = said

	return answered


def _how_to_make_one () -> str:
	"""Return the remedy that works on *this* machine for creating an instance — `#734`.

	**The same sentence is right one file away and wrong here, and the difference is who is
	reading.** ``clients/local.py`` says *"run 'subroutine init'"* to somebody who is running
	the CLI, so they demonstrably have it. This fires for an agent whose plugin launched
	``uvx subroutine~=X.Y mcp``, and ``uvx`` runs from a cache and puts nothing on ``PATH`` —
	so the same advice answers ``command not found`` for exactly the audience `#585` created by
	making the plugin work on arrival.

	**Measured rather than assumed.** Whether the command exists is a fact about this machine
	and :func:`shutil.which` is how you ask; guessing from *how the relay was started* would be
	the same mistake one level along, since somebody may have both. A machine that has the
	command gets the short answer it has always had.

	**Version-matched, and that is not fussiness.** Plain ``uvx subroutine init`` fetches the
	newest release, which can create an instance whose schema is ahead of the program that will
	read it — `#250`'s skew, manufactured by our own advice. The pin is derived from the running
	program because the relay *is* what the plugin's pin launched, so it can report itself
	instead of guessing; a second copy of ``~=0.6.0`` here would be one more thing to move at
	release, which is why ``scripts/release.py`` reads the pin out of the manifests rather than
	naming it.
	"""

	if shutil.which("subroutine") is not None:
		return "Run 'subroutine init' in a terminal to create one. It takes no arguments."

	series = ".".join(subroutine.__version__.split(".")[:2])

	return (
		f"Run 'uvx subroutine~={series} init' in a terminal to create one. That needs only "
		"'uv' installed, which is what launched this. To have 'subroutine' as a command as "
		"well, 'uv tool install subroutine' first."
	)


def _refused (raw: str, detail: str, hint: str | None) -> dict[str, typing.Any] | None:
	"""Return a JSON-RPC error for something that went wrong on this side of the wire.

	**Carrying the request's id when there is one**, because a client matches answers to
	requests by it and an error with the wrong id is worse than none: it either resolves the
	wrong call or is dropped and the real one hangs. A message this side could not parse has no
	id to carry, and null is what the specification says to use.

	**Nothing at all for a notification** (`#697`). A request object with no ``id`` *member* is a
	notification, and the specification is explicit that a server must not reply to one — the
	client is not waiting on it, so an answer is an unmatched message arriving out of nowhere.
	This has always been the shape of a refusal here and was only reachable when a connection
	failed; naming a missing instance made it the ordinary first contact, which is how it was
	found. **A literal ``"id": null`` is not a notification** and still gets its answer, which is
	why the test is for the member rather than for the value.
	"""

	identifier = None

	try:
		parsed = json.loads(raw)

	except json.JSONDecodeError:
		parsed = None

	if isinstance(parsed, dict):
		if "id" not in parsed:
			return None

		identifier = parsed["id"]

	return {
		"jsonrpc": "2.0",
		"id": identifier,
		"error": {
			"code": subroutine.mcp.protocol.INTERNAL_ERROR,
			# **A newline between the two, as ``_explained`` uses.** Joined with a space, a
			# detail ending in somebody else's message — "…[Errno 111] Connection refused" —
			# runs straight into the remedy and reads as one malformed sentence.
			"message": detail if hint is None else f"{detail}\n{hint}",
		},
	}


def run (
	incoming: typing.TextIO,
	outgoing: typing.TextIO,
	*,
	connection: str | None = None,
	workspace: str | None = None,
	settings: subroutine.config.Settings | None = None,
) -> None:
	"""Forward a stdio MCP session to the instance one connection names.

	**The fallback is the configured default, not the current context** (`#276`). ``subroutine
	use`` is working state that a person moves between tasks, and a server reads it once at
	startup and holds the answer for the whole session — which made which instance an agent
	wrote to depend on where that happened to point at the unrelated moment its process started.
	``default_connection`` is a decision somebody took and can read back.
	"""

	resolved = settings or subroutine.config.load_settings()
	roster = subroutine.connections.roster(resolved)
	chosen = roster.require(connection or roster.default)

	subroutine.mcp.protocol.relay(
		answering(chosen, roster, resolved, workspace=workspace), incoming, outgoing
	)
