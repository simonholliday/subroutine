"""How a task and a project look on the wire, and the envelope a collection travels in.

**This is deliberately not in the ``api`` package**, and moving it out was a requirement
rather than tidying. SPEC.md §13.7 makes the local database a connection like any other, so
``subroutine today`` fans out across it and every remote through one code path that does not
know which of its answers arrived over a socket — and that is only true if the local client
and the HTTP client return *the same objects*. Two definitions of "a task" that happen to
agree would be two definitions free to stop agreeing. So the schemas live where both can
reach them and the routers are a transport over them, which is what §4's layering rule asks
for anyway. The practical test is ``tests/test_transport_equivalence.py``.

The second consequence: nothing here may import ``fastapi``, or every ``subroutine add``
would pay to load a web framework to print one line. That is why
:func:`subroutine.domain.text.truncated` lives in the domain rather than beside the rest of
``api.shaping``.

Two decisions shape everything else here.

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

import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.system
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.domain.agenda
import subroutine.domain.durations
import subroutine.domain.links
import subroutine.domain.refs
import subroutine.domain.tags
import subroutine.domain.text

Item = typing.TypeVar("Item")


class Page(pydantic.BaseModel):
	"""Where a collection response sits in the sequence it came from."""

	limit: int
	next_cursor: str | None = None
	has_more: bool = False

	#: Null unless ``include_total=true`` was asked for.
	total: int | None = None


# ``LinkEnd`` and ``Edge`` sit above ``Collection`` because ``Collection.links`` annotates
# a field with ``Edge``, and a pydantic field annotation is evaluated when the class body
# runs — not lazily. Defined below, this module imports fine under mypy and raises
# ``NameError`` on import. Same trap as ``Item`` above ``World`` in ``cli/personal.py``.
class LinkEnd(pydantic.BaseModel):
	"""What is at the far end of a link, with enough of the row to render it.

	Enough, and no more. A caller looking at an item's links wants to know what it is joined
	to, not to receive every field of everything it touches — and an end the caller may not
	see is never reported at all, which is :mod:`subroutine.domain.links`' obligation rather
	than this model's.
	"""

	entity_type: str
	id: uuid.UUID
	ref: int
	title: str


class Edge(pydantic.BaseModel):
	"""A link among a page's items, named by both its ends (SPEC.md §5.7, §8.4).

	The counterpart to :class:`Link`, which is the same row seen from one item. There is no
	``direction`` here and no inverted label, because a listing has no single vantage point
	to invert for — and an edge that named only "the other end" would be meaningless when
	both ends are on the page.
	"""

	id: uuid.UUID
	link_type: str

	#: The forward title only — "Blocks", never "Blocked by". A client wanting the inverse
	#: reads it from the target's side.
	label: str

	source: LinkEnd
	target: LinkEnd

	def address (self) -> str:
		"""Return what a caller addresses this by. A link has no ref of its own."""

		return str(self.id)


class Collection(pydantic.BaseModel, typing.Generic[Item]):
	"""Every list response, in one shape."""

	items: list[Item]
	page: Page

	# **``?include=links`` adds a `links` sibling and it is deliberately not a field here.**
	# A pydantic field with a default of ``None`` is *serialised*, so declaring it would put
	# `"links": null` on every listing of every entity for the benefit of the callers not
	# using it — §14.10 exists to stop exactly that. So the include path returns a
	# ``JSONResponse`` instead, which FastAPI passes through untouched, and a listing that did
	# not ask is byte-for-byte what it was. The shape is ``views.Edge``; the parameter's own
	# description documents it, since OpenAPI cannot see a key that is not on the model.


class Instance(pydantic.BaseModel):
	"""Which installation this is, and where it thinks it is.

	``id`` is the one value in this program that must never change (SPEC.md §13.7). A client
	keys its caches on it, notices the same instance configured twice under two names by it,
	and labels merged results with it — so an id that moved would silently corrupt all three
	at once. ``name`` is the server's own label and may be changed freely; neither is the
	*connection* name, which is the nickname in the reader's own configuration.

	``timezone`` is here so that a merged view can say what 16:00 on a New York server is
	*there*, which is the difference between a calendar entry a person can act on and one
	they have to do arithmetic on.
	"""

	id: uuid.UUID
	name: str
	timezone: str


class WorkspaceRef(pydantic.BaseModel):
	"""One workspace, by both the name it is stored under and the name a person types.

	Typed rather than left as a bare mapping because a client resolves ``acme/42`` through
	this: the slug comes off a command line and the id goes into a query.
	"""

	id: uuid.UUID
	slug: str
	title: str


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

	#: The parent's **ref and title**, resolved. A ref is how an item is addressed (§6.2), so
	#: a client given only `parent_task_id` has to fetch the parent before it can print
	#: anything at all — and on a listing that is one call per row. Both are batch-loaded with
	#: the status and project names, and both are null when the item has no parent.
	#:
	#: Denormalised like `project_key` and `status`, and for the same reason: a response that
	#: forces a second call to be readable is the failure review dimension 4 names.
	parent_ref: int | None = None
	parent_title: str | None = None

	#: The vocabulary, resolved. ``status`` is the key an installation may have renamed;
	#: ``status_category`` is the fixed set a client can branch on (SPEC.md §5.5).
	status: str
	status_category: str
	status_id: uuid.UUID

	#: Whether this is the status every item starts in, so a surface can tell a decision
	#: somebody made from the absence of one. `#168`: without it `subroutine show` had no way
	#: to print `blocked` while staying quiet about `open`, so it printed neither — and a
	#: status somebody set was stored and then invisible everywhere.
	status_is_default: bool = False
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

	#: §6.4 promises both spellings, and until 2026-07-30 only the first was here — the
	#: section, two docstrings in ``domain.durations`` and a test docstring all described a
	#: response field that no response carried. ``estimate_human`` is what a person would
	#: say, and it feeds straight back into ``estimate`` on a write: an agent that reads
	#: ``"1h 30m"`` and sends it back is understood.
	estimate_minutes: int | None
	estimate_human: str | None

	#: The tag names on this task, alphabetical. Batch-loaded per page like the vocabulary
	#: above and for the same reason. A tag is never an id here: a client acts on the word,
	#: applies one by writing ``#health`` in a captured line, and would have to fetch a
	#: second list to learn what any id meant.
	tags: list[str] = pydantic.Field(default_factory=list)

	completed_at: datetime.datetime | None
	archived_at: datetime.datetime | None
	deleted_at: datetime.datetime | None

	created_at: datetime.datetime
	updated_at: datetime.datetime

	#: When the *meaning* last changed, as against when the row did (§6.1). Reported on a
	#: document since it existed and not on a task, off the same distinction — an
	#: inconsistency rather than an absence, which is the harder kind to notice.
	content_updated_at: datetime.datetime

	#: Who made it and who last changed it (§6.1). **Ids rather than resolved names**, the same
	#: choice ``assignee_id`` makes and for ``views.Event``'s reason: resolving every actor on
	#: every page is what the compact format exists to avoid, and a client that wants a name
	#: asks once and caches it.
	#:
	#: Null where a system action wrote the row — ``domain.bootstrap`` runs before any
	#: principal exists — so null means "nobody was signed in", never "unknown".
	created_by: uuid.UUID | None
	updated_by: uuid.UUID | None


	#: The concurrency token (SPEC.md §8.9), reported so a caller can send it back.
	version: int

	def address (self) -> int:
		"""Return what a caller addresses this by — its ref (SPEC.md §6.2)."""

		return self.ref

	def columns (self) -> tuple[str, ...]:
		"""Return this task as the cells of one compact line (SPEC.md §14.10).

		Each view renders its own columns because each knows which of its fields are worth a
		line, and the alignment across a page is ``shaping.aligned``'s job. The order is the
		one §14.10 gives: address, status, priority, deadline, plan, title, tags.

		Tags and the plan cost nothing on a page that has neither: ``shaping.aligned`` drops
		a column that is empty in every row, so the common case is the line it was before
		they existed.

		**The plan is written ``→2026-08-01`` and the deadline is bare, and that asymmetry is
		the point.** Two adjacent date columns would be told apart only by position, and
		position is exactly what a dropped column takes away — a page with no plans would
		shift every later cell one place left, so an agent parsing by index would read a
		deadline as a plan on some pages and not others. A marked cell says what it is
		wherever it lands. The deadline stays bare because it is never dropped and because
		marking it would cost four characters a row on every listing ever made.

		``@assignee`` still appears in §14.10's example and not here. It needs a username
		rather than an ``assignee_id``, which is a lookup this view does not carry — view
		*enrichment* rather than shaping, and filed as such rather than half-done.
		"""

		return (
			subroutine.domain.refs.format_ref(self.ref),
			f"[{self.status}]",
			_priority_cell(self.importance, self.urgency),
			"—" if self.due_at is None else self.due_at.date().isoformat(),
			"" if self.planned_for is None else f"→{self.planned_for.isoformat()}",
			subroutine.domain.text.truncated(self.title),
			" ".join(f"#{name}" for name in self.tags),
		)


class Comment(pydantic.BaseModel):
	"""One entry in an item's record of what happened (SPEC.md §5.10).

	No ``parent_comment_id``: comments are flat and chronological by decision, and the column
	stays in the schema as the escape hatch rather than as a field anybody can set.
	"""

	id: uuid.UUID
	body: str

	entity_type: str
	entity_id: uuid.UUID
	workspace_id: uuid.UUID
	author_id: uuid.UUID | None

	deleted_at: datetime.datetime | None
	created_at: datetime.datetime
	updated_at: datetime.datetime
	version: int

	def address (self) -> str:
		"""Return what a caller addresses this by. A comment has no ref of its own."""

		return str(self.id)

	def columns (self) -> tuple[str, ...]:
		"""Return this comment as the cells of one compact line."""

		return (
			self.created_at.date().isoformat(),
			subroutine.domain.text.truncated(self.body),
		)


class Event(pydantic.BaseModel):
	"""One thing that happened, as the history and the change feed both report it (§5.11).

	**Addressed by ``seq``, not by ``id``.** The sequence number is the primary key, and it
	is the only field a client can order or resume on; the UUID is carried because every
	other entity here has one and a client keying a local cache by id should not have to
	special-case this table.

	``changes`` is whatever the service that recorded it chose to say — ``{"status": {"from":
	…, "to": …}}`` and similar. Deliberately untyped: the shape belongs to the action, a new
	action adds its own without a migration or a schema change here, and a client reads it
	after switching on ``action``.

	**Both actor fields, and both nullable.** A system action has no user and a
	session-authenticated one has no token; recording which is which is what makes an audit
	trail worth reading (§5.11). Ids rather than names, per §8.5 — an unrequested relation is
	an id, and resolving every actor on every page is what the compact format exists to avoid.

	``subject_*`` is what the event happened *on* when that differs from the entity, and it is
	null for almost everything. A comment's event names the comment and is reported in the
	commented-on item's history, so a client rendering that history needs the subject to tell
	"somebody edited this task" from "somebody commented on it" without a second call.
	"""

	seq: int
	id: uuid.UUID

	entity_type: str
	entity_id: uuid.UUID
	workspace_id: uuid.UUID

	subject_type: str | None
	subject_id: uuid.UUID | None

	action: str
	changes: dict[str, typing.Any] | None

	actor_user_id: uuid.UUID | None
	actor_token_id: uuid.UUID | None

	created_at: datetime.datetime

	def address (self) -> str:
		"""Return what a caller addresses this by — its sequence number."""

		return str(self.seq)

	def columns (self) -> tuple[str, ...]:
		"""Return this event as the cells of one compact line."""

		return (
			str(self.seq),
			self.created_at.date().isoformat(),
			self.action,
			self.entity_type,
		)


class Link(pydantic.BaseModel):
	"""One link, seen from the item that was asked about (SPEC.md §5.7).

	A link is one stored row displayed from both ends, so ``label`` arrives already the right
	way round: "Blocks" from one end and "Blocked by" from the other, off the same row. A
	client that had to invert it would be a second place the inverse could be got wrong.
	"""

	id: uuid.UUID
	link_type: str

	label: str
	direction: str
	other: LinkEnd

	def address (self) -> str:
		"""Return what a caller addresses this by. A link has no ref of its own."""

		return str(self.id)

	def columns (self) -> tuple[str, ...]:
		"""Return this link as the cells of one compact line."""

		return (
			self.label,
			subroutine.domain.refs.format_ref(self.other.ref),
			subroutine.domain.text.truncated(self.other.title),
		)


class Workspace(pydantic.BaseModel):
	"""A workspace as the API reports it.

	Richer than :class:`WorkspaceRef`, which is the two-field form embedded in other responses.
	This is what ``/v1/workspaces`` returns, and the difference is that a caller reading *this*
	is administering the workspace rather than resolving an address through it.

	``next_ref_number`` is deliberately absent. It is the counter behind the ref sequence, and
	publishing it would invite a client to predict the next ref — which is exactly the guess that
	breaks the moment two writes race.
	"""

	id: uuid.UUID
	slug: str
	title: str
	description: str | None

	#: Null means "not stated", which lets the instance's zone show through (§12.3). It is not
	#: a missing value to be helpfully defaulted.
	timezone: str | None

	settings: dict[str, typing.Any]

	deleted_at: datetime.datetime | None
	created_at: datetime.datetime
	updated_at: datetime.datetime
	version: int

	def address (self) -> str:
		"""Return what a caller addresses this by — its short name (SPEC.md §13.7)."""

		return self.slug

	def columns (self) -> tuple[str, ...]:
		"""Return this workspace as the cells of one compact line."""

		return (
			self.slug,
			self.timezone or "—",
			subroutine.domain.text.truncated(self.title),
		)


class User(pydantic.BaseModel):
	"""An account as the API reports it — item ``#174``.

	**No email address, deliberately.** Everything here is an *identifier* or a fact about what
	the account can do, both of which somebody adding a colleague to a workspace needs. An email
	is personal data, it is needed for none of that, and a directory that hands one to every
	authenticated caller is a directory that leaks by default rather than on purpose. Decision
	``#161``'s line is the one being followed: identifiers are unique and public, content is
	neither.

	``is_service_account`` is reported because it changes what a name *means*. A list mixing
	people and agents with nothing to tell them apart is one where somebody eventually adds the
	robot to the stand-up.
	"""

	id: uuid.UUID
	username: str
	display_name: str | None
	is_service_account: bool
	is_superuser: bool
	is_active: bool

	#: Null means "not stated", so the workspace's zone and then the instance's show through
	#: (§12.3). It is not a missing value to be helpfully defaulted.
	timezone: str | None

	created_at: datetime.datetime

	def address (self) -> str:
		"""Return what a caller addresses this by — the username."""

		return self.username

	def columns (self) -> tuple[str, ...]:
		"""Return this account as the cells of one compact line."""

		return (
			self.username,
			"agent" if self.is_service_account else "person",
			"admin" if self.is_superuser else "",
			"" if self.is_active else "inactive",
			self.display_name or "",
		)


class Member(pydantic.BaseModel):
	"""One person's role in one workspace — item ``#174``.

	The join is reported as a thing in its own right rather than as a field on either side,
	because that is what it is: §7.3a grants sight of a private project to holders of a
	``project_member`` row, and membership of a workspace is the same shape one level up.
	"""

	user: User
	role: str
	workspace: WorkspaceRef
	created_at: datetime.datetime

	def address (self) -> str:
		"""Return what a caller addresses this by — the member's username."""

		return self.user.username

	def columns (self) -> tuple[str, ...]:
		"""Return this membership as the cells of one compact line."""

		return (
			self.user.username,
			self.role,
			"agent" if self.user.is_service_account else "person",
			self.user.display_name or "",
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
	#: Whether this is the status it starts in — see :class:`Task`, same reason (`#168`).
	status_is_default: bool = False

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
			subroutine.domain.text.truncated(self.title),
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
	#: Whether this is the status it starts in — see :class:`Task`, same reason (`#168`).
	status_is_default: bool = False
	type: str
	type_id: uuid.UUID

	owner_id: uuid.UUID | None
	supersedes_id: uuid.UUID | None

	archived_at: datetime.datetime | None
	deleted_at: datetime.datetime | None
	created_at: datetime.datetime
	updated_at: datetime.datetime
	content_updated_at: datetime.datetime

	#: Who made it and who last changed it (§6.1). **Ids rather than resolved names**, the same
	#: choice ``assignee_id`` makes and for ``views.Event``'s reason: resolving every actor on
	#: every page is what the compact format exists to avoid, and a client that wants a name
	#: asks once and caches it.
	#:
	#: Null where a system action wrote the row — ``domain.bootstrap`` runs before any
	#: principal exists — so null means "nobody was signed in", never "unknown".
	created_by: uuid.UUID | None
	updated_by: uuid.UUID | None

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
			subroutine.domain.text.truncated(self.title),
		)


