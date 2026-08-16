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

import collections.abc
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
import subroutine.domain.readiness
import subroutine.domain.refs
import subroutine.domain.tags
import subroutine.domain.text
import subroutine.domain.workspaces
import subroutine.errors
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


class Reading(pydantic.BaseModel):
	"""What a written repeat turned out to mean."""

	#: The stored form — RFC 5545, and what every calendar application reads.
	rule: str

	#: The same thing as a sentence, generated rather than echoed. This is the field that
	#: makes the endpoint a check rather than a mirror.
	description: str

	#: The words that were sent, when they were words. Null when a rule was sent directly,
	#: which is the honest answer: nobody wrote a sentence.
	text: str | None

	#: The next few occurrences, in UTC, computed where the caller is.
	occurrences: list[datetime.datetime]


class Occurrences(pydantic.BaseModel):
	"""When a repeating task comes round, over a stretch of time.

	§6.7 reserved this for a calendar, and decision `#915` is why it is computed rather than
	stored: **one occurrence is real and the rest are arithmetic**. A birthday is one row for
	ever rather than one row per year since 1974, and *show me every occurrence* turns out to
	be a question about a **view** rather than about the backlog.

	**Dates and nothing else.** An occurrence that has not happened has no status, no assignee
	and no comments — it is not a row and reporting it in a task's shape would invite a client
	to act on something that does not exist. What it does carry is the description, so a
	calendar can say what the rule *is* beside the dates it produced.
	"""

	#: The rule these came from, and the same rule as a sentence. Both, because a calendar
	#: drawing a month needs something to label the series with and `RRULE` is not it — while
	#: a client feeding another calendar needs exactly the `RRULE` and nothing else.
	rule: str
	description: str

	occurrences: list[datetime.datetime]

	#: Whether the answer stopped at `limit` rather than at `until`. A rule with no end runs
	#: for ever, so *there are no more* and *I stopped counting* are different facts and a
	#: caller drawing a month cannot tell them apart from the list alone.
	has_more: bool


