"""A misspelt name's refusal lists a workspace's members only to who may read them - `SR#5311`.

A bullet of L-1 of the cold review of 2026-11-11: a credential scoped ``task:read,task:write`` was
refused ``GET /v1/workspaces/<slug>/members`` and handed the same roster in the hint refusing an
assignee nobody holds - the listing `SR#5110` closed, through a refusal. **The roster is listed
only to a credential that may read every account on it**, and no hint is given otherwise: some of
it would read as all of it.
"""

import typing
import uuid

import pytest
import sqlalchemy.orm

import api_support
import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_tasks

World = test_api_tasks.World


def _secret (world: World, *scopes: str) -> str:
	"""Issue the world's person a credential narrowed to these verbs."""

	_row, issued = subroutine.domain.authentication.issue_token(
		world.session, user=world.user, title=f"Scoped {uuid.uuid4().hex[:4]}", scopes=list(scopes)
	)
	world.session.flush()

	return str(issued.value.get_secret_value())


def _filed (world: World, secret: str, body: dict[str, typing.Any]) -> typing.Any:
	"""File a task with ``secret``."""

	return api_support.call(
		world.application,
		"POST",
		"/v1/tasks",
		json=body,
		headers={"authorization": f"Bearer {secret}"},
	)


#: A task naming nobody, by its own field and in a captured line.
NAMING_NOBODY: list[dict[str, typing.Any]] = [
	{"title": "Fix the sign-up form", "assignee": "nobody-here"},
	{"text": "Fix the sign-up form @nobody-here"},
]


def _keanu (world: World) -> subroutine.db.models.identity.User:
	"""Seat keanu in the world's workspace beside its person."""

	keanu = subroutine.domain.users.create(world.session, username=f"keanu-{uuid.uuid4().hex[:6]}")
	subroutine.domain.workspaces.add_member(world.session, world.workspace, keanu, role_key="member")
	world.session.flush()

	return keanu


@pytest.mark.parametrize("body", NAMING_NOBODY, ids=["assignee", "captured"])
def test_a_credential_refused_the_members_is_not_given_them (
	session: sqlalchemy.orm.Session, body: dict[str, typing.Any]
) -> None:
	"""Task-only is refused the listing, and the refusal names nobody - not even the caller."""

	world = test_api_tasks._world(session)
	keanu = _keanu(world)
	secret = _secret(world, "task:read", "task:write")
	listing = api_support.call(
		world.application,
		"GET",
		f"/v1/workspaces/{world.workspace.slug}/members",
		headers={"authorization": f"Bearer {secret}"},
	)
	refused = _filed(world, secret, body)

	assert listing.status_code == 403, listing.text
	assert refused.status_code == 422, refused.text
	assert refused.json()["errors"][0].get("hint") is None, refused.text
	assert keanu.username not in refused.text, refused.text
	assert world.user.username not in refused.text, refused.text


@pytest.mark.parametrize("body", NAMING_NOBODY, ids=["assignee", "captured"])
@pytest.mark.parametrize("reading", ["workspace", "everything"])
def test_a_credential_that_reads_the_members_is_given_them (
	session: sqlalchemy.orm.Session, body: dict[str, typing.Any], reading: str
) -> None:
	"""With ``workspace:read`` added, and with a full credential, the hint lists every member."""

	world = test_api_tasks._world(session)
	keanu = _keanu(world)
	secret = (
		_secret(world, "task:read", "task:write", "workspace:read")
		if reading == "workspace"
		else world.secret
	)
	refused = _filed(world, secret, body)
	members = ", ".join(sorted([world.user.username, keanu.username]))

	assert refused.status_code == 422, refused.text
	assert refused.json()["errors"][0]["hint"] == f"Members here: {members}.", refused.text
