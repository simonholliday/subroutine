"""Tasks over HTTP: what the endpoints promise, and the places they could quietly not.

The interesting tests here are not "does create work". They are the three things a task
API gets wrong: telling omitted apart from null on a PATCH, paginating a table that is
being written to, and narrowing a listing to what the caller may actually see.
"""

import itertools
import typing
import uuid

import fastapi
import pytest
import sqlalchemy.event
import sqlalchemy.orm

import api_support
import subroutine.api.tasks
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.ordering
import subroutine.domain.projects
import subroutine.domain.search
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
	session: sqlalchemy.orm.Session,
	*,
	instance: dict[str, typing.Any] | None = None,
	**token: typing.Any,
) -> World:
	"""Bootstrap an installation and a token to reach it with.

	``instance`` overrides the settings the application is built with, for the handful of
	behaviours that are configuration rather than code — ``search_backend`` is the first
	(`#823`), and a test that needs the indexed one cannot get there any other way.
	"""

	setup = subroutine.domain.bootstrap.initialise(
		session, username=f"si-{uuid.uuid4().hex[:8]}", instance_name="Test"
	)
	token.setdefault("title", "Test token")
	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=setup.user, **token
	)
	session.flush()

	return World(
		application=api_support.build_app(api_support.factory_for(session), **(instance or {})),
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

	assert (
		world.call("GET", f"/v1/tasks/{one['ref']}/links").json()["items"] == []
	), "nothing was linked"


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


def test_the_all_day_flag_can_be_changed_without_resending_the_date (world: World) -> None:
	"""`#195`. It was declared, documented, reported — and dropped on the way in.

	``due_is_all_day`` was a plain argument on the service rather than a patch sentinel, so it
	was consulted only when ``due`` was being set too. Sent on its own it produced ``200 OK``,
	changed nothing, and left ``version`` where it was: a correctly spelled, published field
	silently discarded, which is what the ``unknown_field`` refusal exists to argue against —
	and worse, because a typo at least gets refused.

	Both directions, because they are two different answers and only one of them is "leave the
	instant alone". Off keeps the moment the task already had and reports it as a moment; on
	snaps to the boundary of that local day, which is §6.5's rule and the reason a task due
	"Friday" is not overdue on Friday morning.
	"""

	created = world.call(
		"POST", "/v1/tasks", json={"title": "Ship it", "due": "2026-09-01"}
	).json()

	assert created["due_is_all_day"] is True
	assert created["due_at"] == "2026-09-01T23:59:59.999999Z"

	instant = world.call(
		"PATCH", f"/v1/tasks/{created['ref']}", json={"due_is_all_day": False}
	).json()

	assert instant["due_is_all_day"] is False
	assert instant["due_at"] == created["due_at"], "the flag says how to read it, not when"
	assert instant["version"] > created["version"], "a change that happened has to be a change"

	# The other way, from a time to a day: the boundary is applied, not the clock.
	timed = world.call(
		"PATCH", f"/v1/tasks/{created['ref']}", json={"due": "2026-09-01T14:00:00Z"}
	).json()

	assert timed["due_is_all_day"] is False

	whole = world.call(
		"PATCH", f"/v1/tasks/{created['ref']}", json={"due_is_all_day": True}
	).json()

	assert whole["due_is_all_day"] is True
	assert whole["due_at"] == "2026-09-01T23:59:59.999999Z"


def test_the_all_day_flag_is_refused_when_there_is_no_date_to_describe (
	world: World,
) -> None:
	"""The one combination that cannot mean anything, and so the one that must not be silent.

	Storing it against a null would put `#195` back in a smaller form: a request that reports
	success and leaves the caller believing something they cannot see is not true.
	"""

	created = world.call("POST", "/v1/tasks", json={"title": "No dates at all"}).json()
	response = world.call(
		"PATCH", f"/v1/tasks/{created['ref']}", json={"start_is_all_day": True}
	)

	assert response.status_code == 422

	body = response.json()

	assert body["errors"][0]["field"] == "start_is_all_day", "and it names which one"
	assert "start" in body["errors"][0]["hint"], "and what to send instead"


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
		key="web",
		title="Website",
		owner_id=world.user.id,
	)
	world.session.flush()

	world.call("POST", "/v1/tasks", json={"title": "Fix the header", "project": "web"})
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
		key="secret",
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
		session, workspace_id=second.id, key="work", title="Work", owner_id=world.user.id
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

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Web"})

	response = world.call(
		"POST", "/v1/tasks", json={"text": "Ship the release ~2h", "project": "web"}
	)

	assert response.status_code == 201

	body = response.json()

	assert body["project_key"] == "web"
	assert body["title"] == "Ship the release", "capture should still clean the line"
	assert body["estimate_minutes"] == 120, "and still parse what it parses"


def test_an_explicit_project_beats_one_named_in_the_captured_line (world: World) -> None:
	"""§6.13's rule — structured fields win over parsed ones — applied to where it lands."""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Web"})
	world.call("POST", "/v1/projects", json={"key": "ops", "title": "Ops"})

	response = world.call(
		"POST", "/v1/tasks", json={"text": "Rotate the keys +web", "project": "ops"}
	)

	assert response.json()["project_key"] == "ops"


def test_capture_still_uses_the_project_named_in_the_line (world: World) -> None:
	"""And the fix must not have replaced a `+KEY` with the Inbox, which is the other misfiling."""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Web"})

	response = world.call("POST", "/v1/tasks", json={"text": "Fix the header +web"})

	assert response.json()["project_key"] == "web"


def test_an_estimate_can_be_given_at_creation_and_revised_afterwards (world: World) -> None:
	"""SPEC.md §6.4 through the API rather than only through a captured line.

	Until 2026-07-30 ``estimate_minutes`` was reported by the view, printed by ``show``,
	drawn on the compact line and published in ``/v1/meta``, and could be set only by the
	``~4h`` token of a quick-capture line. Both endpoints refused ``estimate`` and
	``estimate_minutes`` with a 422, so an estimate could come only from whoever typed the
	original sentence and could never be revised — which is what made the roadmap in ``#18``
	unbuildable, six of twelve items having no estimate that nobody was able to supply.
	"""

	created = world.call("POST", "/v1/tasks", json={"title": "Build the thing", "estimate": "4h"})

	assert created.status_code == 201
	assert created.json()["estimate_minutes"] == 240

	ref = created.json()["ref"]
	revised = world.call("PATCH", f"/v1/tasks/{ref}", json={"estimate": "1h30m"})

	assert revised.status_code == 200
	assert revised.json()["estimate_minutes"] == 90


def test_an_estimate_can_be_withdrawn_rather_than_only_replaced (world: World) -> None:
	"""§8.3: a field sent as ``null`` is cleared, and an estimate is not an exception.

	A guess that turned out to be meaningless should be removable. Leaving a wrong number
	because the only alternative is a different wrong number is how an unreliable estimate
	becomes a permanent one.
	"""

	created = world.call("POST", "/v1/tasks", json={"title": "Unknowable", "estimate": 90})

	assert created.json()["estimate_minutes"] == 90

	ref = created.json()["ref"]
	cleared = world.call("PATCH", f"/v1/tasks/{ref}", json={"estimate": None})

	assert cleared.status_code == 200
	assert cleared.json()["estimate_minutes"] is None


def test_an_unparseable_estimate_is_refused_before_the_task_is_touched (world: World) -> None:
	"""The validation pass in ``tasks.update`` must reject before anything is assigned.

	The caller holds a session it may still commit, so a refusal that arrived after a
	partial assignment would commit half the change alongside whatever else was in flight.
	"""

	created = world.call("POST", "/v1/tasks", json={"title": "Careful", "estimate": "2h"})
	ref = created.json()["ref"]

	refused = world.call("PATCH", f"/v1/tasks/{ref}", json={"title": "Renamed", "estimate": "2x"})

	assert refused.status_code == 422

	unchanged = world.call("GET", f"/v1/tasks/{ref}").json()

	assert unchanged["title"] == "Careful", "the title moved despite the estimate being refused"
	assert unchanged["estimate_minutes"] == 120


def test_an_explicit_estimate_beats_the_one_in_the_captured_line (world: World) -> None:
	"""SPEC.md §6.13: structured fields win over parsed ones, ``estimate`` included.

	It used to be enforced by a condition nothing could satisfy — ``create`` had no
	``estimate`` parameter, so the override it checked for would have raised ``TypeError``
	before reaching the guard. The rule now holds by the same ``fields.update(overrides)``
	every other field goes through.
	"""

	response = world.call(
		"POST", "/v1/tasks", json={"text": "Write the report ~30m", "estimate": "3h"}
	)

	assert response.status_code == 201

	body = response.json()

	assert body["estimate_minutes"] == 180
	assert body["title"] == "Write the report", "the token is still stripped from the title"


def test_a_response_says_the_estimate_in_both_spellings (world: World) -> None:
	"""SPEC.md §6.4: a response carries ``estimate_minutes`` *and* ``estimate_human``.

	It said so from the beginning and only the first was ever rendered — §6.4 itself, the
	module docstring of ``domain.durations`` and a test docstring in ``test_durations.py``
	all described a response field no response contained. ``humanize`` was correct and
	reachable only from the CLI.

	The round trip is the reason it is worth the bytes: what a caller reads here is exactly
	what ``estimate`` accepts on the way back, so an agent can echo a value it did not
	parse.
	"""

	created = world.call("POST", "/v1/tasks", json={"title": "Long one", "estimate": "1h30m"})

	assert created.json()["estimate_human"] == "1h 30m"

	ref = created.json()["ref"]
	echoed = world.call(
		"PATCH", f"/v1/tasks/{ref}", json={"estimate": created.json()["estimate_human"]}
	)

	assert echoed.status_code == 200
	assert echoed.json()["estimate_minutes"] == 90


