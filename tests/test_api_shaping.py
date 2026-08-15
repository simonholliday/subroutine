"""Response shaping — ``?fields=`` and ``?format=`` (SPEC.md §14.10).

Not cosmetic, and the tests are written to hold that line: a verbose task is 400-600 tokens
and fifty of them is a substantial fraction of an agent's working context. So there is a
test asserting the *saving* is real, not merely that the parameter is accepted — a shaping
feature that returned the same number of bytes would pass every other test in this file.

The other theme is that everything published is usable. ``/v1/meta`` lists selectable fields
and formats; two tests take that list and exercise every entry, for the reason the sortable
guard exists: a discovery endpoint that names something the endpoint refuses is worse than
one that names nothing.
"""

import json
import typing

import pytest
import sqlalchemy.orm

import subroutine.api.shaping
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def _populate (world: test_api_tasks.World) -> None:
	"""Three tasks with enough set on them for a compact line to be worth reading."""

	world.call(
		"POST",
		"/v1/tasks",
		json={
			"title": "Fix token prefix collision",
			"importance": 4,
			"urgency": 5,
			"due": "2026-08-01",
		},
	)
	world.call("POST", "/v1/tasks", json={"title": "Add /v1/changes endpoint", "importance": 4})
	world.call("POST", "/v1/tasks", json={"title": "Nothing assessed about this one"})


def test_the_default_response_is_unchanged (world: test_api_tasks.World) -> None:
	"""Shaping is opt-in, and a request that asks for none of it is what it always was."""

	_populate(world)

	body = world.call("GET", "/v1/tasks").json()

	assert set(body) == {"items", "page"}
	assert all(isinstance(item, dict) for item in body["items"])
	assert "title" in body["items"][0] and "version" in body["items"][0]


def test_fields_returns_only_what_was_asked_for (world: test_api_tasks.World) -> None:
	"""And the envelope stays, so pagination is found where it always was."""

	_populate(world)

	body = world.call("GET", "/v1/tasks?fields=ref,title").json()

	assert body["page"]["limit"] > 0, "the envelope survives shaping"
	assert all(set(item) == {"ref", "title"} for item in body["items"])


def test_fields_keeps_the_order_asked_for_and_ignores_a_repeat (
	world: test_api_tasks.World,
) -> None:
	"""``fields=ref,ref`` is a client generating a list, not a request for it twice."""

	_populate(world)

	body = world.call("GET", "/v1/tasks?fields=title,ref,title").json()

	assert list(body["items"][0]) == ["ref", "title"], "the view's own order, deduplicated"


def test_compact_is_one_aligned_line_per_item (world: test_api_tasks.World) -> None:
	"""§14.10's rendering: address, status, priority, deadline, title."""

	_populate(world)

	body = world.call("GET", "/v1/tasks?format=compact").json()
	lines = body["items"]

	assert all(isinstance(line, str) for line in lines)
	assert len(lines) == 3

	found = next(line for line in lines if "Fix token prefix collision" in line)

	assert found.startswith("#")
	assert "[open]" in found
	assert "I4/U5" in found, "both axes, as §6.3 pairs them"
	assert "2026-08-01" in found

	# Aligned down the page, which is what makes it scannable rather than merely short.
	titles = [line.index("[open]") for line in lines]

	assert len(set(titles)) == 1, f"the status column does not line up: {lines}"


def test_compact_says_when_a_priority_was_not_assessed (world: test_api_tasks.World) -> None:
	"""Absence is distinct from 1 (§6.3) and has to read as absence.

	A task nobody has assessed showing ``I1/U1`` would be a fabrication a client would sort
	on, and one with importance but no urgency has to say which half is missing.
	"""

	_populate(world)

	lines = world.call("GET", "/v1/tasks?format=compact").json()["items"]

	assert "—" in next(line for line in lines if "Nothing assessed" in line)
	assert "I4/U-" in next(line for line in lines if "changes endpoint" in line)


