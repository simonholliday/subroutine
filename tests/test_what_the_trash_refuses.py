"""An item in the trash takes no change but its restore - `#4019`, decision `#4096`.

Editing, finishing, skipping, claiming, moving and commenting on one were already refused. These
are the changes that reach it from somewhere else: something filed or moved under it, a check
recorded on it, and a link at either end. Each is refused naming the trash, and what is beneath
an item in the trash is refused naming that item, since decision `#4091` hides it with it.
"""

import typing
import uuid

import pytest
import sqlalchemy

import subroutine.db.models.work
import subroutine.domain.trash
import subroutine.errors
import test_api_tasks

World = test_api_tasks.World
world = test_api_tasks.world


def _made (world: World, path: str, body: dict[str, typing.Any]) -> dict[str, typing.Any]:
	"""Make one thing over HTTP and return it, failing loudly if it was refused."""

	made = world.call("POST", path, json=body)

	assert made.status_code == 201, made.text

	return typing.cast(dict[str, typing.Any], made.json())


def _refused (answer: typing.Any, ref: int) -> None:
	"""Hold that a request was refused because ``ref`` is in the trash, and said so."""

	assert answer.status_code == 422, answer.text
	assert f"#{ref} is in the trash" in answer.json()["detail"], answer.text


def test_nothing_is_filed_under_an_item_in_the_trash (world: World) -> None:
	"""It would be out of sight the moment it was made, beneath the trash (`#4091`)."""

	parent = _made(world, "/v1/tasks", {"title": "Plan the launch party"})
	decision = _made(world, "/v1/documents", {"title": "Why the deploy script moved", "body": "."})

	assert world.call("DELETE", f"/v1/tasks/{parent['ref']}").is_success
	assert world.call("DELETE", f"/v1/documents/{decision['ref']}").is_success

	_refused(
		world.call(
			"POST", "/v1/tasks", json={"title": "Book the venue", "parent_task_id": parent["id"]}
		),
		parent["ref"],
	)
	_refused(
		world.call(
			"POST",
			"/v1/documents",
			json={"title": "What moved with it", "body": ".", "parent": decision["ref"]},
		),
		decision["ref"],
	)


def test_nothing_is_moved_under_an_item_in_the_trash (world: World) -> None:
	"""Moving a live item under one in the trash would hide it as surely as filing it there."""

	parent = _made(world, "/v1/tasks", {"title": "Plan the launch party"})
	venue = _made(world, "/v1/tasks", {"title": "Book the venue"})
	decision = _made(world, "/v1/documents", {"title": "Why the deploy script moved", "body": "."})
	notes = _made(world, "/v1/documents", {"title": "What moved with it", "body": "."})

	assert world.call("DELETE", f"/v1/tasks/{parent['ref']}").is_success
	assert world.call("DELETE", f"/v1/documents/{decision['ref']}").is_success

	_refused(
		world.call("POST", f"/v1/tasks/{venue['ref']}/move", json={"parent": str(parent["ref"])}),
		parent["ref"],
	)
	_refused(
		world.call(
			"POST", f"/v1/documents/{notes['ref']}/move", json={"parent": str(decision["ref"])}
		),
		decision["ref"],
	)


def test_nothing_is_recorded_against_an_item_in_the_trash (world: World) -> None:
	"""A check adds to the item's own record, which the comment rule already closes (`#535`)."""

	fix = _made(world, "/v1/tasks", {"title": "Fix the deploy script"})

	assert world.call("DELETE", f"/v1/tasks/{fix['ref']}").is_success

	_refused(
		world.call("POST", f"/v1/tasks/{fix['ref']}/verifications", json={"passed": True}),
		fix["ref"],
	)


def test_nothing_in_the_trash_is_linked_at_either_end (world: World) -> None:
	"""Either end, and from either item's page, since one stored link can be made from both.

	A link made before the delete is still answered when it is sent again, as `#3798` answers
	every link already there: nothing new is written by it.
	"""

	notes = _made(world, "/v1/tasks", {"title": "Write the release notes"})["ref"]
	party = _made(world, "/v1/tasks", {"title": "Plan the launch party"})["ref"]
	fix = _made(world, "/v1/tasks", {"title": "Fix the deploy script"})["ref"]
	before = {"target": fix, "link_type": "relates_to"}

	assert world.call("POST", f"/v1/tasks/{notes}/links", json=before).status_code == 201
	assert world.call("DELETE", f"/v1/tasks/{fix}").is_success

	for asked_from, body in (
		(party, {"target": fix, "link_type": "blocks"}),
		(fix, {"target": party, "link_type": "blocks"}),
		(party, {"target": fix, "link_type": "blocks", "direction": "incoming"}),
	):
		_refused(world.call("POST", f"/v1/tasks/{asked_from}/links", json=body), fix)

	again = world.call("POST", f"/v1/tasks/{notes}/links", json=before)

	assert again.is_success, again.text


def test_what_is_beneath_the_trash_is_refused_naming_what_is_in_it (world: World) -> None:
	"""Refused with the sentence a lookup of it gives: what it is beneath, and what to restore."""

	parent = _made(world, "/v1/tasks", {"title": "Plan the launch party"})
	venue = _made(world, "/v1/tasks", {"title": "Book the venue", "parent_task_id": parent["id"]})

	assert world.call("DELETE", f"/v1/tasks/{parent['ref']}").is_success

	model = subroutine.db.models.work.Task
	hidden = world.session.scalars(
		sqlalchemy.select(model).where(model.id == uuid.UUID(venue["id"]))
	).one()

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.trash.refuse_reaching(
			world.session, hidden, doing="nothing can be filed under it"
		)

	said = str(refused.value)

	assert f"#{venue['ref']} is beneath #{parent['ref']}, which is in the trash" in said, said
