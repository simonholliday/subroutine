"""An item's tags are compared in the order they are shown, however they are read - `SR#5291`.

M-3 of the cold review of 2026-11-11. ``tags.on`` reads an item's tags in no order to rely on
(`SR#1059`), and ``tags.names_on`` handed that order on, so a series' tags and its occurrence's
compared unequal when they were the same set read in another order: on PostgreSQL, given the
plan, an edit to the series from now on skipped an occurrence as one that had changed its tags,
and a change of tags was recorded in no fixed order. Here ``on`` is made to give a different
order on purpose rather than waiting for a planner to.
"""

import typing
import uuid

import fastapi
import httpx
import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.tags
import subroutine.domain.users
import subroutine.domain.workspaces

Task = subroutine.db.models.work.Task


class World:
	"""keanu's MetaCortex, reached through the API with his own credential."""

	def __init__ (self, session: sqlalchemy.orm.Session) -> None:
		"""Make keanu and his workspace."""

		suffix = uuid.uuid4().hex[:6]
		self.session = session
		self.keanu = subroutine.domain.users.create(session, username=f"keanu-{suffix}")
		self.workspace = subroutine.domain.workspaces.create(
			session, slug=f"metacortex-{suffix}", title="MetaCortex", owner=self.keanu
		)
		_row, issued = subroutine.domain.authentication.issue_token(
			session, user=self.keanu, title="own"
		)
		self.secret = issued.value.get_secret_value()
		session.flush()
		self.application: fastapi.FastAPI = api_support.build_app(api_support.factory_for(session))

	def ask (self, method: str, path: str, **given: typing.Any) -> httpx.Response:
		"""Make one request as keanu."""

		return api_support.call(
			self.application,
			method,
			path,
			headers={"Authorization": f"Bearer {self.secret}"},
			**given,
		)

	def task (self, ref: int) -> Task:
		"""Return the row behind a number, as it is stored."""

		row = self.session.scalars(
			sqlalchemy.select(Task).where(Task.workspace_id == self.workspace.id, Task.ref == ref)
		).one()
		self.session.refresh(row)

		return row


def _read_in (monkeypatch: pytest.MonkeyPatch, order: typing.Callable[[typing.Any], bool]) -> None:
	"""Make ``tags.on`` give an item's tags in name order, or reversed where ``order`` says so."""

	original = subroutine.domain.tags.on

	def planned (session: sqlalchemy.orm.Session, item: typing.Any) -> list[typing.Any]:
		"""Return an item's tags as one plan or another might."""

		found = sorted(original(session, item), key=lambda tag: tag.name)

		return found[::-1] if order(item) else found

	monkeypatch.setattr(subroutine.domain.tags, "on", planned)


def _never (_item: typing.Any) -> bool:
	"""Reverse no item's tags."""

	return False


def _always (_item: typing.Any) -> bool:
	"""Reverse every item's tags."""

	return True


def test_names_on_gives_the_order_names_for_shows_whatever_order_the_tags_arrive_in (
	session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`names_on` read `on`'s order; it is now the order `names_for` renders, either way round."""

	world = World(session)
	made = world.ask("POST", "/v1/tasks", json={"title": "Pack", "tags": ["zeta", "Alpha", "mid"]})

	assert made.status_code == 201, made.text

	task = world.task(made.json()["ref"])
	shown = subroutine.domain.tags.names_for(session, Task, [task.id])[task.id]

	for order in (_never, _always):
		_read_in(monkeypatch, order)

		assert subroutine.domain.tags.names_on(session, task) == shown, order.__name__


def test_an_occurrence_holding_its_series_tags_in_another_order_takes_an_edit_from_now_on (
	session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""M-3: the occurrence's tags, read in another order than the series', looked changed by hand,
	so the series' new tag from now on was not carried to it."""

	world = World(session)
	made = world.ask(
		"POST",
		"/v1/tasks",
		json={
			"title": "Water the plants",
			"recurrence": "every week",
			"due": "2031-12-01",
			"tags": ["home", "indoors"],
		},
	)

	assert made.status_code == 201, made.text

	ref = made.json()["ref"]
	series = world.task(ref).recurrence_template_id

	assert series is not None

	_read_in(monkeypatch, lambda item: not item.is_template)
	edited = world.ask(
		"PATCH",
		f"/v1/tasks/{world.session.get_one(Task, series).ref}",
		json={"tags": ["home", "indoors", "garden"], "applies_to": "from_now_on"},
	)

	assert edited.status_code == 200, edited.text
	assert "garden" in world.ask("GET", f"/v1/tasks/{ref}").json()["tags"]


def test_a_change_of_tags_is_recorded_in_name_order (
	session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""M-3: recorded as `on` read them - `zeta, alpha` to `zeta, alpha, beta`."""

	world = World(session)
	made = world.ask("POST", "/v1/tasks", json={"title": "Pack", "tags": ["zeta", "alpha"]})

	assert made.status_code == 201, made.text

	ref = made.json()["ref"]
	_read_in(monkeypatch, _always)
	changed = world.ask("PATCH", f"/v1/tasks/{ref}", json={"tags": ["zeta", "alpha", "beta"]})

	assert changed.status_code == 200, changed.text

	events = world.ask("GET", f"/v1/tasks/{ref}/events").json()["items"]
	recorded = [row["changes"]["tags"] for row in events if "tags" in (row["changes"] or {})]

	assert {"from": ["alpha", "zeta"], "to": ["alpha", "beta", "zeta"]} in recorded, recorded
