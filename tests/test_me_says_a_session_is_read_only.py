"""``/v1/me`` says when a session is read-only, and offers a write only where it writes - `SR#5126`.

The *Under-reported permissions* bullet of M-23 of the cold review of 2026-10-17, and decision
`#5097`'s rule that ``/v1/me`` says when a session is read-only. A read-only superuser read every
workspace and backup while ``/v1/me`` said it held no permission over the installation, and nothing
said the session was read-only, so an agent behind such a connection learned it from its refusals.
And a credential writing in a project of one workspace was offered its write verbs in another.
"""

import typing
import uuid

import pytest
import sqlalchemy.orm

import api_support
import subroutine.api.security
import subroutine.clients.http
import subroutine.connections
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.projects
import subroutine.domain.sessions
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.mcp.tools
import subroutine.permissions


class Installation:
	"""MetaCortex, laurence's, with ``web``; Zion, marcus's, with ``ops``; hugo is in both."""

	def __init__ (self, session: sqlalchemy.orm.Session) -> None:
		"""Make both workspaces and their accounts through the domain."""

		self.session = session
		suffix = uuid.uuid4().hex[:6]
		setup = subroutine.domain.bootstrap.initialise(
			session, username=f"laurence-{suffix}", instance_name="MetaCortex"
		)
		self.laurence = setup.user
		self.metacortex = setup.workspace
		self.hugo = subroutine.domain.users.create(session, username=f"hugo-{suffix}")
		marcus = subroutine.domain.users.create(session, username=f"marcus-{suffix}")
		self.zion = subroutine.domain.workspaces.create(
			session, slug=f"zion-{suffix}", title="Zion", owner=marcus
		)

		for workspace in (self.metacortex, self.zion):
			subroutine.domain.workspaces.add_member(session, workspace, self.hugo, role_key="member")

		self.web = subroutine.domain.projects.create(
			session, workspace_id=self.metacortex.id, key="web", title="Web"
		)
		self.ops = subroutine.domain.projects.create(
			session, workspace_id=self.zion.id, key="ops", title="Ops"
		)
		session.flush()
		self.application = api_support.build_app(api_support.factory_for(session))

	def secret (self, user: typing.Any, **narrowing: typing.Any) -> str:
		"""Issue ``user`` a credential, narrowed as given, and return its secret."""

		_row, issued = subroutine.domain.authentication.issue_token(
			self.session, user=user, title=f"{user.username}'s {uuid.uuid4().hex[:6]}", **narrowing
		)
		self.session.flush()

		return issued.value.get_secret_value()

	def client (self, secret: str, *, read_only: bool) -> subroutine.clients.http.Client:
		"""Return the real HTTP client a connection builds, read-only or not."""

		return subroutine.clients.http.Client(
			subroutine.connections.Connection(name="work", url="https://example.com", read_only=read_only),
			token=secret,
			transport=api_support.SyncTransport(self.application),
			base_url=api_support.BASE_URL,
		)


@pytest.mark.parametrize("read_only", [True, False], ids=["read-only", "ordinary"])
def test_me_says_whether_a_session_only_reads_and_what_it_reads_with (
	session: sqlalchemy.orm.Session, read_only: bool
) -> None:
	"""laurence, a superuser: the flag says which, and the installation's permissions are listed both
	ways, since its verbs gate its listings too; the agent tools' ``whoami`` says it in a line, and the
	terminal's is driven in ``tests/test_cli_connections.py``."""

	here = Installation(session)

	with here.client(here.secret(here.laurence), read_only=read_only) as client:
		me = client.me()
		said = subroutine.mcp.tools._whoami(client)

	assert me.credential is not None
	assert me.credential.read_only is read_only
	assert set(me.instance_permissions) == set(subroutine.permissions.INSTANCE_LEVEL)

	assert (subroutine.domain.authentication.READ_ONLY in said) is read_only, said


@pytest.mark.parametrize("read_only", [True, False], ids=["read-only", "ordinary"])
def test_a_browser_session_says_whether_it_only_reads (
	session: sqlalchemy.orm.Session, read_only: bool
) -> None:
	"""A signed-in browser is described as a credential too, and ``read_only=true`` narrows it alike."""

	here = Installation(session)
	_link, secret = subroutine.domain.sessions.mint_link(session, user=here.hugo)
	_opened, cookie = subroutine.domain.sessions.redeem(session, secret)
	session.flush()

	answer = api_support.call(
		here.application,
		"GET",
		"/v1/me",
		params={"read_only": "true"} if read_only else {},
		cookies={subroutine.api.security.SESSION_COOKIE: cookie},
	)

	assert answer.status_code == 200, answer.text
	assert answer.json()["credential"]["kind"] == "web_session"
	assert answer.json()["credential"]["read_only"] is read_only


def test_a_credential_writing_in_one_workspace_is_offered_no_write_in_another (
	session: sqlalchemy.orm.Session,
) -> None:
	"""hugo's credential reads ``web`` and ``ops`` and writes only in ``web``: Zion offers no write."""

	here = Installation(session)
	secret = here.secret(
		here.hugo,
		project_scope=[str(here.web.id), str(here.ops.id)],
		project_write_scope=[str(here.web.id)],
	)
	answer = api_support.call(
		here.application, "GET", "/v1/me", headers={"Authorization": f"Bearer {secret}"}
	)

	assert answer.status_code == 200, answer.text

	offered = {row["slug"]: set(row["permissions"]) for row in answer.json()["workspaces"]}
	writes = {subroutine.permissions.TASK_WRITE, subroutine.permissions.COMMENT_WRITE}

	assert writes <= offered[here.metacortex.slug], offered
	assert subroutine.permissions.TASK_READ in offered[here.zion.slug], offered
	assert not writes & offered[here.zion.slug], offered
