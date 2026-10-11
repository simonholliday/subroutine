"""An item in the trash takes no change but its restore - `#4019`, decision `#4096`.

Editing, finishing, skipping, claiming, moving and commenting on one were already refused. These
are the changes that reach it from somewhere else: something filed or moved under it, a check
recorded on it, and a link at either end. Each is refused naming the trash, and what is beneath
an item in the trash is refused naming that item, since decision `#4091` hides it with it.
"""

import datetime
import typing
import uuid

import pytest
import sqlalchemy

import subroutine.db.models.activity
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.claims
import subroutine.domain.comments
import subroutine.domain.tasks
import subroutine.domain.trash
import subroutine.domain.verifications
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


def test_the_trash_takes_back_only_what_somebody_attached (world: World) -> None:
	"""Q10 of the cold review of 2026-10-05, `SR#4548`, as `SR#4946` left it: words and links.

	Editing or deleting a comment on a trashed task and removing a link from it each take back
	something somebody attached and leave the task as it is, so each is answered as it would be on a
	live one. An edit, a claim, giving a lease back and a new comment are each refused naming the
	trash: the lease went back as the task went in, so there is none to give.
	"""

	fix = _made(world, "/v1/tasks", {"title": "Fix the deploy script"})
	notes = _made(world, "/v1/tasks", {"title": "Write the release notes"})
	started = _made(world, f"/v1/tasks/{fix['ref']}/comments", {"body": "Started on staging."})
	waiting = _made(world, f"/v1/tasks/{fix['ref']}/comments", {"body": "Needs the new key."})
	link = _made(
		world, f"/v1/tasks/{fix['ref']}/links", {"target": notes["ref"], "link_type": "blocks"}
	)

	assert world.call("POST", f"/v1/tasks/{fix['ref']}/claim").is_success
	assert world.call("DELETE", f"/v1/tasks/{fix['ref']}").is_success

	for withdrawn in (
		world.call("PATCH", f"/v1/comments/{started['id']}", json={"body": "Started, then not."}),
		world.call("DELETE", f"/v1/comments/{waiting['id']}"),
		world.call("DELETE", f"/v1/tasks/{fix['ref']}/links/{link['id']}"),
	):
		assert withdrawn.is_success, withdrawn.text

	_refused(
		world.call("PATCH", f"/v1/tasks/{fix['ref']}", json={"title": "Fix it properly"}),
		fix["ref"],
	)
	_refused(world.call("POST", f"/v1/tasks/{fix['ref']}/claim"), fix["ref"])
	_refused(world.call("POST", f"/v1/tasks/{fix['ref']}/release"), fix["ref"])
	_refused(
		world.call("POST", f"/v1/tasks/{fix['ref']}/comments", json={"body": "One more."}),
		fix["ref"],
	)


def test_the_domain_itself_refuses_a_write_beneath_the_trash (world: World) -> None:
	"""`SR#4548`: the permission check holds what is beneath the trash for any caller of the domain.

	A lookup hides it, so no transport reaches it to write. A direct call did: it changed the task,
	claimed it, and was told *there is no task here* when it commented, where every transport names
	the item in the trash. Each now says where it is.
	"""

	parent = _made(world, "/v1/tasks", {"title": "Plan the launch party"})
	venue = _made(world, "/v1/tasks", {"title": "Book the venue", "parent_task_id": parent["id"]})

	assert world.call("DELETE", f"/v1/tasks/{parent['ref']}").is_success

	actor = subroutine.domain.authentication.Principal(user=world.user)
	hidden = world.session.get(subroutine.db.models.work.Task, uuid.UUID(venue["id"]))
	beneath = f"#{venue['ref']} is beneath #{parent['ref']}, which is in the trash"

	assert hidden is not None

	with pytest.raises(subroutine.errors.ValidationError) as changed:
		subroutine.domain.tasks.update(world.session, hidden, title="Book a bigger venue", actor=actor)

	with pytest.raises(subroutine.errors.ValidationError) as claimed:
		subroutine.domain.claims.claim(world.session, hidden, actor=actor)

	with pytest.raises(subroutine.errors.ValidationError) as checked:
		subroutine.domain.verifications.record(world.session, hidden, passed=True, actor=actor)

	with pytest.raises(subroutine.errors.ValidationError) as commented:
		subroutine.domain.comments.create(
			world.session, entity_type="task", entity_id=hidden.id, body="Booked.", actor=actor
		)

	for refused in (changed, claimed, checked, commented):
		assert beneath in str(refused.value), str(refused.value)


