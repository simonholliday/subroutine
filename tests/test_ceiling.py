"""The agent tools' ceiling - decision `#4520`, `SR#4563`.

**A session that arrives through the agent tools** - ``/mcp``, and ``subroutine mcp`` relaying to it
or driving it in process - is handed no secret, holds no instance verb, and holds neither
``workspace:admin`` nor ``workspace:delete``, whatever its credential allows. Everything else its
credential allows, it may do. It replaced three lists of routes ``subroutine_call_api`` refused,
which refused creating a workspace and let deleting one through, while the same credential reached
every one of them over HTTP (`#4507`, area T).
"""

import ast
import dataclasses
import json
import pathlib
import typing
import uuid

import pytest
import sqlalchemy.orm

import api_support
import subroutine.api.app
import subroutine.config
import subroutine.connections
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.bootstrap
import subroutine.domain.calendars
import subroutine.domain.sessions
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.mcp.relay
import subroutine.permissions
import test_api_mcp
import test_api_tasks

Principal = subroutine.domain.authentication.Principal

#: The program's source, which the scan reads.
SOURCE = pathlib.Path(__file__).resolve().parent.parent / "src" / "subroutine"


@dataclasses.dataclass(frozen=True)
class Place:
	"""An installation's first account, its workspace, and a feed of theirs to reset."""

	owner: subroutine.db.models.identity.User
	workspace: subroutine.db.models.identity.Workspace
	feed: subroutine.db.models.identity.CalendarFeed
	free: Principal

	@property
	def ceilinged (self) -> Principal:
		"""Return the same credential as it arrives through the agent tools."""

		return dataclasses.replace(self.free, through_the_agent_tools=True)


