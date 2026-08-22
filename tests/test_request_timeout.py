"""A request that can never finish is stopped and says so, rather than hanging — `#568`.

**Read :func:`test_a_request_the_database_gave_up_on_is_reported_as_a_timeout` first.** It is
the only test here that drives the whole path — a real bounded factory, a real application, a
real statement that will not finish — and the two either side of it exist because that one
cannot run on SQLite and cannot show what is *not* bounded.

The defect this is about is an **absence**: nothing bounded how long a statement could run, so a
row lock or a query that would never finish reached the caller as silence. From outside, silence
is indistinguishable from a deploy, a network fault or a proxy, which is what a visitor's agent
concluded during `#553` — reasonably, and wrongly.
"""

import time
import typing

import fastapi
import pytest
import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.orm

import api_support
import subroutine.api.app
import subroutine.api.dependencies
import subroutine.config
import subroutine.db.failures
import subroutine.db.session
import subroutine.mcp.protocol

#: What the bounded sessions in this file are given, in seconds. One rather than the shipped
#: thirty because every test here has to wait it out, and the number under test is the
#: mechanism rather than the default.
BOUND = 1

#: Longer than :data:`BOUND` by enough that a slow machine cannot make the two the same
#: measurement. A statement asked to run this long and refused at ``BOUND`` is the claim.
LONGER = 5


def _postgresql_only (engine: sqlalchemy.engine.Engine) -> None:
	"""Skip a test SQLite structurally cannot answer, saying why rather than passing quietly.

	SQLite has no statement timeout of any kind and no way to make a statement take a
	measurable time on demand, so there is nothing here for it to be asked. What it *does*
	have is ``busy_timeout``, which bounds the case that actually hangs there — see
	:func:`test_sqlite_is_given_no_statement_timeout_and_that_is_deliberate`.
	"""

	if engine.dialect.name != "postgresql":
		pytest.skip("Only PostgreSQL has a statement timeout to set.")


def _sleeping (application: fastapi.FastAPI, seconds: int) -> None:
	"""Give ``application`` a route whose only work is a statement that will not finish soon.

	A synthetic endpoint rather than a real one, because no endpoint here is slow on purpose
	and one contrived to be would be a worse test: the subject is the plumbing between the
	session factory, the failure and the handler, and every part of that is the application's
	own. What the endpoint does is the fixture.
	"""

	def probe (session: subroutine.api.dependencies.SessionDep) -> dict[str, bool]:
		"""Wait longer than this instance allows, and never answer."""

		session.execute(sqlalchemy.text(f"SELECT pg_sleep({seconds})"))

		return {"finished": True}

	application.get("/probe-568")(probe)


def test_a_request_the_database_gave_up_on_is_reported_as_a_timeout (
	engine: sqlalchemy.engine.Engine,
) -> None:
	"""A request that cannot finish is refused in seconds, naming itself rather than the code."""

	_postgresql_only(engine)

	factory = subroutine.db.session.create_session_factory(
		engine, statement_timeout_seconds=BOUND
	)
	application = api_support.build_app(factory, request_timeout_seconds=BOUND)

	_sleeping(application, LONGER)

	started = time.monotonic()
	answer = api_support.call(application, "GET", "/probe-568")
	waited = time.monotonic() - started

	assert answer.status_code == 503
	assert answer.json()["code"] == "request_timed_out"

	# The number the caller is told is the one actually in force, not a constant beside it.
	assert f"{BOUND} seconds" in answer.json()["detail"]

	# **The claim, and the reason the two numbers are far apart**: refused on the bound rather
	# than on the statement finishing. Without the listener this waits `LONGER` and answers 200.
	assert waited < LONGER


def test_the_served_application_bounds_the_sessions_it_builds_for_itself (
	engine: sqlalchemy.engine.Engine, postgres_url: str
) -> None:
	"""An instance started the ordinary way puts its own setting in force.

	**Every other test here passes a factory this file bounded by hand**, so all of them
	survive ``create_app`` building its own and passing nothing — measured, and the reason this
	test exists. The decision was lifted out and driven, and the *wiring* was read rather than
	driven, which is this project's own recorded shape: the rule right, the display right, and
	nothing joining them.

	It is the one test that goes through ``create_app``'s engine branch, so it is also the only
	one that would notice the setting being renamed on one side of the call.
	"""

	_postgresql_only(engine)

	application = subroutine.api.app.create_app(
		settings=subroutine.config.Settings(
			dev_mode=True, database_url=postgres_url, request_timeout_seconds=BOUND
		)
	)

	_sleeping(application, LONGER)

	answer = api_support.call(application, "GET", "/probe-568", lifespan=True)

	assert answer.status_code == 503
	assert answer.json()["code"] == "request_timed_out"


