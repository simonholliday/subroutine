"""What happened: comments people write, and events the system records."""

import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.base
import subroutine.db.mixins
import subroutine.db.types


class Comment(
	subroutine.db.base.Base,
	subroutine.db.mixins.WorkspaceScopedMixin,
	subroutine.db.mixins.TimestampMixin,
	subroutine.db.mixins.VersionMixin,
	subroutine.db.mixins.SoftDeleteMixin,
):
	"""Threaded discussion on a task, project or document.

	Where an agent records progress and findings, and where a human answers a question
	the agent parked. The subject is addressed polymorphically, so there is no foreign
	key on the pair — the integrity rule lives in the service layer instead.
	"""

	__tablename__ = "comment"
	__table_args__ = (
		sqlalchemy.Index(
			"ix_comment_workspace_id_entity_type_entity_id_created_at",
			"workspace_id",
			"entity_type",
			"entity_id",
			"created_at",
		),
		subroutine.db.mixins.enum_check("entity_type", subroutine.db.mixins.COMMENT_ENTITY_TYPES),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	entity_type: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(16), nullable=False
	)
	entity_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(), nullable=False
	)
	parent_comment_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("comment.id", ondelete="CASCADE"),
		nullable=True,
	)
	author_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="SET NULL"),
		nullable=True,
	)
	body: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Text, nullable=False
	)


class Event(subroutine.db.base.Base, subroutine.db.mixins.WorkspaceScopedMixin):
	"""An append-only record of every mutation, and the backbone of four features.

	One table serves as the audit trail, the activity feed, the change feed that clients
	poll for "what happened while I was away", and the outbox a webhook dispatcher will
	later drain. Writing these from the first migration costs one insert per mutation;
	adding them later means the history starts from whenever someone remembered.

	The primary key is ``seq`` rather than ``id``, which is the one deliberate exception
	to the naming convention: clients page through changes by sequence number, and that
	needs a monotonic integer rather than a UUID.
	"""

	__tablename__ = "event"
	__table_args__ = (
		sqlalchemy.Index("ix_event_workspace_id_seq", "workspace_id", "seq"),
		sqlalchemy.Index(
			"ix_event_workspace_id_entity_type_entity_id_seq",
			"workspace_id",
			"entity_type",
			"entity_id",
			"seq",
		),
	)

	seq: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.autoincrement_bigint(), primary_key=True, autoincrement=True
	)
	id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		default=subroutine.db.types.new_uuid,
		nullable=False,
		unique=True,
	)

	# Both nullable: a system action has no user, and a session-authenticated action has
	# no token. Recording which is which is what makes the audit trail worth having.
	actor_user_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="SET NULL"),
		nullable=True,
	)
	actor_token_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("api_token.id", ondelete="SET NULL"),
		nullable=True,
	)
	entity_type: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(32), nullable=False
	)
	entity_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(), nullable=False
	)
	action: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=False
	)
	changes: sqlalchemy.orm.Mapped[dict[str, typing.Any] | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.json_column(), nullable=True
	)
	created_at: sqlalchemy.orm.Mapped[datetime.datetime] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), default=subroutine.db.types.utcnow, nullable=False
	)