def test_a_task_with_no_estimate_says_so_in_both_spellings (world: World) -> None:
	"""Null rather than an empty string: a task nobody has sized is not a task sized at zero."""

	created = world.call("POST", "/v1/tasks", json={"title": "Unsized"})

	assert created.json()["estimate_minutes"] is None
	assert created.json()["estimate_human"] is None


@pytest.mark.parametrize("field", sorted(subroutine.api.tasks.SORTABLE))
def test_every_sortable_field_can_be_paged_through (world: World, field: str) -> None:
	"""Every ordering this endpoint advertises must survive a cursor.

	Read from ``SORTABLE`` itself rather than from a list written out here, so a sort field
	added to the endpoint is covered by this test the moment it is declared. That matters
	because of what it was written for.

	``priority_score`` — §6.3's derived key, the one ``/v1/docs/agent`` recommends to an
	agent working a backlog — was declared as a bare ``importance * urgency`` expression. It
	*ordered* perfectly. But a cursor has to name the row it stopped at, and ``encode`` read
	each sort value with ``getattr(row, key.column.key)``; a computed expression has a
	``.key`` of ``None``, so every request whose result set exceeded one page died with
	``TypeError: attribute name must be string, not 'NoneType'``.

	Nothing caught it. The test above walks pages in the *default* order; ``/v1/meta``
	publishes the field name without exercising it; the only installation using it had
	fifteen items and a default page size of fifty, so no page ever filled. It surfaced by
	accident, asking for three rows instead of fifty. **The sort recommended for agents was
	the one that broke as soon as there was enough work to page through** — which is the
	worst possible distribution for a defect, since it is absent exactly while a project is
	small enough for anyone to be looking.
	"""

	for index in range(5):
		world.call(
			"POST",
			"/v1/tasks",
			json={
				"title": f"Task {index}",
				# Both axes on some and neither on others, so `priority_score` is null for
				# part of the page: with NULLS LAST that is the case the seek predicate
				# treats specially, and a derived key has to get it right too.
				"importance": (index % 5) + 1 if index % 2 == 0 else None,
				"urgency": 5 - (index % 5) if index % 2 == 0 else None,
				"due": "tomorrow" if index % 3 == 0 else None,
				"planned_for": "today" if index % 4 == 0 else None,
			},
		)

	for direction in ("", "-"):
		seen: list[int] = []
		cursor = None

		for _ in range(10):
			query = f"/v1/tasks?limit=2&order={direction}{field}" + (
				f"&cursor={cursor}" if cursor else ""
			)
			response = world.call("GET", query)

			assert response.status_code == 200, (
				f"paging by {direction}{field} failed: {response.json()}"
			)

			page = response.json()
			seen.extend(item["ref"] for item in page["items"])

			if not page["page"]["has_more"]:
				break

			cursor = page["page"]["next_cursor"]

			assert cursor is not None, "has_more with no cursor is a page nobody can reach"

		assert len(seen) == 5, f"paging by {direction}{field} returned {len(seen)} of 5"
		assert len(seen) == len(set(seen)), f"paging by {direction}{field} repeated a task"


def _linked (world: World, count: int) -> list[int]:
	"""Create ``count`` tasks in a chain, each blocking the next, and return their refs."""

	refs = [
		world.call("POST", "/v1/tasks", json={"title": f"Step {index}"}).json()["ref"]
		for index in range(count)
	]

	for earlier, later in itertools.pairwise(refs):
		world.call(
			"POST",
			f"/v1/tasks/{earlier}/links",
			json={"target": later, "target_type": "task", "link_type": "blocks"},
		)

	return refs


def test_a_listing_can_return_the_links_among_its_items (world: World) -> None:
	"""``?include=links`` answers "what depends on what" without a request per item.

	Assembling this project's own backlog took thirteen requests and 18,759 bytes on
	2026-07-30 — one listing plus one ``/links`` sub-resource per task — because a link was
	readable only as a sub-resource of the thing it hung off. That is the wrong shape for an
	agent: a person reads one item's links while thinking about that item, an agent wants the
	shape of the whole backlog *before* deciding anything.
	"""

	refs = _linked(world, 4)
	body = world.call("GET", "/v1/tasks?include=links&limit=50").json()

	assert len(body["items"]) == 4

	edges = {(edge["source"]["ref"], edge["target"]["ref"]) for edge in body["links"]}

	assert edges == set(itertools.pairwise(refs))


def test_a_link_with_both_ends_on_the_page_is_reported_once (world: World) -> None:
	"""The reason this is an edge list and not a ``links`` field on every item.

	A link is one stored row. Hung off each end it appears twice, in opposite directions,
	and a caller building a graph has to notice the two are the same fact — on a page of one
	project's backlog, where ``#12 blocks #13`` has *both* ends present, that is the common
	case rather than a corner.
	"""

	refs = _linked(world, 2)
	body = world.call("GET", "/v1/tasks?include=links&limit=50").json()

	assert {item["ref"] for item in body["items"]} == set(refs), "both ends are on the page"
	assert len(body["links"]) == 1, "one stored row, reported once"

	edge = body["links"][0]

	assert (edge["source"]["ref"], edge["target"]["ref"]) == (refs[0], refs[1])
	assert edge["label"] == "Blocks", "the forward title; the inverse is read from the target"


def test_a_listing_that_did_not_ask_for_links_carries_no_links_key (world: World) -> None:
	"""Omitted, not null. A listing that did not ask is byte-for-byte what it was.

	§14.10 exists because response size is a first-order cost for an agent, so a feature that
	added a key to every response of every listing to say "you did not use me" would be taking
	with one hand what it gives with the other.
	"""

	_linked(world, 2)

	assert "links" not in world.call("GET", "/v1/tasks?limit=50").json()


def test_links_survive_field_selection (world: World) -> None:
	"""``?fields=`` selects fields *of an item*, and an edge is not one.

	Asking for two fields and the link graph should give both. Dropping the graph because the
	caller economised on item fields would be the two economy features cancelling each other
	out — and the roadmap query wants exactly this combination.
	"""

	_linked(world, 3)
	body = world.call("GET", "/v1/tasks?include=links&fields=ref,title&limit=50").json()

	assert len(body["links"]) == 2
	assert set(body["items"][0]) == {"ref", "title"}


def test_an_unknown_include_is_refused_and_says_what_it_accepts (world: World) -> None:
	"""``?include=backlinks`` is specified in §8.5 and built by nothing.

	Accepting it and returning nothing is precisely the failure ``api/query.py`` exists to
	prevent: the caller believes they asked for something, and reads an empty result as an
	answer. It was live for a day when the guide promised it.
	"""

	response = world.call("GET", "/v1/tasks?include=backlinks")

	assert response.status_code == 422

	body = response.json()

	assert body["errors"][0]["field"] == "include"
	assert "links" in body["errors"][0]["hint"]


