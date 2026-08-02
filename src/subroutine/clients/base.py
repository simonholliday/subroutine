"""What every connection can be asked, whichever side of a socket it is on.

SPEC.md §13.7 makes the local database a connection like any other, so that
``subroutine today`` fans out across it and every configured remote through one code path
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


class Client(typing.Protocol):
	"""One connection, ready to be asked things.

	Every implementation is a context manager, because both of them hold something that has
	to be given back — an engine on one side, a pooled socket on the other.
	"""

	connection: subroutine.connections.Connection

	def identity (self) -> Identity:
		"""Report which instance this is and which workspaces the credential reaches."""

	def agenda (
		self,
		*,
		date: datetime.date | None = None,
		timezone: str | None = None,
		horizon_days: int | None = None,
		unscheduled_limit: int | None = None,
		workspace: str | None = None,
	) -> subroutine.views.Agenda:
		"""Return the four buckets, across every workspace this credential reaches.

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

	def tasks (
		self,
		*,
		workspace: str | None = None,
		limit: int | None = None,
		include_completed: bool = False,
		order: str | None = None,
		project: str | None = None,
		deferred: str = subroutine.domain.readiness.DEFAULT_DEFERRAL,
		q: str | None = None,
		parent: int | None = None,
		ready: bool = False,
		deleted: bool = False,
	) -> list[subroutine.views.Task]:
		"""List one workspace's open tasks, newest first unless ``order`` says otherwise.

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
	) -> list[subroutine.views.Document]:
		"""List one workspace's documents, newest first unless ``order`` says otherwise.

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

	def unlink (
		self, *, ref: int, link_id: str, entity_type: str = "task", workspace: str | None = None
	) -> None:
		"""Withdraw a link.

		Follows :meth:`link` closely rather than waiting for somebody to ask: a link added by
		mistake blocks work that is not blocked, and readiness then hides it — so an unwanted
		link is worse than a missing one, because it narrows what looks startable and says
		nothing about having done so.
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
	) -> list[subroutine.views.Event]:
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
		mine: bool = False,
		newest: bool = False,
		workspace: str | None = None,
		limit: int | None = None,
	) -> list[subroutine.views.Event]:
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
		"""

	def projects (
		self, *, workspace: str | None = None, limit: int | None = None
	) -> list[subroutine.views.Project]:
		"""List the projects this credential can see, parents before children.

		Ordered by materialised path rather than by name, so a child follows its parent and
		the tree can be printed without the caller reassembling it (§8.4).
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
	) -> subroutine.views.User:
		"""Add a person, or a machine identity, to this instance.

		Needs ``instance:user_create``, which no role carries (§7.1). The new account belongs to
		no workspace: that is a separate act with a separate permission, because deciding
		somebody exists and deciding where they may work are different decisions.
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
		project: str | None = None,
		workspace: str | None = None,
	) -> subroutine.views.Document:
		"""Write a document — a conclusion the next reader needs (§5.10).

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

	def capture (
		self,
		*,
		text: str,
		workspace: str | None = None,
		timezone: str | None = None,
		type: str | None = None,
		project: str | None = None,
	) -> Captured:
		"""Create a task from a line of text (§6.13).

		``project`` is where it goes when the *line* does not say — a `+KEY` in the text wins,
		because that is somebody being explicit about this one item (§13.7a, `#159`).

		``type`` is the one field here that the *grammar* cannot carry, and it is passed
		separately rather than given a sigil: §6.13's sigils are for things a person types
		mid-sentence, and "this is a bug" is a classification made about the sentence rather
		than part of it. Filing with the right type matters because the type is a promise
		about what the title says — a bug's title states what is wrong, everything else states
		what will be true when it is done — and until `#42` it could not be corrected later.
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
		project: str = UNSET,
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
		``trash_retention_days`` has been a setting for as long; ``EventAction.RESTORED`` has
		been in the vocabulary. Nothing anywhere set ``deleted_at`` back to null — so three
		places said the same true-sounding thing about a product where "delete" meant "gone".
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
		planned_for: datetime.date | None = UNSET,
		start: datetime.date | None = UNSET,
	) -> subroutine.views.Task:
		"""Set the day a task is planned for, or the day it becomes visible."""

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
