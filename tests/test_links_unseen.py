"""A link whose other end the reader cannot see is shown as unseen, on every surface - `SR#4575`.

Decision `#4521`, on E #10 of the cold review of 2026-10-05: five shapes of "unseen" answered one
question - a dropped link end, a counted blocker, a 404 on withdrawal, a flag on milestones - where
docs/design.md §7.3a promised ``{"visible": false}`` and nothing produced it. **One shape**: the link
is shown, so a reader told an item is blocked sees the link that blocks it, and nothing of what is at
the other end is said. A count says how much of what the reader can see is done, as the row does.
"""

import datetime
import typing
import uuid

import pytest
import sqlalchemy.orm

import api_support
import subroutine.clients.http
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.db.models.identity
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.events
import subroutine.domain.projects
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.mcp.protocol
import subroutine.mcp.tools
import subroutine.views
import test_api_tasks
import test_mcp


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


class Scene (typing.NamedTuple):
	"""A public task held up by a finished task and by one in a private project, and who reads it."""

	public: int
	hidden: subroutine.db.models.work.Task
	outsider: subroutine.db.models.identity.User
	secret: str


def _scene (world: test_api_tasks.World) -> Scene:
	"""Make the scene: Keanu, a member, is not shared into the private project."""

	session = world.session
	vault = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="vault",
		title="Vault",
		visibility="private",
		owner_id=world.user.id,
	)
	hidden = subroutine.domain.tasks.create(session, project=vault, title="Pay the deposit, quietly")
	session.flush()
	public = world.call("POST", "/v1/tasks", json={"title": "Book the venue"}).json()["ref"]
	done = world.call("POST", "/v1/tasks", json={"title": "Choose the date"}).json()["ref"]

	# **Made from the public end**, as an incoming link, so its event is about an item the outsider
	# sees: one made from the private end is about an item they cannot, and so never reaches them.
	for source in (hidden.ref, done):
		linked = world.call(
			"POST",
			f"/v1/tasks/{public}/links",
			json={"target": source, "target_type": "task", "link_type": "blocks", "direction": "incoming"},
		)

		assert linked.status_code == 201, linked.text

	assert world.call("POST", f"/v1/tasks/{done}/complete").status_code == 200

	outsider = subroutine.domain.users.create(session, username=f"keanu-{uuid.uuid4().hex[:6]}")
	subroutine.domain.workspaces.add_member(session, world.workspace, outsider, role_key="member")
	_row, issued = subroutine.domain.authentication.issue_token(session, user=outsider, title="keanu")
	session.flush()

	return Scene(public, hidden, outsider, str(issued.value.get_secret_value()))


def _over_http (world: test_api_tasks.World, secret: str) -> subroutine.clients.http.Client:
	"""Return the real HTTP client, as the holder of ``secret``."""

	return subroutine.clients.http.Client(
		subroutine.connections.Connection(name="work", url="https://example.com"),
		token=secret,
		transport=api_support.SyncTransport(world.application),
		base_url=api_support.BASE_URL,
	)


def _locally (world: test_api_tasks.World, user: subroutine.db.models.identity.User) -> subroutine.clients.local.Client:
	"""Return the local client, as ``user``."""

	return subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(
			dev_mode=True,
			local_user=user.username,
			database_url=world.session.get_bind().engine.url.render_as_string(hide_password=False),
		),
		session_factory=api_support.factory_for(world.session),
	)


def _names_none_of (text: str, scene: Scene) -> None:
	"""Fail if ``text`` names the hidden task, by its ref or its title."""

	assert f"#{scene.hidden.ref}" not in text and scene.hidden.title not in text, text


