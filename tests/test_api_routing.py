"""Route ordering, and the words reserved so that ordering stays possible.

Two halves, and both are needed. The static check catches an unreachable route when the
application is built; the request-driven tests below prove the static check agrees with
what the framework actually does, because a matcher we wrote ourselves is only useful for
as long as that remains true (docs/design.md §8.1).
"""

import asyncio
import pathlib
import typing
import uuid

import fastapi
import fastapi.routing
import httpx
import pytest
import sqlalchemy
import sqlalchemy.orm
import starlette.requests
import starlette.responses

import api_support
import subroutine.addressing
import subroutine.api.app
import subroutine.api.middleware
import subroutine.api.query
import subroutine.api.routing
import subroutine.config
import subroutine.db.models.identity
import subroutine.db.models.work
import subroutine.db.session
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.projects
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


@pytest.fixture
def workspace (
	session: sqlalchemy.orm.Session,
) -> subroutine.db.models.identity.Workspace:
	"""Create a seeded workspace with an owner."""

	owner = subroutine.domain.users.create(
		session, username=f"founder-{uuid.uuid4().hex[:8]}"
	)

	return subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Test workspace", owner=owner
	)


def _router (*paths: str) -> fastapi.APIRouter:
	"""Build a router answering GET on each path in the order given."""

	router = fastapi.APIRouter()

	for path in paths:

		def handler (path: str = path) -> dict[str, str]:
			"""Report which route answered."""

			return {"route": path}

		router.add_api_route(path, handler, methods=["GET"], name=path)

	return router


def test_a_literal_route_after_a_parameterised_one_is_refused () -> None:
	"""Building the application fails, naming both routes and the method."""

	routers = (("/v1/tasks", _router("/{id_or_ref}", "/next")),)

	with pytest.raises(RuntimeError) as raised:
		subroutine.api.routing.check(routers)

	message = str(raised.value)

	assert "/v1/tasks/next" in message
	assert "/v1/tasks/{id_or_ref}" in message
	assert "GET" in message


def test_the_right_order_is_accepted () -> None:
	"""Literal first is exactly what the rule asks for, and passes silently."""

	subroutine.api.routing.check((("/v1/tasks", _router("/next", "/{id_or_ref}")),))


def test_ordering_is_only_checked_within_a_path_space () -> None:
	"""A parameterised task route cannot shadow a project route."""

	subroutine.api.routing.check(
		(
			("/v1/tasks", _router("/{id_or_ref}")),
			("/v1/projects", _router("/search")),
		)
	)


def test_a_parameterised_route_does_not_shadow_a_deeper_literal () -> None:
	"""``{id_or_ref}`` matches one segment, so ``/{id}/links`` is still reachable."""

	subroutine.api.routing.check((("/v1/tasks", _router("/{id_or_ref}", "/{id_or_ref}/links")),))


def test_shadowing_needs_an_overlapping_method () -> None:
	"""GET on one path cannot swallow POST on another."""

	router = fastapi.APIRouter()

	def read (id_or_ref: str) -> dict[str, str]:
		"""Read one."""

		return {}

	def search () -> dict[str, str]:
		"""Search."""

		return {}

	router.add_api_route("/{id_or_ref}", read, methods=["GET"], name="read")
	router.add_api_route("/search", search, methods=["POST"], name="search")

	subroutine.api.routing.check((("/v1/tasks", router),))


@pytest.mark.parametrize("word", sorted(subroutine.addressing.RESERVED_PATH_WORDS))
def test_each_reserved_word_reaches_its_literal_handler (
	session: sqlalchemy.orm.Session, word: str
) -> None:
	"""The reserved words resolve to their own endpoint, not to ``{id_or_ref}``.

	Driven through a real application rather than through the static check, because the
	claim being made is about the framework's behaviour. The routers here stand in for the
	task and project routers until S3-03 writes them; the assertion is the one that will
	then be made against those.
	"""

	application = api_support.build_app(api_support.factory_for(session))
	application.include_router(
		_router(f"/{word}", "/{id_or_ref}"), prefix="/v1/things"
	)

	response = api_support.call(application, "GET", f"/v1/things/{word}")

	assert response.status_code == 200
	assert response.json() == {"route": f"/{word}"}


