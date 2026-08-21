"""The wire: newline-delimited JSON-RPC 2.0 over stdio, and the MCP handshake on top.

Deliberately knows nothing about tasks. It reads messages, dispatches them by method name,
and writes answers back — so the half that could be wrong about the *protocol* is separate
from the half that could be wrong about *Subroutine*, and each can be tested without the
other.

Two rules from the specification are easy to get wrong and are load-bearing here:

* **A notification has no ``id`` and gets no answer at all.** Replying to one is not a
  harmless extra: the client is not waiting for it, and an unmatched response is a protocol
  error at the other end.
* **A tool that fails is a *successful* JSON-RPC response carrying ``isError: true``**, not
  a JSON-RPC error. Errors are for the protocol — unknown method, bad params. "There is no
  task #900" is an answer, and the model is meant to read it and try something else. Sending
  it as a JSON-RPC error hides it from the model, which is the whole failure this
  distinction exists to prevent.
"""

import dataclasses
import json
import typing

import subroutine.errors

#: The specification revision these shapes were taken from. Sent back verbatim when the
#: client asks for it, and used as the fallback when it asks for something else — the
#: specification says a server that does not support the requested version answers with one
#: it does support, and lets the client decide whether to continue.
PROTOCOL_VERSION = "2025-06-18"

#: JSON-RPC's own codes. Only the ones this server can actually produce.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


@dataclasses.dataclass(frozen=True)
class Tool:
	"""One callable tool, as ``tools/list`` describes it and ``tools/call`` invokes it.

	``schema`` is a JSON Schema for the arguments. **Every byte of it is context an agent
	carries for the whole session whether it calls the tool or not**, which is why the tools
	in this server are few and their arguments lean on grammars that already exist (§6.13's
	capture line) rather than restating every field as a property.
	"""

	name: str
	title: str
	description: str
	schema: dict[str, typing.Any]
	call: typing.Callable[[dict[str, typing.Any]], str]

	#: What this tool does to the world, in the protocol's own vocabulary — `#489`.
	#:
	#: **Absent means the worst case, not "unknown".** The specification tells a client to read
	#: an unannotated tool as potentially destructive, non-idempotent and open-world, and clients
	#: increasingly drive their approval prompts off exactly this. So declaring nothing is not
	#: neutral: it is a claim, and on the five tools here that only read it is a false one.
	annotations: dict[str, bool] | None = None

	#: What this tool calls a field the layer below names differently — `#547`. Only for the
	#: irregular cases: :func:`_as_this_tool_calls_it` already handles an ``_id`` suffix, and
	#: nothing here is applied unless the tool really does declare the name it maps to.
	renames: dict[str, str] | None = None

	def described (self) -> dict[str, typing.Any]:
		"""Return this tool as ``tools/list`` reports it."""

		described = {
			"name": self.name,
			"title": self.title,
			"description": self.description,
			"inputSchema": self.schema,
		}

		# Omitted rather than sent empty: an empty object is a statement in this protocol, and
		# a tool with nothing to declare should read as unannotated rather than as annotated
		# with no claims.
		if self.annotations:
			described["annotations"] = self.annotations

		return described


@dataclasses.dataclass(frozen=True)
class Resource:
	"""One document an agent may read when it wants it — `#483`.

	**A second channel, and the difference from a tool is the whole reason this exists.** A
	tool's schema is context every session carries whether it is called or not, which is why
	§21.2 caps the surface at thirteen and why this server has no room for a documentation
	tool. A resource costs a name and a line in ``resources/list``; the *content* is fetched
	only when a model asks for it.

	``read`` is a callable rather than a string so nothing is captured at start-up. The guide,
	the worked examples and this installation's vocabulary all live on the instance and change
	without this process restarting — a resource holding a copy would be the fourth edition of
	a document, which is the duplication `#47` forbids.
	"""

	uri: str
	name: str
	title: str
	description: str
	mime_type: str
	read: typing.Callable[[], str]

	#: How a client that cannot read resources reaches the same thing — `#506`.
	#:
	#: **Declared rather than derived.** Decision `#499`'s guard used to compose this by string
	#: surgery on the URI, which worked while every resource happened to be a document under
	#: ``/v1/docs/``. The first one that was not — an index assembled from a listing — would
	#: have been asserted against ``/v1/conventions``, a route that does not exist. A resource
	#: knows its own second route; a test guessing it is a map, and this repository has been
	#: bitten by every map it has written (`#336`, `#427`).
	#:
	#: Not optional, so a fifth resource cannot quietly have only one way in. That is the
	#: half of `#499` about a channel being client-dependent: a ``subroutine://`` URI is
	#: useless to a client that does not read resources.
	also_at: str

	def described (self) -> dict[str, typing.Any]:
		"""Return this resource as ``resources/list`` reports it."""

		return {
			"uri": self.uri,
			"name": self.name,
			"title": self.title,
			"description": self.description,
			"mimeType": self.mime_type,
		}


