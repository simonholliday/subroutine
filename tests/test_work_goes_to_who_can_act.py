"""Work is handed only to somebody who can act on it - `SR#5104`.

M-10 of the cold review of 2026-10-17, against decision `#4509`'s rule that work is handed only to a
member who can see it: a task was assigned, a document given a maintainer and a project handed to
keanu, who had left, and to an agent that had stopped working here, and nobody could act on any of it.
**Each is refused, saying so**, only as a change: a value already held, sent back unchanged as the
browser sends every field, still saves.
"""

import uuid

import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_tasks


def _person (world: test_api_tasks.World, name: str) -> subroutine.db.models.identity.User:
	"""Make a member of the world's workspace."""

	made = subroutine.domain.users.create(world.session, username=f"{name}-{uuid.uuid4().hex[:6]}")
	subroutine.domain.workspaces.add_member(world.session, world.workspace, made, role_key="member")

	return made


def _gone (world: test_api_tasks.World) -> tuple[subroutine.db.models.identity.User, subroutine.db.models.identity.User]:
	"""Return keanu, who has left, and an agent whose person was taken out of the workspace."""

	keanu = _person(world, "keanu")
	trinity = _person(world, "trinity")
	bot = subroutine.domain.users.create(
		world.session,
		username=f"bot-{uuid.uuid4().hex[:6]}",
		is_service_account=True,
		responsible_user_id=trinity.id,
	)
	subroutine.domain.workspaces.add_member(world.session, world.workspace, bot, role_key="member")

	subroutine.domain.users.set_active(world.session, keanu, active=False)
	subroutine.domain.workspaces.remove_member(world.session, world.workspace, trinity)
	world.session.flush()

	return keanu, bot


def test_work_is_not_handed_to_somebody_who_has_left_or_an_agent_that_has_stopped (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The three doors, for each: the refusal says why, on the field that named them."""

	world = test_api_tasks._world(session)
	keanu, bot = _gone(world)

	assert world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"}).status_code == 201

	for account, why in ((keanu, "has left"), (bot, "has stopped working here")):
		for method, path, body, field, what in (
			("POST", "/v1/tasks", {"title": "Fix the sign-up form", "assignee": account.username}, "assignee", "a task there cannot be assigned to them"),
			("POST", "/v1/documents", {"title": "How we ship", "owner_id": str(account.id)}, "owner_id", "they cannot maintain a document there"),
			("PATCH", "/v1/projects/web", {"owner_id": str(account.id)}, "owner_id", "this project cannot be handed to them"),
		):
			refused = world.call(method, path, json=body)

			assert refused.status_code == 422, refused.text
			assert refused.json()["detail"] == f"{account.username} {why}, so {what}.", refused.text
			assert refused.json()["errors"][0]["field"] == field, refused.text
			assert refused.json()["errors"][0]["hint"] == "Choose somebody who can act.", refused.text


def test_a_value_already_held_saves_unchanged (session: sqlalchemy.orm.Session) -> None:
	"""The trap the plain filter fell into: the browser sends the assignee, maintainer and owner it
	shows with every save, so asking about a value already held refused every edit of the item."""

	world = test_api_tasks._world(session)
	keanu = _person(world, "keanu")

	made = world.call("POST", "/v1/projects", json={"key": "web", "title": "Website"})
	task = world.call("POST", "/v1/tasks", json={"title": "Fix the sign-up form", "assignee": keanu.username})
	document = world.call("POST", "/v1/documents", json={"title": "How we ship", "owner_id": str(keanu.id)})

	assert world.call("PATCH", "/v1/projects/web", json={"owner_id": str(keanu.id)}).status_code == 200
	assert made.status_code == task.status_code == document.status_code == 201, (task.text, document.text)

	subroutine.domain.users.set_active(session, keanu, active=False)
	session.flush()

	for path, body in (
		(f"/v1/tasks/{task.json()['id']}", {"title": "Fix the sign-up form today", "assignee": keanu.username}),
		(f"/v1/documents/{document.json()['id']}", {"title": "How we ship it", "owner_id": str(keanu.id)}),
		("/v1/projects/web", {"title": "The website", "owner_id": str(keanu.id)}),
	):
		saved = world.call("PATCH", path, json=body)

		assert saved.status_code == 200, saved.text


def test_the_members_offered_are_those_who_can_act (session: sqlalchemy.orm.Session) -> None:
	"""A name nobody has is answered with who is here: never somebody who would then be refused."""

	world = test_api_tasks._world(session)
	keanu, bot = _gone(world)
	neo = _person(world, "neo")

	refused = world.call("POST", "/v1/tasks", json={"title": "Fix the sign-up form", "assignee": "kenau"})
	hint = refused.json()["errors"][0]["hint"]

	assert refused.status_code == 422, refused.text
	assert neo.username in hint, hint
	assert keanu.username not in hint and bot.username not in hint, hint
