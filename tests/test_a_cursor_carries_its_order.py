"""A cursor carries the order it was made with - `SR#5140`.

M-19 of the cold review of 2026-10-17, L-7's bullet that ``pagination.decode`` ignores the sort
direction, and the review's Open Question 8, decided in `#5097`: **a cursor carries the sort keys
and directions it was made with.** Followed with no order, it continues in its own; followed with
a different one, it is refused by name, saying which order it was made for. A cursor made before
this is still read.
"""

import base64
import json
import typing

import pytest
import sqlalchemy.orm

import subroutine.api.pagination
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def _filed (world: test_api_tasks.World, count: int) -> list[int]:
	"""File ``count`` tasks, oldest first, and return their refs."""

	return [
		world.call("POST", "/v1/tasks", json={"title": f"Task {number}"}).json()["ref"]
		for number in range(count)
	]


@pytest.mark.parametrize("key", ["done", "cancelled"])
def test_a_finished_groups_cursor_continues_as_its_hint_says (
	world: test_api_tasks.World, key: str
) -> None:
	"""A group's ``next_cursor``, followed with the group's key and nothing else as the hint says,
	continues the group. ``status_category=done`` defaults to when work finished, not to the
	grouped order, so the cursor was refused for keys that did not match."""

	refs = _filed(world, 9)

	for ref in refs[3:6]:
		assert world.call("POST", f"/v1/tasks/{ref}/complete").status_code == 200

	for ref in refs[6:]:
		assert world.call(
			"PATCH", f"/v1/tasks/{ref}", json={"status": "cancelled"}
		).status_code == 200

	grouped = world.call(
		"GET", "/v1/tasks?group_by=status_category&group_limit=1&include_completed=true&fields=ref"
	)

	assert grouped.status_code == 200, grouped.text

	group = next(one for one in grouped.json()["groups"] if one["key"] == key)
	followed = world.call(
		"GET", f"/v1/tasks?status_category={key}&fields=ref&cursor={group['page']['next_cursor']}"
	)

	assert followed.status_code == 200, followed.text

	held = refs[3:6] if key == "done" else refs[6:]
	read = [row["ref"] for row in group["items"] + followed.json()["items"]]

	assert read == sorted(held, reverse=True), read


def _listing (world: test_api_tasks.World, kind: str) -> str:
	"""File five of one kind and return the path that lists them; a history is one task's."""

	if kind == "history":
		ref = _filed(world, 1)[0]

		for number in range(5):
			world.call("PATCH", f"/v1/tasks/{ref}", json={"title": f"Renamed {number}"})

		return f"/v1/tasks/{ref}/events"

	for number in range(5):
		body: dict[str, typing.Any] = {"title": f"{kind} {number}"}

		if kind == "projects":
			body["key"] = f"place{number}"

		if kind == "workspaces":
			body["slug"] = f"place{number}"

		assert world.call("POST", f"/v1/{kind}", json=body).status_code == 201

	return f"/v1/{kind}"


@pytest.mark.parametrize(
	("kind", "field"),
	[
		("tasks", "created_at"),
		("documents", "created_at"),
		("projects", "created_at"),
		("workspaces", "created_at"),
		("history", "seq"),
	],
)
def test_a_cursor_continues_in_its_own_order_and_refuses_another_by_name (
	world: test_api_tasks.World, kind: str, field: str
) -> None:
	"""Made with ``order=<field>``: followed with no order it continues forwards, where it used to
	meet the newest-first default; followed with the same order, the same; and followed with the
	reverse it is refused, naming its order, where it answered one row already seen as the end."""

	path = _listing(world, kind)
	whole = [one["id"] for one in world.call("GET", f"{path}?order={field}&limit=100").json()["items"]]
	first = world.call("GET", f"{path}?order={field}&limit=2")
	cursor = first.json()["page"]["next_cursor"]

	assert len(whole) >= 5 and cursor is not None, first.text

	for asked in ("", f"&order={field}"):
		followed = world.call("GET", f"{path}?limit=2&cursor={cursor}{asked}")

		assert followed.status_code == 200, followed.text
		assert [one["id"] for one in followed.json()["items"]] == whole[2:4], (asked, followed.text)

	reversed_ = world.call("GET", f"{path}?order=-{field}&limit=2&cursor={cursor}")

	assert reversed_.status_code == 422, reversed_.text
	assert reversed_.json()["detail"] == f"That cursor was made for order={field}.", reversed_.text
	assert reversed_.json()["errors"][0]["field"] == "query.cursor"


def test_a_cursor_made_before_cursors_carried_their_order_is_still_read (
	world: test_api_tasks.World,
) -> None:
	"""A cursor in the earlier shape, holding its keys' values and no order, is read as it was."""

	refs = _filed(world, 4)
	first = world.call("GET", "/v1/tasks?limit=2&fields=ref")
	body = first.json()["page"]["next_cursor"].partition(".")[0]
	payload = json.loads(base64.urlsafe_b64decode(body))

	assert payload.pop("~order"), "a cursor made now carries its order"

	earlier = base64.urlsafe_b64encode(
		json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
	).decode("ascii")
	signature = subroutine.api.pagination._sign(
		world.application.state.settings.require_secret_key(),
		earlier,
		subroutine.api.pagination.in_workspace("tasks", world.workspace.id),
	)
	followed = world.call("GET", f"/v1/tasks?limit=2&fields=ref&cursor={earlier}.{signature}")

	assert followed.status_code == 200, followed.text

	read = [row["ref"] for row in first.json()["items"] + followed.json()["items"]]

	assert read == sorted(refs, reverse=True), read
