"""The MCP server: the wire, and the tools on it.

Two halves tested apart, because they can be wrong independently. The protocol tests use
tools that do arithmetic, so a failure means the *messages* are wrong; the tool tests drive
a real client against a real database, so a failure means *Subroutine* is wrong.

The message shapes here were taken from the published specification (2025-06-18) rather than
from memory, and the ones that are easy to get wrong are asserted rather than assumed: a
notification gets no answer at all, and a tool that fails is a successful response carrying
``isError`` rather than a JSON-RPC error.
"""

import io
import json
import typing
import uuid

import pytest
import sqlalchemy.orm

import api_support
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.domain.bootstrap
import subroutine.mcp.protocol
import subroutine.mcp.tools


def _adding (name: str = "add") -> subroutine.mcp.protocol.Tool:
	"""Return a tool that adds two numbers, for testing the wire and nothing else."""

	return subroutine.mcp.protocol.Tool(
		name=name,
		title="Add",
		description="Add two numbers.",
		schema={"type": "object", "properties": {"a": {"type": "integer"}}},
		call=lambda arguments: str(arguments["a"] + arguments["b"]),
	)


@pytest.fixture
def server () -> subroutine.mcp.protocol.Server:
	"""A server with one arithmetic tool."""

	return subroutine.mcp.protocol.Server(
		[_adding()], name="test", version="0", instructions="Adds numbers."
	)


def _exchange (
	server: subroutine.mcp.protocol.Server, *messages: dict[str, typing.Any]
) -> list[dict[str, typing.Any]]:
	"""Run messages through the real stdio loop and return what came back."""

	incoming = io.StringIO("\n".join(json.dumps(message) for message in messages) + "\n")
	outgoing = io.StringIO()

	subroutine.mcp.protocol.serve(server, incoming, outgoing)

	return [json.loads(line) for line in outgoing.getvalue().splitlines() if line]


def test_the_handshake_answers_with_what_this_server_is (
	server: subroutine.mcp.protocol.Server,
) -> None:
	"""``initialize`` is the first thing any client sends, and everything else waits on it."""

	answered = _exchange(
		server,
		{
			"jsonrpc": "2.0",
			"id": 1,
			"method": "initialize",
			"params": {"protocolVersion": "2025-06-18", "capabilities": {}},
		},
	)

	assert len(answered) == 1

	result = answered[0]["result"]

	assert result["protocolVersion"] == "2025-06-18"
	assert result["capabilities"]["tools"] == {"listChanged": False}
	assert result["serverInfo"]["name"] == "test"
	assert result["instructions"] == "Adds numbers."


def test_a_version_this_server_does_not_speak_is_answered_with_one_it_does (
	server: subroutine.mcp.protocol.Server,
) -> None:
	"""The specification puts the decision to continue on the client, not on us.

	Refusing outright would make a client that could have downgraded fail instead.
	"""

	answered = _exchange(
		server,
		{
			"jsonrpc": "2.0",
			"id": 1,
			"method": "initialize",
			"params": {"protocolVersion": "1999-01-01"},
		},
	)

	assert answered[0]["result"]["protocolVersion"] == subroutine.mcp.protocol.PROTOCOL_VERSION


def test_a_notification_is_answered_with_nothing_at_all (
	server: subroutine.mcp.protocol.Server,
) -> None:
	"""A notification has no ``id`` and expects no reply.

	Replying is not a harmless extra: the client is not waiting for it, so an unmatched
	response is a protocol error at the other end. ``notifications/initialized`` is the one
	every client sends, so getting this wrong breaks every session at the handshake.
	"""

	assert _exchange(server, {"jsonrpc": "2.0", "method": "notifications/initialized"}) == []


def test_an_unknown_notification_is_ignored_rather_than_refused (
	server: subroutine.mcp.protocol.Server,
) -> None:
	"""There is nowhere to send a refusal, and a client may tell us things we do not act on."""

	assert _exchange(server, {"jsonrpc": "2.0", "method": "notifications/cancelled"}) == []