class Task(pydantic.BaseModel):
	"""A task as the API reports it."""

	id: uuid.UUID
	ref: int
	title: str
	description: str | None

	#: **How much prose this item carries, in bytes** (`#595`). A row in a listing is the same
	#: shape whether the item is three words or 128,083 characters — which is what one document
	#: on this instance measured, about 32,000 tokens, read into an agent's context with nothing
	#: anywhere to warn it. Context economy is a first-order cost here (§13), the tool surface
	#: is budgeted to a few thousand bytes and held by a test, and one unannounced read spends
	#: ten times that.
	#:
	#: **A number rather than a flag**, because the threshold is the caller's: a session with
	#: room to spare and one nearly full need different answers to "is this too big", and a
	#: boolean decides for both of them.
	#:
	#: The prose only — not the whole response, which also carries links, comments and possibly
	#: a history. Those are bounded by what somebody typed; this is the part that is not, and
	#: the part that dominates when it is large.
	#: **Optional, and null is not zero** (`#482`). A field added since the last release must
	#: carry a default or a client one commit ahead refuses the whole response — `#345`, twice
	#: in one day. Null says *this instance did not tell you*; zero says *there is no prose*,
	#: and a reader deciding whether to spend a context window needs those apart.
	size_bytes: int | None = None

	workspace_id: uuid.UUID
	project_id: uuid.UUID
	project_key: str
	parent_task_id: uuid.UUID | None

	#: Who holds a lease on this, and until when (§14.11, `#350`).
	#:
	#: **An expired lease is still reported.** Who was working on this is worth knowing even
	#: once the lease has run out, and ``claim_expires_at`` against the clock is what says
	#: whether it still counts — the same reading ``domain.claims.held_by`` applies. Defaulted,
	#: so a client can read a response from an instance that predates them (`#345`).
	claimed_by_id: uuid.UUID | None = None
	claimed_at: datetime.datetime | None = None
	claim_expires_at: datetime.datetime | None = None

	#: The holder's username, batch-loaded beside the assignee's (`#726`).
	#:
	#: **This said "ids, not names" and gave `assignee_id` as its precedent** — and `#511` then
	#: gave the assignee a name, so the sentence went on citing as support the one thing that
	#: contradicted it. The reasoning it rested on is the reasoning against it: a username is how
	#: a person is addressed, so reporting only an id makes every surface resolve a UUID before
	#: it can print anything, which is review dimension 4's second call multiplied by the page.
	#:
	#: Loaded in the same query as the assignee, so a page of rows costs no request it did not
	#: already make. Defaulted for `#345`'s reason, like the three above it.
	claimed_by: str | None = None

	#: Whether something unfinished is blocking this — item `#425`, and **one query for the
	#: page** rather than one per row (`#39`'s N+1, which is what kept it unreported).
	#:
	#: **Only the `blocks` half of readiness, deliberately.** ``?ready=true`` also excludes work
	#: that is deferred or claimed by somebody else, and those two are already visible: a defer
	#: renders as its date and a claim as its holder. A blocker was the one that could not be
	#: seen at all, so a listing put a blocked item above the thing blocking it with nothing to
	#: say so — reported by an agent reading a default listing as "start with #2".
	#:
	#: A fact about the *work*, not about the viewer, so unlike a claim it reads the same for
	#: everybody. Defaulted, so a client can read an instance that predates it (`#345`) — and
	#: ``False`` there means "nothing says so", which is the honest reading of silence.
	blocked: bool = False

	#: Whether this is holding something unfinished up — item `#569`, and **the mirror of
	#: `blocked` above**. `#425` made work that cannot be started visible and nothing made the
	#: work *doing the blocking* visible, so a board showed the urgent item marked `blocked` and
	#: said nothing about the five-minute errand holding it up — which was the only thing on the
	#: board worth doing, and an agent reading that board said so.
	#:
	#: **A boolean and not the far end's ref, which is a rule rather than an omission.** This
	#: says *that* something is held up; naming *what* would report the existence and standing
	#: of an item the reader may not be allowed to see. `subroutine show` names it, through
	#: `domain.links.edges`, which drops an end the caller cannot see — so the listing says
	#: *that* and the detail view says *what*. `#856` is what happens when that line is crossed.
	#:
	#: Same query shape as `blocked`: one `EXISTS` scan for the page, never one per row.
	#: Defaulted for `#345`'s reason, and `False` honestly means "nothing says so".
	blocking: bool = False

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

	#: The assignee's username, batch-loaded beside the statuses and types (`#511`). §8.5 says
	#: an unrequested relation appears as an id, and an id is what every surface then had to
	#: print — so work could be handed over on all three surfaces and no surface said to whom.
	#: A username is what somebody types into `--assignee` and what §14.10's `@assignee` means,
	#: which makes this enrichment rather than a second representation of the same field.
	#:
	#: **Defaulted because it was added after this model shipped** (`#345`, guarded by `#482`).
	assignee: str | None = None

	#: Who put it in that person's queue (`#477`). Derived from whoever made the change, so it
	#: is reported and never accepted — an assigner a caller could type would be a claim rather
	#: than a record. Null with the assignee, and null for a change nobody was acting for.
	#:
	#: **Defaulted because it was added after this model shipped** (`#345`): an instance one
	#: release behind sends a body without it, and a required field here makes this client
	#: refuse that instance outright rather than read the rest of what it said.
	assigned_by_id: uuid.UUID | None = None

	#: §6.3's two independent axes, 1-5 where 5 is highest, and the product of them.
	#: Null means *not assessed* and is distinct from 1. ``priority_score`` is derived and
	#: read-only — null unless both axes are set — and exists so that an agent sorting by
	#: "most important" has one key rather than a convention it invented.
	importance: int | None
	urgency: int | None
	priority_score: int | None

	#: **Where this row sat in the ordering the listing was asked for, and nothing else**
	#: (`#569`). Compare it; do not read meaning into the number. It is not a score a person
	#: assessed — ``priority_score`` above is that — and it is deliberately opaque, because
	#: what goes into it is an ordering decision that may change without the two axes changing.
	#:
	#: **Null unless the listing was sorted by it.** Its only job is to let a client merging
	#: pages from several places reproduce the order it asked each of them for, which is what
	#: ``subroutine list`` does across connections; computing it for callers who did not ask
	#: would spend a correlated subquery per row on a number nobody reads.
	#:
	#: **Defaulted because it was added after this model shipped** (`#345`, `#482`): an
	#: instance one release behind sends a body without it, and a required field here makes a
	#: newer client refuse that instance outright rather than read the rest of what it said.
	rank: int | None = None

	#: How well this row answered the search that selected it (`#823`), and **published for
	#: exactly ``rank``'s reason**: a client merging pages from several places has to be able
	#: to reproduce the order it asked each of them for. The browser holds tasks and documents
	#: as two collections and re-sorts them into one list, so without this it could only merge
	#: on a date — which is `#875`, where the server ranked a search and the client threw the
	#: ranking away.
	#:
	#: **Null unless the listing was ranked**, which needs both a search and a backend that can
	#: score one. Compare it; do not read meaning into the number. It is not comparable between
	#: two different searches, and it is not a property of the item — it is the score of one
	#: query, which is why it took a decision to publish rather than being obvious.
	#:
	#: **Defaulted because it was added after this model shipped** (`#345`, `#482`).
	relevance: float | None = None

	due_at: datetime.datetime | None
	due_is_all_day: bool

	#: **An instant with a flag since `#854`**, where it was a bare date called
	#: ``planned_for``. Both halves are defaulted because they are new to a model that has
	#: already shipped, which `#345` and `#482` made a rule rather than a courtesy: a client
	#: one release behind must not be refused outright for a field it has never heard of.
	starts_at: datetime.datetime | None = None
	starts_is_all_day: bool = False

	snoozed_until: datetime.datetime | None = None
	snoozed_is_all_day: bool = False
	timezone: str | None

	#: How this repeats, and what that means (§6.7, decision `#915`). **All defaulted**, per
	#: `#345` and `#482`: a client one release behind must not be refused outright for fields
	#: it has never heard of.
	#:
	#: The rule and its qualifiers are *stored* on the **template** and **reported on both**,
	#: so an occurrence can say how it repeats without a second call; ``occurrence_at`` and
	#: ``recurrence_template_ref`` belong to an **instance** alone. Reading which is which is
	#: what ``is_template`` answers, and it is the only thing that explains why a row with a
	#: ref appears in no listing.
	#:
	#: **Write them on whichever end is in hand.** A repeat addressed to an occurrence is
	#: addressed to its series (§6.7), because the template is in no listing and nobody
	#: navigates to one — so the field a client reads here is the field it sends back.
	recurrence_rule: str | None = None
	recurrence_text: str | None = None
	recurrence_anchor: str | None = None
	recurrence_trigger: str | None = None
	occurrence_at: datetime.datetime | None = None
	recurrence_template_ref: int | None = None
	is_template: bool = False

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

		``@assignee`` arrived in `#511`, batch-loaded onto the view the way ``#tags`` already
		was — the lookup this docstring used to say the view did not carry. It sits *after*
		the title deliberately: like the plan and the tags it marks itself with a sigil, so
		its position is not what identifies it, and putting it there means no column that
		existed before this moves on any page.
		"""

		return (
			subroutine.domain.refs.format_ref(self.ref),
			f"[{self.status}]",
			_priority_cell(self.importance, self.urgency),
			"—" if self.due_at is None else self.due_at.date().isoformat(),
			"" if self.starts_at is None else f"→{self.starts_at.date().isoformat()}",
			subroutine.domain.text.truncated(self.title),
			"" if self.assignee is None else f"@{self.assignee}",
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
	#: **Both null where there is nothing to name**: a workspace, an item outside the three that
	#: carry titles. `item_ref` is null for a project too, which addresses itself by key and
	#: never had one (§6.2).
	#:
	#: **A link event names its *source*, and this used to say it named nothing** (`#783`). That
	#: was true when written and stopped being true with `#252`, which gave link events a
	#: `subject_id` so the change feed could scope them — so a link reports the item it hangs
	#: off, exactly as a comment reports the item it was written on. A client watching one item
	#: therefore sees a link created **from** it and not one created **to** it, although the
	#: backlink appears on both ends; a client that needs both must re-read on any link event.
	#: Published as this model's OpenAPI description, so it is a statement to every caller.
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

	#: Who answers for this agent (decision `#473`). Null on a person, who answers for
	#: themselves. **Null on a service account means nobody does**, which is a state the
	#: instance refuses to act on rather than a value still to be filled in.
	#:
	#: Defaulted for `#345`'s reason: added after this model shipped, so an older instance's
	#: body must still parse.
	responsible_user_id: uuid.UUID | None = None

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


class SignInLink(pydantic.BaseModel):
	"""A sign-in link, at the one moment it can be read — item `#248`.

	**The secret is in the URL and nowhere else in this object.** Two fields carrying one
	credential would be two places for it to be logged, and a caller that wants the parts has
	the URL to take them from.

	Unlike :class:`Token`, there is no ``prefix`` here. Nothing revokes an individual link —
	it is spent by being used and gone within the half hour either way — so the public half
	would be a field with no question to answer.
	"""

	url: str

	#: Whose sign-in this is. Said back because a link is usually minted *for* somebody, and
	#: an administrator handing out four of them needs to know which is which.
	username: str
	expires_at: datetime.datetime


class SignedOut(pydantic.BaseModel):
	"""What signing somebody out of everything actually did — item `#248`.

	The count is here because the alternative is a 204 that looks identical whether it ended
	four sessions or none, and "none" is the answer somebody needs to see when they have
	revoked the wrong account.
	"""

	username: str
	sessions_ended: int


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

	No ``due_at``, ``starts_at``, ``estimate_minutes`` or ``assignee_id``, and their
	absence is the point (SPEC.md §6.14): a specification is never "done" and nobody is
	working on it. A deadline about a document belongs on a task that ``documents`` it.
	"""

	id: uuid.UUID
	ref: int
	title: str
	body: str | None

	#: **How much prose this item carries, in bytes** (`#595`). A row in a listing is the same
	#: shape whether the item is three words or 128,083 characters — which is what one document
	#: on this instance measured, about 32,000 tokens, read into an agent's context with nothing
	#: anywhere to warn it. Context economy is a first-order cost here (§13), the tool surface
	#: is budgeted to a few thousand bytes and held by a test, and one unannounced read spends
	#: ten times that.
	#:
	#: **A number rather than a flag**, because the threshold is the caller's: a session with
	#: room to spare and one nearly full need different answers to "is this too big", and a
	#: boolean decides for both of them.
	#:
	#: The prose only — not the whole response, which also carries links, comments and possibly
	#: a history. Those are bounded by what somebody typed; this is the part that is not, and
	#: the part that dominates when it is large.
	#: **Optional, and null is not zero** (`#482`). A field added since the last release must
	#: carry a default or a client one commit ahead refuses the whole response — `#345`, twice
	#: in one day. Null says *this instance did not tell you*; zero says *there is no prose*,
	#: and a reader deciding whether to spend a context window needs those apart.
	size_bytes: int | None = None

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

	#: The tag names on this document, alphabetical — `#819`. **The same vocabulary a task's
	#: tags come from**, decided with Simon: a tag is scoped to a workspace rather than to a
	#: kind, so `#health` here and `#health` on a task are one tag. Unlike a status or an item
	#: type, which §5.5 keeps per kind because *done* means nothing about a specification.
	#:
	#: `document_tag` has existed since the initial migration and nothing wrote to it until now,
	#: which is why this field is new on a table that is not.
	tags: list[str] = pydantic.Field(default_factory=list)

	supersedes_id: uuid.UUID | None

	#: How well this row answered the search that selected it — the same field :class:`Task`
	#: carries, for the same reason, and it has to be on **both** or a merged list is back to
	#: having no shared key (`#875`). §6.2 gives the two kinds one ref counter, so a search
	#: spans them and the client holds two collections it must interleave.
	#:
	#: Null unless the listing was ranked. **Defaulted** (`#345`, `#482`).
	relevance: float | None = None

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

	#: What is already started — status category ``in_progress`` (`#853`). An agent could not
	#: see its own half-finished work from a listing at all (`#841`), and a person reading an
	#: agenda could not tell what they had picked up from what they had not.
	#:
	#: **Defaulted, so a client can read an instance that predates it** (`#345`, `#482`): a
	#: required field here makes a newer client refuse an older instance outright rather than
	#: read the rest of what it said.
	in_progress: list[Task] = pydantic.Field(default_factory=list)

	#: How many unscheduled tasks there are in total, which is usually more than are listed:
	#: an agenda that dumped a 400-item backlog would not be an agenda.
	unscheduled_total: int


