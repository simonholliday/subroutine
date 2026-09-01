"""Grouped listings — one query answered as several, each with its own allowance (`#1790`).

**The defect this is about is silent, and the tests are written to hold that line.** A board
spends one page across every column in one order, so a column holding older work loses its rows
to an unrelated column's recency and says nothing. Measured on the project's own instance when
the item was filed: *In progress* drew one row where three existed, and *Open* drew 27 where
there were 275. Nothing in the response was wrong — the page was honest about itself — and every
column heading read as a total.

So the first test here is the *defect*, reproduced: it asserts that an ungrouped page really
does starve a group, and then that the grouped one does not. A test that only checked the
grouped shape would pass against a version that grouped a page it had already truncated.
"""

import typing

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.domain.grouping
import subroutine.views
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def _spread (world: test_api_tasks.World) -> None:
	"""Six open tasks written first, then two started ones — the shape the defect needs.

	**The order matters and is the whole fixture.** The started tasks are newest, so a
	newest-first page small enough to be interesting holds only those; the six older ones are
	what a single allowance loses. Reverse it and the defect cannot be reproduced at all.
	"""

	for number in range(1, 7):
		assert world.call("POST", "/v1/tasks", json={"title": f"Open {number}"}).status_code == 201

	for number in (1, 2):
		created = world.call("POST", "/v1/tasks", json={"title": f"Started {number}"})

		assert created.status_code == 201
		assert world.call(
			"PATCH",
			f"/v1/tasks/{created.json()['ref']}",
			json={"status": "in_progress"},
		).status_code == 200


def _drawn (payload: dict[str, typing.Any]) -> dict[str, int]:
	"""Return how many rows each group actually holds, keyed by group."""

	return {group["key"]: len(group["items"]) for group in payload["groups"]}


def test_one_allowance_starves_a_group_and_its_own_allowance_does_not (
	world: test_api_tasks.World,
) -> None:
	"""The defect and the fix, measured against each other on one fixture.

	**Both halves, because either alone proves nothing.** Without the first, a grouped answer
	that had truncated before grouping would pass; without the second there is no fix.
	"""

	_spread(world)

	flat = world.call("GET", "/v1/tasks?limit=2&fields=ref,status_category")

	assert flat.status_code == 200

	starved = [
		row for row in flat.json()["items"] if row["status_category"] == "todo"
	]

	assert starved == [], (
		"the fixture no longer reproduces the defect: a two-row page was supposed to hold "
		"only the started tasks, so that the six open ones are the rows a single allowance loses"
	)

	grouped = world.call("GET", "/v1/tasks?group_by=status_category&group_limit=2&fields=ref")

	assert grouped.status_code == 200

	drawn = _drawn(grouped.json())

	assert drawn["todo"] == 2, "a group did not get an allowance of its own"
	assert drawn["in_progress"] == 2, "a group lost rows to another group's allowance"


def test_every_group_the_axis_has_is_reported_including_the_empty_ones (
	world: test_api_tasks.World,
) -> None:
	"""An empty group is present and says it is empty.

	**This is the property, not a convenience.** A response assembled from the rows that came
	back cannot tell *this column holds nothing* from *this column was not asked about*, and
	`#718`, `#738` and `#744` are three separate filings of a surface stating the first when the
	second was true. Deriving the groups from the fixed vocabulary is what makes the difference
	sayable at all.
	"""

	_spread(world)

	payload = world.call(
		"GET", "/v1/tasks?group_by=status_category&fields=ref&include_completed=true"
	).json()

	assert [group["key"] for group in payload["groups"]] == list(
		subroutine.domain.grouping.keys_for("status_category", kind="task")
	)

	drawn = _drawn(payload)

	assert drawn["cancelled"] == 0
	assert drawn["done"] == 0


def test_a_group_says_when_it_held_something_back (world: test_api_tasks.World) -> None:
	"""``has_more`` is per group, and it is what a column heading needs.

	Simon's own example is *"in progress: 20 (and more hidden)"*, which is the boolean rather
	than the count — so this is the half that must never be behind a flag. A total costs a scan
	per group and stays where §8.4 already put that trade.
	"""

	_spread(world)

	payload = world.call(
		"GET", "/v1/tasks?group_by=status_category&group_limit=2&fields=ref"
	).json()

	pages = {group["key"]: group["page"] for group in payload["groups"]}

	assert pages["todo"]["has_more"] is True, "a cut group did not say it was cut"
	assert pages["in_progress"]["has_more"] is False, "a whole group claimed to be cut"
	assert pages["todo"]["total"] is None, "a total was computed without being asked for"

	counted = world.call(
		"GET",
		"/v1/tasks?group_by=status_category&group_limit=2&fields=ref&include_total=true",
	).json()

	assert {group["key"]: group["page"]["total"] for group in counted["groups"]}["todo"] == 6


