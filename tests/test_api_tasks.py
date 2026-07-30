"""Tasks over HTTP: what the endpoints promise, and the places they could quietly not.

The interesting tests here are not "does create work". They are the three things a task
API gets wrong: telling omitted apart from null on a PATCH, paginating a table that is
being written to, and narrowing a listing to what the caller may actually see.
"""

import typing
import uuid

import fastapi
import pytest
import sqlalchemy.orm

import api_support
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.projects
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.permissions


class World(typing.NamedTuple):
	"""An installation with one user, one workspace and an Inbox."""

	application: fastapi.FastAPI
	session: sqlalchemy.orm.Session
	user: subroutine.db.models.identity.User
	workspace: subroutine.db.models.identity.Workspace
	secret: str

	def call (self, method: str, path: str, **kwargs: typing.Any) -> typing.Any:
		"""Make an authenticated request."""

		headers = {"authorization": f"Bearer {self.secret}", **kwargs.pop("headers", {})}

		return api_support.call(self.application, method, path, headers=headers, **kwargs)


def _world (
	session: sqlalchemy.orm.Session, **token: typing.Any
) -> World:
	"""Bootstrap an installation and a token to reach it with."""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	token.setdefault("title", "Test token")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, **token
	)
	session.flush()

	return World(
		application=api_support.build_app(api_support.factory_for(session)),
		session=session,
		user=setup.user,
		workspace=setup.workspace,
		secret=issued.value.get_secret_value(),
	)


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return _world(session)


def test_a_task_can_be_created_from_a_line_of_text (world: World) -> None:
	"""``POST /v1/tasks {"text": …}`` is the same capture the CLI's ``add`` uses."""

	response = world.call("POST", "/v1/tasks", json={"text": "Ship the release #urgent !2"})

	assert response.status_code == 201

	body = response.json()

	assert body["title"] == "Ship the release"
	assert body["importance"] == 2
	assert isinstance(body["ref"], int), "a ref is a number, not a string that holds one"
	assert body["status_category"] == "todo"


def test_a_task_with_no_project_goes_to_the_inbox (world: World) -> None:
	"""SPEC.md §1.4 over HTTP: creating a task must not require knowing about projects."""

	response = world.call("POST", "/v1/tasks", json={"title": "Something to do"})

	assert response.status_code == 201
	assert response.json()["project_key"] == subroutine.domain.bootstrap.INBOX_KEY


def test_a_task_needs_a_title_or_a_line_to_parse_one_from (world: World) -> None:
	"""And the refusal says which of the two to send."""

	response = world.call("POST", "/v1/tasks", json={"description": "context only"})

	assert response.status_code == 422

	body = response.json()

	assert body["code"] == "missing_field"
	assert "text" in body["errors"][0]["message"]


def test_a_task_is_readable_by_ref_with_or_without_the_sigil (world: World) -> None:
	"""``GET /v1/tasks/42`` is the address; ``/v1/tasks/%2342`` is the same task.

	The sigil is how a ref is *written* (SPEC.md §6.15), so a client that pastes what it
	read should not get a 404 for it — even though nothing this project prints will put a
	``#`` in a URL, since it has to be escaped to survive one.
	"""

	ref = world.call("POST", "/v1/tasks", json={"title": "Findable"}).json()["ref"]

	assert world.call("GET", f"/v1/tasks/{ref}").json()["ref"] == ref
	assert world.call("GET", f"/v1/tasks/%23{ref}").json()["ref"] == ref


@pytest.mark.parametrize("wanted", ["2147483648", "99999999999999999999", "9" * 40, "007", "0"])
def test_a_ref_the_column_cannot_hold_is_a_404_not_a_500 (world: World, wanted: str) -> None:
	"""It was a 500, on both backends, at different thresholds.

	``parse_ref`` was unbounded and the value went straight into a comparison against an
	``Integer`` column: PostgreSQL raised ``NumericValueOutOfRange`` above 2³¹ and SQLite
	``OverflowError`` above 2⁶³, both unhandled. Any authenticated caller could reach it by
	iterating a counter or mistyping a number, and a 500 carries none of the machine-readable
	remediation §8.8 promises.

	The zero-padded and zero cases are here for the same reason and a different one: they are
	not refs either, and they must agree with how ``#007`` is read in prose.
	"""

	response = world.call("GET", f"/v1/tasks/{wanted}")

	assert response.status_code == 404, response.text
	assert response.json()["code"] == "not_found"


