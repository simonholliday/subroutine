"""Projects over HTTP.

The tree is what makes projects different from a flat list, so most of what is worth
testing here is about the tree: that a listing reads as one, that a move takes the subtree,
and that privacy inherits down it.
"""

import typing
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

	assert body["key"] == "web", "keys are stored upper-cased"
	assert body["owner_id"] == str(world.user.id)
	assert body["visibility"] == "public"
	assert body["depth"] == 0


def test_a_project_is_readable_by_key_in_any_case (world: test_api_tasks.World) -> None:
	"""A key is what people have in front of them; requiring an id would be a round trip."""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})

	assert world.call("GET", "/v1/projects/WEB").json()["key"] == "web"
	assert world.call("GET", "/v1/projects/web").json()["key"] == "web"


def test_a_reserved_key_is_refused_before_it_becomes_unreachable (
	world: test_api_tasks.World,
) -> None:
	"""A project keyed 'search' would share an address with an endpoint (docs/design.md §8.1)."""

	response = world.call("POST", "/v1/projects", json={"key": "search", "title": "Nope"})

	assert response.status_code == 422
	assert "reserved" in response.json()["errors"][0]["message"]


def test_a_project_listing_reads_as_the_tree_it_is (world: test_api_tasks.World) -> None:
	"""Ordered by path, so a parent is immediately followed by its children."""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})
	world.call("POST", "/v1/projects", json={"key": "api", "title": "api", "parent": "web"})
	world.call("POST", "/v1/projects", json={"key": "ZED", "title": "Zed"})

	listed = world.call("GET", "/v1/projects").json()["items"]
	positions = {item["key"]: index for index, item in enumerate(listed)}

	assert positions["api"] == positions["web"] + 1, "a child follows its parent"
	assert listed[positions["api"]]["depth"] == 1


def test_a_listing_can_be_narrowed_to_one_parent (world: test_api_tasks.World) -> None:
	"""The children of one project, without walking the whole tree."""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})
	world.call("POST", "/v1/projects", json={"key": "api", "title": "api", "parent": "web"})

	children = world.call("GET", "/v1/projects?parent=WEB").json()["items"]

	assert [item["key"] for item in children] == ["api"]


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

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})

	changed = world.call("PATCH", "/v1/projects/WEB", json={"title": "The Website"})

	assert changed.status_code == 200
	assert changed.json()["title"] == "The Website"

	renamed = world.call("PATCH", "/v1/projects/WEB", json={"key": "SITE"})

	assert renamed.status_code == 200
	assert renamed.json()["key"] == "site"

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

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})
	world.call("POST", "/v1/projects", json={"key": "api", "title": "The API"})

	assert world.call("PATCH", "/v1/projects/WEB", json={"key": "api"}).status_code == 409
	assert world.call("PATCH", "/v1/projects/WEB", json={"key": "9NO"}).status_code == 422


def test_an_omitted_field_is_untouched_and_a_null_one_is_cleared (
	world: test_api_tasks.World,
) -> None:
	"""docs/design.md §8.3 applies here exactly as it does to tasks."""

	world.call(
		"POST", "/v1/projects", json={"key": "web", "title": "Website", "description": "Notes"}
	)

	renamed = world.call("PATCH", "/v1/projects/WEB", json={"title": "Site"}).json()

	assert renamed["description"] == "Notes", "an omitted field must be left alone"

	cleared = world.call("PATCH", "/v1/projects/WEB", json={"description": None}).json()

	assert cleared["description"] is None
	assert cleared["title"] == "Site"


def test_moving_a_project_takes_its_subtree (world: test_api_tasks.World) -> None:
	"""The materialised path is rewritten for every descendant, not just the node moved."""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})
	world.call("POST", "/v1/projects", json={"key": "api", "title": "api", "parent": "web"})
	world.call("POST", "/v1/projects", json={"key": "docs", "title": "Docs", "parent": "api"})
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

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})
	world.call("POST", "/v1/projects", json={"key": "api", "title": "api", "parent": "web"})

	moved = world.call("POST", "/v1/projects/API/move", json={"parent": None})

	assert moved.status_code == 200
	assert moved.json()["parent_id"] is None
	assert moved.json()["depth"] == 0


