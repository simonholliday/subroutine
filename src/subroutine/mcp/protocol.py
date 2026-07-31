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

	def described (self) -> dict[str, typing.Any]:
		"""Return this tool as ``tools/list`` reports it."""

		return {
			"name": self.name,
			"title": self.title,
			"description": self.description,
			"inputSchema": self.schema,
		}


class Server:
	"""Answers MCP methods for a fixed set of tools.

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
	) -> None:
		"""Build a server over these tools."""

		self.tools = {tool.name: tool for tool in tools}
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

		return _failure(identifier, METHOD_NOT_FOUND, f"Unknown method: {method}")

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
			"capabilities": {"tools": {"listChanged": False}},
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

		try:
			return _result(identifier, _content(tool.call(arguments)))

		except Exception as failure:
			# Deliberately broad. A tool that raises anything at all must still produce a
			# readable answer: an exception escaping here would take the whole session down
			# over one bad argument, and the model would learn nothing from the silence.
			return _result(identifier, _content(str(failure) or repr(failure), failed=True))


def _result (identifier: typing.Any, payload: dict[str, typing.Any]) -> dict[str, typing.Any]:
	"""Wrap a successful answer."""

	return {"jsonrpc": "2.0", "id": identifier, "result": payload}


def _failure (identifier: typing.Any, code: int, message: str) -> dict[str, typing.Any]:
	"""Wrap a protocol-level refusal."""

	return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def _content (text: str, *, failed: bool = False) -> dict[str, typing.Any]:
	"""Return a tool result carrying one block of text."""

	return {"content": [{"type": "text", "text": text}], "isError": failed}


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

	for line in incoming:
		stripped = line.strip()

		if not stripped:
			continue

		try:
			message = json.loads(stripped)

		except json.JSONDecodeError:
			# There is no id to answer against, so this is reported as a null-id error —
			# which is what JSON-RPC asks for and is at least visible in a client's log.
			_write(outgoing, _failure(None, PARSE_ERROR, "That was not JSON."))

			continue

		if not isinstance(message, dict):
			_write(outgoing, _failure(None, INVALID_REQUEST, "A message must be an object."))

			continue

		answer = server.handle(message)

		if answer is not None:
			_write(outgoing, answer)


def _write (outgoing: typing.TextIO, message: dict[str, typing.Any]) -> None:
	"""Send one message, framed and flushed."""

	outgoing.write(json.dumps(message, separators=(",", ":")) + "\n")
	outgoing.flush()