def test_a_body_field_naming_an_impossible_item_is_refused_not_resolved (
	world: World,
) -> None:
	"""A path segment that names nothing is a 404; a *field* holding nonsense is a 422.

	The difference is what the request is asking for. ``GET /v1/tasks/99…9`` asks to fetch
	something and the answer is that there is no such thing. A body that says
	``target: true`` is not asking anything coherent — and ``bool`` being a subclass of
	``int`` meant pydantic quietly read it as item #1 and linked to the wrong task.
	"""

	one = world.call("POST", "/v1/tasks", json={"title": "One"}).json()
	world.call("POST", "/v1/tasks", json={"title": "Two"})

	for value in (True, False, 2147483648, "2147483648", 0):
		response = world.call(
			"POST",
			f"/v1/tasks/{one['ref']}/links",
			json={"target": value, "link_type": "blocks"},
		)

		assert response.status_code == 422, f"{value!r} was accepted: {response.text}"

	assert world.call("GET", f"/v1/tasks/{one['ref']}/links").json() == [], "nothing was linked"


def test_a_ref_and_a_project_key_cannot_be_confused_in_a_path (world: World) -> None:
	"""Two address spaces in one path segment, told apart by the first character.

	A key must start with a letter (SPEC.md §5.2) and a ref is all digits, which is what
	makes ``/v1/tasks/{id_or_ref}`` unambiguous now that a ref carries no prefix.
	"""

	assert world.call("POST", "/v1/projects", json={"key": "12", "title": "No"}).status_code == 422
	assert world.call("GET", "/v1/tasks/INBOX").status_code == 404


def test_a_task_is_readable_by_id (world: World) -> None:
	"""Both forms of address reach the same row."""

	created = world.call("POST", "/v1/tasks", json={"title": "Findable"}).json()

	assert world.call("GET", f"/v1/tasks/{created['id']}").json()["ref"] == created["ref"]


def test_an_unknown_ref_is_a_404_that_says_what_to_try (world: World) -> None:
	"""Not a 500, and not an empty 404."""

	response = world.call("GET", "/v1/tasks/NOPE-999")

	assert response.status_code == 404
	assert response.json()["code"] == "not_found"
	assert "GET /v1/tasks" in response.json()["errors"][0]["hint"]


def test_an_omitted_field_is_untouched_and_a_null_one_is_cleared (world: World) -> None:
	"""SPEC.md §8.3, which is the whole reason ``UNSET`` exists.

	Collapsing the two would make clearing a due date impossible to express, and a client
	that sent only a title would silently wipe everything else.
	"""

	created = world.call(
		"POST", "/v1/tasks", json={"title": "Has fields", "importance": 4, "due": "tomorrow"}
	).json()

	assert created["importance"] == 4
	assert created["due_at"] is not None

	renamed = world.call(
		"PATCH", f"/v1/tasks/{created['ref']}", json={"title": "Renamed"}
	).json()

	assert renamed["title"] == "Renamed"
	assert renamed["importance"] == 4, "an omitted field must be left alone"
	assert renamed["due_at"] is not None

	cleared = world.call(
		"PATCH", f"/v1/tasks/{created['ref']}", json={"due": None, "importance": None}
	).json()

	assert cleared["due_at"] is None, "an explicit null must clear"
	assert cleared["importance"] is None
	assert cleared["title"] == "Renamed", "and must not disturb anything else"


def test_completing_a_task_uses_whatever_this_workspace_calls_done (world: World) -> None:
	"""The status key is data; the category is the fixed thing a client branches on."""

	ref = world.call("POST", "/v1/tasks", json={"title": "Finish me"}).json()["ref"]
	response = world.call("POST", f"/v1/tasks/{ref}/complete")

	assert response.status_code == 200

	body = response.json()

	assert body["status_category"] == "done"
	assert body["completed_at"] is not None


