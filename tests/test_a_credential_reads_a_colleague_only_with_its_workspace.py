"""A credential lists the accounts of its workspaces only where it reads the workspace - `SR#5110`.

**The review's Open Question 10(a), found a defect against docs/design.md §7.1 in its verification
and fixed as `#5097` says**: ``workspace:read`` gates a workspace's members, and a credential scoped
to ``task:read`` alone was refused ``GET /v1/workspaces/<slug>/members`` while ``GET /v1/users``
listed the same accounts. Measured over HTTP, as the review measured it.
"""

import typing
import uuid

import sqlalchemy.orm

import api_support
import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_tasks


def _seen (world: test_api_tasks.World, secret: str) -> typing.Callable[[str], typing.Any]:
	"""Return a reader making requests with ``secret``."""

	def read (path: str) -> typing.Any:
		"""Make one request as that credential."""

		return api_support.call(
			world.application, "GET", path, headers={"authorization": f"Bearer {secret}"}
		)

	return read


def test_a_colleague_is_read_only_with_the_workspace (session: sqlalchemy.orm.Session) -> None:
	"""Task-only lists itself and its chain and is told a co-member is out of reach; adding
	``workspace:read`` lists the co-member; the accounts administrator's listing is unchanged."""

	world = test_api_tasks._world(session)
	keanu = subroutine.domain.users.create(session, username=f"keanu-{uuid.uuid4().hex[:6]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, keanu, role_key="member")
	session.flush()

	def secret (*scopes: str) -> str:
		"""Issue the world's person a credential narrowed to these verbs."""

		_row, issued = subroutine.domain.authentication.issue_token(
			session, user=world.user, title=f"Scoped {uuid.uuid4().hex[:4]}", scopes=list(scopes)
		)
		session.flush()

		return str(issued.value.get_secret_value())

	tasks = _seen(world, secret("task:read"))
	listed = tasks("/v1/users")

	assert listed.status_code == 200, listed.text
	assert [one["username"] for one in listed.json()["items"]] == [world.user.username]

	read = tasks(f"/v1/users/{keanu.username}")

	assert read.status_code == 404, read.text
	assert read.json()["detail"] == f"There is no account called {keanu.username!r} that you can reach."
	assert tasks(f"/v1/workspaces/{world.workspace.slug}/members").status_code == 403

	workspace = _seen(world, secret("task:read", "workspace:read"))
	names = {one["username"] for one in workspace("/v1/users").json()["items"]}

	assert names == {world.user.username, keanu.username}, names
	assert workspace(f"/v1/users/{keanu.username}").status_code == 200

	administering = _seen(world, world.secret)

	assert keanu.username in {one["username"] for one in administering("/v1/users").json()["items"]}