#: What a listing calls work that cannot start yet, and work that is holding others up.
#:
#: **Here rather than once per surface** (`#913`). The terminal named them and the agent's
#: listing wrote the same two words out again, so there were two copies with nothing comparing
#: them — this codebase's signature defect, in the module both of those surfaces already import
#: precisely so they cannot disagree.
#:
#: **``blocker`` rather than ``blocks``, because a mark has no object.** *Blocks what?* is the
#: question `#569` settled a listing does not answer — it says *that* an item blocks something
#: and ``show`` says *what* — so a verb here asks something the row is built not to say. A noun
#: naming the item's role is complete standing alone.
#:
#: **It was ``holds up``, and `#569`'s argument for two words still stands**: ``blocker``
#: differs from ``blocked`` by two letters and means the opposite, and a column a reader has to
#: look at twice misinforms at a glance. Simon was shown that and overruled it — one
#: relationship called two things across two surfaces is paid on every reading, where the
#: similarity is paid while scanning. `#764` adds a glyph, and `#102` binds it: neither word may
#: be replaced by one, only joined.
#:
#: The browser capitalises its marks and cannot import this, so it carries the only other copy
#: and ``tests/test_web.py`` compares the two.
BLOCKED_MARK = "blocked"
BLOCKING_MARK = "blocker"


