"""A moved task or document names the private parent it left only to who can see it - `SR#5290`.

M-2 of the cold review of 2026-11-11, beside `SR#5241`'s rule for a project, decided in `#5097`: a
side naming a row its reader cannot see is said as unseen. keanu's sub-task or sub-document in
private `vault`, detached from its parent and moved to public `web`, is readable to laurence, and
its older events gave him the parent: by id in a task's and a document's ``path`` and a document's
``parent_id``, and, for a sub-task carried with its parent first, by number in ``moved_with``.
"""

import datetime
import typing
import uuid

import fastapi
import httpx
import pytest
import sqlalchemy.orm

import api_support
import subroutine.domain.authentication
import subroutine.domain.events
import subroutine.domain.users
import subroutine.domain.workspaces

#: What every surface says in place of a side its reader cannot see.
UNSEEN = subroutine.domain.events.UNSEEN_END

#: The item's own history, then the change feed; the journal is asked by its own test.
SURFACES = ("events", "changes")


@pytest.fixture(autouse=True)
def _no_watermark (monkeypatch: pytest.MonkeyPatch) -> None:
	"""Report an event the moment it is written, as ``test_events_scoping`` does (`#404`)."""

	monkeypatch.setattr(subroutine.domain.events, "WATERMARK", datetime.timedelta(0))


class World:
	"""keanu's MetaCortex, laurence a member, private `vault` and `vault2`, and public `web`."""

	def __init__ (self, session: sqlalchemy.orm.Session) -> None:
		"""Make the people and the projects through the API, as a person would."""

		suffix = uuid.uuid4().hex[:6]
		self.keanu = subroutine.domain.users.create(session, username=f"keanu-{suffix}")
		self.laurence = subroutine.domain.users.create(session, username=f"laurence-{suffix}")
		workspace = subroutine.domain.workspaces.create(
			session, slug=f"metacortex-{suffix}", title="MetaCortex", owner=self.keanu
		)
		subroutine.domain.workspaces.add_member(
			session, workspace, self.laurence, role_key="member"
		)
		self.secrets = {}

		for who in (self.keanu, self.laurence):
			_row, issued = subroutine.domain.authentication.issue_token(
				session, user=who, title="own"
			)
			self.secrets[who.username] = issued.value.get_secret_value()

		session.flush()
		self.application: fastapi.FastAPI = api_support.build_app(api_support.factory_for(session))

		for key, visibility in (("vault", "private"), ("vault2", "private"), ("web", "public")):
			made = self.ask(
				self.keanu,
				"POST",
				"/v1/projects",
				json={"key": key, "title": key.title(), "visibility": visibility},
			)

			assert made.status_code == 201, made.text

	def ask (self, who: typing.Any, method: str, path: str, **given: typing.Any) -> httpx.Response:
		"""Make one request as somebody, with their own credential."""

		return api_support.call(
			self.application,
			method,
			path,
			headers={"Authorization": f"Bearer {self.secrets[who.username]}"},
			**given,
		)

	def make (self, plural: str, title: str, **given: typing.Any) -> dict[str, typing.Any]:
		"""Make a task or a document in `vault`, and return it."""

		made = self.ask(
			self.keanu, "POST", f"/v1/{plural}", json={"title": title, "project": "vault", **given}
		)

		assert made.status_code == 201, made.text

		return dict(made.json())

	def take_out_and_move (self, plural: str, ref: int) -> None:
		"""Detach an item from its parent, then move it to `web`, where laurence can see it."""

		detached = self.ask(self.keanu, "POST", f"/v1/{plural}/{ref}/move", json={"parent": None})

		assert detached.status_code == 200, detached.text

		moved = self.ask(self.keanu, "PATCH", f"/v1/{plural}/{ref}", json={"project": "web"})

		assert moved.status_code == 200, moved.text

	def changes (
		self, who: typing.Any, surface: str, plural: str, item: dict[str, typing.Any], field: str
	) -> list[typing.Any]:
		"""Return each change to ``field`` on the item's events, as this person reads them."""

		path = f"/v1/{plural}/{item['ref']}/events" if surface == "events" else "/v1/changes"
		answer = self.ask(who, "GET", path)

		assert answer.status_code == 200, answer.text

		return [
			row["changes"][field]
			for row in answer.json()["items"]
			if str(row["entity_id"]) == str(item["id"]) and field in (row["changes"] or {})
		]


