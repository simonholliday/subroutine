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

import subroutine
import subroutine.db.migrate
import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.system
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.types
import subroutine.domain.agenda
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.durations
import subroutine.domain.events
import subroutine.domain.links
import subroutine.domain.projects
import subroutine.domain.refs
import subroutine.domain.tags
import subroutine.domain.text
import subroutine.domain.workspaces
import subroutine.installations

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

	#: Whether the thing at this end is finished (`#210`). A link is how `#84` models a
	#: milestone — an item whose blockers are its contents — so a client rendering "N of M"
	#: needs this and would otherwise have to fetch every end to count them.
	#:
	#: **Only a task can be finished.** A document has no state that could, so one is reported
	#: as incomplete rather than judged by a status it does not have.
	is_complete: bool = False


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

	#: Who holds a lease on this, and until when (§14.11, `#350`). **Ids, not names**, which is
	#: §8.5's rule for an unrequested relation and is how ``assignee_id`` is already reported —
	#: a caller that needs the name asks once for the accounts rather than paying for one on
	#: every row of every listing.
	#:
	#: **An expired lease is still reported.** Who was working on this is worth knowing even
	#: once the lease has run out, and ``claim_expires_at`` against the clock is what says
	#: whether it still counts — the same reading ``domain.claims.held_by`` applies. Defaulted,
	#: so a client can read a response from an instance that predates them (`#345`).
	claimed_by_id: uuid.UUID | None = None
	claimed_at: datetime.datetime | None = None
	claim_expires_at: datetime.datetime | None = None

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

	#: How the item this event is *about* is named to a reader — its ref and its title. The
	#: subject's when there is one, the entity's otherwise, so a comment reports the item
	#: somebody wrote on rather than the comment row, which has neither.
	#:
	#: **Both null where there is nothing to name**: a workspace, a link, an item outside the
	#: three that carry titles. `item_ref` is null for a project too, which addresses itself by
	#: key and never had one (§6.2).
	#:
	#: Here rather than left to each client because the alternative is every client resolving
	#: the same ids again — a CLI, an agent and a browser writing three answers to one
	#: question, which is the divergence this module sits outside `api/` to prevent.
	item_ref: int | None = None
	item_title: str | None = None

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
		"""Return this account as the cells of one compact line.

		**"instance admin", not "admin"** (`#204`). ``admin`` is also the key of a workspace
		role, and ``user list`` and ``user list --workspace acme`` print their answers in the
		same column position two commands apart — so the same person read as ``admin`` in one
		and ``owner`` in the other, where the first word named a role she does not hold and the
		second could legitimately have printed it. Being a superuser is an *instance* fact and
		saying so collides with nothing.
		"""

		return (
			self.username,
			"agent" if self.is_service_account else "person",
			"instance admin" if self.is_superuser else "",
			"" if self.is_active else "inactive",
			self.display_name or "",
		)


class Caller(pydantic.BaseModel):
	"""The account somebody is acting as, told to that somebody — item ``#336``.

	**Not :class:`User`, and it was written as a subclass of one first.** Decision ``#161``'s
	line is where the two part: an email is personal data that no caller needs a colleague's,
	and everybody is entitled to their own — so this carries one and a directory entry does
	not. What a directory carries and this does not is ``is_active`` and ``created_at``, facts
	about an account *as an administrator sees it* rather than about the authority being
	exercised.

	Inheriting looked tidier and was wrong for a reason worth keeping: it would have added two
	required fields to a response that has been shipping without them, so a client built from
	this tree could not read ``/v1/me`` from a server one release behind — and the failure it
	produced said the server was "not a Subroutine instance". Found by running the command
	against the live instance, which is a release behind, in the first minute of its existence.
	"""

	id: uuid.UUID
	username: str
	display_name: str | None

	#: Null when the account has none. Reported only about oneself.
	email: str | None

	#: Null means "not stated", so the workspace's zone and then the instance's show through
	#: (§12.3).
	timezone: str | None

	is_superuser: bool
	is_service_account: bool