class Server:
	"""Answers MCP methods for a fixed set of tools and resources.

	Holds no connection and no session of its own: the tools close over whatever they need,
	so this class is testable with tools that do arithmetic.
	"""

	def __init__ (
		self,
		tools: typing.Sequence[Tool],
		*,
		name: str,
		version: str,
		instructions: str | None = None,
		resources: typing.Sequence[Resource] = (),
	) -> None:
		"""Build a server over these tools and resources."""

		self.tools = {tool.name: tool for tool in tools}
		self.resources = {resource.uri: resource for resource in resources}
		self.name = name
		self.version = version
		self.instructions = instructions

	def handle (self, message: dict[str, typing.Any]) -> dict[str, typing.Any] | None:
		"""Answer one message, or return ``None`` when it deserves no answer.

		``None`` means a notification. The caller must write nothing at all rather than
		writing an empty object, which would be an unmatched response to a client that is
		not waiting for one.
		"""

		method = message.get("method")
		identifier = message.get("id")

		if identifier is None:
			# A notification. `notifications/initialized` is the one this server expects;
			# anything else is ignored rather than refused, because there is nowhere to send
			# a refusal and a client is entitled to tell us things we do not act on.
			#
			# **This reads an absent `id` and an explicit `"id": null` as the same thing**,
			# which is worth stating because the code does not show it. MCP says a request id
			# MUST NOT be null, so a null one is malformed either way and silence is a
			# defensible answer to it. Note `id: 0` is *not* caught here — `is None` rather
			# than a falsy test, deliberately, since zero is a legal id.
			return None

		if not isinstance(method, str):
			return _failure(identifier, INVALID_REQUEST, "A request needs a method name.")

		if method == "initialize":
			return _result(identifier, self._initialize(message.get("params") or {}))

		if method == "ping":
			return _result(identifier, {})

		if method == "tools/list":
			return _result(
				identifier, {"tools": [tool.described() for tool in self.tools.values()]}
			)

		if method == "tools/call":
			return self._call(identifier, message.get("params") or {})

		if method == "resources/list":
			return _result(
				identifier,
				{"resources": [found.described() for found in self.resources.values()]},
			)

		if method == "resources/read":
			return self._read(identifier, message.get("params") or {})

		return _failure(identifier, METHOD_NOT_FOUND, f"Unknown method: {method}")

	def _read (
		self, identifier: typing.Any, params: dict[str, typing.Any]
	) -> dict[str, typing.Any]:
		"""Return one resource's content, or say why not.

		**A failure here is a protocol error rather than a result**, unlike ``tools/call``,
		which reports a refusal *inside* a successful response so the model can read it and try
		something else. A resource has no such conversation: the client asked for a document by
		uri and either got it or did not, and a client is the only thing that ever composes one
		of these — so a wrong uri is a bug in the client rather than something a model should
		be handed to reason about.
		"""

		uri = params.get("uri")
		found = self.resources.get(uri) if isinstance(uri, str) else None

		if found is None:
			known = ", ".join(sorted(self.resources)) or "none"

			return _failure(
				identifier, INVALID_PARAMS, f"No such resource: {uri!r}. There is {known}."
			)

		try:
			text = found.read()

		except Exception as unreachable:
			# The instance is on the far end of a network for every resource here, so this is
			# the ordinary case rather than the exceptional one — an unreachable server must
			# read as "could not fetch it" and not as this process falling over.
			return _failure(
				identifier,
				INTERNAL_ERROR,
				f"{found.uri} could not be read from the instance: {unreachable}",
			)

		return _result(
			identifier,
			{"contents": [{"uri": found.uri, "mimeType": found.mime_type, "text": text}]},
		)

	def _initialize (self, params: dict[str, typing.Any]) -> dict[str, typing.Any]:
		"""Return what this server is and what it can do.

		The requested version is echoed when it is one we speak, and otherwise answered with
		ours — the specification puts the decision to continue on the client rather than
		making a mismatch fatal here.
		"""

		wanted = params.get("protocolVersion")
		agreed = wanted if wanted == PROTOCOL_VERSION else PROTOCOL_VERSION

		described: dict[str, typing.Any] = {
			"protocolVersion": agreed,
			# `listChanged` is false and stated rather than omitted: this server's tools are
			# fixed at startup, so promising notifications would be a promise nothing keeps.
			# **Declared from what this server actually has**, not from what the class can do:
			# a server built with no resources must not advertise the capability, or a client
			# calls `resources/list` and is told about an empty channel it was promised.
			"capabilities": _capabilities(bool(self.resources)),
			"serverInfo": {"name": self.name, "version": self.version},
		}

		if self.instructions is not None:
			described["instructions"] = self.instructions

		return described

	def _call (
		self, identifier: typing.Any, params: dict[str, typing.Any]
	) -> dict[str, typing.Any]:
		"""Run one tool and report what it said.

		An unknown tool is a *protocol* error, because the client asked for something that
		does not exist. Anything the tool itself raises is a *result* carrying ``isError``,
		because the model asked a reasonable question and deserves to read the answer.
		"""

		name = params.get("name")
		tool = self.tools.get(name) if isinstance(name, str) else None

		if tool is None:
			return _failure(identifier, INVALID_PARAMS, f"Unknown tool: {name}")

		arguments = params.get("arguments") or {}

		if not isinstance(arguments, dict):
			return _failure(identifier, INVALID_PARAMS, "'arguments' must be an object.")

		unknown = _undeclared(tool, arguments)

		if unknown:
			accepted = ", ".join(sorted(tool.schema.get("properties", {}))) or "no arguments"

			# **A result rather than a protocol error, for this method's own reason.** A
			# JSON-RPC error is handled by the client and may never reach the model, and the
			# whole value here is the *model* learning that what it asked for did not happen.
			return _result(
				identifier,
				_content(
					f"{tool.name} does not take {', '.join(unknown)}. "
					f"It accepts: {accepted}.",
					failed=True,
				),
			)

		mistyped = _mistyped(tool, arguments)

		if mistyped:
			# Reported the same way an unknown argument is, and for the same reason: the model
			# is the one that has to try again, so it has to be the one told.
			return _result(
				identifier,
				_content(f"{tool.name} was given {'; '.join(mistyped)}.", failed=True),
			)

		try:
			return _result(identifier, _content(tool.call(arguments)))

		except Exception as failure:
			# Deliberately broad. A tool that raises anything at all must still produce a
			# readable answer: an exception escaping here would take the whole session down
			# over one bad argument, and the model would learn nothing from the silence.
			return _result(identifier, _content(_explained(failure, tool), failed=True))


