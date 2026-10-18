"""A read-only session reads and changes nothing, and the instance enforces it - `SR#4562`.

**S1 of the cold review of 2026-10-05** (`#4506`), the one High: on a connection configured
``read_only = true`` the terminal was refused, while ``subroutine mcp`` wrote through every tool and
through ``subroutine_call_api`` - in process and against a served instance - on every release from
0.5.0. Each client kept the promise for itself, and the agent tools run on the instance, through a
client of their own that nobody had told.

**Decision `#4510`** moved the rule into the domain: a read-only connection says so on every request,
as the ``read_only`` query parameter, and the instance refuses that session every act that is not a
read, including the acts that ask no permission verb. These tests hold the instance's half and the
relay's; ``tests/test_transport_equivalence.py`` drives every write both clients have.
"""

import json
import typing
import uuid

import httpx
import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.api.app
import subroutine.clients.http
import subroutine.config
import subroutine.connections
import subroutine.db.models.activity
import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.errors
import subroutine.mcp.relay
import test_api_tasks

READ_ONLY = subroutine.domain.authentication.READ_ONLY


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def _events (world: test_api_tasks.World) -> int:
	"""Return how many events this installation holds, which a refused write must not change."""

	return int(
		world.session.scalar(
			sqlalchemy.select(sqlalchemy.func.count()).select_from(
				subroutine.db.models.activity.Event
			)
		)
		or 0
	)


def _titles (world: test_api_tasks.World) -> list[str]:
	"""Return every task's title, asked by a session that may write."""

	answered = world.call("GET", "/v1/tasks")

	assert answered.status_code == 200, answered.text

	return [row["title"] for row in answered.json()["items"]]


def test_a_read_only_session_is_refused_a_write_and_answered_a_read (
	world: test_api_tasks.World,
) -> None:
	"""The instance's half of decision `#4510`: ``read_only=true`` narrows the session it arrives with.

	**Refused in the domain's own words, which name no file and no connection**, since the session
	may come from another machine whose configuration this one cannot see.
	"""

	refused = world.call(
		"POST", "/v1/tasks", params={"read_only": "true"}, json={"title": "Not written"}
	)

	assert refused.status_code == 403, refused.text
	assert refused.json()["detail"] == READ_ONLY

	answered = world.call("GET", "/v1/tasks", params={"read_only": "true"})

	assert answered.status_code == 200, answered.text
	assert "Not written" not in _titles(world)


def test_saying_false_is_saying_nothing (world: test_api_tasks.World) -> None:
	"""``read_only=false`` is a session that may write, so a client may always send the flag."""

	made = world.call("POST", "/v1/tasks", params={"read_only": "false"}, json={"title": "Written"})

	assert made.status_code == 201, made.text


@pytest.mark.parametrize("order", [("true", "false"), ("false", "true")], ids=["true first", "false first"])
def test_a_flag_sent_twice_is_read_only_if_either_says_so (
	world: test_api_tasks.World, order: tuple[str, str]
) -> None:
	"""**Any true wins, so a repeated parameter can only narrow** - `SR#5131`, L-6 of the cold review
	of 2026-10-17, where nothing sent it twice."""

	refused = world.call(
		"POST",
		"/v1/tasks",
		params=[("read_only", order[0]), ("read_only", order[1])],
		json={"title": "Not written"},
	)

	assert refused.status_code == 403, refused.text
	assert refused.json()["detail"] == READ_ONLY
	assert "Not written" not in _titles(world)


def test_a_caller_s_query_cannot_switch_a_read_only_connection_s_flag_off () -> None:
	"""`SR#5131`: ``call_api`` on a read-only connection still says so, whatever its query says.

	httpx laid a request's parameters over the client's by replacing a key, so a query naming
	``read_only=false`` sent that alone, and the instance took the write. Driven against a server
	that records what arrived, from a release that holds the flag.
	"""

	sent: list[list[str]] = []

	def current (request: httpx.Request) -> httpx.Response:
		"""Answer as a release that holds the flag, recording what each write said of it."""

		if request.url.path == "/v1/meta":
			return httpx.Response(200, json={"workspaces": [], "instance_version": "0.10.0"})

		sent.append(request.url.params.get_list("read_only"))

		return httpx.Response(
			403,
			headers={"content-type": "application/problem+json"},
			json={"type": "about:blank", "title": "Forbidden", "status": 403, "detail": READ_ONLY},
		)

	client = subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url="https://employer.example.com", read_only=True),
		token="sr_not_a_real_one",
		transport=httpx.MockTransport(current),
		base_url="https://employer.example.com",
	)

	with client:
		answered = client.call_api(
			method="POST", path="/v1/tasks", body={"title": "No"}, query={"read_only": "false", "x": "1"}
		)

	assert answered.status == 403, answered.text
	assert sent == [["true"]], f"the caller's query changed what the connection sent: {sent}"


