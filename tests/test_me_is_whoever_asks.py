"""Three ways an assignee was refused, each one resolved - `SR#5144`.

**Three bullets of L-7 of the cold review of 2026-10-17**: ``@me`` and an assignee of ``me`` were
refused as *nobody called 'me'*, which decision `#5097` settles as whoever is asking; an account
named with 32 hex characters could not be named, since the name parses as an id; and a captured
name that resolves to nobody was refused even where the request named its own assignee.
"""

import typing
import uuid

import pytest
import sqlalchemy.orm
import typer.testing

import subroutine.db.models.identity
import subroutine.domain.authentication
import subroutine.domain.selection
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import test_api_tasks
import test_multi_user

#: The tests' own fixtures, bound here so pytest finds them.
world = test_api_tasks.world
World = test_api_tasks.World
home = test_multi_user.home
run = test_multi_user.run

Run = typing.Callable[..., typer.testing.Result]

#: A username that parses as an id: 32 hex characters, a letter first.
HEX_NAME = "abcdef0123456789abcdef0123456789"


def test_me_assigns_to_the_person_asking (world: World) -> None:
	"""``@me`` in a captured line, and an assignee of ``me`` on a create and an update."""

	captured = world.call("POST", "/v1/tasks", json={"text": "Water the plants @me"})

	assert captured.status_code == 201, captured.text
	assert captured.json()["assignee"] == world.user.username

	made = world.call("POST", "/v1/tasks", json={"title": "Call the landlord", "assignee": "me"})

	assert made.status_code == 201, made.text
	assert made.json()["assignee"] == world.user.username

	cleared = world.call("PATCH", f"/v1/tasks/{made.json()['ref']}", json={"assignee": None})

	assert cleared.json()["assignee"] is None

	again = world.call("PATCH", f"/v1/tasks/{made.json()['ref']}", json={"assignee": "me"})

	assert again.status_code == 200, again.text
	assert again.json()["assignee"] == world.user.username


def test_me_assigns_to_the_agent_asking (world: World) -> None:
	"""For an agent, ``me`` is the agent itself, never the person it answers to (`#158`)."""

	agent = subroutine.domain.users.create(
		world.session,
		username=f"zion-{uuid.uuid4().hex[:6]}",
		is_service_account=True,
		responsible_user_id=world.user.id,
	)
	subroutine.domain.workspaces.add_member(
		world.session, workspace=world.workspace, user=agent, role_key="member"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		world.session, user=agent, title="Zion's"
	)
	world.session.flush()

	made = world.call(
		"POST",
		"/v1/tasks",
		json={"text": "Renew the certificate @me"},
		headers={"authorization": f"Bearer {issued.value.get_secret_value()}"},
	)

	assert made.status_code == 201, made.text
	assert made.json()["assignee"] == agent.username


def _hex_named (world: World) -> subroutine.db.models.identity.User:
	"""Return a member named :data:`HEX_NAME`, as one made before such names were refused."""

	held = subroutine.domain.users.create(world.session, username=f"keanu-{uuid.uuid4().hex[:6]}")
	held.username = HEX_NAME
	held.username_normalized = HEX_NAME
	subroutine.domain.workspaces.add_member(
		world.session, workspace=world.workspace, user=held, role_key="member"
	)
	world.session.flush()

	return held


def test_an_account_named_like_an_id_is_named_by_its_name (world: World) -> None:
	"""Assigned by its name, and found by it in a filter, where no account has it as an id."""

	held = _hex_named(world)

	made = world.call(
		"POST", "/v1/tasks", json={"title": "Pick up the dry cleaning", "assignee": HEX_NAME}
	)

	assert made.status_code == 201, made.text
	assert made.json()["assignee"] == HEX_NAME

	listed = world.call("GET", "/v1/tasks", params={"assignee": HEX_NAME})

	assert listed.status_code == 200, listed.text
	assert [row["ref"] for row in listed.json()["items"]] == [made.json()["ref"]]
	assert subroutine.domain.selection.user(world.session, HEX_NAME).id == held.id


def test_a_new_username_that_reads_as_an_id_is_refused (world: World) -> None:
	"""A name such as :data:`HEX_NAME` would name nobody, so no account may take it from now on."""

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.users.create(world.session, username=HEX_NAME)

	assert refused.value.errors[0].field == "username"
	assert "reads as an account's id" in refused.value.errors[0].message


def test_a_captured_name_the_request_replaces_is_not_asked_about (world: World) -> None:
	"""``@nobody`` beside an explicit assignee files with that assignee, rather than refusing."""

	made = world.call(
		"POST",
		"/v1/tasks",
		json={"text": "Water the plants @nobody", "assignee": world.user.username},
	)

	assert made.status_code == 201, made.text
	assert made.json()["assignee"] == world.user.username


def test_the_terminal_assigns_to_me_and_says_it_can (run: Run) -> None:
	"""``update --assignee me`` at a terminal, and ``@me`` when filing, hand work to whoever is
	there; ``explain capture`` and the flag's help both say so."""

	run("init", "--username", "keanu", "--workspace", "Metacortex")
	run("add", "Water the plants @me")
	run("add", "Call the landlord")
	run("update", "2", "--assignee", "me")

	theirs = run("list", "--assignee", "keanu").output

	assert "Water the plants" in theirs and "Call the landlord" in theirs, theirs
	assert "'@me' to yourself" in run("explain", "capture").output
	assert "or 'me'" in " ".join(run("update", "--help").output.split())


def test_me_without_anybody_asking_names_nobody (session: sqlalchemy.orm.Session) -> None:
	"""With no caller, ``me`` is a name like any other, and nobody holds it."""

	setup = test_api_tasks._world(session)

	with pytest.raises(subroutine.errors.ValidationError, match="nobody called 'me'"):
		subroutine.domain.users.member(session, setup.workspace.id, "me", field="assignee")