def _capabilities (has_resources: bool) -> dict[str, typing.Any]:
	"""Return what this server says it can do.

	``listChanged`` is false and *stated* rather than omitted, for both channels: the tools and
	the resources are fixed at start-up, so promising notifications would be a promise nothing
	keeps.
	"""

	described: dict[str, typing.Any] = {"tools": {"listChanged": False}}

	if has_resources:
		described["resources"] = {"listChanged": False, "subscribe": False}

	return described


def _result (identifier: typing.Any, payload: dict[str, typing.Any]) -> dict[str, typing.Any]:
	"""Wrap a successful answer."""

	return {"jsonrpc": "2.0", "id": identifier, "result": payload}


def _undeclared (tool: "Tool", arguments: dict[str, typing.Any]) -> list[str]:
	"""Return the argument names this tool does not declare — item ``#379``.

	**Refused rather than dropped**, which is the rule ``api/query.py`` already settled for a
	listing's query parameters: one that ignores a name it does not know returns a complete,
	plausible, wrong answer and charges the caller for it. This surface was the odd one out.

	Reported by an agent that met it: it narrowed a listing to a project on an installation
	whose schema had no such argument, was neither honoured nor refused, and believed the
	answer. **It survives every upgrade**, because what it needs is a client newer than its
	server — the ordinary state of a fleet, and the family `#345` belongs to.

	Names only, deliberately. Validating *types* would mean a JSON Schema implementation in a
	module whose whole argument is that it is small, and the failure it would catch is one the
	tool itself reports perfectly well.
	"""

	declared = tool.schema.get("properties")

	if not isinstance(declared, dict):
		return []

	return sorted(name for name in arguments if name not in declared)


