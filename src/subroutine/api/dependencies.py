"""What an endpoint is handed: configuration, and a session for one unit of work.

Kept small on purpose. An endpoint that needs anything else should be asking a service for
it, not assembling it out of dependencies (SPEC.md §8.1).
"""

import typing

import fastapi
import sqlalchemy.orm
import starlette.requests

import subroutine.config
import subroutine.db.session


def settings (request: starlette.requests.Request) -> subroutine.config.Settings:
	"""Return the settings this instance was started with."""

	resolved = request.app.state.settings

	assert isinstance(resolved, subroutine.config.Settings)

	return resolved


def session (
	request: starlette.requests.Request,
) -> typing.Iterator[sqlalchemy.orm.Session]:
	"""Yield a session for this request, committing it if the request succeeds.

	One transaction per request, so a mutation and the event recording it either both
	happen or neither does. Anything raised in the endpoint — including a service refusing
	the change — reaches this generator and rolls the whole thing back before the failure
	is rendered.
	"""

	factory = request.app.state.session_factory

	with subroutine.db.session.session_scope(factory) as opened:
		yield opened


#: Written as annotations so an endpoint reads ``session: SessionDep`` rather than
#: repeating the ``Depends`` call at every one of them.
SettingsDep = typing.Annotated[subroutine.config.Settings, fastapi.Depends(settings)]
SessionDep = typing.Annotated[sqlalchemy.orm.Session, fastapi.Depends(session)]
