"""Route ordering, and the words reserved so that ordering stays possible.

Two halves, and both are needed. The static check catches an unreachable route when the
application is built; the request-driven tests below prove the static check agrees with
what the framework actually does, because a matcher we wrote ourselves is only useful for
as long as that remains true (SPEC.md §8.1).
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

import api_support
import subroutine.addressing
import subroutine.api.app
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
