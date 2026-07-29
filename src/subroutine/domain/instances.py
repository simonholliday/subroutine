"""The identity of this installation.

One row, written once, never replaced (SPEC.md §13.7). An agent connected to a personal
instance and a work one keys its caches on this value, uses it to notice the same instance
configured twice under two names, and labels merged results with it — so a value that
changed would silently corrupt all three at once.
"""

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.system
import subroutine.errors


def get (session: sqlalchemy.orm.Session) -> subroutine.db.models.system.Instance | None:
	"""Return this installation's row, or ``None`` if it has never been initialised."""

	return session.scalars(sqlalchemy.select(subroutine.db.models.system.Instance)).one_or_none()


def establish (
	session: sqlalchemy.orm.Session, *, name: str, timezone: str = "UTC"
) -> tuple[subroutine.db.models.system.Instance, bool]:
	"""Return this installation's row, creating it if there is not one yet.

	Returns ``(instance, created)``. Running it twice is safe and changes nothing, which
	is what lets ``subroutine init`` be re-run against a database that already exists —
	the common case in a container that restarts.
	"""

	existing = get(session)

	if existing is not None:
		return existing, False

	instance = subroutine.db.models.system.Instance(name=name, timezone=timezone)
	session.add(instance)
	session.flush()

	return instance, True


def require (session: sqlalchemy.orm.Session) -> subroutine.db.models.system.Instance:
	"""Return this installation's row, or explain that it has not been set up."""

	instance = get(session)

	if instance is None:
		raise subroutine.errors.NotFound(
			"This database has not been set up yet.",
			hint="Run 'subroutine init' to create it.",
		)

	return instance