@pytest.fixture
def place (session: sqlalchemy.orm.Session) -> Place:
	"""An instance administrator who owns their workspace, holding a credential nothing narrows.

	**Holding everything**, so wherever the agent tools are refused, the ceiling is what refused.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"laurence-{uuid.uuid4().hex[:8]}", instance_name="MetaCortex"
	)
	token, _issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, title="Everything"
	)
	session.flush()
	free = Principal(user=setup.user, token=token)
	feed, _secret = subroutine.domain.calendars.create(
		session, free, workspace_id=setup.workspace.id, title="Agenda"
	)
	session.flush()

	return Place(owner=setup.user, workspace=setup.workspace, feed=feed, free=free)


#: Every function that hands back a secret, and how to ask it for one as a session would.
MINTS: dict[str, typing.Callable[[sqlalchemy.orm.Session, Place, Principal], object]] = {
	"subroutine.domain.authentication.issue_token": lambda session, place, actor: (
		subroutine.domain.authentication.issue_token(
			session, user=place.owner, title="Minted", actor=actor
		)
	),
	"subroutine.domain.sessions.mint_link": lambda session, place, actor: (
		subroutine.domain.sessions.mint_link(session, user=place.owner, actor=actor)
	),
	"subroutine.domain.calendars.create": lambda session, place, actor: (
		subroutine.domain.calendars.create(
			session, actor, workspace_id=place.workspace.id, title="Another agenda"
		)
	),
	"subroutine.domain.calendars.reset": lambda session, place, actor: (
		subroutine.domain.calendars.reset(session, place.feed, actor=actor, enabled=True)
	),
}

#: Functions that hand back a secret and do not ask the ceiling, each with why.
NOT_ASKED: dict[str, str] = {
	"subroutine.domain.sessions.redeem": (
		"Spends a sign-in link the caller already holds for the browser session it buys, and is "
		"presented no session to ask: the link is the credential."
	),
}


def _minting (root: pathlib.Path) -> set[str]:
	"""Return every public function under ``root`` that mints a secret, by what it calls.

	**Found rather than listed**, so a fifth way to hand one back cannot be added without this
	file being told about it: a function calling ``generate_token``, or a private helper in its
	own module that does, at any depth.
	"""

	found: set[str] = set()

	for path in sorted(root.rglob("*.py")):
		module = ".".join(("subroutine", *path.relative_to(root).with_suffix("").parts))
		tree = ast.parse(path.read_text(encoding="utf-8"))
		calls = {
			node.name: {
				called.func.attr if isinstance(called.func, ast.Attribute) else called.func.id
				for called in ast.walk(node)
				if isinstance(called, ast.Call) and isinstance(called.func, (ast.Attribute, ast.Name))
			}
			for node in tree.body
			if isinstance(node, ast.FunctionDef)
		}
		mints = {name for name, called in calls.items() if "generate_token" in called}

		# Through the module's private helpers, until nothing more is learned.
		while True:
			more = {
				name
				for name, called in calls.items()
				if name not in mints and any(helper.startswith("_") and helper in mints for helper in called)
			}

			if not more:
				break

			mints |= more

		found |= {f"{module}.{name}" for name in mints if not name.startswith("_")}

	return found


def test_every_function_that_mints_a_secret_is_asked_or_excused () -> None:
	"""Each way to hand back a secret is driven below or excused here, and no entry outlives one."""

	found = _minting(SOURCE)

	assert len(found) >= 4, f"the scan found {sorted(found)} - has it stopped reading the source?"
	assert found == set(MINTS) | set(NOT_ASKED), (
		f"unlisted: {sorted(found - set(MINTS) - set(NOT_ASKED))}; "
		f"no longer minting: {sorted(set(MINTS) | set(NOT_ASKED) - found)}"
	)
	assert not set(MINTS) & set(NOT_ASKED)


def test_the_scan_finds_a_secret_minted_through_a_private_helper (tmp_path: pathlib.Path) -> None:
	"""The scan is fed a defect through its own entry point: a public door behind a private helper."""

	(tmp_path / "domain").mkdir()
	(tmp_path / "domain" / "keys.py").write_text(
		"def _minted ():\n"
		"\treturn subroutine.auth.generate_token()\n"
		"\n"
		"def _again ():\n"
		"\treturn _minted()\n"
		"\n"
		"def handed_back ():\n"
		"\treturn _again()\n"
		"\n"
		"def unrelated ():\n"
		"\treturn 1\n",
		encoding="utf-8",
	)

	assert _minting(tmp_path) == {"subroutine.domain.keys.handed_back"}


@pytest.mark.parametrize("name", sorted(MINTS))
def test_every_function_that_mints_a_secret_refuses_the_agent_tools (
	session: sqlalchemy.orm.Session, place: Place, name: str
) -> None:
	"""A session through the agent tools is handed no secret, and the same credential elsewhere is.

	The credential holds everything, so the refusal is the ceiling's and nothing else's: the same
	call made by the same credential arriving any other way is answered.
	"""

	with pytest.raises(subroutine.errors.Forbidden) as refused:
		MINTS[name](session, place, place.ceilinged)

	assert "A session through the agent tools is never handed" in str(refused.value), refused.value
	assert "Run 'subroutine " in (refused.value.hint or ""), refused.value.hint

	MINTS[name](session, place, place.free)


def test_the_agent_tools_hold_no_instance_verb_and_no_workspace_administration (
	session: sqlalchemy.orm.Session, place: Place
) -> None:
	"""The verbs above the ceiling are refused it, reads included, and nothing else is.

	``/v1/me`` reads the same decision, so an agent asking what it may do is told the truth about
	the ceiling rather than discovering it by being refused.
	"""

	above = subroutine.domain.authorization.AuthorizationFailure.ABOVE_THE_AGENT_TOOLS

	for verb in sorted(subroutine.permissions.ABOVE_THE_AGENT_TOOLS):
		assert subroutine.domain.authorization.refusal(
			session, place.ceilinged, verb, workspace_id=place.workspace.id
		) is above, verb
		assert subroutine.domain.authorization.refusal(
			session, place.free, verb, workspace_id=place.workspace.id
		) is None, verb

	for verb in sorted(subroutine.permissions.INSTANCE_LEVEL):
		for reading in (True, False):
			assert not subroutine.domain.authorization.may_instance(
				place.ceilinged, verb, reading=reading
			), verb
			assert subroutine.domain.authorization.may_instance(place.free, verb, reading=reading), verb

	assert subroutine.domain.authorization.instance_permissions(place.ceilinged) == frozenset()

	held = subroutine.domain.authorization.explain(session, place.free, place.workspace.id)
	ceilinged = subroutine.domain.authorization.explain(session, place.ceilinged, place.workspace.id)

	assert ceilinged.permissions == held.permissions - subroutine.permissions.ABOVE_THE_AGENT_TOOLS
	assert held.permissions >= subroutine.permissions.ABOVE_THE_AGENT_TOOLS


def test_the_ceiling_is_named_only_where_nothing_else_refuses (
	session: sqlalchemy.orm.Session, place: Place
) -> None:
	"""Somebody who could not do it anywhere is told why, not sent to a terminal that refuses too.

	Intersected as a credential's scopes are, after the role and the scopes: a member, whose role
	holds no ``workspace:admin``, is told so through the agent tools, and is no superuser either.
	"""

	member = subroutine.domain.users.create(session, username=f"mouse-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(session, place.workspace, member, role_key="member")
	session.flush()
	through_the_tools = Principal(user=member, through_the_agent_tools=True)
	failure = subroutine.domain.authorization.AuthorizationFailure

	assert subroutine.domain.authorization.refusal(
		session,
		through_the_tools,
		subroutine.permissions.WORKSPACE_ADMIN,
		workspace_id=place.workspace.id,
	) is failure.ROLE_LACKS_PERMISSION

	with pytest.raises(subroutine.domain.authorization.AuthorizationError) as refused:
		subroutine.domain.authorization.authorize_instance(
			through_the_tools, subroutine.permissions.INSTANCE_USER_CREATE
		)

	assert refused.value.failure is failure.NOT_A_SUPERUSER


def _relayed (
	world: test_api_tasks.World,
	monkeypatch: pytest.MonkeyPatch,
	method: str,
	path: str,
	body: dict[str, typing.Any] | None = None,
) -> str:
	"""Make one ``subroutine_call_api`` request through ``subroutine mcp`` in process.

	**A fresh application each time one is built**, as in the program: ``call_api`` builds its own
	and overrides who it acts as, so one application handed to both turned the relay's override
	into the tools' own, which then asked itself who the caller was until the stack ran out.
	"""

	arguments: dict[str, typing.Any] = {"method": method, "path": path}

	if body is not None:
		arguments["body"] = body

	building = subroutine.api.app.create_app
	monkeypatch.setattr(
		subroutine.api.app,
		"create_app",
		lambda **kwargs: building(
			settings=subroutine.config.Settings(dev_mode=True),
			session_factory=api_support.factory_for(world.session),
		),
	)
	local = subroutine.connections.Connection(name="local")
	answered = subroutine.mcp.relay.answering(
		local,
		subroutine.connections.Roster(connections=(local,), default="local"),
		subroutine.config.Settings(dev_mode=True),
	)(
		json.dumps(
			{
				"jsonrpc": "2.0",
				"id": 1,
				"method": "tools/call",
				"params": {"name": "subroutine_call_api", "arguments": arguments},
			}
		)
	)

	assert answered is not None, "the adapter answered nothing"

	return str(answered["result"]["content"][0]["text"])


def test_every_way_into_the_agent_tools_is_held_to_the_ceiling (
	session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The instance's own ``/mcp`` and ``subroutine mcp`` in process alike, and not the HTTP API.

	Driven through ``subroutine_call_api``, which reached every route its credential allowed but
	the seven on a list: minting a credential, which the list refused; creating a workspace, which
	it refused too; and deleting one, which it let through. The same credential over HTTP mints.
	"""

	world = test_api_tasks._world(session)
	minting = ("POST", "/v1/tokens", {"title": "Minted"})
	creating = ("POST", "/v1/workspaces", {"slug": "zion", "title": "Zion"})
	deleting = ("DELETE", f"/v1/workspaces/{world.workspace.slug}", None)

	for method, path, body in (minting, creating, deleting):
		arguments: dict[str, typing.Any] = {"method": method, "path": path}

		if body is not None:
			arguments["body"] = body

		served = test_api_mcp._said(test_api_mcp._tool(world, "subroutine_call_api", **arguments))
		relayed = _relayed(world, monkeypatch, method, path, body)

		for said in (served, relayed):
			assert said.startswith("403 "), said
			assert "agent tools" in said, said

	assert world.call("POST", "/v1/tokens", json={"title": "Minted"}).status_code == 201