def test_a_cycle_is_refused (world: test_api_tasks.World) -> None:
	"""Nothing may become its own ancestor."""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})
	world.call("POST", "/v1/projects", json={"key": "api", "title": "api", "parent": "web"})

	response = world.call("POST", "/v1/projects/WEB/move", json={"parent": "api"})

	assert response.status_code == 409
	assert response.json()["code"] == "cycle_detected"


def test_deleting_a_project_hides_its_tasks_with_it (world: test_api_tasks.World) -> None:
	"""Soft, and the tasks need no touching: every listing joins the project."""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})
	world.call("POST", "/v1/tasks", json={"title": "Fix the header", "project": "web"})

	assert len(world.call("GET", "/v1/tasks").json()["items"]) == 1

	deleted = world.call("DELETE", "/v1/projects/WEB")

	assert deleted.status_code == 200
	assert deleted.json()["deleted_at"] is not None
	assert world.call("GET", "/v1/tasks").json()["items"] == []
	assert world.call("GET", "/v1/projects/WEB").status_code == 404


def test_restoring_a_project_brings_its_tasks_back_with_it (
	world: test_api_tasks.World,
) -> None:
	"""The reversal ``DELETE`` has always promised, and nothing provided until `#308`.

	The test above ends where the product used to: everything hidden, permanently, by a route
	whose own summary says the tasks "return with it". Nothing touches the tasks in either
	direction — undeleting the project is the whole of undeleting what is in it, which is what
	makes this one row rather than a cascade to unwind.
	"""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})
	world.call("POST", "/v1/tasks", json={"title": "Fix the header", "project": "web"})
	world.call("DELETE", "/v1/projects/WEB")

	assert world.call("GET", "/v1/tasks").json()["items"] == []

	back = world.call("POST", "/v1/projects/WEB/restore")

	assert back.status_code == 200
	assert back.json()["deleted_at"] is None
	assert len(world.call("GET", "/v1/tasks").json()["items"]) == 1
	assert world.call("GET", "/v1/projects/WEB").status_code == 200


def test_restoring_a_project_twice_is_not_an_error (world: test_api_tasks.World) -> None:
	"""Symmetrically with deleting twice, and neither moves a timestamp already in place."""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})
	world.call("DELETE", "/v1/projects/WEB")
	world.call("POST", "/v1/projects/WEB/restore")

	again = world.call("POST", "/v1/projects/WEB/restore")

	assert again.status_code == 200
	assert again.json()["deleted_at"] is None


def test_a_project_under_a_deleted_one_is_not_restored_alone (
	world: test_api_tasks.World,
) -> None:
	"""Putting a row back inside a subtree nobody can see would report a success that is not one.

	Privacy and deletion both inherit down the tree, so restoring the child of a deleted parent
	clears one ``deleted_at`` and changes nothing anybody can observe. The refusal names the
	ancestor, because that is the command the caller actually wants.
	"""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})
	world.call("POST", "/v1/projects", json={"key": "api", "title": "api", "parent": "web"})
	world.call("DELETE", "/v1/projects/WEB")
	world.call("DELETE", "/v1/projects/API")

	refused = world.call("POST", "/v1/projects/API/restore")

	assert refused.status_code == 422
	assert "web" in refused.json()["detail"]


def test_restoring_a_project_whose_key_was_reused_is_refused_by_name (
	world: test_api_tasks.World,
) -> None:
	"""Three ordinary commands produced a 500, and the index is why.

	The uniqueness of a key is a *partial* index, ignoring deleted rows — which is exactly
	what makes the key reusable once a project is in the trash. So delete, create another with
	the same key, restore the first, and the constraint fires at flush time as an unhandled
	``IntegrityError``: a 500 over HTTP, a bare traceback at the terminal.

	Refused with the command to run instead. There is nothing to do on the caller's behalf: a
	restore that renamed one of the two would be choosing which project keeps the name people
	have been typing.
	"""

	old = world.call("POST", "/v1/projects", json={"key": "web", "title": "Old site"}).json()

	world.call("DELETE", "/v1/projects/web")
	world.call("POST", "/v1/projects", json={"key": "web", "title": "New site"})

	# By id, because the key now names the live one — which is the whole situation.
	refused = world.call("POST", f"/v1/projects/{old['id']}/restore")

	assert refused.status_code == 409, refused.text
	assert refused.json()["code"] == "duplicate_key"
	assert "project rename" in refused.text, "and it names the way out"


