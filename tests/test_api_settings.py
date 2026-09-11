"""What a settings page reads: every setting on one workspace or project, and where it came from.

Design `#2110` §4, built by `#2450`. A page has to tell *this project chose teal* from *this
project inherits teal* — they render differently and clear differently, and neither the raw
``settings`` map nor a resolved value can say which. So each setting is answered with its value,
its default, whether it is set here, and the entity it was inherited from.
"""

import typing

import pytest
import sqlalchemy.orm

import subroutine.domain.authentication
import subroutine.domain.settings
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def _by_key (answer: typing.Any) -> dict[str, typing.Any]:
	"""A settings read's answer, by setting key."""

	assert answer.status_code == 200, answer.text

	return {one["key"]: one for one in answer.json()["settings"]}


def _chain (world: test_api_tasks.World) -> str:
	"""Make ``parent`` and ``parent/child``, colour the parent and hide a status on the workspace.

	Returns the workspace's short name, which every address below needs.
	"""

	slug = world.workspace.slug

	for body in (
		{"key": "parent", "title": "The parent", "workspace_id": slug},
		{"key": "child", "title": "The child", "workspace_id": slug, "parent": "parent"},
	):
		made = world.call("POST", "/v1/projects", json=body)

		assert made.status_code == 201, made.text

	coloured = world.call(
		"PATCH",
		f"/v1/projects/parent?workspace_id={slug}",
		json={"settings": {"appearance.colour": "teal"}},
	)
	hidden = world.call(
		"PATCH", f"/v1/workspaces/{slug}", json={"settings": {"statuses.hidden": ["blocked"]}}
	)

	assert coloured.status_code == 200, coloured.text
	assert hidden.status_code == 200, hidden.text

	return slug


def test_a_workspace_says_which_settings_it_states_and_which_are_the_default (
	world: test_api_tasks.World,
) -> None:
	"""A workspace is the widest scope, so each setting is stated there or is the default."""

	slug = world.workspace.slug
	before = _by_key(world.call("GET", f"/v1/workspaces/{slug}/settings"))

	assert set(before) == set(
		subroutine.domain.settings.offered(subroutine.domain.settings.WORKSPACE)
	)
	assert before["statuses.hidden"] == {
		"key": "statuses.hidden", "value": [], "default": [], "set_here": False,
		"inherited_from": None,
	}

	world.call(
		"PATCH", f"/v1/workspaces/{slug}", json={"settings": {"statuses.hidden": ["blocked"]}}
	)
	after = _by_key(world.call("GET", f"/v1/workspaces/{slug}/settings"))

	assert after["statuses.hidden"]["value"] == ["blocked"]
	assert after["statuses.hidden"]["set_here"] is True
	assert after["statuses.hidden"]["inherited_from"] is None


def test_a_project_says_where_each_value_came_from (world: test_api_tasks.World) -> None:
	"""Set here, inherited from a named ancestor, inherited from the workspace — `#2110` §4.

	**Read through the nested address**, ``parent/child/settings``, because that is the case the
	route is registered ahead of ``/{id_or_key:path}`` for: read the other way round, the path
	converter would take ``child/settings`` as part of a project's address.
	"""

	slug = _chain(world)
	child = _by_key(world.call("GET", f"/v1/projects/parent/child/settings?workspace_id={slug}"))

	assert child["appearance.colour"]["value"] == "teal"
	assert child["appearance.colour"]["set_here"] is False
	assert child["appearance.colour"]["inherited_from"] == {
		"scope": "project", "address": "parent", "title": "The parent",
	}

	assert child["statuses.hidden"]["value"] == ["blocked"]
	assert child["statuses.hidden"]["set_here"] is False
	assert child["statuses.hidden"]["inherited_from"]["scope"] == "workspace"
	assert child["statuses.hidden"]["inherited_from"]["address"] == slug

	parent = _by_key(world.call("GET", f"/v1/projects/parent/settings?workspace_id={slug}"))

	assert parent["appearance.colour"]["set_here"] is True
	assert parent["appearance.colour"]["inherited_from"] is None


