"""How a task and a project look on the wire, and the envelope a collection travels in.

**This is deliberately not in the ``api`` package**, and moving it out was a requirement
rather than tidying. docs/design.md §13.7 makes the local database a connection like any other, so
``subroutine agenda`` fans out across it and every remote through one code path that does not
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
(docs/design.md §13.1). The keys are batch-loaded per page, never per row.

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
import subroutine.domain.dates
import subroutine.domain.durations
import subroutine.domain.events
import subroutine.domain.instances
import subroutine.domain.links
import subroutine.domain.projects
import subroutine.domain.readiness
import subroutine.domain.recurrence
import subroutine.domain.refs
import subroutine.domain.schedule
import subroutine.domain.settings
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
	"""What is at the far end of a link, with enough of the row to **judge** it.

	Enough, and no more. A caller looking at an item's links wants to know what it is joined
	to, not to receive every field of everything it touches — and an end the caller may not
	see is never reported at all, which is :mod:`subroutine.domain.links`' obligation rather
	than this model's.

	**What changed with `#970` is what *enough* means, not the rule.** It was five fields,
	which identify a thing; Simon, reading `#94`'s own links: *"I cannot look at a task and
	see whether all of its blockers are complete, without looking at each blocker
	individually."* Identifying an end is not judging one, and a list of blockers nobody can
	judge is a list that has to be clicked through one item at a time.

	**The set is derived rather than chosen.** These are the fields the browser's ``marks``
	reads — the indicator vocabulary its list, board and agenda rows already share — so a link
	line renders through the same function as a row and cannot drift from it.
	``tests/test_web.py`` fails if ``marks`` grows a read this cannot answer.

	**And it is a projection of the full rendering rather than a parallel one**, which is what
	:func:`_end` is about: sixteen of :class:`Task`'s fifty-nine, resolved by the code that
	resolves them for a row. The one field deliberately not taken is ``description`` — the
	whole body of every item this one touches, which is what the paragraph above refuses and
	what `#595` measured as a first-order cost.
	"""

	entity_type: str
	id: uuid.UUID
	ref: int
	title: str

	#: What it is and where it lives. A ref says which item; these say whether it is a bug in
	#: another project or a decision document in this one, which is most of what a reader
	#: scanning a blocker list is asking.
	type: str = ""

	#: What kind of thing that type is, for a client that does not recognise the key (`#1134`).
	#: Here for the reason every field on this model is: ``marks`` reads it, and a link line
	#: renders through the same function as a row.
	type_category: str = ""
	project_path: str = ""

	#: What state it is in. ``status_is_default`` is what stops every open item carrying a
	#: mark that says nothing (§12.2a), and ``status_category`` is what tells *cancelled* from
	#: *done* — a distinction ``is_complete`` deliberately does not make and which read as
	#: `done` on the item page until this landed.
	status: str = ""
	status_category: str = ""
	status_is_default: bool = False

	#: Whether this end is itself waiting on something, or holding something up. A blocker
	#: that is itself blocked is the answer to *why has this not moved*, and it was reachable
	#: only by opening it.
	blocked: bool = False
	blocking: bool = False

	#: Who has it and who is on it now. A lease expires, so ``claim_expires_at`` travels with
	#: the holder for the reason :class:`Task` gives: a client answers *is this still held*
	#: without a request per row.
	assignee: str | None = None
	claimed_by_id: uuid.UUID | None = None
	claimed_by: str | None = None
	claim_expires_at: datetime.datetime | None = None

	#: When, with the zone that stored it. ``timezone`` is not decoration: a day-scale date
	#: rendered in the reader's zone rather than the one it was written in is `#773`, which
	#: shipped once and was correct in winter.
	due_at: datetime.datetime | None = None
	snoozed_until: datetime.datetime | None = None
	snoozed_is_all_day: bool = False
	timezone: str | None = None

	#: That it comes back at all (`#925`). Generated on the server, like every other reading
	#: of a rule, because a client would otherwise need a second copy of the grammar.
	recurrence_description: str | None = None

	#: What somebody labelled it (`#1019`). Here because a link line draws the same marks a
	#: row does and the two must not say different things about one item, which is the whole
	#: obligation this model carries — the guard derives its field list from what ``marks``
	#: reads, so a mark added to a row fails until this can answer it.
	tags: list[str] = pydantic.Field(default_factory=list)

	#: Whether the thing at this end is finished (`#210`). A link is how `#84` models a
	#: milestone — an item whose blockers are its contents — so a client rendering "N of M"
	#: needs this and would otherwise have to fetch every end to count them.
	#:
	#: **Only a task can be finished.** A document has no state that could, so one is reported
	#: as incomplete rather than judged by a status it does not have.
	is_complete: bool = False


class Edge(pydantic.BaseModel):
	"""A link among a page's items, named by both its ends (docs/design.md §5.7, §8.4).

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

	``id`` is the one value in this program that must never change (docs/design.md §13.7). A client
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

	#: The zone this caller's *typed* days are read in here — §6.5's chain already resolved,
	#: for this account, in this workspace (decision `#1088`).
	#:
	#: **Published rather than left to the client to derive** (`#925`, `#1083`). A written day
	#: like ``friday`` or ``today`` means the day it is in the **account's** zone, and only the
	#: instance holds that chain: a client resolving it reached for whatever zone the machine it
	#: runs on was set to — the laptop for a terminal, and the *server's* ``/etc/localtime`` for
	#: a relayed agent, since `#539` runs those tools inside the instance. One word therefore
	#: meant up to three different days depending on who asked.
	#:
	#: **The chain is resolved here rather than published in parts**, which is the difference
	#: between this and the raw ``timezone`` on :class:`WorkspaceAccess`: a client given the
	#: three levels would hold a copy of §6.5, and two copies of a rule is what this codebase
	#: spends most of its time removing.
	#:
	#: **Null means this response does not carry it**, not that no zone applies — an instance one
	#: release behind sends no such key (`#345`, `#482`), and only ``GET /v1/meta`` fills it in.
	#: A caller that finds it null falls back to its own machine, which is what it did before.
	reader_timezone: str | None = None

	#: The address of the one project whose work rises in this workspace's ranked listings
	#: (decision ``#982``), or null for none — which is most workspaces.
	#:
	#: **The whole disclosure lives on this one field, and that is the design rather than
	#: economy.** `#851` requires a computed rank to be able to say why, and 84% of this
	#: instance's open tasks are in the project most likely to be prioritised — so a mark on
	#: each task row would appear on 84% of them, which §12.2a drops as saying nothing. The fact
	#: is about the *list*, so it is said once in a header; and wherever projects are listed, the
	#: one project whose address equals this is the one to mark. A path is unique inside a
	#: workspace, so that comparison is exact and needs no second field.
	#:
	#: **A path rather than an id**, because every reader wants the same string: it is what a
	#: person reads, and since `#957` it is what a caller can send back to ``PATCH
	#: /v1/workspaces``.
	#:
	#: **Never a magnitude.** The bonus is a fixed number in the ordering and is deliberately
	#: unpublished: a visible one invites *"can I set it to 2?"*, which is the dial decision
	#: ``#982`` declines. Surfaces say *prioritised*.
	#:
	#: **Null where the caller cannot see the project**, not where none is set — the two are
	#: indistinguishable here on purpose, since a focus somebody's credential cannot reach is
	#: one they receive no bonus from either (``scoping.prioritised_projects``).
	#:
	#: **Defaulted** (`#345`, `#482`): an instance older than this field sends no such key.
	prioritised_project: str | None = None


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

	#: Where it lives, as a whole address inside its workspace — ``subroutine/ui`` (`#512`).
	#:
	#: **Beside ``project_key`` rather than instead of it.** The key is what a project is
	#: *called* and is what a heading says; this is what it is *addressed* by, and since
	#: `#957` those are two different strings whenever a key is shared. A caller wanting one
	#: word still has one.
	#:
	#: **Workspace-relative, so it is what a caller can send back** — `#151`'s rule. It goes
	#: straight into ``--project``, ``?project=`` and ``+key``. The workspace is reported
	#: separately and a surface composing a label prefixes it when the request did not already
	#: say which workspace (decision `#957` §4).
	#:
	#: **Composed on the server, once, rather than by every client.** A client would need the
	#: whole ancestor chain to build this, which is a second query and a second implementation
	#: — ``recurrence_description``'s argument (`#925`) applied to a tree: when a client would
	#: need a copy of a rule to render a field, publish the rendering.
	#:
	#: Empty only where an ancestor could not be read, which no supported path produces.
	#: **Defaulted** (`#345`, `#482`): an instance older than this field sends no such key.
	project_path: str = ""

	#: The colour this item is marked with — a palette *name*, never a value (`#1026`).
	#:
	#: **What is in force, not what was chosen.** A project's own colour, or the nearest
	#: ancestor's, or its workspace's, or ``None``. Which of those supplied it is deliberately
	#: not reported: a row renders a mark and has no use for the provenance, and a settings form
	#: reads the entity's own ``settings`` to tell chosen from inherited.
	#:
	#: **A name so that every surface can render it its own way**, or ignore it — the terminal
	#: draws nothing today (Simon, 2026-08-19) and needs no resolver to decline. A stored value
	#: could not be rendered under `#102`'s sixteen-ANSI rule at all.
	#:
	#: **Defaulted** (`#345`, `#482`), and ``None`` is a real answer meaning *nothing up this
	#: tree has chosen one* rather than *this instance did not say*.
	project_colour: str | None = None
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
	#: ``status_category`` is the fixed set a client can branch on (docs/design.md §5.5).
	status: str
	status_category: str
	status_id: uuid.UUID

	#: Whether this is the status every item starts in, so a surface can tell a decision
	#: somebody made from the absence of one. `#168`: without it `subroutine show` had no way
	#: to print `blocked` while staying quiet about `open`, so it printed neither — and a
	#: status somebody set was stored and then invisible everywhere.
	status_is_default: bool = False
	type: str

	#: The fixed set a client may branch on when it does not recognise ``type`` — decision
	#: `#1133`, and ``status_category``'s counterpart one vocabulary along. A workspace may call
	#: a type anything; this says whether the thing is work, a defect, a question, a decision, a
	#: reference or a record.
	type_category: str = ""

	#: Whether this is the type every item of its kind starts as (`#1135`), so a surface can say
	#: nothing about a type nobody chose — §12.2a's rule that a column saying the same thing on
	#: every row says nothing, applied to a fact rather than a column.
	#:
	#: ``status_is_default``'s counterpart, and it was the missing half: the terminal decided by
	#: hardcoding ``("task", "note")``, which are the keys *this installation's seeder* happens
	#: to use. That is latent until `#1129` lets a workspace rename ``task``, at which point a
	#: workspace whose default is ``story`` prints `story` on every line.
	type_is_default: bool = False
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

	#: **When it is over** — decision `#1235`, and the far half of a span. Null on almost
	#: everything: a task has a start because somebody planned it, and an end only when it
	#: occupies a period — a booked fortnight, a code freeze, an hour with the dentist.
	#:
	#: Defaulted for ``starts_at``'s reason above (`#345`, `#482`).
	ends_at: datetime.datetime | None = None

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

	#: The rule as a sentence, **generated from what is stored rather than echoed from what was
	#: typed** — `#925`. §6.7's whole argument is that reading a repeat back in *different words*
	#: is what turns an ambiguous natural-language feature into a checkable one, and until this
	#: the only surface that could do it was one holding a copy of the grammar.
	#:
	#: **`estimate_human`'s precedent exactly**, and the same reason: a grammar rendered once, on
	#: the server, so no client needs its own copy. The browser receives a rule and an anchor and
	#: has no way to turn either into English — so without this it could either say nothing about
	#: a repeating task, which is what it did, or carry a second implementation of `describe`
	#: free to disagree with the first in silence.
	#:
	#: It carries the anchor where that is news, so *every 3 days, from when it is done* reads as
	#: one fact rather than two fields a reader has to combine.
	recurrence_description: str | None = None

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

	#: How long before this a reminder is wanted — `#1211`. Both spellings, for
	#: ``estimate``'s reason above: ``reminder_human`` is what a person says and feeds
	#: straight back into ``reminder`` on a write.
	#:
	#: **Defaulted, so a client can read an instance that predates it** (`#345`, `#482`).
	reminder_minutes: int | None = None
	reminder_human: str | None = None

	#: The tag names on this task, alphabetical. Batch-loaded per page like the vocabulary
	#: above and for the same reason. A tag is never an id here: a client acts on the word,
	#: applies one by writing ``#health`` in a captured line, and would have to fetch a
	#: second list to learn what any id meant.
	tags: list[str] = pydantic.Field(default_factory=list)

	completed_at: datetime.datetime | None

	#: Whether this is finished, which is ``completed_at is not None`` and is here so that
	#: nothing has to work that out again (`#1281`).
	#:
	#: **It was on a link's end and nowhere else**, so four renderings of one fact existed: the
	#: link view derived it, the terminal and the agent's ``show`` each re-derived it from
	#: ``completed_at``, and the browser asked a *task* for the field — getting ``undefined``
	#: on every row. A parent's sub-tasks therefore counted **0 of 13** with thirteen finished,
	#: and none of them was struck through, on the one surface a person is most likely to be
	#: looking at.
	#:
	#: **Derived on the server rather than left to each client**, which is the whole point: the
	#: alternative offered was for the browser to read ``completed_at`` itself, and that is a
	#: fifth copy of the rule — in the same page where the defect being fixed is two lists
	#: disagreeing about one item.
	#:
	#: **It does not tell *done* from *cancelled***, deliberately, and neither does the link
	#: end's. ``status_category`` is what answers that; this answers *is it over*.
	is_complete: bool = False

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


	#: The concurrency token (docs/design.md §8.9), reported so a caller can send it back.
	version: int

	def address (self) -> int:
		"""Return what a caller addresses this by — its ref (docs/design.md §6.2)."""

		return self.ref

	def columns (self, reader: str | None) -> tuple[str, ...]:
		"""Return this task as the cells of one compact line (docs/design.md §14.10).

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

		**Both dates are rendered in the zone they were stored in** (`#773`, `#1090`). A
		day-scale value is an instant at one end of its own day, so ``.date()`` on the stored
		UTC value reports the day either side of itself for anybody not on UTC — which this
		did, on both columns, until `#1090`. Decision `#1088`: a day is a label and never
		converts.
		"""

		return (
			subroutine.domain.refs.format_ref(self.ref),
			f"[{self.status}]",
			_priority_cell(self.importance, self.urgency),
			"—" if self.due_at is None else _day_cell(self.due_at, self.timezone),
			"" if self.starts_at is None else f"→{_day_cell(self.starts_at, self.timezone)}",
			subroutine.domain.text.truncated(self.title),
			"" if self.assignee is None else f"@{self.assignee}",
			" ".join(f"#{name}" for name in self.tags),
		)


