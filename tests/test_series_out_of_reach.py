"""A repeat whose series the reader cannot reach - `SR#5135` and `SR#5141`.

H-5 of the cold review of 2026-10-17, and NEW-C-1 of its verification, both decided in `#5097`. A
weekly series is made in `aaa`, and its occurrence moved to `web` and handed to carrieanne. Then
`aaa` goes in the trash, or is private to keanu. **Finishing the occurrence works**, the series left
where it is beneath the trash and finished as a consequence where it is merely out of sight; **a
stop, a restart, a new rule and an edit from now on are refused in the reader's words**, naming the
place only to a reader who can see it and never the series' own number.

**And the row an edit from now on reaches is asked what an edit made to it would be** (`SR#5289`,
M-1 of the cold review of 2026-11-11, with NEW-B-1 of its verification): with the occurrence in
`web` and the series in `aaa`, a move, a new assignee and a rewrite were carried to the series
with none of those checks. **Sight is reach, and writing is the place's** (`SR#5292`, M-4): a
credential scoped to tasks was told its own Inbox was out of sight, and the blank repeat box of
a stopped series was refused as a stop.
"""

import typing
import uuid

import fastapi
import httpx
import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine.db.models.project
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
	"""keanu's MetaCortex, two members, and a weekly series in `aaa` whose occurrence is in `web`.

	carrieanne holds the occurrence, and laurence is a member who holds nothing. ``keys`` names the
	projects keanu makes, `aaa` and `web` first.
	"""

	def __init__ (
		self,
		session: sqlalchemy.orm.Session,
		*,
		private: bool = False,
		count: int | None = 1,
		keys: tuple[str, ...] = ("aaa", "web"),
	) -> None:
		"""Make the series through the API, as a person would, and move its occurrence out."""

		suffix = uuid.uuid4().hex[:6]
		self.session = session
		self.keanu = subroutine.domain.users.create(session, username=f"keanu-{suffix}")
		self.carrieanne = subroutine.domain.users.create(session, username=f"carrieanne-{suffix}")
		self.laurence = subroutine.domain.users.create(session, username=f"laurence-{suffix}")
		workspace = subroutine.domain.workspaces.create(
			session, slug=f"metacortex-{suffix}", title="MetaCortex", owner=self.keanu
		)
		subroutine.domain.workspaces.add_member(session, workspace, self.carrieanne, role_key="member")
		subroutine.domain.workspaces.add_member(
			session, workspace, self.laurence, role_key="member"
		)
		self.secrets = {}

		for who in (self.keanu, self.carrieanne, self.laurence):
			_row, issued = subroutine.domain.authentication.issue_token(session, user=who, title="own")
			self.secrets[who.username] = issued.value.get_secret_value()

		session.flush()
		self.application: fastapi.FastAPI = api_support.build_app(api_support.factory_for(session))
		self.ids: dict[str, str] = {}

		for key in keys:
			visibility = {"visibility": "private"} if private and key == "aaa" else {}
			made = self.ask(self.keanu, "POST", "/v1/projects", json={"key": key, "title": key.title(), **visibility})

			assert made.status_code == 201, made.text

			self.ids[key] = made.json()["id"]

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

	def ask (
		self,
		who: typing.Any,
		method: str,
		path: str,
		*,
		secret: str | None = None,
		**given: typing.Any,
	) -> httpx.Response:
		"""Make one request as somebody, with their own credential or the ``secret`` given."""

		return api_support.call(
			self.application,
			method,
			path,
			headers={"Authorization": f"Bearer {secret or self.secrets[who.username]}"},
			**given,
		)

	def token (self, who: typing.Any, **narrowing: typing.Any) -> str:
		"""Issue somebody a second credential, narrowed as given, and return its secret."""

		_row, issued = subroutine.domain.authentication.issue_token(
			self.session, user=who, title="narrowed", **narrowing
		)
		self.session.flush()

		return issued.value.get_secret_value()

	def key_of (self, task: Task) -> str:
		"""Return the key of the project a task is filed in."""

		project = self.session.get(subroutine.db.models.project.Project, task.project_id)

		assert project is not None

		return project.key

	@property
	def series (self) -> Task:
		"""Return the series the occurrence came from, as it is stored."""

		return self.series_behind(self.ref)

	def series_behind (self, ref: int) -> Task:
		"""Return the series behind an occurrence, by the occurrence's number, as it is stored."""

		occurrence = self.session.scalars(
			sqlalchemy.select(Task).where(Task.ref == ref, Task.recurrence_template_id.is_not(None))
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


def _shared (place: Place, key: str, who: typing.Any) -> None:
	"""Share a private project of keanu's with somebody."""

	shared = place.ask(
		place.keanu, "POST", f"/v1/projects/{key}/members", json={"username": who.username}
	)

	assert shared.status_code in (200, 201), shared.text


def test_a_series_is_moved_out_of_a_private_project_only_by_its_steward (
	session: sqlalchemy.orm.Session,
) -> None:
	"""M-1 of the cold review of 2026-11-11: carrieanne, shared into private `aaa` but not its
	steward, moved its series to public `web2` through the occurrence, and every later occurrence
	was made in public. Refused as moving a task of her own out of `aaa` is; keanu, its owner,
	moves it."""

	place = Place(session, private=True, count=None, keys=("aaa", "web", "web2"))
	_shared(place, "aaa", place.carrieanne)
	series = place.series
	moving = {"project": "web2", "applies_to": "from_now_on"}

	answer = place.ask(place.carrieanne, "PATCH", f"/v1/tasks/{place.ref}", json=moving)

	assert answer.status_code == 403, answer.text
	assert answer.json()["detail"] == (
		"Only the owner of 'aaa', or somebody who administers this workspace, may move every one "
		"from now on out of a private project."
	), _said(answer)
	assert place.key_of(place.series) == "aaa"
	assert place.ask(place.laurence, "GET", f"/v1/tasks/{series.ref}").status_code == 404

	allowed = place.ask(place.keanu, "PATCH", f"/v1/tasks/{place.ref}", json=moving)

	assert allowed.status_code == 200, allowed.text
	assert place.key_of(place.series) == "web2"


def test_a_credential_narrowed_to_some_projects_moves_no_series_across_a_boundary (
	session: sqlalchemy.orm.Session,
) -> None:
	"""M-1: keanu's credential narrowed to `aaa`, `web` and `web2` moved the series out of private
	`aaa` through its occurrence, past `#5097`'s "A credential narrowed to some projects makes no
	crossing move"."""

	place = Place(session, private=True, count=None, keys=("aaa", "web", "web2"))
	narrowed = place.token(
		place.keanu, project_scope=[place.ids[key] for key in ("aaa", "web", "web2")]
	)

	answer = place.ask(
		place.keanu,
		"PATCH",
		f"/v1/tasks/{place.ref}",
		secret=narrowed,
		json={"project": "web2", "applies_to": "from_now_on"},
	)

	assert answer.status_code == 403, answer.text
	assert "whole workspace" in _said(answer), _said(answer)
	assert place.key_of(place.series) == "aaa"


def test_a_series_is_assigned_only_to_somebody_who_can_see_where_it_is (
	session: sqlalchemy.orm.Session,
) -> None:
	"""M-1: assigned from now on through its occurrence in `web`, the series in private `aaa` became
	laurence's, and so did every later occurrence, where a task made in `aaa` for him is refused.
	Shared into `aaa`, he takes it."""

	place = Place(session, private=True, count=None)
	assigning = {"assignee": place.laurence.username, "applies_to": "from_now_on"}

	answer = place.ask(place.keanu, "PATCH", f"/v1/tasks/{place.ref}", json=assigning)

	assert answer.status_code == 422, answer.text
	assert answer.json()["detail"] == (
		f"{place.laurence.username} cannot see aaa, so every one from now on cannot be assigned "
		"to them."
	), _said(answer)
	assert place.series.assignee_id != place.laurence.id

	_shared(place, "aaa", place.laurence)
	allowed = place.ask(place.keanu, "PATCH", f"/v1/tasks/{place.ref}", json=assigning)

	assert allowed.status_code == 200, allowed.text
	assert place.series.assignee_id == place.laurence.id


def test_a_series_edited_from_now_on_reaches_its_occurrence_only_where_the_credential_writes (
	session: sqlalchemy.orm.Session,
) -> None:
	"""M-1, widened: an edit to the series itself from now on is carried to its live occurrence, and
	was asked nothing there, so a credential writing only `aaa` rewrote the occurrence in `web`."""

	place = Place(session, count=None)
	writes_aaa = place.token(
		place.keanu,
		project_scope=[place.ids["aaa"], place.ids["web"]],
		project_write_scope=[place.ids["aaa"]],
	)
	series = place.series

	answer = place.ask(
		place.keanu,
		"PATCH",
		f"/v1/tasks/{series.ref}",
		secret=writes_aaa,
		json={"title": "Water the roses", "applies_to": "from_now_on"},
	)

	shown = place.ask(place.keanu, "GET", f"/v1/tasks/{place.ref}").json()

	assert answer.status_code == 403, answer.text
	assert shown["title"] == "Water the plants"
	assert place.series.title == "Water the plants"


def test_a_series_edited_from_now_on_with_its_occurrence_out_of_sight_says_so_in_the_readers_words (
	session: sqlalchemy.orm.Session,
) -> None:
	"""M-1, widened: the occurrence is in carrieanne's private `hers`, which keanu no longer sees.
	His edit to the series from now on is refused, naming neither `hers` nor the occurrence."""

	place = Place(session, count=None)
	made = place.ask(
		place.carrieanne,
		"POST",
		"/v1/projects",
		json={"key": "hers", "title": "Hers", "visibility": "private"},
	)

	assert made.status_code == 201, made.text

	shared = place.ask(
		place.carrieanne,
		"POST",
		"/v1/projects/hers/members",
		json={"username": place.keanu.username},
	)

	assert shared.status_code in (200, 201), shared.text

	moved = place.ask(
		place.keanu,
		"PATCH",
		f"/v1/tasks/{place.ref}",
		json={"project": "hers", "applies_to": "this_one"},
	)

	assert moved.status_code == 200, moved.text

	unshared = place.ask(
		place.carrieanne, "DELETE", f"/v1/projects/hers/members/{place.keanu.username}"
	)

	assert unshared.status_code == 204, unshared.text

	series = place.series
	renaming = {"title": "Water the roses", "applies_to": "from_now_on"}
	answer = place.ask(place.keanu, "PATCH", f"/v1/tasks/{series.ref}", json=renaming)
	said = _said(answer)

	assert answer.status_code == 422, answer.text
	assert answer.json()["detail"] == (
		"Every one from now on includes one where you cannot see it, so this cannot change from "
		"here."
	), said
	assert "hers" not in said and f"#{place.ref}" not in said, said
	assert place.series.title == "Water the plants"

	alone = place.ask(
		place.keanu, "PATCH", f"/v1/tasks/{series.ref}", json={**renaming, "applies_to": "this_one"}
	)

	assert alone.status_code == 200, alone.text


def test_a_series_under_a_task_stays_in_the_project_its_parent_is_in (
	session: sqlalchemy.orm.Session,
) -> None:
	"""NEW-B-1 of the verification: a weekly sub-task's occurrence, detached from its parent and
	moved from now on, moved its series into `web2` beneath a parent in `web` - the mixed state
	`create` and `update` refuse - and that refusal then kept it there. Just this one still
	moves."""

	place = Place(session, keys=("aaa", "web", "web2"))
	parent = place.ask(
		place.keanu, "POST", "/v1/tasks", json={"title": "Plan the garden", "project": "web"}
	)

	assert parent.status_code == 201, parent.text

	made = place.ask(
		place.keanu,
		"POST",
		"/v1/tasks",
		json={
			"title": "Feed the fish",
			"project": "web",
			"parent_task_id": parent.json()["id"],
			"recurrence": "every week",
			"due": "2031-12-01",
		},
	)

	assert made.status_code == 201, made.text

	ref = made.json()["ref"]
	detached = place.ask(place.keanu, "POST", f"/v1/tasks/{ref}/move", json={"parent": None})

	assert detached.status_code == 200, detached.text

	moving = {"project": "web2", "applies_to": "from_now_on"}
	answer = place.ask(place.keanu, "PATCH", f"/v1/tasks/{ref}", json=moving)

	assert answer.status_code == 422, answer.text
	assert answer.json()["detail"] == (
		f"Every one from now on is part of #{parent.json()['ref']}, which decides where it is "
		"filed."
	), answer.text
	assert place.key_of(place.series_behind(ref)) == "web"

	alone = place.ask(
		place.keanu, "PATCH", f"/v1/tasks/{ref}", json={**moving, "applies_to": "this_one"}
	)

	assert alone.status_code == 200, alone.text
	assert place.key_of(place.series_behind(ref)) == "web"


@pytest.mark.parametrize("change", CHANGES, ids=list(CHANGES))
def test_a_credential_scoped_to_tasks_changes_how_a_repeat_in_its_own_inbox_repeats (
	session: sqlalchemy.orm.Session, change: str
) -> None:
	"""M-4 of the cold review of 2026-11-11: a credential scoped `task:read,task:write`, which reads
	the task and edits just this one, was told for a stop, a new rule and an edit from now on that
	how it repeats was set where it could not see - its own Inbox."""

	suffix = uuid.uuid4().hex[:6]
	keanu = subroutine.domain.users.create(session, username=f"keanu-{suffix}")
	subroutine.domain.workspaces.create(
		session, slug=f"metacortex-{suffix}", title="MetaCortex", owner=keanu
	)
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=keanu, title="capture", scopes=["task:read", "task:write"]
	)
	session.flush()
	application = api_support.build_app(api_support.factory_for(session))
	headers = {"Authorization": f"Bearer {issued.value.get_secret_value()}"}
	made = api_support.call(
		application,
		"POST",
		"/v1/tasks",
		headers=headers,
		json={"title": "Water the plants", "recurrence": "every week", "due": "2031-12-01"},
	)

	assert made.status_code == 201, made.text

	answer = api_support.call(
		application,
		"PATCH",
		f"/v1/tasks/{made.json()['ref']}",
		headers=headers,
		json=CHANGES[change],
	)

	assert answer.status_code == 200, answer.text


