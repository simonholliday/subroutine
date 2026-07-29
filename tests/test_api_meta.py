"""``/v1/meta`` and the agent guide.

Most of these check that what is published is *true* — that a status key it names really
resolves, that a filter it advertises really filters, that a sort field it offers really
sorts. A discovery endpoint that describes an installation slightly inaccurately is worse
than none, because a client believes it.
"""

import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine
import subroutine.api.meta
import subroutine.db.models.vocabulary
import subroutine.domain.dates
import subroutine.domain.workspaces
import subroutine.errors
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def test_meta_reports_this_installation_rather_than_a_global_vocabulary (
	world: test_api_tasks.World,
) -> None:
	"""Statuses, item types and link types come from the workspace, because they are data."""

	body = world.call("GET", "/v1/meta").json()

	assert body["api_version"] == subroutine.API_VERSION
	assert body["instance"]["name"] == "Test"
	assert body["workspace"] == str(world.workspace.id)

	task_statuses = {status["key"] for status in body["statuses"]["task"]}

	assert {"open", "done"} <= task_statuses
	assert "document" in body["statuses"]
	assert {"task", "document"} <= set(body["item_types"])
	assert {link["key"] for link in body["link_types"]} >= {"blocks", "derives_from"}


def test_a_renamed_status_is_reported_under_its_new_name (
	session: sqlalchemy.orm.Session, world: test_api_tasks.World
) -> None:
	"""The whole point of the endpoint.

	An installation renames ``done`` to "Shipped" and a client that assumed a global
	vocabulary breaks. This one reads the local one — and the *category* stays fixed, which
	is what a client branches on.
	"""

	model = subroutine.db.models.vocabulary.Status
	done = session.scalars(
		sqlalchemy.select(model).where(
			model.workspace_id == world.workspace.id,
			model.entity_type == "task",
			model.key == "done",
		)
	).one()
	done.label = "Shipped"
	session.flush()

	reported = {
		status["key"]: status for status in world.call("GET", "/v1/meta").json()["statuses"]["task"]
	}

	assert reported["done"]["label"] == "Shipped"
	assert reported["done"]["category"] == "done", "the category is the fixed thing"


def test_every_status_it_publishes_can_actually_be_used (
	world: test_api_tasks.World,
) -> None:
	"""A published key that the service refuses would be a lie a client acts on."""

	body = world.call("GET", "/v1/meta").json()
	created = world.call("POST", "/v1/tasks", json={"title": "Try every status"}).json()

	for status in body["statuses"]["task"]:
		response = world.call(
			"PATCH", f"/v1/tasks/{created['ref']}", json={"status": status["key"]}
		)

		assert response.status_code == 200, f"{status['key']} was published but refused"


def test_every_link_type_it_publishes_can_actually_be_used (
	world: test_api_tasks.World,
) -> None:
	"""Likewise for the link vocabulary."""

	body = world.call("GET", "/v1/meta").json()
	one = world.call("POST", "/v1/tasks", json={"title": "One"}).json()
	two = world.call("POST", "/v1/tasks", json={"title": "Two"}).json()

	for link_type in body["link_types"]:
		response = world.call(
			"POST",
			f"/v1/tasks/{one['ref']}/links",
			json={"target": two["ref"], "link_type": link_type["key"]},
		)

		assert response.status_code == 201, f"{link_type['key']} was published but refused"


def test_every_sort_field_it_publishes_actually_sorts (world: test_api_tasks.World) -> None:
	"""Published from the routers' own constants, and checked against the live endpoint."""

	world.call("POST", "/v1/tasks", json={"title": "Something"})
	listings = world.call("GET", "/v1/meta").json()["listings"]

	for field in listings["task"]["sortable"]:
		assert world.call("GET", f"/v1/tasks?order={field}").status_code == 200
		assert world.call("GET", f"/v1/tasks?order=-{field}").status_code == 200


def test_every_filter_it_publishes_is_one_the_endpoint_accepts (
	world: test_api_tasks.World,
) -> None:
	"""Read from the application's own OpenAPI document, so it cannot claim a missing one."""

	listings = world.call("GET", "/v1/meta").json()["listings"]

	assert set(listings) == {"task", "document", "project"}
	assert listings["task"]["path"] == "/v1/tasks"
	assert {"project", "status", "q", "include_completed"} <= set(listings["task"]["filters"])

	# Paging controls are deliberately not listed as filters — they are how you read a
	# result, not how you narrow one.
	assert "cursor" not in listings["task"]["filters"]
	assert "limit" not in listings["task"]["filters"]