def test_the_inbox_cannot_be_deleted (world: test_api_tasks.World) -> None:
	"""A workspace without one has nowhere to file a task with no project."""

	response = world.call("DELETE", f"/v1/projects/{subroutine.domain.bootstrap.INBOX_KEY}")

	assert response.status_code == 422
	assert "Inbox" in response.json()["detail"]


def test_privacy_inherits_down_the_tree (session: sqlalchemy.orm.Session) -> None:
	"""A sub-project of a private project is private too (docs/design.md §7.3a).

	Without this, marking a project private and creating a sub-project inside it publishes
	the sub-project's titles to the whole workspace.
	"""

	world = test_api_tasks._world(session)

	world.call("POST", "/v1/projects", json={"key": "secret", "title": "Secret", "visibility": "private"})
	world.call("POST", "/v1/projects", json={"key": "inner", "title": "Inner", "parent": "secret"})

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, outsider, role_key="member")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=outsider, title="outsider"
	)
	session.flush()

	nosy = world._replace(secret=issued.value.get_secret_value())
	visible = {item["key"] for item in nosy.call("GET", "/v1/projects").json()["items"]}

	assert "secret" not in visible
	assert "inner" not in visible, "privacy reaches the children"

	assert nosy.call("GET", "/v1/projects/INNER").status_code == 404


def test_the_owner_of_a_private_project_can_still_reach_it (
	world: test_api_tasks.World,
) -> None:
	"""The membership row `create` writes is what makes this true at all."""

	world.call(
		"POST", "/v1/projects", json={"key": "secret", "title": "Secret", "visibility": "private"}
	)

	assert world.call("GET", "/v1/projects/SECRET").status_code == 200
	assert "secret" in {
		item["key"] for item in world.call("GET", "/v1/projects").json()["items"]
	}


