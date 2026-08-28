"""What every connection can be asked, whichever side of a socket it is on.

docs/design.md §13.7 makes the local database a connection like any other, so that
``subroutine agenda`` fans out across it and every configured remote through one code path
that does not know which of its answers arrived over a network. That is only true if both
transports answer the *same questions* with the *same objects*, which is what this module
declares and ``tests/test_transport_equivalence.py`` is what checks.

**Reads fan out; writes never do.** A read is issued to every connection concurrently and
merged by the caller. A write names exactly one connection, explicitly or by default, because
there is no transaction that could span two instances and no sensible way to report a
half-failure.

The granularity of each method is the granularity of the endpoint behind it, and that is not
an accident either. :meth:`Client.agenda` spans every workspace a credential reaches, because
``GET /v1/agenda`` does and because "what am I doing today" is a question about a person's
day. :meth:`Client.tasks` takes one workspace, because ``GET /v1/tasks`` refuses an ambiguous
one (§8.2) and a client that quietly spanned them locally would return different rows
depending on where the tasks were.
"""

import dataclasses
import datetime
import types
import typing

import subroutine.config
import subroutine.connections
import subroutine.domain.readiness
import subroutine.errors
import subroutine.views

#: Distinguishes "leave this alone" from "clear it" on a scheduling call, the same way §8.3
#: distinguishes an omitted field from a null one. ``None`` genuinely means "clear the
#: date", so it cannot double as "not asked for".
UNSET: typing.Any = object()

#: The verbs that only read, so a raw call knows which side of ``read_only`` it is on.
#:
#: Named here rather than in each client, because the two would come to disagree about
#: ``HEAD`` — and a disagreement in this particular list is a write escaping the one control
#: §13.7 says the far end cannot enforce for you.
READING_VERBS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Every method a raw call may present. Not a syntax rule — an allow-list, because the point
#: is that both transports answer the *same* input the same way (`#530`), and a merely
#: well-formed method they both accept still diverges: measured, ``BREW`` is a 405 from the
#: router in process and a 400 from the server over a socket.
#:
#: ``tests/test_transport_equivalence.py`` checks this against the methods the application
#: actually mounts, so a route added with a verb missing here fails the build rather than
#: becoming unreachable through ``call_api``.
CALLABLE_METHODS = READING_VERBS | frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Where this instance's API lives, and the only place a raw call may be pointed — `#557`.
#:
#: **A positive rule rather than a deny-list**, which is a deliberate choice. `#484`'s escape
#: hatch exists to reach *the API*: routes no tool covers but a credential already allows. The
#: three routes outside this prefix are not that — ``/healthz`` and ``/readyz`` are public
#: liveness, and ``/mcp`` is a transport that itself hosts this tool, so pointing it there let
#: one authenticated request nest until the instance stopped answering anybody.
#:
#: The deny-list is the wrong instrument for it: `#527` records in terms that it is not a
#: privilege boundary but a list of *consequential acts a person should be asked about*, and it
#: would name one route where the rule is about a class. ``tests/test_transport_equivalence.py``
#: checks this against the routes the application actually mounts.
API_PREFIX = "/v1"


#: What a listing method returns — the rows, and whether they are all of them.
#:
#: **A list, so nothing that already reads one has to change** (`#1037`). Every caller here
#: iterates, indexes or counts, and a subclass answers all three; what it adds is the one fact
#: the envelope always carried and no client ever passed on. Returning a wrapper object instead
#: would have made every existing call site a rewrite for a fact most of them do not want.
LISTED = typing.TypeVar("LISTED")


class Listing(list[LISTED]):
	"""Rows from one listing, and whether the instance had more of them.

	**The defect this exists for is not truncation, it is silent truncation** (`#1037`). The API
	is completely correct — it caps at ``max_page_size``, says ``has_more: true`` and hands back
	a keyset cursor — and every client threw the envelope away, so a caller asking for 500 got
	200 and had no way to tell. **Worse than a plain cut**, because the way anything here
	detects a short answer is to ask for one more than it wants: ask for 501, receive 200,
	conclude 200 < 501 and therefore that is all there is.

	``has_more`` is the instance's own answer rather than a count compared against a limit, so
	it stays right when a page is short for some other reason.
	"""

	#: Whether the instance holds rows beyond the ones here.
	has_more: bool = False

	#: Which kinds of thing this answer is *able* to include, where the instance said (`#1085`).
	#: Empty everywhere but the change feed, which narrows to the kinds a credential may read
	#: rather than refusing because of one it never asked about — so *nothing happened* and
	#: *I cannot see that* would otherwise be the same empty page.
	covers: tuple[str, ...] = ()

	def __init__ (
		self,
		rows: typing.Iterable[LISTED] = (),
		*,
		has_more: bool = False,
		covers: typing.Iterable[str] = (),
	) -> None:
		"""Hold the rows, and what the instance said about the ones past them."""

		super().__init__(rows)

		self.has_more = has_more
		self.covers = tuple(covers)


@dataclasses.dataclass(frozen=True)
class Identity:
	"""Who a connection says it is, and what it lets this credential reach.

	One call answers both because ``GET /v1/meta`` does. The instance is what settles whether
	two connections are secretly the same server; the workspaces are what resolve ``acme/42``.
	"""

	instance: subroutine.views.Instance | None
	workspaces: tuple[subroutine.views.WorkspaceRef, ...]

	def workspace (self, slug: str) -> subroutine.views.WorkspaceRef | None:
		"""Return the workspace with this short name, or ``None``."""

		wanted = slug.strip().lower()

		for candidate in self.workspaces:
			if candidate.slug == wanted:
				return candidate

		return None


@dataclasses.dataclass(frozen=True)
class Captured:
	"""A task that was made from a line of text, and what the grammar declined to read.

	``unparsed`` is §6.13's obligation rather than a nicety: text that looks like grammar and
	is not implemented stays in the title verbatim, and the user is told so. Left unsaid, a
	person who wrote "every monday" has no way to tell whether it was understood, ignored, or
	silently dropped.
	"""

	task: subroutine.views.Task
	unparsed: tuple[str, ...] = ()

	#: The sigils the grammar *did* read, written back as they were typed — `#135`. The
	#: mirror of ``unparsed`` and carried for the same reason: a caller told only what was
	#: left as written cannot tell a field that was set from one that was misread.
	summary: str | None = None


@dataclasses.dataclass(frozen=True)
class Answered:
	"""What a raw call came back with — `#485`.

	Status and body rather than a parsed model, because the whole point is reaching routes no
	method covers: there is nothing to parse it *into*. The status is carried separately so a
	caller can tell a refusal from a result without reading prose, which is the distinction an
	agent gets wrong when both arrive as text.
	"""

	status: int
	text: str


#: The arguments to :meth:`Client.update` and :meth:`Client.schedule` that a repeating item is
#: **not** asked about — decision `#1249` §1.
#:
#: **Written at this layer because this is where both callers are.** The command line puts the
#: question to a person and the MCP tool relays it from an agent, and each would otherwise have
#: had its own opinion of which fields have a second answer. The domain holds the same rule in
#: its own vocabulary (``tasks.NEVER_ASKS``); the two are kept honest by driving every argument
#: through a real edit rather than by comparing the lists, which would only prove they agree
#: about the names they share.
#:
#: A status has no second answer, the three repeat arguments live on the series and nowhere
#: else, and a timezone is not a patchable field at all — the service reads it beside a date
#: rather than storing what was sent.
NEVER_ASKS = frozenset(
	{"status", "recurrence", "recurrence_anchor", "recurrence_trigger", "timezone"}
)