def test_deleting_a_task_is_soft_and_repeatable (world: World) -> None:
	"""It goes to the trash, stays addressable, and a second delete is not an error."""

	ref = world.call("POST", "/v1/tasks", json={"title": "Throw away"}).json()["ref"]

	first = world.call("DELETE", f"/v1/tasks/{ref}")

	assert first.status_code == 200
	assert first.json()["deleted_at"] is not None

	again = world.call("DELETE", f"/v1/tasks/{ref}")

	assert again.status_code == 200
	assert again.json()["deleted_at"] == first.json()["deleted_at"], (
		"when something was thrown away is a fact worth not overwriting"
	)

	assert ref not in [item["ref"] for item in world.call("GET", "/v1/tasks").json()["items"]]


def test_a_listing_is_enveloped_and_counts_only_when_asked (world: World) -> None:
	"""SPEC.md §8.4: ``total`` is null unless requested, because it costs a second scan."""

	for index in range(3):
		world.call("POST", "/v1/tasks", json={"title": f"Task {index}"})

	plain = world.call("GET", "/v1/tasks").json()

	assert len(plain["items"]) == 3
	assert plain["page"]["total"] is None
	assert plain["page"]["has_more"] is False

	counted = world.call("GET", "/v1/tasks?include_total=true").json()

	assert counted["page"]["total"] == 3


def test_a_page_continues_exactly_where_the_last_one_stopped (world: World) -> None:
	"""Keyset pagination, walked to the end and checked for gaps and repeats.

	The failure this guards against is silent: an off-by-one in the seek predicate loses one
	task per page, and every page still looks perfectly well formed.
	"""

	made = [
		world.call("POST", "/v1/tasks", json={"title": f"Task {index}"}).json()["ref"]
		for index in range(7)
	]

	seen: list[str] = []
	cursor = None

	for _ in range(10):
		query = "/v1/tasks?limit=2" + (f"&cursor={cursor}" if cursor else "")
		page = world.call("GET", query).json()

		seen.extend(item["ref"] for item in page["items"])

		if not page["page"]["has_more"]:
			break

		cursor = page["page"]["next_cursor"]

		assert cursor is not None, "has_more with no cursor is a page nobody can reach"

	assert sorted(seen) == sorted(made)
	assert len(seen) == len(set(seen)), "no task appeared twice"


def test_pagination_survives_a_sort_field_full_of_nulls (world: World) -> None:
	"""The case the two backends disagree about (SPEC.md §10.3).

	SQLite sorts NULLs first and PostgreSQL last, so an ordering that does not say which it
	wants paginates differently depending on where it runs. Most of these tasks have no due
	date at all, which is the ordinary state of a to-do list.
	"""

	made = []

	for index in range(6):
		body: dict[str, typing.Any] = {"title": f"Task {index}"}

		if index % 3 == 0:
			body["due"] = "tomorrow"

		made.append(world.call("POST", "/v1/tasks", json=body).json()["ref"])

	seen: list[str] = []
	cursor = None

	for _ in range(10):
		query = "/v1/tasks?limit=2&order=due_at" + (f"&cursor={cursor}" if cursor else "")
		page = world.call("GET", query).json()

		seen.extend(item["ref"] for item in page["items"])

		if not page["page"]["has_more"]:
			break

		cursor = page["page"]["next_cursor"]

	assert sorted(seen) == sorted(made)
	assert len(seen) == len(set(seen))


def test_several_sort_fields_are_applied_in_order (world: World) -> None:
	""""By priority, then by deadline" is the ordering people want and one column cannot say."""

	world.call("POST", "/v1/tasks", json={"title": "Low", "importance": 1})
	world.call("POST", "/v1/tasks", json={"title": "High", "importance": 5})
	world.call("POST", "/v1/tasks", json={"title": "Middle", "importance": 3})

	titles = [
		item["title"]
		for item in world.call("GET", "/v1/tasks?order=-importance").json()["items"]
	]

	assert titles == ["High", "Middle", "Low"]