def test_an_administrator_through_the_agent_tools_is_still_offered_an_agent_of_their_own (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Who may make an agent is the account, not the session (Simon, decision `#4520`).

	The offer was read from the session's instance permissions, which the ceiling empties, so an
	instance administrator working through the tools was told an administrator could make one.
	The command is run at their own terminal, which the ceiling does not reach.
	"""

	world = test_api_tasks._world(session)
	said = test_api_mcp._said(test_api_mcp._tool(world, "subroutine_whoami"))

	assert f"{world.user.username} can run 'subroutine agent create <name>" in said, said
	assert "an administrator here can run" not in said, said
	assert "Over the installation" not in said, said


def test_the_terminals_own_person_is_held_to_the_ceiling_through_the_agent_tools (
	session: sqlalchemy.orm.Session, place: Place
) -> None:
	"""`subroutine mcp` on this machine acts as its person with no credential, and is held too.

	Signing somebody else out everywhere asks an instance verb, which a person at the terminal is
	asked as over HTTP (`SR#4567`, decision `#4514`), so an instance administrator there holds it -
	and through the agent tools does not, which is what the ceiling exists to stop.
	"""

	colleague = subroutine.domain.users.create(session, username=f"tank-{uuid.uuid4().hex[:8]}")
	session.flush()
	at_the_terminal = Principal(user=place.owner)

	assert at_the_terminal.is_local

	with pytest.raises(subroutine.domain.authorization.AuthorizationError) as refused:
		subroutine.domain.sessions.sign_out_everywhere(
			session,
			user=colleague,
			actor=dataclasses.replace(at_the_terminal, through_the_agent_tools=True),
		)

	assert refused.value.failure is (
		subroutine.domain.authorization.AuthorizationFailure.ABOVE_THE_AGENT_TOOLS
	)

	subroutine.domain.sessions.sign_out_everywhere(session, user=colleague, actor=at_the_terminal)