def test_a_link_to_something_the_caller_cannot_see_is_not_reported (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A link is only as visible as *both* the things it joins (SPEC.md §7.3a).

	Sharper here than for the sub-resource. There, one end is the item that was asked about
	and so is known-visible, and only the far end needs checking. In a listing **neither end
	is guaranteed to be one of the items asked about** — the page may hold a task whose link
	points into a private project. Reporting that edge would disclose the existence and the
	title of something §7.3a hides.
	"""

	world = _world(session)

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(
		session, world.workspace, outsider, role_key="member"
	)
	private = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="secret",
		title="Secret",
		visibility="private",
		owner_id=world.user.id,
	)
	hidden = subroutine.domain.tasks.create(session, project=private, title="Confidential")
	session.flush()

	visible = _linked(world, 1)[0]

	world.call(
		"POST",
		f"/v1/tasks/{visible}/links",
		json={"target": hidden.ref, "target_type": "task", "link_type": "relates_to"},
	)

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
	body = nosy.call("GET", "/v1/tasks?include=links&limit=50").json()

	titles = [end["title"] for edge in body["links"] for end in (edge["source"], edge["target"])]

	assert "Confidential" not in titles
	assert body["links"] == [], "the only link on this page points into a private project"


def test_including_links_costs_the_same_number_of_queries_whatever_the_page_holds (
	world: World, session: sqlalchemy.orm.Session
) -> None:
	"""``?include=links`` must be a bounded number of queries, not one per row.

	This is the property the parameter exists for, and it is the one a reading of the code
	cannot confirm — an include that fans out per row still returns the right answer, still
	passes every other test here, and has simply moved the caller's N+1 inside the server
	where they cannot see it or page around it. The obvious implementation, ``around`` in a
	loop, is exactly that; and ``around`` itself resolved each far end separately, so it was
	N+M before this was built on top of it.

	Counted rather than asserted structurally, because the count is the promise. Two pages of
	very different sizes, same number of statements.
	"""

	counted: list[str] = []

	def record (_connection: typing.Any, _cursor: typing.Any, statement: str, *_rest: typing.Any) -> None:
		"""Note every statement the engine is asked to run."""

		counted.append(statement)

	def queries_for (count: int) -> int:
		"""Return how many statements one listing of ``count`` linked tasks takes."""

		_linked(world, count)
		counted.clear()

		sqlalchemy.event.listen(session.get_bind(), "before_cursor_execute", record)

		try:
			response = world.call("GET", "/v1/tasks?include=links&limit=50")

			assert response.status_code == 200
			assert len(response.json()["links"]) >= count - 1

			return len(counted)

		finally:
			sqlalchemy.event.remove(session.get_bind(), "before_cursor_execute", record)

	small = queries_for(2)
	large = queries_for(12)

	assert large == small, (
		f"a page of 14 tasks took {large} queries where a page of 2 took {small}: "
		f"the include is fanning out per row"
	)


def test_a_part_ranked_task_outranks_a_deliberately_trivial_one (world: World) -> None:
	"""SPEC.md §6.3's three bands: ranked, then part-ranked, then unranked.

	The defect this fixes, with the numbers that made it obvious. ``priority_score`` is null
	unless *both* axes are set and every ordering is NULLS LAST, so:

	* "critically important, urgency not yet judged" (``!5``) scored null and sorted **below**
	  "explicitly judged trivial and not urgent" (``!1/1``, score 1). The person who said the
	  most about an item was penalised for not finishing the sentence.
	* that item and one nobody had assessed at all were **indistinguishable**, although only
	  one of them carries a judgement.

	The claim now being made is that part-ranked sits *between* the other two — assessed and
	incomplete carries more than not assessed and less than a finished assessment. That is a
	judgement rather than a fact, and it is written down as one where the bands are declared.
	"""

	ranked_high = world.call("POST", "/v1/tasks", json={"text": "Rewrite auth !5/5"}).json()
	ranked_low = world.call("POST", "/v1/tasks", json={"text": "Tidy the README !1/1"}).json()
	part = world.call("POST", "/v1/tasks", json={"text": "Production is down !5"}).json()
	unranked = world.call("POST", "/v1/tasks", json={"title": "Someday: learn Rust"}).json()

	order = [
		item["ref"]
		for item in world.call("GET", "/v1/tasks?order=-priority_score&limit=50").json()["items"]
	]

	assert order == [ranked_high["ref"], ranked_low["ref"], part["ref"], unranked["ref"]]

	# And the *field* is untouched: banding is an ordering concern and must not leak into
	# what §6.3 says `priority_score` means.
	assert part["priority_score"] is None
	assert ranked_low["priority_score"] == 1


def test_part_ranked_tasks_order_among_themselves_by_the_axis_that_is_set (
	world: World,
) -> None:
	"""Within the middle band, the one thing that was said is what orders them."""

	high = world.call("POST", "/v1/tasks", json={"text": "Louder !5"}).json()["ref"]
	low = world.call("POST", "/v1/tasks", json={"text": "Quieter !2"}).json()["ref"]

	order = [
		item["ref"]
		for item in world.call("GET", "/v1/tasks?order=-priority_score&limit=50").json()["items"]
	]

	assert order.index(high) < order.index(low)


def test_tasks_that_tie_are_ordered_oldest_first (world: World) -> None:
	"""The tiebreak decides a third of a ranked backlog, so its direction is a real choice.

	52 of this project's 172 open tasks share one score, so for that third nothing but the
	tiebreak is deciding. It used to follow the last key's direction — newest first under
	``-priority_score`` — which meant the most recently captured item won for ever.

	**Simon's decision, 2026-08-13**: age is not a signal, *"we can't make a general decision
	about whether something is important because it's been in the backlog for more or less
	time"*. So it is a separator, always ascending, and it does not inherit a direction from
	a key it has nothing to do with. The primary key is a time-ordered UUID, so ascending is
	oldest first.
	"""

	first = world.call("POST", "/v1/tasks", json={"text": "Written first !3/3"}).json()["ref"]
	second = world.call("POST", "/v1/tasks", json={"text": "Written second !3/3"}).json()["ref"]
	third = world.call("POST", "/v1/tasks", json={"text": "Written third !3/3"}).json()["ref"]

	order = [
		item["ref"]
		for item in world.call("GET", "/v1/tasks?order=-priority_score&limit=50").json()["items"]
	]

	assert order == [first, second, third]

	# **The direction does not flip with the key it follows**, which is the half a tiebreak
	# that "follows the last key" gets wrong: ascending and descending by the same score must
	# put the same two rows in the same order relative to each other.
	ascending = [
		item["ref"]
		for item in world.call("GET", "/v1/tasks?order=priority_score&limit=50").json()["items"]
	]

	assert ascending == [first, second, third]


def test_the_value_a_cursor_carries_is_the_one_the_query_sorted_by (
	world: World, session: sqlalchemy.orm.Session
) -> None:
	"""What orders the query and what names a page boundary must be the same number.

	**This used to compare two implementations and now checks there is one.** Until `#569` the
	rule was written twice — a SQL expression and a Python reader — and this test existed
	because that pair had been watched to disagree, with a specific consequence: the expression
	*orders* the query and the reader names the row a cursor stopped at, so a disagreement is a
	page boundary that silently skips or repeats rows.

	An ordering that consults *other* rows cannot be written the second way at all, because a
	loaded task does not know what it blocks. So the expression is the only copy and the value
	travels to the row on a loader option. The failure mode moves rather than going away — a
	query that sorts by it and omits the option — which is what this now measures, through the
	reader a cursor actually calls.

	Checked over every combination of the two axes rather than a sample, since there are only
	thirty-six.
	"""

	for importance in (None, 1, 3, 5):
		for urgency in (None, 1, 3, 5):
			body: dict[str, typing.Any] = {"title": f"i={importance} u={urgency}"}

			if importance is not None:
				body["importance"] = importance

			if urgency is not None:
				body["urgency"] = urgency

			world.call("POST", "/v1/tasks", json=body)

	# Loaded the way a listing loads them — with the ordering's own loader option — because
	# that pairing is now the thing under test rather than an incidental detail.
	rows = list(
		session.scalars(
			sqlalchemy.select(subroutine.db.models.work.Task)
			.options(
				*subroutine.domain.ordering.options(
					"-priority_score",
					allowed=subroutine.domain.ordering.TASK_FIELDS,
					default=subroutine.domain.ordering.DEFAULT_TASK_ORDER,
				)
			)
			.where(subroutine.db.models.work.Task.deleted_at.is_(None))
		)
	)

	# `.tuples()` before `dict()`, never `dict(session.execute(...))`: a `Result` has a
	# `.keys()` method, so `dict()` treats it as a mapping and raises. Ruff's C416 suggests
	# exactly that rewrite, and taking it has broken working code here before.
	from_sql: dict[uuid.UUID, int | None] = dict(
		session.execute(
			sqlalchemy.select(
				subroutine.db.models.work.Task.id, subroutine.domain.ordering.RANKING
			).where(subroutine.db.models.work.Task.deleted_at.is_(None))
		)
		.tuples()
		.all()
	)

	assert rows, "nothing to compare"

	# The reader a cursor actually calls, rather than a second reading of the same rule.
	reader = subroutine.domain.ordering.TASK_FIELDS["priority_score"]
	assert isinstance(reader, subroutine.domain.ordering.Derived)

	for row in rows:
		assert from_sql[row.id] == reader.read(row), (
			f"the ordering and the cursor disagree for importance={row.importance} "
			f"urgency={row.urgency}: the query sorted by {from_sql[row.id]}, the cursor would "
			f"carry {reader.read(row)}"
		)


def test_a_priority_ordering_still_pages_correctly_across_the_bands (world: World) -> None:
	"""The bands must not break the cursor — this is the sort a backlog is read in.

	Two things could go wrong and neither would be visible on one page: the seek predicate
	could lose the row at a band boundary, or the cursor could carry a value the ordering no
	longer agrees with.
	"""

	made = []

	for index in range(9):
		body: dict[str, typing.Any] = {"title": f"Task {index}"}

		if index % 3 == 0:
			body["importance"], body["urgency"] = 5, (index % 5) + 1

		elif index % 3 == 1:
			body["importance"] = (index % 5) + 1

		made.append(world.call("POST", "/v1/tasks", json=body).json()["ref"])

	seen: list[int] = []
	cursor = None

	for _ in range(12):
		query = "/v1/tasks?limit=2&order=-priority_score" + (f"&cursor={cursor}" if cursor else "")
		page = world.call("GET", query).json()

		seen.extend(item["ref"] for item in page["items"])

		if not page["page"]["has_more"]:
			break

		cursor = page["page"]["next_cursor"]

	assert sorted(seen) == sorted(made)
	assert len(seen) == len(set(seen)), "a task appeared twice across a band boundary"


def test_a_task_says_who_made_it_and_who_last_changed_it (world: World) -> None:
	"""SPEC.md §6.1's attribution, on the item rather than only in its history.

	The README's claim is that every action is attributed. That was true and expensive: the
	event table recorded an actor and SR#12's histories exposed it, so learning who created
	an item meant a second request and a scan of its whole history for the `created` event.
	The row held the answer the whole time.

	Ids rather than resolved names, the same choice `assignee_id` makes: resolving every
	actor on every page is what the compact format exists to avoid.
	"""

	created = world.call("POST", "/v1/tasks", json={"title": "Attributed"}).json()

	assert created["created_by"] == str(world.user.id)
	assert created["updated_by"] is None, "nothing has changed it yet"

	changed = world.call(
		"PATCH", f"/v1/tasks/{created['ref']}", json={"title": "Renamed"}
	).json()

	assert changed["updated_by"] == str(world.user.id)


def test_attribution_cannot_be_supplied_by_the_caller (world: World) -> None:
	"""It comes from the credential, never from the body.

	A caller that could name someone else could forge attribution, which would make the
	whole record worth less than not having one — an audit trail anybody can write is a
	story rather than evidence.
	"""

	for field in ("created_by", "updated_by"):
		response = world.call(
			"POST", "/v1/tasks", json={"title": "Forged", field: str(uuid.uuid4())}
		)

		assert response.status_code == 422, f"{field} was accepted from the body"


def test_a_task_reports_when_its_meaning_last_changed (world: World) -> None:
	"""§6.1's distinction, which a document reported and a task did not.

	`updated_at` moves on any write and `content_updated_at` only when the *meaning* did —
	which is what lets a verification know whether it is stale, and stops a repositioning
	from invalidating evidence. Reporting it on one of the two entities and not the other
	was an inconsistency rather than an absence, and those are harder to notice.
	"""

	created = world.call("POST", "/v1/tasks", json={"title": "Meaningful"}).json()

	# Not asserted equal to `created_at`: the two are stamped independently and differ by
	# microseconds, which is a fact about how a row is written rather than about the rule.
	assert created["content_updated_at"] is not None

	moved = world.call(
		"PATCH", f"/v1/tasks/{created['ref']}", json={"planned_for": "tomorrow"}
	).json()

	assert moved["content_updated_at"] == created["content_updated_at"], (
		"planning a task is not a change to what it means"
	)

	edited = world.call(
		"PATCH", f"/v1/tasks/{created['ref']}", json={"title": "Reworded"}
	).json()

	assert edited["content_updated_at"] > created["content_updated_at"]


def _tree (world: World) -> tuple[int, int, int]:
	"""Build parent → child → grandchild and return their refs."""

	parent = world.call("POST", "/v1/tasks", json={"title": "Parent"}).json()
	child = world.call(
		"POST", "/v1/tasks", json={"title": "Child", "parent_task_id": parent["id"]}
	).json()
	grandchild = world.call(
		"POST", "/v1/tasks", json={"title": "Grandchild", "parent_task_id": child["id"]}
	).json()

	return parent["ref"], child["ref"], grandchild["ref"]


def test_a_listing_can_return_one_task_s_children (world: World) -> None:
	"""There was no way to ask what is under an item, which made hierarchy nearly write-only.

	The column, the materialised path, the depth ceiling and `hierarchy.subtree` all existed
	and nothing read them — a subtree was real in the database and invisible from outside,
	which is this codebase's recurring defect at the scale of a whole feature rather than a
	field. Found on 2026-07-30 by building the first real subtree in this project's own
	instance and being unable to list it.
	"""

	parent, child, _grandchild = _tree(world)
	listed = world.call("GET", f"/v1/tasks?parent={parent}").json()

	assert [item["ref"] for item in listed["items"]] == [child]


def test_a_subtree_is_deeper_than_a_child_listing (world: World) -> None:
	"""``parent=`` is one level; ``subtree=true`` is everything beneath.

	Both are wanted and they are different questions — "what did I break this into" against
	"how much work is under here", which is what SR#17's rollup will need.
	"""

	parent, child, grandchild = _tree(world)
	listed = world.call("GET", f"/v1/tasks?parent={parent}&subtree=true").json()

	assert sorted(item["ref"] for item in listed["items"]) == sorted([child, grandchild])


def test_a_subtree_excludes_the_parent_itself (world: World) -> None:
	"""``hierarchy.subtree`` matches the node *and* its descendants.

	Right for the predicate and wrong for this question: "what is under #42" does not include
	#42, and a caller totalling estimates would count the parent twice.
	"""

	parent, _child, _grandchild = _tree(world)
	listed = world.call("GET", f"/v1/tasks?parent={parent}&subtree=true").json()

	assert parent not in [item["ref"] for item in listed["items"]]


def test_subtree_without_a_parent_is_refused (world: World) -> None:
	"""It qualifies another parameter and means nothing alone.

	Ignored, it would return the whole listing to a caller who believes they asked for one
	tree — the failure `api/query.py` exists to prevent, one level in.
	"""

	response = world.call("GET", "/v1/tasks?subtree=true")

	assert response.status_code == 422
	assert response.json()["errors"][0]["field"] == "subtree"


def test_a_parent_the_caller_cannot_see_is_not_found_rather_than_empty (
	session: sqlalchemy.orm.Session,
) -> None:
	"""SPEC.md §7.3a, and the distinction matters especially here.

	An empty listing says "that tree is empty", which is a different and false claim from
	"there is no such task" — and it would confirm the item exists to somebody probing refs.
	"""

	world = _world(session)

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(
		session, world.workspace, outsider, role_key="member"
	)
	private = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="secret",
		title="Secret",
		visibility="private",
		owner_id=world.user.id,
	)
	hidden = subroutine.domain.tasks.create(session, project=private, title="Confidential")
	session.flush()

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

	assert nosy.call("GET", f"/v1/tasks?parent={hidden.ref}").status_code == 404


def _blocking (world: World, blocker: int, blocked: int) -> None:
	"""Record that one task must land before another."""

	world.call(
		"POST",
		f"/v1/tasks/{blocker}/links",
		json={"target": blocked, "target_type": "task", "link_type": "blocks"},
	)


def test_ready_excludes_a_task_something_unfinished_is_blocking (world: World) -> None:
	"""The ordering this project actually follows, made queryable.

	`priority_score` is a scalar and a dependency is a graph; folding one into the other
	would make the number mean two things and rank badly at both. So readiness is a filter
	and the ordering is unchanged — which is why a blocked item still *has* a rank, it just
	is not offered as startable.
	"""

	first = world.call("POST", "/v1/tasks", json={"title": "Groundwork"}).json()["ref"]
	second = world.call("POST", "/v1/tasks", json={"title": "Built on it"}).json()["ref"]
	_blocking(world, first, second)

	listed = [
		item["ref"] for item in world.call("GET", "/v1/tasks?ready=true&limit=50").json()["items"]
	]

	assert first in listed
	assert second not in listed


def test_finishing_the_blocker_makes_the_blocked_task_ready (world: World) -> None:
	"""The half that matters more: readiness has to *change*, not merely be computed once."""

	first = world.call("POST", "/v1/tasks", json={"title": "Groundwork"}).json()["ref"]
	second = world.call("POST", "/v1/tasks", json={"title": "Built on it"}).json()["ref"]
	_blocking(world, first, second)

	world.call("POST", f"/v1/tasks/{first}/complete", json={})

	listed = [
		item["ref"] for item in world.call("GET", "/v1/tasks?ready=true&limit=50").json()["items"]
	]

	assert second in listed, "the blocker is done and the task is still held back"


def test_ready_excludes_a_task_deferred_to_the_future (world: World) -> None:
	"""SPEC.md §6.5's third reason to skip something, which is a clock rather than a graph.

	"Don't show me the renewal form until March" and "this is blocked on the migration" are
	different facts and the same answer to "can I start it?", which is why one filter covers
	both. A caller that needs to tell them apart reads `start_at` and the blockers, which are
	on the item already.
	"""

	now = world.call("POST", "/v1/tasks", json={"title": "Startable"}).json()["ref"]
	later = world.call(
		"POST", "/v1/tasks", json={"title": "Not yet", "start": "2099-01-01"}
	).json()["ref"]

	listed = [
		item["ref"] for item in world.call("GET", "/v1/tasks?ready=true&limit=50").json()["items"]
	]

	assert now in listed
	assert later not in listed


def test_a_defer_that_has_passed_does_not_hold_a_task_back (world: World) -> None:
	"""The boundary, which is where an off-by-one in the comparison would live."""

	past = world.call(
		"POST", "/v1/tasks", json={"title": "Was deferred", "start": "2020-01-01"}
	).json()["ref"]

	listed = [
		item["ref"] for item in world.call("GET", "/v1/tasks?ready=true&limit=50").json()["items"]
	]

	assert past in listed


def test_a_document_does_not_block_a_task (world: World) -> None:
	"""Only a task can block, and this is why.

	A document has no state that could ever finish, so a `blocks` link from a specification
	would hold every task derived from it back forever — and this project's own backlog links
	its work to document #4 exactly that way. Restricted to tasks in the predicate rather
	than left to whoever creates links to be careful.
	"""

	spec = world.call("POST", "/v1/documents", json={"title": "The plan"}).json()
	task = world.call("POST", "/v1/tasks", json={"title": "Derived work"}).json()["ref"]

	world.call(
		"POST",
		f"/v1/documents/{spec['ref']}/links",
		json={"target": task, "target_type": "task", "link_type": "blocks"},
	)

	listed = [
		item["ref"] for item in world.call("GET", "/v1/tasks?ready=true&limit=50").json()["items"]
	]

	assert task in listed, "a document with no state held a task back"


def test_a_withdrawn_block_stops_blocking (world: World) -> None:
	"""Links are soft-deleted, so the predicate has to exclude the withdrawn ones itself."""

	first = world.call("POST", "/v1/tasks", json={"title": "Groundwork"}).json()["ref"]
	second = world.call("POST", "/v1/tasks", json={"title": "Built on it"}).json()["ref"]
	_blocking(world, first, second)

	link = world.call("GET", f"/v1/tasks/{second}/links").json()["items"][0]
	world.call("DELETE", f"/v1/tasks/{second}/links/{link['id']}")

	listed = [
		item["ref"] for item in world.call("GET", "/v1/tasks?ready=true&limit=50").json()["items"]
	]

	assert second in listed


def test_a_blocker_the_caller_cannot_see_still_blocks (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**Readiness is a fact about the work, not about the viewer**, and this is the trade.

	A task in a private project can block one the caller can see. Counting only the blockers
	they can see would report the item as startable when it is not — the caller picks it up,
	and finds out the hard way.

	What this discloses is bounded and deliberate: the item is absent from `ready=true` while
	present in the ordinary listing, so a determined reader learns that *something* unseen
	holds it back, and never what. §7.3a protects the existence of the private item, which
	this does not reveal. Recorded in `tests/test_scoping.py` as the reason
	`domain/readiness.py` reaches tasks without narrowing.
	"""

	world = _world(session)

	private = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="secret",
		title="Secret",
		visibility="private",
		owner_id=world.user.id,
	)
	hidden = subroutine.domain.tasks.create(session, project=private, title="Confidential")
	session.flush()

	visible = world.call("POST", "/v1/tasks", json={"title": "Waiting on something"}).json()

	world.call(
		"POST",
		f"/v1/tasks/{hidden.ref}/links",
		json={"target": visible["ref"], "target_type": "task", "link_type": "blocks"},
	)

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(
		session, world.workspace, outsider, role_key="member"
	)
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

	assert visible["ref"] in [
		item["ref"] for item in nosy.call("GET", "/v1/tasks?limit=50").json()["items"]
	], "the task itself is visible"

	assert visible["ref"] not in [
		item["ref"] for item in nosy.call("GET", "/v1/tasks?ready=true&limit=50").json()["items"]
	], "an unseen blocker did not hold it back"

	# And nothing about the blocker leaked on the way.
	assert nosy.call("GET", f"/v1/tasks/{hidden.ref}").status_code == 404


