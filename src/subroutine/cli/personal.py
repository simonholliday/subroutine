"""``add``, ``today``, ``ls``, ``show``, ``done``, ``plan``, ``defer``, ``comment``, ``use``.

These few commands are the entire surface a person needs, and §13.5b says so with a
stopwatch: a fresh installation to a working to-do list in three commands, a task completed
with a fourth, and **not one of those outputs mentioning a workspace, a status, a project, a
criterion, a verification, a session or a claim**. That is the guard on §1.4's
progressive-disclosure rule, and it will fail the first time somebody adds a required field
for an agent's benefit — which is the point of having it.

Everything here runs through a **connection** (§13.7), and the local database is one of them.
There is one code path, and it does not know which of its answers arrived over a socket. With
a single connection and a single workspace — the overwhelmingly common case, and the whole of
§13.5b — nothing is labelled, nothing is grouped and no address carries a prefix, because
there is nothing to disambiguate.

Two rules shape the rendering, and both are product surface rather than polish:

* **What is printed is what can be typed back.** Every row shows the shortest address that
  actually resolves, so a merged listing documents itself per row rather than in a footer.
* **A command that acts names what it acted on**, absolutely, whenever the address it was
  given was relative. The moment of consequence is where a confirmation belongs.
"""

import contextlib
import dataclasses
import datetime
import functools
import json
import operator
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import textwrap
import typing
import uuid

import rich.console
import rich.text
import typer
import typer.core

import subroutine.cli.output
import subroutine.clients.base
import subroutine.clients.local
import subroutine.clients.opening
import subroutine.config
import subroutine.connections
import subroutine.context
import subroutine.credentials
import subroutine.db.models.work
import subroutine.db.seed
import subroutine.db.types
import subroutine.directory
import subroutine.domain.agenda
import subroutine.domain.capture
import subroutine.domain.dates
import subroutine.domain.durations
import subroutine.domain.filtering
import subroutine.domain.ordering
import subroutine.domain.palette
import subroutine.domain.projects
import subroutine.domain.readiness
import subroutine.domain.recurrence
import subroutine.domain.refs
import subroutine.domain.schedule
import subroutine.domain.search
import subroutine.domain.settings
import subroutine.domain.tasks
import subroutine.domain.text
import subroutine.errors
import subroutine.fanout
import subroutine.installations
import subroutine.permissions
import subroutine.views

#: What ``plan``'s day argument says, the clear included — `#1316`.
#:
#: Out here for `#943`'s ratchet, like the two below it: the sentinel default and
#: ``show_default=False`` are three more lines in the closure, and the help text is the part
#: that does not need to be there.
PLANNED_DAY = "A day — 'today', 'tomorrow', 'friday', '2026-08-01'. Pass '' to clear it."

#: What ``--type`` offers, built from the seeds rather than written out — `#1240`.
#:
#: **Module level rather than in the option**, because `#943`'s ratchet counts the closure and
#: an f-string over two calls is seven lines where a literal was one. It also puts the two
#: spellings beside each other: ``add`` names the default because somebody filing their first
#: item has not chosen one, and ``update`` does not because they are changing a type that is
#: already set.
TASK_TYPES = f"{subroutine.db.seed.named_types('task')}."

TASK_TYPES_WITH_DEFAULT = (
	f"{TASK_TYPES[:-1]}. Defaults to {subroutine.db.seed.default_type('task')}."
)

#: How many tasks ``ls`` shows before it stops. Enough to scroll, few enough to read.
DEFAULT_LIST_LIMIT = 50

#: Styles, applied to the parts of a line this program wrote and never to the parts the
#: user did. Rich turns them off by itself when the output is not a terminal, which is what
#: §12.2a means by "detected, never configured" — there is no flag and no setting.
HEADING = "bold"
POSITION = "dim"
DETAIL = "dim"
LATE = "red"
SUGGESTION = "dim cyan"
GROUP = "bold cyan"

#: Where a search term matched. One of the sixteen basic names, so it is *the user's* yellow
#: and their terminal theme decides what that looks like (decision `#102`) — a hex value would
#: ignore the theme and land unreadable on somebody's background. Not `LATE`'s red: red already
#: means overdue here, and one colour with two meanings is the thing that rule exists to stop.
MATCH = "yellow"

#: What a row says when a search found it somewhere this listing cannot read — `#881`. A word
#: rather than a blank, for `STARTED_MARK`'s reason, and *this* word rather than ``comment``
#: because it is the one the function can prove: a comment is what it usually is and a stemmed
#: title match is what it sometimes is, and only "not in what you can see" covers both.
ELSEWHERE = "elsewhere"

#: How many comments ``show`` prints in full before it stops and says how many there are
#: (`#37`). Enough that the ordinary item — everything in this instance has a handful — reads
#: exactly as it did, and small enough that an item somebody has worked on for a month is still
#: an answer rather than a transcript. The *count* is always shown, so a reader is never left
#: unaware that a record exists.
COMMENTS_SHOWN = 5


#: What a ref may turn out to name. **One counter per workspace serves both** (§6.2), so
#: ``#4`` is as likely to be a specification as a job — and a command that only ever asked
#: about tasks would report that ``#4`` does not exist while it sits in the same listing.
Item = subroutine.views.Task | subroutine.views.Document

@dataclasses.dataclass
class Selected:
	"""What the options before the subcommand chose.

	Held as one mutable object rather than passed down, because Typer resolves a callback's
	options before it resolves the command's and there is no other way for the two to meet.
	"""

	connection: str | None = None
	workspace: str | None = None


@dataclasses.dataclass(frozen=True)
class Reached:
	"""One connection, open, and what it says it can reach."""

	client: subroutine.clients.base.Client
	identity: subroutine.clients.base.Identity

	@property
	def name (self) -> str:
		"""Return the connection's name."""

		return self.client.connection.name

	def reader_timezone (self, workspace: str | None) -> str | None:
		"""Return the zone this connection says the caller reads a *typed* day in.

		§6.5's chain, resolved by the instance and published on ``/v1/meta`` (`#1083`, decision
		`#1088`). ``None`` where the instance is a release behind and sends no such key, which
		is the caller's cue to fall back to this machine — the answer it gave before.

		**Matched by slug, and unmatched falls through to the first workspace this connection
		reaches.** A command that names no workspace is acting in whichever one the context
		chose, and every workspace on one instance shares a user and an instance zone; the level
		that can differ is the workspace's own, which is null on every workspace here and is a
		fallback below the account's in any case.
		"""

		for candidate in self.identity.workspaces:
			if workspace is not None and candidate.slug == workspace.strip().lower():
				return candidate.reader_timezone

		return self.identity.workspaces[0].reader_timezone if self.identity.workspaces else None

	def slug_of (self, workspace_id: typing.Any) -> str | None:
		"""Return the short name of one of this connection's workspaces."""

		for workspace in self.identity.workspaces:
			if workspace.id == workspace_id:
				return workspace.slug

		return None


@dataclasses.dataclass(frozen=True)
class World:
	"""Every connection this command can reach, and which one a bare number means."""

	roster: subroutine.connections.Roster
	current: subroutine.context.Current
	reached: tuple[Reached, ...]
	unreachable: tuple[subroutine.fanout.Failure, ...]
	settings: subroutine.config.Settings

	#: The `.subroutine` marker found at or above the working directory, if any (§13.7a).
	#: Carried on the world rather than read where it is needed, so one walk answers every
	#: command and two commands in one process cannot disagree about which directory they are
	#: in.
	marker: subroutine.directory.Marker | None = None

	#: The connection a `--connection` filter narrowed this listing to, if one did (`#272`).
	#: Carried rather than inferred from :attr:`reached`, because the two questions differ:
	#: how many connections are being *read* decides nothing, and how many this machine has
	#: decides whether a printed address still resolves once the flag is gone — which is
	#: `#280`'s rule, met here by a filter rather than by a context flag.
	narrowed_to: str | None = None

	#: Two connections that turned out to name one instance, if any — held rather than raised
	#: (`#942`). See :meth:`merging` for why it is carried this far.
	collision: subroutine.errors.SubroutineError | None = None

	def account_zone (self, connection: str | None, workspace: str | None) -> str:
		"""Return the account's zone here — §6.5 resolved by the instance, not by this machine.

		**One function because decision `#1088` asks one question twice.** A day somebody
		*writes* is read in the setter's zone and a moment somebody *reads* is rendered in the
		reader's; at a terminal the setter and the reader are the same account, so both are
		this. It was called ``typed_day_zone`` while writing was its only caller, which made
		the name a claim about the use rather than about the value (`#1091`).

		Published on ``/v1/meta`` rather than resolved here, so no client holds a copy of the
		chain (`#925`). This used to be ``settings.default_timezone``, whose default is this
		machine's OS zone — so ``subroutine agenda today`` and a bare ``subroutine agenda``
		could name different days near midnight, because the answer is bucketed in the
		account's zone and the question was asked in the laptop's.

		**Falls back to this machine when the instance does not say**, which is an instance one
		release behind (`#345`): the old answer, rather than a refusal for a field that has
		only just started existing.
		"""

		for reached in self.reached:
			if connection is None or reached.name == connection:
				said = reached.reader_timezone(workspace)

				if said is not None:
					return said

				break

		return self.settings.default_timezone

	def merging (self) -> None:
		"""Refuse to combine answers from connections that turn out to be one instance.

		**Called where answers are flattened, not where the world is opened** (`#942`, cold
		review `#927`'s M-33). `#327` wrote the rule down — *the check runs unless the command
		reports each connection separately or targets exactly one* — and then applied it with a
		flag on :func:`opened`, which asks *"is this command a merge?"* before anybody knows
		what the command will do. Thirty-five of thirty-eight call sites took the fail-closed
		default and were never revisited, so ``subroutine add "milk"`` — §1.4's primary path,
		writing to exactly one connection — was refused by a guard that protects a merge.

		Asked here, the question is local and obvious: *this line is putting two connections'
		rows in one list*. A command that prints a heading per connection never reaches it,
		and a merge written next year is covered by reaching for the same flattener.
		"""

		if self.collision is not None:
			raise self.collision

	@property
	def clients (self) -> list[subroutine.clients.base.Client]:
		"""Return every open client, in roster order."""

		return [item.client for item in self.reached]

	@property
	def qualifies_connection (self) -> bool:
		"""Report whether an address here has to name its connection.

		**Narrowing by `--connection` does not shorten an address** (`#272`, on `#280`'s
		rule). The flag is gone by the command somebody types next, so a row printed bare
		because *this* listing had one connection in it would be an invitation to act on the
		wrong instance — the exact hazard qualifying exists to remove.
		"""

		return len(self.reached) > 1 or self.narrowed_to is not None

	@property
	def qualifies_workspace (self) -> bool:
		"""Report whether an address here has to name its workspace.

		True as soon as *anything* reachable holds a second workspace. A listing must show
		the shortest form that resolves, and with one workspace everywhere there is nothing
		to say — which is why §13.5b's output is untouched by any of this.
		"""

		return sum(len(item.identity.workspaces) for item in self.reached) > 1

	@property
	def prioritised (self) -> tuple[str, ...]:
		"""Return each prioritised project, addressed the way a row here would be.

		Decision ``#982``: one project per workspace may be prioritised, so a merged view can
		hold one per place (§13.7) — usually none, and here at most two.

		**Qualified by :attr:`qualifies_workspace` and :attr:`qualifies_connection`**, which is
		the rule every other address on this surface follows: the shortest form that identifies
		the thing, and no shorter. Saying *dist is prioritised* on a machine reaching two
		workspaces that both hold a ``dist`` would name neither.
		"""

		found: list[str] = []

		for item in self.reached:
			for workspace in item.identity.workspaces:
				if not workspace.prioritised_project:
					continue

				address = workspace.prioritised_project

				if self.qualifies_workspace:
					address = f"{workspace.slug}/{address}"

				if self.qualifies_connection:
					address = f"{item.name}/{address}"

				found.append(address)

		return tuple(found)

	def connection (self, name: str) -> Reached | None:
		"""Return one open connection by name."""

		for item in self.reached:
			if item.name == name:
				return item

		return None

	def writing_to (self) -> Reached:
		"""Return the connection a write goes to, or explain why there is not one."""

		found = self.connection(self.current.connection)

		if found is not None:
			return found

		raise subroutine.errors.ServiceUnavailable(
			f"{self.current.connection} could not be reached, so nothing can be changed "
			"there.",
			hint="Try again when it is back, or work somewhere else with "
			"'subroutine use <connection>'.",
		)

	def address_of (
		self,
		connection: str,
		workspace_id: typing.Any,
		ref: int,
		*,
		next_time: bool = False,
	) -> str:
		"""Return the shortest address that resolves to this item, from here.

		A row inside the current context is a bare ``#42``; one in another workspace carries
		it; one on another connection carries both. That is what makes a merged listing safe
		to copy out of — a bare number beside an item somewhere else is an invitation to act
		on the wrong one.

		``next_time`` asks the *other* question: not "how do I label this row for somebody
		reading it now" but "what would reach this item from the command they type next"
		(`#280`). The two answers differ exactly when the context came from a flag, because
		a flag is gone by then — and the shortest form is then the dangerous one.
		"""

		item = self.connection(connection)
		slug = None if item is None else item.slug_of(workspace_id)

		if slug is None:
			return subroutine.domain.refs.format_ref(ref)

		if next_time and not self.current.persists:
			return subroutine.domain.refs.format_address(
				ref,
				workspace=slug,
				connection=connection if self.qualifies_connection else None,
			)

		if connection == self.current.connection and slug == self.current.workspace:
			return subroutine.domain.refs.format_ref(ref)

		if connection == self.current.connection or not self.qualifies_connection:
			return subroutine.domain.refs.format_address(ref, workspace=slug)

		return subroutine.domain.refs.format_address(ref, workspace=slug, connection=connection)

	def address_of_item (self, connection: str, item: Item, *, next_time: bool = False) -> str:
		"""Return the shortest address that resolves to this item, task or document."""

		return self.address_of(connection, item.workspace_id, item.ref, next_time=next_time)

	def address_of_located (self, located: "Located") -> str:
		"""Return the shortest address that resolves to an item already found.

		The shortest, not the absolute one: a refusal listing candidates wants every address
		spelled out, while a heading over the item somebody just asked for wants the form
		they typed. Printing ``si/#24`` back at a person with one workspace is the §1.4
		leak this whole command had to avoid.
		"""

		return self.address_of(located.connection, located.item.workspace_id, located.ref)


#: The kinds each command is willing to be given. ``done`` acts on work and nothing else;
#: ``show`` reads whatever the number names. Passed rather than assumed, so that a command
#: which cannot act on a document *says* so instead of claiming the item is missing.
TASKS_ONLY = ("task",)
ANY_ITEM = ("task", "document")

#: Named here so the message a person reads and the file they will look for agree.
FILE_NAME = subroutine.directory.FILE_NAME

#: The narrowing words whose value belongs to one workspace rather than to the instance.
#:
#: A read spans every workspace a credential can reach (§13.7), so one of these being absent
#: from *a* workspace is the ordinary answer and the workspace is passed over; one that is
#: absent from *every* workspace is a typo and is still refused by name. That distinction is
#: `#332`'s, corrected by `#1468`, and this register exists because it was written out twice
#: and `tag` was added to neither — `#1575`, which made `--tag` refuse on every instance with
#: more than one workspace while the API answered the same question correctly.
#:
#: **An assignee is deliberately not here.** An account belongs to the instance, so a name that
#: resolves nowhere is a typo wherever it was asked, and tolerating it would turn one into
#: "nothing on your list" across every workspace at once.
_PER_WORKSPACE_WORDS = frozenset({"status", "type", "tag"})

#: What ``update`` treats as "you did not name this field", for the two it can *clear*.
#: §8.3's distinction between omitted and null is the whole of `PATCH`'s semantics, and a
#: shell has only one way to say nothing — so `--description ""` has to mean "clear it" and
#: leaving the flag off has to mean "leave it alone". A default of `""` would collapse the two
#: and make clearing unreachable; these are values nobody can type by accident.
UNGIVEN = "\x00not given"
UNGIVEN_NUMBER = -1

#: One row of a listing: which connection it came from, and the item itself. A *listing* row
#: may be either kind — ``list`` spans both, because refs are shared and a reader who has
#: learned that a number names an item is owed a list where every item appears. The *agenda*
#: only ever holds tasks, and passes them through the same helpers because a task is an item.
Row = tuple[str, Item]

#: What the terminal calls each bucket, and whether it is late.
#:
#: **The order is not here** — it is :data:`subroutine.views.AGENDA_BUCKETS`, so the terminal,
#: the browser and an agent cannot disagree about which section comes first or which sections
#: there are (`#992`). What is here is the terminal's own two facts about each.
#:
#: A bucket the agenda carries and this does not name raises at import, which is the intended
#: failure: a heading nobody chose is worse than a build that stops.
_HEADINGS: dict[str, tuple[str, bool]] = {
	# **First, Simon's decision of 2026-08-25** (`#1243`): *"I would naturally complete a task
	# before starting another."* It was between the day and the rest (`#853`), on the argument
	# that started work is neither scheduled nor a candidate to pick up. That is still true and
	# it now puts this at the top rather than in the middle: everything below is something to
	# *begin*, and this is the only section that is already in hand.
	#
	# **Not marked late as a section**, and it is the reason the marking moved to the row: this
	# bucket takes a started task whose deadline has passed, so a flag here would paint work
	# that is merely in progress, and a flag nowhere would lose the late one entirely.
	"in_progress": ("In progress", False),
	# **Marked late for the same reason `overdue` is** (`#1116`). Somebody has been
	# waiting on an answer, which is a commitment you have not kept in exactly the way a passed
	# deadline is — and unlike everything below it, nothing here can move until you act.
	"waiting": ("Waiting on you", True),
	# **The pair with the one above it, and that is what makes both legible** (`#1285`,
	# decision `#1267` §3). *Waiting on you* is a question somebody parked for you; this is
	# your work held up by somebody else's row. The key says the mechanism and the heading
	# says the experience, which is `occasions`/*Happening*'s established shape.
	#
	# **Not marked late as a section**, unlike the one above. Nothing here is a commitment you
	# have failed to keep — the whole point is that the next move is not yours — and the buckets
	# are disjoint in order, so a blocked task with a passed deadline lands here rather than in
	# *Overdue*. The row still marks itself late, exactly as `in_progress` does.
	"blocked_by_others": ("Waiting on somebody else", False),
	"overdue": ("Overdue", True),
	# **What is happening to you, above what there is to do** (decision `#1235` §4). A code
	# freeze or a fortnight off is the context the rest of the page is read in, and none of it
	# is work anybody can pick up — which is why it is not in *Today*, whose question is *what
	# can I start*.
	#
	# **"Happening" rather than "Happening today"**, which would be false under `--day`: the
	# heading below already carries that debt and one copy of it is enough. It is also true of
	# a fortnight that began last week, which "today" would quietly deny.
	#
	# **Never marked late**, because an occasion cannot be: it has no deadline, and Simon's own
	# words are that *it does not become overdue, it just finishes*.
	"occasions": ("Happening", False),
	"today": ("Today", False),
	# The number is filled in per render by :func:`agenda_sections`, because `--days` moves
	# the window and a heading saying seven over a two-day look-ahead would be a defect
	# shipped with the flag that causes it (`#1005`).
	"upcoming": ("Next {days} day{s}", False),
	# **"Next" rather than "Unscheduled"**, because it is ordered by rank now rather than
	# by capture order — the heading names what the section is *for*, and the old one
	# named only what its rows lacked.
	"unscheduled": ("Next", False),
}

#: The agenda's sections whose rows nobody can be advised to finish (`#1288`).
#:
#: **Two ways to be un-finishable and they are different questions**, which is why this is a set
#: of buckets rather than one more clause in :func:`_happens`. An occasion is un-finishable
#: because of what it *is* — decision `#1235` §5, and it is a property of the row, so it travels
#: with the row into whichever bucket claims it. A blocked row is un-finishable because of where
#: it *stands*: the heading above it says somebody else has to move first, and the item itself
#: is perfectly ordinary work.
#:
#: **Named as a set because it is the second one and there was no list when it arrived.** Each
#: was added when its own bucket shipped, and a third would meet the same question with nothing
#: to ask it.
UNFINISHABLE: frozenset[str] = frozenset({"blocked_by_others"})


def agenda_sections (days: int) -> tuple[tuple[str, str, bool], ...]:
	"""Return the agenda's sections for a given look-ahead: heading, field, whether it is late.

	**A function because ``--days`` moves the window** (`#1005`). The heading naming a number
	the request did not use is the kind of defect that ships *with* the feature causing it, so
	the number comes from the same place the request does.

	The order and the membership are :data:`subroutine.views.AGENDA_BUCKETS`', so the terminal,
	the browser and an agent cannot disagree about which sections there are (`#992`).
	"""

	return tuple(
		(
			_HEADINGS[field][0].format(days=days, s="" if days == 1 else "s"),
			field,
			_HEADINGS[field][1],
		)
		for field in subroutine.views.AGENDA_BUCKETS
	)


#: The agenda's sections at the default look-ahead, in the order a day is read: heading, the
#: field on :class:`subroutine.views.Agenda` that fills it, and whether it is late.
#:
#: **A module constant because a second surface renders the same sections** (`#927` H-15).
#: §12.2 decided what the agenda says and the browser is held to the same words, so this being
#: a local in one function meant the browser's copy could — and did — drift: it was missing
#: ``in_progress`` entirely and still called the last section *Unscheduled*, under a comment
#: claiming to print "deliberately the same words". `tests/test_web.py` compares them now, at
#: the default the browser also uses.
AGENDA_SECTIONS: tuple[tuple[str, str, bool], ...] = agenda_sections(
	subroutine.domain.agenda.DEFAULT_HORIZON_DAYS
)


def agenda_asked (
	*,
	workspace: str | None,
	date: datetime.date | str | None = None,
	horizon_days: int | None = None,
	project: str | None = None,
) -> dict[str, typing.Any]:
	"""Return what the agenda asks every connection for.

	**Lifted out of the command so that something other than a person can ask it** (`#992`).
	Three surfaces build this request — here, `agendaRequest()` in `app.js`, and the `today`
	branch of `mcp/tools._listed` — and nothing compared them, which is how they came to ask
	three different questions of one function. The guard drives this; a copy of it inside a
	Typer closure could only ever be driven by running the command.

	**The horizon is passed rather than left to default.** `GET /v1/agenda` omits the
	`upcoming` bucket unless asked, because an agent asking "what is on today" means today; a
	person running `subroutine agenda` wants the week in front of them, and §12.2a's agenda
	has a heading for it.

	**Neither `date` nor `timezone` is sent, so §6.5's chain decides** — Simon's decision of
	2026-08-18, decision `#989`: *"a user should always see items displayed in their own local
	timezone, where possible."*

	This used to fill the chain's `explicit` slot with `world.settings.default_timezone`, a
	*client-machine* setting whose default is that machine's OS zone. **Its stated reason does
	not survive its own case** (`#995`): the argument was the merge, and with a work connection
	on America/New_York and a personal one on Europe/London the value sent is the *typing
	machine's* — a third answer matching neither. It did not resolve the ambiguity, it resolved
	it arbitrarily and said nothing.

	`agendaRequest()` in `app.js` had already decided the same question the other way, with the
	reason written down: *"`Intl` knows where the machine is; it does not know where the reader
	keeps their diary."*

	**So a genuine disagreement is reported rather than resolved** — :func:`_report_zones` — and
	the value the chain reads is one a person can set, which is `#994` and is why that blocked
	this.

	**It takes neither the world nor the clock any more**, and that is the change rather than a
	tidy-up: what a person's agenda is about stopped being a property of the machine they typed
	on. A parameter left behind for a value nothing reads is the shape `#303` went round this
	repository deleting.
	"""

	return {
		# **A day only when the caller named one** (`#1005`), which is exactly what `#995`
		# leaves room for: *no surface sends `date` unless the caller asked*. Unset, §6.5's
		# chain decides which day this is about, in the reader's own zone.
		"date": date,
		"horizon_days": (
			subroutine.domain.agenda.DEFAULT_HORIZON_DAYS
			if horizon_days is None
			else horizon_days
		),
		# `-w` narrows the agenda the same way it narrows every other listing. Unset spans
		# everything, which is what makes this one list rather than one per workspace
		# (§13.7) — the dentist and the stand-up belong in the same place. Naming a
		# workspace is how you ask for half of it.
		"workspace": workspace,
		# **One area of work, and everything under it** (`#1215`, `#320`). The browser gained
		# an agenda it can point at a project, and a capability on one surface and not another
		# is what §14.1 forbids and what this project keeps paying for — so both ask the same
		# function the same question. It needs a workspace beside it and the client refuses it
		# without one, because a project key is per workspace (§5.4).
		"project": project,
	}


@dataclasses.dataclass(frozen=True)
class Program:
	"""What a command has of the program around it: how to speak, and how to reach.

	**Every one of these used to be a name closed over by `register`** (`#943`, cold review
	`#927`'s L-1). Seven arrive as arguments and two are built from them, and a nested command
	simply saw all nine — which is what made 4,769 lines one function and every helper in it
	reachable only by running a Typer command.

	Named rather than threaded one callable at a time, because the count is the point: most
	helpers want one or two of these and the next surface wants a different one or two, so a
	per-function parameter list would be nine ways to be inconsistent. What a reader gets from
	the name is the answer to *where does this sentence go* — :attr:`say` for a line,
	:attr:`console` for something with style in it, :attr:`stop` and :attr:`fail` for the two
	kinds of ending — and that question was previously answered by scrolling.

	**`selected` is mutable and deliberately so**, which is why this is frozen and it is not:
	Typer resolves the callback's options before the command's, and the object is how the two
	meet. Freezing the box does not freeze what is in it.
	"""

	say: typing.Callable[[str], None]
	fail: typing.Callable[[subroutine.errors.SubroutineError], typing.NoReturn]
	stop: typing.Callable[..., typing.NoReturn]
	settings: typing.Callable[[], subroutine.config.Settings]
	console: rich.console.Console
	warn: typing.Callable[[str], None]
	mask: typing.Callable[[str], str]
	selected: Selected

	@contextlib.contextmanager
	def opened (self, *, strict: bool = False) -> typing.Iterator[World]:
		"""Yield every reachable connection, with the current context settled.

		One ``identity()`` per connection, fanned out — which is what resolves a workspace
		slug, prints an address and notices the same instance configured twice. It is one
		cheap query locally and one request remotely, and everything after it is narrower
		for having been asked.

		**Two connections naming one instance is recorded here and refused elsewhere**
		(`#942`). It used to be refused here, behind a ``merged`` flag saying whether this
		command combines what the connections answer — which asks the question before the
		command has done anything, so the answer had to be given thirty-eight times and was
		given three. See :meth:`World.merging` for what replaced it.
		"""

		resolved = self.settings()

		_warn_about_the_credentials_file(self.warn)

		try:
			roster = subroutine.connections.roster(resolved)
			marker = subroutine.directory.find()
			current = subroutine.context.resolve(
				roster,
				connection=self.selected.connection,
				workspace=self.selected.workspace,
				marker=marker,
			)

		except subroutine.errors.SubroutineError as error:
			self.fail(error)

		with contextlib.ExitStack() as stack:
			clients = []

			# **A connection that cannot even be built is a failure like any other** (`#175`).
			# It used to be warned about and then forgotten, so with one broken connection and
			# no others, `gathered.failures` was empty and the reader was told "No connection
			# could be reached. Run 'subroutine connections'" — a generic line naming a cause
			# that is not the cause, with the sentence explaining the real one already printed
			# above it and its remedy thrown away. Collected here, they reach the same report
			# as a connection that was reached and refused.
			unbuilt: list[subroutine.fanout.Failure] = []

			for connection in roster:
				try:
					clients.append(stack.enter_context(_client(connection, roster, resolved)))

				except subroutine.errors.SubroutineError as error:
					if strict:
						self.fail(error)

					unbuilt.append(
						subroutine.fanout.Failure(connection=connection, error=error)
					)

			# Both inside one handler, because under ``--strict`` the gather *re-raises* rather
			# than collecting — and a refusal that escapes to Typer arrives as a traceback,
			# which is exactly what `fail` exists to prevent. Found by stopping a server and
			# running the flag, which is the only way this shows up.
			try:
				gathered = subroutine.fanout.gather(
					clients, lambda client: client.identity(), strict=strict
				)

			except subroutine.errors.SubroutineError as error:
				self.fail(error)

			collision = subroutine.fanout.duplicate_instances(gathered.answers)

			reached = tuple(
				Reached(client=_matching(clients, answer.connection.name), identity=answer.value)
				for answer in gathered.answers
			)

			if not reached:
				# **Say what each connection actually said** (`#127`). Every failure here already
				# carries a sentence somebody can act on — `clients/local._reported` turns a
				# SQLAlchemy error into "local could not be read: no such column tasks.ref", with
				# a hint naming `database_url` — and this discarded all of it for a generic line
				# plus an instruction to go and check configuration that is perfectly fine. That
				# is worse than saying nothing, because it names a cause which is not the cause.
				#
				# The asymmetry that hid it: `_report` prints every failure beside the results
				# that *did* arrive, so one connection down out of three read well. It runs after
				# this context manager has yielded, so the case where everything failed — which
				# is what "my only connection is broken" looks like — never reached it.
				reasons = []

				for failure in (*unbuilt, *gathered.failures):
					reasons.append(failure.describe())

					# Indented under the connection it belongs to, so several broken connections
					# do not run their remedies together. Only here: beside a partial result a
					# hint per failure is noise, which is why `describe()` still omits it.
					if failure.error.hint is not None:
						reasons.append(f"  {failure.error.hint}")

				# **"Nothing could be read", not "no connection could be reached"**, once there
				# are reasons to print. A database at the wrong schema *was* reached — it is the
				# wrong shape — and the old line asserted a cause as confidently as the hint
				# did. The original wording is still right for the case it was written for,
				# which is having nothing to ask in the first place.
				self.stop(
					"Nothing could be read." if reasons else "No connection could be reached.",
					"\n".join(reasons)
					if reasons
					else "Run 'subroutine connections' to see what is configured.",
				)

			try:
				yield World(
					roster=roster,
					current=_settled(
							self,
						roster, _marker_taken_elsewhere(current, reached, marker), reached, marker
					),
					reached=reached,
					unreachable=(*unbuilt, *gathered.failures),
					settings=resolved,
					marker=marker,
					collision=collision,
				)

			except subroutine.errors.SubroutineError as error:
				self.fail(error)


@dataclasses.dataclass(frozen=True)
class Listing:
	"""One connection's rows, and whether there were more it did not return.

	``more`` exists because a truncated list that does not say so is worse than a short one:
	refs are how items are addressed, so the list is where a number is found, and a silent cut
	makes absence from the list stop meaning absence from the system.

	**A flag rather than a count, deliberately.** An exact "and 14 more" needs a second full
	scan per workspace per kind, on every listing, for a number that is only wanted in the
	uncommon case where the page filled — which is the same trade `?include_total=` makes by
	defaulting to off (§8.4). Asking for one row past the limit costs nothing and answers the
	question that actually changes what the reader does: *is this all of them?*
	"""

	rows: list[Row]
	more: bool = False

	#: How many open tasks were held back because their start date has not arrived. Carried
	#: rather than recomputed, because it is per connection and the report is not.
	parked: int = 0

	#: How many otherwise-startable tasks `--ready` held back because something they are
	#: filed under cannot start (`#1610`). Carried for ``parked``'s reason, and read off the
	#: instance's own answer rather than worked out here — the rule belongs to the server,
	#: and a client that re-derived it would be a second implementation of it.
	held_back: int = 0


# These live above every function that annotates with them. A module-level annotation is
# evaluated when the `def` runs, not lazily, so `Columns` referenced before its own
# definition raises `NameError` on import while mypy reports nothing — the same trap as
# `Item` above `World`, which cost an import failure on 2026-07-30.
def _column (
	values: typing.Iterable[str], *, drop_if_uniform: bool = True, alone_is_news: bool = True
) -> int:
	"""Return how wide a column must be, or zero when it would say nothing.

	**A column that says the same thing on every row says nothing**, whether what it says is
	a word or is nothing at all. Both cases collapse to one test: fewer than two distinct
	values and the column does not earn its place.

	This is the generalisation of ``shaping.aligned``'s empty-column rule — that drops a
	column nothing fills, this also drops one everything fills identically. It is what lets
	the item type, the priority and the estimate be shown at all: a personal to-do list is
	ordinary undated tasks with no priorities, so it gets none of them and looks exactly as
	it did before they existed. A mixed backlog gets all three, which is the case they were
	asked for — with bugs, features and spikes in one list, ranked, what kind of thing
	something is and how it is ranked are the first two things you want.

	That is §1.4 falling out of a layout rule rather than being enforced by one: the columns
	appear when the data has something to say and are invisible to somebody keeping a to-do
	list.

	**``drop_if_uniform=False`` is for a column whose uniform value is itself news** (`#511`).
	The rule above collapses two opposite states when the default is blank: "nobody has been
	assigned any of this" and "one person has been assigned all of it" are both a single
	distinct value, so both render as no column — and the second reads as the first. For the
	type or the priority that conflation cannot arise, because there is no reading of a
	uniform `bug` that means its own absence. The assignee is the column where it can, and
	`#511` exists because delegation was invisible; a rule that hides it again exactly when
	everything is delegated would reintroduce the defect at its worst moment.

	The cost is one redundant column on `list --assignee jo`, which is visible, cheap, and
	the right way round: a column somebody can see is not a wrong answer.

	**A page of one row keeps what somebody filled in** (`SR#1715`). The rule above is a
	statement about *contrast between rows*, and one row has no contrast to lose — every column
	holds exactly one distinct value, so the unguarded test dropped all of them. Measured on the
	same item seconds apart: ``search "the"`` gave ``#5  !4/2  2h  Cache the roster`` and
	``search "roster"`` gave ``#5  Cache the roster``. **The one-row page is the lookup page** —
	`#873` made ``search <ref>`` return one item on purpose — so the page most likely to be
	acted on was the one that said least.

	**``alone_is_news=False`` is the half that keeps §1.4 intact, and driving it is what found
	the collision.** Lifting the rule for every column printed ``#1  task  inbox  ordinary
	work`` on a fresh instance's only item — which is this function's own defining case, and
	`#512`'s decision of 2026-08-05 reversed without anybody being asked. The difference is not
	the row count: it is that a priority, an estimate or a state renders *blank* when nobody
	chose one, so the empty rule below already covers them, while a type and a project render a
	**default word**. A default nobody chose is not a fact about this item, which is ``show``'s
	rule in ``_facts`` said in a layout function.

	**``_tabulated`` had already met the one-row case and fixed it in place**, guarding its own
	call with ``len(rows) == 1``; the rule it was working around lived here and was left
	standing for every other caller. One rule applied to one of two callers is this codebase's
	signature defect, and the remedy is to answer it where the rule is.
	"""

	# Materialised because how many rows there are is now part of the answer, and a generator
	# can only be counted by consuming it.
	values = list(values)
	distinct = set(values)

	# **A column nothing fills says nothing however many rows there are**, so this half is
	# unconditional — including on the single row, where a blank cell is still a blank cell.
	if not any(distinct):
		return 0

	if drop_if_uniform and len(distinct) < 2 and (len(values) > 1 or not alone_is_news):
		return 0

	return max(len(value) for value in distinct)


def _tabulated (rows: typing.Sequence[typing.Sequence[str]]) -> list[str]:
	"""Lay rows out as aligned columns, dropping any that say the same thing throughout.

	``shaping.aligned`` does this for the API's compact format, and importing it here would
	pull ``fastapi`` into the start-up of every command — the cost `serve` goes out of its way
	to avoid. So this is `_column`'s rule applied to a plain table, which is the part that
	matters: on a one-person instance ``user list`` prints a name and nothing else, because
	"person", "not an admin" and "active" on the only row are three ways of saying nothing.
	"""

	if not rows:
		return []

	# **The first column is never dropped.** A page of one row keeping whatever it filled in is
	# `_column`'s own answer since `SR#1715`; this guarded its own call for it and left the rule
	# wrong for every other caller, which is what that item is. The name is a separate rule and
	# stays here: it is the only cell that must appear even when it is blank.
	total = max(len(row) for row in rows)
	widths = [_column(row[index] for row in rows) for index in range(total)]
	widths[0] = max(len(row[0]) for row in rows)

	lines = []

	for row in rows:
		cells = [
			value.ljust(widths[index])
			for index, value in enumerate(row)
			if widths[index]
		]

		lines.append("  ".join(cells).rstrip())

	return lines


@dataclasses.dataclass(frozen=True)
class Columns:
	"""How much room each cell needs on this page. Zero means the column is not shown.

	Measured once for the whole page rather than per row, because "is this list all one kind
	of thing?" is a question about the page — and measured across every bucket of an agenda
	and every connection of a grouped listing, so the addresses line up down the whole output
	rather than stepping in and out as the sections change.
	"""

	address: int = 0
	kind: int = 0
	state: int = 0
	blocked: int = 0
	sub_tasks_done: int = 0
	priority: int = 0
	estimate: int = 0
	matched: int = 0
	parent: int = 0
	assignee: int = 0
	size: int = 0
	project: int = 0

	#: What was searched for, so a row can say where it was found. ``None`` on any listing
	#: that was not a search, which is what drops the column entirely.
	term: str | None = None

	#: The address prefix every row on this page shares *because the request named it*, so a
	#: project label can leave it out — decision `#957` §4. Empty on an unfiltered listing.
	within: str = ""

	@classmethod
	def measured (
		cls,
		world: World,
		rows: typing.Sequence[Row],
		*,
		term: str | None = None,
		project: str | None = None,
	) -> "Columns":
		"""Return the widths this page needs."""

		within = _asked_within(project)

		# Built once rather than per use: it decides both the width and, on a page of one row,
		# whether the column is worth drawing at all.
		matched = [_match_cell(item, term) for _name, item in rows]

		return cls(
			term=term,
			within=within,
			# **Not on a page of one row either** — `#512`, Simon's decision of 2026-08-05,
			# which weighed showing a new reader where things go against §12.2a and chose
			# §12.2a. Nothing here reports whether a project was *chosen* or is the Inbox
			# everything lands in, so lifting the rule for one row would say `inbox` to the
			# reader that decision is about. `show` is where one item's project is named.
			project=_column(
				(_project_cell(item, within) for _name, item in rows), alone_is_news=False
			),
			# **On one row, only where the reader cannot already see it.** The title wins when
			# both match, so `title` beside a title visibly holding the word says nothing —
			# and `description` on the same page is the whole reason this column exists.
			matched=_column(
				matched, alone_is_news=any(one != VISIBLE_MATCH for one in matched)
			),
			parent=_column(_parent_cell(item) for _name, item in rows),
			address=max(
				(len(world.address_of_item(name, item)) for name, item in rows), default=0
			),
			# **News on its own only when somebody chose it.** On one row there is nothing to
			# contrast against, so the question becomes whether the type is the workspace's
			# default — which the view reports, unlike a project's.
			kind=_column(
				(item.type for _name, item in rows),
				alone_is_news=any(not item.type_is_default for _name, item in rows),
			),
			state=_column(_state_cell(item) for _name, item in rows),
			blocked=_column(_blocked_cell(item) for _name, item in rows),
			sub_tasks_done=_column(_sub_tasks_cell(item) for _name, item in rows),
			priority=_column(_priority_cell(item) for _name, item in rows),
			estimate=_column(_estimate_cell(item) for _name, item in rows),
			assignee=_column(
				(_assignee_cell(item) for _name, item in rows), drop_if_uniform=False
			),
			size=_column(_size_cell(item) for _name, item in rows),
		)


#: Marks work somebody is in the middle of. **A word, not a symbol**: decision `#102` says no
#: information exists only in a colour, and the same argument retires a bare glyph — a reader
#: meeting `▶` has to be told what it means, where a reader meeting `doing` does not.
#:
#: Not the word "status", which §13.5b forbids on this path and which nobody needs: `start` and
#: `stop` are actions that happen to set a field, exactly as `done`, `plan` and `defer` are.
STARTED_MARK = "doing"

#: Marks work that is over — `#874`. Same argument as :data:`STARTED_MARK` for being a word,
#: and the same column because the two states are exclusive: an item is in the middle of
#: something or it is finished, never both, so a second column would be empty wherever this one
#: is not and would cost width on every listing to say so.
FINISHED_MARK = "done"


def _state_cell (item: Item) -> str:
	"""Return where a task is in its life — in progress, finished, or nothing to say (`#75`).

	**A `start` command whose effect is invisible is half a feature.** The status was reachable
	only over HTTP until now, and adding a way to set it without a way to see it would have
	moved the gap rather than closed it.

	**Finished work joined it in `#874`, and the gap was made reachable by `#873`.** A terminal
	listing used to show a finished task only when the reader had explicitly filtered on
	completion — and somebody who asks about completion does not need telling. Then a bare
	`search <ref>` began surfacing finished items by design, 548 of this instance's 721 tasks,
	and they arrived beside open ones looking identical. `#102`'s rule is that a distinction a
	reader has to learn in order to tell it from a defect *is* one.

	**`views.status_is_news` said this was already handled and was wrong about this surface.**
	Its reason for staying quiet about finished work was that *"a completion has a better
	rendering on every surface"* — true of `show`, true of the browser's row, and untrue here,
	where the marks were `doing`, `blocked` and `holds up` and there was no fourth. That
	sentence is corrected rather than left as prose asserting a completeness nothing checked.

	**Tasks only, and that is `#841`'s measurement rather than laziness.** A document's
	categories are `draft`, `current`, `superseded` and `archived`; `draft` is its default and
	`active` is not, so 111 of 122 documents here carry a status worth remarking on against 2
	of 172 tasks. A rule that is a signal on one population is noise on the other, and marking
	every document would put a word on nine rows in ten. A superseded document is worth marking
	and is a different judgement, not this one.

	Empty on every row of an ordinary list, which drops the column entirely — the same rule the
	kind, priority and parent columns follow, and what keeps a personal to-do list from looking
	like a database (§1.4, §14.10).
	"""

	if not isinstance(item, subroutine.views.Task):
		return ""

	if item.status_category == "in_progress":
		return STARTED_MARK

	# **The third state, and the first read by *key* rather than by category** (`#1383`).
	# `views.waiting_on_a_person` carries why there is no category to ask for, and what a
	# workspace renaming the key gives up. Above the completion test because the two cannot
	# both be true — a `needs_input` row is `todo` — and below `in_progress` for the same
	# reason, so the order is documentation rather than precedence.
	if subroutine.views.waiting_on_a_person(item):
		return WAITING_MARK

	return FINISHED_MARK if item.status_category == "done" else ""


#: Marks work something unfinished is in the way of — item `#425`, and its mirror `#569`.
#:
#: **The words live in :mod:`subroutine.views` now** (`#913`), because the agent's listing wrote
#: the same two out again and nothing compared them. Named here so this module reads as it did.
#:
#: **A word rather than a glyph, for `STARTED_MARK`'s reason**: decision `#102` says no
#: information exists only in a colour, and the same argument retires a bare symbol.
#:
#: Not §13.5b words — a person who has never linked two items sees neither, because the column
#: is dropped when no row on the page carries one.
BLOCKED_MARK = subroutine.views.BLOCKED_MARK
BLOCKING_MARK = subroutine.views.BLOCKING_MARK

#: Marks a row somebody has to answer something about — `#1383`, and it shares
#: :data:`STARTED_MARK`'s column because the three states are exclusive.
#:
#: **Not a §13.5b word, and it does not need to be one.** *Needs input* is the seeded status's
#: own title and Simon's own sentence; a reader who has never parked a question never sees it,
#: because the column is dropped when no row on the page carries one.
WAITING_MARK = subroutine.views.WAITING_MARK

#: Marks an unfinished parent whose sub-tasks are all done — `#1615`, and **its own column
#: rather than :data:`BLOCKED_MARK`'s**.
#:
#: The item's own evidence decides that: a stale parent is usually the thing holding the next
#: milestone up, so it already reads `blocker` there — *"which describes what it does to
#: others, not that it is holding them back for no reason"*. One column would print whichever
#: won and lose the other, and losing this one is the defect being fixed.
#:
#: Not a §13.5b word: *sub-task* is `#84`'s own, and every surface already heads that section
#: with it. A reader who has never made one never sees this, because the column is dropped when
#: no row on the page carries it.
SUB_TASKS_DONE_MARK = subroutine.views.SUB_TASKS_DONE_MARK


def _sub_tasks_cell (item: Item) -> str:
	"""Return the marker for a parent whose sub-tasks are all done, or nothing — `#1615`.

	**`#84` refuses auto-completion and leaves the question to a person; nothing was putting
	it.** Complete every sub-task of a parent and the parent stays open, anything it blocks
	stays blocked, and the listing shows an ordinary open row. The failure is silent and
	delayed — the person best placed to notice has already moved on.

	Empty on every row of an ordinary list, which drops the column entirely, exactly as the
	kind, state, blocked and priority columns do (§1.4, §14.10).
	"""

	if not isinstance(item, subroutine.views.Task):
		return ""

	return SUB_TASKS_DONE_MARK if item.sub_tasks_done else ""


def _blocked_cell (item: Item) -> str:
	"""Return the marker for work that cannot be started yet, or nothing (`#425`).

	**A filter is not a signal, and the default listing is the one somebody reads.**
	``--ready`` has excluded blocked work since `#69`; the list you get by typing nothing
	showed a blocked item above the thing blocking it with no way to tell. Reported by an agent
	that read a default listing as "start with #2" — and `#69` itself recorded the same
	observation about `#57` and `#58` a week before shipping only the filter half.

	**The marker rather than the ordering**, which is what was asked for and is also right: the
	default order is newest-first, and reordering by readiness would make the list answer a
	question it was not asked. A row that says why it is not first costs nothing and stays true
	under every order.

	**One column for both directions, and ``blocked`` wins when a row is both** (`#569`). A
	middle link in a chain is held up *and* holding something up; the first is what decides
	whether you can act, so it is what the cell says. Two columns would be two mostly-empty
	ones, and §12.2a's rule is that a column saying the same thing on every row says nothing.
	The other half is a call away — `subroutine show` lists every link either way.

	Empty on every row of an ordinary list, which drops the column entirely — the same rule the
	kind, state and priority columns follow (§1.4, §14.10).
	"""

	if not isinstance(item, subroutine.views.Task):
		return ""

	if item.blocked:
		return BLOCKED_MARK

	return BLOCKING_MARK if item.blocking else ""


def _priority_cell (item: Item) -> str:
	"""Return §6.3's two axes as one self-describing cell, or nothing when neither is set.

	Written ``!4/2``, marked with the same ``!`` that *sets* importance in a captured line, so
	the cell says what it is wherever the column happens to land. §14.10 already paid for that
	lesson on the compact line: dropping a column moves every later cell, so a bare number
	beside a title is one a reader has to work out from position.

	**An unset axis is ``?`` rather than blank**, so ``!4/?`` reads as half-ranked rather than
	as unranked. That distinction is not cosmetic — ``priority_score`` is null unless *both*
	are set and every ordering is NULLS LAST, so a half-ranked item sinks to the bottom of a
	ranked list looking exactly like one judged unimportant. It happened to this project's own
	backlog for a day. The cell is where that becomes visible.
	"""

	if not isinstance(item, subroutine.views.Task):
		return ""

	if item.importance is None and item.urgency is None:
		return ""

	return f"!{item.importance or '?'}/{item.urgency or '?'}"


#: Marks a parent's ref on a listing row. **`^` because everything else is taken** — `#` is
#: a ref, `!` is priority, `~` an estimate, `+` a project and `@` an assignee, all claimed by
#: §6.13's capture grammar. It reads as "up", which is the relationship.
PARENT_SIGIL = "^"


def _asked_within (project: str | None) -> str:
	"""Return the address prefix a project label may leave out, because the request said it.

	**The ``--project`` filter alone, and deliberately not the checkout's context.** Decision
	`#957` §4 named both; building it showed the second is wrong here, because §13.7 makes the
	context direct *writes* and never narrow a read. A `.subroutine` marker naming
	``subroutine`` does not stop ``subroutine list`` showing personal items, so stripping by it
	would drop a segment the reader needs from rows the request never scoped.

	**Only what was typed, which is the whole of it.** ``--project subroutine`` filtering to
	``subroutine/ui`` and ``subroutine/spec`` leaves ``ui`` and ``spec``; ``--project ui``,
	which resolves by *search* to ``subroutine/ui``, matches no prefix and so strips nothing —
	the row shows its whole address, which is longer than it needs to be and is visible. That
	is the right way round: the alternative is stripping a prefix nobody named.
	"""

	return "" if not project else subroutine.domain.projects.normalize_path(project)


def _project_cell (item: Item, within: str) -> str:
	"""Return where an item lives, as much of its address as the request did not already say.

	**The whole rule is: full address, strip what was asked for, then §12.2a** — and only the
	first two steps are here, because dropping a uniform column is :func:`_column`'s job and
	is measured across the page rather than per row.

	So an ordinary to-do list shows nothing: every item is in the Inbox, the remainder is
	``inbox`` on every line, and the column does not earn its place. That is `#512`'s
	2026-08-05 decision working unchanged — Simon chose consistency with §12.2a over showing
	a new reader where things go — narrowed by `#957` only in *what* the rule is applied to.

	**The workspace is not in this cell**, though `#957`'s table puts it in the browser's
	label. It is already in the address column: ``World.address_of`` prints ``acme/#42`` for a
	row in another workspace and a bare number for one here, so naming the workspace again
	would say it twice on every line that needed it and never on the lines that did not.
	"""

	path = item.project_path

	if not path or not within:
		return path

	# **Only on a segment boundary**, because `removeprefix` on an address is otherwise wrong
	# in general: `--project ui` against a row in `ui-things/x` would leave `-things/x`, which
	# is not an address of anything. The server has already narrowed a filtered listing to the
	# subtree, so no supported path reaches that — this is one condition rather than a comment
	# saying it cannot happen, which is what `#303` is about.
	separator = subroutine.domain.projects.PATH_SEPARATOR

	if path == within:
		return ""

	inside = f"{within}{separator}"

	return path.removeprefix(inside) if path.startswith(inside) else path


def _parent_cell (item: Item) -> str:
	"""Return the parent's ref as ``^57``, or nothing when the item is top-level.

	**A column rather than indentation, and that is the whole design** (`#63`). A listing is
	ordered by recency or by priority, so a child is rarely next to its parent — drawing
	``└─`` under an unrelated row states a relationship that is not there. A ref is true
	wherever the row lands.

	It costs nothing: `parent_ref` is on the view, batch-loaded with the status and project
	names, so this is a field read rather than a query per row.

	**Not marking a parent as having children**, deliberately. That needs a count per row, and
	that is the N+1 `#39` was spent removing. A child pointing up is enough to see the
	structure and is cheaper than every parent pointing down.
	"""

	if not isinstance(item, subroutine.views.Task) or item.parent_ref is None:
		return ""

	return f"{PARENT_SIGIL}{item.parent_ref}"


def _append_title (line: rich.text.Text, title: str, term: str | None) -> None:
	"""Append a title, marking where a search term matched it (`#103`).

	**A highlight, never an encoding** (decision `#102`). The `matched` column already says
	*which field* the hit was in, so this adds nothing a reader needs and only saves them
	scanning a long title for the word — which is exactly what colour is good at and what §12.2a
	means by marking an exception. Piped, or under ``NO_COLOR``, the styles fall away and the
	answer is unchanged.

	**Spans, never markup.** A title is user data and is rendered through ``rich.text.Text``
	precisely so that a literal ``[bold]`` somebody typed prints rather than obeys; building a
	marked-up string here would reopen that. ``Text.stylize`` takes offsets and cannot.

	Case-insensitively, to match the ``ilike`` that selected the row. Every occurrence is marked
	rather than the first: a title matching twice and highlighted once reads as though the
	program found something the reader cannot see.
	"""

	start = len(line)
	line.append(title)

	if not term:
		return

	wanted = term.casefold()
	folded = title.casefold()
	at = folded.find(wanted)

	# `casefold` can change a string's *length* — 'ß' folds to 'ss' — so an offset found in the
	# folded text is not always an offset into the original. Skip the highlight rather than
	# stylise the wrong characters; the row and its `matched` cell are correct either way.
	if len(folded) != len(title):
		return

	while at >= 0:
		line.stylize(MATCH, start + at, start + at + len(wanted))
		at = folded.find(wanted, at + len(wanted))


#: The answer :func:`_match_cell` gives when the word is in the part of the row already on
#: screen. It is the one answer that tells a reader nothing they cannot see, which is what makes
#: it the value a page of one row drops (`SR#1715`).
VISIBLE_MATCH = "title"


def _match_cell (item: Item, term: str | None) -> str:
	"""Return where a search term was found, or nothing when no search was made.

	**A hit whose reason is invisible reads as a bug.** Searching this project for
	"pagination" returns document `#4`, whose title says "Subroutine MVP plan and delivery
	record" — with nothing to say why, the honest reading of that row is that the search is
	broken. Naming the field is the smallest thing that turns it back into an answer.

	The title wins when both match, because it is the part already on the row: saying
	`description` beside a title that visibly contains the word would send the reader looking
	for a second occurrence.

	Case-folded here to match the ``ilike`` that selected the row. The two can still disagree
	on non-ASCII — Python's ``casefold`` is more thorough than either database's ``LOWER`` —
	and the cost of that is a blank cell on a row that did match, which is why this returns
	empty rather than guessing.

	**Every word, not the whole query, and that is `#881`.** A search is *a set of words, all
	of which must appear, in any order and in any column* (`#620`), so testing the query as one
	contiguous substring answers a question nobody asked: `cursor pagination` did not match a
	title reading *"Pagination resumes from the wrong cursor row"*, and the row fell through
	every branch. Measured before the fix — of ten real multi-word searches returning 70 rows,
	**30% could not be explained at all** by the old test.

	**Two of the four answers are not fields, and `#870` is why they had to be added.**
	`#867` made a query that is exactly a ref match the item with that number, and `#83` made
	one match a comment on it. Neither is in a column this function can read, so both produced
	precisely the row this cell exists to prevent: a hit with no visible reason, which reads as
	a broken search. The number case was found in driving output within an hour of shipping it.

	**The last answer names the fact rather than the place, and the measurement is why.** It
	used to say ``comment``, which this function cannot prove: under the ``native`` backend a
	stemmed match is never a substring, so the check cannot model what selected the row at all.
	Measured on the served instance — of the rows that reached the fall-through, **a comment
	explained two and nothing explained three**, and two of those three had no comments on them.
	A cell whose only job is to explain a match was stating a false one three times in five.

	``elsewhere`` is true on both backends: *the match is not in what you can see*. That is the
	whole of what `#870` needed, and it is the only claim available without fetching every row's
	comments, which is `#39`'s N+1 on a listing. **``comment`` is recoverable under ``like``**,
	where matching is pure substring and a comment is the only *elsewhere* there is — and is
	deliberately not done, because a word a reader sees should not depend on configuration.
	`#840` is the version of this question a scripted caller needs, and is the real answer.
	"""

	if not term:
		return ""

	words = [word.casefold() for word in subroutine.domain.search.terms(term)]

	if not words:
		return ""

	# Exact, and stated first because it is the only answer here that is *certain*: a query
	# that is entirely a ref matched this row by its number whatever its prose says.
	if subroutine.domain.refs.parse_ref(term) == item.ref:
		return "number"

	title = item.title.casefold()

	if all(word in title for word in words):
		return VISIBLE_MATCH

	prose = (
		item.description if isinstance(item, subroutine.views.Task) else item.body
	) or ""
	folded = prose.casefold()
	named = "description" if isinstance(item, subroutine.views.Task) else "body"

	# **The prose answers for a spread match as well as for its own**, because the title is
	# already on the row: when the words are split across the two, what a reader cannot see is
	# the half that is worth naming.
	if all(word in folded for word in words) or all(
		word in title or word in folded for word in words
	):
		return named

	return ELSEWHERE


def _estimate_cell (item: Item) -> str:
	"""Return how long the work is thought to take, as a person would say it."""

	if not isinstance(item, subroutine.views.Task) or item.estimate_human is None:
		return ""

	return item.estimate_human


def _size_cell (item: Item) -> str:
	"""Return a mark for an item too big to read without meaning to — `#595`.

	One document on this instance is 128,083 characters, about 32,000 tokens, and its row was
	the same shape as a row for a three-word note. Nothing anywhere said so, on any surface, so
	the only way to find out was to read it and watch a context window go.

	**Only where it matters** (§12.2a): a column saying the same thing on every row says
	nothing, and almost every item here is a few hundred bytes. `shaping.aligned` drops a
	column that is empty in every row, so a personal list never sees this at all.

	Rounded to whole kilobytes, because the decision it informs is *should I open this* and no
	part of that turns on the last hundred bytes.
	"""

	if item.size_bytes is None or item.size_bytes < subroutine.domain.text.LARGE_PROSE:
		return ""

	return f"{round(item.size_bytes / 1000)}k"


def _assignee_cell (item: Item) -> str:
	"""Return who the work is with, or nothing when it is with nobody.

	**A to-do list nobody delegates on is unchanged** (`#511`): §12.2a drops a column that is
	empty in every row, so this costs a personal listing nothing and appears the moment one
	item is handed over. A document has no assignee and returns empty for the same reason it
	has no deadline — the field is not a task's alone by accident, it is what `Item` covers.
	"""

	if not isinstance(item, subroutine.views.Task) or not item.assignee:
		return ""

	return subroutine.views.principal_named(
		item.assignee,
		is_agent=item.assignee_is_agent,
		answers_to=item.assignee_answers_to,
	)


#: A page with nothing worth putting in a column, which is what a bare row looks like.
NO_COLUMNS = Columns()


@dataclasses.dataclass(frozen=True)
class Welcomed:
	"""What an instance said when a connection to it was checked, before it was recorded.

	Flattened out of the two calls that produced it, because the caller wants one sentence and
	neither answer alone can make it: the instance and its workspaces come from ``identity()``,
	and the name the credential authenticates as comes from ``me()``.
	"""

	instance: subroutine.views.Instance | None
	username: str
	workspaces: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Located:
	"""One item, and which connection and workspace it was found on."""

	connection: str
	workspace: str
	item: Item

	@property
	def ref (self) -> int:
		"""Return the number this item is addressed by."""

		return self.item.ref

	@property
	def title (self) -> str:
		"""Return what to call this item when naming it back to the user."""

		return self.item.title

	@property
	def entity_type (self) -> str:
		"""Return which kind of item this is, in the vocabulary the API uses."""

		return "task" if isinstance(self.item, subroutine.views.Task) else "document"


#: What an event is about, when it is about the model rather than about an item. Only these
#: two arise: everything else an event names has a title, which is what the line prints. §13.5b
#: forbids both words on this path, and ``subroutine init`` writes one of each.
_AN_EVENT_ABOUT = {"workspace": "this list", "workspace_member": "your account"}

# --- Helpers that need nothing from the command closure -------------------------------
#
# **These were nested inside `register` and closed over nothing at all** (`#943`, cold
# review `#927`'s L-1). Measured by walking the tree for references to the seven arguments
# and the two objects built from them: twenty-four functions, 821 lines, none of which
# touched any of them. Nested, they were reachable only by running a Typer command; here a
# test can call one directly, which is the whole of what this move buys.


def _marker_taken_elsewhere (
	current: subroutine.context.Current,
	reached: typing.Sequence[Reached],
	marker: subroutine.directory.Marker | None,
) -> subroutine.context.Current:
	"""Find a dropped marker's workspace on whichever connection actually holds it — `#556`.

	**A connection name is each machine's private alias, and a shared checkout makes it
	shared** (`#330`). Two machines mounting one filesystem read one `.subroutine`, so the
	nickname has to agree — and when it does not, the *whole* marker stopped directing, not
	just the connection: `context.resolve` drops the workspace with it, because a workspace
	*slug* means nothing on an instance that has never heard of it.

	The id does. `workspace_id` has been in every marker since `#317` and is a uuid, so
	asking which reached connection holds it is a question with one answer or none.

	**By id, never by name, and `#414` is why.** That finding is a marker whose connection
	was dropped matching its project by *key* on whichever instance answered, and filing
	work into a same-named project somewhere else. A key is a claim that can be true twice;
	a uuid is not. So this widens `Marker.speaks_for` in exactly one direction and leaves
	the name test alone.

	**Nothing reachable, nothing changes** (`#166`): this runs after the fan-out that had to
	happen anyway, so it adds no request, and with nothing to ask it returns what it was
	given. Several matches — which `#327` made a state somebody can be in — also return
	unchanged, because refusing to guess is the whole point of using an id.
	"""

	if current.unusable_marker_connection is None or marker is None:
		return current

	if marker.workspace_id is None:
		return current

	holding = [
		item
		for item in reached
		if any(str(row.id) == marker.workspace_id for row in item.identity.workspaces)
	]

	if len(holding) != 1:
		return current

	found = holding[0].name

	return dataclasses.replace(
		current,
		connection=found,
		connection_source=subroutine.context.FROM_DIRECTORY,
		marker_found_on=found,
		workspace=marker.workspace,
		workspace_source=subroutine.context.FROM_DIRECTORY,
	)


def _writing_workspace (world: World) -> str:
	"""Return the workspace a write lands in, refusing when nothing has said which."""

	if world.current.workspace is not None:
		return world.current.workspace

	here = world.writing_to()

	subroutine.context.refuse(
		world.roster,
		world.current,
		[workspace.slug for workspace in here.identity.workspaces],
	)


#: What a new account may do in the workspace it is put into, when nobody said. `#587`
#: decision 1: the ordinary case is the shortest thing to type (§1.4). ``--role`` still
#: narrows to ``viewer`` or widens to ``admin``, and both are unchanged.
#:
#: Not defaulted on ``user add``, deliberately, and the two are not in disagreement. There
#: the role *is* the decision being taken — somebody already has an account and is being
#: given a second workspace — so a default would take it quietly. Here it is one clause of
#: onboarding a colleague, and the ordinary answer is the only one most instances ever use.
ONBOARDING_ROLE = "member"


def _onboarding_workspace (where: Reached, named: str) -> str:
	"""Return the workspace a newly created account joins, or refuse — item `#587`.

	**Decision 2: it defaults when there is exactly one and is required when there are
	several.** Do not ask a question with one answer — :func:`_in_place` already applies that
	rule when it says nothing about *where* if there is only one place it could be.

	**The rule self-adjusts across eras**, which is why it needs no setting and no revisiting:
	silent on a fresh instance, because a workspace nobody chose is not a choice; insistent on
	an instance with several, because by then somebody made the others deliberately. The
	instance grows into it.

	**The population is what this credential can reach, not what exists** (`#1418`). That is
	correct behaviour today and is inherited rather than chosen: while a superuser does not
	reach a workspace they are not a member of, *how many workspaces are there* is answered
	per caller. Worth knowing before reading the refusal as a complete list.
	"""

	wanted = named.strip()

	if wanted:
		return wanted

	reachable = [workspace.slug for workspace in where.identity.workspaces]

	if len(reachable) == 1:
		return reachable[0]

	if not reachable:
		raise subroutine.errors.ValidationError(
			"This credential does not reach any workspace, so there is none to put them in.",
			code="missing_field",
			hint="Whoever administers this instance can add you to one; until then there is "
			"nowhere for a new account to work.",
		)

	raise subroutine.errors.ValidationError(
		f"There are {len(reachable)} workspaces here, so say which one they work in.",
		code="missing_field",
		errors=[
			subroutine.errors.FieldError(
				field="workspace",
				code="missing_field",
				message=f"Workspaces you can reach: {', '.join(reachable)}.",
			)
		],
		hint=f"For example: --workspace {reachable[0]}. They can be added to the others "
		f"afterwards with 'subroutine user add'.",
	)


def _handover_address (where: Reached, settings: subroutine.config.Settings) -> str | None:
	"""Return the address to give somebody else for this instance, or ``None`` — item `#587`.

	Two sources and they answer for two different operators. Over a **remote** connection the
	address is the one this machine is already reaching, which is by construction an address
	that works from somewhere other than the server. On a **local** connection the operator is
	on the server itself, and the only thing there that speaks for anybody else is
	``public_url`` — the operator saying *this is where people reach me*.

	**Deliberately not :func:`subroutine.config.browsable_url`, and the difference is the whole
	of this function.** That one answers *where can a browser reach this instance*, and its
	middle branch returns the bind when the bind is loopback — correct, because nothing off the
	machine can reach a loopback socket, so the address it listens on is the entire set of
	places the reader can browse. **A handover asks a different question**: where can *somebody
	else* reach it. On that question a loopback address is not merely unhelpful, it is wrong in
	the worst way — it resolves on the colleague's own machine, where it either fails or, on a
	laptop that also runs one, quietly reaches their instance instead of this one.

	``None`` is a real answer and is said out loud rather than papered over. An instance nobody
	else can reach has been set up perfectly well; what is missing is one setting.
	"""

	told = (where.client.connection.url or "").strip()

	if told:
		return told.rstrip("/")

	published = (settings.public_url or "").strip()

	return published.rstrip("/") if published else None


def _connection_suggestion (where: Reached) -> str:
	"""Return a nickname for this instance that somebody could type — item `#587`.

	**The name is the reader's and nobody else's**, which is what ``connections add`` says
	about it: it becomes the first segment of every address they write, and two people
	reaching one server may call it different things. So this is an example to adapt rather
	than a value to copy, and the instance's own label is the best guess available.

	Checked against the real rule rather than a second copy of it, so a server whose label has
	a space or begins with a digit falls back instead of producing a line that would be
	refused when it was run.
	"""

	instance = where.identity.instance
	label = (instance.name if instance is not None else "").strip().lower().replace(" ", "-")

	try:
		return subroutine.connections.check_name(label)

	except subroutine.errors.SubroutineError:
		return "work"


def _terminal_handover (
	secret: str, *, username: str, address: str | None, nickname: str
) -> list[str]:
	"""Return what to print when a colleague's own credential has just been made — `#587`.

	**Not ``token create``'s closing line, and the difference is who is holding the token.**
	That one is written for the operator's own machine and offers the environment variable and
	the credentials file. This one is written to be *forwarded*: the reader is about to send
	both halves to somebody who has to set an instance up they have never seen.

	The secret comes first and the instruction second. If a terminal scrolls, the half that
	cannot be recovered is the half worth having at the top.
	"""

	# **Announced, because it is shown once and it may not be the only secret on screen**
	# (`#1442`). With ``--browser --terminal`` a sign-in link and a credential arrive in one
	# stream; the link says what it is and this did not, so a reader handing both over had to
	# tell them apart from context. They are not interchangeable — one goes in a browser and
	# one goes in a configuration file — and there is no reading it back later to check.
	said = [
		"",
		f"A credential for {username}, for the command line.",
		"",
		secret,
		"",
		"That is the only time it is shown. Nothing recovers it afterwards.",
		"",
	]

	if address is None:
		# **Refusing to guess, rather than printing the bind.** A loopback address in a
		# handover resolves on the colleague's own machine, where it either fails or — far
		# worse on a laptop that also runs one — reaches their instance instead of this one.
		#
		# **Named on ``public_url`` rather than on the absence** (`#1442`). A sign-in link
		# printed moments earlier says *nobody has set public_url, so that address is where
		# this instance listens*, and this used to open *this instance has no address anybody
		# else can reach* — one installation described as having an address and not having
		# one, in two paragraphs. Both were true and they answer different questions; naming
		# the same cause is what lets a reader see that.
		said.append(
			"There is no line to send with it: nobody has set public_url, so this instance "
			"has no address anybody else can use. Setting it gives both a real one."
		)

		return said

	said.extend(
		[
			"Send it with this, which they run on their own machine:",
			"",
			f"  subroutine connections add {nickname} --url {address}",
			"",
			f"It asks for the credential, so {nickname} is theirs to change — it becomes the "
			f"first part of every address they write.",
		]
	)

	return said


def _what_is_still_needed (username: str) -> list[str]:
	"""Return the two commands that hand a new account over — item `#587`.

	**Decision 3: naming no path succeeds and signposts.** The account and the role are real
	work and it succeeded, so this is ``init``'s shape — do the thing, then say what to try
	next — rather than ``db restore``'s refusal, which is right when both defaults are wrong
	and is answering a question nobody asked once the two options are not exclusive.

	Both are named, never one. Decision 4 is that the paths are additive because somebody may
	genuinely need both: a link for the browser, and a credential for whoever is configuring
	their machine.
	"""

	return [
		"",
		"They cannot get in yet. Either of these hands it over, and both is fine:",
		"",
		f"  subroutine login link --username {username}      a sign-in link for the browser",
		f"  subroutine token create --username {username}    a credential for the terminal",
		"",
		"Both are what --browser and --terminal would have done here.",
	]


def _a_superuser_joins_nothing () -> subroutine.errors.ValidationError:
	"""Return the refusal for a superuser given a workspace or a role — item `#587`.

	**Refused rather than resolved**, which is `agent create`'s rule for a profile combined
	with a flag that means something else: a combination meaning two things at once is turned
	down by name instead of one half being picked silently.

	Decision 1 special-cases ``--superuser`` to join no workspace, because an instance owner
	needs no workspace role and granting one quietly would be a permission taken by default.
	So a role or a workspace beside it is not a narrower superuser — it is somebody asking for
	two different accounts in one command.
	"""

	return subroutine.errors.ValidationError(
		"--superuser administers the whole instance, so it joins no workspace and takes no "
		"role.",
		code="invalid_field_value",
		hint="Make the account with --superuser, then 'subroutine user add' if they should "
		"also work in a particular workspace.",
	)


def _an_agent_has_no_browser () -> subroutine.errors.ValidationError:
	"""Return the refusal for a machine identity asked for a sign-in link — item `#587`.

	**The rule belongs to the domain and this is not a second copy of it.**
	:mod:`subroutine.domain.sessions` refuses a session for a service account because a
	credential carries a scope and a reach and a session carries neither — that is a fact
	about an account, checked where it is stored. This is a fact about two *flags*, checked
	before anything is written, and the two answer different questions.

	**Which matters because of when the domain's refusal arrives.** Driven: without this the
	account is created, joined to a workspace, and *then* turned down for the link — leaving a
	half-made identity and a command that refuses on the retry because the name is taken. The
	rule was right and it fired one step too late to be acted on.
	"""

	return subroutine.errors.ValidationError(
		"An agent cannot sign in to a browser, so --agent and --browser mean two different "
		"things.",
		code="invalid_field_value",
		hint="Use --terminal for a credential it can present, or 'subroutine agent create', "
		"which issues one and says how to hand it over.",
	)


def _handed_over (
	where: Reached,
	settings: subroutine.config.Settings,
	username: str,
	*,
	browser: bool,
	terminal: bool,
	workspace: str | None,
) -> list[str]:
	"""Mint whatever was asked for and return what to say about it — item `#587`.

	**Decision 4: the two are additive rather than exclusive**, and a real person forced it.
	Somebody who uses the web interface *and* has a colleague configuring their machine needs
	a link and a credential; a command framed as either/or refuses the one person who needs
	both.

	**Minted inside the caller's open connection, and printed outside it.** A secret that
	exists and was never shown cannot be recovered — only a hash is kept (§7.4) — so nothing
	here is allowed to fail between the mint and the line that carries it.
	"""

	if not (browser or terminal):
		return _what_is_still_needed(username)

	said: list[str] = []

	if browser:
		link = where.client.create_login_link(username=username)
		said.append("")
		said.extend(
			subroutine.cli.output.sign_in_lines(
				username=link.username,
				url=link.url,
				minutes=subroutine.cli.output.minutes_until(
					link.expires_at, subroutine.db.types.utcnow()
				),
				address_assumed=link.address_assumed,
			)
		)

	if terminal:
		# **Pinned to the workspace they were just put in, and that is not a narrowing.** On
		# the day it is issued the pin reaches everything the account reaches, because this
		# command put them in exactly one workspace. What it buys is `#571`: on a credential
		# reaching more than one workspace with no pin, `subroutine://meta` and
		# `subroutine://conventions` answer with an explanation instead of content — so an
		# agent told that a document binds it cannot read it. `#1386`'s pre-flight list says
		# to pin every issued connection by hand, and a step somebody has to remember is one
		# they will not.
		#
		# A superuser has no workspace, and passing ``None`` correctly leaves the credential
		# reaching all of them.
		minted = where.client.issue_token(
			username=username, title=f"{username} at the terminal", workspace=workspace
		)
		said.extend(
			_terminal_handover(
				minted.token,
				username=username,
				address=_handover_address(where, settings),
				nickname=_connection_suggestion(where),
			)
		)

	return said

def _kept (held: int) -> str:
	"""Say how many items survive a rename, with the verb and the possessive agreeing.

	**One sentence, one place, because both rename commands print it** (`#296`). They had a
	copy each and the copies disagreed twice over: `project rename` pluralised the *noun*
	and left the verb behind — "1 item keep their numbers" — while `workspace rename` got
	the grammar right and hedged the count with "at least" because it was reading a page.

	It is only ever read at the moment somebody is deciding whether to do something
	irreversible, which is the worst possible place for either fault.
	"""

	if held == 1:
		return "1 item keeps its number"

	return f"{held:,} items keep their numbers"


def _in_place (world: World, workspace: str) -> str:
	"""Return " in <workspace>", or nothing when there is only one place it could be.

	§1.4 again, in the place it is easiest to miss: a refusal is written when something has
	already gone wrong, so it is the *last* output anybody re-reads for stray vocabulary.
	Somebody with one workspace who typed a number that does not exist was being told
	"there is no #9 in si" — a workspace they never named, introduced by an error message,
	about a to-do list. The guard on the four §13.5b commands cannot see this, because a
	refusal is not in the transcript.
	"""

	if not world.qualifies_workspace and not world.qualifies_connection:
		return ""

	return f" in {workspace}"


def _found_at (
	item: Reached, workspace: str, ref: int, kinds: tuple[str, ...]
) -> Item | None:
	"""Ask one connection what this ref names there, trying each kind in turn.

	Tasks first wherever both are wanted, because that is overwhelmingly what a number on
	a command line means — and because a ref belongs to exactly one item, so the order
	decides only which question is asked twice, never which answer is returned.
	"""

	for kind in kinds:
		found: Item | None = (
			item.client.task(ref=ref, workspace=workspace)
			if kind == "task"
			else item.client.document(ref=ref, workspace=workspace)
		)

		if found is not None:
			return found

	return None


def _everywhere (world: World, ref: int, kinds: tuple[str, ...]) -> list[Located]:
	"""Find this ref in every workspace that is reachable.

	A point lookup per workspace. That is what §13.7 means by there being no bulk "all
	item ids" endpoint: an index of what exists would go stale exactly when it mattered,
	while disambiguating one ref needs a point lookup and nothing more.
	"""

	found: list[Located] = []

	for item in world.reached:
		for workspace in item.identity.workspaces:
			# A connection that fails mid-search must not turn a refusal about one ref
			# into a refusal about the network. The listing already said which
			# connections answered.
			with contextlib.suppress(subroutine.errors.SubroutineError):
				here = _found_at(item, workspace.slug, ref, kinds)

				if here is not None:
					found.append(
						Located(
							connection=item.name, workspace=workspace.slug, item=here
						)
					)

	return found


def _absolute (world: World, located: Located) -> str:
	"""Return a candidate's address, qualified as far as this world needs."""

	return subroutine.domain.refs.format_address(
		located.ref,
		workspace=located.workspace,
		connection=located.connection if world.qualifies_connection else None,
	)


def _place_of (world: World, located: Located) -> str:
	"""Return what ``use`` would be given to make this candidate's home current."""

	if world.qualifies_connection:
		return f"{located.connection}{subroutine.domain.refs.SEPARATOR}{located.workspace}"

	return located.workspace


def _capture_name (world: World, project: subroutine.views.Project) -> str:
	"""Return what to write after ``+`` to file something in this project.

	**Its whole address where it has a parent, since `#957`.** A key stopped being unique in
	its workspace, so suggesting ``+dist`` to somebody who has just made a second one would be
	suggesting a line the program then refuses — and a suggestion exists precisely to be
	copied.

	A root project needs no listing fetched for it, which is the common case: its address is
	its key, and one extra request on the sentence after ``Created`` is worth avoiding.
	"""

	if project.parent_id is None:
		return project.key

	where = world.writing_to()

	return subroutine.directory.address(
		project, where.client.projects(workspace=_writing_workspace(world))
	)


def _addressed_in (
	tree: typing.Sequence[subroutine.views.Project], wanted: str
) -> subroutine.views.Project | None:
	"""Find the one project a listing holds under this name or address.

	**The same order ``domain.selection.addressed`` resolves in** — the whole address first,
	then a bare name — because they answer the same question, and a command whose confirmation
	counted one project while the server moved another would be worse than either rule alone.
	Ambiguity is ``None`` here rather than a refusal: this runs to *count* what is about to
	move, and the server refuses in its own words a moment later.

	The composing is :func:`subroutine.directory.address`, which both clients already share.
	"""

	address = subroutine.domain.projects.normalize_path(wanted)
	exact = [item for item in tree if subroutine.directory.address(item, tree) == address]

	if exact:
		return exact[0]

	named = [item for item in tree if item.key == address]

	return named[0] if len(named) == 1 else None


def _subtree (
	tree: list[subroutine.views.Project], key: str
) -> list[subroutine.views.Project]:
	"""Return the project with this key and everything under it.

	**Walked through ``parent_id``, not read off ``path``.** The materialised path is
	exactly what makes this a single containment test in the database, and it is
	deliberately *not* on the view — §6.9 calls it an implementation of the hierarchy
	rather than a field of it, and the writability guard records that. So a client
	assembles the tree from the one relation it is given, which is the right side of that
	trade: `path` is ours to change and `parent_id` is a fact.

	``projects()`` returns parents before children (§8.4), so one forward pass is enough
	and nothing has to recurse.
	"""

	root = _addressed_in(tree, key)

	if root is None:
		return []

	inside = {root.id}

	for item in tree:
		if item.parent_id in inside:
			inside.add(item.id)

	return [item for item in tree if item.id in inside]


def _keep_the_operators_own_list (
	world: World, before: typing.Sequence[subroutine.views.User]
) -> str | None:
	"""Pin local commands to the existing account, and return who that was.

	**Adding a colleague must not cost you your own to-do list.** Local mode picks an
	account by there being exactly one (§12.1a); the moment a second exists it refuses,
	correctly, with "there is more than one account, so there is no way to tell whose
	to-do list to show". So on an instance somebody actually uses, `user create` broke
	`subroutine add` for them — the same shape as service accounts counting towards that
	total until 2026-07-30, and the same answer: setting somebody up must not take
	something away.

	Only when there was exactly **one** account and nothing has already chosen. Two
	accounts already means the operator has settled this, and overwriting their choice
	would be a worse version of the problem being fixed.

	Returns ``None`` when nothing needed doing, which is every case after the first.

	**Written to the file *and* to the settings this process is holding** (`#587`). They are
	one fact and the client reads the in-memory one on every call — measured, the object a
	client holds is the object the world holds — so storing only the file left the repair
	true for the next command and false for the rest of this one. That cost nothing while
	``user create`` did one thing; the moment it also grants a role, the very next call
	resolves an operator and finds the ambiguity this function had just written the cure for.
	"""

	people = [account for account in before if not account.is_service_account]

	if len(people) != 1 or world.settings.local_user:
		return None

	subroutine.config.store_setting("local_user", people[0].username)
	world.settings.local_user = people[0].username

	return people[0].username


def _workspace_id_of (world: World, slug: str | None) -> str | None:
	"""Return the permanent id of the workspace this slug names, or ``None``.

	``None`` rather than a refusal when it does not resolve: the slug written beside it is
	the fallback :func:`subroutine.directory.resolve_workspace` already uses for every
	marker predating `#317`, so a marker with a name and no id is the state the program has
	always handled rather than a broken one.
	"""

	if slug is None:
		return None

	for item in world.reached:
		if item.name != world.current.connection:
			continue

		for space in item.identity.workspaces:
			if space.slug == slug:
				return str(space.id)

	return None


def _project_written_down (
	world: World, wanted: str, *, workspace: str | None = None
) -> tuple[str, str] | None:
	"""Return the address and permanent id of the project this names, or ``None``.

	Checked before the file is written, because a marker naming a project that does not
	exist fails on the *next* capture rather than here — and the person who would have to
	work out why is not the one who typed this.

	Returns the **id** rather than a yes-or-no, because that is what the marker records
	(`#177`) and asking twice would be two chances for the answers to differ. The address
	comes back beside it so the readable half of the pair is the one that resolves: a bare
	key stopped naming one project with `#957`, so writing down what somebody typed would
	leave a file whose two halves can point at different projects.

	**``workspace`` says which one to look in, and its absence still means "wherever a write
	would land"** — :func:`_writing_workspace`, which refuses when nothing has said. Named by
	:func:`_adopted_project` alone, which asks this of several workspaces in turn (`#1501`)
	and so cannot let each call answer that question for itself.
	"""

	where = world.writing_to()
	tree = where.client.projects(
		workspace=_writing_workspace(world) if workspace is None else workspace
	)
	found = _addressed_in(tree, wanted)

	if found is None:
		return None

	return subroutine.directory.address(found, tree), str(found.id)


def _project_named_by (world: World, marker: subroutine.directory.Marker) -> str | None:
	"""Return the current key of the project a marker names, or ``None`` if there is none.

	The matching itself is `subroutine.directory.resolve`, which is shared with the MCP
	server — this half is only the fetching, because that is the part that needs a world
	to fetch through. It was one function here until `#232` found the other surface had no
	equivalent at all.

	**Nothing is fetched at all unless the marker speaks for the connection being written
	to** (`#414`). `context.resolve` has always applied that test to the marker's workspace;
	without it here, a marker whose connection was dropped (`#409`) or overridden by `-c`
	still matched its project **by key** on whichever instance answered instead — filing
	work into a same-named project somewhere else entirely, one line under a warning saying
	the connection had been ignored.
	"""

	where = world.writing_to()

	# **Or found there by id** (`#556`). `speaks_for` compares the name the marker wrote,
	# which is exactly the test `#414` added and it stays; `marker_found_on` is set only
	# when a `workspace_id` matched, which is a claim a key could never make.
	if not (
		marker.speaks_for(where.name) or world.current.marker_found_on == where.name
	):
		return None

	found = where.client.projects(workspace=_writing_workspace(world))

	return subroutine.directory.resolve(marker, found)


def _agent_because (
	connection: subroutine.connections.Connection, default_connection: str
) -> str | None:
	"""Name the variable that made this an agent's credential, or ``None`` if it did not.

	``describe_only`` because this is a report: without it, asking who you are would run every
	connection's ``token_command`` — a ``pass show`` or a ``gpg`` that can prompt — purely to
	build a line about a *different* source. It is the same reason ``subroutine connections``
	passes it.

	**The default is passed in rather than read here**, because this is called once per
	connection and :func:`connections.roster` reads the configuration file each time.
	"""

	resolved = subroutine.credentials.resolve(
		connection, default_connection=default_connection, describe_only=True
	)

	return connection.agent_variable if resolved.by_agent else None


def _whoami_lines (me: subroutine.views.Me, *, agent_because: str | None = None) -> list[str]:
	"""Describe one connection's answer to "who am I": the account, the credential, the room.

	**What the credential withholds is stated; what it grants is not enumerated.** An
	unnarrowed owner would otherwise get twenty permission keys they already have, and the
	one reader who needs the list — an agent working under a deliberately small credential —
	is exactly the one whose list is short. Same rule as every other column here: what is
	true of every row says nothing, and it is the exception that has to be visible.
	"""

	kind = "agent" if me.user.is_service_account else "person"
	credential = me.credential
	how = (
		"the local database"
		if credential is None
		else f"token {credential.title!r} ({credential.prefix}…)"
	)
	lines = [f"{me.user.username} ({kind}), via {how}."]

	# **Said only when it happened, which is what makes it worth a line** (`#1715`). A
	# credential chosen by a condition nobody can see is the defect `#1449` exists to fix, and
	# fixing it by adding a second invisible condition would be no fix at all. `subroutine
	# connections` reports the source for every connection and §1.4 hides that command until
	# there are two — which is exactly the machine this arrives on.
	if agent_because is not None:
		lines.append(
			f"This connection's agent credential, because {agent_because} is set here."
		)

	if credential is not None and credential.narrows:
		lines.append(
			f"Narrowed to "
			f"{subroutine.views.narrowing(credential, me.workspaces)}."
		)

	if me.instance_permissions:
		lines.append(f"Over the installation itself: {', '.join(me.instance_permissions)}.")

	if not me.workspaces:
		# **The failure this command exists to make legible.** A credential pinned to a
		# workspace it has no membership of reaches nothing, and every *other* command
		# reports that as an empty list — which reads as an empty instance rather than as
		# a credential that cannot see it.
		lines.append("No workspace here can be read with this credential.")

		return lines

	# **Not `_tabulated`, and that is the one place this output departs from every other
	# listing here.** Its rule is that a column saying the same thing on every row says
	# nothing — true of a backlog, false of this: two workspaces both answering "Owner" is
	# not a column with nothing to say, it is the answer to the question that was asked.
	# Dropped silently, the command printed two slugs and no statement of authority at all.
	names = max(len(workspace.slug) for workspace in me.workspaces)
	roles = max(len(_role(workspace)) for workspace in me.workspaces)
	rows = []

	for workspace in me.workspaces:
		cells = [workspace.slug.ljust(names), _role(workspace).ljust(roles)]

		# **Per row rather than per column.** One credential can be narrowed in one
		# workspace and not in another, and the row where it is narrowed is the row whose
		# permissions somebody has to read.
		#
		# **The test is whether the role holds everything, not whether the credential was
		# narrowed** (`#717`). The old condition meant an agent learned *more* about what
		# it could do by being restricted — a plain contributor got the word `Contributor`
		# and nothing else, while the same account with a pinned token got the list.
		if subroutine.permissions.worth_listing(workspace.permissions):
			# **Described rather than listed** (`#703`). A verb whose subject is wider than
			# its own name is a list that reads as complete and is not — `task:write` is
			# the only thing granting document writes, and nothing said so.
			cells.append(
				f"may: {', '.join(subroutine.permissions.described(workspace.permissions))}"
			)

		rows.append(f"  {'  '.join(cells)}".rstrip())

	return [*lines, "", *rows]


def _role (workspace: subroutine.views.WorkspaceAccess) -> str:
	"""Return the role held in one workspace.

	**This said a superuser reaches every workspace whether or not they are a member, cited
	§7.1 for it, and had a branch for the roleless case that could not execute** (`#1418`).
	``workspaces.readable`` is an inner join on membership, so every row reaching here has a
	role; §7.1 said neither thing, and two surfaces each guessed in opposite directions for
	months. Membership is reach, and `#1418`'s decision writes that down.

	The fallback stays because ``role`` is optional on the wire and an instance a release
	behind may not send one — but it is a null guard now rather than a claim about permissions.
	"""

	return workspace.role or "no role"


def _connection_named (
	program: Program,
	resolved: subroutine.config.Settings,
	name: str,
	url: str,
) -> tuple[str, str, subroutine.connections.Roster]:
	"""Read and refuse the two things `connections add` cannot proceed without — `#943`.

	Lifted out of ``register`` rather than written there: `#943`'s ratchet only goes down, and a
	new option elsewhere is paid for by a block that was never a closure's business. Every
	refusal here is about the *arguments*, before anything is reached or written, so none of it
	needs the command's later state.
	"""

	try:
		wanted = subroutine.connections.check_name(name)
		roster = subroutine.connections.roster(resolved)

	except subroutine.errors.SubroutineError as error:
		program.fail(error)

	if wanted == subroutine.connections.LOCAL_NAME:
		program.stop(
			f"{subroutine.connections.LOCAL_NAME!r} already means this machine's own database, "
			"so it cannot name another instance.",
			hint="Give it a name of its own, as in 'subroutine connections add work'.",
		)

	# **The file's names rather than the roster's**, because a connection turned off is still in
	# the file: adding a second table under that name would leave the meaning of the file to
	# whichever one TOML kept.
	if wanted in subroutine.connections.declared_names():
		program.stop(
			f"There is already a connection called {wanted!r}.",
			hint=(
				f"Choose another name, or edit {subroutine.config.config_file_path()} to "
				"change that one."
			),
		)

	if not url.strip():
		program.stop(
			f"Say where {wanted!r} is.",
			hint=(
				f"For example: subroutine connections add {wanted} --url "
				"https://tasks.example.com"
			),
		)

	try:
		address = subroutine.connections.check_url(url)

	except subroutine.errors.SubroutineError as error:
		program.fail(error)

	return wanted, address, roster


def _has_never_had_a_list_of_its_own (
	roster: subroutine.connections.Roster, resolved: subroutine.config.Settings
) -> bool:
	"""Report whether this machine reaches only ``local`` and has no database there.

	**Decided before the token is resolved**, because the answer changes which environment
	variable applies: a bare ``SUBROUTINE_TOKEN`` belongs to the default connection (§12.3a),
	so asking with the old default would prompt for a token the machine already has and store
	a second copy of it in a file.

	This is the case ``connections add`` exists for — somebody's second laptop, reaching work.
	Leaving the default pointed at a database nobody created would make the very next ``add``
	fail, on a machine where the person has just said where their work is.

	**Never true when there is a local database**, because that is somebody's own list and
	moving their writes off it is their call. Two decisions read this, and that limit is what
	makes both of them safe.
	"""

	alone = roster.names == (subroutine.connections.LOCAL_NAME,)

	return alone and resolved.has_no_instance_yet()


def _stops_looking_for_a_local_list (never_had_one: bool) -> bool:
	"""Report whether ``local`` should be turned off as this connection is recorded — `#1454`.

	``connections.roster`` puts ``local`` in whether it is declared or not, so without this
	every command on a server-only machine answers *"no Subroutine instance has been set up
	here yet — run 'subroutine init'"* above the real result. **That advice is wrong for this
	person**: following it gives them a second, empty instance beside the one they were just
	onboarded to, and then ``use`` and the default connection become things they have to
	understand on their first day.

	**The same evidence as :func:`_has_never_had_a_list_of_its_own`, one step further.** This
	command already decides where writes go on that fact and already says so, so nothing extra
	is being inferred — and the limit that keeps the first decision safe keeps this one safe.

	**Only when ``local`` is not declared at all.** Somebody who wrote the table by hand has
	said something about it, and ``config.store_table`` refuses a header that is already there
	— which would surface through this command's failure path as *"the connection could not be
	written"*, about a connection that was.

	**A flag was the first design and reading this code rejected it**: it would have to be
	discovered by somebody pasting a line another person printed for them, and ``connections``
	is hidden from ``--help`` until a second connection exists.
	"""

	return never_had_one and (
		subroutine.connections.LOCAL_NAME not in subroutine.connections.declared_names()
	)


def _where_new_work_goes (wanted: str, *, sole: bool) -> str:
	"""Say that writes now go to a connection this command just made the default.

	**The effect in the reader's terms, and no new noun** (§1.4). Somebody being onboarded has
	never heard of ``local`` and does not need to: what changed for them is that this machine
	now answers from one place. The person who does need the word meets it in ``init``'s
	refusal, at the moment it matters and with the remedy beside it (`#1470`).
	"""

	said = f"New work goes to {wanted} now, because this machine has no list of its own"

	return f"{said} — and nothing here will look for one." if sole else f"{said}."


def _connection_settings (
	connection: subroutine.connections.Connection,
) -> dict[str, str | bool]:
	"""Return what to write under a connection's table, leaving out what was not asked for.

	A default written down is a decision recorded, and this command takes none: a table
	saying ``read_only = false`` reads as somebody having considered it, and it would
	outlive a change to what the default means.
	"""

	values: dict[str, str | bool] = {"url": str(connection.url)}

	if connection.read_only:
		values["read_only"] = True

	if connection.token_env is not None:
		values["token_env"] = connection.token_env

	if connection.token_command is not None:
		values["token_command"] = connection.token_command

	return values


def _describing (reached: Welcomed) -> str:
	"""Say what a new connection turned out to be, in one line."""

	instance = "it" if reached.instance is None else reached.instance.name

	if not reached.workspaces:
		# Reachable and useless, which every other command would report as an instance with
		# nothing in it. Said here because this is the one moment somebody still has the
		# person who issued the token in mind.
		return f"Reached {instance} as {reached.username}, who is in no workspace there."

	return f"Reached {instance} as {reached.username}, in {', '.join(reached.workspaces)}."


class Reach(typing.NamedTuple):
	"""What asking every configured connection who it is could establish."""

	#: The connection already naming the instance in question, if one does.
	twice: str | None

	#: The connections that did not answer, so this could not be settled for them.
	unchecked: tuple[str, ...]


def _already_reached (
	roster: subroutine.connections.Roster,
	resolved: subroutine.config.Settings,
	instance: uuid.UUID,
) -> Reach:
	"""Report which connection already names this instance, and which could not be asked.

	A connection that cannot be reached is passed over rather than treated as a failure. It
	is the ordinary state of the local one on the machine this command is written for, and
	refusing to add a work connection because a *different* server is down would be a worse
	outcome than the collision this protects against.

	**Passed over is not the same as answered, and saying so is the point** (`#1258`). What
	was wrong here was not the skipping but the positive claim that followed it: every
	connection was asked, some said nothing, and the command reported that this machine does
	not already reach the instance. The gap is carried back so the caller can name it.

	**A local database at an older schema is answered rather than skipped.** It raises like
	one that is switched off — and it is neither switched off nor unknown, it is on this disk
	with its own id in it. That state is *guaranteed* by the migration `docs/hosting.md`
	prescribes, since the old database is deliberately kept as the rollback, so the guard was
	defeated by the exact procedure the page recommends.
	"""

	unchecked: list[str] = []

	for existing in roster:
		try:
			with subroutine.clients.opening.for_connection(
				existing, roster, resolved
			) as client:
				answered = client.identity()

		except subroutine.errors.SubroutineError:
			if existing.is_local:
				if subroutine.clients.local.instance_id(resolved) == instance:
					return Reach(existing.name, ())

				continue

			unchecked.append(existing.name)

			continue

		if answered.instance is not None and answered.instance.id == instance:
			return Reach(existing.name, ())

	return Reach(None, tuple(unchecked))


def _refuse_a_second_name_for_one_instance (
	program: Program,
	roster: subroutine.connections.Roster,
	resolved: subroutine.config.Settings,
	reached: Welcomed | None,
	*,
	wanted: str,
) -> tuple[str, ...]:
	"""Stop if this instance is already configured, and return what could not be asked.

	**Refused here, where it is one word to change.** Left to be discovered, it is discovered by
	``subroutine list`` refusing outright — the instance is counted once per name it is
	configured under, so a merged read cannot be trusted and the whole listing is withheld. That
	message can only tell somebody to edit a file, which is the friction this command exists to
	remove.

	It is the *check* that finds this, so ``--no-check`` passes it by. That is the escape hatch
	and not a recommendation: `#327` is where two connections naming one instance becomes a
	workable arrangement rather than a broken machine.

	**A module-level function rather than the body of the command**, which is
	``tests/test_personal_path.py``'s ratchet being paid rather than raised, the same way
	:func:`_finished` and :func:`_planned` came out.
	"""

	if reached is None or reached.instance is None:
		return ()

	reach = _already_reached(roster, resolved, reached.instance.id)

	if reach.twice is None:
		return reach.unchecked

	program.stop(
		f"{wanted!r} is the same instance as {reach.twice!r}, which this machine already "
		"reaches.",
		f"Use {reach.twice!r} instead. Two names for one instance would make every merged "
		"listing count its work twice, so this machine holds one.",
	)


def _what_was_not_checked (unchecked: typing.Sequence[str]) -> str:
	"""Say which connections the duplicate check could not ask, and what that leaves open.

	**The check said nothing about these, so neither does the command** (`#1258`). Said after
	the connection is written rather than as a refusal: a server being down is not evidence of a
	collision, and it is a poor reason to decline the one thing somebody is trying to do. What
	it *is* evidence of is that the check was partial, and that is worth knowing while the name
	they chose is still one word to change.
	"""

	named = ", ".join(repr(name) for name in unchecked)

	return (
		f"{named} did not answer, so this may be a second name for an instance this machine "
		"already reaches."
	)


def _completions (item: Reached) -> str:
	"""Return the ``use`` a person meant, given the connection they named.

	Its workspaces are already loaded — ``identity()`` is asked of every connection when
	the world opens — so the completion can be exact rather than a shape to fill in.
	"""

	slugs = [workspace.slug for workspace in item.identity.workspaces]

	if len(slugs) == 1:
		return f"Say which workspace on it — 'subroutine use {item.name}/{slugs[0]}'."

	if not slugs:
		return (
			"It has no workspace this credential can see, so there is nothing to "
			"work in yet."
		)

	listed = ", ".join(sorted(slugs))

	return f"Say which workspace on it — 'subroutine use {item.name}/<one of: {listed}>'."


def _finished (program: Program, *, which: str, because: str) -> None:
	"""Tick something off, or — for something that merely happened — put it away.

	**A module-level function rather than the body of the command**, which is
	``tests/test_personal_path.py``'s ratchet being paid rather than raised: ``register`` is a
	closure and every line inside it is one more line that only one command can reach.
	:func:`_planned`, ``_show_today`` and ``_changed`` came out the same way and for the reason.
	"""

	with program.opened() as world:
		located, task = _a_task(program,
			world,
			_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
			verb="done",
		)

		# **An occasion is put away rather than achieved** (decision `#1235` §3). Ticking off
		# somebody's birthday is not refused here — a refusal is a wall with nothing to do next
		# — but calling it *Done* would be the program congratulating the reader on a day going
		# by. Nothing ever suggests this; when somebody asks for it they get it, in words that
		# say what actually happened.
		achieved = not _happens(task)

		if task.completed_at is not None:
			# Saying so beats reporting success twice. The case this is really about is an
			# up-arrow repeat, which used to land on whatever had taken that number.
			program.say(_acted(world, located, "Already done" if achieved else "Already past"))
			_suggest(program.console, "subroutine list", "everything still open")

			return

		client = _require_connection(program, world, located.connection)
		finished = client.complete(ref=task.ref, workspace=located.workspace)

		# **Derived once because both readers must agree, and they did not** (`#1310`'s sibling,
		# `#1312`). ``_because`` writes a comment that outlives the session and the line below
		# is gone the moment the screen scrolls — so the word avoided above was avoided only in
		# the place nobody re-reads, and a birthday carried "Done — ..." on its record for ever.
		outcome = "Done" if achieved else "Marked as past"

		_because(client, located, because, what=outcome)

		program.say(_acted(world, dataclasses.replace(located, item=finished), outcome))
		_suggest(program.console, "subroutine agenda")


def _planned (
	program: Program,
	*,
	which: str,
	when: str,
	until: str,
	because: str,
	just_this_one: bool = False,
	from_now_on: bool = False,
) -> None:
	"""Set the day a task starts, and the day it is over if it lasts more than one.

	**A module-level function rather than the body of the command**, which is the ratchet in
	``tests/test_personal_path.py`` being paid rather than raised: ``register`` is a closure and
	every line inside it is one more line that only one command can reach. ``_show_today`` and
	``_changed`` came out the same way and for the same reason.
	"""

	with program.opened() as world:
		located, task = _a_task(program,
			world,
			_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
			verb="plan",
		)
		client = _require_connection(program, world, located.connection)

		# **A plan names a day and never touches the clock** (`#1299`). It sent a bare date,
		# which means *the whole of that day* wherever it is stored, so planning a doctor's
		# appointment for tomorrow destroyed the 14:00 that ``add`` had just read.
		zone = world.account_zone(located.connection, located.workspace)

		# **Three states, the same three ``--until`` has had all along** (`#1316`). The argument
		# defaulted to ``""`` and went straight into :func:`_asked`, so *left out* and *cleared*
		# were one value and the empty one prompted — which left a start settable from here and
		# clearable only over HTTP, on a command whose own ``--until`` documents ``''`` as the
		# clear. ``UNGIVEN`` is what separates them, and it is why the sentinel exists.
		cleared = when == ""
		changed = client.schedule(
			ref=task.ref,
			workspace=located.workspace,
			starts=None
			if cleared
			else subroutine.domain.schedule.on_the_day(
				_day(world, _asked(when, "Which day?"), at=located),
				keeping=task.starts_at,
				all_day=task.starts_is_all_day,
				timezone=zone,
			),
			applies_to=_which_occurrences(
				program, task, just_this_one=just_this_one, from_now_on=from_now_on
			),
			**_until(world, until, beside=when, at=located, task=task, timezone=zone),
		)

		# The planned day, not `_when`'s answer. `_when` prefers a deadline, which is right in
		# a list and wrong in the confirmation of a command whose whole job was to set the
		# other field — the user said "tomorrow" and was shown Friday.
		#
		# **And it says the o'clock the command has just kept** (`#1330`). This was the last
		# rendering in the file still going through :func:`_render_date`, so ``plan`` printed
		# *Starts Wed 2 Dec* — byte for byte what it printed while it was **destroying** that
		# time, which is the silence `#1299` was about. Worse than a screen: ``_because``
		# writes this sentence into the item's record, where it outlives the session.
		planned = (
			"No longer starts on a day"
			if changed.starts_at is None
			else "Starts "
			+ _render_moment(
				changed.starts_at, changed.timezone, all_day=changed.starts_is_all_day
			)
		)

		_because(client, located, because, what=planned)

		program.say(_acted(world, dataclasses.replace(located, item=changed), planned))
		_suggest(program.console, "subroutine agenda")


def _hidden (
	program: Program,
	*,
	which: str,
	when: str,
	because: str,
	just_this_one: bool = False,
	from_now_on: bool = False,
) -> None:
	"""Hide a task until a day, or a time on one.

	**Out of `register`'s closure to pay for the two flags this command grew** (`#943`'s
	ratchet, and the fourth command to leave the same way — ``_planned``, ``_changed`` and
	``_withdrawn`` went before it). Nothing here needs the closure that :class:`Program` does
	not carry, which is the test that ratchet is really applying.
	"""

	with program.opened() as world:
		located, task = _a_task(program,
			world,
			_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
			verb="defer",
		)
		client = _require_connection(program, world, located.connection)

		changed = client.schedule(
			ref=task.ref,
			workspace=located.workspace,
			snooze=_moment(world, _asked(when, "Hide it until when?"), at=located),
			applies_to=_which_occurrences(
				program, task, just_this_one=just_this_one, from_now_on=from_now_on
			),
		)

		hidden = f"Hidden until {_when_rendered(changed)}"

		_because(client, located, because, what=hidden)

		program.say(_acted(world, dataclasses.replace(located, item=changed), hidden))
		_suggest(program.console, "subroutine agenda")


class Ending(typing.TypedDict, total=False):
	"""What ``--until`` contributes to a scheduling call: an end, or nothing at all.

	**Named rather than left as a ``dict[str, ...]``**, because it is splatted into
	:meth:`schedule` and a loose mapping tells the type checker to look away at exactly the
	call it is describing. That became load-bearing when ``schedule`` grew ``expected_version``
	(`SR#1696`): a wide value type made a version argument look reachable from this splat, and
	mypy said so. ``total=False`` is the first of the three states below — *not given* is the
	absence of the key, which is what makes it expressible at the call site at all.
	"""

	ends: datetime.date | datetime.datetime | None


def _until (
	world: World,
	written: str,
	*,
	beside: str,
	at: "Located",
	task: subroutine.views.Task,
	timezone: str,
) -> Ending:
	"""Return what to pass a client for ``--until``: nothing, a day, or an explicit clear.

	**Three states, and a flag can only carry two of them without this.** ``UNSET`` and
	``None`` mean different things to every client method here (§8.3) — leave it alone, and
	clear it — so a bare ``plan 42 friday`` on something already running to the 28th must not
	quietly end it early, and ``--until ''`` must be able to say *no longer a span* rather
	than being read as a day nobody named.

	Returned as keyword arguments rather than a sentinel because that is what makes the
	first state expressible at the call site at all.

	**The end keeps its own clock, exactly as the start does** (`#1299`) — ``ends_at`` shares
	``starts_is_all_day``, because an end has none of its own (decision `#1235` §2).

	**And it says so itself when there is no clock to keep** (`#1329`). An end named as a day
	with nothing to carry is a whole day, so on a row whose start has a time the two ends come
	out different shapes and ``check_span`` refuses — correctly, and with a hint written for
	somebody sending fields over HTTP: *give both ends a time, or give both a date with no
	time*. **Neither is reachable from here.** Nothing at this surface writes a time onto an
	end, and nothing at this surface takes the clock off a start, so following the advice was
	impossible in both directions — which is `#1322`'s finding met in the first refusal written
	after it. Refusing here instead names the thing that is actually in the way.
	"""

	if written is UNGIVEN:
		return {}

	if written == "":
		return {"ends": None}

	if task.starts_at is not None and not task.starts_is_all_day and task.ends_at is None:
		raise subroutine.errors.ValidationError(
			f"{task.title!r} starts at a time, and an end named as a day is a whole day.",
			hint=(
				"Something is a whole day at both ends or a time at both. Giving an end its "
				"own time of day is not something the command line can do yet."
			),
		)

	# **A bare day at each end is one pair, counted from the first** (`SR#1557`). This surface
	# resolves both days itself, before a client is called, so the rule the domain applies to
	# ``starts``/``ends`` sent as strings cannot reach here — the domain is handed two dates
	# that already disagree. One rule, so the moment to count from comes from the same
	# function the domain asks.
	return {
		"ends": subroutine.domain.schedule.on_the_day(
			_day(
				world,
				written,
				at=at,
				field="ends_at",
				counting_from=subroutine.domain.schedule.end_counted_from(
					beside if beside is not UNGIVEN else None,
					written,
					timezone=timezone,
					now=subroutine.db.types.utcnow(),
				),
			),
			keeping=task.ends_at,
			all_day=task.starts_is_all_day,
			timezone=timezone,
			field="ends_at",
		)
	}


def _day (
	world: World,
	written: str,
	*,
	at: "Located",
	field: str = "starts_at",
	counting_from: datetime.datetime | None = None,
) -> datetime.date:
	"""Read a day the user named, **in their account's zone** (`#1083`, decision `#1088`).

	**A written time is refused rather than dropped** (`#1299`), which is
	``interpret_written_day_only``'s whole subject: ``plan 1 tomorrow --until
	'2026-08-27T11:30:00'`` used to keep the date, throw away the 11:30 and report success.
	``field`` is the column being written, so the refusal names something a caller can send.

	**A weekday name is resolved here rather than by the expression grammar** (`#167`).
	``plan 1 friday`` is promised by ``explain dates``, by ``plan --help`` twice, by
	``defer --help`` twice and by this function's own refusal — and it did not work, while
	``add "Something by friday"`` did. Weekdays are what a person types; §9.3's expressions
	serve programs, which have a calendar and should send a date. The two vocabularies meet
	in ``dates.day_named``, so there is one answer to what "friday" means.

	**The zone is the instance's answer, not this machine's.** ``friday`` is the soonest Friday
	counting *today*, so which day it names depends on whose today — and the account's is the
	one the agenda is bucketed in. Asking the laptop made a written date and the answer about it
	disagree, which is what `#1001` filed and `#1088` settled.
	"""

	resolved = subroutine.domain.schedule.interpret_written_day_only(
		written,
		timezone=world.account_zone(at.connection, at.workspace),
		# **``counting_from`` is how the far end of a span stops being read against today**
		# (`SR#1557`). Every caller but that one leaves it alone and gets the clock.
		now=counting_from or subroutine.db.types.utcnow(),
		field=field,
	)

	if resolved is None:
		raise subroutine.errors.ValidationError(
			f"{written!r} is not a day this understands.",
			hint=subroutine.domain.schedule.WRITTEN_DAY_HINT,
		)

	return resolved


def _moment (world: World, written: str, *, at: "Located") -> datetime.datetime | datetime.date:
	"""Read a day the user named, **keeping a time of day when they wrote one** (`#858`).

	`_day`'s sibling, for the one command whose field carries a clock. Same vocabulary, the
	same refusal and the same zone — both go through ``schedule.interpret_written_moment``, and
	`_day` is that function with the time thrown away, so there is no second grammar to drift.

	**Two readers disagree about a deferred item and both are right** (`#771`, and `#858`
	asked for this to be written down). ``readiness.undeferred`` compares to the minute, so
	``subroutine list`` shows it at six in the morning; ``domain.agenda`` compares against the
	*end of the day being shown*, so ``subroutine agenda`` has it from midnight. That is not a
	defect to reconcile: an agenda answers *what is today about*, and an item arriving at six
	is part of today from the moment the day starts.

	**Only ``defer`` reads a moment**, deliberately. ``plan`` sets ``starts_at``, and the
	terminal renders no times anywhere — `#576` is where an event's span is decided, and
	giving one command a clock ahead of that decision would be `#251`'s inert control with the
	inconsistency showing.
	"""

	resolved = subroutine.domain.schedule.interpret_written_moment(
		written,
		timezone=world.account_zone(at.connection, at.workspace),
		now=subroutine.db.types.utcnow(),
		field="when",
	)

	if resolved is None:
		raise subroutine.errors.ValidationError(
			f"{written!r} is not a day this understands.",
			hint=subroutine.domain.schedule.WRITTEN_DAY_HINT,
		)

	return resolved


def _a_readable_day (written: str) -> str:
	"""Return a written day unchanged, refusing here what every connection would refuse — `#1083`.

	**Readability only. The day itself is resolved by the instance**, in the account's zone
	(decision `#1088`), which is the whole point of sending the word rather than a date.

	This exists because the agenda *fans out*: a connection that refuses is reported and the
	others still answer, so a mistyped day printed a refusal and then ``Nothing due then.`` —
	a plausible, complete, wrong answer sitting directly beneath the reason it was wrong. A
	write goes to one connection and needs no such check, which is why ``plan`` and ``defer``
	have none.

	**The zone passed here cannot matter and the answer is thrown away.** Whether a word parses
	is a property of the vocabulary; *which day it names* is the question this deliberately does
	not ask. Anything else would be the machine's zone deciding a day again, one function along.
	"""

	subroutine.domain.schedule.interpret_written_day(
		written, timezone="UTC", now=subroutine.db.types.utcnow(), field="when"
	)

	return written


def _change_line (event: subroutine.views.Event) -> str:
	"""Render one event as a line somebody can read.

	Names the item rather than its id — ``item_ref``/``item_title`` are on the view for
	exactly this, so that a CLI, an agent and a browser say the same thing about one row.

	**The changed field *names*, not their values.** A status moving from one word to
	another is worth a glance; a description rewritten is not worth four lines of the
	terminal, and anybody who wants the values has ``subroutine show``.
	"""

	named = (
		f"{subroutine.domain.refs.format_ref(event.item_ref)} {event.item_title}"
		if event.item_ref is not None and event.item_title is not None
		else event.item_title or _in_this_persons_terms(event.entity_type)
	)
	verb = event.action.replace("_", " ")

	# A comment is the one action whose entity is not what it is about, and "commented on"
	# reads as what happened where "created" would name the comment row nobody can see.
	if event.entity_type == "comment":
		verb = f"{verb} a comment on"

	fields = sorted({subroutine.views.field_in_words(name) for name in (event.changes or {})})
	listed = f"  ({', '.join(fields)})" if fields and event.action == "updated" else ""

	return f"{verb:<12}  {named}{listed}"


def _journal_line (entry: subroutine.views.JournalEntry, *, who_wide: int = 0) -> str:
	"""Render the headline of one journal entry — who, what, and which item.

	**Where `_change_line` names the fields, this names the actor**, which is the difference
	the two readings exist for: a feed is *what moved* and a journal is *what happened*, and
	what happened has somebody doing it.
	"""

	named = (
		f"{subroutine.domain.refs.format_ref(entry.item_ref)} {entry.item_title}"
		if entry.item_ref is not None and entry.item_title is not None
		else entry.item_title or _in_this_persons_terms(entry.entity_type)
	)
	verb = entry.action.replace("_", " ")

	if entry.entity_type == "comment":
		verb = f"{verb} a comment on"

	# **Not "nobody"**, which reads as an omission. `actor_user_id` is null exactly when the
	# instance acted with no principal behind it — bootstrapping a workspace is the one such
	# path — and naming that plainly is more use than a blank column.
	who = entry.actor or "the instance"

	# **Padded to the widest name on the page, which is `#1424`'s finding one surface along.**
	# Without it the verb begins at a different column on every row — *the instance* is twelve
	# characters and *@si* is three — and a reader scanning for what happened has to find where
	# each line put it. The width is the caller's because it is a fact about the page.
	return f"{who:<{who_wide}}  {verb:<20}  {named}"


def _journal_detail (entry: subroutine.views.JournalEntry) -> list[str]:
	"""Return the lines the change feed deliberately leaves out — `#1430`.

	`_change_line`'s docstring states the feed's rule: *the changed field names, not their
	values*, because a rewritten description is not worth four lines of a terminal and anybody
	who wants the values has `show`. **A journal is the surface where that is the wrong answer**
	— somebody asking what happened over a period cannot open fifty items — so this is where
	the values and the words go.

	**Only for an update.** Creating a task writes a change for every column it was born with,
	so rendering those would put twenty lines under *filed it* and bury everything else.
	"""

	lines = []

	if entry.action == "updated":
		for change in entry.changed:
			if change.before is None and change.after is None:
				# Both sides unnameable — an id nobody has a lookup for. The phrase alone is
				# the honest answer and is what `#1430` chose over rendering a UUID.
				lines.append(change.said)

			else:
				lines.append(
					f"{change.said}: {change.before or 'nothing'} to {change.after or 'nothing'}"
				)

	if entry.said:
		lines.append(entry.said)

	return lines


def _in_this_persons_terms (entity_type: str) -> str:
	"""Name the thing an event is about, for an event that is not about an item.

	The rows ``init`` writes have no item to name, so this printed the entity type — which
	is ``workspace`` and ``workspace_member``, two of the seven words §13.5b says a person
	setting up a to-do list must never meet. Every other row already reads well, because
	every other row has a title.

	Anything unmapped keeps its own name rather than being made up: a word a reader has not
	met is better than a wrong one, and the guard beside this is what stops a new kind
	arriving unnoticed.
	"""

	return _AN_EVENT_ABOUT.get(entity_type, entity_type)


def _listing (
	world: World,
	*,
	limit: int,
	strict: bool,
	order: str | None = None,
	project: str | None = None,
	deferred: bool = False,
	q: str | None = None,
	ready: bool = False,
	trash: bool = False,
	assignee: str | None = None,
	claimed_by: str | None = None,
	status: str | None = None,
	type: str | None = None,
	tag: str | None = None,
	filters: dict[str, str] | None = None,
) -> subroutine.fanout.Gathered[Listing]:
	"""List every reachable workspace's items, one request per workspace per kind.

	Per workspace rather than per connection because ``GET /v1/tasks`` refuses an
	ambiguous one (§8.2) — and a local client that quietly spanned them would return
	different rows depending on where the tasks were, which is the divergence this whole
	arrangement exists to prevent.

	**Tasks and documents in one list.** Refs come from one counter per workspace and are
	shared between them (§6.2), and ``show`` already takes either — so a listing that held
	only tasks was telling a reader who had learned that a number names an item that half
	the numbers did not exist. Simon asked why ``#5``-``#8`` were missing from his list;
	they are decision documents, and nothing in the output said so.

	Each kind is fetched at the full limit and the merged result is cut to it, so the cut
	is made across both rather than allocated between them — twenty documents must not be
	able to push every task off a page.

	**``order`` is parsed once, here, against the task vocabulary**, which is the richer of
	the two: a person ranking a backlog wants ``-priority_score``, and a document has no
	priority to be ranked by. A name outside it is refused before a single request goes
	out, so an unknown sort field costs one message rather than one per workspace.
	"""

	shared, merging = _ordering(order)

	def ask (client: subroutine.clients.base.Client) -> Listing:
		"""Ask one connection for each of its workspaces in turn."""

		item = world.connection(client.connection.name)
		rows: list[Row] = []

		# **The instance's own answer, rather than one row past the limit** (`#1037`).
		# This asked for `limit + 1` so that *was anything cut?* was answered by what came
		# back — which was the only thing available while every client threw the envelope
		# away, and is exactly the trick that made the truncation invisible: ask for 501,
		# receive 200, conclude that is all there is.
		#
		# **Both signals are still needed and they answer different questions.** A listing's
		# `has_more` says the instance held more of *that kind*; the merge can also overflow
		# because two kinds of `limit` rows each make more than `limit` between them, and no
		# single listing knows that.
		asked = limit
		cut = False

		parked = 0
		held_back = 0

		# **A project belongs to one workspace, and this asks them all** (`#332`). Until
		# 2026-08-03 every instance had exactly one, so the loop ran once and could not
		# disagree with itself; `#288` created a second and `--project` stopped working
		# the same afternoon — the workspace that does not hold the key raised, the
		# fan-out read that as the connection failing, and the rows the *right* workspace
		# returned were discarded with it.
		#
		# So a key that resolves nowhere on this connection is still refused by name, and
		# a key that resolves somewhere is simply absent from the workspaces it is not in.
		# Suppressing unconditionally would turn a typo into "nothing on your list", which
		# is the same answer as a project that exists and is empty.
		# **Two slots, because the two refusals are not equally true** (`#1468`). This was one,
		# overwritten by whichever workspace failed last — and with `--project X --status
		# nonsense` the workspaces that do not hold `X` raise about the project while the one
		# that does raises about the status, so the message depended on the order they were
		# iterated in. The one that usually came last said *"There is no project 'X' here"*
		# about a project the caller had just listed.
		#
		# **An absent project is ordinary; a vocabulary key that is nowhere is a typo.** A
		# project legitimately does not exist in most workspaces, so that sentence is only the
		# answer when the key resolved nowhere at all. When both happened, the project sentence
		# is simply **false** and the vocabulary one is true — so the true one is raised.
		absent_project: subroutine.errors.SubroutineError | None = None
		unknown_word: subroutine.errors.SubroutineError | None = None
		reached = False

		for workspace in () if item is None else item.identity.workspaces:
			try:
				found_here = client.tasks(
					workspace=workspace.slug,
					limit=asked,
					order=order,
					project=project,
					deferred="include" if deferred else "exclude",
					q=q,
					ready=ready,
					deleted=trash,
					assignee=assignee,
					claimed_by=claimed_by,
					status=status,
					type=type,
					tag=tag,
					filters=filters,
				)

			except subroutine.errors.NotFound as absent:
				# Only a named project can legitimately be absent from a workspace the
				# caller can otherwise read. Anything else is this connection failing.
				#
				# **An assignee is deliberately not in this list.** An account belongs to
				# the instance rather than to a workspace, so a name that resolves nowhere
				# is a typo wherever it was asked — and tolerating it here would turn one
				# into "nothing on your list" across every workspace at once.
				if project is None:
					raise

				absent_project = absent

				continue

			except subroutine.errors.ValidationError as unknown:
				# **A status and a type are per-entity, per-workspace vocabulary** (§5.5),
				# so `blocked` existing in one workspace and not the next is ordinary
				# rather than an error — the same shape as `--project`, and the same
				# tolerance. What is not ordinary is a key that is nowhere, which is a typo
				# and is raised below by the `reached` check.
				#
				# **It falls through to documents rather than skipping the workspace**, and
				# that is the whole subtlety: a task status and a document status are
				# different vocabularies, so `--status active` misses every task and is
				# exactly the question somebody asking for documents in force is putting.
				# `continue` here answered it with the refusal instead.
				#
				# `#332` is why this is here at all: every instance had one workspace until
				# 2026-08-03, so this loop ran once and could not disagree with itself, and
				# `--project` broke the same afternoon a second one existed.
				#
				# **Identified by the field it names rather than by its code**, because the
				# two lookups disagree about the code: `status_for` raises `invalid_status`
				# and `item_type_for` takes the default. The field is what actually says
				# "a vocabulary key this workspace has not got", and matching on it means
				# a genuine refusal about something else is still raised.
				#
				# **A tag is the third such vocabulary and was missing from this set until
				# `#1575`**, which is `#1468`'s defect a third time. A tag row belongs to one
				# workspace exactly as a status does, so `--tag ui` refused in the four
				# workspaces that have not got it and took the whole listing with it — while
				# `GET /v1/tasks?tag=ui` answered correctly, and the sentence printed said the
				# tag was unused. One workspace made it unreachable; a second made it wrong.
				if not {problem.field for problem in unknown.errors} & _PER_WORKSPACE_WORDS:
					raise

				unknown_word = unknown_word or unknown
				# **An empty `Listing`, not an empty list** (`#1037`). A listing carries
				# `has_more`, and a workspace that answered nothing genuinely has no more.
				found_here = subroutine.clients.base.Listing()
				answered = False

			else:
				reached = True
				answered = True

			cut = cut or found_here.has_more
			held_back += found_here.held_back or 0
			rows.extend((client.connection.name, found) for found in found_here)

			# **`--ready` is about work you could start, so a document is not an answer to
			# it** (`#136`). §6.14 says a document is not scheduled and nothing blocks one,
			# so including them would mean every specification and decision in the instance
			# reported as ready — which is true and useless, and would bury the tasks.
			# The trash is a different list, not a wider one: nothing parked is reported
			# beside it, and a deferral count would be about the live list.
			if ready or trash:
				continue

			# **Counted before the assignee check below, not after.** A list narrowed to
			# one person still hides that person's deferred work, and hiding without
			# saying how much is the silence `#33` was about — the narrowing does not
			# change which half of the rule applies.
			#
			# Skipped when the task call did not answer, because the count would ask the
			# same question with the same key and be refused the same way.
			if not deferred and answered:
				# **A second request, and it buys the difference between narrowing a
				# list and truncating one in silence** — the failure `#33` was about.
				# Counted rather than flagged, unlike `…and more`: that declines a count
				# because it would mean a second full scan of the *whole* result, where
				# this set is the parked work alone and is small by construction. Asked
				# at `asked` so a pathological backlog cannot make the count the
				# expensive part; the report says `N+` if it fills.
				parked += len(
					client.tasks(
						workspace=workspace.slug,
						limit=asked,
						project=project,
						deferred="only",
						q=q,
						assignee=assignee,
						claimed_by=claimed_by,
						status=status,
						type=type,
						tag=tag,
						filters=filters,
					)
				)
			# **A document has no assignee and cannot be claimed, so a list narrowed to
			# either is a list of tasks** (§6.14 — a document has an owner rather than a
			# worker, and nobody works on one). The same argument `ready` makes above:
			# including them would answer a question nobody asked, and "everything Simon is
			# working on" ending in every specification in the workspace is worse than
			# useless.
			if assignee is not None or claimed_by is not None:
				continue

			# **A date field a document has not got means *no* documents, never all of
			# them** (`#815`). `completed_at`, `due_at`, `snoozed_until` and `starts_at` are
			# task fields — §6.14 says a document is not scheduled — so asking *what was
			# completed yesterday* is a question about tasks, and a document half that
			# ignored the filter would answer it by adding every decision in the workspace.
			# That is precisely the `--type bug` defect described below, and the reason it
			# is worth naming twice is that this one widens a list the user asked to narrow.
			if any(
				name.partition(subroutine.domain.filtering.SEPARATOR)[0]
				not in subroutine.domain.filtering.DOCUMENT_FILTERS
				for name in (filters or {})
			):
				continue

			# **Asked of documents as well, and a kind without that key contributes
			# nothing.** `--type bug` must not return every decision in the workspace,
			# which is what it did until this was driven against the real instance: the
			# filter reached tasks and the documents call below ignored it, so narrowing
			# the list *widened* the part of it nobody had filtered. A status and a type
			# are separate vocabularies per entity (§5.5), so `bug` being absent from the
			# document types is the ordinary answer — no documents — rather than an error.
			try:
				found_documents = client.documents(
					workspace=workspace.slug,
					limit=asked,
					order=order if shared else None,
					project=project,
					q=q,
					deleted=trash,
					status=status,
					type=type,
					tag=tag,
					filters=filters,
				)

			except subroutine.errors.NotFound as absent:
				# **`#332`'s tolerance, which this half never had** (`#1468`). The task call
				# above has caught an absent project per workspace since a second workspace
				# existed; this one did not, so a project that lives in one workspace escaped
				# from here and was reported as though it did not exist anywhere — while the
				# caller had just listed it.
				#
				# **It is reachable because tasks and documents resolve in opposite orders**,
				# and both clients agree with each other: a task listing resolves the status
				# first, a document listing resolves the project first. So in a workspace that
				# has no such project, `--status <nonsense>` makes the task call raise about
				# the *status* rather than the project — which falls through instead of
				# skipping the workspace — and this call then ran where the project does not
				# exist, with nothing to catch it.
				#
				# Same rule as above: only a named project may legitimately be absent from a
				# workspace the caller can otherwise read.
				if project is None:
					raise

				absent_project = absent
				found_documents = subroutine.clients.base.Listing()

			except subroutine.errors.ValidationError as unknown:
				if not {problem.field for problem in unknown.errors} & _PER_WORKSPACE_WORDS:
					raise

				# **The first refusal wins.** Both vocabularies rejecting the key is what
				# makes it a typo, and somebody typing `--status` means a task's status far
				# more often than a document's — so reporting the document one, purely
				# because it was asked second, names the less likely of two right answers.
				unknown_word = unknown_word or unknown
				found_documents = subroutine.clients.base.Listing()

			else:
				# **Documents answering is reaching this workspace too.** Without this,
				# `--status active` — which no *task* status matches — would collect every
				# document in force and then throw them away, because the task half had
				# refused in every workspace and `missing` would be raised below.
				#
				# *Answering*, not *matching*: a key both vocabularies reject is a typo and
				# must still be refused by name, which is the difference between this and
				# "a filter was asked for".
				reached = True

			cut = cut or found_documents.has_more
			rows.extend((client.connection.name, found) for found in found_documents)

		# **Refused by name when the key is nowhere on this connection.** A project that
		# exists and holds nothing answers "nothing on your list"; a project that does not
		# exist has to say so, or a mistyped `--project` is indistinguishable from an
		# empty one. Raised rather than returned so `fanout` reports it per connection —
		# a key on one instance and not another is a fact about that instance, and the
		# other one's rows still arrive.
		# **The vocabulary refusal first**, per the two slots above: it is true wherever it was
		# raised, where the project one is false as soon as the key resolved anywhere.
		missing = unknown_word or absent_project

		if missing is not None and not reached:
			raise missing

		# Re-sorted after the merge, because a merged result is a merge of pages and not
		# one ordered page — the limit is per workspace and has to be applied again here.
		# The domain owns the comparison so that the merged order matches the order each
		# page arrived in, NULLS LAST included (§10.3): a document sorts last in a list
		# ranked by priority, which is the same answer §6.3a gives an unranked task.
		#
		# **On the order the answer is actually in, which is not always `merging`** (`#1012`).
		# This sorted by the *parsed* order and then cut — and `relevance` is not in that,
		# because it enters the vocabulary per request and `_ordering` parses against the
		# static one. So a search was cut on `-created_at` and only then re-merged by
		# relevance one level up, which throws the best matches away before anything ranks
		# them. Measured on the served instance: `search timezone --limit 4` answered
		# `989 906 904 1001` where the top four by relevance are `4 989 525 827` — and the
		# top match appeared at no limit below the one that cut nothing at all.
		#
		# `#878` fixed the merge in `_listed` and left this one, so the ranking was applied
		# to whatever the wrong ordering had happened to keep. `#71`'s shape, one layer down:
		# an ordering chosen by the server and discarded above it, where the output looks
		# entirely reasonable.
		rows = subroutine.domain.ordering.merged(
			rows,
			key=lambda row: row[1],
			order=subroutine.domain.ordering.merge_order(
				order,
				merging,
				ranked=any(
					getattr(row[1], subroutine.domain.ordering.RELEVANCE, None) is not None
					for row in rows
				),
			),
		)

		# **What was cut is carried, not discarded.** `rows[:limit]` used to be the end of
		# it, so a backlog longer than the limit simply stopped — no count, no marker —
		# and "it is not in the list" quietly stopped meaning "it does not exist", which
		# is the one inference ref addressing is built to support. The agenda had always
		# reported its own remainder; this is the same fact, carried the same way.
		return Listing(
			rows=rows[:limit],
			more=cut or len(rows) > limit,
			parked=parked,
			held_back=held_back,
		)

	return subroutine.fanout.gather(world.clients, ask, strict=strict)


def _acted (world: World, located: Located, verb: str) -> str:
	"""Return what to say after acting on a task, naming it absolutely when that matters.

	A command that acts on a *relative* address should say what it actually acted on —
	the moment of consequence is where a confirmation belongs, the same reason
	``git commit`` prints the branch and the sha. But only where the address could have
	been relative: with one workspace on one connection there is nothing to disambiguate,
	and adding ``#1`` to "Done: Buy wine" is noise for the person §1.4 exists to protect.

	``add`` uses this too (`#279`), and the guard is exactly right for it: the misfile
	it exists to prevent is only *possible* where there is more than one place to file
	to, which is the same condition.
	"""

	if not world.qualifies_workspace and not world.qualifies_connection:
		return f"{verb}: {located.title}"

	absolute = subroutine.domain.refs.format_address(
		located.ref,
		workspace=located.workspace,
		connection=located.connection if world.qualifies_connection else None,
	)

	return f"{verb}: {absolute}  {located.title}"


def _say_where_a_bare_number_goes (
	world: World, *, console: rich.console.Console
) -> None:
	"""Name the current context under a listing, when there is more than one to be in.

	**Not a banner, and the distinction is the whole of `#271`.** §13.7's argument for
	leaving it unsaid is that forgetting your context cannot cost you a *missed* item,
	because reads span everything reachable — and that is true and is why there is no
	banner on every response. It does not cover a **write**. Somebody reading a list of
	fifty rows and typing a number off it is one keystroke from acting on the right number
	in the wrong place, and the only thing telling them which place is which rows happen to
	be printed bare.

	Silent with one connection, so §13.5b's transcript and everybody who has never heard of
	a connection see exactly what they saw before.
	"""

	if not world.qualifies_connection:
		return

	# The bare address rather than `describe`, which carries its provenance — useful when
	# somebody asks where the context came from, and one clause too many under a list they
	# are about to act on.
	where = world.current.workspace or "(no workspace chosen)"
	said = f"{world.current.connection}/{where}"

	# **Except when the context will not outlive the command** (`#281`). Under `-c`/`-w`
	# the sentence is true of the listing above and false of the next command, which is
	# the one the reader is about to type — and it was read exactly that way: as evidence
	# that the stored context had changed. Naming the source is what separates them, and
	# `describe` already words it.
	if not world.current.persists:
		said = world.current.describe(qualified=True)

	console.print(
		rich.text.Text(
			f"      A bare number means {said}. 'subroutine use' to change it.",
			style=DETAIL,
		)
	)


def _say_journal (
	world: World,
	gathered: subroutine.fanout.Gathered[list[subroutine.views.JournalEntry]],
	*,
	console: rich.console.Console,
	say: typing.Callable[[str], None],
) -> None:
	"""Print what happened, grouped by connection and then by day — `#1430`.

	**The same grouping `_say_changes` uses and for a different reason.** There it is arithmetic:
	a resume number belongs to one instance, so an interleaved list would carry two. Here it is
	that a day is what somebody asked about — *what did we do on Friday* — so the day is the
	heading whether or not more than one connection answered.

	**No resume line**, which is the visible half of decision `#1429`. A journal is a statement
	about a period and is not resumable; somebody wanting the entries before these narrows the
	period rather than carrying a number forward.
	"""

	for answer in gathered.answers:
		if world.qualifies_connection:
			console.print(rich.text.Text(answer.connection.label, style=HEADING))

		if not answer.value:
			console.print(rich.text.Text("  Nothing happened in that time.", style=DETAIL))
			say("")

			continue

		# **The account's zone, not this machine's** (`#1091`, decision `#1088`) — and it
		# matters more here than on the feed, because the day is what was asked for rather than
		# just how the answer is grouped.
		named = world.account_zone(answer.connection.name, None)
		zone = subroutine.domain.dates.zone(named)
		day = None

		# **Measured over the page, exactly as `shaping.aligned` measures a column.** A name is
		# three characters or twelve depending on whether a person or the instance acted, and an
		# unaligned one is what `#1424` was filed about in the browser.
		who_wide = max(
			(len(entry.actor or "the instance") for entry in answer.value), default=0
		)

		for entry in answer.value:
			when = entry.created_at.astimezone(zone)
			fell_on = subroutine.domain.schedule.day_in(entry.created_at, named)

			if fell_on != day:
				day = fell_on

				console.print(rich.text.Text(f"  {when:%a %d %b}", style=HEADING))

			console.print(f"    {when:%H:%M}  {_journal_line(entry, who_wide=who_wide)}")

			for detail in _journal_detail(entry):
				# **Wrapped here rather than left to the console.** `overflow="fold"` on a
				# `Text` was the first version and it *cut* the line instead — a comment ended
				# mid-word with no ellipsis, which reads as a truncated body rather than as a
				# wrapped one. Found by driving it; no test would have said anything, because
				# every assertion about a comment's text was made against the API.
				#
				# **Indented to the same column the line above starts at**, so a body reads as
				# belonging to its entry rather than as a new one.
				for line in textwrap.wrap(
					detail,
					width=max(console.width - 14, 20),
					initial_indent=" " * 12,
					subsequent_indent=" " * 12,
				) or [" " * 12]:
					console.print(rich.text.Text(line, style=DETAIL))

		say("")

	for failure in gathered.failures:
		console.print(rich.text.Text(failure.describe(), style=LATE))


def _say_changes (
	world: World,
	gathered: subroutine.fanout.Gathered[list[subroutine.views.Event]],
	*,
	console: rich.console.Console,
	say: typing.Callable[[str], None],
) -> None:
	"""Print what moved, grouped by connection and then by day.

	**Grouped rather than merged**, unlike ``today``. §13.7 makes that call per command,
	and the reason here is arithmetic rather than taste: a resume number belongs to one
	instance, so a single interleaved list would carry two of them and no way to say which
	row ended which.

	The last number is printed at the end because it is the whole point — a feed you
	cannot resume from is a feed you have to read twice.
	"""

	for answer in gathered.answers:
		if world.qualifies_connection:
			console.print(rich.text.Text(answer.connection.label, style=HEADING))

		if not answer.value:
			console.print(rich.text.Text("  Nothing new.", style=DETAIL))
			say("")

			continue

		# **The account's zone, not this machine's** (`#1091`, decision `#1088`). This was a
		# bare ``.astimezone()``, which is the laptop's — and it is the *heading* work is
		# grouped under, so on the wrong side of midnight it put two days' events under one
		# date and called it by the earlier name.
		named = world.account_zone(answer.connection.name, None)
		zone = subroutine.domain.dates.zone(named)
		day = None

		for event in answer.value:
			when = event.created_at.astimezone(zone)
			fell_on = subroutine.domain.schedule.day_in(event.created_at, named)

			if fell_on != day:
				day = fell_on

				console.print(rich.text.Text(f"  {when:%a %d %b}", style=HEADING))

			console.print(f"    {when:%H:%M}  {_change_line(event)}")

		last = answer.value[-1].seq

		say("")
		_suggest(console, f"subroutine changes --since {last}", "carry on from here")
		say("")

	for failure in gathered.failures:
		console.print(rich.text.Text(failure.describe(), style=LATE))


# --- Helpers that take the program with them ------------------------------------------
#
# **Twenty more out of `register`'s closure, threaded through :class:`Program`** (`#943`).
# Each wanted one or two of the nine names it used to see all of; the object is what makes
# that a parameter rather than nine ways to be inconsistent about it.


def _settled (
	program: Program,
	roster: subroutine.connections.Roster,
	current: subroutine.context.Current,
	reached: typing.Sequence[Reached],
	marker: subroutine.directory.Marker | None,
) -> subroutine.context.Current:
	"""Answer steps 4 and 5 of §13.7's order, now that the connections have been asked.

	**And drop a marker that names somewhere this connection has never heard of** — the
	one thing a marker must not do is break the program (`#166`). It is advisory context
	written by a machine into a directory, so a checkout marked for one instance must not
	stop every command working against another; `--profile` puts a second instance one
	flag away, and the suite itself proved the point by failing 154 tests the first time
	this repository carried its own marker.

	Anything a person typed *now* still refuses, loudly. The difference is who said it and
	when.
	"""

	# **Said here rather than before the fan-out** (`#409`, moved by `#556`). Until the
	# connections have been asked there is no way to know whether the marker can still be
	# honoured, and a line saying "using X instead" printed above one that quietly used Y
	# is the two-lines-of-one-act disagreement `#414` found.
	if current.unusable_marker_connection is not None:
		if current.marker_found_on is None:
			program.warn(
				f"{FILE_NAME} here names connection "
				f"{current.unusable_marker_connection!r}, which is not configured. "
				f"Using {current.connection!r} instead."
			)

		else:
			# Not silence: the marker was honoured, and the file still names something
			# this machine does not have, which somebody will want to put right.
			program.warn(
				f"{FILE_NAME} here names connection "
				f"{current.unusable_marker_connection!r}, which is not configured — its "
				f"workspace is on {current.marker_found_on!r}, so that is where this goes."
			)

	if (
		current.workspace is not None
		and current.workspace_source == subroutine.context.FROM_DIRECTORY
	):
		here = next((item for item in reached if item.name == current.connection), None)
		# **Resolved by id where the marker carries one** (`#317`), so a workspace that has
		# merely been renamed is followed rather than reported missing. Without it a
		# `workspace rename` left every marked checkout printing the warning below on every
		# command for ever — about nothing, since `project_id` went on carrying the work to
		# the right place. Markers written before `#317` have no id and fall back to the
		# slug, which is what they have always done.
		resolved = (
			None
			if here is None or marker is None
			else subroutine.directory.resolve_workspace(marker, here.identity.workspaces)
		)

		if here is not None and resolved is None:
			# **Dropped to the next source in the chain, not to nothing** (`#324`). This
			# assigned `workspace=None` and fell straight through to the sole-workspace
			# default — so on an instance with two, a stale marker *erased* a perfectly
			# good stored context and turned `use --here --project SR` into a refusal
			# immediately after `use projects` had succeeded. The warning said "Ignoring
			# it", and ignoring it is the one thing it did not do.
			#
			# Only the stored context can be the fallback, and that is not a choice: the
			# marker won in the first place because the flag and the environment were
			# empty, so §13.7's order has exactly one step left.
			instead = subroutine.context.stored_workspace(current.connection)
			usable = instead is not None and any(
				workspace.slug == instead for workspace in here.identity.workspaces
			)

			program.warn(
				f"{FILE_NAME} here names workspace {current.workspace!r}, which is not on "
				f"{current.connection}. "
				+ (
					f"Using {instead!r} instead."
					if usable
					else "Ignoring it."
				)
			)
			current = (
				current.with_workspace(
					typing.cast("str", instead), subroutine.context.FROM_STORED
				)
				if usable
				else dataclasses.replace(
					current,
					workspace=None,
					workspace_source=subroutine.context.FROM_NOTHING,
				)
			)

		elif resolved is not None and resolved != current.workspace:
			current = dataclasses.replace(current, workspace=resolved)

	if current.workspace is not None:
		return current

	here = next((item for item in reached if item.name == current.connection), None)

	if here is None or not here.identity.workspaces:
		return current

	if len(here.identity.workspaces) == 1:
		return current.with_workspace(
			here.identity.workspaces[0].slug, subroutine.context.FROM_SOLE
		)

	# Deliberately *not* refused here. A read spans everything reachable and needs no
	# context at all, so refusing at this point would stop `subroutine agenda` working
	# for anybody with two workspaces — which is precisely the person §13.7 is for.
	# The refusal belongs to the write, and `_writing_workspace` makes it.
	return current


def _several (program: Program, given: str) -> list[str]:
	"""Split one argument into the refs it names, refusing anything that is not one — `#1352`.

	**The refusal is here rather than at :func:`_locate`**, because the two say different
	things. *There is no #9 in projects* is a question about what exists; this is a question
	about what was typed, and a caller who wrote ``9;11`` needs to be told about the separator
	rather than sent looking for an item.

	**An argument with no comma in it is handed straight back, untouched**, so this cannot
	narrow the command it is widening: whatever :func:`_locate` accepted before, it still gets,
	unexamined. Written that way rather than routed through :func:`parse_refs` because that
	reads numbers and a single argument may not be one — measured, and what it refuses is not
	what I first assumed, which is why this says *unchanged* rather than naming a form.

	**The offending entry is named, and finding it is a report rather than a second rule.**
	:func:`parse_refs` decides; this walks the same values afterwards only to say which one
	failed, because *'3,nope,4' is not a list of item numbers* sends somebody to check all
	three.
	"""

	if subroutine.domain.refs.LIST_SEPARATOR not in given:
		return [given]

	written = [one.strip() for one in given.split(subroutine.domain.refs.LIST_SEPARATOR) if one.strip()]

	if subroutine.domain.refs.parse_refs(given) is None:
		bad = [one for one in written if subroutine.domain.refs.parse_ref(one) is None]
		# **Built before the message rather than inside it.** Written as
		# `f"{', '.join(...) or given!r} …"` first, where `!r` applies to the whole `or`
		# expression rather than to its right-hand side — so a single bad entry was quoted
		# twice and printed as `"'nope'"`. Found by running it.
		named = ", ".join(repr(one) for one in bad) or repr(given)

		program.fail(
			subroutine.errors.ValidationError(
				f"{named} is not an item number."
				if len(bad) < 2
				else f"{named} are not item numbers.",
				hint="Every one has to be a number 'subroutine list' prints, separated by "
				"commas — as in '9,11,12'.",
			)
		)

	return written


def _locate (
	program: Program,
	world: World,
	given: str,
	*,
	kinds: tuple[str, ...] = TASKS_ONLY,
	verb: str = "done",
) -> Located:
	"""Resolve an address into one item, or refuse in a way that can be acted on.

	**A bare number means the current context** (§13.7), which is what makes a number
	typeable at all: refs are per-workspace, so every low number exists nearly everywhere
	and a search for one is ambiguous by construction. ``acme/42`` says which workspace
	and ``work/acme/42`` says which instance.

	Two things soften that without weakening it. When a bare number is *not* in the
	current context, everywhere else is asked before refusing — not to guess, but so the
	refusal can say where it is instead. And when nothing has chosen a context at all,
	everywhere is searched: one match is not ambiguous and refusing it would be pedantry,
	while several is a refusal **naming the candidates with their titles**, so the choice
	can be made without a second command.

	Never a guess, in any of those paths. Until 2026-07-29 this resolved a bare ref with
	``.first()`` on an unordered query across every readable workspace, so two workspaces
	each holding a ``#1`` was enough to complete whichever row the database happened to
	return — the same defect as the positional numbering this replaced.

	``kinds`` is what a *reading* command needs and an acting one does not: ``show`` will
	take a document, ``done`` will not. Both search the same way, because a number that
	names a document has to be *found* before it can be turned down — telling somebody
	``#4`` does not exist, when it is the plan they were just reading, is worse than
	telling them it is not a task.
	"""

	address = subroutine.domain.refs.parse_address(given)

	if address is None:
		program.stop(
			f"{given!r} is not an item number.",
			"Items are named by the number 'subroutine list' prints beside them — "
			f"'subroutine {verb} 42'.",
		)

	named = _named_place(program, world, address)

	if named is None:
		return _unqualified(program, world, address.ref, given, kinds=kinds, verb=verb)

	found = _found_at(named[0], named[1], address.ref, kinds)

	if found is not None:
		return Located(connection=named[0].name, workspace=named[1], item=found)

	# Not in the place the address named. Ask everywhere else before giving up, so the
	# refusal can say where it *is* — the docstring above promised this and the code did
	# not do it, which is the documented-but-absent shape this project keeps meeting.
	# Only for a *bare* number: if the caller named a workspace, they meant that one.
	elsewhere = (
		[]
		if address.workspace is not None or address.connection is not None
		else [
			item
			for item in _everywhere(world, address.ref, kinds)
			if item.workspace != named[1]
		]
	)

	if not elsewhere:
		program.stop(
			f"There is no {subroutine.domain.refs.format_ref(address.ref)}"
			f"{_in_place(world, named[1])}.",
			"Run 'subroutine list' to see what there is — or "
			"'subroutine list --trash' if you deleted it.",
		)

	shown = [_absolute(world, item) for item in elsewhere]
	width = max(len(text) for text in shown)
	listed = "\n".join(
		f"    {text:>{width}}  {item.title}"
		for text, item in zip(shown, elsewhere, strict=True)
	)

	program.stop(
		f"There is no {subroutine.domain.refs.format_ref(address.ref)}"
		f"{_in_place(world, named[1])}, but there is one here:\n{listed}",
		f"Say which — 'subroutine {verb} "
		f"{shown[0].replace(subroutine.domain.refs.SIGIL, '')}'.",
	)


def _a_task (program: Program, world: World, given: str, *, verb: str) -> tuple[Located, subroutine.views.Task]:
	"""Resolve an address into a task, turning down a document by saying what it is.

	**Documents are searched even though they cannot be acted on**, which looks like extra
	work and is the whole value: refs are allocated from one counter per workspace, so a
	command that only asked about tasks answered "there is no #4" about a specification
	sitting in the same listing. Being told ``#4`` is a document, with its title, is an
	answer somebody can act on; being told it does not exist is not.
	"""

	located = _locate(program, world, given, kinds=ANY_ITEM, verb=verb)
	found = located.item

	if not isinstance(found, subroutine.views.Task):
		# The shortest form, not the absolute one: the caller named this item directly,
		# so echoing back a qualified address they did not type reads as a correction.
		shown = world.address_of_located(located)

		program.stop(
			f"{shown} is a document, not a task — {found.title}",
			f"'subroutine {verb}' works on tasks. Read this one with 'subroutine show "
			f"{shown.replace(subroutine.domain.refs.SIGIL, '')}'.",
		)

	return located, found


def _a_document (
	program: Program,
	world: World, given: str, *, verb: str
) -> tuple[Located, subroutine.views.Document]:
	"""Resolve an address into a document, turning down a task by saying what it is.

	``_a_task``'s argument run the other way (§12.2c, `#42`), and it needed making the
	moment a document command took a ref: one counter per workspace serves both kinds, so
	``doc edit 3`` may name a task perfectly reasonably — and "there is no #3" about
	something sitting in the listing the reader just printed is exactly the answer that
	item exists to stop being given.
	"""

	located = _locate(program, world, given, kinds=ANY_ITEM, verb=verb)
	found = located.item

	if not isinstance(found, subroutine.views.Document):
		shown = world.address_of_located(located)
		bare = shown.replace(subroutine.domain.refs.SIGIL, "")

		program.stop(
			f"{shown} is a task, not a document — {found.title}",
			f"'subroutine document {verb}' works on documents. Change this one with "
			f"'subroutine update {bare}'.",
		)

	return located, found


def _named_place (
	program: Program,
	world: World, address: subroutine.domain.refs.Address
) -> tuple[Reached, str] | None:
	"""Return the place an address names, or ``None`` when it named none.

	``None`` covers a bare number with no context chosen, which is the one case that has
	to go looking; every other case has somewhere definite to ask.
	"""

	name = address.connection or world.current.connection
	item = world.connection(name)

	if item is None:
		program.stop(
			f"There is no connection called {name!r} here, or it could not be reached.",
			world.roster.alternatives(),
		)

	wanted = address.workspace or world.current.workspace

	if wanted is None:
		return None

	if item.identity.workspace(wanted) is None:
		program.stop(f"There is nothing called {wanted!r} on {item.name}.", _workspace_hint(item))

	return item, wanted


def _unqualified (
	program: Program,
	world: World, ref: int, given: str, *, kinds: tuple[str, ...], verb: str
) -> Located:
	"""Resolve a bare number when nothing has chosen a context, or refuse with the choice."""

	candidates = _everywhere(world, ref, kinds)

	if not candidates:
		program.stop(
			f"There is no {subroutine.domain.refs.format_ref(ref)} here.",
			"Run 'subroutine list' to see what there is — or "
			"'subroutine list --trash' if you deleted it.",
		)

	if len(candidates) == 1:
		return candidates[0]

	shown = [_absolute(world, item) for item in candidates]
	width = max(len(text) for text in shown)
	listed = "\n".join(
		f"    {text:>{width}}  {item.title}"
		for text, item in zip(shown, candidates, strict=True)
	)

	program.stop(
		f"{given!r} could mean any of these:\n{listed}",
		f"Say which — 'subroutine {verb} "
		f"{shown[0].replace(subroutine.domain.refs.SIGIL, '')}', or "
		f"'subroutine use {_place_of(world, candidates[0])}' to keep working there.",
	)


def _listed (
	program: Program,
	*,
	limit: int,
	json_output: bool,
	merged: bool,
	strict: bool,
	order: str | None = None,
	project: str | None = None,
	connection: str | None = None,
	deferred: bool = False,
	q: str | None = None,
	ready: bool = False,
	trash: bool = False,
	assignee: str | None = None,
	claimed_by: str | None = None,
	status: str | None = None,
	type: str | None = None,
	tag: str | None = None,
	filters: dict[str, str] | None = None,
) -> None:
	"""Print the list. Registered twice — three times, with ``search`` — from one body."""

	# **The scripted path is never narrowed by a presentation rule.** Hiding parked work
	# is a decision about a list somebody *reads*, which is what §6.5's "default views"
	# means and the whole basis for leaving the API default alone. `--json` is the other
	# half of that: a script asking for open work must not silently lose rows, and every
	# row already carries `snoozed_until`, so it can make the same choice for itself.
	#
	# So the two outputs differ, deliberately, and only in this. It is the one place
	# §12.2a's "the human path and the scripted path are the same code" gives way — the
	# code is the same, the presentation rule is not applied.
	hiding = not deferred and not json_output

	# **Deferred work sinks to the bottom of a list it is in — `#877`, Simon's decision of
	# 2026-08-14**: *"deferred items appearing last. That way they are not invisible, but
	# neither are they confused with non-deferred items in lists."* A leading sort key, so
	# it holds under every ordering rather than only under the default.
	#
	# **Not while hiding, because there is nothing to sink**, and **not while searching**,
	# which is the exception worth stating: a search is ordered by how well a row answers
	# the question asked, and a deferred item is still the best answer to it. `#867` is the
	# case that decides it — typing a number finds that item, and sinking would put it
	# below every row that merely mentions the digits.
	sunk = _sunk(order) if not hiding and not q else order

	# Grouped by connection, one heading each, so the same instance under two names is
	# shown twice rather than counted twice — which is what the file says (`#327`).
	with program.opened(strict=strict) as world:
		if connection is not None:
			world = _only_this_connection(program, world, connection)

		gathered = _listing(
			world,
			limit=limit,
			strict=strict,
			order=sunk,
			project=project,
			deferred=not hiding,
			q=q,
			ready=ready,
			trash=trash,
			assignee=assignee,
			claimed_by=claimed_by,
			status=status,
			type=type,
			tag=tag,
			filters=filters,
		)

		_report(program, world, gathered.failures)

		# **The order that was *asked for*, not the one the reader typed.** The merge has to
		# compare rows by the same keys the server paged them with, or a page boundary
		# lands where the next page does not start (`#782`) — so the sunk spelling goes to
		# both, from one variable, and the two cannot disagree about the leading key.
		# **Flattened only where the output is flat** (`#942`). This used to run before the
		# branch below, so a grouped listing paid for a merge it never printed — harmless
		# until the merge became the thing that refuses two connections naming one
		# instance, at which point computing it eagerly would have refused the one path
		# that handles them correctly.
		flat = functools.partial(
			_merged, world, gathered, order=_merge_order(sunk, gathered)
		)
		empty = not any(answer.value.rows for answer in gathered.answers)
		more = any(answer.value.more for answer in gathered.answers)

		if json_output:
			program.say(
				json.dumps(
					[_as_json(world, name, item, term=q) for name, item in flat()], indent=2
				)
			)

			return

		if empty and q:
			# **Not "nothing on your list".** The list is not empty; this search found
			# nothing in it, and saying the first about the second is how somebody
			# concludes their data is gone. The remedy named is the widening one,
			# because a search that missed is usually a search that was too narrow.
			program.say(f"Nothing matches {q!r}.")

			if not deferred:
				_suggest(program.console, f'subroutine search "{q}" --deferred', "look in what you have put off too")

			return

		if empty:
			# **"Nothing on your list" is a claim, and it is false when something refused
			# to answer.** The failure has already been named on stderr; following it with
			# a cheerful empty list says the opposite — that the question was put and the
			# answer was none — and then suggests adding a task, which is wrong advice
			# about a question that was never answered.
			#
			# `--project` is what made this ordinary rather than rare: before it, an empty
			# listing with a failure meant a server was down, and now it means a typo'd
			# key, which reads exactly like a project that happens to be empty.
			if gathered.failures or world.unreachable:
				program.say("Nothing to show — some of what you asked for could not be read.")

				return

			# And it is equally false when everything on the list is simply parked, which
			# is the case a person hits after deferring the last thing they were avoiding.
			# Telling them to add something would be advice about a list they have.
			if hiding and any(answer.value.parked for answer in gathered.answers):
				# Not "nothing to do today" — that is the agenda's sentence, and `list` is
				# not the agenda. What is true is that everything open starts later.
				program.say("Nothing you can start yet.")
				_say_parked(gathered, console=program.console, hidden=True)

				return

			program.say("Nothing on your list.")
			_suggest(program.console, 'subroutine add "something to do"')

			return

		# **`shown` is the order the reader is looking at**, which the two paths disagree
		# about: flat re-sorts across connections, grouped prints each connection's own
		# order under its own heading. The tip below names a row, and it should name one
		# near the top of the page rather than one the merge happened to rank first.
		#
		# **Grouping is a concatenation, not a merge**, which is why it does not go through
		# `_across` (`#942`): nothing is combined into one ranked list, and two connections
		# naming one instance are shown under two headings — visible, which is the whole
		# reason that case is allowed here and refused in the flat one.
		# **Said only where it changes the answer** (`#986`). A prioritised project raises work
		# inside a *ranked* order and does nothing to a listing sorted newest-first, so
		# announcing it over every list would be a sentence a reader has to learn to ignore —
		# and one that claims an effect the page does not show. `project prioritise` with no
		# argument is where the question is asked directly.
		if _ranked_by_priority(order):
			_say_prioritised(world, program.console)

		if merged or not world.qualifies_connection:
			shown = flat()

			_flat(world, shown, console=program.console, term=q, project=project)

		else:
			shown = [
				(answer.connection.name, row[1])
				for answer in gathered.answers
				for row in answer.value.rows
			]

			_grouped(
				world,
				gathered,
				console=program.console,
				say=program.say,
				term=q,
				project=project,
			)

		if more:
			# The agenda has always said this about its own remainder; the list said
			# nothing at all and simply stopped. Phrased as an instruction rather than a
			# bare "there are more", because the reader's next question is how to see them.
			#
			# **It repeats the narrowing it was given.** A suggestion that dropped
			# `--project` or `--order` would widen the list while claiming to extend it,
			# and the reader would blame the flag rather than the advice.
			repeated = (
				f"subroutine search {shlex.quote(q)} --limit {limit * 2}"
				if q
				else f"subroutine list --limit {limit * 2}"
			)

			if order:
				repeated += f" --order {order}"

			if project:
				repeated += f" --project {project}"

			if deferred:
				repeated += " --deferred"

			program.console.print(
				rich.text.Text(f"      …and more. '{repeated}' to see further.", style=DETAIL)
			)

		_say_parked(gathered, console=program.console, hidden=hiding)

		_say_held_back(gathered, console=program.console)

		_say_where_a_bare_number_goes(world, console=program.console)

		program.say("")

		# **The trash gets its own tip, because the ordinary one refuses every row it was
		# printed under** (`#693`). `show` does not find a deleted item, so the generic
		# suggestion was wrong for the whole of this listing rather than for an unlucky
		# row — and the refusal it earned then pointed at plain `list`, which is precisely
		# where a deleted item is not. `restore` is what a reader of this list wants, and
		# `delete` named it a moment earlier.
		address = _typeable(world, shown[0][0], shown[0][1])

		_suggest(
			program.console,
			f"subroutine restore {address}" if trash else f"subroutine show {address}",
			"put one of them back" if trash else "read one of them in full",
		)


def _filters (program: Program, dated: typing.Sequence[str] | None) -> dict[str, str]:
	"""Read every ``--filter`` a command was given, refusing what is not one — `#815`.

	**Through ``fail`` rather than by raising**, which is the difference between a sentence
	somebody can act on and a traceback. A refusal raised in a command body escapes past
	``opened``'s handler, and only ``main``'s outermost catch renders it — so a person sees
	the right thing and anything driving the app through Click's runner sees nothing at all.
	Here so that all three registrations share it, and so the message is asserted where it
	is produced.
	"""

	try:
		return subroutine.domain.filtering.parsed(dated or [])

	except subroutine.errors.SubroutineError as error:
		program.fail(error)


def _refuse_words (program: Program, words: list[str] | None, looking_for: str) -> None:
	"""Send somebody who tried to search a listing to the command that searches.

	**Three shapes, one signpost** (`#282`). ``list -q words``, ``list --search words`` and
	a bare ``list words`` were three different refusals naming neither each other nor
	``search`` — and Click's did-you-mean made the middle one actively misleading by
	offering ``--strict``, so the one message that tried to help pointed away from the
	answer. §12.2a: a dead end where a signpost would do.

	Caught as *hidden parameters* rather than by reading Click's usage errors, because
	that keeps this in Typer's own vocabulary and out of an undeclared dependency's
	internals. They refuse rather than search: `list` takes filters and `search` takes
	words, and a hidden flag that quietly did the other command's job would be the second
	way to do one thing that the `ls` synonym is hidden to avoid.
	"""

	wanted = " ".join(words or []).strip() or looking_for.strip()

	if not wanted:
		return

	program.fail(
		subroutine.errors.ValidationError(
			"'subroutine list' filters what you have; it does not search it.",
			hint=f'Try: subroutine search "{wanted}"',
		)
	)


def _status_in (
	client: subroutine.clients.base.Client,
	*,
	workspace: str | None,
	category: str,
	verb: str,
) -> str:
	"""Return the key of this workspace's status in a category, whatever it calls it.

	**A status key is data an installation renames** (§5.5), so a command may not send one it
	made up. `done` has never had to: it goes through a verb route and the server resolves the
	category with `domain.tasks.status_key_in`. `start` and `stop` send a `PATCH`, so the
	resolution has to happen on this side — and `/v1/meta` publishes it, one key per status
	with the fixed category beside it, in the same `position` order that function reads.

	`#1036` met this a layer over and chose the same remedy: the mapping is published, so ask
	for it rather than restating it. The cost is one round trip on two commands that were
	already making one.

	**This is the CLI's only call to `meta`, and it must stay the only kind.** `test_reach`
	carried the argument until this landed and its register cannot hold it now, so it lives
	here: §1.4 requires a person keeping a to-do list never to read a vocabulary listing before
	setting a status, so a wrong key is refused by name with the alternatives beside it,
	`explain` carries the grammars (`#154`), and the words somebody can use are the ones the
	program prints back at them. **There is deliberately no `subroutine meta` command**, and
	calling it *in order to answer a question the reader never asked* is the opposite of
	offering it as one. An agent is the asymmetric case — holding a path and a body, with no
	point of use to be corrected at — which is why the same thing is an MCP resource (`#486`).
	"""

	meta = client.meta(workspace=workspace)

	for status in meta.statuses.get("task", []):
		if status.category == category:
			return status.key

	# **Reachable, and the refusal has to say what to do.** A workspace whose vocabulary is
	# editable (`#826`) can delete every status in a category, and then this command has no
	# honest target. Naming the category rather than a key it might have had is the point: the
	# reader chose the names, so a made-up one would tell them nothing.
	raise subroutine.errors.ValidationError(
		f"There is nothing to {verb} to in this workspace.",
		hint=(
			f"'{verb}' moves work into the '{category}' part of the workflow and this "
			f"workspace has no status there."
		),
	)


def _moved_to (program: Program, which: str, category: str, *, verb: str, said: str) -> None:
	"""Move a task into a category of the workflow, in the shape `done` uses.

	One body for both, because they differ in two words. **Neither says "status"** — §13.5b
	forbids the vocabulary and does not need it: `done`, `plan` and `defer` are all actions
	that happen to set a field, and "Started: <title>" is the same shape as "Done: <title>".

	**A category, never a key** (`#1128`). These two used to send `"in_progress"` and `"open"`
	as literals, twenty lines from `done`, which resolves. See :func:`_status_in`.
	"""

	with program.opened() as world:
		located, task = _a_task(program,
			world,
			_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
			verb=verb,
		)

		if task.completed_at is not None:
			# Nothing else would go wrong, and this is the honest answer: picking up
			# something already ticked off is nearly always the wrong number.
			program.say(_acted(world, located, "Already done"))
			_suggest(program.console, "subroutine list", "everything still open")

			return

		client = _require_connection(program, world, located.connection)
		status = _status_in(
			client, workspace=located.workspace, category=category, verb=verb
		)
		moved = client.update(ref=task.ref, status=status, workspace=located.workspace)

		program.say(_acted(world, dataclasses.replace(located, item=moved), said))
		_suggest(program.console, "subroutine agenda")


def _in_an_editor (program: Program, current: str) -> str:
	"""Open text in the reader's editor and return what they saved.

	**`$VISUAL` before `$EDITOR`**, which is the older convention and still the right one:
	``VISUAL`` names the full-screen editor and ``EDITOR`` the line editor, so a terminal
	that can run the first should get it.

	Neither set is a refusal rather than a guess. Falling back to ``vi`` looks helpful
	until it is not installed, and then the failure is about a program the reader never
	chose — where "set EDITOR, or pass --body" is something they can act on (§13.5).

	The scratch file goes wherever the platform puts temporary files, never beside the
	checkout: this project's own working tree is on a share that does not honour
	create-then-rename, which is the hazard that has eaten files here before.
	"""

	chosen = os.environ.get("VISUAL", "").strip() or os.environ.get("EDITOR", "").strip()

	if not chosen:
		program.fail(
			subroutine.errors.ValidationError(
				"Nothing says which editor to open.",
				hint="Set $EDITOR, or pass --body, or pipe the text in.",
			)
		)

	with tempfile.NamedTemporaryFile(
		"w+", suffix=".md", delete=False, encoding="utf-8"
	) as handle:
		handle.write(current)
		path = pathlib.Path(handle.name)

	try:
		# `shlex.split`, so `EDITOR="code --wait"` works — an editor setting carrying
		# arguments is ordinary, and treating the whole string as a filename would look
		# for a program with a space in its name.
		subprocess.run([*shlex.split(chosen), str(path)], check=True)

		return path.read_text(encoding="utf-8")

	finally:
		path.unlink(missing_ok=True)


#: The filters that narrow a listing and take exactly one value each — `#1484`. Named here so
#: the refusal and the declarations cannot come to disagree about which flags are covered, and
#: so a sixth is a one-line addition rather than a fifth copy of the same three lines.
#:
#: **Not `--order`, `--limit` or `--connection`.** The first already takes several sort keys as
#: one comma-separated value, so repeating it asks a different question; the other two are not
#: narrowings. They are still last-wins and that is recorded on `#1484` rather than fixed here.
ONE_VALUE_EACH = (
	"--project", "--assignee", "--claimed-by", "--status", "--type", "--tag",
)

#: The five declared once, because three commands offer them and the wording is user-facing —
#: `JUST_THIS_ONE_OPTION`'s precedent, and for its reason: three copies is three places for a
#: help string to drift, and a flag whose wording differs between `list` and `search` reads as
#: two different flags. Typer builds its own parameter per command from one of these, so
#: sharing them changes nothing about what any command parses.
#:
#: **Each takes a list and each accepts one**, which is what makes the repetition *visible* to
#: :func:`_only_once`. Declared as a sequence and refused above one, rather than declared
#: singular and silently keeping the last (`#1484`).
PROJECT_OPTION = typer.Option(None, "--project", help="Only this project, by key.")
ASSIGNEE_OPTION = typer.Option(
	None, "--assignee", help="Only what is assigned to somebody. A username, or 'me'."
)
CLAIMED_BY_OPTION = typer.Option(
	None, "--claimed-by", help="Only what somebody is holding now. A username, or 'me'."
)
STATUS_OPTION = typer.Option(None, "--status", help="Only this status, e.g. 'blocked'.")
TYPE_OPTION = typer.Option(None, "--type", help="Only this type, e.g. 'bug'.")
#: **Without the `#`**, which a POSIX shell eats as a comment before this program sees it —
#: the same reason a ref is typed bare (§12.2a). Written `#home` in a captured line, asked for
#: as `--tag home` (`#1319`).
TAG_OPTION = typer.Option(None, "--tag", help="Only what carries this tag, without the '#'.")


def _only_once (program: Program, flag: str, given: typing.Sequence[str] | None) -> str | None:
	"""Return the one value a narrowing filter was given, refusing several — `#1484`.

	**Repeating one silently kept the last**, so `subroutine list --type finding --type note`
	answered about notes alone — and on a project holding none it printed *"Nothing on your
	list"*, which reads as *nothing has ever been filed here*. That is how it was found: an
	agent following the import process ran a four-type filter and concluded the project was
	empty. Same shape as `#1468` — a listing answering a narrower question than it was asked
	and not saying so.

	**Refused rather than unioned**, on Simon's decision of 2026-08-28. A union has to reach
	the domain, both clients and the published contract, and five filters share this shape; a
	refusal is one sentence where the flag is read. It is also the direction that costs nothing
	if the other is wanted later: refusing now does not stop us accepting a union, and unioning
	now and refusing later would be a break.

	**The API refuses the same thing at its own door** (`api/query.refuse_repeated`), so a
	terminal on a remote connection and one on a local database answer alike — which they would
	not if this lived only here.
	"""

	if not given:
		return None

	if len(given) > 1:
		program.stop(
			f"'{flag}' takes one value and was given {len(given)}: "
			f"{', '.join(repr(one) for one in given)}.",
			f"Ask for one of them. '{flag}' repeated kept only the last, which answered a "
			"narrower question than you asked without saying so.",
		)

	return given[0] or None


def _shown_list (
	program: Program,
	*,
	limit: int,
	json_output: bool,
	merged: bool,
	strict: bool,
	order: str,
	project: typing.Sequence[str] | None,
	connection: str,
	deferred: bool,
	ready: bool,
	trash: bool,
	assignee: typing.Sequence[str] | None,
	claimed_by: typing.Sequence[str] | None,
	status: typing.Sequence[str] | None,
	kind: typing.Sequence[str] | None,
	tag: typing.Sequence[str] | None,
	dated: typing.Sequence[str] | None,
) -> None:
	"""Print the list, for ``list`` and for its hidden synonym ``ls``.

	**Named around `_listing`, which was already taken** — that is the fan-out gatherer three
	thousand lines above, and Python takes the later binding, so the first version of this
	shadowed it silently (`#1409`'s shape, third recorded instance). mypy caught it; so would
	``tests/test_imports.py``, which refuses a module-level name bound twice.

	**One body, which is the rule this file already states about those two names**: they call
	one thing so they cannot drift into two. It was true of :func:`_listed` and not of the
	twenty lines in front of it, which were duplicated verbatim in both commands — so a filter
	added to one and not the other would have made the synonym quietly narrower.

	**Outside `register` because that closure only shrinks** (`#943`). It came out when `#1319`
	gave both commands a `--tag`, and taking it out is what paid for the option.

	``kind`` is `--type` to the reader and ``type=`` to the client: ``type`` is a builtin, and
	shadowing it inside a function that also annotates with ``str | None`` is how a signature
	comes to mean something it does not.
	"""

	_listed(program,
		limit=limit,
		json_output=json_output,
		merged=merged,
		strict=strict,
		order=order or None,
		project=_only_once(program, "--project", project),
		connection=connection or None,
		deferred=deferred,
		ready=ready,
		trash=trash,
		assignee=_only_once(program, "--assignee", assignee),
		claimed_by=_only_once(program, "--claimed-by", claimed_by),
		status=_only_once(program, "--status", status),
		type=_only_once(program, "--type", kind),
		tag=_only_once(program, "--tag", tag),
		filters=_filters(program, dated),
	)


def _searched (
	program: Program,
	*,
	terms: str,
	limit: int,
	json_output: bool,
	merged: bool,
	strict: bool,
	order: str,
	project: typing.Sequence[str] | None,
	tag: typing.Sequence[str] | None,
	connection: str,
	deferred: bool,
	dated: typing.Sequence[str] | None,
) -> None:
	"""Print what matches, by any of the words.

	**Outside `register` because that closure only shrinks**, which is the ratchet's own
	instruction: a command's body belongs in a function it calls. It came out when `#1319`
	gave three commands a `--tag`, which is `#943` working rather than being worked around.
	"""

	_listed(program,
		limit=limit,
		json_output=json_output,
		merged=merged,
		strict=strict,
		order=order or None,
		project=_only_once(program, "--project", project),
		tag=_only_once(program, "--tag", tag),
		connection=connection or None,
		deferred=deferred,
		q=_asked(terms, "What are you looking for?"),
		filters=_filters(program, dated),
	)


def _names_in_words (names: typing.Sequence[str]) -> str:
	"""Return names as a sentence rather than as a comma-separated list — "a, b and c".

	Written once because a refusal listing candidates and a line saying what is prioritised
	both need it, and two copies of an English rule are two places for a stray comma to
	appear in front of an *and*.
	"""

	if len(names) < 2:
		return "".join(names)

	return f"{', '.join(names[:-1])} and {names[-1]}"


def _no_such_project (
	program: Program, wanted: str, searched: typing.Sequence[str]
) -> typing.NoReturn:
	"""Refuse a project nobody can find, saying where it was looked for — `#1501`.

	**Where matters as soon as there is more than one place.** *"There is no project 'web'
	here"* is a complete answer on an instance with one workspace and an assertion the reader
	cannot check on an instance with four — they cannot tell a key they got wrong from a key
	in a workspace this credential does not reach.
	"""

	program.stop(
		f"There is no project {wanted!r} in {_names_in_words(searched)}."
		if len(searched) > 1
		else f"There is no project {wanted!r} here.",
		"Run 'subroutine project list' to see them, or "
		f"'subroutine project create {wanted.rsplit('/', 1)[-1]} \"A title\"' to make it.",
	)


def _adopted_project (
	program: Program, world: World, wanted: str, workspace: str | None
) -> tuple[str, tuple[str, str]]:
	"""Return the workspace a named project is in, and the project — `#1501`.

	**§13.7's resolution order, with one step added for this command**, on Simon's decision of
	2026-08-28. Steps 1 to 5 are unchanged and have already run; this is what happens where
	they all decline *and a project was named anyway*. Adopting a checkout is the one command
	handed a project key before it has a workspace, and on the instance this was found on every
	key but ``inbox`` is in exactly one — so refusing for want of something the argument settles
	turned one command into an interview, which is the outcome :func:`_use_here`'s own docstring
	promises to avoid. It was met twice in one day by two separate import runs.

	**Silent while the answer is unambiguous, insistent when it is not**, which is `#587`'s
	shape: a fresh instance has one workspace and never reaches this at all, and an instance
	with several made the others deliberately — so the refusal there names the workspaces that
	*hold the key* rather than every workspace there is, which is strictly more than the
	general refusal can say.

	**It does not widen :func:`_writing_workspace`**, which is asked wherever refusing is
	right: every write that lands somewhere has to know where before it starts, and this is the
	one command carrying an argument that answers. Widening it there would make every write
	guess.

	One request per workspace, because a project listing is scoped to one at both transports.
	Four, once, in a command somebody runs when they adopt a repository.
	"""

	if workspace is not None:
		named = _project_written_down(world, wanted, workspace=workspace)

		if named is None:
			_no_such_project(program, wanted, [workspace])

		return workspace, named

	reachable = [one.slug for one in world.writing_to().identity.workspaces]
	holding = [
		(slug, found)
		for slug, found in (
			(slug, _project_written_down(world, wanted, workspace=slug)) for slug in reachable
		)
		if found is not None
	]

	if not holding:
		_no_such_project(program, wanted, reachable)

	if len(holding) > 1:
		candidates = [slug for slug, _found in holding]

		program.stop(
			f"{wanted!r} is a project in {_names_in_words(candidates)}, so there is no way "
			"to tell which one this directory is about.",
			f"Say which — 'subroutine -w {candidates[0]} use --here --project {wanted}'.",
		)

	return holding[0]


def _use_here (program: Program, world: World, where: str, project: str) -> None:
	"""Write a marker into the current directory, and say what it will do.

	The connection and workspace come from the *current context* unless the caller named
	them, so ``subroutine use --here --project SR`` records where they already are rather
	than making them type it again — which is the whole difference between adopting a
	repository in one command and adopting it in an interview.

	**And where there is no current context, the project answers** (`#1501`). That sentence
	above assumed one, so on a fresh credential reaching several workspaces — no stored
	context, no marker, nothing to inherit — this refused for want of something the argument
	settles. :func:`_adopted_project` is that step, and it is an addition to §13.7's order for
	this command alone, on Simon's decision of 2026-08-28.
	"""

	connection, workspace = (
		_chosen(program, world, where)
		if where.strip()
		else (world.current.connection, world.current.workspace)
	)
	asked = subroutine.domain.projects.normalize_path(project) or None
	found = None

	# **The project settles the workspace where nothing else has** (`#1501`). Both refusals
	# live in :func:`_adopted_project`, including the one for a key nothing holds, because
	# saying *there is no project 'web' here* means naming where "here" was.
	if asked is not None:
		workspace, found = _adopted_project(program, world, asked, workspace)

	key, identifier = found if found is not None else (None, None)

	written = subroutine.directory.write(
		pathlib.Path.cwd(),
		# **Always, not only when there is more than one connection** (`#273`). Omitting
		# it saved one line in a file and made the marker's completeness depend on how
		# many connections existed the day it was written — so configuring a second one
		# later left every existing marker naming a workspace that may not be on the
		# current connection, silently, for exactly the caller §13.7a says cannot be
		# asked. It happened to an agent working in this repository within an hour of a
		# second connection being added.
		connection=connection,
		workspace=workspace,
		# **The same pairing as the project, and it was missing** (`#317`). A workspace
		# could not be renamed when this file was designed, so its slug was durable by
		# construction; `#295` made renaming possible and the marker went on recording only
		# the name.
		workspace_id=_workspace_id_of(world, workspace),
		project=key,
		# **The id is what makes this survive a rename** (`#177`). The key is written
		# beside it so the file stays readable, and is the half that goes stale.
		project_id=identifier,
	)

	program.say(f"Wrote {written}.")

	if key is None:
		program.say("New work here goes to the Inbox. Add --project to file it somewhere.")

		return

	program.say(f"New work started in this directory goes to {key}, unless a line says otherwise.")
	program.say("")
	_suggest(program.console, "subroutine add \"something to do\"")


def _default_project (program: Program, world: World, text: str) -> str | None:
	"""Return the project a captured line should go to when it does not say (§13.7a).

	``None`` whenever the answer is "wherever it went before" — no marker, no project in the
	marker, or a ``+KEY`` in the line, which is somebody being explicit about this one item
	and must beat a file they may not know is there.
	"""

	if world.marker is None:
		return None

	if world.marker.project_id is None and world.marker.project is None:
		return None

	if subroutine.domain.capture.names_a_project(text):
		return None

	# **The id decides, and the key is what gets reported** (`#177`). A key can be renamed
	# as of `#176`, so a marker naming one is stale the moment somebody does — and every
	# checkout on every machine would silently start filing work into the Inbox. The id
	# cannot change, so it is asked first.
	named = _project_named_by(world, world.marker)

	if named is None:
		# **Ignored rather than refused** (`#166`). A marker is advisory context written by
		# a machine, so a checkout marked for one instance must not stop `add` working
		# against another.
		shown = world.marker.project or world.marker.project_id

		# **Two reasons, and only one of them is "there is no such project"** (`#414`).
		# Where the marker names another connection, its project was never looked for —
		# saying it "is not on local" would assert something the program has not checked
		# and is often false, since a key like SR is exactly the kind two instances share.
		# The whole marker applies somewhere else, which is a different thing to be told
		# and a different thing to do about it.
		if world.marker.speaks_for(world.current.connection):
			program.warn(
				f"{FILE_NAME} here names project {shown!r}, which is not on "
				f"{world.current.connection}. Ignoring it."
			)

		else:
			program.warn(
				f"{FILE_NAME} here names project {shown!r} on "
				f"{world.marker.connection}, and this is going to "
				f"{world.current.connection}. Ignoring it."
			)

		return None

	# **The one moment this file can explain itself.** The id resolved and the key beside
	# it does not match what is stored, which is what a rename leaves behind — so say it
	# once, here, rather than letting the file go on quietly disagreeing with itself.
	#
	# **Compared exactly, against the stored key** (`#554`). It used to normalise both
	# sides, so a marker saying `WEB` matched `web` and nothing was ever said — and `#508`
	# then changed the stored form, leaving every marker written before it holding a
	# spelling this program no longer stores, prints or writes anywhere. The file said
	# something no other surface agreed with and the one mechanism built to notice could
	# not see it. *Resolution* stays case-insensitive, in `directory.resolve`, so those
	# markers go on working; only the question "does this file agree with us" is exact.
	if world.marker.project and world.marker.project != named:
		# **Two different things to be told.** A rename changed which project the key
		# names; a respelling changed nothing but how it is written, and saying "that
		# project is now reprobate" about a marker reading `REPROBATE` would read as a
		# rename that half-failed — which is exactly how this was met on a real instance.
		respelling = (
			subroutine.domain.projects.normalize_key(world.marker.project) == named
		)

		program.warn(
			f"{FILE_NAME} here says {world.marker.project!r}; the project's key is stored "
			f"as {named!r}. It still resolves, so nothing is broken — 'subroutine use "
			f"--here --project {named}' brings the file into line."
			if respelling
			else f"{FILE_NAME} here still says {world.marker.project!r}; that project is "
			f"now {named}. Run 'subroutine use --here --project {named}' to bring it up "
			f"to date."
		)

	return named


def _asked_for_a_token (program: Program, name: str) -> str:
	"""Ask for a connection's token, or take it from a pipe when there is one.

	Never an option on the command line (§12.3a): an argument lands in shell history and in
	the process list, where a token that has been logged is a token that has been shared.
	A pipe is the scripted path, so that 'pass show work' can supply one without either.
	"""

	if not sys.stdin.isatty():
		piped = sys.stdin.readline().strip()

		if piped:
			return piped

		program.stop(
			f"Nothing was piped in, so there is no token for {name!r}.",
			"Pipe it in, or run this at a terminal and it will ask.",
		)

	typed = str(typer.prompt(f"Token for {name}", hide_input=True)).strip()

	if not typed:
		program.stop(
			f"No token was given, so {name!r} was not added.",
			"Issue one on that instance with 'subroutine token create', then try again.",
		)

	return typed


def _reaching (
	program: Program,
	connection: subroutine.connections.Connection,
	roster: subroutine.connections.Roster,
	resolved: subroutine.config.Settings,
	secret: str,
) -> Welcomed:
	"""Reach an instance with a credential, and report what answered.

	**The same call the fan-out makes**, deliberately: ``identity()`` is what every listing
	begins with, so a connection that passes here is one that works rather than one that
	merely parses. It is also the call that refuses a proxy, a captive portal or a typo'd
	address answering 200 with something that is not an instance.

	``me()`` beside it because the address being right is only half of what somebody wants
	confirmed. The other half is that the credential they pasted is the one they meant, and
	the only thing that says so is the name the far end gives back.
	"""

	try:
		with subroutine.clients.opening.for_connection(
			connection, roster, resolved, token=secret
		) as client:
			identity = client.identity()
			me = client.me()

	except subroutine.errors.SubroutineError as error:
		program.fail(error)

	return Welcomed(
		instance=identity.instance,
		username=me.user.username,
		workspaces=tuple(workspace.slug for workspace in identity.workspaces),
	)


def _connection_row (
	program: Program,
	connection: subroutine.connections.Connection,
	roster: subroutine.connections.Roster,
	resolved: subroutine.config.Settings,
	current: subroutine.context.Current,
) -> tuple[str, str, str, str]:
	"""Describe one connection: its name, where it is, its token, and what it is."""

	try:
		token = subroutine.credentials.resolve(
			connection, default_connection=roster.default, describe_only=True
		).source

	except subroutine.errors.SubroutineError as error:
		token = f"unusable — {error.detail}"

	notes = []

	# **"in use" first, because it is the one somebody is asking about** (`#278`). A
	# reader scanning this column wants to know where their next command goes; "default"
	# answers a narrower question — where it would go if nothing had chosen — and read as
	# the first, which is how an agent came to tell Simon the wrong thing.
	if connection.name == current.connection:
		notes.append("in use")

	if connection.name == roster.default:
		notes.append("default")

	if connection.read_only:
		notes.append("read-only")

	# The local connection's "address" is its database URL, masked — it is the one piece
	# of configuration that routinely carries a password, and this output is exactly what
	# ends up pasted into a bug report.
	where = program.mask(resolved.database_url) if connection.is_local else str(connection.url)

	return (connection.name, where, token, ", ".join(notes))


def _chosen (program: Program, world: World, where: str) -> tuple[str, str]:
	"""Read what ``use`` was given into a connection and a workspace."""

	parts = [part.strip() for part in where.split(subroutine.domain.refs.SEPARATOR)]

	if len(parts) > 2 or any(not part for part in parts):
		program.stop(
			f"{where!r} is not a place to work.",
			"Give a workspace, or a connection and a workspace — 'subroutine use "
			"work/acme'.",
		)

	name = parts[0] if len(parts) == 2 else world.current.connection
	wanted = parts[-1]
	item = world.connection(name)

	if item is None:
		program.stop(
			f"There is no connection called {name!r} here, or it could not be reached.",
			world.roster.alternatives(),
		)

	if item.identity.workspace(wanted) is None:
		# **A bare connection name is the likely typo, and the program knows it is one**
		# (`#270`). Told `use workshop`, this looked for a *workspace* of that name on the
		# current connection, did not find one, and reported about somewhere else
		# entirely — while the roster listed `workshop` on the line above.
		named = world.connection(wanted)

		if len(parts) == 1 and named is not None:
			program.stop(
				f"{wanted!r} is a connection, not a workspace.",
				_completions(named),
			)

		program.stop(f"There is nothing called {wanted!r} on {item.name}.", _workspace_hint(item))

	return item.name, wanted


def _require_connection (
	program: Program,
	world: World, name: str
) -> subroutine.clients.base.Client:
	"""Return the open client for a connection a lookup already found something on."""

	item = world.connection(name)

	if item is None:
		program.stop(f"{name} could not be reached.")

	return item.client


def _report_zones (
	program: Program, gathered: subroutine.fanout.Gathered[subroutine.views.Agenda]
) -> None:
	"""Say when two connections are not counting the same day — `#995`.

	**Reported rather than silently resolved.** ``subroutine agenda`` used to send one zone to
	every connection, which made the answer consistent by making it wrong: the value was the
	*typing machine's*, so a person with a work profile on America/New_York and a personal one
	on Europe/London got a third day matching neither, and nothing said so.

	Each instance resolves the reader's own zone now (§6.5), so the answers can genuinely be
	about different days. That is the truth of the arrangement and the person is the only one
	who can settle it — by setting the same zone on both accounts, or by knowing.

	**On the zones rather than on the dates**, deliberately. Two zones are on the same date for
	part of every day, so a warning keyed on the dates would appear and disappear under the
	reader while nothing changed — which is the failure `#966` records about a message whose
	trigger is a coincidence.
	"""

	zones = {answer.connection.name: answer.value.timezone for answer in gathered.answers}

	if len(set(zones.values())) < 2:
		return

	named = ", ".join(f"{name} in {zone}" for name, zone in sorted(zones.items()))

	program.warn(
		f"These connections are counting different days: {named}. "
		f"Set the same timezone on each account to merge one day rather than two."
	)


def _report_dates_set_elsewhere (
	program: Program, gathered: subroutine.fanout.Gathered[subroutine.views.Agenda]
) -> None:
	"""Say when a row's date was set in a zone other than the one bucketing it — `#1039`.

	**Two correct rules meet on one line and contradict each other.** `#773` renders a
	day-scale date in the **task's** own zone, because re-rendering a day through another zone
	makes it a *different day*; `#989` buckets in the **reader's**, because a person's agenda
	is about their own day. So a deadline set for the end of somebody's UTC day falls 59
	minutes past the end of a London reader's, and the row says *due Thu 20 Aug* under a
	heading that means *not today*.

	Neither rule is wrong, and this does not resolve them — reaching for either answer would
	overturn a written decision for a presentation problem. It says what happened, which is
	`_report_zones`'s shape one level down: that one reports two *connections* counting
	different days, this one reports two *people* in one workspace who are.

	**Found by using the product** on the day a second human first dated something here, which
	is `#589`. Fifteen items were dated the same day, fourteen under Today and one under Next 7
	days, every one of them rendering the same words — and I spent an hour diagnosing it as a
	write-path defect before measuring the actor.

	**On the zones rather than on the dates**, which is `#966`'s recorded rule and `#995`'s
	before it: two zones share a date for part of every day, so a message keyed on the dates
	would appear and vanish under the reader while nothing changed.
	"""

	reader = {answer.value.timezone for answer in gathered.answers}
	elsewhere = {
		task.timezone
		for answer in gathered.answers
		for bucket in subroutine.views.AGENDA_BUCKETS
		for task in getattr(answer.value, bucket, ())
		if task.timezone is not None and task.timezone not in reader
	}

	if not elsewhere:
		return

	named = ", ".join(sorted(elsewhere))
	yours = ", ".join(sorted(zone for zone in reader if zone is not None))

	program.warn(
		f"Some of this was dated in {named}, where your day is {yours}. A date is shown for "
		f"the day it was set, so one of these may sit under a heading that does not match it."
	)


def _report (program: Program, world: World, failures: typing.Sequence[subroutine.fanout.Failure]) -> None:
	"""Name every connection that could not be reached, and carry on.

	To standard error, and the command still exits 0: an agenda that refuses to print
	because one of three servers is down is worse than an agenda with a line saying which
	one. ``--strict`` is how a script says it would rather stop.
	"""

	for failure in (*world.unreachable, *failures):
		program.warn(failure.describe())


def _only_this_connection (program: Program, world: World, name: str) -> World:
	"""Return the world with only the named connection in it — `#272`.

	**A filter, not a context, and the difference is the whole item.** `-c` before the
	command moves where a *write* goes; §13.7 keeps reads spanning everything reachable so
	that forgetting which context you are in can never cost you a missed item. That leaves
	"show me only what is on the work instance" — asked for its own sake while checking one
	server, or driving one instance during a migration — with no spelling at all, and the
	only way to say it was to disable the others in `config.toml` and put them back.

	So it narrows the rows and changes nothing durable, exactly as `--project` does one
	level up, and it is spelled *after* the command for that reason: before the command is
	context, after it is a filter.

	**The roster is left whole on purpose.** `World.address_of` qualifies an address when
	more than one connection is configured, so rows still print as `work/#42` — which is
	what somebody types next, after the flag is gone.
	"""

	wanted = world.roster.require(name)
	kept = tuple(item for item in world.reached if item.client.connection.name == wanted.name)

	if not kept:
		# **Configured but not reached is a different answer from "nothing there"**, and an
		# empty listing would report the second while meaning the first. The failure is
		# already collected — a connection that could not be built or could not be read —
		# so this says so rather than showing an empty list somebody would believe.
		program.stop(
			f"Connection {wanted.name!r} was asked for and could not be read.",
			"Run 'subroutine connections' to see what this machine can reach.",
		)

	return dataclasses.replace(world, reached=kept, narrowed_to=wanted.name)


def _project_moved (program: Program, *, key: str, under: str, root: bool, yes: bool) -> None:
	"""Move a project and its subtree, counting what travels before it asks — `#320`."""

	# **Neither, or both, is a refusal rather than a default** — this is the one project
	# command with no undo, and `POST /v1/projects/{key}/move` refuses the same way for
	# the same reason. Guessing either direction is how a subtree gets flattened.
	if bool(under) == root:
		program.stop(
			"Say where to move it.",
			"'--under KEY' puts it inside another project; '--root' makes it top-level.",
		)

	with program.opened() as world:
		place = world.writing_to()
		workspace = _writing_workspace(world)

		# Counted before anything changes, like `project rename` — "this moves a subtree"
		# is abstract, and "this moves 3 projects and 137 items" is something somebody can
		# weigh. Reading first is the whole reason this is not a one-liner.
		tree = place.client.projects(workspace=workspace)
		moving = _subtree(tree, key)

		if not moving:
			program.stop(
				# Named as they typed it, never as we would have stored it: telling
				# somebody 'WEB' is not here when they wrote 'web' reads as the program
				# mangling their input and then blaming them for it.
				f"There is no project called {key!r} here.",
				"Run 'subroutine project list' to see what there is.",
			)

		# **One count over the subtree root** (`#320`). This used to ask per project and add
		# the answers up, which was right when a project listing meant that project alone
		# and became a double count the moment naming a parent included its children — the
		# item in a sub-project was counted once for the sub-project and again for its
		# parent. `tests/test_personal_path.py` caught it, which is what that test is for.
		#
		# It also fixes `#296`'s fault here in passing: `len(client.tasks(...))` capped at a
		# page, so a subtree of more than fifty items understated itself in the sentence
		# somebody says yes to.
		held = place.client.count_tasks(workspace=workspace, project=key)

		if not yes:
			destination = (
				"the top level" if root else subroutine.domain.projects.normalize_key(under)
			)
			projects = f"{len(moving)} project{'' if len(moving) == 1 else 's'}"
			items = f"{held} item{'' if held == 1 else 's'}"

			program.say(f"Moving {subroutine.domain.projects.normalize_key(key)} to {destination}.")
			program.say(f"  {projects} move, and {items} {'goes' if held == 1 else 'go'} with them.")
			program.say("  Every number stays the same, and nothing is refiled.")

			if not typer.confirm("Go on?"):
				program.stop("Nothing was moved.")

		moved = place.client.move_project(
			key, parent=None if root else under, workspace=workspace
		)

		program.say(f"Moved {moved.key} — {moved.title}")

def _project_renamed (program: Program, *, key: str, to: str, yes: bool) -> None:
	"""Retire a project's short name, saying what stops working first — `#176`."""

	with program.opened() as world:
		where = world.writing_to()
		workspace = _writing_workspace(world)

		# **Counted before anything changes, so the question is answerable.** "This will
		# break addresses" is abstract; "this project holds 137 items and three commands
		# will stop finding it" is a thing somebody can weigh. The count is the reason
		# this reads the project first rather than renaming and reporting.
		held = where.client.count_tasks(workspace=workspace, project=key)

		if not yes:
			program.say(f"Renaming {key} to {subroutine.domain.projects.normalize_key(to)}.")
			program.say(f"  {_kept(held)}.")
			program.say(f"  '{key}' stops working: as an address, in '+{key}', and in any")
			program.say("  .subroutine file that names it.")

			if not typer.confirm("Go on?"):
				program.stop("Nothing was renamed.")

		renamed = where.client.rename_project(key, key=to, workspace=workspace)

		program.say(f"Renamed to {renamed.key} — {renamed.title}")

		# The marker in *this* directory is the one that can be repaired from here, and
		# the one most likely to be stale a second from now (`#177`).
		# A marker holds what somebody wrote, so this compares normalised forms rather
		# than raw ones — a checkout marked `WEB` still matches the project `web`.
		if world.marker is not None and world.marker.project is not None and (
			subroutine.domain.projects.normalize_key(world.marker.project)
			== subroutine.domain.projects.normalize_key(key)
		):
			_suggest(program.console, f"subroutine use --here --project {renamed.key}")


def _instance_workspaces (program: Program, *, json_output: bool) -> None:
	"""Say what workspaces exist here, member or not — `#1418`."""

	with program.opened() as world:
		where = world.writing_to()

		found = where.client.instance_workspaces()

		if json_output:
			program.say(json.dumps([one.model_dump(mode="json") for one in found], indent=2))

			return

		for one in found:
			people = "1 person" if one.members == 1 else f"{one.members} people"
			mark = "" if one.joined else "  — you are not a member"

			program.say(f"  {one.slug}  {one.title}  ({people}){mark}")

		# **The count is the point, and a silent absence is the defect.** Somebody who can
		# create a workspace can create one the instance owner is not in, and until this
		# existed the owner's answer to *what is here* left it out with nothing to notice.
		outside = [one for one in found if not one.joined]

		if outside:
			program.say("")
			program.say(
				f"{len(outside)} of these you cannot see into. "
				f"'subroutine user add <you> --workspace <name>' joins one."
			)


def _instance_updated (program: Program, *, name: str, timezone: str) -> None:
	"""Change what this installation is called, or where it says it is — `#1669`."""

	with program.opened() as world:
		where = world.writing_to()

		changed = where.client.update_instance(
			name=name.strip() or None, timezone=timezone.strip() or None
		)

		program.say(f"{changed.name} — days here are counted in {changed.timezone}.")

		# **Said because it is the commonest reason to be here and the easiest to get wrong.**
		# This zone is the last word in the chain rather than the first, so it is read only by
		# people who have set neither their own nor their workspace's — which on a shared
		# instance is usually nobody, and on a fresh one is everybody.
		program.say("That is the fallback: a workspace or an account with its own zone keeps it.")


def _project_shared (program: Program, *, key: str, username: str) -> None:
	"""Let one more person see a private project — `#1444`."""

	with program.opened() as world:
		where = world.writing_to()
		workspace = _writing_workspace(world)

		member = where.client.share_project(key, username=username, workspace=workspace)

		program.say(f"{member.user.username} can now see {member.project}.")

		# **Sight is not authority, and this is where somebody will assume otherwise.** The
		# row grants nothing but the ability to read the project; what they may *do* in it is
		# still their workspace role, and `#1452` is where a project-scoped role would live.
		program.say("What they may do there is still their workspace role.")


def _project_unshared (program: Program, *, key: str, username: str) -> None:
	"""Take somebody's sight of a project away again — `#1444`."""

	with program.opened() as world:
		where = world.writing_to()
		workspace = _writing_workspace(world)

		where.client.unshare_project(key, username=username, workspace=workspace)

		program.say(f"{username} can no longer see {key}.")


def _project_sharing (program: Program, *, key: str, json_output: bool) -> None:
	"""Say who has been shared into a project — `#1444`."""

	with program.opened() as world:
		where = world.writing_to()
		workspace = _writing_workspace(world)

		people = where.client.project_members(key, workspace=workspace)

		if json_output:
			program.say(json.dumps([one.model_dump(mode="json") for one in people], indent=2))

			return

		# **A project always has at least its owner**, so an empty answer here means the
		# owner's account was deleted rather than that nobody can see it — `#1453`, and
		# saying so is cheaper than leaving a blank.
		if not people:
			program.say(f"Nobody holds {key}. That is a project nobody can reach.")

			return

		for one in people:
			program.say(f"  {one.user.username}{'  (agent)' if one.user.is_service_account else ''}")


def _workspace_renamed (program: Program, *, slug: str, to: str, yes: bool) -> None:
	"""Retire a workspace's short name, naming the members it changes the address for."""

	with program.opened() as world:
		where = world.writing_to()

		# **Counted before anything changes**, exactly as `project rename` does: "this
		# breaks addresses" is abstract where "this holds 249 items and two people" is
		# something a person can weigh. A workspace is a tenancy boundary, so the member
		# count belongs here and does not on the project version — a rename changes the
		# address for everybody who can reach it, not only whoever typed it.
		# **A count rather than a page, since `#296`.** This asked for a whole page's worth
		# and hedged with "at least" when it came back full, which was honest and was still
		# a workaround; `count_tasks` asks §8.4's `include_total` and the hedge is gone.
		held = where.client.count_tasks(workspace=slug)
		people = where.client.members(workspace=slug)

		if not yes:
			program.say(f"Renaming {slug} to {to.lower()}.")
			program.say(f"  {_kept(held)}.")

			if len(people) > 1:
				program.say(
					f"  {len(people)} people reach it, and the address changes for all of them."
				)

			program.say(f"  '{slug}' stops working: in an address like '{slug}/42', in")
			program.say("  'subroutine use', and in any .subroutine file that names it.")

			if not typer.confirm("Go on?"):
				program.stop("Nothing was renamed.")

		renamed = where.client.rename_workspace(slug, slug=to)

		program.say(f"Renamed to {renamed.slug} — {renamed.title}")

		# The stored context is the one caller we *can* repair, and the one that would
		# otherwise fail on the very next command.
		if world.current.workspace == slug:
			_suggest(program.console, f"subroutine use {renamed.slug}")


def _workspace_deleted (program: Program, *, slug: str, yes: bool) -> None:
	"""Put a workspace in the trash, having said what goes with it and what comes back."""

	with program.opened() as world:
		where = world.writing_to()

		# **Counted before anything changes**, for `workspace rename`'s reason and more so:
		# a rename keeps everything reachable and this does not. "This deletes a workspace"
		# is abstract where "this hides 249 items and two people lose sight of them" is
		# something a person can weigh.
		held = where.client.count_tasks(workspace=slug)
		people = where.client.members(workspace=slug)

		if not yes:
			program.say(f"Deleting {slug}.")
			program.say(f"  {_kept(held)}, out of sight until it is restored.")

			if len(people) > 1:
				program.say(f"  {len(people)} people reach it, and it disappears for all of them.")

			program.say(f"  '{slug}' becomes free, so something else can take the name.")
			program.say(f"  Undo it with 'subroutine workspace restore {slug}'.")

			if not typer.confirm("Go on?"):
				program.stop("Nothing was deleted.")

		removed = where.client.delete_workspace(slug)

		program.say(f"Deleted {removed.slug} — {removed.title}")
		_suggest(program.console, f"subroutine workspace restore {removed.slug}")


def _workspace_restored (program: Program, *, slug: str) -> None:
	"""Take a workspace back out of the trash, and everything in it with it."""

	with program.opened() as world:
		where = world.writing_to()
		back = where.client.restore_workspace(slug)

		program.say(f"Restored {back.slug} — {back.title}")


#: What ``--colour`` offers, composed once. **At module level because `#943`'s ratchet counts
#: lines inside ``register``**, and a help string assembled in the closure is three lines of
#: sentence per command for a value that is the same on both.
_COLOURS = ", ".join(subroutine.domain.palette.NAMES)

#: A project's, which falls back to whatever is above it rather than to nothing.
COLOUR_HELP = f"What its work is marked with: {_COLOURS}. Pass '' to inherit."

#: A workspace's, which everything in it takes unless a project says otherwise.
WORKSPACE_COLOUR_HELP = (
	f"What its work is marked with, unless a project says otherwise: {_COLOURS}. Pass '' to clear."
)

#: Repeatable, and the whole list each time rather than one more each time — a second invocation
#: says what this hides now, not what to add.
#:
#: **Two strings, because `''` means different things at the two scopes**, which is the colour's
#: own shape one setting along. Clearing a *workspace's* genuinely offers everything, since
#: nothing sits above it; clearing a *project's* makes it inherit, which offers whatever the
#: workspace offers rather than the whole vocabulary. One sentence for both was written first,
#: said *offer them all* at each, and was false at the project — found by driving it against a
#: workspace that hides one, minutes after it shipped.
HIDE_STATUS_HELP = (
	"A status not to offer here, e.g. 'blocked'. Repeat for more, or pass '' to inherit."
)

#: A workspace's, which is the top of the chain, so clearing it really does offer everything.
WORKSPACE_HIDE_STATUS_HELP = (
	"A status not to offer anywhere in it, e.g. 'blocked'. Repeat for more, or pass '' to "
	"offer them all."
)


def _hidden_statuses (
	given: list[str] | None, *, nothing: bool = False
) -> dict[str, typing.Any] | None:
	"""Read ``--hide-status`` and ``--hide-nothing`` into a settings change, or ``None``.

	Three states from one option, and the middle one is the reason this is a function: absent
	says nothing, ``''`` clears, and any word sets. Typer reports the first two as an empty list
	and as a list holding one empty string, which are a character apart and mean opposite things.

	**And a fourth the command line could not spell** (`#1034`). The stored value has three
	meanings — absent inherits whatever is above, a list hides those, and an **empty list**
	offers everything and overrides what is above — and only the first two had a spelling here.
	A project inside a workspace that hides ``needs_input`` could say *inherit* or *hide these*
	and could not say *offer them all anyway*, which was reachable over HTTP and from an agent
	through ``subroutine_call_api``.

	**A sentinel rather than a mirror**, which is the decision recorded on the item. A
	``--show-status needs_input`` reads more naturally and composes, and it needs a rule for
	what happens when a key is named on both sides — a contradiction the caller can express and
	somebody then has to resolve. ``--hide-nothing`` cannot contradict itself, and the state is
	genuinely rare: the ordinary project inherits.

	**Not offered on a workspace, and that is not an oversight.** A workspace is the top of the
	chain, so an absent value falls through to the default of nothing hidden — absent and empty
	are already the same answer there. The flag would be a control that changes nothing, which
	is the defect this project finds most often.
	"""

	if nothing:
		if given and any(one for one in given):
			raise ValueError("cannot both hide a status and hide nothing")

		return {subroutine.domain.settings.HIDDEN_STATUSES.key: []}

	if not given:
		return None

	wanted = [one for one in given if one]

	return {subroutine.domain.settings.HIDDEN_STATUSES.key: wanted or None}


def _project_updated (
	program: Program,
	*,
	key: str,
	title: str,
	description: str,
	status: str,
	colour: str,
	hide_status: list[str] | None,
	hide_nothing: bool,
	private: bool | None,
) -> None:
	"""Change the fields beside a project's address, and say what it means — `#983`, `#434`."""

	changes: dict[str, typing.Any] = {}

	if title is not UNGIVEN:
		changes["title"] = title

	if description is not UNGIVEN:
		changes["description"] = description or None

	if status is not UNGIVEN:
		changes["status"] = status

	if private is not None:
		changes["visibility"] = "private" if private else "public"

	# **A settings map rather than a column, and merged per key** (`#1025`). An empty string
	# clears it, which is how every other option here spells *unset* — and clearing means the
	# project falls back to whatever is above it rather than going unmarked.
	settings: dict[str, typing.Any] = {}

	if colour is not UNGIVEN:
		settings[subroutine.domain.settings.COLOUR.key] = colour or None

	try:
		settings.update(_hidden_statuses(hide_status, nothing=hide_nothing) or {})

	except ValueError:
		program.stop(
			"--hide-status and --hide-nothing say opposite things.",
			hint="Use --hide-nothing on its own to offer every status here, whatever the "
			"workspace hides.",
		)

	if settings:
		changes["settings"] = settings

	if not changes:
		program.stop(
			"Nothing to change.",
			hint="Pass --title, --description, --status, --colour, --hide-status, "
			"--hide-nothing or --private.",
		)

	with program.opened() as world:
		where = world.writing_to()
		changed = where.client.update_project(
			key, workspace=_writing_workspace(world), **changes
		)

		program.say(f"Changed {changed.key} — {changed.title}")

		# **Said only when the status moved, and said as a consequence rather than as a
		# label.** `on_hold` is a seeded key a workspace may rename (§5.5), so what is
		# reported is what the *category* does — the part that stays true whatever the row is
		# called locally.
		if "status" in changes:
			program.say(f"  Now {changed.status}.")

			# **The second sentence is here because the first one was false without it.**
			# Driving this said "no longer … on the agenda" and the next command showed a
			# task in the held project under Today — dated work deliberately stays (`#983`),
			# so a message that did not say so was contradicted by the screen underneath it.
			if changed.status_category != "in_progress":
				program.say("  Its work is no longer offered as ready.")
				program.say("  Anything dated stays on your agenda.")


def _focus_of (where: Reached, workspace: str) -> str | None:
	"""Return the project one workspace has prioritised, by address, or ``None``.

	Read off the identity this connection already answered with rather than fetched, so asking
	costs nothing — which is what lets every surface say it instead of only the one that thought
	of it. Null also covers *a project this credential cannot see*, and the two are
	indistinguishable on purpose: a focus somebody cannot reach gives them no bonus either
	(``scoping.prioritised_projects``).
	"""

	for space in where.identity.workspaces:
		if space.slug == workspace:
			return space.prioritised_project

	return None


def _projects_listed (program: Program, *, json_output: bool) -> None:
	"""Print the project tree, marking the one whose work is raised — `#986`."""

	with program.opened() as world:
		where = world.writing_to()
		workspace = _writing_workspace(world)
		found = where.client.projects(workspace=workspace)

		if json_output:
			program.say(json.dumps([one.model_dump(mode="json") for one in found], indent=2))

			return

		if not found:
			program.say("No projects here yet.")
			_suggest(program.console, 'subroutine project create WEB "Website redesign"')

			return

		# Indented by depth, which is why the listing is ordered by path rather than by
		# name: a child follows its parent, so the shape can be printed in one pass.
		width = max(len(one.key) + one.depth * 2 for one in found)
		focus = _focus_of(where, workspace)

		for one in found:
			shown = f"{'  ' * one.depth}{one.key}".ljust(width)

			# **Marked on the project rather than on its work** (decision `#982`). This is the
			# listing where the question *which one is it* is actually asked, and the answer is
			# one row out of a handful — where a mark on task rows would land on most of them
			# and §12.2a would drop it for saying nothing. Only the project itself is marked:
			# its subtree inherits the bonus, and labelling children would read as four
			# prioritised projects, which is the state this design makes impossible.
			marked = "  (prioritised)" if focus is not None and one.path == focus else ""

			program.say(f"{shown}  {one.title}{marked}")


def _written_back (document: typing.Any, *, without_body: bool) -> str:
	"""Return a document write's JSON answer, with the text left out if it was not wanted.

	**`#1360`.** Setting a 9 KB specification's status to ``active`` printed the whole
	specification back. Context economy is a first-order cost for the agent client rather than
	an optimisation, and a document is the one entity whose body is large by design.

	**Opt-in omission and not a cap**, so `#849`'s rule — that a cap is only defensible together
	with a way to read the rest — does not bind: the caller asked for less and knows it. The
	default is unchanged, because the shape is published and somebody parses it.

	One function for both writes, because two copies of *what a write answers* is how they come
	to disagree a field at a time.
	"""

	shown = document.model_dump(mode="json")

	if without_body:
		shown.pop("body", None)

	return json.dumps(shown, indent=2)


def _workspaces_listed (program: Program, *, json_output: bool) -> None:
	"""Print the workspaces this account can reach, by the name you type and what it is called.

	**Rendered from the identity the client already holds** (`#1355`), never from a listing of
	its own. ``GET /v1/me`` answers *which workspaces am I in*, which is the question somebody
	typing this is asking — so this needs no round trip of its own and no client method, and
	cannot drift from what ``whoami`` says on the line above it.

	The role is deliberately not a column here. ``whoami`` states it, and on the ordinary
	install every row would carry the same word — which §12.2a drops as saying nothing.
	"""

	with program.opened() as world:
		found = world.writing_to().client.identity().workspaces

		if json_output:
			program.say(json.dumps([one.model_dump(mode="json") for one in found], indent=2))

			return

		if not found:
			program.say("No workspace here can be read with this credential.")

			return

		width = max(len(one.slug) for one in found)

		for one in found:
			program.say(f"{one.slug.ljust(width)}  {one.title}")


def _ranked_by_priority (order: str | None) -> bool:
	"""Report whether this listing is sorted by §6.3a's rank, in either direction.

	Read from the order the reader asked for rather than from the merge's spelling, because the
	question is *does what I am about to say apply to this page* — and `_sunk` prepends a
	deferral band without changing whether priority decides anything.
	"""

	named = [part.strip().removeprefix("-") for part in (order or "").split(",")]

	return "priority_score" in named


def _say_prioritised (world: World, console: rich.console.Console) -> None:
	"""Say which project is prioritised, once, above a list it affects — `#986`.

	**About the list rather than about a row, and that is measured rather than tidy.** `#851`
	requires a computed rank to be able to explain itself; 84% of this instance's open tasks are
	in the project most likely to be prioritised, so a mark on each row would appear on 84% of
	them — which §12.2a drops as a column that says the same thing on every line. The effect is
	uniform across the page, so the explanation belongs to the page.

	**Silent when nothing is prioritised**, because that is most workspaces and every day. And
	never a magnitude: the bonus is a fixed number the ordering knows and no surface publishes,
	since a visible one invites *"can I set it to 2?"* — the dial decision ``#982`` declines.
	"""

	found = world.prioritised

	if not found:
		return

	console.print(rich.text.Text(_prioritised_sentence(found), style=DETAIL))
	console.print("")


def _prioritised_sentence (found: typing.Sequence[str]) -> str:
	"""Say what is prioritised, with everything in the sentence agreeing about how many.

	Two workspaces may each prioritise a project (§13.7), so this has to survive a plural in
	three places at once — the verb, the possessive and the noun. A line reading
	"personal/home, projects/subroutine is prioritised, so its work rises" is the kind of detail
	that makes a reader distrust every number beside it.
	"""

	if len(found) == 1:
		return f"{found[0]} is prioritised, so its work rises here."

	return f"{_names_in_words(found)} are prioritised, so their work rises here."


def _claimed (program: Program, *, which: str, minutes: int) -> None:
	"""Take a task so nobody else starts it too — a lease rather than a lock (§14.11)."""

	with program.opened() as world:
		located, task = _a_task(
			program,
			world,
			_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
			verb="claim",
		)
		client = _require_connection(program, world, located.connection)

		try:
			held = client.claim(
				ref=task.ref, minutes=minutes or None, workspace=located.workspace
			)

		except subroutine.errors.SubroutineError as error:
			program.fail(error)

		program.say(_acted(world, dataclasses.replace(located, item=held), "Claimed"))
		_suggest(program.console, "subroutine list --ready", "what is free to start")


def _git (*arguments: str) -> str | None:
	"""Ask git one thing, or answer ``None`` where there is nothing to ask.

	**Absent rather than an error**, and every way of not being in a repository takes the same
	branch: no git on the machine, not a checkout, a repository with no commits. §1.4's rule is
	that no §14 entity may be *required* to do the ordinary thing, and most machines have no
	checkout — so a record made without a tree is the ordinary case rather than a degraded one.
	"""

	try:
		answered = subprocess.run(
			["git", *arguments],
			capture_output=True,
			text=True,
			timeout=10,
			check=False,
		)

	except (OSError, subprocess.SubprocessError):
		return None

	written = answered.stdout.strip()

	return written if answered.returncode == 0 and written else None


def _tree_here () -> str:
	"""Return the tree object this checkout's `HEAD` names, or nothing.

	**The tree rather than the commit**, which is `#1121`'s whole correction: a commit sha
	names a commit that may not exist yet — the gate runs *before* the commit it is about,
	which is the one commit nothing else ever runs (`#894`) — where a tree names the content
	either way. Two commits with the same content share a tree, which is the right answer:
	rebasing does not invalidate what was checked.
	"""

	return _git("rev-parse", "HEAD^{tree}") or ""


def _commit_here () -> str:
	"""Return the commit this checkout is on, or nothing. Beside the tree, never instead."""

	return _git("rev-parse", "HEAD") or ""


def _verified (
	program: Program,
	*,
	which: str,
	summary: str,
	passed: bool,
	tree: str,
	commit: str,
) -> None:
	"""Record what was checked against one task."""

	with program.opened() as world:
		located, task = _a_task(
			program,
			world,
			_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
			verb="verify",
		)
		client = _require_connection(program, world, located.connection)

		try:
			written = client.verify(
				ref=task.ref,
				passed=passed,
				summary=summary or None,
				tree_hash=tree or None,
				commit_sha=commit or None,
				workspace=located.workspace,
			)

		except subroutine.errors.SubroutineError as error:
			program.fail(error)

		program.say(
			f"Recorded: {'passed' if written.passed else 'failed'} on "
			f"{world.address_of_located(located)}"
			+ (f" against tree {written.tree_hash[:7]}" if written.tree_hash else "")
		)

		if written.tree_hash is None:
			# **Said rather than left to be discovered.** A record with no tree cannot go out
			# of date, and somebody who thinks it can will trust it after the code has moved.
			program.warn(
				"No tree was recorded, so this cannot go out of date when the code changes."
			)


def _released_everything (program: Program) -> None:
	"""Give back everything this account is holding, wherever it is.

	`#1122`. What a session-end hook needs, because a hook has no list of refs to give back and
	the session that would have collected one has stopped. Every reachable place is asked, so
	an agent working across two connections does not leave half its leases behind.

	**Only what is *held*, which is not the same as what was ever claimed** — an expired claim
	is treated as absent (§10.7 invariant 10), so this releases what somebody else would
	otherwise be told is taken, and says nothing about the rest.

	**Silent when there is nothing**, because that is the ordinary case at the end of a session
	that finished what it started — finishing hands a claim back by itself (`#1113`) — and a
	hook printing a line every time it does nothing is a hook people turn off.
	"""

	with program.opened() as world:
		freed: list[str] = []
		unanswered: list[str] = []

		# **Per workspace rather than per connection**, like every other listing here: a task
		# listing refuses an ambiguous workspace (§8.2), and a client that quietly spanned them
		# would return different rows depending on where the work was.
		for reached in world.reached:
			for workspace in reached.identity.workspaces:
				try:
					holding = reached.client.tasks(
						workspace=workspace.slug,
						claimed_by="me",
						include_completed=True,
					)

				except subroutine.errors.SubroutineError as refused:
					# **A place that cannot answer is skipped and *said*, not swallowed.** It
					# runs as somebody's session ends, so refusing to give anything back
					# because one of three places is unreachable would be the worst of both —
					# and staying silent about it is worse still, because an instance a release
					# too old to be asked would report success and change nothing. That is the
					# defect this whole command exists inside.
					unanswered.append(f"{reached.name}/{workspace.slug}: {refused}")

					continue

				for task in holding:
					try:
						reached.client.release(ref=task.ref, workspace=workspace.slug)

					except subroutine.errors.SubroutineError as refused:
						unanswered.append(f"{reached.name}/#{task.ref}: {refused}")

						continue

					freed.append(f"#{task.ref}  {task.title}")

		if freed:
			program.say(f"Released {len(freed)}:")

			for line in freed:
				program.say(f"  {line}")

		for line in unanswered:
			program.warn(line)


def _release_asked (program: Program, *, which: str, everything: bool) -> None:
	"""Give back one claim or all of them, refusing the pair that says both.

	**Outside `register` because that closure only shrinks**, which is `#943`'s own
	instruction: a command's body belongs in a function `register` calls. It came out when
	`#1576` gave `search` four lines of help, which is the ratchet working rather than being
	worked around — the bill for a sentence arrives in the same currency as the bill for a
	command.
	"""

	if everything:
		if which:
			# **`program.stop` rather than the `stop` the closure is handed**, which this
			# function is outside of. Same refusal, reached by the name a module-level
			# helper can see.
			program.stop(
				"Say which one, or say --all. Not both.",
				hint="'--all' gives back everything you are holding, so a number narrows "
				"nothing.",
			)

		_released_everything(program)

		return

	_released(program, which=which)


def _released (program: Program, *, which: str) -> None:
	"""Put a task back, whether or not anybody had claimed it."""

	with program.opened() as world:
		located, task = _a_task(
			program,
			world,
			_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
			verb="release",
		)
		client = _require_connection(program, world, located.connection)

		try:
			freed = client.release(ref=task.ref, workspace=located.workspace)

		except subroutine.errors.SubroutineError as error:
			program.fail(error)

		program.say(_acted(world, dataclasses.replace(located, item=freed), "Released"))


def _project_prioritised (program: Program, *, key: str, none: bool) -> None:
	"""Raise one project's work in this workspace's ranked listings — `#986`, decision `#982`.

	**The command is on the project because that is what a person says**, while the state is one
	field on the *workspace*. The two are not in tension: ``prioritise web`` names the thing being
	chosen, and there is only ever one choice, which is why an API route on the project would
	have been wrong — it would read as a per-project flag and invite four of them.

	**It says what it displaced, and that sentence is the whole anti-spiral mechanism.** Simon's
	question was *"how would we stop this spiralling?"*: a per-project dial has an equilibrium
	indistinguishable from having no feature at all, reached by locally rational moves, because
	every boost is a silent demotion of everything untouched. One prioritised project makes the
	trade **visible at the moment it is made** — choosing B is B rising *and* A stopping, in one
	line, rather than a fact somebody has to go and reconstruct later.
	"""

	if key and none:
		program.stop(
			"Name a project or pass --none, not both.",
			hint="--none leaves nothing prioritised here.",
		)

	with program.opened() as world:
		where = world.writing_to()
		workspace = _writing_workspace(world)

		# Read before writing, so the answer can name what stopped being the priority. Read
		# from this connection rather than remembered, because a checkout is not the only thing
		# that writes here.
		before = _focus_of(where, workspace)

		if not key and not none:
			if before is None:
				program.say("Nothing is prioritised here.")
				program.say("  Name a project to raise its work: 'project prioritise web'.")

				return

			program.say(f"{before} is the priority here.")

			return

		changed = where.client.update_workspace(
			workspace, prioritised_project=None if none else key
		)

		if changed.prioritised_project is None:
			if before is None:
				program.say("Nothing was prioritised here, and nothing is now.")

				return

			program.say(f"{before} is no longer the priority here.")
			program.say("  Everything is ranked on its own importance and urgency again.")

			return

		program.say(f"{changed.prioritised_project} is the priority here.")

		# **Named rather than implied**, because the displacement is the cost of the choice and
		# a reader who is not shown it is the reader who sets a fifth one.
		if before is not None and before != changed.prioritised_project:
			program.say(f"  {before} is not, any more.")

		program.say("  Its work rises in ranked listings, and on your agenda under Next.")
		program.say("  Anything urgent or important elsewhere still comes first.")


def _user_timezone (program: Program, *, zone: str, clear: bool) -> None:
	"""Say where you keep your diary, or report where the instance thinks you do — `#994`.

	**Your own account and nobody else's**, which is why this takes no username. Simon's
	decision of 2026-08-18: *"A user knows which timezone they are in better than anyone else."*
	There is no permission that grants it for somebody else, so there is nothing to name.
	"""

	if zone and clear:
		program.stop(
			"Say a zone or pass --clear, not both.",
			hint="Clearing puts you back on whatever this workspace uses.",
		)

	with program.opened() as world:
		where = world.writing_to()
		username = where.client.me().user.username

		if not zone and not clear:
			account = next(
				(one for one in where.client.users() if one.username == username), None
			)

			if account is not None and account.timezone is not None:
				program.say(f"You are in {account.timezone}")

				return

			program.say("You have not said which timezone you are in.")
			program.say("  Your days are counted in this workspace's zone until you do.")

			# **Suggested only when there is nothing to read back** (`#1002`). §12.2a's habit
			# is that a command names the next one, and the next one is not the one that has
			# just been answered — the example names a zone, so offering it to somebody who
			# has already said where they are is an invitation to be wrong.
			_suggest(program.console, "subroutine user timezone Europe/London")

			return

		changed = where.client.set_timezone(
			username=username, timezone=None if clear else zone
		)

		if changed.timezone is None:
			program.say("Cleared. Your days are counted in this workspace's zone again.")

			return

		program.say(f"You are in {changed.timezone}")

		# **Said because it is the only thing this changes**, and it is not obvious: a zone is
		# usually a display setting, and this one is not — `#773` renders a day-scale date in
		# the *task's* zone whatever the reader's is. What it moves is which day a deadline
		# counts as, which is the whole of `#989`'s second decision.
		program.say("  Your agenda is counted from midnight there, on every surface.")


def _whoami (program: Program, *, json_output: bool, strict: bool) -> None:
	"""Say which account this machine is acting as, per connection.

	**Out of `register`'s closure to pay for `#1034`'s `--hide-nothing`** (`#943`'s ratchet,
	which only goes down). It needed `say` and `console`, both of which :class:`Program` already
	carries — which is the whole reason that class exists, and the reason this was a lift rather
	than a rewrite.
	"""

	with program.opened(strict=strict) as world:
		gathered = subroutine.fanout.gather(
			world.clients, lambda client: client.me(), strict=strict
		)

		if json_output:
			program.say(
				json.dumps(
					[
						{
							"connection": answer.connection.name,
							# **Beside the instance's own numbers, not instead of them**
							# (`#381`). `instance_version` arrives inside the response;
							# these two are properties of the process that asked, and a
							# reader comparing them is doing the whole job of this field.
							"program_version": subroutine.installations.program(),
							"plugin_version": subroutine.installations.plugin(),
							**answer.value.model_dump(mode="json"),
						}
						for answer in gathered.answers
					],
					indent=2,
				)
			)

			return

		for index, answer in enumerate(gathered.answers):
			if index:
				program.say("")

			if world.qualifies_connection:
				program.console.print(rich.text.Text(answer.connection.label, style=HEADING))

			because = _agent_because(answer.connection, world.roster.default)

			for line in _whoami_lines(answer.value, agent_because=because):
				program.console.print(line)

			# **A footer, and per connection rather than once** (`#381`). The program is
			# the same for every block and the *instance* is not, so the one line that
			# would be repeated is also the one that has to sit beside the answer it
			# describes — a single trailing line naming three connections' versions is
			# unreadable, and worse, is read as applying to whichever block is nearest.
			program.console.print("")

			for line in subroutine.views.versions(
				answer.value,
				program=subroutine.installations.program(),
				plugin=subroutine.installations.plugin(),
			):
				program.console.print(line)

			# **After the versions, and for the same reason they are here** (`#1089`): a
			# divergence nobody can see from inside, reported by the one command whose job is
			# *what am I actually talking to*. Per connection, because each names an account
			# and two accounts may sit in different zones.
			for line in subroutine.views.zones(
				answer.value, machine=subroutine.config.system_timezone()
			):
				program.console.print(line)

		_report(program, world, gathered.failures)


def _connections_listed (program: Program) -> None:
	"""Print every instance this machine reaches, and which one a write goes to.

	**Lifted out of ``register`` to pay for `#1025`'s two ``--colour`` options** (`#943`'s
	ratchet, which only goes down). It needed nothing but :class:`Program`, which is what that
	class exists for: seven of these were closure names until `#943`, and a helper reachable
	only by running a Typer command is one nothing can test directly.
	"""

	resolved = program.settings()
	current = None

	try:
		roster = subroutine.connections.roster(resolved)

		# Resolved without opening anything, deliberately: this is the command somebody runs
		# when a connection is *not* working, so it must not need one to answer.
		current = subroutine.context.resolve(
			roster,
			connection=program.selected.connection,
			workspace=program.selected.workspace,
			marker=subroutine.directory.find(),
		)

	except subroutine.errors.SubroutineError as error:
		program.fail(error)

	_warn_about_the_credentials_file(program.warn)

	# Named per connection, because a person with three of them needs to know *which*.
	for exposed in roster:
		if subroutine.connections.in_the_clear(exposed):
			program.warn(
				f"{exposed.name} is reached over plain http, so its token crosses the "
				f"network readable by anything in between."
			)

	rows = [
		_connection_row(program, connection, roster, resolved, current) for connection in roster
	]
	widths = [max(len(row[column]) for row in rows) for column in range(3)]

	for row in rows:
		program.say(
			f"{row[0].ljust(widths[0])}  {row[1].ljust(widths[1])}  "
			f"{row[2].ljust(widths[2])}  {row[3]}"
		)

	# **Where it came from, when the two answers differ** (`#278`). One word in a column cannot
	# say why, and why is the whole question when somebody has just watched a write land
	# somewhere they did not expect. Silent when they agree, which is the ordinary case.
	if current.connection != roster.default:
		program.say("")
		program.say(f"Writing to {current.describe(qualified=roster.qualifies)}.")

	program.say("")
	_suggest(program.console, "subroutine use")


def _workspace_updated (
	program: Program,
	*,
	slug: str,
	title: str,
	description: str,
	timezone: str,
	colour: str,
	hide_status: list[str] | None,
) -> None:
	"""Change the fields beside a workspace's address — `#434`, `#1025`."""

	changes: dict[str, typing.Any] = {}

	if title is not UNGIVEN:
		changes["title"] = title

	if description is not UNGIVEN:
		changes["description"] = description or None

	if timezone is not UNGIVEN:
		changes["timezone"] = timezone or None

	# Everything in the workspace inherits these unless a project sets its own (`#1026`,
	# `#1029`) — one chain, walked upwards, resolved on the server.
	settings: dict[str, typing.Any] = {}

	if colour is not UNGIVEN:
		settings[subroutine.domain.settings.COLOUR.key] = colour or None

	settings.update(_hidden_statuses(hide_status) or {})

	if settings:
		changes["settings"] = settings

	if not changes:
		program.stop(
			"Nothing to change.",
			hint="Pass --title, --description, --timezone, --colour or --hide-status.",
		)

	with program.opened() as world:
		where = world.writing_to()
		changed = where.client.update_workspace(slug, **changes)

		program.say(f"Changed {changed.slug} — {changed.title}")

		# §6.5 makes this the step every date in the workspace is read through, so the
		# confirmation names the zone in force rather than reporting that something changed.
		if "timezone" in changes:
			program.say(f"  Dates here are read in {changed.timezone or 'the instance zone'}.")


def _register_documents (app: typer.Typer, program: Program) -> None:
	"""Add the ``doc`` group to the application.

	The fifth group to leave ``register`` rather than raise `#943`'s ratchet. Two commands and
	the longest pair left in the closure; by now the move costs a paragraph, which is the
	ratchet doing what it was built for — making a feature notice.
	"""

	# **`doc create` and no `doc list` or `doc show`**, which is §12.2's shape rather than an
	# omission: one counter per workspace serves both kinds (§6.2), so `list` already holds
	# documents and `show <ref>` already reads either. A second listing would be a second
	# answer to a question already answered, and the *first* listing is what taught somebody
	# that a number names an item.
	document_app = typer.Typer(
		help="Write down what you concluded.", no_args_is_help=True
	)

	# **`document` visible and `doc` hidden**, which is `list`/`ls` unchanged (`#154`): the real
	# word in the help, the abbreviation still working and out of the way. It had been the other
	# way round, so the word the other three surfaces use — `subroutine_document`, the
	# `/v1/documents` collection, the browser's *Writing* select — did not exist here at all, and
	# `subroutine document --help` answered *"Did you mean 'comment'?"*, which is a different
	# kind of record (`#1549`).
	#
	# One group object registered twice, so there is no second declaration to drift.
	app.add_typer(document_app, name="document")
	app.add_typer(document_app, name="doc", hidden=True)

	@document_app.command("create")
	def document_create (
		title: str = typer.Argument(..., help="What it concludes, in one line."),
		body: str = typer.Option("", "--body", help="The reasoning. Or pipe it in."),
		kind: str = typer.Option(
			"", "--type", help="note, spec, design, decision, finding or dead_end."
		),
		status: str = typer.Option(
			"", "--status", help="A status key. A decision starts 'active'; use 'draft' if not."
		),
		project: str = typer.Option("", "--project", help="File it under this project, by key."),
		tag: list[str] | None = typer.Option(
			None, "--tag", help="Label it. Repeatable, and the same tags tasks use."
		),
		json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
		without_body: bool = typer.Option(
			False, "--no-body", help="Leave the document's text out of the result."
		),
	) -> None:
		"""Write a document — a decision, a finding, a design, a dead end.

		A decision, a finding and a dead end are in force the moment you write them, so they
		start as 'active'; a specification or a design starts as 'draft'. Pass '--status draft'
		for a decision you are still thinking about.

		Examples:

		  subroutine document create "Why we dropped the queue" --type decision

		  cat notes.md | subroutine document create "Review findings" --type finding

		A comment is what happened; a document is what you concluded. If the next person to
		look would need to read it, it is a document.
		"""

		# **Piped input is the ordinary way to write more than a sentence at a terminal**, and
		# it is the path an agent takes too. Read only when something is actually piped:
		# `isatty` false with no pipe would block forever waiting for a keystroke nobody knows
		# to give, which is the worst possible way for a first attempt to go.
		written = body.strip() or (None if sys.stdin.isatty() else sys.stdin.read().strip())

		with program.opened() as world:
			where = world.writing_to()

			created = where.client.create_document(
				title=title,
				body=written or None,
				type=kind.strip() or None,
				status=status.strip() or None,
				project=project.strip() or None,
				tags=tag or None,
				workspace=_writing_workspace(world),
			)

			if json_output:
				program.say(_written_back(created, without_body=without_body))

				return

			# **A slug, not the id** (`#289`). `Located.workspace` is what `refs.format_address`
			# composes `connection/workspace/ref` from, so the id rendered an address nobody
			# could type: `local/019fad98-4313-7e36-b972-f7decf66f8ae/#288`. Every other caller
			# of `_acted` passes a slug, and `add` gets it from this same function.
			program.say(
				_acted(
					world,
					Located(
						connection=where.name,
						workspace=_writing_workspace(world),
						item=created,
					),
					"Wrote",
				)
			)
			_suggest(
				program.console,
				f"subroutine show {_typeable(world, where.name, created)}",
				"read it back",
			)

	@document_app.command("edit")
	def document_edit (
		which: str = typer.Argument("", help="Which document, by its number."),
		body: str = typer.Option("", "--body", help="Replace the text. Or pipe it in."),
		title: str = typer.Option("", "--title", help="Say what it concludes, in one line."),
		kind: str = typer.Option(
			"", "--type", help="note, spec, design, decision, finding or dead_end."
		),
		status: str = typer.Option("", "--status", help="A status key, e.g. superseded."),
		project: str = typer.Option("", "--project", help="File it under this project, by key."),
		tag: list[str] | None = typer.Option(
			None, "--tag", help="Label it. Repeatable, and the same tags tasks use."
		),
		json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
		without_body: bool = typer.Option(
			False, "--no-body", help="Leave the document's text out of the result."
		),
	) -> None:
		"""Revise a document you have already written.

		Examples:

		  subroutine document edit 42

		  subroutine document edit 42 --title "What we settled, and why"

		  cat revised.md | subroutine document edit 42

		With nothing to change, this opens the document in your editor.

		A conclusion that cannot be revised is a record of what you concluded once — so this
		is what keeps the instance the place the *current* answer lives.
		"""

		asked = _asked(which, "Which document?")

		# **Read before any of the three sources are consulted**, because the editor needs the
		# current text to open and the refusal for a bad ref should arrive before somebody has
		# spent five minutes typing into vim.
		with program.opened() as world:
			located, document = _a_document(program, world, asked, verb="edit")

			# **Standard input is consulted only when nothing else was said at all** (`#299`).
			# There is no way to tell an empty pipe from no pipe without blocking, so the
			# question has to be settled before reading rather than by reading: a caller who
			# named a field has told us what they wanted, and reading on top of that would
			# hang wherever there is no terminal — every script, CI job and agent shelling
			# out — as well as replacing a body nobody asked to replace.
			#
			# So `doc edit 42 --title "…"` changes the title and leaves the text alone, which
			# is also what somebody typing it expects.
			# **Derived, because the hand-written version fell behind twice** (`#1201`). Two
			# flags were added to this command after the tuple was written and neither was
			# added to it, so each was refused when used on its own. A list somebody has to
			# remember to extend is the shape this repository keeps removing; anything that is
			# not the ref, the output format or the body itself is somebody saying what they
			# wanted.
			named = any(
				(
					title.strip(),
					kind.strip(),
					status.strip(),
					project.strip(),
					tag is not None,
				)
			)
			said = body.strip()
			revised: str = subroutine.clients.base.UNSET

			if said:
				revised = said

			elif not named:
				# Nothing was said, so the text comes from somewhere else: the editor when
				# there is a person, and otherwise whatever was piped.
				if sys.stdin.isatty():
					revised = _in_an_editor(program, document.body or "")

				else:
					revised = sys.stdin.read()

					# **An empty pipe is not an instruction to empty the document.**
					# `subroutine doc edit 42 < /dev/null` would otherwise silently replace a
					# conclusion with nothing, which is the one outcome nobody types that to get.
					if not revised.strip():
						program.fail(
							subroutine.errors.ValidationError(
								"Nothing was piped in, so there is nothing to change.",
								# **Every flag this command takes, and a test compares this
								# sentence against the command's own options** (`#1201`). It
								# listed five and omitted `--tag`, so somebody who had just
								# used that flag was told to try something else — the copy a
								# caller reads having fallen furthest behind. A guard rather
								# than runtime introspection: the list is worth reading in the
								# source, and what it must not do is disagree.
								hint="Pipe the new text in, or pass --body, --title, --type, "
								"--status, --project or --tag.",
							)
						)

			where = world.connection(located.connection)

			if where is None:
				program.fail(
					subroutine.errors.ServiceUnavailable(
						f"{located.connection} could not be reached, so nothing can be changed "
						"there."
					)
				)

			changed = where.client.update_document(
				ref=document.ref,
				workspace=located.workspace,
				title=title.strip() or subroutine.clients.base.UNSET,
				body=revised,
				type=kind.strip() or subroutine.clients.base.UNSET,
				status=status.strip() or subroutine.clients.base.UNSET,
				project=project.strip() or subroutine.clients.base.UNSET,
				# **`--tag` given no value clears them**, which is §8.3's null and the only way
				# to take a mistyped tag off. Typer gives an empty list when the flag is absent,
				# so "not asked" and "asked for none" are told apart by `None`.
				tags=subroutine.clients.base.UNSET if tag is None else tag,
				# **Sent only when this command is replacing the body, and that is the whole
				# of the rule** (`#842`, §8.9). A revision is a whole-body replace, so a lost
				# update here takes every paragraph the other writer added, with no record
				# that they existed — where a lost update on a task takes one field.
				#
				# **The version is one this invocation genuinely showed somebody.** It was
				# read seconds or minutes ago by `_a_document` above, and in the editor path
				# it is the id of the exact text they have been editing. §8.9 is opt-in
				# because `None` means *did not ask* rather than *asked and passed*, and
				# refusing somebody for a version they never saw would be `#755`'s
				# quick-status argument in reverse — which is why a field-only edit is not
				# guarded. `subroutine doc edit 42 --title "…"` changes a title and puts no
				# body at stake, so it goes on working while somebody else is writing.
				expected_version=(
					document.version if revised is not subroutine.clients.base.UNSET else None
				),
			)

			if json_output:
				program.say(_written_back(changed, without_body=without_body))

				return

			program.say(
				_acted(
					world,
					Located(
						connection=located.connection,
						workspace=located.workspace,
						item=changed,
					),
					"Revised",
				)
			)
			_suggest(
				program.console,
				f"subroutine show {_typeable(world, located.connection, changed)}",
				"read it back",
			)



def _register_links (app: typer.Typer, program: Program) -> None:
	"""Add ``link`` and ``unlink`` to the application.

	**Out of ``register`` rather than in it**, for the reason ``_register_workspace`` gives:
	`#943`'s ratchet fired on `#1136`, which added five lines to one command's help, and what
	the ratchet asks for is that a group leaves rather than that the ceiling moves. These two
	are the natural unit — a link and its undo, sharing every helper they use and reaching
	nothing in the closure but ``program`` and the application itself.
	"""

	@app.command("link")
	def link_items (
		which: str = typer.Argument("", help="Which item, by its number. Or several: 9,11,12."),
		relation: str = typer.Argument(
			"", help=f"{subroutine.db.seed.named_link_types()}."
		),
		other: str = typer.Argument("", help="The other item, by its number. Or several: 9,11,12."),
	) -> None:
		"""Say how two items are related.

		Examples:

		  subroutine link 42 blocks 43

		  subroutine link 42 blocks 43,44,45

		  subroutine link 43,44,45 blocks 42

		  subroutine link 42 relates-to 12

		  subroutine link 7 documents 42

		'blocks' is the one that changes what you see: 'subroutine list --ready' leaves out
		anything blocked by unfinished work, so this is how that filter learns anything.

		'documents' is the one that says a decision governs a piece of work, so that whoever
		picks #42 up can be shown what they have to read before starting it.

		Those are the five a new workspace is given. A workspace can rename them or add its
		own, and naming one this workspace does not have lists the ones it does.

		Several numbers separated by commas make one link each, all of the same kind. Either
		side takes them, and both sides at once means every one of the first joined to every
		one of the second — which is what 'each of these blocks each of those' says and is the
		only thing it could say.

		Both sides matter because a plan is written from both ends: 'these six make up the
		roadmap' is six things blocking one, and 'this has to happen before those three' is one
		blocking three. Laying out a plan is the moment this is most heavily used, and it is
		the moment one link per command costs most.
		"""

		wanted = _relation_key(_asked(relation, "How are they related?"))

		with program.opened() as world:
			_joined(
				program,
				world,
				which=_asked(which, "Which one?"),
				other=_asked(other, "And the other one?"),
				relation=wanted,
			)

	@app.command("unlink")
	def unlink_items (
		which: str = typer.Argument("", help="Which item, by its number."),
		other: str = typer.Argument("", help="The item it is joined to. Or several: 9,11,12."),
		relation: str = typer.Option(
			"",
			"--type",
			show_default=False,
			help=f"Which kind to undo, when there is more than one: "
			f"{subroutine.db.seed.named_link_types()}.",
		),
	) -> None:
		"""Undo a link between two items.

		Examples:

		  subroutine unlink 42 43

		  subroutine unlink 42 43,44,45

		  subroutine unlink 42 43 --type blocks

		Worth having beside 'link' rather than later. A link added by mistake blocks work that
		is not blocked, and --ready then hides it — so an unwanted link is worse than a missing
		one, because it narrows what looks startable and says nothing about doing so.

		You do not have to say which kind, because usually there is only one and having to
		remember the relation is what leaves a wrong link in place. Where two items are joined
		more than one way, this says so and lists them rather than removing both.

		Several numbers separated by commas undo one link each, which is what a plan laid out
		the wrong way round needs.
		"""

		with program.opened() as world:
			_unjoined(
				program,
				world,
				which=_asked(which, "Which one?"),
				other=_asked(other, "And the other one?"),
				relation=relation,
			)



#: What a `SessionEnd` hook runs when a session using Subroutine finishes.
#:
#: **Guarded on the program being there**, because a hook that fails noisily at the end of
#: every session is one somebody turns off — and this settings file may outlive the install,
#: travel to another machine in a repository, or be read by somebody who never had it.
#:
#: **Quiet on success and loud on nothing**: `release --all` says nothing when there is nothing
#: to give back, which is the ordinary case once work has been finished (`#1113`), so the noise
#: is proportional to what actually happened.
SESSION_END_COMMAND = (
	"command -v subroutine >/dev/null 2>&1 && subroutine release --all || true"
)

#: Which harnesses ``setup`` knows how to wire, and where each keeps its settings.
#:
#: **One, and it says so** (`#1122`). A ``setup`` that silently covered one of five would be
#: `#559`'s shape: a name promising more than it does, where the gap is invisible until
#: somebody depends on it. Codex and Cursor are the same shape in a different file and are a
#: separate item once this one has been driven.
HARNESSES: dict[str, str] = {"claude": ".claude/settings.json"}


def _hooked (settings: dict[str, typing.Any]) -> bool:
	"""Report whether this settings object already runs the release command at session end."""

	for entry in settings.get("hooks", {}).get("SessionEnd", []):
		for hook in entry.get("hooks", []):
			if hook.get("command") == SESSION_END_COMMAND:
				return True

	return False


def _with_the_hook (settings: dict[str, typing.Any]) -> dict[str, typing.Any]:
	"""Return these settings with the session-end hook added, keeping everything else.

	**Merged per key rather than written whole.** This file is the reader's, not ours: it holds
	their permissions, their model, their other hooks. Replacing it would be a tool taking a
	shared store for its own — and `#1043`'s recorded shape, where a settings blob carrying
	another subsystem's live state was rewritten from a partial read.
	"""

	merged = dict(settings)
	hooks = dict(merged.get("hooks", {}))
	at_the_end = list(hooks.get("SessionEnd", []))

	at_the_end.append(
		{"hooks": [{"type": "command", "command": SESSION_END_COMMAND}]}
	)
	hooks["SessionEnd"] = at_the_end
	merged["hooks"] = hooks

	return merged


def _agent_files (root: pathlib.Path) -> list[pathlib.Path]:
	"""Return the agent instruction files this repository already keeps, if any.

	**Only ones that exist.** Creating an agent file is a decision about how a project is run
	and is not this command's to take — and a `CLAUDE.md` appearing in somebody's repository
	because they wired up a task tracker is precisely the kind of thing that gets a tool
	uninstalled.
	"""

	return [
		root / name
		for name in ("CLAUDE.md", "AGENTS.md")
		if (root / name).is_file()
	]


def _pointer (marker: subroutine.directory.Marker) -> str:
	"""Return the one line that tells a future session which project this checkout is."""

	named = marker.project or marker.workspace or ""

	return (
		"This project's work is tracked in Subroutine"
		+ (f", under `{named}`" if named else "")
		+ ". Read `subroutine help`, or the `subroutine` skill if you have it.\n"
	)


def _register_projects (app: typer.Typer, program: Program) -> None:
	"""Add the ``project`` group to the application.

	The fourth group to leave ``register`` rather than raise `#943`'s ratchet, after
	``workspace``, ``link``/``unlink`` and ``user``. Six commands, and by now the move is
	mechanical: the groups were already there, so what a feature pays is the cost of noticing
	rather than the cost of designing.
	"""

	# **Visible, unlike `use` and `connections` below.** Progressive disclosure (§1.4) is
	# about never *requiring* a project in order to keep a to-do list, not about hiding the
	# noun — `subroutine list --project SR` already names it, and until 2026-07-31 there was
	# no way to make one outside the HTTP API, so on a default install the only project
	# anybody would ever have was the Inbox (`#134`). A hidden command would have left that
	# wall standing with the door merely painted over.
	project_app = typer.Typer(
		help="Group work into projects.", invoke_without_command=True
	)
	app.add_typer(project_app, name="project")

	# **A bare `project` lists, because a bare `connections` already does** (`#1355`). The two
	# surfaces disagreed: `subroutine_project` with no arguments lists the projects and this
	# printed help, so an agent that learned one form had to learn the other separately. Help
	# is still one keystroke away and is what an unrecognised subcommand gets.
	@project_app.callback()
	def project_group (context: typer.Context) -> None:
		"""Group work into projects."""

		if context.invoked_subcommand is not None:
			return

		_projects_listed(program, json_output=False)

	@project_app.command("create")
	def project_create (
		key: str = typer.Argument(..., help="Its permanent short name, like WEB."),
		title: str = typer.Argument(..., help="What it is called."),
		description: str = typer.Option("", "--description", help="What it is for."),
		parent: str = typer.Option("", "--parent", help="Put it inside this project."),
		private: bool = typer.Option(
			# **"Only you", not "only its members"** (`#1444`). Both are true and one of them
			# is the wrong emphasis at the moment of choosing: §7.3a grants sight to holders
			# of a `project_member` row, and creating one writes exactly one — the owner's. So
			# *members* describes a set that starts at one, and the reader needs to know that
			# before they file anything into it.
			#
			# **It now names the way in as well.** Until `#1444` there was no writer at all and
			# this said "nothing can share it yet", which was true and is not.
			False, "--private", help="Only you can see it, until you share it."
		),
		json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
	) -> None:
		"""Make a project to file work under.

		Examples:

		  subroutine project create WEB "Website redesign"

		  subroutine project create API "Public API" --parent WEB

		The key is how this project is addressed here — in '+KEY' when you capture a line,
		and in its web address. A to Z and 0 to 9, starting with a letter, up to sixteen
		characters.

		It can be changed later with 'subroutine project rename', which says what will stop
		working before it does it. Nothing already recorded moves: every item keeps its
		number, because a number belongs to the workspace rather than to the project.
		"""

		with program.opened() as world:
			where = world.writing_to()

			created = where.client.create_project(
				key=key,
				title=title,
				description=description.strip() or None,
				parent=parent.strip() or None,
				visibility="private" if private else "public",
				workspace=_writing_workspace(world),
			)

			if json_output:
				program.say(json.dumps(created.model_dump(mode="json"), indent=2))

				return

			program.say(f"Created {created.key} — {created.title}")

			# **Said at the one moment it can be acted on** (`#1444`). A private project is
			# visible to its owner and to nobody else until somebody is named, and this is the
			# only point at which the person choosing it is thinking about who will read it.
			#
			# **Both ways out, and said so**, because the alternative reads as a refusal: a
			# reader who has just been told a thing is invisible needs to know what to do about
			# it more than they need the reason. Naming one person and publishing it to the
			# workspace are different acts with different consequences, so both are offered
			# rather than the wider one standing in for the narrower.
			#
			# This paragraph used to say a private project was invisible *permanently* and that
			# the writer "was never built". True when it was written, and the whole of `#1444`.
			if created.visibility == "private":
				program.say(
					f"Only you can see it. 'subroutine project share "
					f"{_capture_name(world, created)} <username>' lets somebody else in, and "
					f"'subroutine project update {_capture_name(world, created)} --public' "
					f"shows it to the whole workspace."
				)

			# **The next command is the one that uses it**, not another one about projects.
			# A project nobody files anything into is an empty gesture, and `+KEY` is the part
			# of the capture grammar somebody who has just made one has no reason to know.
			#
			_suggest(program.console, f'subroutine add "something to do +{_capture_name(world, created)}"')

	@project_app.command("list")
	def project_list (
		json_output: bool = typer.Option(False, "--json", help="Print the list as JSON."),
	) -> None:
		"""Show the projects you can see, with what is inside what.

		Examples:

		  subroutine project list
		"""

		_projects_listed(program, json_output=json_output)

	@project_app.command("rename")
	def project_rename (
		key: str = typer.Argument(..., help="The project, by its current short name."),
		to: str = typer.Argument(..., help="Its new short name."),
		yes: bool = typer.Option(False, "--yes", help="Do not ask."),
	) -> None:
		"""Give a project a different short name.

		Examples:

		  subroutine project rename ST SR

		The old name stops working, and nothing is left pointing at it. That is deliberate:
		a name you retired should be retired. Nothing already recorded moves — every item keeps
		its number, and what it is filed under does not change.

		What does break is anything that wrote the old name down: a bookmarked address, a
		'.subroutine' file in a checkout, a '+OLD' in a shell history. This says so before it
		does it.
		"""

		_project_renamed(program, key=key, to=to, yes=yes)

	@project_app.command("share")
	def project_share (
		key: str = typer.Argument(..., help="The project, by its short name."),
		username: str = typer.Argument(..., help="Who to let in."),
	) -> None:
		"""Let somebody see a private project.

		Examples:

		  subroutine project share secret jo

		This grants sight and nothing else — what they may do in the project is still their
		role in the workspace, so they have to be in it already.

		A project inside a private one is hidden by the parent, and a membership on the child
		grants nothing while that is true. Sharing the parent is what opens it, and this says
		which project that is rather than appearing to work.
		"""

		_project_shared(program, key=key, username=username)

	@project_app.command("unshare")
	def project_unshare (
		key: str = typer.Argument(..., help="The project, by its short name."),
		username: str = typer.Argument(..., help="Who to shut out."),
	) -> None:
		"""Stop somebody seeing a private project.

		Examples:

		  subroutine project unshare secret jo

		The owner cannot be removed, and neither can the last person left: a private project
		nobody holds is one nobody can see or make public again.
		"""

		_project_unshared(program, key=key, username=username)

	@project_app.command("sharing")
	def project_sharing (
		key: str = typer.Argument(..., help="The project, by its short name."),
		json_output: bool = typer.Option(False, "--json", help="Print the list as JSON."),
	) -> None:
		"""Show who can see a project.

		Examples:

		  subroutine project sharing secret

		A public project usually shows just its owner. That is not a mistake — it says who
		would still see it if somebody made it private, which is what anybody about to do
		that is asking.
		"""

		_project_sharing(program, key=key, json_output=json_output)

	@project_app.command("update")
	def project_update (
		key: str = typer.Argument(..., help="The project, by its short name."),
		title: str = typer.Option(UNGIVEN, "--title", show_default=False, help="What to call it."),
		description: str = typer.Option(
			UNGIVEN, "--description", show_default=False, help="What it is for. Pass '' to clear."
		),
		status: str = typer.Option(
			UNGIVEN,
			"--status",
			show_default=False,
			help="active, on_hold, completed or archived.",
		),
		colour: str = typer.Option(
			UNGIVEN, "--colour", show_default=False, help=COLOUR_HELP
		),
		hide_status: list[str] = typer.Option(
			None, "--hide-status", show_default=False, help=HIDE_STATUS_HELP
		),
		hide_nothing: bool = typer.Option(
			False, "--hide-nothing", help="Offer every status here, whatever the workspace hides."
		),
		private: bool | None = typer.Option(
			None, "--private/--public", show_default=False, help="Who can see it."
		),
	) -> None:
		"""Change a project's name, what it is for, who can see it, or where it stands.

		Examples:

		  subroutine project update web --title "Website redesign"

		  subroutine project update web --status on_hold

		Putting a project on hold leaves everything in it exactly where it is and still
		findable. What changes is that its work stops being offered as something to start.

		Its short name is not changed here — that breaks addresses, so it has a command of
		its own with a warning attached: 'subroutine project rename'.
		"""

		_project_updated(
			program,
			key=key,
			title=title,
			description=description,
			status=status,
			colour=colour,
			hide_status=hide_status,
			hide_nothing=hide_nothing,
			private=private,
		)

	instance_app = typer.Typer(
		help="Look after this installation itself.", no_args_is_help=True
	)
	app.add_typer(instance_app, name="instance")

	@instance_app.command("update")
	def instance_update (
		name: str = typer.Option("", "--name", help="What to call this installation."),
		timezone: str = typer.Option(
			"", "--timezone", help="Where it is, as an IANA zone like Europe/London."
		),
	) -> None:
		"""Change what this installation is called, or where it says it is.

		Examples:

		  subroutine instance update --name "Hyperfence"

		  subroutine instance update --timezone Europe/London

		The name is a label rather than an identity — it is what tells this installation from
		another one you can reach, and changing it breaks nothing.

		The timezone is the last word on what a day means here. It is read by anybody who has
		not set their own and whose workspace has not set one, so on a fresh installation that
		is everybody.
		"""

		if not name.strip() and not timezone.strip():
			program.stop("Say what to change: --name, --timezone, or both.")

		_instance_updated(program, name=name, timezone=timezone)

	@instance_app.command("workspaces")
	def instance_workspaces (
		json_output: bool = typer.Option(False, "--json", help="Print the list as JSON."),
	) -> None:
		"""Show every workspace on this installation, member or not.

		Examples:

		  subroutine instance workspaces

		'subroutine workspace list' shows the ones you can work in. This shows the ones that
		exist — which is a different question, and the one nothing answered: somebody who can
		create a workspace can create one you are not in, and it would not appear anywhere you
		were looking.

		Seeing a workspace here does not let you read what is in it. Joining does, and joining
		is recorded.
		"""

		_instance_workspaces(program, json_output=json_output)

	_register_workspace(app, program)

	@project_app.command("prioritise")
	def project_prioritise (
		key: str = typer.Argument("", help="The project to raise, by name or address."),
		none: bool = typer.Option(False, "--none", help="Stop prioritising anything here."),
	) -> None:
		"""Raise one project's work above the rest, without hiding anybody else's.

		Examples:

		  subroutine project prioritise web

		  subroutine project prioritise --none

		One project per workspace, and choosing another moves it. Its work rises in ranked
		listings and on your agenda under Next; anything urgent or important in another
		project still comes first, which is the difference between this and hiding things.

		With no argument it says what is prioritised here.
		"""

		_project_prioritised(program, key=key, none=none)

	@project_app.command("move")
	def project_move (
		key: str = typer.Argument(..., help="The project to move, by its short name."),
		under: str = typer.Option(
			"", "--under", help="Put it inside this project, by key."
		),
		root: bool = typer.Option(
			False, "--root", help="Make it a top-level project instead."
		),
		yes: bool = typer.Option(False, "--yes", help="Do not ask."),
	) -> None:
		"""Move a project, and everything underneath it, somewhere else in the tree.

		Examples:

		  subroutine project move WEB --under ACME

		  subroutine project move WEB --root

		Nothing is renumbered and nothing is refiled: every item keeps its number and stays in
		the project it was in. What moves is where that project sits.

		'--under' and '--root' are the two directions and one of them has to be said. An
		omitted destination once meant "move to root", which flattened subtrees by accident.
		"""

		_project_moved(program, key=key, under=under, root=root, yes=yes)



def _register_setup (app: typer.Typer, program: Program) -> None:
	"""Add the ``setup`` group to the application — `#1122`.

	**Out of ``register``**, like ``workspace``, ``link`` and ``user`` before it, so that
	`#943`'s ratchet is not paid for a new command that had somewhere else to go.
	"""

	setup_app = typer.Typer(
		help="Wire Subroutine into the tools on this machine.", no_args_is_help=True
	)
	app.add_typer(setup_app, name="setup")

	@setup_app.command("claude")
	def setup_claude (
		yes: bool = typer.Option(
			False, "--yes", "-y", help="Do not ask before writing."
		),
	) -> None:
		"""Wire this checkout into Claude Code, so a session hands work back when it ends.

		Examples:

		  subroutine setup claude

		It writes one hook into '.claude/settings.json' here: when a session ends, anything
		this account is still holding is given back, so nobody has to wait for the lease to
		run out on work that stopped. Everything already in that file is left alone.

		Run it from the top of the repository you want wired. It writes nothing outside this
		directory and stores no credential, so the file is safe to commit.
		"""

		root = pathlib.Path.cwd()
		where = root / HARNESSES["claude"]
		existing: dict[str, typing.Any] = {}

		if where.is_file():
			try:
				existing = json.loads(where.read_text(encoding="utf-8"))

			except (OSError, json.JSONDecodeError) as unreadable:
				# **Refused rather than overwritten.** The file is the reader's and holds
				# their permissions and their other hooks; replacing one this cannot parse
				# would be taking a shared store for our own on the strength of a syntax error.
				program.stop(
					f"{where} is there and cannot be read: {unreadable}",
					"Fix or move it, then run this again. Nothing has been written.",
				)

		if _hooked(existing):
			program.say(f"Already wired: {where}")

			return

		if not yes and not typer.confirm(f"Write a session-end hook into {where}?"):
			program.say("Nothing written.")

			return

		where.parent.mkdir(parents=True, exist_ok=True)
		where.write_text(
			json.dumps(_with_the_hook(existing), indent=2) + "\n", encoding="utf-8"
		)

		# **Read back rather than assumed** (`#236`). Installing something and its taking
		# effect are separate moments and only the first one reports — so this checks the file
		# it just wrote says what it meant, and then says plainly which half it cannot check.
		try:
			written = json.loads(where.read_text(encoding="utf-8"))

		except (OSError, json.JSONDecodeError) as unreadable:
			program.stop(
				f"{where} was written and cannot be read back: {unreadable}",
				"Check the file. The hook may not be there.",
			)

		if not _hooked(written):
			program.stop(
				f"{where} was written and does not carry the hook.",
				"Nothing here can explain that; check the file before relying on it.",
			)

		program.say(f"Wired: {where}")
		program.say("  A session ending here now gives back anything it is still holding.")

		marker = subroutine.directory.find(root)

		if marker is None:
			program.warn(
				"No .subroutine marker here, so a session has nothing saying which project "
				"this is. 'subroutine use --here --project <key>' writes one."
			)

		for file in _agent_files(root):
			line = _pointer(marker) if marker is not None else ""

			if not line or line.strip() in file.read_text(encoding="utf-8"):
				continue

			program.say(f"  {file.name} does not name the project. One line worth adding:")
			program.say(f"    {line.strip()}")

		# **What this cannot check, said out loud** (`#236` again, and the item's own rule).
		# Whether the harness reads this file, and whether it runs the hook, is only provable
		# by a session ending — and a command reporting success about somebody else's runtime
		# is the failure this whole paragraph exists to avoid.
		program.say("")
		program.say(
			"Start a new session for it to be read. Nothing here can confirm the harness "
			"runs it; ending a session and running 'subroutine list --claimed-by me' can."
		)


def _register_users (app: typer.Typer, program: Program) -> None:
	"""Add the ``user`` group to the application.

	**Out of ``register`` rather than in it**, the third time `#943`'s ratchet has been paid
	rather than raised — after ``workspace`` (`#704`) and ``link``/``unlink`` (`#1136`). Eight
	commands and 283 lines, and the largest natural unit left in the closure: every one of them
	is about membership, and none reaches anything in it but ``program`` and the application.
	"""

	# **Membership lives under `user`, and it is about the person rather than the place.**
	# Who somebody is and where they may work is one question, asked of an account; what one
	# project lets them see is another, and it is under `project` for the same reason. The
	# workspace is still where a workspace membership *lives*; it is named by `--workspace`
	# when there is more than one, and inferred otherwise.
	#
	# **This used to say "there is deliberately no `workspace` group" and cite `#174` for it,
	# and it was false the day after it was written** — the comment is `cb7f655`, 2026-08-01;
	# the group arrived in `d46490f`, 2026-08-02, and has six commands in the top-level help.
	# It stood 26 days and was quoted as the thing deciding where `project share` should go
	# (`#1444`). `#174` itself says nothing about command placement.
	user_app = typer.Typer(
		help="Add the people and agents this instance is for.", no_args_is_help=True
	)
	app.add_typer(user_app, name="user")

	@user_app.command("create")
	def user_create (
		username: str = typer.Argument(..., help="What they will be called here."),
		display_name: str = typer.Option("", "--name", help="Their full name."),
		email: str = typer.Option("", "--email", help="Their email address."),
		role: str = typer.Option(
			"",
			"--role",
			help=f"What they may do — 'member', 'admin', 'viewer'. Unset means "
			f"'{ONBOARDING_ROLE}'.",
		),
		workspace: str = typer.Option(
			"", "--workspace", help="Which workspace they work in. Needed if there are several."
		),
		browser: bool = typer.Option(
			False, "--browser", help="Also make them a sign-in link for the web interface."
		),
		terminal: bool = typer.Option(
			False, "--terminal", help="Also make them a credential for the command line."
		),
		agent: bool = typer.Option(
			False, "--agent", help="A machine identity rather than a person."
		),
		superuser: bool = typer.Option(
			False,
			"--superuser",
			help="Let them administer this installation: create accounts and workspaces.",
		),
		json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
	) -> None:
		"""Add somebody to this instance, and hand them the way in.

		Examples:

		  subroutine user create thomas --name "Thomas Anderson"

		  subroutine user create thomas --terminal

		  subroutine user create thomas --browser --terminal --workspace acme

		  subroutine user create sam --superuser

		One command rather than five. It makes the account, puts them in a workspace with a
		role, and — if you say how they will reach this instance — produces the sign-in link
		or the credential in the same breath. An account with no membership authenticates and
		can see nothing, which reads as a broken token rather than as a missing role.

		'--role' is 'member' unless you say otherwise: enough to do the work, not to
		administer the place. '--workspace' can be left out when there is only one, and is
		asked for when there are several.

		'--browser' and '--terminal' are not alternatives. Somebody who uses the web interface
		and has a colleague setting their machine up needs both, so both may be given. Naming
		neither is fine too — the account is real, and the two commands that hand it over are
		printed.

		'--superuser' is what lets somebody create accounts and workspaces, and it is the only
		way to grant that — no role carries it. It joins no workspace: an instance owner needs
		no workspace role, and granting one quietly would be a permission taken by default.

		There is no password. Subroutine authenticates with tokens, so what a new person needs
		next is one of those, or a link.
		"""

		if superuser and (role.strip() or workspace.strip()):
			program.fail(_a_superuser_joins_nothing())

		if agent and browser:
			program.fail(_an_agent_has_no_browser())

		with program.opened() as world:
			where = world.writing_to()

			# **Resolved before anything is written, and that ordering is not a tidiness.**
			# It is `token create`'s own rule — *checked before anything is issued, so a
			# credential is never minted and then stranded* — and the failure without it is
			# worse here: an ambiguous workspace refused *after* the account exists leaves a
			# person with no membership, and the same command re-run then refuses again
			# because the username is taken. Driven, and it left one behind.
			#
			# **Resolved once and carried**, because the workspace decides two things: where
			# they are a member, and what a credential issued here is pinned to. Asking twice
			# would let a second reading of "which workspace" disagree with the first.
			joining = None if superuser else _onboarding_workspace(where, workspace)

			# Read *before* creating, because the question is how many accounts there were —
			# see `_keep_the_operators_own_list` for why that is the one that matters.
			before = where.client.users() if where.client.connection.is_local else []

			created = where.client.create_user(
				username=username,
				display_name=display_name.strip() or None,
				email=email.strip() or None,
				is_service_account=agent,
				is_superuser=superuser,
			)

			# **Before anything else that needs an actor, and that ordering is the second of
			# `#587`'s two.** Local mode picks an account by there being exactly one (§12.1a),
			# so the account created one line above has just made that ambiguous — and every
			# call after it resolves an operator. Until this command did a second thing, the
			# repair could sit at the end and nothing noticed.
			settled = _keep_the_operators_own_list(world, before)

			joined = (
				None
				if joining is None
				else where.client.add_member(
					username=created.username,
					role=role.strip() or ONBOARDING_ROLE,
					workspace=joining,
				)
			)

			handed = _handed_over(
				where,
				world.settings,
				created.username,
				browser=browser,
				terminal=terminal,
				workspace=joining,
			)

		if json_output:
			program.say(json.dumps(created.model_dump(mode="json"), indent=2))

			return

		program.say(f"Created {created.username}")

		if joined is not None:
			program.say(
				f"{joined.user.username} is now {joined.role} in {joined.workspace.slug}"
			)

		if settled is not None:
			program.say(f"Local commands will go on acting as {settled}.")

		for line in handed:
			program.say(line)

	@user_app.command("list")
	def user_list (
		workspace: str = typer.Option(
			"", "--workspace", help="Show who belongs to this workspace, and their roles."
		),
		json_output: bool = typer.Option(False, "--json", help="Print the list as JSON."),
	) -> None:
		"""Show who is on this instance.

		Examples:

		  subroutine user list

		  subroutine user list --workspace acme

		Without --workspace this is every account, oldest first — the first one is whoever ran
		'subroutine init'. With it, only that workspace's members, and what each may do there.
		"""

		with program.opened() as world:
			where = world.writing_to()
			# Neither of these rows carries a moment, so nothing here reads it; it is passed
			# because `columns` takes it everywhere rather than only where it is used, which
			# is what stops the next cell rendering a day in the server's zone (`#1091`).
			reading = world.account_zone(where.name, None)

			if workspace.strip():
				members = where.client.members(workspace=workspace.strip())
				rows = [member.columns(reading) for member in members]
				payload = [member.model_dump(mode="json") for member in members]

			else:
				accounts = where.client.users()
				rows = [account.columns(reading) for account in accounts]
				payload = [account.model_dump(mode="json") for account in accounts]

			if json_output:
				program.say(json.dumps(payload, indent=2))

				return

			if not rows:
				program.say("Nobody here yet.")
				_suggest(program.console, "subroutine user create thomas")

				return

			for line in _tabulated(rows):
				program.say(line)

	@user_app.command("add")
	def user_add (
		username: str = typer.Argument(..., help="Who, by the name 'user list' shows."),
		role: str = typer.Option(
			"", "--role", help="What they may do there — 'member', 'admin', 'viewer'."
		),
		workspace: str = typer.Option("", "--workspace", help="Which workspace."),
	) -> None:
		"""Let somebody work in a workspace.

		Examples:

		  subroutine user add thomas --role member

		  subroutine user add thomas --role admin --workspace acme

		The role is named rather than assumed. What somebody may do is the decision being
		taken here, and a default would be this command taking it quietly on your behalf.
		"""

		# Through `fail` rather than raised: every other refusal in this module goes that way,
		# and a bare raise here leaves the command's own guard — `opened()` — behind, so the
		# message arrives as an exception rather than as a sentence.
		if not role.strip():
			program.fail(
				subroutine.errors.ValidationError(
					"Say what they may do there, with --role.",
					hint=(
						"'member' to work in it, 'admin' to also manage it, 'viewer' to only "
						"read. A role belongs to a workspace, so these are that workspace's."
					),
				)
			)

		with program.opened() as world:
			where = world.writing_to()
			joined = where.client.add_member(
				username=username,
				role=role.strip(),
				workspace=workspace.strip() or _writing_workspace(world),
			)

			program.say(f"{joined.user.username} is now {joined.role} in {joined.workspace.slug}")

	@user_app.command("role")
	def user_role (
		username: str = typer.Argument(..., help="Who, by the name 'user list' shows."),
		role: str = typer.Argument(
			..., help="What they may do there — 'member', 'admin', 'viewer'."
		),
		workspace: str = typer.Option("", "--workspace", help="Which workspace."),
	) -> None:
		"""Change what somebody who is already there may do.

		Examples:

		  subroutine user role thomas admin

		  subroutine user role thomas viewer --workspace acme

		The role is positional rather than an option, unlike 'user add': there it is one
		decision among several, and here it is the whole of what this command is for.

		Somebody who is not there yet is turned down by name — 'user add' is what puts them in
		a workspace, and this is what moves them once they are.
		"""

		with program.opened() as world:
			where = world.writing_to()
			moved = where.client.set_member_role(
				username=username,
				role=role.strip(),
				workspace=workspace.strip() or _writing_workspace(world),
			)

			# **The role they now hold, not the move.** What it was before is on the event,
			# which is where a change of this kind is read — a line in a terminal is a
			# confirmation rather than the record.
			program.say(f"{moved.user.username} is now {moved.role} in {moved.workspace.slug}")

	@user_app.command("deactivate")
	def user_deactivate (
		username: str = typer.Argument(..., help="Who, by the name 'user list' shows."),
		yes: bool = typer.Option(False, "--yes", help="Do not ask."),
	) -> None:
		"""Mark somebody as having left, stopping the agents that answer to them.

		Examples:

		  subroutine user deactivate thomas

		Their account stays and so does everything they wrote, still attributed to them. What
		stops is their credentials and every agent answerable to them — because somebody gave
		those agents permission to work, and that permission was this person's to give.

		The last person who can administer this instance cannot leave: an instance nobody can
		administer cannot be repaired from inside, and it would stop every agent at once.
		"""

		with program.opened() as world:
			where = world.writing_to()
			stopping = subroutine.views.answering_to(where.client.users(), username)

			# **Named before it happens, not counted** — `project rename`'s rule. A deactivation
			# that silently stops a shared agent is how somebody learns to stop deactivating
			# leavers, which costs more than the thing it was protecting.
			if stopping and not yes:
				program.say(f"This also stops {len(stopping)} agent(s): {', '.join(stopping)}")

				if not typer.confirm(f"Mark {username} as having left?"):
					program.say("Left as they were.")

					return

			where.client.set_active(username=username, active=False)

			program.say(f"{username} is marked as having left")

			for name in stopping:
				program.say(f"  {name} has stopped")

	@user_app.command("reactivate")
	def user_reactivate (
		username: str = typer.Argument(..., help="Who, by the name 'user list' shows."),
	) -> None:
		"""Bring somebody back, and with them the agents that answer to them.

		Examples:

		  subroutine user reactivate thomas

		The same operation as 'deactivate' in reverse, deliberately: two commands with their own
		rules would be two places for those rules to disagree.
		"""

		with program.opened() as world:
			where = world.writing_to()

			where.client.set_active(username=username, active=True)

			program.say(f"{username} is active again")

	@user_app.command("timezone")
	def user_timezone (
		zone: str = typer.Argument(
			"", help="Your zone, e.g. 'Europe/London'. Say nothing to see it."
		),
		clear: bool = typer.Option(
			False, "--clear", help="Follow the workspace's zone again."
		),
	) -> None:
		"""Say which timezone you are in, so your days are counted where you are.

		Examples:

		  subroutine user timezone Europe/London

		  subroutine user timezone

		Your own account and nobody else's — you know which zone you are in better than
		anybody else does, so there is no permission that lets somebody set it for you.

		It decides which day a deadline counts as on every surface. It does not change how a
		date is written down: a day belongs to the item that has it, so 'due Fri 14 Aug' says
		Friday wherever it is read.
		"""

		_user_timezone(program, zone=zone, clear=clear)

	@user_app.command("transfer")
	def user_transfer (
		username: str = typer.Argument(..., help="Which agent, by the name 'user list' shows."),
		to: str = typer.Option(..., "--to", help="Who becomes answerable for it."),
	) -> None:
		"""Hand an agent to somebody else, who becomes answerable for what it does.

		Examples:

		  subroutine user transfer deploy-bot --to jo

		Agents stop when the person answerable for them leaves, so this is how one is kept when
		somebody goes. Only a person can take an agent on — being accountable is something
		somebody agrees to, and an agent cannot agree on anybody's behalf.
		"""

		with program.opened() as world:
			where = world.writing_to()

			where.client.transfer_agent(username=username, to=to)

			program.say(f"{to} now answers for {username}")

	@user_app.command("remove")
	def user_remove (
		username: str = typer.Argument(..., help="Who, by the name 'user list' shows."),
		workspace: str = typer.Option("", "--workspace", help="Which workspace."),
	) -> None:
		"""Take somebody out of a workspace.

		Examples:

		  subroutine user remove thomas

		This removes their membership, not their account: what they wrote stays, and stays
		attributed to them. The last person able to administer a workspace cannot be removed
		from it, because a workspace nobody can administer cannot be repaired from inside.
		"""

		with program.opened() as world:
			where = world.writing_to()
			chosen = workspace.strip() or _writing_workspace(world)

			where.client.remove_member(username=username, workspace=chosen)

			program.say(f"{username} is no longer a member of {chosen}")



def _register_workspace (app: typer.Typer, program: Program) -> None:
	"""Add the ``workspace`` group to the application.

	**Out of ``register`` rather than in it**, which is `#943`'s ratchet doing what it was
	built for: `#704` added two commands here, the closure went over its ceiling, and the
	remedy the ratchet asks for is to move a group out rather than to make room. This one
	was the natural unit — five commands that touch nothing in the closure but ``program``
	and the application itself.
	"""

	workspace_app = typer.Typer(
		help="Look after the spaces work is kept in.", no_args_is_help=True
	)
	app.add_typer(workspace_app, name="workspace")

	# **You could make one and rename one and never see one** (`#1355`). Every other verb here
	# takes a slug, so the listing that tells you the slugs was the gap — and `/v1/meta` and the
	# tools both answered it happily while the terminal had no word for the question.
	@workspace_app.command("list")
	def workspace_list (
		json_output: bool = typer.Option(False, "--json", help="Print the list as JSON."),
	) -> None:
		"""Show the workspaces you can reach, by the name you type and what it is called.

		Examples:

		  subroutine workspace list
		"""

		_workspaces_listed(program, json_output=json_output)

	@workspace_app.command("create")
	def workspace_create (
		slug: str = typer.Argument(..., help="Its short name, used in addresses."),
		title: str = typer.Argument(..., help="What to call it."),
		timezone: str = typer.Option(
			"", "--timezone", help="Its zone, e.g. 'Europe/London'. Unset follows the instance."
		),
	) -> None:
		"""Make another workspace, for work that should be kept apart.

		Examples:

		  subroutine workspace create personal Personal

		  subroutine workspace create acme "Acme Ltd" --timezone Europe/London

		Numbers start again at 1 in a new workspace, so the two do not have to share a
		sequence — and nothing in one is visible from the other unless you are in both.
		"""

		with program.opened() as world:
			where = world.writing_to()
			created = where.client.create_workspace(
				slug=slug, title=title, timezone=timezone.strip() or None
			)

			program.say(f"Created {created.slug} — {created.title}")

			# **Said because it is the surprising part.** Everything reachable is still listed,
			# but a *write* goes to one place (§13.7), so a new workspace is not where the next
			# `add` lands until somebody says so.
			_suggest(program.console, f"subroutine use {created.slug}", "work in it")

	@workspace_app.command("rename")
	def workspace_rename (
		slug: str = typer.Argument(..., help="The workspace to rename, by its short name."),
		to: str = typer.Argument(..., help="The new short name."),
		yes: bool = typer.Option(False, "--yes", help="Do not ask."),
	) -> None:
		"""Give a workspace a different short name.

		Examples:

		  subroutine workspace rename si projects

		Nothing inside moves — every item keeps its number and everything stays joined to what
		it was joined to. What stops working is anything that wrote the old name down.
		"""

		_workspace_renamed(program, slug=slug, to=to, yes=yes)

	@workspace_app.command("update")
	def workspace_update (
		slug: str = typer.Argument(..., help="The workspace, by its short name."),
		title: str = typer.Option(UNGIVEN, "--title", show_default=False, help="What to call it."),
		description: str = typer.Option(
			UNGIVEN, "--description", show_default=False, help="What it is for. Pass '' to clear."
		),
		timezone: str = typer.Option(
			UNGIVEN,
			"--timezone",
			show_default=False,
			help="Its zone, e.g. 'Europe/London'. Pass '' to follow the instance.",
		),
		colour: str = typer.Option(
			UNGIVEN, "--colour", show_default=False, help=WORKSPACE_COLOUR_HELP
		),
		hide_status: list[str] = typer.Option(
			None, "--hide-status", show_default=False, help=WORKSPACE_HIDE_STATUS_HELP
		),
	) -> None:
		"""Change what a workspace is called, what it is for, or which zone its dates are in.

		Examples:

		  subroutine workspace update projects --title Projects

		  subroutine workspace update acme --timezone Europe/London

		The zone is the one that matters: every date in the workspace is read in it, so a
		workspace set up in the wrong one shows every deadline at the wrong time. Clearing it
		follows the instance instead.

		Its short name is not changed here — that breaks addresses, so it has a command of
		its own with a warning attached: 'subroutine workspace rename'.
		"""

		_workspace_updated(
			program,
			slug=slug,
			title=title,
			description=description,
			timezone=timezone,
			colour=colour,
			hide_status=hide_status,
		)

	@workspace_app.command("delete")
	def workspace_delete (
		slug: str = typer.Argument(..., help="The workspace to delete, by its short name."),
		yes: bool = typer.Option(False, "--yes", help="Do not ask."),
	) -> None:
		"""Put a workspace in the trash, with everything filed in it.

		Examples:

		  subroutine workspace delete acme

		Nothing is destroyed: its items keep their numbers and come back exactly as they were
		with 'subroutine workspace restore'. Its short name is freed, so if something else
		takes the name in the meantime the restore will ask you to rename that one first.

		The only workspace here cannot be deleted — make the one that replaces it first.
		"""

		_workspace_deleted(program, slug=slug, yes=yes)

	@workspace_app.command("restore")
	def workspace_restore (
		slug: str = typer.Argument(..., help="The workspace to bring back, by its short name."),
	) -> None:
		"""Take a workspace back out of the trash, and everything in it with it.

		Examples:

		  subroutine workspace restore acme
		"""

		_workspace_restored(program, slug=slug)



def register (
	app: typer.Typer,
	*,
	say: typing.Callable[[str], None],
	fail: typing.Callable[[subroutine.errors.SubroutineError], typing.NoReturn],
	stop: typing.Callable[..., typing.NoReturn],
	settings: typing.Callable[[], subroutine.config.Settings],
	console: rich.console.Console,
	warn: typing.Callable[[str], None],
	mask: typing.Callable[[str], str],
) -> tuple[typing.Callable[[], None], Selected]:
	"""Add the personal commands to the application.

	Returns the bare-invocation callable and the object holding the options that appear
	before a subcommand. The callable is handed back rather than looked up afterwards
	because Typer leaves an ``OptionInfo`` as the default of every option, so calling a
	registered command's function without arguments passes an object where a boolean
	belongs — and ``--json`` would silently be on.
	"""

	selected = Selected()
	program = Program(
		say=say,
		fail=fail,
		stop=stop,
		settings=settings,
		console=console,
		warn=warn,
		mask=mask,
		selected=selected,
	)

	# --- The commands --------------------------------------------------------------------

	@app.command()
	def add (
		words: list[str] = typer.Argument(None, help="What you need to do."),
		kind: str = typer.Option("", "--type", help=TASK_TYPES_WITH_DEFAULT),
		description: str = typer.Option(
			"", "--description", help="What it is about, in full. The title stays one line."
		),
		under: int = typer.Option(
			None, "--under", help="File it underneath this item, by number."
		),
		repeat: str = typer.Option(
			"", "--repeat", help="How often it comes round, like 'every other tuesday'."
		),
		repeat_from: str = typer.Option(
			"",
			"--repeat-from",
			help="Measure the next one from 'schedule' or from 'completion'. Defaults to "
			"schedule.",
		),
		json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
	) -> None:
		"""Add something to your list.

		Examples:

		  subroutine add "Call the dentist before Sunday"

		  subroutine add "Write the report by friday !3 ~2h #work"

		  subroutine add "Dates render as if this year" --type bug

		  subroutine add "Cache the roster" --description "Measured at 400ms a call."

		  subroutine add "Pay the rent" --repeat "every month on the 30th"

		  subroutine add "Water the plants tomorrow" --repeat "every 3 days" --repeat-from completion

		'--description' is where the reasoning goes, so the title can say what will be true
		when the work is done rather than what is wrong today. A title stating a condition
		becomes false when the condition changes; one stating an outcome cannot.

		'--repeat-from schedule' keeps the rhythm whatever you do — rent is due on the 30th
		whether or not last month's was paid late. '--repeat-from completion' measures from
		when you finished, which is what "every three days" means about watering.
		"""

		text = " ".join(words or [])

		if not text.strip():
			# A required-argument error is a dead end where a question would do (§12.2a) — but
			# the question goes to **stderr**, because `typer.prompt` echoes to stdout by
			# default and `add --json` then emitted `What do you need to do?: {…}`, which is not
			# JSON. The scripted path is the agent's path, and `topics.py` advertises it.
			text = typer.prompt("What do you need to do?", err=True)

		with program.opened() as world:
			where = world.writing_to()
			filed = _default_project(program, world, text)
			# **A flag rather than a sigil** (`#178`). §6.13's sigils are for things somebody
			# types mid-sentence; "this is a bug" is a classification *about* the sentence
			# rather than part of it — which is the argument `client.capture` already makes for
			# taking it separately. HTTP and MCP have accepted it since they were written, and
			# only the CLI made a person file everything as a task and correct it afterwards.
			captured = where.client.capture(
				text=text,
				workspace=_writing_workspace(world),
				project=filed,
				type=kind.strip() or None,
				# **The same argument as `--type`, and it was missing for the same reason**
				# (`#424`): the grammar cannot carry it, so nothing that reads the line could
				# supply it, and the endpoint's ability to take one beside `text` went
				# unreachable from every client. Reported by an agent asked why the six items
				# it had just filed had no descriptions.
				description=description.strip() or None,
			# **The same word `move` uses, for the same act** (`#510`). `POST /v1/tasks`
			# has taken a parent since M1 and no client passed one, so breaking a piece of
			# work into parts — the first step of handing any of it over — needed raw HTTP.
			# **Not a sigil**: §6.13's line is deliberately small and a parent is a
			# statement about where this item sits rather than part of the sentence.
			parent=under,
				# **Set precisely, rather than only read out of a sentence** (`#94`, Simon's
				# direction of 2026-08-16). The grammar reads *"every 14 days"* out of a
				# captured line, which is the fast path and stays the fast path — but a line
				# can only ever *create* a repeat, and the words for one it cannot read are
				# simply left in the title. A flag is how somebody says exactly what they
				# mean, and it is the same argument `--type` already makes.
				recurrence=repeat.strip() or None,
				recurrence_anchor=repeat_from.strip() or None,
				# **No zone is sent, so §6.5's chain decides** — decision `#1088`: a day is
				# resolved in the **account's** zone, never the machine's. This used to fill
				# the chain's `explicit` slot — its *top* — with this machine's OS zone, which
				# is `#1014`'s defect and the reason reading and writing came apart (`#1001`).
				# Two accounts in different zones now genuinely file different Fridays;
				# `_report_zones` says so, and `#1089` is `whoami` saying it here.
				# **No `--repeat-trigger`, deliberately** (`#94`). `time` is refused by name
				# until `#916` expands a rule into a date-ranged view, so the flag would offer
				# one accepted value and one that always fails — a control with nothing to
				# decide, which is this codebase's second signature defect. It arrives with
				# the calendar, when there is a second answer for it to carry.
			)

			if json_output:
				# `unparsed` is carried on the scripted path too. §6.13 requires the caller to
				# be told what the grammar declined to read, and the human was while the agent
				# was not — which is backwards, since the agent is the caller most likely to
				# have written something it believes was understood.
				body = _as_json(world, where.name, captured.task)
				body["unparsed"] = list(captured.unparsed)
				body["read"] = captured.summary

				say(json.dumps(body, indent=2))

				return

			# **What was read, beside what it became** (`#135`). The date is already rendered
			# in human form by `_when` — "(due Sun 2 Aug)" is a better confirmation than
			# echoing "by friday" back, because the useful part is which day that turned out
			# to be. Everything else is written back as the sigil that was typed, which needs
			# no vocabulary and is what somebody would type again.
			# Wrapped by `capture.read_back` rather than here, so this surface and the MCP
			# adapter cannot come to render it differently — the same argument the `unparsed`
			# half already runs on, and the half that had drifted (`#426`).
			echoed = subroutine.domain.capture.read_back(captured.summary)
			read = "" if echoed is None else f"  {echoed}"

			# **Where it landed** (`#279`). Every other acting command says this through
			# `_acted`; `add` said only the title, so a write that went to the wrong
			# connection — the `#273` hazard, which happened twice in one afternoon — read
			# exactly like one that went to the right place. It is the *creating* command,
			# so it is the one where a silent misfile leaves a row nobody goes back for.
			#
			# Deliberately not louder than that. `#135` settled that an ordinary "Buy milk"
			# is owed no report of machinery it did not ask for, and `_acted`'s guard already
			# draws the line in the right place: a misfile needs somewhere else to file to.
			landed = Located(
				connection=where.name,
				workspace=_writing_workspace(world),
				item=captured.task,
			)

			say(f"{_acted(world, landed, 'Added')}{_when(captured.task)}{read}")

			# **Said out loud, every time, because nobody typed it** (§13.7a, `#159`). A file
			# three directories up that silently redirects where work is filed is the footgun
			# `context.py` calls the standing one in comparable tooling — not having a setting,
			# but not knowing where it came from. One line is the whole cost of not having it.
			if filed is not None:
				console.print(rich.text.Text(f"  in {filed}, from {FILE_NAME}", style=DETAIL))

			# The sentence itself is `domain.capture.explain`'s, so this surface and the MCP
			# adapter cannot come to word §6.13's obligation differently.
			left = subroutine.domain.capture.explain(captured.unparsed)

			if left is not None:
				console.print(rich.text.Text(f"  {left}", style=DETAIL))

			_suggest(console, "subroutine agenda")

	@app.command("agenda")
	def agenda (
		when: str = typer.Argument(
			"", help="A day — 'tomorrow', 'friday', '+2w', '2026-08-01'. Default today."
		),
		days: int = typer.Option(
			subroutine.domain.agenda.DEFAULT_HORIZON_DAYS,
			"--days",
			min=1,
			help="How far ahead the look-ahead section reaches.",
		),
		project: str = typer.Option(
			"", "--project", help="One project and everything under it. Needs -w."
		),
		json_output: bool = typer.Option(False, "--json", help="Print the agenda as JSON."),
		strict: bool = typer.Option(
			False, "--strict", help="Stop if any connection cannot be reached."
		),
	) -> None:
		"""Show what you are doing today, or what another day looks like.

		Examples:

		  subroutine agenda

		  subroutine agenda tomorrow

		  subroutine agenda saturday --days 2

		  subroutine -w work agenda

		  subroutine -w work agenda --project acme

		A named day is shown as it stands now, so anything already late appears under
		Overdue whether or not it was late on that day.
		"""

		_agenda(
			program,
			json_output=json_output,
			strict=strict,
			workspace=selected.workspace,
			when=when,
			days=days,
			project=project,
		)

	@app.command("today", hidden=True)
	def today_moved () -> None:
		"""Say where this went. It is not an alias and does not print an agenda.

		'subroutine agenda' is the command now.
		"""

		# **Not the synonym `#996` shipped**, and Simon reversed that within the afternoon
		# (`#1003`). It kept `today` on `ls`/`list`'s precedent — nothing anybody has typed
		# stops working — and the argument does not transfer: `ls` is a convenience nobody
		# ever had to unlearn, where this was the *former primary name* for the thing `#990`
		# was about unifying. Two names for one answer is the condition that milestone exists
		# to remove.
		#
		# **A signpost rather than a bare removal**, which is `#509`'s shape and its recorded
		# rule: this refuses, which is what a removed command should do. Measured before
		# choosing it — Typer offers a near-miss where it can find one, and with this gone
		# there is none, so a bare removal answers `No such command 'today'.` and nothing
		# else. Honest, and useless to somebody with it in their shell history.
		program.say("'subroutine today' is now 'subroutine agenda'.")
		program.say("")
		program.say("Every surface calls it an agenda — the page, the API and the tools an")
		program.say("agent uses — so the command does too.")

		raise typer.Exit(2)

	class _Listing(typer.core.TyperCommand):
		"""``list``, with its catch-all argument kept out of the usage line.

		The argument exists only to intercept ``subroutine list some words`` and point at
		``search`` (`#282`). Click renders a positional in the usage line whether or not it is
		``hidden``, so without this the help reads ``list [OPTIONS] [words]...`` — advertising
		the very thing the argument refuses, which would trade one confusion for a worse one.
		A ``metavar=""`` alone leaves ``[]`` behind, hence filtering rather than naming.
		"""

		def collect_usage_pieces (self, ctx: typing.Any) -> list[str]:
			"""Return the usage pieces, dropping the placeholder left by a hidden argument."""

			return [
				piece for piece in super().collect_usage_pieces(ctx) if piece.strip("[]. ")
			]

	# **Registered twice, and `list` is the one the help shows.** Simon's preference, and the
	# right way round: a real word teaches itself, where `ls` only reads as "list" to somebody
	# who already knows Unix — which is not the audience §1.4 is written for. `ls` keeps
	# working because it is in muscle memory and in every note anybody has written, and is
	# hidden rather than removed: a synonym in the help is a second thing to choose between.
	@app.command("list", cls=_Listing)
	def list_items (
		limit: int = typer.Option(DEFAULT_LIST_LIMIT, "--limit", help="How many to show."),
		json_output: bool = typer.Option(False, "--json", help="Print the list as JSON."),
		merged: bool = typer.Option(
			False, "--merged", help="One list rather than a group per connection."
		),
		strict: bool = typer.Option(
			False, "--strict", help="Stop if any connection cannot be reached."
		),
		order: str = typer.Option(
			"", "--order", help="Sort by, e.g. '-priority_score' or 'due_at,-importance'."
		),
		project: list[str] | None = PROJECT_OPTION,
		connection: str = typer.Option(
			"", "--connection", help="Only this connection, by name."
		),
		deferred: bool = typer.Option(
			False, "--deferred", help="Include things you have put off until a later date."
		),
		ready: bool = typer.Option(
			False,
			"--ready",
			help="Only what you could start now — nothing unfinished blocks it.",
		),
		trash: bool = typer.Option(
			False, "--trash", help="Show what you have deleted, instead of the list."
		),
		assignee: list[str] | None = ASSIGNEE_OPTION,
		claimed_by: list[str] | None = CLAIMED_BY_OPTION,
		status: list[str] | None = STATUS_OPTION,
		kind: list[str] | None = TYPE_OPTION,
		tag: list[str] | None = TAG_OPTION,
		dated: list[str] | None = typer.Option(
			None,
			"--filter",
			help="Narrow by date, e.g. 'created_at.gte=yesterday'. Repeat for a range.",
		),
		words: list[str] | None = typer.Argument(
			None, hidden=True, metavar="", help="Not a filter — see 'subroutine search'."
		),
		looking_for: str = typer.Option(
			"", "-q", "--search", hidden=True, help="Not a filter — see 'subroutine search'."
		),
	) -> None:
		"""List everything still open — tasks and documents — newest first.

		Examples:

		  subroutine list

		  subroutine list --limit 10

		  subroutine list --order -priority_score

		  subroutine list --project SR --order due_at

		  subroutine list --assignee si --status blocked

		  subroutine list --claimed-by claude --order claimed_at

		  subroutine list --filter created_at.gte=yesterday

		  subroutine list --filter completed_at.gte=2026-08-02 --filter completed_at.lt=today
		"""

		_refuse_words(program, words, looking_for)

		_shown_list(
			program, limit=limit, json_output=json_output, merged=merged, strict=strict,
			order=order, project=project, connection=connection, deferred=deferred,
			ready=ready, trash=trash, assignee=assignee, claimed_by=claimed_by,
			status=status, kind=kind, tag=tag, dated=dated,
		)

	@app.command()
	def search (
		terms: str = typer.Argument("", help="What to look for."),
		limit: int = typer.Option(DEFAULT_LIST_LIMIT, "--limit", help="How many to show."),
		json_output: bool = typer.Option(False, "--json", help="Print the results as JSON."),
		merged: bool = typer.Option(
			False, "--merged", help="One list rather than a group per connection."
		),
		strict: bool = typer.Option(
			False, "--strict", help="Stop if any connection cannot be reached."
		),
		order: str = typer.Option(
			"", "--order", help="Sort by, e.g. '-priority_score' or 'due_at,-importance'."
		),
		project: list[str] | None = PROJECT_OPTION,
		tag: list[str] | None = TAG_OPTION,
		connection: str = typer.Option(
			"", "--connection", help="Only this connection, by name."
		),
		deferred: bool = typer.Option(
			False, "--deferred", help="Include things you have put off until a later date."
		),
		dated: list[str] | None = typer.Option(
			None,
			"--filter",
			help="Narrow by date, e.g. 'created_at.gte=yesterday'. Repeat for a range.",
		),
	) -> None:
		"""Find things by any of their words, wherever they were written.

		Searches tasks and documents together, like 'subroutine list', because one number
		names either and a search that found only half of them would be lying about the rest.

		A tag is found by writing it as you wrote it. Quote it, or the shell reads the '#'
		as the start of a comment. That finds what carries the tag and anything that
		mentions it in writing; '--tag' is the narrower question and answers with
		everything carrying it and nothing else.

		Examples:

		  subroutine search "dentist"

		  subroutine search "#errand"

		  subroutine search "pagination" --project SR

		  subroutine search "boiler" --filter created_at.gte=yesterday
		"""

		_searched(
			program,
			terms=terms,
			limit=limit,
			json_output=json_output,
			merged=merged,
			strict=strict,
			order=order,
			project=project,
			tag=tag,
			connection=connection,
			deferred=deferred,
			dated=dated,
		)

	@app.command()
	def journal (
		dated: list[str] | None = typer.Option(
			None,
			"--filter",
			help="Which period, e.g. 'created_at.gte=yesterday'. Repeat for a range.",
		),
		by: str = typer.Option(
			"", "--by", help="Only what one account did, by name."
		),
		mine: bool = typer.Option(
			False, "--mine", help="Only what this machine's own credential did."
		),
		oldest: bool = typer.Option(
			False, "--oldest", help="Read the period forwards, in the order it happened."
		),
		limit: int = typer.Option(DEFAULT_LIST_LIMIT, "--limit", help="How many to show."),
		json_output: bool = typer.Option(False, "--json", help="Print the entries as JSON."),
		strict: bool = typer.Option(
			False, "--strict", help="Stop if any connection cannot be reached."
		),
	) -> None:
		"""What happened over a period, with who did it and what they said.

		'subroutine changes' says what *moved* and is what you resume from a number. This says
		what *happened* — the same events, with the comments people wrote, the names of who
		did each thing, and what a change moved between rather than which rows it touched.

		Ask it for a period. It is the question to ask when somebody wants writing up.

		Examples:

		  subroutine journal --filter created_at.gte=yesterday

		  subroutine journal --filter created_at.gte=2026-08-28 --filter created_at.lt=2026-08-29

		  subroutine journal --by claude --filter created_at.gte=start_of_week

		  subroutine journal --oldest --filter created_at.gte=today
		"""

		_read_journal(
			program,
			dated=dated,
			by=by,
			mine=mine,
			oldest=oldest,
			limit=limit,
			json_output=json_output,
			strict=strict,
		)

	@app.command()
	def changes (
		since: int | None = typer.Option(
			None, "--since", help="Carry on from this number, printed by the last run."
		),
		mine: bool = typer.Option(
			False, "--mine", help="Only what this machine's own credential did."
		),
		by: str = typer.Option(
			"", "--by", help="Only what one account did, by name. Try it with an agent's name."
		),
		dated: list[str] | None = typer.Option(
			None,
			"--filter",
			help="Narrow to a period, e.g. 'created_at.gte=yesterday'. Repeat for a range.",
		),
		limit: int = typer.Option(DEFAULT_LIST_LIMIT, "--limit", help="How many to show."),
		json_output: bool = typer.Option(False, "--json", help="Print the events as JSON."),
		strict: bool = typer.Option(
			False, "--strict", help="Stop if any connection cannot be reached."
		),
	) -> None:
		"""What has changed, oldest first — the question to ask after time away.

		'subroutine list' says what is open now. This says what *moved*, which is the thing
		you cannot work out by looking at the current state.

		'--by' is how you find out what somebody else has been doing, which is usually an
		agent you handed work to. '--mine' is the same question about this machine.

		'--since' resumes where you left off; '--filter' asks about a period. They are
		different questions — you have no number to offer for 'what happened yesterday'.

		Examples:

		  subroutine changes

		  subroutine changes --since 412

		  subroutine changes --filter created_at.gte=yesterday

		  subroutine changes --mine

		  subroutine changes --by claude
		"""

		_what_moved(
			program,
			since=since,
			mine=mine,
			by=by,
			dated=dated,
			limit=limit,
			json_output=json_output,
			strict=strict,
		)

	@app.command("ls", hidden=True, cls=_Listing)
	def list_tasks (
		limit: int = typer.Option(DEFAULT_LIST_LIMIT, "--limit", help="How many to show."),
		json_output: bool = typer.Option(False, "--json", help="Print the list as JSON."),
		merged: bool = typer.Option(
			False, "--merged", help="One list rather than a group per connection."
		),
		strict: bool = typer.Option(
			False, "--strict", help="Stop if any connection cannot be reached."
		),
		order: str = typer.Option(
			"", "--order", help="Sort by, e.g. '-priority_score' or 'due_at,-importance'."
		),
		project: list[str] | None = PROJECT_OPTION,
		connection: str = typer.Option(
			"", "--connection", help="Only this connection, by name."
		),
		deferred: bool = typer.Option(
			False, "--deferred", help="Include things you have put off until a later date."
		),
		ready: bool = typer.Option(
			False,
			"--ready",
			help="Only what you could start now — nothing unfinished blocks it.",
		),
		trash: bool = typer.Option(
			False, "--trash", help="Show what you have deleted, instead of the list."
		),
		assignee: list[str] | None = ASSIGNEE_OPTION,
		claimed_by: list[str] | None = CLAIMED_BY_OPTION,
		status: list[str] | None = STATUS_OPTION,
		kind: list[str] | None = TYPE_OPTION,
		tag: list[str] | None = TAG_OPTION,
		dated: list[str] | None = typer.Option(
			None,
			"--filter",
			help="Narrow by date, e.g. 'created_at.gte=yesterday'. Repeat for a range.",
		),
		words: list[str] | None = typer.Argument(
			None, hidden=True, metavar="", help="Not a filter — see 'subroutine search'."
		),
		looking_for: str = typer.Option(
			"", "-q", "--search", hidden=True, help="Not a filter — see 'subroutine search'."
		),
	) -> None:
		"""The short name for 'subroutine list'. Both do the same thing.

		Examples:

		  subroutine ls
		"""

		_refuse_words(program, words, looking_for)

		_shown_list(
			program, limit=limit, json_output=json_output, merged=merged, strict=strict,
			order=order, project=project, connection=connection, deferred=deferred,
			ready=ready, trash=trash, assignee=assignee, claimed_by=claimed_by,
			status=status, kind=kind, tag=tag, dated=dated,
		)

	@app.command()
	def show (
		which: str = typer.Argument("", help="An item number, as shown by 'subroutine list'."),
		history: bool = typer.Option(False, "--history", help="Every change, newest first."),
		tree: bool = typer.Option(
			False, "--tree", help="What has to happen first, all the way down."
		),
		json_output: bool = typer.Option(False, "--json", help="Print as JSON."),
	) -> None:
		"""Read one item — what it is, what it is joined to, and what happened to it.

		Works on a task or on a document, because one counter per workspace serves both and
		a number on a command line does not say which it is.

		Examples:

		  subroutine show 42

		  subroutine show 42 --json

		  subroutine show 42 --history

		  subroutine show 42 --tree

		'--tree' walks what has to happen before this can, indented by how deep it sits. On a
		milestone that is its contents, since a milestone is an item whose blockers are its
		parts — so it is how you read a plan without opening every item in it.
		"""

		# One address resolved in one context, so there is nothing to combine (`#327`).
		with program.opened() as world:
			located = _locate(program,
				world,
				_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
				kinds=ANY_ITEM,
				verb="show",
			)
			client = _matching(world.clients, located.connection)

			gathered = _sections(client, located, history=history)

			_shown_item(
				program,
				world,
				located,
				gathered,
				client=client,
				tree=tree,
				json_output=json_output,
			)

	# **Named `start_item`/`stop_item`, not `start`/`stop`.** `stop` is the refusal helper this
	# whole function is handed, and `def stop` inside it rebinds that name for the entire
	# enclosing scope — so every refusal in every command registered here would have called the
	# command instead. `mypy --strict` caught it; nothing at runtime would have, because the
	# paths that call `stop()` are the ones nobody exercises on a good day.
	@app.command("claim", hidden=not _worth_showing(settings))
	def claim_item (
		which: str = typer.Argument("", help="A task number, as shown by 'subroutine list'."),
		minutes: int = typer.Option(
			0,
			"--minutes",
			show_default=False,
			help="How long to hold it. Defaults to this instance's setting.",
		),
	) -> None:
		"""Take something, so nobody else starts it too.

		Examples:

		  subroutine claim 42

		  subroutine release 42

		For when more than one person or agent works from the same list. A claim expires on its
		own, so nothing is stranded if whoever took it never comes back — say it again to hold
		it for longer.

		Work somebody else has claimed disappears from 'subroutine list --ready' until their
		claim runs out. Your own never disappears from your own.
		"""

		_claimed(program, which=which, minutes=minutes)

	@app.command("release", hidden=not _worth_showing(settings))
	def release_item (
		which: str = typer.Argument("", help="A task number, as shown by 'subroutine list'."),
		everything: bool = typer.Option(
			False, "--all", help="Give back everything you are holding, wherever it is."
		),
	) -> None:
		"""Put something back, so somebody else can pick it up.

		Examples:

		  subroutine release 42

		  subroutine release --all

		Releasing something nobody had claimed is not an error, so this is safe to run when you
		are not sure. Anybody who can change the task can release it — which is what makes an
		agent that died mid-task somebody else's problem to solve rather than nobody's.

		'--all' is what a session-end hook runs. It says nothing when there is nothing to give
		back, which is the ordinary case once work has been finished.
		"""

		_release_asked(program, which=which, everything=everything)

	@app.command("verify", hidden=not _worth_showing(settings))
	def verify_item (
		which: str = typer.Argument("", help="A task number, as shown by 'subroutine list'."),
		summary: str = typer.Option("", "--summary", help="What was run, in one line."),
		failed: bool = typer.Option(
			False, "--failed", help="Record a check that did not pass."
		),
		tree: str = typer.Option(
			"", "--tree", help="The tree it ran against. Read from git here when omitted."
		),
		commit: str = typer.Option("", "--commit", help="The commit it ran against."),
	) -> None:
		"""Record what you checked against something, so the next person can see it.

		Examples:

		  subroutine verify 42 --summary "5,610 passed, 41 skipped"

		  subroutine verify 42 --failed --summary "3 failed in test_agenda"

		This is a record, not a proof — anybody can say a check passed without running one.
		What it is worth is being kept, attributed, and able to go out of date: it carries the
		state of the code it ran against, so somebody reading it later can tell whether the
		code has moved since.

		In a git checkout the tree is read from git unless you name one. Outside one there is
		nothing to read, and the record is kept without it — it simply cannot go out of date.
		"""

		_verified(
			program,
			which=which,
			summary=summary,
			passed=not failed,
			tree=tree or _tree_here(),
			commit=commit or _commit_here(),
		)

	@app.command("start")
	def start_item (
		which: str = typer.Argument("", help="A task number, as shown by 'subroutine list'."),
	) -> None:
		"""Say you have started something.

		Examples:

		  subroutine start 42

		  subroutine stop 42

		A person could finish work and put work off and never say they were doing it. The one
		state that answers "what am I in the middle of" was reachable only over the API.
		"""

		_moved_to(program, which, "in_progress", verb="start", said="Started")

	@app.command("stop")
	def stop_item (
		which: str = typer.Argument("", help="A task number, as shown by 'subroutine list'."),
	) -> None:
		"""Say you have put something down again, without finishing it.

		Examples:

		  subroutine stop 42

		A state you can enter and not leave is worse than no state, which is why this exists
		beside 'start' rather than after somebody has asked for it. Picking something up and
		putting it down is ordinary; having to finish it to stop showing as busy is not.
		"""

		_moved_to(program, which, "todo", verb="stop", said="Stopped")

	@app.command()
	def done (
		which: str = typer.Argument("", help="A task number, as shown by 'subroutine list'."),
		because: str = typer.Option("", "--because", help="Why, recorded against it."),
	) -> None:
		"""Tick something off.

		Examples:

		  subroutine done 42

		  subroutine done 42 --because "superseded by #99"
		"""

		_finished(program, which=which, because=because)

	@app.command()
	def skip (
		which: str = typer.Argument("", help="A task number, as shown by 'subroutine list'."),
		because: str = typer.Option("", "--because", help="Why, recorded against it."),
	) -> None:
		"""Let one of a repeating task go by, and bring the next.

		Examples:

		  subroutine skip 42

		  subroutine skip 42 --because "away that week"
		"""

		with program.opened() as world:
			located, task = _a_task(program,
				world,
				_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
				verb="skip",
			)
			client = _require_connection(program, world, located.connection)
			skipped = client.skip(ref=task.ref, workspace=located.workspace)

			_because(client, located, because, what="Skipped")

			say(_acted(world, dataclasses.replace(located, item=skipped), "Skipped"))
			_suggest(console, "subroutine agenda")

	@app.command()
	def plan (
		which: str = typer.Argument("", help="A task number, as shown by 'subroutine list'."),
		when: str = typer.Argument(UNGIVEN, help=PLANNED_DAY, show_default=False),
		until: str = typer.Option(
			UNGIVEN,
			"--until",
			show_default=False,
			help="The last day of it, if it lasts more than one. Pass '' to clear it.",
		),
		just_this_one: bool = JUST_THIS_ONE_OPTION,
		from_now_on: bool = FROM_NOW_ON_OPTION,
		because: str = typer.Option("", "--because", help="Why, recorded against it."),
	) -> None:
		"""Say which day you will do something.

		'--until' is for something that lasts — a holiday, a conference, a code freeze.

		Examples:

		  subroutine plan 1 tomorrow

		  subroutine plan 7 "14 august" --until "28 august"

		  subroutine plan 42 friday --because "the review is on monday"
		"""

		_planned(
			program,
			which=which,
			when=when,
			until=until,
			because=because,
			just_this_one=just_this_one,
			from_now_on=from_now_on,
		)

	@app.command()
	def defer (
		which: str = typer.Argument("", help="A task number, as shown by 'subroutine list'."),
		when: str = typer.Argument("", help="A day to hide it until, or a day and a time."),
		just_this_one: bool = JUST_THIS_ONE_OPTION,
		from_now_on: bool = FROM_NOW_ON_OPTION,
		because: str = typer.Option(
			"", "--because", help="What you are waiting for, recorded against it."
		),
	) -> None:
		"""Hide something until later.

		A day on its own hides it until that morning. Write a time as well and it comes back
		at that time — your agenda still waits for the day to turn.

		Examples:

		  subroutine defer 1 monday

		  subroutine defer 7 "2026-08-18 06:00"

		  subroutine defer 42 2026-09-01 --because "waiting on the provider's reply"
		"""

		_hidden(
			program,
			which=which,
			when=when,
			because=because,
			just_this_one=just_this_one,
			from_now_on=from_now_on,
		)

	@app.command()
	def move (
		which: str = typer.Argument("", help="A number, as shown by 'subroutine list'."),
		under: str = typer.Option(
			"", "--under", help="The number of the item this becomes a part of."
		),
		top: bool = typer.Option(False, "--top", help="Make it a top-level item instead."),
	) -> None:
		"""Make something part of another item, or a top-level item again.

		Examples:

		  subroutine move 42 --under 7

		  subroutine move 42 --top
		"""

		_moved_under(program, which=which, under=under, top=top)

	@app.command()
	def update (
		which: str = typer.Argument("", help="A task number, as shown by 'subroutine list'."),
		title: str = typer.Option("", "--title", help="What it is called."),
		# **`show_default=False` on every sentinel-defaulted option** (`#170`). Typer prints a
		# default it was not asked to hide, so `--importance` advertised `[default: -1]` beside
		# the words "1-5" — which invites `--importance -1` and answers "Nothing to change." —
		# and the string sentinels printed their own escape character as `[default:  not
		# given]`. A sentinel exists so that "not given" and "given this" can be told apart;
		# publishing it makes it look like a value somebody may pass.
		description: str = typer.Option(
			UNGIVEN,
			"--description",
			show_default=False,
			help="What it is about. Pass '' to clear it.",
		),
		importance: int = typer.Option(
			UNGIVEN_NUMBER,
			"--importance",
			show_default=False,
			help="How much it matters, 1-5.",
		),
		urgency: int = typer.Option(
			UNGIVEN_NUMBER, "--urgency", show_default=False, help="How soon, 1-5."
		),
		estimate: str = typer.Option(
			UNGIVEN,
			"--estimate",
			show_default=False,
			help="How long, like '2h' or '90m'. Pass '' to clear it.",
		),
		remind: str = typer.Option(
			UNGIVEN,
			"--remind",
			show_default=False,
			help="How long before, like '2w' or '1h'. Pass '' to clear it.",
		),
		kind: str = typer.Option("", "--type", help=TASK_TYPES),
		status: str = typer.Option("", "--status", help="A status, like 'blocked'."),
		project: str = typer.Option("", "--project", help="File it under this project, by key."),
		assignee: str = typer.Option(
			UNGIVEN,
			"--assignee",
			show_default=False,
			help="Who is to do it, by username. Pass '' to leave it with nobody.",
		),
		tags: str = typer.Option(
			UNGIVEN,
			"--tags",
			show_default=False,
			help="Replace its tags, comma-separated. Pass '' to remove them all.",
		),
		due: str = typer.Option(
			UNGIVEN,
			"--due",
			show_default=False,
			help="When it is due, like 'friday' or '2026-08-20'. Pass '' to clear it.",
		),
		timezone: str = typer.Option(
			UNGIVEN, "--timezone", show_default=False, help="The zone the deadline is read in."
		),
		repeat: str = typer.Option(
			UNGIVEN,
			"--repeat",
			show_default=False,
			help="How often it comes round. Pass '' to stop it repeating.",
		),
		repeat_from: str = typer.Option(
			UNGIVEN,
			"--repeat-from",
			show_default=False,
			help="Measure the next one from 'schedule' or from 'completion'.",
		),
		just_this_one: bool = JUST_THIS_ONE_OPTION,
		from_now_on: bool = FROM_NOW_ON_OPTION,
		expected_version: int = typer.Option(
			UNGIVEN_NUMBER,
			"--expected-version",
			show_default=False,
			help="Refuse the change if the task has moved on. See 'show --json'.",
		),
		because: str = typer.Option("", "--because", help="Why, recorded against it."),
		json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
	) -> None:
		"""Change what a task says about itself.

		Everything you do not name is left alone.

		Examples:

		  subroutine update 42 --importance 4 --urgency 3

		  subroutine update 42 --estimate 2h --type bug

		  subroutine update 42 --assignee jo --due friday

		  subroutine update 42 --title "Fix the parser, not the tokeniser"

		  subroutine update 42 --repeat "every other tuesday"

		  subroutine update 42 --repeat ""

		A repeat belongs to the series rather than to the one in front of you, so changing it
		changes every occurrence after this one. '--repeat ""' stops it: the work in hand keeps
		its number and its history, and nothing follows it.

		'--expected-version' turns the change down if somebody has saved since. The number comes
		from 'subroutine show 42 --json', which is where a script reads it; the plain command
		does not print it, because a version is machinery rather than something you set. Without
		it the last save wins and the other person's edit goes with no record that it happened.
		"""

		changes = _named_changes(
			program,
			title=title,
			description=description,
			estimate=estimate,
			remind=remind,
			importance=importance,
			urgency=urgency,
			kind=kind,
			status=status,
			assignee=assignee,
			tags=tags,
			due=due,
			timezone=timezone,
			repeat=repeat,
			repeat_from=repeat_from,
			project=project,
		)
		_changed(
			program,
			which=which,
			changes=changes,
			because=because,
			as_json=json_output,
			just_this_one=just_this_one,
			from_now_on=from_now_on,
			expected_version=(
				None if expected_version == UNGIVEN_NUMBER else expected_version
			),
		)

	@app.command()
	def comment (
		which: str = typer.Argument("", help="An item number, as shown by 'subroutine list'."),
		body: str = typer.Argument("", help="What happened."),
	) -> None:
		"""Record what happened against an item.

		A comment is what you *did*; a document is what you concluded. If the next session
		would need to read it, write it down properly instead.

		Examples:

		  subroutine comment 42 "ran the suite, two failures in the date parser"
		"""

		with program.opened() as world:
			located = _locate(program,
				world,
				_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
				kinds=ANY_ITEM,
				verb="comment",
			)
			client = _require_connection(program, world, located.connection)

			client.remark(
				ref=located.ref,
				body=_asked(body, "What happened?"),
				entity_type=located.entity_type,
				workspace=located.workspace,
			)

			say(_acted(world, located, "Noted on"))
			_suggest(
				console,
				f"subroutine show {world.address_of_located(located).replace(subroutine.domain.refs.SIGIL, '')}",
			)

	@app.command("uncomment")
	def withdraw_comment (
		which: str = typer.Argument("", help="An item number, as shown by 'subroutine list'."),
		words: str = typer.Argument("", help="Words from the comment you want taken out."),
	) -> None:
		"""Take a comment back out of an item's record.

		Examples:

		  subroutine uncomment 42 "two failures in the date parser"

		Named by what it says, because that is what you are looking at. A comment has no
		number of its own and its id is a UUID that appears in nothing a person reads — so
		asking for one would make this a command only a script could run.

		Matching more than one is refused rather than guessed at: say more of the sentence.
		Deleting rather than editing is deliberate. A comment is attributed prose, and
		rewriting somebody's words under their name is not a thing to be able to do.
		"""

		_withdrawn(program, which=which, words=words)

	_register_documents(app, program)

	_register_links(app, program)

	@app.command("delete")
	def discard_item (
		which: str = typer.Argument("", help="Which one, by its number."),
	) -> None:
		"""Take something off the list that should not have been on it.

		Examples:

		  subroutine delete 42

		It goes to the trash rather than vanishing, so it can be put back — the wrong number
		is the commonest mistake anybody makes here, and the second commonest is making it
		twice.
		"""

		with program.opened() as world:
			_discarded(program, world, which=_asked(which, "Which one?"))

	@app.command("restore")
	def undiscard_item (
		which: str = typer.Argument("", help="Which one, by its number."),
	) -> None:
		"""Put something back that was deleted.

		Examples:

		  subroutine restore 42

		  subroutine list --trash
		"""

		with program.opened() as world:
			located = _locate(program, world, _asked(which, "Which one?"), kinds=ANY_ITEM, verb="restore")
			where = world.writing_to()

			back = where.client.undiscard(
				ref=located.ref,
				entity_type=located.entity_type,
				workspace=located.workspace,
			)

			say(_acted(world, dataclasses.replace(located, item=back), "Restored"))
			_suggest(console, "subroutine agenda")

	_register_projects(app, program)

	_register_setup(app, program)
	_register_users(app, program)

	# **Hidden until there is something to choose between** (§1.4). `use` and `connections`
	# are the full model's vocabulary — a workspace, an instance — and somebody with one
	# database and one workspace has no use for either. Both stay fully documented, fully
	# callable and fully discoverable through `--help` on themselves; they are simply not in
	# the way of the six commands §12.2 puts first.
	@app.command(hidden=not _worth_showing(settings))
	def use (
		where: str = typer.Argument("", help="A workspace, or 'connection/workspace'."),
		here: bool = typer.Option(
			False, "--here", help="Write it into this directory instead, for this checkout."
		),
		project: str = typer.Option(
			"", "--project", help="With --here: file new work under this project, by key."
		),
		reset: bool = typer.Option(
			False, "--reset", help="Go back to the configured default."
		),
	) -> None:
		"""Choose what a bare task number means.

		It changes what a number means. It never changes what you can see — every listing
		still spans everything you can reach.

		Examples:

		  subroutine use

		  subroutine use acme

		  subroutine use work/acme

		  subroutine use --here --project SR

		  subroutine use --reset

		'--here' writes a .subroutine file in the current directory, and is what a checkout of
		a repository wants: it answers "which project is this work" for everything started
		from here, including an agent, which cannot be asked. Without it the choice is
		machine-wide, which cannot be right for two repositories at once.
		"""

		if reset:
			removed = subroutine.context.clear()

			with program.opened() as world:
				say(
					f"Now working in {world.current.describe(qualified=world.qualifies_connection)}."
					if removed is not None
					else "There was nothing to reset."
				)

			return

		with program.opened() as world:
			if here:
				_use_here(program, world, where, project)

				return

			if project.strip():
				stop(
					"--project only means something with --here.",
					"A project belongs to a directory, not to the whole machine: "
					"'subroutine use --here --project SR'.",
				)

			if not where.strip():
				say(f"Working in {world.current.describe(qualified=world.qualifies_connection)}.")

				# The marker is reported here rather than only where it acts, because this is
				# the command somebody runs when they are asking "why is my work going there".
				if world.marker is not None:
					say(f"This directory says {world.marker.describe()}.")

				say("")
				_suggest(console, "subroutine use --reset", "go back to working everywhere")

				return

			connection, workspace = _chosen(program, world, where)
			subroutine.context.store(connection, workspace)

			shown = f"{connection}/{workspace}" if world.qualifies_connection else workspace
			say(f"Now working in {shown}.")
			say("")
			_suggest(console, "subroutine agenda")

	# **Visible on a one-connection install, unlike `use` and `connections`.** Those answer
	# *where* work goes, which is a question nobody has until there are two answers. This
	# answers *who is asking* — and the case that most needs it is precisely the one
	# `_worth_showing` would hide: a single connection with an agent's token in the environment,
	# where two principals share one machine and nothing else in the program says which of them
	# the next command acts as (`#335`).
	@app.command()
	def whoami (
		json_output: bool = typer.Option(False, "--json", help="Print the answer as JSON."),
		strict: bool = typer.Option(
			False, "--strict", help="Stop if any connection cannot be reached."
		),
	) -> None:
		"""Which account this machine is acting as, and what it is allowed to do.

		Examples:

		  subroutine whoami

		  subroutine whoami --json

		Worth asking before the first change of a session. One machine can hold more than one
		credential — yours in the credentials file, an agent's in the environment — and the one
		that answers here is the one your next command will act under.
		"""

		# One line per connection. Refusing this reported an ambiguous configuration through
		# the one command somebody would run to find out about it (`#327`).
		_whoami(program, json_output=json_output, strict=strict)

	# **A group whose bare invocation is still the listing**, the way the application's own is
	# still the agenda (§12.2a). `subroutine connections` is in other people's notes and in
	# every refusal that offers it, so turning it into `connections list` to make room for
	# `connections add` would rename a command to add one beside it.
	connections_app = typer.Typer(invoke_without_command=True)

	# **Hidden under the same rule as `use`, and `add` is hidden with it** (§1.4). That reads
	# backwards — the person who most needs `connections add` is the one with no second
	# connection — and it is right anyway, because *nothing on the filesystem tells a machine
	# that has not been set up yet from one that never will be*. Both look like one connection
	# and no database. So the way somebody finds this command is prose they are reading, which
	# is what `#542` is for, and revealing it here would put "instance" in front of §1.4's
	# reader on the strength of a guess.
	app.add_typer(
		connections_app, name="connections", hidden=not _worth_showing(settings)
	)

	# **This docstring is published as `--help`**, so the reasoning lives out here. `#278`:
	# the listing marks the connection being written to as well as the one that is merely the
	# fallback. Those are different questions, and only the second used to be answered — under
	# a word, "default", that reads like the first. An agent read it, told Simon local was
	# where writes went, and a bare `add` filed to the other instance.
	@connections_app.callback()
	def connections (context: typer.Context) -> None:
		"""List the instances this reaches, which one you are working in, and where each
		one's token came from.

		No token is ever printed, and none can be recovered from what is. Which of the four
		places supplied it is the useful part — the standing footgun in comparable tooling is
		not having several sources but not knowing which one won.
		"""

		if context.invoked_subcommand is not None:
			return

		_connections_listed(program)

	@connections_app.command("add")
	def connections_add (
		name: str = typer.Argument("", help="What to call it here, as in 'work'."),
		url: str = typer.Option(
			"", "--url", help="Where that instance is, as in https://tasks.example.com."
		),
		token_env: str = typer.Option(
			"",
			"--token-env",
			help="Read its token from this environment variable rather than storing one.",
		),
		token_command: str = typer.Option(
			"",
			"--token-command",
			help="Run this to get its token, as in a password manager's show command.",
		),
		read_only: bool = typer.Option(
			False, "--read-only", help="Read that instance but refuse to write to it."
		),
		default: bool = typer.Option(
			False, "--default", help="Send new work there rather than to your own database."
		),
		check: bool = typer.Option(
			True,
			"--check/--no-check",
			help="Reach the instance before recording it. On by default.",
		),
	) -> None:
		"""Record another instance this machine can reach.

		Examples:

		  subroutine connections add work --url https://tasks.example.com

		  subroutine connections add work --url https://tasks.example.com --read-only

		  subroutine connections add acme --url https://acme.example --token-env ACME_TOKEN

		The name is yours and nobody else's. It becomes the first part of every address that
		instance's items print as, so work/acme/42, and two people reaching one server may
		call it different things.

		It asks for the token unless one is already set for that name, checks that the
		address and the token both work by reaching the instance, and writes nothing until
		they do. Tokens are kept in a file of their own that nothing else reads.
		"""

		resolved = settings()

		# **Before the name is validated**, or the empty string is refused as a badly-shaped
		# name — which is true and is not what happened. Somebody who typed the command and
		# stopped needs the example, not the grammar.
		if not name.strip():
			stop(
				"Say what to call it.",
				"For example: subroutine connections add work --url "
				"https://tasks.example.com",
			)

		wanted, address, roster = _connection_named(program, resolved, name, url)

		connection = subroutine.connections.Connection(
			name=wanted,
			url=address,
			read_only=read_only,
			token_env=token_env.strip() or None,
			token_command=token_command.strip() or None,
		)

		never_had_one = _has_never_had_a_list_of_its_own(roster, resolved)
		leads = default or never_had_one
		sole = _stops_looking_for_a_local_list(never_had_one)

		try:
			found = subroutine.credentials.resolve(
				connection, default_connection=wanted if leads else roster.default
			)

		except subroutine.errors.Unauthenticated as error:
			# **The hint is replaced and the detail is kept.** What went wrong is exactly right
			# — the variable is unset, the helper is not installed, it printed nothing — but
			# the advice offers to change 'token_env' in the configuration file, and nothing
			# has been written to it yet. The thing this person can act on is the option they
			# just typed.
			stop(
				error.detail,
				"Fix that and run this again, or leave --token-env and --token-command out "
				"and it will ask for the token.",
			)

		except subroutine.errors.SubroutineError as error:
			fail(error)

		secret = found.token
		asked = secret is None

		if secret is None:
			secret = _asked_for_a_token(program, wanted)

		reached = _reaching(program, connection, roster, resolved, secret) if check else None
		unchecked = _refuse_a_second_name_for_one_instance(
			program, roster, resolved, reached, wanted=wanted
		)

		# **The token before the connection.** Both writes can fail on a full or read-only
		# filesystem, and the two half-written states are not equally bad: a credential under a
		# name nothing reaches is inert, where a connection with no credential makes every
		# subsequent listing report a connection it cannot use.
		if asked:
			kept = subroutine.credentials.store(wanted, secret)

		try:
			written = subroutine.config.store_table(
				f"connections.{wanted}", _connection_settings(connection)
			)

			if leads:
				subroutine.config.store_setting("default_connection", wanted)

			if sole:
				subroutine.config.store_table(
					f"connections.{subroutine.connections.LOCAL_NAME}", {"enabled": False}
				)

		except (OSError, ValueError) as error:
			stop(
				f"{wanted!r} could not be written to "
				f"{subroutine.config.config_file_path()}: {error}",
				"Check that the file is writable, then try again.",
			)

		if reached is not None:
			say(_describing(reached))

		say(f"Added {wanted} to {written}")

		if unchecked:
			say(_what_was_not_checked(unchecked))

		if asked:
			say(f"Its token is in {kept}, readable only by you.")

		else:
			say(f"Its token comes from {found.source}.")

		# **The rule `serve` enforces, said from the other end.** An instance refuses to listen
		# beyond its own machine without TLS — "bearer tokens sent over plain HTTP are
		# compromised tokens" — and nothing said a word to somebody pointing a *client* at
		# exactly that address and handing it a credential. Said rather than refused: this is
		# somebody else's server and the reader may have no say over it, so the useful thing is
		# knowing rather than being stopped.
		if subroutine.connections.in_the_clear(connection):
			warn(
				f"{wanted} is reached over plain http, so its token crosses the network "
				f"readable by anything in between. Ask whoever runs it for an https address."
			)

		# **Three sentences because there are three situations**, and the differences are what a
		# reader needs. A read-only connection is where a bare number points and is not where
		# work goes, so saying it is would be contradicted by the very next command; and the
		# reason 'because this machine has no list of its own' belongs only to the case that
		# was decided here rather than asked for.
		if leads and connection.read_only:
			say(f"A bare number means {wanted} now, and nothing can be written there.")

		elif leads and default:
			say(f"New work goes to {wanted} now.")

		elif leads:
			say(_where_new_work_goes(wanted, sole=sole))

		say("")
		_suggest(console, "subroutine list", "everything this machine can now reach")

	def show_today () -> None:
		"""Print today's agenda, as a bare ``subroutine`` invocation does."""

		_show_today(program, workspace=selected.workspace)

	return show_today, selected


def _named_changes (program: Program, **given: typing.Any) -> dict[str, typing.Any]:
	"""Return the fields a caller actually named, from options that each mean *unset* differently.

	**Out of `register`'s closure to pay for an option that grew it** (`#943`'s ratchet,
	met by `SR#1005`, `SR#1215`, `SR#1430` and now `SR#1696`). The bill for a new command
	arrives for an *option* on an existing one too, in smaller instalments, and the remedy
	the ratchet names is the same: what can leave the closure, leaves.

	Nothing here needs the closure. It reads fifteen option values and returns a dict, so
	the one thing it did need — the closure's `stop` — is `program.stop` now, exactly as
	`release`'s extracted body found before it.

	**Taken as ``**given`` rather than as fifteen keyword parameters.** They are read by
	name below and every one is a Typer option whose type is decided at the declaration, so
	spelling them again here would be a second copy of that list free to disagree with it —
	and a signature nobody could read. What makes that safe is that the caller is the single
	command these belong to, and a name this does not read is a name it does not write.
	"""

	title = given["title"]
	description = given["description"]
	estimate = given["estimate"]
	remind = given["remind"]
	importance = given["importance"]
	urgency = given["urgency"]
	kind = given["kind"]
	status = given["status"]
	assignee = given["assignee"]
	tags = given["tags"]
	due = given["due"]
	timezone = given["timezone"]
	repeat = given["repeat"]
	repeat_from = given["repeat_from"]
	project = given["project"]

	changes: dict[str, typing.Any] = {}

	# Written out rather than looped, because each of these decides *not given* differently
	# and a loop would hide that: a title cannot be blank, a description and an estimate can
	# be cleared by passing nothing, and a number has no empty string to be given.
	if title:
		changes["title"] = title

	if description is not UNGIVEN:
		changes["description"] = description or None

	if estimate is not UNGIVEN:
		changes["estimate"] = estimate or None

	if remind is not UNGIVEN:
		changes["reminder"] = remind or None

	if importance != UNGIVEN_NUMBER:
		changes["importance"] = importance

	if urgency != UNGIVEN_NUMBER:
		changes["urgency"] = urgency

	if kind:
		changes["type"] = kind

	if status:
		changes["status"] = status

	# **Handing work over, which is the whole of `#493` and the reason it ranked where it
	# did.** A task could be assigned when it was filed — §6.13's `@name` has always worked
	# — and never afterwards, so work could not be passed between two people or two agents
	# once it was under way. An empty string leaves it with nobody, which is how something
	# is handed back without being handed to anybody in particular.
	if assignee is not UNGIVEN:
		changes["assignee"] = assignee or None

	# Comma-separated, because a repeated option would make "no tags" impossible to say —
	# and replacing rather than adding is what §8.3 means by a field, here and in the API.
	if tags is not UNGIVEN:
		changes["tags"] = [
			word.strip() for word in tags.split(",") if word.strip()
		]

	if due is not UNGIVEN:
		changes["due"] = due or None

	# Sent on its own as well as beside a date: the zone a deadline is *read* in can be
	# wrong while the date is right, and §6.4 keeps the two separate for that reason.
	if timezone is not UNGIVEN:
		changes["timezone"] = timezone or None

	# **Editing a repeat, which a captured line can never do** (`#94`, Simon's direction of
	# 2026-08-16). The grammar reads one out of a sentence at creation and that is the fast
	# path — but a line only ever *makes* one, so before this the only way to change how
	# something came round was the API. Empty stops the series, which `stop_repeating`
	# explains is completing the template rather than clearing a column.
	if repeat is not UNGIVEN:
		changes["recurrence"] = repeat.strip() or None

	# **Sent on its own as well as beside a rule**, exactly as `--timezone` is beside a date
	# and for the same reason: *how often* can be right while *measured from where* is
	# wrong, and re-sending a rule in order to change the field next to it is how a rule
	# gets retyped slightly differently. `#918` is what made that reach anything.
	if repeat_from is not UNGIVEN:
		# **No empty form, unlike every sentinel above it.** Those clear a field that can
		# legitimately hold nothing; a series always measures from *somewhere*, so there is
		# no state for this to clear to — and passing it empty would reach the service as
		# *not given*, which answers "Changed" having changed nothing. `#918`, met once
		# already today, one layer up.
		if not repeat_from.strip():
			program.stop(
				"A repeat is always measured from something.",
				"Say --repeat-from schedule or --repeat-from completion, or use "
				"--repeat '' to stop it repeating at all.",
			)

		changes["recurrence_anchor"] = repeat_from.strip()

	# **Moving between projects, which `update` could not do until `#169`.** The endpoint
	# has taken it since `#43`; I added this command without it, and the sequence a new
	# user actually performs — accumulate tasks, notice a theme, make a project, file them
	# — dead-ended at the last step.
	if project:
		# Passed as typed: `projects.normalize_key` in the service decides the stored
		# form, and a second opinion here is a copy of that rule free to disagree with
		# it — which is exactly what happened when the rule changed (`#508`).
		changes["project"] = project.strip()

	# **A refusal rather than a cheerful no-op**, matching the MCP tool: somebody who ran
	# this and named no field meant to change something, and "unchanged" would hide the
	# mistake at exactly the moment it could still be corrected.
	if not changes:
		program.stop(
			"Nothing to change.",
			"Name a field: --title, --description, --importance, --urgency, "
			"--estimate, --type, --status or --repeat.",
		)


	return changes


def _changed (
	program: Program,
	*,
	which: str,
	changes: dict[str, typing.Any],
	because: str,
	as_json: bool,
	just_this_one: bool = False,
	from_now_on: bool = False,
	expected_version: int | None = None,
) -> None:
	"""Apply the fields a caller named to one task, and say what happened.

	**Out of `register`'s closure to pay for an option that grew it** (`#943`'s ratchet, met by
	`#1005` and `#1215` before this). Nothing here needs the closure that :class:`Program` does
	not carry, which is the test that ratchet is really applying: what stayed behind is the part
	that decides *whether a field was given*, and each of those decides it differently.
	"""

	with program.opened() as world:
		located, task = _a_task(program,
			world,
			_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
			verb="update",
		)
		client = _require_connection(program, world, located.connection)

		# **Asked before the write and never after it**, which is what makes the answer a
		# decision rather than a confirmation. A change that names only a status or only how
		# something repeats has no second answer (decision `#1249` §1) and the domain lets it
		# through — but the question is put here, before the request, so a repeating item is
		# never edited and then asked about.
		changed = client.update(
			ref=task.ref,
			workspace=located.workspace,
			applies_to=(
				None
				if not (changes.keys() - subroutine.clients.base.NEVER_ASKS)
				else _which_occurrences(
					program, task, just_this_one=just_this_one, from_now_on=from_now_on
				)
			),
			**changes,
			# **The version the caller quoted, never one this command read for itself**
			# (`#1696`, §8.9). ``_a_task`` above resolves the ref with a fresh read, so a
			# version taken from *there* would be milliseconds old and would pass whatever
			# happened while somebody was thinking — a guard that reports success for the
			# case it exists to catch. What is compared is the number a person saw in an
			# earlier ``subroutine show``, which is the only thing here that spans the gap.
			#
			# ``None`` is *did not ask* rather than *asked and passed*, so leaving the option
			# out behaves exactly as it did before this existed.
			expected_version=expected_version,
		)
		now = dataclasses.replace(located, item=changed)

		_because(client, located, because, what="Changed")

		if as_json:
			program.say(json.dumps(_as_json(world, now.connection, now.item), indent=2))

			return

		program.say(_acted(world, now, "Changed"))
		_suggest(
			program.console,
			f"subroutine show "
			f"{world.address_of_located(now).replace(subroutine.domain.refs.SIGIL, '')}",
		)


def _show_today (program: Program, *, workspace: str | None) -> None:
	"""Print today's agenda, as a bare ``subroutine`` invocation does (§12.2a).

	**It reaches :func:`_agenda` rather than the Typer command**, and that is a fix rather than
	a tidy-up (`#1215`). Calling a decorated command as a Python function hands every option its
	``typer.Option(...)`` *descriptor* instead of its default — an object that is truthy, so
	``--project``'s empty string arrived as an ``OptionInfo`` and the bare invocation refused
	itself with *'project' names a project inside one workspace*. Every option had to be named
	at the call site for the wrapper to be correct, which made adding one a silent trap.

	The bare invocation carries no day, no look-ahead override and no flags by construction:
	somebody who has typed nothing has asked for the default of everything.
	"""

	_agenda(
		program,
		json_output=False,
		strict=False,
		workspace=workspace,
		days=subroutine.domain.agenda.DEFAULT_HORIZON_DAYS,
	)


def _matching (
	clients: typing.Sequence[subroutine.clients.base.Client], name: str
) -> subroutine.clients.base.Client:
	"""Return the client for one connection name."""

	for client in clients:
		if client.connection.name == name:
			return client

	raise LookupError(f"No open client for connection {name!r}.")


def _warn_about_the_credentials_file (
	warn: typing.Callable[[str], None],
) -> None:
	"""Say once that the credentials file is readable by anyone else, if it is.

	**The warning existed and one command produced it.** Its own docstring promised
	``subroutine connections`` *and any command that actually reads a token from the file* —
	and ``connections`` was the only caller, which §1.4 hides from ``--help`` until a second
	connection exists. So the person most likely to have a loose file, and least likely to go
	looking, was told nothing by anything they would run.

	Called where every command that opens a connection passes through, which is what the
	sentence claimed all along. **No once-per-process flag**, tempting as one is for a command
	opening three connections: this is called from two places and each runs once, and a module
	flag would be state that survives an invocation — which in a test process means the
	warning is said to whichever test happens to run first and to none of the others. ``ssh`` refuses a private key with loose permissions outright;
	this warns, because the consequence of refusing is somebody unable to see their own to-do
	list, and their tasks are not their SSH key.
	"""

	warning = subroutine.credentials.permission_warning()

	if warning is not None:
		warn(warning)


def _client (
	connection: subroutine.connections.Connection,
	roster: subroutine.connections.Roster,
	settings: subroutine.config.Settings,
) -> subroutine.clients.base.Client:
	"""Open whichever kind of client this connection needs."""

	return subroutine.clients.opening.for_connection(connection, roster, settings)


def _workspace_hint (item: Reached) -> str:
	"""Describe the workspaces one connection reaches."""

	if not item.identity.workspaces:
		return "That connection reaches no workspaces at all."

	listed = ", ".join(workspace.slug for workspace in item.identity.workspaces)

	return f"Workspaces on {item.name}: {listed}."


def _deadline (item: Item) -> datetime.datetime | None:
	"""Return an item's deadline, or ``None`` when it is not the kind of thing that has one."""

	return item.due_at if isinstance(item, subroutine.views.Task) else None


def _typeable (world: World, connection: str, item: Item) -> str:
	"""Return what to type to reach one item — the printed form without its sigil.

	A suggested command has to be one that works, and ``#`` starts a comment in every POSIX
	shell (docs/design.md §12.2a), so a suggestion carries the bare number or the qualified path.

	**"Works" means when it is typed, not when it is printed** (`#280`). A ``-c``/``-w`` flag
	settles this invocation alone, so a bare number justified by it names something else by
	the time anybody acts on the advice — and the agenda's closing tip is ``subroutine done``,
	where that is somebody's item completed on the wrong instance.
	"""

	return world.address_of_item(connection, item, next_time=True).replace(
		subroutine.domain.refs.SIGIL, ""
	)


def _discarded (program: Program, world: World, *, which: str) -> None:
	"""Move one item to the trash and say what the move did not reach.

	**Outside `register` because that closure only shrinks**, which is the ratchet's own
	instruction: a command's body belongs in a function it calls. It came out when `#1294`
	added the line below, which is `#943` working rather than being worked around.
	"""

	located = _locate(program, world, which, kinds=ANY_ITEM, verb="delete")
	where = world.writing_to()

	gone = where.client.discard(
		ref=located.ref,
		entity_type=located.entity_type,
		workspace=located.workspace,
	)

	program.say(_acted(world, dataclasses.replace(located, item=gone), "Deleted"))

	# **What the delete did not reach** (`#1294`). Said above the tip rather than as a second
	# one, because it is part of what happened rather than a command to try next.
	standing = _the_repeat_left_behind(world, located, gone)

	if standing is not None:
		program.say(f"  {standing}")

	# **The remedy, not a reassurance.** "It can be restored" is a claim the reader has to
	# trust; the command that does it is one they can run. Printed with the ref because after
	# this the item is out of every listing, so the number on screen is the only way back to it.
	_suggest(
		program.console,
		f"subroutine restore {_typeable(world, located.connection, located.item)}",
		"put it back",
	)


def _the_repeat_left_behind (world: World, located: Located, gone: Item) -> str | None:
	"""Return the line naming the repeat a deleted occurrence leaves standing, or ``None``.

	**`#1294`, and the mirror of a refusal that already exists one row along.** ``delete`` on
	the *series* is turned down and names the occurrence; this is the same fact told from the
	other end. Deleting the visible row is what somebody reaches for when they mean *stop the
	repeat*, and until now it did something strictly worse than stopping: ``done <series>``
	leaves a tidy one-off, while deleting the occurrence leaves the series present, rendered
	by no listing and no agenda, and **unable to produce another**.

	That last part is measured rather than assumed. :func:`subroutine.domain.tasks.materialise`
	is called from two places — once when a repeat is created, and once on *completion* — so
	the next occurrence is minted by finishing the last one. With the only finishable row in
	the trash the series has no route to a successor and is reachable solely by its number.

	**So the sentence says the repeat is still there and how to end it, and does not say it
	will come back.** It would not: a message implying the series still runs on a clock would
	be false, which is the failure this codebase files as *a refusal asserting a cause it has
	not established*. Restoring is already the tip printed underneath.

	``None`` for a document, for a plain task, for the series itself, and for an occurrence of
	a repeat somebody has already stopped — ``recurrence_rule`` resolves through
	:func:`subroutine.views._from_a_live_series`, so a completed template answers nothing and
	there is no rule left to end.

	The kind is settled with :func:`isinstance` and the fields are read as plain attributes,
	because `#674`'s cross-surface scan reads ``item.<field>`` to derive what a rendering
	shows and cannot see a lookup spelled as a string.
	"""

	if not isinstance(gone, subroutine.views.Task):
		return None

	if gone.recurrence_template_ref is None or gone.recurrence_rule is None:
		return None

	named = world.address_of(
		located.connection, gone.workspace_id, gone.recurrence_template_ref
	)
	typeable = world.address_of(
		located.connection, gone.workspace_id, gone.recurrence_template_ref, next_time=True
	).replace(subroutine.domain.refs.SIGIL, "")

	return (
		f"The repeat behind it, {named}, is still there — "
		f"'subroutine done {typeable}' stops it altogether."
	)


#: What a person may type at decision `#1249`'s prompt, and what each answer means.
#:
#: **Longer forms as well as the letter**, because somebody who has been asked *just this one,
#: or every one from now on* will type a word from the question as often as an initial. What is
#: deliberately absent is ``a`` and ``all``: *all* promises something about history that does
#: not happen (`#1249` §2), so it is not a word this program answers to.
OCCURRENCE_ANSWERS = {
	"j": subroutine.domain.tasks.THIS_ONE,
	"just": subroutine.domain.tasks.THIS_ONE,
	"this": subroutine.domain.tasks.THIS_ONE,
	"just this one": subroutine.domain.tasks.THIS_ONE,
	"e": subroutine.domain.tasks.FROM_NOW_ON,
	"every": subroutine.domain.tasks.FROM_NOW_ON,
	"from now on": subroutine.domain.tasks.FROM_NOW_ON,
	"every one from now on": subroutine.domain.tasks.FROM_NOW_ON,
}

JUST_THIS_ONE = "--just-this-one"
FROM_NOW_ON = "--from-now-on"

#: Decision `#1249`'s two answers as command-line flags, declared once for the three commands
#: that write a field with two answers on it.
#:
#: **One descriptor rather than three copies of the same nine lines.** The help text is
#: user-facing, so three copies is three places for it to drift — and a flag whose wording
#: differs between `update` and `plan` reads as two different flags. Typer builds its own
#: parameter per command from one of these, so sharing it changes nothing about what either
#: command parses; what it removes is the drift.
JUST_THIS_ONE_OPTION = typer.Option(
	False, JUST_THIS_ONE, help="If it repeats: change this one only."
)
FROM_NOW_ON_OPTION = typer.Option(
	False, FROM_NOW_ON, help="If it repeats: change this one and every one after it."
)

#: What to say when there is nobody to ask, in one place because three commands say it.
NOBODY_TO_ASK = (
	f"Add {JUST_THIS_ONE}, or {FROM_NOW_ON} for every one after it too."
)


def _a_terminal_is_attached () -> bool:
	"""Say whether there is somebody who could answer a question.

	**Asked of stdin, which is where an answer would have to come from.** Not of the console: a
	piped ``--json`` reader still leaves somebody at a keyboard, and refusing them would be
	reading the wrong end of the pipe.

	**A function rather than the expression inline, because ``CliRunner`` replaces
	``sys.stdin``** for the duration of an invocation — so a test monkeypatching the real one
	is patching something the command never sees, and passes against the defect. That trap is
	recorded here already (`#299`) and this is the seam that makes the *other* half drivable:
	the piped refusal is driven with this function untouched, so the real ``isatty`` is what
	answers there, and only the prompt path is reached by substituting it.
	"""

	return sys.stdin.isatty()


def _which_occurrences (
	program: Program,
	task: subroutine.views.Task,
	*,
	just_this_one: bool,
	from_now_on: bool,
) -> str | None:
	"""Settle which occurrences an edit is for — decision `#1249` §5, and `#1251`.

	**Three answers to one question, chosen by who is there.** A flag settles it. Otherwise a
	terminal with somebody at it is asked, because being asked is the point of decision 1 —
	and anything else is refused by name, which is `#299`'s rule that the question has to be
	settled *before* stdin is read rather than by reading it. A script, a CI job or an agent
	that blocked on a prompt would hang for ever on input that is not coming.

	``None`` for something that does not repeat, which is most of what anybody edits: the
	prompt must not become a toll on every ordinary change.

	**The refusal names the two flags rather than the two values.** ``this_one`` and
	``from_now_on`` are what goes over the wire and neither is a thing to type at a terminal;
	`#1259`'s rule is that a refusal offers the remedy for the surface it arrived on.
	"""

	if just_this_one and from_now_on:
		program.stop(
			"An edit is for this one or for every one from now on, not both.",
			f"Pass one of {JUST_THIS_ONE} and {FROM_NOW_ON}.",
		)

	if just_this_one:
		return subroutine.domain.tasks.THIS_ONE

	if from_now_on:
		return subroutine.domain.tasks.FROM_NOW_ON

	if not subroutine.views.repeats(task):
		return None

	if not _a_terminal_is_attached():
		program.stop(
			"That repeats: is this one changing, or every one from now on?",
			NOBODY_TO_ASK,
		)

	while True:
		# To stderr, like every other prompt here, so a `--json` reader on stdout is never
		# handed a question.
		answer: str = typer.prompt(
			"That repeats. Change just this one, or every one from now on? [j/e]", err=True
		)
		settled = OCCURRENCE_ANSWERS.get(answer.strip().lower())

		if settled is not None:
			return settled

		program.warn("Answer j for just this one, or e for every one from now on.")


def _asked (given: str, question: str) -> str:
	"""Return an argument, asking for it if it was left out.

	docs/design.md §12.2a: bare commands prompt rather than error. A required-argument error is a
	dead end where a question would do — and in a pipe, where there is nobody to ask, the
	prompt fails with the usage anyway, which is the right answer there.
	"""

	if given.strip():
		return given

	# To stderr, so that a `--json` reader on stdout is never handed a prompt (see `add`).
	answer: str = typer.prompt(question, err=True)

	return answer


def agenda_rows (
	world: World, gathered: subroutine.fanout.Gathered[subroutine.views.Agenda]
) -> dict[str, list[Row]]:
	"""Return every agenda bucket, merged across connections and ordered as it is read.

	**Re-sorted across connections, not concatenated.** Each connection answers already
	ordered, and appending one block after another left ``--merged`` showing all of A
	newest-first and then all of B — two sorted runs end to end, which §13.7 explicitly rules
	out ("sorting is re-applied after the merge"). It also made the suggested ``done`` command
	name the first *connection's* first row rather than the newest one.

	**Lifted out of :func:`_render` so that the ordering has one definition and something can
	drive it** (`#992`). It was a loop inside the renderer, so the only way to ask what order
	the terminal puts a bucket in was to render one and read it back — and the scripted path
	beside it does not call this at all, which is `#993`.
	"""

	return {
		field: _in_order(_across(world, gathered, operator.attrgetter(field)), field)
		for _heading, field, _late in AGENDA_SECTIONS
	}


def _withdrawn (program: Program, *, which: str, words: str) -> None:
	"""Take a comment back out of an item's record.

	**Named by what it says**, because that is what a person is looking at. A comment has no
	number of its own and its id is a UUID that appears in nothing anybody reads, so asking
	for one would make this a command only a script could run.

	**Out of `register`'s closure to pay for a command that grew** (`#943`'s ratchet, met by
	`#1005`). The rule that ratchet enforces is that a command body belongs in a function
	`register` calls, and this one needs nothing from the closure that :class:`Program` does
	not carry.
	"""

	with program.opened() as world:
		located = _locate(program,
			world,
			_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
			kinds=ANY_ITEM,
			verb="uncomment",
		)
		client = _require_connection(program, world, located.connection)
		wanted = _asked(words, "Which comment? (some of its words)")

		# **The matching is shared with the agent's tool** (`#415`). Both surfaces filtered
		# for themselves, and only this one — which prompts, so a person is asked — happened
		# to be safe against words that name every comment rather than one.
		try:
			recorded = subroutine.views.comments_saying(
				client.comments(
					ref=located.ref,
					entity_type=located.entity_type,
					workspace=located.workspace,
				),
				wanted,
			)

		except subroutine.errors.SubroutineError as error:
			program.fail(error)

		if not recorded:
			program.stop(
				f"Nothing recorded on {world.address_of_located(located)} says that.",
				f"Run 'subroutine show {located.ref}' to see what is there.",
			)

		# **Refused rather than resolved, and the several are not listed back.** Printing
		# them would put the reader in the position of choosing by position, which is the
		# one way of naming things this program does not have (§12.2a) — so the answer is
		# to be more specific, and the count is what says how much more.
		if len(recorded) > 1:
			program.stop(
				f"{len(recorded)} comments there say that.",
				"Say more of the one you mean.",
			)

		client.uncomment(
			ref=located.ref,
			comment_id=str(recorded[0].id),
			entity_type=located.entity_type,
			workspace=located.workspace,
		)

		program.say(_acted(world, located, "Taken out of"))
		_suggest(
			program.console,
			f"subroutine show "
			f"{world.address_of_located(located).replace(subroutine.domain.refs.SIGIL, '')}",
		)


def _agenda (
	program: Program,
	*,
	json_output: bool,
	strict: bool,
	workspace: str | None,
	when: str = "",
	days: int | None = None,
	project: str | None = None,
) -> None:
	"""Show what somebody is doing today, merged across every connection they can reach.

	**The command is `subroutine agenda` and `subroutine agenda` is a hidden synonym** (`#996`).
	Simon, 2026-08-18: the rename *"helps consolidate that expectation of similarity"* across
	the surfaces — and it is consolidation rather than redesign, because everything below the
	CLI was already called agenda: ``GET /v1/agenda``, :meth:`Client.agenda`,
	:class:`subroutine.views.Agenda`, :mod:`subroutine.domain.agenda`. ``today`` was the only
	thing in the product wearing the other name.

	``ls``/``list`` is the precedent, and the reasoning is §12.2a's: nothing anybody has typed
	stops working, and a synonym you can *see* is a second thing to choose between.

	**This is not `#509`'s situation, which is why an alias is safe here.** There,
	``subroutine upgrade`` and ``db upgrade`` swapped meanings, so a surviving alias would have
	answered to a name that used to mean the opposite. ``today`` goes on meaning exactly what
	it meant.

	**`-w` precedes the command**, because it is an application-wide option: it changes what
	every command means, not what this one does. ``subroutine agenda -w work`` is therefore
	refused by Typer as an unknown option, which is correct and is also the order most people
	will try first — so the example in the command's help is written the working way round
	rather than the natural-reading way.

	**``project`` arrives as Typer wrote it and is emptied here** rather than at the call site.
	A string option has no ``None``, so "not asked for" is ``""`` — and asking each connection
	for a project named nothing is a 422 rather than the whole agenda.
	"""

	with program.opened(strict=strict) as world:
		# Sent as it was typed; each instance reads it in its own reader's zone. See
		# `_a_readable_day` for why this stopped resolving the word here (`#1083`).
		day = _a_readable_day(when) if when else None

		asked = agenda_asked(
			workspace=workspace, date=day, horizon_days=days, project=project or None
		)

		gathered = subroutine.fanout.gather(
			world.clients, lambda client: client.agenda(**asked), strict=strict
		)

		_report(program, world, gathered.failures)
		_report_zones(program, gathered)
		_report_dates_set_elsewhere(program, gathered)

		if json_output:
			program.say(json.dumps(_agenda_json(world, gathered), indent=2))

			return

		_render(
			world,
			gathered,
			say=program.say,
			console=program.console,
			horizon=asked["horizon_days"],
			named=day is not None,
		)


def _render (
	world: World,
	gathered: subroutine.fanout.Gathered[subroutine.views.Agenda],
	*,
	say: typing.Callable[[str], None],
	console: rich.console.Console,
	horizon: int = subroutine.domain.agenda.DEFAULT_HORIZON_DAYS,
	named: bool = False,
) -> None:
	"""Print the agenda, merged across connections and addressed so it can be typed back.

	**Merged rather than grouped by connection, and deliberately.** §13.7 exists so that a
	developer keeping their own to-do list here and their team's on a company server sees the
	dentist and the stand-up *in one place*; a heading per connection would put them in two.
	The labelling rule is satisfied per row instead, which is what ``address_of`` is for.
	"""

	buckets = agenda_sections(horizon)
	rows = agenda_rows(world, gathered)
	asked_about = gathered.answers[0].value if gathered.answers else None

	# **Which day this is about, when it is not today** (`#1005`). Asked for a future day the
	# `Overdue` section becomes a *projection* — everything due before then, which is true and
	# reads as a fault without a line saying what you are looking at. Silent on the ordinary
	# call, because a date over every agenda is §12.2a's line that says the same thing every
	# time.
	if named and asked_about is not None:
		console.print(
			rich.text.Text(_dated(asked_about.date), style=HEADING)
		)
		say("")

	# **Above the buckets, because it is about the page** (`#986`). `Next` is the ranked bucket
	# and the one this changes; the dated buckets are untouched by design, since a deadline is
	# answered by *when* rather than by whose project it is (`#857`, decision `#982` answer 4).
	_say_prioritised(world, console)

	# One width across every bucket, so the addresses line up down the whole agenda rather
	# than stepping in and out as the sections change. The type column is measured over the
	# whole agenda for the same reason, and because "is this page all one kind of thing?" is
	# a question about the agenda rather than about whichever bucket a row landed in.
	everything = [row for group in rows.values() for row in group]
	columns = Columns.measured(world, everything)
	remaining = sum(
		answer.value.unscheduled_total - len(answer.value.unscheduled)
		for answer in gathered.answers
	)
	# **Dated work further out than the look-ahead** (`#997`). Summed rather than taken from
	# one answer, because §13.7 merges connections and a deadline on the work instance counts
	# exactly as much as one on the personal list.
	later = sum(answer.value.later_total for answer in gathered.answers)
	# **The other two things a day holds back** (`#1215`, Simon's decision of 2026-08-24). The
	# two above report a cap and a window edge; these report what somebody chose to put down —
	# a defer, and a project nobody is running. Summed across connections for `later`'s reason.
	deferred = sum(answer.value.deferred_total for answer in gathered.answers)
	paused = sum(answer.value.paused_total for answer in gathered.answers)
	# **What simply went by** (decision `#1235` §3), summed for `later`'s reason. Not a decision
	# anybody took and not an edge of the window: a day that has been.
	gone = sum(answer.value.passed_total for answer in gathered.answers)
	# **Somebody else's work**, summed for `later`'s reason (`#1265`). The only exclusion here
	# that is about a person rather than a date, and the only one whose remedy is a
	# conversation rather than a command.
	theirs = sum(answer.value.assigned_elsewhere_total for answer in gathered.answers)
	# **The second capped bucket, counted for the first one's reason** (`#1285`). A cap must
	# say it is one, count what is hidden and offer a way to see it all.
	held_up = sum(
		answer.value.blocked_by_others_total - len(answer.value.blocked_by_others)
		for answer in gathered.answers
	)
	printed = False
	first: Row | None = None
	# **One instant for the whole page**, the rule `domain.tasks` follows: two rows compared
	# against two clocks can disagree about the same second, and a listing that marks one late
	# and not the next is worse than one that marks neither.
	moment = subroutine.db.types.utcnow()

	# **And nothing started is carrying a deadline** (`#1243`). The two dated buckets used to be
	# the whole question; `in_progress` now leads and the buckets are disjoint in the order they
	# are computed, so a started task that is late or due today is reported *there* and both of
	# them are empty. Without this clause the page said *Nothing due today* directly above a row
	# reading `(due Sat 22 Aug)` — measured, on a disposable instance, immediately after the
	# reorder.
	#
	# **Any deadline at all, rather than one that has passed**, deliberately: a started task due
	# in three weeks would also be in this section rather than in `upcoming`, and telling a
	# reader something true is worth less than never telling them something false. The cost is a
	# sentence sometimes not printed, and §12.2a already says a line that appears on every page
	# says nothing.
	dated = any(
		isinstance(task, subroutine.views.Task) and task.due_at is not None
		for _connection, task in rows.get("in_progress") or []
	)

	if not rows.get("overdue") and not rows.get("today") and not dated:
		# **Named rather than "today" when a day was asked for**, or the sentence contradicts
		# the heading two lines above it.
		say(
			"Nothing due then."
			if named
			else "Nothing due today."
		)

	for heading, field, late in buckets:
		group = rows.get(field) or []

		if not group:
			continue

		if printed:
			say("")

		console.print(rich.text.Text(heading, style=LATE if late else HEADING))
		printed = True

		# **The tip may never name an occasion** (decision `#1235` §5). `done` on one is
		# accepted and nothing here refuses it; what the defect actually was is the product
		# *advising* it — measured on a disposable instance, where a birthday five months past
		# sat under *Today* with `subroutine done 2` printed beneath it. So the first row that
		# can honestly be finished is what the tip names, and a page holding nothing but
		# occasions falls through to the empty-handed suggestion below.
		#
		# **Nor a row somebody else is holding up** (`#1288`), which is the same defect
		# measured the same way one bucket along: *Waiting on somebody else* is the heading
		# that says nobody can finish this, and `subroutine done 1` sat directly beneath it.
		# :data:`UNFINISHABLE` carries why the two are separate tests.
		if first is None and field not in UNFINISHABLE:
			first = next((row for row in group if not _happens(row[1])), None)

		for connection, task in group:
			console.print(
				# **The row decides, not the section** (`#1243`). `in_progress` leads now, and
				# the buckets are disjoint in order, so a started task with a passed deadline
				# is reported there rather than under *Overdue* — where the heading and the
				# colour both used to come from. Two of `#102`'s three signals would have gone
				# with it, leaving only the date in the ordinary style.
				#
				# **`or`, not a replacement**: `#1116` marks everything under *Waiting on you*
				# late whether or not it has a deadline, and that decision is untouched.
				_item_line(
					world,
					connection,
					task,
					late=late or _is_late(task, now=moment),
					columns=columns,
				)
			)

			for line in _waiting_on(task):
				console.print(line)

	if remaining > 0:
		console.print(rich.text.Text(f"      and {remaining} more unscheduled", style=DETAIL))

	# **The other cap saying it is one** (`#1285`, decision `#1267` §3b). Simon's condition on
	# `unscheduled_total`, applied to the bucket he set it for: this section is ordered by rank
	# and holds the top few, so the reader has to be told how much more somebody else is
	# sitting on.
	if held_up > 0:
		console.print(
			rich.text.Text(
				f"      and {held_up} more waiting on somebody else", style=DETAIL
			)
		)

	# **Said because the window has an edge and nothing else says so** (`#997`, Simon's
	# decision of 2026-08-18). The agenda stays a day view — a listing answers *what is due
	# this quarter* — so what was missing was never the work, it was any sign that the view
	# had left some out. `subroutine list --filter due_at.gte=today` is where it is, and
	# naming the command is what turns a count into something a reader can act on.
	if later > 0:
		console.print(
			rich.text.Text(f"      and {later} dated further out", style=DETAIL)
		)
		console.print(
			rich.text.Text(
				"      subroutine list --filter due_at.gte=today --order due_at",
				style=DETAIL,
			)
		)

	# **What somebody chose to put down, said for the browser's reason** (`#1215`). The agenda
	# is a view beside a list at one address now, so an unexplained difference between the two
	# is what `#649`'s amendment forbids — and `#989` binds the agenda to one answer on every
	# surface, so the terminal says what the page says.
	#
	# **Each on its own line here where the browser puts all four on one**, which is not a
	# divergence: `#989` binds the *answer*, never the rendering, and this surface already
	# spends a line per fact above.
	if deferred > 0:
		console.print(
			rich.text.Text(f"      and {deferred} put off until later", style=DETAIL)
		)

	if paused > 0:
		console.print(
			rich.text.Text(
				f"      and {paused} in projects nobody is running", style=DETAIL
			)
		)

	# **Said for the same reason as the four above** (`#649`'s amendment, decision `#1235` §3):
	# a list at this scope still shows these and this page does not, and an unexplained
	# difference between two views of one place is the thing that rule exists to prevent.
	if gone > 0:
		console.print(
			rich.text.Text(f"      and {gone} already past", style=DETAIL)
		)

	# **Last of the six, because it is the one the reader cannot act on alone** (`#1265`,
	# decision `#1267` §1). Every line above names work that is the reader's, held back by a
	# date or a decision they took; this names work that is somebody else's, and it is here so
	# that an empty agenda on a team instance can be told from an idle one.
	if theirs > 0:
		console.print(
			rich.text.Text(f"      and {theirs} assigned to somebody else", style=DETAIL)
		)

	if first is None:
		_suggest(console, 'subroutine add "something to do"')

		return

	say("")
	_suggest(console, f"subroutine done {_typeable(world, first[0], first[1])}")


def _flat (
	world: World,
	rows: typing.Sequence[Row],
	*,
	console: rich.console.Console,
	columns: Columns | None = None,
	term: str | None = None,
	project: str | None = None,
) -> None:
	"""Print one list, every row addressed by the shortest form that resolves.

	``columns`` is passed in when the page is larger than these rows — a grouped listing
	measures across every connection, so the addresses line up down the whole output rather
	than stepping in and out as each heading changes what is below it.
	"""

	measured = (
		Columns.measured(world, rows, term=term, project=project)
		if columns is None
		else columns
	)

	for connection, task in rows:
		console.print(_item_line(world, connection, task, late=False, columns=measured))


def _grouped (
	world: World,
	gathered: subroutine.fanout.Gathered[Listing],
	*,
	console: rich.console.Console,
	say: typing.Callable[[str], None],
	term: str | None = None,
	project: str | None = None,
) -> None:
	"""Print a group per connection, which is what a flat listing has instead of structure.

	Unlike the agenda, a list of open tasks has no ordering a person already holds in their
	head, so the connection is the only structure there is — and a heading carries the label
	once rather than repeating it on every line (§13.7).
	"""

	printed = False
	columns = Columns.measured(
		world,
		[row for answer in gathered.answers for row in answer.value.rows],
		term=term,
		project=project,
	)

	for answer in gathered.answers:
		# **An empty connection keeps its heading once there are several** (`#269`). Skipping
		# it made a reachable instance with nothing in it indistinguishable from one that is
		# not working — and somebody who has just wired up a connection reads a missing group
		# as a missing connection, and goes back to check the thing that was never wrong.
		#
		# Guarded on `qualifies_connection`, because with one connection there is no heading at
		# all and §13.5b's four-command transcript must stay exactly as it is.
		if not answer.value.rows and not world.qualifies_connection:
			continue

		if printed:
			say("")

		console.print(rich.text.Text(answer.connection.label, style=GROUP))
		printed = True

		if not answer.value.rows:
			console.print(rich.text.Text("  Nothing here.", style=DETAIL))

			continue

		_flat(world, answer.value.rows, console=console, columns=columns)


def _width (world: World, rows: typing.Sequence[Row]) -> int:
	"""Return how wide the address column needs to be for these rows."""

	return max(
		(len(world.address_of_item(connection, task)) for connection, task in rows), default=0
	)


def _suggest (
	console: rich.console.Console, command: str, about: str | None = None
) -> None:
	"""Print the command to try next (docs/design.md §12.2a).

	The single most valuable habit here: the user is never left wondering what exists.

	**``Tip:`` is not decoration, it is the fix for a defect** (`#128`). Until 2026-07-31 this
	printed the bare command, and the only thing marking it as advice rather than as an answer
	was ``dim cyan`` — so piped, redirected, read aloud, or quoted inside a fenced block in the
	README, the distinction was simply gone. Decision `#102` says no information exists only in
	a colour, and this was the counter-example sitting in the output of every command. Found by
	Simon reading the README as a stranger would, where the suggestion after ``add`` looks
	exactly like a second line of what happened.

	``about`` says what the command *gets you*, for the ones whose name does not. It is left off
	where the command already reads as English — a line explaining that ``subroutine agenda``
	shows today is noise, and noise is how a signpost stops being read.
	"""

	line = f"  Tip: {command}" if about is None else f"  Tip: {command} — {about}"

	console.print(rich.text.Text(line, style=SUGGESTION))


def _because (
	client: subroutine.clients.base.Client, located: Located, reason: str, *, what: str
) -> None:
	"""Record why an act was taken, as a comment on the item it was taken against.

	**A comment rather than a field** (decision `#96`, item `#99`). A field would be
	overwritten by the next defer, and each wait has its own reason; a comment is a sequence,
	which is what a record of what happened is.

	`#99` also argued that a ``#42`` in the reason becomes a backlink for free. It becomes an
	indexed *mention*, which is not the same claim: nothing reads that table yet, so "waiting
	on #42" links nothing anybody can see until `#144`. The sequence argument above carries
	this on its own, which is why it is stated first.

	**The comment carries the act as well as the reason**, so it reads as a sentence about
	what happened rather than as a fragment nobody can place — "Hidden until Mon 3 Aug —
	waiting on the provider's reply". The event beside it records the field that moved; this
	records the part no field holds.

	Written **after** the act, so a reason that cannot be recorded never claims a defer that
	did not happen. Nothing is written when nobody gave a reason: an empty record entry would
	timestamp a claim that something was said.
	"""

	if not reason.strip():
		return

	client.remark(
		ref=located.ref,
		body=f"{what} — {reason.strip()}",
		entity_type=located.entity_type,
		workspace=located.workspace,
	)


def _waiting_on (item: Item) -> list[rich.text.Text]:
	"""Return a line per item holding this row up, or nothing where none is named — `#1287`.

	**Only the agenda resolves this**, so on every other listing ``blocked_by`` is null and this
	returns nothing at all. That is the rule rather than an accident of where it is called: a
	listing marks a row blocked and says *that*, and naming the far end is `#856`'s line.
	:attr:`subroutine.views.Task.blocked_by` carries why the agenda is the argued exception.

	**It was one section of the agenda until `SR#1847`** and is now any blocked row on it,
	Simon's decision of 2026-09-04: `#1846` moved ``overdue`` above *Waiting on somebody else*,
	so a task both blocked and late reported with its **Blocked** mark and without this line —
	the mark carrying the half a reader cannot act on.

	**A line each rather than one line of refs**, because an agent is written
	``@claude-super (agent, @si)`` wherever a surface names a principal (`#1414`) and three of
	those on one line wraps at whatever width the reader's terminal happens to be — which is
	the trap ``_say`` already records. Each line here is short whatever is on it.

	**The ref and who has it, and not the title.** The heading above says what kind of waiting
	this is and the row above says what is waiting; what a reader cannot get from either is
	*whom to chase* and *which item to look at*, and both of those are addressable. A title
	would double the width of every line to save a reader who wants it one ``subroutine show``.

	**Nothing is said where the list is empty**, which is a row whose blockers this reader may
	not see. The section heading has already told them somebody else is holding it up; a line
	saying *somebody you cannot see* would add a sentence and no remedy.
	"""

	if not isinstance(item, subroutine.views.Task) or not item.blocked_by:
		return []

	lines = []

	for end in item.blocked_by:
		line = rich.text.Text("      waiting on ", style=DETAIL)
		line.append(f"#{end.ref}", style=POSITION)

		named = subroutine.views.principal_named(
			end.assignee,
			is_agent=end.assignee_is_agent,
			answers_to=end.assignee_answers_to,
		)

		if named:
			line.append(f"  {named}", style=DETAIL)

		lines.append(line)

	return lines


def _item_line (
	world: World,
	connection: str,
	item: Item,
	*,
	late: bool,
	columns: Columns = NO_COLUMNS,
) -> rich.text.Text:
	"""Return one listing line, addressed by a ref that never changes.

	**The identifier shown is the item's own ref.** It used to be the row's position in the
	last listing, and that was a quiet trap: completing something renumbered everything below
	it, so re-running ``done 1`` after a fresh listing marked a *different* task done — one
	up-arrow away, and wrong without saying so.

	Takes a task **or a document**, because refs are shared between them (§6.2) and a listing
	that showed only one kind told a reader that half the numbers did not exist.

	``columns`` carries how much room each optional cell needs, and **a zero width means the
	column is not shown at all** — see :class:`Columns`. It is the caller's measurement rather
	than this function's because it is a property of the page, not of the row.

	**Not the same rendering as ``views.Task.columns()``, and deliberately so.** That is
	§14.10's compact line, which leads with the status — a word §13.5b forbids on the personal
	path. Two audiences, two renderings; the shared thing is the *rule* about when a column
	earns its place, not the columns themselves.

	Built with :class:`rich.text.Text` rather than markup, because a title is user data: an
	item called ``Fix [bold] handling`` must print as written, not as an instruction.
	"""

	line = rich.text.Text()
	shown = world.address_of_item(connection, item)
	line.append(f"  {shown:>{max(columns.address, 3)}}  ", style=POSITION)

	if columns.kind:
		line.append(f"{item.type:<{columns.kind}}  ", style=DETAIL)

	# **First after the address, because it answers a different question from the rest.** The
	# other cells describe what an item *is*; this one says you are in the middle of it, which
	# is what somebody scanning for "where was I" is looking for.
	if columns.state:
		line.append(f"{_state_cell(item):<{columns.state}}  ", style=DETAIL)

	# Beside `state` because it answers the same kind of question — not what an item *is*,
	# but whether you can act on it now. `state` first: "I am in the middle of this" outranks
	# "something is in the way", and an item can carry both.
	if columns.blocked:
		line.append(f"{_blocked_cell(item):<{columns.blocked}}  ", style=DETAIL)

	# **After `blocked` and before the rest**, because it answers the same question those two
	# do — whether you can act on this now — and the answer here is *yes, and it is a decision
	# rather than work*. An item can carry this and `blocker` at once, which is the ordinary
	# case for a milestone and the reason the two are not one column.
	if columns.sub_tasks_done:
		line.append(f"{_sub_tasks_cell(item):<{columns.sub_tasks_done}}  ", style=DETAIL)

	if columns.priority:
		line.append(f"{_priority_cell(item):<{columns.priority}}  ", style=DETAIL)

	if columns.parent:
		line.append(f"{_parent_cell(item):<{columns.parent}}  ", style=DETAIL)

	if columns.matched:
		line.append(f"{_match_cell(item, columns.term):<{columns.matched}}  ", style=DETAIL)

	if columns.estimate:
		# Right-aligned: durations are read by magnitude and `30m` under `2d` compares at a
		# glance only if the units line up.
		line.append(f"{_estimate_cell(item):>{columns.estimate}}  ", style=DETAIL)

	if columns.assignee:
		line.append(f"{_assignee_cell(item):<{columns.assignee}}  ", style=DETAIL)

	# **Last of the properties, next to the title** (`#512`). Where something lives is read
	# together with what it is called — a reader scanning a mixed backlog asks "what is this,
	# and whose tree is it in" as one question — and it is the widest of these cells, so
	# putting it here keeps the narrow ones aligned against the address.
	if columns.project:
		line.append(f"{_project_cell(item, columns.within):<{columns.project}}  ", style=DETAIL)

	# **Before the title, where a decision is made** (`#595`). A reader scanning for something
	# to open passes the mark on the way to the words, rather than after them.
	if columns.size:
		line.append(f"{_size_cell(item):>{columns.size}}  ", style=DETAIL)

	_append_title(line, item.title, columns.term)

	detail = _when(item)

	if detail:
		line.append(detail, style=LATE if late else DETAIL)

	return line


#: How many children `show` will list. A depth ceiling exists but nothing bounds breadth, and
#: an item with four hundred children should print a number rather than four hundred lines.
MAX_CHILDREN = 50


@dataclasses.dataclass(frozen=True)
class Sections:
	"""Everything ``show`` renders around an item, gathered before anything is printed."""

	links: typing.Sequence[subroutine.views.Link]
	remarks: typing.Sequence[subroutine.views.Comment]
	referring: typing.Sequence[subroutine.views.Backlink]
	proposed: typing.Sequence[subroutine.views.Proposal]
	governing: typing.Sequence[subroutine.views.Governing]
	checked: typing.Sequence[subroutine.views.Verification]
	children: typing.Sequence[subroutine.views.Task]
	events: typing.Sequence[subroutine.views.Event]

	#: Whether the caller asked for the record at all, which `events` cannot say (`#349`). An
	#: empty sequence is what *both* answers look like, and the scripted path has to tell them
	#: apart — carried here rather than passed beside `Sections`, so the two cannot disagree.
	asked_for_history: bool


#: What a section returns when this instance is too old to have the route behind it.
_Section = typing.TypeVar("_Section")


def _if_the_instance_can_answer (
	ask: typing.Callable[[], typing.Sequence[_Section]],
) -> typing.Sequence[_Section]:
	"""Return a section, or nothing where this instance is too old to have its route.

	**A missing route is not a missing item, and this is `#250`'s skew shape** (found within a
	minute of building the first of these, against an instance one commit behind). The program
	and the instance upgrade separately, and upgrading the program first is the ordinary order
	— so a ``show`` that failed outright because one of its sections is newer than the server
	would break the commonest command over the newest one. The item is resolved before any of
	these is called, so a ``not_found`` here can only be the route.

	**Silent rather than noted**, which is the trade and is worth saying out loud: a reader on
	an older instance sees no section rather than a line explaining why. That is a plausible,
	complete, wrong answer — and it is accepted because the program already reports the
	mismatch that causes it, in ``whoami``'s closing line (`#381`), and a second notice on
	every ``show`` would be noise for a state nobody stays in.

	**Written once for two sections rather than twice** (`#1137`). The first version of this
	lived inside the backlinks section, so the second new section would have been a second
	copy of a rule about version skew — and the whole failure it guards against is two things
	that were meant to agree not agreeing.
	"""

	try:
		return ask()

	except subroutine.errors.NotFound:
		return []


def _sections (
	client: subroutine.clients.base.Client, located: Located, *, history: bool
) -> Sections:
	"""Gather what hangs off one item — `#144`, and `#943`'s ratchet is why it is out here.

	**Asked for separately rather than embedded**, because every one is a sub-resource over
	HTTP and pretending otherwise would make the local client the only one that could answer in
	a single call — which is exactly the divergence S3-07 removed.
	"""

	where = {"entity_type": located.entity_type, "workspace": located.workspace}

	return Sections(
		links=client.links(ref=located.ref, **where),
		remarks=client.comments(ref=located.ref, **where),
		referring=_if_the_instance_can_answer(
			lambda: client.backlinks(ref=located.ref, **where)
		),
		proposed=_if_the_instance_can_answer(
			lambda: client.proposed_links(ref=located.ref, **where)
		),
		governing=_if_the_instance_can_answer(
			lambda: client.governing(ref=located.ref, **where)
		),
		# **Only a task**, because only a task is checked. A document reaching this would be
		# asking a question the route does not answer for its kind.
		checked=(
			_if_the_instance_can_answer(
				lambda: client.verifications(
					ref=located.ref, workspace=located.workspace
				)
			)
			if located.entity_type == "task"
			else []
		),
		# **Completed children included**, unlike every listing here. A parent showing two of
		# its four children because the other two are finished would misreport the thing
		# somebody opened it to see. `#84` says report the rollup and leave completion an act;
		# this is where the rollup is read.
		children=(
			client.tasks(
				parent=located.ref,
				workspace=located.workspace,
				limit=MAX_CHILDREN,
				include_completed=True,
				order="ref",
			)
			if located.entity_type == "task"
			else []
		),
		events=(
			client.history(
				ref=located.ref,
				entity_type=located.entity_type,
				workspace=located.workspace,
			)
			if history
			else []
		),
		asked_for_history=history,
	)


def _read_journal (
	program: Program,
	*,
	dated: list[str] | None,
	by: str,
	mine: bool,
	oldest: bool,
	limit: int,
	json_output: bool,
	strict: bool,
) -> None:
	"""What happened over a period, with who did it and what they said — `#1430`.

	**Module level from the first line**, which is `#943`'s ratchet applied before it fires
	rather than after: a new command belongs in a function `register` calls.
	"""

	asked_about = _filters(program, dated)

	with program.opened(strict=strict) as world:

		def ask (
			client: subroutine.clients.base.Client,
		) -> list[subroutine.views.JournalEntry]:
			"""Ask one connection what happened."""

			return client.journal(
				dated=asked_about,
				by=by or None,
				mine=mine,
				oldest=oldest,
				limit=limit,
			)

		gathered = subroutine.fanout.gather(world.clients, ask, strict=strict)

		if json_output:
			program.say(
				json.dumps(
					[
						{"connection": name, **entry.model_dump(mode="json")}
						for name, entry in _across(world, gathered, lambda entries: entries)
					],
					indent=2,
				)
			)

			return

		_say_journal(world, gathered, console=program.console, say=program.say)


def _moved_under (program: Program, *, which: str, under: str, top: bool) -> None:
	"""Make one item part of another, or a top-level item again.

	**Out of `register`'s closure to pay for `subroutine journal`** (`#943`'s ratchet, `#1430`).
	The ratchet's arrangement is that adding a command costs an extraction, so what is added is
	paid for rather than accumulated — and this body needed nothing from the closure that
	`Program` does not carry.
	"""

	# **Neither, or both, is a refusal rather than a default**, which is `project move`'s
	# rule and the endpoint's: an omitted destination that meant "move to the top" would
	# flatten a tree by accident, and there is nothing that records where it was.
	if bool(under) == top:
		program.stop(
			"Say where to move it.",
			"'--under 7' makes it part of #7; '--top' makes it a top-level item.",
		)

	with program.opened() as world:
		# Either kind, because one counter serves both (§6.2) and a document's sections
		# are a tree exactly as a task's subtasks are — so refusing a document here would
		# be turning down half the numbers a reader can see, which is `#44`'s worse half.
		located = _locate(program,
			world,
			_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
			kinds=ANY_ITEM,
			verb="move",
		)
		client = _require_connection(program, world, located.connection)
		kind = (
			"document"
			if isinstance(located.item, subroutine.views.Document)
			else "task"
		)

		parent = None

		if not top:
			# Resolved through the same locator, so an unknown number is refused by the
			# same words rather than by the service, and a document named as a task's
			# parent is turned down for what it is.
			beneath = _locate(program, world, under, kinds=ANY_ITEM, verb="move")

			if isinstance(beneath.item, subroutine.views.Document) != (kind == "document"):
				program.stop(
					f"#{located.item.ref} and #{beneath.item.ref} are not the same kind "
					f"of thing.",
					"A task is part of a task, and a document is part of a document.",
				)

			parent = beneath.item.ref

		changed = client.move(
			ref=located.item.ref,
			parent=parent,
			entity_type=kind,
			workspace=located.workspace,
		)

		where = "a top-level item" if top else f"part of #{parent}"

		program.say(_acted(world, dataclasses.replace(located, item=changed), f"Now {where}"))


def _what_moved (
	program: Program,
	*,
	since: int | None,
	mine: bool,
	by: str,
	dated: list[str] | None,
	limit: int,
	json_output: bool,
	strict: bool,
) -> None:
	"""What has moved, across every connection this machine can reach.

	**Out of `register`'s closure to pay for `--filter`** (`#943`'s ratchet, `#1431`). That
	ratchet's rule is that a new command belongs in a function `register` calls rather than in
	the closure, and an option on an existing command is the same bill arriving in instalments
	— sixteen lines here took it sixteen over. Extracting the body rather than the option is
	what makes the next one free.
	"""

	# **Read before the connections are opened**, like every other command that takes one:
	# a misspelt filter is a refusal about what somebody typed, and making them wait for a
	# network round trip to hear it would be answering a local question remotely.
	asked_about = _filters(program, dated)

	with program.opened(strict=strict) as world:
		# **A number belongs to one instance.** Every connection counts its own events from
		# one, so resuming from 412 against two of them would mean two different places in
		# two different histories — and the half that was wrong would look like an ordinary
		# quiet week rather than an error.
		if since is not None and len(world.reached) > 1:
			program.stop(
				"'--since' needs one connection, and this machine can reach "
				f"{len(world.reached)}.",
				"Each one counts its changes separately, so a number means nothing to "
				"the others. Run it against one at a time.",
			)

		def ask (client: subroutine.clients.base.Client) -> list[subroutine.views.Event]:
			"""Ask one connection what has moved."""

			# **The newest page unless resuming.** Somebody typing this for the first
			# time against a long history wants this morning, not the instance's first
			# afternoon — and `--since` is what says they have a place already.
			return client.changes(
				since=since,
				mine=mine,
				by=by or None,
				newest=since is None,
				limit=limit,
				dated=asked_about,
			)

		gathered = subroutine.fanout.gather(world.clients, ask, strict=strict)

		if json_output:
			program.say(
				json.dumps(
					[
						{"connection": name, **event.model_dump(mode="json")}
						for name, event in _across(world, gathered, lambda events: events)
					],
					indent=2,
				)
			)

			return

		_say_changes(world, gathered, console=program.console, say=program.say)


def _shown_item (
	program: Program,
	world: World,
	located: Located,
	gathered: Sections,
	*,
	client: subroutine.clients.base.Client,
	tree: bool,
	json_output: bool,
) -> None:
	"""Write one item out, on whichever of the two paths was asked for.

	**Out of `register`'s closure to pay for `--tree`** (`#943`'s ratchet, `#1358`). The rule
	that ratchet enforces is that a command's body belongs in a function it calls, and the
	pattern has not varied: what a feature pays is the cost of noticing.
	"""

	# **Asked only when wanted**, because it is a walk: three queries per level against nothing
	# at all for a reader who did not ask. `history` is fetched the same way and for the same
	# reason.
	walked = (
		client.beneath(
			ref=located.ref,
			entity_type=located.entity_type,
			workspace=located.workspace,
		)
		if tree
		else []
	)

	if json_output:
		program.say(
			json.dumps(
				_shown_as_json(world, located, gathered, walked=walked if tree else None),
				indent=2,
			)
		)

		return

	_render_item(world, located, gathered, console=program.console)

	if tree:
		_render_tree(program.console, walked)

	program.say("")

	# **What to do next depends on where it is** (`#700`). Inviting somebody to comment on
	# something in the trash offers the one act that changes nothing anybody will read;
	# `restore` is the question they actually have, and it is the same command `list --trash`
	# already ends with, so the two agree about what a deleted row is for.
	addressed = world.address_of_located(located).replace(subroutine.domain.refs.SIGIL, "")

	_suggest(
		program.console,
		f"subroutine restore {addressed}"
		if located.item.deleted_at is not None
		else f'subroutine comment {addressed} "what happened"',
		"put it back" if located.item.deleted_at is not None else None,
	)


def _joined (
	program: Program,
	world: World,
	*,
	which: str,
	other: str,
	relation: str,
) -> None:
	"""Join every item named on the left to every one named on the right — `#1352`.

	**Every one on both sides is resolved before any of them is written**, which is `project
	rename`'s precedent: count what will happen and name what will break before doing any of
	it. Nothing here spans a transaction — each link is its own call, and over HTTP its own
	request — so a typo in the fourth of five would otherwise leave three made, one refused
	and no statement of which.

	The commonest failure is a ref that does not resolve, and this catches all of those. What
	it cannot catch is a link the service refuses for a reason only it knows, which is why the
	report says what was made rather than assuming.

	**Outside `register` because that closure only shrinks**, which is the ratchet's own
	instruction: a command's body belongs in a function it calls.
	"""

	near = [
		_locate(program, world, one, kinds=ANY_ITEM, verb="link")
		for one in _several(program, which)
	]
	far = [
		_locate(program, world, one, kinds=ANY_ITEM, verb="link")
		for one in _several(program, other)
	]
	where = world.writing_to()

	for source in near:
		for target in far:
			made = where.client.link(
				ref=source.ref,
				link_type=relation,
				target=target.ref,
				entity_type=source.entity_type,
				target_type=target.entity_type,
				workspace=source.workspace,
			)

			# **Both ends named once there is more than one source** (`#1190`'s argument at
			# width): `Blocks: Tag it` is unambiguous from one item and says nothing about
			# which of six it came from.
			program.say(
				f"{made.label}: {made.other.title}"
				if len(near) == 1
				else f"#{source.ref} {made.label} #{made.other.ref}  {made.other.title}"
			)

	_suggest(
		program.console,
		f"subroutine show {_typeable(world, near[0].connection, near[0].item)}",
		"see everything it is joined to",
	)


def _relation_key (relation: str) -> str:
	"""Return the stored key for a relation somebody typed at a terminal.

	Hyphens read better than underscores at a command line and the seeded keys use
	underscores, so both spellings are accepted rather than making anybody guess which.

	**Here rather than inline, because there are two callers now** (`SR#1637`). ``link`` carried
	this as one expression in its command body; ``unlink`` gaining ``--type`` would have made it
	two copies of one rule, which is the defect this codebase finds most often — and they would
	have agreed until somebody changed one, at which point ``link derives-from`` and
	``unlink --type derives-from`` would disagree about the same word.

	**A terminal convenience rather than a domain rule**, which is why it lives here: the
	service takes the key as it is stored, and nothing over HTTP or MCP accepts the hyphen.
	"""

	return relation.strip().replace("-", "_")


def _unjoined (
	program: Program, world: World, *, which: str, other: str, relation: str = ""
) -> None:
	"""Undo the links between one item and each of the items named — `#1352`.

	**Found by the pair rather than asked for by id.** A link's id is a UUID that appears in no
	listing a person reads, so requiring one would make this a command only a script could run
	— and `show` prints the two refs, which is what somebody actually has in front of them.

	**Asked once and matched against every target**, rather than once per target: the answer is
	the same list each time, and re-fetching it would turn one call into N for a command whose
	whole subject is that N calls are too many.

	**Every end resolved before any link is withdrawn**, the same rule :func:`_joined` follows
	and for the same reason.
	"""

	near = _locate(program, world, which, kinds=ANY_ITEM, verb="unlink")
	far = [
		_locate(program, world, one, kinds=ANY_ITEM, verb="unlink")
		for one in _several(program, other)
	]
	where = world.writing_to()

	held = where.client.links(
		ref=near.ref, entity_type=near.entity_type, workspace=near.workspace
	)
	joins = {
		target.ref: [one for one in held if one.other.ref == target.ref] for target in far
	}
	missing = [target for target in far if not joins[target.ref]]

	if missing:
		# **The shortest address that resolves, not the absolute one.** A refusal is written
		# when something has already gone wrong and is the last output anybody re-reads for
		# stray vocabulary — printing `personal/#1` at somebody with one workspace introduces
		# the word in an error message, about a to-do list. Same §1.4 leak `_in_place` exists
		# for.
		#
		# **Named all at once**, because undoing a mistaken batch is exactly when more than one
		# of them will already be gone, and one refusal per run is a command somebody has to
		# run five times to learn five things.
		program.stop(
			f"{world.address_of_located(near)} is not joined to "
			+ ", ".join(world.address_of_located(one) for one in missing)
			+ ".",
			f"Run 'subroutine show {near.ref}' to see what it is joined to.",
		)

	if relation:
		wanted = _relation_key(relation)
		joins = {
			ref: [one for one in links if one.link_type == wanted]
			for ref, links in joins.items()
		}
		nothing_of_that_kind = [target for target in far if not joins[target.ref]]

		if nothing_of_that_kind:
			program.stop(
				f"{world.address_of_located(near)} has no {wanted!r} link to "
				+ ", ".join(
					world.address_of_located(one) for one in nothing_of_that_kind
				)
				+ ".",
				f"Run 'subroutine show {near.ref}' to see what it is joined to.",
			)

	# **Refused rather than resolved, where a pair carries more than one link** (`SR#1637`).
	# Both defaults are wrong half the time: removing every link destroys a statement somebody
	# meant to keep, and removing one leaves a command that did less than it said. That is
	# ``db restore``'s rule — when neither answer is right, refuse and let the caller say —
	# and it applies harder here, because the report was *singular either way* and the
	# information needed to notice the loss is gone by the time anybody could look.
	#
	# **The no-verb design survives where it was argued for.** ``unlink``'s help is right that
	# an unwanted link is worse than a missing one, so somebody undoing a mistake should not
	# have to remember the relation — and with one link between the pair, which is the ordinary
	# case, they still do not.
	ambiguous = [target for target in far if len(joins[target.ref]) > 1]

	if ambiguous:
		program.stop(
			f"{world.address_of_located(near)} has more than one link to "
			+ ", ".join(world.address_of_located(one) for one in ambiguous)
			+ ", so this would remove more than one thing.",
			# **Offered in the spelling this command's own help lists**, which is hyphens
			# (`SR#1547`): both are accepted, and printing the stored key here would show a
			# reader a third spelling of a word they have already met twice.
			"Say which with --type: "
			+ ", ".join(
				sorted(
					{
						one.link_type.replace("_", "-")
						for target in ambiguous
						for one in joins[target.ref]
					}
				)
			)
			+ ".",
		)

	for target in far:
		for one in joins[target.ref]:
			where.client.unlink(
				ref=near.ref,
				link_id=str(one.id),
				entity_type=near.entity_type,
				workspace=near.workspace,
			)

		# **The relation, not only the title.** The old line was the same sentence whether it
		# removed one edge or five and named the item rather than what was withdrawn, so there
		# was no reading of it that revealed a loss.
		program.say(
			f"Unlinked: {joins[target.ref][0].label} {joins[target.ref][0].other.title}"
		)

	_suggest(program.console, f"subroutine show {_typeable(world, near.connection, near.item)}")


def _render_tree (
	console: rich.console.Console,
	walked: typing.Sequence[subroutine.views.Beneath],
) -> None:
	"""Draw what has to happen first, indented by how deep it sits — `#1358`.

	**Indentation rather than box-drawing**, which is `#63`'s decision read for the case it
	excluded. That rule refuses ``└─`` under a *listing*, because a listing is ordered by
	recency or by priority so a child is rarely next to its parent and the glyph would state a
	relationship that is not there. This is ordered by the tree, so the indentation is the
	relationship — the same reasoning ``project list`` already follows.

	**Dimmed rather than removed or ticked**, exactly as a finished blocker is in the links
	section above: the point of the line is seeing what the thing *is*, and hiding a finished
	one hides the contents of a finished milestone. Decision `#102` besides — no information
	exists only in a colour, and the count on the heading carries it.

	**Silent when there is nothing**, like every other section here (§12.2c).
	"""

	if not walked:
		return

	# **A deleted part is out of the count and stays on the page** (`#1403`), which is the rule
	# the links section above follows: the total has to be one a reader can reconcile, and an
	# absence they would have to infer is worse than a mark.
	#
	# **And so is a second drawing of something already above** (`#1410`). The question the
	# heading answers is *how much of this is left*, and one item finished once is finished —
	# counting a shared blocker twice inflates the plan. Measured on the real roadmap: 56
	# drawings, 29 of them repeats. The mark on the row is what explains the difference, which
	# is the same bargain the deleted rows make.
	counted = [
		one for one in walked if one.item.deleted_at is None and one.stopped != "again"
	]
	done = sum(1 for one in counted if one.item.is_complete)

	console.print("")
	console.print(
		rich.text.Text(
			f"What has to happen first ({done} of {len(counted)} done)", style=HEADING
		)
	)

	for one in walked:
		line = rich.text.Text()
		line.append("  " + "  " * one.depth, style=DETAIL)
		line.append(
			f"{subroutine.domain.refs.format_ref(one.item.ref):>4}  ", style=POSITION
		)
		line.append(
			one.item.title,
			style=DETAIL if one.item.is_complete or one.item.deleted_at else "",
		)

		if one.item.deleted_at is not None:
			line.append("  (deleted)", style=DETAIL)

		# **What is not drawn is said**, and the two reasons are different questions. *again*
		# means it is above and its parts are drawn there; *deeper* means the walk stopped and
		# there may be more. A tree silently truncated answers *is this the order I meant* with
		# a yes it has not earned.
		if one.stopped is not None:
			line.append(
				"  (shown above)" if one.stopped == "again" else "  (more below this)",
				style=DETAIL,
			)

		console.print(line)


def _render_item (
	world: World,
	located: Located,
	gathered: Sections,
	*,
	console: rich.console.Console,
) -> None:
	"""Print one item in full: what it is, what it is joined to, what happened to it.

	**A field nobody set is not printed, and neither is a default nobody chose.** That is the
	rule that lets this command exist at all without breaking §1.4: ``show`` on a personal
	to-do item prints its number, its title and the day it is planned for, and says nothing
	about a status, a project or a type — because none of those were asked for and the
	answers would all be "the default". The same command on an agent's bug prints every one
	of them, because there each carries a decision somebody made.

	The consequence worth stating: this output *grows* with how much the user has told the
	system, which is the shape §1.4 asks for and the opposite of a form with empty fields.
	"""

	links = gathered.links
	remarks = gathered.remarks
	referring = gathered.referring
	proposed = gathered.proposed
	governing = gathered.governing
	checked = gathered.checked
	children = gathered.children
	events = gathered.events

	# **A comment's day and an event's day are the reader's, not the server's** (`#1091`).
	# Both were ``.date()`` on the stored instant, which is UTC — so a comment written at
	# nine in the evening in Auckland was reported as having happened the next day.
	zone = world.account_zone(located.connection, located.workspace)

	shown = world.address_of_located(located)
	heading = rich.text.Text()
	heading.append(f"{shown}  ", style=POSITION)
	heading.append(located.title, style=HEADING)
	console.print(heading)

	facts = _facts(located)

	if facts:
		console.print(rich.text.Text(f"  {' · '.join(facts)}", style=DETAIL))

	# **The other direction, and it needs its own line rather than a fact.** `^57` in the
	# facts row would be true and unreadable — the reason to name a parent is to say what
	# this is part *of*, which is a title. The heading mirrors `Parts` below, so the two
	# directions of one relationship read as one relationship.
	item = located.item

	if isinstance(item, subroutine.views.Task) and item.parent_ref is not None:
		belongs = rich.text.Text()
		belongs.append("  part of ", style=DETAIL)
		belongs.append(
			f"{subroutine.domain.refs.format_ref(item.parent_ref)}  ", style=POSITION
		)
		belongs.append(item.parent_title or "", style=DETAIL)
		console.print(belongs)

	body = (
		located.item.description
		if isinstance(located.item, subroutine.views.Task)
		else located.item.body
	)

	if body:
		console.print("")

		# As written, never as markup: a description is user data, and one containing
		# ``[bold]`` must print those characters rather than obey them.
		console.print(rich.text.Text(body))

	if children:
		done = sum(1 for child in children if child.completed_at is not None)

		console.print("")
		console.print(
			rich.text.Text(f"Sub-tasks  ({done} of {len(children)} done)", style=HEADING)
		)

		for child in children:
			row = rich.text.Text()
			row.append(
				f"  {subroutine.domain.refs.format_ref(child.ref):>5}  ", style=POSITION
			)

			# A finished child is dimmed rather than removed or ticked: the rollup above
			# already carries the count, and what this line is for is seeing what the parts
			# *are*.
			row.append(child.title, style=DETAIL if child.completed_at else "")
			console.print(row)

	if governing:
		# **What binds whoever picks this up** (`#1119`), and it is the workspace-wide *what
		# is in force here* narrowed to one item. Printed **above** the links rather than among
		# them, because it is the one section somebody has to read before doing anything and
		# the links are what they read afterwards.
		#
		# **From typed links only** (`#1124` Q2, Simon's). Filed nearby and mentioned in
		# passing mean *near this*, which is a different claim — and answering it under this
		# heading is how a reader learns not to trust the heading.
		console.print("")
		console.print(rich.text.Text("Read first", style=HEADING))

		for binds in governing:
			row = rich.text.Text()
			row.append(
				f"  {subroutine.domain.refs.format_ref(binds.document.ref):>6}  ",
				style=POSITION,
			)
			row.append(f"{binds.document.type or '':<9}  ", style=DETAIL)
			row.append(binds.document.title)
			console.print(row)

	if links:
		# **The rollup `#84` specified and nothing built** (`#210`). A milestone is modelled as
		# an item whose blockers are its contents, "presented GitHub-style as N of M" — and
		# every link printed identically, so `show 85` reported forty-eight *finished* blockers
		# as forty-eight outstanding ones. The item somebody opens to ask whether a release is
		# ready said the opposite of the truth about it.
		#
		# Counted over the blockers alone. A `relates to` has nothing to be N of.
		blockers = [
			link
			for link in links
			# **The category, never the key** — what a relation *is*, never what it is called (decision `#1157`). Comparing `link_type` to the literal `blocks` kept working while `#1156` broke: a workspace that renames the key keeps every label and loses every count.
			if link.link_category == subroutine.domain.readiness.GATING
			and link.direction == "incoming"
			# **A blocker in the trash is not one** (`#1403`). It held a milestone at `0 of 6`
			# with one of the six deleted, so the milestone could never reach 6 of 6 and
			# nothing on the page said why. `readiness.unblocked` has excluded a deleted
			# blocker since it was written; this is the count catching up with the rule.
			and link.other.deleted_at is None
		]
		done = sum(1 for link in blockers if link.other.is_complete)
		rollup = f"  ({done} of {len(blockers)} blockers done)" if blockers else ""

		console.print("")
		console.print(rich.text.Text(f"Links{rollup}", style=HEADING))

		width = max(len(link.label) for link in links)

		for link in links:
			line = rich.text.Text()
			line.append(f"  {link.label:<{width}}  ", style=DETAIL)
			line.append(
				f"{subroutine.domain.refs.format_ref(link.other.ref):>4}  ", style=POSITION
			)

			# Dimmed rather than removed or ticked, exactly as a finished part is above: the
			# rollup carries the count, and what this line is for is seeing what the thing at
			# the other end *is*. Removing it would hide the contents of a finished milestone.
			line.append(
				link.other.title,
				style=DETAIL if link.other.is_complete or link.other.deleted_at else "",
			)

			# **Said rather than left to be inferred** (`#1403`, §12.2a). A deleted blocker is
			# out of the count above, and a row that is simply absent from a total nobody can
			# reconcile is the thing that made this unfindable. `restore` puts it back, which
			# is why the row stays.
			if link.other.deleted_at is not None:
				line.append("  (deleted)", style=DETAIL)

			console.print(line)

	if referring:
		# **What refers to this, and it is not a link** (`#144`). A link is an assertion
		# somebody made about two items; a mention only records that one piece of writing
		# talks about another (§6.15). They are separate sections for that reason and not
		# merged into one — a reader deciding whether something is safe to close needs to know
		# which of the two they are looking at.
		#
		# **Silent when there are none**, like every other section here: §12.2c's rule that a
		# field nobody set is not printed, applied to a whole heading.
		console.print("")
		console.print(rich.text.Text(f"Referred to by ({len(referring)})", style=HEADING))

		for one in referring:
			line = rich.text.Text()
			line.append(
				f"  {subroutine.domain.refs.format_ref(one.ref):>6}  ", style=POSITION
			)

			# **`in a comment` where the sentence is not in that item's own prose.** A reader
			# who opens #42 and cannot find the number has been sent to the wrong half of it.
			line.append(f"{'in a comment' if one.via else '':<13}", style=DETAIL)
			line.append(one.title)
			console.print(line)

	if proposed:
		# **What the writing suggests, and nobody has said so** (`#1137`). Separate from the
		# links above and phrased as a suggestion, because that is the whole of respecting the
		# decision underneath it: *what governs this* answers from links somebody made, and a
		# citation is evidence that one belongs rather than the thing itself. A sentence citing
		# a decision can as easily mean *this contradicts it*.
		#
		# **Silent when there are none**, like every other section here, so a personal to-do
		# list never grows a heading about governance.
		console.print("")
		console.print(
			rich.text.Text(
				f"Not linked, but its writing suggests ({len(proposed)})", style=HEADING
			)
		)

		for suggestion in proposed:
			line = rich.text.Text()
			line.append(
				f"  {subroutine.domain.refs.format_ref(suggestion.other.ref):>6}  ",
				style=POSITION,
			)
			line.append(suggestion.other.title)
			line.append(f"  ({suggestion.because})", style=DETAIL)
			console.print(line)

		# **The governing end first, and a fixed order was `SR#1609`.** ``confirmed_as`` decides
		# it once for both surfaces, because a swap written here and again in `mcp` is two
		# copies of one rule about a link neither renderer can show is backwards.
		source, target = proposed[0].confirmed_as(located.ref)

		_suggest(
			console,
			f"subroutine link {source} "
			f"{proposed[0].link_type.replace('_', '-')} {target}",
			"confirm one",
		)

	if checked:
		# **What was checked, and it is a record rather than a proof** (`#1121`). Somebody can
		# post an exit code of zero without having run anything, so the heading says *recorded*
		# and never *verified* — `#593` settled that sentence and this is where a person meets
		# it. What the record is worth is being durable, attributable and invalidatable.
		#
		# **The tree, not the clock.** A record naming no tree cannot expire and says so, which
		# is a different answer from being current — and §1.4 requires it to be possible,
		# because most machines have no checkout.
		console.print("")
		console.print(rich.text.Text(f"Recorded checks ({len(checked)})", style=HEADING))

		for record in checked:
			line = rich.text.Text()
			line.append(
				f"  {'passed' if record.passed else 'failed':<7}  ",
				style="" if record.passed else LATE,
			)
			line.append(
				f"{subroutine.views.moment_day(record.ran_at, zone):<11}  ",
				style=DETAIL,
			)
			line.append(record.summary or "")
			line.append(
				f"  ({'tree ' + record.tree_hash[:7] if record.tree_hash else 'no tree'})",
				style=DETAIL,
			)
			console.print(line)

	if remarks:
		# **The count is always shown; the bodies are bounded** (`#37`, Simon's request). Every
		# comment in full is right for the three or four an item usually has and wrong for the
		# hundred it might accumulate — a reader asking "what is this" gets a wall of history.
		# Printing only the newest few and heading the section with the count answers "is there
		# more" without a second command, which is what `#33` did for the listing.
		#
		# Simon was content for the bodies not to be printed at all. Keeping the recent ones is
		# a deliberate departure: removing a working display is a regression for every item in
		# this instance, none of which has more than a handful.
		recent = remarks[-COMMENTS_SHOWN:]
		rollup = (
			"" if len(recent) == len(remarks) else f" ({len(remarks)}, showing {len(recent)})"
		)

		console.print("")
		console.print(rich.text.Text(f"What happened{rollup}", style=HEADING))

		for remark in recent:
			line = rich.text.Text()
			line.append(f"  {subroutine.views.moment_day(remark.created_at, zone)}  ", style=DETAIL)

			# **Who, beside when** (`#636`). A record of what happened with the names cut out
			# is half a record, and it matters more here than the count of accounts suggests:
			# five of this instance's eight are service accounts, so *who wrote this* is the
			# difference between a colleague's note and a machine's (`#759`'s argument, on the
			# surface that still lacked it).
			#
			# **Printed on every line rather than dropped when uniform**, which is where this
			# parts company with §12.2a. That rule drops a column saying the same thing on
			# every row because the reader can see the whole page and lose nothing; a name
			# cannot be inferred from its own absence, so dropping it answers *nobody* rather
			# than *the same person throughout*.
			if remark.author is not None:
				line.append(f"@{remark.author}  ", style=DETAIL)

			line.append(remark.body)
			console.print(line)

	# **Last, behind a flag, and newest first.** A comment is what somebody wrote and belongs
	# in the reading order of the item; a history is what the system recorded, it is unbounded,
	# and most of the time the answer is "it was created and nothing else has happened" — which
	# is §1.4's rule about a default nobody chose, applied to a whole section. `--history` is
	# what somebody asks for when the question is "why does this say that".
	if events:
		console.print("")
		console.print(rich.text.Text("History", style=HEADING))

		for event in events:
			line = rich.text.Text()
			line.append(f"  {subroutine.views.moment_day(event.created_at, zone)}  ", style=DETAIL)
			line.append(_event_line(event))
			console.print(line)


def _event_line (event: subroutine.views.Event) -> str:
	"""Return one event as a sentence somebody can read.

	**In `views.happened` rather than here** (`#1115`). It was written out here and again in
	`mcp/tools.py`, identically — and identically wrong, reading `subject_type is not None` as
	*this is a comment* where it means *this is about something other than the item*. Links set
	it too, so an item's history called every link a conversation, on both surfaces, agreeing.
	"""

	return subroutine.views.happened(event)


def _facts (located: Located) -> list[str]:
	"""Return the things worth saying about an item beyond its title, and nothing more.

	Each entry earns its place by having been *chosen*. A task of the default type says
	nothing about its type; one filed in the Inbox says nothing about its project; a status
	in the ``open`` category is the absence of news and is left out, while a completed one is
	reported as a date because that is the fact somebody wants.
	"""

	facts: list[str] = []
	item = located.item

	# **Asked rather than hardcoded** (`#1135`). This read `item.type not in ("task", "note")`,
	# which is the right *question* — is this the type nobody chose — answered by naming the two
	# keys this installation's seeder happens to use. `ItemType.is_default` has always held the
	# answer; the item view simply did not carry it, so the two keys were the only thing to
	# reach for.
	#
	# **Latent rather than live, until `#1129`.** Nothing can rename a type today, so the keys
	# are correct now — and a workspace whose default task type is `story` would print `story` on
	# every line of every listing, which is precisely the rule this exists to keep.
	#
	# **Against an instance too old to send it, every item says its type.** The field defaults to
	# `False` exactly as `status_is_default` does, so an older body reads as *nothing here is the
	# default*. Noise rather than loss, which is the right way round to degrade: the alternative
	# default hides a `bug` label, and `whoami` already reports the mismatch.
	if not item.type_is_default:
		facts.append(item.type)

	# **A status somebody chose, and silence about the one everything starts in** (`#168`,
	# Simon 2026-08-01). This printed nothing at all, so `update 5 --status blocked` answered
	# "Changed" and then no surface in the product would ever mention it again — a clean-room
	# tester assumed it had not saved.
	#
	# The rule moved to `views.status_is_news` when `#841` made it a third caller. It was
	# written out here and in `mcp/tools._more` identically, which is fine for two and is how
	# every duplicated rule in this codebase started.
	if subroutine.views.status_is_news(item):
		# **The workspace's own word, falling back to the key** (`#1717`). This printed
		# `needs_input` — snake_case, reading as a database field — while the browser's control
		# beside it said *Needs input*. Three of the four seeded task statuses are single words
		# whose key passes for a name, which is why it took a fifth to notice.
		#
		# **The fallback carries a real case rather than being defensive**: a client is
		# upgraded before an instance is, and one that is a release behind sends no label.
		facts.append(item.status_label or item.status)

	if isinstance(item, subroutine.views.Task):
		# **`_priority_cell`, not a second literal.** This printed `!4/u3` where the listing
		# printed `!4/3`, and only the listing's spelling is one §6.13 accepts — so a reader
		# who retyped what `show` had just shown them got it verbatim in the title with no
		# priority set (`#151`). Two strings that agree is what these were doing until one of
		# them was edited.
		if _priority_cell(item):
			facts.append(_priority_cell(item))

		if item.estimate_minutes is not None:
			facts.append(subroutine.domain.durations.humanize(item.estimate_minutes))

		# **What a subscribed calendar will remind about, and when** (`#1211`). Said here
		# rather than on a listing row for `#819`'s rule: a reminder changes nothing about
		# which item to pick up, and 1 of 136 tasks on this instance carries one.
		# **`reminder_human`, not `humanize(reminder_minutes)`.** The view already carries the
		# rendered form and both surfaces read the same one, so there is no second copy of the
		# grammar to drift — which is what `estimate_minutes` beside it needs an excuse entry
		# in `tests/test_mcp.py` for, and this does not.
		if item.reminder_human is not None:
			facts.append(f"reminds {item.reminder_human} before")

		# **Reported whether or not it has passed**, unlike `_when` below. A defer somebody
		# set is a decision they made, and one that has since come round is still the answer
		# to "why was this not on my list in June" — where a field that erased itself on
		# arrival would leave that question permanently unanswerable.
		if item.snoozed_until is not None:
			facts.append(f"from {_when_rendered(item)}")

		if item.due_at is not None:
			facts.append(
				"due "
				f"{_render_moment(item.due_at, item.timezone, all_day=item.due_is_all_day)}"
			)

		if item.starts_at is not None:
			# **One fact when there are two dates, not two** (`#576`). *starts 14 Aug · until
			# 28 Aug* reads as two unrelated things; a span is one, and it is what somebody
			# typed. The start is still printed alone when there is no end, which is every
			# ordinary task.
			#
			# **An end shares the start's flag**, because it has none of its own (decision
			# `#1235` §2) — so a span is timed at both ends or at neither, and one call
			# answers for both.
			started = _render_moment(
				item.starts_at, item.timezone, all_day=item.starts_is_all_day
			)
			facts.append(
				f"starts {started}"
				if item.ends_at is None
				else f"{started} to "
				f"{_render_moment(item.ends_at, item.timezone, all_day=item.starts_is_all_day)}"
			)

		if item.recurrence_rule is not None:
			facts.append(
				subroutine.domain.recurrence.describe(
					item.recurrence_rule, anchor=item.recurrence_anchor
				)
			)

		if item.completed_at is not None:
			facts.append(f"done {_render_date(item.completed_at, item.timezone)}")

		# **Who has it** (`#511`). A field somebody chose, so it meets this function's own rule
		# — and it was the one chosen field with no surface at all: `update --assignee jo`
		# answered "Changed" and then `show` printed the priority, the deadline and the tags
		# and never mentioned jo. `#168`'s defect exactly, three lines below `#168`'s comment.
		if item.assignee:
			facts.append(
				subroutine.views.principal_named(
					item.assignee,
					is_agent=item.assignee_is_agent,
					answers_to=item.assignee_answers_to,
				)
			)

		# **The fact that is not a choice, and the one that changes what the others mean**
		# (`#921`). A series and its occurrence carry the same title, so once `#921` made the
		# template's ref resolve, `show 1` and `show 2` rendered identically and nothing said
		# which was which. ``views.Task.is_template``'s own comment already named this as its
		# job — "the only thing that explains why a row with a ref appears in no listing" — and
		# until now no rendering read it, so the explanation existed and reached nobody.
		#
		# **Inside the task block, read as an attribute, and that is not a style choice.** It
		# was ``getattr(item, "is_template", False)`` out below, which works and is *invisible
		# to `#674`'s guard*: that scan reads ``item.<field>`` to derive what each rendering
		# shows, so a read spelled as a lookup by string is one it cannot see. Deleting the
		# agent's copy left 508 tests green. A task-only fact belongs in the task block anyway,
		# which is what makes the plain attribute available.
		if item.is_template:
			facts.append(subroutine.views.THE_SERIES)

		# **The other half of the same explanation** (`#1247`). The template's own ref was
		# reachable — `show 5` works and says *the repeat itself* — and printed by nothing, so
		# somebody who had renamed this occurrence and watched the correction come back wrong
		# next month had no number to act on. Measured on a disposable instance: three faces of
		# one defect, and this is the one that costs a line.
		elif item.recurrence_template_ref is not None:
			facts.append(
				f"{subroutine.views.FROM_THE_REPEAT} "
				f"{subroutine.domain.refs.format_ref(item.recurrence_template_ref)}"
			)

	# **Outside the task block since `#819`**, because both kinds carry tags now and from one
	# vocabulary. It sat inside for as long as only a task could have them — so a document
	# tagged through the API rendered nothing here, which is the *stored and shown nowhere*
	# half of the same defect. Last of the task facts either way, so nothing about a task's
	# line moved.
	if item.tags:
		facts.extend(f"#{tag}" for tag in item.tags)

	# The project only when it is one somebody filed this in. The Inbox is where things go
	# when nobody said, so naming it would be reporting the absence of a decision.
	#
	# **The whole address since `#512`**, because a key stopped naming one project: a reader
	# who learns `substation/dist` from a listing looks for it on the item, and `dist` alone
	# would be a different claim from the one the listing made.
	if item.project_path and item.project_path.lower() != "inbox":
		facts.append(item.project_path)

	# **Last, and it is the one fact here that is not about a choice somebody made** (`#700`).
	# Everything above earns its place by having been chosen; this earns it by changing what
	# all of them mean. An item in the trash rendered exactly like a live one — no marker, no
	# date — so it could be read, acted on, and never known to have been deleted.
	#
	# ``getattr`` for the zone because a document has none: §6.14 says a document is not
	# scheduled, so it carries no timezone and `_render_date` falls back to the default.
	if item.deleted_at is not None:
		facts.append(f"deleted {_render_date(item.deleted_at, getattr(item, 'timezone', None))}")

	# **After the deletion, because it is a fact about the text rather than about the item**
	# (`#1768`). Everything above says what this *is*; this says the body you are reading has
	# been replaced, which is what decides whether to trust a comment written under an
	# earlier draft.
	#
	# **Absent on a first draft**, which is this function's own rule read one field along: an
	# item nobody has revised has nothing to report, exactly as an unranked one shows no
	# priority. `views.Revisions` is null in that case rather than a count of zero.
	#
	# **Null also means nobody asked**, and at this surface nobody ever does not: `show` is
	# the only caller and it always resolves it. A listing leaves it unresolved and renders
	# no facts at all, so the two cannot be confused here.
	revisions = getattr(item, "revisions", None)

	if revisions is not None:
		facts.append(
			subroutine.views.revised_in_words(
				revisions,
				when=_render_date(revisions.last_at, getattr(item, "timezone", None)),
			)
		)

	return facts


#: How far either side of today a bare date can be read without a year. **The two together are
#: deliberately narrower than a year** (331 days), and that is the whole argument: inside a
#: window shorter than 365 days a rendering like "Tue 30 Nov" can only name one date, and
#: outside it, it names at least two. A wider window would not be a friendlier default, it
#: would be an ambiguous one.
#:
#: A month back covers ordinary overdue work without putting a year on something three days
#: late. Ten months forward covers everything anybody schedules routinely.
_A_BARE_DATE_READS_BACK = datetime.timedelta(days=31)
_A_BARE_DATE_READS_FORWARD = datetime.timedelta(days=300)


def _dated (day: datetime.date, *, today: datetime.date | None = None) -> str:
	"""Render a calendar date, with a year only when a bare one would be ambiguous (`#78`).

	``%a %-d %b`` and never a year meant a deadline in 2027 printed exactly as one this
	November — "due Tue 30 Nov" either way, with nothing in the line to tell them apart. Found
	while writing an exact assertion for a 2020 date and being unable to.

	The year **earns its place** rather than always appearing, which is the same rule the
	compact line's columns follow: a to-do list where every date carries a year it does not
	need is one that looks like a database (§1.4). An ordinary list is unchanged by this.

	``today`` is injectable because the alternative is a test that passes for ten months of the
	year, and this is precisely the kind of thing that would be written in July and start
	failing in June.
	"""

	now = today if today is not None else datetime.date.today()
	bare = day.strftime("%a %-d %b")

	if now - _A_BARE_DATE_READS_BACK <= day <= now + _A_BARE_DATE_READS_FORWARD:
		return bare

	return f"{bare} {day.year}"


def _is_late (item: Item, *, now: datetime.datetime) -> bool:
	"""Report whether this row's deadline has passed — `#1243`.

	**Asks the domain rather than comparing here.** §6.5 stores an all-day deadline at the last
	microsecond of its day precisely so that *due all day Friday* is not late on Friday morning,
	and a second copy of ``due_at < now`` written at a terminal is the copy that would drift
	from it.

	**A document is never late**, because it has no deadline to pass: ``_when`` says the same
	thing one function down, and for the same reason.
	"""

	if not isinstance(item, subroutine.views.Task):
		return False

	return subroutine.domain.schedule.is_overdue(item, now=now)


def _happens (item: Item) -> bool:
	"""Report whether this happens to you rather than being work you could finish.

	Decision `#1235`. **The type's category, never its key** — a workspace may rename ``event``
	or add ``holiday`` beside it through `#1129`, and a rule comparing the label is `#1156`.

	Read here so a surface can decline to *suggest* one: the agenda's closing tip is
	``subroutine done``, and the measured defect was the product advising a reader to tick off
	somebody's birthday. Completing one is still accepted — a refusal is a wall — it is simply
	never proposed.
	"""

	return item.type_category == subroutine.domain.readiness.OCCASION


def _when (item: Item) -> str:
	"""Return a short trailing phrase describing an item's dates, or nothing at all.

	Nothing at all is the common case, and it matters: a to-do list that annotates every
	line with empty fields is one that looks like a database (§1.4).

	**A document has no dates to describe** — no deadline, no day it is planned for — so it
	is nothing at all, always. That is not a gap to fill later: a specification is not
	scheduled, and inventing a date column for it would be the database look this avoids.
	"""

	if not isinstance(item, subroutine.views.Task):
		return ""

	task = item

	# **A defer leads, and says so with the word that sets it.** `from` is one of §6.13's
	# own `DEFER_WORDS`, so the phrase reads back as something typeable rather than as a
	# label invented for the listing — the same self-describing rule as `!4/2`.
	#
	# It leads because it is the one fact the rest of the CLI cannot supply: `today` hides a
	# deferred task by design, so without this the item is simply absent for months and the
	# agenda looks broken. A deadline still prints alongside it, because "not until December,
	# and wanted by the fifteenth" is two facts and dropping either misinforms.
	if _deferred(task):
		deferred = f"from {_when_rendered(task)}"

		if task.due_at is not None:
			return (
				f"  ({deferred}, due "
				f"{_render_moment(task.due_at, task.timezone, all_day=task.due_is_all_day)})"
			)

		return f"  ({deferred})"

	# **Both, when there are both** (`#673`). These used to be alternatives with the deadline
	# winning, so `add "Fix the sink on monday by friday"` confirmed Friday and said nothing
	# about Monday — the planned day was read, stored, and reported nowhere on the one line
	# somebody reads to check what was understood.
	#
	# The pattern is the deferred branch's, three lines up, and so is the argument: "not until
	# December, and wanted by the fifteenth" is two facts and dropping either misinforms. The
	# same sentence is true of a day you mean to do it and a day it is wanted by.
	#
	# The cost is that a listing row grows — but only for a task carrying both dates, which is
	# uncommon, and this function already spends the same on a defer.
	said = [
		phrase
		for phrase in (
			# **The repeat first, because it is the thing somebody just typed** (`#94`). A
			# captured line is confirmed by this function, and *"every 14 days"* read out of a
			# sentence needs the same confirmation a date does — §6.13's rule is that a word
			# may only vanish if a field was set, and that is a property of the code rather
			# than something a person can see. Read back from the rule rather than echoed, so
			# it says what was understood and not what was typed.
			None
			if task.recurrence_rule is None
			else subroutine.domain.recurrence.describe(
				task.recurrence_rule, anchor=task.recurrence_anchor
			),
			# **The o'clock when the row carries one** (`#1298`). A doctor's appointment and a
			# birthday were the same line until this, on the one line somebody reads to check
			# what was understood.
			None
			if task.starts_at is None
			else (
				"starts "
				f"{_render_moment(task.starts_at, task.timezone, all_day=task.starts_is_all_day)}"
			),
			None
			if task.due_at is None
			else (
				"due "
				f"{_render_moment(task.due_at, task.timezone, all_day=task.due_is_all_day)}"
			),
		)
		if phrase is not None
	]

	if not said:
		return ""

	return f"  ({', '.join(said)})"


def _deferred (task: subroutine.views.Task) -> bool:
	"""Return whether this task's start has not come round yet.

	**Only while it is still hiding something**, which is why this is not simply
	``snoozed_until is not None``. A listing row has one short phrase to spend, and once the
	instant has passed the defer explains nothing: the task is startable and behaves like any
	other. ``show`` reports it either way, because there the question being asked is "what has
	been decided about this", not "why is this not in front of me".

	**The rule itself is ``ordering.put_off``**, which the merged sort reads to decide where a
	row lands (`#877`). Saying it a second time here would be a phrase and a position
	disagreeing about the same task — a row printed *(from Fri 15 Aug)* in the middle of the
	list, which is the defect the sinking was built to remove.
	"""

	return subroutine.domain.ordering.put_off(task) == subroutine.domain.ordering.DEFERRED_BAND


def _render_date (instant: datetime.datetime | None, timezone: str | None) -> str:
	"""Render an instant the way a person reads a date."""

	if instant is None:
		return "—"

	# Through the same function as a calendar date, so an instant and a day cannot come to
	# disagree about when a year is worth printing — one rule, one place. The conversion is
	# `schedule.day_in` for the same reason one level down: this file had the rule right and
	# two other surfaces had it wrong (`#1063`, `#1064`).
	return _dated(subroutine.domain.schedule.day_in(instant, timezone))


def _render_moment (
	instant: datetime.datetime | None, timezone: str | None, *, all_day: bool
) -> str:
	"""Render a date, **saying the o'clock when the row says there is one** (`#1298`).

	:func:`_render_date` with the one thing a whole day does not have. *Tue 1 Dec* and *Tue 1
	Dec at 11:00* are different facts, and until this the terminal printed the first for both
	— so a doctor's appointment and a birthday were indistinguishable on every surface here.
	``explain dates`` says of ``starts``: *"It takes a time, so 'monday at 14:00' is an
	appointment"*, which was true of the store and false of everything that drew it.

	**Read from the stored flag rather than from the instant.** An all-day start is the first
	microsecond of its day and an all-day deadline the last (§6.5), so *"is the time
	midnight"* would call a real midnight appointment a whole day and call every all-day
	deadline timed. The flag is what the store decided and is the only honest source.

	**In the row's own zone**, like every date this program renders: decision `#1088`'s rule
	that a day is a label. Converting to the reader's clock would move an 11:00 appointment to
	a different o'clock, and — for the all-day rows this deliberately says nothing about — to
	a different day.
	"""

	day = _render_date(instant, timezone)

	if all_day or instant is None:
		return day

	local = instant.astimezone(
		subroutine.domain.dates.zone(
			timezone or subroutine.domain.schedule.DEFAULT_TIMEZONE
		)
	)

	return f"{day} at {local.strftime('%H:%M')}"


def _when_rendered (task: subroutine.views.Task) -> str:
	"""Render when a task is hidden until, **saying the o'clock when there is one** (`#858`).

	**The terminal renders no times anywhere else, and that is deliberate rather than an
	omission** — a to-do list is a day-scale thing and `#576` is where an event's span is
	decided. This is the one exception, and it earns it: without it the confirmation for
	``defer 42 2026-08-18T06:00`` is *"Hidden until Tue 18 Aug"*, which is what the command
	said while storing midnight, so a working fix and the defect would print the same
	sentence and nobody could tell which they had.

	**Read from the stored flag rather than from what was typed.** `#925`'s finding: a
	read-back computed from the input confirms only that the input was received, which is the
	one thing never in doubt. ``snoozed_is_all_day`` is what the store decided, so this echo
	is wrong exactly when the row is.

	**In the task's zone**, like every other instant this program renders: ``snoozed_until``
	is midnight where the task lives, so reading it in a westward client zone named the day
	before the one that was asked for.

	It lives out here rather than in ``defer``'s body because `#943`'s ratchet holds
	``register`` and only goes down — which is the rule working, since the argument is about
	rendering and belongs beside the rendering.
	"""

	return _render_moment(
		task.snoozed_until, task.timezone, all_day=task.snoozed_is_all_day
	)


def _as_json (
	world: World, connection: str, item: Item, *, term: str | None = None
) -> dict[str, typing.Any]:
	"""Return one listing row as the scripted path sees it.

	Carries the *address* as well as the ref, because a script merging two connections needs
	the thing it can type back — which is exactly what a bare number stops being once there
	is more than one place an item could live.

	**A document carries the shared fields and stops there**, rather than carrying the task
	fields as nulls. A `due_at` of null on something that cannot have a deadline is a
	statement that it has none, which is a different and false claim; `entity_type` is how a
	script tells the two apart, and it is present on every row so the test is never "did the
	key appear".
	"""

	shared = {
		"ref": item.ref,
		"address": world.address_of_item(connection, item),
		"connection": connection,
		"entity_type": "task" if isinstance(item, subroutine.views.Task) else "document",
		"title": item.title,
		"type": item.type,
		"status": item.status,
		# **How much prose it carries** (`#595`). The terminal marks anything large; a script
		# gets the number, because the threshold is the caller's — a session with room to
		# spare and one nearly full need different answers to "is this too big".
		"size_bytes": item.size_bytes,
		# **The renameable key and the fixed axis, both** (`#583`). A status key is a
		# workspace's own word for a stage, so a script branching on `status == "done"` is
		# reading a label somebody may rename this afternoon; the category is the thing that
		# cannot move. `Status` publishes the pair for exactly this reason and this row
		# carried only the half that rots.
		"status_category": item.status_category,
		# **Shared since `#819`**, when a document gained tags from the same vocabulary. A
		# scripted reader that could see a task's tags and not a document's would be the
		# §12.2a drift this function's own docstring is about.
		"tags": list(item.tags),
		# **Why this row matched, or null when nothing was searched for** (`#840`). The
		# terminal has rendered this since `#870` on the argument that *a hit whose reason is
		# invisible reads as a bug* — searching for "pagination" returns a document whose title
		# says nothing about it, and with no reason the honest reading of that row is that the
		# search is broken. That argument does not weaken for a caller with no eyes.
		#
		# **The computed cell, not the fields it was computed from.** A listing row carrying
		# every hit's whole `description` is what §14.10 exists to prevent; the name of the
		# field that matched is one short string, and it is the same string the terminal shows.
		#
		# Empty rather than null where a search was made and this could not say — the
		# case-folding disagreement `_match_cell` documents. Null means *nothing was searched
		# for*, and collapsing the two would be an absence two behaviours produce.
		"matched": _match_cell(item, term) if term else None,
		# **How well this row answered the search that found it, or null** (`#878`). A script
		# merging two connections has to put them into one order, and this is the only key that
		# says what order the instance chose — the same field the browser reads and the
		# terminal's own merge now reads. Null on any listing that was not ranked, which is how
		# a caller tells "not searched" from "searched and scored zero".
		"relevance": getattr(item, subroutine.domain.ordering.RELEVANCE, None),
		# **Where it lives, whole** (`#512`). Shared rather than task-only, because a document
		# is filed in a project exactly as a task is — and unlike the terminal's column this
		# is never dropped or shortened: a script has no page to be uniform across, and the
		# form it wants is the one it can send back to `--project`.
		"project_path": item.project_path,
	}

	if not isinstance(item, subroutine.views.Task):
		return shared

	task = item

	return {
		**shared,
		# §12.2a wants the scripted path and the human one to be the same code so they cannot
		# drift, and these had. `type` because the terminal now shows it and a script reading
		# the same listing could not see it; `urgency` because §6.3 pairs the two axes and
		# half a priority is worse than none — a script sorting on `importance` alone would
		# rank a 5/1 above a 4/5.
		"due_at": None if task.due_at is None else task.due_at.isoformat(),
		"due_is_all_day": task.due_is_all_day,
		"starts_at": None if task.starts_at is None else task.starts_at.isoformat(),
		# **The flag beside its instant, as the deadline above already has it** (`#1298`). It
		# was the one date column here whose shape a script could not read, so *Anna's birthday*
		# and *the dentist at 11:00* arrived identical — and unlike the API's row there is no
		# `?fields=` to ask for it by name. An end shares this flag, having none of its own.
		"starts_is_all_day": task.starts_is_all_day,
		"ends_at": None if task.ends_at is None else task.ends_at.isoformat(),
		"snoozed_until": None if task.snoozed_until is None else task.snoozed_until.isoformat(),
		"snoozed_is_all_day": task.snoozed_is_all_day,
		"importance": task.importance,
		"urgency": task.urgency,
		"estimate_minutes": task.estimate_minutes,
		# **Who has it** (`#583`). `#511` put the assignee on the terminal row and stopped
		# there, so the one reader most likely to be automating a handover — a script, or an
		# agent reading this listing — could not see that anything had been handed over.
		"assignee": task.assignee,
		# **And what that name is** (`#1414`). The terminal row says *(agent, @si)* beside a
		# name, and a script reading the same listing saw a bare username — so the reader most
		# likely to be routing work automatically was the one that could not tell a colleague
		# from something somebody set running. Two facts rather than the rendered phrase: a
		# script wants to branch on them, and `views.principal_named` is where the wording
		# lives for anything that prints.
		"assignee_is_agent": task.assignee_is_agent,
		"assignee_answers_to": task.assignee_answers_to,
		# **Whether it can be started** (`#425`). A default listing puts a blocked item above
		# the thing blocking it, and the terminal marks it; a script sorting the same rows
		# had no way to tell, so it would confidently recommend starting the one that cannot
		# be started. `?ready=true` is a filter, and `#425`'s whole finding is that a filter
		# is not a signal.
		"blocked": task.blocked,
		# **And whether it is the thing in somebody else's way** (`#569`, the mirror of
		# `#425`). Sent as its own field rather than folded in with `blocked`, because the
		# terminal shows one column with a precedence and a script wants both facts: a row can
		# be held up and holding something up at once, and an agent choosing what to start
		# next needs to see the second even when the first is true.
		"blocking": task.blocking,
		# **And whether the only thing left is a decision** (`#1615`). A parent whose sub-tasks
		# are all finished is not completed for anybody — `#84` refuses that — so a script
		# watching a milestone has no other way to learn that it is answerable. Its own field
		# for `blocking`'s reason: the two are commonly true of one row, and a script that got
		# only the first would learn what the item does to others and not that nothing is left
		# to do about it.
		"sub_tasks_done": task.sub_tasks_done,
		# **What it is part of**, which the terminal shows as `↳ #12`. A sub-task read on its
		# own is work whose context is one field away, and the number is what a script types
		# back.
		"parent_ref": task.parent_ref,
	}


def _shown_as_json (
	world: World,
	located: Located,
	gathered: Sections,
	*,
	walked: typing.Sequence[subroutine.views.Beneath] | None = None,
) -> dict[str, typing.Any]:
	"""Return one item, its links and its record, as the scripted path sees it.

	The **whole** view model rather than the handful of fields ``_as_json`` selects for a
	listing, because the reason to ask about one item is to read what a listing left out —
	and a caller who has already named the item is not paying for a page of them.
	"""

	links = gathered.links
	remarks = gathered.remarks
	referring = gathered.referring
	proposed = gathered.proposed
	governing = gathered.governing
	children = gathered.children
	events = gathered.events

	return {
		"address": world.address_of_located(located),
		"connection": located.connection,
		"entity_type": located.entity_type,
		"item": located.item.model_dump(mode="json"),
		"links": [link.model_dump(mode="json") for link in links],
		"comments": [remark.model_dump(mode="json") for remark in remarks],
		# **What refers to this** (`#144`), carried for the scripted reader as well: a script
		# asking "is this safe to close" wants what mentions it exactly as a person does, and
		# a section the rendered path shows and this one omits is `#583`'s two-renderings
		# defect arriving on a new field.
		"backlinks": [one.model_dump(mode="json") for one in referring],
		# **Its own key rather than merged into `links`** (`#1137`). A scripted reader that
		# could not tell a suggestion from a link would report one as the other, which is the
		# single thing this feature must never do.
		"proposed_links": [one.model_dump(mode="json") for one in proposed],
		"governing": [one.model_dump(mode="json") for one in governing],
		"verifications": [one.model_dump(mode="json") for one in gathered.checked],
		"children": [child.model_dump(mode="json") for child in children],
		# **Always present, and `null` when it was not asked for** (`#349`). The key is
		# unconditional for the reason it always was: one that appears only with `--history`
		# makes a script test for the key rather than read it, so *absent* and *nothing
		# happened* would be one shape for two facts.
		#
		# **That argument was sound and it collapsed the two facts one level along anyway.**
		# `[]` was written for *not asked* and is also what *asked, and nothing has happened*
		# produces — and an agent reading one invocation's output cannot know which flags
		# produced it, which a script can. It was read as "the history is empty" on `#346` by
		# somebody with no way to know better.
		#
		# So the distinction moves into the value, where a reader of the output can see it,
		# rather than staying in a comment only a reader of this file can.
		"history": (
			[event.model_dump(mode="json") for event in events]
			if gathered.asked_for_history
			else None
		),
		# **`null` for *not asked*, and a list for *asked***, which is `#349`'s decision one
		# section along and for its exact reason: `[]` is also what *asked, and nothing blocks
		# this* produces, and a reader of one invocation's output cannot tell which flags made
		# it. The distinction lives in the value, where they can see it.
		"tree": (
			None if walked is None else [one.model_dump(mode="json") for one in walked]
		),
	}


def _agenda_json (
	world: World, gathered: subroutine.fanout.Gathered[subroutine.views.Agenda]
) -> dict[str, typing.Any]:
	"""Return the merged agenda as the scripted path sees it.

	``unreachable`` is reported rather than left to be inferred from a short list. A script
	acting on a partial view should be able to tell that it is one — which is the same reason
	``--strict`` exists for a script that would rather not have one at all.
	"""

	buckets = tuple(field for _heading, field, _late in AGENDA_SECTIONS)
	rows = agenda_rows(world, gathered)
	first = gathered.answers[0].value if gathered.answers else None

	return {
		"date": None if first is None else first.date.isoformat(),
		"timezone": None if first is None else first.timezone,
		# **Per connection, because they can genuinely differ** (`#995`). Each instance
		# resolves the reader's own zone (§6.5), so the two scalars above are the first
		# answer's and are the whole truth only while these agree. A script merging several
		# instances has to be able to tell — the rendered path says it in words, and this is
		# the same fact for something that is not reading.
		"timezones": {
			answer.connection.name: answer.value.timezone for answer in gathered.answers
		},
		# **The same rows in the same order as the page**, which they were not: this called
		# `_across` and never `_in_order`, so a scripted reader with two connections got two
		# sorted runs end to end while the rendered path got one list (`#993`). On one
		# connection the two coincide, which is why nothing had caught it.
		**{
			field: [_as_json(world, name, task) for name, task in rows[field]]
			for field in buckets
		},
		# **Every count the agenda publishes, summed across connections and read off the
		# model rather than listed** (`#1285`). There were six here, each with its own line
		# — how much the cap held back, what the window left out, what somebody deferred,
		# what is in a project nobody is running, and what simply went by — and a seventh
		# would have been missing from the scripted path alone, which is `#992`'s defect
		# exactly: the human path and the scripted path answering differently about one day
		# (§12.2a). A script asking whether this view is complete has the same question a
		# person reading the footer does, so it gets the same numbers by construction.
		**{
			field: sum(getattr(answer.value, field) for answer in gathered.answers)
			for field in subroutine.views.Agenda.model_fields
			if field.endswith("_total")
		},
		# **Every connection that did not answer, not only the ones that failed at this
		# call.** A connection that could not be *opened* (no token, unparseable
		# credentials) or that failed at `identity()` was named on stderr and reported here
		# as absent — so a script could not tell a partial view from a complete one, which
		# is the only reason this field exists.
		"unreachable": sorted(
			{failure.connection.name for failure in world.unreachable}
			| {failure.connection.name for failure in gathered.failures}
		),
	}


def _worth_showing (
	settings: typing.Callable[[], subroutine.config.Settings],
) -> bool:
	"""Report whether the context commands are worth putting in front of a new user.

	They are, as soon as a second connection is configured. Until then ``subroutine --help`` is
	the six commands §12.2 lists first plus the administrative ones, and neither ``use`` nor
	``connections`` says "workspace" or "instance" at somebody who has not asked (§1.4).

	Deliberately cheap and deliberately fallible: this runs when the commands are registered,
	before any of them, so it reads a file and never opens a database. If the configuration
	cannot be read it returns ``True`` — a visible command is a smaller mistake than a hidden
	one, and the command itself explains a broken file far better than its absence would.
	"""

	try:
		return len(subroutine.connections.roster(settings())) > 1

	except Exception:
		return True


def _say_parked (
	gathered: subroutine.fanout.Gathered[Listing],
	*,
	console: rich.console.Console,
	hidden: bool,
) -> None:
	"""Say how much was held back for a later date, and how to see it.

	**A hidden row is never silent.** §6.5 says a deferred task is not actionable and default
	views hide it, and until now the only view that did was the agenda — so `list` showed work
	nobody could start, and `today` hid it with no explanation anywhere (`#72`, `#73`). Doing
	the hiding without saying so would swap one of those failures for the worse one: a list
	that quietly omits things stops supporting the inference refs exist for, that *not in the
	list* means *not in the system*.

	A count rather than `…and more`'s flag, and the difference is affordable. That flag exists
	because an exact remainder needs a second scan of the whole result; this set is the parked
	work alone, which is small by construction — somebody defers a handful of things, not a
	backlog. It is still asked at the page limit, so `N+` is what a pathological case reads as.
	"""

	if not hidden:
		return

	total = sum(answer.value.parked for answer in gathered.answers)

	if not total:
		return

	# "put off" rather than "deferred": §13.5b forbids the vocabulary of the full model on
	# this path, and while "deferred" is not on its list, `defer` is the command and the plain
	# phrase is what somebody who has not met it would recognise.
	things = "thing" if total == 1 else "things"
	console.print(
		rich.text.Text(
			f"      {total} {things} put off until later. 'subroutine list --deferred' to "
			f"include them.",
			style=DETAIL,
		)
	)


def _say_held_back (
	gathered: subroutine.fanout.Gathered[Listing],
	*,
	console: rich.console.Console,
) -> None:
	"""Say how much startable work is waiting on something it is filed under — `#1610`.

	**The other half of Simon's decision of 2026-08-31**: work under something that cannot
	start stopped being offered, and a listing that hides without saying so is the failure
	`_say_parked` above exists to prevent, arriving on a second axis. *There is nothing to do*
	and *all of it is waiting on something above it* read identically as an empty page, and the
	second is the ordinary state of somebody's first morning on a real plan.

	**Only the inherited half is counted, and only where readiness was asked for.** A parent
	with unfinished sub-tasks is absent because it was never work — that needs no explaining,
	and a number for it would put a figure on the ordinary shape of every plan.

	**No flag to widen it, unlike the line above.** There is deliberately no way to ask for
	work under a blocked ancestor: the remedy is to look at what is holding the parent up,
	which `list` and `show` already say. A sentence offering a switch that does not exist
	would be worse than none.
	"""

	total = sum(answer.value.held_back for answer in gathered.answers)

	if not total:
		return

	# **"waiting on" rather than "blocked"**, on `_say_parked`'s reasoning: §13.5b keeps the
	# full model's vocabulary off this path, and this is a sentence somebody meets before they
	# have met any of it.
	things = "thing" if total == 1 else "things"
	console.print(
		rich.text.Text(
			f"      {total} more {things} waiting on something they are filed under.",
			style=DETAIL,
		)
	)


def _sunk (order: str | None) -> str:
	"""Return this ordering with deferred work sinking to the bottom of it — ``#877``.

	**A leading key rather than a replacement**, so that whatever the reader asked for still
	decides the arrangement *within* each band: most important first, and then the same again
	for the work that has been put off. Simon's decision of 2026-08-14 is that deferred work is
	*"not invisible, but neither confused with non-deferred items"*, and only a leading key says
	both at once.

	**The default is spelled out rather than left off.** An empty ordering means the server's
	own default, so sending ``deferred`` alone would silently drop newest-first — the whole
	arrangement replaced by the one key that was meant to sit above it.

	A reader who names ``deferred`` themselves is left alone: repeating it is refused by name
	(:func:`subroutine.domain.ordering.requested`), and they have already said what they want.
	"""

	named = [part.strip() for part in (order or "").split(",") if part.strip()]

	if not named:
		named = list(subroutine.domain.ordering.DEFAULT_TASK_ORDER)

	if any(
		part.removeprefix("-") == subroutine.domain.ordering.DEFERRED for part in named
	):
		return ",".join(named)

	return ",".join((subroutine.domain.ordering.DEFERRED, *named))


def _ordering (order: str | None) -> tuple[bool, tuple[tuple[str, bool], ...]]:
	"""Return whether documents can be asked in this order, and how to compare merged rows.

	**One place decides both**, because they are two consequences of the same answer and
	every merge in this module has to reach the same one. ``--order`` is parsed against the
	*task* vocabulary, which is the richer of the two: a person ranking a backlog wants
	``-priority_score``, and a document has no priority to be ranked by (§6.14).

	The first half of the answer is whether a document listing can be asked for the same
	order. When it cannot, documents come back in their default order and the merge settles
	where they land — asking for a page in one order and re-sorting it in another returns the
	*wrong rows* rather than merely the wrong order.

	The second is the comparison, with ``ref`` appended **always ascending**, which is oldest
	first and is what :func:`subroutine.domain.ordering.clauses` and
	:func:`subroutine.api.pagination.parse_order` both do.

	**That sentence used to say "following the last key's direction", and it was true until
	`eecbd93` and false afterwards** — `#879`. Simon's decision of 2026-08-13 is that age is a
	separator and not a signal, so it must not inherit a direction from a key it has nothing to
	do with; the query side moved and the two client-side copies did not, and both of their
	docstrings went on asserting they agreed. **A sentence claiming a rule holds is why nobody
	checks it**, which is the reason `#879` was rated above the tie order it changes.

	It matters more than a tiebreak usually would: ranked listings here are tie-heavy — 52 of
	172 open tasks shared one score when that was measured — so across a third of a backlog
	this is the only thing deciding the order.

	An unknown field is refused here, before a single request goes out.
	"""

	# **Both vocabularies through `sinking`, so `deferred` is a name this parses** (`#877`).
	# The endpoint adds it to every item listing, and a sort field the API accepts and the
	# terminal refuses is exactly the divergence this module exists to prevent. The
	# expressions are never executed here — nothing in the CLI runs a query — but the instant
	# is a real one rather than a placeholder, because a value invented to satisfy a signature
	# is the sort of thing that later gets used.
	allowed = subroutine.domain.ordering.sinking(
		subroutine.domain.ordering.TASK_FIELDS,
		model=subroutine.db.models.work.Task,
		now=subroutine.db.types.utcnow(),
	)

	wanted = subroutine.domain.ordering.requested(
		order,
		allowed=allowed,
		default=subroutine.domain.ordering.DEFAULT_TASK_ORDER,
	)

	# **A document answers `deferred` too, with *no***, which is what keeps a sunk list from
	# quietly becoming a list of tasks — `ordering.UNDEFERRABLE` and `#782` for why.
	readable = subroutine.domain.ordering.sinking(subroutine.domain.ordering.DOCUMENT_FIELDS)

	shared = all(name in readable for name, _descending in wanted)

	return shared, (*wanted, ("ref", False))


def _merge_order (
	order: str | None, gathered: subroutine.fanout.Gathered[Listing]
) -> tuple[tuple[str, bool], ...]:
	"""Return the comparison a merged page is actually in — ``#878``.

	**A reader's explicit choice wins. Failing that, a ranked search is what the server chose
	for itself**, and the rows are what say so: a listing that was ranked carries a relevance on
	every row and one that was not carries null (`#875`). So this reads the data rather than
	re-deriving the server's rule, which would be that rule written down twice and free to
	disagree — and the browser's ``mergeOrder`` is the same three answers, reached the same way.

	**Without this the terminal threw the ranking away.** ``_ordering`` parses the caller's
	``--order`` against the static vocabulary, which has no ``relevance`` in it — that entry is
	added per request — so a search with no explicit order fell back to ``-created_at``, each
	connection came back correctly ranked, and the merge re-sorted the lot into newest-first.
	Measured on the served instance: the API answered ``877, 389, 444, 598, 541`` where
	``subroutine search`` answered the same rows strictly newest-first.

	**It also defeated `#867`**, which is the sharpest way to say what it cost: an exact ref
	match carries ``EXACT_MATCH_RANK`` and was then buried wherever its creation date fell —
	the very defect ``api/tasks.py`` records as fixed.
	"""

	return subroutine.domain.ordering.merge_order(
		order,
		_ordering(order)[1],
		ranked=any(
			getattr(row[1], subroutine.domain.ordering.RELEVANCE, None) is not None
			for answer in gathered.answers
			for row in answer.value.rows
		),
	)


def _across (
	world: World,
	gathered: subroutine.fanout.Gathered[typing.Any],
	of: typing.Callable[[typing.Any], typing.Iterable[typing.Any]],
) -> list[tuple[str, typing.Any]]:
	"""Return one flat sequence across every connection, each item paired with where it came from.

	**This is the only place answers from more than one connection become one list, which is
	what makes it the place the duplicate-instance guard belongs** (`#942`). Everything else
	iterates the connections and prints a heading per group, and a heading is what makes two
	names for one instance visible rather than doubled.

	So the four callers are the four merges: a listing's rows, the agenda's buckets, the
	agenda's JSON and the change feed's JSON. Reach for this to write a fifth and the guard
	comes with it; write the comprehension by hand and it does not, which is the residual risk
	and is why ``test_personal_path`` drives every command against a duplicated world rather
	than scanning for the shape.
	"""

	world.merging()

	return [
		(answer.connection.name, item)
		for answer in gathered.answers
		for item in of(answer.value)
	]


def _merged (
	world: World,
	gathered: subroutine.fanout.Gathered[Listing],
	*,
	order: tuple[tuple[str, bool], ...],
) -> list[Row]:
	"""Flatten a listing across connections, in the order the caller asked for.

	§13.7: "sorting is re-applied after the merge". Each connection answers already ordered, so
	concatenating them produced one sorted run per connection rather than one ordered list —
	which is not what "newest first" means and is not what the suggested next command assumed.

	**``order`` is passed in rather than assumed here, and that is what ``#71`` actually cost.**
	This function sorted by ``created_at`` unconditionally — a second copy of the merge rule,
	one level above the one in ``_listing`` — so giving the clients an ordering fixed *which*
	rows came back and then threw away the order they came in. The listing looked plausible in
	both directions: the right items, arranged by date, with nothing to say the sort had been
	discarded. Two copies of "how is a merged listing sorted" is one too many, and this is now
	the only one that decides.
	"""

	rows = _across(world, gathered, lambda listing: (row[1] for row in listing.rows))

	return subroutine.domain.ordering.merged(rows, key=lambda row: row[1], order=order)


def _in_order (rows: list[Row], bucket: str) -> list[Row]:
	"""Order one agenda bucket on the keys the instance ordered it by.

	**Re-sorted at all because ``_across`` concatenates**, and two sorted runs end to end is
	what §13.7 rules out: a person with a work connection and a personal one would otherwise
	see all of one and then all of the other rather than one day.

	**On :data:`subroutine.domain.agenda.ORDERS` rather than on keys of its own** (`#993`).
	This wrote the rule out a second time and got it wrong twice: the ref where the server
	breaks ties on ``created_at``, and nothing at all where the server reads ``starts_at``.
	Refs are allocated per workspace, so the first agreed for exactly as long as an agenda was
	dominated by one — which is the state every fixture and this project's own instance were
	in, and is why nothing caught it.

	That is `#71`'s shape, which ``domain/ordering.py``'s own docstring records: an ordering
	chosen by the server and discarded one level up, where **the output looks entirely
	reasonable**. Reading the declaration is what makes a third disagreement impossible rather
	than unlikely.
	"""

	return subroutine.domain.ordering.merged(
		rows,
		key=lambda row: row[1],
		order=subroutine.domain.agenda.order_for(bucket),
	)


def suggest (command: str, about: str | None = None) -> None:
	"""Print the command to try next, on the shared console (docs/design.md §12.2a).

	The public face of :func:`_suggest`, for callers outside this module that have no console
	of their own — the bare invocation and ``--version`` in ``cli/main``. Kept as one function
	so the styling cannot drift into a second definition, which it had begun to: both of those
	callers used to pad their own explanation into a column, which was a second shape for one
	thing and lined up with nothing else on the screen.
	"""

	_suggest(subroutine.cli.output.Terminal(), command, about)
