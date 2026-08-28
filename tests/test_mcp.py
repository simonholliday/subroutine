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
import datetime
import io
import json
import os
import pathlib
import re
import shutil
import time
import typing
import unittest.mock
import uuid
import zoneinfo

import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.api.app
import subroutine.api.routing
import subroutine.cli.main
import subroutine.cli.personal
import subroutine.clients.http
import subroutine.clients.local
import subroutine.clients.opening
import subroutine.config
import subroutine.connections
import subroutine.context
import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.seed
import subroutine.db.types
import subroutine.directory
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.capture
import subroutine.domain.documents
import subroutine.domain.events
import subroutine.domain.filtering
import subroutine.domain.text
import subroutine.domain.workspaces
import subroutine.installations
import subroutine.mcp.protocol
import subroutine.mcp.relay
import subroutine.mcp.session
import subroutine.mcp.tools
import subroutine.permissions
import subroutine.views


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

	**Rows rather than lines**, which `SR#1071` corrected: the answer now carries a footer when
	it was cut, so counting every line would make this test refuse the sentence that says the
	limit was honoured. The claim was always about how many *items* come back.
	"""

	for index in range(5):
		_added(bound, f"Task {index}")

	text, _failed = _called(bound, "subroutine_list", limit=3)

	assert len([line for line in text.splitlines() if line.startswith("#")]) == 3


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


def test_an_agent_can_see_who_work_is_with (
	bound: subroutine.mcp.protocol.Server,
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#511`, on the surface its own description said was already covered.

	The item argued that an agent could see this because ``assignee_id`` is in the JSON. That
	is true of raw HTTP and **not** of here: these tools return text, and ``_line`` rendered
	the rank, the estimate and the deadline and nothing about who had it. So `#493` gave an
	agent a way to hand work over and left the tool that reads it unable to report the result
	— the two disagreeing about whether anything had happened.

	A username rather than the id, for the reason the comment renderer already gives: a UUID
	is thirty-six characters a model cannot resolve without another call.
	"""

	ref = _added(bound, "Build the endpoint")
	who = session.scalars(
		sqlalchemy.select(subroutine.db.models.identity.User.username)
	).first()

	assert who is not None

	changed, failed = _called(bound, "subroutine_update", ref=ref, assignee=who)

	assert not failed, changed

	shown = _called(bound, "subroutine_show", ref=ref)[0]

	assert f"@{who}" in shown, "the tool that assigns and the tool that reads still disagree"

	listed = _called(bound, "subroutine_list")[0]

	assert f"@{who}" in listed


def test_a_listing_says_what_is_already_started (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#841`. The convention asks an agent to announce what it is doing; this is who hears it.

	`#705` mandates claiming an item and setting it ``in_progress``, and `#777` measured the
	result: nothing had ever been claimed. This is the read half — until now an agent listing
	the backlog could not see that another agent had already started something, so two would
	pick the same item.

	**Two rows, because the assertion that matters is the negative one.** ``open`` is the
	status everything starts in and saying it on every row is what §1.4 would not survive; a
	single-row listing would satisfy this test whatever the rule did.
	"""

	started = _added(bound, "Rewire the parser")
	untouched = _added(bound, "Sweep the logs")

	changed, failed = _called(
		bound, "subroutine_update", ref=started, status="in_progress"
	)

	assert not failed, changed

	listed = _called(bound, "subroutine_list")[0]
	rows = {
		line.split()[0]: line for line in listed.splitlines() if line.startswith("#")
	}

	assert "in_progress" in rows[f"#{started}"], listed
	assert "open" not in rows[f"#{untouched}"], (
		f"the status everything starts in is on an ordinary row: {rows[f'#{untouched}']!r}"
	)


def test_a_listing_says_who_is_holding_something_and_forgets_when_the_lease_runs_out (
	bound: subroutine.mcp.protocol.Server, session: sqlalchemy.orm.Session
) -> None:
	"""`#841`, and the half that is easy to get wrong.

	A claim is a **lease** (§14.11): it expires, and an expired one is ignored, because a
	worker that dies holding one must not strand the work. The view reports the holder of an
	expired lease on purpose — who was working on this is worth keeping — so a renderer that
	read ``claimed_by`` and stopped would report work as taken that nobody is doing.

	**That is not a hypothetical.** Of the three claim records on this project's own instance
	when this was written, two had expired: reading the column alone would have been wrong
	about two rows in three, in the direction that stops an agent picking up free work.
	"""

	ref = _added(bound, "Rotate the certificates")
	who = session.scalars(
		sqlalchemy.select(subroutine.db.models.identity.User.username)
	).first()

	assert who is not None

	taken, failed = _called(bound, "subroutine_claim", ref=ref)

	assert not failed, taken
	assert f"claimed by @{who}" in _called(bound, "subroutine_list")[0]

	# Backdated rather than waited for: the lease is half an hour by default, and the point is
	# the clock rather than the duration.
	session.execute(
		sqlalchemy.update(subroutine.db.models.work.Task)
		.where(subroutine.db.models.work.Task.ref == ref)
		.values(claim_expires_at=subroutine.db.types.utcnow() - datetime.timedelta(hours=1))
	)
	session.flush()

	expired = _called(bound, "subroutine_list")[0]

	assert "claimed by" not in expired, (
		f"an expired lease is still reported as held: {expired!r}"
	)
	assert "Rotate the certificates" in expired, "the row itself should still be there"


def test_a_documents_status_is_not_a_cell_in_a_listing (
	bound: subroutine.mcp.protocol.Server, session: sqlalchemy.orm.Session
) -> None:
	"""The measured half of `#841`, and it went the other way from what tidiness suggested.

	``views.status_is_news`` is asked of tasks only. Applying it to documents as well is the
	obvious symmetry and is wrong here, because **a document's default is ``draft`` and
	``active`` is not** — so on this project's own instance the rule would put a cell on 111
	of 122 document rows, saying the ordinary thing about nearly all of them. That is §12.2a's
	"a column that says the same thing on every row says nothing", reached from the other
	direction.

	``show`` still reports it, and that difference is the design: a listing says *which* item
	to pick and a fact sheet says everything about one.
	"""

	written, failed = _called(
		bound, "subroutine_document", title="What we decided about caching", body="Because."
	)

	assert not failed, written

	numbered = re.search(r"#(\d+)", written)

	assert numbered is not None, written

	ref = int(numbered.group(1))
	# **`entity_type` as well as the key**, because two seeded statuses are called `active` —
	# a project's, which is its default, and a document's, which is not. Asking by key alone
	# picked the project's and made this test assert against the wrong vocabulary, which is
	# `#722`'s narrower-paths-first trap wearing a table instead of a URL.
	active = session.scalars(
		sqlalchemy.select(subroutine.db.models.vocabulary.Status).where(
			subroutine.db.models.vocabulary.Status.entity_type == "document",
			subroutine.db.models.vocabulary.Status.key == "active",
		)
	).first()

	assert active is not None, "the workspace was seeded without an 'active' document status"
	assert not active.is_default, "this test needs a status that is not the document default"

	session.execute(
		sqlalchemy.update(subroutine.db.models.work.Document)
		.where(subroutine.db.models.work.Document.ref == ref)
		.values(status_id=active.id)
	)
	session.flush()

	listed = _called(bound, "subroutine_list")[0]
	row = next(line for line in listed.splitlines() if line.startswith(f"#{ref} "))

	assert "active" not in row, f"a document's status is a cell on every row: {row!r}"
	assert "active" in _called(bound, "subroutine_show", ref=ref)[0], (
		"the fact sheet should still report it — the listing is where it is noise"
	)


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


def test_an_agent_reading_an_item_is_told_how_many_blockers_are_left (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#970`, and it is `#84`'s rollup arriving on the surface that had least of it.

	A milestone here is an item whose blockers are its contents, so *how many are left* is the
	question somebody opens one to ask. ``subroutine show`` has answered it since `#210`; this
	line said the label, the ref and the title and nothing else — so an agent deciding whether
	it could start had to read every blocker in turn, which is the loop this surface exists to
	remove.

	**Counted over incoming ``blocks`` alone**, which is the terminal's own rule and is why a
	*relates to* added below moves neither number.

	**``over`` rather than ``done``**, because ``is_complete`` is ``completed_at is not None``
	and invariant 5 makes that true for done *and* cancelled — so the obvious word asserts
	something about half of them that nobody did.
	"""

	milestone = _added(bound, "Ship the release")
	first = _added(bound, "Write the client")
	second = _added(bound, "Build the endpoint")
	aside = _added(bound, "Something else entirely")

	for blocker in (first, second):
		made, failed = _called(
			bound, "subroutine_link", ref=blocker, type="blocks", other=milestone
		)
		assert not failed, made

	noise, failed = _called(
		bound, "subroutine_link", ref=milestone, type="relates_to", other=aside
	)
	assert not failed, noise

	shown = _called(bound, "subroutine_show", ref=milestone)[0]

	assert "0 of 2 blockers done" in shown, shown
	assert "(over)" not in shown, "nothing is finished, so nothing may say it is"

	done, failed = _called(bound, "subroutine_done", ref=first)

	assert not failed, done

	shown = _called(bound, "subroutine_show", ref=milestone)[0]

	assert "1 of 2 blockers done" in shown, shown
	assert shown.count("(over)") == 1, (
		"exactly one end is finished, so exactly one line may say so"
	)

	# **The item nobody has to do first is not counted and is still listed.** A rollup over
	# every link would read `1 of 3` about an item with two blockers, which is the arithmetic
	# `#210` fixed at the terminal.
	assert "Something else entirely" in shown


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
	the five buckets: the buckets are a *terminal* structure, and a model reading five headings
	for what is usually five rows is paying for the headings.

	**This asserted the opposite until 2026-08-18, and the reversal is Simon's** (`#991`,
	decision `#989`). It required ``unscheduled`` to be absent, on the argument that it is the
	terminal's filler and none of it is *on today*. What reversed it is a measurement: **11 of
	170 open tasks on this project's own instance are dated**, so on an ordinary day an agent
	was told *"Nothing on today."* while the browser showed twenty ranked items. The intent is
	kept and the satisfier changed — the question is still *what is on now*, and the answer now
	says which part of the day each row belongs to.
	"""

	ref = _added(bound, "Ring the dentist")

	_added(bound, "Rewrite the importer one day")

	_called(bound, "subroutine_update", ref=ref, plan="today")

	on_today, failed = _called(bound, "subroutine_list", today=True)

	assert not failed, on_today
	assert "Ring the dentist" in on_today

	# **Every bucket reaches an agent, which is the half that changed.**
	assert "Rewrite the importer" in on_today, (
		f"undated work is on the agenda every other surface renders: {on_today}"
	)

	# **And each row says which bucket it is in, which is the condition on the parity rather
	# than a nicety.** Without it a backlog suggestion is distinguishable from a commitment
	# only by the absence of a deadline, so flat parity would be worse than the drop it
	# replaced. Read off the rows rather than the whole answer, because a word appearing
	# somewhere in a block of text is not a label on anything.
	buckets = {
		next(
			(
				cell
				for cell in (part.strip() for part in line.split("  "))
				if cell in subroutine.views.AGENDA_BUCKETS
			),
			None,
		)
		for line in on_today.splitlines()
		if line.startswith("#")
	}

	assert buckets == {"today", "unscheduled"}, (
		f"a row names the section of the day it is in: {on_today}"
	)


def test_an_agent_can_make_and_list_a_project (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#149`, and the last of the skill's shell-outs.

	SPEC §21.5's adoption asks what exists and then adds to it, which is why one tool does
	both: the two questions arrive together and a second name would be schema spent on the
	seam between them.
	"""

	made, failed = _called(
		bound, "subroutine_project", key="web", title="Website redesign"
	)

	assert not failed, made
	assert "web" in made

	listed, failed = _called(bound, "subroutine_project")

	assert not failed
	assert "Website redesign" in listed


