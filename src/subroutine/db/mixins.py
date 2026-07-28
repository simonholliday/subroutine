"""Column groups shared by most tables, and the vocabularies they constrain.

Declared as mixins rather than repeated per model so that "every mutable entity carries
these" (SPEC.md §6.1) is enforced by construction instead of by memory.
"""

import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.types

#: What a status means regardless of the label an installation gives it. Task and
#: project statuses use the first four; documents use the second four, because a
#: superseded specification is not "done" and pretending otherwise makes both sets lie.
TASK_STATUS_CATEGORIES = ("todo", "in_progress", "done", "cancelled")
DOCUMENT_STATUS_CATEGORIES = ("draft", "current", "superseded", "archived")
STATUS_CATEGORIES = TASK_STATUS_CATEGORIES + DOCUMENT_STATUS_CATEGORIES

#: Entities that can carry a status, a type, a comment or a link.
STATUS_ENTITY_TYPES = ("task", "project", "document")
ITEM_ENTITY_TYPES = ("task", "document")
COMMENT_ENTITY_TYPES = ("task", "project", "document")
LINK_ENTITY_TYPES = ("task", "document", "verification")

PROJECT_VISIBILITIES = ("public", "private")
PROJECT_TEMPLATES = ("blank", "personal", "software")
RECURRENCE_ANCHORS = ("schedule", "completion")

#: The gap left between adjacent `position` values, so an item can be inserted between
#: two others without renumbering the whole sibling set.
POSITION_GAP = 1000


def enum_check (column: str, values: typing.Sequence[str], name: str) -> sqlalchemy.CheckConstraint:
	"""Build a named CHECK constraint restricting ``column`` to ``values``.

	Native database enums are avoided throughout: altering one is painful on PostgreSQL
	and impossible on SQLite. A named CHECK is portable, and the name is what lets
	Alembic's batch mode drop it during a SQLite table rebuild.
	"""

	quoted = ", ".join(f"'{value}'" for value in values)

	return sqlalchemy.CheckConstraint(f"{column} IN ({quoted})", name=name)


def uuid_primary_key () -> sqlalchemy.orm.Mapped[uuid.UUID]:
	"""Return the standard time-ordered UUID primary key column."""

	return sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		primary_key=True,
		default=subroutine.db.types.new_uuid,
	)


class TimestampMixin:
	"""Records when a row was created and last changed."""

	created_at: sqlalchemy.orm.Mapped[datetime.datetime] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), default=subroutine.db.types.utcnow, nullable=False
	)
	updated_at: sqlalchemy.orm.Mapped[datetime.datetime] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(),
		default=subroutine.db.types.utcnow,
		onupdate=subroutine.db.types.utcnow,
		nullable=False,
	)


class AuthorshipMixin:
	"""Records which user created and last changed a row.

	Nullable because some rows are written by the system during setup, before any user
	exists to attribute them to.
	"""

	@sqlalchemy.orm.declared_attr
	@classmethod
	def created_by (cls) -> sqlalchemy.orm.Mapped[uuid.UUID | None]:
		"""Return the user who created this row, if a user did."""

		return sqlalchemy.orm.mapped_column(
			subroutine.db.types.uuid_column(),
			sqlalchemy.ForeignKey("user.id", ondelete="SET NULL"),
			nullable=True,
		)

	@sqlalchemy.orm.declared_attr
	@classmethod
	def updated_by (cls) -> sqlalchemy.orm.Mapped[uuid.UUID | None]:
		"""Return the user who last changed this row, if a user did."""

		return sqlalchemy.orm.mapped_column(
			subroutine.db.types.uuid_column(),
			sqlalchemy.ForeignKey("user.id", ondelete="SET NULL"),
			nullable=True,
		)


class VersionMixin:
	"""Carries the optimistic-concurrency counter surfaced as an ETag."""

	version: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, default=1, nullable=False
	)


class SoftDeleteMixin:
	"""Marks a row as deleted without removing it, so it can be restored from the trash."""

	deleted_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)


class WorkspaceScopedMixin:
	"""Binds a row to its tenancy root.

	Every query in the system filters on this column. It is denormalised onto tables that
	could reach the workspace through a parent, because the alternative is a join on the
	hottest path in the application.
	"""

	@sqlalchemy.orm.declared_attr
	@classmethod
	def workspace_id (cls) -> sqlalchemy.orm.Mapped[uuid.UUID]:
		"""Return the workspace this row belongs to."""

		return sqlalchemy.orm.mapped_column(
			subroutine.db.types.uuid_column(),
			sqlalchemy.ForeignKey("workspace.id", ondelete="CASCADE"),
			nullable=False,
			index=True,
		)