def test_a_value_that_says_neither_is_refused_by_name (world: test_api_tasks.World) -> None:
	"""Neither true nor false is refused, rather than guessed at in either direction."""

	answered = world.call("GET", "/v1/tasks", params={"read_only": "yes"})

	assert answered.status_code == 422, answered.text
	assert "read_only" in answered.json()["errors"][0]["field"]


def test_a_misspelt_flag_is_refused_rather_than_ignored (world: test_api_tasks.World) -> None:
	"""**It fails closed, as the flag's whole design does**: a write carrying something that is not
	the flag is refused as a parameter this endpoint does not accept, never taken as a write."""

	answered = world.call(
		"POST", "/v1/tasks", params={"READ_ONLY": "true"}, json={"title": "Not written"}
	)

	assert answered.status_code == 422, answered.text
	assert "Not written" not in _titles(world)


def test_the_installation_s_listings_answer_a_read_only_administrator (
	world: test_api_tasks.World,
) -> None:
	"""**An instance verb gates reads as well as writes**, so a read-only check on the verb alone would
	have refused an administrator's read-only session the installation's own listings."""

	for path in (
		"/v1/instance/workspaces",
		"/v1/instance/unreachable-projects",
		"/v1/instance/unadministered-workspaces",
		"/v1/admin/backups",
	):
		answered = world.call("GET", path, params={"read_only": "true"})

		assert answered.status_code == 200, f"{path}: {answered.text}"


def test_a_read_only_administrator_still_lists_everybody_s_credentials (
	world: test_api_tasks.World,
) -> None:
	"""The credential listing asks the accounts verb to show somebody else's, and that is a read."""

	colleague = subroutine.domain.users.create(
		world.session, username=f"keanu-{uuid.uuid4().hex[:6]}"
	)
	subroutine.domain.authentication.issue_token(
		world.session, user=colleague, title="Keanu's own"
	)
	world.session.flush()

	answered = world.call("GET", "/v1/tokens", params={"read_only": "true"})

	assert answered.status_code == 200, answered.text
	assert "Keanu's own" in answered.text


def test_the_agent_tools_on_the_instance_refuse_a_read_only_session_s_write (
	world: test_api_tasks.World,
) -> None:
	"""``/mcp`` runs every tool through a client of its own, which is how S1 happened: the flag
	reaches it on the request, and the second resolution of the credential carries it too."""

	before = _events(world)
	answered = world.call(
		"POST",
		"/mcp",
		params={"read_only": "true"},
		content=json.dumps(
			{
				"jsonrpc": "2.0",
				"id": 1,
				"method": "tools/call",
				"params": {"name": "subroutine_add", "arguments": {"text": "Not written"}},
			}
		),
		headers={"content-type": "application/json"},
	)

	assert answered.status_code == 200, answered.text

	result = answered.json()["result"]

	assert result.get("isError") is True, result
	assert READ_ONLY in json.dumps(result), result
	assert _events(world) == before


def _problem (detail: str) -> dict[str, typing.Any]:
	"""Return the refusal an instance from before decision `#4510` gives the flag it does not know."""

	return {
		"type": "about:blank",
		"title": "Validation failed",
		"status": 422,
		"code": "unknown_field",
		"detail": detail,
		"errors": [
			{
				"field": "read_only",
				"code": "unknown_field",
				"message": "'read_only' is not a parameter of this endpoint.",
			}
		],
	}


