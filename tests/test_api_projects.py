"""Projects over HTTP.

The tree is what makes projects different from a flat list, so most of what is worth
testing here is about the tree: that a listing reads as one, that a move takes the subtree,
and that privacy inherits down it.
"""

import uuid

import pytest
import sqlalchemy.orm

import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def test_a_project_is_created_and_owned_by_its_creator (
	world: test_api_tasks.World,
) -> None:
	"""Ownership is the default because it is what makes a private project visible."""

	response = world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})

	assert response.status_code == 201

	body = response.json()

	assert body["key"] == "WEB", "keys are stored upper-cased"
	assert body["owner_id"] == str(world.user.id)
	assert body["visibility"] == "public"
	assert body["depth"] == 0


def test_a_project_is_readable_by_key_in_any_case (world: test_api_tasks.World) -> None:
	"""A key is what people have in front of them; requiring an id would be a round trip."""

	world.call("POST", "/v1/projects", json={"key": "WEB", "title": "Website"})

	assert world.call("GET", "/v1/projects/WEB").json()["key"] == "WEB"
	assert world.call("GET", "/v1/projects/web").json()["key"] == "WEB"


def test_a_reserved_key_is_refused_before_it_becomes_unreachable (
	world: test_api_tasks.World,
) -> None:
	"""A project keyed 'search' would share an address with an endpoint (SPEC.md §8.1)."""

	response = world.call("POST", "/v1/projects", json={"key": "search", "title": "Nope"})

	assert response.status_code == 422
	assert "reserved" in response.json()["errors"][0]["message"]


def test_a_project_listing_reads_as_the_tree_it_is (world: test_api_tasks.World) -> None:
	"""Ordered by path, so a parent is immediately followed by its children."""

	world.call("POST", "/v1/projects", json={"key": "WEB", "title": "Website"})
	world.call("POST", "/v1/projects", json={"key": "API", "title": "API", "parent": "WEB"})
	world.call("POST", "/v1/projects", json={"key": "ZED", "title": "Zed"})

	listed = world.call("GET", "/v1/projects").json()["items"]
	positions = {item["key"]: index for index, item in enumerate(listed)}

	assert positions["API"] == positions["WEB"] + 1, "a child follows its parent"
	assert listed[positions["API"]]["depth"] == 1


def test_a_listing_can_be_narrowed_to_one_parent (world: test_api_tasks.World) -> None:
	"""The children of one project, without walking the whole tree."""

	world.call("POST", "/v1/projects", json={"key": "WEB", "title": "Website"})
	world.call("POST", "/v1/projects", json={"key": "API", "title": "API", "parent": "WEB"})

	children = world.call("GET", "/v1/projects?parent=WEB").json()["items"]

	assert [item["key"] for item in children] == ["API"]


def test_a_project_can_be_rekeyed_and_the_old_address_stops_working (
	world: test_api_tasks.World,
) -> None:
	"""`#176`. This test asserted the opposite, on a reason that had been false for days.

	Its name was ``..._but_never_rekeyed`` and its docstring said "the key is the first half of
	every ref the project has minted" — one of four places saying so, all of them wrong since
	§6.2 made a ref a bare workspace-scoped integer on 2026-07-29.

	The half worth keeping is the last assertion. There is deliberately **no alias**: the old
	address 404s rather than redirecting, because a redirect is a rename nobody notices.
	"""

	world.call("POST", "/v1/projects", json={"key": "WEB", "title": "Website"})

	changed = world.call("PATCH", "/v1/projects/WEB", json={"title": "The Website"})

	assert changed.status_code == 200
	assert changed.json()["title"] == "The Website"

	renamed = world.call("PATCH", "/v1/projects/WEB", json={"key": "SITE"})

	assert renamed.status_code == 200
	assert renamed.json()["key"] == "SITE"

	assert world.call("GET", "/v1/projects/WEB").status_code == 404
	assert world.call("GET", "/v1/projects/SITE").status_code == 200