class Agenda(pydantic.BaseModel):
	"""The four buckets, and what they were computed against.

	``date`` and ``timezone`` are both reported because "today" is not a fact about the
	server (SPEC.md §6.5) — and a client merging several instances resolves the date *once*,
	in its own zone, then asks every connection for that explicit day. Without that, a person
	whose work profile says ``America/New_York`` and whose personal one says
	``Europe/London`` would get two different days merged into one list.
	"""

	date: datetime.date
	timezone: str

	overdue: list[Task]
	today: list[Task]
	upcoming: list[Task]
	unscheduled: list[Task]

	#: How many unscheduled tasks there are in total, which is usually more than are listed:
	#: an agenda that dumped a 400-item backlog would not be an agenda.
	unscheduled_total: int


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
		task_ids: typing.Iterable[uuid.UUID] = (),
		parent_ids: typing.Iterable[uuid.UUID] = (),
	) -> None:
		"""Load the vocabulary rows these ids refer to."""

		self.statuses = _by_id(
			session,
			subroutine.db.models.vocabulary.Status,
			status_ids,
			("key", "category", "is_default"),
		)
		self.types = _by_id(
			session, subroutine.db.models.vocabulary.ItemType, type_ids, ("key",)
		)
		self.projects = _by_id(
			session, subroutine.db.models.project.Project, project_ids, ("key",)
		)
		self.tags = subroutine.domain.tags.names_for_tasks(session, task_ids)

		# **One query for every parent on the page, not one per row.** A ref is how an item
		# is addressed (§6.2), so a view reporting only `parent_task_id` forces every client
		# to resolve a UUID before it can print anything — which is the second call review
		# dimension 4 exists to prevent, multiplied by the page.
		self.parents = _by_id(
			session, subroutine.db.models.work.Task, parent_ids, ("ref", "title")
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
			task_ids={task.id for task in tasks},
			parent_ids={task.parent_task_id for task in tasks if task.parent_task_id},
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
		parent_ref=_parent_field(vocabulary, row.parent_task_id, "ref"),
		parent_title=_parent_field(vocabulary, row.parent_task_id, "title"),
		status=str(status.get("key", "")),
		status_category=str(status.get("category", "")),
		status_is_default=bool(status.get("is_default", False)),
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
		content_updated_at=row.content_updated_at,
		created_by=row.created_by,
		updated_by=row.updated_by,
		estimate_minutes=row.estimate_minutes,
		estimate_human=(
			None
			if row.estimate_minutes is None
			else subroutine.domain.durations.humanize(row.estimate_minutes)
		),
		tags=vocabulary.tags.get(row.id, []),
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
		status_is_default=bool(status.get("is_default", False)),
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
		created_by=row.created_by,
		updated_by=row.updated_by,
		version=row.version,
	)