def _failure (identifier: typing.Any, code: int, message: str) -> dict[str, typing.Any]:
	"""Wrap a protocol-level refusal."""

	return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


#: What a shell command in a shared refusal is called on this surface — `#548`.
#:
#: **A refusal has to name something its reader can do.** The layers below are written for a
#: person at a terminal and say so — *"Run 'subroutine list' to see what there is"* — and a
#: remote agent has no terminal at all, which is the whole premise of `#516`. It is not oddly
#: worded to that reader, it is unfollowable.
#:
#: **Translated here rather than reworded at the source**, and the reason is coverage rather
#: than effort. ``mcp/tools.py`` already carries two hand-written replacements, which is a
#: second copy of a message and is why only two of them exist; and neutering the shared text
#: would cost the command line a concrete remedy on the surface whose whole principle is that
#: an error says what to do next. This is one rule, in the one place a refusal becomes text
#: for this surface, and it reaches messages nobody has audited yet.
#:
#: Longest first when substituting, so ``doc edit`` is not read as ``doc``.
INSTEAD_OF = {
	"subroutine doc edit": "subroutine_document",
	# **Both halves of one tool** (`#588`). `subroutine_project` lists with no arguments and
	# creates with a key and a title, so the two commands translate to the same name — which is
	# why the substitution is longest-first: without that, `project create` would be rewritten
	# through `project list`'s entry and lose the verb.
	"subroutine project create": "subroutine_project",
	"subroutine project list": "subroutine_project",
	"subroutine search": "subroutine_search",
	"subroutine list": "subroutine_list",
	"subroutine show": "subroutine_show",
	"subroutine ls": "subroutine_list",
}

#: A shell command with no tool that does the same thing, and why leaving it is still right.
#:
#: **Both come from a database that could not be opened**, which is not a thing any tool acts
#: on: the remedy is somebody's, on the machine that holds it. An agent meeting one of these has
#: nothing to try and should say so rather than keep calling, and naming the command is what
#: tells it whose problem this is.
#:
#: **Reachable on a served instance too**, which is worth writing down because the first draft
#: of this comment said otherwise and an agent met ``subroutine config show`` within the hour —
#: a locked SQLite database, on a server, reported to a caller with no shell. The claim that
#: only a local connection could reach these was reasoning rather than measurement.
#:
#: An entry here says a translation would be wrong, not that the string is unreachable;
#: ``tests/test_mcp.py`` checks that no entry is also translated and that each gives a reason.
NO_TOOL_DOES_THIS = {
	"subroutine init": "no tool creates an instance, and nothing else works until one exists",
	"subroutine config": "no tool reports a machine's configuration, and a database that cannot "
	"be opened is fixed by whoever runs it rather than by the caller",
}


#: What each declared JSON Schema type accepts, as a Python check — `#549`.
#:
#: **``bool`` is excluded from ``integer`` deliberately.** ``isinstance(True, int)`` is true in
#: Python, so without it ``{"limit": true}`` would pass a check written to catch exactly that
#: class of mistake.
_ACCEPTS: dict[str, typing.Callable[[typing.Any], bool]] = {
	"boolean": lambda value: isinstance(value, bool),
	"integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
	"string": lambda value: isinstance(value, str),
	"object": lambda value: isinstance(value, dict),
}


