"""Liveness and readiness, the two questions a deployment asks before sending traffic.

They are deliberately different questions, and answering them with the same check is a
common way to turn a brief database hiccup into an outage (SPEC.md §8.6):

* **Liveness** — is this process still working? It touches nothing external, because a
  liveness probe that fails when the database does gets the container *killed* rather
  than taken out of rotation, and killing it does not bring the database back.
* **Readiness** — can this instance serve a request right now? That means the database is
  reachable *and* its schema is the one this code was written against. An instance whose
  migrations have not been run answers queries against columns that are not there, and it
  should be held back rather than allowed to fail one request at a time.

Both are unauthenticated: whatever is probing them runs before any credential exists, and
neither reveals anything an unauthenticated caller could not learn by connecting.
"""

import typing

import fastapi
import sqlalchemy.exc
import starlette.requests

import subroutine
import subroutine.api.routing
import subroutine.db.migrate
import subroutine.errors

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
		# The cause is reported rather than hidden: this endpoint exists to be read by
		# whoever is trying to get the instance running, and "not ready" on its own sends
		# them to the logs for something the probe already knows.
		raise subroutine.errors.ServiceUnavailable(
			f"The database cannot be reached: {getattr(error, 'orig', None) or error}",
			hint="Check that the database is running and that 'database_url' is right.",
		) from error

	# The same three-way decision the CLI makes, from the same function. A monitoring alert
	# quotes this endpoint, so a remedy that differs from the one a person is given at the
	# terminal — or that cannot be followed at all — is worse here than anywhere.
	mismatch = subroutine.db.migrate.mismatch_reason(revision, expected)

	if mismatch is not None:
		detail, hint = mismatch

		raise subroutine.errors.ServiceUnavailable(detail, hint=hint)

	return {
		"status": "ready",
		"api_version": subroutine.API_VERSION,
		"schema_revision": revision,
	}
