"""What a caller is told when its request cannot be used.

The point of these is agent legibility (docs/design.md §8.1). A 422 that says only "unprocessable
entity" leaves a client with no better next move than the guess that just failed, so every
failure here has to name the field it is about and, where the valid answers are a known
set, list them.
"""

import typing

import fastapi
import pytest
import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.orm

import api_support
import subroutine.api.schemas
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.domain.versions
import subroutine.errors
import test_api_tasks


class _Body(subroutine.api.schemas.RequestModel):
	"""A body standing in for the real ones, until S3-03 writes them."""

	title: str
	importance: int | None = None


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


@pytest.fixture
def application (session: sqlalchemy.orm.Session) -> fastapi.FastAPI:
	"""Build an application with one endpoint that accepts a body."""

	built = api_support.build_app(api_support.factory_for(session))

	@built.post("/v1/things")
	def create (body: _Body) -> dict[str, typing.Any]:
		"""Accept a thing."""

		return {"title": body.title}

	@built.get("/v1/refused")
	def refused () -> dict[str, str]:
		"""Fail the way a service fails."""

		raise subroutine.errors.NotFound(
			"There is no task SR-9999.",
			hint="Run 'subroutine ls' to see the tasks you can reach.",
		)

	@built.get("/v1/overtaken")
	def overtaken () -> dict[str, str]:
		"""Fail the way a write that lost a race fails."""

		raise subroutine.domain.versions.RACED(
			"UPDATE statement on table 'task' expected to update 1 row(s); 0 were matched."
		)

	return built


def test_a_service_failure_arrives_as_it_described_itself (application: fastapi.FastAPI) -> None:
	"""The detail, the hint and the code all survive the trip through HTTP."""

	response = api_support.call(application, "GET", "/v1/refused")

	assert response.status_code == 404
	assert response.headers["content-type"].startswith("application/problem+json")

	body = response.json()

	assert body["code"] == "not_found"
	assert body["detail"] == "There is no task SR-9999."
	assert body["hint"].startswith("Run 'subroutine ls'")
	# The registry's own answer, not a literal: `#163` moved these from a domain the project
	# does not own to the repository, and a hardcoded shape here would have to be edited again
	# the day a product domain replaces it.
	assert body["type"] == subroutine.errors.definition("not_found").type_uri


def test_an_unknown_field_is_refused_and_the_real_ones_are_listed (
	application: fastapi.FastAPI,
) -> None:
	"""A typo'd field is a 422, not a silently dropped value.

	This is the failure the rule exists for: accepting the request would produce a thing
	with no importance and a caller convinced it had set one.
	"""

	response = api_support.call(
		application, "POST", "/v1/things", json={"title": "Write it", "importanc": 3}
	)

	assert response.status_code == 422

	body = response.json()

	assert body["code"] == "unknown_field"

	field = body["errors"][0]

	assert field["field"] == "importanc"
	assert field["code"] == "unknown_field"
	assert "importance" in field["hint"]
	assert "title" in field["hint"]


def test_a_missing_field_names_itself (application: fastapi.FastAPI) -> None:
	"""A required field that was not sent is named."""

	response = api_support.call(application, "POST", "/v1/things", json={"importance": 3})

	assert response.status_code == 422

	body = response.json()

	assert body["code"] == "missing_field"
	assert body["errors"][0]["field"] == "title"
	assert "required" in body["errors"][0]["message"]


def test_a_bad_value_names_the_field_it_is_about (application: fastapi.FastAPI) -> None:
	"""A field of the wrong type is reported against that field."""

	response = api_support.call(
		application, "POST", "/v1/things", json={"title": "Fine", "importance": "high"}
	)

	assert response.status_code == 422

	body = response.json()

	assert body["code"] == "invalid_field_value"
	assert body["errors"][0]["field"] == "importance"


def test_several_complaints_are_all_reported (application: fastapi.FastAPI) -> None:
	"""One round trip tells the caller everything that is wrong, not the first thing."""

	response = api_support.call(
		application, "POST", "/v1/things", json={"importance": "high", "extra": 1}
	)

	assert response.status_code == 422

	fields = {error["field"] for error in response.json()["errors"]}

	assert fields == {"title", "importance", "extra"}


def test_a_body_that_is_not_json_is_a_different_failure (application: fastapi.FastAPI) -> None:
	"""Unreadable is not the same as read-and-rejected, and gets a 400."""

	response = api_support.call(
		application,
		"POST",
		"/v1/things",
		content=b"{not json",
		headers={"content-type": "application/json"},
	)

	assert response.status_code == 400
	assert response.json()["code"] == "malformed_request"


