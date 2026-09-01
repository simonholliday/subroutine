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
import subroutine.cli.topics
import subroutine.db.models.vocabulary
import subroutine.domain.capture
import subroutine.domain.dates
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.installations
import subroutine.mcp.tools
import subroutine.views
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
	"""Published from the routers' own constants, and checked against the live endpoint.

	**The floor is load-bearing since ``order`` gained a default.** It replaced ``sortable``,
	which was a required field — so an endpoint that forgot to fill it produced a body the
	client refused outright, and this loop could never run on nothing. A defaulted field fails
	the other way: the key is present, empty, and every ``for`` over it passes by running zero
	times. The default is deliberate and cannot go (an older instance sends no ``order`` at
	all), so the assertion has to carry what the type used to.
	"""

	world.call("POST", "/v1/tasks", json={"title": "Something"})
	listings = world.call("GET", "/v1/meta").json()["listings"]

	assert listings["task"]["order"], "nothing published, so this asserts about an empty set"

	for field in listings["task"]["order"]:
		assert world.call("GET", f"/v1/tasks?order={field}").status_code == 200
		assert world.call("GET", f"/v1/tasks?order=-{field}").status_code == 200


def test_every_filter_it_publishes_is_one_the_endpoint_accepts (
	world: test_api_tasks.World,
) -> None:
	"""Read from the application's own OpenAPI document, so it cannot claim a missing one."""

	listings = world.call("GET", "/v1/meta").json()["listings"]

	# **Which entities, read off the table that publishes them.** This was a literal of three
	# and the fourth broke it — reporting a set difference rather than anything about whether
	# the endpoint accepts what it advertises, which is what this test is for. A second copy of
	# a list the application already declares is the defect this codebase meets most.
	assert set(listings) == {entity for entity, *_rest in subroutine.api.meta.LISTINGS}
	assert len(listings) > 3, "the published table shrank; is a listing no longer discoverable?"
	assert listings["task"]["path"] == "/v1/tasks"
	assert {"project", "status", "q", "include_completed"} <= set(listings["task"]["filters"])

	# Paging controls are deliberately not listed as filters — they are how you read a
	# result, not how you narrow one.
	assert "cursor" not in listings["task"]["filters"]
	assert "limit" not in listings["task"]["filters"]

	# **And neither is arrangement, reporting or scope** — `SR#1803`, design `SR#1801` §1. All
	# four of these were published as filters, and `group_by` and `group_limit` arrived with
	# `SR#1790` four commits before anybody looked. An agent building a request from that list
	# was being told something false about every one of them.
	for excused in ("group_by", "group_limit", "include", "workspace_id"):
		assert excused not in listings["task"]["filters"], (
			f"{excused!r} is published as a filter and narrows nothing"
		)


def test_every_parameter_a_listing_takes_is_a_filter_or_says_why_not (
	world: test_api_tasks.World,
) -> None:
	"""**What makes an entry in ``meta.NOT_FILTERS`` go away** — `SR#1803`, and the question
	every excuse list in this repository is required to answer (`SR#405`).

	That set had no such rule, which is half of why it fell four names behind: it could name a
	parameter no route declares for ever, and nothing about a *new* parameter asked whether it
	belonged in it. This closes the first half. The second — whether a name really is a filter
	— has a right answer per name that no test can decide, so the register carries a written
	reason each and this holds them to naming something real.

	**Read off the application rather than off a list**, so a listing added tomorrow is in scope
	the day it is registered.
	"""

	schema = world.application.openapi()
	declared: set[str] = set()

	for _entity, path, *_rest in subroutine.api.meta.LISTINGS:
		operation = schema.get("paths", {}).get(path, {}).get("get", {})
		declared |= {
			parameter["name"]
			for parameter in operation.get("parameters", [])
			if parameter.get("in") == "query"
		}

	# **The floor.** A reflection that read nothing would make every entry look live.
	assert len(declared) > 20, (
		f"only {len(declared)} query parameters were found across every listing, so this is "
		f"checking the excuses against an empty set"
	)

	stale = sorted(set(subroutine.api.meta.NOT_FILTERS) - declared)

	assert not stale, (
		f"{stale} is excused from being called a filter and is a parameter of no listing — "
		f"the entry describes something that has gone"
	)

	unexplained = sorted(
		name for name, why in subroutine.api.meta.NOT_FILTERS.items() if not why.strip()
	)

	assert not unexplained, (
		f"{unexplained} is excused with no reason, and *is this a filter* has a right answer "
		f"per name that a reader cannot check without one"
	)


