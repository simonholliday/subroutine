"""Per-entity histories over HTTP — SPEC.md §5.11a.

The first reader of the ``event`` table, which five domain modules had been writing to since
M1 with nothing reading them back. Building it found a real hole in the writes on its first
run, which is the argument for building readers early rather than the argument for this file.

Two tests here are named in the done-criteria and are the reason the endpoint is not simply
"the feed with a filter":

* **An event written this instant is returned.** The feed will carry a ``now() - 1s``
  watermark because it is resumable; a history is not, and inheriting the watermark would
  mean commenting on an item and immediately reading its history shows nothing — which
  somebody meets in the first minute and reads as a lost write.
* **A private project's history is not readable by a non-member.** Resolving the subject
  *is* the permission check, so this is what proves the resolution actually happens.
"""

import uuid

import pytest
import sqlalchemy.orm

import subroutine.domain.authentication
import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation with something to have a history."""

	return test_api_tasks._world(session)


def test_a_task_has_a_history_from_the_moment_it_exists (
	world: test_api_tasks.World,
) -> None:
	"""Creation is itself an event, so a history is never empty for a thing that exists."""

	task = world.call("POST", "/v1/tasks", json={"title": "Fix the parser"}).json()

	answered = world.call("GET", f"/v1/tasks/{task['ref']}/events")

	assert answered.status_code == 200

	events = answered.json()["items"]

	assert [item["action"] for item in events] == ["created"]
	assert events[0]["entity_type"] == "task"
	assert events[0]["entity_id"] == task["id"]
	assert events[0]["seq"] > 0


def test_an_event_written_this_instant_is_returned (world: test_api_tasks.World) -> None:
	"""The watermark bug, and the one thing a history must not inherit from the feed.

	`GET /v1/changes` will refuse to return events newer than `now() - 1s`, because `seq` is
	allocated at insert and becomes visible at commit — a resumable reader that advances past
	an uncommitted number never sees that event again. A history is not resumable: ask again
	and the row is there. So it takes no watermark, and this is what says so.

	There is no sleep here on purpose. A test that waited would pass against an endpoint that
	carried the watermark, and would therefore be testing nothing at all.
	"""

	task = world.call("POST", "/v1/tasks", json={"title": "Fix the parser"}).json()

	world.call("PATCH", f"/v1/tasks/{task['ref']}", json={"importance": 4})

	events = world.call("GET", f"/v1/tasks/{task['ref']}/events").json()["items"]

	assert [item["action"] for item in events] == ["updated", "created"]
	assert events[0]["changes"] == {"importance": {"from": None, "to": 4}}


def test_a_history_is_newest_first (world: test_api_tasks.World) -> None:
	"""The opposite of the feed, which runs forwards because a cursor goes forwards."""

	task = world.call("POST", "/v1/tasks", json={"title": "Fix the parser"}).json()

	for importance in (1, 2, 3):
		world.call("PATCH", f"/v1/tasks/{task['ref']}", json={"importance": importance})

	events = world.call("GET", f"/v1/tasks/{task['ref']}/events").json()["items"]
	sequence = [item["seq"] for item in events]

	assert sequence == sorted(sequence, reverse=True)
	assert events[-1]["action"] == "created", "the oldest event is the creation"


def test_a_history_pages_with_the_ordinary_cursor (world: test_api_tasks.World) -> None:
	"""SPEC.md §5.11a: the standard keyset cursor, deliberately not ``?since=``.

	A `?since=` here would invite treating a history as resumable, which is how the watermark
	problem would arrive per entity having been solved once globally.
	"""

	task = world.call("POST", "/v1/tasks", json={"title": "Fix the parser"}).json()

	for importance in (1, 2, 3, 4):
		world.call("PATCH", f"/v1/tasks/{task['ref']}", json={"importance": importance})

	first = world.call("GET", f"/v1/tasks/{task['ref']}/events?limit=2").json()

	assert len(first["items"]) == 2
	assert first["page"]["has_more"] is True
	assert first["page"]["next_cursor"] is not None

	second = world.call(
		"GET",
		f"/v1/tasks/{task['ref']}/events?limit=2&cursor={first['page']['next_cursor']}",
	).json()

	seen = [item["seq"] for item in first["items"]] + [item["seq"] for item in second["items"]]

	assert seen == sorted(seen, reverse=True), "the cursor continues the ordering"
	assert len(set(seen)) == len(seen), "no row is returned twice across the page boundary"


def test_a_history_can_be_read_oldest_first (world: test_api_tasks.World) -> None:
	"""``?order=seq``, because reading a story from the beginning is a real thing to want."""

	task = world.call("POST", "/v1/tasks", json={"title": "Fix the parser"}).json()
	world.call("PATCH", f"/v1/tasks/{task['ref']}", json={"importance": 4})

	events = world.call("GET", f"/v1/tasks/{task['ref']}/events?order=seq").json()["items"]

	assert [item["action"] for item in events] == ["created", "updated"]


def test_a_history_refuses_a_sort_field_it_does_not_have (
	world: test_api_tasks.World,
) -> None:
	"""An event log has one meaningful ordering, and the refusal says which."""

	task = world.call("POST", "/v1/tasks", json={"title": "Fix the parser"}).json()

	refused = world.call("GET", f"/v1/tasks/{task['ref']}/events?order=created_at")

	assert refused.status_code == 422
	assert "seq" in refused.text


def test_projects_and_documents_have_histories_too (world: test_api_tasks.World) -> None:
	"""All three, from one registration, so they cannot drift into three different APIs."""

	project = world.call(
		"POST", "/v1/projects", json={"key": "WEB", "title": "Website"}
	).json()
	document = world.call("POST", "/v1/documents", json={"title": "How it works"}).json()

	from_project = world.call("GET", f"/v1/projects/{project['key']}/events").json()

	assert [item["action"] for item in from_project["items"]] == ["created"]
	assert from_project["items"][0]["entity_type"] == "project"

	from_document = world.call("GET", f"/v1/documents/{document['ref']}/events").json()

	assert [item["action"] for item in from_document["items"]] == ["created"]
	assert from_document["items"][0]["entity_type"] == "document"


def test_a_history_holds_only_that_item (world: test_api_tasks.World) -> None:
	"""Two tasks changed in the same workspace must not appear in each other's history."""

	one = world.call("POST", "/v1/tasks", json={"title": "One"}).json()
	two = world.call("POST", "/v1/tasks", json={"title": "Two"}).json()

	world.call("PATCH", f"/v1/tasks/{two['ref']}", json={"importance": 4})

	events = world.call("GET", f"/v1/tasks/{one['ref']}/events").json()["items"]

	assert {item["entity_id"] for item in events} == {one["id"]}