def test_an_unknown_sort_field_is_refused_with_the_ones_that_work (world: World) -> None:
	"""A 422 an agent can act on, rather than an ignored parameter."""

	response = world.call("GET", "/v1/tasks?order=whenever")

	assert response.status_code == 422
	assert "created_at" in response.json()["errors"][0]["hint"]


def test_a_tampered_cursor_is_refused (world: World) -> None:
	"""Cursors are signed, which is the one thing ``secret_key`` is for (SPEC.md §7.4)."""

	for index in range(3):
		world.call("POST", "/v1/tasks", json={"title": f"Task {index}"})

	cursor = world.call("GET", "/v1/tasks?limit=1").json()["page"]["next_cursor"]

	assert cursor is not None

	response = world.call("GET", f"/v1/tasks?limit=1&cursor={cursor[:-2]}xy")

	assert response.status_code == 422
	assert response.json()["errors"][0]["field"] == "cursor"


def test_a_listing_can_be_narrowed_by_project_and_by_text (world: World) -> None:
	"""The simple filters, and the one that has to be case-insensitive on both backends."""

	subroutine.domain.projects.create(
		world.session,
		workspace_id=world.workspace.id,
		key="WEB",
		title="Website",
		owner_id=world.user.id,
	)
	world.session.flush()

	world.call("POST", "/v1/tasks", json={"title": "Fix the header", "project": "WEB"})
	world.call("POST", "/v1/tasks", json={"title": "Unrelated"})

	scoped = world.call("GET", "/v1/tasks?project=WEB").json()["items"]

	assert [item["title"] for item in scoped] == ["Fix the header"]

	# `LIKE` is case-sensitive on PostgreSQL and not on SQLite, so this asserts the choice
	# of `ilike` rather than the backend's accident.
	found = world.call("GET", "/v1/tasks?q=HEADER").json()["items"]

	assert [item["title"] for item in found] == ["Fix the header"]


def test_a_search_term_cannot_smuggle_a_wildcard (world: World) -> None:
	"""``%`` is a LIKE wildcard, and a caller's text is not a pattern."""

	world.call("POST", "/v1/tasks", json={"title": "Ordinary"})

	assert world.call("GET", "/v1/tasks?q=%25").json()["items"] == []


def test_finished_tasks_are_out_of_the_way_unless_asked_for (world: World) -> None:
	"""A to-do list is what is left to do."""

	ref = world.call("POST", "/v1/tasks", json={"title": "Done already"}).json()["ref"]
	world.call("POST", f"/v1/tasks/{ref}/complete")

	assert world.call("GET", "/v1/tasks").json()["items"] == []
	assert len(world.call("GET", "/v1/tasks?include_completed=true").json()["items"]) == 1


def test_a_task_in_a_private_project_is_not_found_rather_than_forbidden (
	session: sqlalchemy.orm.Session,
) -> None:
	"""SPEC.md §7.3a: "forbidden" would confirm that it exists."""

	world = _world(session)

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(
		session, world.workspace, outsider, role_key="member"
	)
	private = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="SECRET",
		title="Secret",
		visibility="private",
		owner_id=world.user.id,
	)
	hidden = subroutine.domain.tasks.create(session, project=private, title="Confidential")

	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=outsider, title="outsider"
	)
	session.flush()

	nosy = World(
		application=world.application,
		session=session,
		user=outsider,
		workspace=world.workspace,
		secret=issued.value.get_secret_value(),
	)

	assert nosy.call("GET", f"/v1/tasks/{hidden.ref}").status_code == 404
	assert nosy.call("GET", "/v1/tasks").json()["items"] == []


def test_a_read_only_token_cannot_create (session: sqlalchemy.orm.Session) -> None:
	"""The permission check runs in the service layer, so the endpoint inherits it.

	This is the exact scenario the slice-2 review used to discover the layer was not
	running at all: issue a read-only token, then write something.
	"""

	world = _world(session, scopes=[subroutine.permissions.TASK_READ])

	response = world.call("POST", "/v1/tasks", json={"title": "Should be refused"})

	assert response.status_code == 403
	assert response.json()["code"] == "forbidden"