@pytest.mark.parametrize("change", CHANGES, ids=list(CHANGES))
def test_a_credential_that_only_reads_where_the_series_is_is_refused_as_beyond_its_places (
	session: sqlalchemy.orm.Session, change: str
) -> None:
	"""M-4, and M-1's write-scope case: a credential reading `aaa` and `web` and writing only `web`
	was told the series was where it could not see, or rewrote it through the occurrence. It hears
	what an edit to the series' own number hears."""

	place = Place(session, count=None)
	reads_aaa = place.token(
		place.keanu,
		project_scope=[place.ids["aaa"], place.ids["web"]],
		project_write_scope=[place.ids["web"]],
	)
	series = place.series

	answer = place.ask(
		place.keanu, "PATCH", f"/v1/tasks/{place.ref}", secret=reads_aaa, json=CHANGES[change]
	)
	direct = place.ask(
		place.keanu,
		"PATCH",
		f"/v1/tasks/{series.ref}",
		secret=reads_aaa,
		json={"title": "Water the roses", "applies_to": "this_one"},
	)

	assert answer.status_code == 403, answer.text
	assert answer.json()["detail"] == direct.json()["detail"], _said(answer)
	assert place.series.title == series.title
	assert place.series.recurrence_rule == series.recurrence_rule
	assert place.series.completed_at is None