def test_a_request_that_is_given_up_on_is_not_reported_as_a_bug (
	engine: sqlalchemy.engine.Engine,
) -> None:
	"""The 503 is what the caller gets — never the 500 an untranslated failure would be.

	Falsifies the handler from the side that matters. Deleting the registration leaves the
	first test asserting a status it would still not get, but a reader could believe the shape
	came from somewhere else; this says what the alternative is.
	"""

	_postgresql_only(engine)

	factory = subroutine.db.session.create_session_factory(
		engine, statement_timeout_seconds=BOUND
	)
	application = api_support.build_app(factory, request_timeout_seconds=BOUND)

	_sleeping(application, LONGER)

	body = api_support.call(application, "GET", "/probe-568").json()

	assert body["code"] != "internal_error"
	assert "request id" not in (body.get("hint") or "")


def _statements_from (
	factory: sqlalchemy.orm.sessionmaker[sqlalchemy.orm.Session],
	engine: sqlalchemy.engine.Engine,
) -> list[str]:
	"""Return everything a session from ``factory`` puts to the database, doing trivial work.

	**What reaches the database, rather than what is registered against the factory.** A
	``sessionmaker`` exposes no listener collection to count, and counting one would answer
	whether something was *attached* — where the question is whether the limit is *in force*,
	which is the distinction this project keeps finding on the wrong side.
	"""

	seen: list[str] = []

	def record (
		_connection: typing.Any,
		_cursor: typing.Any,
		statement: str,
		_parameters: typing.Any,
		_context: typing.Any,
		_many: bool,
	) -> None:
		"""Keep every statement, in the order the driver was given it."""

		seen.append(statement)

	sqlalchemy.event.listen(engine, "before_cursor_execute", record)

	try:
		session = factory()

		try:
			assert session.execute(sqlalchemy.text("SELECT 1")).scalar_one() == 1

		finally:
			session.rollback()
			session.close()

	finally:
		sqlalchemy.event.remove(engine, "before_cursor_execute", record)

	return seen


def _limited (statements: typing.Sequence[str]) -> bool:
	"""Report whether any of these told the database how long a statement may run."""

	return any("statement_timeout" in one for one in statements)


def test_a_bounded_session_tells_the_database_the_limit (
	engine: sqlalchemy.engine.Engine,
) -> None:
	"""The limit is set on the transaction, and set to the number the caller asked for."""

	_postgresql_only(engine)

	factory = subroutine.db.session.create_session_factory(
		engine, statement_timeout_seconds=BOUND
	)
	statements = _statements_from(factory, engine)

	assert _limited(statements)
	assert any(f"= {BOUND * 1000}" in one for one in statements)

	# `SET` rather than `SET LOCAL` passes every other test in this file and leaves the value
	# on a pooled connection for whatever borrows it next — including a backup.
	assert any("SET LOCAL" in one for one in statements)


def test_sqlite_is_given_no_statement_timeout_and_that_is_deliberate (
	engine: sqlalchemy.engine.Engine,
) -> None:
	"""On SQLite the setting reaches nothing, rather than appearing to and quietly not.

	SQLite has no statement timeout, so the honest thing is to send nothing and say so — a
	listener issuing a ``SET`` no SQLite understands would fail every request on a laptop. The
	bound that does apply there is ``busy_timeout``, set per connection in ``db/session`` and
	covering the lock wait, which is the case that actually hangs.
	"""

	if engine.dialect.name != "sqlite":
		pytest.skip("About what SQLite does with a setting it cannot honour.")

	factory = subroutine.db.session.create_session_factory(
		engine, statement_timeout_seconds=BOUND
	)

	assert not _limited(_statements_from(factory, engine))


def test_a_factory_nobody_bounded_is_left_alone (
	engine: sqlalchemy.engine.Engine,
) -> None:
	"""Only the served application's sessions are limited; everything else is as it was.

	The CLI, the migrator and the backup path all build their own factories and pass nothing.
	Were the default anything but *no limit*, a person at a terminal would inherit a bound
	nobody chose for them, on the connections their own commands run through.
	"""

	factory = subroutine.db.session.create_session_factory(engine)

	assert not _limited(_statements_from(factory, engine))


