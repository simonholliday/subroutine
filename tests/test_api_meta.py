"""``/v1/meta`` and the agent guide.

Most of these check that what is published is *true* — that a status key it names really
resolves, that a filter it advertises really filters, that a sort field it offers really
sorts. A discovery endpoint that describes an installation slightly inaccurately is worse
than none, because a client believes it.
"""

import re
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import api_support
import subroutine
import subroutine.api.app
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


def test_meta_says_what_this_is_for_the_caller_that_cannot_read_the_readme (
	world: test_api_tasks.World,
) -> None:
	"""An agent arriving with a base URL and a token has no other way to find out.

	A person has the README. This is the one response every client fetches first, which is
	why the sentence is here as well as in the guide it points at.
	"""

	body = world.call("GET", "/v1/meta").json()

	assert "docs/agent" in body["purpose"], "and it says where the rest is"
	assert body["docs"]["agent_guide"] == "/v1/docs/agent"


def test_the_agent_guide_says_what_the_reader_gets_before_how_to_authenticate (
	world: test_api_tasks.World,
) -> None:
	"""Ordering, asserted rather than left to whoever edits next.

	A guide that opens with "read /v1/meta first" reads as a chore having been handed over.
	An agent has no other source for why it should bother, and §14.1 had already enumerated
	the failure modes this answers without ever pointing them at that reader.
	"""

	guide = world.call("GET", "/v1/docs/agent").text

	why = guide.index("Why this is worth your context")
	how = guide.index("## How to use it")

	assert why < how
	assert why < guide.index("Authorization: Bearer")

	# The reciprocal argument, which is the part an agent will act on and will not infer.
	assert "Being bounded is what earns you more to do" in guide


def test_the_agent_guide_names_no_endpoint_this_application_does_not_serve (
	world: test_api_tasks.World,
) -> None:
	"""The guard on §13.1's rule, pointed at the guide rather than at ``/v1/meta``.

	Publishing what an installation does not implement is worse than publishing less, and it
	is worse here than anywhere: an agent told to leave a handoff at an endpoint that 404s
	stops trusting the rest of the document. Sessions, decisions and verification evidence are
	specified and unbuilt, so the guide must name them as absent and never as reachable.

	Compared by path prefix rather than by the whole path, because the guide writes
	illustrative forms — ``/v1/tasks/42`` for ``/v1/tasks/{id_or_ref}``. The prefix is what
	catches the failure this exists for.
	"""

	guide = world.call("GET", "/v1/docs/agent").text
	# ``getattr`` rather than ``route.path``: Starlette's ``BaseRoute`` does not declare one,
	# and a route without a path is exactly what `api/routing.check` exists to notice — see
	# CLAUDE.md on `_IncludedRouter`.
	paths = [
		getattr(route, "path", "")
		for _prefix, router in subroutine.api.app.ROUTERS
		for route in router.routes
	]
	served = {
		"/".join(path.split("/")[:3]) for path in paths if path.startswith("/v1/")
	}
	named = {"/".join(found.split("/")[:3]) for found in re.findall(r"/v1/[\w/{}-]+", guide)}

	assert named, "the guide should reference at least one endpoint"
	assert named <= served, f"the guide names endpoints that do not exist: {named - served}"


@pytest.mark.parametrize(
	"unbuilt", ["/v1/sessions", "/v1/decisions", "/v1/verifications", "/v1/claims"]
)
def test_the_guide_does_not_offer_the_unbuilt_agent_machinery (
	world: test_api_tasks.World, unbuilt: str
) -> None:
	"""Named explicitly, because these are the four an agent would most want to exist.

	They are specified in full (§14, §15) and are M4-M7. The guide says so in one sentence;
	what it must not do is describe how to call them.
	"""

	guide = world.call("GET", "/v1/docs/agent").text

	assert unbuilt not in guide
	assert "Not built yet" in guide, "and the absence is stated rather than left to be found"