def test_an_instance_that_does_not_know_the_flag_is_said_to_be_too_old () -> None:
	"""**It fails closed, and says why in terms of what the person set.** Every release from 0.7.5 until
	this one refuses the parameter as unknown, which keeps a read-only connection from writing to it and
	reads, to somebody who never typed ``read_only``, as nonsense."""

	def older (request: httpx.Request) -> httpx.Response:
		"""Answer as a server from before the flag did, to a request carrying it."""

		assert request.url.params.get("read_only") == "true", "the flag was not sent"

		return httpx.Response(
			422,
			headers={"content-type": "application/problem+json"},
			json=_problem("This endpoint does not accept 'read_only'."),
		)

	client = subroutine.clients.http.Client(
		subroutine.connections.Connection(
			name="work", url="https://employer.example.com", read_only=True
		),
		token="sr_not_a_real_one",
		transport=httpx.MockTransport(older),
		base_url="https://employer.example.com",
	)

	with client, pytest.raises(subroutine.errors.ValidationError) as refused:
		client.tasks()

	assert "does not know the 'read_only' setting" in refused.value.detail
	assert "Upgrade work" in (refused.value.hint or "")


class Older:
	"""A server from before 0.7.5, which takes a write and ignores ``read_only`` on it - `SR#5116`.

	``refuses_meta`` answers ``/v1/meta`` as 0.7.1 does, refusing the parameter there as on every
	listing; otherwise it answers as 0.5.0 does, reporting ``version`` and ignoring the parameter.
	Every write that reaches it is recorded, and refused as a release that holds the flag would.
	"""

	def __init__ (self, version: str | None, *, refuses_meta: bool = False) -> None:
		"""Remember how this server answers."""

		self.version = version
		self.refuses_meta = refuses_meta
		self.written: list[str] = []
		self.asked_its_release = 0

	def __call__ (self, request: httpx.Request) -> httpx.Response:
		"""Answer one request as the old server would."""

		assert request.url.params.get("read_only") == "true", "the flag was not sent"

		if request.method != "GET":
			self.written.append(f"{request.method} {request.url.path}")

			return httpx.Response(
				403,
				headers={"content-type": "application/problem+json"},
				json={"type": "about:blank", "title": "Forbidden", "status": 403, "detail": "Read-only."},
			)

		self.asked_its_release += request.url.path == "/v1/meta"

		if self.refuses_meta or request.url.path != "/v1/meta":
			return httpx.Response(
				422,
				headers={"content-type": "application/problem+json"},
				json=_problem("This endpoint does not accept 'read_only'."),
			)

		return httpx.Response(
			200, json={"workspaces": [], **({} if self.version is None else {"instance_version": self.version})}
		)


def _read_only_client (server: Older) -> subroutine.clients.http.Client:
	"""Return a read-only connection's client to an old server."""

	return subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url="https://employer.example.com", read_only=True),
		token="sr_not_a_real_one",
		transport=httpx.MockTransport(server),
		base_url="https://employer.example.com",
	)


#: The writes H-3 of the cold review of 2026-10-17 made through a read-only connection to 0.7.1 and
#: 0.5.0: a credential, an agent's account and credential, a sign-in link, a task and its completion.
OLD_WRITES: dict[str, typing.Callable[[subroutine.clients.http.Client], typing.Any]] = {
	"token create": lambda client: client.issue_token(title="Spare"),
	"agent create": lambda client: client.create_user(username="scout", is_service_account=True),
	"login link": lambda client: client.create_login_link(),
	"add": lambda client: client.capture(text="Buy milk"),
	"done": lambda client: client.complete(ref=1),
	"call_api": lambda client: client.call_api(method="POST", path="/v1/tasks", body={"title": "No"}),
}


@pytest.mark.parametrize("write", OLD_WRITES, ids=list(OLD_WRITES))
@pytest.mark.parametrize(
	("server", "said"),
	[
		(lambda: Older(None, refuses_meta=True), "does not know the 'read_only' setting"),
		(lambda: Older("0.5.0"), "runs 0.5.0, and a release before 0.7.5 ignores"),
		(lambda: Older("0.7.4"), "runs 0.7.4, and a release before 0.7.5 ignores"),
		(lambda: Older("0.7.5.dev3+gabcdef0"), "cannot be placed against 0.7.5"),
		(lambda: Older(None), "does not say which release it runs"),
	],
	ids=["0.7.1", "0.5.0", "0.7.4", "a build of 0.7.5", "no version"],
)
def test_a_read_only_connection_writes_nothing_to_a_release_that_would_ignore_it (
	server: typing.Callable[[], Older], said: str, write: str
) -> None:
	"""`SR#5116`, H-3 of the cold review of 2026-10-17: refused before any write leaves the client.

	The flag fails closed only from 0.7.5, where every route refuses a parameter it does not know;
	0.7.1 refused it on its listings alone, so its write routes took a credential, a sign-in link, an
	account and a task from a read-only connection, and 0.5.0 ignored it everywhere. The client now
	asks the release first, and an instance that cannot be placed is refused like an old one.
	"""

	old = server()

	with _read_only_client(old) as client, pytest.raises(subroutine.errors.SubroutineError) as refused:
		OLD_WRITES[write](client)

	assert old.written == [], f"{write} reached an instance that ignores read_only"
	assert said in refused.value.detail, refused.value.detail
	assert "Upgrade work" in (refused.value.hint or "")