def test_the_listing_default_still_shows_deferred_work (world: World) -> None:
	"""The compatibility guarantee, pinned so that changing it has to be deliberate.

	`#73` made `subroutine list` hide deferred work, because §6.5 says a default *view* hides
	it and a person reading a list cannot act on something that starts in March. Simon's
	decision of 2026-07-31 was that an API listing is not one of those views: `?ready=true`
	already answers the question explicitly and opt-in, and changing a published default would
	break every existing client in order to say something they can already ask for.

	So this asserts an absence of change, which is the kind of test that only earns its place
	when somebody has been tempted — and the temptation here was to make one rule apply
	everywhere for tidiness.
	"""

	later = world.call(
		"POST", "/v1/tasks", json={"title": "Not yet", "start": "2099-01-01"}
	).json()["ref"]

	listed = [item["ref"] for item in world.call("GET", "/v1/tasks?limit=50").json()["items"]]

	assert later in listed


def test_deferred_exclude_and_only_partition_the_listing (world: World) -> None:
	"""The two narrowings are complements, which is what makes the count reportable.

	`subroutine list` shows the `exclude` set and reports the size of the `only` set. If they
	overlapped or left a gap, the number beside the list would be about a different set of
	rows than the list — and nothing in the output would say so.
	"""

	startable = world.call("POST", "/v1/tasks", json={"title": "Startable"}).json()["ref"]
	parked = world.call(
		"POST", "/v1/tasks", json={"title": "Parked", "start": "2099-01-01"}
	).json()["ref"]

	def refs (query: str) -> set[int]:
		"""Return the refs one listing reports."""

		return {
			item["ref"] for item in world.call("GET", f"/v1/tasks?limit=50&{query}").json()["items"]
		}

	everything = refs("")
	shown = refs("deferred=exclude")
	held = refs("deferred=only")

	assert startable in shown and parked not in shown
	assert parked in held and startable not in held

	# Complements: no overlap, and nothing falls between them.
	assert not (shown & held)
	assert shown | held == everything


