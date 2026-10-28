"""A project moved out from under a private parent names that parent only to who can see it - `SR#5241`.

Found building `SR#5148`, and placed in 1.0 in `#5097`. keanu's `plans` sat under `vault`, private to
him, and was moved to the top of the tree, where carrieanne, a member who cannot see `vault`, can see
it. Its history and the change feed gave her `vault`'s id twice: as the parent it left and inside the
path it had.
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


@pytest.fixture(autouse=True)
def _no_watermark (monkeypatch: pytest.MonkeyPatch) -> None:
	"""Report an event the moment it is written, as ``test_events_scoping`` does (`#404`)."""

	monkeypatch.setattr(subroutine.domain.events, "WATERMARK", datetime.timedelta(0))


class Moved:
	"""keanu's MetaCortex, carrieanne a member, and `plans` moved out from under private `vault`."""

	def __init__ (self, session: sqlalchemy.orm.Session) -> None:
		"""Make both projects and the move through the API, as a person would."""

		suffix = uuid.uuid4().hex[:6]
		self.keanu = subroutine.domain.users.create(session, username=f"keanu-{suffix}")
		self.carrieanne = subroutine.domain.users.create(session, username=f"carrieanne-{suffix}")
		workspace = subroutine.domain.workspaces.create(
			session, slug=f"metacortex-{suffix}", title="MetaCortex", owner=self.keanu
		)
		subroutine.domain.workspaces.add_member(session, workspace, self.carrieanne, role_key="member")
		self.secrets = {}

		for who in (self.keanu, self.carrieanne):
			_row, issued = subroutine.domain.authentication.issue_token(session, user=who, title="own")
			self.secrets[who.username] = issued.value.get_secret_value()

		session.flush()
		self.application: fastapi.FastAPI = api_support.build_app(api_support.factory_for(session))

		for body in (
			{"key": "vault", "title": "Vault", "visibility": "private"},
			{"key": "plans", "title": "Plans", "parent": "vault"},
		):
			made = self.ask(self.keanu, "POST", "/v1/projects", json=body)

			assert made.status_code == 201, made.text

		self.vault = str(self.ask(self.keanu, "GET", "/v1/projects/vault").json()["id"])
		moved = self.ask(self.keanu, "POST", "/v1/projects/plans/move", json={"parent": None})

		assert moved.status_code == 200, moved.text

	def ask (self, who: typing.Any, method: str, path: str, **given: typing.Any) -> httpx.Response:
		"""Make one request as somebody, with their own credential."""

		return api_support.call(
			self.application,
			method,
			path,
			headers={"Authorization": f"Bearer {self.secrets[who.username]}"},
			**given,
		)

	def move_as_read_by (self, who: typing.Any, path: str) -> dict[str, typing.Any]:
		"""Return the move's changes, as this person reads them at ``path``."""

		answer = self.ask(who, "GET", path)

		assert answer.status_code == 200, answer.text

		(changes,) = [
			row["changes"]
			for row in answer.json()["items"]
			if row["entity_type"] == "project" and "descendants_rewritten" in (row["changes"] or {})
		]

		return dict(changes)


@pytest.mark.parametrize("path", ["/v1/projects/plans/events", "/v1/changes"])
def test_a_moved_projects_parent_and_path_are_unseen_to_who_cannot_see_the_parent (
	session: sqlalchemy.orm.Session, path: str
) -> None:
	"""carrieanne is told it moved from something she cannot see; keanu, from `vault`."""

	moved = Moved(session)
	hers = moved.move_as_read_by(moved.carrieanne, path)
	his = moved.move_as_read_by(moved.keanu, path)

	assert hers["parent_id"] == {"from": {"visible": False}, "to": None}, hers
	assert hers["path"]["from"] == {"visible": False}, hers
	assert moved.vault not in str(hers), f"carrieanne was given vault's id: {hers}"

	# **What she can see is still said**: where it is now names nothing out of her sight.
	assert isinstance(hers["path"]["to"], str) and hers["path"]["to"].startswith("/"), hers

	assert his["parent_id"]["from"] == moved.vault, his
	assert moved.vault in his["path"]["from"], his


def test_the_journal_gives_carrieanne_nothing_of_vaults_id (session: sqlalchemy.orm.Session) -> None:
	"""The journal printed the old path whole, ids and all; the side through `vault` is unseen now."""

	moved = Moved(session)
	answer = moved.ask(moved.carrieanne, "GET", "/v1/journal")

	assert answer.status_code == 200, answer.text
	assert moved.vault not in answer.text, "the journal gave carrieanne vault's id"
	assert moved.vault in moved.ask(moved.keanu, "GET", "/v1/journal").text, (
		"keanu's journal no longer carries the move, so this could not fail"
	)