def test_both_clients_read_a_hidden_end_as_unseen (world: test_api_tasks.World) -> None:
	"""The one shape, through both transports, and an end the reader sees beside it as it was."""

	scene = _scene(world)

	with _over_http(world, scene.secret) as remote, _locally(world, scene.outsider) as local:
		answers = [
			client.links(ref=scene.public, workspace=world.workspace.slug) for client in (remote, local)
		]

	for links in answers:
		ends = [link.other for link in links]

		assert sum(isinstance(end, subroutine.views.Unseen) for end in ends) == 1, ends
		assert sum(isinstance(end, subroutine.views.LinkEnd) for end in ends) == 1, ends

	assert [link.model_dump(mode="json") for link in answers[0]] == [
		link.model_dump(mode="json") for link in answers[1]
	], "the two transports disagree about a link to something hidden"

	_names_none_of(str([link.model_dump(mode="json") for link in answers[0]]), scene)


def test_the_agent_tools_show_a_hidden_blocker_as_unseen_and_count_what_they_can_see (
	world: test_api_tasks.World,
) -> None:
	"""``subroutine_show``: the line is there, said as unseen, and the count is of what is seen."""

	scene = _scene(world)

	with _over_http(world, scene.secret) as client:
		server = subroutine.mcp.protocol.Server(
			subroutine.mcp.tools.catalogue(client, workspace=world.workspace.slug),
			name="subroutine",
			version="0",
		)
		shown, failed = test_mcp._called(server, "subroutine_show", ref=scene.public)

	assert not failed, shown
	assert "Blocked by  something you cannot see" in shown, shown
	assert "1 of 1 blockers done" in shown, shown

	_names_none_of(shown, scene)


def test_the_feed_and_the_journal_show_a_link_to_something_hidden_as_unseen (
	world: test_api_tasks.World, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The feed left the link out (`SR#302`); it carries it now, its hidden end unseen (`SR#4575`)."""

	monkeypatch.setattr(subroutine.domain.events, "WATERMARK", datetime.timedelta(0))
	scene = _scene(world)
	secret = {"authorization": f"Bearer {scene.secret}"}
	changes = api_support.call(world.application, "GET", "/v1/changes", headers=secret)

	assert changes.status_code == 200, changes.text

	linked = [
		one
		for one in changes.json()["items"]
		if one["entity_type"] == "link" and one["changes"]["source"]["to"] == {"visible": False}
	]

	assert len(linked) == 1, changes.text

	_names_none_of(changes.text, scene)

	journal = api_support.call(world.application, "GET", "/v1/journal", headers=secret)

	assert journal.status_code == 200, journal.text
	assert any(one["entity_type"] == "link" for one in journal.json()["items"]), journal.text

	_names_none_of(journal.text, scene)

	# **And the owner, who sees both ends, is told which** - so this is a redaction, not a gap.
	told = world.call("GET", "/v1/changes")

	assert f'"to":{scene.hidden.ref}' in told.text.replace(" ", ""), told.text


def test_a_link_to_work_beneath_the_trash_is_left_out_rather_than_unseen (
	world: test_api_tasks.World,
) -> None:
	"""**Out of sight is not the same as in the trash** (decision `#4091`): the trash hides what is
	beneath it from everybody, its owner included, and that is no reason to say a link's other end is
	something the reader may not know of. Left out, as before `SR#4575`.
	"""

	made = world.call("POST", "/v1/projects", json={"key": "old", "title": "Old"})

	assert made.status_code == 201, made.text

	public = world.call("POST", "/v1/tasks", json={"title": "Book the venue"}).json()["ref"]
	gone = world.call("POST", "/v1/tasks", json={"title": "Ask the old venue", "project": "old"}).json()
	linked = world.call(
		"POST",
		f"/v1/tasks/{public}/links",
		json={"target": gone["ref"], "target_type": "task", "link_type": "relates_to"},
	)

	assert linked.status_code == 201, linked.text
	assert world.call("DELETE", "/v1/projects/old").status_code < 300

	listed = world.call("GET", f"/v1/tasks/{public}/links")

	assert listed.json()["items"] == [], listed.text