@pytest.mark.parametrize("version", ["0.7.5", "0.10.0", "0.10.1.dev4+g7a85a36", "1.0.0rc1"])
def test_a_release_that_holds_read_only_is_asked_once_and_sent_the_write (version: str) -> None:
	"""From 0.7.5 the write goes to the instance, which refuses it or does not know the flag.

	The release is read once, before the first write, and not again: a build between releases is
	placed by the release it names, so a served instance reporting a development version is held.
	"""

	current = Older(version)

	with _read_only_client(current) as client:
		for write in (lambda: client.issue_token(title="Spare"), client.create_login_link):
			with pytest.raises(subroutine.errors.Forbidden):
				write()

	assert current.written == ["POST /v1/tokens", "POST /v1/login-links"]
	assert current.asked_its_release == 1, "the release was asked again for the second write"


# --- The relay, on both of its paths ----------------------------------------------------------

#: What each tool that is not marked read-only is asked to do, against the targets below.
WRITES: dict[str, typing.Callable[[int, int], dict[str, typing.Any]]] = {
	"subroutine_add": lambda ref, other: {"text": "Not written"},
	"subroutine_comment": lambda ref, other: {"ref": ref, "body": "Not written."},
	"subroutine_document": lambda ref, other: {"title": "Not written", "body": "No."},
	"subroutine_update": lambda ref, other: {"ref": ref, "title": "Renamed"},
	"subroutine_link": lambda ref, other: {"ref": ref, "other": other, "type": "blocks"},
	"subroutine_project": lambda ref, other: {"key": "notwritten", "title": "Not written"},
	"subroutine_claim": lambda ref, other: {"ref": ref},
	"subroutine_done": lambda ref, other: {"ref": ref},
	"subroutine_call_api": lambda ref, other: {
		"method": "PATCH",
		"path": f"/v1/tasks/{ref}",
		"body": {"title": "Renamed"},
	},
}

#: And what each tool marked read-only is asked, which must still answer.
READS: dict[str, typing.Callable[[int, int], dict[str, typing.Any]]] = {
	"subroutine_list": lambda ref, other: {},
	"subroutine_search": lambda ref, other: {"q": "crew"},
	"subroutine_show": lambda ref, other: {"ref": ref},
	"subroutine_changes": lambda ref, other: {},
	"subroutine_journal": lambda ref, other: {},
	"subroutine_whoami": lambda ref, other: {},
}


def _relay (
	world: test_api_tasks.World, monkeypatch: pytest.MonkeyPatch, path: str
) -> typing.Callable[[str], dict[str, typing.Any] | None]:
	"""Start one ``subroutine mcp`` session onto this world, on a read-only connection.

	**In process**, the relay drives the application itself and builds the principal from the
	connection's own setting. **Remote**, it posts to ``/mcp`` with the flag in the query, through
	the transport the rest of the suite drives applications with.
	"""

	# **Only the relay's own application and its own HTTP client are replaced.** ``call_api`` builds
	# an application of its own for the request it makes, with a session factory, and talks to it
	# through an HTTP client of its own: replacing those too sent it to the outer application,
	# which recursed into its own override in process and had no credential to read remotely.
	real_app = subroutine.api.app.create_app
	built = httpx.Client

	if path == "in process":
		monkeypatch.setattr(
			subroutine.api.app,
			"create_app",
			lambda **kwargs: real_app(**kwargs) if "session_factory" in kwargs else world.application,
		)
		connection = subroutine.connections.Connection(name="local", read_only=True)

	else:
		monkeypatch.setattr(
			httpx,
			"Client",
			lambda **kwargs: built(
				**{**kwargs, "transport": api_support.SyncTransport(world.application)}
			)
			if kwargs.get("base_url") == api_support.BASE_URL
			else built(**kwargs),
		)
		monkeypatch.setenv("SUBROUTINE_TOKEN_WORK", world.secret)
		connection = subroutine.connections.Connection(
			name="work", url=api_support.BASE_URL, read_only=True
		)

	return subroutine.mcp.relay.answering(
		connection,
		subroutine.connections.Roster(connections=(connection,), default=connection.name),
		subroutine.config.Settings(dev_mode=True),
	)


