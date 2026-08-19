"""The application skeleton: its headers, its health checks, and its error envelope.

Nothing here is about a feature. It is about the promises every response makes whatever
the request was — that it carries a correlation id, that it says which API version
answered, and that a failure looks the same whether a service refused it, the router never
found it, or something broke.
"""

import json
import pathlib
import typing

import fastapi
import pytest
import sqlalchemy.engine
import sqlalchemy.orm

import api_support
import subroutine
import subroutine.api.app
import subroutine.api.mcp
import subroutine.api.middleware
import subroutine.api.routing
import subroutine.config
import subroutine.db.migrate
import subroutine.db.models.system
import subroutine.db.session
import subroutine.db.types
import subroutine.domain.instances


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
	# `subroutine init`, not `db upgrade`: this database has no schema at all, and telling
	# somebody to migrate an empty database is advice that does nothing (`#175`). The same
	# three-way decision the CLI makes, from the same function, so the two cannot drift.
	assert "subroutine init" in body["hint"]


def _instance (session: sqlalchemy.orm.Session) -> subroutine.db.models.system.Instance:
	"""Give this database the single ``instance`` row a real installation has."""

	established, _made = subroutine.domain.instances.establish(session, name="Probe")

	session.flush()

	return established


def test_readiness_notices_the_database_underneath_it_has_been_replaced (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#179`. **A reachable connection is not readiness, and this said it was.**

	A serving process whose database file has been replaced keeps its descriptors on the
	unlinked file, so its reads succeed against data nobody else can see and every probe
	answers 200 — this one included. The clean-room sysadmin who found it used ``/readyz`` to
	confirm a restore had worked, and it told them yes.

	`#171` closed the route that produced it, so what is left is everything out of band: an
	operator with ``cp``, a volume remount, a restore run with ``--force``. The claim was wrong
	however it was reached.

	**The identity is what answers the operator's question.** *Am I serving the data I think I
	am* is a question about which instance this is, not about whether a socket works — and the
	instance id is the thing agents and configuration already refer to.
	"""

	established = _instance(session)
	application = api_support.build_app(api_support.factory_for(session))

	# The first reading latches, because the database may not be up when the process starts —
	# which is the whole reason this endpoint exists.
	assert api_support.call(application, "GET", "/readyz").status_code == 200

	# What a replaced file looks like from inside the process: same connection, same schema,
	# different instance. Nothing about the transport has changed.
	established.id = subroutine.db.types.new_uuid()

	session.flush()

	response = api_support.call(application, "GET", "/readyz")

	assert response.status_code == 503

	body = response.json()

	assert body["code"] == "service_unavailable"
	assert str(established.id) in body["detail"], "the detail must say which one it is serving"
	assert "Restart" in body["hint"]


def test_readiness_says_nothing_while_the_instance_is_the_one_it_started_on (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The other half, and without it the check above passes on an endpoint that always refuses.

	A ``db restore --recover`` keeps the identity deliberately (§12.6a), so the ordinary case —
	a process serving the database it has always served — must go on answering ready however
	many times it is asked.
	"""

	_instance(session)

	application = api_support.build_app(api_support.factory_for(session))

	for _ in range(3):
		assert api_support.call(application, "GET", "/readyz").status_code == 200


def test_readiness_latches_nothing_until_there_is_an_instance_to_latch (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A process started before ``subroutine init`` must not be pinned to the absence.

	Latching ``None`` and comparing against it would make the first real instance look like a
	replacement — the check firing on the one moment it is most likely to be met, which is
	somebody setting the thing up.
	"""

	application = api_support.build_app(api_support.factory_for(session))

	assert api_support.call(application, "GET", "/readyz").status_code == 200

	_instance(session)

	assert api_support.call(application, "GET", "/readyz").status_code == 200


#: What SQLite says when the directory above the database does not exist. Pinned as a constant
#: because two tests need the same string to mean opposite things — one asserts it arrives and
#: the other that it does not, and a typo in either would quietly make that test vacuous.
DRIVER_SAYS = "unable to open database file"


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

	# **The detail, not only the hint** (`#832`). This test asserted the code and the hint and
	# never looked at what the detail contained, which is the half that discloses — so it was
	# green throughout the period the endpoint was handing the driver's error to anybody.
	assert DRIVER_SAYS in response.json()["detail"], (
		"a private instance should still report the driver's own cause"
	)


def test_readiness_keeps_the_cause_to_itself_once_the_instance_is_public (
	tmp_path: pathlib.Path,
) -> None:
	"""`#832`. The driver's error names things a stranger could not learn by connecting.

	An unreachable host, a database name, or — as here — a **filesystem path**. On a served
	instance that is the layout of somebody's machine, handed to anybody who can reach a URL
	that is public by design and has no credential to check.

	**Keyed on ``public_url`` rather than on the bind**, which is `#286`'s ordering and the
	whole reason this is answerable at all: the arrangement ``docs/hosting.md`` recommends
	terminates TLS at a proxy and binds the application to loopback, so the socket says
	"private" about an instance on the public internet.
	"""

	unreachable = tmp_path / "no-such-directory" / "subroutine.db"
	engine = subroutine.db.session.create_engine(f"sqlite:///{unreachable}")

	try:
		application = api_support.build_app(
			subroutine.db.session.create_session_factory(engine),
			public_url="https://work.example.com",
		)
		response = api_support.call(application, "GET", "/readyz")

	finally:
		engine.dispose()

	assert response.status_code == 503
	assert response.json()["code"] == "service_unavailable"

	# The fact still arrives, and so does the remedy — what goes is the cause.
	assert "not ready" in response.json()["detail"]
	assert "database_url" in response.json()["hint"]

	# **Asserted on the string the private case actually discloses**, not on the path. SQLite
	# reports "unable to open database file" and never names the file, so a check for the path
	# would pass here whatever the endpoint did — an absence both behaviours produce, which is
	# a test that cannot fail. The PostgreSQL failure the review measured names a host instead;
	# what is common to both, and what this pins, is that the driver's words do not travel.
	assert DRIVER_SAYS not in json.dumps(response.json()), (
		"the driver's cause must not survive anywhere in the body"
	)


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
	"""``/v1/openapi.json`` is where docs/design.md §8.6 says it is."""

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


# --- What a started instance says it is serving ----------------------------------------


#: Addresses a started instance answers that are deliberately not announced, and why.
#:
#: **The list is the point rather than the contents.** `#780` happened because ``POST /mcp``
#: was mounted and no channel a reader is guaranteed said so; the only way to notice the next
#: one is to be made to write down why it needs no saying. Anything under ``/v1`` is the HTTP
#: API itself and is announced as one line rather than sixty.
#:
#: **The reach this has is ``ROUTERS``**, so FastAPI's own ``/docs`` and ``/redoc`` are
#: outside it — they are built into the application rather than mounted, and ``/v1/meta``
#: publishes both. Said here rather than left to be discovered, because a guard's blind spot
#: is worth more written down than a guard's coverage is.
NOT_ANNOUNCED = {
	"/healthz": "a liveness probe, read by whatever watches the process rather than by a person",
	"/readyz": "the same, and docs/hosting.md names it where an operator configures one",
	"/": "the browser app, which is what the address on the line above already opens",
	"/app/{name}": "the browser app's own files, fetched by that page rather than by anybody",
	"/signin": "how a person signs in, reached from the page rather than typed",
}


def test_a_transport_that_is_not_mounted_is_not_announced () -> None:
	"""`#780`. The announcement is derived from the routes, so it cannot outlive one.

	Falsified by handing in the routers with the MCP one taken out: the answer loses that
	line. A function that read the module's own constant would pass this by saying the same
	thing whatever it was given, which is `#405`'s whole complaint about a scanner that
	cannot be handed its subject.
	"""

	whole = [surface.path for surface in subroutine.api.app.serving()]
	without = [
		surface.path
		for surface in subroutine.api.app.serving(
			tuple(
				mounting
				for mounting in subroutine.api.app.ROUTERS
				if mounting[1] is not subroutine.api.mcp.router
			)
		)
	]

	assert subroutine.api.mcp.PATH in whole, "this instance serves MCP and should say so"
	assert subroutine.api.mcp.PATH not in without
	assert "/v1" in without, "and removing one transport must not remove the other"


def test_every_address_a_started_instance_answers_is_announced_or_excused () -> None:
	"""The direction `#780` came from: something is served and nothing mentions it.

	Checking that what is announced exists is the easy half and catches a removal. This is
	the half that catches an addition — a new root-level route is either something an
	operator is told about when the server starts, or it is a written reason why not.
	"""

	answered = {
		path
		for path, _methods, _route in subroutine.api.routing.mounted(subroutine.api.app.ROUTERS)
		if not path.startswith("/v1")
	}
	announced = {surface.path for surface in subroutine.api.app.serving()}

	unexplained = answered - announced - set(NOT_ANNOUNCED)

	assert not unexplained, (
		f"a started instance answers {sorted(unexplained)} and says nothing about it. "
		f"Add it to api.app.SURFACES, or to NOT_ANNOUNCED with the reason it needs no line."
	)


def test_nothing_is_excused_from_the_announcement_that_no_longer_exists () -> None:
	"""What makes an entry go away. Every allow-list here owes this test."""

	answered = {
		path
		for path, _methods, _route in subroutine.api.routing.mounted(subroutine.api.app.ROUTERS)
	}
	gone = set(NOT_ANNOUNCED) - answered

	assert not gone, f"{sorted(gone)} is excused from the announcement and is not served"
