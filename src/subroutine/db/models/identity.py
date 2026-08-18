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
	#: every workspace created without an explicit zone (docs/design.md §6.5).
	timezone: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=True
	)
	#: The counter every ref in this workspace is drawn from, shared by tasks and documents
	#: so that a ref names exactly one thing (docs/design.md §6.2). It lives here rather than on
	#: the project because a ref must not name anything the item can be moved out of: a
	#: number minted per project either follows the item and lies about where it is, or
	#: changes when it moves and stops being an identifier.
	next_ref_number: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, default=1, nullable=False
	)
	#: The one project whose work rises in this workspace's ranked listings (decision
	#: ``#982``), or null for none. Its whole subtree inherits, because there is only ever one.
	#:
	#: **A single nullable pointer is the anti-spiral mechanism, and it is why this is not a
	#: boolean on ``project``.** One column holds one value, so choosing B unsets A atomically:
	#: that *is* the radio-button semantic, with no clearing logic and no unique index to get
	#: wrong. ``ON DELETE SET NULL`` means the state cannot outlive its subject.
	#:
	#: **``is_inbox`` is the existing one-per-workspace flag and is not the pattern to copy.**
	#: It carries no uniqueness constraint at all and is safe only because nothing but
	#: ``workspaces.create`` writes it, which is not true of anything a person toggles.
	#:
	#: The pointer may only name a project *in this workspace*; ``workspaces.update`` is what
	#: enforces that, since a pointer across the boundary is a scoping hole rather than a
	#: curiosity.
	#:
	#: **``use_alter`` because this column closes a foreign-key cycle**, and the cycle is not
	#: obvious from either end: ``project`` references ``status``, ``status`` references
	#: ``workspace``, and this closes the loop. Without it ``metadata.sorted_tables`` and
	#: ``drop_all`` both warn that they cannot order the schema — and ``filterwarnings =
	#: ["error"]`` turns that into a failure, so the suite would go red a long way from here.
	#: The flag says *this cycle is known*: the constraint is created and dropped by its own
	#: ``ALTER`` rather than inline, which is what lets the three tables be ordered at all.
	prioritised_project_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = (
		sqlalchemy.orm.mapped_column(
			subroutine.db.types.uuid_column(),
			sqlalchemy.ForeignKey("project.id", ondelete="SET NULL", use_alter=True),
			nullable=True,
		)
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

	# Who answers for what this account does (SPEC §7.1, decision `#473`). An agent is not a
	# principal anybody can blame: somebody gave it permission to work, and that somebody is
	# accountable for the result. **Accountability is a property of the agent rather than of
	# any task**, which is why it lives here and not on `task` — it does not vary per ticket.
	#
	# Null on a person, who answers for themselves. Null on a service account means nobody is
	# accountable, which is a reason to refuse rather than a default to tolerate.
	#
	# `ondelete="SET NULL"` fails safe on purpose: deleting the responsible person leaves the
	# agent with no chain, and an agent with no chain stops.
	responsible_user_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="SET NULL"),
		nullable=True,
		index=True,
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

	# **The write set: where this credential may change things, within what it can reach**
	# (§7.3, item `#371`). NULL means "wherever it can reach", so every credential issued
	# before this column existed keeps exactly the authority it had — the whole point of
	# spelling the default as a null rather than as a copy of `project_scope`.
	#
	# A list is a *subset* of `project_scope` when that is set, enforced at issue. Reads still
	# go by `project_scope` alone: this narrows the verbs in
	# `permissions.WRITES_INSIDE_A_PROJECT` and nothing else.
	project_write_scope: sqlalchemy.orm.Mapped[list[str] | None] = sqlalchemy.orm.mapped_column(
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


class LoginLink(subroutine.db.base.Base, subroutine.db.mixins.TimestampMixin):
	"""A single-use credential that buys one browser session, and nothing else.

	**Short-lived and used once, because it travels in a URL.** A link lands in browser
	history, in a referrer header and in whatever forwarded the message carrying it, so
	the mitigation is not secrecy but a lifetime measured in minutes and a row that is
	spent the first time it is redeemed (§7.4's rule against tokens in query strings is
	the same reasoning arriving at a different answer for a different kind of credential).

	It is deliberately not a session: the two have different lifetimes and different
	single-use semantics, and one table with a flag for which it was would make every read
	ask a question the type should already have answered.
	"""

	__tablename__ = "login_link"

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	user_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="CASCADE"),
		nullable=False,
		index=True,
	)

	# The public half, as on `ApiToken`: found by index rather than by scanning hashes.
	token_prefix: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(32), nullable=False, unique=True, index=True
	)
	token_hash: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(128), nullable=False
	)
	# NOT NULL where `ApiToken.expires_at` is nullable, and that difference is the point: an
	# API token may be deliberately permanent, and a credential that arrives in a URL may
	# not become permanent by somebody omitting a field.
	expires_at: sqlalchemy.orm.Mapped[datetime.datetime] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=False
	)
	redeemed_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)

	# Who asked for it. NULL is somebody at a terminal with the database file, which §12.1a
	# says is the one caller no check narrows.
	created_by: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="SET NULL"),
		nullable=True,
	)