def _mistyped (tool: "Tool", arguments: dict[str, typing.Any]) -> list[str]:
	"""Return a complaint per argument whose value is not the type the schema declares.

	**The schema is published and was never used as one** (`#549`). ``_undeclared`` checks the
	argument *names* and nothing checked their values, so a boolean given the string ``"false"``
	was truthy in Python and the filter came on — the model asked for it off, got a plausible
	answer, and had nothing to notice. `#149`'s blind spot one level down: a guard shaped around
	one property of an argument cannot see another.

	**Refused rather than coerced**, both kinds, which is one rule instead of two. ``"5"`` for an
	integer is unambiguous and could be taken; ``"false"`` for a boolean is unambiguous to a
	person and is the one value that must never be. Accepting the first and refusing the second
	teaches a model that strings are sometimes fine, and the case where they are not is the case
	that fails silently.

	``None`` is passed over rather than refused. Every tool reads its arguments with ``.get``, so
	an explicit null already behaves exactly as an omission does, and some clients send one for
	every field they have not filled in. Refusing it would break them to no purpose.

	**A list of types is a union and any of them will do**, which is what ``A_REF`` is: an item
	is named by ``42`` or by ``"#42"`` and both are published. Skipping a union instead would
	have left the seven arguments most likely to be sent badly as the seven this cannot see.

	A type this does not know is left alone, so a schema growing an array is a thing somebody
	adds a rule for rather than a thing that quietly starts being rejected.
	"""

	declared = tool.schema.get("properties", {})
	wrong = []

	for name in sorted(arguments):
		kinds = _declared_types(declared.get(name, {}))
		value = arguments[name]

		if not kinds or value is None:
			continue

		if any(_ACCEPTS[kind](value) for kind in kinds):
			continue

		wrong.append(f"{name}={json.dumps(value)} where it takes {_readable(kinds)}")

	return wrong


def _declared_types (specification: dict[str, typing.Any]) -> list[str]:
	"""Return the types a property declares that this knows how to check."""

	declared = specification.get("type")
	named = [declared] if isinstance(declared, str) else declared

	if not isinstance(named, list):
		return []

	return [kind for kind in named if isinstance(kind, str) and kind in _ACCEPTS]


def _readable (kinds: typing.Sequence[str]) -> str:
	"""Name what a property accepts, the way somebody reading a sentence would."""

	spelt = {
		"boolean": "true or false",
		"integer": "a whole number",
		"string": "text",
		"object": "an object",
	}
	named = [spelt.get(kind, kind) for kind in kinds]

	if len(named) == 1:
		return named[0]

	return f"{', '.join(named[:-1])} or {named[-1]}"


def _as_this_tool_calls_it (field: str, tool: Tool | None) -> str:
	"""Return a field's name as an argument of this tool, or unchanged when it has none.

	**A refusal is only actionable if the thing it names is a thing the reader can pass**
	(`#547`). The layer below is shared by the CLI, the HTTP API and this server, so it names
	fields in its own vocabulary — ``workspace_id``, which is exactly right in a problem
	document and is not an argument any tool here declares. An agent following that advice is
	refused a second time, and recovers only because `#379` lists what the tool does accept.

	**It can only ever rename to a name the tool actually declares**, which is what makes the
	``_id`` rule safe to derive rather than list. A convention deciding a user-facing string is
	a shape this codebase has been bitten by; this one cannot invent a name, because it checks
	the schema before using it, and a tool with no such argument keeps the original.
	"""

	if tool is None:
		return field

	arguments = tool.schema.get("properties", {})

	if field in arguments:
		return field

	renamed = (tool.renames or {}).get(field)

	if renamed is not None and renamed in arguments:
		return renamed

	shortened = field.removesuffix("_id")

	if shortened != field and shortened in arguments:
		return shortened

	return field


def _explained (failure: BaseException, tool: Tool | None = None) -> str:
	"""Render a refusal with its remedy attached, not only its complaint.

	**The hint is the half an agent needs most, and it was being dropped** (`#165`). ``str()``
	on a :class:`~subroutine.errors.SubroutineError` is its ``detail`` alone, so "no Subroutine
	instance has been set up here yet." reached the model without the one sentence that says
	what to do about it. A person can run ``--help``; a model gets one answer and either
	guesses from it or gives up.

	This is the same assembly ``cli.main._fail`` does, including its rule against saying one
	thing three ways: a field message that merely restates the detail, or a field hint that
	restates the overall hint, is left out. A refusal read as noise makes the next one noise
	too, and that costs more on a surface where every line is context the model carries.
	"""

	if not isinstance(failure, subroutine.errors.SubroutineError):
		return str(failure) or repr(failure)

	lines = [failure.detail]

	if failure.hint is not None:
		lines.append(failure.hint)

	for field in failure.errors:
		if field.message not in (failure.detail, failure.hint):
			lines.append(f"{_as_this_tool_calls_it(field.field, tool)}: {field.message}")

		if field.hint is not None and field.hint not in (failure.hint, field.message):
			lines.append(field.hint)

	return _in_this_surfaces_words("\n".join(lines))


