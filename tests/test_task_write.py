"""What a credential narrowed to ``task:write`` may do - `SR#4162`, decided on `#373`.

**``task:write`` keeps everything it covers at 1.0** (decision `#4164`), and nothing pinned it.
A change carving finishing or assigning out of it passed the whole suite when it was tried, and
the first to notice would have been a credential that quietly stopped doing what its holder
relied on. Finer control arrives only as narrower verbs added beside it, so if anything here
ever needs another verb, this is where it shows.
"""

import uuid

import sqlalchemy.orm

import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_tasks


def test_a_credential_narrowed_to_task_write_does_the_whole_of_the_work (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Filing, editing, planning, deferring, assigning, claiming, checking, linking, moving,
	finishing, reopening and cancelling work, and writing a document, with ``task:read`` beside it.

	Every request carries the narrowed credential, and each is named in its own assertion so a
	refusal says which act lost its verb.
	"""

	world = test_api_tasks._world(session, scopes=["task:read", "task:write"])
	colleague = subroutine.domain.users.create(
		session, username=f"trinity-{uuid.uuid4().hex[:8]}"
	)
	subroutine.domain.workspaces.add_member(session, world.workspace, colleague, role_key="member")
	session.flush()

	venue = world.call("POST", "/v1/tasks", json={"title": "Book the venue"})
	deposit = world.call("POST", "/v1/tasks", json={"title": "Pay the deposit"})

	assert venue.status_code == 201, ("filing", venue.text)
	assert deposit.status_code == 201, ("filing", deposit.text)

	ref = venue.json()["ref"]
	other = deposit.json()["ref"]
	acts = [
		("editing", "PATCH", f"/v1/tasks/{ref}", {"title": "Book the hall"}),
		("planning", "PATCH", f"/v1/tasks/{ref}", {"starts": "2026-11-02"}),
		("deferring", "PATCH", f"/v1/tasks/{ref}", {"snooze": "2026-11-01"}),
		("assigning", "PATCH", f"/v1/tasks/{ref}", {"assignee": colleague.username}),
		("claiming", "POST", f"/v1/tasks/{ref}/claim", None),
		("releasing", "POST", f"/v1/tasks/{ref}/release", None),
		(
			"checking",
			"POST",
			f"/v1/tasks/{ref}/verifications",
			{"passed": True, "summary": "The hall confirmed by email."},
		),
		("linking", "POST", f"/v1/tasks/{ref}/links", {"target": other, "link_type": "relates_to"}),
		("moving", "POST", f"/v1/tasks/{other}/move", {"parent": str(ref)}),
		("finishing", "POST", f"/v1/tasks/{ref}/complete", None),
		("reopening", "PATCH", f"/v1/tasks/{ref}", {"status": "open"}),
		("cancelling", "PATCH", f"/v1/tasks/{ref}", {"status": "cancelled"}),
		("writing a document", "POST", "/v1/documents", {"title": "Venue options"}),
	]

	for what, method, path, body in acts:
		answered = world.call(method, path, json=body)

		assert answered.is_success, (what, answered.status_code, answered.text)

	finished = world.call("GET", f"/v1/tasks/{ref}").json()

	assert finished["status_category"] == "cancelled", finished
	assert finished["assignee"] is not None, "the assignment did not land"
