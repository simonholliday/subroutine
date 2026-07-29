"""Documents and links over HTTP.

The last test in this file is the point of the slice: write a specification into the
system, derive tasks from it, and read the relationship back from both ends. That is what
"the roadmap moves into the system" means in practice, and everything else in slice 3 is
machinery for it.
"""

import uuid

import pytest
import sqlalchemy.orm

import subroutine.domain.authentication
import subroutine.domain.projects
import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def test_a_document_is_written_and_read_back (world: test_api_tasks.World) -> None:
	"""The default type is a note, which is the least presumptuous thing to assume."""

	response = world.call(
		"POST", "/v1/documents", json={"title": "How deploys work", "body": "Step one…"}
	)

	assert response.status_code == 201

	body = response.json()

	assert body["title"] == "How deploys work"
	assert body["type"] == "note"
	assert body["status_category"] == "draft"
	assert body["owner_id"] == str(world.user.id)

	assert world.call("GET", f"/v1/documents/{body['ref']}").json()["body"] == "Step one…"


def test_a_document_has_no_deadline_and_cannot_be_given_one (
	world: test_api_tasks.World,
) -> None:
	"""SPEC.md §6.14: a specification is never "done" and nobody is working on it.

	"The spec must be signed off by Friday" is a *task* that documents the spec. Keeping
	dates off the document is what stops every scheduling query needing an entity filter.
	"""

	created = world.call("POST", "/v1/documents", json={"title": "Spec"}).json()

	assert "due_at" not in created
	assert "assignee_id" not in created

	refused = world.call("PATCH", f"/v1/documents/{created['ref']}", json={"due": "tomorrow"})

	assert refused.status_code == 422
	assert refused.json()["code"] == "unknown_field"


def test_documents_and_tasks_share_one_ref_space (world: test_api_tasks.World) -> None:
	"""So ``SR-42`` is unambiguous whichever it names (SPEC.md §5.6)."""

	task = world.call("POST", "/v1/tasks", json={"title": "A task"}).json()
	document = world.call("POST", "/v1/documents", json={"title": "A document"}).json()

	assert task["ref"] != document["ref"]

	# And each endpoint declines the other's ref rather than pretending it has no such row.
	assert world.call("GET", f"/v1/documents/{task['ref']}").status_code == 404
	assert world.call("GET", f"/v1/tasks/{document['ref']}").status_code == 404


def test_superseding_a_document_retires_the_one_it_replaces (
	world: test_api_tasks.World,
) -> None:
	"""The two are one fact: a superseded document still reading as active is a trap."""

	first = world.call(
		"POST", "/v1/documents", json={"title": "Deploy process v1", "status": "active"}
	).json()

	assert first["status_category"] == "current"

	second = world.call(
		"POST", "/v1/documents", json={"title": "Deploy process v2", "supersedes": first["ref"]}
	).json()

	assert second["supersedes_id"] == first["id"]

	retired = world.call("GET", f"/v1/documents/{first['ref']}").json()

	assert retired["status_category"] == "superseded"


def test_a_document_cannot_supersede_itself (world: test_api_tasks.World) -> None:
	"""A one-element cycle is still a cycle."""

	created = world.call("POST", "/v1/documents", json={"title": "Spec"}).json()
	response = world.call(
		"PATCH", f"/v1/documents/{created['ref']}", json={"supersedes": created["ref"]}
	)

	assert response.status_code == 409
	assert response.json()["code"] == "cycle_detected"


def test_an_omitted_field_is_untouched_and_a_null_one_is_cleared (
	world: test_api_tasks.World,
) -> None:
	"""§8.3 again, because it has to hold on every entity or it holds on none."""

	created = world.call(
		"POST", "/v1/documents", json={"title": "Spec", "body": "Original"}
	).json()

	renamed = world.call(
		"PATCH", f"/v1/documents/{created['ref']}", json={"title": "Specification"}
	).json()

	assert renamed["body"] == "Original", "an omitted field must be left alone"

	cleared = world.call("PATCH", f"/v1/documents/{created['ref']}", json={"body": None}).json()

	assert cleared["body"] is None
	assert cleared["title"] == "Specification"


