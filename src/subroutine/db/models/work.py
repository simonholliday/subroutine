"""Work items: tasks, documents, the tags on them, and the links between them.

Two sibling entities, split by one test: **if it can be done, it is a task; if it can
only be current, it is a document.** A bug is done or not done and carries a deadline, an
estimate and an assignee. A specification is never done — it is draft, then active, then
superseded — and carries none of those. That is half the columns, so they are two tables.
"""

import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.base
import subroutine.db.mixins
import subroutine.db.types


class Task(
	subroutine.db.base.Base,
	subroutine.db.mixins.WorkspaceScopedMixin,
	subroutine.db.mixins.TimestampMixin,
	subroutine.db.mixins.AuthorshipMixin,
	subroutine.db.mixins.VersionMixin,
	subroutine.db.mixins.SoftDeleteMixin,
):
	"""Something that can be finished. Typed task, bug, feature, chore or spike."""

	__tablename__ = "task"
	__table_args__ = (
		sqlalchemy.Index(
			"uq_task_workspace_id_ref",
			"workspace_id",
			"ref",
			unique=True,
			sqlite_where=sqlalchemy.text("deleted_at IS NULL"),
			postgresql_where=sqlalchemy.text("deleted_at IS NULL"),
		),
		sqlalchemy.Index(
			"ix_task_workspace_id_project_id_status_id", "workspace_id", "project_id", "status_id"
		),
		sqlalchemy.Index("ix_task_workspace_id_due_at", "workspace_id", "due_at"),
		sqlalchemy.Index("ix_task_workspace_id_planned_for", "workspace_id", "planned_for"),
		sqlalchemy.Index(
			"ix_task_workspace_id_assignee_id_status_id",
			"workspace_id",
			"assignee_id",
			"status_id",
		),
		sqlalchemy.Index("ix_task_workspace_id_updated_at", "workspace_id", "updated_at"),
		sqlalchemy.Index("ix_task_workspace_id_path", "workspace_id", "path"),
		sqlalchemy.CheckConstraint(
			"importance IS NULL OR (importance BETWEEN 1 AND 5)", name="importance_range"
		),
		sqlalchemy.CheckConstraint(
			"urgency IS NULL OR (urgency BETWEEN 1 AND 5)", name="urgency_range"
		),
		sqlalchemy.CheckConstraint(
			"recurrence_anchor IS NULL OR recurrence_anchor IN ('schedule', 'completion')",
			name="recurrence_anchor",
		),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	project_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("project.id", ondelete="RESTRICT"),
		nullable=False,
	)
	parent_task_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("task.id", ondelete="RESTRICT"),
		nullable=True,
		index=True,
	)
	type_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("item_type.id", ondelete="RESTRICT"),
		nullable=False,
	)

	# Human-readable and immutable: the number a person types and writes down. UUIDs are
	# unusable in a commit message, and a prefix would name something the task can be
	# moved out of (SPEC.md §6.2).
	ref: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, nullable=False
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

	# 1 to 5, where 5 is highest. Absent means "not assessed", which is distinct from 1.
	importance: sqlalchemy.orm.Mapped[int | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.SmallInteger, nullable=True
	)
	urgency: sqlalchemy.orm.Mapped[int | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.SmallInteger, nullable=True
	)

	# **Where an ordering's answer arrives, rather than a column of its own** (`#569`). Not
	# stored and not migrated: the query computes it, `sqlalchemy.orm.with_expression` attaches
	# the result here, and a row that was never asked for it reads `None`.
	#
	# This exists to remove a duplication rather than to add a field. §6.3a records that an
	# ordering lives twice — a SQL expression that sorts the query and a Python function
	# that names the row a keyset cursor stopped at — and that a disagreement between them
	# is a page boundary which skips or repeats rows. An ordering that reads *other* rows,
	# as `#569`'s does, cannot have the second half at all, because a loaded task does not know
	# what it blocks. So SQL keeps the only copy and Python reads its answer off the row.
	#
	# `domain.ordering` owns what goes in here and is the only thing that should populate it.
	rank: sqlalchemy.orm.Mapped[int | None] = sqlalchemy.orm.query_expression()

	# Three distinct date fields. Conflating a deadline with an intended day is what
	# makes an overdue list meaningless within a month.
	due_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	due_is_all_day: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, default=False, nullable=False
	)
	planned_for: sqlalchemy.orm.Mapped[datetime.date | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.CalendarDate(), nullable=True
	)
	start_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	start_is_all_day: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, default=False, nullable=False
	)

	# The zone the dates were authored in, needed for correct recurrence across daylight
	# saving and for rendering an all-day date on the right day.
	timezone: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=True
	)
	estimate_minutes: sqlalchemy.orm.Mapped[int | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, nullable=True
	)
	spent_minutes: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, default=0, nullable=False
	)
	assignee_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="SET NULL"),
		nullable=True,
	)

	# Who put this in that person's queue (decision `#473`, `#476`). Derived from whoever made
	# the change rather than accepted from the caller — "who assigned this to me" is a fact
	# about an act, and a field the assigner could type would be a claim instead.
	#
	# **Not the history**: the event log already carries every assignment change with its actor
	# and its sequence, and decision `#473` settled that the log is the record. This is the
	# current answer, which is what a hand-back reads and what a person asks of their own list.
	# It goes null with the assignee, because an assigner with no assignee names nobody.
	assigned_by_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="SET NULL"),
		nullable=True,
	)

	# **A lease, not a lock** (§14.11). Agents die mid-task routinely, and a hard lock would
	# strand the work permanently — so a claim carries an expiry and an expired one is ignored
	# rather than needing anybody to clean it up. All three move together or none of them do.
	claimed_by_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="SET NULL"),
		nullable=True,
		index=True,
	)
	claimed_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	claim_expires_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = (
		sqlalchemy.orm.mapped_column(subroutine.db.types.UtcDateTime(), nullable=True)
	)

	# Recurrence is stored as an RFC 5545 rule; natural language is an input convenience
	# that is parsed into this, never the stored form.
	recurrence_rule: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Text, nullable=True
	)
	recurrence_anchor: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(16), nullable=True
	)
	recurrence_text: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Text, nullable=True
	)
	recurrence_template_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("task.id", ondelete="SET NULL"),
		nullable=True,
		index=True,
	)
	occurrence_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)

	# A template is a rule-bearing row, not work. It is excluded from every list, search,
	# agenda and rollup by the default repository filter.
	is_template: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, default=False, nullable=False
	)
	path: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(1024), nullable=False
	)
	depth: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, default=0, nullable=False
	)
	position: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, default=0, nullable=False
	)
	completed_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	archived_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	meta: sqlalchemy.orm.Mapped[dict[str, typing.Any]] = sqlalchemy.orm.mapped_column(
		"metadata", subroutine.db.types.json_column(), default=dict, nullable=False
	)

	# Bumped only by changes that invalidate prior work — title, description, acceptance
	# criteria, deadline, status. Claiming a task or renewing a lease bumps `updated_at`
	# but not this, so an agent's own bookkeeping does not void its own evidence.
	content_updated_at: sqlalchemy.orm.Mapped[datetime.datetime] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), default=subroutine.db.types.utcnow, nullable=False
	)