def test_an_unknown_deferred_value_is_refused_by_name (world: World) -> None:
	"""Named as the field the caller sent, with the values that would have worked."""

	response = world.call("GET", "/v1/tasks?deferred=banana")

	assert response.status_code == 422

	body = response.json()

	assert body["errors"][0]["field"] == "deferred"
	assert "include" in body["errors"][0]["message"]


def test_search_reads_the_description_as_well_as_the_title (world: World) -> None:
	"""§9.4 says ``q`` searches title *and* description. It searched the title alone.

	Not a widening: the specification said this from the start, the endpoint's own OpenAPI
	description said "Match this text in the title", and so the published contract documented
	the defect rather than the intent. Invisible because a search that drops rows returns
	*plausible* rows — the ones it loses are the ones nobody knew to look for.
	"""

	titled = world.call("POST", "/v1/tasks", json={"title": "Fix the pagination cursor"}).json()
	described = world.call(
		"POST",
		"/v1/tasks",
		json={"title": "Unrelated heading", "description": "The pagination is wrong here."},
	).json()
	neither = world.call("POST", "/v1/tasks", json={"title": "Something else"}).json()

	found = {
		item["ref"] for item in world.call("GET", "/v1/tasks?q=pagination&limit=50").json()["items"]
	}

	assert titled["ref"] in found
	assert described["ref"] in found
	assert neither["ref"] not in found


def test_a_search_term_cannot_smuggle_in_a_like_wildcard (world: World) -> None:
	"""``%`` and ``_`` are escaped, on every column rather than only on the first.

	The escaping moved to ``domain.search`` when the description was added, and a helper
	applied to one of two columns is the shape this project keeps finding. Without it a search
	for ``50%`` matches everything — and on a large table it is an accidental full scan.
	"""

	literal = world.call("POST", "/v1/tasks", json={"title": "Cut it by 50% this year"}).json()
	other = world.call("POST", "/v1/tasks", json={"title": "No numbers at all"}).json()

	found = {
		item["ref"] for item in world.call("GET", "/v1/tasks?q=50%25&limit=50").json()["items"]
	}

	assert literal["ref"] in found
	assert other["ref"] not in found, "'%' was treated as a wildcard"

	# The same, in the column that was added rather than the one that always worked.
	described = world.call(
		"POST", "/v1/tasks", json={"title": "Plain", "description": "Down 50% on last year"}
	).json()
	again = {
		item["ref"] for item in world.call("GET", "/v1/tasks?q=50%25&limit=50").json()["items"]
	}

	assert described["ref"] in again
	assert other["ref"] not in again


def test_a_task_reports_its_parent_by_ref_and_title (world: World) -> None:
	"""A ref is how an item is addressed (§6.2), so reporting only a UUID forces a second call.

	That is the failure review dimension 4 names — "responses embed enough context to avoid a
	second call" — multiplied by the page, since a listing would need one lookup per row.
	Both fields are batch-loaded with the status and project names.
	"""

	parent = world.call("POST", "/v1/tasks", json={"title": "The whole feature"}).json()
	child = world.call(
		"POST",
		"/v1/tasks",
		json={"title": "One part", "parent_task_id": str(parent["id"])},
	).json()

	assert child["parent_ref"] == parent["ref"]
	assert child["parent_title"] == "The whole feature"

	# And a top-level task reports neither, rather than reporting something empty.
	assert parent["parent_ref"] is None
	assert parent["parent_title"] is None


def test_reporting_a_parent_costs_no_query_per_row (
	world: World, session: sqlalchemy.orm.Session
) -> None:
	"""The guard that matters: a page of children must not fan out into a lookup each.

	`#39` was spent removing exactly this shape from the link listing, and the obvious
	implementation of `parent_title` reintroduces it — correctly, invisibly, and only under
	load. Counted rather than asserted structurally, because the count is the promise.
	"""

	parent = world.call("POST", "/v1/tasks", json={"title": "Parent"}).json()
	counted: list[str] = []

	def record (
		_connection: typing.Any, _cursor: typing.Any, statement: str, *_rest: typing.Any
	) -> None:
		"""Note every statement the engine is asked to run."""

		counted.append(statement)

	def queries_for (children: int) -> int:
		"""Return how many statements one page of ``children`` children takes."""

		while (
			len(world.call("GET", f"/v1/tasks?parent={parent['ref']}&limit=50").json()["items"])
			< children
		):
			world.call(
				"POST",
				"/v1/tasks",
				json={"title": "A part", "parent_task_id": str(parent["id"])},
			)

		counted.clear()
		sqlalchemy.event.listen(session.get_bind(), "before_cursor_execute", record)

		try:
			body = world.call("GET", f"/v1/tasks?parent={parent['ref']}&limit=50").json()

			assert len(body["items"]) == children
			assert all(item["parent_title"] == "Parent" for item in body["items"])

			return len(counted)

		finally:
			sqlalchemy.event.remove(session.get_bind(), "before_cursor_execute", record)

	small = queries_for(2)
	large = queries_for(12)

	assert large == small, (
		f"a page of 12 children took {large} queries where a page of 2 took {small}: "
		f"reporting the parent is fanning out per row"
	)


def test_a_task_can_be_moved_between_projects (world: World) -> None:
	"""`#43`: a task's project was fixed at creation forever.

	Felt directly rather than reasoned about — `#23` filed seven tasks into the Inbox behind
	seven 201s, and had they stayed misfiled there would have been no way to move them. The
	capture path was fixed; this is the other half.
	"""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Web"})
	made = world.call("POST", "/v1/tasks", json={"title": "Filed nowhere"}).json()

	assert made["project_key"] == "inbox"

	moved = world.call("PATCH", f"/v1/tasks/{made['ref']}", json={"project": "web"}).json()

	assert moved["project_key"] == "web"
	assert moved["version"] > made["version"], "a move is a change and moves the version"


def test_moving_a_task_takes_its_parts_with_it (world: World) -> None:
	"""The invariant runs in both directions, so the subtree is not optional.

	`create` refuses a subtask in a different project from its parent. Moving a parent and
	leaving its children behind would break that from the other side — and silently, since
	nothing re-checks it afterwards.
	"""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Web"})
	parent = world.call("POST", "/v1/tasks", json={"title": "The feature"}).json()
	child = world.call(
		"POST", "/v1/tasks", json={"title": "A part", "parent_task_id": str(parent["id"])}
	).json()
	grandchild = world.call(
		"POST", "/v1/tasks", json={"title": "A smaller part", "parent_task_id": str(child["id"])}
	).json()

	world.call("PATCH", f"/v1/tasks/{parent['ref']}", json={"project": "web"})

	for ref in (parent["ref"], child["ref"], grandchild["ref"]):
		found = world.call("GET", f"/v1/tasks/{ref}").json()

		assert found["project_key"] == "web", f"#{ref} was left behind"

	# **The parts' versions move too.** A client holding one and sending it back under §8.9
	# has a stale view of where that task lives, which is what the check exists to catch.
	assert world.call("GET", f"/v1/tasks/{child['ref']}").json()["version"] > child["version"]


def test_a_part_cannot_be_moved_out_of_its_parent (world: World) -> None:
	"""Refused, and the refusal names what to do instead rather than just saying no."""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Web"})
	parent = world.call("POST", "/v1/tasks", json={"title": "The feature"}).json()
	child = world.call(
		"POST", "/v1/tasks", json={"title": "A part", "parent_task_id": str(parent["id"])}
	).json()

	response = world.call("PATCH", f"/v1/tasks/{child['ref']}", json={"project": "web"})

	assert response.status_code == 422

	body = response.json()

	assert body["errors"][0]["field"] == "project"
	assert "parent" in body["errors"][0]["hint"].lower()

	# And nothing moved.
	assert world.call("GET", f"/v1/tasks/{child['ref']}").json()["project_key"] == "inbox"