def test_a_document_is_soft_deleted (world: test_api_tasks.World) -> None:
	"""Recoverable, like everything else (SPEC.md §6.9)."""

	created = world.call("POST", "/v1/documents", json={"title": "Throw away"}).json()
	deleted = world.call("DELETE", f"/v1/documents/{created['ref']}")

	assert deleted.status_code == 200
	assert deleted.json()["deleted_at"] is not None
	assert world.call("GET", "/v1/documents").json()["items"] == []


def test_documents_in_a_private_project_are_hidden_like_its_tasks (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A specification is exactly as private as the work derived from it.

	It would be an odd kind of privacy if the plan were readable and only the tasks were not.
	"""

	world = test_api_tasks._world(session)

	world.call(
		"POST", "/v1/projects", json={"key": "SECRET", "title": "Secret", "visibility": "private"}
	)
	hidden = world.call(
		"POST", "/v1/documents", json={"title": "The plan", "project": "SECRET"}
	).json()

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, outsider, role_key="member")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=outsider, title="outsider"
	)
	session.flush()

	nosy = world._replace(secret=issued.value.get_secret_value())

	assert nosy.call("GET", "/v1/documents").json()["items"] == []
	assert nosy.call("GET", f"/v1/documents/{hidden['ref']}").status_code == 404


def test_a_link_reads_the_right_way_round_from_each_end (
	world: test_api_tasks.World,
) -> None:
	"""One stored row, two readings. The link type carries the inverse label (SPEC.md §5.7)."""

	blocker = world.call("POST", "/v1/tasks", json={"title": "Do this first"}).json()
	blocked = world.call("POST", "/v1/tasks", json={"title": "Then this"}).json()

	created = world.call(
		"POST",
		f"/v1/tasks/{blocker['ref']}/links",
		json={"target": blocked["ref"], "link_type": "blocks"},
	)

	assert created.status_code == 201
	assert created.json()["label"] == "Blocks"
	assert created.json()["direction"] == "outgoing"

	from_other_end = world.call("GET", f"/v1/tasks/{blocked['ref']}/links").json()

	assert len(from_other_end) == 1
	assert from_other_end[0]["label"] == "Blocked by"
	assert from_other_end[0]["direction"] == "incoming"
	assert from_other_end[0]["other"]["ref"] == blocker["ref"]


def test_a_symmetric_link_reads_the_same_from_both_ends (
	world: test_api_tasks.World,
) -> None:
	"""``relates_to`` has no inverse, so it must not be given one."""

	one = world.call("POST", "/v1/tasks", json={"title": "One"}).json()
	two = world.call("POST", "/v1/tasks", json={"title": "Two"}).json()

	world.call(
		"POST", f"/v1/tasks/{one['ref']}/links", json={"target": two["ref"], "link_type": "relates_to"}
	)

	assert world.call("GET", f"/v1/tasks/{one['ref']}/links").json()[0]["label"] == "Relates to"
	assert world.call("GET", f"/v1/tasks/{two['ref']}/links").json()[0]["label"] == "Relates to"


def test_linking_twice_is_not_an_error (world: test_api_tasks.World) -> None:
	"""A client retrying a request it is unsure landed should not be punished for it."""

	one = world.call("POST", "/v1/tasks", json={"title": "One"}).json()
	two = world.call("POST", "/v1/tasks", json={"title": "Two"}).json()
	body = {"target": two["ref"], "link_type": "blocks"}

	first = world.call("POST", f"/v1/tasks/{one['ref']}/links", json=body)
	again = world.call("POST", f"/v1/tasks/{one['ref']}/links", json=body)

	assert first.json()["id"] == again.json()["id"]
	assert len(world.call("GET", f"/v1/tasks/{one['ref']}/links").json()) == 1


def test_nothing_can_be_linked_to_itself (world: test_api_tasks.World) -> None:
	"""The database refuses it too; this refuses it in words."""

	task = world.call("POST", "/v1/tasks", json={"title": "Alone"}).json()
	response = world.call(
		"POST", f"/v1/tasks/{task['ref']}/links", json={"target": task["ref"], "link_type": "blocks"}
	)

	assert response.status_code == 422


def test_an_unknown_link_type_names_the_ones_that_exist (
	world: test_api_tasks.World,
) -> None:
	"""Link types are workspace data, so the valid set is read rather than assumed."""

	one = world.call("POST", "/v1/tasks", json={"title": "One"}).json()
	two = world.call("POST", "/v1/tasks", json={"title": "Two"}).json()

	response = world.call(
		"POST", f"/v1/tasks/{one['ref']}/links", json={"target": two["ref"], "link_type": "supersedes"}
	)

	assert response.status_code == 422
	assert "derives_from" in response.json()["errors"][0]["hint"]


def test_a_link_can_be_withdrawn (world: test_api_tasks.World) -> None:
	"""And withdrawing it leaves the items alone."""

	one = world.call("POST", "/v1/tasks", json={"title": "One"}).json()
	two = world.call("POST", "/v1/tasks", json={"title": "Two"}).json()

	link = world.call(
		"POST", f"/v1/tasks/{one['ref']}/links", json={"target": two["ref"], "link_type": "blocks"}
	).json()

	removed = world.call("DELETE", f"/v1/tasks/{one['ref']}/links/{link['id']}")

	assert removed.status_code == 204
	assert world.call("GET", f"/v1/tasks/{one['ref']}/links").json() == []
	assert world.call("GET", f"/v1/tasks/{two['ref']}").status_code == 200


def test_a_link_to_something_invisible_is_not_reported (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A link is only as visible as the thing at the other end of it.

	Reporting "there is a link to something you may not see" would disclose exactly what
	§7.3a's existence rule protects.
	"""

	world = test_api_tasks._world(session)

	public = world.call("POST", "/v1/tasks", json={"title": "Public work"}).json()
	world.call(
		"POST", "/v1/projects", json={"key": "SECRET", "title": "Secret", "visibility": "private"}
	)
	secret = world.call(
		"POST", "/v1/tasks", json={"title": "Secret work", "project": "SECRET"}
	).json()

	world.call(
		"POST",
		f"/v1/tasks/{public['ref']}/links",
		json={"target": secret["ref"], "link_type": "relates_to"},
	)

	assert len(world.call("GET", f"/v1/tasks/{public['ref']}/links").json()) == 1

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, outsider, role_key="member")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=outsider, title="outsider"
	)
	session.flush()

	nosy = world._replace(secret=issued.value.get_secret_value())

	assert nosy.call("GET", f"/v1/tasks/{public['ref']}/links").json() == []


