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
import subroutine.db.fulltext
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
		# **Follows the planned day, which `starts_at` absorbed** (`#854`). The defer instant
		# carried no index before the rename and `snoozed_until` inherits that, which is right:
		# a defer is read as a predicate on rows a listing already narrowed, never asked for a
		# range.
		sqlalchemy.Index("ix_task_workspace_id_starts_at", "workspace_id", "starts_at"),
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
		# **The two values, and nothing about the pair.** Whether `time` may sit beside a
		# `completion` anchor is a cross-field rule, and a CHECK constraint is not input
		# validation here — it would arrive as a driver error naming no field. The service
		# refuses it and says which of the two to change.
		sqlalchemy.CheckConstraint(
			"recurrence_trigger IS NULL OR recurrence_trigger IN ('completion', 'time')",
			name="recurrence_trigger",
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
	# moved out of (docs/design.md §6.2).
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

	# How well this row answers the search that selected it — `#823`, and the same mechanism as
	# `rank` above for the same reason: a loaded row cannot compute its own relevance, because
	# the value depends on a query it knows nothing about. Populated only when the caller
	# searched *and* a native backend is in force; `None` on every other listing.
	relevance: sqlalchemy.orm.Mapped[float | None] = sqlalchemy.orm.query_expression()

	# Which band this row falls in when a listing sinks deferred work — `#877`. Nought for work
	# that can be started and one for work somebody has put off, so ascending is *deferred last*.
	#
	# The same mechanism as the two above, for a reason of its own: the answer depends on **the
	# clock**, and one request settles every relative comparison against a single instant. A
	# Python half reading `snoozed_until` off a loaded row would be a second clock, so a row an
	# hour either side of its snooze could be filtered by one and sorted by the other.
	parked: sqlalchemy.orm.Mapped[int | None] = sqlalchemy.orm.query_expression()

	# Four distinct date fields, and each says a different thing about *when* (`#854`, and
	# `ends_at` since `#1235`). Conflating a deadline with an intended day is what makes an
	# overdue list meaningless within a month — and conflating an intended day with a *defer*
	# is worse, because a defer hides the row, so an appointment filed as one is invisible
	# until it starts.
	#
	# | field | means | hides the row? |
	# | --- | --- | --- |
	# | `due_at` | must be finished by | no |
	# | `starts_at` | begins at, or the day I intend to do it | no |
	# | `ends_at` | is over at, sharing `starts_is_all_day` | no |
	# | `snoozed_until` | do not show me this until | **yes** |
	due_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	due_is_all_day: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, default=False, nullable=False
	)

	# **This absorbed the old `planned_for`** (`#854`). *Planned for Tuesday* is *starts Tuesday,
	# all day*, so a separate date column was one field saying a subset of what this says —
	# and `_apply_time` already discarded the plan and wrote a start when handed both.
	#
	# With `estimate_minutes` beside it this is also an event's **span**: `at 2pm ~1h` has
	# parsed to exactly that pair since `#797`, and decision `#915` made it the stored form
	# of an appointment's end rather than adding a column. Deliberately **not** `due_at`:
	# `agenda.py` has no item-type filter, so an end time stored as a deadline puts every
	# past meeting in Overdue for ever.
	starts_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	starts_is_all_day: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, default=False, nullable=False
	)

	# **When it is over** — decision `#1235`, and the fourth thing a task can say about *when*.
	# Meaningless without `starts_at`, which the service refuses rather than the column.
	#
	# **An end rather than a length, which reverses `#915` §3 and `#972` §2 on the condition
	# they both named.** Those chose `estimate_minutes` as an appointment's span and said in
	# terms: if the effort-versus-occupancy conflation bites, add the field. It bit on a
	# fortnight's holiday — `estimate_minutes` is how much *work* something takes, so a booked
	# holiday stored there shows `2w` in the agenda's estimate column and is swept up by
	# `--filter estimate_minutes.lte=2h` when somebody asks for quick jobs.
	#
	# **An instant and not a duration**, which reverses `#576`'s own preference for the reason
	# it did not have: an all-day end is a *date*. A fortnight is 20,160 minutes, which nobody
	# types and nothing reads back, and `DTEND` is what RFC 5545 wants — so a duration would be
	# converted to a date on every poll of every feed.
	#
	# **Any task may have one.** *Write the report, 2 to 4pm* is a span and is work; what makes
	# something an event is its type, never its dates.
	#
	# **No all-day flag of its own, unlike the other three dates.** They are independent facts
	# and each needs its own; an end is the far side of *one* span, so `starts_is_all_day`
	# describes both of its edges. A second column would be a copy that has to be kept equal,
	# which is this codebase's signature defect written into the schema — and it would make
	# *starts all-day, ends at three* representable, which is not a thing anybody means. Input
	# whose two ends disagree in shape is refused by `schedule.check_span` rather than stored.
	ends_at: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)

	# **Renamed from `start_at`**, which carried both meanings and was read as this one by
	# every consumer — so `Dentist at 2pm` was filed under *Unscheduled*, hidden from
	# `--ready`, and described as *"put off until later"*. `readiness.undeferred` reads it
	# to the minute, which is what made the old name expensive rather than merely vague.
	#
	# **The all-day flag survives the rename deliberately.** `#858` — whether a defer should
	# honour a time of day or refuse one by name — is undecided, and dropping the flag here
	# would answer it by accident. Keeping it makes this a pure rename, and leaves either
	# answer reachable without a second migration.
	snoozed_until: sqlalchemy.orm.Mapped[datetime.datetime | None] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=True
	)
	snoozed_is_all_day: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
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
	# How long before this a reminder is wanted, in minutes — `#1211`.
	#
	# **Relative, which is what makes it follow a repeat.** `#577` had already concluded that
	# "one hour before" beats "09:00 on Thursday" because it survives the date moving; the same
	# property is what lets a birthday reminder repeat every year without anything computing a
	# date per occurrence. It is rendered as a `VALARM` hanging off the `VEVENT`, so a client
	# expands it against the `RRULE` itself.
	#
	# **It attaches to the event rather than to a field**, which is how this sidesteps the
	# question `#577` is still open on. An occasion is already one date — whichever of
	# `due_at` and `starts_at` produced it — so the alarm is relative to *that*, and nothing
	# here has to decide whether a reminder is a nudge or a warning.
	#
	# **Minutes, matching `estimate_minutes` beside it**, so one unit is stored throughout and
	# `durations` is the one place that reads "2w" — a second unit here would be the lossless
	# round trip that hides a disagreement.
	reminder_minutes: sqlalchemy.orm.Mapped[int | None] = sqlalchemy.orm.mapped_column(
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

	# **What brings the next occurrence into being**, which the anchor beside it does not say
	# (`#915`). One of them decides *when* the next one falls and this decides *whether one is
	# waiting for you at all* — `db.mixins.RECURRENCE_TRIGGERS` carries the argument.
	recurrence_trigger: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
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

	# When what this task *means* last changed, as opposed to when its row last moved.
	# Claiming it, renewing a lease, re-ranking it or planning it bumps `updated_at` and not
	# this, so a reader can tell a rewrite from a reshuffle.
	#
	# **Which changes count is `domain.events.CONTENT_FIELDS`, and is deliberately not
	# restated here** — this comment named a set the code did not implement for as long as the
	# column has existed (`#1112`), which is the two-copies defect in its cheapest form.
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

	# The same as ``Task.relevance`` and for the same reason — `#823`. A search spans both
	# kinds (§6.2 gives them one ref counter), so an ordering one of them cannot answer would
	# make a merged result set sortable only by dropping half of it.
	relevance: sqlalchemy.orm.Mapped[float | None] = sqlalchemy.orm.query_expression()
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
	# The twin of the task column above, and answering the same question from the same list.
	content_updated_at: sqlalchemy.orm.Mapped[datetime.datetime] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), default=subroutine.db.types.utcnow, nullable=False
	)


