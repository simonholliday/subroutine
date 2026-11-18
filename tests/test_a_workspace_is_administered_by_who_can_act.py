"""A workspace is administered only by somebody who can act - `SR#5309`, decision `#5285`.

A bullet of L-1 of the cold review of 2026-11-11: gloria, who had left, was made owner with a 200,
and the workspace gained an owner who could do nothing. **Giving either role that administers it to
an account that cannot act is refused**, by moving a member and by adding one, as handing a project
to them already was (`SR#5104`). Somebody returning is reactivated first.
"""

import typing
import uuid

import pytest
import sqlalchemy.orm

import api_support
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.db.models.identity
import subroutine.domain.accountability
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import test_api_tasks

World = test_api_tasks.World
User = subroutine.db.models.identity.User

ADMINISTERING = ("owner", "admin")


def _account (world: World, name: str, *, answers_to: User | None = None) -> User:
	"""Make a person, or an agent answering to ``answers_to``, who belongs to no workspace yet."""

	return subroutine.domain.users.create(
		world.session,
		username=f"{name}-{uuid.uuid4().hex[:6]}",
		is_service_account=answers_to is not None,
		responsible_user_id=None if answers_to is None else answers_to.id,
	)


def _member (world: World, name: str, *, answers_to: User | None = None) -> User:
	"""Make an account and seat it in the world's workspace as a member."""

	made = _account(world, name, answers_to=answers_to)
	subroutine.domain.workspaces.add_member(world.session, world.workspace, made, role_key="member")

	return made


def _seated_and_gone (world: World) -> list[tuple[User, str]]:
	"""Return members who cannot act, each with what is said of them."""

	gloria = _member(world, "gloria")
	trinity = _member(world, "trinity")
	smith = _member(world, "smith", answers_to=trinity)
	subroutine.domain.users.set_active(world.session, gloria, active=False)
	subroutine.domain.workspaces.remove_member(world.session, world.workspace, trinity)
	world.session.flush()

	return [(gloria, "has left"), (smith, "has stopped working here")]


def _arriving_and_gone (world: World) -> list[tuple[User, str]]:
	"""Return accounts outside the workspace who could not act in it, with what is said of them."""

	gloria = _account(world, "gloria")
	morpheus = _account(world, "morpheus")
	stopped = _account(world, "smith", answers_to=morpheus)
	subroutine.domain.users.set_active(world.session, gloria, active=False)
	subroutine.domain.users.set_active(world.session, morpheus, active=False)
	world.session.flush()

	return [(gloria, "has left"), (stopped, "has stopped working here")]


def _said (world: World, account: User, why: str) -> str:
	"""Return the refusal for ``account``."""

	return f"{account.username} {why}, so they cannot administer {world.workspace.slug}."


def _row_of (world: World, account: User) -> dict[str, typing.Any] | None:
	"""Return ``account``'s row in the world's workspace's members listing, if it has one."""

	listed = world.call("GET", f"/v1/workspaces/{world.workspace.slug}/members")

	assert listed.status_code == 200, listed.text

	return next(
		(row for row in listed.json()["items"] if row["user"]["username"] == account.username),
		None,
	)


def _role_of (world: World, account: User) -> str | None:
	"""Return the role ``account`` holds in the world's workspace, as its members listing says."""

	row = _row_of(world, account)

	return None if row is None else row["role"]


def _stopped (world: World, account: User) -> str | None:
	"""Return what the members listing says has stopped ``account``, who must be listed."""

	row = _row_of(world, account)

	assert row is not None, account.username

	stopped: str | None = row["stopped"]

	return stopped


def _assert_refused (answer: typing.Any, detail: str) -> None:
	"""Assert one HTTP refusal: the sentence, the field and what to do."""

	assert answer.status_code == 422, answer.text
	assert answer.json()["detail"] == detail, answer.text
	assert answer.json()["errors"][0]["field"] == "role", answer.text
	assert answer.json()["errors"][0]["hint"] == "Choose somebody who can act.", answer.text


@pytest.mark.parametrize("role", ADMINISTERING)
def test_a_member_who_cannot_act_is_not_moved_into_a_role_that_administers (
	session: sqlalchemy.orm.Session, role: str
) -> None:
	"""gloria, who left, and an agent whose person was taken out; each stays as they were."""

	world = test_api_tasks._world(session)

	for account, why in _seated_and_gone(world):
		path = f"/v1/workspaces/{world.workspace.slug}/members/{account.username}"

		_assert_refused(world.call("PATCH", path, json={"role": role}), _said(world, account, why))

		assert _role_of(world, account) == "member"


@pytest.mark.parametrize("role", ADMINISTERING)
def test_an_account_who_could_not_act_here_is_not_added_with_a_role_that_administers (
	session: sqlalchemy.orm.Session, role: str
) -> None:
	"""Somebody who left, and an agent whose person left."""

	world = test_api_tasks._world(session)

	for account, why in _arriving_and_gone(world):
		answer = world.call(
			"POST",
			f"/v1/workspaces/{world.workspace.slug}/members",
			json={"username": account.username, "role": role},
		)

		_assert_refused(answer, _said(world, account, why))

		assert _role_of(world, account) is None


