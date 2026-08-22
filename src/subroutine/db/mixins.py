"""Column groups shared by most tables, and the vocabularies they constrain.

Declared as mixins rather than repeated per model so that "every mutable entity carries
these" (docs/design.md §6.1) is enforced by construction instead of by memory.
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

#: What text can hold a reference to a work item (docs/design.md §6.15). A comment can mention a
#: task; nothing mentions a comment, so the target set is just the work items.
MENTION_SOURCE_TYPES = ("task", "document", "comment")

PROJECT_VISIBILITIES = ("public", "private")
#: What the next occurrence's date is measured *from* — the rule's own grid, or the instant
#: the last one was finished. "The 1st of each month" is the 1st whether or not you were late;
#: "every 14 days" means fourteen days after you actually watered the plants.
RECURRENCE_ANCHORS = ("schedule", "completion")

#: What *brings* the next occurrence into being, which is a different question from the one
#: above and was folded into it until `#915` (`#94`).
#:
#: ``completion`` puts one occurrence in the list ahead of time, because work you have to do
#: needs somewhere to be finished. ``time`` puts none there at all: a birthday is not a to-do
#: and nobody ever closes one, so the series lives on the calendar and an occurrence becomes
#: a row only when somebody acts on it. **Three of the four combinations are meaningful and
#: ``time`` + ``completion`` is not** — with nothing ever completed there is no instant for
#: that anchor to measure from — so it is refused by name in the service layer.
RECURRENCE_TRIGGERS = ("completion", "time")

#: The gap left between adjacent `position` values, so an item can be inserted between
#: two others without renumbering the whole sibling set.
POSITION_GAP = 1000


def enum_check (column: str, values: typing.Sequence[str]) -> sqlalchemy.CheckConstraint:
	"""Build a named CHECK constraint restricting ``column`` to ``values``.

	Native database enums are avoided throughout: altering one is painful on PostgreSQL
	and impossible on SQLite. A named CHECK is portable, and the name is what lets
	Alembic's batch mode drop it during a SQLite table rebuild.

	The constraint is named after its column, and the naming convention in ``base`` adds
	the ``ck_<table>_`` prefix. Passing a name that already carries that prefix produces
	``ck_status_ck_status_category``, which is what the database then puts in front of the
	user when the constraint fires.
	"""

	quoted = ", ".join(f"'{value}'" for value in values)

	return sqlalchemy.CheckConstraint(f"{column} IN ({quoted})", name=column)


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
	"""Carries the optimistic-concurrency counter surfaced as an ETag.

	**The counter is also the condition every ``UPDATE`` is written under** (`#927` H-12).
	Without that, §8.9's promise held only against a change that had *already* landed:
	:func:`subroutine.domain.versions.require` compares the caller's number against the row
	as loaded in this transaction, so two writers who both read version 1 both passed the
	check and both wrote. Reproduced on both backends — two titles sent, one silently gone,
	and the row left at **version 2 rather than 3**, so a client that read 1 and now sees 2
	concludes that exactly one change happened. The mechanism built to report a lost update
	was concealing it.

	``version_id_col`` makes SQLAlchemy add ``WHERE version = <the value this transaction
	read>`` to every ``UPDATE`` of these tables, and raise
	:class:`~sqlalchemy.orm.exc.StaleDataError` when that matches no row.
	``subroutine.api.problems`` and ``subroutine.clients.local`` turn that into the same 409
	a stale ``expected_version`` earns.

	**``version_id_generator`` is ``False`` on purpose, and it is what makes this one
	declaration rather than thirty edits.** The automatic generator would bump the column
	itself, so every ``row.version += 1`` in the services — the convention this project
	documents — would count twice. With it off, the services go on owning the number and
	SQLAlchemy only owns the condition. Measured: a solo write is unaffected, and a mutation
	that does *not* bump the version is still allowed, which matters because that is the
	state ``tasks.delete`` and ``documents.delete`` were both once in.
	"""

	version: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, default=1, nullable=False
	)

	@sqlalchemy.orm.declared_attr.directive
	@classmethod
	def __mapper_args__ (cls) -> dict[str, typing.Any]:
		"""Point the mapper at this table's own ``version`` column.

		Through ``cls.version`` rather than the mixin's own attribute: a mixin's
		``mapped_column`` is a template copied per subclass, so naming it at class level would
		hand every model one unattached column object. By the time a directive runs, ``cls`` is
		the model and its attribute is that model's column.
		"""

		return {"version_id_col": cls.version, "version_id_generator": False}


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