def test_a_project_listing_shows_where_each_project_sits_in_the_tree (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#617`, met at Phase 0 by two separate import runs before it was fixed.

	**Both drawings are named, because a test that only checks the child is indented cannot
	tell a tree from a listing that indents every row.** The parent's line is asserted flush,
	which is what fails if the rule is applied to everything rather than to depth.

	**And the titles are asserted to begin at one column**, which a bare check for leading
	spaces would miss: the key column is padded to the deepest row, so indenting without
	widening leaves the two titles ragged — the defect one layer down, and the shape `#1424`
	met in CSS.
	"""

	_called(bound, "subroutine_project", key="web", title="Website")
	_called(bound, "subroutine_project", key="docs", title="Documentation", parent="web")

	listed, failed = _called(bound, "subroutine_project")

	assert not failed, listed

	drawn = {line.strip().split("  ")[0]: line for line in listed.splitlines()}

	assert drawn["web"].startswith("web"), listed
	assert drawn["docs"].startswith("  docs"), listed
	assert drawn["web"].index("Website") == drawn["docs"].index("Documentation"), listed


def test_making_a_project_private_says_so_on_the_agent_surface (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#1450`, which is `#1444`'s fix reaching the surface that can create the trap.

	**The CLI has said this since `e98a848` and this surface said nothing at all.** A private
	project is visible to its owner and to nobody else, permanently — §7.3a grants sight to
	holders of a ``project_member`` row and no route, command or tool writes a second one. The
	caller most able to walk into that is an agent, because ``subroutine_project`` accepts
	``private`` and is often the first thing a session does in a new codebase.

	**Said in the reply and not only in the schema.** A property description is read once when
	the tool list loads, by a model that has not yet decided anything; this is read by the
	caller who has just done it.

	**The way back is an API call rather than this tool**, because this tool lists and creates
	and cannot update — naming ``private: false`` here would offer a remedy that does not
	exist, which is the same defect one turn later.
	"""

	made, failed = _called(
		bound, "subroutine_project", key="secret", title="Secret", private=True
	)

	assert not failed, made
	assert "Only you can see it" in made, made
	assert "subroutine_call_api" in made, (
		f"the way back has to be reachable from here, and this tool cannot take it:\n{made}"
	)


def test_an_ordinary_project_says_nothing_about_who_can_see_it_to_an_agent (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""The other half, and §13 is why it is a test rather than an obvious omission.

	Public is what nearly every project is, so a sentence about visibility on it is a line paid
	on the ordinary case — on the surface where response size is a first-order cost. The CLI
	half of this rule is `#1444`'s, argued from §1.4; here the argument is the byte budget, and
	both land on the same behaviour.
	"""

	made, failed = _called(bound, "subroutine_project", key="web", title="Website")

	assert not failed, made
	assert "Only you can see it" not in made, made
	assert "web" in made


def test_a_project_named_without_a_title_is_refused (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""One tool doing two jobs has to say which one it thought it was being asked for.

	A key with no title is a create that cannot finish, not a list — answering with the
	listing would be the tool quietly doing something else.
	"""

	answered, failed = _called(bound, "subroutine_project", key="web")

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
	assert "changed how it is ranked" not in plain

	shown, failed = _called(bound, "subroutine_show", ref=ref, history=True)

	assert not failed, shown
	assert "created" in shown
	# The reader's word, not the column (`SR#1187`) — ``importance`` is a database name and
	# this surface mentions one nowhere else.
	assert "changed how it is ranked" in shown
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

	* ``subroutine_journal`` (`SR#1430`, decision `SR#1429`), 2026-08-27 — the fourteenth, and
	  the case is that the tool beside it answers a different question badly.

	  **Measured before it was asked for.** One real day on this instance is 450 events, of
	  which **130 are `comment.created` carrying no body at all**; 51 field-changes are bare
	  UUIDs, and `actor_user_id` is a UUID on every row. So an agent asked *what did we do on
	  Friday* and holding only `subroutine_changes` can report that fourteen comments were
	  written and not one word of what any said — a list of things to go and look up, produced
	  confidently.

	  **Not an argument on `subroutine_changes`, and that is Simon's decision**: an agent has
	  to be able to *discover* the distinction, and a flag on an existing tool is something you
	  have to already know exists. This is `SR#367`'s lesson pointing the other way — a
	  capability parked in another tool's argument is undiscoverable because a model reads tool
	  *names* — and here the two also want different defaults, since the feed resumes forwards
	  from a cursor and this reads a past period back.

	  **What it costs is smaller than a new capability**, because the schema is almost entirely
	  shared: `DATE_FILTER` and `WORKSPACE` are the same objects the listing already declares,
	  so what is new is a description, a `by` and an `oldest`.

	  **Fat was read for first and none was taken, for the fifth time running.**

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

	* **`#485`, to 10,400 and a fourteenth tool** — ``subroutine_call_api``, decision `#484`.
	  Simon's decision, 2026-08-05. **The only entry here that adds no capability of its own and
	  instead removes the reason the others had to be argued for.**

	  The measurement that settled it: of twenty capabilities this surface lacked, **thirteen
	  were excluded for budget** and five by judgement. So the surface had not been curated, it
	  had been *rationed* — and every raise above this line is a case for spending scarcity that
	  nobody had chosen to impose.

	  **Measured at +794 bytes.** Roughly 200 tokens a session, against thirteen capabilities
	  that no longer need a slot and a fourteenth that no longer needs this argument.

	  **What the description spends its bytes on is the part worth keeping.** It points at the
	  named tools first, because `subroutine_add`'s 250-byte description is the *only* place an
	  agent learns §6.13's capture grammar — and ``POST /v1/tasks`` takes structured fields too
	  and documents them as the deliberate no-magic path. So an agent working from the API alone
	  sends ``{"title": …, "importance": 4}``, which **works perfectly**, and the grammar
	  silently stops being used. Every call succeeds and nothing reports it, which is this
	  project's signature failure. The pointer lives in the description rather than only in the
	  skill because `#378` is standing evidence that the skill alone does not arrive.

	  **Fat was read for, for the seventh time, and none was taken.** ``subroutine_update`` is
	  still the largest and still ten argument descriptions doing work.

	  **The ratchet stops here rather than moving.** The test for a fourteenth used to be "is
	  there room"; from now it is *"what would an agent get wrong without it?"* If the answer is
	  "nothing, it would just have to look up a path", it does not need a tool — it has one.

	The slack above the current total is deliberate and small — **418 bytes** as of 2026-08-05,
	which is about one description. A cap set exactly at what is there makes every addition a
	cap change, which is theatre; a generous one stops being a budget.

	**`#424` spent 104 of it and the cap did not move, which is the slack doing its job.**
	``subroutine_add`` gained a ``description``, because `#392` had put one on
	``subroutine_update`` and thereby made a described item two calls on two tools — an agent
	reported that it had simply skipped the second, six times, and that its own titles were
	unreadable as a result. Worth recording that no fat was read for this time: the addition
	fitted, so looking would have been a ritual rather than a check.

	**`#549` spent 84 more and the cap did not move either.** Seven arguments that name an item
	were declared ``integer`` and have always accepted ``"#42"`` as well, because §6.2 requires
	it: this system prints that form in every listing, so a model sends it back. Publishing one
	of two accepted spellings was free while nothing checked the types and was the first thing
	to break when something did — a client obeying the contract could not send the notation the
	product's own output uses.

	**Fat was read for and none was taken, for the eighth time.** What was read this time was
	the *addition* — `#489`'s lesson, where 132 of 591 bytes said nothing because they restated
	a protocol default. ``["integer", "string"]`` has no such slack: dropping ``type`` entirely
	would be cheaper than today and leaves a model guessing, and ``"string"`` alone is cheaper
	still and would refuse the integer every existing caller sends.

	**That number is now stated as of a date, because the last one rotted.** It said 33 bytes,
	which was true when it was written and was 7 by the time anybody read it again — a title
	stating a condition becomes false when the condition changes, silently, which is the
	argument `#139` settled about item titles and applies to a comment just as well (`#198`).
	Do not trust it; run the test and read what it says.

	* **`#815`, to 10,600** — a `filter` argument on `subroutine_list`, so an agent can ask
	  *what was created yesterday*. Simon's decision, 2026-08-11, taken against the measured
	  alternatives rather than in the abstract.

	  **Measured at +401 bytes**, roughly 100 tokens a session. The capability was not absent:
	  `subroutine_call_api` has reached `GET /v1/tasks?created_at.gte=yesterday` since `#485`,
	  and both `/v1/meta` and `/v1/docs/agent` now describe the grammar. So this raise buys
	  **discoverability**, which is the same case `#282` made for `subroutine_search` and named
	  as the weakest of its kind — a model deciding what it can do reads tool *names*, and a
	  capability reachable only through another tool's escape hatch is found by reading every
	  schema in full, which is what a model reliably does not do.

	  What makes it stronger than `#282`'s is that the question is one a person asked for. `#815`
	  is Simon's own request, and its stated requirement is that *an agent can generate the
	  request* — which an agent that never learns the grammar exists cannot.

	  **The cheaper description was measured and refused.** A terse form came to 10,393, seven
	  bytes under the standing cap, and taking it would have been exactly the theatre the
	  paragraph below warns about: a cap satisfied by editing prose has stopped measuring
	  anything. The bytes it saved were the field list, which is the part an agent needs to use
	  the argument without a second call.

	  **Fat was read for first and none was taken, for the ninth time.** The addition itself was
	  read too — `#489`'s lesson — and the one thing removed was a second example of the value
	  grammar, which `/v1/docs/agent` already carries in full.

	  **The description is built from the registry rather than written**, so it cannot advertise
	  a field the instance refuses. That is not a nicety: `#815` produced that exact defect twice
	  in a day, once in `/v1/meta` and once in the guide.

	* **`#815` again, to 10,700** — `touched_at` and `touched_by`, in the same item and against
	  the raise above. Simon approved *a full description* over a surface of seven date fields;
	  this is what that decision costs once the surface is nine and two of them are not dates.

	  **Measured at +87 bytes** on top of the 401, so 488 in all — roughly 120 tokens a session.

	  **A derived description still has to say what the registry means**, which is the lesson
	  worth keeping. Deriving the field list from `TASK_FILTERS` was right and kept the schema
	  honest about *which* fields exist — and it silently started calling `touched_by` a date
	  field and claiming `eq` was day-only, because it was written when every filterable field
	  was a timestamp. A guard against advertising a field that does not exist is not a guard
	  against describing the ones that do incorrectly.

	  **Fat was read for in the addition and 65 bytes were taken**, which is the first time
	  since `#489` that reading found any: a second spelling of the value grammar, an
	  enumeration of five activity verbs where one example carries it, and *(a day, an instant)*
	  restating what "the date grammar" already means.

	* **`#819`, to 10,800** — `tags` on ``subroutine_document``, so a conclusion can be labelled
	  by the surface that writes it. Simon's decision, 2026-08-12, taken after a measured pass
	  over the whole surface rather than before one.

	  **Measured at +124 bytes, against 112 found by reading** — so the raise buys 12 bytes and
	  the rest was already there. That order matters and is §21.2's own: measure, read for fat,
	  *then* raise. The alternative was to take a further 60 by shortening the ``workspace``
	  description on twelve tools, which would have landed 8 bytes under the standing cap — the
	  theatre this file already warns about, where a number satisfied by editing prose has
	  stopped measuring anything.

	  	* **`#94`, to 10,900** — ``skip`` on ``subroutine_done``, so an agent can let one of a
	  repeating series go by rather than recording it as done.

	  **The ratchet's test is what an agent would get wrong without it**, and here that is
	  precise: both verbs end the occurrence and both bring the next, so without this the only
	  reachable answer is *done* — and a series recorded entirely as done cannot say how often
	  it is actually skipped, which is exactly `#574`'s observation that a habit skipped leaves
	  no trace. Recording it as done does not leave no trace; it leaves a wrong one.

	  **A flag on the tool that already finishes an occurrence, not a fifteenth tool.** The
	  same subject, two verbs — where a new tool would have cost a name, a title, a
	  ``workspace`` property and a ``ref`` before saying anything.

	  **Measured at +51 over the cap after reading the addition for fat** — the description was
	  53 bytes and is 30. The three longest existing descriptions were read and none was fat:
	  *"does not read an item's own status"* is a correction an agent gets wrong without it,
	  and the ``order`` examples are the grammar. So this raise buys the whole 51 rather than
	  most of it having been there, which is the honest report and the opposite of `#819`'s.

	**The byte cap is retired, and the count is not** — Simon's decision of 2026-08-23,
	answering `#1124` Q3 and closing the spike `#541`. Everything below it is the record of how
	it was spent and is left standing, because the reasoning in it is about *what earns a
	place on this surface*, which is the question that survives.

	**What falsified it was a measurement of the client, not an argument.** A session was found
	loading tool **names** eagerly and deferring every schema until one was fetched — so the
	ceiling rationed a cost that client does not charge at session start. §21.2 stated the
	premise as a law where it is a worst case, and clients without tool search do exist; what
	it was doing in practice was blocking `#999` by 85 bytes and `#1114` by 91.

	**The risk that survives is discoverability, and a count measures it where bytes do not.**
	A schema never fetched is a tool never called, so a fat surface hides its own tools — and
	fourteen names in a list is the thing an agent actually reads before choosing. The cap
	stays at fourteen and raising it is still meant to be an act.

	The spending record follows.

	* **`#94`, to 11,000** — ``repeat`` on ``subroutine_update``, and ``repeats`` named in
	  ``subroutine_add``'s list of what the line carries.

	  **The ratchet's test decided which tool got the argument, and the answer was one rather
	  than both.** ``subroutine_add`` takes a captured line and the grammar already reads a
	  repeat out of it, so an argument there would be a second way to say what that tool
	  exists to say. What an agent could not do *at all* was change how something came round
	  or stop it — a line is typed once. So the argument goes where the gap is, and
	  ``subroutine_add`` gets twelve words naming the capability instead of 96 bytes
	  duplicating it.

	  **Measured at 121 bytes: 86 for the property, 34 for the description, 1 for a
	  correction.** The fourteen tool descriptions and every property description were read
	  longest-first before raising, and **there was no fat** — the two longest are
	  ``subroutine_call_api``'s pointer and ``subroutine_comment``'s comment-against-document
	  rule, both of which are things an agent gets wrong without them.

	  **`recurrence_anchor` was weighed and left off**, which is where the restraint went. It
	  is a second argument for a choice that matters mostly to habits a *person* files, and
	  ``subroutine_call_api`` reaches it — `#484` built that escape hatch so the curated
	  surface could stay an opinion rather than a complete one, and this is the first raise to
	  spend it deliberately rather than for want of room.

	  **And reading for bytes found a fossil, for the fourth time running.** The published
	  example line ended ``+SR`` — this project's own key, retired on 2026-08-08 (`#176`) — so
	  a schema shipped to every session named a project that resolves nowhere, and it appeared
	  in ``subroutine_call_api`` as well. Driven to be sure: it is refused by name rather than
	  ignored, which is `#778` working, but a worked example that fails is worse than none. It
	  is ``+web`` now, matching ``subroutine_project``'s own example.

**Three of the four things read for were corrections, not trims**, which is the finding
	  worth keeping about this exercise: reading a schema for *bytes* is what made anybody read
	  it at all.

	  - `#821`: ``subroutine_link``'s ``type`` listed three of five seeded link types, and the
	    two missing were ``derives_from`` and ``documents`` — the pair that join work to the
	    conclusions about it, which is the loop the skill spends most of its words on. It points
	    at ``subroutine://meta`` now, which is 19 bytes cheaper *and* right, and stops the schema
	    holding a literal copy of vocabulary §5.5 makes renameable.
	  - `#822`: ``subroutine_document`` told an agent to revise with ``subroutine doc edit 42``
	    — a shell command, on the surface whose premise is having no shell. `#548` fixed that
	    class for *refusals*, and ``protocol.INSTEAD_OF`` does not reach a tool description.
	  - The date grammar was spelled out twice in ``filter``, and the ranking rule twice in
	    ``list`` — once in the tool's description and again in the property's.

	  **``title`` was considered and kept.** Dropping it from all fourteen saves 356 bytes, far
	  more than everything above, and it is what a *person* reads in an approval dialog — which
	  is the moment `#489` raised this cap to protect.
	"""

	answered = _exchange(bound, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
	tools = answered[0]["result"]["tools"]

	assert len(tools) <= 15, "the surface has grown; is each new tool worth every session?"

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

	# **Still measured against the whole, now that nothing else is.** The claim is a *share*
	# rather than a figure, so retiring the ceiling above leaves this one exactly as it was —
	# what it needs is the total, not a limit on it.
	size = len(json.dumps(tools))

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


def test_a_refusal_names_a_field_this_tool_actually_takes (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#547`. Naming the workspaces is half of it; the other half is what to do with one.

	The test above asserts the refusal lists both names, and passed for as long as the advice
	beside them was unfollowable — *"Pass 'workspace_id'"*, which no tool here declares. An
	agent that followed it was refused a second time and recovered only because `#379` prints
	the arguments the tool does accept. That is the shape `#492` is about: a guard checking one
	half of a message cannot see the half that decides whether the reader can act.

	**Driven rather than reasoned about** (`#489`): every tool that takes a workspace is called
	with no arguments at all on an instance holding two, which is the one bad input that reaches
	every one of them, and the rendered text is read the way a model reads it.
	"""

	client, _first, _second = _two_workspaces(session)
	wrong = []
	named = 0

	with client:
		server = subroutine.mcp.protocol.Server(
			subroutine.mcp.tools.catalogue(client), name="subroutine", version="0"
		)

		for tool in server.tools.values():
			takes = tool.schema.get("properties", {})

			if "workspace" not in takes:
				continue

			text, failed = _called(server, tool.name)

			if not failed:
				continue

			for field in re.findall(r"^([A-Za-z_][\w-]*): ", text, re.MULTILINE):
				named += 1

				if field not in takes:
					wrong.append(
						f"{tool.name} was refused naming {field!r}, which it does not take. "
						f"It accepts: {', '.join(sorted(takes))}"
					)

	assert not wrong, "\n".join(wrong)

	# Otherwise a rendering that stopped naming fields at all would pass this most comfortably,
	# which is the one thing a check over prose cannot notice about itself.
	assert named, "no refusal named a field — has the rendering stopped reporting them?"


def test_a_tool_that_calls_a_field_something_else_says_so (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The irregular case the ``_id`` rule cannot derive, and the reason ``renames`` exists.

	``subroutine_project`` is the only tool that takes a project and does not call it one: a key
	goes in ``parent``. The layer below reports it as ``project``, which is right everywhere
	else and is a word this tool does not accept.
	"""

	client, first, _second = _two_workspaces(session)

	with client:
		server = subroutine.mcp.protocol.Server(
			subroutine.mcp.tools.catalogue(client), name="subroutine", version="0"
		)
		text, failed = _called(
			server,
			"subroutine_project",
			key="child",
			title="Child",
			parent="nosuchproject",
			workspace=first,
		)

	assert failed
	assert "parent:" in text, text
	assert "project:" not in text, "the layer below's name for it reached the agent"


@pytest.mark.parametrize(
	"field,expected,why",
	[
		("workspace_id", "workspace", "the suffix rule, where the shorter name is declared"),
		("workspace", "workspace", "a name the tool already takes is left alone"),
		("project", "project", "no rename declared and no shorter form"),
		("owner_id", "owner_id", "shortening it would name an argument that does not exist"),
	],
)
def test_a_field_is_renamed_only_to_something_the_tool_declares (
	field: str, expected: str, why: str
) -> None:
	"""A rule that could invent a name would be worse than the defect it replaces.

	Renaming is derived rather than listed, which is only safe because it checks the schema
	first — so ``owner_id`` stays ``owner_id`` on a tool with no ``owner``, and the reader is
	left with the original rather than sent after a word that does not exist.
	"""

	tool = subroutine.mcp.protocol.Tool(
		name="example",
		title="Example",
		description="",
		schema={
			"type": "object",
			"properties": {"workspace": {"type": "string"}, "project": {"type": "string"}},
		},
		call=lambda arguments: "",
	)

	assert subroutine.mcp.protocol._as_this_tool_calls_it(field, tool) == expected, why


def test_a_field_is_left_alone_when_there_is_no_tool_to_ask () -> None:
	"""A resource read and a protocol-level refusal have no tool, and must still render."""

	assert subroutine.mcp.protocol._as_this_tool_calls_it("workspace_id", None) == "workspace_id"


def test_a_boolean_given_a_string_is_refused_rather_than_read_as_true (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#549`, and the reason it is ranked where it is: this one failed *silently*.

	``"false"`` is a non-empty string and so is truthy in Python, so a model that asked for a
	filter to be **off** got it **on** — with a plausible answer, no error, and nothing to
	notice. `#379`'s own words for the class: a plausible, complete, wrong answer.

	Asserted against the behaviour rather than the message, because the message is not what was
	wrong: it is that the two calls below used to return *different* listings while meaning the
	same thing.
	"""

	_added(bound, "Something unplanned")

	proper, failed = _called(bound, "subroutine_list", today=False)

	assert not failed
	assert "Something unplanned" in proper, "with the filter off, an unplanned task is listed"

	text, refused = _called(bound, "subroutine_list", today="false")

	assert refused, "and a string saying the same thing must not turn the filter on"
	assert "today" in text and "true or false" in text, text


def test_the_change_feed_tells_an_agent_which_kinds_it_covers (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`SR#1085` on the surface it was found for, and Simon's decision of 2026-08-22.

	**This is the first call of a session**, per the skill, so a credential narrowed away from
	one of the three kinds used to fail before doing anything — refused the whole feed because
	of a kind it never asked about. It gets what it may read now, and the answer says what it
	is a feed *of*.

	**Asserted on both the empty and the populated answer**, because they are two separate
	returns and the empty one is where the sentence matters most: *nothing has changed* and
	*I am not shown that* are otherwise the same four words.

	**The sleep is the feed's own watermark, not flakiness insurance.** Events under a second
	old are withheld deliberately — a ``seq`` becomes visible at commit rather than at insert,
	so reporting the newest instantly is how a change ends up behind a cursor that has already
	passed it. Without waiting, the second call returns the *empty* branch and the populated one
	is asserted by nothing while the test reads as covering both.
	"""

	empty, failed = _called(bound, "subroutine_changes")

	assert not failed, empty
	assert "Nothing has changed." in empty, empty
	assert "covers tasks, projects and documents" in empty, empty

	_added(bound, "Something happened")
	time.sleep(subroutine.domain.events.WATERMARK.total_seconds() + 0.2)

	after, failed = _called(bound, "subroutine_changes")

	assert not failed, after
	assert "Nothing has changed." not in after, "the wait was not long enough to populate it"
	assert "covers tasks, projects and documents" in after, after
	assert "Resume with since=" in after, "the resumption line is still there"


def test_a_number_given_text_is_refused_by_name_rather_than_leaking_python (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""The loud half. ``'<' not supported between instances of 'str' and 'int'`` reached agents.

	Not even a refusal — a ``TypeError`` is not a ``SubroutineError``, so ``_explained`` fell
	through to ``str(failure)``. No field named, no remedy, and nothing saying which argument.

	``since`` is the one an agent is likeliest to hit: it is a *seq*, and it is called ``since``,
	so a date is the obvious guess and every date failed this way.
	"""

	text, failed = _called(bound, "subroutine_changes", since="2026-08-01")

	assert failed
	assert "since" in text and "whole number" in text, text
	assert "not supported between" not in text, "the Python message reached the agent"


def test_true_is_not_a_whole_number_however_python_feels_about_it (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""``isinstance(True, int)`` is true, so the check has to say otherwise itself.

	Written because the obvious implementation passes this by, and a limit of ``true`` then
	reaches a comparison as ``1`` — a listing of one row, which looks like an answer.
	"""

	text, failed = _called(bound, "subroutine_list", limit=True)

	assert failed
	assert "limit" in text and "whole number" in text, text


def test_an_item_can_still_be_named_the_way_this_program_prints_it (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""The union `A_REF` publishes, and the reason it had to be published.

	§6.2 requires ``#42`` to work: every listing this returns prints that form, so a model sends
	it back. The schema said ``integer`` alone, which made the accepted spelling invisible to a
	client reading the contract — harmless while nothing checked the types, and the first thing
	to break when something did. It was caught by the test above this one failing, which is the
	suite naming a schema that had been lying since the tool was written.
	"""

	ref = _added(bound, "Findable either way")

	for named in (ref, str(ref), f"#{ref}"):
		_answer, failed = _called(bound, "subroutine_show", ref=named)

		assert not failed, f"{named!r} was refused"

	# A union is not "anything goes": the two spellings are both published and nothing else is.
	text, refused = _called(bound, "subroutine_show", ref=True)

	assert refused
	assert "whole number or text" in text, text


#: A UUID as it renders: five hex groups separated by single hyphens. Written out because the
#: check it serves used to look for four hyphens in a row, which is a run a UUID does not have
#: (`#947`) — so the assertion could not fail on the thing its own message named.
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)

#: What to send beside a ref so the call gets as far as *reading* it. A tool that needs a body
#: stops on the missing body, and one asked to change nothing says so without ever looking the
#: item up — neither of which says anything about the argument under test. Sent whenever the
#: tool declares the property, rather than only when it is required, for that reason.
_BESIDE_A_REF: dict[str, typing.Any] = {
	"body": "Recorded.",
	"type": "relates_to",
	"title": "Renamed",
	# **Added by `#999`, which is this guard working as designed.** Declaring `parent` as a
	# ref on `subroutine_add` enrolled that tool here automatically, without anybody listing
	# it — and it stopped on the missing `text` rather than on the argument under test, which
	# is exactly what this register is for.
	"text": "Something to file",
}

#: A ref nothing answers to. Well-formed, so a tool that reads it gets as far as a lookup and
#: refuses *by that number* — which is the observation this test is built on, because a tool
#: that refused the spelling instead never sees the number at all.
_NO_SUCH_REF = 999999


def test_a_cut_body_says_where_to_carry_on_and_carrying_on_works (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#849`. A cap is only defensible together with a way to read the rest.

	The cut note offered *all of it* — a terminal, or the raw route — and never *the next part
	of it*. So for a 129 KB document the two available answers were 64 KB and 129 KB, and the
	remedy an agent was handed was the request that was already too big. `#595` shipped the
	*before* half of this, so an agent knows how big an item is; this is what it can do about
	the answer.
	"""

	body = "".join(f"line {number} of a very long document\n" for number in range(4000))

	assert len(body) > subroutine.mcp.tools.MAX_ANSWER * 1.5, "the fixture is not long enough"

	made, failed = _called(
		bound, "subroutine_document", title="A long one", body=body, type="note"
	)

	assert not failed, made

	numbered = re.search(r"#(\d+)", made)

	assert numbered is not None, made

	ref = int(numbered.group(1))
	first, failed = _called(bound, "subroutine_show", ref=ref)

	assert not failed, first
	assert len(first) <= subroutine.mcp.tools.MAX_ANSWER, len(first)

	at = re.search(r"cut here at character (\d+)", first)

	assert at is not None, f"the cut does not say where it stopped: {first[-400:]}"

	stopped = int(at.group(1))
	rest, failed = _called(bound, "subroutine_show", ref=ref, **{"from": stopped})

	assert not failed, rest
	assert f"continuing at character {stopped}" in rest, rest[:200]

	# **The join is exact**, which is the whole promise: the next page starts where the last
	# one stopped, so a reader concatenating them gets the body and not an overlap or a gap.
	assert body[stopped : stopped + 60] in rest, "the continuation does not resume where it cut"

	# **And it does not repeat the item around it.** A continuation is the rest of one field;
	# sending the links and the record again would spend the budget on what the caller has.
	assert "A long one" not in rest, rest[:200]


def test_continuing_past_the_end_of_a_body_says_so (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#1177`. A well-formed answer with nothing in it is the worst of both.

	The continuation was built as a header saying *continuing at character N* and then
	``body[N:]``, so an offset past the end gave an empty third part under a header claiming to
	resume. An agent that copied the number from a note and called again after the body had
	been shortened could not tell that from *the rest was empty* — and ``from=`` on an item with
	no body at all was ignored the same silent way.

	**Both cases, because they read identically from the outside and are different mistakes**:
	one is an offset that has gone stale, the other is a field that was never there.
	"""

	made, failed = _called(
		bound, "subroutine_document", title="A short one", body="Two lines.", type="note"
	)

	assert not failed, made

	numbered = re.search(r"#(\d+)", made)

	assert numbered is not None, made

	ref = int(numbered.group(1))
	past, failed = _called(bound, "subroutine_show", ref=ref, **{"from": 5000})

	assert not failed, past
	assert "continuing at character" not in past, "it must not claim to resume"
	assert "ends at character 10" in past, past
	assert "5000" in past, "the offset that was asked for is named back"
	assert "Nothing follows" in past, past

	# The remedy names a number that works, so the next call is a correction rather than a guess.
	assert "from=10" in past, past

	task, failed = _called(bound, "subroutine_add", text="Something with no description")

	assert not failed, task

	numbered = re.search(r"#(\d+)", task)

	assert numbered is not None, task

	empty, failed = _called(
		bound, "subroutine_show", ref=int(numbered.group(1)), **{"from": 40}
	)

	assert not failed, empty
	assert "no description" in empty, empty
	assert "continuing at character" not in empty, empty


def test_a_body_short_enough_to_fit_is_never_cut (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""So the test above is about the cap rather than about every item this tool answers."""

	made, failed = _called(
		bound, "subroutine_document", title="A short one", body="Two lines.", type="note"
	)

	assert not failed, made

	numbered = re.search(r"#(\d+)", made)

	assert numbered is not None, made

	shown, failed = _called(bound, "subroutine_show", ref=int(numbered.group(1)))

	assert not failed, shown
	assert "cut here" not in shown, shown
	assert "Two lines." in shown


def test_an_agent_reading_an_item_sees_its_parts (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#1117`. A person's `subroutine show` has rendered these since `#84`; this did not.

	That model is how a plan is expressed here — a milestone **is** an item whose blockers are
	its contents, a feature **is** just a parent item — so an agent reading a parent saw the
	prose saying *four sub-items below* and nothing under it, which reads as *the parts were
	deleted*.

	**With `#999` the two halves compound**: an agent could file a sub-task and then be unable
	to see that it had worked.
	"""

	parent = _added(bound, "Ship the release")
	first, failed = _called(bound, "subroutine_add", text="Write the changelog", parent=parent)

	assert not failed, first

	second, failed = _called(bound, "subroutine_add", text="Cut the tag", parent=parent)

	assert not failed, second

	shown, failed = _called(bound, "subroutine_show", ref=parent)

	assert not failed, shown
	assert "Sub-tasks (0 of 2 done)" in shown, shown
	assert "Write the changelog" in shown and "Cut the tag" in shown


def test_a_finished_part_is_still_shown_and_says_it_is_over (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""A parent showing two of four parts misreports the thing somebody opened it to see.

	`#84` says report the rollup and leave completion an act, which is the terminal's own rule
	here — the count is the question being put to a person, and hiding the answered half of it
	would make the count unreadable.

	**`over` rather than `done`**, for the reason the links rollup beside it gives:
	`completed_at` is non-null for a `done` *and* a `cancelled` status, so the obvious word
	asserts something about half of them that nobody did.
	"""

	parent = _added(bound, "Ship the release")
	made, failed = _called(bound, "subroutine_add", text="Write the changelog", parent=parent)

	assert not failed, made

	numbered = re.search(r"#(\d+)", made)

	assert numbered is not None, made

	finished, failed = _called(bound, "subroutine_done", ref=int(numbered.group(1)))

	assert not failed, finished

	shown, failed = _called(bound, "subroutine_show", ref=parent)

	assert not failed, shown
	assert "Sub-tasks (1 of 1 done)" in shown, shown
	assert "Write the changelog" in shown, "a finished part vanished from its parent"
	assert "(over)" in shown, shown


def test_a_document_is_not_asked_for_parts (bound: subroutine.mcp.protocol.Server) -> None:
	"""Only a task has children, so a document reaches this with nothing to ask.

	Worth driving rather than reading: the request is not made at all, and a version that
	asked and got an empty answer would look identical from the output and cost a call on
	every document an agent opened.
	"""

	made, failed = _called(
		bound, "subroutine_document", title="What we settled", body="Because.", type="decision"
	)

	assert not failed, made

	numbered = re.search(r"#(\d+)", made)

	assert numbered is not None, made

	shown, failed = _called(bound, "subroutine_show", ref=int(numbered.group(1)))

	assert not failed, shown
	assert "Sub-tasks" not in shown, shown


def test_an_agent_can_file_a_task_under_another_one (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#999`. The one part of a captured line that has no sigil and cannot be said in it.

	`+web` says where something is filed and nothing says what it is *part of*, which is the
	asymmetry that earns the bytes where a `project` argument would not. Breaking work up and
	handing the parts over is `#503`'s loop, and an agent is half that audience.
	"""

	parent = _added(bound, "Ship the release")
	answered, failed = _called(
		bound, "subroutine_add", text="Write the changelog", parent=parent
	)

	assert not failed, answered

	# **Read back through the hatch rather than through `subroutine_show`**, which does not
	# list children: the fact under test is the column, and asserting on a rendering that does
	# not carry it would be a test passing for a reason unrelated to what it names.
	children, failed = _called(
		bound,
		"subroutine_call_api",
		method="GET",
		path="/v1/tasks",
		query={"parent": str(parent), "fields": "ref,title"},
	)

	assert not failed, children
	assert "Write the changelog" in children, children


def test_the_parent_is_read_the_way_this_program_prints_a_ref (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#42` and `42` alike, which is what publishing it as a ref promises.

	Covered by the sweep below as well; this is the positive case, because that one drives a
	number nothing answers to and an argument refused for its *spelling* never reaches a
	lookup at all.
	"""

	parent = _added(bound, "Ship the release")
	answered, failed = _called(
		bound, "subroutine_add", text="Write the changelog", parent=f"#{parent}"
	)

	assert not failed, answered


def test_an_agent_can_ask_what_has_been_assigned_to_it (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#1114`. The write half of delegation was on this surface and the read half was not.

	`subroutine_update` has taken an assignee since `#392`, so an agent could hand work to
	somebody and could not ask what had been handed to it — a gap that `#501` closed at every
	layer beneath this one and that moved *up* rather than closing.
	"""

	# **The account's own generated name**, because the fixture mints one per test and the
	# *body* of an update takes a username where a listing also takes `me` — an asymmetry
	# `#1114` is not about and that a test hard-coding `si` would trip over.
	whoami, failed = _called(bound, "subroutine_whoami")

	assert not failed, whoami

	who = re.search(r"\bsi-[0-9a-f]{8}\b", whoami)

	assert who is not None, whoami

	mine = _added(bound, "For me")
	_added(bound, "For nobody")
	assigned, failed = _called(
		bound, "subroutine_update", ref=mine, assignee=who.group(0)
	)

	assert not failed, assigned

	listed, failed = _called(bound, "subroutine_list", assignee="me")

	assert not failed, listed
	assert "For me" in listed, listed
	assert "For nobody" not in listed, f"the filter narrowed nothing: {listed}"


def test_every_argument_published_as_a_ref_accepts_the_way_this_program_prints_one (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""``A_REF`` is used seven times and one argument was checked, which is how the eighth broke.

	``subroutine_link.other`` published ``["integer", "string"]`` and then read the value with
	``isinstance(other, int)`` — so the schema promised a spelling the tool refused, and the
	refusal said *pass the number in the listing*, which every listing writes ``#2``: the value
	that had just failed, handed back as the remedy. `A_REF`'s own comment describes exactly
	this the other way up, as the thing publishing the union was meant to stop.

	**Derived from the schemas rather than named**, because the test above it drove one
	argument on one tool and was true. Every property this surface types as a ref is sent one
	the way this program prints it, and an argument added tomorrow is asked without anybody
	remembering.
	"""

	real = _added(bound, "Something to point at")

	asked = 0
	unread = []

	for tool in bound.tools.values():
		properties = tool.schema.get("properties", {})
		# **Every property whose declared types *include* the ref pair, not only those equal to
		# it** (`SR#1352`). This read `== A_REF`, and the moment `subroutine_link.other` grew
		# `"array"` beside the two it dropped out of this guard's population entirely —
		# silently, because a parametrised walk cannot tell *no case failed* from *one case
		# fewer ran*. The floor below catches a walk that reads nothing and could not have
		# caught a walk that read one thing less.
		refs = [
			name
			for name, declared in properties.items()
			if set(subroutine.mcp.tools.A_REF) <= set(_types(declared))
		]

		for under_test in refs:
			arguments: dict[str, typing.Any] = {
				name: value for name, value in _BESIDE_A_REF.items() if name in properties
			}
			arguments.update(dict.fromkeys(refs, real))
			arguments[under_test] = f"#{_NO_SUCH_REF}"

			for name in tool.schema.get("required", []):
				assert name in arguments, (
					f"{tool.name} requires {name!r} and this test has no value for it"
				)

			asked += 1
			text, _failed = _called(bound, tool.name, **arguments)

			if str(_NO_SUCH_REF) not in text:
				unread.append(f"{tool.name}.{under_test} answered {text!r}")

	assert asked >= 7, f"only {asked} ref arguments were found, so this reads almost nothing"
	assert not unread, (
		"These published a ref and did not read one written the way every listing prints it: "
		+ "; ".join(unread)
	)


def _types (declared: dict[str, typing.Any]) -> list[str]:
	"""Return the JSON Schema types a property declares, however it spells them."""

	kind = declared.get("type")

	return [kind] if isinstance(kind, str) else list(kind or ())


def test_reading_one_very_large_item_does_not_spend_the_whole_context (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""``subroutine_show`` was the one answer here with no ceiling on it.

	A 200 KB document came back whole — about fifty thousand tokens, in one call, from the
	tool an agent reaches for to *check* something before writing. ``subroutine_call_api``
	refuses the same object at the same size and names three ways to narrow the request.

	**Trimmed rather than refused**, which is the difference between the two: that one answers
	in JSON, where a truncation is unparseable and still looks like a result, and this one
	answers in prose, where a cut that says so is legible. What is asserted is that the answer
	shrank, that it says it was cut, and that the *end* of it survived — the links and the
	record are written last and are what a caller most often came for.
	"""

	answered, failed = _called(
		bound,
		"subroutine_document",
		title="Enormous",
		body="paragraph. " * 20_000,
	)

	assert not failed, answered

	ref = int(answered.split()[1].lstrip("#"))
	text, failed = _called(bound, "subroutine_show", ref=ref)

	assert not failed, text
	assert len(text) <= subroutine.mcp.tools.MAX_ANSWER, f"{len(text)} characters came back"
	assert "cut here" in text, "a reader has to be able to tell this is not the whole thing"
	assert "Enormous" in text, "and the parts that are not the body survived"


def test_an_ordinary_item_is_not_marked_as_cut (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""The other half. A ceiling nothing reaches is one nobody would notice was wrong."""

	ref = _added(bound, "Something short")
	text, failed = _called(bound, "subroutine_show", ref=ref)

	assert not failed
	assert "cut here" not in text


def test_a_null_is_passed_over_rather_than_refused (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""Some clients send one for every field the user left blank.

	Every tool reads its arguments with ``.get``, so an explicit null already behaves exactly as
	an omission does. Refusing it would break those clients to no purpose, and this is the kind
	of decision that is invisible until somebody's editor is the one sending them.
	"""

	_answer, failed = _called(bound, "subroutine_list", today=None, limit=None)

	assert not failed


def test_every_declared_type_is_actually_checked (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""Driven across the whole surface, because the defect was that a schema went unused.

	A static reading of the schemas cannot see that a value reaches a comparison unvalidated —
	that is exactly what was true of all thirteen integer arguments. So each declared type is
	given a value of the wrong kind and the answer has to name the argument.

	**The floor matters more than usual here.** A check that stopped running would leave every
	assertion below satisfied by an empty loop, which is the shape this project has shipped
	twice.
	"""

	wrong_for = {"boolean": "yes", "integer": "lots", "string": 7, "object": "not-an-object"}
	checked = 0

	for tool in bound.tools.values():
		for name, specification in tool.schema.get("properties", {}).items():
			kinds = subroutine.mcp.protocol._declared_types(specification)

			# A union accepts more than one shape, so no single wrong value is wrong for it.
			if len(kinds) != 1 or kinds[0] not in wrong_for:
				continue

			text, failed = _called(bound, tool.name, **{name: wrong_for[kinds[0]]})
			checked += 1

			assert failed, f"{tool.name}.{name} accepted {wrong_for[kinds[0]]!r}"
			assert name in text, f"{tool.name}.{name} was refused without being named: {text}"

	assert checked > 20, f"only {checked} arguments were reached — has the walk stopped?"


#: Bad input that reaches a refusal, one per shape an agent actually gets wrong. Kept as data
#: so that the two tests below drive the same set: one reads what comes back, the other proves
#: the set reaches anything at all.
REFUSED = (
	("subroutine_show", {"ref": 9999}),
	("subroutine_update", {"ref": 9999, "importance": 3}),
	("subroutine_done", {"ref": 9999}),
	("subroutine_claim", {"ref": 9999}),
	("subroutine_comment", {"ref": 9999, "body": "x"}),
	("subroutine_link", {"ref": 9999, "type": "blocks", "other": 9998}),
	("subroutine_add", {"text": "x", "project": "nosuchproject"}),
	("subroutine_project", {"key": "child", "title": "Child", "parent": "nosuchproject"}),
	("subroutine_search", {"q": "x", "project": "nosuchproject"}),
)


def test_a_refusal_never_tells_an_agent_to_run_a_command (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#548`. A remote agent has no shell, which is the whole premise of `#516`.

	``subroutine_update`` on a missing ref said *"Run 'subroutine list' to see what there is"*,
	because the layers below are written for a person at a terminal. ``subroutine_show`` did
	not, because somebody had written a second message for it — so the surface already knew the
	right answer in two places and inherited the wrong one everywhere else.

	**Driven rather than scanned**, because a static reading cannot tell which of the sixty-nine
	such strings in ``src`` an agent ever meets, and the excuse list would be sixty-seven
	entries of "unreachable" that nobody could check.
	"""

	spoken = re.compile(r"\bsubroutine\s+([a-z][a-z-]*)")
	wrong = []

	for name, arguments in REFUSED:
		text, failed = _called(bound, name, **arguments)

		if not failed:
			continue

		for said in spoken.finditer(text):
			command = f"subroutine {said.group(1)}"

			if command not in subroutine.mcp.protocol.NO_TOOL_DOES_THIS:
				wrong.append(f"{name} answered with {command!r}: {text}")

	assert not wrong, "\n".join(wrong)


def test_the_refusals_this_drives_are_actually_refusals (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""The floor for the test above, which passes most comfortably against nothing at all.

	A ceiling over rendered prose is satisfied by a set of inputs that all *succeed* — and this
	set is hand-written, so an argument renamed under it turns into a silent hole rather than a
	failure. `#379` reports that as a refusal too, which is why success is what is checked.
	"""

	for name, arguments in REFUSED:
		_text, failed = _called(bound, name, **arguments)

		assert failed, f"{name} was expected to be refused and was not: {arguments}"


def test_every_command_named_instead_of_a_tool_is_a_tool (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""A translation to a name nothing answers to would be worse than the sentence it replaced.

	`#405`'s question of both lists here: `INSTEAD_OF` goes stale when a tool is renamed, and
	`NO_TOOL_DOES_THIS` goes stale when one is *added* that does the thing it excuses — which is
	the direction nobody checks, because the entry goes on reading as considered.
	"""

	catalogue = set(bound.tools)

	for command, tool in subroutine.mcp.protocol.INSTEAD_OF.items():
		assert tool in catalogue, f"{command!r} is translated to {tool!r}, which is not a tool"

	for command, why in subroutine.mcp.protocol.NO_TOOL_DOES_THIS.items():
		assert why.strip(), f"{command!r} is excused without a reason"
		assert command not in subroutine.mcp.protocol.INSTEAD_OF, (
			f"{command!r} is both excused and translated"
		)


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

	assert "Work goes to the 'acme' workspace" in _standing_up(roster, workspace="acme"
	)
	assert "Work goes to" not in _standing_up(roster)


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

	text, failed = _called(bound, "subroutine_add", text="Water the plants every fortnight")

	assert not failed, "the task was created, so this is a success"
	assert "every fortnight" in text
	assert "not a repeat this understands" in text

	# **And the readable case says nothing**, which is the other half of §6.13's obligation:
	# a complaint on every capture is a complaint nobody reads. `#94` made `every monday` a
	# rule, so this line is the control that keeps the sentence rare.
	read, _ = _called(bound, "subroutine_add", text="Water the plants every monday")

	assert "Left as written" not in read


def test_an_agent_planning_a_timed_event_keeps_the_clock_it_captured (
	session: sqlalchemy.orm.Session,
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`SR#1299` on the surface whose comment predicted this and was not reread.

	``_updated`` said *"``defer`` reads a clock and ``plan`` does not... ``starts_at`` is
	rendered by nothing at this scale yet, which is `SR#576`"*. That was true when it was
	written, it named the item that would expire it, `SR#576` shipped an event with a real
	start — and the truncation went on destroying a time the same tool had just captured one
	call earlier.

	**Read out of the store rather than off the confirmation line**, because no surface here
	renders a time on ``starts_at`` (`SR#1298`), so the line is identical either way.
	"""

	text, failed = _called(
		bound, "subroutine_add", text="Doctor's appointment on 2027-03-01 at 14:00"
	)

	assert not failed, text

	found = re.search(r"#(\d+)", text)

	assert found is not None, text

	ref = int(found.group(1))
	stored = sqlalchemy.select(subroutine.db.models.work.Task).where(
		subroutine.db.models.work.Task.ref == ref
	)
	row = session.scalars(stored).one()

	assert row.starts_at is not None
	assert row.starts_is_all_day is False, "the fixture is not a timed event"

	was = row.starts_at.timetz()

	_answer, failed = _called(bound, "subroutine_update", ref=ref, plan="2027-03-02")

	assert not failed
	session.expire_all()

	after = session.scalars(stored).one()

	assert after.starts_at is not None
	assert after.starts_at.date() == datetime.date(2027, 3, 2), "the day did not land"
	assert after.starts_at.timetz() == was, "planning it destroyed the time it was created with"
	assert after.starts_is_all_day is False, "a timed event was re-snapped to a whole day"


def test_an_agent_is_told_a_day_argument_will_not_take_a_time (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`SR#1299`. A written time was converted to a day in silence on this surface too.

	An agent has no way to notice: nothing renders a time on either column, so the call
	succeeds, the confirmation reads correctly, and the value it wrote is gone. Refusing is
	what turns that into something it can act on — and the message names the **column**, so it
	names a field a caller can actually send (`SR#1311`).
	"""

	text, _failed = _called(bound, "subroutine_add", text="The conference")
	found = re.search(r"#(\d+)", text)

	assert found is not None, text

	ref = int(found.group(1))

	answered, failed = _called(
		bound, "subroutine_update", ref=ref, until="2027-03-05T11:30:00"
	)

	assert failed, f"a time was taken by an argument that stores one and reports none:\n{answered}"
	assert "11:30" in answered, f"the refusal has to quote what it will not take:\n{answered}"

	# A day still works, so the refusal has not turned down the documented form. Both ends in
	# one call, because an end cannot be set without a start.
	_taken, failed = _called(
		bound, "subroutine_update", ref=ref, plan="2027-03-01", until="2027-03-05"
	)

	assert not failed, _taken


def test_an_agents_row_says_the_o_clock_when_the_row_carries_one (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`SR#1298` on this surface, where the line **is** the confirmation of every write.

	A doctor's appointment and a birthday rendered identically — *for 2026-12-01* on both —
	so an agent capturing *"on 2026-12-01 at 11:00"* was answered in words that would also
	have been printed had the time never been read. The skill tells it that this line is the
	check.

	**Written as ISO**, because this surface sends instants and a model should not have to
	parse a human date phrase to answer a question about one.

	Both are asserted together, so a fix that appended a time unconditionally fails on the
	birthday — an all-day start is the *first* microsecond of its day, so guessing from the
	clock would print ``T00:00`` against every whole-day row.
	"""

	timed, failed = _called(
		bound, "subroutine_add", text="Doctor's appointment on 2026-12-01 at 11:00"
	)

	assert not failed, timed
	assert "2026-12-01T11:00" in timed, f"the agent's row dropped the o'clock:\n{timed}"

	whole, failed = _called(bound, "subroutine_add", text="Anna's birthday on 2026-12-01")

	assert not failed, whole
	assert "2026-12-01" in whole, f"the agent's row dropped the day:\n{whole}"
	assert "2026-12-01T" not in whole, (
		f"a whole day was given an o'clock nobody wrote:\n{whole}"
	)


def test_a_planned_day_is_reported_where_a_deadline_is (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""``#673``, and on this surface the day was reported **nowhere at all**.

	`_line` had a cell for `due_at` and none for `starts_at`, so an agent capturing
	"Dentist appointment on monday" got back a line saying nothing about Monday. It had been
	read — `starts_at` came back set — and the only trace was the words having gone from the
	title, which is what their never having been parsed also looks like.

	**Why it is worse here than on the command line.** The skill tells an agent that this line
	is the confirmation: *"whatever it read is echoed back, so check that line."* One doing
	exactly as instructed could not tell a day set correctly from a day set wrongly, and the
	cost is asymmetric — a wrong day is discovered after it has passed. Confirming it by hand
	took the agent that reported this three further calls, ending at the raw API.

	Both dates are asked for in one line so the assertion is about them being *reported
	together*: reporting one and dropping the other is the defect one surface over.
	"""

	text, failed = _called(
		bound, "subroutine_add", text="Sand the door on 2027-03-01 by 2027-03-05"
	)

	assert not failed
	assert "2027-03-01" in text, f"the planned day was read and reported nowhere:\n{text}"
	assert "2027-03-05" in text, f"the deadline was dropped:\n{text}"


@pytest.mark.parametrize(
	"timezone", ["America/Los_Angeles", "Europe/London", "Pacific/Auckland", "UTC"]
)
def test_a_day_an_agent_is_told_is_the_day_the_day_was_meant (
	session: sqlalchemy.orm.Session,
	bound: subroutine.mcp.protocol.Server,
	timezone: str,
) -> None:
	"""An agent reads a day in the zone it was written in (`SR#1064`).

	A deadline is stored as the last microsecond of its day and a plan as the first, both
	local to whoever set them — so ``.date()`` on the stored UTC instant reported a Los
	Angeles deadline **a day late** and a London plan **a day early**. The terminal and the
	browser convert (`SR#773`); this surface did not.

	**It matters most here.** The skill names this line as the check — *"whatever it read is
	echoed back, so check that line"* — so an agent doing exactly as instructed was told the
	wrong day by the sentence whose job is to be right about it.

	Driven per zone because the defect is invisible in UTC, which is every CI job. UTC is kept
	in the list so a fix that drops the conversion entirely fails here rather than passing
	three cases and looking careful.
	"""

	who = session.scalars(sqlalchemy.select(subroutine.db.models.identity.User)).all()
	assert len(who) == 1, "the fixture's one account is what carries the zone"

	who[0].timezone = timezone
	session.flush()

	# **August rather than March, and that is measured rather than tidy.** London is GMT in
	# March, so both instants land on their own UTC date and the zone cannot show the defect
	# — it passed against the original code on a spring date. In August it is BST and a plan
	# stored at local midnight is 23:00 the evening before.
	meant = "2027-08-05"
	ref = _added(bound, f"Sand the door on 2027-08-01 by {meant}")

	listed, failed = _called(bound, "subroutine_list")
	assert not failed, listed

	shown, failed = _called(bound, "subroutine_show", ref=ref)
	assert not failed

	for surface, answer in (("the listing row", listed), ("show", shown)):
		assert meant in answer, (
			f"{timezone}: {surface} reported a deadline set for {meant} as:\n{answer}"
		)
		assert "2027-08-01" in answer, (
			f"{timezone}: {surface} reported a plan set for 2027-08-01 as:\n{answer}"
		)


@pytest.mark.parametrize(
	("timezone", "at", "expected"),
	[
		("Pacific/Auckland", "2027-08-05T13:00:00Z", datetime.date(2027, 8, 6)),
		("America/Los_Angeles", "2027-08-05T04:00:00Z", datetime.date(2027, 8, 4)),
		("UTC", "2027-08-05T13:00:00Z", datetime.date(2027, 8, 5)),
	],
)
def test_a_day_an_agent_writes_is_read_in_the_account_s_zone (
	session: sqlalchemy.orm.Session,
	bound: subroutine.mcp.protocol.Server,
	monkeypatch: pytest.MonkeyPatch,
	timezone: str,
	at: str,
	expected: datetime.date,
) -> None:
	"""The write half of `SR#1064`, and its zone was nobody's — decision `SR#1088`.

	``_moment`` read ``config.system_timezone()``, under a comment calling it *"the client's
	own zone"*. That was true of a stdio adapter on the agent's own machine and false of every
	relayed connection: since `SR#539` this module runs **inside the instance**, so the word an
	agent wrote was resolved against the server's ``/etc/localtime``.

	The account's zone decides it now, published on ``/v1/me`` as ``reader_timezone`` so that
	no client has to hold a copy of §6.5 to know what ``today`` means (`SR#925`).

	**Only the *day* discriminates, and that is worth knowing before changing this.** The first
	version asserted the stored instant was midnight in the account's zone — and it passed
	against the original code, because the adapter resolved ``today`` to a bare ``date`` and
	the *instance* then turned that date into midnight in the account's zone. So the boundary
	was already right and the day was wrong, which is the one thing an assertion about the
	boundary cannot see.

	**The clock is frozen, and each zone gets an instant chosen to make it disagree.** On a
	real clock every zone shares a date with UTC for part of every day, so between 07:00 and
	12:00 UTC neither Auckland nor Los Angeles disagrees and the whole parametrisation is
	vacuous for five hours — a suite that looks thorough and asserts nothing, which is
	`SR#737`'s shape. Auckland at 13:00 UTC is already tomorrow; Los Angeles at 04:00 UTC is
	still yesterday. **UTC is the control**: it cannot show the defect, and it is here so that a
	fix which hard-codes one zone fails rather than passing two cases and looking careful.
	"""

	who = session.scalars(sqlalchemy.select(subroutine.db.models.identity.User)).all()
	assert len(who) == 1, "the fixture's one account is what carries the zone"

	who[0].timezone = timezone
	session.flush()

	frozen = datetime.datetime.fromisoformat(at)
	monkeypatch.setattr(subroutine.db.types, "utcnow", lambda: frozen)

	assert frozen.astimezone(zoneinfo.ZoneInfo(timezone)).date() == expected, (
		"the case is built so the account's today differs from the runner's; if this fires "
		"the instant and the expected day have come apart"
	)

	ref = _added(bound, "Sand the door")

	changed, failed = _called(bound, "subroutine_update", ref=ref, plan="today")
	assert not failed, changed

	row = session.scalars(
		sqlalchemy.select(subroutine.db.models.work.Task).where(
			subroutine.db.models.work.Task.ref == ref
		)
	).one()

	assert row.starts_at is not None, "the plan reached no column at all"
	assert row.starts_is_all_day, "'today' names a whole day, not an o'clock"

	stored = row.starts_at.astimezone(zoneinfo.ZoneInfo(timezone)).date()

	assert stored == expected, (
		f"{timezone}: at {at} it is {expected} there, and an agent planning something for "
		f"'today' had it stored as {stored} — so the word was read in some other zone"
	)


@pytest.mark.parametrize(
	("timezone", "at", "expected"),
	[
		("UTC", "2026-08-03T21:30:00+00:00", "2026-08-03"),
		("Pacific/Auckland", "2026-08-03T21:30:00+00:00", "2026-08-04"),
		("America/Los_Angeles", "2026-08-03T02:30:00+00:00", "2026-08-02"),
	],
)
def test_a_comment_an_agent_reads_is_dated_where_the_account_is (
	session: sqlalchemy.orm.Session,
	bound: subroutine.mcp.protocol.Server,
	timezone: str,
	at: str,
	expected: str,
) -> None:
	"""The read half of the same rule — `SR#1091`, decision `SR#1088`.

	Its sibling above covers a day an agent **writes**. This covers a moment it **reads**: a
	comment's ``created_at`` is a point in time and has no day until somebody names a zone,
	and this took ``.date()`` on the stored value, which is UTC — the server's, for every
	relayed connection since `SR#539`.

	**Both directions, because they fail in opposite ones**, with UTC as the control so that a
	change dropping the conversion fails here rather than passing one case. The instant differs
	per case for the reason the write test records: for five hours of every day no zone in this
	table disagrees with UTC about the date.
	"""

	who = session.scalars(sqlalchemy.select(subroutine.db.models.identity.User)).all()
	assert len(who) == 1, "the fixture's one account is what carries the zone"

	who[0].timezone = timezone
	session.flush()

	ref = _added(bound, "Rehang the gate")

	written, failed = _called(bound, "subroutine_comment", ref=ref, body="Hinges are seized.")
	assert not failed, written

	session.execute(
		sqlalchemy.update(subroutine.db.models.activity.Comment).values(
			created_at=datetime.datetime.fromisoformat(at)
		)
	)
	session.flush()

	shown, failed = _called(bound, "subroutine_show", ref=ref)
	assert not failed, shown
	assert "Hinges are seized." in shown, f"the comment is not in the answer at all:\n{shown}"

	line = next(part for part in shown.splitlines() if "Hinges are seized." in part)

	assert line.startswith(expected), (
		f"{timezone}: a comment written at {at} was dated as:\n{line}"
	)


def test_a_listing_an_agent_asked_for_says_when_it_was_cut (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""docs/design.md §12.2a on the one branch that did not follow it (`SR#1064`'s neighbour, `SR#1071`).

	The **agenda** branch of this same function appends *"N more not shown"*, ten lines away.
	The listing branch returned `ordered[:limit]` and nothing, so an agent asking for three got
	three and could not tell the answer from the cut — which is the difference between *there
	are three things to do* and *here are three of them*.

	`Listing.has_more` was added in `SR#1037` for exactly this and was read by nothing here.
	"""

	for index in range(6):
		_added(bound, f"Something to do number {index}")

	cut, failed = _called(bound, "subroutine_list", limit=3)

	assert not failed
	assert len([line for line in cut.splitlines() if line.startswith("#")]) == 3
	assert "More matched than are shown" in cut, (
		f"three of six were shown and nothing said so:\n{cut}"
	)


def test_a_listing_that_is_everything_says_nothing_extra (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""The ordinary day, and the reason a footer is not free.

	Every line here is context the model carries, so a caption paid on every listing to cover
	the case where there is nothing to say is the trade `SR#1010` refused for the empty capture
	line. It is also the falsification that matters: a footer appended unconditionally passes
	the test above and is wrong on most calls.
	"""

	for index in range(3):
		_added(bound, f"Something to do number {index}")

	whole, failed = _called(bound, "subroutine_list", limit=20)

	assert not failed
	assert "More matched" not in whole, (
		f"a complete listing claimed there was more:\n{whole}"
	)


def test_an_ordinary_capture_carries_only_the_line_that_is_news (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""Context economy: a caption for the empty case would be paid on every capture.

	**Still the rule, and it decided `#1438`.** Where the caller wrote ``+web`` the echo
	already says where the work went; repeating it would be the wallpaper §12.2a warns about,
	on the surface that pays by the byte. So the destination is said only where **nobody**
	chose — which is not the empty case, it is the one fact the caller cannot otherwise get.

	**Asserted as an exact count rather than as an absence**, so a fourth line is an act
	somebody takes rather than one that accumulates. This used to read ``"\n" not in text``,
	which was broader than the sentence above it and would have refused any line at all.
	"""

	text, failed = _called(bound, "subroutine_add", text="Fix the boiler by friday")

	assert not failed
	assert text.count("\n") == 1, text
	assert text.endswith("filed in inbox"), text


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

	# **Tokens sharing a reason are joined into one clause.** Both of these are phrases the
	# grammar cannot read at all, which is what puts them in the same sentence.
	several = subroutine.domain.capture.explain(("every fortnight", "every 3rd"))

	assert several is not None
	assert "every fortnight, every 3rd" in several

	# **And two reasons are two clauses, which this pair used to hide** (`SR#1401`). It was
	# `("every monday", "every 3rd")` — one phrase the grammar reads and one it does not, so
	# once *read out of the middle of a sentence* became a separate reason they stopped
	# belonging in one sentence. Offering *try 'every day', 'every 14 days'…* to somebody who
	# never wanted a repeat is a refusal asserting a cause it has not established, so the two
	# must not be merged back to keep this shape.
	mixed = subroutine.domain.capture.explain(("every monday", "every 3rd"))

	assert mixed is not None
	assert "every monday" in mixed
	assert "every 3rd" in mixed
	assert "every monday, every 3rd" not in mixed, (
		f"one clause for two different reasons, so one of them is wrong:\n{mixed}"
	)


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
	# **A UUID carries single hyphens, so this looked for something one never contains**
	# (`#947`, cold review `#927`'s L-3). It read `"-" * 4 not in text` under a message about a
	# leaked UUID, and would have passed with every id in the output. Matched by shape now.
	assert not _UUID.search(text), f"a UUID leaked into the record: {text}"


def test_add_tells_the_agent_what_the_grammar_read (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""``#135``, and this is the surface where it matters most.

	An agent is the caller most likely to have written something it believes was understood —
	the same reason ``#115`` put the *unparsed* sentence here. Being told only what was left as
	written answers the rarer half: an agent that captured ``+web`` and got no confirmation has
	to spend a second call reading the task back to find out whether it worked.
	"""

	answered, failed = _called(bound, "subroutine_add", text="Fix it !4/2 ~2h")

	assert not failed, answered
	assert "!4/2" in answered
	assert "~2h" in answered

	# And an ordinary line carries no echo at all, so a caller who wrote nothing structural is
	# told nothing about structure. The destination line beneath it is `#1438` and is a
	# different fact — see `test_an_ordinary_capture_carries_only_the_line_that_is_news`.
	plain, failed = _called(bound, "subroutine_add", text="Buy milk")

	assert not failed, plain
	assert "(read " not in plain, plain
	assert plain.split("\n")[0].endswith("Buy milk"), plain


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

	# **The first line.** `#1438` added a line beneath it, and partitioning the whole answer
	# would swallow that into the echo — which is a property of this test's fixture rather than
	# of the rule. `test_the_echo_stays_beside_the_title_when_another_line_is_added` is the
	# half that checks the adjacency when there is something after it.
	head, separator, echoed = made.split("\n")[0].partition("  (read ")

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
		'project = "elsewhere"\n', encoding="utf-8"
	)
	os.chdir(tmp_path)

	text, failed = _called(bound, "subroutine_add", text="Filed anyway")

	assert not failed, text
	assert "Added" in text

	# And it says so, for the reason this function says everything else out loud: the agent is
	# holding a repository whose file claims one thing and an instance that says another.
	assert "elsewhere" in text
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

	_called(bound, "subroutine_project", key="web", title="Website")

	(tmp_path / subroutine.directory.FILE_NAME).write_text(
		'connection = "somewhere-else"\nproject = "web"\n', encoding="utf-8"
	)
	os.chdir(tmp_path)

	text, failed = _called(bound, "subroutine_add", text="Filed where this session points")

	assert not failed, text
	assert "Added" in text

	# Not "in web, from .subroutine" — the marker was never consulted, so nothing about it is
	# reported as having decided anything.
	assert subroutine.directory.FILE_NAME not in text
	assert "in WEB" not in text


def test_a_document_is_filed_where_the_checkout_says (
	bound: subroutine.mcp.protocol.Server, tmp_path: pathlib.Path
) -> None:
	"""Item ``#1219``. ``subroutine_add`` read the marker from the start; this never did.

	So a conclusion written from a marked repository went to the workspace Inbox while a task
	captured in the same directory, in the same session, went to the project. Two ways of
	creating something with a project, written months apart, and nothing compared them.

	**Falsified against the original code**: put ``project=_text(arguments, "project")`` back in
	``_wrote`` and both assertions fail — the document is in the Inbox, and the answer says
	nothing at all about where it went, which is the half that kept this invisible.
	"""

	_called(bound, "subroutine_project", key="web", title="Website")

	(tmp_path / subroutine.directory.FILE_NAME).write_text(
		'project = "web"\n', encoding="utf-8"
	)
	os.chdir(tmp_path)

	made, failed = _called(
		bound,
		"subroutine_document",
		title="Why the cache is keyed on the version",
		body="Because a plugin copy is stored under it.",
		type="decision",
	)

	assert not failed, made

	# The write half: it is actually there, read back from the instance rather than from the
	# sentence the tool just printed.
	shown, _ = _called(bound, "subroutine_show", ref=_numbered(made))

	assert "web" in shown, shown

	# The reporting half, which is the one that made five of these accumulate unnoticed.
	assert subroutine.directory.FILE_NAME in made, made


def test_a_project_argument_outranks_the_checkout (
	bound: subroutine.mcp.protocol.Server, tmp_path: pathlib.Path
) -> None:
	"""Somebody speaking now beats a file on disk (§13.7a), same as a ``+key`` in a line.

	**Falsified against the fix**: drop ``overridden`` from ``_wrote``'s ``_checkout`` call and
	this fails, filing into ``web`` a document the caller explicitly put in ``docs``.
	"""

	_called(bound, "subroutine_project", key="web", title="Website")
	_called(bound, "subroutine_project", key="docs", title="Documentation")

	(tmp_path / subroutine.directory.FILE_NAME).write_text(
		'project = "web"\n', encoding="utf-8"
	)
	os.chdir(tmp_path)

	made, failed = _called(
		bound,
		"subroutine_document",
		title="How the guide is published",
		body="From the repository.",
		project="docs",
	)

	assert not failed, made

	shown, _ = _called(bound, "subroutine_show", ref=_numbered(made))

	assert "docs" in shown, shown

	# And nothing claims the checkout decided it, because it did not.
	assert subroutine.directory.FILE_NAME not in made, made


def test_revising_a_document_does_not_move_it_to_the_checkouts_project (
	bound: subroutine.mcp.protocol.Server, tmp_path: pathlib.Path
) -> None:
	"""Omitted means unchanged (§8.3), and that has to survive the fix above.

	A revision consulting the marker would move somebody else's document because of where the
	editor happened to be standing — the mirror defect of the one ``#1219`` fixes, and the easy
	way to introduce it while fixing that one.
	"""

	_called(bound, "subroutine_project", key="web", title="Website")
	_called(bound, "subroutine_project", key="docs", title="Documentation")

	made, failed = _called(
		bound,
		"subroutine_document",
		title="Where the errors page comes from",
		body="Generated.",
		project="docs",
	)

	assert not failed, made

	ref = _numbered(made)

	(tmp_path / subroutine.directory.FILE_NAME).write_text(
		'project = "web"\n', encoding="utf-8"
	)
	os.chdir(tmp_path)

	revised, failed = _called(
		bound, "subroutine_document", ref=ref, body="Generated by registry_markdown()."
	)

	assert not failed, revised

	shown, _ = _called(bound, "subroutine_show", ref=ref)

	assert "docs" in shown, shown
	assert "web" not in shown, shown


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
	roster: subroutine.connections.Roster, workspace: str | None = None
) -> str:
	"""Return what a stdio session is told, which two parties now write between them (`#539`).

	The instance writes the paragraph and knows nothing about the caller's roster; the adapter
	corrects the name and restores the sentence about the instances this session is *not*
	reaching, which the far end could not have written.

	**The instance is given a different label on purpose.** Passing the connection's own name
	on both sides would leave the rewrite untested and every assertion below passing — the
	shape `#492` is about. ``the-instance`` stands for what a server calls itself.
	"""

	chosen = roster.require(roster.default)
	served = subroutine.mcp.session.over(
		unittest.mock.MagicMock(spec=subroutine.clients.base.Client),
		label="the-instance",
		workspace=workspace,
	)

	assert served.instructions is not None, "a session is always told where it is"

	answered = subroutine.mcp.relay._in_this_machines_terms(
		{"result": {"instructions": served.instructions}},
		chosen.label,
		tuple(name for name in roster.names if name != chosen.name),
	)

	said = answered["result"]["instructions"]

	assert "the-instance" not in said, (
		"the instance's own name for itself reached the caller, who has never heard it"
	)

	return str(said)


def test_the_instructions_name_the_instances_this_session_cannot_reach (
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""``#276``. Naming only the bound one is what let an agent be sure it knew where it was.

	The sentence was true — it said 'on connection Local' — and nothing in it suggested the
	name was one of several, so there was no reason to ask. `subroutine connections` answers
	it from the command line and has no equivalent here, which is `#232`'s gap from the other
	side.
	"""

	said = _standing_up(_roster("local", "work", default="local"))

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

	said = _standing_up(_roster("local", default="local"))

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

	``run`` is driven for real, with only the forwarding stubbed — that needs a database or a
	socket, and the question here is purely which connection is chosen. A first version
	asserted ``(None or roster.default) == "local"``, which re-implemented the expression under
	test and would have passed with the defect still in place.

	**The choice moved with the transport** (`#539`): it was ``session.build``'s and is now
	``relay.run``'s. The property is the one thing about that move which must not change, so
	the test moved with it rather than being deleted alongside the function it drove.
	"""

	roster = _roster("local", "work", default="local")
	handed: list[str] = []

	monkeypatch.setattr(
		subroutine.context, "read", lambda: {"connection": "work", "workspace": "acme"}
	)
	monkeypatch.setattr(subroutine.connections, "roster", lambda settings: roster)
	monkeypatch.setattr(
		subroutine.mcp.relay,
		"answering",
		lambda connection, roster, settings, workspace=None: handed.append(connection.name),
	)

	# The stored context says 'work'. Asserted first, so a fixture that failed to set it
	# cannot let the real assertion pass for the wrong reason.
	assert subroutine.context.resolve(roster).connection == "work"

	subroutine.mcp.relay.run(
		io.StringIO(""),
		io.StringIO(),
		settings=subroutine.config.Settings(dev_mode=True),
	)

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

	for key in ("here", "elsewhere"):
		_answer, failed = _called(bound, "subroutine_project", key=key, title=key.title())

		assert not failed, _answer

	_added(bound, "Work in this project +here")
	_added(bound, "Work in the other one +elsewhere")

	everything, failed = _called(bound, "subroutine_list")

	assert not failed
	assert "Work in this project" in everything
	assert "Work in the other one" in everything, "both are visible without the argument"

	narrowed, failed = _called(bound, "subroutine_list", project="here")

	assert not failed
	assert "Work in this project" in narrowed
	assert "Work in the other one" not in narrowed, "the argument has to actually narrow"

	found, failed = _called(bound, "subroutine_search", q="Work", project="here")

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
	instructions = _standing_up(roster)

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

	**This asserted `Program X, instance X` until `#564`, which is the shape that was wrong.**
	These tools run where the *instance* runs, so with no plugin in the environment there is
	nothing here that knows what the caller is running — and reporting the instance's own
	version under the word *Program* was `#381`'s comparison answering with one value supplied
	twice. An agent read that and concluded there was no version problem.
	"""

	text, failed = _called(bound, "subroutine_whoami")

	assert not failed
	assert f"Instance {subroutine.__version__}, schema " in text

	assert f"Program {subroutine.__version__}" not in text, (
		"the instance's own version is being reported as the caller's, which is `#564`"
	)
	assert "not visible from here" in text, (
		"the answer does not say the caller's own versions were never compared"
	)


def test_asking_who_you_are_beside_the_caller_reports_all_three (
	bound: subroutine.mcp.protocol.Server, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#564`'s other half: where the caller *can* be seen, `#381`'s check is genuinely three-way.

	A local connection is answered in the relay's own process — the process the plugin started
	— so its environment is the caller's, and `CLAUDE_PLUGIN_ROOT` being readable is the proof
	of that. Without this case the fix above could be "never report anything", which would
	close `#564` by deleting the feature it is about.
	"""

	monkeypatch.setenv(
		subroutine.installations.PLUGIN_ROOT,
		str(pathlib.Path(__file__).resolve().parent.parent / "plugins" / "subroutine"),
	)

	text, failed = _called(bound, "subroutine_whoami")

	assert not failed
	assert "Plugin " in text, f"a visible plugin is not reported: {text}"
	assert f"program {subroutine.__version__}" in text
	assert f"instance {subroutine.__version__}" in text
	assert "not visible from here" not in text


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

	# `#564`: the version answer is still here, and it is the honest one — this branch runs
	# server-side like every other, so the caller's own installations are not readable from it.
	assert f"Instance {subroutine.__version__}" in text


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

	**It is a `ref` argument on this tool since `#822`, and that reverses what was decided
	here.** The objection recorded at the time was that the CLI has `doc create` and `doc edit`
	as two commands, so create-or-update would make the surfaces disagree about whether writing
	and revising are one act or two. What answered it is `subroutine_claim`'s precedent: taking
	and giving back are one capability in two directions on this surface and two verbs at the
	terminal, deliberately, because a model reads tool *names* and a person reads a help page.

	Of the two silent failures it also named, one is answered and one is not. **Omitting the
	ref no longer writes a duplicate** — with `title` no longer required by the schema, neither
	given is refused by name. **A stale ref still overwrites somebody's conclusion**, because
	nothing here sends `expected_version` and `doc edit` is a whole-body replace; the browser
	does send one and argues it matters more on a document than on a task. That is `#842`, and
	it is filed rather than folded in because it needs the byte cap raised.

	**What it must name changed with `#822`, and this test used to demand the defect.** It
	asserted the description said ``doc edit`` — a *shell command*, on the surface whose whole
	premise is a reader with no shell (`#516`). `#548` settled that class for refusals, and
	``protocol.INSTEAD_OF`` translates them; it does not reach a tool description, so this was
	the one place the rule did not arrive. The intent is unchanged and is the part worth
	keeping: the only tool that writes a document has to say how one is revised, or an agent
	reasonably concludes it cannot be.
	"""

	answered = _exchange(bound, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
	tools = {tool["name"]: tool for tool in answered[0]["result"]["tools"]}
	described = tools["subroutine_document"]["description"]

	assert "revise" in described.lower(), (
		"the only tool that writes a document must say how one is revised, or an agent "
		"reasonably concludes it cannot be"
	)

	# **And it must name something this reader can reach**, which is `#134`/`#136`/`#138`'s
	# lesson aimed at the right registry. `test_plugin` asks this of the skill against the tool
	# catalogue and the Typer app; a description on *this* surface may only name the former,
	# because an agent holding a URL and a token has no other.
	#
	# **This tool's own arguments count, and did not have to before `#822`.** The answer used
	# to live somewhere else — a shell command, then `subroutine_call_api` — so naming a *tool*
	# was the only way to be followable. Revising is an argument here now, which is a better
	# answer to the same question and one this check would have called unreachable.
	named = {word.strip(".,'\"") for word in described.split()}
	reachable = (
		set(tools)
		| set(tools["subroutine_document"]["inputSchema"]["properties"])
		| {"subroutine://meta", "subroutine://docs/examples"}
	)

	assert named & reachable, (
		f"the description names no tool this surface has: {sorted(named)}"
	)

	for word in named:
		if word.startswith("subroutine_"):
			assert word in tools, f"{word!r} is named by a tool description and does not exist"


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
			"""Note that this method was called, then call the real one."""

			self.called.append(name)

			return attribute(*args, **kwargs)

		return recording


#: Handlers that hand a client a ``project`` and deliberately do **not** read the checkout.
#: Each entry is a decision with its reason; deleting one is what closes it, and
#: :func:`test_no_excused_handler_reads_the_checkout` fails when a reason expires.
NOT_FROM_THE_CHECKOUT = {
	"_listed": (
		"A marker decides where a write goes and never what a read shows. That is `use`'s own "
		"rule — reads span everything reachable, writes target the current context — and a "
		"listing that silently narrowed to the checkout's project would hide work from an "
		"agent standing in the wrong directory, which is the failure a listing cannot report."
	),
}


def _files_under_a_project () -> dict[str, set[str]]:
	"""Return each handler that hands a client a ``project``, and the methods it hands it to.

	**Derived from what makes a handler a member rather than from a list** (`#405`): passing a
	project to a client call *is* filing something under one, so a handler added in 2027 is in
	this population the moment it is written. A hand-kept list is the thing `#1219` was — two
	ways of creating an item with a project, months apart, and nothing comparing them.
	"""

	source = pathlib.Path(subroutine.mcp.tools.__file__).read_text(encoding="utf-8")
	found: dict[str, set[str]] = {}

	for node in ast.walk(ast.parse(source)):
		if not isinstance(node, ast.FunctionDef):
			continue

		for inner in ast.walk(node):
			if (
				isinstance(inner, ast.Call)
				and isinstance(inner.func, ast.Attribute)
				and isinstance(inner.func.value, ast.Name)
				and inner.func.value.id == "client"
				and any(keyword.arg == "project" for keyword in inner.keywords)
			):
				found.setdefault(node.name, set()).add(inner.func.attr)

	return found


def _reads_the_checkout (name: str) -> bool:
	"""Return whether one handler in ``mcp/tools.py`` consults the checkout marker."""

	source = pathlib.Path(subroutine.mcp.tools.__file__).read_text(encoding="utf-8")

	for node in ast.walk(ast.parse(source)):
		if not isinstance(node, ast.FunctionDef) or node.name != name:
			continue

		return any(
			isinstance(inner, ast.Call)
			and isinstance(inner.func, ast.Name)
			and inner.func.id == "_checkout"
			for inner in ast.walk(node)
		)

	return False


def test_every_handler_that_files_under_a_project_reads_the_checkout () -> None:
	"""Item ``#1219``, as a guard rather than as a comment.

	``subroutine_add`` read the ``.subroutine`` marker and ``subroutine_document`` did not, for
	a month, and no test could see it: ``test_reach`` is per *method*, so a behaviour that
	differs **inside** two handlers both surfaces already have is structurally invisible to it
	(`#149`, `#178`). Five documents were filed into the wrong project before a person noticed
	by reading a page in the browser.

	**Falsified by breaking the scan** as well as the subject: point ``_files_under_a_project``
	at an empty tree and the stale-entry test below fails, so a walk that reads nothing cannot
	report success (`#405`).
	"""

	filing = _files_under_a_project()

	assert filing, "the scan found no handler at all, so it is measuring nothing"

	missing = {
		name
		for name in filing
		if name not in NOT_FROM_THE_CHECKOUT and not _reads_the_checkout(name)
	}

	assert not missing, (
		f"{sorted(missing)} hand a client a project without reading the checkout. Call "
		f"_checkout, or add an entry to NOT_FROM_THE_CHECKOUT saying why not."
	)


def test_no_excused_handler_reads_the_checkout () -> None:
	"""An excuse must stop being needed when its reason expires (`#405`).

	Three ways an entry can go stale, and this catches all of them: the handler is gone, it no
	longer hands a client a project, or it has since started reading the checkout — the last
	being the one that leaves a written reason standing beside code that contradicts it.
	"""

	filing = _files_under_a_project()

	for name in NOT_FROM_THE_CHECKOUT:
		assert name in filing, (
			f"NOT_FROM_THE_CHECKOUT names {name!r}, which no longer hands a client a project. "
			f"Delete the entry."
		)
		assert not _reads_the_checkout(name), (
			f"NOT_FROM_THE_CHECKOUT says {name!r} does not read the checkout, and it does. "
			f"Delete the entry."
		)


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
			"subroutine_journal": {},
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


def _resource (client: subroutine.clients.local.Client, uri: str) -> str:
	"""Return what one resource publishes, through the real ``resources/read`` exchange."""

	with client:
		server = subroutine.mcp.protocol.Server(
			subroutine.mcp.tools.catalogue(client),
			name="subroutine",
			version="0",
			resources=subroutine.mcp.tools.references(client),
		)
		answered = _exchange(
			server,
			{"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}},
		)

	assert "error" not in answered[0], answered[0]

	text: str = answered[0]["result"]["contents"][0]["text"]

	return text


def test_the_vocabulary_resource_removes_what_it_cannot_know_rather_than_emptying_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#496`, and it needs two workspaces to happen at all — `#177`'s lesson.

	Measured against a real instance: this returned ``statuses: {}``, ``item_types: {}``,
	``link_types: []`` and no tags, from the one document whose stated job is publishing them.
	``/v1/meta`` answers an unbound request that way on purpose and is right to, because an HTTP
	caller can read ``workspaces`` and ask again — **a resource has no second call**, so here the
	emptiness is terminal and reads as a claim that this workspace has no vocabulary.

	The fix is a subtraction, not a refusal: most of this document is instance-wide and correct,
	and an absent key makes no claim where an empty one does.
	"""

	client, _first, _second = _two_workspaces(session)

	published = json.loads(_resource(client, "subroutine://meta"))

	for section in subroutine.mcp.tools.PER_WORKSPACE:
		assert section not in published, (
			f"{section!r} is per workspace and no workspace was chosen, so publishing it at all "
			f"— even empty — tells an agent something false about this installation"
		)

	assert "acme" in published["vocabulary_not_shown"], "it must name what to choose between"
	assert "workspace_id" in published["vocabulary_not_shown"], "and how to ask for one"

	# The rest is instance-wide and is exactly what `call_api` needs, so refusing the whole read
	# would have thrown away the majority of a document that was mostly right.
	assert published["listings"]["task"]["filters"]
	assert published["error_codes"]
	assert published["grammars"]


def test_the_conventions_resource_answers_an_unbound_session_rather_than_refusing (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#496`'s other half, which the item did not know about and reading found.

	Two resources, one unset workspace, opposite failures: the vocabulary published an empty
	vocabulary, and this one raised the ordinary ambiguity refusal — *"it needs to say which"*,
	whose remedy is to pass ``workspace_id``. **A resource takes no arguments**, so that is
	advice its reader cannot act on, and the refusal is as much a dead end as the false answer.
	"""

	client, _first, _second = _two_workspaces(session)

	published = _resource(client, "subroutine://conventions")

	assert "acme" in published
	assert "workspace" in published


def test_a_bound_session_still_gets_the_whole_vocabulary (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The other side of it, so the fix above cannot be "always leave the vocabulary out"."""

	client, first, _second = _two_workspaces(session)

	with client:
		server = subroutine.mcp.protocol.Server(
			subroutine.mcp.tools.catalogue(client, workspace=first),
			name="subroutine",
			version="0",
			resources=subroutine.mcp.tools.references(client, workspace=first),
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

	published = json.loads(answered[0]["result"]["contents"][0]["text"])

	assert published["statuses"], "a session that named a workspace must get its keys"

	# **Null rather than absent since `SR#627`**, which gave the field to
	# :class:`subroutine.views.Meta` so `/v1/meta` could answer the same question. It is
	# serialised with the rest of the model now, so a bound session carries the key with nothing
	# in it — and both spellings mean the same thing, which is that nothing was withheld.
	assert published["vocabulary_not_shown"] is None


def test_an_agent_can_reach_a_route_no_tool_covers (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#485`, decision `#484`. The escape hatch, doing the thing it exists for.

	``PATCH /v1/documents/{ref}`` is the example the decision was argued from: `#293`'s reporter
	could write a conclusion and not revise one, and there was no tool for it and no room for a
	tool. This is what "thirteen of twenty were excluded for budget" stops meaning.
	"""

	written, failed = _called(bound, "subroutine_document", title="A first conclusion")

	assert not failed, written

	ref = int(written.split()[1].lstrip("#"))
	answered, failed = _called(
		bound,
		"subroutine_call_api",
		method="patch",
		path=f"/v1/documents/{ref}",
		body={"title": "A better conclusion"},
	)

	assert not failed, answered
	assert answered.startswith("200 "), answered
	assert "A better conclusion" in answered

	read, failed = _called(bound, "subroutine_show", ref=ref)

	assert not failed, read
	assert "A better conclusion" in read, "the write did not land"


def test_the_three_refused_routes_name_what_to_do_instead (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""Decision `#484`'s deny-list, and the half that stops it being a wall.

	A refusal saying only "not here" strands an agent mid-task. These three exist at a terminal,
	so naming the command is the difference between a dead end and a hand-off — review dimension
	4 applied to our own guard rather than to the API's.
	"""

	for method, path in (
		("POST", "/v1/workspaces"),
		("PATCH", "/v1/workspaces/somewhere"),
		("POST", "/v1/projects/SR/move"),
	):
		answered, failed = _called(bound, "subroutine_call_api", method=method, path=path)

		assert failed, f"{method} {path} was allowed through"
		assert "subroutine" in answered, f"{method} {path} refuses without naming an alternative"
		assert "cannot be undone" in answered


def test_a_route_that_merely_looks_like_a_refused_one_is_allowed (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""The other side, so the deny-list is a rule rather than a substring search.

	``POST /v1/projects`` is not ``POST /v1/projects/{key}/move``, and ``GET /v1/workspaces`` is
	not ``POST`` of the same path. A guard matching too widely would refuse ordinary work and
	pass every test above.
	"""

	answered, failed = _called(bound, "subroutine_call_api", method="GET", path="/v1/workspaces")

	assert not failed, answered
	assert answered.startswith("200 ")

	made, failed = _called(
		bound,
		"subroutine_call_api",
		method="POST",
		path="/v1/projects",
		body={"key": "NEW", "title": "Somewhere to file things"},
	)

	assert not failed, made
	assert made.startswith("201 ")


def test_an_enormous_answer_is_refused_rather_than_truncated (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""Simon's decision, 2026-08-05, and the reason it is a refusal.

	**A truncated JSON document is unparseable and reads as an answer.** The caller gets
	something shaped like a result and has no way to tell — which is worse than being told to
	ask again, and is the family of failure this project keeps recording.

	The refusal names ``fields``, ``limit`` and ``format``, which is also the one place an agent
	reliably learns §14.10's shaping exists at all.
	"""

	for index in range(3):
		_added(bound, f"Something to fill the page {index}")

	# Rather than making a real 64 KB response, which would need hundreds of rows: the rule is
	# about the measured length of what came back, so the measurement is what gets exercised.
	original = subroutine.mcp.tools.MAX_ANSWER

	try:
		subroutine.mcp.tools.MAX_ANSWER = 10
		answered, failed = _called(
			bound, "subroutine_call_api", method="GET", path="/v1/tasks"
		)

	finally:
		subroutine.mcp.tools.MAX_ANSWER = original

	assert failed, "an answer over the cap was returned whole"
	assert "fields" in answered and "limit" in answered, answered
	assert "compact" in answered

	# And under the cap it comes back untouched, so the rule is a bound rather than a filter.
	whole, failed = _called(bound, "subroutine_call_api", method="GET", path="/v1/tasks")

	assert not failed, whole
	assert "Something to fill the page 0" in whole


def test_a_write_over_the_cap_is_not_reported_as_though_it_had_not_happened (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#531`. The cap is applied after the call, so on a write the change is already made.

	`#505`'s shape one layer up: something that succeeded is reported as a failure, and the
	message is what invites the retry that then does it twice. Latent today — no write returns
	64 KB, since a create returns one entity — and real the day a batch endpoint exists, which
	`POST /v1/tasks/batch` already is in §8.6's unbuilt list.

	The wording is the whole fix. Refusing to report a long answer is right; saying nothing
	about what became of the request is what makes it dangerous.
	"""

	original = subroutine.mcp.tools.MAX_ANSWER

	try:
		subroutine.mcp.tools.MAX_ANSWER = 10
		answered, failed = _called(
			bound,
			"subroutine_call_api",
			method="POST",
			path="/v1/tasks",
			body={"title": "Filed once, and only once"},
		)

	finally:
		subroutine.mcp.tools.MAX_ANSWER = original

	assert failed, "the cap did not fire, so nothing here is asserting anything about it"

	assert "201" in answered, (
		f"the refusal does not say the instance answered, or how: {answered}"
	)

	assert "already changed" in answered and "twice" in answered, (
		f"the refusal does not warn against repeating a write that already happened: {answered}"
	)

	assert "fields" not in answered and "compact" not in answered, (
		f"a write is being told to narrow its columns, which is advice for a listing: {answered}"
	)

	# And it really did happen — which is the whole of why the message must not read as a
	# failure, and is the half a wording assertion on its own could not establish.
	listed, failed = _called(bound, "subroutine_list")

	assert not failed, listed
	assert listed.count("Filed once, and only once") == 1, (
		f"the write did not land exactly once, so the refusal's claim is wrong: {listed}"
	)


def test_a_raw_call_is_narrowed_by_the_credential_it_already_had (
	session: sqlalchemy.orm.Session,
) -> None:
	"""It widens nothing, which is the sentence the whole decision rests on.

	An escape hatch that authenticated differently from the tools beside it would be a privilege
	escalation wearing a convenience's clothes — and §12.1a makes that easy to assert *falsely*
	here: an unscoped local principal is the sole account, holds everything, and would pass a
	test that checked "some route refuses". So the credential is deliberately narrowed first, and
	the assertion is that the narrowing survives the raw path.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Read only", scopes=["task:read"]
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

		reading, failed = _called(
			server, "subroutine_call_api", method="GET", path="/v1/tasks"
		)

		assert not failed, f"a scope this credential holds was refused: {reading}"

		writing, failed = _called(
			server,
			"subroutine_call_api",
			method="POST",
			path="/v1/tasks",
			body={"title": "Something it may not file"},
		)

	assert failed or writing.startswith("403"), (
		f"a task:read credential filed a task through the raw path: {writing}"
	)


def test_the_instructions_say_the_tools_are_not_the_whole_product (
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`#480`. An agent that believes the tools *are* the product stops at the first gap.

	Measured on 2026-08-04, from a third-party agent on a fresh install: it could not revise a
	document through the tools, concluded documents were immutable, and **changed how it
	worked** — declining to file a draft at all and giving one-item-in-one-place, which is this
	project's own principle, as the reason. It was right about the surface and wrong about the
	system, and nothing anywhere told it so. Told the command line existed, it found
	``subroutine doc edit`` in under a minute.

	**Here rather than only in the skill**, which is the decision the item left open. The skill
	carries the *reasoning* — a schema is context every session carries, so a tool is expensive
	in a way a command is not — and costs nothing per session. But `#378` is standing evidence
	that the skill is read only by an agent that opened it, and this is the one text guaranteed
	to be in context. So: point here, teach there.

	**Conditional for a reason the skill pointer does not share**: a client reaching this server
	over HTTP may have no shell at all, and an instruction telling it to run a command would be
	the confident wrongness §13.1 forbids.
	"""

	roster = subroutine.connections.Roster(
		(subroutine.connections.Connection(name="local"),), default="local"
	)
	instructions = _standing_up(roster)

	assert "budget" in instructions, (
		"nothing says the surface is deliberate, so a gap reads as the product's limit"
	)
	assert "subroutine --help" in instructions, (
		"the pointer must name the check — the agent above ran exactly that"
	)
	assert "if you can run commands" in instructions.lower(), (
		"it must stay conditional: a session reaching this over HTTP may have no shell"
	)


def test_a_refusal_reads_as_a_sentence_somebody_could_follow (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#497`. Every other test here asks whether the refusal *names* an alternative.

	None of them read it as a person would, so ``Run 'subroutine init, or 'workspace create''
	instead`` shipped — the entry carried a clause and the message quotes it as a command. That
	is `#366`'s shape: a substring assertion cannot see a sentence malformed around the
	substring it is looking for.

	Checked as a class rather than as the instance: nested quotes anywhere in a rendered refusal
	mean data and prose have been mixed again, whichever entry did it.
	"""

	for method, path in (
		("POST", "/v1/workspaces"),
		("PATCH", "/v1/workspaces/somewhere"),
		("POST", "/v1/projects/SR/move"),
	):
		answered, failed = _called(bound, "subroutine_call_api", method=method, path=path)

		assert failed, f"{method} {path} was allowed through"
		assert "''" not in answered, f"nested quotes in the refusal for {method} {path}"

#: Every way `#527` found of spelling a denied route so the old guard missed it, plus the ones
#: that pass looking for the fourth. Each created or moved something when it was measured.
RESPELT: tuple[tuple[str, str], ...] = (
	("POST", "/v1/workspaces?x=1"),
	("POST", "/v1/workspaces/"),
	("POST", "/v1/../v1/workspaces"),
	("POST", "/v1/./workspaces"),
	("POST", "/v1/%77orkspaces"),
	("POST", "/v1/%2e%2e/v1/workspaces"),
	("POST", "//v1/workspaces"),
	("POST", "/v1/workspaces#x"),
	("PATCH", "/v1/workspaces/personal?x=1"),
	("PATCH", "/v1/workspaces/personal/?x=1"),
	("POST", "/v1/projects/a/move?x=1"),
	("POST", "/v1/projects/a/move/?x=1"),
	("POST", "/v1/projects/a/%6dove"),
)


@pytest.mark.parametrize(("method", "path"), RESPELT, ids=[p for _m, p in RESPELT])
def test_a_denied_route_is_refused_however_it_is_spelled (
	bound: subroutine.mcp.protocol.Server, method: str, path: str
) -> None:
	"""`#528`. The old guard matched the caller's raw string with `$`-anchored regexes.

	Everything downstream normalises, so the string it inspected was not the path the router
	matched — and three of these created a workspace while a fourth moved a project. A query
	string fell outside the anchor; httpx resolved `..` after the check; the server decoded `%77`
	after it. The one entry that held did so because `[^/]+` happens to swallow `?x=1`, which is
	luck rather than design.

	**Not privilege escalation**, and this test is not claiming it was: the credential still
	needed the permission. What was defeated is decision `#484`'s stated property, that three
	consequential and un-undoable acts are reachable only where a person is asked first.
	"""

	answered, failed = _called(bound, "subroutine_call_api", method=method, path=path)

	assert failed, f"{method} {path} reached the application"
	assert "deliberately not reachable" in answered, (
		f"{method} {path} was refused for some other reason: {answered}"
	)
	# And the entries themselves, so a new one cannot reintroduce it before anybody renders it.
	groups = {
		group.name: group for group in subroutine.cli.main.app.registered_groups if group.name
	}

	for _verb, _pattern, instead in subroutine.mcp.tools.DENIED:
		assert "'" not in instead and "," not in instead, (
			f"{instead!r} is a clause rather than a command — the refusal quotes it as one"
		)

		# **And it has to be a command that exists**, which is `#134`/`#136`/`#138`'s lesson:
		# every one of those was a page naming something nobody could run. A refusal is the
		# worst place to do it — the reader is already stuck, and is being handed a second
		# dead end by the message meant to release them.
		words = instead.split()

		assert words[0] == "subroutine", f"{instead!r} does not name this program"
		assert words[1] in groups, f"{instead!r} names no such command group"

		nested = groups[words[1]].typer_instance

		assert nested is not None, f"{words[1]} is a group with nothing under it"
		assert words[2] in {
			command.name or (command.callback.__name__ if command.callback else "")
			for command in nested.registered_commands
		}, f"{instead!r} names no such command"


def test_the_instructions_name_every_document_a_session_might_not_find (
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`#498`, on decision `#499`'s rule: **the guaranteed channel names every conditional one.**

	Measured 2026-08-05: this text and the tool schemas are the only two things that reach an
	agent unconditionally, and they named none of the three documents `#483` and `#486` built.
	So 9.5 KB written for precisely this reader — ``/v1/docs/agent``, which opens *"You are a
	principal here, not a tool being driven"* — was unreachable because nothing said it was
	there. The inert-control defect (`#247`, `#251`, `#303`) applied to prose: every piece
	individually correct, and nobody receiving it.

	**Derived from the resources rather than listing them**, which is the whole point of writing
	it as a rule. A fourth document added without a signpost fails this, and that is the failure
	the fix alone would not have prevented — the resources themselves were three hours old when
	the gap was measured.

	**Both routes are asserted** because one is client-dependent. A ``subroutine://`` URI is
	useless to a client that does not read resources, and this was measured against one that
	exposed resource-listing as a tool the agent had to go looking for.
	"""

	roster = subroutine.connections.Roster(
		(subroutine.connections.Connection(name="local"),), default="local"
	)
	instructions = _standing_up(roster)
	client = unittest.mock.MagicMock(spec=subroutine.clients.base.Client)

	published = subroutine.mcp.tools.references(client)

	assert published, "no resources at all — has this stopped reaching them?"

	for resource in published:
		assert resource.uri in instructions, (
			f"{resource.uri} exists and the one text every session receives does not name it, "
			f"so only a client that lists resources will ever find it"
		)

		# **The route the resource declares, not one composed from its URI** (`#506`). The
		# derivation worked while every resource was a document under `/v1/docs/`, and would
		# have asserted `/v1/conventions` — which does not exist — for the first one that was
		# an index assembled from a listing instead.
		route = resource.also_at

		assert route in instructions, (
			f"{resource.uri} is named only as a resource; {route} is how a client without them "
			f"reaches the same document through subroutine_call_api"
		)

	assert "principal" in instructions, (
		"the big picture is one sentence and it is the reason an agent engages at all — "
		"without it this opens as a description of a filing system"
	)


def test_a_ref_that_names_nothing_says_how_to_find_the_right_one (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#184`. The surface built for the reader who *cannot ask a follow-up* had no next step.

	``subroutine_show`` on a ref that does not exist answered ``There is no #99999 here.`` and
	stopped. The CLI's equivalent says the same and then ``Run 'subroutine list' to see what
	there is`` — so the transport with a person attached, who could have guessed, was the one
	being helped, and the transport without one was not.

	Not the defect `#165` fixed, which was a hint lost in rendering: these are raised as a plain
	``LookupError`` in two places and never had a hint to lose. It is the same principle `#497`
	settled for the deny-list — **a refusal must not be a dead end** — and `#488` for the API.
	"""

	answered, failed = _called(bound, "subroutine_show", ref=99999)

	assert failed, "a ref naming nothing must be refused"
	assert "subroutine_list" in answered, "the refusal offers no way to find the right item"

	# **The update path never reaches that refusal**, which is worth pinning rather than
	# assuming: it is turned away by `clients/local._require` first, whose message carries the
	# *CLI's* vocabulary — "Run 'subroutine list'". Both are a genuine next step, and `#480`
	# settled that pointing an agent at the command line is right rather than a fallback. What
	# the rule requires is that neither refusal is a dead end.
	changed, failed = _called(bound, "subroutine_update", ref=99999, title="Nope")

	assert failed
	assert "list" in changed, "the update path refuses with no way to find the right item"

	# **And the tools it names have to exist**, which is `#136`'s lesson: prose in a refusal is
	# read by an agent that is already stuck, so naming something uncallable strands it twice.
	catalogue = {tool.name for tool in subroutine.mcp.tools.catalogue(unittest.mock.MagicMock())}

	for named in ("subroutine_list", "subroutine_search"):
		assert named in catalogue, f"the refusal names {named}, which is not a tool"


def test_a_day_resolves_on_a_machine_whose_zone_abbreviation_is_not_a_zone () -> None:
	"""`#532`, found on a stranger's Fedora laptop and not by 2,930 tests.

	``_day`` passed the *client's* zone so that an agent saying "friday" means the Friday it is
	looking at — right, and it derived it with ``str(utcnow().astimezone().tzinfo)``, which
	yields a fixed-offset zone whose ``str()`` is the **abbreviation**. So ``BST``, ``PDT``,
	``CEST`` and ``AEST`` were handed to a function wanting a zoneinfo key, and ``plan`` and
	``defer`` failed outright with *"'BST' is not a timezone"*.

	**Why nothing here saw it, and why this test names Sydney.** A few abbreviations are also
	zoneinfo keys — ``UTC``, ``GMT``, ``EST``, ``MST`` — so the expression works in UTC, which
	is every CI job and every test in this file, and in London in *winter*. Testing in
	``Europe/London`` would therefore pass for five months of the year and fail for seven, which
	is worse than not testing it. Sydney is ``AEST`` or ``AEDT`` and neither is a zone, so this
	reproduces on any date.

	Driven through the real function rather than by inspecting what it passes: the assertion is
	that a day resolves, which is what the user could not do.

	**The zone is passed in now rather than read here** (`#1064`, decision `#1088`), because the
	process's zone is the *server's* for every relayed connection. The defect this reproduces is
	unmoved: it was never about *which* zone was chosen but about the value being an
	abbreviation, and ``config.system_timezone()`` — which is what a caller falls back to when
	the instance publishes none — is the expression that used to be wrong.
	"""

	original = os.environ.get("TZ")
	os.environ["TZ"] = "Australia/Sydney"
	time.tzset()

	try:
		resolved = subroutine.mcp.tools._day(
			"friday", field="plan", timezone=subroutine.config.system_timezone()
		)

	finally:
		if original is None:
			os.environ.pop("TZ", None)

		else:
			os.environ["TZ"] = original

		time.tzset()

	assert resolved is not None, "a day an agent named did not resolve"


# ---- a machine with no instance on it yet (`SR#697`) ----------------------------------------


def _relayed (
	lines: str, settings: subroutine.config.Settings, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, typing.Any]]:
	"""Drive ``relay.run`` for real over the given messages and return what it wrote back."""

	monkeypatch.setattr(
		subroutine.connections, "roster", lambda settings: _roster("local", default="local")
	)

	outgoing = io.StringIO()

	subroutine.mcp.relay.run(io.StringIO(lines), outgoing, settings=settings)

	return [json.loads(line) for line in outgoing.getvalue().splitlines() if line.strip()]


def _nowhere (tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> subroutine.config.Settings:
	"""Return settings for a machine where nobody has run ``init``."""

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(tmp_path / variable.lower()))

	settings = subroutine.config.Settings(dev_mode=True)

	assert settings.has_no_instance_yet(), "the fixture built an instance, so it proves nothing"

	return settings


def test_a_machine_with_no_instance_is_told_which_command_makes_one (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""``SR#697``, and it is `SR#585`'s sequel rather than a separate accident.

	Nothing has to be installed before the plugin starts now, so *"an agent asking its first
	question on a machine where nobody has run init"* stopped being an edge and became the
	ordinary first contact. What it used to get was three problem documents written straight
	onto the protocol channel — no envelope, no id, including for ``initialize`` — and 564 lines
	of traceback on stderr, ending ``unable to open database file``.

	The sentence is the one ``clients/local.py`` already gives a person, and the predicate is
	the same: a missing SQLite file is a *fact*, where an unreachable PostgreSQL might be
	absent, asleep or firewalled, and guessing produces confident bad advice.

	**The remedy depends on the machine and this pins which machine it is** (`SR#734`). The
	assertion below used to be `"subroutine init" in said` with nothing controlling whether that
	command exists — so it asserted the installed-CLI branch by accident of the environment it
	ran in, and would have gone on passing while the other audience got advice that answers
	`command not found`.
	"""

	monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/subroutine")

	answered = _relayed(
		'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
		_nowhere(tmp_path, monkeypatch),
		monkeypatch,
	)

	assert len(answered) == 1
	assert answered[0]["jsonrpc"] == "2.0", "a reply with no envelope cannot be matched to a call"
	assert answered[0]["id"] == 1, "an error carrying the wrong id resolves the wrong call"

	said = answered[0]["error"]["message"]

	# **One check rather than two, and the first of the two had never run** (`#947`, cold review
	# `#927`'s L-3). It read `"no Subroutine instance" in said.lower() or "No Subroutine
	# instance" in said` — the needle in the first half carries a capital `S` and was searched
	# in a lower-cased string, so only the second half was ever doing anything. `#366`'s
	# recorded shape: an `or` in a diagnostic is a silent filter that looks like a fallback.
	#
	# Folded on both sides, because two places raise this and they disagree about the first
	# letter — `mcp/relay` opens the sentence and `clients/local` continues one.
	assert "no subroutine instance" in said.lower(), f"the refusal does not say why: {said}"
	assert "subroutine init" in said, f"the remedy is not named: {said}"


def test_a_machine_with_no_instance_is_refused_rather_than_logged (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#698`, and the half `SR#697` deliberately left: the **API** raised where it should refuse.

	`SR#697` fixed the protocol channel — the model gets a proper JSON-RPC error naming the
	command. What it could not change is that to the API layer this was an *unexpected*
	exception: the session opened, the endpoint touched it, SQLite reported a file it could not
	open, and the unhandled-error handler wrote a stack trace. **Measured either side of the
	fix, driving three messages through the relay on a machine with no instance: 600 lines of
	stderr before, 0 after.**

	It is not unexpected. ``Settings.has_no_instance_yet`` names the condition exactly and the
	command line has answered it since `SR#165`, so the true statement was one predicate away —
	which is `SR#573`'s worst category, a thing that works and says something false about
	itself. An operator reading that log concludes the database is broken.

	**Asserted at the API rather than by counting stderr**, because the line count is a symptom
	of the handler that ran and the claim is about which one runs. A refusal carrying the code
	and the remedy cannot be produced by the unexpected-error path at all.

	**It fires before authentication, and an unauthenticated probe is therefore a sufficient
	witness.** Without the check this request is a 401 — the credential is resolved before
	anything touches a database — so 401 against 503 is what this asserts, and it is not a
	test about authentication. Refusing first is the honest order: there is no instance to
	hold a credential, and the predicate only ever answers true for SQLite, so a served
	PostgreSQL instance cannot reach it and no stranger learns anything from it.

	**Beside `SR#697`'s test rather than in an API file**, because the two are one story and
	``_nowhere`` is the fixture both need — a machine where nobody has run ``init``, which is
	the state no ordinary test fixture can be in.
	"""

	settings = _nowhere(tmp_path, monkeypatch)
	application = subroutine.api.app.create_app(settings=settings)

	answered = api_support.call(application, "GET", "/v1/tasks")

	assert answered.status_code == 503, answered.text

	body = answered.json()

	assert body["code"] == "service_unavailable", body
	assert "no subroutine instance" in body["detail"].lower(), body
	assert "subroutine init" in body["hint"], body


def test_a_machine_reached_only_through_uvx_is_told_a_command_it_can_actually_run (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`SR#734`. The audience `SR#585` created, meeting its first wall.

	The plugin launches `uvx subroutine~=X.Y mcp`, and `uvx` runs from a cache and puts nothing
	on `PATH` — so somebody who followed the plugin's own promise and installed only `uv` was
	told to run `subroutine init`, which answers `command not found`.

	**The version is pinned in the advice for a reason.** Plain `uvx subroutine init` fetches
	the newest release, which can create an instance whose schema is ahead of the program that
	will read it — `SR#250`'s skew, manufactured by our own remedy. The series comes from the
	running program because the relay *is* what the plugin's pin launched.
	"""

	monkeypatch.setattr(shutil, "which", lambda name: None)

	answered = _relayed(
		'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
		_nowhere(tmp_path, monkeypatch),
		monkeypatch,
	)

	said = answered[0]["error"]["message"]
	series = ".".join(subroutine.__version__.split(".")[:2])

	assert "uvx" in said, f"the remedy names a command this machine does not have: {said}"
	assert f"subroutine~={series} init" in said, (
		f"the remedy is unpinned, so it can create an instance ahead of this program: {said}"
	)


def test_a_notification_is_not_answered_even_when_it_fails (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The specification is explicit that a server must not reply to a notification.

	**Found by driving the fix rather than by reading it.** Naming the missing instance made
	every message fail, including ``notifications/initialized`` — which has no ``id``, is not
	waited on, and so received an answer the client could match to nothing. The shape was always
	there in the refusal path and had only ever been reachable when a whole connection failed.

	**A literal ``"id": null`` is a request, not a notification**, and still gets its answer:
	the test is for the member being absent rather than for the value being null, which is the
	distinction ``dict.get`` silently loses.
	"""

	settings = _nowhere(tmp_path, monkeypatch)

	answered = _relayed(
		'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
		'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
		'{"jsonrpc":"2.0","id":null,"method":"tools/list"}\n',
		settings,
		monkeypatch,
	)

	assert [message.get("id") for message in answered] == [1, None], (
		"three messages went in and the notification is the one that must not come back"
	)


def test_an_answer_that_is_not_a_json_rpc_message_becomes_one (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The guard tested whether the body *parsed*, and a problem document parses perfectly.

	So every refusal this API makes reached the protocol channel verbatim. The condition is
	about being a JSON-RPC message now, and the document's own ``detail`` and ``hint`` are used
	when it has them — they are written for a person and are worth more than a sentence composed
	here about a status code.
	"""

	class _Answered:
		status_code = 500
		text = json.dumps({
			"status": 500,
			"title": "Internal error",
			"detail": "The instance could not read its own vocabulary.",
			"hint": "Check that the database is migrated.",
		})

	monkeypatch.setattr(
		subroutine.api.inprocess, "call", lambda *args, **kwargs: _Answered()
	)

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(tmp_path / variable.lower()))

	# Made, so the missing-instance refusal above does not fire first and answer a different
	# question than this test is asking.
	(tmp_path / "xdg_data_home" / "subroutine").mkdir(parents=True)
	(tmp_path / "xdg_data_home" / "subroutine" / "subroutine.db").touch()

	answered = _relayed(
		'{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n',
		subroutine.config.Settings(dev_mode=True),
		monkeypatch,
	)

	assert len(answered) == 1
	assert answered[0]["jsonrpc"] == "2.0"
	assert answered[0]["id"] == 7

	said = answered[0]["error"]["message"]

	assert "could not read its own vocabulary" in said, f"its own words were dropped: {said}"
	assert "Check that the database is migrated." in said, "the hint was dropped"


def test_an_agent_can_ask_what_was_created_recently (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""**`#815`'s requirement in the words it was asked in**: an agent generates the request.

	The whole case for spending 401 bytes of every session's context on this argument is that a
	model reads tool *names* — so a capability reachable only through `subroutine_call_api` is
	one it never finds. That is worth nothing unless the argument actually narrows, which is
	what this drives.
	"""

	_called(bound, "subroutine_add", text="Fix the boiler")

	recent, failed = _called(
		bound, "subroutine_list", filter={"created_at.gte": "today"}
	)

	assert not failed, recent
	assert "Fix the boiler" in recent

	older, failed = _called(bound, "subroutine_list", filter={"created_at.lt": "today"})

	assert not failed, older
	assert "Fix the boiler" not in older


def test_a_misspelled_filter_field_is_refused_by_name (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""Refused by the instance, which is the side holding the registry.

	The tool checks the *shape* and not the names — so a field added to the registry is
	accepted the day it exists, and a client one release behind cannot refuse a question its
	instance understands.
	"""

	answered, failed = _called(
		bound, "subroutine_list", filter={"creatd_at.gte": "today"}
	)

	assert failed
	assert "creatd_at" in answered
	assert "created_at" in answered, "the refusal did not name the fields that do exist"


def test_a_filter_that_is_not_an_object_is_refused_rather_than_guessed (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#549`'s rule reaching a new argument, from **two** places — and that split was measured.

	``protocol._mistyped`` refuses an argument whose value does not match its declared
	``type``, so the string below never reaches the tool. It does not recurse, though: it reads
	the property's own type and knows nothing about ``additionalProperties``, so a *value* of
	the wrong kind is the tool's to refuse.

	Both cases are here because the first version of ``_filters`` checked the top level as well
	— and the falsification that removed that check **passed**, which is how the duplication was
	found. A second copy of a rule is this codebase's signature defect, and it is invisible
	exactly while the two agree.
	"""

	# Refused by the protocol layer, before the tool is called at all.
	answered, failed = _called(bound, "subroutine_list", filter="created_at.gte=today")

	assert failed
	assert "object" in answered
	assert "created_at.gte" in answered, "the refusal did not quote what was sent"

	# Refused by the tool, because nothing else looks inside.
	answered, failed = _called(bound, "subroutine_list", filter={"created_at.gte": 5})

	assert failed
	assert "created_at.gte" in answered
	assert "yesterday" in answered, "the refusal did not show the shape"


def test_a_date_a_document_has_not_got_returns_no_documents (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""**Narrowing a list must not make it longer** — `#815`, the same rule the CLI applies.

	`subroutine_list` fills whatever the task rows leave with documents, in a second call. A
	document is not scheduled (§6.14), so it has no `completed_at` — and a second call that
	dropped the filter it could not honour would answer *what did I complete today* by adding
	every decision in the workspace.

	The task has to be *there* in the narrowed answer for this to mean anything: a listing that
	refused outright would produce the same absence, and the two are opposite behaviours.
	"""

	_called(bound, "subroutine_add", text="Fix the boiler")
	_called(bound, "subroutine_done", ref=1)
	_called(
		bound, "subroutine_document", title="How the thing works", body="Like this."
	)

	answered, failed = _called(
		bound, "subroutine_list", filter={"completed_at.gte": "today"}
	)

	assert not failed, answered
	assert "Fix the boiler" in answered, "the listing refused rather than skipping documents"
	assert "How the thing works" not in answered, "the document half ignored the filter"

	# A field both kinds have still reaches both, so the rule is about the field.
	both, failed = _called(bound, "subroutine_list", filter={"created_at.gte": "today"})

	assert not failed, both
	assert "How the thing works" in both


def test_the_filter_schema_names_every_field_it_accepts (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""A published description is a contract, and this is the half a test can hold — `#815`.

	The *wording* is judgement and is not asserted. What is derivable is that an agent reading
	this schema and nothing else can name every field the tool will take: Simon's raise to
	10,600 was bought specifically so the description would be complete enough to use without a
	second call, and a field quietly missing from it spends the bytes and loses the reason.

	It also catches the shape that actually went wrong. The description is *generated* from the
	registry and stayed honest about which fields exist — while silently calling `touched_by` a
	date field, because it was written when every filterable field was a timestamp. Deriving a
	list is not the same as describing it correctly, so both halves are checked: every field is
	named, and `touched_by` is not named among the ones taking the date grammar.
	"""

	answered = _exchange(bound, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
	listing = next(
		tool for tool in answered[0]["result"]["tools"] if tool["name"] == "subroutine_list"
	)
	described = listing["inputSchema"]["properties"]["filter"]["description"]

	for name in subroutine.domain.filtering.TASK_FILTERS:
		assert name in described, f"{name} is accepted and not named in the schema"

	grammar, _, rest = described.partition("touched_at is")

	assert subroutine.domain.filtering.TOUCHED_BY not in grammar, (
		"touched_by takes a username and is listed among the fields taking a date"
	)
	assert subroutine.domain.filtering.TOUCHED_BY in rest


def test_asking_who_you_are_says_what_an_ordinary_role_may_do (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#717`, on the surface the item names beside the CLI — and the *unnarrowed* case.

	The test above covers a credential bounded on purpose. This is the one that was silent: a
	token with **no scopes, no project scope and no workspace pin**, belonging to somebody whose
	role is not the owner's. It was handed the role's name and nothing else, so an agent learned
	more about what it could do by being restricted than by being trusted.

	The account is a second user with a member role rather than the instance's founder, because
	the founder is a superuser and holding everything is the one case where listing says nothing
	— which is the rule the fix keeps and the old condition never expressed.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	principal = subroutine.domain.authentication.Principal(user=setup.user, token=None)
	colleague = subroutine.domain.users.create(
		session, username=f"colleague-{uuid.uuid4().hex[:8]}", actor=principal
	)
	subroutine.domain.workspaces.add_member(
		session, setup.workspace, colleague, role_key="member", actor=principal
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=colleague, title="unbounded"
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

	assert not failed, text
	assert "Narrowed to" not in text, "the fixture narrowed it, so it proves nothing"
	assert "may: " in text, "a member was told its role and not what the role may do"
	assert subroutine.permissions.TASK_READ in text


def test_an_agent_can_tag_a_conclusion (bound: subroutine.mcp.protocol.Server) -> None:
	"""`#819` reaching the surface that writes documents.

	The skill spends most of its words persuading an agent to write a document rather than a
	comment. A document it cannot label is one nobody finds again by subject, which is what a
	tag is for — and the tags are a task's tags, from one workspace vocabulary.
	"""

	answered, failed = _called(
		bound,
		"subroutine_document",
		title="Why we chose Preact",
		body="Because it is 4 KB.",
		tags=["design", "web"],
	)

	assert not failed, answered

	shown, failed = _called(bound, "subroutine_show", ref=1)

	assert not failed, shown
	assert "design" in shown
	assert "web" in shown


def test_a_tag_that_is_not_a_word_is_refused_by_the_tool (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#549`'s split again: the protocol checks the declared type, the tool checks inside it.

	``protocol._mistyped`` refuses a bare string here, because ``tags`` declares ``array``.
	It does not recurse into ``items``, so an array carrying a number reaches the tool — and
	this is the only place that can turn it down.

	**That first sentence was aspirational until 2026-08-27 and the body said so** (`SR#1352`).
	This docstring described the design; the comment three lines down described what actually
	happened, which was that ``_ACCEPTS`` knew nothing about ``array`` and every bare string
	sailed past the protocol into ``_words``. A published contract nothing enforced — with the
	documentation of it split across one function, saying both things.

	``array`` is in ``_ACCEPTS`` now, taking the invitation ``_mistyped``'s own docstring
	offers, so the docstring is true and the split is where `#549` says it is.
	"""

	# **Refused by the protocol, because `tags` declares `array` and `_ACCEPTS` knows it.**
	answered, failed = _called(bound, "subroutine_document", title="x", tags="design")

	assert failed
	assert "tags" in answered and "a list" in answered, answered

	# Refused by the tool, because nothing else looks inside.
	answered, failed = _called(
		bound, "subroutine_document", title="x", tags=["design", 5]
	)

	assert failed
	assert "tags" in answered
	assert "design" in answered, "the refusal did not show the shape"


def test_the_link_tool_does_not_hard_code_a_renameable_vocabulary (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#821` — five link types are seeded and the schema published three.

	The two it left out were `derives_from` and `documents`, which are the pair that join work
	to the conclusions about it — so the surface that most wants an agent writing documents gave
	it no way to attach one. And because the omission was from the *seeded* set rather than a
	stale default, the usual correction never fired: an agent does not send `documents` and get
	refused, it never learns the word.

	Asserted as *does not enumerate* rather than *enumerates all five*, because §5.5 makes these
	renameable per workspace — a complete list would be right today and wrong on any instance
	that renamed one.
	"""

	answered = _exchange(bound, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
	tools = {tool["name"]: tool for tool in answered[0]["result"]["tools"]}
	link = tools["subroutine_link"]
	described = link["description"] + link["inputSchema"]["properties"]["type"]["description"]

	seeded = {seed.key for seed in subroutine.db.seed.LINK_TYPES}
	named = {key for key in seeded if key in described}

	assert named <= {"blocks"}, (
		f"the schema lists link type keys, so it can be incomplete or stale: {sorted(named)}"
	)
	assert "meta" in described, "nothing points at where this workspace's list actually is"


def _attributes_of (node: ast.FunctionDef, variable: str) -> set[str]:
	"""Return every ``<variable>.<field>`` read inside one function body."""

	return {
		read.attr
		for read in ast.walk(node)
		if isinstance(read, ast.Attribute)
		and isinstance(read.value, ast.Name)
		and read.value.id == variable
	}


def _read_by (module: typing.Any, function: str, variable: str) -> set[str]:
	"""Return the view fields one renderer reads off the item it is given.

	Derived by reading the renderer rather than listed beside it, which is the only reason
	this comparison stays true as either surface grows — `#427`'s method.

	**It follows a helper the whole item is handed to, one level deep** (`#1298`). Reading a
	single function body cannot see a field the renderer reads *through* a call, and the
	symptom is the wrong way round: extracting a shared helper made the terminal look as
	though it had **stopped** reading a field, so the stale-entry test below reported a
	deliberate excuse as expired. A scan that goes blind when code is tidied would train
	somebody to keep the duplication.

	One level rather than a full call graph, because that is what the surfaces do — a renderer
	hands the item to a phrase-maker — and an unbounded walk would need cycle handling for no
	case that exists. The parameter is matched by **position**, so the helper is free to call
	it something else.
	"""

	tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
	defined = {
		node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
	}
	node = defined.get(function)

	if node is None:
		return set()

	found = _attributes_of(node, variable)

	for call in ast.walk(node):
		if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
			continue

		helper = defined.get(call.func.id)

		if helper is None:
			continue

		for position, argument in enumerate(call.args):
			if not isinstance(argument, ast.Name) or argument.id != variable:
				continue

			taken = helper.args.posonlyargs + helper.args.args

			if position < len(taken):
				found |= _attributes_of(helper, taken[position].arg)

	return found


#: A fact the command line's ``show`` reads that the agent's ``show`` deliberately does not,
#: and why. Each has to say what an agent gets instead — `#820`'s rule, applied to a register
#: about a rendering rather than about a column.
NOT_SHOWN_TO_AN_AGENT: dict[str, str] = {
	"timezone": (
		"Used to render an instant in the zone that stored it, which is a courtesy to a "
		"person reading a day name. This surface sends ISO instants and lets the model do "
		"its own arithmetic, so there is nothing to render *into*."
	),
	# `status_is_default` was excused here until `#841` — "the rule rather than the fact, both
	# surfaces read it to decide whether a status is news". Neither reads it now:
	# `views.status_is_news` does, for all three renderers. The entry went because the guard
	# below refused it, which is the "what makes this entry go away?" question working rather
	# than an entry nobody dared delete.
	"type_is_default": (
		"An agent is told the *type*, on every line, unconditionally — so this surface never "
		"asks whether the type is news and there is nothing here for the answer to decide. "
		"It gets more than the terminal does rather than less: `subroutine show` prints the "
		"type only when somebody chose it (`#1135`), and `_line` prints it always.\n\n"
		"This entry goes the day that line starts suppressing anything — at which point both "
		"surfaces are asking one question and it belongs in `views` beside `status_is_news`, "
		"which is exactly how `status_is_default`'s own excuse here came to be deleted."
	),
	"estimate_minutes": (
		"Reported as `estimate_human`, which is §6.4's grammar — the spelling an agent would "
		"send back, and what `_line` has always carried."
	),
}


def test_the_agents_show_reports_what_the_command_lines_show_reports () -> None:
	"""One item, two renderings, and nothing compared them (`#674`).

	``subroutine_show`` gave a model the type, the rank and the estimate and left out the
	project, the deferral and — worst — the **status**, on the one surface whose own
	instructions tell an agent to set it. It could write ``in_progress``, be answered
	*Changed*, and never see the word again.

	Half the item had already closed itself: `#673` added the planned day and the deadline,
	`#511` the assignee, `#425` the blocked flag and `#819` the tags, each fixing one field
	because somebody noticed that one. This is the comparison that would have found them
	together, and it is the same guard `#583` puts on the terminal's own two renderings.
	"""

	terminal = _read_by(subroutine.cli.personal, "_facts", "item")

	# Three renderers, because ``show`` is assembled from three: the row a listing also uses,
	# the facts only this tool promises, and the tool itself, which reports the tags and the
	# body under their own headings rather than as cells.
	agent = (
		_read_by(subroutine.mcp.tools, "_line", "item")
		| _read_by(subroutine.mcp.tools, "_more", "item")
		| _read_by(subroutine.mcp.tools, "_shown", "found")
	)

	assert terminal, "No fact renderer was read at the command line."
	assert agent, "No fact renderer was read on the agent's surface."

	missing = sorted(terminal - agent - set(NOT_SHOWN_TO_AN_AGENT))

	assert not missing, (
		f"'subroutine show' reports {missing} and 'subroutine_show' does not. Report them, or "
		f"record in NOT_SHOWN_TO_AN_AGENT what an agent gets instead."
	)


#: A fact the terminal's listing row carries that the agent's row deliberately does not, and
#: why. Same rule as `NOT_SHOWN_TO_AN_AGENT` above, asked of the *row* rather than of `show`.
NOT_ON_AN_AGENTS_ROW: dict[str, str] = {
	"snoozed_until": (
		"The terminal marks a deferred row because a person asked for a list and got one "
		"item fewer than they expected. An agent's listings hide deferred work by default "
		"and it asks for it by name, so a row it is looking at is one it asked to see."
	),
	"snoozed_is_all_day": (
		"`snoozed_until`'s flag and so `snoozed_until`'s reason: it decides whether the "
		"o'clock is said beside the day (`#1298`), and a row that says nothing about the "
		"defer at all has no day to say it beside. Surfaced when the scan above learned to "
		"follow a renderer into its helper — one fact, two columns, one excuse."
	),
	"timezone": (
		"`NOT_SHOWN_TO_AN_AGENT`'s reason, unchanged: this surface sends ISO instants and "
		"there is nothing to render *into*."
	),
}


def test_the_agents_listing_row_reports_what_the_command_lines_row_reports () -> None:
	"""`#922`. **The pair `#674`'s guard structurally cannot see.**

	That one compares the terminal's `_facts` against the *union* of the three renderers the
	agent's ``show`` is assembled from — which is right for ``show``, and means a fact carried
	by ``_more`` alone satisfies it. So a row and a fact sheet were never told apart, and the
	repeat sat in ``_more`` while ``_line`` said nothing.

	**A row is not a smaller fact sheet, and on this surface it is not only a row.** ``_line``
	is what every write is answered with, so a fact missing from it is a fact an agent cannot
	see itself set — which is exactly the defect `#674` exists for, reappearing two months
	later in the one place that guard was blind to.
	"""

	terminal = _read_by(subroutine.cli.personal, "_when", "task")
	agent = _read_by(subroutine.mcp.tools, "_line", "item")

	assert terminal, "No row renderer was read at the command line."
	assert agent, "No row renderer was read on the agent's surface."

	missing = sorted(terminal - agent - set(NOT_ON_AN_AGENTS_ROW))

	assert not missing, (
		f"a listing row at the terminal reports {missing} and the agent's row does not. Put "
		f"them on it, or record in NOT_ON_AN_AGENTS_ROW what an agent gets instead."
	)


def test_every_fact_excused_from_an_agents_row_is_still_read_at_the_command_line () -> None:
	"""So the register cannot go on excusing a fact no row renders any more — `#405`'s rule."""

	terminal = _read_by(subroutine.cli.personal, "_when", "task")
	unknown = sorted(field for field in NOT_ON_AN_AGENTS_ROW if field not in terminal)

	assert not unknown, (
		f"NOT_ON_AN_AGENTS_ROW names {unknown}, which a terminal row no longer reads."
	)


def test_every_fact_excused_from_the_agents_show_is_still_read_at_the_command_line () -> None:
	"""So the register cannot go on excusing a fact nobody renders any more."""

	terminal = _read_by(subroutine.cli.personal, "_facts", "item")
	unknown = sorted(field for field in NOT_SHOWN_TO_AN_AGENT if field not in terminal)

	assert not unknown, f"NOT_SHOWN_TO_AN_AGENT names {unknown}, which 'show' no longer reads."


def test_the_agents_show_puts_those_facts_in_what_it_returns (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""The half the comparison above structurally cannot see, and it was needed immediately.

	That guard reads the renderers and asks whether one names a fact the other does not — so
	it goes on passing when the renderer is fine and **nothing calls it**. Deleting the call
	to ``_more`` from ``_shown`` left it green, which is `test_reach`'s recorded blind spot
	(it verifies a client method with the right *name* exists, never that it calls the route
	it is mapped to) turning up in a guard written the same afternoon.

	So this drives the tool and reads the answer. A pure function is not enough on its own —
	lift the decision out, then drive the thing that uses it.
	"""

	_called(bound, "subroutine_project", key="ops", title="Operations")
	added, failed = _called(bound, "subroutine_add", text="Rotate the certificates +ops")

	assert not failed, added

	numbered = re.search(r"#(\d+)", added)

	assert numbered is not None, f"nothing in the echo names the item that was made: {added}"

	ref = int(numbered.group(1))

	_called(bound, "subroutine_update", ref=ref, status="in_progress")
	shown, failed = _called(bound, "subroutine_show", ref=ref)

	assert not failed, shown

	# The status is the one this surface's own instructions tell an agent to set, and the one
	# it could not read back — it wrote `in_progress`, was answered *Changed*, and no tool in
	# the catalogue would ever say so again.
	assert "in_progress" in shown, shown
	assert "+ops" in shown, shown


def test_an_agent_can_revise_a_conclusion_it_has_re_read (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#822`. A conclusion that cannot be corrected is a record of what you concluded once.

	The description had said "revise one with 'subroutine doc edit 42'" — a shell command, on
	the surface whose premise is having no shell (`#548`, in the one place its translation
	does not reach). Corrected to name ``subroutine_call_api``, which was true and still asked
	an agent to leave the catalogue, read a schema and compose a PATCH to fix a sentence it
	had just written. What happens instead is a second document, which is the duplication
	`#47` exists to prevent.
	"""

	written, failed = _called(
		bound, "subroutine_document", title="Use SQLite", body="Because it is one file.",
		type="decision", tags=["storage"],
	)

	assert not failed, written

	numbered = re.search(r"#(\d+)", written)

	assert numbered is not None, written

	ref = int(numbered.group(1))
	revised, failed = _called(
		bound, "subroutine_document", ref=ref, body="Because it is one file, and it locks."
	)

	assert not failed, revised
	assert revised.startswith("Revised"), revised

	shown, failed = _called(bound, "subroutine_show", ref=ref)

	assert not failed, shown
	assert "and it locks" in shown

	# **Omitted is unchanged**, which is the whole reason this is a ref rather than a second
	# tool: an agent correcting one paragraph must not have to restate the title, the type and
	# the tags it decided on when it wrote the thing. Restating them from memory is how a
	# document is silently renamed.
	assert "Use SQLite" in shown
	assert "#storage" in shown


def test_writing_a_document_with_neither_a_title_nor_a_ref_is_refused_by_name (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""``title`` stopped being required by the schema, so the pair is refused here instead.

	It had to stop: a revision that only changes the body should not have to resend the title.
	The refusal names both arguments this tool actually has, which is `#547`'s rule — a
	refusal naming a field no tool declares is unfollowable, and the agent surface is where
	that costs most.
	"""

	answered, failed = _called(bound, "subroutine_document", body="Reasoning with no heading.")

	assert failed, answered
	assert "title" in answered
	assert "ref" in answered


def _bound_to (session: sqlalchemy.orm.Session, workspace: str) -> str:
	"""Return ``subroutine://conventions`` as a session pinned to one workspace reads it."""

	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True),
		session_factory=api_support.factory_for(session),
	)

	with client:
		server = subroutine.mcp.protocol.Server(
			subroutine.mcp.tools.catalogue(client, workspace=workspace),
			name="subroutine",
			version="0",
			resources=subroutine.mcp.tools.references(client, workspace=workspace),
		)
		answered = _exchange(
			server,
			{
				"jsonrpc": "2.0",
				"id": 1,
				"method": "resources/read",
				"params": {"uri": "subroutine://conventions"},
			},
		)

	assert "error" not in answered[0], answered[0]

	published: str = answered[0]["result"]["contents"][0]["text"]

	return published


def test_the_conventions_resource_carries_what_was_abandoned_as_well (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#590`. A decision says what to do; a dead end says what not to bother with.

	Only the first had a way of reaching anybody: this resource was built from
	``type=decision`` alone, so the negative half of what a workspace knows was invisible to
	the one channel `#499` calls guaranteed. **That half is the one a newcomer cannot
	reconstruct**, because a route not taken leaves nothing in the code to read.
	"""

	client, first, _second = _two_workspaces(session)

	with client:
		client.create_document(
			workspace=first, type="decision", title="Work is filed against an item first",
		)
		client.create_document(
			workspace=first,
			type="dead_end",
			title="A half-open range over path does not substitute for a prefix match",
		)

	session.flush()

	published = _bound_to(session, first)

	assert "Work is filed against an item first" in published
	assert "half-open range" in published, (
		"the workspace has closed a route off and the guaranteed channel does not say so"
	)


def test_the_abandoned_half_is_reported_where_nothing_has_been_decided (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The two halves are independent, and the obvious shape of this function makes them not.

	`_conventions` answers the no-decisions case in prose and **returned there**, so the second
	section was reachable only through the first — a workspace that had closed a route off
	without marking any decision in force would have been told nothing about it. Found by
	writing this rather than by reading, and the returning branch is easy to miss because the
	one above it reads as the special case.

	**The shape survived `#1036` and the wording did not, which is the correction worth
	keeping.** With four governing types the early return is gone entirely — the empty prose is
	reached only when *every* section came back empty — and this workspace has a dead end in
	force, so saying "nothing is marked as in force here" would now be false rather than
	merely awkward. The claim asserted is the one that was always meant: a workspace whose only
	convention is a closed route is told about it.
	"""

	client, first, _second = _two_workspaces(session)

	with client:
		client.create_document(
			workspace=first, type="dead_end", title="Peppering token hashes with the secret key",
		)

	session.flush()

	published = _bound_to(session, first)

	assert "Peppering token hashes" in published
	assert "Nothing is marked as in force" not in published, (
		"a dead end in force is something in force, and the index said there was nothing"
	)


def test_no_signpost_names_a_status_a_workspace_may_rename () -> None:
	"""`SR#1076`. `_in_force_keys` exists in that file *because* this went wrong once.

	A status key is renameable and its category is not (§5.5), and `SR#1036` records what
	sending the literal cost: an installation that renamed ``active`` did not get an empty
	index, it got **no index at all**, because both transports refuse an unknown status by
	name. The lesson reached the query and not the two strings twenty lines away — the
	resource's own `also_at`, and the sentence every session is handed at `initialize`.

	**On a signpost nothing refuses it**, which is why it survived: the URL is not sent by us,
	so a reader following it simply gets an answer about a status they do not have — or a 422
	naming a key they never chose.

	Narrowing honestly would need ``?status_category=``, which `GET /v1/documents` does not
	offer where `GET /v1/tasks` does. That gap is `SR#1087`; until then the signpost
	over-returns, which its own comment already argues is the honest way to be wrong.

	Scanned rather than asserted per site, because the next signpost is the one nobody will
	think of.
	"""

	keys = {status.key for status in subroutine.db.seed._STATUSES}

	client = unittest.mock.MagicMock(spec=subroutine.clients.base.Client)

	said = [
		*[tool.description for tool in subroutine.mcp.tools.catalogue(client)],
		*[
			f"{one.description} {one.also_at or ''}"
			for one in subroutine.mcp.tools.references(client)
		],
		subroutine.mcp.session._instructions("local"),
	]

	for text in said:
		for key in keys:
			assert f"status={key}" not in text, (
				f"a signpost names the status key {key!r}, which a workspace may rename: {text}"
			)


def test_the_channel_that_binds_you_names_every_kind_of_thing_that_binds_you (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#1036`. The one guard that fails against the code as it stood, and it is the item.

	``subroutine://conventions`` asked ``type=decision&status=active``, and **six documents
	were in force, governing, and excluded by the type filter alone** on this project's own
	instance — with nothing wrong with how any of them was written. `#242` is the one that
	shows the cost: it is the release procedure, and the standing instruction about it is to
	read it rather than reconstruct it. An agent working here is told that by the project notes
	it is handed at session start; an agent on **any other installation** is told nothing at
	all, because a procedure is typed ``spec``.

	**One case exercises the type set, the derivation and the grouping together.** A
	specification and a design in force are both named, under headings that say which is
	which — because *we decided this*, *the specification says this* and *this route is closed*
	are different obligations, and a flat list conflates them.

	A ``note`` is written alongside and must **not** appear: the fix is a classification, not a
	widening to everything.
	"""

	client, first, _second = _two_workspaces(session)

	with client:
		for kind, title in (
			("decision", "Work is filed against an item first"),
			("spec", "Cutting a release: one command, two pushes"),
			("design", "Accountability is a property of the agent, not of the task"),
			("dead_end", "A half-open range over path does not substitute for a prefix match"),
			("note", "Ran the gate twice on Tuesday"),
		):
			client.create_document(workspace=first, type=kind, title=title, status="active")

	session.flush()

	published = _bound_to(session, first)

	for title in (
		"Work is filed against an item first",
		"Cutting a release: one command, two pushes",
		"Accountability is a property of the agent",
		"A half-open range over path",
	):
		assert title in published, f"in force, governing, and not named: {title!r}"

	assert "Ran the gate twice" not in published, (
		"a note describes rather than binds, and widening to every type is not the fix"
	)

	for governing in subroutine.domain.documents.GOVERNING:
		assert f"## {governing.heading}" in published, (
			f"{governing.key} is named but not grouped, so a reader cannot tell what it obliges"
		)


def test_renaming_the_status_that_means_in_force_does_not_empty_the_index (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The status half of `#1036`, falsified separately — the type fix alone passes it.

	The resource sent ``status="active"`` as a literal, and **a status key is this workspace's
	own vocabulary** (§5.5).

	**Falsifying corrected the item, which predicted an empty index.** It is not empty — both
	transports refuse an unknown status by name, so the whole resource fails and the agent is
	handed *there is no document status called 'active' here* in place of everything that binds
	it. `#496`'s shape on the one channel an agent is told to read before its first write, and
	worse than `#496`, which at least answered.

	It needs no new endpoint: ``/v1/meta`` is already fetched here for `#496`'s
	ambiguous-workspace check and publishes every status with its fixed ``category`` beside it.

	The rename is done in the database rather than through a client because **no surface can
	rename a status** — `#826` measures that the vocabulary is seeded and reachable from
	nothing, which is why this defect could sit unnoticed. That makes it a latent one today and
	a live one the moment `#826` is answered.
	"""

	client, first, _second = _two_workspaces(session)

	with client:
		client.create_document(
			workspace=first, type="decision", title="Colour marks exceptions", status="active",
		)

	session.flush()

	assert "Colour marks exceptions" in _bound_to(session, first), "the seed did not take"

	workspace = session.scalars(
		sqlalchemy.select(subroutine.db.models.identity.Workspace).where(
			subroutine.db.models.identity.Workspace.slug == first
		)
	).one()

	current = session.scalars(
		sqlalchemy.select(subroutine.db.models.vocabulary.Status).where(
			subroutine.db.models.vocabulary.Status.workspace_id == workspace.id,
			subroutine.db.models.vocabulary.Status.entity_type == "document",
			subroutine.db.models.vocabulary.Status.category
			== subroutine.domain.documents.CURRENT_CATEGORY,
		)
	).one()
	current.key = "in_force"

	session.flush()

	published = _bound_to(session, first)

	assert "Colour marks exceptions" in published, (
		"the index empties when a workspace renames the status that means in force"
	)


def test_finishing_something_nobody_claimed_says_so (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#777`. `#705` mandated claiming and the behaviour did not change.

	Measured the day after it shipped: nine items opened and closed, none claimed, none through
	`in_progress`, the event history empty of both — and measured again by this item with the
	same answer. **The instruction went into the skill, and the skill reaches a session through
	a plugin cache that lags**, so a session can be told nothing and be none the wiser.

	This is on the surface that runs on the *instance* (`#539`), which is why a caller's stale
	plugin cannot be a version of it behind — the item's third condition, met structurally.
	"""

	ref = _added(bound, "Something nobody took")
	answered, failed = _called(bound, "subroutine_done", ref=ref)

	assert not failed, answered
	assert "not claimed" in answered, answered


def test_finishing_something_still_held_hands_the_claim_back_with_it (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""**Finishing releases**, and this test used to assert that it did not.

	`#1113` changed the behaviour underneath it, which is what a test written for a *fact*
	does when the fact moves. It said *still claimed by @you — release it*: true, actionable,
	and asking for the one thing that reliably does not happen, because an obligation falling
	at the end of a session is one nobody attends. Thirty tasks on the live instance carried a
	claim on work that was finished and shipped.

	Re-aimed rather than deleted, because the property underneath survives: whoever finishes
	something is told what happened to the claim on it.
	"""

	ref = _added(bound, "Something taken properly")

	claimed, failed = _called(bound, "subroutine_claim", ref=ref)

	assert not failed, claimed

	answered, failed = _called(bound, "subroutine_done", ref=ref)

	assert not failed, answered
	assert "went back" in answered, answered
	assert "release=true" not in answered, "it asks for something that has already happened"
	assert "not claimed" not in answered, "it was claimed, and this says the opposite"


def test_a_listing_says_which_items_are_expensive_to_read (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#595`, Simon's question, answered by measurement within the hour he asked it.

	`subroutine_show` on one document here returns 128,083 characters — about 32,000 tokens —
	and the row an agent reads before deciding was the same shape as a row for a three-word
	note. `MAX_ANSWER` exists and is referenced in exactly one place: the raw `call_api` escape
	hatch. **So the curated tool an agent is told to use was uncapped and unannounced, and the
	hatch it is told to use sparingly was capped**, which is backwards.

	This is the *before* half, which is the item's title. Marked rather than measured out loud
	on every row, because §12.2a's rule is that a column saying the same thing everywhere says
	nothing — and almost every item is a few hundred bytes.
	"""

	small = _added(bound, "A short one")
	big = _added(bound, "A long one")

	_called(
		bound, "subroutine_update", ref=big,
		description="x" * (subroutine.domain.text.LARGE_PROSE + 1),
	)

	listed, failed = _called(bound, "subroutine_list")

	assert not failed, listed

	numbered = [(re.match(r"#(\d+)", line), line) for line in listed.splitlines()]
	rows = {int(found.group(1)): line for found, line in numbered if found is not None}

	assert {small, big} <= set(rows), f"both rows have to be here to compare: {listed}"

	# **A digit and then the k**, not a bare letter: the first version of this asked whether
	# `"k"` was in the row, and every row carries the word `task`.
	marked = re.compile(r"\b\d+k\b")

	assert marked.search(rows[big].split(" A long one")[0]), rows[big]
	assert not marked.search(rows[small].split(" A short one")[0]), (
		"a mark on every row is a column that says nothing (§12.2a)"
	)


def test_a_listing_row_says_when_an_item_is_finished (
	bound: subroutine.mcp.protocol.Server, session: sqlalchemy.orm.Session
) -> None:
	"""**`#874` on the agent's surface, and the half `#873` made reachable.**

	`views.status_is_news` stays quiet about finished work because ``show`` prints a completion
	date beside the status. A **row** has no date and no room for one, so a finished item was
	indistinguishable from an open one — and `#873` made ``subroutine_search 815`` return
	finished work by design, 548 of this instance's 721 tasks.

	Found by driving the served instance rather than by a test: `#815` came back marked
	``holds up work`` and said nothing about being over.

	**Two rows, because the assertion that matters is the negative one** — the same reason the
	started test above has two. A single-row listing would satisfy this whatever the rule did.
	"""

	finished = _added(bound, "Rotate the certificates")
	untouched = _added(bound, "Sweep the logs")

	closed, failed = _called(bound, "subroutine_done", ref=finished)

	assert not failed, closed

	# **Through `subroutine_search` on the ref, because that is the path that made this
	# reachable.** `subroutine_list` has no way to ask for finished work at all; `#873` gave a
	# bare number one, which is exactly how a finished row started appearing unannounced.
	listed = _called(bound, "subroutine_search", q=str(finished))[0]
	rows = {
		line.split()[0]: line for line in listed.splitlines() if line.startswith("#")
	}

	assert "done" in rows[f"#{finished}"], listed

	ordinary = _called(bound, "subroutine_list")[0]

	assert f"#{untouched}" in ordinary, "the probe listed nothing, so it proves nothing"
	assert "done" not in ordinary, (
		f"an unfinished row was told it was over: {ordinary!r}"
	)


def test_an_agent_can_change_how_something_repeats_and_stop_it (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#94`. **The half of repeating that was unreachable from this surface entirely.**

	``subroutine_add``'s line grammar creates one, so an agent could always file a repeating
	task — and then had no way to change the rule or end it, because a captured line is typed
	once. That is the ratchet's test answered precisely (§21.2): not *is there room* but *what
	would an agent get wrong without it*, and the answer was everything after the first day.

	Driven through the protocol rather than through ``_updated``, because every silent-discard
	defect in this arc was a value lost between the schema that declared it and the column that
	stores it — a handler test would have passed against all three.
	"""

	ref = _added(bound, "Water the plants by 2026-12-01 every 3 days")

	changed, failed = _called(
		bound, "subroutine_update", ref=ref, repeat="every other tuesday"
	)

	assert not failed, changed
	assert "every other week, on Tuesday" in changed, changed

	# **Read back through a second call**, not from the write's own answer: a handler that
	# rendered what it was sent rather than what it stored would satisfy the line above.
	shown, _ = _called(bound, "subroutine_show", ref=ref)

	assert "every other week, on Tuesday" in shown, shown

	stopped, failed = _called(bound, "subroutine_update", ref=ref, repeat="")

	assert not failed, stopped
	assert "every other week" not in stopped, stopped

	# And the work in hand survived, which is the whole difference between stopping a series
	# and deleting one.
	after, _ = _called(bound, "subroutine_show", ref=ref)

	assert "Water the plants" in after
	assert "every other week" not in after, after


@pytest.mark.parametrize(
	"timezone",
	# Zones whose offset from UTC is never zero, so a rendering that skipped the conversion
	# cannot pass by coincidence. `Pacific/Kiritimati` is +14 and `Pacific/Midway` is -11, which
	# between them put the expiry on a different **day** as well as a different hour.
	["Europe/London", "Pacific/Kiritimati", "Pacific/Midway"],
)
def test_a_claim_expiry_is_shown_on_the_same_clock_as_everything_else_an_agent_reads (
	session: sqlalchemy.orm.Session,
	bound: subroutine.mcp.protocol.Server,
	timezone: str,
) -> None:
	"""`SR#1185`, and the site `SR#1091` did not reach.

	The change feed converts through the account's zone with `SR#1091`'s whole argument behind
	it; this printed ``claim_expires_at.isoformat()``, which is UTC. First contact measured the
	two an hour apart on one instance (`SR#1183`), so a lease taken at 12:11 read as having
	expired *before* the events that had just renewed it.

	**Asserted against the stored instant rather than against a fixed string**, because the
	lease duration is not this test's subject and pinning it would fail whoever changes it.
	"""

	who = session.scalars(sqlalchemy.select(subroutine.db.models.identity.User)).all()
	assert len(who) == 1, "the fixture's one account is what carries the zone"

	who[0].timezone = timezone
	session.flush()

	ref = _added(bound, "Renew the certificate")
	taken, failed = _called(bound, "subroutine_claim", ref=ref)

	assert not failed, taken

	expires = session.scalars(
		sqlalchemy.select(subroutine.db.models.work.Task.claim_expires_at).where(
			subroutine.db.models.work.Task.ref == ref
		)
	).one()

	assert expires is not None, "a claim with no expiry cannot exercise the rendering"

	here = expires.astimezone(zoneinfo.ZoneInfo(timezone))
	elsewhere = expires.astimezone(datetime.UTC)

	assert f"{here:%d %b %H:%M}" in taken, (
		f"{timezone}: a lease expiring at {expires.isoformat()} was shown as:\n{taken}"
	)

	# **The control, and it is what makes the assertion above mean anything.** A format wide
	# enough to contain both renderings would satisfy the first check while still printing the
	# server's clock, which is the defect.
	assert f"{elsewhere:%d %b %H:%M}" not in taken, (
		f"{timezone}: the lease is still being shown in UTC:\n{taken}"
	)


def test_a_link_in_the_feed_names_both_ends_and_the_relation (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`SR#1186`. It read ``linked it to something``, which is the option `SR#302` rejected.

	Weighing how to fix a visibility leak, `SR#302` listed *omit the far end* and said of it:
	"it makes an item's own history less useful — 'linked to something' is a worse record than
	'blocks #42'." The renderer had arrived at that wording independently, while the far end sat
	in the event's own ``changes`` because `SR#252` put it there so the feed could be scoped.

	**Both ends rather than the far one**, because the two readers disagree about what is
	already on the page: the feed prints the subject beside the phrase and an item's history
	does not.
	"""

	first = _added(bound, "Lay the foundations")
	second = _added(bound, "Put the walls up")

	joined, failed = _called(bound, "subroutine_link", ref=first, type="blocks", other=second)
	assert not failed, joined

	# **The feed withholds events under a second old** — a ``seq`` becomes visible at commit
	# rather than at insert. Without this the empty branch answers and the assertion below
	# is made about nothing, which the test beside `SR#253` records as its own trap.
	time.sleep(subroutine.domain.events.WATERMARK.total_seconds() + 0.2)

	feed, failed = _called(bound, "subroutine_changes")
	assert not failed, feed

	assert f"linked #{first} blocks #{second}" in feed, (
		f"a link event should name both ends and the relation:\n{feed}"
	)
	assert "linked it to something" not in feed, "the wording SR#302 rejected is still here"


def test_a_link_event_missing_its_ends_says_the_honest_thing_instead () -> None:
	"""The degradation `SR#302` may force, driven rather than assumed.

	That item weighs omitting the far end from ``changes`` so a reader who cannot see it is told
	nothing about it. If that lands, this phrasing has no refs to name — and the fallback must be
	the old wording rather than a broken string, which is the only reason the old wording is kept.
	"""

	def event (changes: dict[str, typing.Any] | None) -> subroutine.views.Event:
		"""Return a link event carrying whatever payload this case is about."""

		return subroutine.views.Event(
			seq=1,
			id=uuid.uuid4(),
			entity_type="link",
			entity_id=uuid.uuid4(),
			workspace_id=uuid.uuid4(),
			subject_type="task",
			subject_id=uuid.uuid4(),
			action="created",
			changes=changes,
			actor_user_id=None,
			actor_token_id=None,
			created_at=subroutine.db.types.utcnow(),
		)

	whole = {
		"link_type": {"from": None, "to": "blocks"},
		"source": {"from": None, "to": 7},
		"target": {"from": None, "to": 9},
	}

	assert subroutine.views.happened(event(whole)) == "linked #7 blocks #9"

	# Each end removed in turn, because a fallback that only fires when *everything* is gone
	# would still break on the shape SR#302 actually proposes.
	for missing in ("source", "target", "link_type"):
		partial = {k: v for k, v in whole.items() if k != missing}

		assert subroutine.views.happened(event(partial)) == "linked it to something", (
			f"with {missing} withheld the phrase must fall back, not half-render"
		)

	assert subroutine.views.happened(event(None)) == "linked it to something"


def test_a_change_an_agent_reads_is_named_in_the_readers_words (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`SR#1187`. The feed said ``changed status_id``, naming a column nothing else mentions.

	**The map already existed in the terminal** and had since it was written — built to satisfy
	§13.5b's forbidden vocabulary, so readable column names were a side effect rather than the
	goal. That is why nobody looking at this surface suspected a solution one module away.
	"""

	ref = _added(bound, "Renew the lease")

	moved, failed = _called(bound, "subroutine_update", ref=ref, status="in_progress")
	assert not failed, moved

	# **The feed withholds events under a second old** — a ``seq`` becomes visible at commit
	# rather than at insert. Without this the empty branch answers and the assertion below
	# is made about nothing, which the test beside `SR#253` records as its own trap.
	time.sleep(subroutine.domain.events.WATERMARK.total_seconds() + 0.2)

	feed, failed = _called(bound, "subroutine_changes")
	assert not failed, feed

	assert "how it is going" in feed, f"a status change should read as words:\n{feed}"

	for column in ("status_id", "assignee_id", "claimed_by_id", "snoozed_is_all_day"):
		assert column not in feed, f"{column} is a database name and reached an agent"


def test_the_two_surfaces_name_a_changed_field_identically () -> None:
	"""One map, so the terminal and an agent cannot drift — which is how this arose.

	The CLI's changes feed translated and its *history* did not, because they went through two
	different functions. Both now go through :func:`subroutine.views.field_in_words`, and this
	asserts the map is reachable and non-trivial rather than that the two agree — two copies
	agreeing is exactly what hid the original defect.
	"""

	assert subroutine.views.field_in_words("status_id") == "how it is going"
	assert subroutine.views.field_in_words("assignee_id") == "who has it"

	# A column nobody has mapped still loses its internal suffix rather than reaching a reader.
	assert subroutine.views.field_in_words("some_new_column_id") == "some new column"
	assert subroutine.views.field_in_words("title") == "title"


def test_a_deleted_blocker_leaves_an_agents_count_too (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`SR#1403` on the third surface that keeps a count.

	The terminal, this and the browser each derive `N of M` from the same links, and a rule
	applied to one of three is this codebase's signature defect. `readiness.unblocked` already
	excluded a deleted blocker; the counts did not.
	"""

	refs = []

	for title in ("ROADMAP", "PHASE 1", "PHASE 2"):
		written, failed = _called(bound, "subroutine_add", text=title)

		assert not failed, written

		numbered = re.search(r"#(\d+)", written)

		assert numbered is not None, written

		refs.append(int(numbered.group(1)))

	_called(bound, "subroutine_link", ref=[refs[1], refs[2]], other=refs[0])

	before, _failed = _called(bound, "subroutine_show", ref=refs[0])

	assert "0 of 2 blockers done" in before, before

	_called(bound, "subroutine_call_api", method="DELETE", path=f"/v1/tasks/{refs[2]}")

	after, _failed = _called(bound, "subroutine_show", ref=refs[0], tree=True)

	assert "0 of 1 blockers done" in after, after
	assert "What has to happen first (0 of 1 done)" in after, after
	assert after.count("(deleted)") == 2, (
		f"the row is marked in the links section and in the tree:\n{after}"
	)


def test_an_agent_can_read_a_whole_plan_in_one_call (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`SR#1358`, on the surface it was measured on.

	SR#1290's reviewer built 42 links across 28 items and had no way to look at them: `show`
	answers one node at a time, so checking the order of work would have meant 28 calls to
	reconstruct what they had just written. **They verified it by reasoning instead**, which is
	the part worth worrying about.

	**Opt-in**, because it is a walk — three queries per level against nothing at all for a
	caller that did not ask — and because §13's context economy makes a tree of thirty rows a
	real cost on every `show` that did not want one.
	"""

	refs = []

	for title in ("ROADMAP", "PHASE 1", "PHASE 2", "Fix the agenda", "Tag it"):
		written, failed = _called(bound, "subroutine_add", text=title)

		assert not failed, written

		numbered = re.search(r"#(\d+)", written)

		assert numbered is not None, written

		refs.append(int(numbered.group(1)))

	_called(bound, "subroutine_link", ref=[refs[1], refs[2]], other=refs[0])
	_called(bound, "subroutine_link", ref=refs[3], other=refs[1])
	_called(bound, "subroutine_link", ref=refs[4], other=refs[2])

	plain, failed = _called(bound, "subroutine_show", ref=refs[0])

	assert not failed, plain
	assert "What has to happen first" not in plain, (
		f"a walk was paid for by a caller that did not ask:\n{plain}"
	)

	walked, failed = _called(bound, "subroutine_show", ref=refs[0], tree=True)

	assert not failed, walked
	assert "What has to happen first (0 of 4 done)" in walked, walked

	# **Depth-first, and indented by depth** — a flat list carrying a depth only reads as a
	# tree if the rows arrive in the order somebody looks at them.
	rows = walked.split("What has to happen first")[1].splitlines()[1:]
	order = [line.strip().split("  ", 1)[1] for line in rows if line.strip().startswith("#")]

	assert order == ["PHASE 1", "Fix the agenda", "PHASE 2", "Tag it"], walked

	deep = next(line for line in rows if "Fix the agenda" in line)
	shallow = next(line for line in rows if "PHASE 1" in line)

	assert len(deep) - len(deep.lstrip()) > len(shallow) - len(shallow.lstrip()), walked


def test_one_call_joins_an_item_to_several_others (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`SR#1352`, on the surface SR#1290 measured as worst.

	*"An agent on a remote instance has no shell to fall back on — so the surface this product
	is named for is the one where it is worst."* Twenty-three creates and thirty-seven links for
	one modest project is sixty round trips.

	**An argument on the existing tool rather than a fourteenth**, which is what SR#1352 asked
	for and what §21.2's count cap requires: the byte cap was retired on 2026-08-23 and the
	count one was not.

    **All four spellings**, because a model sends back the notation it was shown and this API
	prints `#42` everywhere: a bare number, a list of them, a list of strings, and a
	comma-separated string.
	"""

	refs = []

	for title in ("Ship it", "Changelog", "Tag it", "Announce it", "Update the docs"):
		written, failed = _called(bound, "subroutine_add", text=title)

		assert not failed, written

		numbered = re.search(r"#(\d+)", written)

		assert numbered is not None, written

		refs.append(int(numbered.group(1)))

	said, failed = _called(bound, "subroutine_link", ref=refs[0], other=refs[1:4])

	assert not failed, said
	assert said.count("\n") == 2, f"one line per link, both ends named:\n{said}"

	for one in refs[1:4]:
		assert f"#{one}" in said, said

	# **The string form too**, which is what arrives when a model writes a list the way it read
	# one rather than as JSON.
	also, failed = _called(bound, "subroutine_link", ref=refs[0], other=f"#{refs[4]}")

	assert not failed, also
	assert f"#{refs[4]}" in also, also


def test_a_bad_ref_among_several_joins_nothing (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`SR#1352`. The refusal names the value, and no link is written before the list is read.

	Nothing here spans a transaction — each link is its own call — so resolving every end first
	is what stops a typo in the fourth of five leaving three made and no statement of which.
	"""

	refs = []

	for title in ("Ship it", "Changelog", "Tag it"):
		written, _failed = _called(bound, "subroutine_add", text=title)
		numbered = re.search(r"#(\d+)", written)

		assert numbered is not None, written

		refs.append(int(numbered.group(1)))

	# **A good list first, so this cannot pass by the whole call being refused.** Without it,
	# a version that rejects *every* list satisfies both assertions below — the call fails and
	# nothing is written — which is exactly what the unfixed code does. Measured: this test
	# passed against `HEAD` until this line existed.
	made, failed = _called(bound, "subroutine_link", ref=refs[0], other=[refs[1]])

	assert not failed, made

	refused, failed = _called(
		bound, "subroutine_link", ref=refs[0], other=[refs[2], "nope"]
	)

	assert failed, refused
	assert "'nope'" in refused, f"the entry is named rather than the whole list:\n{refused}"

	shown, _failed = _called(bound, "subroutine_show", ref=refs[0])

	assert "Tag it" not in shown, (
		f"a link was written before the whole list had been read:\n{shown}"
	)
	assert "Changelog" in shown, "and the good list before it is still there"


def test_a_link_echo_names_which_item_gates_which (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`SR#1190`. It answered `Blocks #4 <title>` — the far end alone.

	**A one-ended echo cannot disconfirm a reversed call.** Getting the arguments backwards
	produces an equally plausible answer naming the item you did not mean to gate, which is why
	this is worth four characters: direction is the single most confusable thing about the
	feature, and the skill spends a paragraph on *`ref` is the blocker*.

	The withdraw branch has always named both, so this was an inconsistency inside one tool.
	"""

	first = _added(bound, "Pour the slab")
	second = _added(bound, "Frame the roof")

	made, failed = _called(bound, "subroutine_link", ref=first, type="blocks", other=second)

	assert not failed, made
	assert made.startswith(f"#{first} "), f"the echo does not say which end is the blocker:\n{made}"
	assert f"#{second}" in made, made

	# **The reversed call must read differently**, which is the whole point — under the old
	# wording the two answers differed only by which title came back.
	#
	# A fresh pair, because reversing *this* one is a ring and is refused by name. That
	# refusal is the other half of the same protection and is already covered elsewhere; what
	# is untested is whether an agent that gets the arguments backwards on two *unrelated*
	# items can tell from the answer.
	third = _added(bound, "Glaze the windows")
	fourth = _added(bound, "Hang the doors")

	back, failed = _called(bound, "subroutine_link", ref=fourth, type="blocks", other=third)

	assert not failed, back
	assert back.startswith(f"#{fourth} "), back
	assert f"#{third}" in back, back


def test_filing_a_subtask_says_what_it_was_filed_under (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`SR#1191`. `parent` was accepted and never echoed.

	`_added`'s own docstring is the argument against that: *a caller that cannot see the parse
	cannot tell a deadline that was read from one that stayed in the title*. The rule was
	written for the grammar and `parent` is an argument, so it slipped past a principle that
	plainly covers it — the caller equally cannot tell an accepted parent from a dropped one.
	"""

	whole = _added(bound, "Rewire the kitchen")

	answer, failed = _called(
		bound, "subroutine_add", text="Chase the sockets", parent=whole
	)

	assert not failed, answer
	assert f"part of #{whole}" in answer, f"the parent it was filed under is not said:\n{answer}"

	# The parent really took — otherwise this asserts a sentence rather than a filing.
	shown, failed = _called(bound, "subroutine_show", ref=whole)
	assert not failed, shown
	assert "Sub-tasks" in shown, f"the parent has no parts, so the echo was a lie:\n{shown}"

	# And an ordinary capture says nothing, because nobody named a parent — §1.4's rule that a
	# default nobody chose is not a fact worth a line.
	plain, failed = _called(bound, "subroutine_add", text="Buy cable")

	assert not failed, plain
	assert "part of" not in plain, plain


def test_changing_a_task_says_what_changed_not_only_what_it_now_is (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`SR#1196`. The echo returned the whole row, minus the one field just written.

	**A relative date is where this earns its keep.** `now+3M` cannot be checked by inspection,
	so the resolved day is the only confirmation there is — and first contact spent a
	`subroutine_show` to learn where one had landed (`SR#1183`).
	"""

	ref = _added(bound, "Service the boiler")

	answer, failed = _called(bound, "subroutine_update", ref=ref, defer="now+3M", importance=2)

	assert not failed, answer
	assert "(set " in answer, f"the echo does not say what it set:\n{answer}"
	assert "importance 2" in answer, answer

	# The resolved day, not the words the caller typed — which is the whole point.
	assert "now+3M" not in answer, "the echo repeated the input instead of resolving it"

	shown, failed = _called(bound, "subroutine_show", ref=ref)
	assert not failed, shown

	deferred = next(
		(part for part in answer.split("(set ")[1].rstrip(")").split(", ") if part.startswith("defer ")),
		None,
	)

	assert deferred is not None, f"no defer in the echo:\n{answer}"

	day = deferred.removeprefix("defer ").strip()

	assert day in shown, (
		f"the echo said {day!r} and the item does not agree:\n{shown}"
	)


def test_an_agent_can_refuse_to_lose_somebody_elses_paragraphs (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`SR#842`, §8.9, on the surface where the other writer is not in the room.

	**A revision is a whole-body replace**, so a lost update here takes every paragraph
	somebody else added rather than one field — and nothing afterwards records that they
	existed. The browser has sent `expected_version` on a revision since `SR#761`, arguing in
	its own comment that *it matters more here than on a task*; the terminal and this surface
	were the two that could not.

	**It was blocked on a byte cap that no longer exists.** `SR#842` was filed on 2026-08-12
	reading *"roughly 90 bytes against 55 of headroom — so it needs the cap raised"*, and Simon
	retired the byte cap on 2026-08-23 (`SR#1124` Q3). What survives is a cap on the *count* of
	tools, which an argument on an existing one does not touch. An open ticket is a claim about
	the world, and this one's stated prerequisite had expired.

	**The ratchet's test, answered** (§21.2): not *is there room* but *what would an agent get
	wrong without it?* Without it, an agent revising a document it read two calls ago destroys
	whatever landed in between, is told the write succeeded, and has no way to find out.
	"""

	written, failed = _called(
		bound, "subroutine_document", title="A conclusion", body="First thoughts."
	)

	assert not failed, written

	numbered = re.search(r"#(\d+)", written)

	assert numbered is not None, written

	ref = int(numbered.group(1))

	# What the agent read, and the version it read it at.
	first, failed = _called(bound, "subroutine_show", ref=ref)

	assert not failed, first

	# Somebody else revises it in between — unguarded, because they read it after the agent did.
	between, failed = _called(
		bound, "subroutine_document", ref=ref, body="Their careful paragraphs."
	)

	assert not failed, between

	stale, failed = _called(
		bound,
		"subroutine_document",
		ref=ref,
		body="Mine, written from the old text.",
		expected_version=1,
	)

	assert failed, f"the agent's stale revision was accepted:\n{stale}"

	kept, _failed = _called(bound, "subroutine_show", ref=ref)

	assert "Their careful paragraphs." in kept, kept

	# **And the guard is opt-in, which §8.9 requires**: `None` means *did not ask*, never
	# *asked and passed*. An agent that omits it writes exactly as it did before, so nothing
	# that has been calling this tool since `SR#822` starts failing.
	current, failed = _called(
		bound, "subroutine_document", ref=ref, body="Sent without a version."
	)

	assert not failed, current


def test_a_version_that_is_not_a_number_is_refused_rather_than_dropped (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`SR#842`. Dropping it silently is the failure §8.9 exists to prevent.

	A caller that sends a version and has it ignored believes the write was guarded when it was
	not — worse than never offering the guard, because it converts a real protection into a
	false one. And the schema saying ``integer`` is not enough on its own: over HTTP pydantic
	coerces, where the local client is handed the value as it stands, so the two transports
	would otherwise disagree about which values count.
	"""

	written, failed = _called(
		bound, "subroutine_document", title="A conclusion", body="First thoughts."
	)

	assert not failed, written

	numbered = re.search(r"#(\d+)", written)

	assert numbered is not None, written

	ref = int(numbered.group(1))

	# **The spelling a model actually sends**, which is the string form of what it just read.
	guarded, failed = _called(
		bound, "subroutine_document", ref=ref, body="Stale.", expected_version="1"
	)

	assert failed, f"'1' was dropped rather than read as a version:\n{guarded}"

	nonsense, failed = _called(
		bound, "subroutine_document", ref=ref, body="Stale.", expected_version="soon"
	)

	assert failed, f"an unreadable version was dropped rather than refused:\n{nonsense}"
	assert "version" in nonsense.lower(), nonsense


def test_a_write_says_where_it_landed_even_with_no_marker_to_read (
	bound: subroutine.mcp.protocol.Server, tmp_path: pathlib.Path
) -> None:
	"""`#1438`: the one case that needed telling was the one case that said nothing.

	``_checkout`` reports which project a **marker** chose, so with no marker there was nothing
	to append and the answer named the ref and not the project. Reported by Simon on
	2026-08-27, on two decision documents in the workspace Inbox.

	**The transport it was found on cannot read a marker at all.** Over ``subroutine-remote``
	these handlers run on the *server*, so ``directory.find()`` reads the server's working
	directory and the caller's checkout is on a machine this code has never seen. `#1219` cured
	the same symptom for stdio and could not reach that door — which is why this is asserted of
	the **outcome**, off the created item, rather than of the marker.
	"""

	os.chdir(tmp_path)

	added, failed = _called(bound, "subroutine_add", text="Something to do")

	assert not failed, added
	assert "filed in inbox" in added

	wrote, failed = _called(
		bound, "subroutine_document", title="What we concluded", body="Because."
	)

	assert not failed, wrote
	assert "filed in inbox" in wrote


def test_a_write_says_nothing_about_where_when_the_caller_chose (
	bound: subroutine.mcp.protocol.Server, tmp_path: pathlib.Path
) -> None:
	"""The other half of `#1438`, and a guard is what decided it.

	``test_an_ordinary_capture_carries_no_extra_line`` says a caption for the empty case is
	*paid on every capture*, which is §13's rule that response size is a first-order cost
	here. So the destination is said only where **nobody** chose it. Where the caller wrote
	``+web``, the ``(read +web)`` echo already says where this went, and repeating it is the
	wallpaper §12.2a warns about on the surface that pays for it by the byte.
	"""

	os.chdir(tmp_path)
	_called(bound, "subroutine_project", key="web", title="Web")

	added, failed = _called(bound, "subroutine_add", text="Something to do +web")

	assert not failed, added
	assert "filed in" not in added


def test_the_echo_stays_beside_the_title_when_another_line_is_added (
	bound: subroutine.mcp.protocol.Server, tmp_path: pathlib.Path
) -> None:
	"""`#426`'s rule, which was held by a fixture that had nothing else to say.

	The echo is appended with two spaces to whatever the answer currently ends with, and both
	``part of #7`` and the marker's line are added *before* it. So on a capture that has either,
	``(read !4/2 ~2h)`` attached itself to that line instead of to the title — the exact
	adjacency `#426` exists to control, on the one surface whose skill names this line as *the*
	check.

	**Pre-existing and found by `#1438`**, which adds a third such line and so made the guard
	fire. Asserted with a parent rather than with the new line, because the defect is older
	than the new line and would outlive removing it.
	"""

	os.chdir(tmp_path)
	parent, failed = _called(bound, "subroutine_add", text="Parent item")

	assert not failed, parent

	ref = int(parent.split("#")[1].split()[0])
	made, failed = _called(
		bound, "subroutine_add", text="Fix the header !4/2 ~2h", parent=ref
	)

	assert not failed, made

	# **The first line, because there are others now** — which is the whole point: `#426`'s own
	# test can partition the whole answer only because its fixture produces nothing after the
	# echo, and that is exactly the blindness this test exists to cover.
	head, separator, echoed = made.split("\n")[0].partition("  (read ")

	assert separator, f"the echo is not marked off at all: {made}"
	assert echoed == "!4/2 ~2h)", made
	assert head.endswith("Fix the header"), head


def test_a_written_document_says_its_state_only_when_it_is_not_the_ordinary_one (
	bound: subroutine.mcp.protocol.Server, tmp_path: pathlib.Path
) -> None:
	"""`#1188`, **and the item's own premise had expired when I came to build it.**

	It says a specification starts as a draft. Measured while writing this test, which is how
	it was caught: ``documents.IN_FORCE_WHEN_WRITTEN`` is derived from ``SEEDED_ITEM_TYPES``
	and now holds **every** document type, so `#537` was answered and everything starts in
	force. The first version of this test asserted ``draft`` for a ``spec`` and failed against
	correct code.

	So the status is a constant unless somebody asked for something else, and §12.2a decides
	the rest: a line that says the same thing every time says nothing.

	**``status_is_default`` looked like the field for it and answers a different question**,
	which this test caught: it means *this is the workspace's default status*, and a decision
	written into force is ``active`` where the default is ``draft`` — so it is false on the
	ordinary case, which is the opposite of what is wanted.
	"""

	os.chdir(tmp_path)

	ordinary, failed = _called(
		bound, "subroutine_document", title="What we decided", body="Because.", type="decision"
	)

	assert not failed, ordinary
	assert "active" not in ordinary, ordinary

	asked, failed = _called(
		bound, "subroutine_document", title="Still working", body="Later.", type="spec",
		status="draft",
	)

	assert not failed, asked
	assert "draft" in asked


def test_an_agent_can_put_a_document_in_force_and_take_it_back (
	bound: subroutine.mcp.protocol.Server, tmp_path: pathlib.Path
) -> None:
	"""`#1188`'s other half, and one without it leaves the complaint standing.

	``create_document`` and ``update_document`` have both taken ``status`` since `#506` and
	this tool offered it on neither — so an agent writing a specification got a draft, was not
	told, and had no tool to change it. First contact reached for
	``subroutine_call_api(method="PATCH", …)``, the most context-expensive call on the surface.

	**The revision half is the one the item was filed from**: the case is a correction —
	something written as a draft that turns out to bind the next session.
	"""

	os.chdir(tmp_path)

	written, failed = _called(
		bound, "subroutine_document", title="Still thinking", body="Maybe.", type="decision",
		status="draft",
	)

	assert not failed, written
	assert "draft" in written

	ref = int(written.split("#")[1].split()[0])
	promoted, failed = _called(bound, "subroutine_document", ref=ref, status="active")

	assert not failed, promoted
	assert "active" in promoted


def test_the_document_tool_says_which_kinds_start_in_force (
	bound: subroutine.mcp.protocol.Server,
) -> None:
	"""`#1188`'s third part: the rule is explained on the terminal and was absent here.

	``subroutine doc create --help`` states it. This is the surface **told to read the
	conventions index before its first write**, and it is the surface that could not see why a
	draft of its own never appeared there.

	`#499`'s rule, one level down: the guaranteed channel has to name what the optional ones
	carry, and a tool description is the only thing every session is certain to read.

	**And it may not name the status key**, which `#1076`'s guard caught in the first version
	of this sentence. A key is renameable (§5.5) and an installation that renamed ``active``
	got *no index at all* — so a tool description states the behaviour and the schema property
	beneath it carries the example, which is the shape ``subroutine_update`` already uses.
	"""

	# **Through ``tools/list``, which is how a client actually learns what exists.** Reading
	# the objects would test the catalogue and not the contract, and this assertion is about
	# what reaches a session.
	answered = _exchange(bound, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
	described = {
		tool["name"]: tool["description"] for tool in answered[0]["result"]["tools"]
	}["subroutine_document"]

	assert "in force" in described
	assert "hold it back" in described
	assert "subroutine://conventions" in described
