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


def test_ids_returns_the_addresses_alone (world: test_api_tasks.World) -> None:
	"""The smallest thing that is still useful: what to ask about next."""

	_populate(world)

	body = world.call("GET", "/v1/tasks?format=ids").json()

	assert body["items"] == sorted(body["items"], reverse=True), "newest first, as ever"
	assert all(isinstance(ref, int) for ref in body["items"])


def test_a_project_is_addressed_by_key_not_by_ref (world: test_api_tasks.World) -> None:
	"""``ids`` means "what you address it by", and for a project that is its key (§5.2)."""

	world.call("POST", "/v1/projects", json={"key": "WEB", "title": "Site"})

	body = world.call("GET", "/v1/projects?format=ids").json()

	assert "WEB" in body["items"]
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
		[("INBOX", "[active]", "", "Inbox"), ("WEB", "[active]", "", "Website")]
	) == ["INBOX  [active]  Inbox", "WEB    [active]  Website"]

	# And it stays when any row has something to say in it.
	assert subroutine.api.shaping.aligned(
		[("INBOX", "[active]", "", "Inbox"), ("SEC", "[active]", "private", "Secrets")]
	) == ["INBOX  [active]           Inbox", "SEC    [active]  private  Secrets"]


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