def test_the_limit_reverts_with_the_transaction_it_was_set_for (
	engine: sqlalchemy.engine.Engine,
) -> None:
	"""A pooled connection is handed on unbounded, whoever borrowed it before.

	``SET LOCAL`` rather than ``SET``, and this is the difference. The connections a bounded
	session uses go back to the same pool a **backup** draws from, so a session-wide timeout
	left behind on one would eventually cancel a ``pg_dump`` — the failure this whole design is
	arranged to avoid, arriving by the back door.
	"""

	_postgresql_only(engine)

	bounded = subroutine.db.session.create_session_factory(
		engine, statement_timeout_seconds=BOUND
	)
	plain = subroutine.db.session.create_session_factory(engine)

	session = bounded()

	try:
		with pytest.raises(sqlalchemy.exc.OperationalError):
			session.execute(sqlalchemy.text(f"SELECT pg_sleep({LONGER})"))

	finally:
		session.rollback()
		session.close()

	after = plain()

	try:
		started = time.monotonic()
		after.execute(sqlalchemy.text("SELECT pg_sleep(2)"))

		assert time.monotonic() - started >= 2

	finally:
		after.rollback()
		after.close()


def _refused (
	state: str, session: sqlalchemy.orm.Session
) -> dict[str, typing.Any]:
	"""Ask a real application what it makes of a database failure carrying ``state``.

	The failure is fabricated and the path is not: these are states this instance cannot
	provoke on demand — a dropped connection, a deadlock the detector broke — so raising one
	inside an endpoint is the only way to drive the handler that reads them. Everything after
	the ``raise`` is the application's own, which is what makes this worth more than calling
	the handler with a request built by hand.
	"""

	class Reported(Exception):
		"""Stand in for the driver's own exception, which is all the handler reads."""

		sqlstate = state

	application = api_support.build_app(
		api_support.factory_for(session), request_timeout_seconds=BOUND
	)

	def probe () -> dict[str, bool]:
		"""Fail the way the database would."""

		raise sqlalchemy.exc.OperationalError("SELECT 1", {}, Reported())

	application.get("/probe-568-raises")(probe)

	answer: dict[str, typing.Any] = api_support.call(
		application, "GET", "/probe-568-raises"
	).json()

	return answer


def test_a_deadlock_is_reported_to_the_caller_rather_than_logged_as_a_bug (
	session: sqlalchemy.orm.Session,
) -> None:
	"""PostgreSQL breaks a deadlock by cancelling somebody, and that somebody is owed a reason.

	Not a timeout — the detector fires on its own, long before any bound — but the same answer
	is the right one: nothing was changed by it, and retrying may work.
	"""

	assert _refused("40P01", session)["code"] == "request_timed_out"


