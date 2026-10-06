"""Taking and listing the installation's backups, as its administrator - `#4550`, decision `#4532`.

**The decision is the domain's**, not a route's: ``POST /v1/admin/backups`` asked ``instance:admin``
itself, so a second transport would have had to remember to ask. Nothing skipped it; this is where
the question belongs.

**The terminal's ``db backup`` keeps reaching the database file directly and asks nothing**
(§12.4): the administrative commands have to work when the service will not start, and holding the
file is the authority there.
"""

import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.orm

import subroutine.config
import subroutine.db.backup
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.permissions


def take (
	session: sqlalchemy.orm.Session,
	settings: subroutine.config.Settings,
	*,
	keep: int | None = None,
	actor: subroutine.domain.authentication.Principal,
) -> subroutine.db.backup.Backup:
	"""Take a backup of the database this session is served from, for an administrator.

	**Routine, because somebody asked for it** (`#1712`). A copy taken here is one an operator or
	their agent requested deliberately, which is exactly what the routine lifetime describes - the
	other two are copies the program takes on its own initiative during an upgrade or a restore.
	"""

	subroutine.domain.authorization.authorize_instance(
		actor, subroutine.permissions.INSTANCE_ADMIN
	)

	return subroutine.db.backup.take(
		_engine_behind(session), settings, taken_for=subroutine.db.backup.ROUTINE, keep=keep
	)


def held (
	settings: subroutine.config.Settings,
	*,
	actor: subroutine.domain.authentication.Principal,
) -> tuple[list[subroutine.db.backup.Backup], list[subroutine.db.backup.Unfinished]]:
	"""Return the backups this instance holds, newest first, and any copy marked unfinished."""

	subroutine.domain.authorization.authorize_instance(
		actor, subroutine.permissions.INSTANCE_ADMIN, reading=True
	)

	return subroutine.db.backup.catalogue(settings), subroutine.db.backup.unfinished(settings)


def _engine_behind (session: sqlalchemy.orm.Session) -> sqlalchemy.engine.Engine:
	"""Return the engine this session ultimately talks through.

	The engine behind the *session*, rather than a second one built from the configured URL: a
	backup taken over a different connection than the application serves from is a backup of a
	database that may not be the one being served.

	``get_bind`` answers with an ``Engine`` normally and with a ``Connection`` when something
	has bound one - which the test harness does, so that a request shares the test's
	transaction. Both have an engine behind them and it is the same engine either way.
	"""

	bind = session.get_bind()

	if isinstance(bind, sqlalchemy.engine.Connection):
		return bind.engine

	return bind
