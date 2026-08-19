"""Liveness and readiness, the two questions a deployment asks before sending traffic.

They are deliberately different questions, and answering them with the same check is a
common way to turn a brief database hiccup into an outage (docs/design.md §8.6):

* **Liveness** — is this process still working? It touches nothing external, because a
  liveness probe that fails when the database does gets the container *killed* rather
  than taken out of rotation, and killing it does not bring the database back.
* **Readiness** — can this instance serve a request right now? That means the database is
  reachable *and* its schema is the one this code was written against. An instance whose
  migrations have not been run answers queries against columns that are not there, and it
  should be held back rather than allowed to fail one request at a time.

Both are unauthenticated: whatever is probing them runs before any credential exists.

**What a failure says depends on who can reach this instance** (`#832`). While nothing outside
the machine can, ``/readyz`` reports the driver's own error — an unreachable host, a refused
connection, a database path — because the reader is the person trying to get the instance
running and "not ready" on its own sends them to the logs for something the probe already
knows. Once ``public_url`` is set, that same failure is a generic refusal and the cause goes to
the log instead: an internal hostname, a database name or a filesystem path is not something a
stranger could learn by connecting, and this file used to claim it was.
"""

import logging
import typing

import fastapi
import sqlalchemy.exc
import starlette.requests

import subroutine
import subroutine.api.routing
import subroutine.config
import subroutine.db.migrate
import subroutine.domain.instances
import subroutine.errors

#: The same logger the rest of the API writes to, so an operator following a served instance
#: sees the cause `/readyz` stopped telling the caller (`#832`).
_logger = logging.getLogger("subroutine.api")

router = fastapi.APIRouter(
	tags=["health"],
	route_class=subroutine.api.routing.Transactional,
)


@router.get("/healthz", summary="Is this process alive?")
def liveness () -> dict[str, str]:
	"""Report that the process is running and able to answer."""

	return {"status": "ok", "api_version": subroutine.API_VERSION}


@router.get("/readyz", summary="Can this instance serve requests?")
def readiness (request: starlette.requests.Request) -> dict[str, typing.Any]:
	"""Report whether the database is reachable and its schema is up to date."""

	factory = request.app.state.session_factory
	expected = request.app.state.schema_head

	try:
		with factory() as opened:
			revision = subroutine.db.migrate.revision_on(opened.connection())

	except sqlalchemy.exc.SQLAlchemyError as error:
		# The cause is reported rather than hidden *while only this machine can read it*: the
		# endpoint exists for whoever is trying to get the instance running, and "not ready"
		# alone sends them to the logs for something the probe already knows. Once anybody can
		# reach it the driver's error is an internal hostname, a database name or a filesystem
		# path, so it goes to the log and the caller gets the fact without the detail (`#832`).
		settings = request.app.state.settings
		cause = getattr(error, "orig", None) or error

		if subroutine.config.reachable_by_strangers(settings, host=settings.host):
			_logger.warning("readiness check failed: %s", cause)

			raise subroutine.errors.ServiceUnavailable(
				"This instance is not ready to serve requests.",
				hint="Check that the database is running and that 'database_url' is right.",
			) from error

		raise subroutine.errors.ServiceUnavailable(
			f"The database cannot be reached: {cause}",
			hint="Check that the database is running and that 'database_url' is right.",
		) from error

	# The same three-way decision the CLI makes, from the same function. A monitoring alert
	# quotes this endpoint, so a remedy that differs from the one a person is given at the
	# terminal — or that cannot be followed at all — is worse here than anywhere.
	mismatch = subroutine.db.migrate.mismatch_reason(revision, expected)

	if mismatch is not None:
		detail, hint = mismatch

		raise subroutine.errors.ServiceUnavailable(detail, hint=hint)

	_refuse_a_database_that_has_been_replaced(request)

	return {
		"status": "ready",
		"api_version": subroutine.API_VERSION,
		"schema_revision": revision,
	}


def _refuse_a_database_that_has_been_replaced (
	request: starlette.requests.Request,
) -> None:
	"""Report not-ready when the instance underneath this process is no longer the same one.

	`#179`. **A reachable connection is not readiness, and this endpoint was calling it that.**
	A serving process whose database file has been replaced keeps its descriptors on the
	unlinked file, so its reads succeed against data nobody else can see and every probe
	answers 200 — including this one. The clean-room sysadmin who found it used ``/readyz`` to
	confirm a restore had worked, and it told them yes.

	`#171` closed the route that produced it: ``db restore`` refuses while anything holds the
	database. What is left is everything out of band — an operator with ``cp``, a volume
	remount, a restore run with ``--force`` — and the claim was wrong regardless of how it was
	reached.

	**Latched on the first reading rather than at startup**, because the database may well not
	be up when the process starts; that is what this endpoint is for. So the identity is taken
	the first time it can be seen and compared on every check after.

	**A clone is a change and is meant to fail here.** ``db restore --as-clone`` mints a new
	identity deliberately, so a process left running over one is serving something that is no
	longer what agents and configuration refer to, and it should be restarted. A ``--recover``
	restore keeps the identity and passes, which is the split §12.6a already draws.

	Nothing is latched while there is no instance row, so a process started before
	``subroutine init`` becomes ready when the instance appears rather than being pinned to its
	absence.
	"""

	factory = request.app.state.session_factory

	with factory() as opened:
		instance = subroutine.domain.instances.get(opened)

	if instance is None:
		return

	now = str(instance.id)
	known = getattr(request.app.state, "serving_instance", None)

	if known is None:
		request.app.state.serving_instance = now

		return

	if known == now:
		return

	# Logged whoever can reach this, unlike `#832`'s driver errors: an instance id is this
	# installation's own published identity — `/v1/meta` carries it — so it discloses nothing,
	# and it is the one fact that says which database is which.
	_logger.error(
		"the database underneath this process has been replaced: serving %s, started on %s",
		now,
		known,
	)

	raise subroutine.errors.ServiceUnavailable(
		f"This process started on instance {known} and the database now says {now}.",
		hint=(
			"The database was replaced underneath a running process, so what it is serving "
			"is not what anybody else can see. Restart the service. If this followed a "
			"'db restore --as-clone', that is expected — a clone is deliberately a new "
			"instance."
		),
	)