def test_a_lock_this_instance_stopped_waiting_for_is_reported_as_a_wait (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``lock_timeout`` is not set today, and this branch is here for the day it is.

	Kept deliberately, with the argument in ``db/session._bounded_by``: at one number the state
	is unreachable, and the reason it would be worth reaching is exactly that it names the wait.
	A branch that answers it costs three words and stops the next reader concluding the family
	was never considered.
	"""

	assert "waited" in _refused("55P03", session)["detail"]


#: A tool the failure can be attributed to. Its only part in this is naming a field back to the
#: caller, which none of these refusals does — so any tool serves, and one built here keeps the
#: test off whichever real tool somebody renames next.
_ANY_TOOL = subroutine.mcp.protocol.Tool(
	name="subroutine_probe",
	title="Probe",
	description="Stand in for whichever tool was being called.",
	schema={"type": "object", "properties": {}},
	call=lambda arguments: "",
)


def _raised (state: str) -> sqlalchemy.exc.OperationalError:
	"""Return the exception a driver reporting ``state`` would hand SQLAlchemy.

	The statement and the parameters are real-shaped on purpose: what `SR#1070` is about is
	that an agent was shown them, so a stand-in with nothing in it could not fail.
	"""

	class Reported(Exception):
		"""Stand in for the driver's own exception, which is all this reads."""

		sqlstate = state

	return sqlalchemy.exc.OperationalError(
		"SELECT task.title FROM task WHERE task.workspace_id = %(workspace_id)s",
		{"workspace_id": "019fad98-4313-7e36-b972-f7decf66f8ae"},
		Reported(),
	)


def test_an_agent_is_told_a_request_was_given_up_on_rather_than_shown_the_sql () -> None:
	"""The MCP tools run inside this instance, on the same bounded session (`SR#1070`).

	Since `SR#539` these tools are answered server-side, so ``57014``, ``55P03`` and ``40P01``
	arrive inside a tool call exactly as they arrive at an HTTP route — where they are answered
	`request_timed_out` with a remedy. Here the dispatcher's catch-all rendered
	``str(failure)``, which is SQLAlchemy's own text: **the statement, the bound parameters,
	and a link to its website**.

	The parameters are the part that decides this is more than untidy: they are somebody's
	data, and a model carries what it is shown.
	"""

	answer = subroutine.mcp.protocol._explained(_raised("57014"), _ANY_TOOL)

	assert "SELECT" not in answer, f"the statement reached the agent:\n{answer}"
	assert "workspace_id" not in answer, f"a bound parameter reached the agent:\n{answer}"
	assert "sqlalche" not in answer, f"a link to somebody else's website:\n{answer}"

	assert "given up on" in answer, answer
	assert "Retrying may work" in answer, (
		f"the agent was told what happened and not what to do about it:\n{answer}"
	)


@pytest.mark.parametrize("state", ["55P03", "40P01"])
def test_a_bound_this_instance_did_not_set_is_not_named_in_the_refusal (state: str) -> None:
	"""`SR#1077`. The refusal said "after N seconds" for two states it does not bound.

	``request_timeout_seconds`` is ``statement_timeout`` and bounds ``57014`` alone. A deadlock
	is detected at PostgreSQL's own ``deadlock_timeout``, and ``55P03`` is ``lock_timeout``,
	which ``db/session._bounded_by`` **deliberately does not set** and writes down why — so the
	number was one that had nothing to do with either, and read *"after 0 seconds"* on an
	instance with the bound turned off.

	A refusal must not assert a cause it has not established. This is the same fault one field
	along: the cause was right and the *bound* was invented.
	"""

	answer = subroutine.db.failures.gave_up(_raised(state), seconds=30)

	assert answer is not None
	assert "30 seconds" not in answer.detail, answer.detail
	assert "seconds" not in answer.detail, answer.detail

	bounded = subroutine.db.failures.gave_up(_raised("57014"), seconds=30)

	assert bounded is not None
	assert "after 30 seconds" in bounded.detail, (
		f"the one state this bound really does bound stopped naming it: {bounded.detail}"
	)


def test_a_surface_that_does_not_know_the_bound_claims_no_number () -> None:
	"""Better than claiming the wrong one, which is what an invented default would be.

	The MCP dispatcher holds no settings, so it passes none. The sentence then says what
	happened and what to do, and nothing about how long anybody waited.
	"""

	answer = subroutine.db.failures.gave_up(_raised("57014"))

	assert answer is not None
	assert "seconds" not in answer.detail, answer.detail
	assert "given up on" in answer.detail


def test_anything_else_is_not_this_functions_to_report () -> None:
	"""The falsification that matters, at the layer both surfaces now share.

	``OperationalError`` is most of what a database can raise. Answering ``None`` rather than
	guessing is what keeps a dropped connection going to the handler that logs it with a
	traceback — on **both** surfaces now, rather than on one.
	"""

	assert subroutine.db.failures.gave_up(_raised("08006")) is None
	assert subroutine.db.failures.gave_up(Exception("nothing to do with a database")) is None


def test_any_other_database_failure_is_still_reported_as_a_bug (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A dropped connection is not a request that ran too long, and must not read as one.

	The falsification that matters. ``OperationalError`` is most of what a database can raise —
	a connection lost, a disk full, a database shut down underneath us — so a handler keying on
	the class rather than on the state would rename every one of them and lose the traceback
	that explains them.
	"""

	assert _refused("08006", session)["code"] == "internal_error"


def test_a_failure_carrying_no_state_at_all_is_still_reported_as_a_bug (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A driver that names the state something else costs the translation, never the report."""

	application = api_support.build_app(
		api_support.factory_for(session), request_timeout_seconds=BOUND
	)

	def probe () -> dict[str, bool]:
		"""Fail with something carrying no state at all."""

		raise sqlalchemy.exc.OperationalError("SELECT 1", {}, Exception("no state here"))

	application.get("/probe-568-stateless")(probe)

	assert api_support.call(application, "GET", "/probe-568-stateless").status_code == 500
