"""Projects: the container for work, the unit of permission, and a tree."""

import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.base
import subroutine.db.mixins
import subroutine.db.types


class Project(
	subroutine.db.base.Base,
	subroutine.db.mixins.WorkspaceScopedMixin,
	subroutine.db.mixins.TimestampMixin,
	subroutine.db.mixins.AuthorshipMixin,
	subroutine.db.mixins.VersionMixin,
	subroutine.db.mixins.SoftDeleteMixin,
):
	"""A container for tasks and documents, and a node in a tree.

	Permissions attach here and nowhere below: if you can read a project you can read its
	tasks. There is deliberately no way to hide one task from one member.
	"""

	__tablename__ = "project"
	__table_args__ = (
		# **A key is unique among its siblings, not in its workspace** (decision `#957`).
		# `web-ui` and `marketing` belong under any number of parents, and what has to stay
		# unique is the *path* — exactly as a folder or a URL. `substation/dist` rather than
		# `substation/substation-dist`, which is what keying for workspace-uniqueness cost.
		#
		# Partial, because including `deleted_at` in a plain UNIQUE would achieve nothing:
		# NULLs compare as distinct on both backends, so unlimited live duplicates would
		# satisfy it.
		#
		# **And that is why this is two indexes rather than one.** `parent_id` is nullable
		# and hits the same rule, so `(workspace_id, parent_id, key)` alone would let two
		# *root* projects share a key — the commonest shape here, and a hole the constraint
		# is the backstop for. The second index is that case, spelled out.
		sqlalchemy.Index(
			"uq_project_workspace_id_parent_id_key",
			"workspace_id",
			"parent_id",
			"key",
			unique=True,
			sqlite_where=sqlalchemy.text("deleted_at IS NULL AND parent_id IS NOT NULL"),
			postgresql_where=sqlalchemy.text("deleted_at IS NULL AND parent_id IS NOT NULL"),
		),
		sqlalchemy.Index(
			"uq_project_workspace_id_key_at_root",
			"workspace_id",
			"key",
			unique=True,
			sqlite_where=sqlalchemy.text("deleted_at IS NULL AND parent_id IS NULL"),
			postgresql_where=sqlalchemy.text("deleted_at IS NULL AND parent_id IS NULL"),
		),
		sqlalchemy.Index("ix_project_workspace_id_parent_id", "workspace_id", "parent_id"),
		sqlalchemy.Index("ix_project_workspace_id_path", "workspace_id", "path"),
		sqlalchemy.Index("ix_project_workspace_id_status_id", "workspace_id", "status_id"),
		subroutine.db.mixins.enum_check("visibility", subroutine.db.mixins.PROJECT_VISIBILITIES),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	parent_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("project.id", ondelete="RESTRICT"),
		nullable=True,
	)
	visibility: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(16), default="public", nullable=False
	)

	# Short and lower case (`#508`, matching a workspace slug). It is an *address* — a path
	# segment, a `+key` in a capture line — and not part of any ref: §6.2 made a ref a bare
	# workspace-scoped integer on 2026-07-29, and four places went on saying otherwise until
	# `#176`. `id` is the identifier; this is the name, and a name can be changed.
	key: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(32), nullable=False
	)
	title: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(512), nullable=False
	)
	description: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Text, nullable=True
	)
	status_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("status.id", ondelete="RESTRICT"),
		nullable=False,
	)
	owner_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="SET NULL"),
		nullable=True,
	)
	is_inbox: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, default=False, nullable=False
	)

	# Seed-time only: the template writes `settings` and then has no further effect, so
	# a project can be reconfigured afterwards and no template is a cage.
	#
	# **Nothing reads the value today** (`#1028`). The one setting a template ever wrote —
	# `visible_status_keys` — was read nowhere in `src/`, so removing it left this accepted at
	# creation, validated against three names, refused by name when wrong, and with no effect on
	# anything. Kept rather than dropped on Simon's decision of 2026-08-19: `#1029` is the item
	# that gives it a job again — the statuses a project's board shows — which is the only thing
	# it was ever for. `#524`'s precedent, where `is_system` was kept and excused naming `#826`:
	# a column with a nameable future use is not the same as one nobody can name a use for.
	#
	# The refusal is still worth having without a reader. It stops a caller inventing a fourth
	# template and believing in it, which is a different failure from the value being ignored.
	template: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(32), default="blank", nullable=False
	)
	settings: sqlalchemy.orm.Mapped[dict[str, typing.Any]] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.json_column(), default=dict, nullable=False
	)

	# `parent_id` is the truth; `path` is a maintained denormalisation of it, so that
	# "this project and everything under it" is one indexed prefix scan rather than a
	# recursive query.
	path: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(1024), nullable=False
	)
	depth: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, default=0, nullable=False
	)
	position: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, default=0, nullable=False
	)
	start_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	due_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	timezone: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=True
	)

	archived_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	meta: sqlalchemy.orm.Mapped[dict[str, typing.Any]] = sqlalchemy.orm.mapped_column(
		"metadata", subroutine.db.types.json_column(), default=dict, nullable=False
	)


class ProjectMember(
	subroutine.db.base.Base,
	subroutine.db.mixins.WorkspaceScopedMixin,
	subroutine.db.mixins.TimestampMixin,
):
	"""Grants a user access to a private project.

	Empty for now — every project is public in the MVP. The table exists from the first
	migration because adding it later is a migration, and having it costs nothing.
	"""

	__tablename__ = "project_member"
	__table_args__ = (
		sqlalchemy.UniqueConstraint(
			"project_id", "user_id", name="uq_project_member_project_id_user_id"
		),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	project_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("project.id", ondelete="CASCADE"),
		nullable=False,
	)
	user_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="CASCADE"),
		nullable=False,
		index=True,
	)

	# NULL means the member keeps whatever role they hold at workspace level.
	role_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("role.id", ondelete="RESTRICT"),
		nullable=True,
	)
	created_by: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="SET NULL"),
		nullable=True,
	)
