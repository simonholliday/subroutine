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

**Two things may be added on the way back, and only on this machine's say-so.** This process
is the one the ``subroutine`` plugin starts, so it is the only place that sees the plugin's
token field: it says when that field has emptied since the tools last started (`#3517`), and,
while the field is what the tools act with, that a sign-out of Claude Code would empty it
(`#3603`). It reads a message to decide where to say either, and never refuses one for how it
reads.
"""

import contextlib
import json
import os
import pathlib
import re
import shutil
import typing

import httpx
import sqlalchemy.orm

import subroutine
import subroutine.api.inprocess
import subroutine.auth
import subroutine.config
import subroutine.connections
import subroutine.credentials
import subroutine.domain.authentication
import subroutine.errors
import subroutine.installations
import subroutine.mcp.protocol

#: The endpoint an instance serves MCP on. Named once because both halves below reach it, and
#: because a served instance and an in-process one must not be able to disagree about the path.
PATH = "/mcp"

#: The clause the far end writes about which instance is being worked in, and which this side
#: has to correct: the server knows what it calls itself, and the caller knows the private alias
#: they call it (`#330`). Anchored on the words either side of the name so that a change to the
#: sentence fails the test that drives this rather than silently leaving the wrong label.
_THE_LABEL = re.compile(r"(on connection ')([^']*)(')")

#: The plugin that passes its *Agent token* field to this program: as ``SUBROUTINE_PLUGIN_TOKEN``
#: since 0.9.9 (`#3600`), and as ``SUBROUTINE_TOKEN`` before it (`#3522`). Its ``.mcp.json`` sets
#: the variable in the server's own environment, so in a process that plugin started, the
#: variable is the field.
PLUGIN = "subroutine"

#: Where this machine keeps, per connection, whether the plugin's token field held a token
#: when its tools last started (`#3517`). The token's public prefix is kept, never the token.
FIELD_STATE = "plugin-token.json"

#: The one tool whose whole answer is who the session is, so it carries the notice every time.
_WHO = "subroutine_whoami"


def credential (
	connection: subroutine.connections.Connection,
	roster: subroutine.connections.Roster,
) -> subroutine.credentials.Resolved:
	"""Find the credential this session presents, reading the plugin's field as its own (`#3522`).

	``SUBROUTINE_TOKEN`` is the *default* connection's token, deliberately: a token somebody
	exported in a shell for one instance must never be offered to another, which would hand it
	to that instance's operator. But the ``subroutine`` plugin passes its token field whichever
	connection its *Which instance* field names, and it passed the field as that variable - so a
	plugin pointed at any other connection had its token skipped, and its tools acted as whoever
	``credentials.toml`` held there, the person usually, with nothing said.

	**Where that plugin started this process, its field is the token of the connection the session
	was started for.** Since plugin 0.9.9 the field travels as ``SUBROUTINE_PLUGIN_TOKEN``, a name
	nobody exports for a shell, and ``SUBROUTINE_TOKEN`` keeps its meaning beside it (`#3600`).
	**Which variable carries the field is read from the plugin's own ``.mcp.json``**, never from
	which variables are set: an editor may pass an empty field as a set, empty variable or as none
	at all, so an absent one cannot say which plugin started this.

	**A plugin from before 0.9.9 passes the field as ``SUBROUTINE_TOKEN``**, which is then read as
	the field. That rests on Claude Code setting the variable whether the field is filled or not -
	seen set and empty on nuc14 when the field was (`#3244`), and not measured against a version.
	One that left an empty field unset would let a token exported in the shell stand in for it,
	offered to the named connection. The 0.9.9 plugin still sets that variable too, for the
	programs from before it, which read nothing else.
	"""

	if not subroutine.installations.started_by(PLUGIN):
		return subroutine.credentials.resolve(connection, default_connection=roster.default)

	if subroutine.installations.plugin_sets(subroutine.credentials.PLUGIN_VARIABLE):
		return subroutine.credentials.resolve(
			connection, default_connection=roster.default, plugin_field=True
		)

	return subroutine.credentials.resolve(connection, default_connection=connection.name)


def answering (
	connection: subroutine.connections.Connection,
	roster: subroutine.connections.Roster,
	settings: subroutine.config.Settings,
	*,
	workspace: str | None = None,
) -> typing.Callable[[str], dict[str, typing.Any] | None]:
	"""Return something that answers one raw JSON-RPC message from the chosen instance."""

	# **Resolved once, here, and handed to whichever forwarder is built.** A credential can come
	# from a `token_command` - `pass show`, `gpg` - and asking per message would run it on every
	# tool call and could prompt for a passphrase in the middle of one. Asking a second time to
	# learn where it came from, which the notice below needs (`#3603`), would prompt twice.
	held = credential(connection, roster)
	forward = (
		_in_process(held, settings, workspace=workspace)
		if connection.is_local
		else _over_http(connection, held, workspace=workspace)
	)
	elsewhere = tuple(name for name in roster.names if name != connection.name)
	notice = _notice(connection, held)

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
				"Check what is serving that address - a proxy or a captive portal answers "
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

		ours = _in_this_machines_terms(answered, connection.label, elsewhere)

		return ours if notice is None else notice.added(raw, ours)

	return answer


def _over_http (
	connection: subroutine.connections.Connection,
	resolved: subroutine.credentials.Resolved,
	*,
	workspace: str | None,
) -> typing.Callable[[str], tuple[int, str]]:
	"""Return a forwarder that posts to a served instance, presenting this credential."""

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
			# **Both, because the transport says a client must offer both** — a server is free
			# to answer a stream, and one that does would find this client saying it could not
			# read the only reply it is able to give. Ours answers JSON and always has; this
			# is about what a *different* server is entitled to assume, which is the half a
			# client written against one implementation never exercises.
			"Accept": "application/json, text/event-stream",
			# The version this session speaks, on every request after the handshake, as the
			# transport requires. Sent unconditionally rather than after `initialize`: this
			# forwarder speaks exactly one version, so there is nothing to negotiate and
			# nothing that could make the header disagree with the session.
			"MCP-Protocol-Version": subroutine.mcp.protocol.PROTOCOL_VERSION,
			"User-Agent": f"subroutine/{subroutine.API_VERSION}",
			# **Which copy of us is talking** (`#839`). `User-Agent` carries `API_VERSION` — the
			# contract, `1.0` — so before this nothing told the far end what was running here.
			# This forwarder is the one path where both answers are knowable: the program is the
			# process, and the plugin is the cache directory the editor started it from.
			**subroutine.installations.calling(),
		},
	)

	def forward (raw: str) -> tuple[int, str]:
		"""Post one message and return the status and body."""

		try:
			answered = client.post(
				PATH, params=_asking_for(workspace), content=raw.encode("utf-8")
			)

		except httpx.LocalProtocolError:
			# **Never quoted** (`#3584`), for the client's reason: httpx names the header by its value,
			# and the value is the token.
			raise subroutine.credentials.unsendable(connection.name) from None

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
	held: subroutine.credentials.Resolved,
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


def _emptied (connection: subroutine.connections.Connection) -> str | None:
	"""Return the prefix the plugin's token field held last time, where it is empty now (`#3517`).

	The string is empty where the field held something that was not a token. ``None`` means there
	is nothing to say: the plugin did not start this process, or one from before 0.9.9 did and
	passed no variable to read (:func:`_field`); its field holds a token, which is kept for next
	time; it was empty last time too; or a project's own ``SUBROUTINE_TOKEN_<NAME>`` answers
	anyway, so the field changes nothing here.

	**The case it is for, measured on `#3496`:** Claude Code empties the field when you sign out
	of it, uninstall the plugin or remove its marketplace. The program then resolves whatever this
	machine holds for the connection - usually the person - and on nuc14 about sixty writes went
	out as him before anybody looked (`#3244`). A field that was always blank is the recommended
	setup, so it is never mentioned; only a change is.

	**Nothing is recorded as empty here.** :class:`_Notice` does that once a write has carried the
	notice, so a session that ends before saying it leaves the next one to.
	"""

	if not subroutine.installations.started_by(PLUGIN):
		return None

	field = _field()

	if field is None:
		return None

	if field.strip():
		parsed = subroutine.auth.parse_token(field.strip())
		_record(connection.name, parsed[0] if parsed is not None else "")

		return None

	if os.environ.get(subroutine.credentials.variable_for(connection.name)):
		return None

	return _held().get(connection.name)


def _field () -> str | None:
	"""Return the plugin's token field as this process has it, or ``None`` where that cannot be told.

	**Since plugin 0.9.9 an absent variable is an empty field** (`#3600`): the plugin's own
	``.mcp.json`` says it passes one, so a variable that did not arrive can only be a field the
	editor left out for being empty. Before, the field travelled as ``SUBROUTINE_TOKEN``, and an
	absent one cannot be told from no field at all.
	"""

	if subroutine.installations.plugin_sets(subroutine.credentials.PLUGIN_VARIABLE):
		return os.environ.get(subroutine.credentials.PLUGIN_VARIABLE, "")

	return os.environ.get(subroutine.credentials.DEFAULT_VARIABLE)


def _told (connection: subroutine.connections.Connection, prefix: str) -> str:
	"""Say that the plugin's token field emptied, who the tools act as now, and what to do.

	The ways the field empties are listed rather than one blamed, because this side sees an
	empty field and not what was done to it.
	"""

	held = f"a token ({prefix}…)" if prefix else "a token"

	return (
		f"The Subroutine plugin's token field held {held} when these tools last started, and it "
		f"is empty now. So they act as whoever this machine's own credentials name for "
		f"'{connection.label}' - subroutine_whoami says who - and not as that token's account. "
		"Claude Code empties the field when you sign out of Claude Code, uninstall the plugin or "
		"remove its marketplace. If nobody meant to, enter the token again with /plugin in a "
		"Claude Code terminal session, or give this project an agent of its own with "
		"'subroutine agent create <name> --workspace <workspace> --here'."
	)


def _at_risk (connection: subroutine.connections.Connection) -> str:
	"""Say that the tools act with the plugin's token field, which a sign-out empties (`#3603`).

	**Said while nothing is wrong, because afterwards is too late to be useful.** Once the field is
	empty, `#3517`'s notice reports the change - after writes have gone out under another name.
	Simon asked on 2026-09-24 for people who sign out as a matter of course to know beforehand
	what it will cost them, and this is the one place that knows the field is what answers.
	"""

	return (
		"This session's token is the one in the Subroutine plugin's token field. Claude Code "
		"deletes that field when you sign out of Claude Code, uninstall the plugin or remove its "
		"marketplace, and these tools then act as whoever this machine's own credentials name for "
		f"'{connection.label}'. None of the three touches an agent of this project's own: "
		"'subroutine agent create <name> --workspace <workspace> --here', run in the project's "
		"directory."
	)


class _Notice:
	"""Carry one notice on every ``subroutine_whoami`` answer, and on the first write's if asked.

	**Which tools write is learned, not listed**, from the ``readOnlyHint`` each tool declares in
	the ``tools/list`` answer passing through - so this adapter still holds no catalogue. A tool
	it has not seen declared as reading is taken to write, so a session that never asked for the
	list is told at its first call of any kind.

	**Said on a write, then recorded** (`#3517`). The first write's answer is where the wrong
	name shows up, so once one has carried the notice the field is recorded as empty and the next
	session says nothing. ``subroutine_whoami`` keeps saying it for the rest of this session,
	because it stays true.

	**Where nothing has changed hands yet, it is said on ``subroutine_whoami`` alone** (`#3603`).
	``on_a_write`` is false for that one: every write is going out under the right name, so none
	of them is the moment to interrupt, and nothing is recorded.
	"""

	def __init__ (self, connection: str, text: str, *, on_a_write: bool = True) -> None:
		"""Hold the notice for one connection's session, and whether a write carries it too."""

		self.connection = connection
		self.text = text
		self.on_a_write = on_a_write
		self.reads: set[str] = set()
		self.written = False

	def added (self, raw: str, answered: dict[str, typing.Any]) -> dict[str, typing.Any]:
		"""Return the answer with the notice on it where it belongs."""

		try:
			asked = json.loads(raw)

		except json.JSONDecodeError:
			return answered

		result = answered.get("result")

		if not isinstance(asked, dict) or not isinstance(result, dict):
			return answered

		if asked.get("method") == "tools/list":
			self._learn(result)

			return answered

		params = asked.get("params")
		name = params.get("name") if isinstance(params, dict) else None
		content = result.get("content")

		if asked.get("method") != "tools/call" or not isinstance(content, list):
			return answered

		writing = name != _WHO and name not in self.reads and not result.get("isError")

		if name != _WHO and not (self.on_a_write and writing and not self.written):
			return answered

		content.append({"type": "text", "text": self.text})

		if writing:
			self.written = True
			_forget(self.connection)

		return answered

	def _learn (self, listed: dict[str, typing.Any]) -> None:
		"""Remember which tools declare that they only read."""

		for tool in listed.get("tools") or []:
			if not isinstance(tool, dict):
				continue

			hints = tool.get("annotations")

			if isinstance(hints, dict) and hints.get("readOnlyHint") is True:
				self.reads.add(str(tool.get("name")))