def test_it_does_not_publish_a_filter_grammar_it_cannot_parse (
	world: test_api_tasks.World,
) -> None:
	"""docs/design.md §6.13's rule, applied to §9: a smaller published grammar beats a false one.

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
	"""docs/design.md §2.2: a commitment this product keeps although nothing compels it.

	The AGPL's network clause used to require it. FSL-1.1-ALv2 does not require anything of a
	served instance at all — so this test is now the only thing holding the promise, which is
	a reason to keep it rather than to relax it.
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


def test_an_empty_vocabulary_says_why_it_is_empty (
	session: sqlalchemy.orm.Session, world: test_api_tasks.World
) -> None:
	"""`SR#627`, Simon's decision of 2026-08-28: a flag and a sentence, in one field.

	The leniency the test above pins is right and stays. What was wrong is that nothing in the
	answer related *the maps are empty* to *no workspace was named* — and ``statuses: {}`` is
	exactly what a fresh single-workspace installation says, so the agent in `SR#615` read it as
	*this instance has no custom vocabulary* and acted on it. From the one endpoint whose whole
	purpose is preventing a guess.

	**Presence is the flag and the value is the sentence.** A client branches on ``is not None``
	without parsing prose; a person or an agent reading the raw response is told what happened
	and what to do about it. A boolean beside a sentence that is null under identical conditions
	would be two fields carrying one bit.

	Three cases, and the first and last are what stop this becoming a line on every answer.
	"""

	alone = world.call("GET", "/v1/meta").json()

	assert alone["statuses"]["task"], "one workspace resolves, so nothing is withheld"
	assert alone["vocabulary_not_shown"] is None, (
		"an installation with one workspace has no choice to make and must never see this"
	)

	second = subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Other", owner=world.user
	)
	session.flush()

	bare = world.call("GET", "/v1/meta").json()

	assert bare["statuses"] == {}, "the fixture is not the case this is about"

	said = bare["vocabulary_not_shown"]

	assert said is not None, f"empty sections with nothing saying why:\n{bare['workspaces']}"
	assert "workspace_id" in said, (
		f"the remedy has to be one this caller can act on:\n{said}"
	)
	assert world.workspace.slug in said or second.slug in said, (
		f"it has to name a workspace that can be asked for:\n{said}"
	)

	# **And it goes away when the question is answered**, or it is a permanent apology rather
	# than a statement about this request.
	narrowed = world.call("GET", f"/v1/meta?workspace_id={second.slug}").json()

	assert narrowed["statuses"]["task"]
	assert narrowed["vocabulary_not_shown"] is None

	# **And a credential reaching nothing says nothing**, which is a different fact and one
	# this sentence would misdescribe — there is no workspace to pick from. Asserted directly
	# because reaching it through the endpoint needs an account in no workspace, and the branch
	# it guards would otherwise index an empty list and answer 500.
	assert subroutine.api.meta._why_the_vocabulary_is_empty([]) is None


def test_both_channels_agree_about_when_a_vocabulary_is_withheld (
	session: sqlalchemy.orm.Session, world: test_api_tasks.World
) -> None:
	"""`SR#627`: one fact, two remedies, and the condition derived twice on purpose.

	``mcp.tools._unbound`` reads ``workspace`` and ``workspaces`` off the response, which works
	against **any** instance; the field is sent only by one new enough to have it. Making the
	resource read the field would make it stop withholding against an older server, so both
	derivations stay — and `SR#303`'s rule applies: the list was never the control, the guard is.

	The *sentences* differ and that is not drift. A resource takes no arguments, so its reader is
	told to bind the plugin's workspace setting; a caller here has the query parameter and is
	shown it. What must never differ is when either fires.
	"""

	bound = subroutine.views.Meta.model_validate(world.call("GET", "/v1/meta").json())

	assert bound.vocabulary_not_shown is None
	assert subroutine.mcp.tools._unbound(bound) == [], (
		"the resource would withhold a vocabulary this endpoint is publishing"
	)

	subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Other", owner=world.user
	)
	session.flush()

	unbound = subroutine.views.Meta.model_validate(world.call("GET", "/v1/meta").json())

	assert unbound.vocabulary_not_shown is not None
	assert subroutine.mcp.tools._unbound(unbound), (
		"this endpoint says it withheld a vocabulary and the resource would publish it empty"
	)


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