def _call (
	answer: typing.Callable[[str], dict[str, typing.Any] | None], name: str, arguments: dict[str, typing.Any]
) -> dict[str, typing.Any]:
	"""Send one tool call through the relay and return its result."""

	answered = answer(
		json.dumps(
			{
				"jsonrpc": "2.0",
				"id": 1,
				"method": "tools/call",
				"params": {"name": name, "arguments": arguments},
			}
		)
	)

	assert answered is not None and "result" in answered, answered

	return typing.cast(dict[str, typing.Any], answered["result"])


@pytest.mark.parametrize("path", ["in process", "remote"])
def test_every_tool_that_writes_is_refused_through_a_read_only_relay (
	world: test_api_tasks.World, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
	"""S1 itself, as decision `#4510` asked for it to be tested: every tool not marked read-only, sent
	through the relay on a read-only connection, in process and remote, is refused, nothing is
	written, and every tool marked read-only still answers.

	**The tools are read off the session's own list**, so a tool added later that is not marked
	read-only fails here until somebody says what it is asked to do.
	"""

	ref = world.call("POST", "/v1/tasks", json={"title": "Brief the crew"}).json()["ref"]
	other = world.call("POST", "/v1/tasks", json={"title": "Find the ship"}).json()["ref"]
	answer = _relay(world, monkeypatch, path)

	listed = answer('{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')

	assert listed is not None, "the relay listed no tools"

	tools = {
		tool["name"]: bool((tool.get("annotations") or {}).get("readOnlyHint"))
		for tool in listed["result"]["tools"]
	}

	assert {name for name, reads in tools.items() if not reads} == set(WRITES), (
		"a tool that writes is not driven here, or one driven here is gone"
	)
	assert {name for name, reads in tools.items() if reads} == set(READS)

	before = _events(world)

	for name, arguments in WRITES.items():
		result = _call(answer, name, arguments(ref, other))

		assert READ_ONLY in json.dumps(result), f"{name} was not refused as read-only: {result}"

	for name, arguments in READS.items():
		result = _call(answer, name, arguments(ref, other))

		assert not result.get("isError"), f"{name} is marked read-only and was refused: {result}"

	assert _events(world) == before, "a refused tool call left an event behind"
	assert "Not written" not in _titles(world)


def test_a_relay_to_an_instance_that_does_not_know_the_flag_says_it_is_too_old (
	world: test_api_tasks.World, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The relay's half of the fail-closed design: an older ``/mcp`` refuses the flag by name, and the
	session is told the instance is too old for a read-only connection, not handed a problem
	document about a parameter nobody typed."""

	class Older(httpx.BaseTransport):
		"""An instance from before decision `#4510`: the flag is a parameter it does not accept."""

		def handle_request (self, request: httpx.Request) -> httpx.Response:
			"""Refuse the flag as unknown, as every earlier release does."""

			assert request.url.params.get("read_only") == "true", "the relay did not send the flag"

			return httpx.Response(
				422,
				headers={"content-type": "application/problem+json"},
				json=_problem("This endpoint does not accept 'read_only'."),
			)

	built = httpx.Client
	monkeypatch.setattr(httpx, "Client", lambda **kwargs: built(**{**kwargs, "transport": Older()}))
	monkeypatch.setenv("SUBROUTINE_TOKEN_WORK", world.secret)
	connection = subroutine.connections.Connection(
		name="work", url=api_support.BASE_URL, read_only=True
	)
	answered = subroutine.mcp.relay.answering(
		connection,
		subroutine.connections.Roster(connections=(connection,), default="work"),
		subroutine.config.Settings(dev_mode=True),
	)('{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')

	assert answered is not None and "error" in answered, answered
	assert "does not know the 'read_only' setting" in json.dumps(answered), answered
