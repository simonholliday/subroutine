"""The identity of this installation, and the two labels attached to it.

**One row, and its ``id`` is written once and never replaced** (docs/design.md §13.7). An agent
connected to a personal instance and a work one keys its caches on that value, uses it to notice
the same instance configured twice under two names, and labels merged results with it — so an id
that changed would silently corrupt all three at once.

**That argument is about the id and was applied to the whole row** (`#1669`). This docstring said
*"one row, written once, never replaced"* while the model's own comment said of ``name``:
*"Editable — it is a label, not an identity."* Both could not be right, and the column comment was
the one describing the intent. ``name`` and ``timezone`` are editable by
:func:`update`; nothing else here is.

**Nothing records an instance change in the event feed, and that is a limitation rather than a
decision.** Every event is workspace-scoped, and an instance belongs to no workspace — so there is
no honest place to put one. Recording it against every workspace would be a lie about where it
happened, and against one would be arbitrary.
"""

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.system
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.dates
import subroutine.domain.patch
import subroutine.errors
import subroutine.permissions


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


def update (
	session: sqlalchemy.orm.Session,
	*,
	name: str = subroutine.domain.patch.UNSET,
	timezone: str = subroutine.domain.patch.UNSET,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.system.Instance:
	"""Change what this installation is called, or where it says it is — item `#1669`.

	**Requires ``instance:admin``**, which no role carries and only a superuser holds. Both
	fields are operator concerns: the name is what tells one instance from another wherever
	somebody reaches two, and the timezone is the last word in §6.5's chain, so a wrong one is
	read by everybody who has not set their own.

	**Neither was settable by anything until now.** ``establish`` returns the existing row
	untouched, so a second ``init`` is a no-op on both — which means a self-hosted instance took
	its name from the machine's hostname and kept it for ever.

	**Only these two.** The id is the identity and cannot move; `#1668`'s conclusion is that the
	wider settings-scope question is joined at a settings *page* rather than at this row, so
	this deliberately stops here rather than becoming the first settings surface.

	An omitted field is unchanged, which is §8.3's distinction between *not asked* and *set to
	nothing* — and neither of these may be set to nothing, since both are ``NOT NULL``.
	"""

	if actor is not None:
		subroutine.domain.authorization.authorize_instance(
			actor, subroutine.permissions.INSTANCE_ADMIN
		)

	instance = require(session)

	if subroutine.domain.patch.is_set(name):
		wanted = name.strip()

		# **Refused rather than silently kept**, because a caller that sent a name means to
		# change it, and reporting the old one back would read as success.
		if not wanted:
			raise subroutine.errors.ValidationError(
				"An instance needs a name.",
				hint="It is what tells this installation from another one you can reach.",
				errors=[
					subroutine.errors.FieldError(
						field="name",
						code="invalid_field_value",
						message="A name cannot be empty.",
					)
				],
			)

		instance.name = wanted

	if subroutine.domain.patch.is_set(timezone):
		# Through the shared checker, so an unknown zone is refused here in the same words it
		# is refused in on a workspace and on a user.
		subroutine.domain.dates.zone(timezone, field="timezone")

		instance.timezone = timezone

	session.flush()

	return instance