class Client(typing.Protocol):
	"""One connection, ready to be asked things.

	Every implementation is a context manager, because both of them hold something that has
	to be given back — an engine on one side, a pooled socket on the other.
	"""

	connection: subroutine.connections.Connection

	def identity (self) -> Identity:
		"""Report which instance this is and which workspaces the credential reaches."""

	def reference (self, name: str) -> str:
		"""Return one of the instance's reference documents, as text — `#483`.

		``name`` is ``"agent"`` or ``"examples"``. Named rather than given a path, because a
		client's job is to know where things are and a caller's is not — and because the local
		client reaches these without HTTP at all.

		**This installation's vocabulary is :meth:`meta`**, which used to be excluded here for
		the reason given above — built inside ``api`` against a request — and is not any more
		(`#486`).

		These are what an agent over MCP has no other way to read: it holds a client, so §13.3's
		guide assumed it had already got past the problem the guide solves, and it has not.
		"""

		raise NotImplementedError

	def call_api (
		self,
		*,
		method: str,
		path: str,
		body: typing.Any | None = None,
		query: dict[str, str] | None = None,
	) -> Answered:
		"""Make one request against a route this credential already allows — `#485`.

		**It widens nothing.** The credential is the same one every other method here presents,
		so scopes, project scope and the workspace pin all still apply, and the service layer
		still runs every check it would for a named method. What it removes is the requirement
		that somebody wrote a method first — which is what decision `#484` measured as the real
		constraint: thirteen of twenty missing capabilities were excluded for tool budget rather
		than by any decision.

		``read_only`` is still enforced, and that is not automatic here: §13.7's setting is a
		*client-side* promise about an employer's instance, and a raw call that skipped it would
		be a hole in the one control the far end cannot enforce on the caller's behalf.
		"""

		raise NotImplementedError

	def meta (self, *, workspace: str | None = None) -> subroutine.views.Meta:
		"""Report what this installation calls things — `#486`.

		Statuses, item types, link types, tags, the listings each collection accepts, the small
		closed grammars, the limits and the error codes. **The keys are per workspace and
		renameable** (§5.5), so ``done`` may be called ``Shipped`` here — which is why a caller
		constructing a request against this instance cannot guess them and why §13.2 exists.

		**Ambiguity is answered rather than refused**, unlike every other read: this is often a
		caller's first call, before it knows what workspaces there are, so naming none with
		several reachable returns the workspace list and empty vocabulary sections. A name that
		matches *nothing* is still a refusal — being told something false by the one endpoint
		whose job is to prevent that is worse than being asked to choose.
		"""

		raise NotImplementedError

	def me (self) -> subroutine.views.Me:
		"""Report who this connection thinks the caller is, and what they may do — `#336`.

		**Not :meth:`identity`, and the pair is worth keeping straight.** That one asks about
		the *instance* — which installation this is, and which workspaces are reachable — and
		is what resolves ``acme/#42`` and notices one server configured twice. This asks about
		the *principal*: which account, on which credential, holding what.

		They come apart precisely where it matters. Several agents on one machine reach one
		instance through one connection, so :meth:`identity` answers the same for all of them
		while this answers differently for each — and an agent that cannot tell them apart has
		no way to check it is not writing as its operator (`#335`).

		``credential`` is null in local mode, where the filesystem permission is the
		authentication and there is nothing to describe (§12.1a). It is never null over HTTP,
		which is why a null one is a fact about the connection rather than a missing field.
		"""

	def agenda (
		self,
		*,
		date: datetime.date | str | None = None,
		timezone: str | None = None,
		horizon_days: int | None = None,
		unscheduled_limit: int | None = None,
		workspace: str | None = None,
		project: str | None = None,
	) -> subroutine.views.Agenda:
		"""Return the agenda's buckets, across every workspace this credential reaches.

		``project`` narrows further, to one project **and everything under it** (`#320`). It
		needs ``workspace`` beside it and is refused without one: a project key is per workspace
		(§5.4), so the same key can name two projects on one instance and picking one would file
		a whole agenda under whichever sorted first.

		``workspace`` narrows to one, by id or short name. Spanning everything stays the
		default, because "what am I doing today" is a question about a person's day — but one
		instance may hold a personal list *and* a project's backlog, and then the person wants
		to ask about half of it. Named as on ``tasks`` rather than ``workspace_id``, so a caller
		learns one word.

		``date`` is passed explicitly by a client merging several instances, which resolves
		"today" **once** in its own zone (§13.7). Each instance would otherwise apply its own
		notion of the caller's timezone, and a person whose work profile says
		``America/New_York`` and whose personal one says ``Europe/London`` would get two
		different days merged into one list.
		"""

	def count_tasks (
		self, *, workspace: str | None = None, project: str | None = None
	) -> int:
		"""Return how many tasks a project holds, completed ones included.

		**A count, not a page** (`#296`). ``tasks()`` returns at most a page, so a caller
		measuring a project with ``len()`` reported ``default_page_size`` and called it a total
		— which `project rename` did, in the sentence somebody reads while deciding whether to
		do something irreversible, and it was wrong in the direction that makes the operation
		look *smaller* than it is. §8.4's ``include_total`` has answered this on the endpoint
		since M1 and reached no client.

		Deliberately its own method rather than a flag on :meth:`tasks`. That would have to
		change what the listing returns for every caller in order to carry one number back, and
		the two questions — *what is in here* and *how much is in here* — are asked separately
		and by different code.
		"""

	def tasks (
		self,
		*,
		workspace: str | None = None,
		limit: int | None = None,
		include_completed: bool | None = None,
		order: str | None = None,
		project: str | None = None,
		deferred: str = subroutine.domain.readiness.DEFAULT_DEFERRAL,
		q: str | None = None,
		parent: int | None = None,
		subtree: bool = False,
		ready: bool = False,
		deleted: bool = False,
		assignee: str | None = None,
		claimed_by: str | None = None,
		status: str | None = None,
		status_category: str | None = None,
		type: str | None = None,
		due_before: datetime.datetime | None = None,
		due_after: datetime.datetime | None = None,
		filters: dict[str, str] | None = None,
	) -> Listing[subroutine.views.Task]:
		"""List one workspace's open tasks, newest first unless ``order`` says otherwise.

		``filters`` carries §9.6's date comparisons — ``{"created_at.gte": "yesterday"}``
		(`#815`). **One parameter rather than one per field per direction**, which is the same
		argument decision `#817` settled for the query string: there are seven fields and four
		operators, and naming each pair would be about twenty keyword arguments on this method
		for one kind of question. ``domain/filtering`` holds which pairs exist, so a client
		gains a new field the day the registry does.

		``assignee``, ``status``, ``type``, ``subtree``, ``due_before`` and ``due_after`` were
		declared by ``GET /v1/tasks`` and passed by nothing until `#501`. **The one that was
		costing something is ``assignee``**: it is how *"what is assigned to whom"* is asked,
		which is the question decision `#473`'s whole delegation model exists to answer, and
		until this it was reachable only by an agent that knew the filter was there and was
		holding a UUID. It takes a **username** — resolved by the service, so an unknown name is
		refused once and identically on both transports.

		``subtree`` widens ``parent`` from direct children to the whole tree, which is the shape
		a delegated piece of work takes once somebody has broken it up.

		``status_category`` narrows to one of ``todo``, ``in_progress``, ``done`` or
		``cancelled`` — the fixed field beside a status's renameable key (`#710`). It is what a
		board and a completed-work view ask with, because a filter on the *key* ``done`` breaks
		on the first installation that renames it. Naming a finished category reaches finished
		work on its own: ``include_completed`` is three-valued for that reason, and ``None``
		means the caller did not say rather than said no.

		``order`` is §8.4's spelling — comma-separated field names, a leading ``-`` to
		reverse one — and its vocabulary is ``domain.ordering.TASK_FIELDS``, shared with the
		HTTP endpoint so that both transports accept the same names and refuse the same ones.
		Until 2026-07-30 there was no ordering here at all, so every listing that went through
		a client was newest-first: that is why ``subroutine list`` could not rank a backlog
		while ``GET /v1/tasks?order=`` could.

		``project`` is a key or an id, resolved by ``domain.selection.project`` — the same
		function the endpoint uses, so ``SR`` means one project and an unknown key is refused
		with one message whichever transport asked.

		``deferred`` is one of ``domain.readiness.DEFERRAL`` and defaults to ``include``, so a
		caller that says nothing sees what it always saw. ``only`` exists so that a listing
		hiding deferred work can *say how much* it is hiding, which is the difference between
		narrowing a list and truncating one in silence.

		``q`` is §9.4's free-text match, over the title **and the description**.

		``ready`` narrows to work that can actually be started: nothing unfinished blocks it and
		it is not deferred to a future date (§6.5a). It reached the HTTP endpoint and nothing
		else until `#136`, which made the one question this tool answers that a list of tasks
		does not — *what can I start?* — the one question neither the CLI nor an agent could ask.

		**It deliberately does not read a task's own status**, so something marked "blocked" by
		hand is still returned. A `blocks` link is a tracked dependency that resolves itself; a
		hand-set status is a declaration about the world, and the two are not the same claim
		(§5.5, and the decision that there is no fifth status category).

		``parent`` narrows to one item's **direct children**, by ref. A parent this caller
		cannot see is "no such task" rather than an empty list — an empty listing would claim
		the subtree is empty, which is a different and false claim (§7.3a).
		"""

	def task (
		self, *, ref: int, workspace: str | None = None
	) -> subroutine.views.Task | None:
		"""Return one task by ref, or ``None`` if there is no such task here.

		``None`` rather than a refusal, because resolving an address across several
		connections asks this of all of them and expects most to say no. A caller wanting a
		refusal makes one, with the candidates it collected.
		"""

	def documents (
		self,
		*,
		workspace: str | None = None,
		limit: int | None = None,
		order: str | None = None,
		project: str | None = None,
		q: str | None = None,
		deleted: bool = False,
		status: str | None = None,
		status_category: str | None = None,
		type: str | None = None,
		filters: dict[str, str] | None = None,
	) -> Listing[subroutine.views.Document]:
		"""List one workspace's documents, newest first unless ``order`` says otherwise.

		``status_category`` is the one to reach for, and ``status`` the one that breaks
		(`#1087`). A key is this workspace's own and renameable; the category beside it is
		fixed, so *which documents are in force here* is a question that survives an
		installation renaming ``active`` — and `#1036` is the worked example of what happens
		when it does not, because both transports refuse an unknown key by name rather than
		answering emptily.

		``status`` and ``type`` are what make §6.14's lifecycle usable from a client (`#501`).
		A document is *draft*, then *active*, then *superseded*, and asking for this
		workspace's **active decisions** is how somebody — or something — finds the rules it is
		supposed to be working under without being told each one by name.

		The counterpart to :meth:`tasks`, and ordered the same way by default, so a caller
		showing both in one list can merge them on ``created_at`` without either side having
		sorted by something the other does not have.

		**Its vocabulary is the shorter one** — ``domain.ordering.DOCUMENT_FIELDS``, which has
		no deadline and no priority in it, because §6.14 says a document is not scheduled. A
		caller merging both kinds under a task-only ordering leaves this argument alone and
		sorts the merged result itself; what it must *not* do is ask for a page in one order
		and then sort it in another, which returns the wrong rows rather than the wrong order
		— the newest documents, cut to a limit, presented as the oldest by ref.

		**Superseded documents are included**, deliberately and for now. ``tasks`` excludes
		completed work because ``completed_at`` says so without a join; the equivalent for a
		document is a status *category*, which is a join to the vocabulary table inside the
		one helper every listing narrows through. Not worth that until something is actually
		superseded — deleted and archived are already excluded, which is the part that matters.
		"""

	def document (
		self, *, ref: int, workspace: str | None = None
	) -> subroutine.views.Document | None:
		"""Return one document by ref, or ``None`` if there is no such document here.

		The counterpart to :meth:`task`, and needed for the same reason a ref carries no
		prefix (§6.2): **one counter per workspace serves tasks and documents alike**, so
		``#4`` may perfectly well be a specification rather than a job. A reader that only
		ever asked about tasks would report that ``#4`` does not exist while it sits in the
		same listing the reader printed.
		"""

	def links (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> list[subroutine.views.Link]:
		"""Return every link touching one item, labelled from that item's point of view."""

	def beneath (
		self,
		*,
		ref: int,
		entity_type: str = "task",
		workspace: str | None = None,
		depth: int | None = None,
	) -> list[subroutine.views.Beneath]:
		"""Return what has to happen before one item can, as a walk in reading order — `#1358`.

		:meth:`links` answers one level. This walks them, so a plan of twenty-eight items and
		forty-two links is one call rather than twenty-eight — and the reviewer who measured
		that verified their plan by *reasoning* instead, which is the part worth worrying about.

		Flat, each row carrying its depth, because the shape is a graph: an item reached twice
		is drawn once and says so, and one left unwalked at the limit says so too.
		"""

	def backlinks (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> list[subroutine.views.Backlink]:
		"""Return everything whose prose refers to one item — `#144`.

		**The mention index has been written since M1 and read by nothing.** Every ``#42`` in a
		title, a description, a body or a comment writes a row, and *what refers to this?* was
		answerable on no surface — so the argument that a reason written as a comment gets its
		backlink for free was true of the data and invisible to every reader.

		A comment resolves to the item it is on and says so, because a comment has no ref.
		"""

	def governing (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> list[subroutine.views.Governing]:
		"""Return the documents in force that govern one item — `#1119`.

		What to read before starting this. The workspace-wide *what binds you* narrowed to a
		single item, answered from typed links alone, and empty until somebody has said that a
		document governs something.
		"""

	def verifications (
		self, *, ref: int, workspace: str | None = None
	) -> list[subroutine.views.Verification]:
		"""Return what has been checked against one task, newest first — `#1121`.

		**A record, not a proof.** An agent can post an exit code of zero without having run
		anything, so what this is worth is being durable, attributable and invalidatable —
		never *verified work*.
		"""

	def verify (
		self,
		*,
		ref: int,
		passed: bool,
		summary: str | None = None,
		output_excerpt: str | None = None,
		tree_hash: str | None = None,
		commit_sha: str | None = None,
		workspace: str | None = None,
	) -> subroutine.views.Verification:
		"""Record what was checked against one task.

		``tree_hash`` is what makes the record invalidatable, and ``git rev-parse HEAD^{tree}``
		prints one. Absent is a legitimate answer from a machine with no checkout: such a
		record cannot expire, which is different from being current.
		"""

	def proposed_links (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> list[subroutine.views.Proposal]:
		"""Return the documents this item's writing suggests govern it — `#1137`.

		A proposal is evidence, not an answer: somebody cited a decision in their own words,
		and that is a strong reason to think it binds this work. Confirming one is
		:meth:`link` with the ``link_type`` and the other end it names, which is why there is
		no separate verb — a confirmed proposal is an ordinary link and must be indistinguishable
		from one made by hand.
		"""

	def link (
		self,
		*,
		ref: int,
		link_type: str,
		target: int,
		entity_type: str = "task",
		target_type: str = "task",
		workspace: str | None = None,
	) -> subroutine.views.Link:
		"""Join two items — ``blocks``, ``relates_to``, ``duplicates``, ``derives_from``,
		``documents`` (§5.7).

		**The highest of `#141`'s unreached writers by some distance**, because a ``blocks``
		link is what readiness reads (§6.5a). Until this landed an agent could ask what was
		startable and could not say what blocked what — so the filter existed and nothing but
		raw HTTP could put anything into it.

		Idempotent by (source, target, type), like the service beneath it: asking twice is not
		an error, because a client retrying a request it is unsure landed should not find out
		by being refused.
		"""

	def statuses (
		self, *, workspace: str | None = None, entity_type: str | None = None
	) -> subroutine.views.Collection[subroutine.views.Status]:
		"""List this workspace's statuses, in the order a client should show them — `#826`.

		Separate from :meth:`meta`, which reports the whole vocabulary at once: this is the
		listing you page through while curating one part of it, and it carries the id the
		other three methods take.
		"""

	def create_status (
		self,
		*,
		entity_type: str,
		key: str,
		label: str,
		category: str,
		is_default: bool = False,
		position: int | None = None,
		workspace: str | None = None,
	) -> subroutine.views.Status:
		"""Add a status to this workspace's vocabulary.

		``category`` is the fixed meaning and cannot be changed afterwards; ``key`` is what a
		caller sends and may be renamed whenever.
		"""

	def update_status (
		self,
		*,
		which: str,
		key: str | None = None,
		label: str | None = None,
		is_default: bool | None = None,
		position: int | None = None,
	) -> subroutine.views.Status:
		"""Rename or reposition a status, without changing what it means."""

	def delete_status (self, *, which: str) -> None:
		"""Remove a status nothing is in, and that is not the default."""

	def link_types (self, *, workspace: str | None = None) -> subroutine.views.Collection[subroutine.views.LinkType]:
		"""List the ways two items can relate here."""

	def create_link_type (
		self,
		*,
		key: str,
		title: str,
		inverse_title: str,
		category: str,
		is_symmetric: bool = False,
		workspace: str | None = None,
	) -> subroutine.views.LinkType:
		"""Add a way two items can relate."""

	def update_link_type (
		self,
		*,
		which: str,
		key: str | None = None,
		title: str | None = None,
		inverse_title: str | None = None,
		category: str | None = None,
	) -> subroutine.views.LinkType:
		"""Rename a link type, reword either end of it, or say what it does."""

	def delete_link_type (self, *, which: str) -> None:
		"""Remove a link type nothing is joined by."""

	def tags (self, *, workspace: str | None = None) -> subroutine.views.Collection[subroutine.views.TagEntry]:
		"""List this workspace's tags as things to curate — id, name and what each means.

		**Usage counts are :meth:`meta`'s**, where they are narrowed to what this caller can
		see. A tag used only in a private project they are not a member of does not appear
		there, and recomputing that here would either duplicate the narrowing or lose it.
		"""

	def create_tag (
		self, *, name: str, description: str | None = None, workspace: str | None = None
	) -> subroutine.views.TagEntry:
		"""Declare a tag before anybody uses it, and say what it means here.

		A tag is still made by being *used* (§5.8); this is the other door.
		"""

	def update_tag (
		self, *, which: str, name: str | None = None, description: str | None = None
	) -> subroutine.views.TagEntry:
		"""Rename a tag, or write down what it means in this workspace."""

	def delete_tag (self, *, which: str) -> None:
		"""Remove a tag, and with it every application of it."""

	def unlink (
		self, *, ref: int, link_id: str, entity_type: str = "task", workspace: str | None = None
	) -> None:
		"""Withdraw a link.

		Follows :meth:`link` closely rather than waiting for somebody to ask: a link added by
		mistake blocks work that is not blocked, and readiness then hides it — so an unwanted
		link is worse than a missing one, because it narrows what looks startable and says
		nothing about having done so.
		"""

	def uncomment (
		self,
		*,
		ref: int,
		comment_id: str,
		entity_type: str = "task",
		workspace: str | None = None,
	) -> None:
		"""Withdraw a comment from an item's record — item ``#400``.

		**Deleting rather than editing, and that is a decision** (§5.10). A comment is
		attributed prose, so an administrator rewriting somebody's words under their name is
		not a permission anybody should hold; taking it out is the honest alternative and is
		the half worth reaching. Editing stays HTTP-only.

		Narrowed to the item as well as to the id, exactly as :meth:`unlink` is, so a caller
		cannot withdraw a comment from something it never named.

		Soft, like every other delete here — and the text stops mentioning anything, because a
		backlink pointing at a sentence nobody can read is worse than none.
		"""

	def comments (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> list[subroutine.views.Comment]:
		"""Return one item's record of what happened, oldest first (§5.10).

		**Oldest first, unlike every other listing here.** A record is read from the
		beginning; a task list is read newest-first because the newest is the one you act on.
		"""

	def history (
		self,
		*,
		ref: int,
		entity_type: str = "task",
		workspace: str | None = None,
		limit: int | None = None,
	) -> Listing[subroutine.views.Event]:
		"""Return what has happened to one item, newest first (§5.11a).

		**Not the same question as :meth:`comments`, which is why both exist.** A comment is
		what somebody wrote; an event is what the system recorded — a status change, a
		re-ranking, a deletion — and since ``#52`` an event also names a comment that was made,
		so the history is the one place both halves of "what happened here" appear together.

		Newest first, unlike a comment thread: a record is read from the beginning, and a
		history is read from the end, because the last thing to happen is the one you are
		asking about.
		"""

	def changes (
		self,
		*,
		since: int | None = None,
		before: int | None = None,
		mine: bool = False,
		by: str | None = None,
		newest: bool = False,
		workspace: str | None = None,
		limit: int | None = None,
		dated: typing.Mapping[str, str] | None = None,
	) -> Listing[subroutine.views.Event]:
		"""Return what has changed, oldest first, across everything this credential can see.

		**The resumption question, and the counterpart to :meth:`history`.** That one asks what
		happened to a named item; this asks what happened at all, which is what somebody
		arriving after an absence actually wants and cannot assemble from the other.

		``since`` is the ``seq`` of the last event already dealt with, and is **inclusive** —
		send back what you last saw rather than the number after it, and ignore what you
		already hold (§5.11). ``mine`` narrows to what *this credential* did, not this person.

		Oldest first, because a feed is read forwards. **Events under a second old are withheld**
		and this is not a defect to be worked around: a sequence number becomes visible at
		commit rather than at insert, so reporting the newest instantly is how a change ends up
		behind a cursor that has already passed it.

		``before`` is the other direction and is **exclusive** (`#1097`). With ``newest`` set,
		``has_more`` means there are *earlier* events, and ``since`` is a floor — so this is how
		a long history is walked back through: send the ``seq`` of the earliest event you hold.
		It is not a cursor to persist between polls, which is why it excludes rather than
		includes: the row is already in hand. The two bounds compose, and together they are a
		range.

		``dated`` is §9.6's dotted filters as somebody wrote them — ``created_at.gte`` against
		``yesterday`` — and answers the question a cursor cannot (`#1431`, decision `#1429`).
		**A period and a cursor are different questions and both are kept**: ``since`` resumes
		and is inclusive-with-dedupe, a range is a statement about a period and is not
		resumable. Somebody asking *what did we do on Friday* has no cursor to offer.
		"""

	def journal (
		self,
		*,
		dated: typing.Mapping[str, str] | None = None,
		by: str | None = None,
		mine: bool = False,
		oldest: bool = False,
		workspace: str | None = None,
		limit: int | None = None,
	) -> Listing[subroutine.views.JournalEntry]:
		"""Return what happened, newest first, with who did it and what they said.

		**The counterpart to :meth:`changes`, and the difference is entirely a join** — item
		`#1430`, decision `#1429`. That one answers *what changed*: raw, resumable from a
		cursor, and what a client polling for work should read. This answers *what happened*,
		which is what somebody asked to say what a stretch of time contained needs — the
		comment bodies, the actor's name, and the meaning of the values inside a change.

		``dated`` is the period, in the same spelling every listing takes. Without one you get
		the most recent entries.

		**Newest first, where the feed runs forwards.** A feed is read forwards because it
		resumes; a journal is a report about a past period and the recent end is usually what
		was meant. ``oldest`` reads a period in the order it happened, which is what you want
		when writing it up.
		"""

	def projects (
		self,
		*,
		workspace: str | None = None,
		limit: int | None = None,
		parent: str | None = None,
		visibility: str | None = None,
		include_archived: bool = False,
		order: str | None = None,
	) -> Listing[subroutine.views.Project]:
		"""List the projects this credential can see, parents before children.

		Ordered by materialised path rather than by name, so a child follows its parent and
		the tree can be printed without the caller reassembling it (§8.4). ``order`` overrides
		that, and is the one argument here that will make a listing *harder* to read — a tree
		sorted by title is a list of names whose indentation lies.

		``parent`` narrows to one project's children by key or id, ``visibility`` to public or
		private, and ``include_archived`` widens to projects somebody has finished with. All
		four were declared by ``GET /v1/projects`` and reachable from no client (`#501`).

		**``order`` was the fifth, and this docstring described it for a day before the
		signature had it** — a parameter documented and absent, which reads to anybody skimming
		as one that exists. It arrived last because it needed the sort vocabulary moved out of
		``api/projects.py``, where only one transport could see it.
		"""

	def create_project (
		self,
		*,
		key: str,
		title: str,
		description: str | None = None,
		parent: str | None = None,
		visibility: str = "public",
		workspace: str | None = None,
	) -> subroutine.views.Project:
		"""Create a project.

		Named plainly rather than in one word. :meth:`capture` and :meth:`remark` are single
		verbs because :meth:`tasks` and :meth:`comments` were already taken and a reader and a
		writer must not be told apart by a plural — there is no such collision here, and
		without one the name that says what it does wins over the pattern.

		**No ``template``.** §6.12's templates seed ``project.settings`` at creation and never
		act again, and the one anybody would reach for from a code repository currently writes
		a promise nothing keeps (`#133`). A parameter offering a choice between "nothing" and
		"something untrue" is not a choice worth exposing; the HTTP API still carries it.
		"""

	def users (self) -> list[subroutine.views.User]:
		"""List the accounts on this instance, oldest first — item ``#174``.

		**On the protocol rather than left to the CLI's administrative half**, unlike ``db`` and
		``token``. Those open the database directly because §12.4's recovery property needs them
		to work when the service will not start; adding a colleague has nothing to do with
		recovery, so claiming that exemption would be a shrug wearing its clothes. Going through
		a connection is also what makes ``--connection work`` administer the company instance
		from a laptop, which is the case a page called "Running it for a team" is about.
		"""

	def create_user (
		self,
		*,
		username: str,
		display_name: str | None = None,
		email: str | None = None,
		timezone: str | None = None,
		is_service_account: bool = False,
		is_superuser: bool = False,
	) -> subroutine.views.User:
		"""Add a person, or a machine identity, to this instance.

		Needs ``instance:user_create``, which no role carries (§7.1). The new account belongs to
		no workspace: that is a separate act with a separate permission, because deciding
		somebody exists and deciding where they may work are different decisions.
		"""

	def tokens (self) -> list[subroutine.views.Token]:
		"""List the credentials this caller may act on, newest first — item `#348`.

		**Here at all because §12.4's local-database rule assumed a local database.** `token`,
		`db` and `user` open one directly so that recovery works when the service will not
		start, which is right and is why `subroutine token list` never went through a
		connection. On a machine whose work lives on a *served* instance there is no local
		database to open, so the three commands that administer credentials could only be run
		while sitting on the server — and setting an agent up is something you do on the
		machine the agent runs on.

		Never a secret: only a hash is stored (§7.4), so there is nothing in a listing to leak.
		"""

	def issue_token (
		self,
		*,
		title: str | None = None,
		username: str | None = None,
		service_account: str | None = None,
		workspace: str | None = None,
		scopes: typing.Sequence[str] = (),
		projects: typing.Sequence[str] | None = None,
		writes: typing.Sequence[str] | None = None,
		expires: str | None = None,
	) -> subroutine.views.IssuedToken:
		"""Mint a credential and return it once, secret included — item `#348`.

		**The only moment the secret exists outside the caller.** Only a hash is stored, so
		nothing recovers it afterwards, including the instance that issued it.

		``writes`` narrows where it may *change* things to a subset of ``projects`` — the
		arrangement an agent working inside a related tree needs, where reading the neighbours
		is the point and writing to them is not (`#371`). ``None`` means its whole reach.

		``projects`` restricts it to those projects and everything under them, by key or by id.
		Keys are resolved by the instance rather than by whichever client asked, so ``SR``
		means one project whichever transport carried it and a second resolver cannot drift
		from the first (`#216`).

		A credential may never be wider than the one asking for it: wider scopes, more
		projects, an unpinned workspace where the caller's own is pinned, or issuing for
		somebody else without ``instance:user_create`` are all refused by the service, so both
		transports refuse identically.
		"""

	def create_login_link (
		self, *, username: str | None = None
	) -> subroutine.views.SignInLink:
		"""Mint a single-use sign-in link for a browser, and return it once — item `#248`.

		``username`` unset is the caller themselves and needs no permission. Naming somebody
		else is administering their access, so it is gated exactly as issuing a credential for
		them is, and refused by the service rather than by whichever client asked.

		**This is the recovery path, not a convenience.** Sending the link by email is
		`#599`; until then, and whenever a mail relay is misconfigured, this is the only way
		into a browser — which is §12.4's rule that the administrative path has to work when
		the ordinary one does not.
		"""

	def sign_out_everywhere (self, *, username: str) -> subroutine.views.SignedOut:
		"""End every browser session an account holds, and report how many — item `#248`.

		Unspent sign-in links go with them, because a link is a session that has not happened
		yet. Revocation takes effect on the next request rather than when something expires,
		which is the property decision `#364` chose an opaque cookie to keep.
		"""

	def revoke_token (self, *, id_or_prefix: str) -> subroutine.views.Token:
		"""Stop a credential working, now, and return it as it now is — item `#348`.

		Addressed by the public prefix a listing prints, or by id. Idempotent, and it keeps the
		*first* revocation time: when a credential stopped being trusted is a fact worth not
		overwriting, and a caller retrying a request it is unsure landed should not move it.
		"""

	def calendars (
		self, *, include_revoked: bool = False
	) -> list[subroutine.views.Calendar]:
		"""List your own calendar feeds, newest first — item `#916`, docs/design.md §20.3.

		Yours and nobody else's, on either transport. §20.6 accepts that a feed URL is a bearer
		credential nobody can audit; an inventory of somebody's is the map that makes one worth
		stealing, so there is no way to ask for another person's.

		Never a secret: only a hash is stored, so there is nothing in a listing to leak.
		``last_polled_at`` is what a reader is looking for — a feed nobody has fetched for six
		months is one to revoke, and it is the only signal there is.
		"""

	def create_calendar (
		self,
		*,
		title: str,
		workspace: str | None = None,
		project: str | None = None,
		audience: str = "everything",
		item_types: typing.Sequence[str] | None = None,
		expires: str | None = None,
	) -> subroutine.views.IssuedCalendar:
		"""Mint a calendar feed and return its URL once — item `#916`.

		**The URL is the credential**, so this is the only moment it exists outside the caller.
		Only a hash of it is stored; nothing recovers it afterwards, including the instance.

		``url`` comes back null where the instance has not been told its own ``public_url``.
		Guessing one from wherever the request arrived would put the secret on whatever host a
		proxy named, on every poll for as long as somebody stays subscribed.

		The feed belongs to whoever asked for it and shows what *they* may see, which is why
		there is no owner argument and why a bounded credential is refused outright.
		"""

	def reset_calendar (self, *, id_or_prefix: str) -> subroutine.views.IssuedCalendar:
		"""Give a feed a new URL, so the one somebody had stops working — item `#916`.

		The feed survives: its scope, its audience and its polling record are all kept, which
		is what makes this the answer to a leak rather than revoke-and-make-another.
		"""

	def revoke_calendar (self, *, id_or_prefix: str) -> subroutine.views.Calendar:
		"""Stop a calendar feed for good, now — item `#916`.

		Immediate, and idempotent: ``revoked_at`` is read on every poll rather than cached, and
		a repeat call keeps the first time rather than moving it.
		"""

	def members (self, *, workspace: str | None = None) -> list[subroutine.views.Member]:
		"""List who belongs to one workspace, and with what role."""

	def add_member (
		self, *, username: str, role: str, workspace: str | None = None
	) -> subroutine.views.Member:
		"""Give somebody a role in a workspace.

		``role`` is named rather than defaulted. What somebody may do in a workspace is exactly
		the decision being taken, and a default would be this method taking it quietly.
		"""

	def set_member_role (
		self, *, username: str, role: str, workspace: str | None = None
	) -> subroutine.views.Member:
		"""Change what somebody may do in a workspace they are already in — `#1440`.

		The third membership verb, beside ``add_member`` and ``remove_member``. Until this
		existed the only route was to remove somebody and add them back, which is two events
		for one act and leaves nothing in the record saying a role was moved.
		"""

	def set_active (self, *, username: str, active: bool) -> subroutine.views.User:
		"""Mark somebody as having left, or bring them back — `#475`.

		Deactivating stops every agent answerable to them (decision `#473`), so a caller that
		can name them first should. The last person able to administer the instance is refused.
		"""

		raise NotImplementedError

	def transfer_agent (self, *, username: str, to: str) -> subroutine.views.User:
		"""Hand an agent to somebody else, who becomes answerable for it — `#478`.

		The other half of the leaver path: agents stop when their person goes, so this is how
		one is kept. Only a person may hand an agent over or take one on.
		"""

		raise NotImplementedError

	def set_timezone (
		self, *, username: str, timezone: str | None
	) -> subroutine.views.User:
		"""Say where somebody keeps their diary — your own account only (`#994`).

		§6.5's user level, which was settable at creation and by no surface afterwards. ``None``
		clears it, so the workspace's zone and then the instance's show through again — *not
		stated* is what makes the chain a chain.

		**No permission grants this for anybody else**, deliberately: the check is *are you this
		person*. A permission to write it would be a permission to be wrong on their behalf.
		"""

		raise NotImplementedError

	def remove_member (self, *, username: str, workspace: str | None = None) -> None:
		"""Take somebody out of a workspace.

		Here rather than later, for the reason `#140` gives about anything that can be added: a
		membership that can only be granted is one whose mistakes are permanent, and this one's
		mistake is somebody seeing a private project.
		"""

	def rename_project (
		self, project: str, *, key: str, workspace: str | None = None
	) -> subroutine.views.Project:
		"""Give a project a different short name — item ``#176``.

		**The old key stops working, and nothing is aliased to it.** That is the decision, not
		a limitation: an alias keeps a name resolving after its owner deliberately retired it,
		and a caller holding the old address gets a 404 they can act on rather than a redirect
		they never notice. Nothing joined to the project moves — ``project.id`` is a UUID.
		"""

	def update_project (
		self,
		project: str,
		*,
		title: str = UNSET,
		description: str | None = UNSET,
		visibility: str = UNSET,
		status: str = UNSET,
		settings: dict[str, typing.Any] = UNSET,
		workspace: str | None = None,
		expected_version: int | None = None,
	) -> subroutine.views.Project:
		"""Change the fields beside a project's address — items ``#434`` and ``#983``.

		**Deliberately separate from :meth:`rename_project`, and not a replacement for it.**
		``#434`` proposed folding both into one ``update_project`` taking every field the route
		accepts, including ``key``. Keeping the address in its own method is what stops a caller
		changing one by accident: ``#176`` decided a rename retires a name loudly, with what
		breaks named first, and a general edit that quietly accepted ``key`` alongside a title
		would undo that decision rather than extend it. ``#434``'s own "watch the address rule"
		paragraph is the argument for this shape.

		``status`` is a key from this workspace's own project vocabulary (§5.5). Before ``#983``
		nothing could set one, so three of the four seeded values — ``on_hold``, ``completed``
		and ``archived`` — were unreachable for the life of every project ever created.

		Omitted is unchanged; ``None`` clears (§8.3). ``visibility`` and ``status`` take no
		null: a project is public or private and always has a status, so there is nothing for
		clearing either to mean.
		"""

	def create_workspace (
		self, *, slug: str, title: str, timezone: str | None = None
	) -> subroutine.views.Workspace:
		"""Make another workspace, owned by whoever asked — item `#300`.

		``init`` names the *first* one and nothing made a second, so an instance could not
		grow past the shape it was installed with. ``POST /v1/workspaces`` has existed since
		M1 and no client reached it.

		Needs ``instance:workspace_create``, which is an instance-tier verb: it happens
		outside every workspace, so no role can carry it and only a superuser holds it (§7.1).
		The check lives in the service, so both transports refuse identically.

		``timezone`` unset means *not stated* rather than UTC, so the instance's own zone
		shows through (§12.3) — a default here would shadow it and leave a step in the chain
		nothing could reach.
		"""

	def rename_workspace (self, workspace: str, *, slug: str) -> subroutine.views.Workspace:
		"""Give a workspace a different short name — item `#295`.

		The same trade ``rename_project`` makes, one segment earlier in an address. Nothing
		inside the instance moves: every table keys on ``workspace_id``, so no ref, link,
		mention or membership is touched. What stops working is anything that wrote the old
		name down — a ``.subroutine`` marker, a stored context, an address in somebody's notes
		— and there is deliberately no alias, for ``rename_project``'s reason.

		Takes the workspace by its *current* short name rather than from the ambient context,
		because renaming the place you are standing in is the ordinary case and naming it
		explicitly is what makes the command re-readable in shell history.
		"""

	def update_workspace (
		self,
		workspace: str,
		*,
		title: str = UNSET,
		description: str | None = UNSET,
		timezone: str | None = UNSET,
		prioritised_project: str | None = UNSET,
		settings: dict[str, typing.Any] = UNSET,
		workspace_id: str | None = None,
		expected_version: int | None = None,
	) -> subroutine.views.Workspace:
		"""Change the fields beside a workspace's address — item ``#434``.

		:meth:`update_project`'s sibling, and the same trade one segment earlier: this method
		does not accept ``slug``, which :meth:`rename_workspace` owns for ``#295``'s reasons.

		``timezone`` is the one that is not cosmetic. §6.5 makes it a step in the chain every
		date in the workspace is read through, so a workspace created in the wrong zone
		rendered every deadline in it wrongly and could not be corrected from any surface —
		which is what made ``#434`` a bug rather than a convenience. ``None`` clears it, and
		clearing means *not stated*, so the instance's own zone shows through rather than UTC
		being asserted (§12.3).

		``prioritised_project`` names the one project whose work rises in this workspace's ranked
		listings (decision ``#982``, item ``#986``), by key or by whole path; ``None`` clears it.
		**Naming a second project unsets the first**, which is the design rather than a
		convenience: one column holds one value, so the trade is made in the same write and there
		is no way to accumulate four quiet boosts nobody remembers setting.

		**It is a method on the workspace even though the command line says it of a project.**
		The state is one fact about the workspace; a project-side call would read as a
		per-project flag, which is the mental model decision ``#982`` refuses.
		"""

	def delete_workspace (self, workspace: str) -> subroutine.views.Workspace:
		"""Move a workspace to the trash, with everything in it — item `#704`.

		Soft, and reversible by :meth:`restore_workspace`. Nothing cascades: every listing
		derives the workspaces it may read from one query, so a deleted workspace takes its
		projects, items, vocabulary and history out of sight together and brings them back
		the same way. The ref counter is untouched.

		**The short name is released**, because the unique index ignores deleted rows — so
		the name can be used again, and restoring may then need a rename first.

		Needs ``workspace:delete``, which is the one verb separating the `owner` and `admin`
		roles. Until this existed the two published a difference the product could not honour.

		The last live workspace is refused: an installation with none cannot file a task and
		reports itself as interrupted part-way through setup.
		"""

	def restore_workspace (self, workspace: str) -> subroutine.views.Workspace:
		"""Take a workspace back out of the trash — item `#704`.

		Named by the short name it had, which is still addressable while it is deleted. If a
		live workspace has taken that name in the meantime the restore is refused by name,
		with the rename that clears it — the alternative is a constraint violation surfacing
		as a 500 for an ordinary sequence of three commands.
		"""

	def move_project (
		self, project: str, *, parent: str | None, workspace: str | None = None
	) -> subroutine.views.Project:
		"""Reparent a project, taking everything under it — item ``#246``.

		``parent`` is a key or an id; ``None`` makes it a root. **Null is a real answer here
		and cannot double as "not asked"**, which is why the argument is required rather than
		defaulted: an omitted parent used to mean "move to root", and flattened whole subtrees
		by accident.

		Nothing is renumbered and nothing changes hands — a project's items travel with it,
		and every ref is per-workspace (§6.2) rather than per-project. What does move is the
		materialised path of the project and of every descendant, which is why this is the one
		project operation whose cost is worth reporting before it runs.
		"""

	def create_document (
		self,
		*,
		title: str,
		body: str | None = None,
		type: str | None = None,
		status: str | None = None,
		project: str | None = None,
		workspace: str | None = None,
		tags: typing.Sequence[str] | None = None,
	) -> subroutine.views.Document:
		"""Write a document — a conclusion the next reader needs (§5.10).

		``status`` is how somebody says a decision is **not** settled yet (`#506`). A decision,
		a finding and a dead end are in force the moment they are written, so they start
		``active`` and everything else starts ``draft`` — which makes drafting one the case that
		needs saying out loud, and it had no way to be said from a client at all.

		**The half of the comment/document distinction that could not be reached** until
		`#138`. `POST /v1/documents` was its only caller, so on a default install — where
		nothing runs ``serve`` — a document could not be written at all, while the MCP
		adapter's own ``subroutine_comment`` description told an agent to write one.

		``type`` names one of the seeded document types — ``note``, ``spec``, ``design``,
		``decision``, ``finding``, ``dead_end`` — and defaults to ``note``. It shadows the
		builtin inside this signature on purpose: the HTTP field, the CLI flag and the view
		all call it ``type``, and a fourth name for one thing costs more than the shadow does.

		``project`` is a key, resolved by ``domain.selection.project`` like everywhere else, so
		an unknown one is refused identically whichever transport asked. Omitted means the
		workspace's Inbox.
		"""

	def update_document (
		self,
		*,
		ref: int,
		workspace: str | None = None,
		title: str = UNSET,
		body: str | None = UNSET,
		type: str = UNSET,
		status: str = UNSET,
		project: str = UNSET,
		tags: typing.Sequence[str] | None = UNSET,
		#: The document this one replaces, by ref — `#1144`. ``None`` breaks the chain.
		#:
		#: **It does two things and only one of them can be done by hand.** It records which
		#: document replaced which, under a unique index so the chain cannot fork, *and* it moves
		#: the predecessor to a superseded status. A caller who could not send it could set the
		#: old document's status and get half the outcome: the status moves and the chain stays
		#: empty, so *what replaced this* has no answer afterwards — which `#1119` makes live,
		#: because a superseded decision stops governing and there is then nothing to read
		#: instead.
		supersedes: int | None = UNSET,
		expected_version: int | None = None,
	) -> subroutine.views.Document:
		"""Revise a document. Omitted is unchanged; ``None`` clears (§8.3).

		**Separate from :meth:`update` rather than a flag on it** (`#291`). That one edits a
		task, and the two share almost no fields: a document has a body and no priority, no
		estimate, no schedule, because §6.14 says a document is not scheduled. One signature
		carrying both would be two disjoint halves and a runtime rule about which apply.

		``PATCH /v1/documents/{id_or_ref}`` has existed since M1 and no client could reach it,
		so the instance could accumulate conclusions and never correct one — which defeats the
		point of keeping them there. §5.10 says a document is what you *concluded*, and a
		conclusion that cannot be revised is a record of what you concluded once.
		"""

	def capture (
		self,
		*,
		text: str,
		workspace: str | None = None,
		timezone: str | None = None,
		type: str | None = None,
		project: str | None = None,
		parent: int | None = None,
		description: str | None = None,
		#: How long before this a reminder is wanted, in ``estimate``'s grammar —
		#: ``"2w"``, ``"1h"`` (`#1211`). **The capture line has no sigil for it and this
		#: does not add one**, for the reason ``parent`` gives above: §6.13's line is
		#: deliberately small, and a reminder is set far less often than it is read.
		reminder: int | str | None = None,
		#: When it is over, for something that lasts — decision `#1235`. **The line has no
		#: way to say this and this does not give it one**, exactly as ``reminder`` above:
		#: §6.13's grammar is deliberately small, and an end is rare. It needs a start,
		#: which the line *can* say — ``Away on 14 August`` with ``ends="2026-08-28"``.
		ends: str | None = None,
		recurrence: str | None = None,
		recurrence_anchor: str | None = None,
		recurrence_trigger: str | None = None,
	) -> Captured:
		"""Create a task from a line of text (§6.13).

		``parent`` is the ref of the task this one goes underneath — the first step of
		breaking work up and handing the parts over, and reachable from no client at all
		until `#510`. It is a ref rather than an id for :meth:`move`'s reason: a ref is
		what a caller has. A parent they cannot see is *not found* rather than ignored.

		**The capture grammar has no sigil for it and this does not add one.** §6.13's
		line is deliberately small, and `test_reach` was told for months that the grammar
		covered this field when it does not — which is what hid the gap.

		``project`` is where it goes when the *line* does not say — a `+KEY` in the text wins,
		because that is somebody being explicit about this one item (§13.7a, `#159`).

		``type`` and ``description`` are the two fields the *grammar* cannot carry, and both are
		passed separately rather than given a sigil: §6.13's sigils are for things a person
		types mid-sentence, and neither "this is a bug" nor three paragraphs of reasoning is
		part of the sentence.

		Filing with the right type matters because the type is a promise about what the title
		says — a bug's title states what is wrong, everything else states what will be true when
		it is done — and until `#42` it could not be corrected later.

		**``description`` is here because the skill's argument for those titles depends on it**
		(item ``#424``). It tells a filer to leave the motivation out of the title *"because it
		belongs in the description — which is one field away"*, and from here it was not one
		field away: it was a second call, to a different method, after the item existed. An
		agent weighing calls skips an optional second write, so what the sentence actually
		bought was outcome-shaped titles with the reasoning nowhere. Reported by an agent that
		did exactly that on a fresh install and then explained why its own titles were
		unreadable.

		**``POST /v1/tasks`` has taken ``text`` and ``description`` together since M1** — this
		method dropped it. A capability on a route, missing as an *argument* on a method both
		surfaces already call, is `#149`'s blind spot: `test_reach` compares method names, so it
		cannot see one. Fourth instance, after `#178`, `#367` and `#392`.

		**``recurrence`` is here for that same reason and was caught by that same guard** (`#94`).
		A repeat is structured rather than typed mid-sentence — §6.13's ``every …`` span is
		reserved and reported, not read — so it travels beside ``type`` and ``description``
		rather than inside the line.
		"""

	def remark (
		self,
		*,
		ref: int,
		body: str,
		entity_type: str = "task",
		workspace: str | None = None,
	) -> subroutine.views.Comment:
		"""Add one entry to an item's record of what happened.

		Named ``remark`` rather than ``comment`` so that it does not collide with
		:meth:`comments` by a single letter. A method that reads and a method that writes
		should not be told apart by a plural.
		"""

	def read_repeat (
		self,
		*,
		text: str,
		start: datetime.datetime | None = None,
		timezone: str | None = None,
	) -> subroutine.views.Reading:
		"""Say what a written repeat means, without storing anything (§6.7).

		**A calculator, and that is what makes it worth having on a client.** It turns an
		ambiguous natural-language feature into a checkable one: the description comes back in
		different words from the ones sent, so a caller can see whether the thing understood is
		the thing they meant *before* committing to it.

		Refuses exactly as a create would, because it is the same function — so checking first
		and then committing cannot produce two different answers about one phrase.
		"""

	def skip (
		self,
		*,
		ref: int,
		workspace: str | None = None,
	) -> subroutine.views.Task:
		"""Let one occurrence of a repeat go by, and bring the next one.

		Cancelled rather than done: both finish the occurrence and both advance the series, and
		*I did not do this* is a different fact about the month from *I did*.
		"""

	def occurrences (
		self,
		*,
		ref: int,
		until: str | None = None,
		limit: int | None = None,
		workspace: str | None = None,
	) -> subroutine.views.Occurrences:
		"""Say when a repeating task comes round, without materialising anything (§6.7).

		**One occurrence is real and the rest are arithmetic** (decision `#915`), so this is
		how a calendar asks about a birthday without the backlog growing a row per year. It
		answers about the *series* whichever end the caller is holding, because a person is
		always looking at an occurrence — the template is in no listing.
		"""

	def update (
		self,
		*,
		ref: int,
		workspace: str | None = None,
		title: str = UNSET,
		description: str | None = UNSET,
		status: str = UNSET,
		type: str = UNSET,
		importance: int | None = UNSET,
		urgency: int | None = UNSET,
		estimate: int | str | None = UNSET,
		#: How long before this a reminder is wanted, in ``estimate``'s grammar —
		#: ``"2w"``, ``"1h"`` (`#1211`). ``None`` clears it.
		reminder: int | str | None = UNSET,
		project: str = UNSET,
		assignee: str | None = UNSET,
		tags: typing.Sequence[str] | None = UNSET,
		due: str | None = UNSET,
		due_is_all_day: bool | None = UNSET,
		starts: str | None = UNSET,
		starts_is_all_day: bool | None = UNSET,
		ends: str | None = UNSET,
		snooze: str | None = UNSET,
		snoozed_is_all_day: bool | None = UNSET,
		recurrence: str | None = UNSET,
		recurrence_anchor: str | None = UNSET,
		recurrence_trigger: str | None = UNSET,
		timezone: str | None = UNSET,
		#: Which occurrences of a repeating item this change is for — ``"this_one"`` or
		#: ``"from_now_on"``, decision `#1249`. Required when the task repeats and refused
		#: when it does not, so a caller cannot answer a question nobody asked.
		applies_to: str | None = None,
		expected_version: int | None = None,
	) -> subroutine.views.Task:
		"""Change a task's own fields. Omitted is unchanged; ``None`` clears (§8.3).

		Separate from :meth:`complete` and :meth:`schedule`, which were here first and stay:
		those two are *actions* a person takes on a task and read as such at a command line,
		where this is the general edit an agent needs to keep a backlog honest.

		Without it a client could create work and finish it and never re-rank it — which is
		worse than it sounds, because ``priority_score`` is null unless both axes are set and
		an unranked item sorts below everything, looking judged rather than unassessed
		(§6.3a). An agent that can only add findings would bury every one of them.

		``estimate`` takes §6.4's grammar, so ``"4h"`` works here exactly as ``~4h`` does in
		a captured line.

		**``expected_version`` refuses the change if the task has moved on** — §8.9, and `#494`.
		It was accepted by five routes and passed by no client method for as long as both
		existed: built, tested, documented in four places and reachable only over raw HTTP.

		**Opt-in, and ``None`` means *did not ask* rather than *asked and passed*.** That
		asymmetry is the whole design and is why this is not defaulted to anything: a caller who
		says nothing overwrites whatever somebody else saved while it was reading, which is the
		right behaviour for a script that has just read the row and the wrong one for a form
		somebody left open. Only the caller knows which it is.

		**Two surfaces deliberately do not offer it, and the reasons are different.** The
		command line does not, because a person typing `subroutine update 42 --title x` has
		read the item in a previous command and the version they would quote is already stale
		by however long they spent thinking — an argument that is right only when pasted from a
		script one line earlier. `doc edit` is the one place with a real collision (two sessions
		revising one conclusion) and it is `#842`'s problem rather than this one, since that
		command is a whole-body replace.

		**MCP does not, because claims already answer it better** (`#350`). Two agents on one
		ranked listing is the concurrency this product actually has, and a lease says *somebody
		has this* before the work starts, where a version clash says *you lost* after it. A tool
		argument would also spend bytes from §21.2's budget on the weaker of the two answers.
		"""

	def discard (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> subroutine.views.Task | subroutine.views.Document:
		"""Move an item to the trash (§6.9), returning it as it now is.

		**Named for what it does rather than for the verb underneath.** ``delete`` would promise
		more than happens: the row stays, ``deleted_at`` is set, and :meth:`undiscard` puts it
		back. A method called ``delete`` whose sibling is ``undelete`` reads as a contradiction;
		two words that admit the thing is reversible read as what it is.

		Either kind, because one ref counter serves both (§6.2) and ``show`` already takes
		either — nothing about a number says which kind it is, so an operation that worked on
		half of them would surprise whoever was holding one.
		"""

	def undiscard (
		self, *, ref: int, entity_type: str = "task", workspace: str | None = None
	) -> subroutine.views.Task | subroutine.views.Document:
		"""Take an item back out of the trash (§6.9).

		**The half that made soft delete soft**, missing until `#140`. §6.9 has promised since
		the beginning that a deleted item is "restorable for a configurable retention period";
		``trash_retention_days`` was a setting for as long, until `#187` removed it as one nothing
		read; ``EventAction.RESTORED`` has been in the vocabulary. Nothing anywhere set ``deleted_at`` back to null — so three
		places said the same true-sounding thing about a product where "delete" meant "gone".
		"""

	def move (
		self,
		*,
		ref: int,
		parent: int | None,
		entity_type: str = "task",
		workspace: str | None = None,
	) -> subroutine.views.Task | subroutine.views.Document:
		"""Put an item under another one, or at the top level — item ``#44``.

		``parent`` is a ref, and ``None`` means top-level. **Required rather than defaulted**,
		for ``move_project``'s reason: null is a real answer here and cannot double as "not
		asked", and an omitted parent that meant "move to root" flattened subtrees by accident.

		Either kind, because one ref counter serves both (§6.2) and nothing about a number says
		which it is — the same argument :meth:`discard` makes. A task's tree is its subtasks and
		a document's is its sections, and both travel with it.

		**A parent in another project is refused**, because a subtask belongs to its parent's
		project. Change the project first; the refusal names both and says how.
		"""

	def claim (
		self, *, ref: int, minutes: int | None = None, workspace: str | None = None
	) -> subroutine.views.Task:
		"""Take a lease on a task, or renew one this credential already holds — item `#350`.

		**A lease, not a lock** (§14.11). It expires, and an expired one is ignored rather than
		needing anybody to clear it: workers die mid-task routinely, and a claim that outlived
		its holder would strand the work permanently.

		What it is for is two workers taking the same item off the same ranked listing —
		``tasks(ready=True, order="-priority_score")`` deliberately answers the same for
		everybody, so two agents asking the obvious question collide by construction, and the
		cost is not a merge conflict but two of them doing the same work.

		Refuses with :class:`~subroutine.errors.Conflict` when somebody else holds it, naming
		who and until when. Claiming what you already hold renews it and keeps the instant you
		first took it.
		"""

	def release (
		self, *, ref: int, workspace: str | None = None
	) -> subroutine.views.Task:
		"""Give a task back, so somebody else can take it — item `#350`.

		Releasing what nobody holds is not an error and records nothing, so a worker tidying up
		after itself need not check first. Anybody who may change the task may release it, not
		only the holder: the case it exists for is a worker that died holding a lease, and
		requiring its credential would leave the remedy with the one principal that cannot act.
		"""

	def complete (
		self, *, ref: int, workspace: str | None = None
	) -> subroutine.views.Task:
		"""Mark a task finished."""

	def schedule (
		self,
		*,
		ref: int,
		workspace: str | None = None,
		starts: datetime.datetime | datetime.date | None = UNSET,
		ends: datetime.datetime | datetime.date | None = UNSET,
		snooze: datetime.datetime | datetime.date | None = UNSET,
		applies_to: str | None = None,
	) -> subroutine.views.Task:
		"""Set the day a task is planned for, or the day it becomes visible.

		``applies_to`` is decision `#1249`'s answer — ``"this_one"`` or ``"from_now_on"``.
		**Moving a repeating item is the case the whole story was filed for**, so this is not
		the optional extra it looks like beside the three dates.
		"""

	def close (self) -> None:
		"""Release whatever this holds."""

	def __enter__ (self) -> "Client":
		"""Return this client, ready to use."""

	def __exit__ (
		self,
		kind: type[BaseException] | None,
		value: BaseException | None,
		traceback: types.TracebackType | None,
	) -> None:
		"""Release whatever this holds."""


def require_a_method (method: str) -> str:
	"""Return ``method`` upper-cased if a raw call may present it, refusing anything else — `#530`.

	**Both transports read this argument and neither checked it**, so one input got three
	different answers depending on how the caller happened to be connected. Measured against a
	real instance and a real socket rather than reasoned about:

	==========================  ==============  =========================================
	given                       in process      over HTTP
	==========================  ==============  =========================================
	``BREW``                    405             400, from the server rather than the router
	``GET\r\nX-Smuggled: 1``    405             refused by h11 at write time
	``GET`` with a space        405             refused by h11 at write time
	``""``                      405             refused by h11 at write time
	==========================  ==============  =========================================

	**The three HTTP rows arrived as "could not be reached"**, hinted with *check that the
	instance is running and that you are on a network that can reach it* — a refusal blaming the
	network for the caller's own argument, which is the one thing a refusal here must not do.

	**Nothing was smuggled**, and that is h11's doing rather than ours: a method carrying CRLF
	never reaches the wire. This is an equivalence and legibility fix, not a mitigation.

	An allow-list rather than a syntax check, because a syntactically valid method both
	transports accept is exactly the ``BREW`` row — well-formed, and answered two ways.
	"""

	given = method.strip().upper()

	if given not in CALLABLE_METHODS:
		listed = ", ".join(sorted(CALLABLE_METHODS))

		raise subroutine.errors.ValidationError(
			f"{method!r} is not a method this instance answers to.",
			hint=f"Methods you can use: {listed}.",
			errors=[
				subroutine.errors.FieldError(
					field="method",
					code="invalid_field_value",
					message=f"Expected one of {listed}.",
				)
			],
		)

	return given


def require_a_route (path: str) -> str:
	"""Return ``path`` if it is a route on this instance, refusing anything that is not — `#529`.

	**This is where the credential is, which is why the rule is here.** ``httpx`` treats an
	absolute URL as a *replacement* for the base URL rather than as a path, and a client's
	default headers go with it — so ``call_api(path="https://elsewhere.example/collect")`` sends
	this connection's bearer token to whoever asked for it. Measured, not reasoned about.

	It was unreachable when `#527` found it, because ``mcp/tools`` refuses a path that does not
	start with ``/`` and is the only caller. That is the finding rather than the mitigation: the
	guard was a layer above the thing it protects, so the second caller — a future tool, a
	script, an editor integration — would have inherited a credential-exfiltration primitive
	without anybody deciding to hand it one.

	``//host/x`` is refused too, though httpx merges it against the base URL's host and it is
	*currently* harmless. A protocol-relative reference means "another host" to enough of the
	web that letting it through here would be relying on one library's merge rule to stay put.
	"""

	given = path.strip()

	if not given.startswith("/") or given.startswith("//"):
		raise subroutine.errors.ValidationError(
			f"{path!r} is not a route on this instance. Pass a path beginning with a single "
			f"'/', such as '/v1/tasks'.",
			hint="A whole URL is refused deliberately: this connection's credential travels "
			"with the request, and it belongs only to the instance it was issued for.",
		)

	if not given.startswith(f"{API_PREFIX}/"):
		raise subroutine.errors.ValidationError(
			f"{path!r} is not part of this instance's API. Paths a raw call may reach begin "
			f"with '{API_PREFIX}/', such as '/v1/tasks'.",
			hint="'/healthz' and '/readyz' are public and say nothing this credential cannot "
			"already ask for; '/mcp' is a transport rather than a route, and reaching it from "
			"here would be this tool calling the thing that hosts it.",
		)

	return given


def refuse_a_write (connection: subroutine.connections.Connection) -> typing.NoReturn:
	"""Refuse a write to a connection configured read-only.

	Enforced client-side and worth having (§13.7): pointing an agent at a company instance
	for context while forbidding it to write there is a reasonable posture, and it is not one
	the company's server can be asked to arrange on the agent-owner's behalf.
	"""

	raise subroutine.errors.Forbidden(
		f"Connection {connection.name!r} is configured read-only, so nothing can be changed "
		"there.",
		hint=f"Remove 'read_only' from [connections.{connection.name}] in "
		f"{subroutine.config.config_file_path()} if that is no longer what you want.",
	)
