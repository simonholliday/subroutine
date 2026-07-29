"""Who is acting: workspaces, users, roles, memberships and API tokens."""

import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.base
import subroutine.db.mixins
import subroutine.db.types


class Workspace(
	subroutine.db.base.Base,
	subroutine.db.mixins.TimestampMixin,
	subroutine.db.mixins.VersionMixin,
	subroutine.db.mixins.SoftDeleteMixin,
):
	"""The tenancy root. Everything else belongs to exactly one of these.

	Invisible for a single person — ``subroutine init`` creates one and never mentions it
	again. For a company it is the boundary of a department, a client, or the whole firm.
	"""

	__tablename__ = "workspace"
	__table_args__ = (
		# Partial, so a deleted workspace releases its short name. Every other identifier
		# in the schema — username, email, project key, task and document refs — frees on
		# soft delete the same way; a plain UNIQUE here would retire a slug permanently,
		# which turns deleting a typo into a name you can never use again.
		sqlalchemy.Index(
			"uq_workspace_slug",
			"slug",
			unique=True,
			sqlite_where=sqlalchemy.text("deleted_at IS NULL"),
			postgresql_where=sqlalchemy.text("deleted_at IS NULL"),
		),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	slug: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=False
	)
	title: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(255), nullable=False
	)
	description: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Text, nullable=True
	)
	#: Nullable, and null means *not stated* rather than UTC — the same convention
	#: ``user.timezone`` and ``instance.timezone`` use. A workspace that never chose one
	#: follows the installation, so moving the instance moves it too; a workspace that did
	#: choose is pinned. Defaulting the column to UTC would have shadowed the instance for
	#: every workspace created without an explicit zone (SPEC.md §6.5).
	timezone: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=True
	)
	#: The counter every ref in this workspace is drawn from, shared by tasks and documents
	#: so that a ref names exactly one thing (SPEC.md §6.2). It lives here rather than on
	#: the project because a ref must not name anything the item can be moved out of: a
	#: number minted per project either follows the item and lies about where it is, or
	#: changes when it moves and stops being an identifier.
	next_ref_number: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, default=1, nullable=False
	)
	settings: sqlalchemy.orm.Mapped[dict[str, typing.Any]] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.json_column(), default=dict, nullable=False
	)


class User(
	subroutine.db.base.Base,
	subroutine.db.mixins.TimestampMixin,
	subroutine.db.mixins.VersionMixin,
	subroutine.db.mixins.SoftDeleteMixin,
):
	"""A person or a machine identity.

	A service account has no password and exists so that an agent's work is attributable
	to something other than the human who happens to own its token. An agent that cannot
	be named cannot be audited.
	"""

	__tablename__ = "user"
	__table_args__ = (
		sqlalchemy.Index(
			"uq_user_username_normalized",
			"username_normalized",
			unique=True,
			sqlite_where=sqlalchemy.text("deleted_at IS NULL"),
			postgresql_where=sqlalchemy.text("deleted_at IS NULL"),
		),
		sqlalchemy.Index(
			"uq_user_email_normalized",
			"email_normalized",
			unique=True,
			sqlite_where=sqlalchemy.text("deleted_at IS NULL"),
			postgresql_where=sqlalchemy.text("deleted_at IS NULL"),
		),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	username: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=False
	)

	# PostgreSQL has `citext` and SQLite does not, and a functional index is awkward
	# under Alembic. An explicit normalised column is portable and obvious.
	username_normalized: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=False
	)
	email: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(320), nullable=True
	)
	email_normalized: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(320), nullable=True
	)
	display_name: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(255), nullable=True
	)
	password_hash: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Text, nullable=True
	)
	is_service_account: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, default=False, nullable=False
	)
	is_superuser: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, default=False, nullable=False
	)
	is_active: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, default=True, nullable=False
	)
	timezone: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=True
	)
	last_login_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)


class Role(
	subroutine.db.base.Base,
	subroutine.db.mixins.WorkspaceScopedMixin,
	subroutine.db.mixins.TimestampMixin,
):
	"""A named bundle of permissions within one workspace.

	Permissions are a JSON list rather than a join table so that a custom role is a data
	change and not a migration.
	"""

	__tablename__ = "role"
	__table_args__ = (sqlalchemy.UniqueConstraint("workspace_id", "key", name="uq_role_workspace_id_key"),)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	key: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=False
	)
	title: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(128), nullable=False
	)
	description: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Text, nullable=True
	)
	permissions: sqlalchemy.orm.Mapped[list[str]] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.json_column(), default=list, nullable=False
	)
	is_system: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, default=False, nullable=False
	)


class WorkspaceMember(
	subroutine.db.base.Base,
	subroutine.db.mixins.WorkspaceScopedMixin,
	subroutine.db.mixins.TimestampMixin,
):
	"""Binds a user to a workspace with a role."""

	__tablename__ = "workspace_member"
	__table_args__ = (
		sqlalchemy.UniqueConstraint(
			"workspace_id", "user_id", name="uq_workspace_member_workspace_id_user_id"
		),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	user_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="CASCADE"),
		nullable=False,
		index=True,
	)
	role_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("role.id", ondelete="RESTRICT"),
		nullable=False,
	)


class ApiToken(subroutine.db.base.Base, subroutine.db.mixins.TimestampMixin):
	"""A bearer credential owned by a user, and never wider than its owner.

	The stored hash is a fast SHA-256 rather than Argon2: the secret carries 256 bits of
	entropy, so brute force is infeasible regardless, and hashing slowly on every request
	would put a hundred milliseconds on the hottest path in the service.
	"""

	__tablename__ = "api_token"

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	user_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="CASCADE"),
		nullable=False,
		index=True,
	)

	# NULL means every workspace the owner belongs to.
	workspace_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("workspace.id", ondelete="CASCADE"),
		nullable=True,
	)
	title: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(128), nullable=False
	)

	# The public half of the token, used to find the row without scanning.
	token_prefix: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(32), nullable=False, unique=True, index=True
	)
	token_hash: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(128), nullable=False
	)

	# An empty list means "inherit the owner's permissions unrestricted" — it is a
	# sentinel, not an empty set. Read as literal set algebra it would grant nothing.
	scopes: sqlalchemy.orm.Mapped[list[str]] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.json_column(), default=list, nullable=False
	)

	# NULL means every project the owner can reach; a list restricts to those subtrees.
	project_scope: sqlalchemy.orm.Mapped[list[str] | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.json_column(), nullable=True
	)
	expires_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	last_used_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	revoked_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	created_by: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="SET NULL"),
		nullable=True,
	)