def test_a_rename_is_refused_when_the_new_key_could_not_have_been_chosen (
	world: test_api_tasks.World,
) -> None:
	"""Renaming applies creation's rules, or it is a way round them.

	A reserved word, a shape the pattern refuses, and a key another project already holds are
	all refused at creation; a rename that skipped any of them could arrive at a project
	nobody could have made.
	"""

	world.call("POST", "/v1/projects", json={"key": "WEB", "title": "Website"})
	world.call("POST", "/v1/projects", json={"key": "API", "title": "The API"})

	assert world.call("PATCH", "/v1/projects/WEB", json={"key": "API"}).status_code == 409
	assert world.call("PATCH", "/v1/projects/WEB", json={"key": "9NO"}).status_code == 422


def test_an_omitted_field_is_untouched_and_a_null_one_is_cleared (
	world: test_api_tasks.World,
) -> None:
	"""SPEC.md §8.3 applies here exactly as it does to tasks."""

	world.call(
		"POST", "/v1/projects", json={"key": "WEB", "title": "Website", "description": "Notes"}
	)

	renamed = world.call("PATCH", "/v1/projects/WEB", json={"title": "Site"}).json()

	assert renamed["description"] == "Notes", "an omitted field must be left alone"

	cleared = world.call("PATCH", "/v1/projects/WEB", json={"description": None}).json()

	assert cleared["description"] is None
	assert cleared["title"] == "Site"


def test_moving_a_project_takes_its_subtree (world: test_api_tasks.World) -> None:
	"""The materialised path is rewritten for every descendant, not just the node moved."""

	world.call("POST", "/v1/projects", json={"key": "WEB", "title": "Website"})
	world.call("POST", "/v1/projects", json={"key": "API", "title": "API", "parent": "WEB"})
	world.call("POST", "/v1/projects", json={"key": "DOCS", "title": "Docs", "parent": "API"})
	world.call("POST", "/v1/projects", json={"key": "PLAT", "title": "Platform"})

	moved = world.call("POST", "/v1/projects/API/move", json={"parent": "PLAT"})

	assert moved.status_code == 200
	assert moved.json()["depth"] == 1

	grandchild = world.call("GET", "/v1/projects/DOCS").json()

	assert grandchild["depth"] == 2, "the subtree moved with it"

	platform = world.call("GET", "/v1/projects/PLAT").json()

	assert grandchild["id"] != platform["id"]
	assert platform["id"] in world.call("GET", "/v1/projects/API").json()["parent_id"]


def test_a_project_can_be_moved_to_the_root (world: test_api_tasks.World) -> None:
	"""``parent: null`` is why this is a body rather than a query parameter."""

	world.call("POST", "/v1/projects", json={"key": "WEB", "title": "Website"})
	world.call("POST", "/v1/projects", json={"key": "API", "title": "API", "parent": "WEB"})

	moved = world.call("POST", "/v1/projects/API/move", json={"parent": None})

	assert moved.status_code == 200
	assert moved.json()["parent_id"] is None
	assert moved.json()["depth"] == 0


def test_a_cycle_is_refused (world: test_api_tasks.World) -> None:
	"""Nothing may become its own ancestor."""

	world.call("POST", "/v1/projects", json={"key": "WEB", "title": "Website"})
	world.call("POST", "/v1/projects", json={"key": "API", "title": "API", "parent": "WEB"})

	response = world.call("POST", "/v1/projects/WEB/move", json={"parent": "API"})

	assert response.status_code == 409
	assert response.json()["code"] == "cycle_detected"


def test_deleting_a_project_hides_its_tasks_with_it (world: test_api_tasks.World) -> None:
	"""Soft, and the tasks need no touching: every listing joins the project."""

	world.call("POST", "/v1/projects", json={"key": "WEB", "title": "Website"})
	world.call("POST", "/v1/tasks", json={"title": "Fix the header", "project": "WEB"})

	assert len(world.call("GET", "/v1/tasks").json()["items"]) == 1

	deleted = world.call("DELETE", "/v1/projects/WEB")

	assert deleted.status_code == 200
	assert deleted.json()["deleted_at"] is not None
	assert world.call("GET", "/v1/tasks").json()["items"] == []
	assert world.call("GET", "/v1/projects/WEB").status_code == 404


def test_the_inbox_cannot_be_deleted (world: test_api_tasks.World) -> None:
	"""A workspace without one has nowhere to file a task with no project."""

	response = world.call("DELETE", f"/v1/projects/{subroutine.domain.bootstrap.INBOX_KEY}")

	assert response.status_code == 422
	assert "Inbox" in response.json()["detail"]


