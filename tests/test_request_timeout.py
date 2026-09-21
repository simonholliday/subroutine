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

import pathlib
import sqlite3
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
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.db.failures
import subroutine.db.session
import subroutine.errors
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


def _a_real_busy_error (path: pathlib.Path) -> sqlalchemy.exc.OperationalError:
	"""Return the exception a genuinely busy SQLite hands SQLAlchemy — `SR#3117`.

	**A real one rather than a stand-in**, unlike :func:`_raised` above, and the difference
	matters here: what this translation reads is an attribute the *driver* sets and SQLAlchemy's
	wrapper carries through ``.orig``. A fake with ``sqlite_errorname`` written on it would pass
	whether or not either of those is where the value really lives, which is the whole of what
	could be wrong.

	The busy timeout is lowered for the duration, because the shipped five seconds is a number
	this test would otherwise wait out for no gain.
	"""

	engine = subroutine.db.session.create_engine(f"sqlite+pysqlite:///{path}")

	with engine.begin() as connection:
		connection.exec_driver_sql("CREATE TABLE waiting (n INTEGER)")

	holder = sqlite3.connect(path, timeout=0, isolation_level=None)
	holder.execute("BEGIN EXCLUSIVE")

	try:
		with engine.connect() as connection:
			connection.exec_driver_sql("PRAGMA busy_timeout=50")
			connection.exec_driver_sql("INSERT INTO waiting VALUES (1)")
			connection.commit()

	except sqlalchemy.exc.OperationalError as error:
		return error

	finally:
		holder.close()
		engine.dispose()

	raise AssertionError("SQLite did not report a busy database, so this proves nothing")


def test_a_busy_database_is_recognised_from_what_sqlite_called_it (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#3117`. Keyed on the error *name*, which is SQLite's contract, not on its prose.

	``SQLITE_BUSY`` and ``SQLITE_LOCKED`` say "database is locked" and "database table is
	locked" — two sentences for two conditions — so a translation reading the message would
	have to know both spellings and would still be matching a string SQLite may reword.
	"""

	answer = subroutine.db.failures.busy(_a_real_busy_error(tmp_path / "busy.db"))

	assert answer is not None, (
		"a genuinely busy database was not recognised as one — the attribute this reads is "
		"not where it is being looked for"
	)
	assert answer.CODE == "database_busy", answer.CODE


def test_the_local_client_reports_a_busy_database_as_busy_rather_than_as_unreachable (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#3117`, the defect a guide chapter met and the third narrower case at this one site.

	A busy database **is** reachable: it answered, and what it said was that it was busy. The
	generic branch reported *"<connection> could not be read: database is locked"* under a hint
	to go and check ``database_url`` — a cause nobody had established, about a call that was
	usually a *write*, and advice an agent could do nothing with.
	"""

	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="guide"),
		subroutine.config.Settings(dev_mode=True),
		session_factory=None,
	)

	with pytest.raises(subroutine.errors.DatabaseBusy) as refused, client._reported():
		raise _a_real_busy_error(tmp_path / "busy.db")

	assert "busy" in refused.value.detail, refused.value.detail

	# The three faults it replaces, each asserted rather than assumed gone.
	assert "could not be read" not in refused.value.detail, refused.value.detail
	assert "database_url" not in (refused.value.hint or ""), refused.value.hint
	assert "reachable" not in (refused.value.hint or ""), refused.value.hint


def test_an_agent_meeting_a_busy_database_is_told_to_try_again (
	tmp_path: pathlib.Path,
) -> None:
	"""The report's own complaint: *"not a refusal it can act on"*.

	A refusal here is meant to be a lesson — name what happened, then what would work. For a
	condition that clears by itself, *try again* is both, and it is what the old wording lacked.
	"""

	answer = subroutine.db.failures.busy(_a_real_busy_error(tmp_path / "busy.db"))

	assert answer is not None
	assert "again" in (answer.hint or "").lower(), answer.hint


