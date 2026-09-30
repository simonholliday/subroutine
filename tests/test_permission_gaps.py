"""The smaller permission and narrowing gaps - `SR#3934`.

**Each one answered a question about authority two ways**: a credential narrowed in one view and
not in another, a count of work the reader may not see, an agent refused by its token and
accepted by name, a secret quoted back in a refusal, a workspace found by one route and not its
neighbour, and an account or an owner accepted that the rest of the program would refuse.
"""

import typing
import uuid

import pytest
import sqlalchemy.orm

import subroutine.domain.authentication
import subroutine.domain.local
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, as its first account."""

	return test_api_tasks._world(session)


def _field_named (answered: typing.Any, field: str) -> None:
	"""Assert a 422 naming ``field``, bare or as the part of the request it was in."""

	assert answered.status_code == 422, answered.text

	named = {one["field"] for one in answered.json().get("errors") or []}

	assert {field, f"query.{field}", f"path.{field}"} & named, (field, answered.text)


def test_a_credential_narrowed_only_in_what_it_may_write_says_so_everywhere (
	world: test_api_tasks.World,
) -> None:
	"""``/v1/me`` worked the rule out by hand and never learned the fourth axis, so a credential
	narrowed only in the projects it may write was not narrowed in each workspace there, while
	the same answer's ``credential.narrows`` and ``/v1/tokens`` said it was."""

	_row, issued = subroutine.domain.authentication.issue_token(
		world.session,
		user=world.user,
		title="Writes in the Inbox",
		project_write_scope=[str(_inbox(world))],
	)
	world.session.flush()
	secret = issued.value.get_secret_value()
	me = world.call("GET", "/v1/me", headers={"authorization": f"Bearer {secret}"}).json()

	assert me["credential"]["narrows"] is True, me["credential"]
	assert all(one["narrowed_by_credential"] for one in me["workspaces"]), me["workspaces"]


def test_a_credential_writing_in_one_project_makes_and_moves_nothing_into_another (
	world: test_api_tasks.World,
) -> None:
	"""`SR#4013`, M-10 of the cold review of 2026-09-30: a project's new parent was never asked.

	A credential reaching ``web`` and ``ops`` and writing only in ``web`` was refused a task in
	``ops``, and yet made ``ops/sub`` and moved ``web`` under ``ops``: ``create`` asked the
	workspace and ``move`` the project moved. **The parent is asked as the project is**, and
	``web`` is still its to build under. The top level waits on the review's second question.
	"""

	made = {
		key: world.call("POST", "/v1/projects", json={"key": key, "title": key.title()}).json()
		for key in ("web", "ops")
	}
	_row, issued = subroutine.domain.authentication.issue_token(
		world.session,
		user=world.user,
		title="Writes in web",
		project_scope=[made["web"]["id"], made["ops"]["id"]],
		project_write_scope=[made["web"]["id"]],
	)
	world.session.flush()
	bearer = {"authorization": f"Bearer {issued.value.get_secret_value()}"}

	under = world.call(
		"POST", "/v1/projects", json={"key": "sub", "title": "Sub", "parent": "ops"}, headers=bearer
	)
	moved = world.call("POST", "/v1/projects/web/move", json={"parent": "ops"}, headers=bearer)
	inside = world.call(
		"POST", "/v1/projects", json={"key": "sub", "title": "Sub", "parent": "web"}, headers=bearer
	)

	assert under.status_code == 403, under.text
	assert moved.status_code == 403, moved.text
	assert inside.status_code == 201, inside.text
	assert world.call("GET", "/v1/projects/web").json()["parent_id"] is None, "web was moved"


def _inbox (world: test_api_tasks.World) -> uuid.UUID:
	"""Return the id of the world's Inbox, to narrow a credential to."""

	listed = world.call("GET", "/v1/projects").json()["items"]

	return uuid.UUID(next(one["id"] for one in listed if one["key"] == "inbox"))


def test_a_milestone_refused_a_new_type_says_nothing_of_how_much_it_includes (
	world: test_api_tasks.World,
) -> None:
	"""The refusal counted every link, so it told a reader how much of the milestone's work they
	could not see, and counted work in the trash, which the milestone's own progress leaves out."""

	milestone = world.call("POST", "/v1/tasks", json={"title": "Ship 1.0", "type": "milestone"})
	work = world.call("POST", "/v1/tasks", json={"title": "Write the notes"})

	assert milestone.status_code == 201 and work.status_code == 201, (milestone.text, work.text)

	linked = world.call(
		"POST",
		f"/v1/tasks/{milestone.json()['ref']}/links",
		json={"link_type": "includes", "target": str(work.json()["ref"])},
	)

	assert linked.status_code == 201, linked.text

	world.call("DELETE", f"/v1/tasks/{work.json()['ref']}")
	refused = world.call(
		"PATCH", f"/v1/tasks/{milestone.json()['ref']}", json={"type": "task"}
	)

	assert refused.status_code == 422, refused.text
	assert "includes other work" in refused.text, refused.text
	assert "1 item" not in refused.text, refused.text