def test_clearing_a_value_set_here_puts_the_inherited_one_back (
	world: test_api_tasks.World,
) -> None:
	"""The reason provenance is needed at all: clearing is an act only where the value is set.

	A child that chose its own colour and then clears it reads as its parent's again — named as
	the parent's, which is what tells a page the control is now *override*, not *clear*.
	"""

	slug = _chain(world)
	child = f"/v1/projects/parent/child?workspace_id={slug}"
	read = f"/v1/projects/parent/child/settings?workspace_id={slug}"

	world.call("PATCH", child, json={"settings": {"appearance.colour": "amber"}})
	own = _by_key(world.call("GET", read))["appearance.colour"]

	assert own["value"] == "amber"
	assert own["set_here"] is True
	assert own["inherited_from"] is None

	world.call("PATCH", child, json={"settings": {"appearance.colour": None}})
	back = _by_key(world.call("GET", read))["appearance.colour"]

	assert back["value"] == "teal"
	assert back["set_here"] is False
	assert back["inherited_from"]["address"] == "parent"


def test_nothing_stated_anywhere_reads_as_the_default (world: test_api_tasks.World) -> None:
	"""The fourth answer: not set here and inherited from nobody, so the default applies.

	**Told apart from *set here* by ``set_here`` and from *inherited* by ``inherited_from``**,
	both being empty only here — a page offers the default rather than naming a source.
	"""

	slug = world.workspace.slug
	made = world.call("POST", "/v1/projects", json={"key": "lone", "title": "Lone", "workspace_id": slug})

	assert made.status_code == 201, made.text

	lone = _by_key(world.call("GET", f"/v1/projects/lone/settings?workspace_id={slug}"))

	assert lone["appearance.colour"] == {
		"key": "appearance.colour", "value": None, "default": None, "set_here": False,
		"inherited_from": None,
	}


def test_the_settings_read_and_every_listing_agree_about_inheritance (
	world: test_api_tasks.World,
) -> None:
	"""`#2110` §4's obligation: *in force* has two readers now, so it must be one implementation.

	**Driven through both readers rather than read off the code.** A project's
	``hidden_statuses`` and a task's ``project_colour`` are what every board and listing draws;
	this read is what a settings page draws. Resolving inheritance two ways would let a page say
	one thing beside a board saying another, each consistent with itself, and nothing else
	would notice.
	"""

	slug = _chain(world)
	read = _by_key(world.call("GET", f"/v1/projects/parent/child/settings?workspace_id={slug}"))

	project = world.call("GET", f"/v1/projects/parent/child?workspace_id={slug}")

	assert project.status_code == 200, project.text
	assert project.json()["hidden_statuses"] == read["statuses.hidden"]["value"]

	task = world.call(
		"POST",
		"/v1/tasks",
		json={"text": "Something in the child", "project": "parent/child", "workspace_id": slug},
	)

	assert task.status_code == 201, task.text
	assert task.json()["project_colour"] == read["appearance.colour"]["value"] == "teal"


def test_reading_what_is_in_force_needs_the_read_verb_of_what_is_read (
	session: sqlalchemy.orm.Session, world: test_api_tasks.World
) -> None:
	"""Both reads ask the domain, which asks for ``workspace:read`` or ``project:read``.

	**The owner's own credential reads both too**, so what is refused is the verb and not the
	route: a narrowed credential that could see neither page would pass a check written only
	for the refusal.
	"""

	slug = world.workspace.slug
	world.call("POST", "/v1/projects", json={"key": "alpha", "title": "Alpha", "workspace_id": slug})

	_row, narrow = subroutine.domain.authentication.issue_token(
		session, user=world.user, title="Tasks only", scopes=["task:read"]
	)
	session.flush()
	bearer = {"authorization": f"Bearer {narrow.value.get_secret_value()}"}

	for path in (f"/v1/workspaces/{slug}/settings", f"/v1/projects/alpha/settings?workspace_id={slug}"):
		refused = world.call("GET", path, headers=bearer)

		assert refused.status_code == 403, (path, refused.status_code, refused.text[:200])
		assert world.call("GET", path).status_code == 200, path