def status_is_news (item: "Task | Project | Document") -> bool:
	"""Report whether an item's status is worth telling a reader about.

	**Silence about the status everything starts in** (`#168`, Simon 2026-08-01). Printing
	``open`` on every row of a shopping list is what §1.4 would not survive; printing
	``blocked`` is most of why the field exists, and before this rule no surface said the word
	at all — ``update 5 --status blocked`` answered *Changed* and a clean-room tester concluded
	it had not saved.

	**And not when it is finished**, because a completion has a better rendering wherever this
	is asked — ``show`` on both surfaces prints the date. A document has no ``completed_at`` to
	ask about, so the category is the question both kinds can answer.

	**That used to say "on every surface" and it was not true** (`#874`). The terminal's
	*listing* row had no completion rendering at all — its marks were ``doing``, ``blocked`` and
	``holds up``, and there was no fourth — so a finished task in a listing said nothing about
	being finished. It went unnoticed while such a row was almost unreachable; `#873` made a
	bare ``search <ref>`` surface finished work by design and the sentence became load-bearing
	and wrong on the same afternoon. ``cli.personal.FINISHED_MARK`` is the rendering now, and
	this claim is narrowed to the callers it is true of.

	Here rather than in each renderer because there are three of them: the terminal's ``show``,
	the agent's ``show``, and the agent's listing row (`#841`). It was written out twice,
	identically, and the third asking for it is one copy short of this codebase's signature
	defect.

	**A listing is a different question from a fact sheet, and only for documents.** Measured
	on this instance before `#841` used it: 2 of 172 open tasks have a status worth saying, and
	**111 of 122 documents do**, because ``draft`` is a document's default and ``active`` is
	not. So the same rule that keeps ``show`` quiet puts a cell on nine documents in ten — see
	``mcp.tools._line``, which asks this of tasks only for that reason.
	"""

	return not item.status_is_default and item.status_category != "done"


def state_is_news_in_a_listing (item: "Task | Project | Document") -> bool:
	"""Report whether a listing row should say what state this item is in — `#874`.

	**A listing asks a wider question than a fact sheet**, and that is the whole difference
	between this and :func:`status_is_news`. ``show`` prints a completion date, so naming the
	status beside it says the same thing twice; a listing row has no date and no room for one,
	so a finished item there is indistinguishable from an open one unless the row says so.

	That mattered little while a finished row was almost unreachable. `#873` made a bare
	``search <ref>`` surface finished work by design — 548 of this instance's 721 tasks — and
	the two surfaces that render listings both went quiet about it on the same afternoon.

	**Both listings ask this; each answers in its own vocabulary**, which is why this returns a
	boolean rather than a word. The agent's row prints the status *key*, because an agent reads
	keys and sends them back. The terminal prints ``doing`` or ``done`` from
	``cli.personal._state_cell``, because it has separate columns for ``blocked`` and
	``holds up`` and a reader meeting a raw key would be meeting the vocabulary §13.5b keeps off
	that path. Sharing the *question* is what stops them drifting; sharing the rendering would
	be wrong.
	"""

	return status_is_news(item) or item.status_category == "done"


def holder (item: "Task", *, now: datetime.datetime) -> str | None:
	"""Return who holds a live lease on this item, or ``None`` if nobody effectively does.

	**The view reports an expired lease deliberately** — who was working on something is worth
	keeping after the lease runs out — so every renderer has to apply the clock itself, and
	this is where that is written down for a *view*.
	:func:`subroutine.domain.claims.held_by` answers it for a database row and
	:func:`subroutine.domain.readiness.held` answers it in SQL. Three readings of one rule, for
	three kinds of object, and they must agree: a disagreement here is two workers on one task.

	**Both columns are tested, which is `#362`.** ``NOT (a AND b)`` is *null* rather than true
	when ``b`` is null, so a row carrying a holder and no expiry once vanished from every
	listing while ``held_by`` said nobody held it. The three readings have to agree about a
	state none of them can produce, because whatever produces it will be something nobody was
	thinking about at the time.

	It earns itself immediately: of the three claim records on this instance when `#841` was
	built, **two had expired**, so a renderer reading ``claimed_by`` without the clock would
	have been wrong about two rows in three.
	"""

	if item.claimed_by is None or item.claim_expires_at is None:
		return None

	return None if item.claim_expires_at <= now else item.claimed_by


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
		document_ids: typing.Iterable[uuid.UUID] = (),
		parent_ids: typing.Iterable[uuid.UUID] = (),
		user_ids: typing.Iterable[uuid.UUID] = (),
	) -> None:
		"""Load the vocabulary rows these ids refer to."""

		# Materialised because two loads read it, and a caller passing a generator would give
		# the second one nothing — silently, and only for the field added later.
		wanted = set(task_ids)

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
		# **Both kinds into one map, because one tag vocabulary serves both** (`#819`). An id is
		# a UUID, so a task's and a document's cannot collide and a renderer asks the same
		# question of either. Two queries rather than one because the join tables are two —
		# and `names_for` returns immediately on an empty set, so a page of one kind pays for
		# one.
		self.tags = {
			**subroutine.domain.tags.names_for(
				session, subroutine.db.models.work.Task, wanted
			),
			**subroutine.domain.tags.names_for(
				session, subroutine.db.models.work.Document, set(document_ids)
			),
		}

		# **One query for the whole page, which is the only reason this is affordable**
		# (`#425`). Readiness is a filter by design (`#69`), so asking it per row is `#39`'s
		# N+1 — and that was the recorded obstacle to marking blocked work for as long as
		# anybody wanted it marked. Loaded here because this class is already the answer to
		# "what does a page of rows need that a row cannot know on its own".
		self.blocked = subroutine.domain.readiness.blocked_among(session, wanted)
		# The mirror, one `EXISTS` scan the same way (`#569`). Two queries rather than one
		# because they are opposite directions over the same edges, and both return
		# immediately on an empty page.
		self.blocking = subroutine.domain.readiness.blocking_among(session, wanted)

		# **One query for every parent on the page, not one per row.** A ref is how an item
		# is addressed (§6.2), so a view reporting only `parent_task_id` forces every client
		# to resolve a UUID before it can print anything — which is the second call review
		# dimension 4 exists to prevent, multiplied by the page.
		self.parents = _by_id(
			session,
			subroutine.db.models.work.Task,
			parent_ids,
			# **The whole repeat rides along** (`#94`, `#918`), because an instance is the row
			# a person sees and it carries none of this — so without it "Water the plants"
			# reads as an ordinary task and nothing on any surface says it comes back. The
			# rows are already being fetched; this asks them for more columns.
			#
			# **All four, not the rule alone**, which is `#918`'s read half: the rule fell
			# back and its two qualifiers did not, so an occurrence reported *every three
			# days* and said nothing about whether that counts from the schedule or from the
			# last time somebody finished — half a fact, and the half that decides what the
			# next date will be.
			#
			# **And whether the series is still running** (`#920`), because a stopped one is
			# a finished template rather than a cleared column — so without this the last
			# occurrence of a stopped series goes on advertising a rule that will never fire
			# again, on the one surface somebody would check to see that their *stop* worked.
			(
				"ref",
				"title",
				"completed_at",
				"recurrence_rule",
				"recurrence_text",
				"recurrence_anchor",
				"recurrence_trigger",
			),
		)

		# **One query for every assignee on the page** (`#511`), for the reason the parents load
		# above gives: a username is how a person is addressed, so a view reporting only
		# `assignee_id` makes every surface resolve a UUID before it can print anything.
		self.users = _by_id(
			session, subroutine.db.models.identity.User, user_ids, ("username",)
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
			# **Templates ride with the parents**, because they are the same question — a task
			# on this page naming another task by id, needing a ref before a client can print
			# anything. A second batch load would be a second query for one extra column.
			parent_ids={task.parent_task_id for task in tasks if task.parent_task_id}
			| {task.recurrence_template_id for task in tasks if task.recurrence_template_id},
			# **Both the assignee and the lease holder, in one query** (`#726`). They are
			# usually the same account or absent, so the set is nearly always the size it was.
			user_ids=(
				{task.assignee_id for task in tasks if task.assignee_id}
				| {task.claimed_by_id for task in tasks if task.claimed_by_id}
			),
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
			document_ids={document.id for document in documents},
		)

	@classmethod
	def for_projects (
		cls,
		session: sqlalchemy.orm.Session,
		projects: typing.Sequence[subroutine.db.models.project.Project],
	) -> "Vocabulary":
		"""Load everything a page of projects needs to be rendered."""

		return cls(session, status_ids={project.status_id for project in projects})



