"""A write is refused while this build and its database disagree about the schema (`SR#973`).

Measured on the served instance in the window between new code being deployed and
``subroutine db upgrade`` being run: ``/readyz`` answered 503 naming both revisions, ``POST
/mcp`` refused with the same sentence, and a comment written over HTTP **succeeded**. One
instance, one mismatch, three answers — because ``db.migrate.mismatch_reason`` had two callers
and neither was an API request path.
"""

import typing

import pytest
import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.orm

import api_support
import subroutine.api.app
import subroutine.api.routing
import subroutine.api.schema
import subroutine.api.security
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.db.migrate
import subroutine.db.models.identity
import subroutine.domain.sessions
import subroutine.errors
import subroutine.views
import test_api_sessions
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


def test_who_am_i_answers_while_a_role_holds_a_name_a_migration_has_yet_to_take_out (
	world: test_api_tasks.World, session: sqlalchemy.orm.Session
) -> None:
	"""`SR#5034`: in the window this file is about, ``GET /v1/me`` answered 500 to every owner.

	The build that deleted ``token:admin`` ships the migration that takes it out of every role,
	and an instance serves reads until that migration has run. ``/v1/me`` asks the permission
	check about each name on the caller's role, and the check raises for a name it does not
	know, so the read every client makes first failed for the people who could run the
	upgrade. Seen in a served instance's journal on 2026-10-13.
	"""

	_disagree(world)
	model = subroutine.db.models.identity.Role
	member = subroutine.db.models.identity.WorkspaceMember
	role = session.scalars(
		sqlalchemy.select(model)
		.join(member, member.role_id == model.id)
		.where(member.workspace_id == world.workspace.id, member.user_id == world.user.id)
	).one()
	role.permissions = [*role.permissions, "token:admin"]
	session.flush()

	answer = world.call("GET", "/v1/me")

	assert answer.status_code == 200, answer.text

	(here,) = [one for one in answer.json()["workspaces"] if one["slug"] == world.workspace.slug]

	assert "token:admin" not in here["permissions"]
	assert "task:write" in here["permissions"], "the names this build knows are still offered"