def comment (row: subroutine.db.models.activity.Comment) -> Comment:
	"""Render one comment."""

	return Comment(
		id=row.id,
		body=row.body,
		entity_type=row.entity_type,
		entity_id=row.entity_id,
		workspace_id=row.workspace_id,
		author_id=row.author_id,
		deleted_at=row.deleted_at,
		created_at=row.created_at,
		updated_at=row.updated_at,
		version=row.version,
	)


def event (row: subroutine.db.models.activity.Event) -> Event:
	"""Render one event.

	No vocabulary argument: an event's ``action`` is an open string rather than a seeded
	vocabulary row (§5.11), so there is nothing to batch-load and nothing to resolve.
	"""

	return Event(
		seq=row.seq,
		id=row.id,
		entity_type=row.entity_type,
		entity_id=row.entity_id,
		workspace_id=row.workspace_id,
		subject_type=row.subject_type,
		subject_id=row.subject_id,
		action=row.action,
		changes=None if row.changes is None else dict(row.changes),
		actor_user_id=row.actor_user_id,
		actor_token_id=row.actor_token_id,
		created_at=row.created_at,
	)



def edge (found: subroutine.domain.links.Edge) -> Edge:
	"""Render one link as a stored fact rather than as somebody's view of it."""

	return Edge(
		id=found.id,
		link_type=found.link_type,
		label=found.label,
		source=_end(found.source),
		target=_end(found.target),
	)