class Backlink(pydantic.BaseModel):
	"""One piece of prose that refers to an item — `#144`.

	**The mention table has been written by every title, description, body and comment since
	M1 and read by nothing.** `domain/mentions.backlinks` had no caller and §8.5's
	``?include=backlinks`` was honestly refused, so *what refers to this?* — the question the
	whole table exists for — was answerable on no surface at all.

	**It names something a reader can open**, which is what makes the list worth having: a ref
	and a title, the same argument `#970` makes for a link's far end. A comment has no ref, so
	it resolves to the item it is on and says ``via`` so nobody goes looking for the sentence
	in that item's own prose.
	"""

	#: What is doing the referring, once resolved: ``task`` or ``document``.
	kind: str

	ref: int
	title: str

	#: ``"comment"`` when the sentence is in a comment rather than in the item's own prose.
	via: str | None = None

	created_at: datetime.datetime

	def address (self) -> str:
		"""Return what a caller addresses the referring item by."""

		return str(subroutine.domain.refs.format_ref(self.ref))

	def columns (self, reader: str | None) -> tuple[str, ...]:
		"""Return this backlink as the cells of one compact line."""

		return (self.address(), self.via or "", subroutine.domain.text.truncated(self.title))


class Comment(pydantic.BaseModel):
	"""One entry in an item's record of what happened (docs/design.md §5.10).

	No ``parent_comment_id``: comments are flat and chronological by decision, and the column
	stays in the schema as the escape hatch rather than as a field anybody can set.
	"""

	id: uuid.UUID
	body: str

	entity_type: str
	entity_id: uuid.UUID
	workspace_id: uuid.UUID
	author_id: uuid.UUID | None

	#: Who wrote it, by username — `#636`. **The one view whose whole purpose is reading what
	#: people recorded could not say who recorded it**, so a surface wanting the name had a
	#: lookup per comment, which is `#39`'s N+1 where it can least be afforded.
	#:
	#: Every neighbouring view already answers this: a task publishes ``assignee`` beside
	#: ``assignee_id``, a link's far end carries ``ref`` and ``title`` rather than an id, and
	#: :class:`Token` carries ``username`` beside ``user_id`` — *a caller may be looking at
	#: credentials they issued for somebody else, so the name is here rather than only the id*.
	#: That argument is stronger for a comment, not weaker.
	#:
	#: **Defaulted, like everything added to a response model after it shipped** (`#345`,
	#: `#482`). An instance older than this field sends no such key, and a client one commit
	#: ahead must not refuse the whole response over it.
	author: str | None = None

	deleted_at: datetime.datetime | None
	created_at: datetime.datetime
	updated_at: datetime.datetime
	version: int

	def address (self) -> str:
		"""Return what a caller addresses this by. A comment has no ref of its own."""

		return str(self.id)

	def columns (self, reader: str | None) -> tuple[str, ...]:
		"""Return this comment as the cells of one compact line.

		``reader`` is the zone the *caller* reads days in, and every ``columns`` takes it
		whether or not it uses one — because a cell rendering a moment as a day has no answer
		without it (`#1091`), and a signature that only some of them carried would let the
		next one be added without the question being asked.
		"""

		return (
			moment_day(self.created_at, reader),
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

	def columns (self, reader: str | None) -> tuple[str, ...]:
		"""Return this event as the cells of one compact line, dated where the caller is."""

		return (
			str(self.seq),
			moment_day(self.created_at, reader),
			self.action,
			self.entity_type,
		)



class Changes(Collection[Event]):
	"""The change feed, and which kinds of thing it is able to tell you about — `#1085`.

	**Stated positively, and always**, which is Simon's refinement of 2026-08-22 on the
	obvious alternative. Naming what was *left out* would need the reader to know which kinds
	exist before the omission means anything, and would say nothing at all to a caller who is
	not narrowed. *This feed covers tasks and documents* is a complete sentence either way.

	**It exists because a feed is not a listing of one kind.** A credential narrowed away from
	one of the three used to be refused the whole feed, because each kind enforces its own read
	scope — so an agent whose skill tells it to *ask what changed first* failed on its first
	call rather than degrading to the two thirds it could see.

	``covers`` is a subset of ``scoping.readable_event_kinds``' vocabulary, in a stable order.
	Workspace-level events are not listed: they are narrowed by membership rather than by a
	read verb, so they are carried for everyone and naming them would imply they could be
	withheld.
	"""

	covers: list[str]

class Link(pydantic.BaseModel):
	"""One link, seen from the item that was asked about (docs/design.md §5.7).

	A link is one stored row displayed from both ends, so ``label`` arrives already the right
	way round: "Blocks" from one end and "Blocked by" from the other, off the same row. A
	client that had to invert it would be a second place the inverse could be got wrong.
	"""

	id: uuid.UUID
	link_type: str

	#: What that type *is*, so a reader can decide what the link means without knowing what this
	#: workspace calls it — decision `#1157`. Three surfaces counted blockers by comparing
	#: ``link_type`` to the literal ``blocks``, and `#1156` measured what that costs.
	#:
	#: **Defaulted, because this model is not new** (`#345`, and `#1155` is the last time that
	#: was learned the expensive way): an instance one release behind sends a link without it,
	#: and a required field would make a newer client refuse the whole instance.
	#:
	#: **``None`` rather than ``""``, and that is the whole of `#1168`.** Being defaulted is not
	#: enough on a field somebody *branches* on. An absent category read as the empty string is
	#: read as *not gating*, so against a 0.7.6 instance every blocker count silently went to
	#: zero — held work reported as free to start. ``None`` says *this instance did not say*,
	#: which is a thing :meth:`_read_the_key_when_the_server_did_not_say` can answer and the
	#: empty string is not.
	link_category: str | None = None

	label: str
	direction: str
	other: LinkEnd

	@pydantic.model_validator(mode="after")
	def _read_the_key_when_the_server_did_not_say (self) -> "Link":
		"""Fill the category from the key when the answer came from before there were any.

		**Here rather than on the three surfaces that branch on it** (`#1168`, Simon's call).
		The alternative was a fallback at each consumer, which puts the ``blocks`` literal back
		on the terminal, the agent surface and the browser — the three copies `#1157` spent a
		commit removing and `#1156` was filed about.

		It runs on every ``Link``, and is a no-op for all but one caller: the local client and
		the API both build this from a NOT NULL column, so the value is already there. Only an
		HTTP client parsing an older instance's answer arrives with nothing.

		A key this program does not recognise is left as ``None``. That is honest — a relation
		somebody added by hand on an instance that had no categories cannot be classified from
		here — and it is what the pre-category rule did with it too.
		"""

		if self.link_category is None:
			said = subroutine.domain.links.BEFORE_CATEGORIES.get(self.link_type)

			if said is not None:
				# `object.__setattr__` is not needed — this model is not frozen — but assigning
				# inside an `after` validator re-runs validation unless assignment validation is
				# off, which it is. Checked rather than assumed: `model_config` sets no
				# `validate_assignment`, so pydantic's default of False applies.
				self.link_category = said

		return self

	def address (self) -> str:
		"""Return what a caller addresses this by. A link has no ref of its own."""

		return str(self.id)

	def columns (self, reader: str | None) -> tuple[str, ...]:
		"""Return this link as the cells of one compact line."""

		return (
			self.label,
			subroutine.domain.refs.format_ref(self.other.ref),
			subroutine.domain.text.truncated(self.other.title),
		)


class Governing(pydantic.BaseModel):
	"""A document in force that a typed link says binds this item (`#1119`).

	``subroutine://conventions`` narrowed to one item: that resource says what binds anybody
	working in this workspace, and this says what binds whoever picks *this* up.

	**Titles and refs, never bodies**, which is what makes it affordable. §6.14 makes a
	document's title state its conclusion, so the list is readable on its own and a reader
	fetches only the one they need — a reading list that inlined its reading would be the cost
	it exists to remove.
	"""

	#: How it was said — ``documents`` is *this decision settles that work*, ``derives_from``
	#: is *this work comes out of that specification*. Two different sentences, and a reader
	#: deciding what to read first is served by knowing which one they have.
	link_type: str
	document: LinkEnd

	def address (self) -> str:
		"""Return what a caller addresses this by."""

		return subroutine.domain.refs.format_ref(self.document.ref)

	def columns (self, reader: str | None) -> tuple[str, ...]:
		"""Return this as the cells of one compact line."""

		return (
			subroutine.domain.refs.format_ref(self.document.ref),
			self.document.type or "",
			subroutine.domain.text.truncated(self.document.title),
		)


class Proposal(pydantic.BaseModel):
	"""A link the writing already implies and nobody has confirmed (`#1137`).

	**Deliberately not a :class:`Link`.** It carries no ``id`` because there is no row, and a
	client that could not tell the two apart would report a suggestion as a fact. What it is
	instead is an *offer*: everything needed to make the link, plus the evidence, so that a
	person or an agent can judge it rather than accept it.

	``because`` is the half that makes it judgeable. A citation in prose is written the same
	way whether it means *this follows that decision* or *this contradicts it*, so a proposal
	that could only be accepted or ignored would be asking for a rubber stamp.
	"""

	link_type: str
	label: str
	direction: str
	other: LinkEnd
	because: str

	def address (self) -> str:
		"""Return what a caller addresses this by. There is nothing to address until it exists."""

		return subroutine.domain.refs.format_ref(self.other.ref)

	def columns (self, reader: str | None) -> tuple[str, ...]:
		"""Return this proposal as the cells of one compact line."""

		return (
			subroutine.domain.refs.format_ref(self.other.ref),
			subroutine.domain.text.truncated(self.other.title),
			self.because,
		)


class Verification(pydantic.BaseModel):
	"""What was checked against a task, and which tree it was checked on (`#1121`).

	**A record, not a proof.** An agent can post an exit code of zero without having run
	anything, so what this is worth is being durable, attributable and invalidatable — never
	*verified work*. `#593` settled that sentence and nothing built on this model may soften it.

	``is_stale`` is deliberately absent: it is derived from the tree the *reader* is standing
	on, which is not on this row and is not on the instance either. What is published is
	``tree_hash``, and the comparison belongs to whoever has a checkout.
	"""

	id: uuid.UUID
	task_ref: int
	passed: bool
	summary: str | None
	output_excerpt: str | None
	ran_at: datetime.datetime

	#: Null where the record was made from a machine with no checkout, which §1.4 requires to
	#: be possible. Such a record cannot expire, and saying nothing is a different answer from
	#: saying it is current.
	tree_hash: str | None
	commit_sha: str | None

	#: Who recorded it, by name. The whole value of the record is that it is attributable, and
	#: a uuid is attributable to somebody who can make a second request.
	recorded_by: str | None

	created_at: datetime.datetime

	def address (self) -> str:
		"""Return what a caller addresses this by."""

		return str(self.id)

	def columns (self, reader: str | None) -> tuple[str, ...]:
		"""Return this record as the cells of one compact line."""

		return (
			"passed" if self.passed else "failed",
			moment_day(self.ran_at, reader),
			subroutine.domain.text.truncated(self.summary or ""),
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

	#: The project this workspace has prioritised, as its address — see
	#: :class:`WorkspaceRef`, where the same field is documented and where most readers meet it.
	#: Settable through ``PATCH /v1/workspaces``, which is the point: the state belongs to the
	#: workspace, so this is the model that both reports and accepts it.
	#:
	#: **Defaulted** (`#345`, `#482`).
	prioritised_project: str | None = None

	settings: dict[str, typing.Any]

	deleted_at: datetime.datetime | None
	created_at: datetime.datetime
	updated_at: datetime.datetime
	version: int

	def address (self) -> str:
		"""Return what a caller addresses this by — its short name (docs/design.md §13.7)."""

		return self.slug

	def columns (self, reader: str | None) -> tuple[str, ...]:
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

	#: When this account last **signed in** — a login link redeemed into a browser session
	#: (`#526`). Null means never, which on a service account is the ordinary state: an agent
	#: presents a token and never signs in at all, so this is null for it forever and that is
	#: the honest answer rather than a gap. *When was this credential last used* is a different
	#: question and `Token.last_used_at` answers it.
	#:
	#: Reported here rather than only to the account itself because the question it answers is
	#: an operator's — *is anybody still using this account* — which is the same question
	#: ``is_active`` is beside. It is not an email: §5's rule is that identifiers are public and
	#: content is not, and a login timestamp is neither content nor a contact route.
	#:
	#: Defaulted for `#345`'s reason: added after this model shipped, so an older instance's
	#: body must still parse.
	last_login_at: datetime.datetime | None = None

	def address (self) -> str:
		"""Return what a caller addresses this by — the username."""

		return self.username

	def columns (self, reader: str | None) -> tuple[str, ...]:
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

	#: Empty means **no narrowing**, not "no permissions" (docs/design.md §7.3).
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
	#: by a superuser, and narrowed by the credential even then (docs/design.md §7.1).
	instance_permissions: list[str]

	#: The zone this caller reads days in for a question that is **not** about a workspace —
	#: a credential's expiry, a feed's last poll, an instance's own history (`#1091`). §6.5
	#: with the workspace step omitted rather than guessed at, resolved here so the terminal's
	#: administrative commands do not fall back to whichever machine happens to be running
	#: them. Each workspace publishes its own answer beside this one.
	#:
	#: **Defaulted**, because an instance a release behind sends no such key and a client one
	#: commit ahead must not refuse the whole response over it (`#345`, `#482`).
	reader_timezone: str | None = None

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

	#: Whether the address in ``url`` was worked out rather than configured (`#1007`). True
	#: when nobody has set ``public_url`` and the address came from where this instance
	#: listens, or from the request that asked. Both are right on a laptop and both are wrong
	#: behind a proxy, where the internal address is not where anybody browses — so a surface
	#: that prints the link can say which it is, and the reader can tell a working link from
	#: one that will not resolve before they hand it over.
	#:
	#: **Defaulted, because an older instance answers without it** and a client refusing the
	#: body over a field it has just invented is `#345` for the third time.
	address_assumed: bool = False


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

	#: Empty means **no narrowing**, not "no permissions" (docs/design.md §7.3).
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

	def columns (self, reader: str | None) -> tuple[str, ...]:
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


class Calendar(pydantic.BaseModel):
	"""A calendar feed as it can safely be described — item `#916`, docs/design.md §20.3.

	**Everything but the secret**, exactly as :class:`Token` is: only a hash of it is stored,
	so there is nothing here to rebuild one from. ``prefix`` is the public half, and is what a
	listing prints and what resetting and revoking take.

	``last_polled_at`` is why this view is worth having at all. §20.3: a URL nobody has fetched
	for six months is one to revoke, and there is no other way to tell — a feed has no login,
	so *when was this last used* is the only signal that it is still wanted.
	"""

	id: uuid.UUID
	title: str
	prefix: str

	#: Which workspace's work it shows. A feed is pinned to one, and that is the column's own
	#: reason as well as this field's: an address that spanned workspaces could collide on refs.
	workspace_id: uuid.UUID

	#: ``everything`` or ``assigned_to_me`` — docs/design.md §20.1.
	audience: str

	#: The project it is narrowed to, and everything filed under it. Null means the whole
	#: workspace.
	project_id: uuid.UUID | None

	#: The same, as the address a person types — resolved here rather than by whoever prints
	#: it, for `#348`'s reason: a UUID in a listing is something to go and look up. Null
	#: alongside ``project_id``, and passed through unresolved where it names nothing visible,
	#: because a feed's reported reach must never look narrower than its real one.
	project_key: str | None = None

	#: The item types it shows, or null for all of them. **Ids rather than keys**, decision
	#: `#972` §5: §5.5 makes the vocabulary renameable and a feed has no reader to complain
	#: when it silently stops matching.
	item_type_ids: list[uuid.UUID] | None

	#: The same, as the keys that workspace currently gives them — what a person types back
	#: into ``--type``, which is `#151`'s rule that what a caller is shown is what it can send.
	#: A type that has since been deleted is simply absent, which is the honest rendering of a
	#: filter that no longer matches anything.
	item_types: list[str] | None = None

	#: Whether the URL would be answered right now. False for revoked and expired alike, for
	#: :class:`Token`'s reason: the question being asked at this moment is whether it works.
	usable: bool

	created_at: datetime.datetime
	last_polled_at: datetime.datetime | None
	expires_at: datetime.datetime | None
	revoked_at: datetime.datetime | None

	def address (self) -> str:
		"""Return what a caller addresses this by — the prefix, as resetting takes it."""

		return self.prefix

	def columns (self, reader: str | None) -> tuple[str, ...]:
		"""Return this feed as the cells of one compact line."""

		return (
			self.prefix,
			self.title,
			self.project_key or "everything",
			self.audience,
			"never polled" if self.last_polled_at is None else "",
			"" if self.usable else "not usable",
		)


class IssuedCalendar(Calendar):
	"""A feed at the one moment its URL exists in readable form — item `#916`.

	Returned by creating one and by resetting one, and by nothing else. A separate type rather
	than an optional field, for :class:`IssuedToken`'s reason: a field that is usually absent
	is one somebody eventually expects to find.
	"""

	#: Where to subscribe. **Null when the instance has not been told its own ``public_url``**
	#: — the whole URL is the credential, so a host guessed from a request header is a secret
	#: sent somewhere nobody chose. What a caller gets then is the feed with no URL, which says
	#: plainly that the instance cannot address itself.
	url: str | None = None


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

	def columns (self, reader: str | None) -> tuple[str, ...]:
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

	#: Its whole address inside its workspace — ``subroutine/ui`` (`#512`).
	#:
	#: **Not ``project.path``, which is ids and stays off this model.** §6.9 calls that an
	#: implementation of the hierarchy rather than a field of it, and it is ours to change;
	#: this is the *rendering*, which is a promise. A client assembling a tree still reads
	#: ``parent_id``, which is a fact.
	#:
	#: **Defaulted** (`#345`, `#482`): an instance older than this field sends no such key.
	path: str = ""

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

	#: The statuses this project does not offer, resolved up the tree — `#1029`.
	#:
	#: **Beside ``settings`` rather than inside it, because they answer different questions.**
	#: ``settings`` is what *this* project was told; this is what is **in force**, which may
	#: come from a parent or from the workspace. A client reading the raw map would see nothing
	#: on a project that inherits, and would have to hold every ancestor's settings and repeat
	#: the walk to find out why — `#925`'s rule, the same one that put ``project_colour`` on a
	#: row rather than publishing the chain.
	#:
	#: **On the project rather than on every item**, which is not only bytes: a form knows which
	#: project is *selected* before anything exists to hang a field on, so this is what lets the
	#: add form narrow at all. An item finds its own through ``project_id``, which every row
	#: already carries.
	#:
	#: Defaulted, because `#345`/`#482`: a client one commit ahead of an instance must not
	#: refuse the whole response for a field the instance has never heard of.
	hidden_statuses: list[str] = []

	archived_at: datetime.datetime | None
	deleted_at: datetime.datetime | None
	created_at: datetime.datetime
	updated_at: datetime.datetime
	version: int

	def address (self) -> str:
		"""Return what a caller addresses this by — its path, never a ref (docs/design.md §5.2).

		**The whole address since `#957`**, because a key stopped being one: two projects may
		be keyed ``dist``, and handing back a word that names either is handing back something
		a caller cannot send. `#151`'s rule.
		"""

		return self.path or self.key

	def columns (self, reader: str | None) -> tuple[str, ...]:
		"""Return this project as the cells of one compact line."""

		return (
			self.path or self.key,
			f"[{self.status}]",
			"private" if self.visibility == "private" else "",
			subroutine.domain.text.truncated(self.title),
		)


class Document(pydantic.BaseModel):
	"""A document as the API reports it.

	No ``due_at``, ``starts_at``, ``estimate_minutes`` or ``assignee_id``, and their
	absence is the point (docs/design.md §6.14): a specification is never "done" and nobody is
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

	#: Where it lives, as a whole address inside its workspace — ``subroutine/ui`` (`#512`).
	#:
	#: **Beside ``project_key`` rather than instead of it.** The key is what a project is
	#: *called* and is what a heading says; this is what it is *addressed* by, and since
	#: `#957` those are two different strings whenever a key is shared. A caller wanting one
	#: word still has one.
	#:
	#: **Workspace-relative, so it is what a caller can send back** — `#151`'s rule. It goes
	#: straight into ``--project``, ``?project=`` and ``+key``. The workspace is reported
	#: separately and a surface composing a label prefixes it when the request did not already
	#: say which workspace (decision `#957` §4).
	#:
	#: **Composed on the server, once, rather than by every client.** A client would need the
	#: whole ancestor chain to build this, which is a second query and a second implementation
	#: — ``recurrence_description``'s argument (`#925`) applied to a tree: when a client would
	#: need a copy of a rule to render a field, publish the rendering.
	#:
	#: Empty only where an ancestor could not be read, which no supported path produces.
	#: **Defaulted** (`#345`, `#482`): an instance older than this field sends no such key.
	project_path: str = ""

	#: The colour this item is marked with — a palette *name*, never a value (`#1026`).
	#:
	#: **What is in force, not what was chosen.** A project's own colour, or the nearest
	#: ancestor's, or its workspace's, or ``None``. Which of those supplied it is deliberately
	#: not reported: a row renders a mark and has no use for the provenance, and a settings form
	#: reads the entity's own ``settings`` to tell chosen from inherited.
	#:
	#: **A name so that every surface can render it its own way**, or ignore it — the terminal
	#: draws nothing today (Simon, 2026-08-19) and needs no resolver to decline. A stored value
	#: could not be rendered under `#102`'s sixteen-ANSI rule at all.
	#:
	#: **Defaulted** (`#345`, `#482`), and ``None`` is a real answer meaning *nothing up this
	#: tree has chosen one* rather than *this instance did not say*.
	project_colour: str | None = None
	parent_id: uuid.UUID | None

	status: str
	status_category: str
	status_id: uuid.UUID
	#: Whether this is the status it starts in — see :class:`Task`, same reason (`#168`).
	status_is_default: bool = False
	type: str

	#: The fixed set a client may branch on when it does not recognise ``type`` — decision
	#: `#1133`, and ``status_category``'s counterpart one vocabulary along. A workspace may call
	#: a type anything; this says whether the thing is work, a defect, a question, a decision, a
	#: reference or a record.
	type_category: str = ""

	#: Whether this is the type every item of its kind starts as (`#1135`), so a surface can say
	#: nothing about a type nobody chose — §12.2a's rule that a column saying the same thing on
	#: every row says nothing, applied to a fact rather than a column.
	#:
	#: ``status_is_default``'s counterpart, and it was the missing half: the terminal decided by
	#: hardcoding ``("task", "note")``, which are the keys *this installation's seeder* happens
	#: to use. That is latent until `#1129` lets a workspace rename ``task``, at which point a
	#: workspace whose default is ``story`` prints `story` on every line.
	type_is_default: bool = False
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

	def columns (self, reader: str | None) -> tuple[str, ...]:
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
	"""The sections of a day, and what they were computed against.

	``date`` and ``timezone`` are both reported because "today" is not a fact about the
	server (docs/design.md §6.5) — and a client merging several instances resolves the date *once*,
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

	#: What is waiting on a person — status ``needs_input`` (`#1116`). First in
	#: :data:`AGENDA_BUCKETS`, because it is the only one that is not work the reader could do.
	#:
	#: **Defaulted, so a client can read an instance that predates it** (`#345`, `#482`), for
	#: the reason ``in_progress`` below gives.
	waiting: list[Task] = pydantic.Field(default_factory=list)

	#: What is already started — status category ``in_progress`` (`#853`). An agent could not
	#: see its own half-finished work from a listing at all (`#841`), and a person reading an
	#: agenda could not tell what they had picked up from what they had not.
	#:
	#: **Defaulted, so a client can read an instance that predates it** (`#345`, `#482`): a
	#: required field here makes a newer client refuse an older instance outright rather than
	#: read the rest of what it said.
	in_progress: list[Task] = pydantic.Field(default_factory=list)

	#: What is happening to you today rather than being done by you — the ``occasion`` type
	#: category (decision `#1235` §4). A birthday, a booked fortnight, a code freeze: things
	#: nobody can be offered as work, which is why they are not in ``today``.
	#:
	#: **Defaulted, so a client can read an instance that predates it** (`#345`, `#482`), for
	#: the reason ``in_progress`` above gives.
	occasions: list[Task] = pydantic.Field(default_factory=list)

	#: How many unscheduled tasks there are in total, which is usually more than are listed:
	#: an agenda that dumped a 400-item backlog would not be an agenda.
	unscheduled_total: int

	#: How many *dated* tasks this agenda does not show, because they fall further out than
	#: the look-ahead (`#997`). The window has an edge and every surface has the same one, so
	#: a deadline three weeks away is in no bucket at all — this is what says so, and it is
	#: :attr:`unscheduled_total`'s job for the other pile.
	#:
	#: **Defaulted, like everything added to this model after it shipped** (`#345`, `#482`):
	#: an instance one release behind sends no such key and must keep working.
	later_total: int = 0

	#: How much work this agenda holds back because somebody deferred it past the end of the
	#: day being shown (§6.5).
	#:
	#: **Defaulted, so a client can read an instance that predates it** (`#345`, `#482`), for
	#: the reason ``in_progress`` above gives — a required field here makes a newer client refuse
	#: an older instance outright rather than read the rest of what it said.
	deferred_total: int = 0

	#: How much undated work is in a project nobody is running. Putting a project down says
	#: something about *what to work on*, so its undated work leaves the unscheduled bucket while
	#: anything dated stays on the agenda.
	#:
	#: **Defaulted for the reason above.**
	paused_total: int = 0

	#: How many occasions this agenda leaves out because they have already happened (decision
	#: `#1235` §3). A listing at the same scope still shows them — a passed event is not
	#: *completed* — so the difference between the two is said rather than left to be noticed.
	#:
	#: **Defaulted for the reason above.**
	passed_total: int = 0


#: The agenda's buckets, in the order a day is read (docs/design.md §8.6).
#:
#: **Here rather than once per surface** (`#992`, and `#913`'s move for the same reason). Three
#: surfaces walk these in order — the terminal's sections, the browser's `BUCKETS` and an
#: agent's flat list — and until this existed each carried its own copy, so the order and the
#: membership were free to disagree. They did: `in_progress` reached the dataclass, the CLI and
#: the browser and never reached an agent at all.
#:
#: **The keys, and deliberately not the labels.** What each surface *calls* a bucket differs
#: for good reasons — the terminal says `Next 7 days` where the look-ahead is seven, an agent
#: is handed the bare key because it is parsing rather than reading — and collapsing that into
#: one string would make a rendering decision on behalf of surfaces that have already made it.
AGENDA_BUCKETS: tuple[str, ...] = (
	# **First, and it is Simon's decision of 2026-08-25** (`#1243`): *"I would naturally
	# complete a task before starting another."* Work already in hand is the first thing to
	# look at, because everything below it is a candidate to *begin* and this is the only
	# section that is not.
	#
	# **It outranks `overdue` as well, and that is the part with a consequence.** The buckets
	# are disjoint in order, so a started task with a passed deadline is reported here rather
	# than under *Overdue* — which is right (you are already on it) and which means the late
	# marking cannot come from the section. Both surfaces mark the row instead; the browser
	# always did.
	"in_progress",
	"waiting",
	"overdue",
	# **Above the day's own work, and below what is late** (decision `#1235` §4). Everything
	# around it is work; this is what is happening *to* the reader, and a code freeze or a
	# fortnight off is the context the rest of the page is read in — so it goes before *Today*
	# and after the things that are already owed.
	#
	# **The same position the buckets are computed in**, which is not a tidiness: `agenda.build`
	# takes an occasion's rows before `today` can, and `#1244` is what it costs when this list
	# and that one disagree.
	"occasions",
	"today",
	"upcoming",
	"unscheduled",
)


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


#: What a rendering calls the row a repeat is stored on, as opposed to one of its occurrences.
#:
#: **Here for `BLOCKED_MARK`'s reason**, and it earned that placement immediately: `#921` made a
#: template's ref resolve on every surface, and a series and its occurrence carry the *same
#: title* — so ``show 1`` and ``show 2`` rendered identically and nothing told them apart. Two
#: surfaces inventing their own sentence for that is what `#674` and `#922` were built to catch.
#:
#: **``is_template``'s own comment already promised this** — "the only thing that explains why a
#: row with a ref appears in no listing" — while no rendering read the field. A published field
#: whose stated job is explaining something to a reader, and no reader ever saw it.
#:
#: **No new vocabulary, which is deliberate.** The product says *repeat* everywhere a person
#: meets this — ``--repeat``, ``repeats``, the browser's **Repeats** disclosure — so *series*
#: would be a word somebody has to learn to read one line. *itself* is what separates the rule
#: from the thing it produces, and it needs no glossary.
THE_SERIES = "the repeat itself"

#: What an *occurrence* says about the row that outlives it — `#1247`.
#:
#: **The number was reachable and named nowhere.** ``show`` on the template works and says
#: :data:`THE_SERIES`; nothing anywhere printed which number that was, so the only way to reach
#: the row that persists — the one a reminder has to be set on, and the one a rename has to
#: reach — was to guess an integer.
#:
#: Same vocabulary as its counterpart for the same reason: *repeat* is the word the product uses
#: everywhere a person meets this, and *from* says which way round the two rows stand without
#: asking anybody to learn what a template is.
FROM_THE_REPEAT = "from repeat"


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

	**The clock is read here, once, and deliberately not passed in.** Two of the loads below
	mark rows *blocked* and *blocker*, and what counts as a live blocker became a question about
	the clock when an occasion learned to be over with nobody saying so (decision `#1235` §3).
	The rest of this codebase threads ``now`` from the request, and that is right where the
	caller has one — :func:`subroutine.domain.readiness.ready` and
	:func:`subroutine.domain.agenda.build` both do. **This class has thirty-five call sites and
	not one of them holds a request instant**, so a parameter here would be thirty-five copies of
	``utcnow()`` written at the call rather than one instant threaded through, which is the
	appearance of the property and not the property.

	What is really wanted is that **one page resolves against one instant**, which is what
	reading it here gives. The residue is that a listing's ``?ready=true`` filter and its own
	*Blocked* marks are resolved microseconds apart, so a freeze lapsing between them could show
	a ready row still marked blocked until the next refresh. §6.3a's warning is about a
	*cursor*, where a disagreement skips or repeats rows; nothing here paginates on it.
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
			session,
			subroutine.db.models.vocabulary.ItemType,
			type_ids,
			("key", "category", "is_default"),
		)
		self.projects = _by_id(
			session, subroutine.db.models.project.Project, project_ids, ("key",)
		)
		# **The whole address, batch-loaded like everything else here** (`#512`). A key stopped
		# naming one project with `#957`, so a row saying `dist` no longer says where its item
		# lives — and composing an address per row is `#39`'s N+1 on the one column that would
		# be on every line. This class is already the answer to "what does a page of rows need
		# that a row cannot know on its own", and an ancestor's key is exactly that.
		self.project_paths = subroutine.domain.projects.paths_for(session, set(project_ids))
		# **The colour in force for each project, resolved upwards** (`#1026`, design `#1023`).
		# A project's own, or the nearest ancestor's, or its workspace's, or none. Batch-loaded
		# for the same reason the addresses above are: it renders on every line, so a per-row
		# walk is `#39`'s N+1 on a column that is always there.
		#
		# **Resolved here rather than in each client.** A row carries `project_path`, so a
		# browser could walk it — and would then hold a copy of the inheritance rule, in three
		# surfaces. `#925`: when a client would need a copy of a rule to render a field, publish
		# the rendering instead.
		#
		# **Both settings from one walk** (`#1072`). This asked twice, each call running three
		# queries — six per page, on every task, document and agenda listing — beneath a
		# comment saying the walk was *"shared with the colour rather than repeated"*. It was
		# not, and the sentence is what stopped anybody counting. `hidden_statuses` is read
		# only by a page of projects; it is loaded here because now the walk really is shared.
		in_force = subroutine.domain.settings.several_for_projects(
			session,
			[subroutine.domain.settings.COLOUR, subroutine.domain.settings.HIDDEN_STATUSES],
			set(project_ids),
		)
		self.project_colours = in_force[subroutine.domain.settings.COLOUR.key]
		self.hidden_statuses = in_force[subroutine.domain.settings.HIDDEN_STATUSES.key]
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
		# **One instant for both marks and for every row on the page** — see the class
		# docstring for why it is read here rather than passed.
		now = subroutine.db.types.utcnow()

		self.blocked = subroutine.domain.readiness.blocked_among(session, wanted, now=now)
		# The mirror, one `EXISTS` scan the same way (`#569`). Two queries rather than one
		# because they are opposite directions over the same edges, and both return
		# immediately on an empty page.
		self.blocking = subroutine.domain.readiness.blocking_among(session, wanted, now=now)

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
		"""Load everything a page of projects needs to be rendered.

		``project_ids`` is the page itself, because a project's own address is composed from
		its ancestors' keys exactly as a task's is (`#512`) — the ids differ, the question
		does not.
		"""

		return cls(
			session,
			status_ids={project.status_id for project in projects},
			project_ids={project.id for project in projects},
		)

	@classmethod
	def for_link_ends (
		cls,
		session: sqlalchemy.orm.Session,
		ends: typing.Sequence[subroutine.domain.links.End],
	) -> "Vocabulary":
		"""Load everything the far ends of a set of links need to be rendered — `#970`.

		**One vocabulary across both kinds, rather than one per kind.** A ref names a task or
		a document (§6.2) and an item's links routinely hold both — a decision document
		blocking a task is ordinary here — so asking twice would be two rounds of the same
		queries against a set that is bounded anyway (§5.7: an item's links are bounded by how
		many somebody typed).

		The ids are gathered exactly as :meth:`for_tasks` and :meth:`for_documents` gather
		them, and the reasons for each are on those two rather than repeated here.
		"""

		rows = [end.row for end in ends if end.row is not None]
		tasks = [row for row in rows if isinstance(row, subroutine.db.models.work.Task)]
		documents = [
			row for row in rows if isinstance(row, subroutine.db.models.work.Document)
		]

		return cls(
			session,
			status_ids={row.status_id for row in rows},
			type_ids={row.type_id for row in rows},
			project_ids={row.project_id for row in rows},
			task_ids={row.id for row in tasks},
			document_ids={row.id for row in documents},
			parent_ids={row.parent_task_id for row in tasks if row.parent_task_id}
			| {
				row.recurrence_template_id
				for row in tasks
				if row.recurrence_template_id
			},
			user_ids=(
				{row.assignee_id for row in tasks if row.assignee_id}
				| {row.claimed_by_id for row in tasks if row.claimed_by_id}
			),
		)



def _prose_bytes (text: str | None) -> int:
	"""Return how many bytes this item's prose takes, as a caller will receive it — `#595`.

	UTF-8 rather than characters, because that is what crosses the wire and what a token budget
	is spent on. The two disagree on the punctuation this project writes in — an em dash is
	three bytes and one character — so a character count would understate exactly the documents
	worth warning about.
	"""

	return 0 if text is None else len(text.encode("utf-8"))


def repeats (task: Task) -> bool:
	"""Say whether a rendered task is one of a series, from either end of it.

	**The client-side half of `tasks.repeats`**, which asks the same question of a row. A
	client holds a view and never a model, so a surface deciding whether to put decision
	`#1249`'s question to somebody has nothing else to read.

	Two copies of one rule is this codebase's signature defect, and this pair is deliberate
	and loud rather than silent: getting it wrong in either direction ends in a refusal from
	the domain — *that repeats, say which occurrences* if this is too narrow, *that does not
	repeat* if it is too wide. ``tests/test_recurring_tasks.py`` drives both against one row
	so they cannot drift quietly.
	"""

	return task.is_template or task.recurrence_template_ref is not None


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
		project_path=vocabulary.project_paths.get(row.project_id, ""),
		project_colour=vocabulary.project_colours.get(row.project_id),
		parent_task_id=row.parent_task_id,
		parent_ref=_parent_field(vocabulary, row.parent_task_id, "ref"),
		parent_title=_parent_field(vocabulary, row.parent_task_id, "title"),
		status=str(status.get("key", "")),
		status_category=str(status.get("category", "")),
		status_is_default=bool(status.get("is_default", False)),
		status_id=row.status_id,
		type=str(vocabulary.types.get(row.type_id, {}).get("key", "")),
		type_category=str(vocabulary.types.get(row.type_id, {}).get("category", "")),
		type_is_default=bool(vocabulary.types.get(row.type_id, {}).get("is_default", False)),
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
		ends_at=row.ends_at,
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
		# **Derived from the two fields above rather than beside them** (`#925`), so it cannot
		# describe a rule the same response is not reporting — and so a stopped series, which
		# resolves neither, is silent here too rather than announcing a rule that will never
		# fire again.
		recurrence_description=_described_repeat(vocabulary, row),
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
		reminder_minutes=row.reminder_minutes,
		reminder_human=(
			None
			if row.reminder_minutes is None
			else subroutine.domain.durations.humanize(row.reminder_minutes)
		),
		tags=vocabulary.tags.get(row.id, []),
		completed_at=row.completed_at,
		is_complete=row.completed_at is not None,
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
		project_path=vocabulary.project_paths.get(row.project_id, ""),
		project_colour=vocabulary.project_colours.get(row.project_id),
		parent_id=row.parent_id,
		status=str(status.get("key", "")),
		status_category=str(status.get("category", "")),
		status_is_default=bool(status.get("is_default", False)),
		status_id=row.status_id,
		type=str(vocabulary.types.get(row.type_id, {}).get("key", "")),
		type_category=str(vocabulary.types.get(row.type_id, {}).get("category", "")),
		type_is_default=bool(vocabulary.types.get(row.type_id, {}).get("is_default", False)),
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


def comment (
	row: subroutine.db.models.activity.Comment, vocabulary: Vocabulary
) -> Comment:
	"""Render one comment.

	**The vocabulary is required rather than defaulted**, unlike :func:`event`'s ``described``.
	That one is genuinely optional — an event's description is expensive and several callers
	have nothing useful to say — where an author is one name every reader of a comment wants.
	A defaulted argument here would let a call site forget and answer ``null`` silently, which
	is `#640`'s shape: the rule right, the display right, and no wire between them.
	"""

	return Comment(
		id=row.id,
		body=row.body,
		entity_type=row.entity_type,
		entity_id=row.entity_id,
		workspace_id=row.workspace_id,
		author_id=row.author_id,
		author=_username(vocabulary, row.author_id),
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



def edge (found: subroutine.domain.links.Edge, vocabulary: Vocabulary) -> Edge:
	"""Render one link as a stored fact rather than as somebody's view of it."""

	return Edge(
		id=found.id,
		link_type=found.link_type,
		label=found.label,
		source=_end(found.source, vocabulary),
		target=_end(found.target, vocabulary),
	)


#: The one field a link's end knows that the item it points at does not: which table the ref is
#: in. Everything else on :class:`LinkEnd` is a field of the item, which is what lets the rest
#: be projected rather than assembled.
#:
#: **``is_complete`` was here too until `#1281`**, on the grounds that "finished" was a link's
#: own question (`#210`). It stopped being one when a task learned to answer it: the two
#: derivations read the same row and the same column, and a document takes the declared default
#: either way. Keeping it would have left the one fact this arc is about — *is it over* —
#: computed in two places on one page.
_ENDS_OWN_FIELDS = ("entity_type",)


def _end (end: subroutine.domain.links.End, vocabulary: Vocabulary) -> LinkEnd:
	"""Render one end of a link, as a projection of that item's own rendering — `#970`.

	**Projected rather than assembled, and that is the whole design.** Every field here except
	the two above is a field of :class:`Task` or :class:`Document`, so taking them off the
	rendering makes a link line's facts *the same facts as a row's* by construction — one
	status lookup, one project address, one username resolution, one description of a repeat.
	Assembling them here would have been a second reading of six things, which is this
	codebase's signature defect and is exactly how the four renderings of a link line came to
	disagree in the first place (`#583`, `#674`).

	**Adding a field to :class:`LinkEnd` needs no change here**, which is the property worth
	having: the guard in ``tests/test_web.py`` says which fields the browser's ``marks`` reads,
	and declaring one is all it takes to carry it.

	**A document takes the declared default for anything only a task has.** A deadline, a
	deferral, a lease and a repeat are task-only, and a document is not silent about them
	because nobody loaded them — it is silent because it cannot have them.
	"""

	if end.row is None:
		return LinkEnd(entity_type=end.entity_type, id=end.id, ref=end.ref, title=end.title)

	rendered: Task | Document = (
		task(end.row, vocabulary)
		if isinstance(end.row, subroutine.db.models.work.Task)
		else document(end.row, vocabulary)
	)

	return LinkEnd(
		entity_type=end.entity_type,
		**{
			name: getattr(rendered, name, field.default)
			for name, field in LinkEnd.model_fields.items()
			if name not in _ENDS_OWN_FIELDS
		},
	)


def link (related: subroutine.domain.links.Related, vocabulary: Vocabulary) -> Link:
	"""Render one link, from the point of view of the item it was asked about.

	Takes the domain's own :class:`~subroutine.domain.links.Related` rather than the stored
	row, because working out which end is "the other one" and which way round the label reads
	is the domain's job and is already done by the time this is called.
	"""

	return Link(
		id=related.id,
		link_type=related.link_type,
		link_category=related.link_category,
		label=related.label,
		direction=related.direction,
		other=_end(related.other, vocabulary),
	)


def links (
	session: sqlalchemy.orm.Session,
	related: typing.Sequence[subroutine.domain.links.Related],
) -> list[Link]:
	"""Render one item's links, with a single vocabulary across every end they reach.

	**Plural because the vocabulary is**, which is the whole reason this exists rather than a
	comprehension at each call site: a link's end now carries a status, a type, a project
	address and two usernames, and resolving those per link would be `#39`'s N+1 arriving on
	the one surface built to save a reader from opening five items.
	"""

	vocabulary = Vocabulary.for_link_ends(session, [one.other for one in related])

	return [link(one, vocabulary) for one in related]


def verification (
	row: subroutine.db.models.work.Verification, *, ref: int, recorded_by: str | None
) -> Verification:
	"""Render one record of what was checked."""

	return Verification(
		id=row.id,
		task_ref=ref,
		passed=row.passed,
		summary=row.summary,
		output_excerpt=row.output_excerpt,
		ran_at=row.ran_at,
		tree_hash=row.tree_hash,
		commit_sha=row.commit_sha,
		recorded_by=recorded_by,
		created_at=row.created_at,
	)


def governing (
	session: sqlalchemy.orm.Session,
	found: typing.Sequence[subroutine.domain.links.Governs],
) -> list[Governing]:
	"""Render what governs one item, with a single vocabulary across every document."""

	vocabulary = Vocabulary.for_link_ends(session, [one.document for one in found])

	return [
		Governing(link_type=one.link_type, document=_end(one.document, vocabulary))
		for one in found
	]


def proposal (
	proposed: subroutine.domain.links.Proposed, vocabulary: Vocabulary
) -> Proposal:
	"""Render one proposed link, from the point of view of the item it was asked about."""

	return Proposal(
		link_type=proposed.link_type,
		label=proposed.label,
		direction=proposed.direction,
		other=_end(proposed.other, vocabulary),
		because=proposed.because,
	)


def proposals (
	session: sqlalchemy.orm.Session,
	found: typing.Sequence[subroutine.domain.links.Proposed],
) -> list[Proposal]:
	"""Render one item's proposed links, with a single vocabulary across every end."""

	vocabulary = Vocabulary.for_link_ends(session, [one.other for one in found])

	return [proposal(one, vocabulary) for one in found]


def edges (
	session: sqlalchemy.orm.Session,
	found: typing.Sequence[subroutine.domain.links.Edge],
) -> list[Edge]:
	"""Render a page's links, with a single vocabulary across every end they reach.

	``?include=links`` exists to remove an N+1 from the caller (§8.4), so one here would be a
	joke at their expense — which is the comment the two listings that call this already
	carry about the query behind it.
	"""

	vocabulary = Vocabulary.for_link_ends(
		session, [end for one in found for end in (one.source, one.target)]
	)

	return [edge(one, vocabulary) for one in found]


def workspace (
	row: subroutine.db.models.identity.Workspace, *, prioritised: str | None
) -> Workspace:
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
		prioritised_project=prioritised,
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
		path=vocabulary.project_paths.get(row.id, ""),
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
		hidden_statuses=list(vocabulary.hidden_statuses.get(row.id, ())),
		archived_at=row.archived_at,
		deleted_at=row.deleted_at,
		created_at=row.created_at,
		updated_at=row.updated_at,
		version=row.version,
	)


#: How each kind of event reads, by the **entity** it is about (`#1115`).
#:
#: **The entity decides, not the subject**, and reading it the other way was a defect on every
#: surface at once. Since `#52` a comment's event names the comment and carries the
#: commented-on item as its ``subject`` — so ``subject_type is not None`` looks like a comment
#: marker and is not: ``domain.links`` sets it too, deliberately, so a link event can name the
#: far end and be scoped by it. The predicate means *this event is about something other than
#: the item*, and both readers had it as *this event is a comment*.
#:
#: **Both copies agreed, which is why nothing caught it.** This codebase's signature defect is
#: two copies that disagree; here they were byte-identical and both wrong, so every
#: cross-surface comparison passed. Consistency mistaken for correctness is the one shape a
#: transport-equivalence suite structurally cannot see — hence one function rather than two
#: corrected copies.
_HAPPENED: dict[tuple[str, str], str] = {
	("comment", "created"): "commented",
	("comment", "updated"): "edited a comment",
	("comment", "deleted"): "deleted a comment",
	("link", "created"): "linked it to something",
	("link", "deleted"): "unlinked it from something",
}


#: What a changed column is called by the person whose task it is (`#1187`).
#:
#: **Moved here from `cli/personal` on 2026-08-24, where it had served one surface since it was
#: written.** The terminal built it to satisfy §13.5b — a status change would otherwise have
#: printed one of the seven words that path never uses — so readable column names were a *side
#: effect* rather than the goal, and nobody reading the agent surface had any reason to think a
#: solution already existed one module away. An agent was told ``changed status_id``, which names
#: a column nothing else on that surface mentions, while the terminal said *how it is going*.
#:
#: Several columns collapse to one phrase deliberately: a date and its all-day flag are one fact
#: to a reader and always move together, so listing both says the same thing twice.
_A_CHANGE_TO = {
	"assignee_id": "who has it",
	"assigned_by_id": "who has it",
	"claimed_by_id": "who is holding it",
	"claim_expires_at": "who is holding it",
	"claimed_at": "who is holding it",
	"completed_at": "whether it is done",
	"due_at": "the deadline",
	"due_is_all_day": "the deadline",
	"estimate_minutes": "how long it takes",
	"importance": "how it is ranked",
	"urgency": "how it is ranked",
	"parent_task_id": "what it is part of",
	"project_id": "where it is filed",
	"recurrence_anchor": "how it repeats",
	"recurrence_rule": "how it repeats",
	"recurrence_text": "how it repeats",
	"recurrence_trigger": "how it repeats",
	"snoozed_until": "when it comes back",
	"snoozed_is_all_day": "when it comes back",
	"spent_minutes": "time spent",
	"starts_at": "when it starts",
	"starts_is_all_day": "when it starts",
	"ends_at": "when it is over",
	"status_id": "how it is going",
	"type_id": "what kind it is",
	"owner_id": "whose it is",
	"supersedes_id": "what it replaces",
	"timezone": "its timezone",
	# Never moves on an item — §5.4 refuses a cross-workspace move outright — and it is here
	# because the guard beside this asks every column rather than the ones that have moved so
	# far. A phrase for something that cannot happen costs a line; a leak costs the rule.
	"workspace_id": "which list it is in",
}


def field_in_words (name: str) -> str:
	"""Return what a person calls the thing that changed.

	The internal suffixes come off anything unmapped — ``_id`` names a row nobody can see and
	``_at`` says nothing a reader needs — so a column added tomorrow reads as words rather than
	as a schema. ``title`` and ``description`` are already what they are called, which is why
	most fields are not in the table above.

	Public because three surfaces render an event and none of them may answer this its own way.
	"""

	if name in _A_CHANGE_TO:
		return _A_CHANGE_TO[name]

	for suffix in ("_is_all_day", "_id", "_at"):
		name = name.removesuffix(suffix)

	return name.replace("_", " ")


def _a_link (event: Event) -> str | None:
	"""Return a link event as the relation and both of its ends — ``#15 documents #7``.

	**Both ends rather than the far one**, because the two readers of this disagree about what
	is already on the page: the change feed prints the subject beside this phrase and an item's
	history does not, so naming only the other end says nothing at all in a history. Naming both
	also removes the direction question without needing the inverse label, which the event does
	not carry.

	**It degrades rather than assuming its own payload** (`#302`). The refs come out of
	``changes``, which is exactly what that item may take away — it weighs omitting the far end
	so a reader who cannot see it is told nothing about it. If either end goes, this falls back
	to the wording it replaced, which is then the honest answer rather than a broken one.

	The relation is named by the **key the event stored**, not by today's vocabulary: a workspace
	that renames a relation has not changed what somebody did last week.
	"""

	changes = event.changes or {}
	relation = (changes.get("link_type") or {}).get("to")
	source = (changes.get("source") or {}).get("to")
	target = (changes.get("target") or {}).get("to")

	if relation is None or source is None or target is None:
		return None

	verb = "linked" if event.action == "created" else "unlinked"

	return f"{verb} #{source} {relation} #{target}"


def happened (event: Event) -> str:
	"""Return one event as a phrase somebody can read, in one place for every surface.

	**The field names, not the values**, for an ordinary edit. A history is a list of what
	moved; the values are in the item itself, and a ``from``/``to`` pair per field would make
	the commonest entry the longest one. **The names are the reader's, not the database's** —
	see :func:`field_in_words`.
	"""

	if event.entity_type == "link":
		joined = _a_link(event)

		if joined is not None:
			return joined

	said = _HAPPENED.get((event.entity_type, event.action))

	if said is not None:
		return said

	# A kind this does not name yet, about something other than the item itself. Said as what
	# it is rather than guessed at: an unnamed pair reading as *commented* is the defect this
	# function exists to remove, and reading as the bare action at least cannot mislead.
	if event.subject_type is not None and event.entity_type != event.subject_type:
		return f"{event.action} a {event.entity_type}"

	if event.action != "updated" or not event.changes:
		return event.action

	# **A set, because the map collapses pairs.** A defer moves `snoozed_until` and
	# `snoozed_is_all_day` together and they are one fact to a reader.
	return "changed " + ", ".join(sorted({field_in_words(name) for name in event.changes}))


def agenda (
	session: sqlalchemy.orm.Session, built: subroutine.domain.agenda.Agenda
) -> Agenda:
	"""Render a built agenda, loading the vocabulary for every bucket at once.

	One :class:`Vocabulary` across the whole thing rather than one per bucket: the same
	three statuses turn up in every bucket, and six loads would be five too many.

	**The buckets are walked from :data:`AGENDA_BUCKETS` rather than listed here** (`#1236`).
	They were listed, twice in this function — once to gather the rows and once to render them —
	so a bucket added to the dataclass and to the model reached an agent as an empty list, in
	silence, exactly as ``in_progress`` did before `#992` gave the order one home. This is that
	item's argument arriving at the last surface that had its own copy.
	"""

	everything = [row for bucket in AGENDA_BUCKETS for row in getattr(built, bucket)]
	vocabulary = Vocabulary.for_tasks(session, everything)
	rendered: dict[str, typing.Any] = {
		bucket: [task(row, vocabulary) for row in getattr(built, bucket)]
		for bucket in AGENDA_BUCKETS
	}

	return Agenda(
		date=built.date,
		timezone=built.timezone,
		unscheduled_total=built.unscheduled_total,
		later_total=built.later_total,
		deferred_total=built.deferred_total,
		paused_total=built.paused_total,
		passed_total=built.passed_total,
		**rendered,
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
		last_login_at=row.last_login_at,
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

	reachable = list(subroutine.domain.workspaces.readable(session, principal))

	# **One lookup for every workspace at once** (`#986`). This is the answer a client builds its
	# whole picture from — the CLI's `World`, the browser's masthead — so asking per workspace
	# would be `#39`'s N+1 on the first call of every session.
	focused = subroutine.domain.projects.prioritised_addresses(
		session, principal, workspace_ids=[workspace.id for workspace in reachable]
	)

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
		reader_timezone=reader_zone(session, principal),
		workspaces=[
			workspace_access(
				session, principal, workspace, prioritised=focused.get(workspace.id)
			)
			for workspace in reachable
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
	*,
	prioritised: str | None,
) -> WorkspaceAccess:
	"""Describe what one caller may do in one workspace."""

	grant = subroutine.domain.authorization.explain(session, principal, row.id)

	return WorkspaceAccess(
		id=row.id,
		slug=row.slug,
		title=row.title,
		timezone=row.timezone,
		# **The raw level above, and §6.5 already resolved below it.** ``timezone`` is what this
		# workspace itself says, null where it says nothing; ``reader_timezone`` is the answer
		# — what a day *this caller types here* means (`#1083`, decision `#1088`). Both are
		# published because they answer different questions, and a client that only had the
		# parts would have to hold a copy of the chain to get from one to the other.
		reader_timezone=reader_zone(session, principal, workspace=row),
		prioritised_project=prioritised,
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


def _same_clock (one: str, other: str) -> bool:
	"""Report whether two zone names keep the same time as each other all year — `#1089`.

	**Names are compared through their offsets rather than as strings**, because two spellings
	of one zone are ordinary: this machine reports ``Etc/UTC`` where ``init`` records ``UTC``,
	and 466 of the 521 tasks on the instance this project runs on carry the first against 7
	carrying the second. A string comparison told everybody their machine disagreed with their
	account, which is the state a line like this exists to be *quiet* about.

	**Sampled across a year rather than at this instant**, which is the difference that matters:
	``Europe/London`` and ``UTC`` keep the same clock every winter and part company every
	summer, so a check made in January would go silent about a real divergence until March.

	Two genuinely distinct zones that never differ — ``Europe/London`` and ``Europe/Dublin`` —
	are treated as the same, and that is correct rather than a compromise: a day resolved in
	either lands on the same date, so there is nothing for the reader to act on.

	An unknown name cannot be compared and is reported as a difference, which is the safe
	direction: saying so lets somebody fix a zone this program cannot read.
	"""

	try:
		here = subroutine.domain.dates.zone(one)
		there = subroutine.domain.dates.zone(other)

	except subroutine.errors.SubroutineError:
		return False

	at = subroutine.db.types.utcnow()

	# **The two `at`s are different variables and this is correct.** The inner generator is
	# built in *this* scope, so its `at` is the one above; the outer comprehension binds its
	# own, which shadows it only inside itself. Written down because a reader meets it as a
	# variable used in its own definition — a cold review flagged it, checked it against seven
	# zone pairs, and said plainly that the next reviewer would flag it again.
	return all(
		at.astimezone(here).utcoffset() == at.astimezone(there).utcoffset()
		for at in (at + datetime.timedelta(days=days) for days in (0, 91, 182, 273))
	)


def zones (me: Me, *, machine: str | None) -> list[str]:
	"""Say which zone this account's days are read in, and when the machine differs — `#1089`.

	Decision `#1088` makes the **account's** zone the authority for everything: resolving a day
	somebody typed, bucketing an agenda, rendering a moment. The machine's zone is used for one
	thing only, seeding the first account at ``init``.

	**That rule has exactly one failure mode and it is silent.** Somebody who moves country, or
	whose account was made by ``user create`` rather than ``init``, has their days resolved
	somewhere they are not, and nothing says so. They meet it as *the dates are slightly wrong*
	with no thread to pull — which is the shape `#381` built the version lines for, one fact
	along.

	**A plain statement rather than a warning**, per `#1088` §8. Working from a laptop in
	another country is not a mistake, and a warning that fires on an ordinary state is one
	people learn to skip past. This says the two zones and leaves the judgement.

	``machine`` is ``None`` where the caller's own machine is not visible from here, which is
	the MCP surface: since `#539` those tools run *inside* the instance, so reading the process
	zone there would compare an account against the **server** and call it the caller's. That
	is `#564`'s mistake exactly — a three-way check that was inert in the direction that
	reassures — so this says nothing rather than something it cannot know.

	**Silent when they agree**, which is the ordinary case, and silent when the instance is a
	release behind and publishes no resolved zone: the second is *did not say* rather than
	*they match*, and a line either way would be a claim this cannot support.

	**Agreement is per workspace, and it was not** (`#1172`). This asked whether *any* named
	zone matched the machine and went silent if one did — so somebody in London with a London
	workspace and a New York one was told nothing about New York, and they are precisely the
	reader this line exists for. Only the zones that actually differ are named now.
	"""

	said = sorted({
		workspace.reader_timezone
		for workspace in me.workspaces
		if workspace.reader_timezone is not None
	})

	if not said or machine is None:
		return []

	# Named rather than counted: with more than one the reader needs to know which workspace
	# is which, and that is what the block above this already prints. A zone that agrees with
	# the machine is left out rather than listed — it is not what the line is about, and naming
	# it invites the reader to look for a difference that is not there.
	differing = [name for name in said if not _same_clock(name, machine)]

	if not differing:
		return []

	return [
		f"Your days are read in {', '.join(differing)}; this machine is set to {machine}.",
		f"Set your account's zone with 'subroutine user timezone {machine}' if that is wrong.",
	]


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


class CalendarNames(typing.NamedTuple):
	"""What a page of calendar feeds shares: project addresses and item-type keys, by id."""

	projects: dict[str, str]
	types: dict[uuid.UUID, str]


def _named_for (
	rows: typing.Sequence[subroutine.db.models.identity.CalendarFeed],
	session: sqlalchemy.orm.Session | None,
	principal: subroutine.domain.authentication.Principal | None,
) -> CalendarNames:
	"""Resolve every project and item type this page names, in two queries rather than 2N.

	**Batch-loaded like every other listing here** (`#1080`). This was done per row, one query
	each, which is bounded by how many feeds one person has and is therefore small — and is the
	opposite of the rule `#39` and `#856` are about. A small N is why it had not bitten, not why
	it was right, and the shape is what the next reader copies.

	``keys_for`` narrows, so a project the reader cannot see stays a UUID rather than disclosing
	its name; it answers in the order it was asked, which is what lets the pairs be zipped back.
	"""

	projects: dict[str, str] = {}
	types: dict[uuid.UUID, str] = {}

	if session is not None and principal is not None:
		wanted = sorted(
			{str(row.project_id) for row in rows if row.project_id is not None}
		)

		if wanted:
			# `strict=True`, because `keys_for` answering a different length would silently
			# pair a project with another project's address — a plausible, complete, wrong
			# answer, and the one thing a zip can do that a loop cannot.
			projects = dict(
				zip(
					wanted,
					subroutine.domain.projects.keys_for(session, principal, wanted),
					strict=True,
				)
			)

	if session is not None:
		asked = {
			uuid.UUID(one)
			for row in rows
			if row.item_type_ids is not None
			for one in row.item_type_ids
		}

		if asked:
			model = subroutine.db.models.vocabulary.ItemType
			types = {
				one.id: one.key
				for one in session.scalars(
					sqlalchemy.select(model).where(model.id.in_(asked))
				)
			}

	return CalendarNames(projects=projects, types=types)


def calendars (
	rows: typing.Sequence[subroutine.db.models.identity.CalendarFeed],
	*,
	now: datetime.datetime | None = None,
	session: sqlalchemy.orm.Session | None = None,
	principal: subroutine.domain.authentication.Principal | None = None,
) -> list[Calendar]:
	"""Render a page of calendar feeds, resolving what they share once."""

	named = _named_for(rows, session, principal)

	return [
		calendar(row, now=now, session=session, principal=principal, named=named)
		for row in rows
	]


def calendar (
	row: subroutine.db.models.identity.CalendarFeed,
	*,
	url: str | None = None,
	issued: bool = False,
	now: datetime.datetime | None = None,
	session: sqlalchemy.orm.Session | None = None,
	principal: subroutine.domain.authentication.Principal | None = None,
	named: CalendarNames | None = None,
) -> Calendar:
	"""Render one calendar feed, with its URL only where one has just been minted.

	``named`` is what a page resolved once; a caller with a single row leaves it out and this
	resolves for that row through the same function, so there is one path rather than a batched
	one and a per-row one free to disagree (`#1080`).

	``issued`` rather than ``url is not None`` decides which type comes back, and the two are
	genuinely different questions: an instance with no ``public_url`` mints a feed and can
	describe it and *cannot say where it is*, so the URL is absent on a response that must
	still be an :class:`IssuedCalendar`. Keying on the value would silently answer that case
	with the listing type, and a caller reading it would report success with nothing to
	subscribe to and no field to notice was missing.
	"""

	moment = now or subroutine.db.types.utcnow()
	found = named if named is not None else _named_for([row], session, principal)
	ids = None if row.item_type_ids is None else [uuid.UUID(one) for one in row.item_type_ids]
	fields = {
		"id": row.id,
		"title": row.title,
		"prefix": row.token_prefix,
		"workspace_id": row.workspace_id,
		"audience": row.audience,
		"project_id": row.project_id,
		# **Resolved here rather than by whoever prints it**, which is `#348`'s finding one
		# credential kind along: doing it at print time through a session is something the
		# HTTP client has not got, so the same command would report ids over a connection and
		# addresses locally. `keys_for` narrows, so a project the reader cannot see stays a
		# UUID rather than disclosing its name.
		"project_key": (
			None
			if row.project_id is None
			else found.projects.get(str(row.project_id))
		),
		"item_type_ids": ids,
		"item_types": (
			None
			if ids is None or session is None
			# **An id that names nothing is dropped rather than passed through**, which is the
			# opposite of what a project gets here. A filter naming a deleted type genuinely
			# matches nothing, so reporting the raw id would read as a type whose key we failed
			# to find — the reach is not being under-reported, it is empty.
			else [found.types[one] for one in ids if one in found.types]
		),
		"usable": row.revoked_at is None
		and (row.expires_at is None or row.expires_at > moment),
		"created_at": row.created_at,
		"last_polled_at": row.last_polled_at,
		"expires_at": row.expires_at,
		"revoked_at": row.revoked_at,
	}

	if not issued:
		return Calendar(**fields)

	return IssuedCalendar(**fields, url=url)


def member (
	row: subroutine.db.models.identity.WorkspaceMember,
	*,
	account: subroutine.db.models.identity.User,
	role: subroutine.db.models.identity.Role,
	within: subroutine.db.models.identity.Workspace,
	prioritised: str | None,
) -> Member:
	"""Render one membership, with the four things it joins already resolved.

	Handed the rows rather than fetching them, so a listing loads them once and this does not
	become §8.4's N+1 wearing a rendering hat.

	``prioritised`` is asked for even though a membership has nothing to do with a workspace's
	focus, and **the alternative was passing null here, which would have been a lie rather than
	an omission** (`#986`): a client reading ``member.workspace.prioritised_project`` would be
	told *nothing is prioritised* by a renderer that had simply not looked — a plausible,
	complete, wrong answer, which is the shape this codebase keeps finding. One lookup per
	listing is cheaper than a field that means two things depending on which response carried it.
	"""

	return Member(
		user=user(account),
		role=role.key,
		workspace=workspace_ref(within, prioritised=prioritised),
		created_at=row.created_at,
	)


def workspace_ref (
	row: subroutine.db.models.identity.Workspace,
	*,
	prioritised: str | None,
	reader_timezone: str | None = None,
) -> WorkspaceRef:
	"""Render one workspace as a client addresses it.

	``reader_timezone`` is left out where the caller has no principal to resolve it for — a
	member listing describes a workspace rather than answering *what does 'friday' mean to me*,
	and a zone reported there would be about whoever happened to be asking.
	"""

	return WorkspaceRef(
		id=row.id,
		slug=row.slug,
		title=row.title,
		reader_timezone=reader_timezone,
		prioritised_project=prioritised,
	)


def _day_cell (instant: datetime.datetime, timezone: str | None) -> str:
	"""Render a stored instant as the calendar day it was written on — `#773`, `#1090`.

	One line, and it exists to be the *only* way this module turns one of the three date
	columns into a day. ``schedule.day_in`` is the same function the terminal, the calendar
	feed and an agent's row already go through, so there is one answer to *which day is this*
	rather than five spellings that agree until one of them is edited.
	"""

	return subroutine.domain.schedule.day_in(instant, timezone).isoformat()


def moment_day (instant: datetime.datetime, timezone: str | None) -> str:
	"""Render a stored moment as the day it fell on **where the reader is** — `#1091`.

	**The mirror of :func:`_day_cell`, and decision `#1088` is why they are two functions.**
	A day is a *label*: it renders in the zone that set it and never converts, so
	:func:`_day_cell` reaches for the value's own stored zone. A moment is a *point in time*
	and has no day until somebody names a zone, so this one is handed the reader's — which is
	the account's per §6.5, never the machine's and never the server's.

	Public because both the terminal and an agent render moments as days and neither may
	answer that question its own way. Do not give ``created_at`` a stored zone to make this
	look like the other: *what day was that?* depends on who is asking rather than on who
	wrote it, and that is the whole distinction.
	"""

	return subroutine.domain.schedule.day_in(instant, timezone).isoformat()


def reader_zone (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace: subroutine.db.models.identity.Workspace | None = None,
) -> str:
	"""Return the zone this caller reads days in — §6.5 resolved for a *reader* (`#1091`).

	**The workspace step is optional because some questions are not about a workspace.** A
	credential's expiry and an instance's own history belong to the installation, so resolving
	them through a workspace would answer with whichever one happened to be in hand. Left out,
	the chain is user → instance, which is §6.5 with a step that does not apply omitted rather
	than guessed at.

	Here rather than in ``domain/schedule`` because it takes a principal, and here rather than
	written out at each caller because it had two before this existed and a third was about to
	be added — which is how one rule becomes several that agree until one is edited.
	"""

	return subroutine.domain.schedule.zone_for(
		user=principal.user,
		workspace=workspace,
		instance=subroutine.domain.instances.get(session),
	)


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


def _described_repeat (
	vocabulary: Vocabulary, row: subroutine.db.models.work.Task
) -> str | None:
	"""Return how a task repeats, as a sentence, or ``None`` when it does not.

	**Generated from the stored rule, never from the words somebody typed** — `#925`. That is
	§6.7's whole argument: *every other tuesday* coming back as *every other week, on Tuesday*
	is what tells a reader the phrase was understood the way they meant it, and echoing their
	own input back confirms nothing. Simon met the gap on the item page, where the browser said
	nothing at all and the form showed him his own string.

	Read through the same fallback the rule takes, so an occurrence describes its series and a
	stopped one describes nothing.
	"""

	rule = row.recurrence_rule or _from_a_live_series(vocabulary, row, "recurrence_rule")

	if rule is None:
		return None

	return subroutine.domain.recurrence.describe(
		rule,
		anchor=row.recurrence_anchor or _from_a_live_series(vocabulary, row, "recurrence_anchor"),
	)


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


def username_in (vocabulary: Vocabulary, user_id: uuid.UUID | None) -> str | None:
	"""Return a user's username, for a caller outside this module.

	The published name for :func:`_username`, so a router rendering something this module has
	no renderer for does not reach for a private one. Same answer, same rule.
	"""

	return _username(vocabulary, user_id)


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

	#: **What a caller addresses this by to change it** (`#826`). The key is what you *send*
	#: when filing an item; this is what `PATCH /v1/statuses/{id}` takes, and it is here
	#: because without it the curation routes are unreachable — the shape three other
	#: capabilities in this codebase have arrived in.
	#:
	#: Defaulted for `#345`'s reason: added after this model shipped, so an older instance's
	#: body must still parse.
	id: uuid.UUID | None = None


class Status(Named):
	"""A status, with the fixed category a client may branch on."""

	#: The key is renameable; the category is not. Branch on this.
	category: str


class ItemType(Named):
	"""An item type, with the fixed category a client may branch on (`#1134`).

	**A sibling of :class:`Status` rather than a field on :class:`Named`**, and for its reason:
	a link type is a ``Named`` too and has no category, so putting one on the base would publish
	a field that is empty for one of the three vocabularies — §12.2a's column that says nothing,
	one layer up.

	The category exists for exactly one branch, decision `#1133`: a client draws by key when it
	recognises the key and by category when it does not. It is not a second way of asking what a
	document binds or when it was true.
	"""

	#: The key is renameable; the category is not. Branch on this.
	#:
	#: **Defaulted, and it must be** (`#345`). This model is new but the *position* it occupies
	#: is not: ``Vocabulary.item_types`` was a list of :class:`Named` and is now a list of these,
	#: so an instance one release behind sends exactly this shape without a category. Required,
	#: it made a newer client refuse an older instance outright — measured against the served
	#: one, which answered ``item_types.document.0.category: Field required``.
	category: str = ""


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

	#: What every rule about this relation reads — decision `#1157`. Published so a client can
	#: tell a relation that holds work up from one that only says which came first, without
	#: knowing what this workspace calls either.
	#:
	#: **Defaulted for `#345`'s reason** — added after this model shipped, so an older instance's
	#: body must still parse. `#1155` is the last time that was learned by a client refusing an
	#: instance outright.
	category: str = ""
	is_symmetric: bool

	#: What a caller addresses this by to change it — see :class:`Named`. Defaulted (`#345`).
	id: uuid.UUID | None = None


class Tag(pydantic.BaseModel):
	"""A tag, and how much it is used."""

	name: str
	usage: int


class TagEntry(pydantic.BaseModel):
	"""A tag as something to *curate*, rather than as something being used — `#826`.

	**A second view of one row, deliberately.** :class:`Tag` answers *what labels are in use
	and how much*, and its usage count is narrowed to the tasks the caller can see, because a
	tag list is a small disclosure. This answers *what labels does this workspace have and what
	do they mean*, which needs an id to change one and a description to read one (`#905`), and
	needs no count at all. Folding the two together would either put a scoped aggregate on
	every write response or a meaningless zero.
	"""

	id: uuid.UUID
	name: str
	description: str | None = None


def status (row: typing.Any) -> Status:
	"""Render one status row."""

	return Status(
		id=row.id,
		key=row.key,
		label=row.label,
		category=row.category,
		is_default=row.is_default,
	)


def item_type (row: typing.Any) -> ItemType:
	"""Render one item type row."""

	return ItemType(
		id=row.id,
		key=row.key,
		label=row.label,
		category=row.category,
		is_default=row.is_default,
	)


def link_type (row: typing.Any) -> LinkType:
	"""Render one link type row."""

	return LinkType(
		id=row.id,
		key=row.key,
		title=row.title,
		inverse_title=row.inverse_title,
		category=row.category,
		is_symmetric=row.is_symmetric,
	)


def tag_entry (row: typing.Any) -> TagEntry:
	"""Render one tag as a thing to curate."""

	return TagEntry(id=row.id, name=row.name, description=row.description)


class Tags(pydantic.BaseModel):
	"""The tag list, and an honest statement of what was left out."""

	items: list[Tag]
	total: int
	truncated: bool


class Listing(pydantic.BaseModel):
	"""What one collection endpoint accepts.

	Reflected from the running application, so it cannot claim a filter that does not
	exist or omit one that does.

	**Each key is named after the parameter that consumes it**, except ``path`` and ``filters``:
	``filters`` is a list of parameter *names* rather than one parameter's values, so there is
	no single parameter to name it after. That rule arrived late — the two lists below were
	originally called ``sortable`` and ``selectable``, after what they *contain*, so a caller
	who read one and reached for ``?select=`` earned a refusal.

	**``sortable`` and ``selectable`` are deprecated and will be removed in 0.9.0.** They carry
	exactly what ``order`` and ``fields`` carry; read the new names.
	"""

	path: str
	filters: list[str]

	#: What ``?order=`` may name. Renamed from ``sortable`` by `#616`, which found the same
	#: mismatch on ``selectable`` and settled that both move together or neither does.
	order: list[str] = pydantic.Field(default_factory=list)

	#: What ``?fields=`` may name (docs/design.md §14.10). Published for the reason ``order`` is:
	#: an agent that has to discover a field name by being refused has paid for the discovery in
	#: context, which is the cost shaping exists to avoid in the first place.
	fields: list[str] = pydantic.Field(default_factory=list)

	#: What ``?format=`` accepts.
	formats: list[str]

	#: Deprecated, and the reason both new keys above are **defaulted**. The direction that bites
	#: is a new client reading an older instance: it is handed only these two, so an undefaulted
	#: ``order`` would make every 0.8.0 instance unparseable to a 0.8.1 client — which is `#345`
	#: exactly, in the opposite direction from the one `#616` was worried about.
	sortable: list[str] = pydantic.Field(default_factory=list)
	selectable: list[str] = pydantic.Field(default_factory=list)

	@pydantic.model_validator(mode="after")
	def _fill_each_name_from_its_twin (self) -> "Listing":
		"""Give a caller both spellings whichever one the instance it reached knows.

		Here rather than in the clients, following `#1168`: a fallback per consumer is the
		second copy this rename exists to repay, and the two names are one value.

		**Without this the default is a plausible wrong answer rather than a refusal.** A 0.8.1
		client reading a 0.8.0 instance is handed ``sortable`` and no ``order``; the field
		defaults to empty, so the client concludes the listing sorts by nothing — about an
		instance advertising thirteen names. It has to be a default (a required ``order`` would
		refuse every 0.8.0 instance outright), so the emptiness has to be repaired here.

		**The second half is for a server this client will never otherwise understand.** 0.9.0
		drops ``sortable`` and ``selectable``; a 0.8.1 client reading one then has the same
		silence one version later, in the half nobody is watching. It can only be written now,
		because this is the client that ships with the deprecation.

		An empty list is ambiguous between *the instance did not say* and *there is nothing to
		sort by*, and deliberately not distinguished with ``None``: both readings give the same
		answer, because a listing that genuinely offers nothing sends both keys empty and the
		fill is a no-op. Publishing ``array | null`` to buy a distinction that changes no
		outcome would be the worse contract.
		"""

		if not self.order:
			self.order = self.sortable
		elif not self.sortable:
			self.sortable = self.order

		if not self.fields:
			self.fields = self.selectable
		elif not self.selectable:
			self.selectable = self.fields

		return self


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
	#: it is published anyway (docs/design.md §2.2) because somebody using an instance ought to be
	#: able to find the source of what they are using.
	source_url: str

	#: The address this instance is served on, when a deployment has said (docs/design.md §12.4).
	#: Null on a laptop listening on loopback, which is the ordinary case and is not a gap: a
	#: client that reached this response already knows one address that works. It is here for
	#: the client that must hand out a *durable* one — a webhook target, a shared link, or the
	#: ``subroutine:`` address of an item on this instance — which is not the same as whatever
	#: host happened to be dialled.
	public_url: str | None

	workspace: uuid.UUID | None
	workspaces: list[WorkspaceRef]

	statuses: dict[str, list[Status]]
	item_types: dict[str, list[ItemType]]
	link_types: list[LinkType]
	linkable_types: list[str]
	tags: Tags

	listings: dict[str, Listing]
	grammars: dict[str, Grammar]
	limits: Limits
	error_codes: list[str]
	docs: dict[str, str]
