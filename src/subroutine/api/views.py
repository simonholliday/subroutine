"""How a task and a project look on the wire, and the envelope a collection travels in.

Two decisions shape everything here.

**Vocabulary is resolved, not left as ids.** A task carries ``status`` and ``type`` as
*keys* — ``"in_progress"``, ``"bug"`` — as well as the ids. §8.5 says unrequested relations
appear as ids only, and that is right for an assignee or a parent; it is wrong for the
status, because a caller cannot act on ``status_id`` without fetching the vocabulary, and
an agent that has to do that on every listing pays for it in context on every listing
(SPEC.md §13.1). The keys are batch-loaded per page, never per row.

**A collection is enveloped and a single entity is not** (§8.4). ``total`` is null unless
asked for, because an exact count is a second full scan for a number most callers ignore.
"""

import datetime
import typing
import uuid

import pydantic
import sqlalchemy
import sqlalchemy.orm

import subroutine.api.shaping
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.domain.refs

Item = typing.TypeVar("Item")


class Page(pydantic.BaseModel):
	"""Where a collection response sits in the sequence it came from."""

	limit: int
	next_cursor: str | None = None
	has_more: bool = False

	#: Null unless ``include_total=true`` was asked for.
	total: int | None = None


class Collection(pydantic.BaseModel, typing.Generic[Item]):
	"""Every list response, in one shape."""

	items: list[Item]
	page: Page


class Task(pydantic.BaseModel):
	"""A task as the API reports it."""

	id: uuid.UUID
	ref: int
	title: str
	description: str | None

	workspace_id: uuid.UUID
	project_id: uuid.UUID
	project_key: str
	parent_task_id: uuid.UUID | None

	#: The vocabulary, resolved. ``status`` is the key an installation may have renamed;
	#: ``status_category`` is the fixed set a client can branch on (SPEC.md §5.5).
	status: str
	status_category: str
	status_id: uuid.UUID
	type: str
	type_id: uuid.UUID

	assignee_id: uuid.UUID | None

	#: §6.3's two independent axes, 1-5 where 5 is highest, and the product of them.
	#: Null means *not assessed* and is distinct from 1. ``priority_score`` is derived and
	#: read-only — null unless both axes are set — and exists so that an agent sorting by
	#: "most important" has one key rather than a convention it invented.
	importance: int | None
	urgency: int | None
	priority_score: int | None

	due_at: datetime.datetime | None
	due_is_all_day: bool
	planned_for: datetime.date | None
	start_at: datetime.datetime | None
	start_is_all_day: bool
	timezone: str | None

	estimate_minutes: int | None
	completed_at: datetime.datetime | None
	archived_at: datetime.datetime | None
	deleted_at: datetime.datetime | None

	created_at: datetime.datetime
	updated_at: datetime.datetime

	#: The concurrency token (SPEC.md §8.9), reported so a caller can send it back.
	version: int

	def address (self) -> int:
		"""Return what a caller addresses this by — its ref (SPEC.md §6.2)."""

		return self.ref

	def columns (self) -> tuple[str, ...]:
		"""Return this task as the cells of one compact line (SPEC.md §14.10).

		Each view renders its own columns because each knows which of its fields are worth a
		line, and the alignment across a page is ``shaping.aligned``'s job. The order is the
		one §14.10 gives: address, status, priority, deadline, title.

		``@assignee`` and ``#tags`` appear in §14.10's example and not here. Both need data
		this view does not carry — a username rather than an ``assignee_id``, and tag rows
		that would be another batched query per page — so they are view *enrichment* rather
		than shaping, and filed as such rather than half-done.
		"""

		return (
			subroutine.domain.refs.format_ref(self.ref),
			f"[{self.status}]",
			_priority_cell(self.importance, self.urgency),
			"—" if self.due_at is None else self.due_at.date().isoformat(),
			subroutine.api.shaping.truncated(self.title),
		)


class Project(pydantic.BaseModel):
	"""A project as the API reports it."""

	id: uuid.UUID
	key: str
	title: str
	description: str | None

	workspace_id: uuid.UUID
	parent_id: uuid.UUID | None
	depth: int

	visibility: str
	template: str
	is_inbox: bool
	owner_id: uuid.UUID | None

	status: str
	status_category: str
	status_id: uuid.UUID

	settings: dict[str, typing.Any]

	archived_at: datetime.datetime | None
	deleted_at: datetime.datetime | None
	created_at: datetime.datetime
	updated_at: datetime.datetime
	version: int

	def address (self) -> str:
		"""Return what a caller addresses this by — its key, never a ref (SPEC.md §5.2)."""

		return self.key

	def columns (self) -> tuple[str, ...]:
		"""Return this project as the cells of one compact line."""

		return (
			self.key,
			f"[{self.status}]",
			"private" if self.visibility == "private" else "",
			subroutine.api.shaping.truncated(self.title),
		)


