"""Every worked example in ``/v1/docs/examples`` is executed against a real application.

SPEC.md §13.3 asks for worked request/response examples "executed by a CI job so they cannot
drift". This is that job. The examples are data in :data:`subroutine.api.meta.EXAMPLES` rather
than prose precisely so this test can run them — a shape written into a docstring is a shape
nothing checks.

The 2026-07-30 review found the guide promising `?include=backlinks`, which no endpoint
accepts, and the guard that was supposed to prevent that compared *paths* and could not see a
query parameter. Executing the examples is the version of that guard which cannot be
outsmarted: if the request does not work, the test fails.
"""

import typing

import pytest
import sqlalchemy.orm

import subroutine.api.meta
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def test_the_examples_document_is_served_and_readable (
	world: test_api_tasks.World,
) -> None:
	"""It is markdown, it is authenticated, and it names what it is for."""

	response = world.call("GET", "/v1/docs/examples")

	assert response.status_code == 200
	assert response.headers["content-type"].startswith("text/plain")
	assert "# Subroutine — worked examples" in response.text
	assert "Bearer sr_" in response.text


def test_meta_points_at_the_examples (world: test_api_tasks.World) -> None:
	"""An agent finds them from the response every client fetches first."""

	docs = world.call("GET", "/v1/meta").json()["docs"]

	assert docs["examples"] == "/v1/docs/examples"
	assert world.call("GET", docs["examples"]).status_code == 200


def test_the_guide_points_at_the_examples (world: test_api_tasks.World) -> None:
	"""The guide promises a worked call for each thing it describes; it has to deliver one."""

	guide = world.call("GET", "/v1/docs/agent").text

	assert "/v1/docs/examples" in guide


def test_every_example_actually_works (world: test_api_tasks.World) -> None:
	"""Run them in order, as an agent following the document would.

	In order and against one installation, because they build on each other: the link example
	names the task and document the two before it created. That is also what makes it a
	realistic check — each example's refs are the ones the previous responses handed back.
	"""

	outcomes: list[tuple[str, int]] = []

	for _description, method, path, body in subroutine.api.meta.EXAMPLES:
		kwargs: dict[str, typing.Any] = {} if body is None else {"json": body}
		response = world.call(method, path, **kwargs)

		outcomes.append((f"{method} {path}", response.status_code))

		assert response.status_code < 400, (
			f"{method} {path} answered {response.status_code}:\n{response.text}"
		)

	# Every one of them, not merely the first that happened to pass.
	assert len(outcomes) == len(subroutine.api.meta.EXAMPLES)


def test_the_link_example_needs_its_target_type (world: test_api_tasks.World) -> None:
	"""The example exists because omitting `target_type` is the easiest mistake in the API.

	Refs are shared between tasks and documents (§6.2) and `target_type` defaults to `task`, so
	linking a task to a document without it answers "There is no task '2' here" — about a task
	that genuinely does not exist, while document 2 does. Asserted so that the example cannot be
	"simplified" into the broken form.
	"""

	world.call("POST", "/v1/tasks", json={"title": "The task"})
	world.call("POST", "/v1/documents", json={"title": "The finding"})

	without = world.call(
		"POST", "/v1/tasks/1/links", json={"target": 2, "link_type": "derives_from"}
	)

	assert without.status_code == 404

	with_it = world.call(
		"POST",
		"/v1/tasks/1/links",
		json={"target": 2, "target_type": "document", "link_type": "derives_from"},
	)

	assert with_it.status_code == 201

	# And the example in the document is the working form.
	linked = next(
		example for example in subroutine.api.meta.EXAMPLES if "links" in example[2]
	)

	assert linked[3] is not None
	assert linked[3]["target_type"] == "document"


def test_the_date_field_names_the_guide_gives_are_the_ones_that_work (
	world: test_api_tasks.World,
) -> None:
	"""`due` goes in, `due_at` comes out, and sending `due_at` is refused rather than ignored.

	§8.3 singles this out as needing a worked example — "how do I clear a due date?" is
	otherwise a guess — and the guide stated the abstract rule with no field name at all.
	"""

	created = world.call(
		"POST", "/v1/tasks", json={"title": "Dated", "due": "tomorrow"}
	).json()

	assert created["due_at"] is not None

	# The name you read back is not the name you write, and the wrong one is a refusal.
	assert world.call("PATCH", "/v1/tasks/1", json={"due_at": None}).status_code == 422
	assert world.call("PATCH", "/v1/tasks/1", json={"due": None}).json()["due_at"] is None


def test_a_weekday_name_is_capture_shorthand_and_not_a_field_value (
	world: test_api_tasks.World,
) -> None:
	"""The two date grammars are not the same, and the help text used to imply they were.

	`subroutine plan 1 friday` works and `{"due": "friday"}` is a 422: a weekday name is
	shorthand the *capture* grammar resolves (§6.13), while a field takes §6.5's relative-date
	expressions. The `dates` topic listed both under one heading with no marking, and that
	topic is inlined into the agent guide — so an agent read "a weekday: monday, tuesday…" as a
	field value and got a refusal.

	Asserted rather than merely documented, because the fix was to the *text* and text is what
	drifts.
	"""

	refused = world.call("POST", "/v1/tasks", json={"title": "x", "due": "friday"})

	assert refused.status_code == 422
	assert "not a date this understands" in refused.json()["detail"]

	# The same day, through the grammar that does resolve it.
	captured = world.call("POST", "/v1/tasks", json={"text": "Thing by friday"})

	assert captured.status_code == 201
	assert captured.json()["due_at"] is not None

	# And the topic now says which is which.
	guide = world.call("GET", "/v1/docs/agent").text

	assert "(api)" in guide, "the dates topic marks the field-accepted forms"