def test_compact_carries_the_planned_day_and_marks_it (world: test_api_tasks.World) -> None:
	"""§14.10 showed a deadline and not a plan, so the cheap format could not answer "next".

	An agent reading ``format=compact`` to decide what to do could see that nothing was due
	and not that something was planned for today — which meant a second call, which is the
	whole thing the cheap format exists to avoid.

	**The arrow is load-bearing, not decoration.** A column empty in every row is dropped, so
	a bare second date would sit in a position that moves depending on whether any row on the
	page has a deadline. Marked, the cell says what it is wherever it lands.
	"""

	_populate(world)
	world.call(
		"POST", "/v1/tasks", json={"title": "Planned for a day", "starts": "2026-08-03"}
	)

	lines = world.call("GET", "/v1/tasks?format=compact").json()["items"]
	found = next(line for line in lines if "Planned for a day" in line)

	assert "→2026-08-03" in found

	# And the deadline stays bare, so the two are told apart by the mark rather than by
	# counting columns.
	dated = next(line for line in lines if "Fix token prefix collision" in line)

	assert "2026-08-01" in dated
	assert "→" not in dated


def test_the_plan_column_costs_nothing_on_a_page_with_no_plans (
	world: test_api_tasks.World,
) -> None:
	"""The condition on which it was safe to add a column at all.

	Compared against the line this rendered before the plan existed: with nothing planned,
	every row must be exactly what it was, to the character.
	"""

	_populate(world)

	lines = world.call("GET", "/v1/tasks?format=compact").json()["items"]

	assert all("→" not in line for line in lines)
	assert lines == [
		"#3  [open]  —      —           Nothing assessed about this one",
		"#2  [open]  I4/U-  —           Add /v1/changes endpoint",
		"#1  [open]  I4/U5  2026-08-01  Fix token prefix collision",
	]


def test_the_compact_line_carries_the_assignee_as_a_username (
	world: test_api_tasks.World,
) -> None:
	"""`#511`. §14.10 promised ``@assignee`` and the view carried an ``assignee_id``.

	A UUID is not what anybody types into ``--assignee`` and not what a line has room for, so
	the column could not exist until the username was batch-loaded beside the statuses and the
	tags. It goes *after* the title on purpose: like ``→`` and ``#`` it marks itself, so
	nothing that already had a position moves.
	"""

	_populate(world)

	refs = world.call("GET", "/v1/tasks?format=ids").json()["items"]

	world.call("PATCH", f"/v1/tasks/{refs[0]}", json={"assignee": world.user.username})

	lines = world.call("GET", "/v1/tasks?format=compact").json()["items"]

	assert f"@{world.user.username}" in lines[0]
	assert str(world.user.id) not in lines[0], "a line has no room for a UUID and never had"

	# The other two are unassigned, and the column they share is blank rather than absent —
	# `shaping.aligned` drops a column only when *every* row leaves it empty.
	assert all("@" not in line for line in lines[1:])


def test_the_assignee_column_costs_nothing_when_nobody_is_assigned (
	world: test_api_tasks.World,
) -> None:
	"""The condition on which it was safe to add this column, and the plan column's argument.

	Measured the same way: with nobody assigned, every row is exactly what it was before this
	existed, to the character.
	"""

	_populate(world)

	lines = world.call("GET", "/v1/tasks?format=compact").json()["items"]

	assert lines == [
		"#3  [open]  —      —           Nothing assessed about this one",
		"#2  [open]  I4/U-  —           Add /v1/changes endpoint",
		"#1  [open]  I4/U5  2026-08-01  Fix token prefix collision",
	]


def test_ids_returns_the_addresses_alone (world: test_api_tasks.World) -> None:
	"""The smallest thing that is still useful: what to ask about next."""

	_populate(world)

	body = world.call("GET", "/v1/tasks?format=ids").json()

	assert body["items"] == sorted(body["items"], reverse=True), "newest first, as ever"
	assert all(isinstance(ref, int) for ref in body["items"])


def test_a_project_is_addressed_by_key_not_by_ref (world: test_api_tasks.World) -> None:
	"""``ids`` means "what you address it by", and for a project that is its key (§5.2)."""

	world.call("POST", "/v1/projects", json={"key": "web", "title": "Site"})

	body = world.call("GET", "/v1/projects?format=ids").json()

	assert "web" in body["items"]
	assert all(isinstance(key, str) for key in body["items"])


