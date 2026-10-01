"""A tag only private work uses is out of sight to anybody outside it - `#3922`, decision `#4094`.

You see a tag when something you can read carries it, or when nothing carries it: in the tag
listing on both transports, a lookup of one tag, ``/v1/meta`` and an export. A tag you can see
is the workspace's vocabulary, so renaming it reaches where you cannot see.
"""

import typing

import pytest

import api_support
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.domain.authentication
import subroutine.errors
import test_api_tasks
import test_authorization

World = test_api_tasks.World
world = test_api_tasks.world


def _outsider (world: World) -> World:
	"""Return the same installation as a member who is not in the private project."""

	colleague = test_authorization._member(world.session, world.workspace, "member")
	_row, issued = subroutine.domain.authentication.issue_token(
		world.session, user=colleague.user, title="Outside the project"
	)
	world.session.flush()

	return world._replace(secret=issued.value.get_secret_value())


def _tags (world: World) -> dict[str, dict[str, typing.Any]]:
	"""Return the tags a caller is listed, by name."""

	answer = world.call("GET", "/v1/tags?limit=200")

	assert answer.status_code == 200, answer.text

	return {item["name"]: item for item in answer.json()["items"]}


def _seeded (world: World) -> None:
	"""File a task in a private project, tagged once on its own and once beside public work."""

	made = world.call(
		"POST", "/v1/projects", json={"key": "ops", "title": "Operations", "visibility": "private"}
	)

	assert made.status_code == 201, made.text

	for body in (
		{
			"title": "Fix the leak on the third floor",
			"project": "ops",
			"tags": ["rival-bid", "deploy"],
		},
		{"title": "Fix the deploy script", "tags": ["deploy"]},
	):
		filed = world.call("POST", "/v1/tasks", json=body)

		assert filed.status_code == 201, filed.text


def test_a_tag_only_private_work_uses_is_not_listed_to_somebody_outside_it (
	world: World,
) -> None:
	"""Not in the listing, not in ``/v1/meta`` and its total, and not in an export."""

	_seeded(world)
	outsider = _outsider(world)

	assert {"rival-bid", "deploy"} <= set(_tags(world)), "its owner sees both"
	assert "rival-bid" not in _tags(outsider) and "deploy" in _tags(outsider)

	meta = outsider.call("GET", "/v1/meta").json()["tags"]
	named = {item["name"] for item in meta["items"]}

	assert "rival-bid" not in named and "deploy" in named, named
	assert meta["total"] == len(_tags(outsider)), meta

	exported = outsider.call("GET", "/v1/export/tags").json()["items"]

	assert "rival-bid" not in {item["name"] for item in exported}


def test_a_tag_out_of_sight_cannot_be_renamed_or_deleted_by_somebody_outside_it (
	world: World,
) -> None:
	"""Answered as though there were no such tag, as anything else hidden is."""

	_seeded(world)
	outsider = _outsider(world)
	hidden = _tags(world)["rival-bid"]["id"]

	assert outsider.call("PATCH", f"/v1/tags/{hidden}", json={"name": "x"}).status_code == 404
	assert outsider.call("DELETE", f"/v1/tags/{hidden}").status_code == 404
	assert "rival-bid" in _tags(world), "nothing happened to it"


def test_the_local_client_keeps_it_out_of_sight_as_the_endpoint_does (world: World) -> None:
	"""The terminal on the serving machine reads the same predicate, through the local client.

	Driven as the outsider by naming them as the local user, which is how a machine with more than
	one account chooses whose the local connection is.
	"""

	_seeded(world)
	colleague = test_authorization._member(world.session, world.workspace, "member")
	hidden = _tags(world)["rival-bid"]["id"]
	local = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True, local_user=colleague.user.username),
		session_factory=api_support.factory_for(world.session),
	)

	with local:
		listed = {row.name for row in local.tags(workspace=world.workspace.slug)}

		assert "rival-bid" not in listed and "deploy" in listed, listed

		with pytest.raises(subroutine.errors.NotFound):
			local.update_tag(which=hidden, name="x")

		with pytest.raises(subroutine.errors.NotFound):
			local.delete_tag(which=hidden)


def test_a_tag_in_sight_is_the_workspaces_to_rename_everywhere (world: World) -> None:
	"""As renaming a status reaches projects the renamer cannot see (decision `#4094`)."""

	_seeded(world)
	outsider = _outsider(world)
	shared = _tags(outsider)["deploy"]["id"]
	renamed = outsider.call("PATCH", f"/v1/tags/{shared}", json={"name": "ship"})

	assert renamed.status_code == 200, renamed.text

	inside = world.call("GET", "/v1/tasks?project=ops").json()["items"]

	assert any("ship" in item["tags"] for item in inside), inside


def test_an_owner_keeps_sight_of_a_tag_only_their_trashed_work_carries (world: World) -> None:
	"""Something they can read includes the trash, as their export does."""

	filed = world.call(
		"POST",
		"/v1/tasks",
		json={"title": "Order a new phone for reception", "tags": ["spring-clean"]},
	).json()

	assert world.call("DELETE", f"/v1/tasks/{filed['ref']}").is_success
	assert "spring-clean" in _tags(world)

	named = {item["name"] for item in world.call("GET", "/v1/meta").json()["tags"]["items"]}

	assert "spring-clean" in named, named


def test_a_credential_that_may_not_read_tasks_sees_only_the_tags_nothing_carries (
	world: World,
) -> None:
	"""Narrowed rather than refused: no tag listing asks for ``task:read``."""

	_seeded(world)

	assert world.call("POST", "/v1/tags", json={"name": "someday"}).status_code == 201

	_row, issued = subroutine.domain.authentication.issue_token(
		world.session, user=world.user, title="Projects only", scopes=["project:read"]
	)
	world.session.flush()
	projects_only = world._replace(secret=issued.value.get_secret_value())

	assert set(_tags(projects_only)) == {"someday"}
