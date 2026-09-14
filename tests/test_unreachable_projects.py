"""A private project nobody can see any more, and the way back in - item `#1453`.

**A private project is visible only to its members** (§7.3a), and not even a superuser reads past
that: `#1418` settled that membership is reach. So when the last member is deactivated - or the
only member is an agent whose person is - nobody can see the project, and nothing can make it
public or share it again. The only way back was to act as the person who left.

**Simon's decision, 2026-09-14, is `#1418`'s answer one level down.** An administrator may
*discover* a private project nobody can reach and *let somebody into* it, and doing so is
recorded. And deactivating somebody names, first, what it would leave unreachable.

Every test names at least two principals, because one account cannot tell *what I can see* from
*what anybody can see*.
"""

import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.accountability
import subroutine.domain.authentication
import subroutine.domain.projects
import subroutine.domain.users
import subroutine.domain.workspaces
import test_api_tasks


def _somebody (
	session: sqlalchemy.orm.Session, world: test_api_tasks.World, *, name: str = "thomas"
) -> tuple[subroutine.db.models.identity.User, test_api_tasks.World]:
	"""Add an ordinary person to the workspace, and a caller for them."""

	account = subroutine.domain.users.create(session, username=f"{name}-{uuid.uuid4().hex[:6]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, account, role_key="admin")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=account, title=f"{name}'s"
	)
	session.flush()

	return account, world._replace(secret=issued.value.get_secret_value())


def _private (
	session: sqlalchemy.orm.Session,
	world: test_api_tasks.World,
	owner: subroutine.db.models.identity.User,
	*,
	key: str = "secret",
) -> subroutine.db.models.project.Project:
	"""Make a private project whose only member is its owner."""

	project = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key=key,
		title="Redundancies",
		visibility="private",
		owner_id=owner.id,
	)
	session.flush()

	return project


def _listed (world: test_api_tasks.World, **query: str) -> list[dict[str, object]]:
	"""Ask the installation which private projects nobody can reach."""

	answer = world.call("GET", "/v1/instance/unreachable-projects", params=query)

	assert answer.status_code == 200, answer.text

	items: list[dict[str, object]] = answer.json()["items"]

	return items