def test_a_groups_cursor_continues_on_a_listing_narrowed_to_that_group (
	world: test_api_tasks.World,
) -> None:
	"""The claim that makes *show more* on one column cost nothing new.

	A cursor names a position in a sort order; a group's order is the listing's order; so the
	cursor a group hands back is valid on an ordinary listing narrowed to that group. If this
	stops being true, a column can be drawn and cannot be paged, which is worse than not
	splitting it at all.
	"""

	_spread(world)

	first = world.call(
		"GET", "/v1/tasks?group_by=status_category&group_limit=2&fields=ref"
	).json()

	todo = next(group for group in first["groups"] if group["key"] == "todo")
	seen = [row["ref"] for row in todo["items"]]

	assert todo["page"]["next_cursor"] is not None

	following = world.call(
		"GET",
		"/v1/tasks?status_category=todo&limit=2&fields=ref"
		f"&cursor={todo['page']['next_cursor']}",
	)

	assert following.status_code == 200

	after = [row["ref"] for row in following.json()["items"]]

	assert after and not set(after) & set(seen), (
		f"the group's cursor did not continue the group: saw {seen}, then {after}"
	)
	assert after == sorted(after, reverse=True)


def test_a_grouped_listing_asks_a_bounded_number_of_questions (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""Grouping must not turn one listing into an N+1 — and the first version of it did.

	The vocabulary a group's rows name was loaded inside the row comprehension, so it ran once
	per **row** rather than once per group. Nothing about the answer changed, which is exactly
	why a shape test could not see it: `#39` reintroduced by the change that removes a
	listing's own.
	"""

	counted: list[str] = []

	def record (
		_connection: typing.Any, _cursor: typing.Any, statement: str, *_rest: typing.Any
	) -> None:
		"""Note every statement the engine is asked to run."""

		counted.append(statement)

	def queries_for (rows: int) -> int:
		"""Return how many statements one grouped listing of ``rows`` open tasks takes."""

		for number in range(rows):
			assert world.call(
				"POST", "/v1/tasks", json={"title": f"Row {number}"}
			).status_code == 201

		counted.clear()
		sqlalchemy.event.listen(session.get_bind(), "before_cursor_execute", record)

		try:
			response = world.call("GET", "/v1/tasks?group_by=status_category&fields=ref")

			assert response.status_code == 200

			return len(counted)

		finally:
			sqlalchemy.event.remove(session.get_bind(), "before_cursor_execute", record)

	small = queries_for(2)
	large = queries_for(14)

	assert large == small, (
		f"a grouped page of 16 tasks took {large} queries where 2 took {small}: "
		f"the grouping is fanning out per row"
	)


def test_a_grouping_this_listing_does_not_have_is_refused_by_name (
	world: test_api_tasks.World,
) -> None:
	"""Refused rather than ignored — `#1484`'s rule, and `#1626`'s defect.

	A listing that quietly drops an axis it does not understand answers with the whole
	ungrouped page, and **the wrong answer is a superset**, so nothing looks broken.
	"""

	refused = world.call("GET", "/v1/tasks?group_by=assignee")

	assert refused.status_code == 422

	problem = refused.json()

	# **``query.`` is part of the answer, not noise** (`#1315`). The name alone leaves a caller
	# to guess whether it belongs in the body, and being refused twice for one mistake is what
	# that item is about.
	assert problem["errors"][0]["field"] == "query.group_by"
	assert "status_category" in problem["errors"][0]["hint"]


def test_a_cursor_and_a_grouping_cannot_be_sent_together (
	world: test_api_tasks.World,
) -> None:
	"""There is no one place in a grouped answer to continue from, so saying so beats guessing."""

	refused = world.call("GET", "/v1/tasks?group_by=status_category&cursor=anything")

	assert refused.status_code == 422
	assert refused.json()["errors"][0]["field"] == "query.cursor"


def test_a_group_cannot_be_asked_for_more_than_the_ceiling (
	world: test_api_tasks.World,
) -> None:
	"""The cost of a grouped request is the allowance times the groups, so the allowance is capped.

	**The refusal names ``group_limit``**, not ``limit`` and not FastAPI's ``query.limit`` — a
	caller told the wrong field name looks in the wrong place, which is `#1534` nine times over.
	"""

	refused = world.call(
		"GET",
		f"/v1/tasks?group_by=status_category"
		f"&group_limit={subroutine.domain.grouping.MAX_GROUP_SIZE + 1}",
	)

	assert refused.status_code == 422
	assert refused.json()["errors"][0]["field"] == "query.group_limit"


def test_a_document_listing_groups_by_its_own_categories (
	world: test_api_tasks.World,
) -> None:
	"""A document's categories are not a task's, and the axis follows the kind.

	A superseded specification is not *done* (docs/design.md §5.5), so grouping a document
	listing by the task vocabulary would produce four columns nothing can ever be in.
	"""

	created = world.call("POST", "/v1/documents", json={"title": "A note", "body": "why"})

	assert created.status_code == 201

	payload = world.call("GET", "/v1/documents?group_by=status_category&fields=ref").json()

	assert [group["key"] for group in payload["groups"]] == list(
		subroutine.domain.grouping.keys_for("status_category", kind="document")
	)
	assert sum(len(group["items"]) for group in payload["groups"]) == 1


def test_a_grouped_listing_keeps_every_narrowing_it_was_given (
	world: test_api_tasks.World,
) -> None:
	"""Grouping is a parameter on the listing, so the listing's filters still apply.

	That is the whole reason it is not a route of its own: a second route would have had to
	redeclare twenty narrowings and then keep them in step for ever.
	"""

	_spread(world)

	payload = world.call(
		"GET", "/v1/tasks?group_by=status_category&fields=ref,title&q=Started"
	).json()

	titles = [row["title"] for group in payload["groups"] for row in group["items"]]

	assert titles, "the search found nothing, so this proves nothing"
	assert all(title.startswith("Started") for title in titles), (
		f"a narrowing was dropped on the grouped path: {titles}"
	)


def test_the_default_answer_is_exactly_what_the_published_model_describes (
	world: test_api_tasks.World,
) -> None:
	"""The grouped response is assembled by hand, so something must hold it to its own shape.

	``views.Grouped`` is the contract a client parses into, and the endpoint builds a plain
	document rather than returning the model — it has to, because a shaped item is a line or an
	address and not a task. That leaves the two free to drift, with the model describing
	something the server has stopped sending and nothing to notice.

	**So the model is the assertion.** Parsing the real answer with it makes it load-bearing
	rather than decorative, which is the difference between a published contract and one of
	this project's declared-and-inert controls.
	"""

	_spread(world)

	response = world.call("GET", "/v1/tasks?group_by=status_category")

	assert response.status_code == 200

	parsed = subroutine.views.Grouped[subroutine.views.Task].model_validate(response.json())

	assert parsed.group_by == "status_category"
	assert [group.key for group in parsed.groups] == list(
		subroutine.domain.grouping.keys_for("status_category", kind="task")
	)

	started = next(group for group in parsed.groups if group.key == "in_progress")

	assert [task.title for task in started.items] == ["Started 2", "Started 1"]
	assert started.page.limit == subroutine.domain.grouping.DEFAULT_GROUP_SIZE


def test_grouping_a_listing_costs_a_handful_of_questions_more_than_not_grouping_it (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""`SR#1799`. The guard above cannot see an N+1 that is per *group* rather than per row.

	It compares a page of 2 rows against a page of 16 and asserts the count does not move,
	which is exactly right for a fan-out over rows and blind to one over columns — there are
	always four of those, so the number is stable while being four times what it should be.

	**That is not hypothetical.** `SR#1790` shipped calling the renderer inside the group loop,
	and rendering is where :class:`subroutine.views.Vocabulary` is built — which is not a lookup
	table but a batch of readiness queries. A four-column board ran the whole readiness batch
	four times: 63 statements against an ungrouped listing's 23, and 1.8x the time on the real
	instance's data.

	**Compared against the same listing ungrouped**, because that is the property. An absolute
	ceiling would have to be raised whenever a listing legitimately grows a question, and would
	then stop measuring the thing this is about.
	"""

	_spread(world)

	counted: list[str] = []

	def record (
		_connection: typing.Any, _cursor: typing.Any, statement: str, *_rest: typing.Any
	) -> None:
		"""Note every statement the engine is asked to run."""

		counted.append(statement)

	def queries_for (path: str) -> int:
		"""Return how many statements one listing takes."""

		world.call("GET", path)

		counted.clear()
		sqlalchemy.event.listen(session.get_bind(), "before_cursor_execute", record)

		try:
			assert world.call("GET", path).status_code == 200

			return len(counted)

		finally:
			sqlalchemy.event.remove(session.get_bind(), "before_cursor_execute", record)

	fields = "ref,title,status_category,blocked,blocking,assignee,project_key"

	flat = queries_for(f"/v1/tasks?limit=100&include_completed=true&fields={fields}")
	grouped = queries_for(
		f"/v1/tasks?group_by=status_category&group_limit=25&include_completed=true"
		f"&fields={fields}"
	)

	#: What splitting an answer is allowed to cost, in statements.
	#:
	#: One lookup to find each group's statuses, and one query per group for its rows — four
	#: task categories, so five. The measured figure is four, because the ungrouped listing's
	#: own row query is one of the ones being replaced. Anything beyond this is something being
	#: run per group that belongs to the page.
	allowance = 6

	assert grouped <= flat + allowance, (
		f"grouping a listing took {grouped} statements against {flat} ungrouped, which is "
		f"more than the {allowance} that splitting it can account for. Something the page "
		f"pays for once is being paid per group — the vocabulary is where that happened "
		f"before, and it carries the readiness batch with it."
	)
