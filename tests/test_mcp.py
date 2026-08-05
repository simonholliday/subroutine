"""The MCP server: the wire, and the tools on it.

Two halves tested apart, because they can be wrong independently. The protocol tests use
tools that do arithmetic, so a failure means the *messages* are wrong; the tool tests drive
a real client against a real database, so a failure means *Subroutine* is wrong.

The message shapes here were taken from the published specification (2025-06-18) rather than
from memory, and the ones that are easy to get wrong are asserted rather than assumed: a
notification gets no answer at all, and a tool that fails is a successful response carrying
``isError`` rather than a JSON-RPC error.
"""

import ast
import io
import json
import os
import pathlib
import re
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.cli.main
import subroutine.clients.http
import subroutine.clients.local
import subroutine.clients.opening
import subroutine.config
import subroutine.connections
import subroutine.context
import subroutine.db.models.project
import subroutine.directory
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.capture
import subroutine.domain.documents
import subroutine.domain.workspaces
import subroutine.mcp.protocol
import subroutine.mcp.session
import subroutine.mcp.tools
import subroutine.permissions


def _adding (name: str = "add") -> subroutine.mcp.protocol.Tool:
	"""Return a tool that adds two numbers, for testing the wire and nothing else."""

	return subroutine.mcp.protocol.Tool(
		name=name,
		title="Add",
		description="Add two numbers.",
		# Both, because `#379` refuses an argument a tool does not declare — and this fixture
		# declared `a` while being called with `a` and `b`. It passed for as long as nothing
		# looked, which is the defect it now helps test.
		schema={
			"type": "object",
			"properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
		},
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
	"""One bad message must not end the session — the next one is answered normally.

	**The method named here is deliberately fictional.** This asked for ``resources/list`` until
	`#483` implemented it, at which point the test was asserting that a method we now support is
	refused — it failed, correctly, and said so. A name from the protocol is a name that may
	become real; one that never can is what this test actually needs.
	"""

	answered = _exchange(
		server,
		{"jsonrpc": "2.0", "id": 1, "method": "subroutine/not-a-method"},
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


def test_a_refusal_reaches_the_agent_with_its_remedy_attached (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#165`. The hint was being dropped, on every refusal this surface makes.

	``str()`` on a ``SubroutineError`` is its detail alone, so a model got the complaint and
	never the sentence saying what to do about it. A person can run ``--help`` and try again;
	a model gets one answer and either guesses from it or gives up — which is the difference
	between a tracker an agent recovers from and one it abandons.
	"""

	ref = _added(bound, "Chase the invoice")
	answered, failed = _called(bound, "subroutine_update", ref=ref, plan="next quarter")

	assert failed

	# What is wrong, and then what to do about it. One line means the remedy was lost again.
	assert "next quarter" in answered
	assert "friday" in answered, answered


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


def test_an_agent_can_find_something_by_its_words (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#149`. ``client.tasks`` took ``q`` and the tool did not, so an agent could only list.

	Invisible to `#148`'s guard by construction: both surfaces call ``tasks``, and reach
	there is per method. This is the argument-level half that has to be asserted directly.
	"""

	_added(bound, "Fix the date parser")
	_added(bound, "Paint the shed")

	# **`subroutine_search`, which is the tool that declares `q`.** This asked
	# `subroutine_list`, which shares the same reader and honoured it — so the behaviour was
	# real and undiscoverable, since that tool advertises no such argument. `#379` refuses it
	# now, and the test was the thing depending on the swallow.
	found, failed = _called(bound, "subroutine_search", q="parser")

	assert not failed, found
	assert "Fix the date parser" in found
	assert "Paint the shed" not in found


def test_an_agent_can_say_what_blocks_what (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#149`, and the contradiction that made it more than thinness.

	``subroutine_list(ready=true)`` has always filtered on ``blocks`` links — so the tool
	offered a question whose answer depended on something the same surface could not say.
	"""

	first = _added(bound, "Build the endpoint")
	second = _added(bound, "Write the client")

	made, failed = _called(
		bound, "subroutine_link", ref=first, type="blocks", other=second, workspace=None
	)

	assert not failed, made

	ready = _called(bound, "subroutine_list", ready=True)[0]

	assert "Build the endpoint" in ready
	assert "Write the client" not in ready, "a blocked task is not ready"

	# And withdrawing it, which ships with making it — an unwanted link narrows what looks
	# startable and says nothing about having done so.
	withdrawn, failed = _called(
		bound, "subroutine_link", ref=first, other=second, remove=True
	)

	assert not failed, withdrawn
	assert "Write the client" in _called(bound, "subroutine_list", ready=True)[0]


def test_an_agent_can_defer_with_the_grammar_a_person_types (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#149`. Decision `#96` says an external wait *is* a defer, written on 2026-07-31 for a
	surface that could not set one until 2026-08-01.

	The day is read by ``domain.schedule.interpret_day`` — the same grammar `subroutine defer`
	takes — because an agent working from a conversation has "next tuesday" in front of it,
	and making it convert would be asking it to reimplement a parser this product publishes.
	"""

	ref = _added(bound, "Chase the invoice")
	answered, failed = _called(bound, "subroutine_update", ref=ref, defer="2026-12-01")

	assert not failed, answered

	# Deferred is hidden from what can be started, which is what makes the reason worth having.
	assert "Chase the invoice" not in _called(bound, "subroutine_list", ready=True)[0]

	brought_back, failed = _called(bound, "subroutine_update", ref=ref, defer="")

	assert not failed, brought_back
	assert "Chase the invoice" in _called(bound, "subroutine_list", ready=True)[0]


def test_a_day_that_is_not_a_day_is_refused_by_name (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""An agent gets the same answer a person does, rather than a traceback or a silent no-op.

	The silent case is the one worth pinning: a defer that quietly did nothing would leave
	the agent believing an item was hidden and the person seeing it in their list. The words
	are the domain's, so the two surfaces cannot drift into two explanations of one refusal.
	"""

	ref = _added(bound, "Chase the invoice")
	answered, failed = _called(bound, "subroutine_update", ref=ref, plan="next quarter")

	assert failed
	assert "next quarter" in answered
	assert "understands" in answered


def test_an_agent_can_ask_what_is_on_today (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#149`. ``today`` is the question this product is built around and MCP had no tool.

	An argument on ``subroutine_list`` rather than a tool of its own, and flat rather than in
	the four buckets: the buckets are a *terminal* structure, and a model reading four
	headings for what is usually four rows is paying for the headings.
	"""

	ref = _added(bound, "Ring the dentist")

	_added(bound, "Rewrite the importer one day")

	_called(bound, "subroutine_update", ref=ref, plan="today")

	on_today, failed = _called(bound, "subroutine_list", today=True)

	assert not failed, on_today
	assert "Ring the dentist" in on_today

	# **The agenda's fourth bucket is left out, and this is what says so.** `unscheduled` is
	# the terminal's filler — "your day is empty, here is some backlog", capped at twenty —
	# and none of it is on today. Including it answered the question with the whole backlog,
	# which is what running this against the real instance showed before anybody read it.
	assert "Rewrite the importer" not in on_today


def test_an_agent_can_make_and_list_a_project (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#149`, and the last of the skill's shell-outs.

	SPEC §21.5's adoption asks what exists and then adds to it, which is why one tool does
	both: the two questions arrive together and a second name would be schema spent on the
	seam between them.
	"""

	made, failed = _called(
		bound, "subroutine_project", key="WEB", title="Website redesign"
	)

	assert not failed, made
	assert "WEB" in made

	listed, failed = _called(bound, "subroutine_project")

	assert not failed, listed
	assert "Website redesign" in listed


def test_a_project_named_without_a_title_is_refused (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""One tool doing two jobs has to say which one it thought it was being asked for.

	A key with no title is a create that cannot finish, not a list — answering with the
	listing would be the tool quietly doing something else.
	"""

	answered, failed = _called(bound, "subroutine_project", key="WEB")

	assert failed
	assert "title" in answered


def test_an_agent_can_read_what_has_happened_to_an_item (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#150`, on the surface where the cost of always printing it would be highest.

	An argument rather than a tool, and off by default: a history is unbounded where a comment
	list is bounded by what somebody typed, and most items have one event saying they exist.
	"""

	ref = _added(bound, "Fix the parser")

	_called(bound, "subroutine_update", ref=ref, importance=4)
	_called(bound, "subroutine_comment", ref=ref, body="ran the suite")

	plain, failed = _called(bound, "subroutine_show", ref=ref)

	assert not failed, plain
	assert "changed importance" not in plain

	shown, failed = _called(bound, "subroutine_show", ref=ref, history=True)

	assert not failed, shown
	assert "created" in shown
	assert "changed importance" in shown
	assert "commented" in shown


def test_the_whole_tool_surface_stays_small (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""**A tool surface is context every session carries whether it is used or not.**

	That is the measurement ``SR#14`` was really about, and the one that made its stated
	rationale wrong: Beads found 10-50k tokens via MCP against 1-2k via a CLI, so a tool per
	endpoint spends the benefit before earning it. The arguments here lean on grammars that
	already exist, which is what keeps the schemas small.

	**The numbers are a budget, not a description**, and raising one is meant to be an act
	rather than a shrug. Raised twice:

	- **2026-07-31, 6 tools / 4,096 -> 7 / 4,608**, for ``subroutine_document`` (`#138`).
	  ``subroutine_comment`` already told agents "for a conclusion the next session needs,
	  write a document instead" while no tool could. Measured at 764 bytes against 557 of
	  headroom before the cap moved.
	- **2026-08-01, 7 / 4,608 -> 9 / 6,144**, for `#149`, and the case is worth keeping
	  because it is the first time the budget was weighed against a *measured* alternative
	  rather than against a preference.

	  `#146` measured all three surfaces: an agent could not search, could not defer, could
	  not say what blocks what, could not ask what was on today, and could not make or list a
	  project. Four of those are not thinness, they are contradictions — ``subroutine_list``
	  offers ``ready=true``, which filters on exactly the links the agent could not create,
	  and decision `#96` says an external wait *is* a defer the agent could not set.

	  **The alternative was not free, which is what settles it.** The skill was already
	  telling agents to shell out to the CLI for three of these, so the cost was being paid
	  in Bash calls and in skill text every session — and paid by the agent least able to
	  recover when the shell-out failed. 4,428 -> 6,111 bytes is roughly 1,100 -> 1,530
	  tokens: about 430 more per session, against five capabilities and a workaround the
	  skill no longer has to teach.

	  **Fat was read for first, and there was less than expected.** ``update``'s description
	  lost 90 bytes of priority teaching that belongs in the skill. The ``workspace``
	  property, nine identical copies costing 434 bytes, was named once in the source — which
	  stops it drifting and **saves nothing on the wire**, because the dict is serialised in
	  full each time. Only ``$defs`` would cut it, and a client that does not resolve a
	  reference would show a property with no description at all.

	- **2026-08-02, 9 / 6,144 -> 10 / 6,912**, for ``subroutine_changes`` (`#253`). Measured
	  at **705 bytes**, which is roughly 175 tokens a session.

	  **The case is unlike the other two, and worth keeping for that reason: there was no
	  alternative being paid for elsewhere.** Both earlier raises replaced a shell-out the
	  skill was already teaching. Here the skill could not tell an agent to run a CLI command
	  either, because until the same day there was none — so the capability was not expensive,
	  it was absent.

	  **What settles it is that its absence is self-concealing.** Every other missing tool
	  announces itself: an agent that cannot create a project finds out when it tries. An agent
	  that cannot ask what changed does not discover a gap — it answers from a snapshot and is
	  confident. That happened on 2026-08-02, which is what `#13` was raised from: Simon closed
	  `#85`, asked whether I would have noticed unprompted, and I would not have. No amount of
	  care substitutes, because the failure is invisible from the inside.

	  **Fat was read for first and none was taken.** The nine existing schemas were re-read
	  against `#149`'s pass, which had already removed the priority teaching from ``update``
	  and named the ``workspace`` property once. What is left is argument descriptions a
	  client shows a model, and cutting those trades context for a worse call.

	- **2026-08-02, 10 / 6,912 -> 11 / 7,424**, for ``subroutine_search`` (`#282`), Simon's
	  decision: *"MCP should have a search verb like the CLI. `list -q` is clumsy."*

	  Measured at **+328 bytes** net — the tool costs 406 and taking ``q`` off
	  ``subroutine_list`` returns 80. Roughly 82 tokens a session.

	  **This is the weakest of the four cases and is recorded as such.** The other three
	  bought a capability that was absent. This one was present: an agent could already search
	  with ``list(q=…)``, and the skill never taught a shell-out because it never had to. So
	  the bytes are not buying the ability to search.

	  **What they buy is that the capability is findable, and that the two surfaces agree.** A
	  model deciding what it can do reads tool *names* — a capability parked in one parameter
	  of another tool's schema is discoverable only by reading every schema in full, which is
	  what a model reliably does not do. And a Claude Code agent working through the CLI cold
	  hit this from the other side: it learned ``q`` here, tried ``list -q``, ``list --search``
	  and ``list words``, and got three refusals naming neither ``search`` nor each other.
	  Two surfaces disagreeing about a verb's name is the family `#276` and `#278` are in — a
	  true statement on one surface that misleads about the system.

	  **Fat was read for first and none was taken, for the second time running.** ``update``
	  (1,069 bytes, the largest) is argument descriptions after `#149` stripped its priority
	  teaching; cutting those trades context for a worse call, which is the trade this budget
	  exists to avoid making by accident.

	* ``subroutine_whoami`` (`#347`), 2026-08-03, **323 bytes** — the twelfth, and the only one
	  of the five admitted after being argued *against* in writing. `#336` excused it from
	  this surface that morning on the grounds that a tool would report the credential the
	  connection already implies, so it would spend budget restating a fact every session
	  carries.

	  **That reason was measured false the same day** (`#346`). An agent on `nuc14` predicted
	  the same thing on the same reasoning — one connection, one command, one credentials file
	  — and then wrote two comments, one through each transport, which carried two different
	  *accounts*: a bounded service account through the tools, a superuser through its shell.
	  Its correction is the sentence this tool exists for: **a shared connection name does not
	  imply a shared principal.**

	  **What the bytes buy is that the question is answerable without a side effect.** Before
	  this, an agent could learn its own identity here only by writing to a real item and
	  reading the author back. An identity check whose method is a write to production is one
	  nobody performs before their first write, which is the moment it is worth anything —
	  and the half that turns out to be misattributed is the *privileged* half, so the failure
	  is silent and looks like a correctly bounded agent.

	  Considered instead and rejected: putting it in §21.3's server instructions, which cost
	  the same bytes and arrive unasked. They are built at startup, so reporting identity there
	  means a round trip before the server can serve — an instance that is down would stop the
	  server starting rather than stopping one call.

	  **Fat was read for first and none was taken, for the third time running.** ``update`` is
	  still the largest and is still argument descriptions doing work.

	* ``subroutine_claim`` (`#350`), 2026-08-03, **580 bytes** — the thirteenth, and the second
	  raise in one day, which is worth saying rather than sliding through. `#338` closed that
	  morning and gave every agent a distinct, bounded identity; identity answers *afterwards*
	  who did what, and does nothing about two workers taking the same item off the same ranked
	  listing. ``list(ready=true, order='-priority_score')`` deliberately answers the same for
	  everybody, so two agents asking the obvious question collide by construction — and the
	  cost is not a merge conflict, which git handles, but two of them doing the same work and
	  one finding out at the end.

	  **One tool with a ``release`` flag rather than two**, decided against `#149`'s lesson
	  rather than in ignorance of it. That lesson is that a capability parked in another tool's
	  argument is undiscoverable, because a model reads tool *names*. What makes this different
	  is that taking and giving back are one capability in two directions, named together in a
	  description read whole: an agent that has found "take a task" has found how to give it
	  back, in a way it could never have found ``list(q=…)`` from a tool called ``list``. Two
	  tools measured at roughly two hundred bytes more, for a verb called rarely and only after
	  this one.

	  **Fat was read for first and none was taken, for the fourth time running.**

	* **`#367`, to 8,500** — a `project` argument on `list`, `search` and `document`, which is
	  a *capability* rather than a tool: `subroutine list --project` has always existed and no
	  agent could ask, so one that wanted to spend its context on a single project had to read
	  the whole workspace and discard most of it. That is §13.1's concern paying for itself,
	  and it is `#149`'s blind spot for the third time — this file's reach guard compares
	  surfaces per *method*, and an argument on a method both already call is invisible to it.

	  **Measured at 23 bytes over the old cap**, which is the case worth stating plainly: the
	  addition cost about 80 bytes and the slack had already been spent. Trimming a
	  description by nine characters would have fitted it under 8,250, and that is precisely
	  the theatre the paragraph below warns about — a cap you edit prose to satisfy has
	  stopped measuring anything. Raised deliberately instead.

	* **`#400`, to 8,800** — ``remove`` on ``subroutine_comment``, so that a comment can be
	  taken back out. Asked for by the agent working in ``SUBSAMPLE``, which had written four
	  comments before descriptions worked and could not remove the three that were now pure
	  duplication: *"I'd have removed them if I could."*

	  **A boolean rather than a tool, and that is ``subroutine_link``'s precedent rather than a
	  saving.** ``#141`` settled that withdrawing ships with making and that a second tool
	  would spend a name and a schema on the inverse of a verb the caller has already found;
	  ``link(remove=true)`` has read that way since. Measured at about ninety bytes against
	  four hundred for a tool.

	  **The argument that decided it: a comment is the only thing these tools write that
	  cannot be taken back.** ``link`` withdraws, ``update`` sets any field to anything, a
	  claim is released. An agent that cannot correct its own record either leaves the
	  duplication in — which is what happened — or writes fewer comments, and the second is
	  the expensive failure, because the record of what happened is most of what makes an
	  agent's work auditable.

	  **Fat was read for first and none was taken, for the fifth time running** — and this
	  time the temptation was concrete. ``update``'s ``description`` field carries the sentence
	  *"This is where the reasoning behind an outcome-shaped title goes"*, about sixty bytes,
	  and `CLAUDE.md` says reasoning belongs in the skill rather than in a schema. Cutting it
	  would have fitted this under the old cap exactly. That is the theatre named above,
	  wearing a rule as a disguise: the edit would have been made *because* the number was 52
	  over, and the sentence is teaching at the point of use for the one field an agent was
	  measured getting wrong (`#392`).

	* **`#489`, to 9,500** — tool *annotations*, which is the first raise that buys no
	  capability at all. Simon's decision, 2026-08-05, asked for after the measurement below.

	  **What it buys is that the surface stops lying about itself.** No tool declared
	  annotations, and the specification tells a client to read an unannotated tool as
	  potentially **destructive, non-idempotent and open-world** — so silence was not
	  neutrality, it was the worst available claim, made about five tools that only read. Clients
	  increasingly turn exactly these hints into approval prompts, which puts the cost on an
	  agent's *first* calls: the exploratory ones it makes before it knows what this instance is.
	  That is the moment §1.4 cares most about, and the one nothing else in this file protects.

	  **Measured at +453 bytes**, roughly 110 tokens a session — and the first figure was
	  **591**. The difference is the fat, and it was in the addition rather than in the existing
	  schemas: ``readOnlyHint: false`` is already the protocol's default, so declaring it on the
	  six writing tools cost 132 bytes to repeat what the *absence* of ``READS`` says. An
	  annotation that changes no client's behaviour is context spent on nothing.

	  **Fat was read for in the old schemas too, for the sixth time running, and none was
	  taken.** ``subroutine_update`` is still the largest at 1,214 bytes and is still ten
	  argument descriptions doing work. What is worth recording is that the *fat existed and was
	  somewhere new* — this is the first raise where reading the existing schemas was the wrong
	  place to look, and the addition itself was where the waste was.

	  **Two hints are declared and two are not.** ``idempotentHint`` and ``openWorldHint`` are
	  accurate and buy no client behaviour that matters here, so they are not worth their bytes.
	  ``destructiveHint: false`` is claimed only where it is true — not for ``subroutine_update``
	  or ``subroutine_link``, which overwrite field values and withdraw links, and which
	  therefore keep the pessimistic default because it is *correct* for them rather than merely
	  unstated.

	  **This does not pre-buy `#485`.** ``call_api`` is 500-700 bytes on its own estimate and
	  gets its own act with its own measurement, because a cap raised for work not yet done is
	  a budget for something nobody has weighed.

	The slack above the current total is deliberate and small — **312 bytes** as of 2026-08-05,
	which is about one description. A cap set exactly at what is there makes every addition a
	cap change, which is theatre; a generous one stops being a budget.

	**`#424` spent 104 of it and the cap did not move, which is the slack doing its job.**
	``subroutine_add`` gained a ``description``, because `#392` had put one on
	``subroutine_update`` and thereby made a described item two calls on two tools — an agent
	reported that it had simply skipped the second, six times, and that its own titles were
	unreadable as a result. Worth recording that no fat was read for this time: the addition
	fitted, so looking would have been a ritual rather than a check.

	**That number is now stated as of a date, because the last one rotted.** It said 33 bytes,
	which was true when it was written and was 7 by the time anybody read it again — a title
	stating a condition becomes false when the condition changes, silently, which is the
	argument `#139` settled about item titles and applies to a comment just as well (`#198`).
	Do not trust it; run the test and read what it says.
	"""

	answered = _exchange(bound, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
	tools = answered[0]["result"]["tools"]

	assert len(tools) <= 13, "the surface has grown; is each new tool worth every session?"

	size = len(json.dumps(tools))

	assert size < 9500, f"the tool schemas are {size} bytes of every session's context"

	# **The shared `workspace` description's cost, measured here rather than asserted in a
	# comment** (`#361`). `mcp/tools.py` used to carry the figure in prose beside the constant
	# — "638 bytes across 11 tools as of 2026-08-03" — and it was 696 across 12 two commits
	# later, in the paragraph directly under that module's own note that a count belongs
	# somewhere it can fail. The claim being made is that this is a *small* share of the
	# surface and not worth `$defs`, so that is what is checked.
	repeated = sum(
		len(json.dumps(subroutine.mcp.tools.WORKSPACE))
		for tool in tools
		if "workspace" in tool["inputSchema"].get("properties", {})
	)

	assert repeated < size // 8, (
		f"the repeated workspace description is {repeated} of {size} bytes — past a share "
		f"where 'a repeated literal is cheaper than a reference nobody can be sure resolves' "
		f"is still the obvious answer"
	)


def _two_workspaces (
	session: sqlalchemy.orm.Session,
) -> tuple[subroutine.clients.local.Client, str, str]:
	"""Return a client on an instance holding two workspaces, and both their names.

	**Two, because one cannot fail.** `#333` was latent for as long as every instance had a
	single workspace: the parameter was never supplied and nothing noticed, because the one
	candidate always resolved. A fixture with one workspace reproduces exactly that blindness
	— every assertion below passes against a session that ignores the setting entirely.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	second = subroutine.domain.workspaces.create(
		session, slug="acme", title="Acme", owner=setup.user
	)
	session.flush()

	return (
		subroutine.clients.local.Client(
			subroutine.connections.Connection(name="local"),
			subroutine.config.Settings(dev_mode=True),
			session_factory=api_support.factory_for(session),
		),
		setup.workspace.slug,
		second.slug,
	)


def test_without_a_workspace_a_session_on_two_of_them_cannot_read_anything (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The baseline `#333` was found by, kept so the fix below has something to be a fix of.

	The refusal itself is good — it names both workspaces, which is how a caller recovers.
	What was missing is that there was anything to recover *from*: the CLI carries a workspace
	and a session did not.
	"""

	client, first, second = _two_workspaces(session)

	with client:
		server = subroutine.mcp.protocol.Server(
			subroutine.mcp.tools.catalogue(client), name="subroutine", version="0"
		)
		text, failed = _called(server, "subroutine_list")

	assert failed
	assert first in text and second in text, "and it says which names would work"


def test_a_session_bound_to_a_workspace_reads_and_writes_there (
	session: sqlalchemy.orm.Session,
) -> None:
	"""What the plugin's new setting buys, end to end through the wire."""

	client, first, second = _two_workspaces(session)

	with client:
		server = subroutine.mcp.protocol.Server(
			subroutine.mcp.tools.catalogue(client, workspace=second),
			name="subroutine",
			version="0",
		)

		added, failed = _called(server, "subroutine_add", text="Something in Acme")

		assert not failed, added

		listed, failed = _called(server, "subroutine_list")

		assert not failed
		assert "Something in Acme" in listed

		# **A default, not a pin.** An agent that has to read a decision filed next door can,
		# which is why this is not the same thing as narrowing the credential (§7.3).
		elsewhere, failed = _called(server, "subroutine_list", workspace=first)

		assert not failed
		assert "Something in Acme" not in elsewhere


def test_the_instructions_say_where_work_goes_only_when_a_session_was_told (
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Silent on a single-workspace instance, which is most of them (§1.4).

	An instruction about workspaces is context every session carries, and one that will only
	ever have a single workspace should not carry a sentence about the concept at all.
	"""

	roster = _roster("local", default="local")

	assert "Work goes to the 'acme' workspace" in _standing_up(
		monkeypatch, roster, workspace="acme"
	)
	assert "Work goes to" not in _standing_up(monkeypatch, roster)


def test_an_agent_can_ask_which_principal_it_is (
	bound: subroutine.mcp.protocol.Server, session: sqlalchemy.orm.Session
) -> None:
	"""`#347`. Before this the only method was writing to a real item and reading it back.

	The local client here holds no credential, which is §12.1a's ordinary case and is the
	answer worth being able to *state*: "the local database" rather than a token is a fact
	about how this session is authenticated, not a missing field.
	"""

	text, failed = _called(bound, "subroutine_whoami")

	assert not failed
	assert "si-" in text, "the account it acts as"
	assert "(person)" in text
	assert "the local database" in text, "and how, which is what tells two sessions apart"


def test_asking_who_you_are_says_what_a_narrow_credential_cannot_do (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The case the tool exists for: an agent bounded on purpose, told so before it writes.

	Built with its own server rather than the shared fixture, because the credential *is* the
	variable — a client with no token cannot show that scopes are reported, and the fixture
	deliberately has none.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session,
		user=setup.user,
		title="the agent",
		scopes=[subroutine.permissions.TASK_READ],
		workspace_id=setup.workspace.id,
	)
	session.flush()

	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True),
		session_factory=api_support.factory_for(session),
		token=issued.value.get_secret_value(),
	)

	with client:
		server = subroutine.mcp.protocol.Server(
			subroutine.mcp.tools.catalogue(client), name="subroutine", version="0"
		)
		text, failed = _called(server, "subroutine_whoami")

	assert not failed
	assert "the agent" in text, "named by its title, which is what `token list` shows"
	assert "Narrowed to" in text
	assert subroutine.permissions.TASK_READ in text

	# The secret never appears, in any form — the same rule every other surface keeps.
	assert issued.value.get_secret_value() not in text


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


def test_the_adapter_says_what_the_grammar_declined_to_read (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#115`: §6.13 rule 1 requires the caller be told, and this surface told nobody.

	The CLI had said so on both its human and its scripted path, with a note explaining that
	the agent is the caller most likely to have written something it believes was understood.
	The MCP adapter — the surface where every caller is an agent — said nothing at all, so a
	model writing "every monday" was told only that a task had been added.

	Not `isError`: the task *was* created, and reporting a success as a failure would be the
	opposite mistake.
	"""

	text, failed = _called(bound, "subroutine_add", text="Water the plants every monday")

	assert not failed, "the task was created, so this is a success"
	assert "every monday" in text
	assert "not supported yet" in text


def test_an_ordinary_capture_carries_no_extra_line (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""Context economy: a caption for the empty case would be paid on every capture."""

	text, failed = _called(bound, "subroutine_add", text="Fix the boiler by friday")

	assert not failed
	assert "\n" not in text


def test_both_surfaces_word_it_the_same_way () -> None:
	"""One sentence, in `domain.capture.explain`, because there are three callers.

	The CLI's human path, its `--json` path and this adapter all owe §6.13's obligation, and
	three copies of a sentence is three chances to word an obligation differently — which is
	the shape this codebase keeps finding.
	"""

	assert subroutine.domain.capture.explain(()) is None

	only = subroutine.domain.capture.explain(("every monday",))

	assert only is not None
	assert "every monday" in only

	several = subroutine.domain.capture.explain(("every monday", "every 3rd"))

	assert several is not None
	assert "every monday, every 3rd" in several


def test_a_page_of_nothing_is_refused_rather_than_answered_with_twenty (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`limit: 0` read as "unset" through `or DEFAULT_LIMIT`, and answered with twenty rows.

	Zero is a strange thing to ask for and a stranger thing to answer with a page. It now
	reaches `domain.paging.size`, the one arbiter of what a page may be, and is refused by
	name — the same refusal every other transport gives, rather than a second opinion.
	"""

	text, failed = _called(bound, "subroutine_list", limit=0)

	assert failed
	assert "0" in text


def test_a_record_is_read_by_when_rather_than_by_a_raw_id (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""A UUID per comment is thirty-six characters a model cannot resolve without another call.

	In the module whose entire design argument is that context is a fixed cost, that is the
	wrong thirty-six characters: reading an item's record, *when* something happened is what
	orders it, and *who* is usually the reader.
	"""

	ref = _added(bound, "Something to discuss")
	_called(bound, "subroutine_comment", ref=ref, body="what happened here")

	text, failed = _called(bound, "subroutine_show", ref=ref)

	assert not failed
	assert "what happened here" in text
	assert "-" * 4 not in text, "a UUID leaked into the record"


def test_add_tells_the_agent_what_the_grammar_read (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""``#135``, and this is the surface where it matters most.

	An agent is the caller most likely to have written something it believes was understood —
	the same reason ``#115`` put the *unparsed* sentence here. Being told only what was left as
	written answers the rarer half: an agent that captured ``+WEB`` and got no confirmation has
	to spend a second call reading the task back to find out whether it worked.
	"""

	answered, failed = _called(bound, "subroutine_add", text="Fix it !4/2 ~2h")

	assert not failed, answered
	assert "!4/2" in answered
	assert "~2h" in answered

	# And an ordinary line stays a single unadorned sentence, so the common case pays nothing.
	plain, failed = _called(bound, "subroutine_add", text="Buy milk")

	assert not failed, plain
	assert plain.count("\n") == 0, plain


def test_listing_ready_work_leaves_documents_out_of_it (
	bound: subroutine.mcp.protocol.Server, session: sqlalchemy.orm.Session
) -> None:
	"""``#136``. §6.14: a document is not scheduled and nothing blocks one.

	So every document would report as ready — true, and useless. On this instance that is
	every specification and every decision, which is enough of them to bury the tasks the
	caller asked about. The listing spans both kinds everywhere else by design (§6.2); this is
	the one question where that is the wrong answer.

	Written here rather than through the CLI because a document cannot be created from the CLI
	at all (``#138``) — which is itself the reason this test was worth chasing down.
	"""

	inbox = session.scalars(
		sqlalchemy.select(subroutine.db.models.project.Project).where(
			subroutine.db.models.project.Project.is_inbox
		)
	).one()

	subroutine.domain.documents.create(
		session,
		project=inbox,
		title="A conclusion somebody reached",
		body="Nothing blocks a document and nothing schedules one.",
	)
	session.flush()

	_added(bound, "A task somebody could start")

	everything, _failed = _called(bound, "subroutine_list")
	startable, _failed = _called(bound, "subroutine_list", ready=True)

	assert "A conclusion somebody reached" in everything, everything
	assert "A conclusion somebody reached" not in startable, startable
	assert "A task somebody could start" in startable, startable


def test_an_agent_can_write_the_document_it_is_told_to_write (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""``#138``, and the reason it was the worst of the three gaps.

	``subroutine_comment``'s own description has always said "for a conclusion the next
	session needs, write a document instead" — an instruction in the agent-facing surface
	pointing at something that surface could not do. This is the tool that makes the sentence
	true.
	"""

	answered, failed = _called(
		bound,
		"subroutine_document",
		title="Why the queue went",
		body="It added an operational surface nobody wanted.",
		type="decision",
	)

	assert not failed, answered
	assert "Why the queue went" in answered

	ref = int(answered.split()[1].lstrip("#"))
	read, failed = _called(bound, "subroutine_show", ref=ref)

	assert not failed, read
	assert "nobody wanted" in read


def test_an_agent_can_comment_on_the_document_it_just_wrote (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""``#145``, one layer down from ``#138`` and the same shape.

	``subroutine_comment`` passed no ``entity_type``, so it always meant "task" and answered
	"there is no task #4 here" about a document sitting in the listing. The tool beside it
	tells an agent to write documents and ``subroutine_show`` reads a document's record back —
	so the surface taught that documents take comments and then refused to write one.
	"""

	written, failed = _called(
		bound, "subroutine_document", title="Why the queue went", body="Nobody wanted it."
	)

	assert not failed, written

	ref = int(written.split()[1].lstrip("#"))
	answered, failed = _called(
		bound, "subroutine_comment", ref=ref, body="Reread it; the argument still holds."
	)

	assert not failed, answered

	read, failed = _called(bound, "subroutine_show", ref=ref)

	assert not failed, read
	assert "the argument still holds" in read


def test_a_ref_means_the_same_thing_to_every_tool_that_takes_one (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""The drift that produced ``#145``, guarded rather than fixed twice.

	One counter per workspace serves both kinds (§6.2), so a ref alone does not say which it
	is. ``show`` asked; ``comment`` assumed. They sat three hundred lines apart and nothing
	compared them, which is how a tool can be right about documents and the one beside it
	wrong about the same number.
	"""

	written, failed = _called(bound, "subroutine_document", title="A conclusion")

	assert not failed, written

	ref = int(written.split()[1].lstrip("#"))

	beside = _added(bound, "Something to point at")

	for tool, arguments in (
		("subroutine_show", {}),
		("subroutine_comment", {"body": "Something happened."}),
		# Added for `#491`. This loop asserted a document's ref is accepted as the *subject* of
		# every tool that takes one, which `subroutine_link` passed all along — its defect was
		# in the argument naming the other end, and a guard shaped around `ref` could not see it.
		("subroutine_link", {"type": "relates_to", "other": beside}),
	):
		_, failed = _called(bound, tool, ref=ref, **arguments)

		assert not failed, f"{tool} does not accept a document's ref"


def test_a_document_can_be_named_at_either_end_of_a_link (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#491`. The near end was resolved and the far end was assumed to be a task.

	``_linked`` asked ``_item`` which kind the *subject* was and passed it as ``entity_type``,
	then let ``client.link`` default ``target_type`` to ``"task"``. So naming a document as the
	other end answered *"There is no task '484' here"* — about an item the caller had just read
	in a listing. The CLI passes ``target_type=far.entity_type``; this surface did not.

	**One rule carried to one side of a pair**, which report `#412` found three times in one
	review, and `#149`'s blind spot on top: the capability is an *argument* on a method both
	surfaces already call, so ``test_reach`` compares the names, finds ``link`` on both, and is
	structurally unable to notice.

    Found by hitting it — filing an item that related to a decision document, which is the
    ordinary act in this workspace rather than an exotic one.
	"""

	task = _added(bound, "Build the endpoint")
	another = _added(bound, "Write the client")
	written, failed = _called(bound, "subroutine_document", title="Why we chose the queue")

	assert not failed, written

	document = int(written.split()[1].lstrip("#"))

	# The far end, which is the direction that was broken.
	made, failed = _called(
		bound, "subroutine_link", ref=task, type="relates_to", other=document
	)

	assert not failed, made
	assert f"#{document}" in made, "the answer names what it joined to"

	# The near end, which already worked. Asserted so that a fix resolving the far end *instead*
	# of the near one — rather than as well as — cannot pass this.
	made, failed = _called(
		bound, "subroutine_link", ref=document, type="relates_to", other=another
	)

	assert not failed, made

	# And withdrawing one, which goes through a different path: `links()` around the near end,
	# filtered by the far ref. Refs are unique per workspace across both kinds (§6.2), so that
	# filter needs no target type — verified rather than assumed, since assuming is what put
	# `target_type` wrong in the first place.
	withdrawn, failed = _called(
		bound, "subroutine_link", ref=task, other=document, remove=True
	)

	assert not failed, withdrawn


def test_the_comment_tool_still_points_at_something_that_exists (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""The guard on the sentence, not on the tool.

	``subroutine_comment`` tells an agent to write a document instead when it has a
	conclusion. That instruction was true of the product and false of this adapter for as long
	as both existed, and nothing could notice — a description is prose, and no test had ever
	asked whether prose in a schema named something callable.
	"""

	answered = _exchange(bound, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
	tools = {tool["name"]: tool for tool in answered[0]["result"]["tools"]}

	assert "write a document" in tools["subroutine_comment"]["description"].lower()
	assert "subroutine_document" in tools, (
		"subroutine_comment tells an agent to write a document; there must be a tool for it"
	)


def test_the_echo_is_told_apart_from_the_rank_beside_it (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""Item ``#426``, in its sharpest form — this surface prints the rank as well.

	``Added #2  task  !4/3  Stop the stamp...  !4/3`` put ``!4/3`` on the line twice, once as
	the item's priority and once as the token that set it, separated from the title and from
	each other by nothing but a double space. An agent reported that it liked the echo and
	could not read it.

	Asserted on the *whole* line rather than by substring, because the defect is entirely
	about what sits next to what — a containment check passes on both spellings.
	"""

	made, failed = _called(bound, "subroutine_add", text="Fix the header !4/2 ~2h")

	assert not failed, made

	head, separator, echoed = made.partition("  (read ")

	assert separator, f"the echo is not marked off at all: {made}"
	assert echoed == "!4/2 ~2h)", made
	assert head.endswith("Fix the header"), head

	# Both readings of the old line are now distinguishable: the rank is a bare cell, the
	# echo is named. Nothing else on the line looks like either.
	assert "!4/2" in head, "the rank is still rendered as a cell"


def test_add_carries_a_description_in_one_call (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""Item ``#424``, on the surface it was reported from.

	`#392` put a description on ``subroutine_update``, which left a described item as two calls
	on two tools. An agent filing its first real work on a fresh install skipped the second one
	six times and, asked why, gave the reason worth keeping: *"an agent weighing calls will
	systematically skip an optional second write, and the moment you have the most context
	about an item is when you file it."*

	The tool schema is a budget (§21.2) and this fitted inside it — 104 of the 248 bytes of
	slack, cap unmoved. That is what the slack is for.
	"""

	made, failed = _called(
		bound,
		"subroutine_add",
		text="Cache the connection roster !3/2",
		description="Measured at 400ms a call, four calls a listing.",
	)

	assert not failed, made

	shown, _ = _called(bound, "subroutine_show", ref=_numbered(made))

	assert "Cache the connection roster" in shown
	assert "Measured at 400ms a call" in shown


def test_a_marker_for_another_instance_is_ignored_rather_than_refused (
	bound: subroutine.mcp.protocol.Server, tmp_path: pathlib.Path
) -> None:
	"""`#232`. The failure was reachable by installing the plugin and cloning a team's repo.

	Committing ``.subroutine`` is what the file is *for* once more than one person shares an
	instance (`#159`, `#177`) — so a colleague clones the repository, runs ``subroutine init``
	to get an instance of their own, and the marker then names a project that is not on it.
	Until this, ``subroutine_add`` passed the marker's key to the server unresolved and every
	call came back "There is no project 'ELSEWHERE' here" with nothing filed, while the CLI
	beside it warned and carried on. `#166` had settled that a marker is advisory; only one
	surface had implemented it.

	Reads were never affected — ``list``, ``show`` and ``project`` all answered from the same
	directory — which is what made it a write-path bug rather than an unusable server, and is
	why nobody met it before the package was published.
	"""

	(tmp_path / subroutine.directory.FILE_NAME).write_text(
		'project = "ELSEWHERE"\n', encoding="utf-8"
	)
	os.chdir(tmp_path)

	text, failed = _called(bound, "subroutine_add", text="Filed anyway")

	assert not failed, text
	assert "Added" in text

	# And it says so, for the reason this function says everything else out loud: the agent is
	# holding a repository whose file claims one thing and an instance that says another.
	assert "ELSEWHERE" in text
	assert "Ignoring it" in text


def test_a_marker_for_another_connection_does_not_file_by_key (
	bound: subroutine.mcp.protocol.Server, tmp_path: pathlib.Path
) -> None:
	"""Item ``#414``. The marker names one instance; a same-named project here is not it.

	The test above covers a marker whose project is simply absent. This is the harder half, and
	the one that files work in the wrong place rather than merely ignoring the file: the marker
	names another connection, and **the project key it carries exists here too** — which is what
	``SR``, ``WEB`` and ``DOCS`` are like, and is the whole reason the defect bites. So this
	makes the project first; a marker naming one that is not here could not tell the two
	behaviours apart, and would pass against the defect.

	**Falsified against the original code**: drop ``marker.speaks_for(...)`` from ``_added``'s
	``consulted`` and this fails, reporting the task filed ``in WEB``.
	"""

	_called(bound, "subroutine_project", key="WEB", title="Website")

	(tmp_path / subroutine.directory.FILE_NAME).write_text(
		'connection = "somewhere-else"\nproject = "WEB"\n', encoding="utf-8"
	)
	os.chdir(tmp_path)

	text, failed = _called(bound, "subroutine_add", text="Filed where this session points")

	assert not failed, text
	assert "Added" in text

	# Not "in WEB, from .subroutine" — the marker was never consulted, so nothing about it is
	# reported as having decided anything.
	assert subroutine.directory.FILE_NAME not in text
	assert "in WEB" not in text


# --- Which instance a session is bound to ------------------------------------------------


def _roster (*names: str, default: str) -> subroutine.connections.Roster:
	"""Return a roster of the given connections, the first one local."""

	built = tuple(
		subroutine.connections.Connection(
			name=name, url=None if index == 0 else f"http://127.0.0.1:{8000 + index}"
		)
		for index, name in enumerate(names)
	)

	return subroutine.connections.Roster(connections=built, default=default)


def _standing_up (
	monkeypatch: pytest.MonkeyPatch,
	roster: subroutine.connections.Roster,
	workspace: str | None = None,
) -> str:
	"""Return the instructions ``build`` produces for this roster, without opening anything.

	Driven through ``build`` rather than by calling the private helper, so these assert what
	a session is actually told. Calling ``_instructions`` directly would fail against the
	previous code with a ``TypeError`` about its signature — evidence that the test matches
	the new shape, and none at all that it would have caught the old behaviour.
	"""

	monkeypatch.setattr(subroutine.connections, "roster", lambda settings: roster)
	monkeypatch.setattr(
		subroutine.clients.opening, "for_connection", lambda connection, roster, settings: None
	)
	monkeypatch.setattr(
		subroutine.mcp.tools, "catalogue", lambda client, workspace=None: []
	)

	built = subroutine.mcp.session.build(
		workspace=workspace, settings=subroutine.config.Settings(dev_mode=True)
	)

	assert built.instructions is not None, "a session is always told where it is"

	return built.instructions


def test_the_instructions_name_the_instances_this_session_cannot_reach (
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""``#276``. Naming only the bound one is what let an agent be sure it knew where it was.

	The sentence was true — it said 'on connection Local' — and nothing in it suggested the
	name was one of several, so there was no reason to ask. `subroutine connections` answers
	it from the command line and has no equivalent here, which is `#232`'s gap from the other
	side.
	"""

	said = _standing_up(monkeypatch, _roster("local", "work", default="local"))

	assert "work" in said
	assert "cannot reach" in said


def test_one_connection_is_told_nothing_about_connections (
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""The guard that keeps the sentence above off every session that will never need it.

	Instructions are context every session carries, so this is the same budget the tool
	schemas are held to — and the same rule that keeps ``subroutine connections`` out of
	``--help`` until a second connection exists.
	"""

	said = _standing_up(monkeypatch, _roster("local", default="local"))

	assert "cannot reach" not in said
	assert "configured here" not in said


def test_the_binding_does_not_follow_subroutine_use (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""``#276``, and the half that made it intermittent rather than merely undiscoverable.

	The fallback used to be the stored context, which a person moves between tasks — read
	once at startup and held for the whole session. So which instance an agent wrote to
	depended on where ``subroutine use`` happened to point at the unrelated moment its
	process started. Two sessions on one machine on one day bound different instances and
	neither could tell.

	``build`` is driven for real, with only the client-opening step stubbed — that needs a
	database and the question here is purely which connection is handed to it. A first
	version asserted ``(None or roster.default) == "local"``, which re-implemented the
	expression under test and would have passed with the defect still in place.
	"""

	roster = _roster("local", "work", default="local")
	handed: list[str] = []

	monkeypatch.setattr(
		subroutine.context, "read", lambda: {"connection": "work", "workspace": "acme"}
	)
	monkeypatch.setattr(subroutine.connections, "roster", lambda settings: roster)
	monkeypatch.setattr(
		subroutine.clients.opening,
		"for_connection",
		lambda connection, roster, settings: handed.append(connection.name),
	)
	monkeypatch.setattr(
		subroutine.mcp.tools, "catalogue", lambda client, workspace=None: []
	)

	# The stored context says 'work'. Asserted first, so a fixture that failed to set it
	# cannot let the real assertion pass for the wrong reason.
	assert subroutine.context.resolve(roster).connection == "work"

	subroutine.mcp.session.build(settings=subroutine.config.Settings(dev_mode=True))

	assert handed == ["local"], "the binding follows default_connection, not 'subroutine use'"


def test_search_is_a_verb_of_its_own (bound: subroutine.mcp.protocol.Server) -> None:
	"""``#282``, Simon's decision: *"MCP should have a search verb like the CLI."*

	The capability was never missing — ``subroutine_list`` took ``q``. What was missing was
	the *name*, and a model deciding what it can do reads tool names rather than every
	schema in full. A Claude Code agent hit the same disagreement from the other side: it
	learned ``q`` here and then failed three ways at the CLI.
	"""

	_added(bound, "Call the dentist")
	_added(bound, "Buy milk")

	text, failed = _called(bound, "subroutine_search", q="dentist")

	assert not failed, text
	assert "Call the dentist" in text
	assert "Buy milk" not in text, "a search that returns everything is a listing"


def test_search_without_words_says_so_rather_than_listing_everything (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""The schema requires ``q``; it cannot see a *blank* one.

	Answering "find nothing" with the whole backlog is the shape that costs an agent its
	context window and tells it something false about what matched.
	"""

	_added(bound, "Buy milk")

	text, _failed = _called(bound, "subroutine_search", q="   ")

	assert "Buy milk" not in text
	assert "look for" in text


def test_the_listing_no_longer_advertises_a_search_argument (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""One capability, one name. Leaving ``q`` on both would be the duplication `#282` removed.

	The CLI hides ``ls`` for the same reason — a synonym you can *see* is a second thing to
	choose between — and here neither could be hidden, because both are schemas a model reads.
	"""

	answered = _exchange(bound, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
	tools = {tool["name"]: tool for tool in answered[0]["result"]["tools"]}

	assert "q" not in tools["subroutine_list"]["inputSchema"]["properties"]
	assert "q" in tools["subroutine_search"]["inputSchema"]["properties"]
	assert tools["subroutine_search"]["inputSchema"]["required"] == ["q"]


def test_a_listing_can_be_narrowed_to_one_project (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#367`. `subroutine list --project` existed and no agent could ask.

	**Two projects, because one cannot fail.** A test with a single project passes whether or
	not the argument reaches the client at all — which is exactly how an argument comes to be
	declared, documented and never supplied, the shape `#333` was and the reason `_within`
	exists.

	Built through the tools rather than through the domain, because the question is whether an
	*agent* can do this, and a fixture that reached past the surface would answer a different
	one.

	The marker is deliberately not involved: §13.7 settles that context directs writes and
	never narrows what a caller can see, so this is a capability an agent chooses to use rather
	than a default it has to notice.
	"""

	for key in ("HERE", "ELSEWHERE"):
		_answer, failed = _called(bound, "subroutine_project", key=key, title=key.title())

		assert not failed, _answer

	_added(bound, "Work in this project +HERE")
	_added(bound, "Work in the other one +ELSEWHERE")

	everything, failed = _called(bound, "subroutine_list")

	assert not failed
	assert "Work in this project" in everything
	assert "Work in the other one" in everything, "both are visible without the argument"

	narrowed, failed = _called(bound, "subroutine_list", project="HERE")

	assert not failed
	assert "Work in this project" in narrowed
	assert "Work in the other one" not in narrowed, "the argument has to actually narrow"

	found, failed = _called(bound, "subroutine_search", q="Work", project="HERE")

	assert not failed
	assert "Work in this project" in found
	assert "Work in the other one" not in found, "and on search too, which shares the reader"


def test_the_instructions_send_a_session_to_the_skill_before_its_first_call (
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`#378`. An agent on its first contact had to be *told* to read the skill.

	It answered a capability question, then listed, searched, read an item and recommended what
	to file — all without opening it — and called a bare ``list()`` where ``ready=true`` is the
	whole point. Its own diagnosis of why is the reason this test exists rather than a reworded
	description alone:

	    "a paragraph of correct guidance in context makes the skill feel redundant"

	These instructions are in context for every session and they teach — refs, and the
	comment-versus-document rule. So the text best placed to point at the skill was the text
	making it look unnecessary. This is the one place a pointer is guaranteed to be read, which
	is why losing it silently would matter.

	**Phrased as a condition on purpose.** ``subroutine mcp`` started by hand has no skill, and
	naming one that is not there is confident wrongness of exactly the kind §13.1 forbids.
	"""

	roster = subroutine.connections.Roster(
		(subroutine.connections.Connection(name="local"),), default="local"
	)
	instructions = _standing_up(monkeypatch, roster)

	assert "skill" in instructions, "nothing sends a session to the skill any more"
	assert "before your first call" in instructions, (
		"the pointer no longer says *when*, which is the half that changes behaviour — an "
		"agent backs into this territory sideways rather than being asked to enter it"
	)
	assert "if a" in instructions.lower(), (
		"the pointer must stay conditional: a bare 'mcp' session has no skill to read"
	)


def test_an_argument_a_tool_does_not_declare_is_refused (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#379`, reported by an agent that met it in its first session.

	It passed ``project`` to a listing on an installation whose schema had no such argument.
	It was neither honoured nor refused, so it received a plausible, complete, wrong answer and
	believed it had narrowed a list it had not.

	**It survives every upgrade**, which is the part that makes it worth a guard: what it needs
	is a client newer than the server it is talking to, which is the ordinary state of a fleet
	and the family `#345` belongs to. And it bites hardest on the feature that surfaced it —
	`#367` exists to stop an agent spending context on a whole workspace, and a silent no-op
	means it spends the context *and does not know*.

	The refusal names what the tool does accept, so the correction costs one call rather than a
	guess. It is a tool failure rather than a protocol error for `protocol.py`'s own reason: a
	JSON-RPC error is handled by the client and may never reach the model, and the model is who
	needs to learn.
	"""

	answer, failed = _called(bound, "subroutine_list", nonesuch="SUBSAMPLE")

	assert failed, "an argument the tool does not declare must not be silently dropped"
	assert "nonesuch" in answer
	assert "workspace" in answer, "and the refusal names what it does accept"

	# The ordinary call still works, which is what stops this being a guard that refuses
	# everything and passes its own test.
	fine, failed = _called(bound, "subroutine_list")

	assert not failed, fine


def test_a_session_default_is_not_filled_into_a_tool_that_cannot_take_it (
	monkeypatch: pytest.MonkeyPatch, session: sqlalchemy.orm.Session
) -> None:
	"""The half of `#379` that would have broken a session on its first call.

	`_within` filled the session's workspace into *every* tool, including `subroutine_whoami`,
	which declares no properties at all. That was harmless for exactly as long as nothing
	checked — the `#353` review looked at it and said so, on the grounds that doing it
	uniformly is the point.

	Refusing undeclared arguments ends that, and the layer meant to be helping would have been
	the thing refusing. Asserted through `catalogue` with a workspace bound, because that is
	the arrangement the plugin actually ships.
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
		server = subroutine.mcp.protocol.Server(
			subroutine.mcp.tools.catalogue(client, workspace="projects"),
			name="subroutine",
			version="0",
		)
		answer, failed = _called(server, "subroutine_whoami")

	assert not failed, answer


def test_an_agent_can_write_the_description_its_skill_tells_it_to_use (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#392`, reported by an agent while filing its first real work.

	**The skill's own argument depended on this and it was false.** It tells an agent to write
	an outcome-shaped title, and justifies it: "your motivation is not lost by an outcome-shaped
	title, because it belongs in the description — which is one field away". From this surface
	it was unreachable, so the skill asked an agent to give up its reasoning and pointed at a
	shelf it could not put anything on. It used comments instead and said itself that a comment
	is what happened and a description is what the task is (§5.10).

	Third instance of `#149`'s blind spot: a capability present on one surface, missing as an
	*argument* on a method both surfaces already call, which a per-method reach test cannot see.
	"""

	ref = _added(bound, "Filed with a title that says the outcome")
	answer, failed = _called(
		bound,
		"subroutine_update",
		ref=ref,
		description="Why this is worth doing, which is what the title deliberately omits.",
	)

	assert not failed, answer

	shown, failed = _called(bound, "subroutine_show", ref=ref)

	assert not failed
	assert "which is what the title deliberately omits" in shown, (
		"a description that cannot be read back is not one that was written"
	)


def test_asking_who_you_are_says_which_versions_are_in_play (
	bound: subroutine.mcp.protocol.Server, session: sqlalchemy.orm.Session
) -> None:
	"""Item ``#381``, and the reason it is on *this* tool rather than a new one.

	An agent cannot run a shell command to compare two installations, and it cannot tell a
	capability that does not exist from one its program is too old to have. ``whoami`` is
	already where it goes when something does not add up, and the skill already tells it to
	ask on its first call — so the answer to "which versions?" costs no new tool and no new
	schema against §21.2's budget.
	"""

	text, failed = _called(bound, "subroutine_whoami")

	assert not failed
	expected = f"Program {subroutine.__version__}, instance {subroutine.__version__}, schema "

	assert expected in text


def test_the_versions_are_reported_even_when_nothing_can_be_read (
	session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""A credential that reaches no workspace still learns which versions it is talking to.

	**This branch used to return early**, and it is the single likeliest reason somebody asks
	this question at all — an agent that has just been refused everything wants to know
	whether it is bounded or whether it is talking to the wrong thing. Reporting the answer
	in every case except that one would have been the feature missing from exactly the case
	it was built for.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	stranger = subroutine.domain.users.create(
		session,
		actor=subroutine.domain.authentication.Principal(user=setup.user),
		username=f"nobody-{uuid.uuid4().hex[:8]}",
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session,
		user=stranger,
		title="reaches nothing",
		scopes=[subroutine.permissions.TASK_READ],
	)
	session.flush()

	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True),
		session_factory=api_support.factory_for(session),
		token=issued.value.get_secret_value(),
	)

	with client:
		server = subroutine.mcp.protocol.Server(
			subroutine.mcp.tools.catalogue(client), name="subroutine", version="0"
		)
		text, failed = _called(server, "subroutine_whoami")

	assert not failed
	assert "No workspace here can be read with this credential." in text
	assert f"Program {subroutine.__version__}" in text


def test_a_comment_can_be_taken_back_out (bound: subroutine.mcp.protocol.Server) -> None:
	"""Item ``#400``, asked for by the agent working in ``SUBSAMPLE``.

	**Named by its words, not by an id.** A comment has no number of its own, and its id
	appears in nothing a caller has necessarily read — the same reason ``subroutine_link``
	withdraws by two refs rather than by a link id.
	"""

	made, failed = _called(bound, "subroutine_add", text="A thing to do")

	assert not failed, made

	ref = _numbered(made)

	_called(bound, "subroutine_comment", ref=ref, body="ran the suite, all green")
	_called(bound, "subroutine_comment", ref=ref, body="and then deployed it")

	gone, failed = _called(bound, "subroutine_comment", ref=ref, body="deployed", remove=True)

	assert not failed, gone

	left, _ = _called(bound, "subroutine_show", ref=ref)

	assert "ran the suite" in left
	assert "deployed" not in left


def test_withdrawing_a_comment_refuses_rather_than_guessing (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""Two matches is refused, and so is none.

	**The alternative is deleting somebody's prose on a coin toss.** A comment is attributed
	and nothing here hard-deletes, but a withdrawal that silently took the wrong one would be
	discovered by the person whose sentence went missing, which is the worst way to find it.
	"""

	made, _ = _called(bound, "subroutine_add", text="Another thing")
	ref = _numbered(made)

	_called(bound, "subroutine_comment", ref=ref, body="the parser is wrong")
	_called(bound, "subroutine_comment", ref=ref, body="the parser is fixed")

	several, failed = _called(
		bound, "subroutine_comment", ref=ref, body="the parser", remove=True
	)

	assert failed
	assert "2 comments" in several

	missing, failed = _called(
		bound, "subroutine_comment", ref=ref, body="nothing says this", remove=True
	)

	assert failed
	assert "Nothing recorded" in missing

	# Neither refusal took anything with it — the state is the one the caller left.
	left, _ = _called(bound, "subroutine_show", ref=ref)

	assert "the parser is wrong" in left
	assert "the parser is fixed" in left


def test_withdrawing_a_comment_without_saying_which_is_refused (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""Item ``#415``. The degenerate match the "more than one" refusal above cannot catch.

	``"" in anything`` is true, so an absent or empty ``body`` named **every** comment on the
	item — and with exactly one there, the refusal beside it had nothing to refuse. Measured
	over the real surface before this closed: ``{"ref":1,"remove":true}`` answered *"Taken out
	of #1."*

	**One comment, deliberately.** The test above has two, so it meets the "several" refusal
	whatever the words are and passes against this defect — which is how the case survived: a
	guard for "too many" reads exactly like a guard for "not enough said".

	The schema marks ``body`` required and the server does not enforce that, so the absent case
	is reachable by anything that does not honour it. Both spellings are checked because they
	arrive by different routes: an empty string is a caller being unhelpful, a missing key is a
	client not validating.
	"""

	made, _ = _called(bound, "subroutine_add", text="One comment only")
	ref = _numbered(made)

	_called(bound, "subroutine_comment", ref=ref, body="the only thing recorded here")

	for arguments in ({"remove": True}, {"body": "", "remove": True}):
		refused, failed = _called(bound, "subroutine_comment", ref=ref, **arguments)

		assert failed, arguments
		assert "words" in refused, refused

	# And the comment is still there, which is the whole of what was at risk.
	left, _ = _called(bound, "subroutine_show", ref=ref)

	assert "the only thing recorded here" in left

	# Naming it still works, so this narrowed nothing anybody wanted.
	taken, failed = _called(
		bound, "subroutine_comment", ref=ref, body="only thing recorded", remove=True
	)

	assert not failed, taken


def _numbered (answer: str) -> int:
	"""Read the ref out of what a tool printed, refusing rather than returning nothing.

	``re.search`` gives ``None`` when nothing matched, and a test that reached for ``.group``
	on it would fail with an ``AttributeError`` about the regex rather than saying that the
	tool answered without a number in it.
	"""

	found = re.search(r"#(\d+)", answer)

	assert found is not None, f"no item number in: {answer}"

	return int(found.group(1))


def test_the_document_tool_says_how_to_revise_one (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#293`, resolved by decision `#484` as a pointer rather than a `ref` argument.

	**The failure was a belief, not a missing route.** A third-party agent met this surface,
	concluded *"documents look immutable through these tools"*, declined to file a draft it
	meant to revise later, and gave one-item-in-one-place — decision `#47`, which is ours — as
	the reason. The workaround was invisible *as* a workaround: it reads as good judgement, and
	nothing in any log, test or listing would have shown it as a defect.

	An escape hatch does not correct a belief, so `call_api` (`#485`) would have left an agent
	reaching the same wrong conclusion. `#488` fixed the refusal it met; this is the other half,
	correcting the belief *before* it forms rather than at the moment it does.

	**Not a `ref` argument on this tool**, which was the leading option. The CLI has `doc create`
	and `doc edit` as two commands, so create-or-update here would make the surfaces disagree
	about whether writing and revising are one act or two — and it carries two silent failures a
	pointer does not: omit the `ref` and you get a duplicate document, pass a stale one and you
	overwrite somebody's conclusion.
	"""

	answered = _exchange(bound, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
	tools = {tool["name"]: tool for tool in answered[0]["result"]["tools"]}
	described = tools["subroutine_document"]["description"]

	assert "doc edit" in described, (
		"the only tool that writes a document must say how one is revised, or an agent "
		"reasonably concludes it cannot be"
	)

	# **The command it names has to exist**, which is `#134`/`#136`/`#138`'s lesson: prose in a
	# schema is context every session carries and nothing had ever asked whether it named
	# something callable. `test_plugin` asks this of the skill; a tool description is the surface
	# with no delivery failure at all, so it is the one that must not lie.
	groups = {
		group.name: group for group in subroutine.cli.main.app.registered_groups if group.name
	}

	assert "doc" in groups, "the description names 'subroutine doc', which must be a command"

	nested = groups["doc"].typer_instance

	assert nested is not None, "'doc' is a group with nothing registered under it"

	within = {
		command.name or (command.callback.__name__ if command.callback else "")
		for command in nested.registered_commands
	}

	assert "edit" in within, f"'doc edit' is named by a tool description and is not real: {within}"


class _Recorded:
	"""A client that answers normally and remembers which methods were asked for.

	A proxy rather than a stand-in, so the tools run against the real local client and a real
	database — the annotation being checked is a claim about what a call *does*, and a mock
	returning mocks would let a tool pass by never getting far enough to write.
	"""

	def __init__ (self, wrapped: subroutine.clients.base.Client) -> None:
		"""Wrap a client and start an empty record."""

		self._wrapped = wrapped
		self.called: list[str] = []

	def __getattr__ (self, name: str) -> typing.Any:
		"""Return the wrapped attribute, noting the name if it is callable."""

		attribute = getattr(self._wrapped, name)

		if not callable(attribute):
			return attribute

		def recording (*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
			self.called.append(name)

			return attribute(*args, **kwargs)

		return recording


def _writing_methods () -> set[str]:
	"""Return the client methods that refuse on a read-only connection.

	**Read out of the code rather than listed here**, because a list would be a second copy of
	a rule that already exists and is maintained: ``_refuse_if_read_only`` is what
	``clients/http.py`` calls on every write, and §13.7's read-only connection depends on it
	being complete. A tool that reaches one of these is writing, whatever it claims.
	"""

	source = pathlib.Path(subroutine.clients.http.__file__).read_text(encoding="utf-8")
	found: set[str] = set()

	for node in ast.walk(ast.parse(source)):
		if not isinstance(node, ast.FunctionDef):
			continue

		for inner in ast.walk(node):
			if (
				isinstance(inner, ast.Call)
				and isinstance(inner.func, ast.Attribute)
				and inner.func.attr == "_refuse_if_read_only"
			):
				found.add(node.name)

	return found


def test_a_tool_that_says_it_only_reads_only_reads (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#489`. The annotation is a machine-readable claim, so something has to check it.

	A client is entitled to skip an approval prompt because a tool said ``readOnlyHint``. If
	that is wrong, the tool writes to somebody's instance without the confirmation the client
	would otherwise have asked for — so this is the one annotation whose falsity has a cost
	beyond noise, and prose asserting it is exactly what this codebase has been bitten by.

	Driven rather than compared: every read-only tool is *called*, against a real database, and
	the client methods it reaches are checked against the set that refuses on a read-only
	connection. A test comparing two hand-written lists would agree with itself forever.
	"""

	subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	session.flush()

	writes = _writing_methods()

	assert len(writes) >= 10, (
		f"found {len(writes)} writing client methods, which is too few — has the scan stopped "
		f"reaching them? A guard that finds no writes passes every tool."
	)

	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True),
		session_factory=api_support.factory_for(session),
	)

	with client:
		recorded = _Recorded(client)
		server = subroutine.mcp.protocol.Server(
			subroutine.mcp.tools.catalogue(typing.cast(subroutine.clients.base.Client, recorded)),
			name="subroutine",
			version="0",
		)

		# Something for the readers to find, made through the tools so the fixture cannot be
		# right in a way the surface is not.
		ref = _added(server, "Something to read back")

		reading: dict[str, dict[str, typing.Any]] = {
			"subroutine_list": {},
			"subroutine_search": {"q": "read back"},
			"subroutine_show": {"ref": ref},
			"subroutine_changes": {},
			"subroutine_whoami": {},
		}
		declared = {
			tool.name
			for tool in subroutine.mcp.tools.catalogue(client)
			if (tool.annotations or {}).get("readOnlyHint")
		}

		assert declared == set(reading), (
			f"a tool declares readOnlyHint and this test does not call it: "
			f"{sorted(declared ^ set(reading))}"
		)

		for name, arguments in reading.items():
			recorded.called.clear()

			answered, failed = _called(server, name, **arguments)

			assert not failed, f"{name} did not run, so nothing was measured: {answered}"
			assert recorded.called, f"{name} reached no client method at all"

			trespass = set(recorded.called) & writes

			assert not trespass, (
				f"{name} declares readOnlyHint and called {sorted(trespass)}, which "
				f"clients/http.py refuses on a read-only connection"
			)


def test_an_agent_can_read_this_workspace_s_vocabulary (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#486`. The keys are renameable, so an agent that guesses them is guessing wrong.

	`#483` published the guide and the examples and left this out, because the local client
	would have had to rebuild what ``api/meta.py`` assembles against a request. Decision `#484`
	made that gap load-bearing: ``call_api`` (`#485`) invites an agent to construct a request by
	hand, and **nothing else publishes this installation's status and item-type keys** — ``done``
	may be called ``Shipped`` here (§5.5), which is §13.2's whole subject.

	Driven against a real database through the ordinary local client, because the point is that
	the *local* transport can answer it at all. A mock would have proved the wiring and left the
	thing `#483` deferred entirely unchecked.
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
		server = subroutine.mcp.protocol.Server(
			subroutine.mcp.tools.catalogue(client),
			name="subroutine",
			version="0",
			resources=subroutine.mcp.tools.references(client),
		)
		answered = _exchange(
			server,
			{
				"jsonrpc": "2.0",
				"id": 1,
				"method": "resources/read",
				"params": {"uri": "subroutine://meta"},
			},
		)

	content = answered[0]["result"]["contents"][0]

	assert content["mimeType"] == "application/json"

	published = json.loads(content["text"])

	assert published["statuses"], "an agent cannot set a status it was never told the key for"
	assert published["item_types"], "nor a type"
	assert "done" in {
		row["key"] for rows in published["statuses"].values() for row in rows
	}, "keyed rather than labelled — 'done' is the key `#486`'s own argument is about"

	# The half `call_api` needs beyond the vocabulary: what a listing will accept, so a raw
	# request can be built without discovering each parameter by being refused.
	assert published["listings"]["task"]["filters"], (
		"reflected from the application, so an empty list means the reflection stopped working"
	)
	assert published["error_codes"]