def test_the_static_check_agrees_with_the_framework (
	session: sqlalchemy.orm.Session,
) -> None:
	"""What the check calls unreachable really is unreachable.

	This is the test that keeps the hand-written path matcher honest: it registers the bad
	order the check refuses, and confirms the framework does exactly what the check
	predicted it would.
	"""

	routers = (("/v1/things", _router("/{id_or_ref}", "/next")),)

	assert subroutine.api.routing.shadowed(subroutine.api.routing.declarations(routers))

	application = api_support.build_app(api_support.factory_for(session))

	for prefix, router in routers:
		application.include_router(router, prefix=prefix)

	response = api_support.call(application, "GET", "/v1/things/next")

	assert response.json() == {"route": "/{id_or_ref}"}, "the literal route is swallowed"


def test_the_real_application_is_checked () -> None:
	"""Whatever routers the application declares, they are in a workable order."""

	subroutine.api.routing.check(subroutine.api.app.ROUTERS)


def test_a_router_holding_another_router_is_refused () -> None:
	"""Nesting is outside what the check can read, and it says so rather than skipping.

	FastAPI keeps an included router as an opaque object whose paths are composed when a
	request arrives. Walking into that would be a check written against a private shape, so
	the honest answer is to refuse the arrangement — silently ignoring it would leave the
	nested routes unchecked while the check reported success.
	"""

	outer = fastapi.APIRouter()
	outer.include_router(_router("/next"), prefix="/inner")

	with pytest.raises(RuntimeError, match="not checked"):
		subroutine.api.routing.check((("/v1", outer),))


