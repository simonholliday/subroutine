"""What is beneath a deleted item leaves sight with it and comes back with it - `#3921`.

Decision `#4091`: deleting a project, a task or a document hides everything beneath it rather
than deleting that too. Only what somebody deleted is in the trash. What is beneath it is not
listed, not ready, not blocking and not counted while its container is in the trash, and comes
back as it was when the container is restored. Asked for by number, a hidden item says where it
is, on both transports.
"""

import typing

import pytest

import subroutine.domain.selection
import subroutine.errors
import test_api_tasks
import test_authorization
import test_transport_equivalence

World = test_api_tasks.World
world = test_api_tasks.world
pair = test_transport_equivalence.pair
Pair = test_transport_equivalence.Pair


def _made (world: World, path: str, body: dict[str, typing.Any]) -> dict[str, typing.Any]:
	"""Make one thing over HTTP and return it, failing loudly if it was refused."""

	made = world.call("POST", path, json=body)

	assert made.status_code == 201, made.text

	return typing.cast(dict[str, typing.Any], made.json())


def _tasks (world: World, query: str = "") -> set[int]:
	"""Return the refs a task listing answers with."""

	return {
		item["ref"] for item in world.call("GET", f"/v1/tasks?limit=200{query}").json()["items"]
	}


def test_what_is_in_a_project_beneath_a_deleted_one_leaves_with_it_and_comes_back (
	world: World,
) -> None:
	"""M-7's project half: two projects down was still listed, ready and blocking.

	Every listing tested the project an item is filed in and never one above it, so deleting
	``outer`` left ``outer/inner`` in the project list and its tasks listed, offered as ready, and
	still holding up work outside it.
	"""

	_made(world, "/v1/projects", {"key": "outer", "title": "Outer"})
	_made(world, "/v1/projects", {"key": "inner", "title": "Inner", "parent": "outer"})
	deep = _made(world, "/v1/tasks", {"title": "Filed deep", "project": "outer/inner"})["ref"]
	blocker = _made(world, "/v1/tasks", {"title": "Blocker deep", "project": "outer/inner"})["ref"]
	waiting = _made(world, "/v1/tasks", {"title": "Waiting on it"})["ref"]
	test_api_tasks._blocking(world, blocker, waiting)

	assert waiting not in _tasks(world, "&ready=true"), "the blocker holds it up before the delete"
	assert world.call("DELETE", "/v1/projects/outer").is_success

	keys = {item["key"] for item in world.call("GET", "/v1/projects").json()["items"]}

	assert not {"outer", "inner"} & keys, keys
	assert not {deep, blocker} & _tasks(world)
	assert not {deep, blocker} & _tasks(world, "&ready=true")
	assert waiting in _tasks(world, "&ready=true"), "a blocker out of sight still held it up"
	assert world.call("POST", "/v1/projects/outer/restore").is_success
	assert {deep, blocker} <= _tasks(world)
	assert waiting not in _tasks(world, "&ready=true"), "the blocker came back and holds it again"


def test_what_is_beneath_a_deleted_task_leaves_with_it_and_comes_back (world: World) -> None:
	"""M-7's task half, decided as hiding rather than a cascade (`#4091`).

	Only the task somebody deleted is in the trash: what is beneath it was not deleted, so it is not
	listed there either, and it comes back without being restored one by one.
	"""

	parent = _made(world, "/v1/tasks", {"title": "Plan the launch party"})
	venue = _made(world, "/v1/tasks", {"title": "Book the venue", "parent_task_id": parent["id"]})
	child = venue["ref"]
	# **Two levels down**, because an ancestor is found by the length of its path, and a test whose
	# trashed ancestor was always a root could not tell the second length from the first.
	deeper = _made(
		world, "/v1/tasks", {"title": "Ask Gloria about the vase", "parent_task_id": venue["id"]}
	)["ref"]
	blocker = _made(
		world, "/v1/tasks", {"title": "Order the balloons", "parent_task_id": parent["id"]}
	)["ref"]
	waiting = _made(world, "/v1/tasks", {"title": "Collect the package from reception"})["ref"]
	test_api_tasks._blocking(world, blocker, waiting)

	assert world.call("DELETE", f"/v1/tasks/{parent['ref']}").is_success
	assert not {child, deeper, blocker} & _tasks(world)
	assert waiting in _tasks(world, "&ready=true"), "a blocker out of sight still held it up"
	assert _tasks(world, "&deleted=true") == {parent["ref"]}, "only what somebody deleted"
	assert world.call("POST", f"/v1/tasks/{parent['ref']}/restore").is_success
	assert {child, deeper, blocker} <= _tasks(world)
	assert waiting not in _tasks(world, "&ready=true")

	# And the middle one, so the ancestor in the trash is found at the second length of path.
	assert world.call("DELETE", f"/v1/tasks/{child}").is_success
	assert deeper not in _tasks(world) and blocker in _tasks(world)
	assert world.call("POST", f"/v1/tasks/{child}/restore").is_success
	assert deeper in _tasks(world)


