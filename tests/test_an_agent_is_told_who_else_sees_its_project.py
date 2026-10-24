"""An agent making a private project is told who else sees it - `SR#5112`.

**L-5 of the cold review of 2026-10-17**: sight follows an agent's chain upward (decision `#4943`),
so whoever it answers to sees a private project it makes, and the agent tools and the terminal both
told it that only it could. A person is still told only themselves, which the tests beside each
surface's own answer already hold.
"""

import typing
import uuid

import pytest
import typer.testing

import api_support
import subroutine.clients.http
import subroutine.connections
import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.mcp.protocol
import subroutine.mcp.tools
import test_mcp
import test_multi_user
import test_transport_equivalence

#: The terminal's harness, and the equivalence suite's pair, bound here so pytest finds them.
home = test_multi_user.home
run = test_multi_user.run
pair = test_transport_equivalence.pair
Pair = test_transport_equivalence.Pair


def test_the_terminal_names_whom_an_agent_answers_to (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""``project create --private`` tells a person only they see it, and an agent whom it answers
	to as well."""

	run("init", "--username", "laurence", "--workspace", "Acme")
	run("user", "create", "bot", "--agent", "--workspace", "acme")

	mine = run("project", "create", "dojo", "Dojo", "--private").output

	assert "Only you can see it." in mine, mine

	monkeypatch.setenv("SUBROUTINE_LOCAL_USER", "bot")
	theirs = run("project", "create", "zion", "Zion", "--private").output

	assert "Only you and laurence, whom you answer to, can see it." in theirs, theirs


def test_the_agent_tools_name_everybody_above_a_sub_agent (pair: Pair) -> None:
	"""An agent made by an agent is told both: the one it answers to, and the person above."""

	claude = subroutine.domain.users.create(
		pair.session,
		username=f"claude-{uuid.uuid4().hex[:6]}",
		is_service_account=True,
		responsible_user_id=pair.user.id,
	)
	scout = subroutine.domain.users.create(
		pair.session,
		username=f"scout-{uuid.uuid4().hex[:6]}",
		is_service_account=True,
		responsible_user_id=claude.id,
	)

	for agent in (claude, scout):
		subroutine.domain.workspaces.add_member(
			pair.session, workspace=pair.workspace, user=agent, role_key="member"
		)

	_row, issued = subroutine.domain.authentication.issue_token(
		pair.session, user=scout, title="Scout's"
	)
	pair.session.flush()

	client = subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url="https://tasks.example.com"),
		token=issued.value.get_secret_value(),
		transport=api_support.SyncTransport(
			api_support.build_app(api_support.factory_for(pair.session))
		),
		base_url=api_support.BASE_URL,
	)

	with client:
		server = subroutine.mcp.protocol.Server(
			subroutine.mcp.tools.catalogue(client), name="subroutine", version="0"
		)
		made, failed = test_mcp._called(
			server, "subroutine_project", key="zion", title="Zion", private=True
		)

	assert not failed, made
	assert (
		f"Only you, {claude.username} and {pair.user.username}, whom you answer to, can see it."
		in made
	), made