def test_a_busy_refusal_claims_no_bound_and_reports_a_measured_one (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#1077` read precisely: do not *assert* a bound, and do report what you measured.

	``busy_timeout`` is five seconds, so *"after five seconds"* would be easy and often right —
	and a claim either way, because SQLite does not consult it in every case it reports this
	way. A figure the caller **timed** is the opposite: it establishes rather than assumes.

	`SR#3117` is why the second half matters. Whether a refusal came back at once or after a
	full timeout separates two unrelated causes, and three rounds of investigation could not
	answer it because nothing timed the call that failed.
	"""

	failed = _a_real_busy_error(tmp_path / "busy.db")

	unmeasured = subroutine.db.failures.busy(failed)

	assert unmeasured is not None
	assert "second" not in unmeasured.detail, (
		f"a duration nobody measured was named: {unmeasured.detail}"
	)
	assert "second" not in (unmeasured.hint or ""), unmeasured.hint

	measured = subroutine.db.failures.busy(failed, waited=4.93)

	assert measured is not None
	assert "4.93 seconds" in measured.detail, (
		f"the caller timed this attempt and the refusal did not say so: {measured.detail}"
	)


def test_a_busy_refusal_from_the_local_client_says_how_long_it_took (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#3117`, and the reason the timer is in the client rather than in the translation.

	``db/failures`` is handed an exception and has no idea when the operation began; the client
	that opened the session does. Driving it through ``_reported`` is what shows the two ends
	are actually joined — a timer started and never passed on would leave this refusal silent
	while every unit test on the translation went on passing.
	"""

	client = subroutine.clients.local.Client(
		subroutine.connections.Connection(name="guide"),
		subroutine.config.Settings(dev_mode=True),
		session_factory=None,
	)

	with pytest.raises(subroutine.errors.DatabaseBusy) as refused, client._reported():
		raise _a_real_busy_error(tmp_path / "busy.db")

	assert "seconds" in refused.value.detail, (
		f"the client timed nothing, so the refusal cannot say whether it waited: "
		f"{refused.value.detail}"
	)


def test_neither_backend_s_translation_answers_for_the_other (
	tmp_path: pathlib.Path,
) -> None:
	"""Two vocabularies, asked in turn rather than merged — and each must decline the other.

	PostgreSQL reports giving up in a SQLSTATE and SQLite reports a busy database in an error
	name, and neither exception carries the other's. If either translation answered for both,
	the caller would be told the wrong thing in the one place there is no second opinion.
	"""

	really_busy = _a_real_busy_error(tmp_path / "busy.db")

	assert subroutine.db.failures.gave_up(really_busy) is None, (
		"a busy SQLite was reported as a request this instance gave up waiting for"
	)
	assert subroutine.db.failures.busy(_raised("57014")) is None, (
		"a PostgreSQL statement that was given up on was reported as a busy database"
	)
	assert subroutine.db.failures.busy(_raised("40P01")) is None


def test_a_served_instance_reports_a_busy_database_as_busy_and_not_as_a_bug (
	engine: sqlalchemy.engine.Engine, tmp_path: pathlib.Path
) -> None:
	"""`SR#3117`, on the transport the report did not mention and which had the same fault.

	**Two surfaces meet this condition and neither answers for the other** — which is `SR#1070`'s
	own argument for `db/failures` existing. A served SQLite instance met a busy database
	through this handler, where every `OperationalError` that is not a PostgreSQL SQLSTATE was
	handed on unchanged, and so reported a 500 blaming this program for a database that was
	working perfectly and said so.

	The engine is taken only to build an application; the failure is a real busy SQLite raised
	inside the route, because the question is what the *handler* does with one.
	"""

	failed = _a_real_busy_error(tmp_path / "busy.db")
	application = api_support.build_app(subroutine.db.session.create_session_factory(engine))

	def probe () -> dict[str, bool]:
		"""Meet a database that was busy, exactly as a handler writing to one would."""

		raise failed

	application.get("/probe-3117")(probe)

	answer = api_support.call(application, "GET", "/probe-3117")

	assert answer.status_code == 503, answer.status_code
	assert answer.json()["code"] == "database_busy", answer.json()
	assert "request id" not in (answer.json().get("hint") or ""), answer.json()


def test_an_agent_is_told_a_database_was_busy_rather_than_shown_the_sql (
	tmp_path: pathlib.Path,
) -> None:
	"""`SR#3117`, and the third surface - the one where falling through costs the most.

	**Found by asking what else read the translation, not by the report.** `SR#1070` put the
	PostgreSQL case here because `str(failure)` on SQLAlchemy's wrapper is the statement, **the
	bound parameters** and a link to its website - somebody's data in a model's context. SQLite
	reports a busy database under an error name rather than a SQLSTATE, so `gave_up` declines
	it and it fell through to exactly that, for a condition that clears by itself.
	"""

	failed = _a_real_busy_error(tmp_path / "busy.db")
	answer = subroutine.mcp.protocol._explained(failed, _ANY_TOOL)

	assert "busy" in answer, answer
	assert "again" in answer.lower(), (
		f"the agent was told what happened and not what to do about it:\n{answer}"
	)

	# The leak this block exists to prevent, asserted rather than assumed absent.
	assert "INSERT INTO" not in answer, answer
	assert "sqlalche.me" not in answer, answer
