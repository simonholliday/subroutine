"""Comments over HTTP — SPEC.md §5.10.

The `comment` table has been in the schema since M1 with nothing writing to it. This is what
closes that: an agent working a task now has somewhere to record *what happened*, instead of
overwriting the description — destroying what the task is in order to say what happened to it —
or writing a document, which is far too heavy for a line about a test run.

The test that matters most is the mention one. Comments wired into the mention index is what
makes this more than CRUD.
"""

import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.comments
import subroutine.domain.users
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation with a task to talk about."""

	return test_api_tasks._world(session)


def test_a_comment_records_what_happened (world: test_api_tasks.World) -> None:
	"""The whole point, on the entity an agent works on."""

	task = world.call("POST", "/v1/tasks", json={"title": "Fix the parser"}).json()

	made = world.call(
		"POST",
		f"/v1/tasks/{task['ref']}/comments",
		json={"body": "Ran the suite: two failures, both in the date parser."},
	)

	assert made.status_code == 201
	assert made.json()["body"].startswith("Ran the suite")
	assert made.json()["entity_type"] == "task"

	listed = world.call("GET", f"/v1/tasks/{task['ref']}/comments")

	assert listed.status_code == 200
	assert [item["body"] for item in listed.json()["items"]] == [made.json()["body"]]

	# And the task itself is untouched — which is the thing this exists to avoid.
	assert world.call("GET", f"/v1/tasks/{task['ref']}").json()["description"] is None


def test_comments_are_oldest_first (world: test_api_tasks.World) -> None:
	"""A work record reads as a story from the beginning, unlike every other listing here."""

	task = world.call("POST", "/v1/tasks", json={"title": "Long job"}).json()

	for line in ("Started.", "Halfway.", "Done, with caveats."):
		world.call("POST", f"/v1/tasks/{task['ref']}/comments", json={"body": line})

	bodies = [
		item["body"]
		for item in world.call("GET", f"/v1/tasks/{task['ref']}/comments").json()["items"]
	]

	assert bodies == ["Started.", "Halfway.", "Done, with caveats."]


def test_a_ref_in_a_comment_becomes_a_backlink (world: test_api_tasks.World) -> None:
	"""**The test that makes this more than CRUD.**

	Writing "blocked by #1" in a comment makes the comment a backlink on #1, so somebody reading
	#1 can see that something is waiting on it without anyone having remembered to link them.
	``MENTION_SOURCE_TYPES`` has included ``comment`` since M1 and nothing produced one.
	"""

	blocker = world.call("POST", "/v1/tasks", json={"title": "The blocker"}).json()
	waiting = world.call("POST", "/v1/tasks", json={"title": "The waiting one"}).json()

	world.call(
		"POST",
		f"/v1/tasks/{waiting['ref']}/comments",
		json={"body": f"Cannot start: blocked by #{blocker['ref']}."},
	)

	# Through the model, never raw SQL with a stringified id: SQLite stores a UUID as bare hex
	# and PostgreSQL as a dashed uuid, so comparing the JSON form against the column compares
	# storage formats and silently matches nothing.
	assert _mentions_of(world, blocker["id"]) == [("comment", "task")]


def test_a_deleted_comment_stops_mentioning_things (world: test_api_tasks.World) -> None:
	"""A backlink pointing at a sentence nobody can read is worse than no backlink."""

	blocker = world.call("POST", "/v1/tasks", json={"title": "The blocker"}).json()
	waiting = world.call("POST", "/v1/tasks", json={"title": "The waiting one"}).json()

	made = world.call(
		"POST",
		f"/v1/tasks/{waiting['ref']}/comments",
		json={"body": f"blocked by #{blocker['ref']}"},
	).json()

	world.call("DELETE", f"/v1/comments/{made['id']}")

	assert _mentions_of(world, blocker["id"]) == []

	# And it is gone from the listing, but soft — nothing here hard-deletes.
	assert world.call("GET", f"/v1/tasks/{waiting['ref']}/comments").json()["items"] == []


def test_projects_and_documents_take_comments_too (world: test_api_tasks.World) -> None:
	"""All three subjects §5.10 names, through one registration so they cannot drift."""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Web"})
	document = world.call("POST", "/v1/documents", json={"title": "A finding"}).json()

	on_project = world.call(
		"POST", "/v1/projects/WEB/comments", json={"body": "Renamed from Website."}
	)
	on_document = world.call(
		"POST", f"/v1/documents/{document['ref']}/comments", json={"body": "Superseded."}
	)

	assert on_project.status_code == 201
	assert on_project.json()["entity_type"] == "project"
	assert on_document.status_code == 201
	assert on_document.json()["entity_type"] == "document"


def test_only_the_author_may_edit_but_the_version_still_guards (
	world: test_api_tasks.World,
) -> None:
	"""Attributed prose: an administrator rewriting somebody's words under their name is not a
	permission anybody should hold. Deleting is the honest alternative and is allowed."""

	task = world.call("POST", "/v1/tasks", json={"title": "A task"}).json()
	made = world.call(
		"POST", f"/v1/tasks/{task['ref']}/comments", json={"body": "First take."}
	).json()

	edited = world.call(
		"PATCH", f"/v1/comments/{made['id']}", json={"body": "Second take."}
	)

	assert edited.status_code == 200
	assert edited.json()["body"] == "Second take."
	assert edited.json()["version"] == made["version"] + 1

	stale = world.call(
		"PATCH",
		f"/v1/comments/{made['id']}",
		json={"body": "Third take.", "expected_version": 1},
	)

	assert stale.status_code == 409

	# A different account may not edit it, whatever their role.
	stranger = subroutine.domain.users.create(
		session=world.session, username=f"other-{uuid.uuid4().hex[:8]}"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session=world.session, user=stranger, title="Other"
	)
	world.session.flush()

	refused = api_support.call(
		world.application,
		"PATCH",
		f"/v1/comments/{made['id']}",
		headers={"authorization": f"Bearer {issued.value.get_secret_value()}"},
		json={"body": "Not yours."},
	)

	assert refused.status_code in {403, 404}


def test_an_empty_or_enormous_body_is_refused (world: test_api_tasks.World) -> None:
	"""Checked in the service, so the message names the field — SQLite enforces no length."""

	task = world.call("POST", "/v1/tasks", json={"title": "A task"}).json()

	assert (
		world.call("POST", f"/v1/tasks/{task['ref']}/comments", json={"body": "  "}).status_code
		== 422
	)

	huge = "x" * (subroutine.domain.comments.MAX_BODY_LENGTH + 1)
	refused = world.call("POST", f"/v1/tasks/{task['ref']}/comments", json={"body": huge})

	assert refused.status_code == 413
	assert "body" in refused.text


def test_a_narrowed_token_cannot_comment (session: sqlalchemy.orm.Session) -> None:
	"""``comment:write`` is a real verb and a read-only agent must not have it."""

	owner = test_api_tasks._world(session)
	task = owner.call("POST", "/v1/tasks", json={"title": "A task"}).json()

	narrowed = test_api_tasks._world(session, scopes=["task:read"])
	refused = narrowed.call(
		"POST", f"/v1/tasks/{task['ref']}/comments", json={"body": "Sneaking in."}
	)

	assert refused.status_code == 403


def test_comments_on_something_you_cannot_see_are_absent (
	world: test_api_tasks.World,
) -> None:
	"""A comment endpoint that said "forbidden" would confirm the task exists (§7.3a)."""

	missing = world.call(
		"POST", "/v1/tasks/99999/comments", json={"body": "Into the void."}
	)

	assert missing.status_code == 404


def _mentions_of (world: test_api_tasks.World, target: str) -> list[tuple[str, str]]:
	"""Return the (source_type, target_type) pairs pointing at one item."""

	model = subroutine.db.models.work.Mention
	rows = world.session.scalars(
		sqlalchemy.select(model).where(model.target_id == uuid.UUID(target))
	).all()

	return [(row.source_type, row.target_type) for row in rows]