class Document(
	subroutine.db.base.Base,
	subroutine.db.mixins.WorkspaceScopedMixin,
	subroutine.db.mixins.TimestampMixin,
	subroutine.db.mixins.AuthorshipMixin,
	subroutine.db.mixins.VersionMixin,
	subroutine.db.mixins.SoftDeleteMixin,
):
	"""Something that can only be current. Specs, designs, notes, decisions, dead ends.

	Deliberately has no deadline, planned date, estimate, urgency, importance or
	assignee. "The spec must be signed off by Friday" is a *task* linked to the document
	— which keeps the deadline in the agenda where deadlines belong, and keeps documents
	out of every query written for work.
	"""

	__tablename__ = "document"
	__table_args__ = (
		sqlalchemy.Index(
			"uq_document_workspace_id_ref",
			"workspace_id",
			"ref",
			unique=True,
			sqlite_where=sqlalchemy.text("deleted_at IS NULL"),
			postgresql_where=sqlalchemy.text("deleted_at IS NULL"),
		),
		# A document is superseded at most once, so the chain cannot fork.
		sqlalchemy.Index(
			"uq_document_supersedes_id",
			"supersedes_id",
			unique=True,
			sqlite_where=sqlalchemy.text("deleted_at IS NULL"),
			postgresql_where=sqlalchemy.text("deleted_at IS NULL"),
		),
		sqlalchemy.Index(
			"ix_document_workspace_id_project_id_status_id",
			"workspace_id",
			"project_id",
			"status_id",
		),
		sqlalchemy.Index("ix_document_workspace_id_type_id", "workspace_id", "type_id"),
		sqlalchemy.Index("ix_document_workspace_id_path", "workspace_id", "path"),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	project_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("project.id", ondelete="RESTRICT"),
		nullable=False,
	)
	parent_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("document.id", ondelete="RESTRICT"),
		nullable=True,
		index=True,
	)
	type_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("item_type.id", ondelete="RESTRICT"),
		nullable=False,
	)

	# Drawn from the same workspace counter as tasks, so a ref names exactly one thing.
	ref: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, nullable=False
	)
	title: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(512), nullable=False
	)
	body: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Text, nullable=True
	)
	status_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("status.id", ondelete="RESTRICT"),
		nullable=False,
	)

	# Who maintains it. Not an assignee — nobody is "working on" a document.
	owner_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="SET NULL"),
		nullable=True,
	)

	# A column rather than a link type: it is a strict chain with its own integrity rule,
	# and modelling it twice would let the two representations disagree.
	supersedes_id: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("document.id", ondelete="SET NULL"),
		nullable=True,
	)
	path: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(1024), nullable=False
	)
	depth: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, default=0, nullable=False
	)
	position: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Integer, default=0, nullable=False
	)
	archived_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	meta: sqlalchemy.orm.Mapped[dict[str, typing.Any]] = sqlalchemy.orm.mapped_column(
		"metadata", subroutine.db.types.json_column(), default=dict, nullable=False
	)
	content_updated_at: sqlalchemy.orm.Mapped[datetime.datetime] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), default=subroutine.db.types.utcnow, nullable=False
	)


