"""What an endpoint is handed: configuration, and a session for one unit of work.

Kept small on purpose. An endpoint that needs anything else should be asking a service for
it, not assembling it out of dependencies (docs/design.md §8.1).
"""

import typing

import fastapi
import sqlalchemy.orm
import starlette.requests

import subroutine.config
import subroutine.errors


def settings (request: starlette.requests.Request) -> subroutine.config.Settings:
	"""Return the settings this instance was started with."""

	resolved = request.app.state.settings

	assert isinstance(resolved, subroutine.config.Settings)

	return resolved


#: Where the request's session is parked so :class:`subroutine.api.routing.Transactional`
#: can commit it. A dependency cannot do the committing itself — see below.
SESSION_STATE = "session"


def session (
	request: starlette.requests.Request,
) -> typing.Iterator[sqlalchemy.orm.Session]:
	"""Yield a session for this request. **The commit is not here, deliberately.**

	One transaction per request, so a mutation and the event recording it either both
	happen or neither does. Anything raised in the endpoint — including a service refusing
	the change — reaches this generator and rolls the whole thing back before the failure
	is rendered.

	**Committing here would commit after the response had already been sent.** FastAPI
	closes a request's dependency exit stack *after* the application has emitted the
	response, which was measured rather than assumed: a probe recording the order printed
	``handler body`` → ``response left the app`` → ``dependency exit``. Two things follow,
	and the second is the serious one:

	* A client that writes and immediately reads can beat its own commit. That is how this
	  was found — one read of an item's history missed an event the previous request had
	  just written, and the row was in the database afterwards.
	* **A commit that fails would fail after the caller had been told it succeeded.** A
	  ``201`` whose transaction then rolls back is silent data loss reported as success,
	  and no amount of client care can defend against it.

	So the commit moved to :class:`subroutine.api.routing.Transactional`, which runs
	between the handler returning and the response being sent. This function keeps the
	rollback and the close, because both are still right at teardown.
	"""

	# **Refused here, where the request first needs a database** (`#698`). This is not an
	# unexpected failure and the API was treating it as one: the session opens, the
	# endpoint touches it, SQLite reports a file it cannot open, and the unhandled-error
	# handler writes a stack trace for a condition `Settings.has_no_instance_yet` names
	# exactly. Asking costs one `stat` and only ever answers true for SQLite, so a served
	# PostgreSQL instance is untouched — a database that cannot be reached might be
	# absent, asleep or firewalled, and guessing is how confident bad advice gets given.
	# **Only when this application built its own engine.** `create_app` leaves
	# `state.engine` as `None` when a caller supplies a session factory, which is bound to
	# a database of its own — so `sqlite_path` describes somewhere nothing is reading and
	# the file being absent means nothing at all. Without this the refusal fired for every
	# request in the suite: 1,242 failures, found by running rather than by reading.
	if request.app.state.engine is not None and settings(request).has_no_instance_yet():
		raise subroutine.errors.no_instance_yet()

	factory = request.app.state.session_factory
	opened: sqlalchemy.orm.Session = factory()

	setattr(request.state, SESSION_STATE, opened)

	try:
		yield opened

	except Exception:
		opened.rollback()

		raise

	finally:
		# A session still in a transaction here was never committed by the route class,
		# which means the request failed on its way out. Rolling back is the safe reading
		# and matches what the old `session_scope` did with anything that raised.
		if opened.in_transaction():
			opened.rollback()

		opened.close()


#: Written as annotations so an endpoint reads ``session: SessionDep`` rather than
#: repeating the ``Depends`` call at every one of them.
SettingsDep = typing.Annotated[subroutine.config.Settings, fastapi.Depends(settings)]
SessionDep = typing.Annotated[sqlalchemy.orm.Session, fastapi.Depends(session)]