def test_an_ordinary_edit_does_not_refile_the_task (world: World) -> None:
	"""The trap this shape invites, and the one `#23` already sprang once.

	`selection.project` answers `None` with the workspace's Inbox, so passing the body's
	project through unconditionally would file every ordinary edit into the Inbox — behind a
	200 this time rather than a 201.
	"""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Web"})
	made = world.call(
		"POST", "/v1/tasks", json={"title": "Filed on purpose", "project": "web"}
	).json()

	edited = world.call(
		"PATCH", f"/v1/tasks/{made['ref']}", json={"title": "Renamed, not refiled"}
	).json()

	assert edited["project_key"] == "web"


def test_a_move_is_recorded_as_a_change (world: World) -> None:
	"""§10.7 invariant 9: a change that writes no event is a change nobody can audit.

	`_snapshot` decides both what an event says *and whether one is written at all*, so a
	field missing from it is a silent hole — which is exactly how `urgency` went untracked
	for a day.
	"""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Web"})
	made = world.call("POST", "/v1/tasks", json={"title": "Moves house"}).json()

	world.call("PATCH", f"/v1/tasks/{made['ref']}", json={"project": "web"})

	events = world.call("GET", f"/v1/tasks/{made['ref']}/events").json()["items"]

	assert any("project_id" in (event.get("changes") or {}) for event in events)


def test_a_move_is_refused_when_the_destination_is_out_of_reach (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**Both ends are checked, and the destination is the one that could leak.**

	A caller who may write where the task is but not where it is going must not be able to
	move work out of their own reach — nor learn from a half-applied change that the target
	exists at all. §7.3a hides a private project's contents, so this comes back as "no such
	project" rather than as a refusal that confirms one.
	"""

	world = _world(session)

	outsider = subroutine.domain.users.create(session, username=f"other-{uuid.uuid4().hex[:8]}")
	subroutine.domain.workspaces.add_member(
		session, world.workspace, outsider, role_key="member"
	)
	private = subroutine.domain.projects.create(
		session,
		workspace_id=world.workspace.id,
		key="secret",
		title="Secret",
		visibility="private",
		owner_id=world.user.id,
	)
	session.flush()

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

	mine = nosy.call("POST", "/v1/tasks", json={"title": "Mine to edit"}).json()
	response = nosy.call("PATCH", f"/v1/tasks/{mine['ref']}", json={"project": private.key})

	assert response.status_code == 404, response.text

	# And it stayed where it was.
	assert nosy.call("GET", f"/v1/tasks/{mine['ref']}").json()["project_key"] == "inbox"


def test_tags_can_be_set_on_a_task_built_from_fields (world: World) -> None:
	"""`#41`: a tag could be applied only by writing `#health` in a captured line.

	So a task created from structured fields could not be tagged at all — the view reported
	`tags` and no endpoint accepted them, which is the "readable and unsettable" shape the
	writability guard was built to find.
	"""

	made = world.call(
		"POST", "/v1/tasks", json={"title": "Structured", "tags": ["health", "admin"]}
	).json()

	assert made["tags"] == ["admin", "health"], "reported alphabetically, as the view promises"


def test_a_mistyped_tag_can_be_removed (world: World) -> None:
	"""The half that was permanent: no route removed a tag, on any transport.

	`tags` **replaces** rather than merges, which is what §8.3 means by a field on a PATCH —
	every other field there is assigned. A `tags` that merged would be the only one a caller
	could not use to remove anything.
	"""

	made = world.call(
		"POST", "/v1/tasks", json={"title": "Typo", "tags": ["helth"]}
	).json()

	fixed = world.call(
		"PATCH", f"/v1/tasks/{made['ref']}", json={"tags": ["health"]}
	).json()

	assert fixed["tags"] == ["health"]

	# And an empty list clears them, which is the same statement as null for a scalar.
	assert world.call("PATCH", f"/v1/tasks/{made['ref']}", json={"tags": []}).json()["tags"] == []


def test_omitting_tags_leaves_them_alone (world: World) -> None:
	"""§8.3's other half, and the one a replace-semantics field makes easy to break."""

	made = world.call(
		"POST", "/v1/tasks", json={"title": "Tagged", "tags": ["keep"]}
	).json()
	edited = world.call(
		"PATCH", f"/v1/tasks/{made['ref']}", json={"title": "Renamed"}
	).json()

	assert edited["tags"] == ["keep"]


def test_a_structured_tag_beats_one_in_the_captured_line (world: World) -> None:
	"""§6.13: anything given explicitly wins over what the text said.

	It holds through `fields.update(overrides)` rather than through a rule of its own. The
	captured tags used to be applied *after* `create` returned, which put them outside that
	mechanism — the same shape as `estimate`, whose override was guarded by a condition
	nothing could satisfy.
	"""

	made = world.call(
		"POST",
		"/v1/tasks",
		json={"text": "Water the plants #garden", "tags": ["explicit"]},
	).json()

	assert made["tags"] == ["explicit"]


def test_a_tag_of_only_digits_is_still_refused (world: World) -> None:
	"""§6.2's rule has to hold however a tag arrives, which is why `ensure` is the one door.

	`#3d-printing` is a tag and `#12` is a reference — the test is whether the name is
	*entirely* digits, not whether it starts with a letter. A structured field is a new way
	in, and a rule enforced only by the capture parser would not have covered it.
	"""

	response = world.call("POST", "/v1/tasks", json={"title": "No", "tags": ["404"]})

	assert response.status_code == 422

	# And the neighbouring case that the earlier, wrong wording refused.
	fine = world.call("POST", "/v1/tasks", json={"title": "Yes", "tags": ["3d-printing"]})

	assert fine.status_code == 201


def test_changing_tags_is_recorded_as_a_change (world: World) -> None:
	"""Tags live in a join table, so `_snapshot` has to *read* them rather than take a column.

	That is why it now takes a session. A field missing from that comparison writes no event
	at all — §10.7 invariant 9 failing with nothing failing.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Tag me"}).json()

	world.call("PATCH", f"/v1/tasks/{made['ref']}", json={"tags": ["added"]})

	events = world.call("GET", f"/v1/tasks/{made['ref']}/events").json()["items"]

	assert any("tags" in (event.get("changes") or {}) for event in events)


def test_clearing_a_title_is_refused_rather_than_crashing (world: World) -> None:
	"""§8.3 says null clears — and a title is the field that cannot be cleared.

	It was a **500 on tasks, documents and projects alike**, from `_clean_title(None)` calling
	`.strip()` on it, and it survived two reviews. The fix is at `domain.text.require`, the one
	choke point every required string in the system passes through, so the CLI and the MCP
	adapter are covered by the same change.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Keep me"}).json()
	response = world.call("PATCH", f"/v1/tasks/{made['ref']}", json={"title": None})

	assert response.status_code == 422

	body = response.json()

	assert body["errors"][0]["field"] == "title"

	# And the task is untouched — a refused update must leave the row exactly as it was.
	assert world.call("GET", f"/v1/tasks/{made['ref']}").json()["title"] == "Keep me"


def test_a_null_clears_the_tags (world: World) -> None:
	"""The other answer to the same question, because tags *can* be cleared.

	`{"tags": null}` reached `list(None)` and 500ed — shipped hours before the review that
	found it. Null and `[]` now mean the same thing here, which is what §8.3's null means for
	every other clearable field.
	"""

	made = world.call(
		"POST", "/v1/tasks", json={"title": "Tagged", "tags": ["one", "two"]}
	).json()

	assert made["tags"] == ["one", "two"]
	assert world.call("PATCH", f"/v1/tasks/{made['ref']}", json={"tags": None}).json()["tags"] == []


def test_a_search_finds_words_that_are_not_adjacent_or_in_order (world: World) -> None:
	"""**`#620`, reproduced from the report's own probes.**

	`q` was one contiguous ordered substring, so a multi-word search succeeded only where the
	words happened to sit next to each other in that order. Both of the failing probes below
	returned nothing against an item plainly containing both words.

	**The direction of the failure is why this was urgent.** "Nothing open." reads as "this
	does not exist", so the caller does what an empty result implies and files a duplicate —
	on the one path that exists to prevent duplicates. Two were nearly filed against this
	backlog by an agent doing exactly what the skill tells it to.
	"""

	item = world.call(
		"POST",
		"/v1/tasks",
		json={
			"title": "Nothing can ask which vocabulary entries the installation seeded",
			"description": "`is_system` is written by seed.py and read by nothing.",
		},
	).json()

	def finds (query: str) -> bool:
		"""Report whether this query returns that item."""

		answer = world.call("GET", f"/v1/tasks?q={query}&limit=50").json()

		return item["ref"] in {row["ref"] for row in answer["items"]}

	# Worked before, and must go on working.
	assert finds("vocabulary"), "a single term"
	assert finds("is_system"), "a term that appears only in the description"
	assert finds("vocabulary+entries"), "adjacent, in order"

	# The two the report found returning nothing.
	assert finds("vocabulary+seeded"), "both present, four words apart"
	assert finds("entries+vocabulary"), "adjacent but reversed"

	# And across the two fields at once, which is the ordinary shape of half-remembering.
	assert finds("vocabulary+is_system"), "one word from the title, one from the description"


def test_a_search_still_requires_every_word (world: World) -> None:
	"""The other direction, and the reason this is an AND rather than an OR.

	Widening to "any word matches" would turn every multi-word search into most of the
	backlog, which fails the same task — a caller cannot look before filing if looking always
	answers "here are forty things".
	"""

	world.call("POST", "/v1/tasks", json={"title": "The vocabulary is seeded"}).json()
	missing = world.call("POST", "/v1/tasks", json={"title": "The vocabulary"}).json()

	found = {
		row["ref"]
		for row in world.call("GET", "/v1/tasks?q=vocabulary+seeded&limit=50").json()["items"]
	}

	assert missing["ref"] not in found


def test_a_search_of_nothing_but_spaces_narrows_nothing (world: World) -> None:
	"""It used to search for whatever was typed, so `q=" "` matched every row with a space.

	A filter nobody asked for, answering a question nobody put — and one that looks like a
	working search because it returns plausible rows.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Solitary"}).json()

	found = {
		row["ref"] for row in world.call("GET", "/v1/tasks?q=%20%20&limit=50").json()["items"]
	}

	assert made["ref"] in found


def test_a_search_asking_for_too_many_words_is_refused_by_name (world: World) -> None:
	"""Each term is its own unindexable scan, so a pasted paragraph is real work per row.

	Refused rather than quietly truncated: a search that silently narrows differently from
	what was asked is `#620` in the other direction, and this project's rule is that an
	argument it cannot honour is reported rather than swallowed (`#379`).
	"""

	asked = "+".join(f"word{n}" for n in range(subroutine.domain.search.MAX_TERMS + 1))
	answer = world.call("GET", f"/v1/tasks?q={asked}&limit=50")

	assert answer.status_code == 422
	assert answer.json()["errors"][0]["field"] == "q"
	assert "distinctive" in answer.json()["hint"]


def test_a_search_for_a_number_finds_the_item_with_that_ref (world: World) -> None:
	"""**`#867`.** A ref is this product's primary address and search could not resolve one.

	Measured across ten refs on the live instance before this was built: the item itself was
	**absent in ten of ten**, while four to sixty unrelated rows matched the digits as text.
	The number is in every commit message and every sentence anybody writes about an item, so
	the search box was the one place it did not work.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Entirely unrelated wording"}).json()

	found = world.call("GET", f"/v1/tasks?q={made['ref']}&limit=50").json()

	assert made["ref"] in {row["ref"] for row in found["items"]}


def test_a_search_for_a_ref_still_finds_what_mentions_it (world: World) -> None:
	"""**Both readings are kept, and that is the decision rather than an accident.**

	``862`` may be the item and may equally be a number somebody wrote in a description.
	Neither is obviously the intended one, so the ref match is OR-ed *beside* the text match
	rather than replacing it — a lookup that discarded the text hits would answer a narrower
	question than the one asked.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "The subject itself"}).json()
	citing = world.call(
		"POST",
		"/v1/tasks",
		json={"title": "Something else", "description": f"Follows on from #{made['ref']}."},
	).json()

	found = {
		row["ref"]
		for row in world.call("GET", f"/v1/tasks?q={made['ref']}&limit=50").json()["items"]
	}

	assert made["ref"] in found, "the item with that number"
	assert citing["ref"] in found, "and the one that merely mentions it"


def test_a_ref_search_accepts_the_sigil_it_is_written_with (world: World) -> None:
	"""``#42`` and ``42`` are one request, decided in ``refs.parse_ref`` and nowhere else.

	The sigil is how a ref is written everywhere a person reads one, so a search box that took
	only the bare form would refuse the spelling somebody copied out of a comment.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Addressed two ways"}).json()

	# %23 is `#`, which would otherwise open a URL fragment and never reach the server.
	found = world.call("GET", f"/v1/tasks?q=%23{made['ref']}&limit=50").json()

	assert made["ref"] in {row["ref"] for row in found["items"]}


def test_a_number_among_other_words_is_not_a_ref_lookup (world: World) -> None:
	"""``parse_ref`` is anchored at both ends, and this is what that buys.

	A query is read as a ref only when the *whole* of it is one. Otherwise any search
	containing a number would quietly become a lookup, and ``pagination 42`` would return an
	item having nothing to do with either word — the swallow `#379` exists to refuse.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Nothing to do with it"}).json()

	found = {
		row["ref"]
		for row in world.call(
			"GET", f"/v1/tasks?q={made['ref']}+pagination&limit=50"
		).json()["items"]
	}

	assert made["ref"] not in found


def test_a_ref_search_cannot_reach_past_a_narrowed_credential (
	session: sqlalchemy.orm.Session,
) -> None:
	"""**The clause narrows; it may never widen.**

	It is one ``where`` among many on a statement ``scoping.readable_tasks`` has already
	bounded, so it is AND-ed inside the caller's reach rather than OR-ed around it. That is
	structural — but a ref is a **guessable** address, unlike a word somebody has to know, so
	the one thing this must never become is a way to confirm an item exists by numbering at
	it. Worth a test even where the shape says it cannot.
	"""

	world = _world(session)

	elsewhere = world.call(
		"POST", "/v1/projects", json={"key": "elsewhere", "title": "Elsewhere"}
	).json()
	beyond = world.call(
		"POST", "/v1/tasks", json={"title": "Out of reach", "project": elsewhere["key"]}
	).json()

	inbox = world.call("GET", "/v1/projects").json()["items"]
	reachable = next(
		row for row in inbox if row["key"] == subroutine.domain.bootstrap.INBOX_KEY
	)

	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=world.user, title="Inbox only", project_scope=[reachable["id"]]
	)
	session.flush()

	narrowed = world._replace(secret=issued.value.get_secret_value())
	found = narrowed.call("GET", f"/v1/tasks?q={beyond['ref']}&limit=50").json()

	assert beyond["ref"] not in {row["ref"] for row in found["items"]}


def test_a_search_finds_an_item_by_the_words_in_a_comment_on_it (world: World) -> None:
	"""**`#83`, and it reaches the majority of the prose on a working instance.**

	A comment is where the running record lives (§5.10), and `#825` measured 780 of them
	against 695 tasks here — so a search that skipped them was answering "nothing matches"
	about the largest thing it could have looked in, on the one path built to stop a duplicate
	being filed.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "An ordinary title"}).json()
	world.call(
		"POST",
		f"/v1/tasks/{made['ref']}/comments",
		json={"body": "The planner turns this into a semi-join."},
	)

	found = world.call("GET", "/v1/tasks?q=semi-join&limit=50").json()

	assert made["ref"] in {row["ref"] for row in found["items"]}