def _end (end: subroutine.domain.links.End) -> LinkEnd:
	"""Render one end of a link — enough of the row to identify and show it, no more."""

	return LinkEnd(
		entity_type=end.entity_type, id=end.id, ref=end.ref, title=end.title
	)


def link (related: subroutine.domain.links.Related) -> Link:
	"""Render one link, from the point of view of the item it was asked about.

	Takes the domain's own :class:`~subroutine.domain.links.Related` rather than the stored
	row, because working out which end is "the other one" and which way round the label reads
	is the domain's job and is already done by the time this is called.
	"""

	return Link(
		id=related.id,
		link_type=related.link_type,
		label=related.label,
		direction=related.direction,
		other=LinkEnd(
			entity_type=related.other.entity_type,
			id=related.other.id,
			ref=related.other.ref,
			title=related.other.title,
		),
	)


def workspace (row: subroutine.db.models.identity.Workspace) -> Workspace:
	"""Render one workspace.

	No vocabulary argument: a workspace has no status or item type to resolve, which is what
	makes it the one entity here that needs no batch loading.
	"""

	return Workspace(
		id=row.id,
		slug=row.slug,
		title=row.title,
		description=row.description,
		timezone=row.timezone,
		settings=dict(row.settings or {}),
		deleted_at=row.deleted_at,
		created_at=row.created_at,
		updated_at=row.updated_at,
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
		status_is_default=bool(status.get("is_default", False)),
		status_id=row.status_id,
		settings=dict(row.settings),
		archived_at=row.archived_at,
		deleted_at=row.deleted_at,
		created_at=row.created_at,
		updated_at=row.updated_at,
		version=row.version,
	)