def test_a_specification_can_be_written_and_the_work_derived_from_it (
	world: test_api_tasks.World,
) -> None:
	"""**The capability slice 3 exists for**, end to end.

	Write the plan in as a document, derive a task per item with ``derives_from``, then read
	the relationship back from both ends — from the spec, everything derived from it; from a
	task, the spec it came from. This is what "the roadmap moves into the system" means, and
	the next planning conversation happening against the API rather than a markdown file
	depends on exactly this working.
	"""

	world.call("POST", "/v1/projects", json={"key": "SR", "title": "Subroutine"})

	spec = world.call(
		"POST",
		"/v1/documents",
		json={
			"title": "Slice 4 plan",
			"body": "1. Meta endpoint\n2. Response shaping\n3. Connections",
			"project": "SR",
			"type": "spec",
			"status": "active",
		},
	).json()

	derived = []

	for title in ("Meta endpoint", "Response shaping", "Connections"):
		task = world.call("POST", "/v1/tasks", json={"title": title, "project": "SR"}).json()
		linked = world.call(
			"POST",
			f"/v1/tasks/{task['ref']}/links",
			json={"target": spec["ref"], "target_type": "document", "link_type": "derives_from"},
		)

		assert linked.status_code == 201
		assert linked.json()["label"] == "Derives from"

		derived.append(task["ref"])

	# From the specification: everything that came out of it.
	from_spec = world.call("GET", f"/v1/documents/{spec['ref']}/links").json()

	assert {link["other"]["ref"] for link in from_spec} == set(derived)
	assert {link["direction"] for link in from_spec} == {"incoming"}
	assert {link["label"] for link in from_spec} == {"Derived into"}

	# From a task: the specification it came from.
	from_task = world.call("GET", f"/v1/tasks/{derived[0]}/links").json()

	assert from_task[0]["other"]["ref"] == spec["ref"]
	assert from_task[0]["other"]["entity_type"] == "document"
	assert from_task[0]["label"] == "Derives from"