def _in_this_surfaces_words (text: str) -> str:
	"""Return a refusal with the commands in it named as this surface's tools — `#548`.

	Substituted longest first, so ``subroutine doc edit`` is not read as ``subroutine doc``,
	and the surrounding quotes are left alone: *"Run 'subroutine_list' to see what there is"*
	is a slightly odd sentence and an entirely followable one, which is the trade.
	"""

	for command in sorted(INSTEAD_OF, key=len, reverse=True):
		text = text.replace(command, INSTEAD_OF[command])

	return text


def _content (text: str, *, failed: bool = False) -> dict[str, typing.Any]:
	"""Return a tool result carrying one block of text."""

	return {"content": [{"type": "text", "text": text}], "isError": failed}


def answer (server: Server, raw: str | bytes) -> dict[str, typing.Any] | None:
	"""Parse one message and answer it, or return ``None`` when it deserves no answer.

	**Everything between "bytes arrived" and "here is the reply" lives here, so that a
	transport is only a way of moving them.** It was inlined in :func:`serve` until `#516`
	gave this server a second transport, and the two would then have decided separately what
	a malformed message deserves — which is `#530` exactly: the defect already open against
	this file is that the transports disagree about a message they cannot dispatch.

	A parse failure and a non-object are answered against a ``null`` id, because there is no
	id to answer against. JSON-RPC asks for that, and it is at least visible in a client's log.
	"""

	try:
		message = json.loads(raw)

	except (json.JSONDecodeError, UnicodeDecodeError):
		return _failure(None, PARSE_ERROR, "That was not JSON.")

	if not isinstance(message, dict):
		# **Including a list**, which is a JSON-RPC batch. Batching was removed from MCP in
		# `2025-06-18` — the revision this server speaks — and `2026-07-28` says the body of a
		# POST is a single request or notification. So this is not a gap; it is the rule.
		return _failure(None, INVALID_REQUEST, "A message must be an object.")

	return server.handle(message)


def serve (
	server: Server,
	incoming: typing.TextIO,
	outgoing: typing.TextIO,
) -> None:
	"""Read messages until the input closes, answering each one.

	The stdio transport is newline-delimited JSON, so a message may not contain a raw
	newline — ``json.dumps`` does not emit one inside a string, and every write here ends
	with exactly one.

	**Flushed after every message.** A client is blocked waiting for the answer, so a
	buffered reply is a hung session rather than a slow one.
	"""

	relay(lambda raw: answer(server, raw), incoming, outgoing)


def relay (
	answering: typing.Callable[[str], dict[str, typing.Any] | None],
	incoming: typing.TextIO,
	outgoing: typing.TextIO,
) -> None:
	"""Read messages until the input closes, having each one answered somewhere.

	**The framing without the server** — `#539`. ``subroutine mcp`` is a transport adapter now:
	it reads a message, gets it answered by the instance the connection names, and writes the
	answer back. What that means for this module is that the loop and the answering have to come
	apart, because only one of them moves.

	The rules are the transport's and are unchanged: newline-delimited JSON, blank lines
	ignored, nothing written for a message that deserves no answer, and a flush after each one
	because a client is blocked waiting.
	"""

	for line in incoming:
		stripped = line.strip()

		if not stripped:
			continue

		reply = answering(stripped)

		if reply is not None:
			_write(outgoing, reply)


def _write (outgoing: typing.TextIO, message: dict[str, typing.Any]) -> None:
	"""Send one message, framed and flushed."""

	outgoing.write(json.dumps(message, separators=(",", ":")) + "\n")
	outgoing.flush()