def test_tools_are_listed_with_their_schemas (
	server: subroutine.mcp.protocol.Server,
) -> None:
	"""``tools/list`` is how a client learns what exists, and the schema is the contract."""

	answered = _exchange(server, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
	tools = answered[0]["result"]["tools"]

	assert [tool["name"] for tool in tools] == ["add"]
	assert tools[0]["inputSchema"]["type"] == "object"


def test_a_tool_that_works_answers_with_text (
	server: subroutine.mcp.protocol.Server,
) -> None:
	"""The ordinary case."""

	answered = _exchange(
		server,
		{
			"jsonrpc": "2.0",
			"id": 1,
			"method": "tools/call",
			"params": {"name": "add", "arguments": {"a": 2, "b": 3}},
		},
	)
	result = answered[0]["result"]

	assert result["content"] == [{"type": "text", "text": "5"}]
	assert result["isError"] is False


def test_a_tool_that_raises_is_a_result_and_not_an_error (
	server: subroutine.mcp.protocol.Server,
) -> None:
	"""**The distinction this implementation most needed to get right.**

	A JSON-RPC error is for the *protocol* — unknown method, malformed params. A tool that
	fails is a successful response carrying ``isError: true``, because "there is no task
	#900" is an answer the model is meant to read and act on. Sent as a JSON-RPC error it is
	handled by the client and never reaches the model, which then has no idea why nothing
	happened.
	"""

	answered = _exchange(
		server,
		{
			"jsonrpc": "2.0",
			"id": 1,
			"method": "tools/call",
			"params": {"name": "add", "arguments": {"a": 1}},
		},
	)

	assert "error" not in answered[0], "a failing tool must not be a protocol error"

	result = answered[0]["result"]

	assert result["isError"] is True
	assert result["content"][0]["text"], "a failure with no message tells the model nothing"


def test_an_unknown_tool_is_a_protocol_error (
	server: subroutine.mcp.protocol.Server,
) -> None:
	"""The other side of the same rule: the client asked for something that does not exist."""

	answered = _exchange(
		server,
		{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope"}},
	)

	assert answered[0]["error"]["code"] == subroutine.mcp.protocol.INVALID_PARAMS


def test_an_unknown_method_is_refused_and_the_session_continues (
	server: subroutine.mcp.protocol.Server,
) -> None:
	"""One bad message must not end the session — the next one is answered normally."""

	answered = _exchange(
		server,
		{"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
		{"jsonrpc": "2.0", "id": 2, "method": "ping"},
	)

	assert answered[0]["error"]["code"] == subroutine.mcp.protocol.METHOD_NOT_FOUND
	assert answered[1]["result"] == {}


def test_a_line_that_is_not_json_does_not_end_the_session (
	server: subroutine.mcp.protocol.Server,
) -> None:
	"""A parse error is reported and the loop carries on reading."""

	incoming = io.StringIO('not json\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
	outgoing = io.StringIO()

	subroutine.mcp.protocol.serve(server, incoming, outgoing)

	answered = [json.loads(line) for line in outgoing.getvalue().splitlines() if line]

	assert answered[0]["error"]["code"] == subroutine.mcp.protocol.PARSE_ERROR
	assert answered[1]["result"] == {}


def test_every_message_is_one_line (server: subroutine.mcp.protocol.Server) -> None:
	"""The stdio transport is newline-delimited, so a message may not contain a raw newline.

	A tool whose text has newlines in it — every one of ours does — must still produce a
	single line on the wire, which is a property of the JSON encoding rather than of the
	tool. Asserted because a framing bug looks like a hung client rather than like itself.
	"""

	multiline = subroutine.mcp.protocol.Tool(
		name="lines",
		title="Lines",
		description="Returns several lines.",
		schema={"type": "object"},
		call=lambda _arguments: "one\ntwo\nthree",
	)
	built = subroutine.mcp.protocol.Server([multiline], name="test", version="0")

	incoming = io.StringIO(
		json.dumps(
			{
				"jsonrpc": "2.0",
				"id": 1,
				"method": "tools/call",
				"params": {"name": "lines", "arguments": {}},
			}
		)
		+ "\n"
	)
	outgoing = io.StringIO()

	subroutine.mcp.protocol.serve(built, incoming, outgoing)

	assert len(outgoing.getvalue().strip().splitlines()) == 1

	answered = json.loads(outgoing.getvalue())

	assert answered["result"]["content"][0]["text"] == "one\ntwo\nthree"


@pytest.fixture
def bound (
	session: sqlalchemy.orm.Session,
) -> typing.Iterator[subroutine.mcp.protocol.Server]:
	"""A server whose tools reach a real database through the ordinary local client.

	Built the way ``test_transport_equivalence`` builds its local client — ``factory_for``
	rather than a lambda returning the outer session, and the client entered as a context
	manager so it disposes of what it opened. A factory handing out one shared session let
	the client's commits escape the test's rollback, so refs carried into the next test and
	one of them saw a database with no accounts in it.
	"""

	subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	session.flush()

	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True),
		session_factory=api_support.factory_for(session),
	)

	with client:
		yield subroutine.mcp.protocol.Server(
			subroutine.mcp.tools.catalogue(client), name="subroutine", version="0"
		)


def _added (server: subroutine.mcp.protocol.Server, text: str) -> int:
	"""Capture a line and return the ref it was given.

	Read back rather than assumed: a ref is allocated per workspace and never reused, so
	"the first task is #1" is only true of the first test to run and is exactly the
	positional assumption §6.2 exists to remove.
	"""

	answered, failed = _called(server, "subroutine_add", text=text)

	assert not failed, answered

	return int(answered.split()[1].lstrip("#"))


def _called (
	server: subroutine.mcp.protocol.Server, name: str, **arguments: typing.Any
) -> tuple[str, bool]:
	"""Call one tool and return its text and whether it failed."""

	answered = _exchange(
		server,
		{
			"jsonrpc": "2.0",
			"id": 1,
			"method": "tools/call",
			"params": {"name": name, "arguments": arguments},
		},
	)
	result = answered[0]["result"]

	return result["content"][0]["text"], result["isError"]


def test_a_captured_line_becomes_a_task (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""``subroutine_add`` takes one line, which is the whole argument for its schema.

	Ten typed properties would be ten properties of schema in every session's context. §6.13's
	grammar is published, tested and already what a person types, so the tool reuses it and
	its schema is one string.
	"""

	text, failed = _called(bound, "subroutine_add", text="Fix the boiler !4/2 ~2h")

	assert not failed
	assert "Fix the boiler" in text
	assert "!4/2" in text, "the parsed priority is reported, not silently applied"
	assert "2h" in text


def test_a_listing_can_be_ranked (bound: subroutine.mcp.protocol.Server) -> None:
	"""The reason SR#65 was done first: a list tool that cannot rank answers the wrong question.

	An agent's first question is what to work on, and until the client layer took ``order``
	the only possible answer was "the most recently created thing".
	"""

	_added(bound, "Trivial !1/1")
	_added(bound, "Urgent !5/5")

	text, failed = _called(bound, "subroutine_list", order="-priority_score")

	assert not failed

	lines = text.splitlines()

	assert "Urgent" in lines[0], f"ranking did not apply: {text}"


def test_a_limit_bounds_the_answer_rather_than_each_kind (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""Asking for three and receiving three tasks *and* three documents spends the budget twice.

	For an agent the limit is the whole cost of the call, so a listing that honours it per
	kind has ignored it.
	"""

	for index in range(5):
		_added(bound, f"Task {index}")

	text, _failed = _called(bound, "subroutine_list", limit=3)

	assert len(text.splitlines()) == 3


def test_an_item_can_be_read_by_ref_with_or_without_the_sigil (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""A model reads ``#42`` everywhere this system writes an address, so it will send it back.

	Refusing its own notation over a sigil would be a refusal the caller cannot learn from
	(§6.2).
	"""

	ref = _added(bound, "Something findable")

	plain, failed = _called(bound, "subroutine_show", ref=ref)
	sigil, also_failed = _called(bound, "subroutine_show", ref=f"#{ref}")

	assert not failed and not also_failed
	assert "Something findable" in plain
	assert plain == sigil


def test_reading_something_that_is_not_there_is_an_answer_not_a_crash (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""And it names the ref, so the model can tell a typo from an empty backlog."""

	text, failed = _called(bound, "subroutine_show", ref=9999)

	assert failed
	assert "9999" in text


def test_a_comment_is_recorded_and_read_back (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""§5.10's record of what happened, which is the point of an agent having a tool at all."""

	ref = _added(bound, "Investigate the flake")
	_recorded, failed = _called(
		bound, "subroutine_comment", ref=ref, body="Reproduced on PostgreSQL only."
	)

	assert not failed
	assert "Reproduced on PostgreSQL only." in _called(bound, "subroutine_show", ref=ref)[0]


def test_a_task_can_be_finished (bound: subroutine.mcp.protocol.Server) -> None:
	"""And it leaves the open listing, which is what "done" has to mean."""

	ref = _added(bound, "Ship it")
	text, failed = _called(bound, "subroutine_done", ref=ref)

	assert not failed
	assert "Ship it" in text
	assert "Ship it" not in _called(bound, "subroutine_list")[0]


def test_the_whole_tool_surface_stays_small (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""**A tool surface is context every session carries whether it is used or not.**

	That is the measurement ``SR#14`` was really about, and the one that made its stated
	rationale wrong: Beads found 10-50k tokens via MCP against 1-2k via a CLI, so a tool per
	endpoint spends the benefit before earning it. This server answers with five tools whose
	arguments lean on grammars that already exist.

	The number here is a budget, not a description. If a tool is worth adding it is worth
	raising deliberately — and worth noticing that the cost is paid by every session of every
	agent, including the ones that never call it.
	"""

	answered = _exchange(bound, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
	tools = answered[0]["result"]["tools"]

	assert len(tools) <= 6, "the surface has grown; is each new tool worth every session?"

	size = len(json.dumps(tools))

	assert size < 4096, f"the tool schemas are {size} bytes of every session's context"


def test_a_task_can_be_re_ranked (bound: subroutine.mcp.protocol.Server) -> None:
	"""The difference between an agent that can do work and one that can manage it.

	Without this an agent could add findings and never rank them — and an unranked item
	sorts below everything, looking judged rather than unassessed (§6.3a), so every finding
	it recorded would be buried by the act of recording it.
	"""

	ref = _added(bound, "Something to reconsider")
	text, failed = _called(bound, "subroutine_update", ref=ref, importance=5, urgency=4)

	assert not failed
	assert "!5/4" in text

	assert "!5/4" in _called(bound, "subroutine_show", ref=ref)[0]


def test_an_estimate_takes_the_same_grammar_as_a_captured_line (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""``"4h"`` here and ``~4h`` there are one grammar (§6.4), not two spellings to learn."""

	ref = _added(bound, "Size this")
	text, failed = _called(bound, "subroutine_update", ref=ref, estimate="1h30m")

	assert not failed
	assert "1h 30m" in text


def test_changing_nothing_is_refused_rather_than_reported_as_success (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""An agent that meant to change something and named no field has made a mistake.

	A cheerful "unchanged" would hide it, and the model would carry on believing the backlog
	says something it does not.
	"""

	ref = _added(bound, "Untouched")
	text, failed = _called(bound, "subroutine_update", ref=ref)

	assert failed
	assert "importance" in text, "the refusal must say what it would have accepted"


def test_a_priority_outside_the_range_is_refused_with_the_range (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""The service layer's bound, reaching the model as something it can act on.

	§6.3's 1-5 was held only by a CHECK constraint until 2026-07-30, which made ``6`` a 500
	with no field named. Through a tool that would be worse: an agent reads the failure and
	has nothing to correct.
	"""

	ref = _added(bound, "Out of range")
	text, failed = _called(bound, "subroutine_update", ref=ref, importance=9)

	assert failed
	assert "importance" in text