def test_a_project_scoped_token_sees_only_its_own_subtree (
	session: sqlalchemy.orm.Session,
) -> None:
	"""docs/design.md §7.3: the scope restricts which rows, and a listing is what decides those."""

	world = test_api_tasks._world(session)

	allowed = world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"}).json()
	world.call("POST", "/v1/projects", json={"key": "api", "title": "api", "parent": "web"})
	world.call("POST", "/v1/projects", json={"key": "OTHER", "title": "Other"})

	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=world.user, title="scoped", project_scope=[allowed["id"]]
	)
	session.flush()

	scoped = world._replace(secret=issued.value.get_secret_value())
	visible = {item["key"] for item in scoped.call("GET", "/v1/projects").json()["items"]}

	assert visible == {"web", "api"}, "the scope carries the subtree and stops at it"


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
		"POST", "/v1/projects", json={"key": "top", "title": "Top"}
	).json()
	child = world.call(
		"POST", "/v1/projects", json={"key": "MID", "title": "Middle", "parent": "top"}
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


def _tree (world: test_api_tasks.World) -> None:
	"""Two roots, each holding a project keyed ``dist``, and one holding a ``tools``."""

	for key, title in (("substation", "Substation"), ("websites", "Websites")):
		world.call("POST", "/v1/projects", json={"key": key, "title": title})

	for parent in ("substation", "websites"):
		world.call(
			"POST",
			"/v1/projects",
			json={"key": "dist", "title": "Packaging", "parent": parent},
		)

	world.call(
		"POST",
		"/v1/projects",
		json={"key": "tools", "title": "Tools", "parent": "substation"},
	)


def test_a_whole_address_resolves_exactly (world: test_api_tasks.World) -> None:
	"""Decision `#957`. ``substation/dist`` is one project however many ``dist`` there are."""

	_tree(world)

	read = world.call("GET", "/v1/projects/substation/dist")

	assert read.status_code == 200, read.text
	assert read.json()["key"] == "dist"
	assert read.json()["parent_id"] == world.call("GET", "/v1/projects/substation").json()["id"]


def test_a_bare_name_still_resolves_where_it_names_one_project (
	world: test_api_tasks.World,
) -> None:
	"""Which is what keeps every address anybody has already written working."""

	_tree(world)

	assert world.call("GET", "/v1/projects/tools").json()["key"] == "tools"


def test_an_ambiguous_name_is_refused_with_the_addresses_listed (
	world: test_api_tasks.World,
) -> None:
	"""Never guessed — Simon's answer of 2026-08-17, and the refusal is what teaches the form.

	The accepted cost of resolving a name by search is that a command can begin failing
	because somebody *else* created a second ``dist``. That is bearable exactly because the
	refusal says which two and what to type instead.
	"""

	_tree(world)

	refused = world.call("GET", "/v1/projects/dist")

	assert refused.status_code == 422, refused.text
	assert "substation/dist" in refused.text
	assert "websites/dist" in refused.text


def test_a_root_is_reached_by_its_own_bare_key (world: test_api_tasks.World) -> None:
	"""**And this is why the address is tried before the name**, rather than a preference.

	A root's whole address *is* its bare key. Searching first would refuse that word as
	ambiguous with the nested ones — leaving the root project reachable by no string at all,
	which is the opposite of what addressing it by path is for.
	"""

	_tree(world)
	world.call("POST", "/v1/projects", json={"key": "dist", "title": "Top level"})

	read = world.call("GET", "/v1/projects/dist")

	assert read.status_code == 200, read.text
	assert read.json()["title"] == "Top level"
	assert read.json()["parent_id"] is None


def test_an_address_that_misses_does_not_fall_back_to_its_first_segment (
	world: test_api_tasks.World,
) -> None:
	"""``substation/nope`` is not ``substation``.

	A plausible, complete, wrong answer is the failure this codebase keeps meeting, and a
	resolver that searched for the first segment when the whole address missed would produce
	one — filing work into the parent of the project somebody named.
	"""

	_tree(world)

	assert world.call("GET", "/v1/projects/substation/nope").status_code == 404


def test_a_refusal_offers_addresses_rather_than_bare_keys (
	world: test_api_tasks.World,
) -> None:
	"""A list holding ``dist`` twice says the spelling was right and nothing else."""

	_tree(world)

	refused = world.call("GET", "/v1/projects/nope")

	assert refused.status_code == 404
	assert "substation/dist" in refused.text
	assert "websites/dist" in refused.text


def test_a_task_is_filed_by_a_whole_address (world: test_api_tasks.World) -> None:
	"""On both ways of naming a project, which are one resolver since `#958`."""

	_tree(world)

	captured = world.call("POST", "/v1/tasks", json={"text": "Ship it +substation/dist"})
	structured = world.call(
		"POST", "/v1/tasks", json={"title": "Ship it", "project": "substation/dist"}
	)

	assert captured.status_code == 201, captured.text
	assert captured.json()["title"] == "Ship it", "the address left the title"
	assert captured.json()["project_id"] == structured.json()["project_id"]


def test_both_ways_of_naming_a_missing_project_are_refused_alike (
	world: test_api_tasks.World,
) -> None:
	"""They were 422 and 404 until `#958`, told apart by which field carried the name.

	Two answers to one mistake, from two resolvers. There is one now.
	"""

	_tree(world)

	captured = world.call("POST", "/v1/tasks", json={"text": "Ship it +nope"})
	structured = world.call("POST", "/v1/tasks", json={"title": "Ship it", "project": "nope"})

	assert captured.status_code == structured.status_code == 404
	assert captured.json()["code"] == structured.json()["code"] == "not_found"


def _held (world: test_api_tasks.World, key: str, status: str) -> typing.Any:
	"""Put a project into one of its workspace's own statuses."""

	return world.call("PATCH", f"/v1/projects/{key}", json={"status": status})


def test_a_project_can_be_put_on_hold_and_brought_back (
	world: test_api_tasks.World,
) -> None:
	"""`#983`. Three of the four seeded project statuses could be reached by nothing at all.

	``PATCH /v1/projects`` accepted a key, a title, a description, a visibility and an owner,
	and no route anywhere set a status — so every project ever created was ``active`` for its
	whole life and ``on_hold``, ``completed`` and ``archived`` were vocabulary nobody could
	use. **Both directions are driven**, because a pause that cannot be undone is not a pause.
	"""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})

	assert _held(world, "web", "on_hold").json()["status"] == "on_hold"
	assert _held(world, "web", "on_hold").json()["status_category"] == "todo"
	assert _held(world, "web", "active").json()["status"] == "active"