def test_a_deleted_comment_does_not_surface_the_item_it_was_on (world: World) -> None:
	"""**The one genuine visibility rule search inherits here** (`#825`).

	A hit whose only reason is a comment nobody can open is worse than no hit: the reader opens
	the item and hunts for words that are not there. The mention wiring settled the same
	question the same way — a backlink pointing at a sentence nobody can read is worse than
	none — and this is that rule, not a new one.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Still ordinary"}).json()
	comment = world.call(
		"POST",
		f"/v1/tasks/{made['ref']}/comments",
		json={"body": "Mentioning quinsy before it was withdrawn."},
	).json()

	before = world.call("GET", "/v1/tasks?q=quinsy&limit=50").json()

	assert made["ref"] in {row["ref"] for row in before["items"]}, "the probe proves nothing"

	world.call("DELETE", f"/v1/comments/{comment['id']}")

	after = world.call("GET", "/v1/tasks?q=quinsy&limit=50").json()

	assert made["ref"] not in {row["ref"] for row in after["items"]}


def test_a_comment_search_cannot_reach_past_a_narrowed_credential (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A comment has no visibility of its own, so it inherits its subject's — including this.

	`#825` refuted the objection that kept comments unsearched, on the grounds that a comment
	is readable exactly when its item is. That cuts both ways and the second direction is the
	one worth a test: an item out of reach must not become findable through the prose written
	on it.
	"""

	world = _world(session)

	elsewhere = world.call(
		"POST", "/v1/projects", json={"key": "elsewhere", "title": "Elsewhere"}
	).json()
	beyond = world.call(
		"POST", "/v1/tasks", json={"title": "Out of reach", "project": elsewhere["key"]}
	).json()
	world.call(
		"POST",
		f"/v1/tasks/{beyond['ref']}/comments",
		json={"body": "Written where marmoreal cannot be read."},
	)

	inbox = world.call("GET", "/v1/projects").json()["items"]
	reachable = next(
		row for row in inbox if row["key"] == subroutine.domain.bootstrap.INBOX_KEY
	)

	_row, issued = subroutine.domain.authentication.issue_token(
		session, user=world.user, title="Inbox only", project_scope=[reachable["id"]]
	)
	session.flush()

	narrowed = world._replace(secret=issued.value.get_secret_value())
	found = narrowed.call("GET", "/v1/tasks?q=marmoreal&limit=50").json()

	assert beyond["ref"] not in {row["ref"] for row in found["items"]}


def test_a_search_for_a_number_finds_a_finished_item (world: World) -> None:
	"""**`#873`, and it was the majority case rather than an edge.**

	`#867` made an exact ref match find the item; a listing hides finished work unless asked;
	so the row was found and then filtered away. Measured on the served instance: **548 of 721
	tasks are finished**, so for three items in four typing the number answered nothing about
	something ``subroutine show`` reads happily — `#700`'s divergence between a lookup and a
	listing, from a different direction.

	`#818`'s sentence, third instance: *a rule written down in one vocabulary does not reach
	the next one.* Categories, then filters, and not a lookup.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Long since dealt with"}).json()

	world.call("POST", f"/v1/tasks/{made['ref']}/complete")

	found = world.call("GET", f"/v1/tasks?q={made['ref']}&limit=50").json()

	assert made["ref"] in {row["ref"] for row in found["items"]}


def test_a_number_search_still_honours_being_told_to_exclude_finished_work (
	world: World,
) -> None:
	"""Where this parts company with `#818`'s `completed_at`, and the difference is real.

	*Finished work that is not finished* means nothing, so `#818` refuses the combination. **The
	open item numbered 42 is a coherent question**, so an explicit ``include_completed=false``
	is honoured here rather than refused — the same ending ``about_activity`` has.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Dealt with as well"}).json()

	world.call("POST", f"/v1/tasks/{made['ref']}/complete")

	found = world.call(
		"GET", f"/v1/tasks?q={made['ref']}&include_completed=false&limit=50"
	).json()

	assert made["ref"] not in {row["ref"] for row in found["items"]}