def test_it_does_not_publish_a_filter_grammar_it_cannot_parse (
	world: test_api_tasks.World,
) -> None:
	"""SPEC.md §6.13's rule, applied to §9: a smaller published grammar beats a false one.

	The filter operators (``gte``, ``between``, ``is_null``) are specified and not built.
	Publishing them would have a client compose a query this installation cannot answer.
	"""

	body = world.call("GET", "/v1/meta").json()

	assert "operators" not in body
	assert "fields" not in body

	for listing in body["listings"].values():
		assert all(isinstance(name, str) for name in listing["filters"])


def test_the_grammars_come_from_the_parsers_that_enforce_them (
	world: test_api_tasks.World,
) -> None:
	"""A guide listing a keyword the parser rejects is worse than no guide."""

	grammars = world.call("GET", "/v1/meta").json()["grammars"]

	assert set(grammars["relative_dates"]["vocabulary"]) == set(
		subroutine.domain.dates.KEYWORDS
	)
	assert grammars["durations"]["vocabulary"] == ["w", "d", "h", "m"]

	# And the examples it offers really parse.
	for example in grammars["relative_dates"]["examples"]:
		created = world.call("POST", "/v1/tasks", json={"title": "Try it", "due": example})

		assert created.status_code == 201, f"{example!r} was published but does not parse"


def test_the_tag_list_is_capped_and_says_so (
	session: sqlalchemy.orm.Session, world: test_api_tasks.World
) -> None:
	"""Appendix A filed this: an unbounded tag list makes discovery the expensive call."""

	for index in range(subroutine.api.meta.TAG_LIMIT + 5):
		world.call("POST", "/v1/tasks", json={"text": f"Task {index} #tag{index:03d}"})

	tags = world.call("GET", "/v1/meta").json()["tags"]

	assert len(tags["items"]) == subroutine.api.meta.TAG_LIMIT
	assert tags["truncated"] is True
	assert tags["total"] == subroutine.api.meta.TAG_LIMIT + 5


def test_the_error_codes_are_the_registry (world: test_api_tasks.World) -> None:
	"""Published from the registry, which also generates ``docs/errors.md``."""

	codes = world.call("GET", "/v1/meta").json()["error_codes"]

	assert codes == sorted(subroutine.errors.REGISTRY)


def test_the_source_url_is_published (world: test_api_tasks.World) -> None:
	"""SPEC.md §2.2: the AGPL's network clause makes this a product requirement.

	A served instance must offer its source to the people using it, so this is not optional
	and not a footnote.
	"""

	assert world.call("GET", "/v1/meta").json()["source_url"].startswith("https://")


def test_meta_does_not_demand_a_workspace_it_is_about_to_report (
	session: sqlalchemy.orm.Session, world: test_api_tasks.World
) -> None:
	"""The one endpoint that must not refuse for ambiguity.

	Every other listing refuses when the caller can reach several workspaces and names
	none. Doing that here would answer "which workspace?" to the request that exists to
	tell the client what workspaces there are.
	"""

	second = subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Other", owner=world.user
	)
	session.flush()

	body = world.call("GET", "/v1/meta").json()

	assert body["workspace"] is None
	assert body["statuses"] == {}
	assert len(body["workspaces"]) == 2

	narrowed = world.call("GET", f"/v1/meta?workspace_id={second.slug}").json()

	assert narrowed["workspace"] == str(second.id)
	assert narrowed["statuses"]["task"]


def test_meta_needs_a_credential (world: test_api_tasks.World) -> None:
	"""It reports a workspace's vocabulary, which is not public."""

	assert api_support.call(world.application, "GET", "/v1/meta").status_code == 401


def test_the_agent_guide_is_markdown_generated_from_the_parsers (
	world: test_api_tasks.World,
) -> None:
	"""The same text ``subroutine help`` prints, so the two cannot disagree."""

	response = world.call("GET", "/v1/docs/agent")

	assert response.status_code == 200
	assert response.headers["content-type"].startswith("text/plain")

	guide = response.text

	assert guide.startswith("# Subroutine")
	assert "Authorization: Bearer" in guide
	assert "GET /v1/meta" in guide

	# The PATCH rule is the one agents most often get wrong.
	assert "null" in guide and "cleared" in guide

	# And the vocabulary really is the parser's.
	for keyword in ("tomorrow", "end_of_week"):
		assert keyword in guide


def test_the_agent_guide_stays_small (world: test_api_tasks.World) -> None:
	"""SPEC.md §13.3 targets under 8 KB. Response size is a first-order cost for an agent."""

	guide = world.call("GET", "/v1/docs/agent").text

	assert len(guide.encode("utf-8")) < 8192, "the guide has grown past its budget"