class Verification(
	subroutine.db.base.Base,
	subroutine.db.mixins.WorkspaceScopedMixin,
	subroutine.db.mixins.TimestampMixin,
	subroutine.db.mixins.AuthorshipMixin,
):
	"""What was checked against a task, and which tree it was checked on (docs/design.md §14.5).

	**A record, not a proof, and the distinction is the whole design.** An agent can post an
	exit code of zero without running anything, so what this is worth is being *durable*,
	*attributable* and *invalidatable* — never *verified work*. Nothing in the product may say
	otherwise, and `#593` is where that sentence was settled.

	**Bound to the tree, not to the ticket** (`#1121`, `#1124` Q6). §14.5 measured staleness
	against ``task.content_updated_at``, and that column does not move when the *code* moves:
	run the suite at 14:00, edit five files at 14:05, complete at 14:10, and the evidence is
	fresh by that definition and false in fact. **This project has already paid for the lesson
	once** — `#749` and `#893` are two releases that published nothing because a gate run
	beforehand is green on the previous tree, which looks identical in a terminal, and `#894`'s
	remedy was to gate the commit the script makes.

	**Append-only.** There is no version and no soft delete: a record of what was checked at a
	moment is not a thing to edit, and a wrong one is answered by a later one rather than by a
	rewrite. That is `#52`'s reasoning about the event table applied to evidence.
	"""

	__tablename__ = "verification"
	__table_args__ = (
		# What was checked against this task, newest first — the only question anybody asks
		# of this table, and the same shape §10.6 specified before it existed.
		sqlalchemy.Index(
			"ix_verification_workspace_id_task_id_ran_at",
			"workspace_id",
			"task_id",
			"ran_at",
		),
	)

	id: sqlalchemy.orm.Mapped[uuid.UUID] = subroutine.db.mixins.uuid_primary_key()
	task_id: sqlalchemy.orm.Mapped[uuid.UUID] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.uuid_column(),
		sqlalchemy.ForeignKey("task.id", ondelete="CASCADE"),
		nullable=False,
	)

	#: Whether the check passed. A failing record is worth keeping and is the more useful half
	#: of the pair: *this was tried and did not work* is what stops it being tried again.
	passed: sqlalchemy.orm.Mapped[bool] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Boolean, nullable=False
	)

	#: What was run and how it went, in one line — ``5,610 passed, 41 skipped``. Prose, because
	#: what counts as a check differs per project and a schema for it would be §14.15's
	#: mandatory structure for reasoning, which agents omit unless something forces them.
	summary: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Text, nullable=True
	)

	#: Enough of the output to judge the summary by. Capped by the service rather than by the
	#: column, so the refusal can say what the limit is.
	output_excerpt: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.Text, nullable=True
	)
	ran_at: sqlalchemy.orm.Mapped[datetime.datetime] = sqlalchemy.orm.mapped_column(
		subroutine.db.types.UtcDateTime(), nullable=False
	)

	#: The tree this ran against, so the record can expire when the code changes.
	#:
	#: **Nullable, and that is a guard rather than a convenience** (Simon, 2026-08-23). ``NOT
	#: NULL`` is the natural way to write it and would make a record impossible from a machine
	#: with no checkout — which is most of them, and which §1.4 forbids: no §14 entity may be
	#: *required* in order to do the ordinary thing.
	#:
	#: **A record without one is still a record. It simply cannot expire**, and it says so
	#: rather than reading as fresh. The same asymmetry `db/backup` takes about a schema head:
	#: older is handled, equal is handled, absent is a different answer rather than an error.
	tree_hash: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=True
	)

	#: The commit this ran against, where there was one. Beside the tree hash rather than
	#: instead of it: a sha names a commit that may not exist yet — the gate runs *before* the
	#: commit it is about, which is the one commit nothing else ever runs — where a tree hash
	#: names the content either way.
	commit_sha: sqlalchemy.orm.Mapped[str | None] = sqlalchemy.orm.mapped_column(
		sqlalchemy.String(64), nullable=True
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

	Derived from text and never written directly (docs/design.md §6.15): a mention that did not
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


#: What ``q`` can be served from, where a backend exists to serve it (§9.4, item `#823`).
#:
#: **Declared here rather than in ``__table_args__``** because these are expression indexes and
#: an expression needs the mapped columns, which do not exist until the class does. They attach
#: themselves to their table on construction, so binding the result to a name is for the reader
#: rather than for SQLAlchemy — and :func:`subroutine.db.fulltext.index` asserts the attachment
#: happened, because an index that quietly belongs to no table is built by nothing and fails
#: nowhere.
#:
#: **PostgreSQL only, by `#871`'s decision**, and skipped rather than refused elsewhere: the
#: `like` backend answers on SQLite exactly as it always has.
ix_task_search = subroutine.db.fulltext.index(
	"ix_task_search", Task.__table__.c.title, Task.__table__.c.description
)

ix_document_search = subroutine.db.fulltext.index(
	"ix_document_search", Document.__table__.c.title, Document.__table__.c.body
)