def _prose_bytes (text: str | None) -> int:
	"""Return how many bytes this item's prose takes, as a caller will receive it — `#595`.

	UTF-8 rather than characters, because that is what crosses the wire and what a token budget
	is spent on. The two disagree on the punctuation this project writes in — an em dash is
	three bytes and one character — so a character count would understate exactly the documents
	worth warning about.
	"""

	return 0 if text is None else len(text.encode("utf-8"))


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
		size_bytes=_prose_bytes(row.description),
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
		assignee=_username(vocabulary, row.assignee_id),
		assigned_by_id=row.assigned_by_id,
		claimed_by_id=row.claimed_by_id,
		claimed_by=_username(vocabulary, row.claimed_by_id),
		claimed_at=row.claimed_at,
		claim_expires_at=row.claim_expires_at,
		blocked=row.id in vocabulary.blocked,
		blocking=row.id in vocabulary.blocking,
		importance=row.importance,
		urgency=row.urgency,
		# Computed here rather than read from the database: §6.3 calls it derived, and a
		# stored copy would be a second place for the two axes to disagree.
		priority_score=(
			None
			if row.importance is None or row.urgency is None
			else row.importance * row.urgency
		),
		# Whatever the query computed, or `None` where it was not asked to. Read rather than
		# worked out: since `#569` an ordering may consult rows other than this one, so there
		# is nothing here to work it out from.
		rank=row.rank,
		relevance=row.relevance,
		due_at=row.due_at,
		due_is_all_day=row.due_is_all_day,
		starts_at=row.starts_at,
		starts_is_all_day=row.starts_is_all_day,
		snoozed_until=row.snoozed_until,
		snoozed_is_all_day=row.snoozed_is_all_day,
		# **All four fall back to the template's**, so an occurrence can say how it repeats.
		# They live on the template and the instance is what anybody looks at; a view
		# reporting only what the row itself holds would make every occurrence read as a
		# one-off.
		#
		# **The rule alone fell back until `#918`**, which is that item's read half: an
		# occurrence answered *every three days* and left `recurrence_anchor` null, so
		# nothing said whether the next one is measured from the schedule or from the last
		# completion — and a caller who had just changed it could not read back what they
		# set. One of three qualifying fields resolving is worse than none, because the two
		# that stay null read as *not set* rather than as *not carried here*.
		#
		# **And only while the series is still running** (`#920`). Stopping a repeat completes
		# the template rather than clearing a column, so a fallback that ignored that left the
		# last occurrence promising *every month, on the 30th* about a series that would never
		# fire again — a claim about the future already known to be false, on the surface
		# somebody checks to see their *stop* worked. An exhausted `COUNT` closes the template
		# by the same path, so one condition covers both routes to *nothing follows this*.
		recurrence_rule=row.recurrence_rule
		or _from_a_live_series(vocabulary, row, "recurrence_rule"),
		recurrence_text=row.recurrence_text
		or _from_a_live_series(vocabulary, row, "recurrence_text"),
		recurrence_anchor=row.recurrence_anchor
		or _from_a_live_series(vocabulary, row, "recurrence_anchor"),
		recurrence_trigger=row.recurrence_trigger
		or _from_a_live_series(vocabulary, row, "recurrence_trigger"),
		occurrence_at=row.occurrence_at,
		recurrence_template_ref=_parent_field(
			vocabulary, row.recurrence_template_id, "ref"
		),
		is_template=row.is_template,
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
		size_bytes=_prose_bytes(row.body),
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
		tags=vocabulary.tags.get(row.id, []),
		supersedes_id=row.supersedes_id,
		relevance=row.relevance,
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

	everything = [
		*built.overdue,
		*built.today,
		*built.in_progress,
		*built.upcoming,
		*built.unscheduled,
	]
	vocabulary = Vocabulary.for_tasks(session, everything)

	return Agenda(
		date=built.date,
		timezone=built.timezone,
		overdue=[task(row, vocabulary) for row in built.overdue],
		today=[task(row, vocabulary) for row in built.today],
		upcoming=[task(row, vocabulary) for row in built.upcoming],
		in_progress=[task(row, vocabulary) for row in built.in_progress],
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
		responsible_user_id=row.responsible_user_id,
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

	**A browser session is described too, and answering ``None`` for one would have been a
	false statement rather than a missing feature** (`#248`): every caller of this reads a
	null as "no credential was presented", which for a signed-in browser is exactly wrong.
	"""

	if principal.is_local:
		return None

	if principal.session is not None:
		held = principal.session

		return Credential(
			kind="web_session",
			id=held.id,
			# A session has no title because nobody names one — it is minted by signing in
			# rather than issued for a purpose. This says what it is instead of inventing a
			# field that would only ever hold one value.
			title="Browser session",
			prefix=held.token_prefix,
			# A session narrows nothing: it is its owner acting as themselves (`#364`). Said
			# here as literal empty-and-null rather than reached by omission, because those
			# are the two values that mean "no narrowing" in this model.
			scopes=[],
			project_scope=None,
			project_scope_keys=None,
			project_write_scope=None,
			project_write_scope_keys=None,
			workspace_id=None,
			narrows=False,
			expires_at=held.expires_at,
			last_used_at=held.last_used_at,
		)

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
		narrows=subroutine.domain.authentication.narrowing(
			scopes=row.scopes,
			project_scope=row.project_scope,
			project_write_scope=row.project_write_scope,
			workspace_id=row.workspace_id,
		),
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


def comments_saying (recorded: typing.Sequence[Comment], words: str) -> list[Comment]:
	"""Return the comments whose text contains these words — item ``#415``.

	**A comment is named by what it says, because it has no number of its own** (`#400`), and
	that makes the words the whole of the addressing. So the one thing they may not be is
	*nothing*: ``"" in anything`` is true, so an empty search names every comment on the item —
	and the refusal beside this ("more than one says that") is then the only thing standing
	between a caller and a deletion nobody described. On an item with exactly one comment there
	is nothing standing there at all.

	**Measured over the real tool surface** before it was closed:
	``subroutine_comment(ref=1, remove=true)``, with no ``body`` at all, answered
	*"Taken out of #1."* The schema marks ``body`` required and the server does not enforce
	that, so a required field is whatever the client chooses to honour.

	Here rather than in each caller because both surfaces do this filtering themselves, and the
	one that had a person in front of it — the CLI, which prompts for the words — was the one
	that happened to be safe. A rule that holds on one of two surfaces is the shape this
	codebase finds most often.

	Withdrawal is soft but has no restore on any surface, unlike a task, a project or a
	document, so this is as close to a last check as a comment gets.
	"""

	wanted = words.strip()

	if not wanted:
		raise subroutine.errors.ValidationError(
			"Some of the comment's own words are needed to say which one you mean.",
			errors=[
				subroutine.errors.FieldError(
					field="body",
					code="missing_field",
					message="An empty search matches every comment on the item, which names "
					"none of them.",
					hint="Quote a few words from the comment you want taken out.",
				)
			],
		)

	folded = wanted.casefold()

	return [one for one in recorded if folded in one.body.casefold()]


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


def answering_to (
	rows: collections.abc.Sequence[User], username: str
) -> list[str]:
	"""Return the agents that answer to ``username``, directly or through another — `#475`.

	**Here rather than in the domain, and both exist on purpose.**
	``domain.accountability.agents_answering_to`` is a SQL query for the server, which has a
	session; this walks rows a *client* already holds, because a CLI talking to a remote
	instance has no session and must not gain one. Same reason ``views.py`` sits outside
	``api/`` at all — the alternative is the CLI asking a question the HTTP client cannot.

	Level by level rather than recursively, and bounded: a cycle is refused on the way in, so
	meeting one here means a database somebody edited, and looping for ever while somebody waits
	at a prompt is the worse failure.
	"""

	by_name = {row.username: row for row in rows}
	start = by_name.get(username)

	if start is None:
		return []

	found: dict[uuid.UUID, str] = {}
	frontier = {start.id}

	for _step in range(16):
		if not frontier:
			break

		fresh = {
			row.id: row.username
			for row in rows
			if row.is_service_account
			and row.responsible_user_id in frontier
			and row.id not in found
		}

		found.update(fresh)
		frontier = set(fresh)

	return sorted(found.values())


def versions (me: Me, *, program: str | None, plugin: str | None = None) -> list[str]:
	"""Say which installations answered this call, and whether any of them disagree — ``#381``.

	**One renderer for the same reason :func:`narrowing` is one** (`#357`): the CLI's
	``whoami`` and the MCP tool of the same name both need it, and three copies of a sentence
	about versions would be the one place a version claim could itself go stale.

	Three things can be in play and each upgrades separately — the plugin the editor cached,
	the program on the machine, and the instance on the far end. ``plugin`` is ``None`` when
	no plugin started this process, which is every command line.

	**``program`` is ``None`` when the caller's own installation is not visible from where this
	is rendered, and that is a different answer from every other** (`#564`). Since `#539` the
	MCP tools run *server-side*, so on a served instance ``installations.program()`` is the
	instance's own version and ``installations.plugin()`` is null — the caller's environment is
	on another machine. Passing those through produced *"Program X, instance X"* with X the same
	value twice, one of them labelled as the caller's, and no plugin line at all. An agent read
	that and concluded there was no version problem, which is the worst thing a check can do:
	`#381`'s three-way comparison was **inert in the direction that reassures**.

	So this says what it does not know rather than reporting a comparison it did not make.

	**Every installation is named, even when they all agree**, which is the one place this
	module departs from its own rule that a value repeated on every row says nothing. The
	line is asked for as a question — "what am I talking to?" — and an answer that dropped a
	number *because* it matched would make the reader reason about the omission. The *second*
	line is the exception-shaped half, and it appears only when there is something to act on.

	**The plugin and the program are meant to differ, and only one direction is a fault**
	(item ``#417``, decision `#396`). The manifest's version is a *cache key* that has to move
	on any change under ``plugins/``, so it leads between releases — which means a plain
	"these disagree" fired in the designed steady state: always on a development install, and
	on a released one from the first plugin change until the next release. A warning that is
	usually wrong is one nobody reads, and the skill tells an agent to act on this one.

	So the clause speaks only when the plugin is **behind**, which is the failure that has
	actually happened twice (`#380`, and `#393` when `#380`'s guard was too weak): a cached
	copy older than the program, carrying a skill and configuration fields that describe
	something else. When either version cannot be ordered — a development build, which is every
	editable install — it says nothing at all, because
	:func:`subroutine.installations.ordered` declines rather than guesses.

	**The program and the instance has no *direction*, and that part is not an inconsistency.**
	The instance's version arrives over the wire from a machine somebody else may run, and
	neither being ahead is designed, so this clause asks whether they differ rather than which
	leads. `#345` is what it costs — a field one side has and the other does not — and that is
	symmetrical.

	**What it does share with the clause below is the silence** (`#481`). It compared the two
	*strings*, so on an editable install — every development machine — it fired permanently
	against an instance built from the same commit, because a development build's version is
	fixed at whatever tag its last install saw while the code it runs is the working tree. That
	is not evidence about the code, so there is nothing to say. An instance reporting no version
	at all still warns: predating the field is a fact rather than a comparison.
	"""

	# **A null here is a fact, not a gap.** An instance that sends no version is one that
	# predates this field, which is itself the answer to "why does the feature I read about
	# not work" — so it is worded as a finding rather than left blank.
	instance = me.instance_version or "too old to say"
	schema = "" if me.schema_revision is None else f", schema {me.schema_revision}"

	# **Nothing to compare, so nothing is claimed** (`#564`). Every clause below asks whether
	# two versions agree, and there is only one version here — so they are skipped rather than
	# fed the instance twice, which is precisely how this reported agreement with itself.
	if program is None:
		return [
			f"Instance {instance}{schema}.",
			"What you are running is not visible from here — these tools answer on the "
			"instance, and your plugin and program are on your own machine. Run 'subroutine "
			"whoami' in a terminal there to compare all three.",
		]

	seen = [] if plugin is None else [f"plugin {plugin}"]

	seen.extend([f"program {program}", f"instance {instance}"])

	where = ", ".join(seen)
	lines = [f"{where[0].upper()}{where[1:]}{schema}."]

	# Each disagreement names the failure it actually produced, on 2026-08-03, rather than
	# advising a refresh in general terms: `#345` was a field one side had and the other did
	# not, and `#379` was an argument a tool offered that its program had never heard of.
	running = subroutine.installations.ordered(program)
	served = (
		None
		if me.instance_version is None
		else subroutine.installations.ordered(me.instance_version)
	)

	# **Only when both can actually be compared** (`#481`), which is the half of `#417` the
	# clause below learned and this one did not — eight lines apart, one rule applied to one
	# side of a pair. An editable install's version string is fixed at whatever tag its last
	# `pip install -e .` saw while the code it runs is the working tree, so a plain `!=` fired
	# permanently on every development machine, including the one the skill points an agent at,
	# and said nothing true. Measured against an instance built from the same commit, with
	# `doctor` reporting the machine coherent in the same minute.
	#
	# An instance reporting *no* version still warns: that is a fact rather than a comparison —
	# it predates the field, so it is genuinely older than anything asking.
	if me.instance_version is None or (
		running is not None and served is not None and running != served
	):
		lines.append(
			"The program and the instance disagree, so a call may be refused for a field "
			"one of them does not have."
		)

	# **Behind, not merely different** (`#417`). Ahead is what `#396` designs for, so warning
	# about it trains the reader — and the agent the skill points here — to ignore the line.
	# Either version being unorderable means no answer rather than a guessed one.
	carried = None if plugin is None else subroutine.installations.ordered(plugin)

	if carried is not None and running is not None and carried < running:
		lines.append(
			"The plugin is older than the program, so its skill and its configuration "
			"describe an earlier version of these tools. Refreshing the plugin is the fix."
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
		"narrows": subroutine.domain.authentication.narrowing(
			scopes=row.scopes,
			project_scope=row.project_scope,
			project_write_scope=row.project_write_scope,
			workspace_id=row.workspace_id,
		),
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


def _from_a_live_series (
	vocabulary: Vocabulary, row: subroutine.db.models.work.Task, field: str
) -> typing.Any:
	"""Return one of a template's repeat fields, or ``None`` if the series has stopped.

	**A stopped series is a finished template rather than a cleared column** (§6.7), so the
	occurrence in hand keeps pointing at one — and reading straight through made it advertise
	a rule that would never fire again. `#920`: a row promising *every month, on the 30th*
	about a series somebody had just stopped, on the surface they would check to see that the
	stop had worked.

	``recurrence_template_ref`` deliberately still resolves, because *this came from that
	series* stays true after it ends and is how a client reaches the history.
	"""

	if row.recurrence_template_id is None:
		return None

	series = vocabulary.parents.get(row.recurrence_template_id, {})

	if series.get("completed_at") is not None:
		return None

	return series.get(field)


def _username (vocabulary: Vocabulary, user_id: uuid.UUID | None) -> str | None:
	"""Return a user's username, or ``None`` when nobody is named.

	**Nobody assigned and a name that could not be loaded are both ``None``**, on
	``_parent_field``'s reasoning: the id is reported beside this either way, so a caller that
	genuinely needs to tell them apart still can, and a surface printing a name has nothing
	useful to say about the difference.
	"""

	if user_id is None:
		return None

	name = vocabulary.users.get(user_id, {}).get("username")

	return None if name is None else str(name)


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
# --- What this installation calls things (`/v1/meta`) -----------------------------------
#
# **Moved out of `api/meta.py` for `#486`**, on exactly `views.py`'s founding argument: both
# clients answer `meta()` now, so a model defined inside the transport would be reachable by
# one of them and not the other — the divergence S3-07 removed for tasks, recreated for the
# one response whose entire job is telling a caller what this installation is.
#
# It is also what `tests/test_response_compatibility.py` requires rather than prefers: that
# guard scopes itself to `subroutine.views` because every model `clients/http.py` validates
# comes from here, and it asserts as much. Parsing a `Meta` defined in `api/` would leave the
# new-field-must-be-defaulted rule silently not covering it (`#345`, three times).


class Named(pydantic.BaseModel):
	"""A vocabulary entry, as this workspace has it.

	``label`` rather than ``title``, matching both the column and §13.2's example: it is
	what to show a person, while ``key`` is what to send back.
	"""

	key: str
	label: str
	is_default: bool = False


class Status(Named):
	"""A status, with the fixed category a client may branch on."""

	#: The key is renameable; the category is not. Branch on this.
	category: str


class LinkType(pydantic.BaseModel):
	"""A link type, and how it reads from each end.

	There is no ``inverse_key``, and that is settled rather than missing: **the API names
	the direction, not the inverse type**. A link response carries ``link_type`` (this key),
	``direction`` (``outgoing`` or ``incoming``) and a ``label`` already the right way round.
	Deriving an inverse key by lower-casing ``inverse_title`` works for the five seeded
	types and breaks on the first custom one.
	"""

	key: str
	title: str
	inverse_title: str
	is_symmetric: bool


class Tag(pydantic.BaseModel):
	"""A tag, and how much it is used."""

	name: str
	usage: int


class Tags(pydantic.BaseModel):
	"""The tag list, and an honest statement of what was left out."""

	items: list[Tag]
	total: int
	truncated: bool


class Listing(pydantic.BaseModel):
	"""What one collection endpoint accepts.

	Reflected from the running application, so it cannot claim a filter that does not
	exist or omit one that does.
	"""

	path: str
	filters: list[str]
	sortable: list[str]

	#: What ``?fields=`` may name, and what ``?format=`` accepts (SPEC.md §14.10). Published
	#: for the reason ``sortable`` is: an agent that has to discover a field name by being
	#: refused has paid for the discovery in context, which is the cost shaping exists to
	#: avoid in the first place.
	selectable: list[str]
	formats: list[str]


class Grammar(pydantic.BaseModel):
	"""One of the small closed languages this installation parses."""

	description: str
	vocabulary: list[str]
	examples: list[str]


class Limits(pydantic.BaseModel):
	"""The bounds a request is held to."""

	default_page_size: int
	max_page_size: int
	max_title_length: int
	max_hierarchy_depth: int
	max_estimate_minutes: int


class Meta(pydantic.BaseModel):
	"""Everything needed to construct a valid request against *this* installation."""

	api_version: str
	server_time: datetime.datetime
	instance: Instance | None

	#: Which release this installation is running — the same value ``/v1/me`` has carried
	#: since `#381`, published here too because **this is the response every client fetches
	#: first** (`#250`). A client that learns it from ``identity()`` can explain a later
	#: failure in terms of versions instead of reporting a shape it did not expect.
	#:
	#: Deliberately the same field name as on :class:`Me`, so a client looks for one key
	#: rather than knowing which endpoint answered.
	#:
	#: **Defaulted, like everything added to a response model after it shipped** (`#345`,
	#: `#482`). An instance older than this field sends no such key and must keep working.
	instance_version: str | None = None

	#: What this is, for the reader who arrived here with a base URL and a token and nothing
	#: else. **Addressed to an agent, because an agent is the caller that has no other way to
	#: find out** — a person has the README. It costs about thirty tokens against §13.1's size
	#: budget and it is the one response every client fetches first, which is the whole
	#: argument for it being here rather than only in the guide it points at.
	purpose: str

	#: Which implementation answers ``q`` here — ``like`` or ``native`` (§9.4, item `#823`).
	#:
	#: **Published because the two find different things, not merely at different speeds.**
	#: ``native`` stems, so ``seed`` finds ``seeded``, and matches a trailing prefix, so
	#: ``curs`` finds ``cursor``; ``like`` matches any substring, so it alone finds ``cursor``
	#: from ``ursor``. A caller that knows which is in force can tell an empty result that
	#: means *there is nothing* from one that means *not with those letters*.
	#:
	#: §9.4 designed this channel for exactly this — *"agents learn which is available from
	#: /v1/meta"* — and it is what lets an operator ask for ``native`` on SQLite and be told
	#: what happened rather than refused: the request is legitimate, the backend is simply not
	#: there (`#871`).
	#:
	#: **Defaulted, like everything added to a response model after it shipped** (`#345`,
	#: `#482`): an instance older than this field sends no such key and must keep working.
	search_backend: str | None = None

	#: Where this instance's source can be obtained. Published as a commitment rather than
	#: because anything compels it: the AGPL's network clause did, FSL-1.1-ALv2 does not, and
	#: it is published anyway (SPEC.md §2.2) because somebody using an instance ought to be
	#: able to find the source of what they are using.
	source_url: str

	#: The address this instance is served on, when a deployment has said (SPEC.md §12.4).
	#: Null on a laptop listening on loopback, which is the ordinary case and is not a gap: a
	#: client that reached this response already knows one address that works. It is here for
	#: the client that must hand out a *durable* one — a webhook target, a shared link, or the
	#: ``subroutine:`` address of an item on this instance — which is not the same as whatever
	#: host happened to be dialled.
	public_url: str | None

	workspace: uuid.UUID | None
	workspaces: list[WorkspaceRef]

	statuses: dict[str, list[Status]]
	item_types: dict[str, list[Named]]
	link_types: list[LinkType]
	linkable_types: list[str]
	tags: Tags

	listings: dict[str, Listing]
	grammars: dict[str, Grammar]
	limits: Limits
	error_codes: list[str]
	docs: dict[str, str]