@pytest.mark.parametrize("word", sorted(subroutine.addressing.RESERVED_PATH_WORDS))
def test_a_project_cannot_be_keyed_with_a_reserved_word (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	word: str,
) -> None:
	"""A key that would share an address with an endpoint is refused at creation.

	Refused in the service layer, so the CLI cannot create one either — the API is not the
	only way in, and a project that exists, is listed and cannot be opened would be the
	same defect whichever door made it.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		subroutine.domain.projects.create(
			session, workspace_id=workspace.id, key=word, title="Reserved"
		)

	assert raised.value.errors[0].field == "key"
	assert "reserved" in raised.value.errors[0].message


def test_an_ordinary_key_is_unaffected (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
) -> None:
	"""The reserved list is short and nothing else is touched by it."""

	project = subroutine.domain.projects.create(
		session, workspace_id=workspace.id, key="searchable", title="Fine"
	)

	assert project.key == "searchable"


def test_reserved_words_are_matched_case_insensitively () -> None:
	"""Identifiers resolve case-insensitively in a path, so ``SEARCH`` collides too."""

	assert subroutine.addressing.is_reserved_word("SEARCH")
	assert subroutine.addressing.is_reserved_word(" Next ")
	assert not subroutine.addressing.is_reserved_word("searches")


def _root_segments () -> set[str]:
	"""Return the first path segment of every route the real application registers.

	Read through :func:`subroutine.api.routing.mounted` rather than from a built application,
	for the reason that function documents: an included router is opaque, and walking one
	finds a fraction of what is really declared.
	"""

	found: set[str] = set()

	for path, _verbs, _route in subroutine.api.routing.mounted(subroutine.api.app.ROUTERS):
		first = path.strip("/").split("/")[0]

		if first:
			found.add(first)

	return found


def test_no_route_takes_the_whole_first_segment () -> None:
	"""Nothing at the root is a parameter, which is what leaves the first segment spendable.

	A ``/{anything}`` registered at the root would match every workspace address there is,
	and no list of reserved words could recover it — which is why the browser app answers
	``/{workspace}/{project}`` from a 404 fallback rather than from a route (item ``#648``).
	The test below is only meaningful for as long as this one passes, so it is stated
	separately instead of being folded in.
	"""

	parameterised = sorted(word for word in _root_segments() if word.startswith("{"))

	assert parameterised == [], "a parameterised root route would shadow every workspace"


def test_every_root_path_is_reserved_against_a_workspace_slug () -> None:
	"""A workspace cannot be named after an address this application already answers.

	Derived here rather than restated, because a route added later is precisely how the
	hand-written list in ``addressing`` would fall behind — and it already had. When item
	``#678`` was found the two sets were *disjoint*: all six of these could be created, and
	``mcp`` then answered a protocol where its own page should have been.

	Equality rather than a subset in either direction, so this fails on both kinds of drift —
	a new root path nobody reserved, and a word reserved here for a route that has since gone
	away. The words a *person* would misread are the other list and are deliberately not in
	this comparison; no route claims them and none should have to.
	"""

	assert _root_segments() == subroutine.addressing.ROUTED_WORKSPACE_WORDS


@pytest.mark.parametrize("word", sorted(_root_segments()))
def test_a_workspace_cannot_be_named_after_a_root_path (
	session: sqlalchemy.orm.Session, word: str
) -> None:
	"""Refused where it is created, so the CLI cannot make one either.

	**Parametrised over the routes rather than over the reserved list**, which is the whole
	point of the arrangement: emptying that list would generate no cases at all, and a
	parametrisation cannot tell "nothing failed" from "nothing ran". Driven from what the
	application really answers, it fails six times instead — and a root path added later is
	covered the moment the route exists rather than when somebody remembers a case.
	"""

	with pytest.raises(subroutine.errors.ValidationError) as raised:
		subroutine.domain.workspaces.validated_slug(session, word)

	assert raised.value.errors[0].field == "slug"
	assert "reserved" in raised.value.errors[0].message


def test_a_title_that_derives_a_claimed_slug_falls_back () -> None:
	"""``init --workspace "MCP"`` sets an instance up; it does not refuse to.

	A derived slug is shaped rather than validated, so the reserved words have to be a
	*condition* on it rather than a refusal — otherwise widening the list, as ``#678`` does,
	turns somebody's perfectly reasonable workspace title into a failure to create the
	database at all.

	**The fallback obeys the same rule since ``#690``**, and this docstring used to say it did
	not. A username that was *also* reserved came straight back out and was refused by
	``workspaces.create``, so ``init --username app --workspace MCP`` created nothing at all —
	widened into reach by ``#678``, because ``app`` is an ordinary service-account name where
	``me`` and ``self`` were not.

	Reaching past the underscore is deliberate. The public way in is ``init``, which builds a
	database and an entire instance to answer a question about one string.
	"""

	assert subroutine.domain.bootstrap._derived_slug("MCP", "si") == "si"
	assert subroutine.domain.bootstrap._derived_slug("Acme Ltd", "si") == "acme-ltd"

	# Both reserved: the title's slug and the username's. There is still an answer.
	assert (
		subroutine.domain.bootstrap._derived_slug("MCP", "app")
		== subroutine.domain.bootstrap.LAST_RESORT_SLUG
	)


def test_the_last_resort_slug_is_one_a_workspace_can_actually_have () -> None:
	"""The end of the chain, checked against the list that could move under it — ``#690``.

	``_derived_slug`` tries the title, then the username, then a constant. The constant is the
	only one that cannot be re-derived from somebody's input, so it is the only one that could
	quietly become illegal — and it would do so exactly the way this defect arrived in the
	first place: by somebody widening the reserved words for an unrelated reason.

	Asserted through ``_usable`` rather than by re-stating the rule, so the two cannot disagree.
	"""

	assert subroutine.domain.bootstrap._usable(
		subroutine.domain.bootstrap.LAST_RESORT_SLUG
	), "the last resort is itself refused, so a doubly-reserved name has no answer"


def test_every_mounted_route_commits_before_it_answers () -> None:
	"""The instrument on §8.1's transaction boundary, and it exists because of a real defect.

	FastAPI closes a request's dependency exit stack **after** the application has emitted
	the response. Measured rather than assumed: a probe recording the order printed
	``handler body`` → ``response left the app`` → ``dependency exit``. So a session
	committed inside a ``yield`` dependency — which is where this one lived until
	2026-07-30 — commits after the caller already holds its ``200``.

	Two things follow, and the second is why this is a build-failing check rather than a
	note. A client that writes and immediately reads can beat its own commit, which is how
	it was found. And **a commit that failed would fail after the caller had been told it
	succeeded** — a ``201`` for something that never happened, invisible to any client.

	``routing.Transactional`` commits between the handler returning and the response being
	sent. A router registered without it would silently go back to the old behaviour on
	every one of its endpoints, so the check is over what is actually mounted.
	"""

	loose = [
		f"{route.methods and sorted(route.methods)} {route.path}"
		for _prefix, router in subroutine.api.app.ROUTERS
		for route in router.routes
		if isinstance(route, fastapi.routing.APIRoute)
		and not isinstance(route, subroutine.api.routing.Transactional)
	]

	assert not loose, (
		"These routes would commit after their response was sent: "
		+ ", ".join(loose)
		+ ". Give the router route_class=subroutine.api.routing.Transactional."
	)


def test_a_write_is_committed_before_its_response_is_sent (
	tmp_path: pathlib.Path,
) -> None:
	"""The property the route class exists for, tested through the real ASGI stack.

	A middleware sits outside the route and looks at the database on a **separate
	connection** the moment the handler's response passes it. If the commit happened at
	dependency teardown — after the response — the row would not be there yet, which is
	exactly the race that lost an event on 2026-07-30.

	Its own engine on a temporary file rather than the shared test session: the point is
	what a *second* connection can see, and a fixture that hands both sides one transaction
	can only ever answer yes.
	"""

	database = tmp_path / "commit-order.db"
	engine = sqlalchemy.create_engine(f"sqlite:///{database}")

	try:
		subroutine.db.session.create_all(engine)

		factory = subroutine.db.session.create_session_factory(engine)

		with factory() as setting_up:
			founder = subroutine.domain.bootstrap.initialise(
				setting_up, username="si", instance_name="Test"
			)
			_row, issued = subroutine.domain.authentication.issue_token(
				setting_up, user=founder.user, title="probe"
			)
			secret = issued.value.get_secret_value()
			setting_up.commit()

		application = subroutine.api.app.create_app(
			settings=subroutine.config.Settings(dev_mode=True), session_factory=factory
		)
		seen: list[int] = []

		@application.middleware("http")
		async def count_outside (request: typing.Any, call_next: typing.Any) -> typing.Any:
			"""Count the tasks a *different* connection can see as the response goes past."""

			answer = await call_next(request)

			with factory() as looking:
				seen.append(
					looking.scalar(
						sqlalchemy.select(
							sqlalchemy.func.count(subroutine.db.models.work.Task.id)
						)
					)
					or 0
				)

			return answer

		async def drive () -> None:
			"""Create one task through the real application."""

			transport = httpx.ASGITransport(app=application)

			async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
				answered = await client.post(
					"/v1/tasks",
					json={"title": "Committed before you were told"},
					headers={"Authorization": f"Bearer {secret}"},
				)

				assert answered.status_code == 201, answered.text

		asyncio.run(drive())

		assert seen, "the middleware never ran"
		assert seen[-1] == 1, (
			"the task was not visible to another connection when its 201 went out, so the "
			"commit is happening after the response again"
		)

	finally:
		engine.dispose()


#: Something to put where a route names a path segment. Any value will do — the refusal under
#: test runs before the segment is resolved, which is itself part of what is being asserted:
#: a caller who misspells a parameter is told so rather than being sent to find a task first.
_SEGMENTS = {
	"id_or_ref": "1",
	"id_or_key": "inbox",
	"id_or_slug": "test",
	"id_or_prefix": "sr_nothing",
	"comment_id": "00000000-0000-0000-0000-000000000000",
	"link_id": "00000000-0000-0000-0000-000000000000",
	"username": "nobody",
	"name": "app.js",
}


def _addressable () -> list[tuple[str, str, str]]:
	"""Return every mounted route as ``(name, method, a path that can actually be requested)``."""

	found: list[tuple[str, str, str]] = []

	for path, methods, _route in subroutine.api.routing.mounted(subroutine.api.app.ROUTERS):
		address = path

		for segment, stand_in in _SEGMENTS.items():
			address = address.replace("{" + segment + "}", stand_in)

		if "{" in address:
			raise AssertionError(
				f"{path} names a path segment this test has no value for. Add one to "
				f"_SEGMENTS — leaving it out would silently skip the route."
			)

		for method in sorted(methods):
			found.append((f"{method} {path}", method, address))

	return found


def test_every_route_refuses_a_query_parameter_it_does_not_declare (
	world: test_api_tasks.World,
) -> None:
	"""`#898`, Simon's decision, and `#897` closes with it.

	**Driven rather than read.** ``include_router`` copies a route as it mounts it, so the
	dependency added there is not on the object ``routing.mounted`` returns — a structural
	check would look at the routers, find nothing, and report every route unguarded or (with
	the comparison the other way up) every route fine. The only honest question is what a
	request gets, so this asks for one.

	**Asserting the code, not the status.** A `422` here could equally be a missing body on a
	`POST`, which would make this pass while proving nothing; ``unknown_field`` can only come
	from the refusal under test. That also settles the ordering the refusal depends on: it
	answers before the body is validated, before a workspace is resolved and before a
	credential is checked, so every route can be driven with one junk parameter and nothing
	else.

	Writes are driven too, deliberately — they are the half nobody thought about while this
	was declared by hand, and if the refusal ever stops firing on a `DELETE` this test starts
	deleting things, which is a failure nobody will miss.
	"""

	routes = _addressable()

	assert len(routes) >= 60, (
		f"only {len(routes)} routes were found, so this is reading almost nothing"
	)

	unrefused = []

	for name, method, address in routes:
		if name in subroutine.api.query.NOT_REFUSED:
			continue

		joiner = "&" if "?" in address else "?"
		answered = world.call(method, f"{address}{joiner}nonsense=1")

		if answered.status_code != 422 or answered.json().get("code") != "unknown_field":
			unrefused.append(f"{name} answered {answered.status_code}")

	assert not unrefused, (
		"These routes ignored a query parameter they do not declare: "
		+ ", ".join(sorted(unrefused))
		+ ". Every route is refused one by default since `#898` — add the route to "
		+ "api/query.NOT_REFUSED with a reason if it must answer whatever it is asked."
	)


def _by_path () -> dict[str, set[str]]:
	"""Return every method this application declares, gathered under the path that takes it."""

	found: dict[str, set[str]] = {}

	for path, methods, _route in subroutine.api.routing.mounted(subroutine.api.app.ROUTERS):
		found.setdefault(path, set()).update(methods)

	return found


def _stood_in (path: str) -> str:
	"""Return a path template with something requestable in place of each segment."""

	address = path

	for segment, stand_in in _SEGMENTS.items():
		address = address.replace("{" + segment + "}", stand_in)

	assert "{" not in address, f"{path} names a segment _SEGMENTS has no value for"

	return address


def test_a_refusal_names_every_method_the_path_accepts (world: test_api_tasks.World) -> None:
	"""RFC 9110 wants a 405 to say what would have worked, and this said one of them.

	Starlette answers out of the *first* route whose path matched and FastAPI registers one
	route per method, so ``PUT /v1/tasks`` was told ``Allow: POST`` and "This path accepts
	POST" — with the ``GET`` registered beside it unmentioned. Twenty paths here take more
	than one method, and a caller mapping the surface from its refusals is exactly who reads
	this header.

	Driven with ``PUT``, which this application accepts nowhere, so what comes back is the
	refusal being asked about rather than a body that would not parse. That assumption is
	asserted rather than assumed, because the day something accepts ``PUT`` is the day this
	test starts writing.
	"""

	declared = _by_path()
	shared = {path: methods for path, methods in declared.items() if len(methods) > 1}

	assert len(shared) >= 15, f"only {len(shared)} paths take more than one method"
	assert not any("PUT" in methods for methods in declared.values()), (
		"PUT is accepted somewhere now, so it is no longer a method that only ever refuses"
	)

	quiet = []

	for path, methods in sorted(shared.items()):
		answered = world.call("PUT", _stood_in(path))
		allowed = answered.headers.get("Allow", "")

		if answered.status_code != 405 or any(method not in allowed for method in methods):
			quiet.append(f"{path} answered {answered.status_code} with Allow: {allowed!r}")

	assert not quiet, (
		"These paths accept more methods than their refusal named: " + ", ".join(quiet)
	)


def test_head_answers_wherever_get_does (world: test_api_tasks.World) -> None:
	"""FastAPI's ``APIRoute`` does not pair ``HEAD`` with ``GET`` and Starlette's own does.

	So ``/v1/openapi.json``, which FastAPI registers itself, answered ``HEAD`` and everything
	this application declares answered 405 — including ``/healthz``, where the caller is a
	load balancer whose default check is ``HEAD``, reporting an instance that is serving
	perfectly well as down.

	Compared against ``GET`` at the same address rather than against 200, because most of
	these are driven with a stand-in segment and answer 404 or 422. What is being asserted is
	that the two agree.
	"""

	addresses = sorted(path for path, methods in _by_path().items() if "GET" in methods)

	assert len(addresses) >= 25, f"only {len(addresses)} paths answer GET, so this reads little"

	disagreed = []

	for path in addresses:
		address = _stood_in(path)
		by_get = world.call("GET", address)
		by_head = world.call("HEAD", address)

		if by_head.status_code != by_get.status_code:
			disagreed.append(f"{path}: GET {by_get.status_code}, HEAD {by_head.status_code}")

	assert not disagreed, (
		"These paths answer GET and refuse HEAD: " + ", ".join(disagreed)
	)


def test_head_where_nothing_answers_get_is_refused_as_head (
	world: test_api_tasks.World,
) -> None:
	"""The other half: a write-only path refuses, and refuses the method that was sent.

	``HEAD`` is answered by rewriting it to ``GET`` before routing, which is what keeps the
	published contract from growing a ``head`` operation on every path — so the rewrite is
	done only where a ``GET`` really exists, rather than turning every ``HEAD`` into a
	request nobody made.

	**Only what a caller can read is asserted here**, which is the status and the header: a
	response to ``HEAD`` carries no body, so the detail naming the method is unreachable over
	HTTP by construction. That the request is left as ``HEAD`` rather than rewritten to a
	``GET`` nobody sent is a decision nothing outside can observe, so it is driven directly
	in :func:`test_a_head_where_no_get_exists_reaches_the_route_unchanged`.
	"""

	answered = world.call("HEAD", "/v1/tasks/1/claim")

	assert answered.status_code == 405
	assert answered.headers.get("Allow") == "POST"


def _asked (application: fastapi.FastAPI, method: str, path: str) -> str:
	"""Run the HEAD middleware over one request and return the method routing would see."""

	seen: list[str] = []

	async def onwards (request: starlette.requests.Request) -> starlette.responses.Response:
		"""Stand in for the rest of the application, recording what it was asked."""

		seen.append(str(request.scope["method"]))

		return starlette.responses.Response(status_code=204)

	request = starlette.requests.Request(
		{
			"type": "http",
			"method": method,
			"path": path,
			"raw_path": path.encode(),
			"query_string": b"",
			"headers": [],
			"scheme": "http",
			"server": ("testserver", 80),
			"app": application,
		}
	)

	asyncio.run(subroutine.api.middleware.answer_head_with_get(request, onwards))

	return seen[0]


def test_a_head_where_a_get_exists_reaches_the_route_as_a_get (
	world: test_api_tasks.World,
) -> None:
	"""Which is what makes the ``GET`` handler answer at all — FastAPI pairs neither method."""

	assert _asked(world.application, "HEAD", "/v1/tasks") == "GET"
	assert _asked(world.application, "GET", "/v1/tasks") == "GET", "and nothing else moves"


def test_a_head_where_no_get_exists_reaches_the_route_unchanged (
	world: test_api_tasks.World,
) -> None:
	"""A request nobody made is not a better answer than a refusal.

	Rewriting every ``HEAD`` would be shorter and would produce the same 405 here, so nothing
	a caller reads can tell the two apart — the difference is what this application recorded
	about the request, which is what reaches its own log. Driven directly for that reason:
	a decision no response can show is still a decision, and one nothing exercises is one
	somebody deletes as dead weight.
	"""

	assert _asked(world.application, "HEAD", "/v1/tasks/1/claim") == "HEAD"


def test_an_excused_route_still_answers (world: test_api_tasks.World) -> None:
	"""The other half, and without it the list above could be satisfied by breaking everything.

	An entry in ``NOT_REFUSED`` is a promise that the route goes on working when something
	appends a parameter to it — a monitor's cache-buster, a mail client's rewrite. A refusal
	that fired anyway would be invisible here, because this file's other test only ever asks
	whether routes it does *not* excuse are refused.
	"""

	for name in subroutine.api.query.NOT_REFUSED:
		method, _, path = name.partition(" ")

		for segment, stand_in in _SEGMENTS.items():
			path = path.replace("{" + segment + "}", stand_in)

		answered = world.call(method, f"{path}?utm_source=nothing")

		assert answered.status_code != 422 or answered.json().get("code") != "unknown_field", (
			f"{name} is excused from the query refusal and was refused anyway"
		)


def test_the_excused_list_names_only_routes_that_exist () -> None:
	"""An exemption for a route that has been renamed is an exemption nobody notices.

	`PUBLIC_ROUTES` next door has had this since it existed. The same question asked of the
	same shape of list, because the failure is the same: an entry that no longer matches
	anything reads as a considered decision and grants nothing.
	"""

	registered = {name for name, _method, _address in _addressable()}
	gone = set(subroutine.api.query.NOT_REFUSED) - registered

	assert not gone, (
		f"api/query.NOT_REFUSED names routes that no longer exist: {', '.join(sorted(gone))}."
	)


def test_no_route_that_shapes_its_answer_is_excused () -> None:
	"""`#676`'s measurement, kept as the one thing an exemption may never cover.

	A route declaring ``fields`` or ``format`` is one where an ignored parameter costs the
	caller the whole object — measured at 59 bytes against 99,746 on ``/v1/documents/4``. So
	whatever else `NOT_REFUSED` is allowed to hold, it may not hold one of those, and this is
	what stops the list being widened until it is the default again by another name.
	"""

	shaping: set[str] = set()

	for path, methods, route in subroutine.api.routing.mounted(subroutine.api.app.ROUTERS):
		dependant = getattr(route, "dependant", None)

		declared = {
			alias
			for field in (getattr(dependant, "query_params", None) or [])
			if (alias := getattr(field, "alias", None)) is not None
		}

		if declared & {"fields", "format"}:
			shaping.update(f"{method} {path}" for method in sorted(methods))

	assert len(shaping) >= 15, f"only {len(shaping)} shaping routes found; the walk read nothing"

	excused = shaping & set(subroutine.api.query.NOT_REFUSED)

	assert not excused, (
		f"These routes shape their answer and are excused from the refusal, so a misspelled "
		f"'fields' returns the whole object silently: {', '.join(sorted(excused))}."
	)
