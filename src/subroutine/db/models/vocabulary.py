"""The words an installation uses: statuses, item types, link types and tags.

All four are per-workspace lookup tables rather than hard-coded enumerations, so that
renaming "In progress" to "Cooking", or adding an "epic" item type, is a data change
rather than a migration. What keeps that portable is that each row also carries a fixed,
machine-meaningful category or key alongside its human label.
"""

import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.base
import subroutine.db.mixins


class Status(
	subroutine.db.base.Base,
	subroutine.db.mixins.WorkspaceScopedMixin,
	subroutine.db.mixins.TimestampMixin,
):
	"""A named workflow state for a task, project or document.

	``category`` is the load-bearing field. An installation may call a status anything it
	likes, but every status maps to one of a fixed set of meanings — so "everything not
	finished" is answerable without knowing the local vocabulary.
	"""

	__tablename__ = "status"
	__table_args__ = (
		sqlalchemy.UniqueConstraint(
			"workspace_id", "entity_type", "key", name="uq_status_workspace_id_entity_type_key"
		),
		subroutine.db.mixins.enum_check("entity_type", subroutine.db.mixins.STATUS_ENTITY_TYPES),
		subroutine.db.mixins.enum_check("category", subroutine.db.mixins.STATUS_CATEGORIES),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	entity_type: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(16), nullable=False
	)
	key: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=False
	)
	label: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(128), nullable=False
	)
	category: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(16), nullable=False
	)
	position: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, nullable=False
	)
	is_default: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, default=False, nullable=False
	)


class ItemType(
	subroutine.db.base.Base,
	subroutine.db.mixins.WorkspaceScopedMixin,
	subroutine.db.mixins.TimestampMixin,
):
	"""What kind of thing a task or document is.

	One table serves both, discriminated by ``entity_type`` — the same trick statuses
	use. What each kind is seeded with is ``subroutine.db.seed.named_types``, which is where a
	reader should go rather than to a list written out here: this docstring held one of six
	hand-maintained copies, and all six were wrong on the day a second seed set landed
	(`#1240`).

	``category`` is what a client branches on when it does not recognise the key (decision
	`#1133`), exactly as :class:`Status`'s is: an installation may call a type anything, and a
	workspace that invents ``epic`` should get a picture that means something rather than the
	glyph for *unknown* for ever. It answers that one question and no other — see
	:data:`subroutine.db.mixins.ITEM_TYPE_CATEGORIES` for the two it deliberately does not.
	"""

	__tablename__ = "item_type"
	__table_args__ = (
		sqlalchemy.UniqueConstraint(
			"workspace_id", "entity_type", "key", name="uq_item_type_workspace_id_entity_type_key"
		),
		subroutine.db.mixins.enum_check("entity_type", subroutine.db.mixins.ITEM_ENTITY_TYPES),
		subroutine.db.mixins.enum_check("category", subroutine.db.mixins.ITEM_TYPE_CATEGORIES),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	entity_type: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(16), nullable=False
	)
	key: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=False
	)
	label: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(128), nullable=False
	)
	category: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(16), nullable=False
	)
	position: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, nullable=False
	)
	is_default: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, default=False, nullable=False
	)
	is_system: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, default=False, nullable=False
	)


class LinkType(
	subroutine.db.base.Base,
	subroutine.db.mixins.WorkspaceScopedMixin,
	subroutine.db.mixins.TimestampMixin,
):
	"""How two work items can relate.

	The inverse title lets one stored edge be displayed correctly from both ends —
	"blocks" from the source, "is blocked by" from the target — without storing it twice
	and risking the two halves disagreeing.

	``category`` is what every rule about this relation reads (decision `#1157`). A key is
	renameable and six rules were written in terms of one, so renaming ``blocks`` broke
	``--ready`` while the item page went on saying *Blocked by* — `#1156`. The key names the
	relation; the category is what the program is allowed to conclude from it.
	"""

	__tablename__ = "link_type"
	__table_args__ = (
		sqlalchemy.UniqueConstraint("workspace_id", "key", name="uq_link_type_workspace_id_key"),
		subroutine.db.mixins.enum_check("category", subroutine.db.mixins.LINK_TYPE_CATEGORIES),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	key: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=False
	)
	title: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(128), nullable=False
	)
	inverse_title: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(128), nullable=False
	)
	category: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(16), nullable=False
	)
	is_symmetric: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, default=False, nullable=False
	)
	is_system: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, default=False, nullable=False
	)


class Tag(
	subroutine.db.base.Base,
	subroutine.db.mixins.WorkspaceScopedMixin,
	subroutine.db.mixins.TimestampMixin,
):
	"""A free-form label, created automatically the first time it is used.

	Auto-creation matters for quick capture: typing ``#health`` should not require a
	separate tag-management step, from a person or from an agent.
	"""

	__tablename__ = "tag"
	__table_args__ = (
		sqlalchemy.UniqueConstraint(
			"workspace_id", "name_normalized", name="uq_tag_workspace_id_name_normalized"
		),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	name: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(128), nullable=False
	)
	name_normalized: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(128), nullable=False
	)
	description: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Text, nullable=True
	)