def test_a_comment_on_work_beneath_the_trash_says_where_the_work_is (world: World) -> None:
	"""`SR#4653`: changing or deleting it is refused naming the container, as every door says it.

	The work is hidden with what it is beneath (`#4091`), and nothing is done through it until that
	is restored, withdrawals included (Simon, 2026-10-06). The refusal said *There is no task here
	to comment on*, which was false: the task is there. A link to it is still removed from its live
	end (`#4429`).
	"""

	parent = _made(world, "/v1/tasks", {"title": "Plan the launch party"})
	venue = _made(world, "/v1/tasks", {"title": "Book the venue", "parent_task_id": parent["id"]})
	notes = _made(world, "/v1/tasks", {"title": "Write the release notes"})
	said = _made(world, f"/v1/tasks/{venue['ref']}/comments", {"body": "Asked two places."})
	link = _made(
		world, f"/v1/tasks/{venue['ref']}/links", {"target": notes["ref"], "link_type": "blocks"}
	)

	assert world.call("DELETE", f"/v1/tasks/{parent['ref']}").is_success

	beneath = f"#{venue['ref']} is beneath #{parent['ref']}, which is in the trash."

	for refused in (
		world.call("PATCH", f"/v1/comments/{said['id']}", json={"body": "Asked three."}),
		world.call("DELETE", f"/v1/comments/{said['id']}"),
	):
		assert refused.status_code == 422, refused.text
		assert refused.json()["detail"] == beneath, refused.text

	# **And a caller of the domain that already holds the comment**, which asks the same of its item.
	written = world.session.get(subroutine.db.models.activity.Comment, uuid.UUID(said["id"]))
	actor = subroutine.domain.authentication.Principal(user=world.user)

	assert written is not None

	with pytest.raises(subroutine.errors.ValidationError) as changed:
		subroutine.domain.comments.update(world.session, written, body="Asked three.", actor=actor)

	assert str(changed.value) == beneath, str(changed.value)
	assert world.call("DELETE", f"/v1/tasks/{notes['ref']}/links/{link['id']}").status_code == 204


def _row (world: World, made: dict[str, typing.Any]) -> subroutine.db.models.work.Task:
	"""Return a task's row as the database has it now, trash included."""

	world.session.expire_all()
	model = subroutine.db.models.work.Task

	return world.session.scalars(
		sqlalchemy.select(model).where(model.id == uuid.UUID(made["id"]))
	).one()


def _released (world: World, made: dict[str, typing.Any]) -> int:
	"""Return how many times the record says a lease on this task was given back."""

	event = subroutine.db.models.activity.Event

	return len(
		world.session.scalars(
			sqlalchemy.select(event.seq).where(
				event.entity_id == uuid.UUID(made["id"]), event.action == "released"
			)
		).all()
	)


