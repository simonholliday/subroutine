"""What happened: comments people write, and events the system records."""

import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.base
import subroutine.db.fulltext
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
		sqlalchemy.Index(
			"ix_event_workspace_id_subject_type_subject_id_seq",
			"workspace_id",
			"subject_type",
			"subject_id",
			"seq",
		),
		# **The one index here keyed on a clock rather than on a sequence** (`#815`). Every
		# other reader of this table pages by `seq` — a client resuming a feed asks *what is
		# after 4,812* — but *what was worked on yesterday* is a question about **when**, and
		# `seq` only correlates with time, it does not answer it. Without this, the `EXISTS`
		# behind `touched_at` scans a workspace's whole history for every candidate row.
		sqlalchemy.Index(
			"ix_event_workspace_id_created_at", "workspace_id", "created_at"
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
	#
	# **Deliberately not foreign keys** (`#672`). They were, with ``ON DELETE SET NULL``, so
	# removing a user or a token silently rewrote every event that actor had ever written —
	# retroactively, across the whole history, with nothing recording that it used to say more.
	# A GDPR erasure *is* a hard user delete, and clearing out credentials nobody uses is
	# exactly the tidying nobody thinks of as destructive; neither would have warned.
	#
	# **Dropping the constraint is the whole fix, because nothing joins through it.** This class
	# declares no ``relationship``, and every reader is a plain column read or an ``== id``
	# comparison — a join still works without one. What the constraint added here was the
	# clause that erased the answer.
	#
	# **It also makes ``NULL`` mean one thing again.** It meant *either* a system action *or*
	# somebody acted and the database forgot who, and the column could not tell them apart.
	# Nothing nulls these now, so it means the first.
	#
	# **And it is the better answer for erasure, not the weaker one.** ``User`` carries
	# :class:`~subroutine.db.mixins.SoftDeleteMixin`, so ordinary departure keeps the row and
	# the name; a hard delete happens precisely when the name is meant to go. The identity
	# therefore lives in one place, deleting it erases it, and the id left here is unlinkable —
	# where a snapshotted name would put it on every row and owe a sweep for ever.
	actor_user_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		nullable=True,
	)
	actor_token_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		nullable=True,
	)

	# Which door the request came in through — `#1415`, decision `#1426`. One of
	# `domain.authentication.INTERFACES`: `mcp`, `api`, `browser`, `feed` or `local`.
	#
	# **The third axis of a question this table already asks in two.** It carries the account
	# and the credential it presented as separate facts; *what actually made the request* is
	# the same question again, and the two above it are both things somebody chose. A
	# credential's name is typed once by a human and never changes. This is observed.
	#
	# **Null means nobody said, and that is not the same as `local`.** A system write — seeding,
	# a migration's data fix, `subroutine init` — has no principal at all, and a principal built
	# before this shipped states nothing. `local` is a positive claim that somebody was at the
	# machine with the database file, which is §12.1a's most privileged path and the one an
	# operator most wants named afterwards.
	#
	# **A string rather than an enum, matching `entity_type` and `action` beside it.** A door
	# this code has not been taught about should be recordable by the release that learns it,
	# without a migration; and the check that a *stated* one is coherent with the credential
	# presented lives on `Principal`, where both facts are in the same object.
	actor_interface: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(16), nullable=True
	)
	entity_type: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(32), nullable=False
	)
	entity_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(), nullable=False
	)

	# What this happened *on*, when that is not the entity itself. A comment is the case that
	# forced it: the entity is the comment, but "what happened to #42" has to include somebody
	# commenting on #42, and without this the two are unrelatable rows. Null means the event is
	# about the entity and nothing else, which is every other write in the system.
	#
	# Deliberately not a foreign key and deliberately unconstrained, exactly like `entity_type`
	# on this table: the subject is polymorphic, and a table that must accept a row for anything
	# cannot hold a reference to everything.
	subject_type: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(32), nullable=True
	)
	subject_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(), nullable=True
	)

	# A **second** thing the event happened on, when it happened on two of them (`#302`). One
	# subject can only express one item's visibility, and a link's is the *conjunction* of two:
	# an event whose source is visible carried the `target`'s ref in `changes`, so a reader who
	# could see one end learned that a particular hidden item existed and was joined to it.
	#
	# **Null on every write but a link's, and `scoping.visible_events` must not know that.**
	# The rule it adds is *if a second subject is set it must be visible too* — generic, like
	# every other rule in that module, because the moment one clause names a kind the next one
	# has a precedent. Anything that later happens to two items gets the conjunction free.
	#
	# **It is the end that is not the subject**, which is what makes `#816`'s inversion fall
	# out rather than needing a case: somebody withdrawing an *incoming* link is standing on
	# the target, so the event already names the target and the far end is the source.
	subject_b_type: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(32), nullable=True
	)
	subject_b_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(), nullable=True
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


#: `#83`: a comment is prose a search has to reach, and on a working instance there is more of
#: it than there is of anything else. Same rule as the item indexes in
#: :mod:`subroutine.db.models.work` — see those for why this is not in ``__table_args__``.
ix_comment_search = subroutine.db.fulltext.index(
	"ix_comment_search", Comment.__table__.c.body
)