def test_an_agent_nobody_answers_for_is_refused_by_name_as_by_its_token (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``local_user`` named an agent whose person had left, and it went on working while the
	same agent's own token was refused."""

	world = test_api_tasks._world(session)
	person = subroutine.domain.users.create(session, username="thomas")
	agent = subroutine.domain.users.create(
		session, username="dozer", is_service_account=True, responsible_user_id=person.id
	)
	subroutine.domain.workspaces.add_member(session, world.workspace, agent, role_key="member")
	person.is_active = False
	session.flush()

	with pytest.raises(subroutine.errors.Unauthenticated) as refused:
		subroutine.domain.local.principal(session, token=None, local_user="dozer")

	assert "nobody active answers for" in refused.value.detail, refused.value


@pytest.mark.parametrize(
	("header", "said"),
	[
		("sr_0123abcd_Th3S3cretH4lf", "nothing before it"),
		("Basic dXNlcjpwYXNzd29yZA==", "does not accept that authentication scheme"),
	],
	ids=["a-token-with-no-scheme", "another-scheme"],
)
def test_a_refused_authorization_header_is_not_quoted_back (
	world: test_api_tasks.World, header: str, said: str
) -> None:
	"""A token sent without ``Bearer`` was quoted back whole as the name of a scheme: a live
	secret, in a body an agent keeps in its transcript."""

	refused = world.call("GET", "/v1/me", headers={"authorization": header})

	assert refused.status_code == 401, refused.text
	assert said in refused.text, refused.text

	for part in header.split():
		assert part not in refused.text, f"{part!r} was quoted back: {refused.text}"


def test_a_short_name_written_in_capitals_reaches_every_route_of_its_workspace (
	world: test_api_tasks.World,
) -> None:
	"""``/v1/workspaces/PROJECTS`` read and changed the workspace, while its members and a
	delete of it answered 404.

	**Asked by an ordinary member**, since the world's own account runs the installation and
	would be let through by the administrator's door, which is tested on its own below.
	"""

	member = subroutine.domain.users.create(world.session, username="switch")
	subroutine.domain.workspaces.add_member(
		world.session, world.workspace, member, role_key="member"
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		world.session, user=member, title="Switch's"
	)
	world.session.flush()
	theirs = {"authorization": f"Bearer {issued.value.get_secret_value()}"}
	upper = world.workspace.slug.upper()

	assert world.call("GET", f"/v1/workspaces/{upper}", headers=theirs).status_code == 200
	assert world.call("GET", f"/v1/workspaces/{upper}/members", headers=theirs).status_code == 200, (
		"the members of a workspace its own record answers for were not there"
	)


def test_an_administrator_names_a_workspace_in_capitals_that_they_do_not_belong_to (
	world: test_api_tasks.World,
) -> None:
	"""The administrator's door, which compared the text as written too."""

	other = subroutine.domain.workspaces.create(
		world.session,
		slug=f"w{uuid.uuid4().hex[:8]}",
		title="Elsewhere",
		owner=subroutine.domain.users.create(world.session, username="trinity"),
	)
	world.session.flush()

	listed = world.call("GET", f"/v1/workspaces/{other.slug.upper()}/members")

	assert listed.status_code == 200, listed.text


def test_an_account_cannot_be_made_with_a_zone_nothing_knows (
	world: test_api_tasks.World,
) -> None:
	"""It was accepted, and its agenda then answered 422 about a zone its owner could not have
	set, since setting it on oneself is refused."""

	_field_named(
		world.call("POST", "/v1/users", json={"username": "morpheus", "timezone": "Mars/Olympus"}),
		"timezone",
	)


def test_a_project_s_owner_is_somebody_in_its_workspace (world: test_api_tasks.World) -> None:
	"""An id naming nobody reached the foreign key as a 500, and an account outside the workspace
	was taken, so a private project made so was invisible to whoever made it - and an edit could
	hand an existing project to such an account."""

	outsider = subroutine.domain.users.create(world.session, username="cypher")
	world.session.flush()

	_field_named(
		world.call(
			"POST",
			"/v1/projects",
			json={"key": "web", "title": "The website", "owner_id": str(uuid.uuid4())},
		),
		"owner_id",
	)
	_field_named(
		world.call(
			"POST",
			"/v1/projects",
			json={
				"key": "web",
				"title": "The website",
				"owner_id": str(outsider.id),
				"visibility": "private",
			},
		),
		"owner_id",
	)

	made = world.call("POST", "/v1/projects", json={"key": "web", "title": "The website"})

	assert made.status_code == 201, made.text

	_field_named(
		world.call("PATCH", "/v1/projects/web", json={"owner_id": str(outsider.id)}), "owner_id"
	)