def test_putting_a_task_in_the_trash_gives_its_claim_back (world: World) -> None:
	"""`SR#4946`, decision `#4943`: nobody works on what is in the trash, so nobody holds it.

	Finishing a task already gave its lease back (`SR#1113`). Trashing one left the holder's name on
	it, and giving the lease back was one of three things the trash let through. **Given back as it
	goes in, recorded once, and the task is still nobody's when it comes back.**
	"""

	fix = _made(world, "/v1/tasks", {"title": "Fix the deploy script"})

	assert world.call("POST", f"/v1/tasks/{fix['ref']}/claim").is_success
	assert _row(world, fix).claimed_by_id == world.user.id, "the claim this rests on was not taken"
	assert world.call("DELETE", f"/v1/tasks/{fix['ref']}").is_success

	trashed = _row(world, fix)

	assert trashed.deleted_at is not None
	assert (trashed.claimed_by_id, trashed.claimed_at, trashed.claim_expires_at) == (None, None, None)
	assert _released(world, fix) == 1, "the record does not say the lease went back, or says it twice"

	# **Deleting twice is not an error and records nothing more.**
	assert world.call("DELETE", f"/v1/tasks/{fix['ref']}").is_success
	assert _released(world, fix) == 1

	restored = world.call("POST", f"/v1/tasks/{fix['ref']}/restore")

	assert restored.is_success, restored.text
	assert restored.json()["claimed_by_id"] is None, "a restored task came back held"
	assert _released(world, fix) == 1, "restoring recorded a lease nobody held"


def test_a_task_nobody_holds_goes_in_the_trash_with_nothing_said_of_a_lease (world: World) -> None:
	"""The other half: no lease, so no record of one given back."""

	fix = _made(world, "/v1/tasks", {"title": "Fix the deploy script"})

	assert world.call("DELETE", f"/v1/tasks/{fix['ref']}").is_success
	assert _released(world, fix) == 0


def test_a_lease_left_in_the_trash_from_before_goes_as_the_task_comes_back (world: World) -> None:
	"""`SR#4946`: a lease taken before trashing gave one back, and still running when it is restored.

	Written onto the row as an older build left it. It cannot be given back while it is there, as
	nothing in the trash changes, and **a restored task is held by nobody**.
	"""

	fix = _made(world, "/v1/tasks", {"title": "Fix the deploy script"})

	assert world.call("DELETE", f"/v1/tasks/{fix['ref']}").is_success

	left = _row(world, fix)
	left.claimed_by_id = world.user.id
	left.claimed_at = subroutine.db.types.utcnow()
	left.claim_expires_at = subroutine.db.types.utcnow() + datetime.timedelta(hours=1)
	world.session.flush()

	_refused(world.call("POST", f"/v1/tasks/{fix['ref']}/release"), fix["ref"])

	restored = world.call("POST", f"/v1/tasks/{fix['ref']}/restore")

	assert restored.is_success, restored.text
	assert restored.json()["claimed_by_id"] is None, "a restored task came back held"
	assert _released(world, fix) == 1, "the record does not say the lease went back"


def test_trashing_a_held_task_asks_for_no_more_than_trashing (session: sqlalchemy.orm.Session) -> None:
	"""`SR#4946`: a credential holding ``task:delete`` and not ``task:write`` still trashes it.

	Giving a lease back by hand takes ``task:write``. Giving it back because the task went in the
	trash is part of trashing, so it asks nothing of its own, and the lease still goes.
	"""

	narrow = test_api_tasks._world(session, scopes=["task:read", "task:delete"])
	whole = subroutine.domain.authentication.Principal(user=narrow.user)
	project = subroutine.domain.bootstrap.inbox_for(narrow.session, narrow.workspace)

	assert project is not None

	task = subroutine.domain.tasks.create(
		narrow.session, project=project, title="Fix the deploy script", actor=whole
	)
	subroutine.domain.claims.claim(narrow.session, task, actor=whole)
	narrow.session.flush()

	assert task.claimed_by_id == narrow.user.id

	refused = narrow.call("POST", f"/v1/tasks/{task.ref}/release")

	assert refused.status_code == 403, f"this credential should not hold task:write: {refused.text}"

	trashed = narrow.call("DELETE", f"/v1/tasks/{task.ref}")

	assert trashed.is_success, trashed.text

	narrow.session.expire_all()

	assert task.deleted_at is not None and task.claimed_by_id is None