class Document(pydantic.BaseModel):
	"""A document as the API reports it.

	No ``due_at``, ``planned_for``, ``estimate_minutes`` or ``assignee_id``, and their
	absence is the point (SPEC.md §6.14): a specification is never "done" and nobody is
	working on it. A deadline about a document belongs on a task that ``documents`` it.
	"""

	id: uuid.UUID
	ref: int
	title: str
	body: str | None

	workspace_id: uuid.UUID
	project_id: uuid.UUID
	project_key: str
	parent_id: uuid.UUID | None

	status: str
	status_category: str
	status_id: uuid.UUID
	type: str
	type_id: uuid.UUID

	owner_id: uuid.UUID | None
	supersedes_id: uuid.UUID | None

	archived_at: datetime.datetime | None
	deleted_at: datetime.datetime | None
	created_at: datetime.datetime
	updated_at: datetime.datetime
	content_updated_at: datetime.datetime
	version: int

	def address (self) -> int:
		"""Return what a caller addresses this by — its ref, shared with tasks (§6.2)."""

		return self.ref

	def columns (self) -> tuple[str, ...]:
		"""Return this document as the cells of one compact line.

		No deadline and no priority column, for the reason the class docstring gives: a
		document has neither, and padding the line with two em dashes would spend tokens
		saying so on every row.
		"""

		return (
			subroutine.domain.refs.format_ref(self.ref),
			f"[{self.status}]",
			self.type,
			subroutine.api.shaping.truncated(self.title),
		)


def _priority_cell (importance: int | None, urgency: int | None) -> str:
	"""Render §6.3's two axes as one cell: ``I4/U5``, or a dash for what was not assessed.

	Absence is distinct from 1 and has to read as absence — an unassessed task showing
	``I1/U1`` would be a lie a client would sort on.
	"""

	if importance is None and urgency is None:
		return "—"

	return f"I{importance or '-'}/U{urgency or '-'}"


class Vocabulary:
	"""The status, type and project names a page of rows needs, fetched once.

	Three queries for a page of fifty rows rather than a hundred and fifty. Built as an
	object rather than passed as three dictionaries because every renderer below needs all
	three, and a signature that takes three lookup tables invites one of them being stale.
	"""

	def __init__ (
		self,
		session: sqlalchemy.orm.Session,
		*,
		status_ids: typing.Iterable[uuid.UUID] = (),
		type_ids: typing.Iterable[uuid.UUID] = (),
		project_ids: typing.Iterable[uuid.UUID] = (),
	) -> None:
		"""Load the vocabulary rows these ids refer to."""

		self.statuses = _by_id(
			session, subroutine.db.models.vocabulary.Status, status_ids, ("key", "category")
		)
		self.types = _by_id(
			session, subroutine.db.models.vocabulary.ItemType, type_ids, ("key",)
		)
		self.projects = _by_id(
			session, subroutine.db.models.project.Project, project_ids, ("key",)
		)

	@classmethod
	def for_tasks (
		cls, session: sqlalchemy.orm.Session, tasks: typing.Sequence[subroutine.db.models.work.Task]
	) -> "Vocabulary":
		"""Load everything a page of tasks needs to be rendered."""

		return cls(
			session,
			status_ids={task.status_id for task in tasks},
			type_ids={task.type_id for task in tasks},
			project_ids={task.project_id for task in tasks},
		)

	@classmethod
	def for_documents (
		cls,
		session: sqlalchemy.orm.Session,
		documents: typing.Sequence[subroutine.db.models.work.Document],
	) -> "Vocabulary":
		"""Load everything a page of documents needs to be rendered."""

		return cls(
			session,
			status_ids={document.status_id for document in documents},
			type_ids={document.type_id for document in documents},
			project_ids={document.project_id for document in documents},
		)

	@classmethod
	def for_projects (
		cls,
		session: sqlalchemy.orm.Session,
		projects: typing.Sequence[subroutine.db.models.project.Project],
	) -> "Vocabulary":
		"""Load everything a page of projects needs to be rendered."""

		return cls(session, status_ids={project.status_id for project in projects})


