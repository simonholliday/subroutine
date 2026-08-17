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

import datetime
import json
import os
import typing

import httpx
import pytest
import sqlalchemy.orm

import api_support
import conftest
import subroutine.api.app
import subroutine.api.mcp
import subroutine.api.security
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.db.base
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.sessions
import subroutine.mcp.protocol
import subroutine.mcp.relay
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


def test_a_revision_this_server_does_not_speak_is_refused (
	world: test_api_tasks.World,
) -> None:
	"""`#941`, cold review `#927` M-31 — the transport makes this refusal mandatory.

	Until this landed the header was read by nothing, so ``banana`` and ``2099-01-01`` were
	both answered ``200`` — measured on a served instance rather than inferred.

	**The refusal names the revision this server speaks**, which is the half that makes a 400
	useful: it is how an old server and a new client find each other, and a body saying only
	*no* leaves the caller to guess whether to retry lower.
	"""

	for announced in ("banana", "2099-01-01", "2026-07-28", ""):
		answered = _message(
			world,
			{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
			headers={subroutine.api.mcp.VERSION_HEADER: announced},
		)

		assert answered.status_code == 400, f"{announced!r} was answered"

		body = answered.json()

		assert body["code"] == "unsupported_protocol_version"
		assert subroutine.mcp.protocol.PROTOCOL_VERSION in body["detail"]


def test_the_revisions_this_server_does_speak_are_answered (
	world: test_api_tasks.World,
) -> None:
	"""Both halves, because a check that refused everything would pass the test above.

	**Absent is allowed and read as the revision the transport says to assume**, and that
	value is allowed in its written form too — refusing what is served implicitly would be two
	answers to one question. Driven rather than asserted against the constant: what matters is
	that a real request gets through, not that a set contains a string.
	"""

	for headers in (
		{},
		{subroutine.api.mcp.VERSION_HEADER: subroutine.mcp.protocol.PROTOCOL_VERSION},
		{subroutine.api.mcp.VERSION_HEADER: subroutine.api.mcp.ASSUMED_WHEN_ABSENT},
	):
		answered = _message(
			world,
			{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
			headers=headers,
		)

		assert answered.status_code == 200, f"{headers} was refused: {answered.text}"
		assert answered.json()["result"]["tools"], "the answer is a real one"


def test_the_handshake_itself_carries_no_version_and_must_not_need_one (
	world: test_api_tasks.World,
) -> None:
	"""The measurement this refusal was built on, held as a test.

	``claude-code/2.1.226`` sends no ``MCP-Protocol-Version`` on ``initialize`` — correct,
	since there is nothing negotiated yet — and the negotiated value on everything after. A
	check that demanded the header would refuse the one request that establishes it, which is
	the way this could have locked every remote client out of a working instance.
	"""

	answered = _message(
		world,
		{
			"jsonrpc": "2.0",
			"id": 0,
			"method": "initialize",
			"params": {"protocolVersion": "2025-11-25"},
		},
	)

	assert answered.status_code == 200, answered.text

	agreed = answered.json()["result"]["protocolVersion"]

	assert agreed == subroutine.mcp.protocol.PROTOCOL_VERSION

	# And the version it was just handed is one it may then announce, which is what makes the
	# handshake and the refusal one arrangement rather than two.
	assert (
		_message(
			world,
			{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
			headers={subroutine.api.mcp.VERSION_HEADER: agreed},
		).status_code
		== 200
	)


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


# --- The stdio adapter, driven onto this endpoint ---------------------------------------


def _through_the_adapter (
	world: test_api_tasks.World,
	monkeypatch: pytest.MonkeyPatch,
	*,
	name: str = "local",
	url: str | None = None,
	display_name: str | None = None,
	elsewhere: tuple[str, ...] = (),
	workspace: str | None = None,
	message: str = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
) -> dict[str, typing.Any] | None:
	"""Send one raw line through ``subroutine mcp`` and return what it writes back.

	Nothing between the two ends is stubbed except *which* application the local forwarder
	drives: the message is forwarded unparsed, answered by the real endpoint against a real
	database, and the answer is corrected on the way out. That is the whole of `#539` in one
	call, and it is the only arrangement that can show the two halves composing.
	"""

	monkeypatch.setattr(
		subroutine.api.app, "create_app", lambda **kwargs: world.application
	)

	built = [
		subroutine.connections.Connection(name=name, url=url, display_name=display_name)
	]
	built.extend(subroutine.connections.Connection(name=other) for other in elsewhere)

	return subroutine.mcp.relay.answering(
		built[0],
		subroutine.connections.Roster(connections=tuple(built), default=name),
		subroutine.config.Settings(dev_mode=True),
		workspace=workspace,
	)(message)


def test_a_stdio_session_is_answered_by_this_endpoint (
	world: test_api_tasks.World, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#539`. One implementation of a tool call, and it is the instance's.

	``subroutine mcp`` used to build a catalogue of its own, so what a tool did depended on
	which package happened to be installed on the calling machine. It forwards now, and this
	drives a real line through the adapter into the real endpoint.
	"""

	answered = _through_the_adapter(world, monkeypatch)

	assert answered is not None
	assert answered["result"]["serverInfo"]["name"] == "subroutine"


def test_a_local_session_is_narrowed_by_the_credential_it_was_given (
	world: test_api_tasks.World, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#927`'s H-9 — the plugin's `token` field was advertised containment and read by nothing.

	`_in_process` called `principal` with a username and no credential, so `SUBROUTINE_TOKEN`,
	`SUBROUTINE_TOKEN_<NAME>` and `credentials.toml` were all ignored on a local connection —
	while `_over_http` twelve lines up resolved one properly. The same `--scope task:read`
	service account answered `claudebot (agent) … Narrowed to scopes task:read` at the terminal
	and `si (person) … instance:admin` here.

	**Nothing could have caught it.** `test_plugin` checks that the manifest wires the variable
	into the process, which was true and is a different claim from anything reading it — a
	control declared, documented and inert, which is this codebase's second signature defect.

	Driven through `subroutine_whoami`, because who the session *is* is exactly what the defect
	was about and it is the one tool whose whole answer is that.
	"""

	issued = world.call(
		"POST",
		"/v1/tokens",
		json={"title": "narrow", "service_account": "claudebot", "scopes": ["task:read"]},
	)

	assert issued.status_code == 201, issued.text

	monkeypatch.setenv("SUBROUTINE_TOKEN_LOCAL", issued.json()["token"])

	answered = _through_the_adapter(
		world,
		monkeypatch,
		message=(
			'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
			'"params":{"name":"subroutine_whoami","arguments":{}}}'
		),
	)

	assert answered is not None, "the adapter answered nothing"

	said = answered["result"]["content"][0]["text"]

	assert "claudebot" in said, f"the session was not the credential's principal: {said}"
	assert "task:read" in said, f"the narrowing was not in force: {said}"


def test_the_adapter_names_the_connection_the_caller_typed (
	world: test_api_tasks.World, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The instance calls itself something; the caller calls it something else (`#330`).

	The label in the instructions is the reader's own alias for this server, and the server has
	never heard it — so it writes its own name and this side corrects it. Driven rather than
	asserted against the substitution, because a rewrite that silently found nothing would leave
	the instance's name in place and look exactly like this test passing.
	"""

	answered = _through_the_adapter(world, monkeypatch, display_name="acme-work")

	assert answered is not None

	said = answered["result"]["instructions"]

	assert "on connection 'acme-work'" in said, said
	assert "Test" not in said, "the instance's own name for itself reached the caller"


def test_the_adapter_restores_what_the_far_end_could_not_know (
	world: test_api_tasks.World, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#276`'s clause, which the server cannot write and which this side has to add back.

	Naming only the bound instance is what let an agent be confident it knew where it was: the
	sentence was true, and nothing in it suggested the name was one of several. That roster
	belongs to the caller, so moving the instructions to the server would have dropped it —
	silently, and only on machines with more than one connection.
	"""

	answered = _through_the_adapter(
		world, monkeypatch, elsewhere=("work", "acme")
	)

	assert answered is not None

	said = answered["result"]["instructions"]

	assert "work" in said and "acme" in said
	assert "cannot reach them" in said, said


def test_one_connection_hears_nothing_about_connections (
	world: test_api_tasks.World, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""§1.4. An instruction about instances costs every session that will never have two."""

	answered = _through_the_adapter(world, monkeypatch)

	assert answered is not None
	assert "cannot reach them" not in answered["result"]["instructions"]


def test_a_workspace_travels_as_the_query_the_plugin_already_uses (
	world: test_api_tasks.World, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""One spelling of it rather than two — `#539`.

	Asserted through what the session is *told*, because that is the observable consequence: the
	endpoint writes the clause only when it was given a workspace, so the sentence appearing is
	evidence the parameter arrived.
	"""

	answered = _through_the_adapter(
		world, monkeypatch, workspace=world.workspace.slug
	)

	assert answered is not None
	assert f"'{world.workspace.slug}' workspace" in answered["result"]["instructions"]


def test_a_malformed_message_is_refused_by_the_far_end_rather_than_by_the_adapter (
	world: test_api_tasks.World, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The adapter must not become a second implementation of the protocol.

	It forwards bytes it has deliberately not parsed, so what a broken message gets back is the
	instance's answer — the same one an HTTP caller gets. Parsing here to be helpful is how the
	two transports come to disagree, which is `#530` one layer up.
	"""

	answered = _through_the_adapter(
		world, monkeypatch, message="{not json at all"
	)

	assert answered is not None
	assert answered["error"]["code"] == subroutine.mcp.protocol.PARSE_ERROR


def test_one_request_records_its_credentials_use_once (
	world: test_api_tasks.World, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#565`. Twice was a deadlock, and the deadlock needed no concurrency at all.

	**This endpoint is the only place in the application that opens two sessions for one
	request** — the request's own, and the client's, which `#527` examined and found correct.
	Both authenticated the same credential, so both wrote `token.last_used_at`. The first took
	a row lock that `api/routing.Transactional` holds until *after* the handler returns
	(`#36`); the second blocked on it; the handler could not finish. One request, deadlocked
	against itself, on a freshly started process with no other traffic.

	**It looked intermittent because of the throttle.** `LAST_USED_INTERVAL` is a minute, and
	inside it neither resolution writes — so the endpoint worked, then stopped, then worked.
	Proven on the served instance: `/mcp` hung for 20s cold, and answered in 0.00s when a
	single-session request had committed the timestamp seconds earlier.

	**Counting a request once is not a workaround.** A request is one use; counting it twice
	was always wrong, and the deadlock is only what made it visible.

	**The suite could not have caught this and still cannot.** `api_support.factory_for` binds
	every session to the test's one connection, so the two sessions here share it and cannot
	lock each other — the *one of a thing* shape, at the fixture. So this counts the writes
	rather than reproducing the block, and the block itself is measured against a real
	instance and recorded on the item.
	"""

	counted: list[typing.Any] = []
	original = subroutine.domain.authentication._record_use

	def counting (token: typing.Any, moment: typing.Any) -> None:
		"""Record that a write was attempted, then do it."""

		counted.append(token.token_prefix)
		original(token, moment)

	monkeypatch.setattr(subroutine.domain.authentication, "_record_use", counting)

	assert _said(_tool(world, "subroutine_whoami")), "the call has to succeed to prove anything"

	assert len(counted) == 1, (
		f"one request wrote last_used_at {len(counted)} times, on {len(set(counted))} "
		f"credential(s) — twice on one row is the deadlock `#565` was"
	)


def test_the_second_resolution_leaves_the_token_clean (
	world: test_api_tasks.World,
) -> None:
	"""The rule stated where it is enforced, rather than only where it is used.

	`record_use=False` exists so a caller acting *as* the requester does not count the request
	a second time. Accepting the argument and ignoring it would put the row back into the
	caller's transaction, which is the whole defect — so this asserts nothing is left pending.
	"""

	world.session.flush()

	before = dict(world.session.dirty)

	found = subroutine.domain.authentication.authenticate(
		world.session, world.secret, record_use=False
	)

	assert found.token is not None
	assert dict(world.session.dirty) == before, (
		"resolving with record_use=False dirtied the session, so the write is back inside the "
		"caller's transaction and its row lock is held to the end of the request"
	)

	# And the ordinary path still records, or the throttle would be the only thing writing it.
	world.session.expire_all()
	subroutine.domain.authentication.authenticate(world.session, world.secret)

	assert world.session.dirty, "the default must still record the use"


@pytest.fixture
def two_connections (tmp_path: typing.Any) -> typing.Iterator[str]:
	"""Yield a PostgreSQL database of this test's own, for two genuinely separate sessions.

	**The shared engine cannot be used and that is the point** (`#565`).
	``api_support.factory_for`` binds every session to the test's one connection, so the two
	sessions an MCP request opens share it and cannot lock one another — which is why 3,067
	tests were green while the served endpoint deadlocked on its first cold call. Reproducing
	it needs two real connections, and this follows `tests/test_instances.py`'s pattern of
	creating and dropping a database rather than borrowing the suite's.

	PostgreSQL only: SQLite locks the whole file, so it cannot show a *row* lock and would
	report a different failure for a different reason.
	"""

	reason = conftest._postgres_unavailable_reason()

	if reason is not None:
		if conftest.REQUIRE_POSTGRES:
			pytest.fail(reason)

		pytest.skip(reason)

	name = f"subroutine_lastused_{os.getpid()}_{abs(hash(tmp_path)) % 100000}"
	admin = sqlalchemy.create_engine(
		conftest.POSTGRES_ADMIN_URL, isolation_level="AUTOCOMMIT"
	)

	try:
		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{name}"'))
			connection.execute(sqlalchemy.text(f'CREATE DATABASE "{name}"'))

		yield conftest.with_database(conftest.POSTGRES_ADMIN_URL, name)

		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))

	finally:
		admin.dispose()


def test_authenticating_twice_in_one_request_does_not_block (two_connections: str) -> None:
	"""`#565`, reproduced at the mechanism rather than through the endpoint.

	An MCP request authenticates the same credential in two sessions. Both used to write
	`token.last_used_at`; the first's row lock is held until `Transactional` commits *after*
	the handler, so the second blocked on it and the handler never returned.

	`lock_timeout` turns the hang into a failure a test can assert on — without it this test
	would express the defect by never finishing, which is not a test.

    Both halves are asserted: recording twice **must** block, or the reproduction has stopped
	reproducing and the passing half proves nothing.
	"""

	engine = sqlalchemy.create_engine(two_connections)

	try:
		subroutine.db.base.Base.metadata.create_all(engine)
		factory = sqlalchemy.orm.sessionmaker(bind=engine, expire_on_commit=False)

		with factory() as setup:
			made = subroutine.domain.bootstrap.initialise(
				setup, username="si", instance_name="Test"
			)
			_row, issued = subroutine.domain.authentication.issue_token(
				setup, user=made.user, title="probe"
			)
			setup.commit()
			secret = issued.value.get_secret_value()

		# Older than LAST_USED_INTERVAL, so both resolutions want to write.
		cold = subroutine.db.types.utcnow() - datetime.timedelta(hours=1)

		def authenticate_twice (record_use: bool) -> bool:
			"""Return whether the second resolution completed, as one request does it."""

			with factory() as first, factory() as second:
				second.execute(sqlalchemy.text("SET lock_timeout = '3s'"))

				subroutine.domain.authentication.authenticate(first, secret, now=cold)
				first.flush()

				try:
					subroutine.domain.authentication.authenticate(
						second, secret, now=cold, record_use=record_use
					)
					second.flush()

					return True

				except sqlalchemy.exc.OperationalError:
					return False

		assert not authenticate_twice(record_use=True), (
			"recording the use twice no longer blocks, so this test has stopped reproducing "
			"`#565` and the assertion below proves nothing"
		)

		assert authenticate_twice(record_use=False), (
			"the second resolution still blocks on the first — the request deadlocks against "
			"itself and the endpoint stops answering"
		)

	finally:
		engine.dispose()


def test_the_address_this_request_arrived_at_is_not_an_origin_it_answers (
	world: test_api_tasks.World
) -> None:
	"""**The one place this check and the browser cookie's must differ** (`SR#639`).

	`SR#639` gave a cookie-authenticated write the same header check, and it may compare against
	the address the request arrived at: a session cookie carries no ``Domain``, so it is
	host-only, so a request holding one arrived at this instance's own name and that value is a
	fact rather than a claim.

	**Here the same value is exactly what the attacker chose.** Rebinding is a page on any
	origin resolving a name to loopback, so ``Host`` — and with it ``base_url`` — is whatever
	the attacker put in DNS. Allowing it would be allowing the attack, and the two checks share
	a mechanism precisely so that the difference between their lists is a decision somebody
	made rather than an accident of two implementations.
	"""

	arrived = _message(
		world,
		{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
		headers={"origin": api_support.BASE_URL},
	)

	assert arrived.status_code == 403, (
		f"the origin the request arrived at was answered, which is the rebinding attack: "
		f"{arrived.text[:200]}"
	)


# ---- which credential this transport takes (`SR#809`) ----------------------


def test_a_browser_session_is_not_a_credential_for_this_transport (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**Decided rather than inherited** — Simon, 2026-08-11, on review `SR#807`.

	This endpoint used to accept a cookie, and nobody chose that: `SR#516` read the header
	itself, `SR#539` replaced that with the application's own resolver chain because a second
	copy of an authentication rule is this codebase's signature defect, and `SR#248` then put
	the cookie resolver in that chain. Two right decisions and one absence.

	It was never an escalation — `SR#802` measured that — so what this refuses is a *shape*: the
	cheap way to give a browser-side agent this product is to declare these fourteen tools as
	page tools posting here with the cookie, and that hands `subroutine_call_api` to something
	reading text anybody with a credential may have written (`SR#808`).
	"""

	world = test_api_tasks._world(session)
	_row, secret = subroutine.domain.sessions.mint_link(session, user=world.user)
	_opened, cookie = subroutine.domain.sessions.redeem(session, secret)

	refused = api_support.call(
		world.application,
		"POST",
		subroutine.api.mcp.PATH,
		content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
		headers={"content-type": "application/json"},
		cookies={subroutine.api.security.SESSION_COOKIE: cookie},
	)

	assert refused.status_code == 403, refused.text
	assert "browser session" in refused.json()["detail"]
	assert "token create" in refused.json()["hint"], "the refusal did not say what to send"

	# **The same cookie still works everywhere else**, which is what says this is a rule about
	# one door rather than about the credential. Without it, a mutation revoking the session
	# would pass the assertion above for entirely the wrong reason.
	elsewhere = api_support.call(
		world.application,
		"GET",
		"/v1/me",
		cookies={subroutine.api.security.SESSION_COOKIE: cookie},
	)

	assert elsewhere.status_code == 200, "the refusal reached past the route it belongs to"
	assert elsewhere.json()["credential"]["kind"] == "web_session"


def test_a_token_is_untouched_by_that_refusal (world: test_api_tasks.World) -> None:
	"""The half that would break every agent if the scoping were got wrong.

	`SR#639`'s lesson, one layer over: a rule about one credential type has to be written so it
	cannot reach the others, and the only way to know it was is to drive the one that matters.
	"""

	answered = _message(world, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

	assert answered.status_code == 200, answered.text
	assert answered.json()["result"]["tools"], "a bearer token stopped reaching the tools"


def test_the_credential_write_does_not_hold_the_database_for_the_handler (
	tmp_path: typing.Any
) -> None:
	"""`#932`, from `#927` H-6. `#565` fixed the second authentication and left the first write.

	An MCP request authenticates in the request's session — writing ``last_used_at`` when the
	credential is more than a minute old — and ``api/routing.Transactional`` holds that
	transaction until after the handler. The handler then acts through its own client with its
	own session, for the two reasons ``api/mcp._client`` sets out. **On SQLite a write
	transaction locks the whole file**, so that second session blocked on the request's own
	lock until ``busy_timeout`` gave up.

	**Measured end to end on a served instance**: a write took ``0.04s`` on a credential used
	seconds before and ``5.04s``, refused with *"database is locked"*, on one idle two minutes.
	It failed exactly when an agent paused to think, and told the operator their
	``database_url`` was at fault.

	SQLite rather than PostgreSQL, and that is the opposite of `#565`'s test one screen up:
	that one needs a *row* lock, which only PostgreSQL has. This one needs a whole-file lock,
	which only SQLite has. The same request deadlocks against itself for two different reasons
	on the two backends, and neither harness can show the other's.

	**Both halves are asserted**, because a test that only shows the fix working cannot tell
	you it has stopped reproducing the defect.
	"""

	database = tmp_path / "held.db"
	engine = sqlalchemy.create_engine(
		f"sqlite:///{database}", connect_args={"timeout": 1.0}
	)

	try:
		subroutine.db.base.Base.metadata.create_all(engine)
		factory = sqlalchemy.orm.sessionmaker(bind=engine, expire_on_commit=False)

		with factory() as setup:
			made = subroutine.domain.bootstrap.initialise(
				setup, username="si", instance_name="Test"
			)
			_row, issued = subroutine.domain.authentication.issue_token(
				setup, user=made.user, title="probe"
			)
			setup.commit()
			secret = issued.value.get_secret_value()

		# Older than LAST_USED_INTERVAL, so authenticating genuinely writes.
		cold = subroutine.db.types.utcnow() - datetime.timedelta(hours=1)

		def the_handler_can_write (releasing: bool) -> bool:
			"""Return whether a second session can write while the request holds its own."""

			with factory() as request, factory() as handler:
				subroutine.domain.authentication.authenticate(request, secret, now=cold)

				# **Both branches flush**, so both have genuinely taken the write lock and the
				# only difference is whether it is then released. An earlier version flushed in
				# one branch only, so emptying the release turned the comparison into "a lock
				# against no lock at all" and the falsification passed without the fix.
				request.flush()

				if releasing:
					subroutine.api.security._release_the_authentication_write(request)

				try:
					# An UPDATE of a row that exists, so the statement assumes nothing about
					# the schema. The first version of this was an INSERT naming a column the
					# table does not have — which raises `OperationalError` too, so **both**
					# halves failed for the same unrelated reason and the reproduction half
					# passed without ever taking a lock.
					handler.execute(sqlalchemy.text("UPDATE instance SET name = 'probe'"))
					handler.commit()

					return True

				except sqlalchemy.exc.OperationalError as blocked:
					assert "locked" in str(blocked), (
						f"the handler failed for a reason that is not a lock: {blocked}"
					)

					return False

		assert not the_handler_can_write(releasing=False), (
			"an unreleased credential write no longer blocks the handler, so this test has "
			"stopped reproducing the defect and the assertion below proves nothing"
		)

		assert the_handler_can_write(releasing=True), (
			"the handler still blocks on the request's own credential write — every MCP write "
			"on a credential idle a minute fails with 'database is locked'"
		)

	finally:
		engine.dispose()


def test_a_remote_session_reaches_the_endpoint_and_speaks_the_transport (
	world: test_api_tasks.World, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""``_over_http`` is what every plugin with a URL and a token runs, and nothing drove it.

	The local forwarder above is exercised by half this file; the remote one — the whole point
	of the second plugin — was covered by nothing at all, which is how it came to be missing
	two headers the transport requires of a client. ``Accept`` offered JSON alone, so a server
	entitled to answer a stream would have found this client saying it could not read the only
	reply it can give; and no request carried ``MCP-Protocol-Version``, which the transport
	says a client must send on everything after the handshake.

	Driven through the real ``httpx.Client`` against the real application, with only the
	transport replaced — so the URL, the credential resolution, the headers and the error
	translation are all the ones a served session uses.
	"""

	sent: list[httpx.Headers] = []

	class Recording(httpx.BaseTransport):
		"""The in-process bridge, keeping what the relay asked it to send."""

		def __init__ (self) -> None:
			"""Wrap the transport the rest of the suite drives applications through."""

			self._inner = api_support.SyncTransport(world.application)

		def handle_request (self, request: httpx.Request) -> httpx.Response:
			"""Record the outgoing headers and answer from the application."""

			sent.append(request.headers)

			return self._inner.handle_request(request)

	built = httpx.Client
	monkeypatch.setattr(
		httpx,
		"Client",
		lambda **kwargs: built(**{**kwargs, "transport": Recording()}),
	)
	monkeypatch.setenv("SUBROUTINE_TOKEN_WORK", world.secret)

	connection = subroutine.connections.Connection(name="work", url=api_support.BASE_URL)
	answered = subroutine.mcp.relay.answering(
		connection,
		subroutine.connections.Roster(connections=(connection,), default="work"),
		subroutine.config.Settings(dev_mode=True),
		workspace=None,
	)('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')

	assert answered is not None, "the remote forwarder answered nothing at all"
	assert answered["result"]["serverInfo"]["name"] == "subroutine"

	assert sent, "no request reached the transport"
	assert sent[0]["mcp-protocol-version"] == subroutine.mcp.protocol.PROTOCOL_VERSION
	assert "application/json" in sent[0]["accept"]
	assert "text/event-stream" in sent[0]["accept"], (
		"a client must offer both, because a server is free to answer with a stream"
	)