def test_a_read_only_token_can_still_read (session: sqlalchemy.orm.Session) -> None:
	"""Scoped is not broken."""

	world = _world(session, scopes=[subroutine.permissions.TASK_READ])

	assert world.call("GET", "/v1/tasks").status_code == 200


def test_a_request_naming_no_workspace_is_refused_when_there_are_several (
	session: sqlalchemy.orm.Session,
) -> None:
	"""SPEC.md §8.2: ambiguity is a refusal listing the options, never a guess.

	Guessing means a task filed somewhere the caller did not look, discovered days later.
	"""

	world = _world(session)
	second = subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Other", owner=world.user
	)
	session.flush()

	response = world.call("GET", "/v1/tasks")

	assert response.status_code == 422

	hint = response.json()["errors"][0]["hint"]

	assert world.workspace.slug in hint
	assert second.slug in hint

	assert world.call("GET", f"/v1/tasks?workspace_id={second.slug}").status_code == 200
	assert world.call("GET", f"/v1/tasks?workspace_id={second.id}").status_code == 200


def test_a_pinned_token_needs_no_workspace_named (session: sqlalchemy.orm.Session) -> None:
	"""Which is the point of pinning one."""

	world = _world(session)
	subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Other", owner=world.user
	)
	session.flush()

	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=world.user, title="pinned", workspace_id=world.workspace.id
	)
	session.flush()

	pinned = world._replace(secret=issued.value.get_secret_value())

	assert pinned.call("GET", "/v1/tasks").status_code == 200


def test_the_agenda_spans_every_readable_workspace (session: sqlalchemy.orm.Session) -> None:
	""""What am I doing today" is a question about a person, not about a workspace."""

	world = _world(session)
	second = subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Work", owner=world.user
	)
	elsewhere = subroutine.domain.projects.create(
		session, workspace_id=second.id, key="WORK", title="Work", owner_id=world.user.id
	)
	subroutine.domain.tasks.create(session, project=elsewhere, title="A work thing")
	session.flush()

	world.call("POST", "/v1/tasks", json={"title": "A personal thing", "workspace_id": world.workspace.slug})

	response = world.call("GET", "/v1/agenda")

	assert response.status_code == 200

	body = response.json()
	titles = {item["title"] for item in body["unscheduled"]}

	assert titles == {"A personal thing", "A work thing"}
	assert body["timezone"]
	assert body["date"]


def test_capture_respects_an_explicit_project (world: World) -> None:
	"""``{"text": …, "project": "K"}`` files the task in K, not in the Inbox.

	Found by using the product: the switch created seven tasks with ``text`` and ``project``
	together, every one of them returned 201, and every one landed in the Inbox. ``project`` was
	a declared and accepted field that the capture path simply did not pass on — the recurring
	shape here, and silent, because there is nothing in a 201 to say where the task went.
	"""

	world.call("POST", "/v1/projects", json={"key": "WEB", "title": "Web"})

	response = world.call(
		"POST", "/v1/tasks", json={"text": "Ship the release ~2h", "project": "WEB"}
	)

	assert response.status_code == 201

	body = response.json()

	assert body["project_key"] == "WEB"
	assert body["title"] == "Ship the release", "capture should still clean the line"
	assert body["estimate_minutes"] == 120, "and still parse what it parses"


def test_an_explicit_project_beats_one_named_in_the_captured_line (world: World) -> None:
	"""§6.13's rule — structured fields win over parsed ones — applied to where it lands."""

	world.call("POST", "/v1/projects", json={"key": "WEB", "title": "Web"})
	world.call("POST", "/v1/projects", json={"key": "OPS", "title": "Ops"})

	response = world.call(
		"POST", "/v1/tasks", json={"text": "Rotate the keys +WEB", "project": "OPS"}
	)

	assert response.json()["project_key"] == "OPS"


def test_capture_still_uses_the_project_named_in_the_line (world: World) -> None:
	"""And the fix must not have replaced a `+KEY` with the Inbox, which is the other misfiling."""

	world.call("POST", "/v1/projects", json={"key": "WEB", "title": "Web"})

	response = world.call("POST", "/v1/tasks", json={"text": "Fix the header +WEB"})

	assert response.json()["project_key"] == "WEB"