class WebSession(subroutine.db.base.Base, subroutine.db.mixins.TimestampMixin):
	"""A browser's credential: an opaque value in a cookie, backed by this row.

	Decision `#364` settled the shape and struck the alternative §7.5 reserved. **Not a
	JWT**, for two reasons that outlive the fashion: a signed self-describing credential
	cannot be revoked before it expires, which is the property §7.4 sells; and verifying
	one needs ``secret_key`` on the request path, which is deliberately kept to signing
	pagination cursors so that rotating it never locks anybody out.

	**It carries no scopes, no project scope and no workspace pin, and that is the whole of
	why it is safe.** A browser session is its owner acting as themselves. Every narrowing
	an API token can express is a property of the token, so a session simply has none of
	them — and :class:`~subroutine.domain.authentication.Principal` must therefore never
	read the absence of a token as the absence of narrowing (`#364`).
	"""

	__tablename__ = "web_session"

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	user_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="CASCADE"),
		nullable=False,
		index=True,
	)
	token_prefix: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(32), nullable=False, unique=True, index=True
	)
	token_hash: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(128), nullable=False
	)
	expires_at: sqlalchemy.orm.Mapped[datetime.datetime] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=False
	)
	last_used_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	revoked_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)

	# Which link bought this session. Kept so that a link found to have leaked names the
	# sessions it produced, rather than leaving somebody to guess from timestamps.
	login_link_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("login_link.id", ondelete="SET NULL"),
		nullable=True,
	)


#: What a feed may cover, and the whole of it (docs/design.md §20.1). ``everything`` is every task
#: in scope its owner may read; ``assigned_to_me`` narrows to the ones they hold. Both are
#: wanted and neither is a good default for the other's use, which is why the choice is per
#: feed rather than a setting.
CALENDAR_AUDIENCES = ("everything", "assigned_to_me")


class CalendarFeed(subroutine.db.base.Base, subroutine.db.mixins.TimestampMixin):
	"""A read-only iCalendar subscription: one scope, one audience, one secret in a URL.

	The fourth credential kind, and the only one whose secret travels in a path rather than
	in a header (docs/design.md §20.2). §7.4 forbids that for API tokens and the rule stands — this
	is a *different kind of credential*, and the four properties that make it a different
	question are worth having in front of anybody changing this table:

	* it is **read-only**, and valid on the calendar endpoint and nowhere else;
	* it grants **one scope**, never its owner's whole authority;
	* it exposes titles, dates and refs, and nothing else the API would return;
	* and a leak is **undetectable from the server side**, which is why ``last_polled_at``
	  exists and why resetting the secret is one of the four commands.

	**Visibility is resolved when the feed is rendered, never when it was created.** The row
	carries an owner and each poll narrows by what that owner may read *now* — a feed that
	baked in a project list would go on serving a private project after its owner left it,
	and there is no login to audit that would ever surface it.

	**Not a row on ``api_token`` with a flag.** Every narrowing an API token can express is
	absent here and every property above is absent there, so one table would make each read
	ask a question the type should have answered — which is `#364`'s own argument for
	:class:`WebSession` being its own table, arriving at the same answer for the same reason.
	"""

	__tablename__ = "calendar_feed"
	__table_args__ = (subroutine.db.mixins.enum_check("audience", CALENDAR_AUDIENCES),)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()

	# NOT NULL, unlike `ApiToken.workspace_id`, and the difference is what the field means: a
	# token's null is *every workspace the owner belongs to*, where a feed is one calendar in
	# one client and spanning workspaces would put a dentist appointment and a deployment
	# window in a list with nothing saying which is which.
	workspace_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("workspace.id", ondelete="CASCADE"),
		nullable=False,
		index=True,
	)

	# NULL means the whole workspace. A project covers **its visible sub-projects too**
	# (§20.1), matching §7.3a — privacy inherits down the tree, so a feed on a parent that
	# stopped at the parent would show less than that project's own page does.
	project_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("project.id", ondelete="CASCADE"),
		nullable=True,
	)

	# Whose sight this feed borrows. CASCADE rather than SET NULL: a feed with no owner has
	# no visibility rule left to apply, so it must stop existing rather than fall back to
	# something.
	owner_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="CASCADE"),
		nullable=False,
		index=True,
	)
	audience: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(32), nullable=False
	)

	# **Which item types the feed carries — ids, never keys** (decision `#972`). §5.5 makes the
	# vocabulary per workspace and renameable, so a feed naming `event` would silently stop
	# matching the day somebody renamed it, and silently is the whole problem: a feed has no
	# reader who could complain. NULL means every type, which is what a caller who says
	# nothing gets and what every feed made before this column would get.
	item_type_ids: sqlalchemy.orm.Mapped[list[str] | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.json_column(), nullable=True
	)

	# What the calendar is called in the client. A person subscribes to several of these and
	# a list of identical names is a list nobody can choose from.
	title: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(128), nullable=False
	)

	# The public half, found by index rather than by scanning hashes — `ApiToken`'s pattern,
	# and `sha256(secret)` beside it for the reason §7.4 gives.
	token_prefix: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(32), nullable=False, unique=True, index=True
	)
	token_hash: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(128), nullable=False
	)

	# **What makes a stale feed noticeable** (§20.3). A URL nobody has used for six months is
	# one to revoke, and without this column there is no way to tell — which matters more here
	# than for a token, because a leaked feed is used by somebody who never announces
	# themselves.
	last_polled_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)

	# Nullable, unlike `LoginLink.expires_at`: a link is a credential in a URL that must not
	# become permanent by omission, and a feed is a credential in a URL that a person means
	# to keep. What makes the difference safe is that a feed reads one scope and a link buys
	# a whole session.
	expires_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	revoked_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