def test_a_word_search_goes_on_hiding_finished_work (world: World) -> None:
	"""The other half, and what stops `#873`'s fix being a widening of every search.

	Only a query that is **entirely** a ref reaches finished work. A search for words is an
	ordinary listing and keeps the ordinary rule, or `#873` would have quietly turned every
	search into one that answers with everything ever completed.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Vanishingly rare wording"}).json()

	world.call("POST", f"/v1/tasks/{made['ref']}/complete")

	found = world.call("GET", "/v1/tasks?q=vanishingly&limit=50").json()

	assert made["ref"] not in {row["ref"] for row in found["items"]}


def test_a_status_category_gathers_every_status_in_it (world: World) -> None:
	"""`#710`. The point of the filter: three seeded keys share the ``todo`` category.

	``open``, ``blocked`` and ``needs_input`` are all ``todo``, so a caller asking "what is not
	started" by *key* has to know all three and re-learn them on any installation that adds a
	fourth. The category is the fixed field beside the renameable key, published for this.
	"""

	for key in ("open", "blocked", "needs_input"):
		world.call("POST", "/v1/tasks", json={"title": f"A {key} task", "status": key})

	world.call("POST", "/v1/tasks", json={"title": "Underway", "status": "in_progress"})

	by_category = world.call("GET", "/v1/tasks?status_category=todo").json()["items"]
	by_key = world.call("GET", "/v1/tasks?status=open").json()["items"]

	assert len(by_category) == 3, "the category is all three, not the one sharing its name"
	assert len(by_key) == 1

	assert {item["status_category"] for item in by_category} == {"todo"}


def test_asking_for_a_finished_category_reaches_finished_work (world: World) -> None:
	"""`#710`. ``?status_category=done`` alone must not answer ``[]`` on an instance full of it.

	The trap this is written for is a plausible, complete, wrong answer: ``include_completed``
	defaults to off, so without the implication the one query a completed-work view makes
	returns an empty page and says nothing about why.
	"""

	ref = world.call("POST", "/v1/tasks", json={"title": "Finished"}).json()["ref"]
	world.call("POST", "/v1/tasks", json={"title": "Still going"})
	world.call("POST", f"/v1/tasks/{ref}/complete")

	items = world.call("GET", "/v1/tasks?status_category=done").json()["items"]

	assert [item["title"] for item in items] == ["Finished"]


def test_asking_for_finished_work_and_excluding_it_is_refused (world: World) -> None:
	"""`#710`. A contradiction is named rather than settled in one parameter's favour.

	Both readings are defensible and both are silent, which is what makes refusing right: the
	narrowing could win and return finished work the caller said to exclude, or the default
	could win and return nothing at all.
	"""

	response = world.call("GET", "/v1/tasks?status_category=done&include_completed=false")

	assert response.status_code == 422

	body = response.json()

	assert body["errors"][0]["field"] == "include_completed"
	assert "status_category" in body["errors"][0]["message"]


def test_excluding_finished_work_is_fine_beside_an_unfinished_category (world: World) -> None:
	"""The refusal is about the contradiction, not about naming both parameters."""

	response = world.call("GET", "/v1/tasks?status_category=todo&include_completed=false")

	assert response.status_code == 200


def test_a_document_status_category_is_refused_on_a_task_listing (world: World) -> None:
	"""``superseded`` is a real category and not one a task can be in.

	Two vocabularies for a reason — a superseded specification is not "done" — so passing one
	to the other's listing is a mistake worth being told about rather than an empty page.
	"""

	response = world.call("GET", "/v1/tasks?status_category=superseded")

	assert response.status_code == 422

	hint = response.json()["errors"][0]["hint"]

	assert "cancelled" in hint and "in_progress" in hint
	assert "superseded" not in hint


def test_finished_work_can_be_ordered_by_when_it_finished (world: World) -> None:
	"""`#710`, and the half `#706` needs: *most recently finished first*.

	``updated_at`` is the tempting proxy and is wrong — this asserts the difference by editing
	the *older* completion afterwards, which would reorder the page under that proxy and must
	not under this one.
	"""

	refs = [
		world.call("POST", "/v1/tasks", json={"title": title}).json()["ref"]
		for title in ("First finished", "Second finished")
	]

	for ref in refs:
		world.call("POST", f"/v1/tasks/{ref}/complete")

	world.call("PATCH", f"/v1/tasks/{refs[0]}", json={"description": "touched afterwards"})

	titles = [
		item["title"]
		for item in world.call(
			"GET", "/v1/tasks?status_category=done&order=-completed_at"
		).json()["items"]
	]

	assert titles == ["Second finished", "First finished"]


def test_ordering_by_completion_leaves_unfinished_work_at_the_end (world: World) -> None:
	"""NULLS LAST in both directions (§10.3, `#457`), which is what makes the order usable.

	Descending, the newest finish is first and everything open is at the end — so a single
	query serves "what has been done lately" without a second filter.
	"""

	ref = world.call("POST", "/v1/tasks", json={"title": "Finished"}).json()["ref"]
	world.call("POST", "/v1/tasks", json={"title": "Open"})
	world.call("POST", f"/v1/tasks/{ref}/complete")

	for direction in ("completed_at", "-completed_at"):
		titles = [
			item["title"]
			for item in world.call(
				"GET", f"/v1/tasks?include_completed=true&order={direction}"
			).json()["items"]
		]

		assert titles[0] == "Finished", f"NULLS LAST is not holding for {direction}"


def test_a_completion_ordering_pages_without_repeating_a_row (world: World) -> None:
	"""`#46`'s trap, checked on the new field: a cursor must read the sort value back.

	``priority_score`` ordered perfectly and returned 500 for every result set larger than one
	page, because encoding a cursor reads each sort value off a loaded row. ``completed_at`` is
	a plain column and so needs no ``Derived`` — this is what says so rather than assuming it.
	"""

	for index in range(4):
		ref = world.call("POST", "/v1/tasks", json={"title": f"Task {index}"}).json()["ref"]
		world.call("POST", f"/v1/tasks/{ref}/complete")

	seen: list[str] = []
	base = "/v1/tasks?status_category=done&order=-completed_at&limit=2"
	path: str | None = base

	while path is not None:
		answer = world.call("GET", path)

		assert answer.status_code == 200, answer.json()

		page = answer.json()
		seen.extend(item["title"] for item in page["items"])
		cursor = page["page"]["next_cursor"]
		path = None if cursor is None else f"{base}&cursor={cursor}"

	assert len(seen) == len(set(seen)) == 4


@pytest.fixture
def ranked (session: sqlalchemy.orm.Session) -> World:
	"""An installation with the indexed backend asked for, skipping where it cannot exist.

	`#871`: the native backend is PostgreSQL-only by decision, so on SQLite this is not a
	failure — there is nothing to test. Asked for through settings rather than patched, so the
	test drives the same resolution a real instance does.
	"""

	if session.get_bind().dialect.name != "postgresql":
		pytest.skip("relevance needs a backend that can rank")

	return _world(session, instance={"search_backend": "native"})


def test_a_search_for_a_number_puts_that_item_first (ranked: World) -> None:
	"""**`#867`'s other half, and it is why the predicate alone was not enough.**

	Driven on the served instance before this existed: `815` found `#815` and returned it
	**sixth**, below the fold of an agent's default page. So *a number finds the item* was true
	and worth nothing at a small limit.

	This is not an ordering special case. An exact identifier match is simply the best possible
	hit, which is what a search backend does with one — `db.fulltext.EXACT_MATCH_RANK` against
	a `ts_rank` that never reaches 1.
	"""

	subject = ranked.call("POST", "/v1/tasks", json={"title": "Entirely unlike"}).json()

	for _ in range(5):
		ranked.call(
			"POST",
			"/v1/tasks",
			json={"title": "Mentions it", "description": f"Follows #{subject['ref']}."},
		)

	found = ranked.call("GET", f"/v1/tasks?q={subject['ref']}&limit=50").json()["items"]

	assert found, "the probe matched nothing, so it proves nothing"
	assert found[0]["ref"] == subject["ref"], (
		f"the item asked for came back at position {[r['ref'] for r in found].index(subject['ref'])}"
	)


def test_a_ranked_search_can_be_paged_through (ranked: World) -> None:
	"""**More rows than the limit, which is the only way this test means anything.**

	`priority_score` shipped as an expression with no way to read the value back off a loaded
	row and returned **500 for every result set larger than one page** — invisible because the
	pagination tests only ever walked the default order. A ranking is the same shape: the sort
	value exists only in SQL, so a cursor has to carry what SQL computed.

	Two pages of three against seven rows, and every ref seen exactly once.
	"""

	for number in range(7):
		ranked.call(
			"POST", "/v1/tasks", json={"title": f"Quinsy {number}", "description": "quinsy"}
		)

	seen: list[int] = []
	cursor: str | None = None

	for _ in range(4):
		address = "/v1/tasks?q=quinsy&limit=3"

		if cursor is not None:
			address += f"&cursor={cursor}"

		page = ranked.call("GET", address).json()

		assert page["items"], "a page came back empty before the rows ran out"

		seen.extend(row["ref"] for row in page["items"])
		cursor = page["page"]["next_cursor"]

		if cursor is None:
			break

	assert len(seen) == 7, f"walked {len(seen)} rows of 7"
	assert len(set(seen)) == 7, "a row was repeated across a page boundary"


def test_relevance_is_refused_where_nothing_can_rank (world: World) -> None:
	"""It is not in the vocabulary unless a search ran and a backend can score it.

	Refused by name with the list of what *is* available — the same refusal any unknown sort
	field gets, rather than a special case somebody has to learn. Asked without a search, so
	this holds on either backend.
	"""

	answer = world.call("GET", "/v1/tasks?order=-relevance&limit=5")

	assert answer.status_code == 422
	assert "relevance" in answer.text