#: docs/design.md §13.3's budget, raised from 8 KB on 2026-07-30. The guide had reached 8,148 bytes
#: of 8,192, so the build was one sentence from red — and the obvious repair, editing this
#: number to get green, would have been a decision nobody took. Raised deliberately instead,
#: because there is more worth saying and the old figure was set when the guide said less.
#:
#: **Raising it again is a §13.3 change, not a test change.** The cap is not arbitrary: this
#: is the first thing an agent reads, and 15 KB is one cheap read where 60 KB is not.
GUIDE_BUDGET = 15 * 1024


def test_the_agent_guide_stays_small (world: test_api_tasks.World) -> None:
	"""docs/design.md §13.3 targets under 15 KB. Response size is a first-order cost for an agent."""

	guide = world.call("GET", "/v1/docs/agent").text

	assert len(guide.encode("utf-8")) < GUIDE_BUDGET, "the guide has grown past its budget"


def test_the_guide_never_calls_a_built_thing_unbuilt (
	world: test_api_tasks.World,
) -> None:
	"""`#355`. The guide named task claims among the unbuilt for a day after they shipped.

	**The direction nothing was watching.** §13.1's rule is that promising what an installation
	does not implement is worse than publishing less, and the guide's endpoint check enforces
	it — by comparing the paths the document *names* against routes that exist. That proves
	every path named is real. It is structurally blind to a real path named nowhere, which is
	the failure that actually happened, and the one whose cost falls entirely on the reader
	with no other source: an agent on MCP has the skill and a person has `--help`.

	So the unbuilt list is data, each entry carrying the path segment its endpoints would have.
	Building the thing is then what deletes the entry — which is the question `CLAUDE.md` says
	to ask of every allow-list in this repository, asked here.
	"""

	served = {
		f"{prefix}{route.path}"
		for prefix, router in subroutine.api.app.ROUTERS
		for route in router.routes
		if hasattr(route, "path")
	}

	assert subroutine.api.meta.UNBUILT, "the list is empty — has it stopped being read?"

	for name, fragment in subroutine.api.meta.UNBUILT:
		built = sorted(path for path in served if fragment in path)

		assert not built, (
			f"the guide calls {name!r} unbuilt, and this application serves {built}. "
			f"Delete its entry from meta.UNBUILT and say so in the guide."
		)

	# And the sentence the reader actually sees is built from that same tuple, so the two
	# cannot part company the way the prose and the code did.
	guide = world.call("GET", "/v1/docs/agent").text

	for name, _fragment in subroutine.api.meta.UNBUILT:
		assert name in guide

	assert "task claims" not in guide, "the one that was wrong, pinned"


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


def test_the_guide_names_every_transport_this_instance_answers (
	world: test_api_tasks.World,
) -> None:
	"""`#780`. The guard above only ever looked at ``/v1``, and MCP does not live there.

	``POST /mcp`` was served from `#516` and named by neither channel a reader is guaranteed
	— not the line ``subroutine serve`` prints, not this document. So the cheapest way into
	this product, an address and a token with nothing installed at the far end, could be
	found only by reading the source. Decision `#499` is the rule that was broken.

	**Derived rather than listed.** ``app.serving()`` reads what is mounted, so a transport
	added tomorrow fails this until the guide says it exists. The derivation lives in the
	test for the same reason `#678` put one there: ``meta.py`` writes the prose and must not
	have to import the application that mounts it.
	"""

	guide = world.call("GET", "/v1/docs/agent").text

	for surface in subroutine.api.app.serving():
		assert surface.path in guide, (
			f"this instance answers {surface.path} ({surface.what}) and the guide never "
			f"says so — an agent reading it has no other way to find out"
		)


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