def agenda (
	session: sqlalchemy.orm.Session, built: subroutine.domain.agenda.Agenda
) -> Agenda:
	"""Render a built agenda, loading the vocabulary for all four buckets at once.

	One :class:`Vocabulary` across the whole thing rather than one per bucket: the same
	three statuses turn up in every bucket, and four loads would be three too many.
	"""

	everything = [*built.overdue, *built.today, *built.upcoming, *built.unscheduled]
	vocabulary = Vocabulary.for_tasks(session, everything)

	return Agenda(
		date=built.date,
		timezone=built.timezone,
		overdue=[task(row, vocabulary) for row in built.overdue],
		today=[task(row, vocabulary) for row in built.today],
		upcoming=[task(row, vocabulary) for row in built.upcoming],
		unscheduled=[task(row, vocabulary) for row in built.unscheduled],
		unscheduled_total=built.unscheduled_total,
	)


def instance (row: subroutine.db.models.system.Instance) -> Instance:
	"""Render this installation's identity."""

	return Instance(id=row.id, name=row.name, timezone=row.timezone)


def user (row: subroutine.db.models.identity.User) -> User:
	"""Render one account, without its email address or its password hash."""

	return User(
		id=row.id,
		username=row.username,
		display_name=row.display_name,
		is_service_account=row.is_service_account,
		is_superuser=row.is_superuser,
		is_active=row.is_active,
		timezone=row.timezone,
		created_at=row.created_at,
	)


