"""What a database did when it stopped waiting, in words a caller can act on — `#1070`.

**Here rather than in ``api/problems``, because two surfaces meet the same condition.** Since
`#568` every request runs on a session bounded by ``statement_timeout``, and since `#539` the
MCP tools run *inside* the served instance on that same session factory — so a lock, a deadlock
or a statement given up on arrives in a tool call exactly as it arrives in a request. HTTP
callers were told ``request_timed_out`` with a remedy; an agent was handed ``str(failure)``,
which is SQLAlchemy's own text: the statement, the bound parameters and a link to its website.

Somebody's data in an agent's context is the part that decides this is not merely untidy.

**Keyed on SQLSTATE rather than on the message**, which is localised and which `#568` is
precisely about not reading twice.
"""

import subroutine.errors

#: What PostgreSQL calls each way of giving up, and what this instance tells the caller it was
#: waiting for.
#:
#: **``57014`` is not only a timeout, which is why the wording does not claim it is.** The same
#: state answers a statement an operator cancelled with ``pg_cancel_backend``, so this says the
#: request was given up on and leaves the cause where the database put it — the rule this
#: project records and has broken three times.
GAVE_UP: dict[str, str] = {
	"57014": "was given up on before it finished",
	"55P03": "waited for something another transaction was holding, and was given up on",
	"40P01": "and another were each waiting for what the other held, so this one was stopped",
}

#: The one state ``request_timeout_seconds`` actually bounds, and therefore the only one whose
#: refusal may name it — `#1077`.
#:
#: **The other two are bounded by something else and the message used to claim otherwise.** A
#: deadlock is detected at PostgreSQL's own ``deadlock_timeout``, and ``55P03`` is
#: ``lock_timeout``, which :mod:`subroutine.db.session` **deliberately does not set** and
#: writes down why. So *"after 30 seconds"* was a number that had nothing to do with either —
#: and read *"after 0 seconds"* on an instance with the bound turned off.
#:
#: A refusal naming a cause it has not established is this project's recorded worst case. This
#: is the same fault one field along: the cause was right and the *bound* was invented.
BOUNDED_BY_THE_REQUEST_TIMEOUT = frozenset({"57014"})

#: What SQLite calls a database it could not get into, and what this instance tells the caller
#: was in the way - `#3117`.
#:
#: **Keyed on the name rather than on the message, which is `GAVE_UP`'s rule one backend over.**
#: These two codes say *"database is locked"* and *"database table is locked"* - two sentences
#: for two conditions - so reading the prose would have to know both spellings and would still
#: be matching a string SQLite is free to reword. ``sqlite_errorname`` arrived in Python 3.11,
#: which is this project's floor.
#:
#: **Neither says *process*, and the first draft of this said it** - `#3117`. SQLite defines
#: `SQLITE_BUSY` as concurrent activity by *"some other database connection, **usually** a
#: database connection in a separate process"*, and two connections inside one process produce
#: it exactly as readily as two processes do. Saying *process* named a cause nobody had
#: established, which is this project's recorded worst kind of refusal - and it did real harm
#: within hours, sending somebody hunting for a second process on a machine that runs one.
#:
#: **PostgreSQL cannot reach here and SQLite cannot reach `GAVE_UP`**: one reports a SQLSTATE
#: and the other an error name, and neither carries the other's. So the two are asked in turn
#: rather than merged, and each answers ``None`` for the backend that is not its own.
BUSY: dict[str, str] = {
	"SQLITE_BUSY": "another connection was writing to it",
	"SQLITE_LOCKED": "something sharing this connection's cache held a table in it",
}


def sqlstate (exception: BaseException) -> str:
	"""Return the five-character state the database reported, or the empty string.

	Read off the driver's own exception rather than off SQLAlchemy's wrapper, and defensively:
	a driver that names it something else should cost this translation rather than every
	database failure, which would then reach the caller as a crash inside an error handler.
	"""

	original = getattr(exception, "orig", None)

	return str(getattr(original, "sqlstate", "") or "")


