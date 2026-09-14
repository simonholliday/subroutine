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

import typing
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


def _narrowed (
	session: sqlalchemy.orm.Session, world: test_api_tasks.World, **narrowing: typing.Any
) -> test_api_tasks.World:
	"""A caller for the same administrator, holding a credential narrowed as given."""

	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=world.user, title="narrowed", **narrowing
	)
	session.flush()

	return world._replace(secret=issued.value.get_secret_value())


def _members (
	session: sqlalchemy.orm.Session, project: subroutine.db.models.project.Project
) -> set[uuid.UUID]:
	"""Return who holds a membership of this project."""

	model = subroutine.db.models.project.ProjectMember

	return set(session.scalars(sqlalchemy.select(model.user_id).where(model.project_id == project.id)))


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


def test_a_credential_narrowed_to_some_projects_may_not_ask_about_all_of_them (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#2619`: the listing answers for every project, so a credential narrowed to some is refused.

	**Narrowed either way.** ``project_scope`` says which projects a credential reaches and
	``project_write_scope`` where it may change anything (`#371`), and an administrator's token
	carries either as readily as a pin, still holding ``instance:admin``. Refused by name, as the
	pin is, so ``user deactivate`` says its check did not run rather than reading silence as
	*nothing stranded*.
	"""

	world = test_api_tasks._world(session)
	thomas, _theirs = _somebody(session, world)
	_private(session, world, thomas)
	elsewhere = subroutine.domain.projects.create(
		session, workspace_id=world.workspace.id, key="elsewhere", title="Elsewhere"
	)
	subroutine.domain.users.set_active(session, thomas, active=False)
	session.flush()

	assert len(_listed(world)) == 1

	for narrowing in ("project_scope", "project_write_scope"):
		caller = _narrowed(session, world, **{narrowing: [str(elsewhere.id)]})
		refused = caller.call("GET", "/v1/instance/unreachable-projects")

		assert refused.status_code == 403, (narrowing, refused.text)
		assert "narrowed to some projects" in refused.text, refused.text


def test_only_a_credential_that_reaches_every_project_can_let_somebody_back_in (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#2619`: the share skips its project check for a rescue, so nothing else applies a narrowing.

	The review's reproduction, one narrowing at a time: the same administrator's credential pinned
	to the workspace, narrowed to another project, and narrowed in where it may change anything.
	Each is told the project is not there - the answer a project outside what a credential reaches
	gets everywhere else - and nobody is let in.
	"""

	world = test_api_tasks._world(session)
	thomas, _theirs = _somebody(session, world)
	project = _private(session, world, thomas)
	elsewhere = subroutine.domain.projects.create(
		session, workspace_id=world.workspace.id, key="elsewhere", title="Elsewhere"
	)
	subroutine.domain.users.set_active(session, thomas, active=False)
	session.flush()

	for narrowing in (
		{"workspace_id": world.workspace.id},
		{"project_scope": [str(elsewhere.id)]},
		{"project_write_scope": [str(elsewhere.id)]},
	):
		refused = _narrowed(session, world, **narrowing).call(
			"POST", f"/v1/projects/{project.key}/members", json={"username": world.user.username}
		)

		assert refused.status_code == 404, (narrowing, refused.text)

	assert _members(session, project) == {thomas.id}, (
		"a narrowed credential let somebody into a project outside what it reaches"
	)


def test_the_address_the_listing_gives_reaches_that_project_and_not_a_namesake (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#2620`: a stranded root's address is its bare key, and a namesake elsewhere must not answer.

	The rescue asked the ordinary resolver first, which searches by name among the projects the
	caller can see - and a stranded project is exactly the one it cannot. So the administrator's
	own ``other/dist`` answered to ``dist``, the address the listing had just given for the root:
	the wrong project gained a member, the stranded one stayed stranded, and it said so as success.
	"""

	world = test_api_tasks._world(session)
	thomas, _theirs = _somebody(session, world)
	jo, _hers = _somebody(session, world, name="jo")
	stranded = _private(session, world, thomas, key="dist")
	other = subroutine.domain.projects.create(
		session, workspace_id=world.workspace.id, key="other", title="Other"
	)
	namesake = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="dist",
		title="Theirs",
		parent=other,
		visibility="private",
		owner_id=world.user.id,
	)
	subroutine.domain.users.set_active(session, thomas, active=False)
	session.flush()

	assert [row["project"] for row in _listed(world)] == ["dist"]

	shared = world.call("POST", "/v1/projects/dist/members", json={"username": jo.username})

	assert shared.status_code == 201, shared.text
	assert jo.id in _members(session, stranded), "the project the listing named gained nobody"
	assert jo.id not in _members(session, namesake), "a namesake the administrator sees did"
	assert _listed(world) == []


def test_two_stranded_projects_of_one_name_are_each_reached_by_their_own_address (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#2620`: an address resolves exactly, so the root is ``dist`` and the other ``sub/dist``.

	The widening matched a key at any depth, so ``dist`` named both, and the refusal offered
	``dist`` - the word just refused - as the way out. Only the id reached the root.
	"""

	world = test_api_tasks._world(session)
	thomas, _theirs = _somebody(session, world)
	jo, _hers = _somebody(session, world, name="jo")
	root = _private(session, world, thomas, key="dist")
	sub = subroutine.domain.projects.create(
		session, workspace_id=world.workspace.id, key="sub", title="Sub"
	)
	nested = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="dist",
		title="Nested",
		parent=sub,
		visibility="private",
		owner_id=thomas.id,
	)
	subroutine.domain.users.set_active(session, thomas, active=False)
	session.flush()

	assert sorted(str(row["project"]) for row in _listed(world)) == ["dist", "sub/dist"]

	for address, project in (("dist", root), ("sub/dist", nested)):
		shared = world.call(
			"POST", f"/v1/projects/{address}/members", json={"username": jo.username}
		)

		assert shared.status_code == 201, (address, shared.text)
		assert jo.id in _members(session, project), f"{address} did not reach {project.title}"


def test_a_name_both_a_visible_project_and_a_stranded_one_answer_to_is_refused_by_name (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#2620`, and decision `#957` over both halves: a name resolves by search, and for somebody
	who may let people back in, the search covers what nobody can reach as well.

	Otherwise ``dist`` silently means the administrator's own ``other/dist`` while the listing
	names ``sub/dist`` - the same wrong project, reached by a name instead of an address. **And only
	for somebody who may rescue**: a member who may not is answered from what they can see, and is
	never told a stranded project's address.
	"""

	world = test_api_tasks._world(session)
	thomas, _theirs = _somebody(session, world)
	jo, _hers = _somebody(session, world, name="jo")
	kim, _kims = _somebody(session, world, name="kim")
	carol, hers = _somebody(session, world, name="carol")
	other = subroutine.domain.projects.create(
		session, workspace_id=world.workspace.id, key="other", title="Other"
	)
	visible = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="dist",
		title="Theirs",
		parent=other,
		visibility="private",
		owner_id=world.user.id,
	)
	subroutine.domain.projects.share(session, visible, carol)
	sub = subroutine.domain.projects.create(
		session, workspace_id=world.workspace.id, key="sub", title="Sub"
	)
	stranded = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="dist",
		title="Stranded",
		parent=sub,
		visibility="private",
		owner_id=thomas.id,
	)
	subroutine.domain.users.set_active(session, thomas, active=False)
	session.flush()

	refused = world.call("POST", "/v1/projects/dist/members", json={"username": jo.username})

	assert refused.status_code == 422, refused.text
	assert "other/dist" in refused.text and "sub/dist" in refused.text, refused.text
	assert jo.id not in _members(session, visible) | _members(session, stranded)

	theirs = hers.call("POST", "/v1/projects/dist/members", json={"username": jo.username})

	assert theirs.status_code == 201, theirs.text
	assert "sub/dist" not in theirs.text
	assert jo.id in _members(session, visible) and jo.id not in _members(session, stranded)

	# And the refusal's advice works: each whole address reaches its own project.
	for address, project in (("other/dist", visible), ("sub/dist", stranded)):
		shared = world.call(
			"POST", f"/v1/projects/{address}/members", json={"username": kim.username}
		)

		assert shared.status_code == 201, (address, shared.text)
		assert kim.id in _members(session, project), f"{address} did not reach {project.title}"


def test_a_root_the_administrator_can_see_is_still_named_by_its_own_address (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#2620`'s other direction: an address resolves exactly even beside a stranded namesake.

	``dist`` is a visible root's whole address, so a stranded ``sub/dist`` does not make it
	ambiguous - the one-segment case of decision `#957`, which :func:`addressed` already keeps and
	a search over both halves must not undo.
	"""

	world = test_api_tasks._world(session)
	thomas, _theirs = _somebody(session, world)
	jo, _hers = _somebody(session, world, name="jo")
	root = _private(session, world, world.user, key="dist")
	sub = subroutine.domain.projects.create(
		session, workspace_id=world.workspace.id, key="sub", title="Sub"
	)
	stranded = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="dist",
		title="Stranded",
		parent=sub,
		visibility="private",
		owner_id=thomas.id,
	)
	subroutine.domain.users.set_active(session, thomas, active=False)
	session.flush()

	shared = world.call("POST", "/v1/projects/dist/members", json={"username": jo.username})

	assert shared.status_code == 201, shared.text
	assert jo.id in _members(session, root) and jo.id not in _members(session, stranded)