def test_a_project_status_that_names_nothing_is_refused_with_the_ones_that_exist (
	world: test_api_tasks.World,
) -> None:
	"""§5.5 makes the vocabulary a workspace's own, so the refusal has to read it rather than
	assert the seeded four — an installation is free to have renamed every one of them."""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})

	response = _held(world, "web", "paused")

	assert response.status_code == 422

	body = response.json()

	assert "paused" in body["detail"]
	assert "on_hold" in str(body), "the refusal does not say what would have worked"


def test_work_in_a_project_on_hold_is_not_offered_as_ready (
	world: test_api_tasks.World,
) -> None:
	"""The point of the feature: putting a project down stops it answering "what next".

	Its sibling below is what stops this being a delete — the work is still there, still
	listed and still found. **Both are needed and neither is sufficient**: a version that
	hid the rows entirely would pass this test and be a different, worse feature.
	"""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})
	world.call("POST", "/v1/projects", json={"key": "ops", "title": "Ops"})

	held = world.call(
		"POST", "/v1/tasks", json={"title": "Redesign the header", "project": "web"}
	).json()["ref"]
	running = world.call(
		"POST", "/v1/tasks", json={"title": "Rotate the certificates", "project": "ops"}
	).json()["ref"]

	_held(world, "web", "on_hold")

	listed = [
		item["ref"] for item in world.call("GET", "/v1/tasks?ready=true&limit=50").json()["items"]
	]

	assert running in listed
	assert held not in listed


def test_work_in_a_project_on_hold_is_still_listed_and_still_found (
	world: test_api_tasks.World,
) -> None:
	"""On hold is a pause, not a disappearance — OmniFocus's and Things's rule, and `#983`'s.

	An ordinary listing still holds it and a search still finds it. This is the half that
	makes the status reversible in practice rather than only in principle: work you cannot
	see is work you cannot decide to resume.
	"""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})
	held = world.call(
		"POST", "/v1/tasks", json={"title": "Redesign the header", "project": "web"}
	).json()["ref"]

	_held(world, "web", "on_hold")

	listed = [item["ref"] for item in world.call("GET", "/v1/tasks?limit=50").json()["items"]]
	narrowed = [
		item["ref"]
		for item in world.call("GET", "/v1/tasks?project=web&limit=50").json()["items"]
	]
	found = [
		item["ref"]
		for item in world.call("GET", "/v1/tasks?q=header&limit=50").json()["items"]
	]

	assert held in listed, "an ordinary listing lost it"
	assert held in narrowed, "asking for the project itself lost it"
	assert held in found, "a search lost it"


def test_bringing_a_project_back_offers_its_work_again (
	world: test_api_tasks.World,
) -> None:
	"""Readiness has to *change*, not merely be computed once — `test_ready_excludes_…`'s rule.

	A predicate that reads the status at query time passes this; one that stamped something
	onto the task when the project was held would not, and that is the shape worth refusing.
	"""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})
	held = world.call(
		"POST", "/v1/tasks", json={"title": "Redesign the header", "project": "web"}
	).json()["ref"]

	_held(world, "web", "on_hold")
	_held(world, "web", "active")

	listed = [
		item["ref"] for item in world.call("GET", "/v1/tasks?ready=true&limit=50").json()["items"]
	]

	assert held in listed
