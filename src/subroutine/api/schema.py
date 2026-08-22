"""Refusing a write when this build and its database disagree about the schema (`#973`).

**Measured on the served instance**, in the window between new code being deployed and
``subroutine db upgrade`` being run: ``/readyz`` answered 503 naming both revisions, ``POST
/mcp`` refused with the same sentence, and ``POST /v1/tasks/916/comments`` **succeeded**. One
instance, one mismatch, three different answers.

``db.migrate.mismatch_reason`` had exactly two callers — ``api/health``, which is ``/readyz``,
and ``clients/local``. So no API request path asked, and the MCP tools refused only as a side
effect of `#539`: since then they run server-side and reach the database through the local
client, inheriting a check the endpoint beside them did not have. Not two copies of a rule
disagreeing but the older shape — **one rule applied to one side of a pair** (`#149`).

**Simon's decision of 2026-08-22 is to serve reads and refuse writes**, against refusing to
start at all. Refusing to start takes ``/readyz``'s sentence away — the operator gets a
connection refused and has to reach the journal — stops somebody looking something up, and
breaks a serial per-tenant fleet upgrade, which has to reach an instance to verify it. And a
guard that makes a mistimed deploy a total outage is the one people work around.

**The limit is real and is published rather than closed** (``docs/hosting.md``): this stops you
writing on top of a wrong answer, not being given one. Six of nineteen migrations backfill, and
when one has not run the column exists and is simply unpopulated — so a read is a plausible,
complete, wrong answer and this permits it. ``subroutine db upgrade`` is the supported path and
``/readyz`` is how you find out you are not on it.
"""

import typing

import fastapi
import sqlalchemy.exc
import starlette.requests

import subroutine.api.security
import subroutine.db.migrate
import subroutine.errors

#: Writes that are checked anyway, and why each is not one.
#:
#: Keyed ``"METHOD path"`` off the route that matched, exactly as ``api/query.NOT_REFUSED`` is
#: keyed and for its reason: comparing ``request.url.path`` would measure a literal against a
#: parameterised pattern and never match, so every exception would silently stop applying.
#:
#: **Two entries, and the second is the one that matters.** Refusing a backup because the schema
#: is wrong takes away the thing an operator most wants at exactly that moment — and it reads
#: nothing it could corrupt.
NOT_A_WRITE: dict[str, str] = {
	"POST /v1/recurrence/parse": "reads a phrase back and touches no table",
	"POST /v1/admin/backups": "copies the database rather than changing it, and is what "
	"somebody reaches for first when a deploy has gone wrong",
}


def refuse_a_write_against_a_schema_this_build_does_not_expect (
	request: starlette.requests.Request,
) -> None:
	"""Refuse anything that is not a read while the database is not the expected revision.

	**The method is the definition and it already existed.** ``security.SAFE_METHODS`` has
	decided what counts as a write since `#639`, in the resolver chain every credentialed route
	passes through, so this is not a second list of writing routes — it is the same rule asked
	a second question. A list is what `#676` and `#897` both fell behind.

	A failure to read the revision is a **no-op**, deliberately: this is a courtesy check, and a
	database that cannot be reached at all will fail the request it was going to fail anyway,
	with the driver's own words rather than with a sentence about migrations.
	"""

	if request.method.upper() in subroutine.api.security.SAFE_METHODS:
		return

	route: typing.Any = request.scope.get("route")
	path = getattr(route, "path", None)

	if path is not None and f"{request.method} {path}" in NOT_A_WRITE:
		return

	mismatch = _disagreement(request.app)

	if mismatch is None:
		return

	detail, hint = mismatch

	raise subroutine.errors.SchemaMismatch(detail, hint=hint)


def _disagreement (application: fastapi.FastAPI) -> tuple[str, str] | None:
	"""Return what to say about this database's schema, or ``None`` when it is the right one.

	**Latched when it agrees and re-read while it does not**, which is not symmetry for its own
	sake. Agreement cannot change under a running process without somebody migrating, and they
	are told to stop the service first — so caching it makes the ordinary cost one query for the
	life of the process. Disagreement *can* be resolved under a live service, and re-reading is
	what lets ``db upgrade`` put an instance back into use without a restart. The cost is one
	query per write, paid only while the instance is refusing them anyway.

	Not read at startup, for `#179`'s reason: the database may not be up yet, which is the whole
	reason ``/readyz`` exists. ``state.serving_instance`` is latched the same way and says so.
	"""

	if getattr(application.state, "schema_agrees", False):
		return None

	factory = application.state.session_factory

	if factory is None:
		return None

	try:
		with factory() as opened:
			revision = subroutine.db.migrate.revision_on(opened.connection())

	except sqlalchemy.exc.SQLAlchemyError:
		return None

	mismatch = subroutine.db.migrate.mismatch_reason(revision, application.state.schema_head)

	if mismatch is None:
		application.state.schema_agrees = True

	return mismatch


SchemaDep = fastapi.Depends(refuse_a_write_against_a_schema_this_build_does_not_expect)