def test_a_history_is_not_readable_inside_a_private_project (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The leak this endpoint makes newly possible (SPEC.md §7.3a).

	Resolving the subject **is** the permission check — the route goes through the task's own
	narrowed statement — so this is the test that proves the resolution happens rather than
	being described in a docstring. Reported as *absent* rather than forbidden, because
	"forbidden" would confirm the task exists.
	"""

	world = test_api_tasks._world(session)

	world.call(
		"POST",
		"/v1/projects",
		json={"key": "SECRET", "title": "Secret", "visibility": "private"},
	)
	hidden = world.call(
		"POST", "/v1/tasks", json={"title": "Acquire the rival company", "project": "SECRET"}
	).json()

	assert world.call("GET", f"/v1/tasks/{hidden['ref']}/events").status_code == 200

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, outsider, role_key="member")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=outsider, title="outsider"
	)
	session.flush()

	nosy = world._replace(secret=issued.value.get_secret_value())

	assert nosy.call("GET", f"/v1/tasks/{hidden['ref']}/events").status_code == 404


def test_a_history_refuses_a_parameter_it_does_not_declare (
	world: test_api_tasks.World,
) -> None:
	"""§8.1, and the reason it matters here: a typo'd ``since`` must not read as a filter.

	``?since=`` is the *feed's* parameter and deliberately not this one. Silently ignoring it
	would return the whole history to somebody who believes they asked for part of it.
	"""

	task = world.call("POST", "/v1/tasks", json={"title": "Fix the parser"}).json()

	refused = world.call("GET", f"/v1/tasks/{task['ref']}/events?since=1")

	assert refused.status_code == 422
	assert "since" in refused.text