@pytest.mark.parametrize("where", ["out of sight", "beneath the trash"])
def test_the_browsers_save_of_an_occurrence_of_a_stopped_series_goes_through_for_just_this_one (
	session: sqlalchemy.orm.Session, where: str
) -> None:
	"""M-4, widened: the browser sends every control, its blank repeat box as `recurrence: null`.
	For a stopped series out of sight or beneath the trash that was refused as a stop even with
	`this_one`, so `#5097`'s answer - refused once, and through when sent again with `this_one` -
	was false for every save the browser makes."""

	place = Place(session, private=where == "out of sight", count=None)
	stopped = place.ask(place.keanu, "PATCH", f"/v1/tasks/{place.ref}", json={"recurrence": None})

	assert stopped.status_code == 200, stopped.text

	reader = place.carrieanne

	if where == "beneath the trash":
		place.trash_aaa()
		reader = place.keanu

	shown = place.ask(reader, "GET", f"/v1/tasks/{place.ref}").json()
	body = {
		"title": "Water the cactus",
		"description": None,
		"due": "2031-12-01",
		"recurrence": None,
		"tags": [],
		"expected_version": shown["version"],
		"applies_to": "this_one",
	}

	answer = place.ask(reader, "PATCH", f"/v1/tasks/{place.ref}", json=body)

	assert answer.status_code == 200, answer.text
	assert answer.json()["title"] == "Water the cactus"