class Credential(pydantic.BaseModel):
	"""The credential a caller presented, and how far it narrows their authority.

	**Not :class:`Token`, which answers a different question.** That one describes a credential
	in an inventory — whose it is, whether it still works, when it was revoked — and every one
	of those facts is settled here by the fact that this request was answered at all. What is
	left is the part a caller acts on: what it lets them do.

	Never the secret, and never anything from which one could be rebuilt: ``prefix`` is the
	public half a token is looked up by and is safe to quote in a log (§7.4).
	"""

	kind: str
	id: uuid.UUID
	title: str
	prefix: str

	#: Empty means **no narrowing**, not "no permissions" (SPEC.md §7.3).
	scopes: list[str]

	#: Null means every project, for the same reason.
	project_scope: list[str] | None

	#: The same restriction, with each id resolved to the key a person types — `#216`. Null
	#: alongside ``project_scope``, and never a *narrower* list than it: an id resolving to
	#: nothing visible is passed through as it was stored, because a credential's reported
	#: reach must not be smaller than its real one. Resolved by the instance rather than left
	#: to the caller, since a client would otherwise pay a second call to read back what it
	#: has just set (§13.1) — and one of the two transports has no endpoint to ask.
	#:
	#: **Defaulted, because this model reads a response from a server that may be older**
	#: (`#345`). Every instance sends it; the default is what lets a client one commit ahead
	#: still parse an instance one commit behind, which is the ordinary state of a fleet with
	#: more than one machine in it. Sending null and omitting it mean the same thing here, and
	#: a caller that wants certainty reads ``project_scope``, which has always been sent.
	project_scope_keys: list[str] | None = None

	#: Where it may *change* things, within that reach — item `#371`. Null means its whole
	#: reach, which for a restricted credential is **not** everywhere: a caller reading this
	#: as "unrestricted" would report a bounded agent as unbounded.
	#:
	#: **Defaulted**, so a client can read a response from an instance that predates the field
	#: (`#345`).
	project_write_scope: list[str] | None = None

	#: The same, with each id resolved to the key a person types. Null alongside
	#: ``project_write_scope``, and never a narrower list than it, for ``project_scope_keys``'
	#: reason.
	project_write_scope_keys: list[str] | None = None

	#: Set when the credential may only be used in one workspace.
	workspace_id: uuid.UUID | None

	#: Whether this credential restricts its owner at all. Spelled out so that reading
	#: ``scopes: []`` the wrong way round is not the only thing standing between an agent and a
	#: wrong conclusion.
	narrows: bool

	expires_at: datetime.datetime | None
	last_used_at: datetime.datetime | None


class WorkspaceAccess(WorkspaceRef):
	"""One workspace a caller can reach, and what they may actually do in it.

	:class:`WorkspaceRef` is how a client *addresses* a workspace; this adds what the caller
	may do once they are there. Extending it rather than repeating three fields keeps a slug
	meaning one thing in both.
	"""

	#: Null means "not stated", so the instance's own zone shows through (§12.3).
	timezone: str | None

	#: The role held here, before the credential narrowed anything.
	role: str | None

	#: What they may actually do, after every narrowing in §7.3 has been applied. **This is
	#: the field to act on**; the others explain how it came out this way.
	permissions: list[str]

	narrowed_by_credential: bool


class Me(pydantic.BaseModel):
	"""Who the caller is and exactly what they may do, in one round trip.

	The answer :func:`me` assembles, reported by ``GET /v1/me`` and by the local client alike.
	An agent should not have to discover its own authority by being refused things (§13.1),
	and — since ``#336`` — should not have to infer its own *identity* from a side effect
	either.
	"""

	api_version: str
	user: Caller

	#: What the installation that answered this call is *running* — item ``#381``. Not
	#: :attr:`api_version`, which is the wire contract and has read ``"1.0"`` since M1: this
	#: is the program, and it is the only thing that says whether a feature an agent has read
	#: about exists here yet.
	#:
	#: **Defaulted, like everything added to this model after it shipped** (`#345`). An
	#: instance one release behind sends no such key and must keep working.
	instance_version: str | None = None

	#: Which migration that installation's database is actually at — the same twelve-hex
	#: token ``subroutine db current`` prints and ``/readyz`` compares. Null only where the
	#: database carries no ``alembic_version`` at all, which no installation this program
	#: creates ever does: ``init`` migrates, and the suite *stamps* so that a test database
	#: describes itself the way a real one does.
	#:
	#: Here because the pair is what makes skew legible: a program newer than its schema is
	#: `#376`, and it looked from inside a session exactly like a feature that did not work.
	schema_revision: str | None = None

	#: Absent in local mode, where the CLI acts as a user with no credential at all.
	credential: Credential | None

	#: Permissions over the installation itself — creating workspaces and accounts. Held only
	#: by a superuser, and narrowed by the credential even then (SPEC.md §7.1).
	instance_permissions: list[str]

	workspaces: list[WorkspaceAccess]