def test_shaping_works_on_a_single_entity_too (world: test_api_tasks.World) -> None:
	"""And stays unenveloped, as §8.4 requires of one entity."""

	created = world.call("POST", "/v1/tasks", json={"title": "Alone", "importance": 2}).json()

	selected = world.call("GET", f"/v1/tasks/{created['ref']}?fields=ref,title").json()

	assert selected == {"ref": created["ref"], "title": "Alone"}

	compact = world.call("GET", f"/v1/tasks/{created['ref']}?format=compact").json()

	assert isinstance(compact, str) and "Alone" in compact

	assert world.call("GET", f"/v1/tasks/{created['ref']}?format=ids").json() == created["ref"]


def test_a_compact_line_does_not_carry_a_whole_title (world: test_api_tasks.World) -> None:
	"""A 512-character title (§6.10) in a one-line rendering defeats the point of it."""

	# Comfortably over the compact limit and comfortably under §6.10's 512, which a first
	# version of this was not — the create was refused and the assertion failed on the
	# refusal rather than on the truncation.
	long = ("word " * 40).strip()
	created = world.call("POST", "/v1/tasks", json={"title": long}).json()

	line = world.call("GET", f"/v1/tasks/{created['ref']}?format=compact").json()

	assert len(line) < 120, line
	assert line.endswith("…"), "and it says that something was cut"

	# The full title is still available where a title belongs.
	assert world.call("GET", f"/v1/tasks/{created['ref']}").json()["title"] == long


def test_shaping_actually_saves_what_it_claims (world: test_api_tasks.World) -> None:
	"""The point of the feature, asserted rather than assumed.

	Measured at fifty tasks — the scale §14.10 talks about — compact came out about ten times
	smaller and ids about two hundred. The thresholds here are deliberately loose: the exact
	ratio depends on how much is set on each task, and a test that pinned it would fail on a
	change to the compact columns rather than on a regression in the saving.
	"""

	for index in range(30):
		world.call(
			"POST",
			"/v1/tasks",
			json={
				"title": f"Task number {index} with a realistic sort of title on it",
				"description": "A couple of sentences of context, as a real task would carry.",
				"importance": (index % 5) + 1,
				"urgency": (index % 4) + 1,
				"due": "2026-08-01",
			},
		)

	sizes = {
		name: len(world.call("GET", f"/v1/tasks?limit=30{query}").text)
		for name, query in (
			("full", ""),
			("compact", "&format=compact"),
			("ids", "&format=ids"),
			("fields", "&fields=ref,title"),
		)
	}

	assert sizes["compact"] * 5 < sizes["full"], sizes
	assert sizes["ids"] * 50 < sizes["full"], sizes
	assert sizes["fields"] * 3 < sizes["full"], sizes


@pytest.mark.parametrize(
	("query", "expected"),
	[
		("?format=nonsense", "format"),
		("?fields=ref,nope", "fields"),
		("?fields=", "fields"),
		("?fields=%20", "fields"),
		("?format=compact&fields=ref", "fields"),
		("?format=ids&fields=ref", "fields"),
	],
)
def test_a_request_that_cannot_be_honoured_is_refused (
	world: test_api_tasks.World, query: str, expected: str
) -> None:
	"""Naming the field at fault, per §8.8 — and never quietly picking a reading.

	``format`` together with ``fields`` is the interesting one: both describe the response,
	so a request carrying both has asked for two different things. Choosing one silently is
	choosing which of the caller's intentions to honour, which is the same objection §8.9
	makes to picking between two disagreeing versions.
	"""

	response = world.call("GET", f"/v1/tasks{query}")

	assert response.status_code == 422, response.text

	body = response.json()

	assert body["errors"][0]["field"] == expected
	assert body["errors"][0]["message"], "and says what would have worked"


def test_an_unknown_field_names_the_ones_that_exist (world: test_api_tasks.World) -> None:
	"""Being refused should not be how a client learns the vocabulary — but if it is, it works."""

	body = world.call("GET", "/v1/tasks?fields=titel").json()

	assert "title" in body["errors"][0]["message"]