def task (
	row: subroutine.db.models.work.Task, vocabulary: Vocabulary
) -> Task:
	"""Render one task."""

	status = vocabulary.statuses.get(row.status_id, {})

	return Task(
		id=row.id,
		ref=row.ref,
		title=row.title,
		description=row.description,
		workspace_id=row.workspace_id,
		project_id=row.project_id,
		project_key=str(vocabulary.projects.get(row.project_id, {}).get("key", "")),
		parent_task_id=row.parent_task_id,
		status=str(status.get("key", "")),
		status_category=str(status.get("category", "")),
		status_id=row.status_id,
		type=str(vocabulary.types.get(row.type_id, {}).get("key", "")),
		type_id=row.type_id,
		assignee_id=row.assignee_id,
		importance=row.importance,
		urgency=row.urgency,
		# Computed here rather than read from the database: §6.3 calls it derived, and a
		# stored copy would be a second place for the two axes to disagree.
		priority_score=(
			None
			if row.importance is None or row.urgency is None
			else row.importance * row.urgency
		),
		due_at=row.due_at,
		due_is_all_day=row.due_is_all_day,
		planned_for=row.planned_for,
		start_at=row.start_at,
		start_is_all_day=row.start_is_all_day,
		timezone=row.timezone,
		estimate_minutes=row.estimate_minutes,
		completed_at=row.completed_at,
		archived_at=row.archived_at,
		deleted_at=row.deleted_at,
		created_at=row.created_at,
		updated_at=row.updated_at,
		version=row.version,
	)


def document (
	row: subroutine.db.models.work.Document, vocabulary: Vocabulary
) -> Document:
	"""Render one document."""

	status = vocabulary.statuses.get(row.status_id, {})

	return Document(
		id=row.id,
		ref=row.ref,
		title=row.title,
		body=row.body,
		workspace_id=row.workspace_id,
		project_id=row.project_id,
		project_key=str(vocabulary.projects.get(row.project_id, {}).get("key", "")),
		parent_id=row.parent_id,
		status=str(status.get("key", "")),
		status_category=str(status.get("category", "")),
		status_id=row.status_id,
		type=str(vocabulary.types.get(row.type_id, {}).get("key", "")),
		type_id=row.type_id,
		owner_id=row.owner_id,
		supersedes_id=row.supersedes_id,
		archived_at=row.archived_at,
		deleted_at=row.deleted_at,
		created_at=row.created_at,
		updated_at=row.updated_at,
		content_updated_at=row.content_updated_at,
		version=row.version,
	)


def project (
	row: subroutine.db.models.project.Project, vocabulary: Vocabulary
) -> Project:
	"""Render one project."""

	status = vocabulary.statuses.get(row.status_id, {})

	return Project(
		id=row.id,
		key=row.key,
		title=row.title,
		description=row.description,
		workspace_id=row.workspace_id,
		parent_id=row.parent_id,
		depth=row.depth,
		visibility=row.visibility,
		template=row.template,
		is_inbox=row.is_inbox,
		owner_id=row.owner_id,
		status=str(status.get("key", "")),
		status_category=str(status.get("category", "")),
		status_id=row.status_id,
		settings=dict(row.settings),
		archived_at=row.archived_at,
		deleted_at=row.deleted_at,
		created_at=row.created_at,
		updated_at=row.updated_at,
		version=row.version,
	)


def _by_id (
	session: sqlalchemy.orm.Session,
	model: typing.Any,
	identifiers: typing.Iterable[uuid.UUID],
	fields: typing.Sequence[str],
) -> dict[uuid.UUID, dict[str, typing.Any]]:
	"""Fetch named columns for a set of ids, as one query keyed by id.

	Reads only the columns asked for. A page of tasks needs three strings out of the status
	table and nothing else, and loading whole ORM objects to read one attribute is a cost
	paid on every listing.
	"""

	wanted = {identifier for identifier in identifiers if identifier is not None}

	if not wanted:
		return {}

	columns = [getattr(model, name) for name in fields]
	rows = session.execute(
		sqlalchemy.select(model.id, *columns).where(model.id.in_(wanted))
	).all()

	return {row[0]: dict(zip(fields, row[1:], strict=True)) for row in rows}
