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
		key="SECRET",
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


def test_the_two_halves_of_the_ranking_rule_agree_on_every_row (
	world: World, session: sqlalchemy.orm.Session
) -> None:
	"""The SQL expression and the Python reader must return the same number, always.

	They are two statements of one rule, which is the pair this codebase has watched
	disagree before. Here the consequence is specific rather than cosmetic: the expression
	*orders* the query and the reader names the row a cursor stopped at, so a disagreement is
	a page boundary that silently skips or repeats rows — the failure keyset pagination
	exists to prevent, reintroduced underneath it.

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

	rows = list(
		session.scalars(
			sqlalchemy.select(subroutine.db.models.work.Task).where(
				subroutine.db.models.work.Task.deleted_at.is_(None)
			)
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

	for row in rows:
		assert from_sql[row.id] == subroutine.domain.ordering.ranking(row), (
			f"the two halves disagree for importance={row.importance} urgency={row.urgency}: "
			f"SQL said {from_sql[row.id]}, Python said {subroutine.domain.ordering.ranking(row)}"
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
		key="SECRET",
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
		key="SECRET",
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