@pytest.mark.parametrize("entity", ["task", "document", "project"])
def test_every_field_meta_publishes_can_actually_be_selected (
	world: test_api_tasks.World, entity: str
) -> None:
	"""The guard the sortable fields already have, for the same reason.

	A discovery endpoint naming a field the endpoint then refuses is worse than one naming
	nothing: the client has spent a round trip to be misled.
	"""

	world.call("POST", "/v1/tasks", json={"title": "Something"})
	world.call("POST", "/v1/documents", json={"title": "A note"})

	listing = world.call("GET", "/v1/meta").json()["listings"][entity]

	assert listing["selectable"], f"nothing published for {entity}"

	for field in listing["selectable"]:
		response = world.call("GET", f"{listing['path']}?fields={field}")

		assert response.status_code == 200, f"{entity}.{field} was published but refused"

		for item in response.json()["items"]:
			assert set(item) == {field}


@pytest.mark.parametrize("entity", ["task", "document", "project"])
def test_every_format_meta_publishes_actually_works (
	world: test_api_tasks.World, entity: str
) -> None:
	"""Likewise for the formats, on every entity that claims them."""

	world.call("POST", "/v1/tasks", json={"title": "Something"})
	world.call("POST", "/v1/documents", json={"title": "A note"})

	listing = world.call("GET", "/v1/meta").json()["listings"][entity]

	assert listing["formats"] == list(subroutine.api.shaping.FORMATS)

	for form in listing["formats"]:
		response = world.call("GET", f"{listing['path']}?format={form}")

		assert response.status_code == 200, f"{entity} refused format={form}"


def test_meta_does_not_call_the_shaping_parameters_filters (
	world: test_api_tasks.World,
) -> None:
	"""They are all query parameters, and reflection cannot tell them apart by itself.

	Calling ``format`` a filter would tell an agent it narrows a result set when it changes
	how the same rows are reported — a wrong answer to the question §13.1 exists to answer
	correctly.
	"""

	listings = world.call("GET", "/v1/meta").json()["listings"]

	for entity, listing in listings.items():
		assert "format" not in listing["filters"], entity
		assert "fields" not in listing["filters"], entity


def test_shaping_cannot_change_which_rows_come_back (world: test_api_tasks.World) -> None:
	"""It decides how a row is reported and nothing else.

	Worth asserting rather than assuming: a display parameter that reached the query would be
	a scoping bug wearing a formatting hat, and this project has already shipped two listings
	that returned the wrong rows.
	"""

	_populate(world)

	refs = {item["ref"] for item in world.call("GET", "/v1/tasks").json()["items"]}

	assert set(world.call("GET", "/v1/tasks?format=ids").json()["items"]) == refs
	assert {
		item["ref"] for item in world.call("GET", "/v1/tasks?fields=ref").json()["items"]
	} == refs
	assert len(world.call("GET", "/v1/tasks?format=compact").json()["items"]) == len(refs)


def test_a_shaped_page_still_paginates (world: test_api_tasks.World) -> None:
	"""The reason the envelope survives: a cursor has to come back from somewhere."""

	for index in range(5):
		world.call("POST", "/v1/tasks", json={"title": f"Task {index}"})

	first = world.call("GET", "/v1/tasks?limit=2&format=compact").json()

	assert len(first["items"]) == 2
	assert first["page"]["has_more"] is True
	assert first["page"]["next_cursor"]

	second = world.call(
		"GET", f"/v1/tasks?limit=2&format=compact&cursor={first['page']['next_cursor']}"
	).json()

	assert len(second["items"]) == 2
	assert second["items"] != first["items"]


def test_the_aligner_handles_an_empty_page () -> None:
	"""A listing that matched nothing must not be an index error."""

	assert subroutine.api.shaping.aligned([]) == []


def test_a_column_empty_in_every_row_is_dropped () -> None:
	"""Two spaces per row for a column that says nothing is the waste this module removes.

	The case that produced it: a project listing with nothing private in it, where the
	visibility column is empty on every line and was still being padded and joined.
	"""

	assert subroutine.api.shaping.aligned(
		[("inbox", "[active]", "", "Inbox"), ("web", "[active]", "", "Website")]
	) == ["inbox  [active]  Inbox", "web    [active]  Website"]

	# And it stays when any row has something to say in it.
	assert subroutine.api.shaping.aligned(
		[("inbox", "[active]", "", "Inbox"), ("sec", "[active]", "private", "Secrets")]
	) == ["inbox  [active]           Inbox", "sec    [active]  private  Secrets"]


