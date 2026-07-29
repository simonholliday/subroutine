"""The application skeleton: its headers, its health checks, and its error envelope.

Nothing here is about a feature. It is about the promises every response makes whatever
the request was — that it carries a correlation id, that it says which API version
answered, and that a failure looks the same whether a service refused it, the router never
found it, or something broke.
"""

import pathlib
import typing

import fastapi
import pytest
import sqlalchemy.engine
import sqlalchemy.orm

import api_support
import subroutine
import subroutine.api.app
import subroutine.api.middleware
import subroutine.config
import subroutine.db.migrate
import subroutine.db.session


@pytest.fixture
def application (session: sqlalchemy.orm.Session) -> fastapi.FastAPI:
	"""Build an application sharing the test's transaction."""

	return api_support.build_app(api_support.factory_for(session))


def test_liveness_answers_without_touching_the_database (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``/healthz`` must not depend on storage.

	A liveness probe that fails when the database does gets the container killed, which
	does not bring the database back. This asserts the endpoint answers even when the
	session factory is one that could not possibly work.
	"""

	def unusable () -> typing.NoReturn:
		raise AssertionError("liveness must not open a session")

	application = api_support.build_app(typing.cast(typing.Any, unusable))
	response = api_support.call(application, "GET", "/healthz")

	assert response.status_code == 200
	assert response.json() == {"status": "ok", "api_version": subroutine.API_VERSION}


def test_readiness_reports_the_schema_revision (application: fastapi.FastAPI) -> None:
	"""A migrated database is ready, and says which revision it is at."""

	response = api_support.call(application, "GET", "/readyz")

	assert response.status_code == 200

	body = response.json()

	assert body["status"] == "ready"
	assert body["schema_revision"] == subroutine.db.migrate.head_revision()


def test_readiness_refuses_an_unmigrated_database (tmp_path: pathlib.Path) -> None:
	"""An empty database is running but not ready, and the response says what to do.

	This is the failure the check exists for: an instance that starts happily and then
	answers queries against columns that are not there. It should be held out of the load
	balancer, not left to fail one request at a time.
	"""

	engine = subroutine.db.session.create_engine(f"sqlite:///{tmp_path / 'empty.db'}")

	try:
		application = api_support.build_app(
			subroutine.db.session.create_session_factory(engine)
		)
		response = api_support.call(application, "GET", "/readyz")

	finally:
		engine.dispose()

	assert response.status_code == 503

	body = response.json()

	assert body["code"] == "service_unavailable"
	assert "subroutine db upgrade" in body["hint"]


def test_readiness_reports_an_unreachable_database (tmp_path: pathlib.Path) -> None:
	"""A database that cannot be opened is reported as such, with the reason."""

	unreachable = tmp_path / "no-such-directory" / "subroutine.db"
	engine = subroutine.db.session.create_engine(f"sqlite:///{unreachable}")

	try:
		application = api_support.build_app(
			subroutine.db.session.create_session_factory(engine)
		)
		response = api_support.call(application, "GET", "/readyz")

	finally:
		engine.dispose()

	assert response.status_code == 503
	assert response.json()["code"] == "service_unavailable"
	assert "database_url" in response.json()["hint"]


def test_every_response_carries_the_correlation_and_version_headers (
	application: fastapi.FastAPI,
) -> None:
	"""Both headers appear on a success and on a failure alike."""

	for path, expected in (("/healthz", 200), ("/nothing-here", 404)):
		response = api_support.call(application, "GET", path)

		assert response.status_code == expected
		assert response.headers[subroutine.api.middleware.API_VERSION_HEADER] == (
			subroutine.API_VERSION
		)
		assert response.headers[subroutine.api.middleware.REQUEST_ID_HEADER]


def test_a_supplied_request_id_is_echoed_back (application: fastapi.FastAPI) -> None:
	"""A caller's own correlation id survives the round trip."""

	response = api_support.call(
		application,
		"GET",
		"/healthz",
		headers={subroutine.api.middleware.REQUEST_ID_HEADER: "trace-42.abc"},
	)

	assert response.headers[subroutine.api.middleware.REQUEST_ID_HEADER] == "trace-42.abc"


@pytest.mark.parametrize(
	"supplied",
	[
		"",
		"x" * 200,
		"has spaces",
		"semi;colon",
	],
)
def test_an_implausible_request_id_is_replaced_rather_than_echoed (
	application: fastapi.FastAPI, supplied: str
) -> None:
	"""An id that is not one is quietly replaced.

	The value is reflected into a response header, so accepting anything at all would make
	this endpoint a free amplifier and, for a value containing a newline, a response
	splitting attempt. The caller asked for a health check, not a debate about its header,
	so it is replaced rather than refused.
	"""

	response = api_support.call(
		application,
		"GET",
		"/healthz",
		headers={subroutine.api.middleware.REQUEST_ID_HEADER: supplied},
	)

	assigned = response.headers[subroutine.api.middleware.REQUEST_ID_HEADER]

	assert assigned != supplied
	assert len(assigned) == 36, "a UUID was generated instead"


def test_an_unknown_path_is_a_problem_document (application: fastapi.FastAPI) -> None:
	"""A 404 arrives in the same envelope as every other failure.

	FastAPI's own 404 is ``{"detail": "Not Found"}``, which is a second, undocumented error
	format for a client to learn.
	"""

	response = api_support.call(application, "GET", "/v1/nothing")

	assert response.status_code == 404
	assert response.headers["content-type"].startswith("application/problem+json")

	body = response.json()

	assert body["code"] == "not_found"
	assert body["status"] == 404
	assert body["instance"] == "/v1/nothing"
	assert body["request_id"] == response.headers[subroutine.api.middleware.REQUEST_ID_HEADER]
	assert "/v1/nothing" in body["detail"]


def test_a_wrong_method_names_the_ones_that_work (application: fastapi.FastAPI) -> None:
	"""A 405 says which methods the path does accept."""

	response = api_support.call(application, "POST", "/healthz")

	assert response.status_code == 405

	body = response.json()

	assert body["code"] == "method_not_allowed"
	assert "GET" in body["hint"]


def test_a_bug_becomes_a_500_that_can_be_looked_up (application: fastapi.FastAPI) -> None:
	"""An unhandled exception is reported vaguely, and tied to the log by its id."""

	@application.get("/boom")
	def boom () -> dict[str, str]:
		"""Fail the way a bug fails."""

		raise RuntimeError("a detail the caller must not be told")

	response = api_support.call(application, "GET", "/boom")

	assert response.status_code == 500

	body = response.json()

	assert body["code"] == "internal_error"
	assert "a detail the caller must not be told" not in response.text
	assert body["request_id"] in body["hint"]
	assert response.headers[subroutine.api.middleware.REQUEST_ID_HEADER] == body["request_id"]


def test_the_openapi_document_is_served_under_the_version_prefix (
	application: fastapi.FastAPI,
) -> None:
	"""``/v1/openapi.json`` is where SPEC.md §8.6 says it is."""

	response = api_support.call(application, "GET", "/v1/openapi.json")

	assert response.status_code == 200
	assert response.json()["info"]["version"] == subroutine.API_VERSION


def test_an_application_owns_and_disposes_the_engine_it_built (
	tmp_path: pathlib.Path,
) -> None:
	"""With no factory supplied, the application builds one and cleans it up.

	The other direction — an injected factory — is what every other test here relies on,
	and disposing *that* engine would break the fixture that owns it.
	"""

	settings = subroutine.config.Settings(
		database_url=f"sqlite:///{tmp_path / 'own.db'}", dev_mode=True
	)
	application = subroutine.api.app.create_app(settings=settings)
	engine = application.state.engine

	assert isinstance(engine, sqlalchemy.engine.Engine)

	pool = engine.pool
	response = api_support.call(application, "GET", "/healthz", lifespan=True)

	assert response.status_code == 200

	# `dispose()` closes the pool's connections and puts a fresh pool in its place, so a
	# replaced pool is the observable that shutdown actually ran.
	assert engine.pool is not pool


def test_an_injected_factory_leaves_the_application_without_an_engine (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Nothing is built, so nothing is disposed."""

	application = api_support.build_app(api_support.factory_for(session))

	assert application.state.engine is None