@pytest.mark.parametrize("role", ADMINISTERING)
def test_the_terminal_refuses_both_the_same_way (
	session: sqlalchemy.orm.Session, role: str
) -> None:
	"""The local client, on the database itself, says what the API says."""

	world = test_api_tasks._world(session)
	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True, local_user=world.user.username),
		session_factory=api_support.factory_for(session),
	)
	(seated, seated_why), *_rest = _seated_and_gone(world)
	(arriving, arriving_why), *_others = _arriving_and_gone(world)
	slug = world.workspace.slug

	with client:
		with pytest.raises(subroutine.errors.ValidationError) as moved:
			client.set_member_role(username=seated.username, role=role, workspace=slug)

		with pytest.raises(subroutine.errors.ValidationError) as added:
			client.add_member(username=arriving.username, role=role, workspace=slug)

	assert moved.value.detail == _said(world, seated, seated_why)
	assert added.value.detail == _said(world, arriving, arriving_why)

	for refused in (moved, added):
		assert refused.value.errors[0].hint == "Choose somebody who can act."


@pytest.mark.parametrize("role", ADMINISTERING)
def test_somebody_who_can_act_is_given_it_on_joining (
	session: sqlalchemy.orm.Session, role: str
) -> None:
	"""Nobody being added is a member yet, and that alone does not stop them."""

	world = test_api_tasks._world(session)
	trinity = _member(world, "trinity")
	session.flush()

	for account in (_account(world, "neo"), _account(world, "sati", answers_to=trinity)):
		answer = world.call(
			"POST",
			f"/v1/workspaces/{world.workspace.slug}/members",
			json={"username": account.username, "role": role},
		)

		assert answer.status_code == 201, answer.text
		assert _role_of(world, account) == role


@pytest.mark.parametrize("role", ADMINISTERING)
def test_an_agent_may_be_added_before_its_person (
	session: sqlalchemy.orm.Session, role: str
) -> None:
	"""Simon, 2026-11-18: listed as stopped until niobe joins, and no longer once they have."""

	world = test_api_tasks._world(session)
	niobe = _account(world, "niobe")
	ghost = _account(world, "ghost", answers_to=niobe)
	session.flush()
	members = f"/v1/workspaces/{world.workspace.slug}/members"
	added = world.call("POST", members, json={"username": ghost.username, "role": role})

	assert added.status_code == 201, added.text
	assert _stopped(world, ghost) == subroutine.domain.accountability.OUTSIDE

	joined = world.call("POST", members, json={"username": niobe.username, "role": "member"})

	assert joined.status_code == 201, joined.text
	assert _stopped(world, ghost) is None


@pytest.mark.parametrize("role", ADMINISTERING)
def test_somebody_reactivated_is_given_it (session: sqlalchemy.orm.Session, role: str) -> None:
	"""What the decision accepted: to promote somebody returning, they are reactivated first."""

	world = test_api_tasks._world(session)
	(gloria, _why), *_rest = _seated_and_gone(world)
	path = f"/v1/workspaces/{world.workspace.slug}/members/{gloria.username}"

	assert world.call("PATCH", path, json={"role": role}).status_code == 422

	subroutine.domain.users.set_active(session, gloria, active=True)
	session.flush()
	moved = world.call("PATCH", path, json={"role": role})

	assert moved.status_code == 200, moved.text
	assert _role_of(world, gloria) == role


def test_a_role_that_does_not_administer_is_unaffected (session: sqlalchemy.orm.Session) -> None:
	"""Each account may still be moved to, or added with, a role that administers nothing."""

	world = test_api_tasks._world(session)
	slug = world.workspace.slug

	for account, _why in _seated_and_gone(world):
		moved = world.call(
			"PATCH", f"/v1/workspaces/{slug}/members/{account.username}", json={"role": "viewer"}
		)

		assert moved.status_code == 200, moved.text
		assert _role_of(world, account) == "viewer"

	for account, _why in _arriving_and_gone(world):
		added = world.call(
			"POST",
			f"/v1/workspaces/{slug}/members",
			json={"username": account.username, "role": "viewer"},
		)

		assert added.status_code == 201, added.text


def test_a_role_already_held_is_not_asked_about_again (session: sqlalchemy.orm.Session) -> None:
	"""An administrator who has since left, sent the role they hold: nothing is given or changed."""

	world = test_api_tasks._world(session)
	gloria = _member(world, "gloria")
	path = f"/v1/workspaces/{world.workspace.slug}/members/{gloria.username}"

	assert world.call("PATCH", path, json={"role": "admin"}).status_code == 200

	subroutine.domain.users.set_active(session, gloria, active=False)
	session.flush()
	again = world.call("PATCH", path, json={"role": "admin"})

	assert again.status_code == 200, again.text
	assert _role_of(world, gloria) == "admin"
