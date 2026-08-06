"""``POST /mcp`` — the instance serving its own agent surface (`#516`).

**Three of these are the point rather than coverage**, and each fails against a plausible
wrong implementation of this endpoint:

* **A narrow credential stays narrow.** The local client resolves its own principal from the
  environment when nothing tells it otherwise, and §12.1a says that with no token the sole
  account *is* the caller — correct on a personal machine and catastrophic on a served one. An
  endpoint that forgot to pass ``principal=`` would authenticate the request perfectly well and
  then do the work as an unrestricted principal, so the scopes would vanish silently. The
  no-credential test cannot catch that, because ``PrincipalDep`` refuses first; only a *valid
  but narrow* credential can tell the two apart.
* **A write is committed, and a later call can see it.** Two sessions are in play here — the
  request's, and the client's own — and ``api/inprocess.acting_as`` records what the other
  arrangement cost: a ``PATCH`` that answered ``200`` with the new title while the write was
  silently discarded.
* **Both transports answer a malformed message identically.** `#530` is that defect already
  filed against this server, and a second transport is exactly where it would be reintroduced.
"""

import json
import typing

import pytest
import sqlalchemy.orm

import api_support
import subroutine.api.mcp
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.mcp.protocol
import subroutine.mcp.session
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction.

	Delegated to ``test_api_tasks`` rather than rebuilt, so this file cannot drift into
	testing a differently-shaped installation from the one the API tests use.
	"""

	return test_api_tasks._world(session)


def _message (
	world: test_api_tasks.World, payload: dict[str, typing.Any], **kwargs: typing.Any
) -> typing.Any:
	"""Post one JSON-RPC message and return the raw response."""

	return world.call(
		"POST",
		subroutine.api.mcp.PATH,
		content=json.dumps(payload),
		headers={"content-type": "application/json", **kwargs.pop("headers", {})},
		**kwargs,
	)


def _result (world: test_api_tasks.World, payload: dict[str, typing.Any]) -> typing.Any:
	"""Post a message that is expected to succeed, and return its ``result``."""

	answered = _message(world, payload)

	assert answered.status_code == 200, answered.text

	return answered.json()["result"]


def _tool (
	world: test_api_tasks.World, name: str, **arguments: typing.Any
) -> dict[str, typing.Any]:
	"""Call one tool and return its result object."""

	answered: dict[str, typing.Any] = _result(
		world,
		{
			"jsonrpc": "2.0",
			"id": 1,
			"method": "tools/call",
			"params": {"name": name, "arguments": arguments},
		},
	)

	return answered


def _said (result: dict[str, typing.Any]) -> str:
	"""Return the text a tool answered with."""

	return str(result["content"][0]["text"])


def test_the_handshake_is_answered (world: test_api_tasks.World) -> None:
	"""``initialize`` over HTTP says what it says over stdio."""

	answered = _result(
		world,
		{
			"jsonrpc": "2.0",
			"id": 0,
			"method": "initialize",
			"params": {"protocolVersion": subroutine.mcp.protocol.PROTOCOL_VERSION},
		},
	)

	assert answered["protocolVersion"] == subroutine.mcp.protocol.PROTOCOL_VERSION
	assert answered["serverInfo"]["name"] == "subroutine"
	assert answered["capabilities"]["tools"] == {"listChanged": False}

	# The instructions name where the work lands. On a served endpoint that is the instance's
	# own name, because the caller's alias for this connection is private to their machine and
	# this side has never heard it (`#330`).
	assert "Test" in answered["instructions"]


def test_a_notification_is_answered_with_no_body (world: test_api_tasks.World) -> None:
	"""A message with no ``id`` gets ``202`` and nothing else.

	The stdio loop's rule is *write no line at all*; ``202 Accepted`` with an empty body is how
	that is spelled over HTTP. A ``200`` carrying an empty object would be a response to a
	client that is not waiting for one.
	"""

	answered = _message(world, {"jsonrpc": "2.0", "method": "notifications/initialized"})

	assert answered.status_code == 202
	assert answered.content == b""


def test_the_tools_are_the_ones_the_stdio_server_serves (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""The same catalogue, reached two ways.

	Driven rather than asserted against a list: both assemblies are built and both are asked,
	so a filter applied on one transport and not the other fails here rather than being found
	by an agent that cannot do something its neighbour can.
	"""

	over_http = _result(world, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

	direct = subroutine.mcp.session.over(
		subroutine.clients.local.Client(
			subroutine.connections.Connection(name="local"),
			subroutine.config.Settings(dev_mode=True),
			session_factory=api_support.factory_for(session),
		),
		label="local",
	)
	over_stdio = subroutine.mcp.protocol.answer(
		direct, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
	)

	assert over_stdio is not None
	assert [tool["name"] for tool in over_http["tools"]] == [
		tool["name"] for tool in over_stdio["result"]["tools"]
	]


def test_the_resources_are_readable_over_this_transport (
	world: test_api_tasks.World,
) -> None:
	"""The other half of the surface, and it reaches the instance a different way.

	A tool call goes through the client's service methods; a resource read goes through
	``reference()`` and ``meta()``, which build an application in order to answer — a
	documented late import taken so the CLI does not pay FastAPI's start-up cost. Doing that
	*inside* a request is a path nothing else exercises, so this drives every declared resource
	rather than asserting that the list is non-empty.

	The four exist because `#499` says the guaranteed channel must name every channel that is
	not: an agent is told about them in the instructions, and being told about a document it
	cannot open would be worse than not mentioning it.
	"""

	listed = _result(world, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
	uris = [resource["uri"] for resource in listed["resources"]]

	assert uris, "the instructions name these, so an empty list is a broken promise"

	for uri in uris:
		read = _result(
			world,
			{"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": uri}},
		)

		assert read["contents"][0]["text"].strip(), f"{uri} came back empty"


def test_a_tool_call_writes_and_the_write_is_visible (world: test_api_tasks.World) -> None:
	"""The property two sessions could quietly cost.

	The client commits its own unit of work in a session that is not the request's, so this
	asks the *next* call whether the first one landed rather than trusting what it said.
	"""

	added = _tool(world, "subroutine_add", text="Reach the instance over HTTP !4/3")

	assert added["isError"] is False

	listed = _tool(world, "subroutine_list")

	assert "Reach the instance over HTTP" in _said(listed)


def test_a_narrow_credential_is_not_widened_by_this_endpoint (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**The security test, and the only one that can see the failure it is written for.**

	A token scoped to reading must not be able to write here. It would be able to if the
	endpoint let the client resolve its own principal: with no token to narrow it,
	:func:`subroutine.domain.local.principal` identifies the sole account and returns a
	principal carrying no credential at all — which §12.1a reads as maximum trust, because on
	a personal machine the filesystem permission is the authentication.

	Falsified by removing ``principal=`` from ``api/mcp._client``: this test fails and the
	no-credential one below still passes, because ``PrincipalDep`` refuses before the client is
	ever built. That asymmetry is why both exist.
	"""

	world = test_api_tasks._world(session, scopes=["task:read"])

	refused = _tool(world, "subroutine_add", text="Something this token may not file")

	assert refused["isError"] is True
	assert "task:write" in _said(refused)

	# And the same credential can still read, so this is narrowing rather than a broken client.
	assert _tool(world, "subroutine_list")["isError"] is False


def test_a_request_with_no_credential_is_refused (world: test_api_tasks.World) -> None:
	"""No token, no answer — and not "the only account here", which is §12.1a's local rule."""

	answered = api_support.call(
		world.application,
		"POST",
		subroutine.api.mcp.PATH,
		content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
		headers={"content-type": "application/json"},
	)

	assert answered.status_code == 401
	assert answered.json()["code"] == "unauthenticated"


@pytest.mark.parametrize(
	"body", [b"{not json at all", b'"a string"', b"[]"], ids=["unparseable", "scalar", "batch"]
)
def test_a_malformed_message_is_answered_the_way_stdio_answers_it (
	world: test_api_tasks.World, body: bytes
) -> None:
	"""One parser, two transports — `#530`.

	The answer is compared against what :func:`subroutine.mcp.protocol.answer` produces for the
	same bytes, so this cannot pass by agreeing with a copy of the rule. ``[]`` is a JSON-RPC
	batch, which MCP removed in ``2025-06-18`` and which `2026-07-28` forbids explicitly.
	"""

	answered = world.call(
		"POST",
		subroutine.api.mcp.PATH,
		content=body,
		headers={"content-type": "application/json"},
	)

	expected = subroutine.mcp.protocol.answer(
		subroutine.mcp.protocol.Server((), name="subroutine", version="0"), body
	)

	assert answered.status_code == 400
	assert answered.json() == expected


def test_the_event_stream_is_not_offered (world: test_api_tasks.World) -> None:
	"""``GET`` is refused, and a client that tries carries on regardless.

	This server sends nothing a client did not ask for — the tools and resources are fixed at
	build time and the capabilities say so — so there is no stream to open. Measured against
	``claude-code/2.1.222``: it opens the ``GET``, is answered ``405``, and proceeds.
	"""

	assert world.call("GET", subroutine.api.mcp.PATH).status_code == 405


def test_a_browser_on_another_origin_is_refused (world: test_api_tasks.World) -> None:
	"""The transport specification's DNS-rebinding rule.

	A page on any origin can resolve a name to loopback and post to an MCP server; the defence
	is to ask where the request says it came from. An agent's own client sends no ``Origin`` at
	all, so this costs a real caller nothing.
	"""

	refused = _message(
		world,
		{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
		headers={"origin": "https://elsewhere.example"},
	)

	assert refused.status_code == 403
	assert "elsewhere.example" in refused.json()["detail"]


def test_an_origin_this_instance_answers_is_allowed (session: sqlalchemy.orm.Session) -> None:
	"""A configured origin gets through, so the check is a boundary rather than a wall."""

	world = test_api_tasks._world(session)
	world.application.state.settings = subroutine.config.Settings(
		dev_mode=True, cors_origins=["https://ui.example"]
	)

	answered = _message(
		world,
		{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
		headers={"origin": "https://ui.example"},
	)

	assert answered.status_code == 200


def test_an_unknown_query_parameter_is_refused (world: test_api_tasks.World) -> None:
	"""A typo'd ``?workspace`` is a refusal, not a silently different answer — `#379`.

	The failure this closes is the one an agent reported itself: an argument neither honoured
	nor refused produces a plausible, complete, wrong answer, and it survives every upgrade
	because all it needs is a client newer than its server.
	"""

	answered = _message(
		world, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, params={"workspce": "x"}
	)

	# `422`, which is what every other listing answers a name it does not accept with. A
	# different code here would be this endpoint having its own opinion about a rule the rest
	# of the API already settled.
	assert answered.status_code == 422
	assert "workspce" in answered.text
