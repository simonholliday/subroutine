"""A credential narrowed to some projects administers nothing beyond them, over HTTP - `SR#3812`.

Decision `#3802`, driven the way the cold reviews measured it (`#3744`, and M-4 of `#3883`): each
refusal here answered 200 or 201 on both backends, to a credential its owner had narrowed to one
project. ``tests/test_authorization.py`` holds the decision itself; this is every route that asks
it, the local and HTTP clients' shared door, and ``/v1/me`` saying the same.

The world's person is the installation's first account, so an owner of the workspace *and* a
superuser: one narrowed credential of theirs reaches both tiers the decision closes.
"""

import typing
import uuid

import pytest
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.permissions
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def _narrowed (world: test_api_tasks.World) -> tuple[dict[str, str], str]:
	"""Return headers presenting the world's owner narrowed to a project of theirs, and its key."""

	made = world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})

	assert made.status_code == 201, made.text

	_row, issued = subroutine.domain.authentication.issue_token(
		world.session,
		user=world.user,
		title="Narrowed to web",
		project_scope=[made.json()["id"]],
	)
	world.session.flush()

	return {"authorization": f"Bearer {issued.value.get_secret_value()}"}, made.json()["key"]


def _somebody (session: sqlalchemy.orm.Session) -> subroutine.db.models.identity.User:
	"""Return a second account, a stranger to everything the narrowed credential was issued for."""

	return subroutine.domain.users.create(session, username=f"keanu-{uuid.uuid4().hex[:6]}")


def _refused (response: typing.Any) -> None:
	"""Assert one refusal, by the decision's own sentence rather than by the status alone."""

	assert response.status_code == 403, response.text
	assert "narrowed to" in response.json()["detail"], response.text


def test_a_narrowed_credential_cannot_change_the_workspace_or_point_its_osc (
	world: test_api_tasks.World,
) -> None:
	"""`#3758` and M-4 (c) and (d): renamed, re-pointed and trashed from one project.

	OSC is a workspace setting, so the one check reaches it: a destination set this way sent the
	title of everything filed in every other project to wherever it named.
	"""

	narrowed, _key = _narrowed(world)
	workspace = f"/v1/workspaces/{world.workspace.slug}"
	osc = {"settings": {"osc.send_to": "studio.local:9000"}}

	_refused(world.call("PATCH", workspace, json=osc, headers=narrowed))
	_refused(world.call("PATCH", workspace, json={"title": "Taken"}, headers=narrowed))
	_refused(world.call("DELETE", workspace, headers=narrowed))

	assert world.call("GET", workspace).json()["title"] != "Taken"
	assert world.call("PATCH", workspace, json=osc).status_code == 200, (
		"the credential it was narrowed from may, or the refusals prove nothing"
	)


def test_a_narrowed_credential_cannot_change_who_belongs (world: test_api_tasks.World) -> None:
	"""M-4 (c): an administrator added from one project, whose own credential reads every one."""

	narrowed, _key = _narrowed(world)
	members = f"/v1/workspaces/{world.workspace.slug}/members"
	joining = {"username": _somebody(world.session).username, "role": "admin"}

	_refused(world.call("POST", members, json=joining, headers=narrowed))

	assert world.call("POST", members, json=joining).status_code == 201


def test_a_narrowed_superuser_credential_administers_nothing_on_the_installation (
	world: test_api_tasks.World,
) -> None:
	"""M-4 (a) and NEW-2 of `#3884`: accounts, other people's access, and every workspace.

	The instance tier asked about the superuser and the token's scopes and nothing about its
	projects, so this listed every workspace on the installation, made an account, revoked a
	colleague's credential and signed them out of every browser.
	"""

	narrowed, _key = _narrowed(world)
	colleague = _somebody(world.session)
	their_token, _issued = subroutine.domain.authentication.issue_token(
		world.session, user=colleague, title="Their own"
	)
	world.session.flush()

	_refused(world.call("GET", "/v1/instance/workspaces", headers=narrowed))
	_refused(
		world.call("POST", "/v1/users", json={"username": "carrie-anne"}, headers=narrowed)
	)
	_refused(world.call("POST", f"/v1/users/{colleague.username}/signout", headers=narrowed))

	# **Not even found**, which is how the route answers anybody who may not administer other
	# people's credentials: the listing and the lookup are narrowed the way revoking is.
	listed = world.call("GET", "/v1/tokens", headers=narrowed).json()["items"]
	revoking = world.call("DELETE", f"/v1/tokens/{their_token.token_prefix}", headers=narrowed)

	assert their_token.token_prefix not in {row["prefix"] for row in listed}
	assert revoking.status_code == 404, revoking.text
	assert their_token.revoked_at is None

	everything = world.call("GET", "/v1/tokens").json()["items"]

	assert their_token.token_prefix in {row["prefix"] for row in everything}, (
		"the credential it was narrowed from lists it, or the absence above proves nothing"
	)
	assert world.call("GET", "/v1/instance/workspaces").status_code == 200


def test_me_offers_a_narrowed_credential_only_what_it_may_do (
	world: test_api_tasks.World,
) -> None:
	"""`/v1/me` reads the same decision, so it stops offering what the routes above refuse."""

	narrowed, _key = _narrowed(world)
	me = world.call("GET", "/v1/me", headers=narrowed).json()
	(access,) = [row for row in me["workspaces"] if row["slug"] == world.workspace.slug]

	assert me["instance_permissions"] == []
	assert not set(access["permissions"]) & subroutine.permissions.WORKSPACE_WIDE, access
	assert access["narrowed_by_credential"]


def test_a_narrowed_credential_still_works_in_its_own_project (
	world: test_api_tasks.World,
) -> None:
	"""Filing, tagging with a new tag and renaming its project are unchanged (decision `#3802`)."""

	narrowed, key = _narrowed(world)

	filed = world.call(
		"POST", "/v1/tasks", json={"text": f"Fix the header #layout +{key}"}, headers=narrowed
	)

	assert filed.status_code == 201, filed.text
	assert filed.json()["tags"] == ["layout"]

	renamed = world.call(
		"PATCH", f"/v1/projects/{key}", json={"title": "The website"}, headers=narrowed
	)

	assert renamed.status_code == 200, renamed.text