def test_privacy_inherits_down_the_tree (session: sqlalchemy.orm.Session) -> None:
	"""A sub-project of a private project is private too (SPEC.md §7.3a).

	Without this, marking a project private and creating a sub-project inside it publishes
	the sub-project's titles to the whole workspace.
	"""

	world = test_api_tasks._world(session)

	world.call("POST", "/v1/projects", json={"key": "SECRET", "title": "Secret", "visibility": "private"})
	world.call("POST", "/v1/projects", json={"key": "INNER", "title": "Inner", "parent": "SECRET"})

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, outsider, role_key="member")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=outsider, title="outsider"
	)
	session.flush()

	nosy = world._replace(secret=issued.value.get_secret_value())
	visible = {item["key"] for item in nosy.call("GET", "/v1/projects").json()["items"]}

	assert "SECRET" not in visible
	assert "INNER" not in visible, "privacy reaches the children"

	assert nosy.call("GET", "/v1/projects/INNER").status_code == 404


def test_the_owner_of_a_private_project_can_still_reach_it (
	world: test_api_tasks.World,
) -> None:
	"""The membership row `create` writes is what makes this true at all."""

	world.call(
		"POST", "/v1/projects", json={"key": "SECRET", "title": "Secret", "visibility": "private"}
	)

	assert world.call("GET", "/v1/projects/SECRET").status_code == 200
	assert "SECRET" in {
		item["key"] for item in world.call("GET", "/v1/projects").json()["items"]
	}


def test_a_project_scoped_token_sees_only_its_own_subtree (
	session: sqlalchemy.orm.Session,
) -> None:
	"""SPEC.md §7.3: the scope restricts which rows, and a listing is what decides those."""

	world = test_api_tasks._world(session)

	allowed = world.call("POST", "/v1/projects", json={"key": "WEB", "title": "Website"}).json()
	world.call("POST", "/v1/projects", json={"key": "API", "title": "API", "parent": "WEB"})
	world.call("POST", "/v1/projects", json={"key": "OTHER", "title": "Other"})

	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=world.user, title="scoped", project_scope=[allowed["id"]]
	)
	session.flush()

	scoped = world._replace(secret=issued.value.get_secret_value())
	visible = {item["key"] for item in scoped.call("GET", "/v1/projects").json()["items"]}

	assert visible == {"WEB", "API"}, "the scope carries the subtree and stops at it"


def test_a_move_with_no_parent_is_refused_rather_than_flattening_the_tree (
	world: test_api_tasks.World,
) -> None:
	"""``POST /v1/projects/web/move {}`` used to reparent the project *and its subtree* to root.

	`Move.parent` defaults to `None` and the handler read it directly, so an omitted field and
	an explicit `null` were indistinguishable — the one mutating site in the API that did not
	use `model_fields_set`, twenty lines below its own docstring saying it must. A move rewrites
	the materialised path of every descendant and there is no undo, so this is the worst place
	in the API for §8.3's distinction to be missing.
	"""

	parent = world.call(
		"POST", "/v1/projects", json={"key": "TOP", "title": "Top"}
	).json()
	child = world.call(
		"POST", "/v1/projects", json={"key": "MID", "title": "Middle", "parent": "TOP"}
	).json()

	assert child["parent_id"] == parent["id"]

	refused = world.call("POST", "/v1/projects/MID/move", json={})

	assert refused.status_code == 422
	assert refused.json()["code"] == "missing_field"
	assert refused.json()["errors"][0]["field"] == "parent"

	# Nothing moved.
	assert world.call("GET", "/v1/projects/MID").json()["parent_id"] == parent["id"]


def test_an_explicit_null_parent_still_makes_a_project_a_root (
	world: test_api_tasks.World,
) -> None:
	"""The distinction §8.3 asks for cuts both ways: saying `null` must still work."""

	world.call("POST", "/v1/projects", json={"key": "TOP2", "title": "Top"})
	world.call(
		"POST", "/v1/projects", json={"key": "MID2", "title": "Middle", "parent": "TOP2"}
	)

	moved = world.call("POST", "/v1/projects/MID2/move", json={"parent": None})

	assert moved.status_code == 200
	assert moved.json()["parent_id"] is None
	assert moved.json()["depth"] == 0