def test_meta_says_which_release_is_serving_it (world: test_api_tasks.World) -> None:
	"""`#250`. The response every client fetches first should identify the build that sent it.

	`api_version` is the wire contract and has read `"1.0"` since M1 — it is published seven
	ways and moves for nothing, so comparing it can never report skew. `/v1/me` has carried the
	*release* since `#381`, but a client only calls that for `whoami`, while `identity()` calls
	this on every command. So this is where a client learns what it is talking to early enough
	for a later failure to be explained rather than merely reported.

	Named identically to the field on `Me`, so a client looks for one key rather than knowing
	which endpoint answered.
	"""

	body = world.call("GET", "/v1/meta").json()

	assert body["instance_version"] == subroutine.installations.program()
	assert body["api_version"] == subroutine.API_VERSION, "the contract number is unchanged"

	assert body["instance_version"] != body["api_version"], (
		"a release version that equals the contract version would be the wrong field, and "
		"comparing it could never report skew"
	)


def test_meta_refuses_a_query_parameter_it_does_not_accept (
	world: test_api_tasks.World,
) -> None:
	"""**`#615`, and the spelling in it is the one that cost something.**

	Every MCP tool argument is called ``workspace``; this parameter is ``workspace_id``. The
	mismatch was discarded in silence and answered ``200`` with empty vocabulary maps — which
	is what a fresh instance with no custom vocabulary looks like. An agent believed it,
	concluded there was no way to close an item as a duplicate, and deleted a task instead of
	cancelling it.

	The refusal is what turns that into a second call: it names what the endpoint *does*
	accept, so the caller corrects the spelling rather than the conclusion.
	"""

	answer = world.call("GET", "/v1/meta?workspace=projects")

	assert answer.status_code == 422
	assert answer.json()["code"] == "unknown_field"
	assert answer.json()["errors"][0]["field"] == "workspace"
	assert "workspace_id" in answer.json()["errors"][0]["hint"]


def test_meta_still_answers_the_spelling_it_does_accept (
	world: test_api_tasks.World,
) -> None:
	"""The floor under the test above, and it is not decoration.

	A refusal that fired on *everything* would satisfy the previous assertion perfectly while
	making the endpoint useless — and `#615` is a report about an endpoint answering
	confidently and wrongly, so a fix that answers nothing would be the same fault again.
	"""

	answer = world.call("GET", f"/v1/meta?workspace_id={world.workspace.slug}")

	assert answer.status_code == 200
	assert answer.json()["workspace"] == str(world.workspace.id)
	assert answer.json()["statuses"], "the vocabulary is what this endpoint is for"


def test_meta_with_no_workspace_named_is_unchanged (
	world: test_api_tasks.World,
) -> None:
	"""**The bare call still answers, and that is deliberate rather than overlooked.**

	`_sole` returns nothing when several workspaces are reachable and none was named, because
	a client's first call is often this one — before it knows what workspaces exist — so
	answering "which workspace?" to the request that would have told it is a loop.

	Pinned here because `#615` reported the empty maps and the discarded parameter together,
	and only the second was a defect. A fix that made the bare call refuse would have closed
	the report and broken the case the endpoint was designed around.
	"""

	answer = world.call("GET", "/v1/meta")

	assert answer.status_code == 200
	assert [row["slug"] for row in answer.json()["workspaces"]] == [world.workspace.slug]


def test_every_word_that_sets_a_date_reaches_both_a_person_and_an_agent (
	world: test_api_tasks.World,
) -> None:
	"""**`SR#838`. The same grammar was published twice, completely to one reader and not the
	other.**

	`/v1/meta`'s `capture` vocabulary carried `PLANNED_WORDS` and `DEADLINE_WORDS`; `explain
	capture` built its table from those *and* `DEFER_WORDS` and `BARE_PLANNED_WORDS`. So an
	agent was told `on`, `before`, `by` and `due`, and never met `from`, `defer`, `today` or
	`tomorrow` — four words of eight, on the surface whose reader has no other source.

	**The usual correction cannot fire on an omission**, which is what makes this worth a guard
	rather than two lines. An agent does not write `from monday`, get told the word is wrong,
	and learn: it has no reason to try, so it never discovers there is a way to say this. `#821`
	was the same shape one module along — `subroutine_link` published three of five seeded link
	types, and the two missing were the pair that join work to the conclusions about it.

	**Derived from the constants rather than listed here**, so a fifth kind of date word is
	covered on the day it is written. That is the whole request on `SR#838`: *what is missing is
	anything asserting the two agree*.
	"""

	grammars = world.call("GET", "/v1/meta").json()["grammars"]
	published = " ".join(grammars["capture"]["vocabulary"])

	topic = subroutine.cli.topics.find("capture")

	assert topic is not None, "there is no capture topic, so this compares one rendering"

	constants = {
		"PLANNED_WORDS": subroutine.domain.capture.PLANNED_WORDS,
		"DEADLINE_WORDS": subroutine.domain.capture.DEADLINE_WORDS,
		"DEFER_WORDS": subroutine.domain.capture.DEFER_WORDS,
		"BARE_PLANNED_WORDS": subroutine.domain.capture.BARE_PLANNED_WORDS,
	}

	missing: dict[str, list[str]] = {}

	for name, words in constants.items():
		assert words, f"{name} is empty, so naming it proves nothing"

		for where, rendering in (("/v1/meta", published), ("explain capture", topic.body)):
			absent = [word for word in words if not re.search(rf"\b{re.escape(word)}\b", rendering)]

			if absent:
				missing.setdefault(f"{where} ({name})", []).extend(absent)

	assert not missing, (
		f"the capture grammar is published to one reader and not the other: {missing}. Both "
		f"renderings derive from the same constants, so a word in neither list is a word that "
		f"reader will never write — and will never be corrected about, because they have no "
		f"reason to try it."
	)