def errorname (exception: BaseException) -> str:
	"""Return what SQLite called this, or the empty string.

	:func:`sqlstate`'s counterpart, and defensive for the same reason: a driver that does not
	carry the attribute should cost this translation rather than every database failure, which
	would otherwise reach the caller as a crash inside an error handler.
	"""

	original = getattr(exception, "orig", None)

	return str(getattr(original, "sqlite_errorname", "") or "")


def busy (
	exception: BaseException, *, waited: float | None = None
) -> subroutine.errors.DatabaseBusy | None:
	"""Return the refusal for a database that was busy, or ``None`` for anything else.

	**The configured bound is never named and a measured one always is**, which is `#1077`
	read precisely rather than loosely. That lesson forbids *asserting* a bound nobody
	established - ``busy_timeout`` is five seconds, so *"after five seconds"* would usually be
	right and would be a claim either way, because SQLite does not consult it in every case it
	reports this way. ``waited`` is the opposite of that: it is how long this attempt actually
	took, measured by the caller that made it, so saying it establishes rather than assumes.

	**It is also the number a failure three surfaces away could not otherwise produce** -
	`#3117`, where whether a refusal came back at once or after a full timeout separates two
	unrelated causes, and nobody could answer it because nothing timed the call that failed.

	**What it does claim is that nothing changed**, which is established rather than assumed:
	the statement never took its lock, so the transaction around it has nothing in it to undo.
	"""

	said = BUSY.get(_primary(errorname(exception)))

	if said is None:
		return None

	took = "" if waited is None else f" This attempt was refused after {waited:.2f} seconds."

	# **What the request changed, and nothing about who else was writing** (the cold review of
	# 2026-09-21, `#3153`). This said *nothing was changed by this* and *the ordinary way two
	# processes take turns*: the second is the word `8f591dc` took out of the detail, one field
	# along, and the first is true of one request and was read as true of a whole operation. A
	# request's transaction is undone when it is refused, so that much is established; a caller
	# that made several says which of them went through.
	return subroutine.errors.DatabaseBusy(
		f"The database was busy: {said}.{took}",
		hint="This request changed nothing. Try it again - a busy database clears on its own.",
	)


def _primary (name: str) -> str:
	"""Return the primary name an extended SQLite error name belongs to - `#3153`.

	**``sqlite_errorname`` is the extended name**, so a WAL snapshot conflict arrives as
	``SQLITE_BUSY_SNAPSHOT`` (517, measured) and fell past :data:`BUSY` to the message this
	replaced - *could not be read, check database_url* - on the local client, a 500 over HTTP.
	An extended name is its primary with a suffix, which is SQLite's own naming.
	"""

	for primary in BUSY:
		if name == primary or name.startswith(f"{primary}_"):
			return primary

	return name


def gave_up (
	exception: BaseException, *, seconds: int | None = None
) -> subroutine.errors.RequestTimedOut | None:
	"""Return the refusal for a database that stopped waiting, or ``None`` for anything else.

	**Every other ``OperationalError`` is somebody else's to report.** That class is most of
	what a database can raise — a connection dropped, a disk full, a database shut down
	underneath us — and none of those is this. Answering ``None`` rather than guessing keeps
	them going to whichever handler already logs them with their request id.

	``seconds`` is the bound in force where the caller can know it, and is named only for the
	state it actually bounds. A surface that does not hold the settings passes nothing and the
	sentence simply does not claim a number, which is better than claiming the wrong one.
	"""

	said = GAVE_UP.get(sqlstate(exception))

	if said is None:
		return None

	if seconds is not None and sqlstate(exception) in BOUNDED_BY_THE_REQUEST_TIMEOUT:
		said = f"{said}, after {seconds} seconds"

	return subroutine.errors.RequestTimedOut(
		f"This request {said}.",
		hint=(
			"Nothing was changed by it. Retrying may work; if it does not, ask for less "
			"in one request — a narrower filter, a smaller page, or one item rather than "
			"a listing."
		),
	)