class TaskTag(subroutine.db.base.Base):
	"""Joins a task to a tag."""

	__tablename__ = "task_tag"

	task_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("task.id", ondelete="CASCADE"),
		primary_key=True,
	)
	tag_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("tag.id", ondelete="CASCADE"),
		primary_key=True,
		index=True,
	)
	created_at: sqlalchemy.orm.Mapped[datetime.datetime] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), default=subroutine.db.types.utcnow, nullable=False
	)


class DocumentTag(subroutine.db.base.Base):
	"""Joins a document to a tag."""

	__tablename__ = "document_tag"

	document_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("document.id", ondelete="CASCADE"),
		primary_key=True,
	)
	tag_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("tag.id", ondelete="CASCADE"),
		primary_key=True,
		index=True,
	)
	created_at: sqlalchemy.orm.Mapped[datetime.datetime] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), default=subroutine.db.types.utcnow, nullable=False
	)


class Link(
	subroutine.db.base.Base,
	subroutine.db.mixins.WorkspaceScopedMixin,
	subroutine.db.mixins.SoftDeleteMixin,
):
	"""A typed relationship between two work items, in any combination.

	Stored once and displayed from both ends using the link type's inverse title. The
	polymorphic ends are what let a bug derive from a failing test result, and a task
	derive from the specification that called for it, without a table per pairing.
	"""

	__tablename__ = "link"
	__table_args__ = (
		sqlalchemy.Index(
			"uq_link_source_target_type",
			"source_type",
			"source_id",
			"target_type",
			"target_id",
			"link_type_id",
			unique=True,
			sqlite_where=sqlalchemy.text("deleted_at IS NULL"),
			postgresql_where=sqlalchemy.text("deleted_at IS NULL"),
		),
		sqlalchemy.Index(
			"ix_link_workspace_id_target_type_target_id",
			"workspace_id",
			"target_type",
			"target_id",
		),
		sqlalchemy.Index(
			"ix_link_workspace_id_source_type_source_id",
			"workspace_id",
			"source_type",
			"source_id",
		),
		sqlalchemy.CheckConstraint(
			"NOT (source_type = target_type AND source_id = target_id)", name="not_self"
		),
		subroutine.db.mixins.enum_check("source_type", subroutine.db.mixins.LINK_ENTITY_TYPES),
		subroutine.db.mixins.enum_check("target_type", subroutine.db.mixins.LINK_ENTITY_TYPES),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	source_type: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(16), nullable=False
	)
	source_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(), nullable=False
	)
	target_type: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(16), nullable=False
	)
	target_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(), nullable=False
	)
	link_type_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("link_type.id", ondelete="RESTRICT"),
		nullable=False,
	)
	created_at: sqlalchemy.orm.Mapped[datetime.datetime] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), default=subroutine.db.types.utcnow, nullable=False
	)
	created_by: sqlalchemy.orm.Mapped[uuid.UUID | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("user.id", ondelete="SET NULL"),
		nullable=True,
	)


class Mention(subroutine.db.base.Base, subroutine.db.mixins.WorkspaceScopedMixin):
	"""A reference to a work item found in someone's prose.

	Derived from text and never written directly (SPEC.md §6.15): a mention that did not
	come from a sentence would be a lie about what that sentence says. Every row for one
	source is replaced whenever its text changes, which is why there is no soft delete and
	no version here — there is nothing to restore and no edit to lose a race with.

	Distinct from a link, deliberately. A link is an assertion that changes behaviour; a
	mention only records that one piece of writing talks about another.
	"""

	__tablename__ = "mention"
	__table_args__ = (
		sqlalchemy.UniqueConstraint(
			"source_type",
			"source_id",
			"target_type",
			"target_id",
			name="uq_mention_source_type_source_id_target_type_target_id",
		),
		# The backlink question — "what refers to this?" — is the whole point of the table.
		sqlalchemy.Index(
			"ix_mention_workspace_id_target_type_target_id",
			"workspace_id",
			"target_type",
			"target_id",
		),
		# Used to clear a source's rows before rewriting them.
		sqlalchemy.Index("ix_mention_source_type_source_id", "source_type", "source_id"),
		sqlalchemy.CheckConstraint(
			"NOT (source_type = target_type AND source_id = target_id)",
			name="not_self",
		),
		subroutine.db.mixins.enum_check("source_type", subroutine.db.mixins.MENTION_SOURCE_TYPES),
		subroutine.db.mixins.enum_check("target_type", subroutine.db.mixins.ITEM_ENTITY_TYPES),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	source_type: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(16), nullable=False
	)
	source_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(), nullable=False
	)
	target_type: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(16), nullable=False
	)
	target_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(), nullable=False
	)
	created_at: sqlalchemy.orm.Mapped[datetime.datetime] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), default=subroutine.db.types.utcnow, nullable=False
	)