#: A specification section as it is written anywhere — ``§6.3``, ``§8``, ``§7.3a``.
_SECTION = re.compile(r"§\s*\d+(\.\d+[a-z]?)*")

#: Everything this installation publishes as prose a caller reads, by the path it is served at.
#:
#: **Driven rather than listed** (`#1189`). The population that matters is *what a caller
#: receives*, and only asking for it can answer that — a list of module constants falls behind
#: the first grammar entry or worked example somebody adds, which is how the one this found
#: survived: `/v1/meta` published the capture grammar with ``§6.3`` in it, and every guard over
#: the source read it as an ordinary comment.
_PUBLISHED_PROSE = ("/v1/meta", "/v1/docs/agent", "/v1/docs/examples")


def test_nothing_published_to_a_caller_cites_the_specification (
	world: test_api_tasks.World,
) -> None:
	"""A reader of these has a base URL and a token, and no repository to look a section up in.

	`#944` settled that a tracked file may not point at something a reader of the source cannot
	reach, and did the item refs and the specification's own path. **This is the same rule one layer
	out**: a served instance is read by people who will never see the checkout, so ``§6.3`` on
	the wire names nothing at all. First contact reported it as "cites a section the reader
	cannot follow" (`#1183`).

	**No form of it, rather than a register of allowed ones.** ``docs/design.md §8.3`` is at
	least a findable artefact where a bare ``§8.3`` is not — but neither is worth spending a
	caller's attention on, because §13.1's rule already forces these documents to *state* what
	they mean rather than point at it. The one that was here said "for both of §6.3's axes"
	about two numbers it had already named.
	"""

	served = {path: world.call("GET", path).text for path in _PUBLISHED_PROSE}

	# **A floor, because a scan that read nothing satisfies every assertion below it.** Each of
	# these is a document of some size, and an empty or refused body would otherwise pass as
	# clean — which is the shape a guard fails in silently.
	for path, body in served.items():
		assert len(body) > 500, f"{path} answered with nothing to scan"

	offenders = [
		# **The match and what surrounds it, never the line.** ``/v1/meta`` is a single line of
		# JSON, so a line-oriented message truncates to the opening brace and shows the reader
		# everything except the thing that failed.
		(path, body[max(0, found.start() - 60) : found.end() + 60])
		for path, body in served.items()
		for found in [_SECTION.search(body)]
		if found is not None
	]

	assert not offenders, (
		"a document this instance serves cites a specification section, and its reader has no "
		"repository to resolve one in. State the rule instead of pointing at it.\n"
		+ "\n".join(f"  {path}: …{context}…" for path, context in offenders)
	)


def test_the_specification_scan_can_see_a_citation_that_got_through (
	world: test_api_tasks.World,
) -> None:
	"""Feed the guard a defect through its own entry point, rather than re-stating its rule.

	The pattern is the half that can rot — a scan matching nothing passes for the same reason a
	clean instance does, and this codebase has shipped a guard checking one command out of
	forty-eight for exactly that reason.
	"""

	assert _SECTION.search("for both of §6.3's axes")
	assert _SECTION.search("recovery property (§12.4)")
	assert _SECTION.search("the endpoint §8 reserved")

	# Not a section: a currency, and a bare marker with no number behind it.
	assert not _SECTION.search("costs §nothing")