def test_a_query_parameter_keeps_its_location (session: sqlalchemy.orm.Session) -> None:
	"""``query.limit`` rather than ``limit``.

	Knowing the bad value was in the query string rather than the body is the difference
	between one fix and two.
	"""

	application = api_support.build_app(api_support.factory_for(session))

	@application.get("/v1/things")
	def listing (limit: int = 50) -> dict[str, int]:
		"""List things."""

		return {"limit": limit}

	response = api_support.call(application, "GET", "/v1/things?limit=many")

	assert response.status_code == 422
	assert response.json()["errors"][0]["field"] == "query.limit"


def test_every_registered_code_has_a_documented_status () -> None:
	"""The two codes this slice added are registered and consistent.

	``docs/errors.md`` is generated from the registry and a separate test asserts the file
	matches, so this is about the codes existing at all — a handler mapping a status onto a
	code that is not registered would raise at the moment it was needed most.
	"""

	for code, status in (("method_not_allowed", 405), ("service_unavailable", 503)):
		assert subroutine.errors.definition(code).status == status


def test_a_write_that_lost_a_race_is_a_conflict_rather_than_a_bug (
	application: fastapi.FastAPI,
) -> None:
	"""`#927`'s H-12 — the other half of the fix, and the half nothing else exercises.

	``VersionMixin`` writes every ``UPDATE`` under ``WHERE version = <what this transaction
	read>``, so a racing writer's statement matches no row and SQLAlchemy raises. **Left
	untranslated that is a 500**, on a caller who did nothing wrong, for the one condition
	§8.9 exists to report — and a 500 tells a client to report a bug where a 409 tells it to
	re-read and retry.

	Driven through the real handler stack rather than by calling the function, because what
	can rot is the *registration*: Starlette keys handlers by exception class, and a
	`StaleDataError` with nothing registered for it falls to the catch-all.

	A concurrent test could not reach this. ``test_api_concurrency`` proves the database
	refuses the second writer, over two real connections; this proves what that refusal
	*becomes* on the way out, which is a property of one request.
	"""

	response = api_support.call(application, "GET", "/v1/overtaken")

	assert response.status_code == 409
	assert response.headers["content-type"].startswith("application/problem+json")

	body = response.json()

	assert body["code"] == "version_conflict", (
		"the same code a stale expected_version earns, because the remedy is the same"
	)
	assert "read it again" in body["hint"].lower()

	# **And SQLAlchemy's own wording does not reach the caller.** The exception names a table
	# and a row count, which describes our internals and answers a question nobody asked.
	assert "UPDATE statement" not in response.text
	assert "table" not in body["detail"]