def test_a_project_whose_last_member_left_is_found_by_an_administrator (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The defect: deactivate the only member and the project is invisible to every surface."""

	world = test_api_tasks._world(session)
	thomas, _theirs = _somebody(session, world)
	project = _private(session, world, thomas)

	assert _listed(world) == [], "listed while its member could still see it"

	subroutine.domain.users.set_active(session, thomas, active=False)
	session.flush()

	found = _listed(world)

	assert [row["project"] for row in found] == [project.key]
	assert found[0]["workspace"] == world.workspace.slug
	assert found[0]["members"] == 1

	# **Nothing from inside it**: what exists and where, and no description, settings or work.
	assert set(found[0]) == {"id", "workspace", "project", "title", "created_at", "members"}


def test_an_agent_whose_person_left_reaches_nothing_either (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The second door: an agent keeps its rows, and stops acting when its person leaves."""

	world = test_api_tasks._world(session)
	thomas, _theirs = _somebody(session, world)
	agent = subroutine.domain.users.create(
		session,
		username=f"bot-{uuid.uuid4().hex[:6]}",
		is_service_account=True,
		responsible_user_id=thomas.id,
	)
	subroutine.domain.workspaces.add_member(session, world.workspace, agent, role_key="admin")
	project = _private(session, world, agent)

	assert _listed(world) == []

	subroutine.domain.users.set_active(session, thomas, active=False)
	session.flush()

	assert [row["project"] for row in _listed(world)] == [project.key], (
		"an agent whose person has left was counted as somebody who can see it"
	)


def test_leaving_names_what_a_departure_would_strand_before_it_happens (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The warning half: asked before deactivating, it names what nobody could see afterwards.

	**Only what this departure strands**, so a project already unreachable, or one another member
	can still see, is not blamed on it.
	"""

	world = test_api_tasks._world(session)
	thomas, _theirs = _somebody(session, world)
	jo, _hers = _somebody(session, world, name="jo")
	gone, _gones = _somebody(session, world, name="gone")
	alone = _private(session, world, thomas, key="alone")
	shared = _private(session, world, thomas, key="shared")
	already = _private(session, world, gone, key="already")
	subroutine.domain.projects.share(session, shared, jo)
	subroutine.domain.users.set_active(session, gone, active=False)
	session.flush()

	stranding = _listed(world, leaving=thomas.username)

	assert [row["project"] for row in stranding] == [alone.key], (
		"a project jo can still see, or one nobody could see before, was named as stranded by "
		"thomas leaving - or the one only thomas sees was not"
	)
	assert [row["project"] for row in _listed(world)] == [already.key], (
		"and only the project that was stranded before is unreachable yet"
	)


def test_only_an_unpinned_administrator_may_ask (session: sqlalchemy.orm.Session) -> None:
	"""`instance:admin`, and a credential that answers for the whole installation."""

	world = test_api_tasks._world(session)
	_thomas, theirs = _somebody(session, world)

	assert theirs.call("GET", "/v1/instance/unreachable-projects").status_code == 403

	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=world.user, title="pinned", workspace_id=world.workspace.id
	)
	session.flush()

	pinned = world._replace(secret=issued.value.get_secret_value())
	refused = pinned.call("GET", "/v1/instance/unreachable-projects")

	assert refused.status_code == 403
	assert "pinned" in refused.text, refused.text


def test_an_administrator_can_let_somebody_back_in_and_it_is_recorded (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The way back: naming is not reading, joining is recorded, and then the project is visible."""

	world = test_api_tasks._world(session)
	thomas, _theirs = _somebody(session, world)
	project = _private(session, world, thomas)

	subroutine.domain.users.set_active(session, thomas, active=False)
	session.flush()

	assert world.call("GET", f"/v1/projects/{project.key}").status_code == 404, (
		"an administrator read a private project nobody let them into"
	)

	joined = world.call(
		"POST", f"/v1/projects/{project.key}/members", json={"username": world.user.username}
	)

	assert joined.status_code == 201, joined.text
	assert world.call("GET", f"/v1/projects/{project.key}").status_code == 200
	assert _listed(world) == [], "and it is reachable again"

	recorded = session.scalars(
		sqlalchemy.select(subroutine.db.models.activity.Event).where(
			subroutine.db.models.activity.Event.entity_type == "project_member",
			subroutine.db.models.activity.Event.subject_id == project.id,
		)
	).all()

	assert [event.actor_user_id for event in recorded][-1:] == [world.user.id], (
		"letting somebody into a project nobody could see was not recorded against who did it"
	)


def test_a_project_somebody_can_still_see_is_not_an_administrators_to_share (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The limit of the widening: while a member can see it, it stays theirs to disclose."""

	world = test_api_tasks._world(session)
	thomas, _theirs = _somebody(session, world)
	project = _private(session, world, thomas)

	refused = world.call(
		"POST", f"/v1/projects/{project.key}/members", json={"username": world.user.username}
	)

	assert refused.status_code == 404, refused.text


def test_authentication_and_the_listing_agree_about_who_can_act (
	session: sqlalchemy.orm.Session,
) -> None:
	"""One rule, two readers: an agent refused at the door is an agent that reaches nothing."""

	world = test_api_tasks._world(session)
	thomas, _theirs = _somebody(session, world)
	agent = subroutine.domain.users.create(
		session,
		username=f"bot-{uuid.uuid4().hex[:6]}",
		is_service_account=True,
		responsible_user_id=thomas.id,
	)
	_row, issued = subroutine.domain.authentication.issue_token(session, user=agent, title="bot")
	session.flush()

	robot = world._replace(secret=issued.value.get_secret_value())

	assert robot.call("GET", "/v1/me").status_code == 200

	subroutine.domain.users.set_active(session, thomas, active=False)
	session.flush()

	assert robot.call("GET", "/v1/me").status_code == 401
	assert not subroutine.domain.accountability.can_act(session, agent)
