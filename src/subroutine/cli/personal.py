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
import typing
import uuid

import rich.console
import rich.text
import typer
import typer.core

import subroutine.cli.output
import subroutine.clients.base
import subroutine.clients.opening
import subroutine.config
import subroutine.connections
import subroutine.context
import subroutine.credentials
import subroutine.db.models.work
import subroutine.db.types
import subroutine.directory
import subroutine.domain.agenda
import subroutine.domain.capture
import subroutine.domain.dates
import subroutine.domain.durations
import subroutine.domain.filtering
import subroutine.domain.ordering
import subroutine.domain.projects
import subroutine.domain.recurrence
import subroutine.domain.refs
import subroutine.domain.schedule
import subroutine.domain.search
import subroutine.domain.text
import subroutine.errors
import subroutine.fanout
import subroutine.installations
import subroutine.permissions
import subroutine.views

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
TROUBLE = "yellow"

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
	"overdue": ("Overdue", True),
	"today": ("Today", False),
	# **Between the day and the rest** (`#853`). Work somebody is in the middle of is
	# neither scheduled nor a candidate to pick up, and it is the first thing to look at
	# after what the day demands — a person who left something half-finished yesterday
	# should not have to find it among two hundred captured tasks.
	"in_progress": ("In progress", False),
	"upcoming": (f"Next {subroutine.domain.agenda.DEFAULT_HORIZON_DAYS} days", False),
	# **"Next" rather than "Unscheduled"**, because it is ordered by rank now rather than
	# by capture order — the heading names what the section is *for*, and the old one
	# named only what its rows lacked.
	"unscheduled": ("Next", False),
}

#: The agenda's sections, in the order a day is read: heading, the field on
#: :class:`subroutine.views.Agenda` that fills it, and whether it is late.
#:
#: **A module constant because a second surface renders the same sections** (`#927` H-15).
#: §12.2 decided what the agenda says and the browser is held to the same words, so this being
#: a local in one function meant the browser's copy could — and did — drift: it was missing
#: ``in_progress`` entirely and still called the last section *Unscheduled*, under a comment
#: claiming to print "deliberately the same words". `tests/test_web.py` compares them now.
AGENDA_SECTIONS: tuple[tuple[str, str, bool], ...] = tuple(
	(_HEADINGS[field][0], field, _HEADINGS[field][1])
	for field in subroutine.views.AGENDA_BUCKETS
)