def test_what_is_beneath_a_deleted_document_leaves_with_it_and_comes_back (world: World) -> None:
	"""Documents nest as tasks do, and were left out of M-7 only because nobody had tried one."""

	parent = _made(
		world, "/v1/documents", {"title": "Why the deploy script moved", "body": "It moved."}
	)
	child = _made(
		world,
		"/v1/documents",
		{"title": "What moved with it", "body": "The cron line.", "parent": parent["ref"]},
	)["ref"]

	def listed () -> set[int]:
		"""Return the refs the document listing answers with."""

		return {
			item["ref"] for item in world.call("GET", "/v1/documents?limit=200").json()["items"]
		}

	assert child in listed()
	assert world.call("DELETE", f"/v1/documents/{parent['ref']}").is_success
	assert child not in listed()
	assert world.call("POST", f"/v1/documents/{parent['ref']}/restore").is_success
	assert child in listed()


def test_a_hidden_item_asked_for_by_number_says_where_it_is (pair: Pair) -> None:
	"""Not *There is no #N*, which sent its reader to a trash that does not list it (`#4091`).

	On both transports, because the terminal reads a miss through the client, and a client that
	answered *nothing here* would have dropped the sentence on its way.
	"""

	local = pair.local
	parent = local.capture(text="Plan the launch party").task
	child = local.capture(text="Book the venue", parent=parent.ref).task
	decision = local.create_document(title="Why the deploy script moved", body="It moved.")
	beneath = local.create_document(title="What moved with it", body="Lines.", parent=decision.ref)
	local.create_project(key="outer", title="Outer")
	local.create_project(key="inner", title="Inner", parent="outer")
	deep = local.capture(text="Filed deep", project="outer/inner").task

	local.discard(ref=parent.ref)
	local.discard(ref=decision.ref, entity_type="document")
	pair.remote.call_api(method="DELETE", path="/v1/projects/outer")

	asked = (
		("task", child.ref, f"#{parent.ref}"),
		("document", beneath.ref, f"#{decision.ref}"),
		("task", deep.ref, "outer"),
	)

	for client in pair.both():
		for kind, ref, container in asked:
			with pytest.raises(subroutine.errors.ValidationError) as caught:
				if kind == "task":
					client.task(ref=ref)

				else:
					client.document(ref=ref)

			said = str(caught.value)

			assert container in said and "in the trash" in said, (kind, ref, said)

	answered = pair.remote.call_api(method="GET", path=f"/v1/tasks/{child.ref}")

	assert answered.status == 422 and f"#{parent.ref}" in answered.text, answered.text


def test_an_item_beneath_the_trash_is_not_restored_alone (world: World) -> None:
	"""Restoring it would clear one ``deleted_at`` and change nothing anybody could see.

	So it is refused by name, as ``projects.restore`` refuses a project under a deleted one, and
	restoring what it is beneath brings it within reach again.
	"""

	parent = _made(world, "/v1/tasks", {"title": "Plan the launch party"})
	child = _made(
		world, "/v1/tasks", {"title": "Book the venue", "parent_task_id": parent["id"]}
	)["ref"]

	assert world.call("DELETE", f"/v1/tasks/{child}").is_success
	assert world.call("DELETE", f"/v1/tasks/{parent['ref']}").is_success

	refused = world.call("POST", f"/v1/tasks/{child}/restore")

	assert refused.status_code == 422 and f"#{parent['ref']}" in refused.json()["detail"], (
		refused.text
	)
	assert _tasks(world, "&deleted=true") == {parent["ref"]}, "it is beneath the trash, not in it"
	assert world.call("POST", f"/v1/tasks/{parent['ref']}/restore").is_success
	assert _tasks(world, "&deleted=true") == {child}, "deleted on its own, so still in the trash"
	assert world.call("POST", f"/v1/tasks/{child}/restore").is_success


def test_a_milestone_does_not_count_what_is_beneath_the_trash (world: World) -> None:
	"""A milestone's count reads the same rule as a listing, as decision `#4091` says."""

	milestone = _made(world, "/v1/tasks", {"title": "Ship the site", "type": "milestone"})
	parent = _made(world, "/v1/tasks", {"title": "Rewrite the home page copy"})
	part = _made(
		world, "/v1/tasks", {"title": "Write the release notes", "parent_task_id": parent["id"]}
	)
	linked = world.call(
		"POST",
		f"/v1/tasks/{milestone['ref']}/links",
		json={"link_type": "includes", "target": str(part["ref"])},
	)

	assert linked.status_code == 201, linked.text

	def counted () -> int:
		"""Return how much the milestone says it includes."""

		read = world.call("GET", f"/v1/tasks/{milestone['ref']}").json()

		return typing.cast(int, read["included_count"])

	assert counted() == 1
	assert world.call("DELETE", f"/v1/tasks/{parent['ref']}").is_success
	assert counted() == 0
	assert world.call("POST", f"/v1/tasks/{parent['ref']}/restore").is_success
	assert counted() == 1


def test_somebody_outside_a_private_project_is_told_nothing_of_what_it_holds (
	world: World,
) -> None:
	"""Saying where a hidden item is must not say what a private project holds."""

	_made(world, "/v1/projects", {"key": "ops", "title": "Operations", "visibility": "private"})
	parent = _made(world, "/v1/tasks", {"title": "Fix the leak on the third floor", "project": "ops"})
	child = _made(
		world, "/v1/tasks", {"title": "Call the plumber", "parent_task_id": parent["id"]}
	)["ref"]

	assert world.call("DELETE", f"/v1/tasks/{parent['ref']}").is_success

	colleague = test_authorization._member(world.session, world.workspace, "member")

	with pytest.raises(subroutine.errors.NotFound):
		subroutine.domain.selection.task(world.session, colleague, world.workspace, str(child))