def member (
	row: subroutine.db.models.identity.WorkspaceMember,
	*,
	account: subroutine.db.models.identity.User,
	role: subroutine.db.models.identity.Role,
	within: subroutine.db.models.identity.Workspace,
) -> Member:
	"""Render one membership, with the three things it joins already resolved.

	Handed the rows rather than fetching them, so a listing loads them once and this does not
	become §8.4's N+1 wearing a rendering hat.
	"""

	return Member(
		user=user(account),
		role=role.key,
		workspace=workspace_ref(within),
		created_at=row.created_at,
	)


def workspace_ref (row: subroutine.db.models.identity.Workspace) -> WorkspaceRef:
	"""Render one workspace as a client addresses it."""

	return WorkspaceRef(id=row.id, slug=row.slug, title=row.title)


def _parent_field (
	vocabulary: Vocabulary, parent_id: uuid.UUID | None, field: str
) -> typing.Any:
	"""Return one field of an item's parent, or ``None`` when it has none.

	**A missing entry is ``None`` rather than an error**, deliberately. The parent may be
	outside what this caller can see — §7.3a hides a private project's contents — and the
	right answer there is "no parent reported", not a refusal that confirms one exists.
	"""

	if parent_id is None:
		return None

	return vocabulary.parents.get(parent_id, {}).get(field)


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