@pytest.mark.parametrize("change", CHANGES, ids=list(CHANGES))
def test_a_project_in_the_trash_is_not_named_to_a_reader_who_cannot_see_it (
	session: sqlalchemy.orm.Session, change: str
) -> None:
	"""NT1 of M-17 in the cold review of 2026-11-11: no test sent the trash's refusal to a reader
	who could not see the place in the trash, so the line keeping `aaa` from them could go
	unnoticed."""

	place = Place(session, private=True, count=None)
	place.trash_aaa()
	series = place.series

	answer = place.ask(place.carrieanne, "PATCH", f"/v1/tasks/{place.ref}", json=CHANGES[change])
	said = _said(answer)

	assert answer.status_code == 422, answer.text
	assert answer.json()["detail"] == (
		"How this repeats cannot change until something you cannot see is restored."
	), said
	assert "Ask somebody who can see it to restore it first" in said, said
	assert "aaa" not in said and f"#{series.ref}" not in said, said


def test_a_task_in_the_trash_above_a_series_is_named_only_to_a_reader_who_can_see_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""NT2 of M-17: no test reached a series beneath a task in the trash, so the check of whether
	its reader could see that task could be reduced to whether it was filed anywhere."""

	place = Place(session, private=True, count=None)
	parent = place.ask(
		place.keanu, "POST", "/v1/tasks", json={"title": "Plan the garden", "project": "aaa"}
	)

	assert parent.status_code == 201, parent.text

	made = place.ask(
		place.keanu,
		"POST",
		"/v1/tasks",
		json={
			"title": "Feed the fish",
			"project": "aaa",
			"parent_task_id": parent.json()["id"],
			"recurrence": "every week",
			"due": "2031-12-01",
		},
	)

	assert made.status_code == 201, made.text

	ref = made.json()["ref"]

	detached = place.ask(place.keanu, "POST", f"/v1/tasks/{ref}/move", json={"parent": None})

	assert detached.status_code == 200, detached.text

	moved = place.ask(
		place.keanu,
		"PATCH",
		f"/v1/tasks/{ref}",
		json={"project": "web", "assignee": place.carrieanne.username, "applies_to": "this_one"},
	)

	assert moved.status_code == 200, moved.text

	gone = place.ask(place.keanu, "DELETE", f"/v1/tasks/{parent.json()['ref']}")

	assert gone.status_code in (200, 204), gone.text

	named = f"#{parent.json()['ref']}"

	for reader, sees in ((place.carrieanne, False), (place.keanu, True)):
		answer = place.ask(reader, "PATCH", f"/v1/tasks/{ref}", json={"recurrence": None})
		said = _said(answer)

		assert answer.status_code == 422, answer.text
		assert (named in said) == sees, said
		assert ("something you cannot see" in said) == (not sees), said
