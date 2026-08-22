"""A write is refused while this build and its database disagree about the schema (`SR#973`).

Measured on the served instance in the window between new code being deployed and
``subroutine db upgrade`` being run: ``/readyz`` answered 503 naming both revisions, ``POST
/mcp`` refused with the same sentence, and a comment written over HTTP **succeeded**. One
instance, one mismatch, three answers — because ``db.migrate.mismatch_reason`` had two callers
and neither was an API request path.
"""

import typing

import pytest
import sqlalchemy.orm

import subroutine.api.app
import subroutine.api.routing
import subroutine.api.schema
import subroutine.api.security
import subroutine.db.migrate
import test_api_tasks


@pytest.fixture
def world (session: sqlalchemy.orm.Session) -> test_api_tasks.World:
	"""An installation reachable over HTTP, sharing the test's transaction."""

	return test_api_tasks._world(session)


def _disagree (world: test_api_tasks.World) -> None:
	"""Make this build expect a revision the database is not at.

	The direction a deploy produces: the code is ahead, the migration has not run. Patching the
	*expected* head rather than the database's row is what keeps this a test about the check —
	rewriting ``alembic_version`` inside the suite's shared transaction would leave the schema
	saying something no migration ever wrote.
	"""

	world.application.state.schema_head = "not-a-revision-anybody-has"
	world.application.state.schema_agrees = False


def test_a_read_still_works_when_the_schema_is_behind (world: test_api_tasks.World) -> None:
	"""Simon's decision of 2026-08-22, and the half that is easy to lose.

	Refusing to start would have been cheaper and louder, and it takes away ``/readyz``'s
	sentence, stops somebody looking something up, and breaks a serial per-tenant fleet upgrade
	— which has to reach an instance to verify it. A guard that turns a mistimed deploy into a
	total outage is the one people work around.
	"""

	world.call("POST", "/v1/tasks", json={"title": "Written before the deploy"})
	_disagree(world)

	answered = world.call("GET", "/v1/tasks")

	assert answered.status_code == 200, answered.text
	assert [row["title"] for row in answered.json()["items"]] == [
		"Written before the deploy"
	]


def test_a_write_is_refused_and_says_which_revisions_disagree (
	world: test_api_tasks.World,
) -> None:
	"""409 ``schema_mismatch`` — the code the CLI already gives for the same condition.

	Deliberately not 503: the instance is serving perfectly well and this one request cannot
	be honoured, which is what a 409 says. ``/readyz`` keeps answering 503, because every load
	balancer reading it would change behaviour otherwise.
	"""

	_disagree(world)

	refused = world.call("POST", "/v1/tasks", json={"title": "Written mid-deploy"})

	assert refused.status_code == 409, refused.text

	body = refused.json()

	assert body["code"] == "schema_mismatch"
	assert "not-a-revision-anybody-has" in body["detail"] + body.get("hint", ""), (
		"the refusal does not name the revision this build expects, so an operator is told "
		"something disagrees and not what"
	)
	assert "db upgrade" in body.get("hint", ""), (
		"the remedy is the safe procedure — report both versions, back up, migrate, check — "
		"and a refusal that does not name it sends the reader to the raw migrator"
	)


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_every_kind_of_write_is_refused_not_only_a_create (
	world: test_api_tasks.World, method: str
) -> None:
	"""The rule is the *method*, so it cannot fall behind the routes.

	``security.SAFE_METHODS`` has decided what counts as a write since `SR#639`, in the resolver
	chain every credentialed route already passes through. A second list of writing routes is
	what `SR#676` and `SR#897` both fell behind, months apart and each found by accident.
	"""

	made = world.call("POST", "/v1/tasks", json={"title": "Already here"}).json()
	_disagree(world)

	refused = world.call(
		method,
		f"/v1/tasks/{made['ref']}",
		**({"json": {"title": "Renamed"}} if method == "PATCH" else {}),
	)

	assert refused.status_code == 409, refused.text
	assert refused.json()["code"] == "schema_mismatch"