def _sub_item (world: World, plural: str) -> tuple[str, dict[str, typing.Any]]:
	"""Make a parent and its child in `vault`, take the child out and move it; return both."""

	parent = world.make(plural, "Plan the heist")
	under = {"parent_task_id": parent["id"]} if plural == "tasks" else {"parent": parent["ref"]}
	child = world.make(plural, "Buy a map", **under)
	world.take_out_and_move(plural, child["ref"])

	assert world.ask(world.laurence, "GET", f"/v1/{plural}/{parent['ref']}").status_code == 404
	assert world.ask(world.laurence, "GET", f"/v1/{plural}/{child['ref']}").status_code == 200

	return str(parent["id"]), child


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("plural", ["tasks", "documents"])
def test_a_moved_items_old_path_is_unseen_to_who_cannot_see_its_parent (
	session: sqlalchemy.orm.Session, plural: str, surface: str
) -> None:
	"""M-2: the path it had ran through the hidden parent, and gave laurence its id."""

	world = World(session)
	hidden, item = _sub_item(world, plural)

	his = world.changes(world.laurence, surface, plural, item, "path")
	keanus = world.changes(world.keanu, surface, plural, item, "path")

	assert any(side.get("from") == UNSEEN for side in his), his
	assert hidden not in str(his), f"laurence was given the hidden parent's id: {his}"
	assert any(hidden in str(side.get("from")) for side in keanus), keanus


@pytest.mark.parametrize("surface", SURFACES)
def test_a_moved_documents_old_parent_is_unseen_to_who_cannot_see_it (
	session: sqlalchemy.orm.Session, surface: str
) -> None:
	"""M-2: a document's `parent_id` was taken to be in its own project, which it may have left."""

	world = World(session)
	hidden, item = _sub_item(world, "documents")

	his = world.changes(world.laurence, surface, "documents", item, "parent_id")
	keanus = world.changes(world.keanu, surface, "documents", item, "parent_id")

	assert {"from": UNSEEN, "to": None} in his, his
	assert {"from": hidden, "to": None} in keanus, keanus


@pytest.mark.parametrize("surface", SURFACES)
def test_the_task_a_sub_task_was_carried_with_is_unseen_to_who_cannot_see_it (
	session: sqlalchemy.orm.Session, surface: str
) -> None:
	"""M-2, widened: carried from `vault` to `vault2` with its parent, then taken out and moved to
	`web`, the sub-task's history gave laurence the parent's number, which he gets 404 for."""

	world = World(session)
	parent = world.make("tasks", "Plan the heist")
	child = world.make("tasks", "Buy a map", parent_task_id=parent["id"])
	carried = world.ask(
		world.keanu, "PATCH", f"/v1/tasks/{parent['ref']}", json={"project": "vault2"}
	)

	assert carried.status_code == 200, carried.text

	world.take_out_and_move("tasks", child["ref"])

	his = world.changes(world.laurence, surface, "tasks", child, "moved_with")
	keanus = world.changes(world.keanu, surface, "tasks", child, "moved_with")

	assert his == [{"from": None, "to": UNSEEN}], his
	assert keanus == [{"from": None, "to": parent["ref"]}], keanus


def test_the_journal_gives_laurence_nothing_of_the_hidden_parent (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The journal said what the change feed said, so it gave the same ids and number."""

	world = World(session)
	hidden_task, _task = _sub_item(world, "tasks")
	hidden_document, _document = _sub_item(world, "documents")

	his = world.ask(world.laurence, "GET", "/v1/journal")
	keanus = world.ask(world.keanu, "GET", "/v1/journal")

	assert his.status_code == 200, his.text
	assert hidden_task not in his.text and hidden_document not in his.text, his.text
	assert hidden_task in keanus.text and hidden_document in keanus.text, (
		"keanu's journal no longer carries the moves, so this could not fail"
	)
