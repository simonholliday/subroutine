"""The installation itself — the one thing that sits above workspaces.

Everything else in the schema belongs to a workspace. This does not: it describes the
running instance, and there is exactly one of it per database.
"""

import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.base
import subroutine.db.mixins


class Instance(subroutine.db.base.Base, subroutine.db.mixins.TimestampMixin):
	"""One row describing this installation, written once by ``subroutine init``.

	``id`` is the ``instance_id`` of docs/design.md §13.7 and must never change: an agent
	connected to a personal instance and a work one keys its caches on this, uses it to
	notice the same instance configured twice under two names, and labels merged results
	with it. A value that changed would silently corrupt all three.

	``singleton`` exists only to make a second row impossible. Being unique and required to
	equal 1, it lets the database enforce what would otherwise be a convention that holds
	until the first careless import.
	"""

	__tablename__ = "instance"
	__table_args__ = (
		sqlalchemy.CheckConstraint("singleton = 1", name="singleton"),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	singleton: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, default=1, nullable=False, unique=True
	)

	#: Reported as ``instance_name``. Editable — it is a label, not an identity.
	name: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(255), nullable=False
	)

	#: Where this installation *is*, and the last word in §6.5's timezone chain. Set from
	#: the machine's own zone by ``subroutine init``; ``UTC`` for anything upgraded from
	#: before the column existed, which is what those installations were already doing.
	#:
	#: A server has a locality and it is not necessarily its users'. A person in London
	#: reading a task on a New York instance needs both times to make sense of a meeting,
	#: and neither the user's zone nor the workspace's can supply the other half.
	timezone: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), server_default="UTC", nullable=False
	)