class Token(pydantic.BaseModel):
	"""A credential as it can safely be described — item ``#208``.

	**Everything but the secret, and nothing from which it could be rebuilt.** Only a
	``sha256`` of the secret is stored (§7.4), so there is nothing here to leak; ``prefix`` is
	the public half a token is looked up by and is what revoking takes.

	``usable`` is stated rather than left to be worked out from two nullable columns, for the
	reason ``token list`` states it: a reader who has to compare ``expires_at`` against the
	clock is one who eventually reads a dead credential as live, on the day they are checking
	whether it is.
	"""

	id: uuid.UUID
	title: str
	prefix: str

	#: Whose authority this carries. A caller may be looking at credentials they issued *for*
	#: somebody else, so the name is here rather than only the id.
	user_id: uuid.UUID
	username: str

	#: Empty means **no narrowing**, not "no permissions" (SPEC.md §7.3).
	scopes: list[str]

	#: Null means every project, for the same reason.
	project_scope: list[str] | None

	#: The same restriction with each id resolved to its key, where the caller can see it —
	#: `#203`, `#348`. **Defaulted**, so a client can read a response from an instance that
	#: predates the field (`#345`), and never a *narrower* list than ``project_scope``: an id
	#: that resolves to nothing visible is passed through as it was stored, because a
	#: credential's reported reach must not be smaller than its real one.
	project_scope_keys: list[str] | None = None

	#: Where it may *change* things, within that reach — item `#371`. Null means its whole
	#: reach, which for a restricted credential is **not** everywhere: a caller reading this
	#: as "unrestricted" would report a bounded agent as unbounded.
	#:
	#: **Defaulted**, so a client can read a response from an instance that predates the field
	#: (`#345`).
	project_write_scope: list[str] | None = None

	#: The same, with each id resolved to the key a person types. Null alongside
	#: ``project_write_scope``, and never a narrower list than it, for ``project_scope_keys``'
	#: reason.
	project_write_scope_keys: list[str] | None = None

	#: Set when the credential may only be used in one workspace.
	workspace_id: uuid.UUID | None

	#: Whether this credential restricts its owner at all. Spelled out so that reading
	#: ``scopes: []`` the wrong way round is not the only thing between a caller and a wrong
	#: conclusion about what a leaked token could do.
	narrows: bool

	#: Whether it would be accepted right now. False for revoked and for expired alike: the
	#: question somebody is asking at this moment is whether it still works.
	usable: bool

	created_at: datetime.datetime
	expires_at: datetime.datetime | None
	last_used_at: datetime.datetime | None
	revoked_at: datetime.datetime | None

	def address (self) -> str:
		"""Return what a caller addresses this by — the prefix, as revoking takes it."""

		return self.prefix

	def columns (self) -> tuple[str, ...]:
		"""Return this credential as the cells of one compact line."""

		return (
			self.prefix,
			self.username,
			self.title,
			"" if self.usable else "not usable",
			"everything its owner can do" if not self.scopes else ", ".join(self.scopes),
		)


class IssuedToken(Token):
	"""A credential at the one moment its secret exists in readable form.

	Returned by ``POST /v1/tokens`` and by nothing else, ever. Nothing recovers the secret
	afterwards, including this program — which is why it is a separate type rather than an
	optional field on :class:`Token`: a field that is usually absent is one somebody eventually
	expects to find.
	"""

	#: Show it once and let it go.
	token: str

	#: Whether a machine identity had to be created to hold this — item `#348`. Reported
	#: because "created service account claude" is worth saying and cannot be worked out
	#: afterwards without a second call and a race with anybody else creating accounts.
	#:
	#: **Defaulted**, so a client one release ahead of its instance can still read this
	#: response (`#345`). False and absent mean the same thing: nothing was created.
	account_created: bool = False


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
		claimed_by_id=row.claimed_by_id,
		claimed_at=row.claimed_at,
		claim_expires_at=row.claim_expires_at,
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