def test_the_openapi_document_still_describes_the_default (
	world: test_api_tasks.World,
) -> None:
	"""``response_model`` is what keeps that true while the endpoint returns other shapes.

	Without it the schema would degrade to "anything", and an agent generating a client from
	the document would get nothing useful for the case almost every caller is in.
	"""

	schema: dict[str, typing.Any] = json.loads(
		world.call("GET", "/v1/openapi.json").text
	)
	response = schema["paths"]["/v1/tasks"]["get"]["responses"]["200"]
	reference = response["content"]["application/json"]["schema"]["$ref"]

	assert "Collection" in reference and "Task" in reference


def test_a_single_read_refuses_a_parameter_it_does_not_declare (
	world: test_api_tasks.World,
) -> None:
	"""`#676`. The refusal used to stop at the listings, and shaping does not.

	Driven on all four shaping single reads rather than on one, because the dependency is
	declared per route and the failure this fixes was four routes agreeing and one being
	forgotten.
	"""

	task = world.call("POST", "/v1/tasks", json={"title": "A task"}).json()
	document = world.call(
		"POST", "/v1/documents", json={"title": "A document", "body": "."}
	).json()
	project = world.call("POST", "/v1/projects", json={"key": "web", "title": "Web"}).json()

	addresses = [
		f"/v1/tasks/{task['ref']}",
		f"/v1/documents/{document['ref']}",
		f"/v1/projects/{project['key']}",
		f"/v1/workspaces/{world.workspace.slug}",
	]

	for address in addresses:
		refused = world.call("GET", f"{address}?fieldz=ref")

		assert refused.status_code == 422, f"{address} answered {refused.status_code}"

		body = refused.json()

		assert body["code"] == "unknown_field"
		assert body["errors"][0]["field"] == "fieldz"
		assert "fields" in body["errors"][0]["hint"], "it must say what would have worked"

		# And the correct spelling still works, or the refusal above would be proving
		# nothing more interesting than that the route is broken.
		accepted = world.call("GET", f"{address}?fields=id")

		assert accepted.status_code == 200, accepted.text
		assert set(accepted.json()) == {"id"}


def test_the_workspace_refusal_no_longer_answers_about_the_wrong_thing (
	world: test_api_tasks.World,
) -> None:
	"""`#676`'s reported symptom, which was two faults compounding.

	``?workspace=personal`` was discarded unheard, and the workspace dependency then refused
	*because nothing named a workspace* — describing a request nobody had sent, and listing
	workspaces by name and id, which reads as a menu of values rather than as a missing key.
	So the caller's next guess is another value.

	The unknown-parameter refusal runs first now, so the answer names the key. That ordering
	is the whole fix and is asserted rather than assumed: it is a property of where the
	dependency is declared, not of anything in this module.
	"""

	task = world.call("POST", "/v1/tasks", json={"title": "A task"}).json()

	refused = world.call("GET", f"/v1/tasks/{task['ref']}?workspace=whatever")

	assert refused.status_code == 422, refused.text

	body = refused.json()

	assert body["code"] == "unknown_field", "the workspace refusal answered first"
	assert body["errors"][0]["field"] == "workspace"
	assert "workspace_id" in body["errors"][0]["hint"], "and it must name the key that works"


def test_an_endpoint_that_takes_nothing_says_so_rather_than_trailing_off (
	world: test_api_tasks.World,
) -> None:
	"""``GET /v1/me`` declares no query parameters at all, so the list of them is empty.

	Worth its own case because the obvious rendering is "It accepts: .", which reads as a
	message that was cut off rather than as an answer — and this is the first call an agent
	makes, so it is the worst place to look broken.
	"""

	refused = world.call("GET", "/v1/me?workspace=whatever")

	assert refused.status_code == 422, refused.text

	hint = refused.json()["errors"][0]["hint"]

	assert "no query parameters" in hint
	assert "It accepts: ." not in hint
