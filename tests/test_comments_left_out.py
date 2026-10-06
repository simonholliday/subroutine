"""``show`` prints an item without the comments its credential may not read - `#4554`, decision `#4511`.

NEW-C-1 of the verification of the cold review of 2026-10-05: ``subroutine_show`` and the
terminal's ``show`` asked for an item's comments unguarded, so a credential without
``comment:read`` - the hosting guide's own agent recipe - could not show a single item. Each now
shows the item, leaves the comments out and says so, in one sentence.
"""

import io
import typing
import uuid

import rich.console
import sqlalchemy.orm

import api_support
import subroutine.cli.personal
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.mcp.protocol
import subroutine.mcp.tools
import subroutine.permissions
import subroutine.views
import test_mcp

SAID = "Started on the staging box, then the key ran out."


def _clients (
	session: sqlalchemy.orm.Session,
) -> tuple[subroutine.clients.local.Client, subroutine.clients.local.Client, str]:
	"""Return the person's own client, one narrowed to task:read and task:write, and the workspace."""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"neo-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	secrets = []

	for title, scopes in (
		("Neo's own", []),
		("The guide's agent", [subroutine.permissions.TASK_READ, subroutine.permissions.TASK_WRITE]),
	):
		_row, issued = subroutine.domain.authentication.issue_token(
			session, user=setup.user, title=title, scopes=scopes, workspace_id=setup.workspace.id
		)
		secrets.append(issued.value.get_secret_value())

	session.flush()

	def opened (secret: str) -> subroutine.clients.local.Client:
		"""Return a local client presenting this credential."""

		return subroutine.clients.local.Client(
			subroutine.connections.Connection(name="local"),
			subroutine.config.Settings(dev_mode=True),
			session_factory=api_support.factory_for(session),
			token=secret,
		)

	return opened(secrets[0]), opened(secrets[1]), setup.workspace.slug


def _filed (owner: subroutine.clients.local.Client) -> int:
	"""File one task with one comment on it, as its owner, and return its number."""

	with owner:
		made = owner.capture(text="Fix the deploy script")
		owner.remark(ref=made.task.ref, body=SAID)

	return made.task.ref


def test_the_agent_tools_show_an_item_without_the_comments_they_may_not_read (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``subroutine_show`` answers with the item, leaves the comment out and says why."""

	owner, agent, _slug = _clients(session)
	ref = _filed(owner)

	with agent:
		server = subroutine.mcp.protocol.Server(
			subroutine.mcp.tools.catalogue(agent), name="subroutine", version="0"
		)
		text, failed = test_mcp._called(server, "subroutine_show", ref=ref)

	assert not failed, text
	assert "Fix the deploy script" in text, text
	assert subroutine.views.COMMENTS_LEFT_OUT in text, text
	assert SAID not in text, "a comment the credential may not read was shown"


def test_the_terminal_shows_an_item_without_the_comments_it_may_not_read (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``show`` gathers the item, prints the sentence, and says it in ``--json`` too."""

	owner, agent, slug = _clients(session)
	ref = _filed(owner)

	with agent:
		item = agent.task(ref=ref)

		assert item is not None

		located = subroutine.cli.personal.Located(connection="local", workspace=slug, item=item)
		gathered = subroutine.cli.personal._sections(agent, located, history=False)

	assert gathered.comments_left_out
	assert list(gathered.remarks) == []

	class Place:
		"""The two answers the renderers ask of the terminal's world."""

		def account_zone (self, connection: str | None, workspace: str | None) -> str:
			"""Read every day in UTC."""

			return "UTC"

		def address_of_located (self, found: typing.Any) -> str:
			"""Name it by its number."""

			return f"#{found.ref}"

	world = typing.cast(subroutine.cli.personal.World, Place())
	printed = io.StringIO()
	subroutine.cli.personal._render_item(
		world, located, gathered, console=rich.console.Console(file=printed, width=200)
	)

	assert subroutine.views.COMMENTS_LEFT_OUT in printed.getvalue(), printed.getvalue()
	assert subroutine.cli.personal._shown_as_json(world, located, gathered)["comments_left_out"]
