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


def sqlstate (exception: BaseException) -> str:
	"""Return the five-character state the database reported, or the empty string.

	Read off the driver's own exception rather than off SQLAlchemy's wrapper, and defensively:
	a driver that names it something else should cost this translation rather than every
	database failure, which would then reach the caller as a crash inside an error handler.
	"""

	original = getattr(exception, "orig", None)

	return str(getattr(original, "sqlstate", "") or "")


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
