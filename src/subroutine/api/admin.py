"""Operational endpoints for whoever runs the instance (docs/design.md §12.6).

**Backup is here and restore deliberately is not.** An agent about to attempt something bulk
should be able to snapshot first, which is why taking a backup is reachable over HTTP. Putting
one back replaces the database the serving process currently has open — an endpoint that pulls
the floor out from under its own request is not a feature, and §12.4's recovery property
depends on the administrative commands working when the service will *not* start. Restore is
``subroutine db restore``, and only that.

**When each account last signed in is listed here too** (`SR#5111`, decision `#5097`), the other
listing for an administrator: it left the account view every co-member reads, since it says how
often somebody uses the browser.

The backups require ``instance:admin`` and the sign-ins ``instance:user_create``. No role may
carry either: each is held only by a superuser, and a token still narrows it (§7.3).
"""

import datetime

import fastapi
import pydantic

import subroutine.api.dependencies
import subroutine.api.pagination
import subroutine.api.routing
import subroutine.api.security
import subroutine.api.users
import subroutine.db.backup
import subroutine.domain.backups
import subroutine.domain.users
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1/admin",
	tags=["admin"],
	route_class=subroutine.api.routing.Transactional,
)


class Backup(pydantic.BaseModel):
	"""One copy of the database, described well enough to choose between several.

	**The name identifies it, and the listing does not report the server's path.**
	A caller over HTTP is somewhere else: they cannot open the file, and there is no endpoint
	that takes a path - restore deliberately has none, so the one thing a reader might
	do with it is the one thing they cannot. What it does say is where this instance keeps its
	data, to somebody who by definition is not on that machine.

	A failure names the folder it could not use - the backup folder, or the staging folder in the
	data directory - because putting that folder right is the one thing such a failure asks of
	whoever runs the installation, and only somebody who administers the installation reaches
	these routes.

	``subroutine db backup`` still prints the path, and should: it runs beside the file.
	"""

	name: str
	taken_at: datetime.datetime
	schema_head: str
	size_bytes: int

	#: Null for the default instance, which has no profile name (docs/design.md §12.5).
	profile: str | None

	#: What this copy was taken for — ``routine``, ``upgrade`` or ``restore`` — and **null for
	#: one taken before anything recorded it**, which is not the same as routine (`#1712`).
	#:
	#: Reported because it decides how long the copy lives, and a caller choosing between
	#: several has no other way to tell an operator's deliberate backup from a rollback point
	#: the program took by itself. Null is the answer that most needs saying: those are the
	#: copies that accumulated before any of this existed, they are treated as routine, and
	#: nothing removes them until somebody asks.
	taken_for: str | None = None


class Backups(pydantic.BaseModel):
	"""Every backup this instance holds, newest first, and every copy marked unfinished."""

	items: list[Backup]

	#: The copies in the backup folder still marked unfinished, by name (`#4409`): a take that
	#: stopped before its copy was proved, one still writing, or a good copy whose marker would not
	#: come away. **None is a backup**, so none is among ``items``. Nothing removes one by itself,
	#: and doing so is for whoever runs the installation, beside the file, where ``subroutine
	#: doctor`` gives the command.
	unfinished: list[str] = pydantic.Field(default_factory=list)


def _rendered (backup: subroutine.db.backup.Backup) -> Backup:
	"""Describe a backup for a caller who is not on the machine holding it."""

	return Backup(
		name=backup.name,
		taken_at=backup.taken_at,
		schema_head=backup.schema_head,
		size_bytes=backup.size_bytes,
		profile=backup.profile,
		taken_for=backup.taken_for,
	)


@router.post("/backups", response_model=Backup, status_code=201, summary="Take a backup")
def create_backup (
	actor: subroutine.api.security.PrincipalDep,
	settings: subroutine.api.dependencies.SettingsDep,
	session: subroutine.api.dependencies.SessionDep,
	keep: int | None = fastapi.Body(
		None,
		embed=True,
		# **Refused here, before anything is written** (`#3937`): pruning refused it too, but
		# only once the backup had been taken, so the caller was told no with a new copy on disk.
		ge=1,
		description=(
			"Afterwards, keep only this many of the newest routine backups. Copies taken "
			"before an upgrade or a restore are kept under their own rules and are not "
			"counted here."
		),
	),
) -> Backup:
	"""Take a datetime-stamped copy of the database and report what it is called."""

	return _rendered(subroutine.domain.backups.take(session, settings, keep=keep, actor=actor))


@router.get("/backups", response_model=Backups, summary="List the backups this instance holds")
def list_backups (
	actor: subroutine.api.security.PrincipalDep,
	settings: subroutine.api.dependencies.SettingsDep,
) -> Backups:
	"""List the backups this instance holds, newest first, and name any copy marked unfinished."""

	found, unfinished = subroutine.domain.backups.held(settings, actor=actor)

	return Backups(
		items=[_rendered(backup) for backup in found],
		unfinished=[copy.name for copy in unfinished],
	)


@router.get(
	"/sign-ins",
	summary="List when each account last signed in",
	response_model=subroutine.views.Collection[subroutine.views.SignIn],
)
def sign_ins (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	limit: int | None = fastapi.Query(
		None, description=subroutine.api.pagination.LIMIT_DESCRIPTION
	),
	cursor: str | None = fastapi.Query(None, description="Continue after a previous page."),
	include_total: bool = fastapi.Query(False, description="Count the whole result."),
) -> subroutine.views.Collection[subroutine.views.SignIn]:
	"""List every account on this instance and when it last signed in, oldest account first.

	Needs ``instance:user_create``, which no role carries and only a superuser holds. When somebody
	last signed in says how often they use the browser, so it is not on the account a colleague
	reads; the account itself reads its own on ``GET /v1/me``.

	A sign-in is a sign-in link opened in a browser, and null means never: the ordinary answer for
	an agent, which presents a token instead. Paged as ``GET /v1/users`` is, in the same order.
	"""

	found, page = subroutine.api.users.paged(
		session,
		settings,
		subroutine.domain.users.sign_ins(session, actor=actor),
		limit=limit,
		cursor=cursor,
		include_total=include_total,
		collection="sign-ins",
	)

	return subroutine.views.Collection[subroutine.views.SignIn](
		items=[subroutine.views.sign_in(row) for row in found], page=page
	)