def test_the_local_client_calls_a_lost_update_a_conflict_too (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The same translation on the transport that does not go over HTTP.

	`clients/local.py` turns any `SQLAlchemyError` into `service_unavailable`, deliberately:
	a connection is allowed to fail and is not allowed to escape, because `fanout._attempt`
	catches only this project's own errors. **A `StaleDataError` is a `SQLAlchemyError`**, so
	without a narrower clause in front of it a caller who lost a race was told the instance
	was unreachable — about a database that answered perfectly, and with a remedy aimed at a
	machine rather than at the change they were making. `#899`'s shape: a broad refusal
	declared first swallows the specific one.

	*Which* outage it claimed depends on the instance, which is why this asserts the class
	rather than the sentence: with a real database behind it the message is "could not be
	read", and against the synthetic settings here it is "no instance has been set up yet".
	Both are the same wrong answer.

	The clause order is the whole assertion, so it is driven through `_reported` rather than
	inspected: reordering the two `except` blocks is the edit this has to catch, and it is
	invisible to anything that reads the file.
	"""

	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True),
		session_factory=api_support.factory_for(session),
	)

	with pytest.raises(subroutine.errors.Conflict) as refused, client._reported():
		raise subroutine.domain.versions.RACED("0 were matched")

	assert refused.value.code == "version_conflict"

	# And the broad clause still answers for everything else, which is what it is there for.
	with pytest.raises(subroutine.errors.ServiceUnavailable), client._reported():
		raise sqlalchemy.exc.OperationalError("SELECT 1", {}, Exception("gone away"))


def test_the_ambiguous_workspace_refusal_says_where_the_parameter_goes (
	world: test_api_tasks.World,
) -> None:
	"""`#1315`. ``workspace_id`` is a query parameter on 55 routes and a body field on three.

	The refusal named it bare, which is this API's spelling for *a field of the body* — so a
	caller who did exactly what it said was refused a second time, by ``unknown_field``, and
	had spent a round trip finding out. The field name is the only part of the message that
	can carry this: `#547` establishes that the prose cannot, because the same refusal is
	read on two transports that call the parameter two different names.
	"""

	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"})

	refused = world.call("PATCH", "/v1/tasks/1", json={"title": "Renamed"})

	assert refused.status_code == 422, refused.text

	field = refused.json()["errors"][0]

	assert field["code"] == "missing_field"
	assert field["field"] == "query.workspace_id", refused.text


def test_a_domain_refusal_on_a_bodiless_route_says_where_the_parameter_goes (
	world: test_api_tasks.World,
) -> None:
	"""`SR#1404`, Simon's decision of 2026-08-28: the rest of `SR#1315`.

	That fix qualified a name only where the endpoint *also* takes a body, on the reasoning that
	a bare name is ambiguous only when there is somewhere else to put it. True on its own terms,
	and it left one wire contract saying two things about one parameter depending on which layer
	refused it: Pydantic's path has said ``query.limit`` on a bodiless listing all along, and a
	refusal raised in the domain said ``limit``. Measured before the widening — 40 routes take
	``workspace_id`` in the query and accept no body at all.

	``/v1/tasks/{id_or_ref}/comments`` is the case: it takes ``workspace_id`` in the query and
	no body on a ``GET``, so before this the ambiguous-workspace refusal came back bare there
	and qualified on ``PATCH /v1/tasks/{id_or_ref}`` above.
	"""

	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"})

	refused = world.call("GET", "/v1/tasks/1/comments")

	assert refused.status_code == 422, refused.text

	field = refused.json()["errors"][0]

	assert field["code"] == "missing_field"
	assert field["field"] == "query.workspace_id", refused.text


def test_a_path_parameter_is_not_called_a_query_one (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`SR#1404`, and it is the latent half the widening would otherwise have made live.

	The qualification read the route's *flat* parameters, which include the path's, and wrote
	``query.`` in front of whatever it matched. That was harmless only because it ran on routes
	that take a body, where nothing raised a domain refusal naming a segment of the URL —
	widening it to every route is what would have started answering ``query.id_or_ref`` about
	something that is not in the query string at all.

	**Raised from the handler rather than by Pydantic, and the first version of this test got
	that wrong.** Pydantic names the location itself, and a name that already carries one is
	left alone here — so driving it that way asserted what the framework does and would have
	passed against a version that assumes every parameter is in the query. The refusal has to
	come from below the transport, naming the parameter bare, which is the whole population
	this function exists for.
	"""

	application = api_support.build_app(api_support.factory_for(session))

	@application.get("/v1/things/{which}")
	def one_thing (which: int, limit: int = 50) -> dict[str, int]:
		"""Read one thing."""

		raise subroutine.errors.ValidationError(
			"That is not a thing here.",
			code="invalid_field_value",
			errors=[
				subroutine.errors.FieldError(
					field="which",
					code="invalid_field_value",
					message="Name one that exists.",
				)
			],
		)

	refused = api_support.call(application, "GET", "/v1/things/7?limit=5")

	assert refused.status_code == 422
	assert refused.json()["errors"][0]["field"] == "path.which", refused.text


def test_our_own_clients_are_handed_the_name_without_its_location (  ) -> None:
	"""`SR#1404`: the wire keeps the location and a caller of this project's clients does not.

	**Because a fan-out merges failures from several connections** (§13.7). The local client
	raises the domain's refusal directly, with no transport to qualify anything; the remote one
	reads a problem document. If the location survived that boundary, one mistake would be
	reported two ways by two connections of one command — which is exactly the two vocabularies
	:func:`subroutine.errors.from_problem`'s own docstring says it exists to prevent, and what
	``tests/test_transport_equivalence.py`` asserts about this very field.

	The location is not lost to anybody who can act on it: a third-party client reads the
	document, and so does an agent through ``subroutine_call_api``, which hands back the
	response text rather than an exception.
	"""

	rebuilt = subroutine.errors.from_problem(
		{
			"code": "invalid_field_value",
			"status": 422,
			"detail": "That is not a number.",
			"errors": [
				{
					"field": "query.limit",
					"code": "invalid_field_value",
					"message": "Send a whole number.",
				},
				{
					"field": "title",
					"code": "missing_field",
					"message": "A title is required.",
				},
			],
		}
	)

	assert [one.field for one in rebuilt.errors] == ["limit", "title"], (
		"a body field must come through untouched, or this is a rule about every name rather "
		"than about a location"
	)


def test_a_body_field_is_still_named_bare_where_the_endpoint_takes_one (
	world: test_api_tasks.World,
) -> None:
	"""The other half, and the reason the fix is derived rather than a rename.

	``POST /v1/tasks`` takes ``workspace_id`` in the body and takes **no query parameters at
	all**, so a ``query.`` prefix there would send a caller somewhere the endpoint does not
	read. Same refusal, same code, opposite answer — which is what makes the distinction a
	property of the matched route rather than of the word.
	"""

	world.call("POST", "/v1/workspaces", json={"slug": "acme", "title": "Acme"})

	refused = world.call("POST", "/v1/tasks", json={"text": "Buy milk"})

	assert refused.status_code == 422, refused.text

	field = refused.json()["errors"][0]

	assert field["code"] == "missing_field"
	assert field["field"] == "workspace_id", refused.text
