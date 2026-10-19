"""An agent stopped where it is a member is told where, and whose absence stops it - `SR#5105`.

M-23 of the cold review of 2026-10-17 (`#5096`), its *What agents are told* bullet, decided in
`#5097`. **An agent acts in a workspace only while the person at the top of its chain is a member
there too** (`#4546`). One whose person had been taken out was told *You are not a member of any
workspace*, or that it could not reach the one it named, so it told its person the wrong thing to
fix. It is told which workspace, and whose absence stops it; ``/v1/me`` lists them as
``stopped_in``.
"""

import typing
import uuid

import fastapi
import pytest
import sqlalchemy.orm

import api_support
import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.domain.workspaces

users = subroutine.domain.users
workspaces = subroutine.domain.workspaces

User = subroutine.db.models.identity.User
Workspace = subroutine.db.models.identity.Workspace


class Installation:
	"""carrieanne's agent claude, in MetaCortex and the Dojo, both of which carrieanne has left.

	keanu founded MetaCortex and morpheus the Dojo; marcus founded Zion, where carrieanne and claude
	are both members, so claude still reaches Zion when ``in_zion`` is asked for.
	"""

	def __init__ (self, session: sqlalchemy.orm.Session, *, in_zion: bool = False) -> None:
		"""Make the three workspaces and their members, then take carrieanne out of two."""

		self.session = session
		self.suffix = uuid.uuid4().hex[:6]
		self.keanu = self._person("keanu")
		self.morpheus = self._person("morpheus")
		self.marcus = self._person("marcus")
		self.carrieanne = self._person("carrieanne")
		self.claude = users.create(
			session,
			username=f"claude-{self.suffix}",
			is_service_account=True,
			responsible_user_id=self.carrieanne.id,
		)
		self.metacortex = self._workspace("metacortex", self.keanu)
		self.dojo = self._workspace("dojo", self.morpheus)
		self.zion = self._workspace("zion", self.marcus)

		for workspace in (self.metacortex, self.dojo, *((self.zion,) if in_zion else ())):
			for member in (self.carrieanne, self.claude):
				workspaces.add_member(session, workspace, member, role_key="member")

		session.flush()

		for workspace in (self.metacortex, self.dojo):
			workspaces.remove_member(session, workspace, self.carrieanne)

		session.flush()

	def _person (self, name: str) -> User:
		"""Make a person with a name nobody else in the run has."""

		return users.create(self.session, username=f"{name}-{self.suffix}")

	def _workspace (self, name: str, owner: User) -> Workspace:
		"""Make a workspace with a short name nobody else in the run has."""

		return workspaces.create(
			self.session, slug=f"{name}-{self.suffix}", title=name.title(), owner=owner
		)

	def headers (self, user: User, *, pinned: Workspace | None = None) -> dict[str, str]:
		"""Return the headers presenting a fresh credential of ``user``'s, pinned if asked."""

		_row, issued = subroutine.domain.authentication.issue_token(
			self.session,
			user=user,
			title=f"{user.username}'s {uuid.uuid4().hex[:6]}",
			workspace_id=None if pinned is None else pinned.id,
		)
		self.session.flush()

		return {"Authorization": f"Bearer {issued.value.get_secret_value()}"}


def _asked (
	application: fastapi.FastAPI, headers: dict[str, str], method: str, path: str, **kwargs: typing.Any
) -> tuple[int, dict[str, typing.Any]]:
	"""Make one request, returning the status and the body."""

	answer = api_support.call(application, method, path, headers=headers, **kwargs)

	return answer.status_code, answer.json()


@pytest.mark.parametrize(
	("method", "path", "body"),
	[
		pytest.param("GET", "/v1/tasks", None, id="a listing"),
		pytest.param("POST", "/v1/tasks", {"title": "Feed the cat"}, id="a write"),
		pytest.param("GET", "/v1/tasks?workspace_id=WANTED", None, id="a workspace named"),
	],
)
def test_an_agent_that_reaches_nothing_is_told_where_it_is_stopped (
	session: sqlalchemy.orm.Session, method: str, path: str, body: dict[str, str] | None
) -> None:
	"""claude reaches no workspace, and each request says the two it is stopped in and why."""

	here = Installation(session)
	application = api_support.build_app(api_support.factory_for(session))
	status, said = _asked(
		application,
		here.headers(here.claude),
		method,
		path.replace("WANTED", here.dojo.slug),
		**({} if body is None else {"json": body}),
	)
	first, second = sorted((here.dojo.slug, here.metacortex.slug))

	assert status == 404, said
	assert said["detail"] == (
		f"You are a member of {first} and {second}, and stopped in each because "
		f"{here.carrieanne.username}, whom you answer to, is not a member of any of them."
	), said
	assert said["hint"] == f"Ask an administrator of one of them to add {here.carrieanne.username}.", said


def test_an_agent_that_reaches_another_is_told_only_of_the_one_it_named (
	session: sqlalchemy.orm.Session,
) -> None:
	"""claude still reaches Zion: naming MetaCortex says why, naming a stranger is refused as before."""

	here = Installation(session, in_zion=True)
	application = api_support.build_app(api_support.factory_for(session))
	headers = here.headers(here.claude)

	status, said = _asked(
		application, headers, "GET", f"/v1/tasks?workspace_id={here.metacortex.slug}"
	)

	assert status == 404, said
	assert said["detail"] == (
		f"You are a member of {here.metacortex.slug}, and stopped there because "
		f"{here.carrieanne.username}, whom you answer to, is not a member."
	), said
	assert said["errors"][0]["field"] == "query.workspace_id", said

	status, said = _asked(application, headers, "GET", "/v1/tasks?workspace_id=nowhere")

	assert status == 404, said
	assert "stopped" not in said["detail"], said

	status, said = _asked(application, headers, "GET", "/v1/tasks")

	assert status == 200, said


def test_who_am_i_lists_where_an_agent_is_stopped (session: sqlalchemy.orm.Session) -> None:
	"""claude's /v1/me lists both, with carrieanne; pinned to the Dojo, only the Dojo; keanu, none."""

	here = Installation(session)
	application = api_support.build_app(api_support.factory_for(session))

	def stopped (headers: dict[str, str]) -> list[dict[str, str]]:
		"""Return what /v1/me lists as stopped, for one credential."""

		status, said = _asked(application, headers, "GET", "/v1/me")

		assert status == 200, said

		return list(said["stopped_in"])

	assert stopped(here.headers(here.claude)) == [
		{"id": str(found.id), "slug": found.slug, "answers_to": here.carrieanne.username}
		for found in sorted((here.dojo, here.metacortex), key=lambda row: row.slug)
	]
	assert [one["slug"] for one in stopped(here.headers(here.claude, pinned=here.dojo))] == [
		here.dojo.slug
	]
	assert stopped(here.headers(here.keanu)) == []