def test_a_local_client_that_refused_a_schema_goes_on_refusing_it (
	world: test_api_tasks.World,
	session: sqlalchemy.orm.Session,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`SR#2647`: the local client marked its check done before it compared.

	So the first session on a client that met a database this build does not match was refused,
	and every later session returned at the mark and ran against it - the ``no such column`` the
	check exists to replace. Asked twice through one client, as a merged read that goes on after
	one connection fails would ask it. The expected head is patched rather than the database's
	row, for :func:`_disagree`'s reason.
	"""

	monkeypatch.setattr(
		subroutine.db.migrate, "head_revision", lambda: "not-a-revision-anybody-has"
	)

	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="local"),
		subroutine.config.Settings(dev_mode=True),
		session_factory=api_support.factory_for(session),
	)

	with client:
		with pytest.raises(subroutine.errors.SchemaMismatch):
			client.me()

		# **And again, which is the defect**: the mark said checked and nothing was compared.
		with pytest.raises(subroutine.errors.SchemaMismatch):
			client.me()


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


def _missing_column (*_arguments: typing.Any, **_named: typing.Any) -> typing.Any:
	"""Fail as a statement does that names a column this database does not have yet."""

	raise sqlalchemy.exc.OperationalError(
		"SELECT instance.events_archived_through FROM instance",
		{},
		Exception("no such column: instance.events_archived_through"),
	)


def _unknown_name (*_arguments: typing.Any, **_named: typing.Any) -> typing.Any:
	"""Fail as code does that meets a stored value a migration has yet to rewrite."""

	raise ValueError("Unknown workspace permission 'token:admin'.")


@pytest.mark.parametrize("failing", [_missing_column, _unknown_name])
def test_a_read_that_fails_while_the_schema_is_behind_says_so (
	world: test_api_tasks.World,
	monkeypatch: pytest.MonkeyPatch,
	failing: typing.Callable[..., typing.Any],
) -> None:
	"""`SR#5057`: over a database older than 0.10.0, most reads failed with nothing said.

	This build serving a database at the 0.9.13 schema answered 500 *Something went wrong
	that should not have* on forty of its seventy-six read routes, ``/v1/me`` and every
	calendar feed among them: the instance row gained a column in 0.10.0, and nearly every
	request reads that row. The instance knew why - ``/readyz`` said so - and told nobody who
	asked for anything else. A read that works is still served (the test above); one that
	fails names both revisions and the remedy, whatever it failed on.
	"""

	monkeypatch.setattr(subroutine.views, "me", failing)

	bare = world.call("GET", "/v1/me")

	assert bare.status_code == 500, bare.text
	assert bare.json()["code"] == "internal_error"
	assert "request id" in bare.json()["hint"], (
		"with the schema as this build expects it, a failure is a fault to be reported"
	)

	_disagree(world)

	said = world.call("GET", "/v1/me")

	assert said.status_code == 409, said.text

	body = said.json()

	assert body["code"] == "schema_mismatch"
	assert "not-a-revision-anybody-has" in body["detail"] + body.get("hint", ""), body
	assert "db upgrade" in body.get("hint", ""), body
	assert "should not have" not in said.text


def test_a_failure_while_the_schema_is_behind_is_still_logged (
	world: test_api_tasks.World,
	monkeypatch: pytest.MonkeyPatch,
	caplog: pytest.LogCaptureFixture,
) -> None:
	"""`SR#5057`: what failed stays in the log, under the request's id, as before.

	The answer says the schema is behind because that is what the reader can act on. It may
	not be why this request failed, and whoever runs the instance has to be able to tell.
	"""

	monkeypatch.setattr(subroutine.views, "me", _unknown_name)
	_disagree(world)

	with caplog.at_level("WARNING"):
		world.call("GET", "/v1/me")

	said = [record.getMessage() for record in caplog.records]

	assert any("Unhandled error serving GET /v1/me" in line for line in said), said

	# `SR#5058`: and the log ends on the reason and the remedy, because its last lines are
	# what `systemctl status` shows and the tail of a traceback names neither.
	assert "not-a-revision-anybody-has" in said[-1], said
	assert "db upgrade" in said[-1], said


def test_a_failure_with_the_schema_as_expected_logs_nothing_about_the_schema (
	world: test_api_tasks.World,
	monkeypatch: pytest.MonkeyPatch,
	caplog: pytest.LogCaptureFixture,
) -> None:
	"""`SR#5058`: the line is for a database that disagrees. A bug stays a bug in the log."""

	monkeypatch.setattr(subroutine.views, "me", _unknown_name)

	with caplog.at_level("WARNING"):
		world.call("GET", "/v1/me")

	said = [record.getMessage() for record in caplog.records]

	assert any("Unhandled error serving GET /v1/me" in line for line in said), said
	assert not any("schema" in line for line in said), said


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
	"""The two exceptions that write nothing, and why they are written rather than derived.

	A method rule cannot tell a `POST` that changes something from one that does not. Two
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


def test_confirming_a_sign_in_is_not_refused_while_the_schema_is_behind (
	world: test_api_tasks.World,
) -> None:
	"""`SR#4393` moved signing in from ``GET /signin`` to a ``POST``, which the method rule refuses.

	Being able to sign in and look at an instance that is refusing writes is the courtesy reads
	are given, and the ``GET`` always had it - so the ``POST`` that took its place is written down.
	"""

	_row, secret = subroutine.domain.sessions.mint_link(world.session, user=world.user)
	_disagree(world)

	answer = test_api_sessions._confirmed(world.application, secret)

	assert answer.status_code == 303, answer.text


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


def test_the_one_read_that_writes_is_recorded_rather_than_implied () -> None:
	"""`#1174`. The guard's docstring said the method *is* the definition of a write.

	``GET /signin`` spends a single-use link and creates a browser session, so it is a read by
	method and a write by effect — and it went round the schema check without anybody deciding
	it should. The outcome is defensible and is now written down at the guard; what this holds
	is the *fact* the prose rests on, so the sentence cannot outlive it.

	**Not an entry in ``NOT_A_WRITE``**, which the test above would reject: that map is
	consulted after the method check, so a ``GET`` never reaches it and an entry would be a
	record nothing reads. This is the record instead.
	"""

	mounted = {
		(path, tuple(sorted(methods)))
		for path, methods, _route in subroutine.api.routing.mounted(subroutine.api.app.ROUTERS)
	}

	signin = sorted(methods for path, methods in mounted if path == "/signin")

	# **A `POST` beside it since `SR#4393`**, the confirmation a browser signed in as nobody
	# answers. The `GET` still spends a link for a browser signed in as its account already.
	assert signin == [("GET",), ("POST",)], (
		"the schema guard's docstring names GET /signin as the one route that writes without "
		f"being a write by method, and this application mounts {signin}. If the GET has gone, "
		"the exception is gone and the paragraph explaining it should go too."
	)
