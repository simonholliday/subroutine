"""An installation administrator restores a workspace they may delete - `SR#5108`.

M-23 of the cold review of 2026-10-17 (`#5096`), its *Restore after delete* bullet, decided in
`#5097`: **section 7.3's one clause lets an installation administrator delete any live workspace,
member or not, and now names restoring it too.** The trash was searched among the caller's own
memberships, so a superuser who deleted a workspace they were not in could not bring it back, and one
whose owners had left stayed in the trash.
"""

import uuid

import fastapi
import pytest
import sqlalchemy.orm

import api_support
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.domain.workspaces

users = subroutine.domain.users
workspaces = subroutine.domain.workspaces

User = subroutine.db.models.identity.User


class Installation:
	"""MetaCortex, which keanu founded; Zion, which marcus did; and laurence, who runs the instance.

	laurence is a member of Zion only, so a credential of his pinned there holds nothing over the
	installation.
	"""

	def __init__ (self, session: sqlalchemy.orm.Session) -> None:
		"""Make the two workspaces and the three accounts, through the domain."""

		self.session = session
		suffix = uuid.uuid4().hex[:6]
		self.keanu = users.create(session, username=f"keanu-{suffix}")
		self.marcus = users.create(session, username=f"marcus-{suffix}")
		self.laurence = users.create(session, username=f"laurence-{suffix}", is_superuser=True)
		self.metacortex = workspaces.create(
			session, slug=f"metacortex-{suffix}", title="Metacortex", owner=self.keanu
		)
		self.zion = workspaces.create(
			session, slug=f"zion-{suffix}", title="Zion", owner=self.marcus
		)
		workspaces.add_member(session, self.zion, self.laurence, role_key="member")
		session.flush()

	def headers (self, user: User, *, pinned: bool = False) -> dict[str, str]:
		"""Return the headers presenting a fresh credential of ``user``'s, pinned to Zion if asked."""

		_row, issued = subroutine.domain.authentication.issue_token(
			self.session,
			user=user,
			title=f"{user.username}'s {uuid.uuid4().hex[:6]}",
			workspace_id=self.zion.id if pinned else None,
		)
		self.session.flush()

		return {"Authorization": f"Bearer {issued.value.get_secret_value()}"}


def _asked (
	application: fastapi.FastAPI, headers: dict[str, str], method: str, path: str
) -> tuple[int, str]:
	"""Make one request, returning the status and the body."""

	answer = api_support.call(application, method, path, headers=headers)

	return answer.status_code, answer.text


@pytest.mark.parametrize("by", ["slug", "id"])
def test_an_administrator_outside_a_workspace_restores_what_they_deleted (
	session: sqlalchemy.orm.Session, by: str
) -> None:
	"""laurence deletes MetaCortex, which he is not in, and puts it back, named either way."""

	here = Installation(session)
	application = api_support.build_app(api_support.factory_for(session))
	headers = here.headers(here.laurence)
	named = here.metacortex.slug if by == "slug" else str(here.metacortex.id)

	status, said = _asked(application, headers, "DELETE", f"/v1/workspaces/{named}")

	assert status == 200, said

	status, said = _asked(application, headers, "POST", f"/v1/workspaces/{named}/restore")

	assert status == 200, said

	session.refresh(here.metacortex)

	assert here.metacortex.deleted_at is None


def test_the_terminal_restores_it_the_same_way (session: sqlalchemy.orm.Session) -> None:
	"""laurence's terminal, on the database itself, deletes and restores MetaCortex as the route does."""

	here = Installation(session)
	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True, local_user=here.laurence.username),
		session_factory=api_support.factory_for(session),
	)

	with client:
		client.delete_workspace(here.metacortex.slug)
		back = client.restore_workspace(here.metacortex.slug)

	assert back.id == here.metacortex.id, back


@pytest.mark.parametrize("who", ["pinned", "marcus"], ids=["pinned elsewhere", "another workspace's owner"])
def test_nobody_else_outside_it_finds_it_in_the_trash (
	session: sqlalchemy.orm.Session, who: str
) -> None:
	"""laurence's credential pinned to Zion, and marcus, are told there is no such workspace."""

	here = Installation(session)
	application = api_support.build_app(api_support.factory_for(session))
	workspaces.delete(session, here.metacortex)
	session.flush()
	headers = (
		here.headers(here.laurence, pinned=True) if who == "pinned" else here.headers(here.marcus)
	)

	status, said = _asked(
		application, headers, "POST", f"/v1/workspaces/{here.metacortex.slug}/restore"
	)

	assert status == 404, said
	assert "or an administrator of this instance, can restore one" in said, said

	session.refresh(here.metacortex)

	assert here.metacortex.deleted_at is not None