def test_a_post_that_writes_nothing_is_not_refused (world: test_api_tasks.World) -> None:
	"""The two written exceptions, and the reason they are written rather than derived.

	A method rule cannot tell a `POST` that changes something from one that does not. Both
	entries in ``NOT_A_WRITE`` are the second kind: this one reads a phrase back and touches no
	table, and `POST /v1/admin/backups` **copies** the database rather than changing it —
	refusing that would take away the thing an operator reaches for first at exactly the moment
	a deploy has gone wrong.

	The backup route is covered by the register check below rather than driven, because driving
	it takes a real backup of the suite's own database.
	"""

	_disagree(world)

	answered = world.call("POST", "/v1/recurrence/parse", json={"phrase": "every monday"})

	assert answered.status_code != 409, (
		"reading a phrase back was refused because the schema is behind, and it reads nothing"
	)


def test_an_agreeing_instance_is_asked_once_and_not_again (
	world: test_api_tasks.World, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The cost, which is what decided against checking per request.

	Latched when it agrees, because agreement cannot change under a running process without
	somebody migrating — and they are told to stop the service first. Disagreement is *not*
	latched, so `db upgrade` puts an instance back into use without a restart; that direction
	is asserted below.
	"""

	asked = 0
	reading = subroutine.db.migrate.revision_on

	def counted (connection: typing.Any) -> typing.Any:
		"""Count how often the revision is read off the database."""

		nonlocal asked
		asked += 1

		return reading(connection)

	monkeypatch.setattr(subroutine.db.migrate, "revision_on", counted)
	world.application.state.schema_agrees = False

	for index in range(3):
		world.call("POST", "/v1/tasks", json={"title": f"Number {index}"})

	assert asked == 1, f"the database was asked its revision {asked} times for three writes"


def test_an_instance_put_right_stops_refusing_without_a_restart (
	world: test_api_tasks.World,
) -> None:
	"""The other half of the latch, and the reason it is one-sided.

	An operator who runs the migration against a live service should not also have to restart
	it to find out that they have. Caching the *disagreement* would have made that necessary,
	and the cost of not caching it is one query per write while the instance is refusing them
	anyway.
	"""

	_disagree(world)

	assert world.call("POST", "/v1/tasks", json={"title": "Too soon"}).status_code == 409

	# **The second refusal is what discriminates, and it was missing.** Latching the
	# disagreement alongside the agreement passed every other assertion here: the first write
	# is refused, the flag is set, and the *second* is accepted against the schema this build
	# does not expect. Caching the wrong answer is worse than needing a restart, and only
	# asking twice can see it.
	assert world.call("POST", "/v1/tasks", json={"title": "Still too soon"}).status_code == 409, (
		"a second write was accepted against a schema this build does not expect, so the "
		"refusal cached the fact that it had refused rather than re-reading"
	)

	world.application.state.schema_head = subroutine.db.migrate.head_revision()

	answered = world.call("POST", "/v1/tasks", json={"title": "After the migration"})

	assert answered.status_code == 201, answered.text


def test_every_route_excused_from_the_schema_check_exists_and_is_a_write () -> None:
	"""An excuse naming a route that has moved reads exactly like a considered decision.

	Both directions, which is the shape `SR#405` went round the repository adding: an entry for
	a route that no longer exists, and one for a route that was never checked anyway because
	its method is safe.
	"""

	mounted = {
		f"{method} {path}"
		for path, methods, _route in subroutine.api.routing.mounted(
			subroutine.api.app.ROUTERS
		)
		for method in methods
	}

	unknown = sorted(set(subroutine.api.schema.NOT_A_WRITE) - mounted)

	assert not unknown, f"{unknown} are excused from the schema check and are not routes"

	safe = sorted(
		named
		for named in subroutine.api.schema.NOT_A_WRITE
		if named.split(" ", 1)[0] in subroutine.api.security.SAFE_METHODS
	)

	assert not safe, (
		f"{safe} are excused from a check that never applied to them — the rule is the method, "
		"so a read needs no entry"
	)