def _notice (
	connection: subroutine.connections.Connection, held: subroutine.credentials.Resolved
) -> _Notice | None:
	"""Return what this session says about the plugin's token field, if anything.

	**Two cases, and they cannot both hold.** A field that has emptied since the tools last
	started is a change of hands that has already happened, so the first write says it too
	(`#3517`). A field that is what the tools act with is a change a sign-out *would* make, so it
	is said only where somebody asks who the session is (`#3603`). The field is empty in the first
	case and answers in the second.

	**Only where the field answered**, which the credential's source says: not where a project's
	own ``SUBROUTINE_TOKEN_<NAME>`` answered before it, which a sign-out leaves alone. A plugin from
	before 0.9.9 passes the field as ``SUBROUTINE_TOKEN``, which a shell may export too and which
	survives a sign-out, so there nothing can tell the two apart and nothing is said.
	"""

	emptied = _emptied(connection)

	if emptied is not None:
		return _Notice(connection.name, _told(connection, emptied))

	if held.source == subroutine.credentials.PLUGIN_FIELD:
		return _Notice(connection.name, _at_risk(connection), on_a_write=False)

	return None


def _state () -> pathlib.Path:
	"""Return where the token field's last state is kept."""

	return subroutine.config.state_home() / FIELD_STATE


def _held () -> dict[str, str]:
	"""Return, per connection, the prefix the field held when the tools last started with one."""

	try:
		loaded = json.loads(_state().read_text(encoding="utf-8"))

	except (OSError, ValueError):
		return {}

	if not isinstance(loaded, dict):
		return {}

	return {name: kept for name, kept in loaded.items() if isinstance(kept, str)}


def _record (connection: str, prefix: str) -> None:
	"""Keep the prefix the field holds now, for the next session to compare with."""

	held = _held()

	if held.get(connection) != prefix:
		held[connection] = prefix
		_keep(held)


def _forget (connection: str) -> None:
	"""Record that the field is empty now, so the next session does not say it again."""

	held = _held()

	if held.pop(connection, None) is not None:
		_keep(held)


def _keep (held: dict[str, str]) -> None:
	"""Write the field's state, and say nothing where this machine will not take it.

	A notice is worth less than the session it rides on, so a state directory that cannot be
	written costs the notice and never a tool call.
	"""

	path = _state()

	with contextlib.suppress(OSError):
		path.parent.mkdir(parents=True, exist_ok=True)
		subroutine.config.write_private(path, json.dumps(held, indent=2, sort_keys=True) + "\n")


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
