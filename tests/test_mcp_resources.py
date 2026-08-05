"""An agent over MCP can read the guide written for it — `#483`.

There was no `subroutine_docs` tool, no resource, and for a client with no shell and no HTTP of
its own no other way to reach §13.3's guide. The reach guard excused it, on the grounds that
*"somebody holding a client has already got past the problem it solves"* — written from the CLI,
which has ``--help`` and ``explain``, and untrue of MCP, which has neither.

**Resources rather than a tool, because of the budget.** A tool's schema is context every
session carries whether it is called or not; the surface was 13 of 13 tools and 7,916 of 8,800
bytes when this landed, so a documentation tool was not affordable. A resource costs one line in
``resources/list`` and its content only when a model asks for it.
"""

import typing
import unittest.mock

import pytest
import sqlalchemy.orm

import subroutine.clients.base
import subroutine.db.models.vocabulary
import subroutine.domain.bootstrap
import subroutine.domain.documents
import subroutine.mcp.protocol
import subroutine.mcp.tools


class _NothingInParticular:
	"""Stands in for a :class:`subroutine.views.Meta`, naming as few of its fields as it can.

	**Two, since `#496`, and they had to be named.** This began as a stub with only
	``model_dump_json``, on the argument that these tests are about wiring and the vocabulary is
	proved against a real database in ``test_mcp``. That stopped being possible when the
	resources started deciding what to publish from *whether a workspace was chosen* — the
	condition is now part of the wiring, so a double that cannot express it can only be silent
	about the branch.

	An unambiguous installation, which is what every test in this file is about: one workspace
	or none, so both resources take their ordinary path and the `#496` branch belongs to the
	two-workspace tests in ``test_mcp`` where a real database can show it.
	"""

	workspace = None
	workspaces: typing.ClassVar[list[typing.Any]] = []

	def model_dump_json (self, **options: typing.Any) -> str:
		"""Serialise the way the real model does."""

		return '{"api_version": "0"}'


_NOTHING_IN_PARTICULAR = _NothingInParticular()


def _client (text: str = "the guide") -> typing.Any:
	"""Return a client that answers :meth:`reference` and records what it was asked for."""

	client = unittest.mock.MagicMock(spec=subroutine.clients.base.Client)
	client.reference.side_effect = lambda name: f"{text}: {name}"
	# A stand-in shaped like the real thing without listing every field: the point here is
	# the wiring, and the vocabulary itself is proved against a real database in `test_mcp`.
	client.meta.return_value = _NOTHING_IN_PARTICULAR

	return client


def _server (client: typing.Any) -> subroutine.mcp.protocol.Server:
	"""Return a server carrying the real resources over a stand-in client."""

	return subroutine.mcp.protocol.Server(
		[], name="subroutine", version="0",
		resources=subroutine.mcp.tools.references(client),
	)


