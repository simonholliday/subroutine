"""A repeat whose series the reader cannot reach - `SR#5135` and `SR#5141`.

H-5 of the cold review of 2026-10-17, and NEW-C-1 of its verification, both decided in `#5097`. A
weekly series is made in `aaa`, and its occurrence moved to `web` and handed to carrieanne. Then
`aaa` goes in the trash, or is private to keanu. **Finishing the occurrence works**, the series left
where it is beneath the trash and finished as a consequence where it is merely out of sight; **a
stop, a restart, a new rule and an edit from now on are refused in the reader's words**, naming the
place only to a reader who can see it and never the series' own number.
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
import subroutine.domain.occurrences
import subroutine.domain.users
import subroutine.domain.workspaces

Task = subroutine.db.models.work.Task

#: The four writes that change how the occurrence's series repeats, each as a request body.
CHANGES: dict[str, dict[str, typing.Any]] = {
	"stop": {"recurrence": None},
	"new rule": {"recurrence": "every day"},
	"from now on": {"title": "Water the plants well", "applies_to": "from_now_on"},
}


class Place:
	"""keanu's MetaCortex, carrieanne a member, and a weekly series in `aaa` whose occurrence is in `web`."""

	def __init__ (self, session: sqlalchemy.orm.Session, *, private: bool = False, count: int | None = 1) -> None:
		"""Make the series through the API, as a person would, and move its occurrence out."""

		suffix = uuid.uuid4().hex[:6]
		self.session = session
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

		for key in ("aaa", "web"):
			visibility = {"visibility": "private"} if private and key == "aaa" else {}
			made = self.ask(self.keanu, "POST", "/v1/projects", json={"key": key, "title": key.title(), **visibility})

			assert made.status_code == 201, made.text

		rule = "every week" if count is None else f"FREQ=WEEKLY;COUNT={count}"
		made = self.ask(
			self.keanu,
			"POST",
			"/v1/tasks",
			json={"title": "Water the plants", "project": "aaa", "recurrence": rule, "due": "2031-12-01"},
		)

		assert made.status_code == 201, made.text

		self.ref = made.json()["ref"]
		moved = self.ask(
			self.keanu,
			"PATCH",
			f"/v1/tasks/{self.ref}",
			json={"project": "web", "assignee": self.carrieanne.username, "applies_to": "this_one"},
		)

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

	@property
	def series (self) -> Task:
		"""Return the series the occurrence came from, as it is stored."""

		occurrence = self.session.scalars(
			sqlalchemy.select(Task).where(Task.ref == self.ref, Task.recurrence_template_id.is_not(None))
		).one()
		series = self.session.get(Task, occurrence.recurrence_template_id)

		assert series is not None
		self.session.refresh(series)

		return series

	def trash_aaa (self) -> None:
		"""Put `aaa`, which holds the series, in the trash."""

		gone = self.ask(self.keanu, "DELETE", "/v1/projects/aaa")

		assert gone.status_code in (200, 204), gone.text


def _said (answer: httpx.Response) -> str:
	"""Return all a refusal says: its sentence, its hint and its fields' messages."""

	body = answer.json()

	return " ".join([body["detail"], body.get("hint") or "", *(one["message"] for one in body.get("errors", []))])


@pytest.mark.parametrize("finish", ["complete", "skip", "done"])
def test_the_last_occurrence_of_a_series_beneath_the_trash_is_finished_and_the_series_left (
	session: sqlalchemy.orm.Session, finish: str
) -> None:
	"""H-5: its completion, a skip and `status=done` each answered 422 naming the series' number."""

	place = Place(session)
	place.trash_aaa()

	if finish == "done":
		answer = place.ask(place.carrieanne, "PATCH", f"/v1/tasks/{place.ref}", json={"status": "done"})

	else:
		answer = place.ask(place.carrieanne, "POST", f"/v1/tasks/{place.ref}/{finish}")

	assert answer.status_code == 200, answer.text
	assert place.series.completed_at is None, "a series beneath the trash was finished"


