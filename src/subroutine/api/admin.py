"""Operational endpoints for whoever runs the instance (docs/design.md §12.6).

**Backup is here and restore deliberately is not.** An agent about to attempt something bulk
should be able to snapshot first, which is why taking a backup is reachable over HTTP. Putting
one back replaces the database the serving process currently has open — an endpoint that pulls
the floor out from under its own request is not a feature, and §12.4's recovery property
depends on the administrative commands working when the service will *not* start. Restore is
``subroutine db restore``, and only that.

Both endpoints require ``instance:admin``, which no role may carry: it is held only by a
superuser, and a token still narrows it (§7.3).
"""

import datetime

import fastapi
import pydantic
import sqlalchemy.orm

import subroutine.api.dependencies
import subroutine.api.routing
import subroutine.api.security
import subroutine.db.backup
import subroutine.domain.authorization
import subroutine.permissions

router = fastapi.APIRouter(
	prefix="/v1/admin",
	tags=["admin"],
	route_class=subroutine.api.routing.Transactional,
)


class Backup(pydantic.BaseModel):
	"""One copy of the database, described well enough to choose between several.

	**The name identifies it and the server's path is deliberately not reported.**
	A caller over HTTP is somewhere else: they cannot open the file, and there is no endpoint
	that takes a path — §12.4 gives restore none on purpose, so the one thing a reader might
	do with it is the one thing they cannot. What it does say is where this instance keeps its
	data, to somebody who by definition is not on that machine.

	``subroutine db backup`` still prints the path, and should: it runs beside the file.
	"""

	name: str
	taken_at: datetime.datetime
	schema_head: str
	size_bytes: int

	#: Null for the default instance, which has no profile name (docs/design.md §12.5).
	profile: str | None


class Backups(pydantic.BaseModel):
	"""Every backup this instance holds, newest first."""

	items: list[Backup]


def _rendered (backup: subroutine.db.backup.Backup) -> Backup:
	"""Describe a backup for a caller who is not on the machine holding it."""

	return Backup(
		name=backup.name,
		taken_at=backup.taken_at,
		schema_head=backup.schema_head,
		size_bytes=backup.size_bytes,
		profile=backup.profile,
	)


@router.post("/backups", response_model=Backup, status_code=201)
def create_backup (
	actor: subroutine.api.security.PrincipalDep,
	settings: subroutine.api.dependencies.SettingsDep,
	session: subroutine.api.dependencies.SessionDep,
	keep: int | None = fastapi.Body(
		None,
		embed=True,
		description="Afterwards, keep only this many of the newest backups.",
	),
) -> Backup:
	"""Take a datetime-stamped copy of the database and report what it is called."""

	subroutine.domain.authorization.authorize_instance(
		actor, subroutine.permissions.INSTANCE_ADMIN
	)

	return _rendered(subroutine.db.backup.take(_engine_behind(session), settings, keep=keep))


def _engine_behind (session: sqlalchemy.orm.Session) -> sqlalchemy.engine.Engine:
	"""Return the engine this session ultimately talks through.

	The engine behind the *session*, rather than a second one built from the configured URL: a
	backup taken over a different connection than the application serves from is a backup of a
	database that may not be the one being served.

	``get_bind`` answers with an ``Engine`` normally and with a ``Connection`` when something
	has bound one — which the test harness does, so that a request shares the test's
	transaction. Both have an engine behind them and it is the same engine either way.
	"""

	bind = session.get_bind()

	if isinstance(bind, sqlalchemy.engine.Connection):
		return bind.engine

	return bind


@router.get("/backups", response_model=Backups)
def list_backups (
	actor: subroutine.api.security.PrincipalDep,
	settings: subroutine.api.dependencies.SettingsDep,
) -> Backups:
	"""List the backups this instance holds, newest first."""

	subroutine.domain.authorization.authorize_instance(
		actor, subroutine.permissions.INSTANCE_ADMIN
	)

	return Backups(items=[_rendered(found) for found in subroutine.db.backup.catalogue(settings)])