def _ask (
	server: subroutine.mcp.protocol.Server, method: str, **params: typing.Any
) -> dict[str, typing.Any]:
	"""Send one request and return the answer."""

	answer = server.handle(
		{"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
	)

	assert answer is not None, f"{method} is a request and deserves an answer"

	return answer


def test_the_server_says_it_has_resources () -> None:
	"""A client that is not told will never ask, so the capability is the whole feature."""

	described = _ask(_server(_client()), "initialize", protocolVersion="2025-06-18")

	assert described["result"]["capabilities"]["resources"] == {
		"listChanged": False,
		"subscribe": False,
	}


def test_a_server_with_no_resources_does_not_claim_the_capability () -> None:
	"""Declared from what this server *has*, not from what the class can do.

	Otherwise a client is promised a channel, calls ``resources/list``, and is handed an empty
	one — which reads as a broken server rather than as a server without documents.
	"""

	bare = subroutine.mcp.protocol.Server([], name="subroutine", version="0")
	described = _ask(bare, "initialize", protocolVersion="2025-06-18")

	assert "resources" not in described["result"]["capabilities"]


def test_the_guide_the_examples_the_vocabulary_and_the_conventions_are_offered () -> None:
	"""The two documents §13.3 writes for this reader, and what `#486` and `#506` added."""

	listed = _ask(_server(_client()), "resources/list")["result"]["resources"]

	assert [row["uri"] for row in listed] == [
		"subroutine://docs/agent",
		"subroutine://docs/examples",
		"subroutine://meta",
		"subroutine://conventions",
	]

	for row in listed:
		assert row["description"], f"{row['uri']} must say what it is, or nobody opens it"

	assert [row["mimeType"] for row in listed] == [
		"text/markdown",
		"text/markdown",
		# **Not markdown**, and the difference is the point: the guide is prose a model reads
		# and this is a document it looks keys up in. A client that renders by media type
		# would otherwise show a wall of JSON as if it were something to read through.
		"application/json",
		# Markdown again: an index of conclusions is read, not looked up in.
		"text/markdown",
	]


def test_reading_one_fetches_it_from_the_instance (
) -> None:
	"""A route to the instance's copy, not a fourth edition of it (`#47`).

	Asserted as *the client was asked* rather than as the text matching: a resource holding its
	own copy would pass a text comparison happily and be wrong in the way this project spends
	most of its time on.
	"""

	client = _client()
	answer = _ask(_server(client), "resources/read", uri="subroutine://docs/agent")

	client.reference.assert_called_once_with("agent")

	content = answer["result"]["contents"][0]

	assert content["uri"] == "subroutine://docs/agent"
	assert content["text"] == "the guide: agent"


def test_nothing_is_fetched_until_it_is_asked_for () -> None:
	"""The budget argument, as behaviour: listing must not pull the documents over the wire.

	If building the catalogue read them, every session would pay for both whether or not the
	model ever opened one — which is the cost that made a documentation *tool* unaffordable in
	the first place, reintroduced by the back door.
	"""

	client = _client()
	_ask(_server(client), "resources/list")

	client.reference.assert_not_called()


def test_an_unknown_uri_is_refused_by_name () -> None:
	"""A wrong uri is a client's bug, so it is a protocol error rather than a result."""

	answer = _ask(_server(_client()), "resources/read", uri="subroutine://nope")

	assert "error" in answer
	assert answer["error"]["code"] == subroutine.mcp.protocol.INVALID_PARAMS
	assert "subroutine://docs/agent" in answer["error"]["message"], (
		"the refusal must name what there *is*, or a caller has to guess twice"
	)


def test_an_unreachable_instance_reads_as_a_failure_rather_than_a_crash () -> None:
	"""Every resource here is on the far end of a network, so this is the ordinary case.

	`fanout._attempt`'s lesson one layer over: a connection may fail, it may not escape. An
	exception out of the read would take down the process serving an editor's whole session.
	"""

	client = unittest.mock.MagicMock(spec=subroutine.clients.base.Client)
	client.reference.side_effect = RuntimeError("the instance is not there")

	answer = _ask(_server(client), "resources/read", uri="subroutine://docs/agent")

	assert "error" in answer
	assert "could not be read" in answer["error"]["message"]


def test_the_uri_a_resource_is_read_by_is_the_one_it_was_listed_under () -> None:
	"""Listing and reading must agree, or a client that follows the list gets a refusal."""

	server = _server(_client())
	listed = _ask(server, "resources/list")["result"]["resources"]

	for row in listed:
		answer = _ask(server, "resources/read", uri=row["uri"])

		assert "result" in answer, f"{row['uri']} was listed and cannot be read"


@pytest.mark.parametrize("name", ["agent", "examples"])
def test_the_instance_really_serves_what_the_resource_asks_for (name: str) -> None:
	"""The stand-in client above proves the wiring; this proves the names are real.

	A resource asking for ``"guide"`` when the client understands ``"agent"`` would pass every
	test above and fail on first contact — the shape of defect a mock is worst at seeing.
	"""

	import subroutine.api.meta

	built = {
		"agent": subroutine.api.meta.guide_text,
		"examples": subroutine.api.meta.examples_text,
	}[name]

	assert built(), f"{name} is named by a resource and the instance builds nothing for it"


def test_a_decision_is_in_force_the_moment_it_is_written (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#506`. §6.14's lifecycle fits a specification and was applied to six types.

	A spec is drafted, agreed, replaced — so ``draft`` is honest for it. A decision that has
	been taken is already in force, and calling it a draft is wrong the second the conversation
	ends. **Measured before this changed**: all 72 open documents on this project's own instance
	sat in ``draft``, including 26 decisions that plainly govern.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username="si", instance_name="Test Instance"
	)
	inbox = subroutine.domain.bootstrap.inbox_for(session, setup.workspace)

	assert inbox is not None

	written = {
		kind: subroutine.domain.documents.create(
			session, project=inbox, title=f"A {kind}", type_key=kind, actor=None
		)
		for kind in ("decision", "finding", "dead_end", "spec", "design", "note")
	}
	session.flush()

	def category (kind: str) -> str:
		"""Return the status category a document of this type started in."""

		found = session.get(
			subroutine.db.models.vocabulary.Status, written[kind].status_id
		)

		assert found is not None, f"a {kind} was written with no status at all"

		return found.category

	for kind in ("decision", "finding", "dead_end"):
		assert category(kind) == "current", f"a {kind} is true when it is written"

	for kind in ("spec", "design", "note"):
		assert category(kind) == "draft", f"a {kind} is drafted before it is agreed"


def test_a_status_somebody_asked_for_still_wins (session: sqlalchemy.orm.Session) -> None:
	"""`#506`. The default is a default, not a rule about what a decision may be.

	Somebody drafting a decision they have not taken yet must be able to say so, or the
	convenience becomes a constraint — and this is the half a default-by-type most easily
	breaks, because the type now carries an opinion the caller did not express.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username="si", instance_name="Test Instance"
	)
	inbox = subroutine.domain.bootstrap.inbox_for(session, setup.workspace)

	assert inbox is not None

	drafting = subroutine.domain.documents.create(
		session,
		project=inbox,
		title="Not settled yet",
		type_key="decision",
		status_key="draft",
		actor=None,
	)
	session.flush()

	found = session.get(subroutine.db.models.vocabulary.Status, drafting.status_id)

	assert found is not None and found.category == "draft"


def test_the_conventions_resource_lists_what_is_in_force_and_nothing_else () -> None:
	"""`#506`. The rules an agent must follow, from a channel it is told about.

	57 governing documents were open on this project's instance and the one file a session is
	guaranteed to read named 24 — so ten decisions were reachable only by searching, and
	nothing prompted a search. Decision `#499` one level up.
	"""

	client = _client()
	client.documents.return_value = [
		unittest.mock.MagicMock(ref=47, title="No work without an item first"),
		unittest.mock.MagicMock(ref=102, title="Colour marks exceptions"),
	]

	answer = _ask(_server(client), "resources/read", uri="subroutine://conventions")
	text = answer["result"]["contents"][0]["text"]

	client.documents.assert_called_once_with(
		workspace=None, type="decision", status="active"
	)

	assert "#47" in text and "No work without an item first" in text
	assert "#102" in text

	# **Titles, never bodies** (§14). An index that inlined every decision would be the
	# context cost this whole resource arrangement exists to avoid.
	assert len(text) < 1_000, "an index, not the documents themselves"


def test_an_empty_conventions_resource_says_why_rather_than_nothing () -> None:
	"""`#506`, on `#496`'s lesson. **A resource has no second call.**

	An empty index reads as "there are no rules here", which is a claim and a false one on any
	instance that has been used. It has to say what it did not look at and how to look wider —
	which `#496` found the vocabulary resource failing to do, on the workspace state a
	stranger's agent actually arrives in.
	"""

	client = _client()
	client.documents.return_value = []

	answer = _ask(_server(client), "resources/read", uri="subroutine://conventions")
	text = answer["result"]["contents"][0]["text"]

	assert "not the same as nothing having" in text
	assert "subroutine_list" in text, "an empty answer must name the wider question"