@pytest.mark.parametrize("change", CHANGES, ids=list(CHANGES))
def test_how_a_series_beneath_the_trash_repeats_is_refused_until_it_is_restored (
	session: sqlalchemy.orm.Session, change: str
) -> None:
	"""H-5: a stop was refused naming the series' number, and a new rule and an edit from now on
	wrote it asking nothing. Each is refused, naming `aaa` to a reader who can see it."""

	place = Place(session, count=None)
	place.trash_aaa()
	series = place.series

	answer = place.ask(place.keanu, "PATCH", f"/v1/tasks/{place.ref}", json=CHANGES[change])
	said = _said(answer)

	assert answer.status_code == 422, answer.text
	assert answer.json()["detail"] == "How this repeats cannot change until aaa is restored.", said
	assert f"#{series.ref}" not in said, said
	assert ("or change just this one" in said) == (change == "from now on"), said
	assert place.series.recurrence_rule == series.recurrence_rule
	assert place.series.completed_at is None


def test_restarting_a_stopped_series_beneath_the_trash_is_refused (session: sqlalchemy.orm.Session) -> None:
	"""H-5: setting a rule on a stopped series restarts it, and that writes the series too."""

	place = Place(session, count=None)
	stopped = place.ask(place.keanu, "PATCH", f"/v1/tasks/{place.ref}", json={"recurrence": None})

	assert stopped.status_code == 200, stopped.text

	place.trash_aaa()
	answer = place.ask(place.keanu, "PATCH", f"/v1/tasks/{place.ref}", json={"recurrence": "every day"})

	assert answer.status_code == 422, answer.text
	assert answer.json()["detail"] == "How this repeats cannot change until aaa is restored."
	assert place.series.completed_at is not None, "the stopped series was started again"


def test_an_open_ended_series_beneath_the_trash_comes_back_with_its_next_occurrence (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#5097`: the next occurrence is minted beside the series, and returns with the project."""

	place = Place(session, count=None)
	place.trash_aaa()

	answer = place.ask(place.carrieanne, "POST", f"/v1/tasks/{place.ref}/complete")

	assert answer.status_code == 200, answer.text

	restored = place.ask(place.keanu, "POST", "/v1/projects/aaa/restore")

	assert restored.status_code == 200, restored.text
	assert subroutine.domain.occurrences.live_occurrence(session, place.series) is not None


@pytest.mark.parametrize("finish", ["complete", "skip"])
def test_an_assignee_who_cannot_see_the_series_finishes_its_last_occurrence (
	session: sqlalchemy.orm.Session, finish: str
) -> None:
	"""NEW-C-1: they were told there was no such project. The series is finished as a consequence."""

	place = Place(session, private=True)

	assert place.ask(place.carrieanne, "GET", "/v1/projects/aaa").status_code == 404

	answer = place.ask(place.carrieanne, "POST", f"/v1/tasks/{place.ref}/{finish}")

	assert answer.status_code == 200, answer.text
	assert place.series.completed_at is not None, "the spent series was left open"


@pytest.mark.parametrize("change", CHANGES, ids=list(CHANGES))
def test_how_a_series_the_reader_cannot_see_repeats_is_refused_in_their_words (
	session: sqlalchemy.orm.Session, change: str
) -> None:
	"""NEW-C-1: the stop answered 404 about a project nobody named. Each change is refused in words
	that name nothing the reader cannot see; keanu, who can, makes the same change."""

	place = Place(session, private=True, count=None)
	series = place.series

	answer = place.ask(place.carrieanne, "PATCH", f"/v1/tasks/{place.ref}", json=CHANGES[change])
	said = _said(answer)

	assert answer.status_code == 422, answer.text
	assert answer.json()["detail"] == (
		"How this repeats is set where you cannot see it, so it cannot change from here."
	), said
	assert "aaa" not in said and f"#{series.ref}" not in said, said
	assert place.series.recurrence_rule == series.recurrence_rule

	allowed = place.ask(place.keanu, "PATCH", f"/v1/tasks/{place.ref}", json=CHANGES[change])

	assert allowed.status_code == 200, allowed.text