def agenda_asked (
	world: World, *, workspace: str | None, now: datetime.datetime
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

	**`date` and `timezone` are this machine's, resolved once** (§13.7). Each instance would
	otherwise apply its own notion of the caller's timezone, and a person whose work profile
	says America/New_York and whose personal one says Europe/London would get two different
	days merged into one list. **That reasoning does not survive its own case and `#995` is
	the item**: the zone sent is the *typing machine's*, which on a mismatched pair is a third
	answer matching neither.
	"""

	zone = world.settings.default_timezone

	return {
		"date": subroutine.domain.schedule.local_date(now, zone),
		"timezone": zone,
		"horizon_days": subroutine.domain.agenda.DEFAULT_HORIZON_DAYS,
		# `-w` narrows the agenda the same way it narrows every other listing. Unset spans
		# everything, which is what makes this one list rather than one per workspace
		# (§13.7) — the dentist and the stand-up belong in the same place. Naming a
		# workspace is how you ask for half of it.
		"workspace": workspace,
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


# These live above every function that annotates with them. A module-level annotation is
# evaluated when the `def` runs, not lazily, so `Columns` referenced before its own
# definition raises `NameError` on import while mypy reports nothing — the same trap as
# `Item` above `World`, which cost an import failure on 2026-07-30.
def _column (values: typing.Iterable[str], *, drop_if_uniform: bool = True) -> int:
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
	"""

	distinct = set(values)

	if len(distinct) < 2 and (drop_if_uniform or not any(distinct)):
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

	# **The first column is never dropped, and a single row keeps whatever it filled in.**
	# `_column` asks whether a column *varies*, which on a one-row page is false of every
	# column including the name — so the unguarded rule printed a blank line where the answer
	# was one person. Found by running it on a fresh instance, which is the commonest case
	# there is.
	total = max(len(row) for row in rows)
	widths = [
		len(rows[0][index]) if len(rows) == 1 else _column(row[index] for row in rows)
		for index in range(total)
	]
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

		return cls(
			term=term,
			within=within,
			project=_column(_project_cell(item, within) for _name, item in rows),
			matched=_column(_match_cell(item, term) for _name, item in rows),
			parent=_column(_parent_cell(item) for _name, item in rows),
			address=max(
				(len(world.address_of_item(name, item)) for name, item in rows), default=0
			),
			kind=_column(item.type for _name, item in rows),
			state=_column(_state_cell(item) for _name, item in rows),
			blocked=_column(_blocked_cell(item) for _name, item in rows),
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
		return "title"

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

	return f"@{item.assignee}"


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

#: What a changed column is called by the person whose task it is. A change line lists the
#: *names* of what moved, and the names are the database's — so a defer read
#: ``(snoozed_is_all_day, snoozed_until)``, and a status change would have printed one of the
#: seven words §13.5b says this path never uses.
#:
#: Several columns collapse to one phrase deliberately: a date and its all-day flag are one
#: fact to a reader and always move together, so listing both says the same thing twice.
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


def _field_in_words (name: str) -> str:
	"""Return what a person calls the thing that changed.

	The internal suffixes come off anything unmapped — ``_id`` names a row nobody can see and
	``_at`` says nothing a reader needs — so a column added tomorrow reads as words rather than
	as a schema. ``title`` and ``description`` are already what they are called, which is why
	most of this file's own fields are not in the table.
	"""

	if name in _A_CHANGE_TO:
		return _A_CHANGE_TO[name]

	for suffix in ("_is_all_day", "_id", "_at"):
		name = name.removesuffix(suffix)

	return name.replace("_", " ")


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
	"""

	people = [account for account in before if not account.is_service_account]

	if len(people) != 1 or world.settings.local_user:
		return None

	subroutine.config.store_setting("local_user", people[0].username)

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


def _project_written_down (world: World, wanted: str) -> tuple[str, str] | None:
	"""Return the address and permanent id of the project this names, or ``None``.

	Checked before the file is written, because a marker naming a project that does not
	exist fails on the *next* capture rather than here — and the person who would have to
	work out why is not the one who typed this.

	Returns the **id** rather than a yes-or-no, because that is what the marker records
	(`#177`) and asking twice would be two chances for the answers to differ. The address
	comes back beside it so the readable half of the pair is the one that resolves: a bare
	key stopped naming one project with `#957`, so writing down what somebody typed would
	leave a file whose two halves can point at different projects.
	"""

	where = world.writing_to()
	tree = where.client.projects(workspace=_writing_workspace(world))
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


def _whoami_lines (me: subroutine.views.Me) -> list[str]:
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
	"""Return the role held in one workspace, or say that none is.

	A superuser reaches every workspace whether or not they are a member of one (§7.1), so
	"no role" is a real answer here rather than a missing value — and it is the answer that
	explains why somebody with every permission is not on the members list.
	"""

	return workspace.role or "no role"


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


def _already_reached (
	roster: subroutine.connections.Roster,
	resolved: subroutine.config.Settings,
	instance: uuid.UUID,
) -> str | None:
	"""Return the connection already naming this instance, or ``None`` if none does.

	A connection that cannot be reached is passed over in silence rather than reported. It
	is the ordinary state of the local one on the machine this command is written for, and
	a warning about a work server being down would arrive while somebody is in the middle
	of doing the one thing that does not need it.
	"""

	for existing in roster:
		try:
			with subroutine.clients.opening.for_connection(
				existing, roster, resolved
			) as client:
				answered = client.identity()

		except subroutine.errors.SubroutineError:
			continue

		if answered.instance is not None and answered.instance.id == instance:
			return existing.name

	return None


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


def _day (world: World, written: str) -> datetime.date:
	"""Read a day the user named, in their timezone.

	**A weekday name is resolved here rather than by the expression grammar** (`#167`).
	``plan 1 friday`` is promised by ``explain dates``, by ``plan --help`` twice, by
	``defer --help`` twice and by this function's own refusal — and it did not work, while
	``add "Something by friday"`` did. Weekdays are what a person types; §9.3's expressions
	serve programs, which have a calendar and should send a date. The two vocabularies meet
	in ``dates.day_named``, so there is one answer to what "friday" means.
	"""

	resolved = subroutine.domain.schedule.interpret_written_day(
		written,
		timezone=world.settings.default_timezone,
		now=subroutine.db.types.utcnow(),
		field="when",
	)

	if resolved is None:
		raise subroutine.errors.ValidationError(
			f"{written!r} is not a day this understands.",
			hint=subroutine.domain.schedule.WRITTEN_DAY_HINT,
		)

	return resolved


def _moment (world: World, written: str) -> datetime.datetime | datetime.date:
	"""Read a day the user named, **keeping a time of day when they wrote one** (`#858`).

	`_day`'s sibling, for the one command whose field carries a clock. Same vocabulary and
	the same refusal — both go through ``schedule.interpret_written_moment``, and `_day` is
	that function with the time thrown away, so there is no second grammar to drift.

	**Two readers disagree about a deferred item and both are right** (`#771`, and `#858`
	asked for this to be written down). ``readiness.undeferred`` compares to the minute, so
	``subroutine list`` shows it at six in the morning; ``domain.agenda`` compares against the
	*end of the day being shown*, so ``subroutine today`` has it from midnight. That is not a
	defect to reconcile: an agenda answers *what is today about*, and an item arriving at six
	is part of today from the moment the day starts.

	**Only ``defer`` reads a moment**, deliberately. ``plan`` sets ``starts_at``, and the
	terminal renders no times anywhere — `#576` is where an event's span is decided, and
	giving one command a clock ahead of that decision would be `#251`'s inert control with the
	inconsistency showing.
	"""

	resolved = subroutine.domain.schedule.interpret_written_moment(
		written,
		timezone=world.settings.default_timezone,
		now=subroutine.db.types.utcnow(),
		field="when",
	)

	if resolved is None:
		raise subroutine.errors.ValidationError(
			f"{written!r} is not a day this understands.",
			hint=subroutine.domain.schedule.WRITTEN_DAY_HINT,
		)

	return resolved


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

	fields = sorted({_field_in_words(name) for name in (event.changes or {})})
	listed = f"  ({', '.join(fields)})" if fields and event.action == "updated" else ""

	return f"{verb:<12}  {named}{listed}"


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
	status: str | None = None,
	type: str | None = None,
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

		# One past the limit, of each kind, so that "was anything cut?" is answered by
		# what came back rather than by a second counting query.
		asked = limit + 1

		parked = 0

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
		missing: subroutine.errors.SubroutineError | None = None
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
					status=status,
					type=type,
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

				missing = absent

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
				if not {problem.field for problem in unknown.errors} & {"status", "type"}:
					raise

				missing = unknown
				found_here = []
				answered = False

			else:
				reached = True
				answered = True

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
						status=status,
						type=type,
						filters=filters,
					)
				)
			# **A document has no assignee, so a list narrowed to one is a list of tasks**
			# (§6.14 — a document has an owner rather than a worker). The same argument
			# `ready` makes above: including them would answer a question nobody asked,
			# and "everything Simon is working on" ending in every specification in the
			# workspace is worse than useless.
			if assignee is not None:
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
					filters=filters,
				)

			except subroutine.errors.ValidationError as unknown:
				if not {problem.field for problem in unknown.errors} & {"status", "type"}:
					raise

				# **The first refusal wins.** Both vocabularies rejecting the key is what
				# makes it a typo, and somebody typing `--status` means a task's status far
				# more often than a document's — so reporting the document one, purely
				# because it was asked second, names the less likely of two right answers.
				missing = missing or unknown
				found_documents = []

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

			rows.extend((client.connection.name, found) for found in found_documents)

		# **Refused by name when the key is nowhere on this connection.** A project that
		# exists and holds nothing answers "nothing on your list"; a project that does not
		# exist has to say so, or a mistyped `--project` is indistinguishable from an
		# empty one. Raised rather than returned so `fanout` reports it per connection —
		# a key on one instance and not another is a fact about that instance, and the
		# other one's rows still arrive.
		if missing is not None and not reached:
			raise missing

		# Re-sorted after the merge, because a merged result is a merge of pages and not
		# one ordered page — the limit is per workspace and has to be applied again here.
		# The domain owns the comparison so that the merged order matches the order each
		# page arrived in, NULLS LAST included (§10.3): a document sorts last in a list
		# ranked by priority, which is the same answer §6.3a gives an unranked task.
		rows = subroutine.domain.ordering.merged(
			rows, key=lambda row: row[1], order=merging
		)

		# **What was cut is carried, not discarded.** `rows[:limit]` used to be the end of
		# it, so a backlog longer than the limit simply stopped — no count, no marker —
		# and "it is not in the list" quietly stopped meaning "it does not exist", which
		# is the one inference ref addressing is built to support. The agenda had always
		# reported its own remainder; this is the same fact, carried the same way.
		return Listing(rows=rows[:limit], more=len(rows) > limit, parked=parked)

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

		day = None

		for event in answer.value:
			when = event.created_at.astimezone()

			if when.date() != day:
				day = when.date()

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
	# context at all, so refusing at this point would stop `subroutine today` working
	# for anybody with two workspaces — which is precisely the person §13.7 is for.
	# The refusal belongs to the write, and `_writing_workspace` makes it.
	return current


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
			f"'subroutine doc {verb}' works on documents. Change this one with "
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
	status: str | None = None,
	type: str | None = None,
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
			status=status,
			type=type,
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
					[_as_json(world, name, item) for name, item in flat()], indent=2
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


def _filters (program: Program, dated: list[str] | None) -> dict[str, str]:
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


def _moved_to (program: Program, which: str, status: str, *, verb: str, said: str) -> None:
	"""Move a task to a named status, in the shape `done` uses.

	One body for both, because they differ in two words. **Neither says "status"** — §13.5b
	forbids the vocabulary and does not need it: `done`, `plan` and `defer` are all actions
	that happen to set a field, and "Started: <title>" is the same shape as "Done: <title>".
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
		moved = client.update(ref=task.ref, status=status, workspace=located.workspace)

		program.say(_acted(world, dataclasses.replace(located, item=moved), said))
		_suggest(program.console, "subroutine today")


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


def _use_here (program: Program, world: World, where: str, project: str) -> None:
	"""Write a marker into the current directory, and say what it will do.

	The connection and workspace come from the *current context* unless the caller named
	them, so ``subroutine use --here --project SR`` records where they already are rather
	than making them type it again — which is the whole difference between adopting a
	repository in one command and adopting it in an interview.
	"""

	connection, workspace = (
		_chosen(program, world, where)
		if where.strip()
		else (world.current.connection, world.current.workspace)
	)
	asked = subroutine.domain.projects.normalize_path(project) or None
	found = None if asked is None else _project_written_down(world, asked)

	if asked is not None and found is None:
		program.stop(
			f"There is no project {asked!r} here.",
			"Run 'subroutine project list' to see them, or "
			f"'subroutine project create {asked.rsplit('/', 1)[-1]} \"A title\"' to make it.",
		)

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
		# (`#270`). Told `use hpz2g4`, this looked for a *workspace* of that name on the
		# current connection, did not find one, and reported about somewhere else
		# entirely — while the roster listed `hpz2g4` on the line above.
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


def _project_updated (
	program: Program,
	*,
	key: str,
	title: str,
	description: str,
	status: str,
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

	if not changes:
		program.stop(
			"Nothing to change.", hint="Pass --title, --description, --status or --private."
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


def _workspace_updated (
	program: Program, *, slug: str, title: str, description: str, timezone: str
) -> None:
	"""Change the fields beside a workspace's address — `#434`."""

	changes: dict[str, typing.Any] = {}

	if title is not UNGIVEN:
		changes["title"] = title

	if description is not UNGIVEN:
		changes["description"] = description or None

	if timezone is not UNGIVEN:
		changes["timezone"] = timezone or None

	if not changes:
		program.stop("Nothing to change.", hint="Pass --title, --description or --timezone.")

	with program.opened() as world:
		where = world.writing_to()
		changed = where.client.update_workspace(slug, **changes)

		program.say(f"Changed {changed.slug} — {changed.title}")

		# §6.5 makes this the step every date in the workspace is read through, so the
		# confirmation names the zone in force rather than reporting that something changed.
		if "timezone" in changes:
			program.say(f"  Dates here are read in {changed.timezone or 'the instance zone'}.")


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
		kind: str = typer.Option(
			"", "--type", help="task, bug, feature, chore, spike. Defaults to task."
		),
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

		  subroutine add "Pay the rent" --due "30 aug" --repeat "every month on the 30th"

		  subroutine add "Water the plants" --repeat "every 3 days" --repeat-from completion

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
				# **This machine's zone, for the reason `today` states in the same words**
				# (§13.7): resolved here so every connection reads "friday" as the same day.
				# It was not passed at all, so each instance applied its own notion of the
				# caller — a person whose work profile says America/New_York and whose
				# personal one says Europe/London filed *two different Fridays* and then
				# merged them into one agenda that had already decided which day it was.
				timezone=world.settings.default_timezone,
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

			_suggest(console, "subroutine today")

	@app.command()
	def today (
		json_output: bool = typer.Option(False, "--json", help="Print the agenda as JSON."),
		strict: bool = typer.Option(
			False, "--strict", help="Stop if any connection cannot be reached."
		),
	) -> None:
		"""Show what you are doing today.

		Examples:

		  subroutine today

		  subroutine -w work today
		"""

		# **`-w` precedes the command**, because it is an application-wide option: it changes
		# what every command means, not what this one does. `subroutine today -w work` is
		# therefore refused by Typer as an unknown option, which is correct and is also the
		# order most people will try first — so the example above is written the working way
		# round rather than the natural-reading way.

		with program.opened(strict=strict) as world:
			asked = agenda_asked(
				world, workspace=selected.workspace, now=subroutine.db.types.utcnow()
			)

			gathered = subroutine.fanout.gather(
				world.clients, lambda client: client.agenda(**asked), strict=strict
			)

			_report(program, world, gathered.failures)

			if json_output:
				say(json.dumps(_agenda_json(world, gathered), indent=2))

				return

			_render(world, gathered, say=say, console=console)

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
		project: str = typer.Option("", "--project", help="Only this project, by key."),
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
		assignee: str = typer.Option(
			"", "--assignee", help="Only what is assigned to somebody. A username, or 'me'."
		),
		status: str = typer.Option("", "--status", help="Only this status, e.g. 'blocked'."),
		kind: str = typer.Option("", "--type", help="Only this type, e.g. 'bug'."),
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

		  subroutine list --filter created_at.gte=yesterday

		  subroutine list --filter completed_at.gte=2026-08-02 --filter completed_at.lt=today
		"""

		_refuse_words(program, words, looking_for)

		_listed(program,
			limit=limit,
			json_output=json_output,
			merged=merged,
			strict=strict,
			order=order or None,
			project=project or None,
			connection=connection or None,
			deferred=deferred,
			ready=ready,
			trash=trash,
			assignee=assignee or None,
			status=status or None,
			# **`kind` locally, `--type` to the user, `type=` to the client.** `type` is a
			# builtin and shadowing it inside a function that also annotates with `str | None`
			# is how a signature comes to mean something it does not.
			type=kind or None,
			filters=_filters(program, dated),
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
		project: str = typer.Option("", "--project", help="Only this project, by key."),
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
		"""Find things by their words — in the title, and in what you wrote about them.

		Searches tasks and documents together, like 'subroutine list', because one number
		names either and a search that found only half of them would be lying about the rest.

		Examples:

		  subroutine search "dentist"

		  subroutine search "pagination" --project SR

		  subroutine search "boiler" --filter created_at.gte=yesterday
		"""

		_listed(program,
			limit=limit,
			json_output=json_output,
			merged=merged,
			strict=strict,
			order=order or None,
			project=project or None,
			connection=connection or None,
			deferred=deferred,
			q=_asked(terms, "What are you looking for?"),
			filters=_filters(program, dated),
		)

	@app.command()
	def changes (
		since: int | None = typer.Option(
			None, "--since", help="Carry on from this number, printed by the last run."
		),
		mine: bool = typer.Option(
			False, "--mine", help="Only what this machine's own credential did."
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

		Examples:

		  subroutine changes

		  subroutine changes --since 412

		  subroutine changes --mine
		"""

		with program.opened(strict=strict) as world:
			# **A number belongs to one instance.** Every connection counts its own events from
			# one, so resuming from 412 against two of them would mean two different places in
			# two different histories — and the half that was wrong would look like an ordinary
			# quiet week rather than an error.
			if since is not None and len(world.reached) > 1:
				stop(
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
					since=since, mine=mine, newest=since is None, limit=limit
				)

			gathered = subroutine.fanout.gather(world.clients, ask, strict=strict)

			if json_output:
				say(
					json.dumps(
						[
							{"connection": name, **event.model_dump(mode="json")}
							for name, event in _across(world, gathered, lambda events: events)
						],
						indent=2,
					)
				)

				return

			_say_changes(world, gathered, console=console, say=say)

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
		project: str = typer.Option("", "--project", help="Only this project, by key."),
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
		assignee: str = typer.Option(
			"", "--assignee", help="Only what is assigned to somebody. A username, or 'me'."
		),
		status: str = typer.Option("", "--status", help="Only this status, e.g. 'blocked'."),
		kind: str = typer.Option("", "--type", help="Only this type, e.g. 'bug'."),
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

		_listed(program,
			limit=limit,
			json_output=json_output,
			merged=merged,
			strict=strict,
			order=order or None,
			project=project or None,
			connection=connection or None,
			deferred=deferred,
			ready=ready,
			trash=trash,
			assignee=assignee or None,
			status=status or None,
			type=kind or None,
			filters=_filters(program, dated),
		)

	@app.command()
	def show (
		which: str = typer.Argument("", help="An item number, as shown by 'subroutine list'."),
		history: bool = typer.Option(False, "--history", help="Every change, newest first."),
		json_output: bool = typer.Option(False, "--json", help="Print as JSON."),
	) -> None:
		"""Read one item — what it is, what it is joined to, and what happened to it.

		Works on a task or on a document, because one counter per workspace serves both and
		a number on a command line does not say which it is.

		Examples:

		  subroutine show 42

		  subroutine show 42 --json

		  subroutine show 42 --history
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

			# Asked for separately rather than embedded, because both are sub-resources over
			# HTTP and pretending otherwise here would make the local client the only one that
			# could answer in a single call — which is exactly the divergence S3-07 removed.
			links = client.links(
				ref=located.ref,
				entity_type=located.entity_type,
				workspace=located.workspace,
			)
			remarks = client.comments(
				ref=located.ref,
				entity_type=located.entity_type,
				workspace=located.workspace,
			)

			# **Completed children included**, unlike every listing here. A parent showing two
			# of its four children because the other two are finished would misreport the
			# thing somebody opened it to see. `#84` says report the rollup and leave
			# completion an act; this is where the rollup is read.
			children = (
				client.tasks(
					parent=located.ref,
					workspace=located.workspace,
					limit=MAX_CHILDREN,
					include_completed=True,
					order="ref",
				)
				if located.entity_type == "task"
				else []
			)

			events = (
				client.history(
					ref=located.ref,
					entity_type=located.entity_type,
					workspace=located.workspace,
				)
				if history
				else []
			)

			if json_output:
				say(
					json.dumps(
						_shown_as_json(world, located, links, remarks, children, events),
						indent=2,
					)
				)

				return

			_render_item(world, located, links, remarks, children, events, console=console)
			say("")

			# **What to do next depends on where it is** (`#700`). Inviting somebody to comment
			# on something in the trash offers the one act that changes nothing anybody will
			# read; `restore` is the question they actually have, and it is the same command
			# `list --trash` already ends with, so the two agree about what a deleted row is
			# for.
			addressed = world.address_of_located(located).replace(
				subroutine.domain.refs.SIGIL, ""
			)

			_suggest(
				console,
				f"subroutine restore {addressed}"
				if located.item.deleted_at is not None
				else f'subroutine comment {addressed} "what happened"',
				"put it back" if located.item.deleted_at is not None else None,
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

		with program.opened() as world:
			located, task = _a_task(program,
				world,
				_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
				verb="claim",
			)
			client = _require_connection(program, world, located.connection)

			try:
				held = client.claim(
					ref=task.ref,
					minutes=minutes or None,
					workspace=located.workspace,
				)

			except subroutine.errors.SubroutineError as error:
				fail(error)

			say(_acted(world, dataclasses.replace(located, item=held), "Claimed"))
			_suggest(console, "subroutine list --ready", "what is free to start")

	@app.command("release", hidden=not _worth_showing(settings))
	def release_item (
		which: str = typer.Argument("", help="A task number, as shown by 'subroutine list'."),
	) -> None:
		"""Put something back, so somebody else can pick it up.

		Examples:

		  subroutine release 42

		Releasing something nobody had claimed is not an error, so this is safe to run when you
		are not sure. Anybody who can change the task can release it — which is what makes an
		agent that died mid-task somebody else's problem to solve rather than nobody's.
		"""

		with program.opened() as world:
			located, task = _a_task(program,
				world,
				_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
				verb="release",
			)
			client = _require_connection(program, world, located.connection)

			try:
				freed = client.release(ref=task.ref, workspace=located.workspace)

			except subroutine.errors.SubroutineError as error:
				fail(error)

			say(_acted(world, dataclasses.replace(located, item=freed), "Released"))

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

		_moved_to(program, which, "open", verb="stop", said="Stopped")

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

		with program.opened() as world:
			located, task = _a_task(program,
				world,
				_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
				verb="done",
			)

			if task.completed_at is not None:
				# Saying so beats reporting success twice. The case this is really about is
				# an up-arrow repeat, which used to land on whatever had taken that number.
				say(_acted(world, located, "Already done"))
				_suggest(console, "subroutine list", "everything still open")

				return

			client = _require_connection(program, world, located.connection)
			finished = client.complete(ref=task.ref, workspace=located.workspace)

			_because(client, located, because, what="Done")

			say(_acted(world, dataclasses.replace(located, item=finished), "Done"))
			_suggest(console, "subroutine today")

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
			_suggest(console, "subroutine today")

	@app.command()
	def plan (
		which: str = typer.Argument("", help="A task number, as shown by 'subroutine list'."),
		when: str = typer.Argument("", help="A day — 'today', 'tomorrow', 'friday', '2026-08-01'."),
		because: str = typer.Option("", "--because", help="Why, recorded against it."),
	) -> None:
		"""Say which day you will do something.

		Examples:

		  subroutine plan 1 tomorrow

		  subroutine plan 42 friday --because "the review is on monday"
		"""

		with program.opened() as world:
			located, task = _a_task(program,
				world,
				_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
				verb="plan",
			)
			client = _require_connection(program, world, located.connection)

			changed = client.schedule(
				ref=task.ref,
				workspace=located.workspace,
				starts=_day(world, _asked(when, "Which day?")),
			)

			# The planned day, not `_when`'s answer. `_when` prefers a deadline, which is
			# right in a list and wrong in the confirmation of a command whose whole job was
			# to set the other field — the user said "tomorrow" and was shown Friday.
			planned = f"Starts {_render_date(changed.starts_at, changed.timezone)}"

			_because(client, located, because, what=planned)

			say(_acted(world, dataclasses.replace(located, item=changed), planned))
			_suggest(console, "subroutine today")

	@app.command()
	def defer (
		which: str = typer.Argument("", help="A task number, as shown by 'subroutine list'."),
		when: str = typer.Argument("", help="A day to hide it until, or a day and a time."),
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
				snooze=_moment(world, _asked(when, "Hide it until when?")),
			)

			hidden = f"Hidden until {_when_rendered(changed)}"

			_because(client, located, because, what=hidden)

			say(_acted(world, dataclasses.replace(located, item=changed), hidden))
			_suggest(console, "subroutine today")

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

		# **Neither, or both, is a refusal rather than a default**, which is `project move`'s
		# rule and the endpoint's: an omitted destination that meant "move to the top" would
		# flatten a tree by accident, and there is nothing that records where it was.
		if bool(under) == top:
			stop(
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
					stop(
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

			say(_acted(world, dataclasses.replace(located, item=changed), f"Now {where}"))

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
		kind: str = typer.Option("", "--type", help="task, bug, feature, chore, spike."),
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
		"""

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
				stop(
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
			stop(
				"Nothing to change.",
				"Name a field: --title, --description, --importance, --urgency, "
				"--estimate, --type, --status or --repeat.",
			)

		with program.opened() as world:
			located, task = _a_task(program,
				world,
				_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
				verb="update",
			)
			client = _require_connection(program, world, located.connection)
			changed = client.update(ref=task.ref, workspace=located.workspace, **changes)
			now = dataclasses.replace(located, item=changed)

			_because(client, located, because, what="Changed")

			if json_output:
				say(json.dumps(_as_json(world, now.connection, now.item), indent=2))

				return

			say(_acted(world, now, "Changed"))
			_suggest(
				console,
				f"subroutine show "
				f"{world.address_of_located(now).replace(subroutine.domain.refs.SIGIL, '')}",
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
				fail(error)

			if not recorded:
				stop(
					f"Nothing recorded on {world.address_of_located(located)} says that.",
					f"Run 'subroutine show {located.ref}' to see what is there.",
				)

			# **Refused rather than resolved, and the several are not listed back.** Printing
			# them would put the reader in the position of choosing by position, which is the
			# one way of naming things this program does not have (§12.2a) — so the answer is
			# to be more specific, and the count is what says how much more.
			if len(recorded) > 1:
				stop(
					f"{len(recorded)} comments there say that.",
					"Say more of the one you mean.",
				)

			client.uncomment(
				ref=located.ref,
				comment_id=str(recorded[0].id),
				entity_type=located.entity_type,
				workspace=located.workspace,
			)

			say(_acted(world, located, "Taken out of"))
			_suggest(
				console,
				f"subroutine show "
				f"{world.address_of_located(located).replace(subroutine.domain.refs.SIGIL, '')}",
			)

	# **`doc create` and no `doc list` or `doc show`**, which is §12.2's shape rather than an
	# omission: one counter per workspace serves both kinds (§6.2), so `list` already holds
	# documents and `show <ref>` already reads either. A second listing would be a second
	# answer to a question already answered, and the *first* listing is what taught somebody
	# that a number names an item.
	document_app = typer.Typer(
		help="Write down what you concluded.", no_args_is_help=True
	)
	app.add_typer(document_app, name="doc")

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
	) -> None:
		"""Write a document — a decision, a finding, a design, a dead end.

		A decision, a finding and a dead end are in force the moment you write them, so they
		start as 'active'; a specification or a design starts as 'draft'. Pass '--status draft'
		for a decision you are still thinking about.

		Examples:

		  subroutine doc create "Why we dropped the queue" --type decision

		  cat notes.md | subroutine doc create "Review findings" --type finding

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
				say(json.dumps(created.model_dump(mode="json"), indent=2))

				return

			# **A slug, not the id** (`#289`). `Located.workspace` is what `refs.format_address`
			# composes `connection/workspace/ref` from, so the id rendered an address nobody
			# could type: `local/019fad98-4313-7e36-b972-f7decf66f8ae/#288`. Every other caller
			# of `_acted` passes a slug, and `add` gets it from this same function.
			say(
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
				console,
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
	) -> None:
		"""Revise a document you have already written.

		Examples:

		  subroutine doc edit 42

		  subroutine doc edit 42 --title "What we settled, and why"

		  cat revised.md | subroutine doc edit 42

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
			named = any((title.strip(), kind.strip(), status.strip(), project.strip()))
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
						fail(
							subroutine.errors.ValidationError(
								"Nothing was piped in, so there is nothing to change.",
								hint="Pipe the new text in, or pass --body, --title, --type, "
								"--status or --project.",
							)
						)

			where = world.connection(located.connection)

			if where is None:
				fail(
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
			)

			if json_output:
				say(json.dumps(changed.model_dump(mode="json"), indent=2))

				return

			say(
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
				console,
				f"subroutine show {_typeable(world, located.connection, changed)}",
				"read it back",
			)

	@app.command("link")
	def link_items (
		which: str = typer.Argument("", help="Which item, by its number."),
		relation: str = typer.Argument("", help="blocks, relates-to, duplicates, derives-from."),
		other: str = typer.Argument("", help="The other item, by its number."),
	) -> None:
		"""Say how two items are related.

		Examples:

		  subroutine link 42 blocks 43

		  subroutine link 42 relates-to 12

		'blocks' is the one that changes what you see: 'subroutine list --ready' leaves out
		anything blocked by unfinished work, so this is how that filter learns anything.
		"""

		# Hyphens read better than underscores at a command line and the seeded keys use
		# underscores; accepted either way rather than making somebody guess which.
		wanted = _asked(relation, "How are they related?").strip().replace("-", "_")

		with program.opened() as world:
			near = _locate(program, world, _asked(which, "Which one?"), kinds=ANY_ITEM, verb="link")
			far = _locate(program, world, _asked(other, "And the other one?"), kinds=ANY_ITEM, verb="link")
			where = world.writing_to()

			made = where.client.link(
				ref=near.ref,
				link_type=wanted,
				target=far.ref,
				entity_type=near.entity_type,
				target_type=far.entity_type,
				workspace=near.workspace,
			)

			say(f"{made.label}: {made.other.title}")
			_suggest(
				console,
				f"subroutine show {_typeable(world, near.connection, near.item)}",
				"see everything it is joined to",
			)

	@app.command("unlink")
	def unlink_items (
		which: str = typer.Argument("", help="Which item, by its number."),
		other: str = typer.Argument("", help="The item it is joined to, by its number."),
	) -> None:
		"""Undo a link between two items.

		Examples:

		  subroutine unlink 42 43

		Worth having beside 'link' rather than later. A link added by mistake blocks work that
		is not blocked, and --ready then hides it — so an unwanted link is worse than a missing
		one, because it narrows what looks startable and says nothing about doing so.
		"""

		with program.opened() as world:
			near = _locate(program, world, _asked(which, "Which one?"), kinds=ANY_ITEM, verb="unlink")
			far = _locate(program, world, _asked(other, "And the other one?"), kinds=ANY_ITEM, verb="unlink")
			where = world.writing_to()

			# **Found by the pair rather than asked for by id.** A link's id is a UUID that
			# appears in no listing a person reads, so requiring one would make this a command
			# only a script could run — and `show` prints the two refs, which is what somebody
			# actually has in front of them.
			joins = [
				one
				for one in where.client.links(
					ref=near.ref, entity_type=near.entity_type, workspace=near.workspace
				)
				if one.other.ref == far.ref
			]

			if not joins:
				# **The shortest address that resolves, not the absolute one.** A refusal is
				# written when something has already gone wrong and is the last output anybody
				# re-reads for stray vocabulary — printing `personal/#1` at somebody with one
				# workspace introduces the word in an error message, about a to-do list. Same
				# §1.4 leak `_in_place` exists for.
				stop(
					f"{world.address_of_located(near)} is not joined to "
					f"{world.address_of_located(far)}.",
					f"Run 'subroutine show {near.ref}' to see what it is joined to.",
				)

			for one in joins:
				where.client.unlink(
					ref=near.ref,
					link_id=str(one.id),
					entity_type=near.entity_type,
					workspace=near.workspace,
				)

			say(f"Unlinked: {joins[0].other.title}")
			_suggest(console, f"subroutine show {_typeable(world, near.connection, near.item)}")

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
			located = _locate(program, world, _asked(which, "Which one?"), kinds=ANY_ITEM, verb="delete")
			where = world.writing_to()

			gone = where.client.discard(
				ref=located.ref,
				entity_type=located.entity_type,
				workspace=located.workspace,
			)

			say(_acted(world, dataclasses.replace(located, item=gone), "Deleted"))

			# **The remedy, not a reassurance.** "It can be restored" is a claim the reader has
			# to trust; the command that does it is one they can run. Printed with the ref
			# because after this the item is out of every listing, so the number on screen is
			# the only way back to it.
			_suggest(
				console,
				f"subroutine restore {_typeable(world, located.connection, located.item)}",
				"put it back",
			)

	@app.command("restore")
	def undiscard_item (
		which: str = typer.Argument("", help="Which one, by its number."),
	) -> None:
		"""Put something back that was deleted.

		Examples:

		  subroutine restore 42

		  subroutine list --deleted
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
			_suggest(console, "subroutine today")

	# **Visible, unlike `use` and `connections` below.** Progressive disclosure (§1.4) is
	# about never *requiring* a project in order to keep a to-do list, not about hiding the
	# noun — `subroutine list --project SR` already names it, and until 2026-07-31 there was
	# no way to make one outside the HTTP API, so on a default install the only project
	# anybody would ever have was the Inbox (`#134`). A hidden command would have left that
	# wall standing with the door merely painted over.
	project_app = typer.Typer(
		help="Group work into projects.", no_args_is_help=True
	)
	app.add_typer(project_app, name="project")

	@project_app.command("create")
	def project_create (
		key: str = typer.Argument(..., help="Its permanent short name, like WEB."),
		title: str = typer.Argument(..., help="What it is called."),
		description: str = typer.Option("", "--description", help="What it is for."),
		parent: str = typer.Option("", "--parent", help="Put it inside this project."),
		private: bool = typer.Option(
			False, "--private", help="Only its members can see it."
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
				say(json.dumps(created.model_dump(mode="json"), indent=2))

				return

			say(f"Created {created.key} — {created.title}")

			# **The next command is the one that uses it**, not another one about projects.
			# A project nobody files anything into is an empty gesture, and `+KEY` is the part
			# of the capture grammar somebody who has just made one has no reason to know.
			#
			_suggest(console, f'subroutine add "something to do +{_capture_name(world, created)}"')

	@project_app.command("list")
	def project_list (
		json_output: bool = typer.Option(False, "--json", help="Print the list as JSON."),
	) -> None:
		"""Show the projects you can see, with what is inside what.

		Examples:

		  subroutine project list
		"""

		with program.opened() as world:
			where = world.writing_to()
			found = where.client.projects(workspace=_writing_workspace(world))

			if json_output:
				say(json.dumps([one.model_dump(mode="json") for one in found], indent=2))

				return

			if not found:
				say("No projects here yet.")
				_suggest(console, 'subroutine project create WEB "Website redesign"')

				return

			# Indented by depth, which is why the listing is ordered by path rather than by
			# name: a child follows its parent, so the shape can be printed in one pass.
			width = max(len(one.key) + one.depth * 2 for one in found)

			for one in found:
				shown = f"{'  ' * one.depth}{one.key}".ljust(width)

				say(f"{shown}  {one.title}")

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
			private=private,
		)

	workspace_app = typer.Typer(
		help="Look after the spaces work is kept in.", no_args_is_help=True
	)
	app.add_typer(workspace_app, name="workspace")

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

			say(f"Created {created.slug} — {created.title}")

			# **Said because it is the surprising part.** Everything reachable is still listed,
			# but a *write* goes to one place (§13.7), so a new workspace is not where the next
			# `add` lands until somebody says so.
			_suggest(console, f"subroutine use {created.slug}", "work in it")

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
			program, slug=slug, title=title, description=description, timezone=timezone
		)

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

	# **Membership lives under `user`, and there is deliberately no `workspace` group**
	# (`#174`). Adding one would put the word "workspace" in the top-level help of somebody
	# who has a to-do list and no colleagues, which is what §1.4 forbids — while `user` is a
	# word anybody can read and ignore. The workspace is still where a membership *lives*;
	# it is named by `--workspace` when there is more than one, and inferred otherwise.
	user_app = typer.Typer(
		help="Add the people and agents this instance is for.", no_args_is_help=True
	)
	app.add_typer(user_app, name="user")

	@user_app.command("create")
	def user_create (
		username: str = typer.Argument(..., help="What they will be called here."),
		display_name: str = typer.Option("", "--name", help="Their full name."),
		email: str = typer.Option("", "--email", help="Their email address."),
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
		"""Add somebody to this instance.

		Examples:

		  subroutine user create thomas --name "Thomas Anderson"

		  subroutine user create thomas --name "Thomas Anderson" --email thomas@example.com

		  subroutine user create sam --superuser

		'--superuser' is what lets somebody create accounts and workspaces, and it is the only
		way to grant that — no role carries it. Until '#701' there was exactly one, made by
		'init', and no way to a second, which left an instance with nobody to hand over to.

		A new account belongs to no workspace yet, and until it does there is nothing it can
		see. 'subroutine user add' is the second half, and this command says so when it is
		done rather than leaving somebody with an account that appears not to work.

		There is no password. Subroutine authenticates with tokens, so what a new person needs
		next is one of those.
		"""

		with program.opened() as world:
			where = world.writing_to()

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

			settled = _keep_the_operators_own_list(world, before)

			if json_output:
				say(json.dumps(created.model_dump(mode="json"), indent=2))

				return

			say(f"Created {created.username}")

			if settled is not None:
				say(f"Local commands will go on acting as {settled}.")

			# **The next command is the one that makes the account useful.** An account with
			# no membership can see nothing at all, so stopping at "Created" would leave
			# somebody with a person who appears to be broken.
			_suggest(console, f"subroutine user add {created.username} --role member")

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

			if workspace.strip():
				members = where.client.members(workspace=workspace.strip())
				rows = [member.columns() for member in members]
				payload = [member.model_dump(mode="json") for member in members]

			else:
				accounts = where.client.users()
				rows = [account.columns() for account in accounts]
				payload = [account.model_dump(mode="json") for account in accounts]

			if json_output:
				say(json.dumps(payload, indent=2))

				return

			if not rows:
				say("Nobody here yet.")
				_suggest(console, "subroutine user create thomas")

				return

			for line in _tabulated(rows):
				say(line)

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
			fail(
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

			say(f"{joined.user.username} is now {joined.role} in {joined.workspace.slug}")

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
				say(f"This also stops {len(stopping)} agent(s): {', '.join(stopping)}")

				if not typer.confirm(f"Mark {username} as having left?"):
					say("Left as they were.")

					return

			where.client.set_active(username=username, active=False)

			say(f"{username} is marked as having left")

			for name in stopping:
				say(f"  {name} has stopped")

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

			say(f"{username} is active again")

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

			say(f"{to} now answers for {username}")

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

			say(f"{username} is no longer a member of {chosen}")

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
			_suggest(console, "subroutine today")

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
		with program.opened(strict=strict) as world:
			gathered = subroutine.fanout.gather(
				world.clients, lambda client: client.me(), strict=strict
			)

			if json_output:
				say(
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
					say("")

				if world.qualifies_connection:
					console.print(rich.text.Text(answer.connection.label, style=HEADING))

				for line in _whoami_lines(answer.value):
					console.print(line)

				# **A footer, and per connection rather than once** (`#381`). The program is
				# the same for every block and the *instance* is not, so the one line that
				# would be repeated is also the one that has to sit beside the answer it
				# describes — a single trailing line naming three connections' versions is
				# unreadable, and worse, is read as applying to whichever block is nearest.
				console.print("")

				for line in subroutine.views.versions(
					answer.value,
					program=subroutine.installations.program(),
					plugin=subroutine.installations.plugin(),
				):
					console.print(line)

			_report(program, world, gathered.failures)

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

		resolved = settings()
		current = None

		try:
			roster = subroutine.connections.roster(resolved)

			# Resolved without opening anything, deliberately: this is the command somebody
			# runs when a connection is *not* working, so it must not need one to answer.
			current = subroutine.context.resolve(
				roster,
				connection=selected.connection,
				workspace=selected.workspace,
				marker=subroutine.directory.find(),
			)

		except subroutine.errors.SubroutineError as error:
			fail(error)

		_warn_about_the_credentials_file(warn)

		# Named per connection, because a person with three of them needs to know *which*.
		for exposed in roster:
			if subroutine.connections.in_the_clear(exposed):
				warn(
					f"{exposed.name} is reached over plain http, so its token crosses the "
					f"network readable by anything in between."
				)

		rows = [_connection_row(program, connection, roster, resolved, current) for connection in roster]
		widths = [max(len(row[column]) for row in rows) for column in range(3)]

		for row in rows:
			say(
				f"{row[0].ljust(widths[0])}  {row[1].ljust(widths[1])}  "
				f"{row[2].ljust(widths[2])}  {row[3]}"
			)

		# **Where it came from, when the two answers differ** (`#278`). One word in a column
		# cannot say why, and why is the whole question when somebody has just watched a write
		# land somewhere they did not expect. Silent when they agree, which is the ordinary
		# case and needs no explanation.
		if current.connection != roster.default:
			say("")
			say(f"Writing to {current.describe(qualified=roster.qualifies)}.")

		say("")
		_suggest(console, "subroutine use")

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

		try:
			wanted = subroutine.connections.check_name(name)
			roster = subroutine.connections.roster(resolved)

		except subroutine.errors.SubroutineError as error:
			fail(error)

		if wanted == subroutine.connections.LOCAL_NAME:
			stop(
				f"{subroutine.connections.LOCAL_NAME!r} already means this machine's own "
				"database, so it cannot name another instance.",
				"Give it a name of its own, as in 'subroutine connections add work'.",
			)

		# **The file's names rather than the roster's**, because a connection turned off is
		# still in the file: adding a second table under that name would leave the meaning of
		# the file to whichever one TOML kept.
		if wanted in subroutine.connections.declared_names():
			stop(
				f"There is already a connection called {wanted!r}.",
				f"Choose another name, or edit {subroutine.config.config_file_path()} to "
				"change that one.",
			)

		if not url.strip():
			stop(
				f"Say where {wanted!r} is.",
				f"For example: subroutine connections add {wanted} --url "
				"https://tasks.example.com",
			)

		try:
			address = subroutine.connections.check_url(url)

		except subroutine.errors.SubroutineError as error:
			fail(error)

		connection = subroutine.connections.Connection(
			name=wanted,
			url=address,
			read_only=read_only,
			token_env=token_env.strip() or None,
			token_command=token_command.strip() or None,
		)

		# **Whether this connection is where writes go, decided before the token is resolved**
		# — because the answer changes which environment variable applies. A bare
		# SUBROUTINE_TOKEN belongs to the default connection (§12.3a), so asking with the old
		# default would prompt for a token the machine already has, and store a second copy of
		# it in a file.
		#
		# Automatic on a machine with no instance of its own, which is the case this command
		# exists for: somebody's second laptop, reaching work. Leaving the default at a
		# database that does not exist would make the very next 'add' fail, and the person has
		# just said where their work is. Never automatic when there is a local database,
		# because that is somebody's own list and moving their writes off it is their call.
		alone = roster.names == (subroutine.connections.LOCAL_NAME,)
		leads = default or (alone and resolved.has_no_instance_yet())

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

		if reached is not None and reached.instance is not None:
			twice = _already_reached(roster, resolved, reached.instance.id)

			if twice is not None:
				# **Refused here, where it is one word to change.** Left to be discovered, it is
				# discovered by `subroutine list` refusing outright — the instance is counted
				# once per name it is configured under, so a merged read cannot be trusted and
				# the whole listing is withheld. That message can only tell somebody to edit a
				# file, which is the friction this command exists to remove.
				#
				# It is the *check* that finds this, so `--no-check` passes it by. That is the
				# escape hatch and not a recommendation: `#327` is where two connections naming
				# one instance becomes a workable arrangement rather than a broken machine.
				stop(
					f"{wanted!r} is the same instance as {twice!r}, which this machine already "
					"reaches.",
					f"Use {twice!r} instead. Two names for one instance would make every "
					"merged listing count its work twice, so this machine holds one.",
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

		except (OSError, ValueError) as error:
			stop(
				f"{wanted!r} could not be written to "
				f"{subroutine.config.config_file_path()}: {error}",
				"Check that the file is writable, then try again.",
			)

		if reached is not None:
			say(_describing(reached))

		say(f"Added {wanted} to {written}")

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
			say(f"New work goes to {wanted} now, because this machine has no list of its own.")

		say("")
		_suggest(console, "subroutine list", "everything this machine can now reach")

	def show_today () -> None:
		"""Print today's agenda, as a bare ``subroutine`` invocation does."""

		today(json_output=False, strict=False)

	return show_today, selected


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


def _render (
	world: World,
	gathered: subroutine.fanout.Gathered[subroutine.views.Agenda],
	*,
	say: typing.Callable[[str], None],
	console: rich.console.Console,
) -> None:
	"""Print the agenda, merged across connections and addressed so it can be typed back.

	**Merged rather than grouped by connection, and deliberately.** §13.7 exists so that a
	developer keeping their own to-do list here and their team's on a company server sees the
	dentist and the stand-up *in one place*; a heading per connection would put them in two.
	The labelling rule is satisfied per row instead, which is what ``address_of`` is for.
	"""

	buckets = AGENDA_SECTIONS
	rows = agenda_rows(world, gathered)

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
	printed = False
	first: Row | None = None

	if not rows.get("overdue") and not rows.get("today"):
		say("Nothing due today.")

	for heading, field, late in buckets:
		group = rows.get(field) or []

		if not group:
			continue

		if printed:
			say("")

		console.print(rich.text.Text(heading, style=LATE if late else HEADING))
		printed = True
		first = first or group[0]

		for connection, task in group:
			console.print(
				_item_line(world, connection, task, late=late, columns=columns)
			)

	if remaining > 0:
		console.print(rich.text.Text(f"      and {remaining} more unscheduled", style=DETAIL))

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
	where the command already reads as English — a line explaining that ``subroutine today``
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


def _render_item (
	world: World,
	located: Located,
	links: typing.Sequence[subroutine.views.Link],
	remarks: typing.Sequence[subroutine.views.Comment],
	children: typing.Sequence[subroutine.views.Task] = (),
	events: typing.Sequence[subroutine.views.Event] = (),
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
			rich.text.Text(f"Parts  ({done} of {len(children)} done)", style=HEADING)
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
			if link.link_type == "blocks" and link.direction == "incoming"
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
			line.append(link.other.title, style=DETAIL if link.other.is_complete else "")
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
			line.append(f"  {remark.created_at.date().isoformat()}  ", style=DETAIL)
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
			line.append(f"  {event.created_at.date().isoformat()}  ", style=DETAIL)
			line.append(_event_line(event))
			console.print(line)


def _event_line (event: subroutine.views.Event) -> str:
	"""Return one event as a sentence somebody can read.

	**The subject, not the entity, decides how it reads.** Since `#52` a comment's event names
	the comment and carries the commented-on item as its subject, so "created comment" is the
	shape a history sees — and "commented" is what a person means by it.
	"""

	if event.subject_type is not None:
		return {"created": "commented", "updated": "edited a comment"}.get(
			event.action, f"{event.action} a comment"
		)

	if event.action != "updated" or not event.changes:
		return event.action

	# **The field names, not the values.** A history is a list of what moved; the values are in
	# the item itself, one line above, and a `from`/`to` pair per field would make the commonest
	# entry the longest one.
	return "changed " + ", ".join(sorted(event.changes))


def _facts (located: Located) -> list[str]:
	"""Return the things worth saying about an item beyond its title, and nothing more.

	Each entry earns its place by having been *chosen*. A task of the default type says
	nothing about its type; one filed in the Inbox says nothing about its project; a status
	in the ``open`` category is the absence of news and is left out, while a completed one is
	reported as a date because that is the fact somebody wants.
	"""

	facts: list[str] = []
	item = located.item

	if item.type not in ("task", "note"):
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
		facts.append(item.status)

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

		# **Reported whether or not it has passed**, unlike `_when` below. A defer somebody
		# set is a decision they made, and one that has since come round is still the answer
		# to "why was this not on my list in June" — where a field that erased itself on
		# arrival would leave that question permanently unanswerable.
		if item.snoozed_until is not None:
			facts.append(f"from {_render_date(item.snoozed_until, item.timezone)}")

		if item.due_at is not None:
			facts.append(f"due {_render_date(item.due_at, item.timezone)}")

		if item.starts_at is not None:
			facts.append(f"starts {_render_date(item.starts_at, item.timezone)}")

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
			facts.append(f"@{item.assignee}")

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
		deferred = f"from {_render_date(task.snoozed_until, task.timezone)}"

		if task.due_at is not None:
			return f"  ({deferred}, due {_render_date(task.due_at, task.timezone)})"

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
			None
			if task.starts_at is None
			else f"starts {_render_date(task.starts_at, task.timezone)}",
			None if task.due_at is None else f"due {_render_date(task.due_at, task.timezone)}",
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

	local = instant.astimezone(
		subroutine.domain.dates.zone(timezone or subroutine.domain.schedule.DEFAULT_TIMEZONE)
	)

	# Through the same function as a calendar date, so an instant and a day cannot come to
	# disagree about when a year is worth printing — one rule, one place.
	return _dated(local.date())


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

	day = _render_date(task.snoozed_until, task.timezone)

	if task.snoozed_is_all_day or task.snoozed_until is None:
		return day

	local = task.snoozed_until.astimezone(
		subroutine.domain.dates.zone(
			task.timezone or subroutine.domain.schedule.DEFAULT_TIMEZONE
		)
	)

	return f"{day} at {local.strftime('%H:%M')}"


def _as_json (
	world: World, connection: str, item: Item
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
		"snoozed_until": None if task.snoozed_until is None else task.snoozed_until.isoformat(),
		"importance": task.importance,
		"urgency": task.urgency,
		"estimate_minutes": task.estimate_minutes,
		# **Who has it** (`#583`). `#511` put the assignee on the terminal row and stopped
		# there, so the one reader most likely to be automating a handover — a script, or an
		# agent reading this listing — could not see that anything had been handed over.
		"assignee": task.assignee,
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
		# **What it is part of**, which the terminal shows as `↳ #12`. A sub-task read on its
		# own is work whose context is one field away, and the number is what a script types
		# back.
		"parent_ref": task.parent_ref,
	}


def _shown_as_json (
	world: World,
	located: Located,
	links: typing.Sequence[subroutine.views.Link],
	remarks: typing.Sequence[subroutine.views.Comment],
	children: typing.Sequence[subroutine.views.Task] = (),
	events: typing.Sequence[subroutine.views.Event] = (),
) -> dict[str, typing.Any]:
	"""Return one item, its links and its record, as the scripted path sees it.

	The **whole** view model rather than the handful of fields ``_as_json`` selects for a
	listing, because the reason to ask about one item is to read what a listing left out —
	and a caller who has already named the item is not paying for a page of them.
	"""

	return {
		"address": world.address_of_located(located),
		"connection": located.connection,
		"entity_type": located.entity_type,
		"item": located.item.model_dump(mode="json"),
		"links": [link.model_dump(mode="json") for link in links],
		"comments": [remark.model_dump(mode="json") for remark in remarks],
		"children": [child.model_dump(mode="json") for child in children],
		# **Always present, empty when it was not asked for.** A key that appears only with
		# `--history` makes a script test for the key rather than read it, and "absent" and
		# "nothing happened" would then be the same shape for two different facts.
		"history": [event.model_dump(mode="json") for event in events],
	}


def _agenda_json (
	world: World, gathered: subroutine.fanout.Gathered[subroutine.views.Agenda]
) -> dict[str, typing.Any]:
	"""Return the merged agenda as the scripted path sees it.

	``unreachable`` is reported rather than left to be inferred from a short list. A script
	acting on a partial view should be able to tell that it is one — which is the same reason
	``--strict`` exists for a script that would rather not have one at all.
	"""

	buckets = ("overdue", "today", "in_progress", "upcoming", "unscheduled")
	first = gathered.answers[0].value if gathered.answers else None

	return {
		"date": None if first is None else first.date.isoformat(),
		"timezone": None if first is None else first.timezone,
		**{
			field: [
				_as_json(world, name, task)
				for name, task in _across(world, gathered, operator.attrgetter(field))
			]
			for field in buckets
		},
		"unscheduled_total": sum(
			answer.value.unscheduled_total for answer in gathered.answers
		),
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

	named = _ordering(order)

	if order:
		return named[1]

	ranked = any(
		getattr(row[1], subroutine.domain.ordering.RELEVANCE, None) is not None
		for answer in gathered.answers
		for row in answer.value.rows
	)

	if not ranked:
		return named[1]

	# **Descending, and the tiebreak ascending beneath it**, exactly as `clauses` builds it for
	# the query — the whole point of `#879` being that these two agree.
	return ((subroutine.domain.ordering.RELEVANCE, True), ("ref", False))


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


def _in_order (
	rows: list[Row], bucket: str
) -> list[Row]:
	"""Order one agenda bucket the way that bucket is read.

	The dated buckets read by date — soonest first, because that is the order the days arrive
	in. The ref is the tiebreak throughout, so two tasks with the same date do not swap places
	between runs.

	**The two undated buckets read by rank**, which is the same rule ``?order=-priority_score``
	applies, so the agenda and a ranked listing cannot disagree about which item is the one to
	start. `#853`.

	**And this is where the server's ordering was being discarded** — `#71`'s defect, which
	``domain/ordering.py``'s own docstring records: *a ``--order`` flag whose result was
	re-sorted by ``created_at`` one level further up, so the flag chose which items appeared
	and then discarded the arrangement*. It happened again here the moment the agenda started
	ranking: the section came back best-first and this put it back to newest-first, and the
	output looked entirely reasonable.
	"""

	if bucket in ("unscheduled", "in_progress"):
		return subroutine.domain.ordering.merged(
			rows, key=lambda row: row[1], order=(("priority_score", True), ("ref", False))
		)

	# NULLs last, explicitly: a task in `today` may be there for `starts_at` and carry no
	# deadline at all, and it belongs after the ones that do rather than before them.
	#
	# `_deadline` rather than `row[1].due_at` because a listing row may now hold a document,
	# which has no deadline — and an agenda never does. Written as a guard that answers
	# "no deadline" rather than as a cast, so a document reaching here sorts last instead of
	# raising in a lambda inside a sort, which is a traceback nobody can place.
	rows.sort(
		key=lambda row: (
			_deadline(row[1]) is None,
			_deadline(row[1]) or datetime.datetime.max.replace(tzinfo=datetime.UTC),
			row[1].ref,
		)
	)

	return rows


def suggest (command: str, about: str | None = None) -> None:
	"""Print the command to try next, on the shared console (docs/design.md §12.2a).

	The public face of :func:`_suggest`, for callers outside this module that have no console
	of their own — the bare invocation and ``--version`` in ``cli/main``. Kept as one function
	so the styling cannot drift into a second definition, which it had begun to: both of those
	callers used to pad their own explanation into a column, which was a second shape for one
	thing and lined up with nothing else on the screen.
	"""

	_suggest(subroutine.cli.output.Terminal(), command, about)