def event (
	row: subroutine.db.models.activity.Event,
	described: dict[uuid.UUID, subroutine.domain.events.Described] | None = None,
) -> Event:
	"""Render one event.

	No vocabulary argument: an event's ``action`` is an open string rather than a seeded
	vocabulary row (§5.11), so there is nothing to batch-load and nothing to resolve.

	``described`` is the batch from :func:`subroutine.domain.events.descriptions`, keyed by the
	id each event is about. **Optional, and absent means null rather than a lookup** — a
	renderer that quietly queried per row would be `#39`'s N+1 reintroduced in the one place
	that pages fifty rows at a time.
	"""

	about = None

	if described is not None:
		about = described.get(row.subject_id or row.entity_id)

	return Event(
		seq=row.seq,
		id=row.id,
		item_ref=None if about is None else about.ref,
		item_title=None if about is None else about.title,
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
		entity_type=end.entity_type,
		id=end.id,
		ref=end.ref,
		title=end.title,
		is_complete=end.is_complete,
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
		other=_end(related.other),
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


def me (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
) -> Me:
	"""Assemble the answer to "who am I, and what may I do here?" — item ``#336``.

	**Here rather than in the router**, because both transports have to answer it identically
	and one of them has no router: ``GET /v1/me`` and ``Client.me()`` are two ways of asking
	the same question and the answer is built once (§13.7).

	It reports the *answer* rather than the inputs to it. Each workspace carries the
	permissions that actually apply there, already intersected with the role, the credential's
	scopes and its project scope, so nothing here asks the caller to reproduce §7.3's
	resolution for itself.
	"""

	return Me(
		api_version=subroutine.API_VERSION,
		# **Read off the connection this answer was assembled over, not off configuration**
		# (`#381`). The whole value of reporting it is that it comes from the database the
		# process is actually using, which on 2026-08-03 was not the one anybody expected.
		# Null-safe: a database with no `alembic_version` table makes alembic answer `None`
		# rather than raise, so this cannot be the call that breaks a diagnostic.
		instance_version=subroutine.installations.program(),
		schema_revision=subroutine.db.migrate.revision_on(session.connection()),
		user=caller(principal.user),
		credential=credential(session, principal),
		instance_permissions=sorted(
			subroutine.domain.authorization.instance_permissions(principal)
		),
		workspaces=[
			workspace_access(session, principal, workspace)
			for workspace in subroutine.domain.workspaces.readable(session, principal)
		],
	)


def caller (row: subroutine.db.models.identity.User) -> Caller:
	"""Render the account somebody is acting as, without anything that authenticates it."""

	return Caller(
		id=row.id,
		username=row.username,
		display_name=row.display_name,
		email=row.email,
		timezone=row.timezone,
		is_superuser=row.is_superuser,
		is_service_account=row.is_service_account,
	)


def credential (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
) -> Credential | None:
	"""Describe the credential presented, or ``None`` when there was none.

	``None`` is local mode (§12.1a), where the filesystem permission is the authentication
	and there is no credential to describe — not a caller whose credential failed, who never
	reaches this.
	"""

	row = principal.token

	if row is None:
		return None

	return Credential(
		kind="api_token",
		id=row.id,
		title=row.title,
		prefix=row.token_prefix,
		scopes=sorted(row.scopes),
		project_scope=None if row.project_scope is None else list(row.project_scope),
		project_scope_keys=(
			None
			if row.project_scope is None
			else subroutine.domain.projects.keys_for(session, principal, row.project_scope)
		),
		project_write_scope=(
			None if row.project_write_scope is None else list(row.project_write_scope)
		),
		project_write_scope_keys=(
			None
			if row.project_write_scope is None
			else subroutine.domain.projects.keys_for(
				session, principal, row.project_write_scope
			)
		),
		workspace_id=row.workspace_id,
		narrows=bool(row.scopes)
		or row.project_scope is not None
		or row.project_write_scope is not None
		or row.workspace_id is not None,
		expires_at=row.expires_at,
		last_used_at=row.last_used_at,
	)


def workspace_access (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	row: subroutine.db.models.identity.Workspace,
) -> WorkspaceAccess:
	"""Describe what one caller may do in one workspace."""

	grant = subroutine.domain.authorization.explain(session, principal, row.id)

	return WorkspaceAccess(
		id=row.id,
		slug=row.slug,
		title=row.title,
		timezone=row.timezone,
		role=grant.from_role,
		permissions=sorted(grant.permissions),
		narrowed_by_credential=grant.narrowed_by_token,
	)


def writable (credential: Credential | Token) -> list[str]:
	"""Name the projects a credential may *change* things in, as somebody would type them.

	The write half of :func:`reach`, and it follows the same rule for the same reason: keys
	where the instance could resolve them, ids where it could not, and never a shorter list
	than the credential really has.

	Empty means null — its whole reach — which is what every credential issued before ``#371``
	means and is a different statement from a list with nothing in it.
	"""

	if credential.project_write_scope is None:
		return []

	return list(credential.project_write_scope_keys or credential.project_write_scope)


def reach (credential: Credential | Token) -> list[str]:
	"""Name the projects a credential is restricted to, as somebody would type them.

	**Keys where the instance could resolve them, ids where it could not** — never a shorter
	list than the credential actually reaches (`#203`). An id that resolves to nothing visible
	is passed through as it was stored, because a credential's *reported* reach must never be
	smaller than its real one.

	Empty for a credential restricted to nothing at all, which is a different statement from
	``None`` and is the caller's to word.
	"""

	if credential.project_scope is None:
		return []

	return list(credential.project_scope_keys or credential.project_scope)


def narrowing (
	credential: Credential,
	workspaces: typing.Sequence[WorkspaceAccess] = (),
) -> str:
	"""Say what a credential has been limited to, in the words it was limited with.

	**One renderer, because there were three** (`#357`). The CLI's `whoami`, the MCP tool of
	the same name and `agent create`'s closing check each built this sentence themselves, from
	the same three clauses in the same order with the same comment above them — and they had
	already parted company: where a workspace pin names a workspace the credential cannot read,
	one printed the raw id and another printed "one workspace". Both are defensible and only
	one can be right, and nothing would ever have noticed they disagreed. That is this
	codebase's signature defect, arriving divergent rather than drifting into it.

	**The id wins**, which was the CLI's answer. A pin the caller cannot resolve is exactly
	when they need something to go and look up; "one workspace" tells them a fact they can
	already see in the word "pinned".

	``workspaces`` is what a slug is resolved through, and defaults to none — a caller that has
	not got the list gets the id rather than a second query.
	"""

	parts = []

	if credential.workspace_id is not None:
		named = [
			workspace.slug
			for workspace in workspaces
			if workspace.id == credential.workspace_id
		]

		parts.append(
			f"workspace {named[0]!r}" if named else f"workspace {credential.workspace_id}"
		)

	if credential.project_scope is not None:
		within = reach(credential)

		parts.append(f"projects {', '.join(within)}" if within else "no project at all")

	# **The clause `#371` shipped without, found by `#372` driving the command** (`#403`).
	# A credential that reads a tree and writes one project reported only the tree, so the
	# whole of what `#370` was decided for was invisible on the three surfaces that describe
	# a credential — and a credential narrowed *only* this way printed "Narrowed to ." with
	# nothing in it, which claims a boundary and refuses to name it.
	#
	# Said as "writing in …" rather than as a second list of projects, because the two lists
	# are answers to different questions and a reader seeing two comma-separated sets of keys
	# has to work out which is which.
	if credential.project_write_scope is not None:
		changing = writable(credential)

		parts.append(
			f"writing in {', '.join(changing)}" if changing else "writing nowhere at all"
		)

	if credential.scopes:
		parts.append(f"scopes {', '.join(credential.scopes)}")

	return "; ".join(parts)


def versions (me: Me, *, program: str, plugin: str | None = None) -> list[str]:
	"""Say which installations answered this call, and whether any of them disagree — ``#381``.

	**One renderer for the same reason :func:`narrowing` is one** (`#357`): the CLI's
	``whoami`` and the MCP tool of the same name both need it, and three copies of a sentence
	about versions would be the one place a version claim could itself go stale.

	Three things can be in play and each upgrades separately — the plugin the editor cached,
	the program on the machine, and the instance on the far end. ``plugin`` is ``None`` when
	no plugin started this process, which is every command line; ``program`` is required,
	because a caller that could not say what it is running has nothing to report.

	**Every installation is named, even when they all agree**, which is the one place this
	module departs from its own rule that a value repeated on every row says nothing. The
	line is asked for as a question — "what am I talking to?" — and an answer that dropped a
	number *because* it matched would make the reader reason about the omission. The *second*
	line is the exception-shaped half, and it appears only when there is something to act on.

	**It never says which is newer.** Ordering two version strings correctly needs
	``packaging``, which is not one of this project's declared dependencies, and a comparison
	that is right for ``0.2.1`` and wrong for ``0.2.1.dev51`` would be a diagnostic asserting
	a cause it has not established. Naming the disagreement is what the reader can act on.
	"""

	# **A null here is a fact, not a gap.** An instance that sends no version is one that
	# predates this field, which is itself the answer to "why does the feature I read about
	# not work" — so it is worded as a finding rather than left blank.
	instance = me.instance_version or "too old to say"

	seen = [] if plugin is None else [f"plugin {plugin}"]

	seen.extend([f"program {program}", f"instance {instance}"])

	where = ", ".join(seen)
	schema = "" if me.schema_revision is None else f", schema {me.schema_revision}"
	lines = [f"{where[0].upper()}{where[1:]}{schema}."]

	# Each disagreement names the failure it actually produced, on 2026-08-03, rather than
	# advising a refresh in general terms: `#345` was a field one side had and the other did
	# not, and `#379` was an argument a tool offered that its program had never heard of.
	if me.instance_version != program:
		lines.append(
			"The program and the instance disagree, so a call may be refused for a field "
			"one of them does not have."
		)

	if plugin is not None and plugin != program:
		lines.append(
			"The plugin and the program disagree, so a tool may offer an argument the "
			"program does not accept, or lack one it does."
		)

	return lines


def token (
	row: subroutine.db.models.identity.ApiToken,
	*,
	owner: subroutine.db.models.identity.User | None,
	now: datetime.datetime | None = None,
	secret: str | None = None,
	account_created: bool = False,
	session: sqlalchemy.orm.Session | None = None,
	principal: subroutine.domain.authentication.Principal | None = None,
) -> Token:
	"""Render one credential, with its secret only when it has just been minted.

	``owner`` may be ``None`` — a credential outlives the account it was issued for by exactly
	as long as it takes somebody to revoke it, and a listing that raised on one of those would
	hide the rest.
	"""

	moment = now or subroutine.db.types.utcnow()
	fields = {
		"id": row.id,
		"title": row.title,
		"prefix": row.token_prefix,
		"user_id": row.user_id,
		"username": "someone since deleted" if owner is None else owner.username,
		"scopes": list(row.scopes),
		"project_scope": None if row.project_scope is None else list(row.project_scope),
		# **Resolved here rather than by whoever prints it** (`#348`). `token list` did it at
		# print time through a session, which the HTTP client has not got — so a credential
		# read over a connection would have reported ids where the same command reported keys.
		"project_scope_keys": (
			None
			if row.project_scope is None or session is None or principal is None
			else subroutine.domain.projects.keys_for(session, principal, row.project_scope)
		),
		"project_write_scope": (
			None if row.project_write_scope is None else list(row.project_write_scope)
		),
		"project_write_scope_keys": (
			None
			if row.project_write_scope is None or session is None or principal is None
			else subroutine.domain.projects.keys_for(
				session, principal, row.project_write_scope
			)
		),
		"workspace_id": row.workspace_id,
		"narrows": bool(row.scopes)
		or row.project_scope is not None
		or row.project_write_scope is not None
		or row.workspace_id is not None,
		"usable": row.revoked_at is None
		and (row.expires_at is None or row.expires_at > moment),
		"created_at": row.created_at,
		"expires_at": row.expires_at,
		"last_used_at": row.last_used_at,
		"revoked_at": row.revoked_at,
	}

	if secret is None:
		return Token(**fields)

	return IssuedToken(**fields, token=secret, account_created=account_created)


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
