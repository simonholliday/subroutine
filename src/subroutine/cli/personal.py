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
import json
import pathlib
import shlex
import sys
import typing

import rich.console
import rich.text
import typer
import typer.core

import subroutine.clients.base
import subroutine.clients.opening
import subroutine.config
import subroutine.connections
import subroutine.context
import subroutine.credentials
import subroutine.db.types
import subroutine.directory
import subroutine.domain.agenda
import subroutine.domain.capture
import subroutine.domain.dates
import subroutine.domain.durations
import subroutine.domain.ordering
import subroutine.domain.refs
import subroutine.domain.schedule
import subroutine.errors
import subroutine.fanout
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

	@property
	def clients (self) -> list[subroutine.clients.base.Client]:
		"""Return every open client, in roster order."""

		return [item.client for item in self.reached]

	@property
	def qualifies_connection (self) -> bool:
		"""Report whether an address here has to name its connection."""

		return len(self.reached) > 1

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
def _column (values: typing.Iterable[str]) -> int:
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
	"""

	distinct = set(values)

	if len(distinct) < 2:
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
	started: int = 0
	priority: int = 0
	estimate: int = 0
	matched: int = 0
	parent: int = 0

	#: What was searched for, so a row can say where it was found. ``None`` on any listing
	#: that was not a search, which is what drops the column entirely.
	term: str | None = None

	@classmethod
	def measured (
		cls, world: World, rows: typing.Sequence[Row], *, term: str | None = None
	) -> "Columns":
		"""Return the widths this page needs."""

		return cls(
			term=term,
			matched=_column(_match_cell(item, term) for _name, item in rows),
			parent=_column(_parent_cell(item) for _name, item in rows),
			address=max(
				(len(world.address_of_item(name, item)) for name, item in rows), default=0
			),
			kind=_column(item.type for _name, item in rows),
			started=_column(_started_cell(item) for _name, item in rows),
			priority=_column(_priority_cell(item) for _name, item in rows),
			estimate=_column(_estimate_cell(item) for _name, item in rows),
		)


#: Marks work somebody is in the middle of. **A word, not a symbol**: decision `#102` says no
#: information exists only in a colour, and the same argument retires a bare glyph — a reader
#: meeting `▶` has to be told what it means, where a reader meeting `doing` does not.
#:
#: Not the word "status", which §13.5b forbids on this path and which nobody needs: `start` and
#: `stop` are actions that happen to set a field, exactly as `done`, `plan` and `defer` are.
STARTED_MARK = "doing"


def _started_cell (item: Item) -> str:
	"""Return the marker for work in progress, or nothing (`#75`).

	**A `start` command whose effect is invisible is half a feature.** The status was reachable
	only over HTTP until now, and adding a way to set it without a way to see it would have
	moved the gap rather than closed it.

	Empty on every row of an ordinary list, which drops the column entirely — the same rule the
	kind, priority and parent columns follow, and what keeps a personal to-do list from looking
	like a database (§1.4, §14.10).
	"""

	if not isinstance(item, subroutine.views.Task):
		return ""

	return STARTED_MARK if item.status_category == "in_progress" else ""


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
	"""

	if not term:
		return ""

	wanted = term.casefold()

	if wanted in item.title.casefold():
		return "title"

	if isinstance(item, subroutine.views.Task):
		return "description" if (item.description or "").casefold().find(wanted) >= 0 else ""

	return "body" if (item.body or "").casefold().find(wanted) >= 0 else ""


def _estimate_cell (item: Item) -> str:
	"""Return how long the work is thought to take, as a person would say it."""

	if not isinstance(item, subroutine.views.Task) or item.estimate_human is None:
		return ""

	return item.estimate_human


#: A page with nothing worth putting in a column, which is what a bare row looks like.
NO_COLUMNS = Columns()


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

	@contextlib.contextmanager
	def opened (*, strict: bool = False) -> typing.Iterator[World]:
		"""Yield every reachable connection, with the current context settled.

		One ``identity()`` per connection, fanned out — which is what resolves a workspace
		slug, prints an address and notices the same instance configured twice. It is one
		cheap query locally and one request remotely, and everything after it is narrower
		for having been asked.
		"""

		resolved = settings()

		try:
			roster = subroutine.connections.roster(resolved)
			marker = subroutine.directory.find()
			current = subroutine.context.resolve(
				roster,
				connection=selected.connection,
				workspace=selected.workspace,
				marker=marker,
			)

		except subroutine.errors.SubroutineError as error:
			fail(error)

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
						fail(error)

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
				subroutine.fanout.refuse_duplicate_instances(gathered.answers)

			except subroutine.errors.SubroutineError as error:
				fail(error)

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
				stop(
					"Nothing could be read." if reasons else "No connection could be reached.",
					"\n".join(reasons)
					if reasons
					else "Run 'subroutine connections' to see what is configured.",
				)

			try:
				yield World(
					roster=roster,
					current=_settled(roster, current, reached),
					reached=reached,
					unreachable=(*unbuilt, *gathered.failures),
					settings=resolved,
					marker=marker,
				)

			except subroutine.errors.SubroutineError as error:
				fail(error)

	def _settled (
		roster: subroutine.connections.Roster,
		current: subroutine.context.Current,
		reached: typing.Sequence[Reached],
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

		if (
			current.workspace is not None
			and current.workspace_source == subroutine.context.FROM_DIRECTORY
		):
			here = next((item for item in reached if item.name == current.connection), None)
			known = (
				{space.slug for space in here.identity.workspaces} if here is not None else set()
			)

			if here is not None and current.workspace not in known:
				warn(
					f"{FILE_NAME} here names workspace {current.workspace!r}, which is not on "
					f"{current.connection}. Ignoring it."
				)
				current = dataclasses.replace(
					current, workspace=None, workspace_source=subroutine.context.FROM_NOTHING
				)

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

	def _locate (
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
			stop(
				f"{given!r} is not an item number.",
				"Items are named by the number 'subroutine list' prints beside them — "
				f"'subroutine {verb} 42'.",
			)

		named = _named_place(world, address)

		if named is None:
			return _unqualified(world, address.ref, given, kinds=kinds, verb=verb)

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
			stop(
				f"There is no {subroutine.domain.refs.format_ref(address.ref)}"
				f"{_in_place(world, named[1])}.",
				"Run 'subroutine list' to see what there is.",
			)

		shown = [_absolute(world, item) for item in elsewhere]
		width = max(len(text) for text in shown)
		listed = "\n".join(
			f"    {text:>{width}}  {item.title}"
			for text, item in zip(shown, elsewhere, strict=True)
		)

		stop(
			f"There is no {subroutine.domain.refs.format_ref(address.ref)}"
			f"{_in_place(world, named[1])}, but there is one here:\n{listed}",
			f"Say which — 'subroutine {verb} "
			f"{shown[0].replace(subroutine.domain.refs.SIGIL, '')}'.",
		)

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

	def _a_task (world: World, given: str, *, verb: str) -> tuple[Located, subroutine.views.Task]:
		"""Resolve an address into a task, turning down a document by saying what it is.

		**Documents are searched even though they cannot be acted on**, which looks like extra
		work and is the whole value: refs are allocated from one counter per workspace, so a
		command that only asked about tasks answered "there is no #4" about a specification
		sitting in the same listing. Being told ``#4`` is a document, with its title, is an
		answer somebody can act on; being told it does not exist is not.
		"""

		located = _locate(world, given, kinds=ANY_ITEM, verb=verb)
		found = located.item

		if not isinstance(found, subroutine.views.Task):
			# The shortest form, not the absolute one: the caller named this item directly,
			# so echoing back a qualified address they did not type reads as a correction.
			shown = world.address_of_located(located)

			stop(
				f"{shown} is a document, not a task — {found.title}",
				f"'subroutine {verb}' works on tasks. Read this one with 'subroutine show "
				f"{shown.replace(subroutine.domain.refs.SIGIL, '')}'.",
			)

		return located, found

	def _named_place (
		world: World, address: subroutine.domain.refs.Address
	) -> tuple[Reached, str] | None:
		"""Return the place an address names, or ``None`` when it named none.

		``None`` covers a bare number with no context chosen, which is the one case that has
		to go looking; every other case has somewhere definite to ask.
		"""

		name = address.connection or world.current.connection
		item = world.connection(name)

		if item is None:
			stop(
				f"There is no connection called {name!r} here, or it could not be reached.",
				world.roster.alternatives(),
			)

		wanted = address.workspace or world.current.workspace

		if wanted is None:
			return None

		if item.identity.workspace(wanted) is None:
			stop(f"There is nothing called {wanted!r} on {item.name}.", _workspace_hint(item))

		return item, wanted

	def _unqualified (
		world: World, ref: int, given: str, *, kinds: tuple[str, ...], verb: str
	) -> Located:
		"""Resolve a bare number when nothing has chosen a context, or refuse with the choice."""

		candidates = _everywhere(world, ref, kinds)

		if not candidates:
			stop(
				f"There is no {subroutine.domain.refs.format_ref(ref)} here.",
				"Run 'subroutine list' to see what there is.",
			)

		if len(candidates) == 1:
			return candidates[0]

		shown = [_absolute(world, item) for item in candidates]
		width = max(len(text) for text in shown)
		listed = "\n".join(
			f"    {text:>{width}}  {item.title}"
			for text, item in zip(shown, candidates, strict=True)
		)

		stop(
			f"{given!r} could mean any of these:\n{listed}",
			f"Say which — 'subroutine {verb} "
			f"{shown[0].replace(subroutine.domain.refs.SIGIL, '')}', or "
			f"'subroutine use {_place_of(world, candidates[0])}' to keep working there.",
		)

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

	# --- The commands --------------------------------------------------------------------

	@app.command()
	def add (
		words: list[str] = typer.Argument(None, help="What you need to do."),
		kind: str = typer.Option(
			"", "--type", help="task, bug, feature, chore, spike. Defaults to task."
		),
		json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
	) -> None:
		"""Add something to your list.

		Examples:

		  subroutine add "Call the dentist before Sunday"

		  subroutine add "Write the report by friday !3 ~2h #work"

		  subroutine add "Dates render as if this year" --type bug
		"""

		text = " ".join(words or [])

		if not text.strip():
			# A required-argument error is a dead end where a question would do (§12.2a) — but
			# the question goes to **stderr**, because `typer.prompt` echoes to stdout by
			# default and `add --json` then emitted `What do you need to do?: {…}`, which is not
			# JSON. The scripted path is the agent's path, and `topics.py` advertises it.
			text = typer.prompt("What do you need to do?", err=True)

		with opened() as world:
			where = world.writing_to()
			filed = _default_project(world, text)
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
			read = "" if captured.summary is None else f"  {captured.summary}"

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

		with opened(strict=strict) as world:
			# **Resolved once, here, in this machine's zone** (§13.7). Each instance would
			# otherwise apply its own notion of the caller's timezone, and a person whose work
			# profile says America/New_York and whose personal one says Europe/London would
			# get two different days merged into one list.
			zone = world.settings.default_timezone
			day = subroutine.domain.schedule.local_date(subroutine.db.types.utcnow(), zone)

			gathered = subroutine.fanout.gather(
				world.clients,
				# The horizon is passed rather than left to default. `GET /v1/agenda` omits
				# the `upcoming` bucket unless asked, because an agent asking "what is on
				# today" means today; a person running `subroutine today` wants the week in
				# front of them, and §12.2a's agenda has four buckets.
				lambda client: client.agenda(
					date=day,
					timezone=zone,
					horizon_days=subroutine.domain.agenda.DEFAULT_HORIZON_DAYS,
					# `-w` narrows the agenda the same way it narrows every other listing.
					# Unset spans everything, which is what makes `today` one list rather
					# than one per workspace (§13.7) — the dentist and the stand-up belong
					# in the same place. Naming a workspace is how you ask for half of it.
					workspace=selected.workspace,
				),
				strict=strict,
			)

			_report(world, gathered.failures)

			if json_output:
				say(json.dumps(_agenda_json(world, gathered), indent=2))

				return

			_render(world, gathered, say=say, console=console)

	def _listed (
		*,
		limit: int,
		json_output: bool,
		merged: bool,
		strict: bool,
		order: str | None = None,
		project: str | None = None,
		deferred: bool = False,
		q: str | None = None,
		ready: bool = False,
		trash: bool = False,
	) -> None:
		"""Print the list. Registered twice — three times, with ``search`` — from one body."""

		# **The scripted path is never narrowed by a presentation rule.** Hiding parked work
		# is a decision about a list somebody *reads*, which is what §6.5's "default views"
		# means and the whole basis for leaving the API default alone. `--json` is the other
		# half of that: a script asking for open work must not silently lose rows, and every
		# row already carries `start_at`, so it can make the same choice for itself.
		#
		# So the two outputs differ, deliberately, and only in this. It is the one place
		# §12.2a's "the human path and the scripted path are the same code" gives way — the
		# code is the same, the presentation rule is not applied.
		hiding = not deferred and not json_output

		with opened(strict=strict) as world:
			gathered = _listing(
				world,
				limit=limit,
				strict=strict,
				order=order,
				project=project,
				deferred=not hiding,
				q=q,
				ready=ready,
				trash=trash,
			)

			_report(world, gathered.failures)

			rows = _merged(gathered, order=_ordering(order)[1])
			more = any(answer.value.more for answer in gathered.answers)

			if json_output:
				say(json.dumps([_as_json(world, name, item) for name, item in rows], indent=2))

				return

			if not rows and q:
				# **Not "nothing on your list".** The list is not empty; this search found
				# nothing in it, and saying the first about the second is how somebody
				# concludes their data is gone. The remedy named is the widening one,
				# because a search that missed is usually a search that was too narrow.
				say(f"Nothing matches {q!r}.")

				if not deferred:
					_suggest(console, f'subroutine search "{q}" --deferred', "look in what you have put off too")

				return

			if not rows:
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
					say("Nothing to show — some of what you asked for could not be read.")

					return

				# And it is equally false when everything on the list is simply parked, which
				# is the case a person hits after deferring the last thing they were avoiding.
				# Telling them to add something would be advice about a list they have.
				if hiding and any(answer.value.parked for answer in gathered.answers):
					# Not "nothing to do today" — that is the agenda's sentence, and `list` is
					# not the agenda. What is true is that everything open starts later.
					say("Nothing you can start yet.")
					_say_parked(gathered, console=console, hidden=True)

					return

				say("Nothing on your list.")
				_suggest(console, 'subroutine add "something to do"')

				return

			if merged or not world.qualifies_connection:
				_flat(world, rows, console=console, term=q)

			else:
				_grouped(world, gathered, console=console, say=say, term=q)

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

				console.print(
					rich.text.Text(f"      …and more. '{repeated}' to see further.", style=DETAIL)
				)

			_say_parked(gathered, console=console, hidden=hiding)

			_say_where_a_bare_number_goes(world, console=console)

			say("")
			_suggest(
				console,
				f"subroutine show {_typeable(world, rows[0][0], rows[0][1])}",
				"read one of them in full",
			)

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

	def _refuse_words (words: list[str] | None, looking_for: str) -> None:
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

		fail(
			subroutine.errors.ValidationError(
				"'subroutine list' filters what you have; it does not search it.",
				hint=f'Try: subroutine search "{wanted}"',
			)
		)

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
		"""

		_refuse_words(words, looking_for)

		_listed(
			limit=limit,
			json_output=json_output,
			merged=merged,
			strict=strict,
			order=order or None,
			project=project or None,
			deferred=deferred,
			ready=ready,
			trash=trash,
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
		deferred: bool = typer.Option(
			False, "--deferred", help="Include things you have put off until a later date."
		),
	) -> None:
		"""Find things by their words — in the title, and in what you wrote about them.

		Searches tasks and documents together, like 'subroutine list', because one number
		names either and a search that found only half of them would be lying about the rest.

		Examples:

		  subroutine search "dentist"

		  subroutine search "pagination" --project SR
		"""

		_listed(
			limit=limit,
			json_output=json_output,
			merged=merged,
			strict=strict,
			order=order or None,
			project=project or None,
			deferred=deferred,
			q=_asked(terms, "What are you looking for?"),
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

		with opened(strict=strict) as world:
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
							{"connection": answer.connection.name, **event.model_dump(mode="json")}
							for answer in gathered.answers
							for event in answer.value
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

		_refuse_words(words, looking_for)

		_listed(
			limit=limit,
			json_output=json_output,
			merged=merged,
			strict=strict,
			order=order or None,
			project=project or None,
			deferred=deferred,
			ready=ready,
			trash=trash,
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

		with opened() as world:
			located = _locate(
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
			_suggest(
				console,
				f"subroutine comment {world.address_of_located(located).replace(subroutine.domain.refs.SIGIL, '')} "
				f'"what happened"',
			)

	# **Named `start_item`/`stop_item`, not `start`/`stop`.** `stop` is the refusal helper this
	# whole function is handed, and `def stop` inside it rebinds that name for the entire
	# enclosing scope — so every refusal in every command registered here would have called the
	# command instead. `mypy --strict` caught it; nothing at runtime would have, because the
	# paths that call `stop()` are the ones nobody exercises on a good day.
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

		_moved_to(which, "in_progress", verb="start", said="Started")

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

		_moved_to(which, "open", verb="stop", said="Stopped")

	def _moved_to (which: str, status: str, *, verb: str, said: str) -> None:
		"""Move a task to a named status, in the shape `done` uses.

		One body for both, because they differ in two words. **Neither says "status"** — §13.5b
		forbids the vocabulary and does not need it: `done`, `plan` and `defer` are all actions
		that happen to set a field, and "Started: <title>" is the same shape as "Done: <title>".
		"""

		with opened() as world:
			located, task = _a_task(
				world,
				_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
				verb=verb,
			)

			if task.completed_at is not None:
				# Nothing else would go wrong, and this is the honest answer: picking up
				# something already ticked off is nearly always the wrong number.
				say(_acted(world, located, "Already done"))
				_suggest(console, "subroutine list", "everything still open")

				return

			client = _require_connection(world, located.connection)
			moved = client.update(ref=task.ref, status=status, workspace=located.workspace)

			say(_acted(world, dataclasses.replace(located, item=moved), said))
			_suggest(console, "subroutine today")

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

		with opened() as world:
			located, task = _a_task(
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

			client = _require_connection(world, located.connection)
			finished = client.complete(ref=task.ref, workspace=located.workspace)

			_because(client, located, because, what="Done")

			say(_acted(world, dataclasses.replace(located, item=finished), "Done"))
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

		with opened() as world:
			located, task = _a_task(
				world,
				_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
				verb="plan",
			)
			client = _require_connection(world, located.connection)

			changed = client.schedule(
				ref=task.ref,
				workspace=located.workspace,
				planned_for=_day(world, _asked(when, "Which day?")),
			)

			# The planned day, not `_when`'s answer. `_when` prefers a deadline, which is
			# right in a list and wrong in the confirmation of a command whose whole job was
			# to set the other field — the user said "tomorrow" and was shown Friday.
			planned = f"Planned for {_render_day(changed.planned_for)}"

			_because(client, located, because, what=planned)

			say(_acted(world, dataclasses.replace(located, item=changed), planned))
			_suggest(console, "subroutine today")

	@app.command()
	def defer (
		which: str = typer.Argument("", help="A task number, as shown by 'subroutine list'."),
		when: str = typer.Argument("", help="A day to hide it until."),
		because: str = typer.Option(
			"", "--because", help="What you are waiting for, recorded against it."
		),
	) -> None:
		"""Hide something until later.

		Examples:

		  subroutine defer 1 monday

		  subroutine defer 42 2026-09-01 --because "waiting on the provider's reply"
		"""

		with opened() as world:
			located, task = _a_task(
				world,
				_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
				verb="defer",
			)
			client = _require_connection(world, located.connection)

			changed = client.schedule(
				ref=task.ref,
				workspace=located.workspace,
				start=_day(world, _asked(when, "Hide it until when?")),
			)

			# The *task's* zone, like every other instant this program renders. `start_at` is
			# midnight where the task lives, so re-reading it in a westward client zone named
			# the day before the one that was asked for.
			hidden = f"Hidden until {_render_date(changed.start_at, changed.timezone)}"

			_because(client, located, because, what=hidden)

			say(_acted(world, dataclasses.replace(located, item=changed), hidden))
			_suggest(console, "subroutine today")

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
		because: str = typer.Option("", "--because", help="Why, recorded against it."),
		json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
	) -> None:
		"""Change what a task says about itself.

		Everything you do not name is left alone.

		Examples:

		  subroutine update 42 --importance 4 --urgency 3

		  subroutine update 42 --estimate 2h --type bug

		  subroutine update 42 --title "Fix the parser, not the tokeniser"
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

		# **Moving between projects, which `update` could not do until `#169`.** The endpoint
		# has taken it since `#43`; I added this command without it, and the sequence a new
		# user actually performs — accumulate tasks, notice a theme, make a project, file them
		# — dead-ended at the last step.
		if project:
			changes["project"] = project.strip().upper()

		# **A refusal rather than a cheerful no-op**, matching the MCP tool: somebody who ran
		# this and named no field meant to change something, and "unchanged" would hide the
		# mistake at exactly the moment it could still be corrected.
		if not changes:
			stop(
				"Nothing to change.",
				"Name a field: --title, --description, --importance, --urgency, "
				"--estimate, --type or --status.",
			)

		with opened() as world:
			located, task = _a_task(
				world,
				_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
				verb="update",
			)
			client = _require_connection(world, located.connection)
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

		with opened() as world:
			located = _locate(
				world,
				_asked(which, "Which one? (a number like 42 — a shell eats '#42')"),
				kinds=ANY_ITEM,
				verb="comment",
			)
			client = _require_connection(world, located.connection)

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
		project: str = typer.Option("", "--project", help="File it under this project, by key."),
		json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
	) -> None:
		"""Write a document — a decision, a finding, a design, a dead end.

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

		with opened() as world:
			where = world.writing_to()

			created = where.client.create_document(
				title=title,
				body=written or None,
				type=kind.strip() or None,
				project=project.strip() or None,
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

		with opened() as world:
			near = _locate(world, _asked(which, "Which one?"), kinds=ANY_ITEM, verb="link")
			far = _locate(world, _asked(other, "And the other one?"), kinds=ANY_ITEM, verb="link")
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

		with opened() as world:
			near = _locate(world, _asked(which, "Which one?"), kinds=ANY_ITEM, verb="unlink")
			far = _locate(world, _asked(other, "And the other one?"), kinds=ANY_ITEM, verb="unlink")
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

		with opened() as world:
			located = _locate(world, _asked(which, "Which one?"), kinds=ANY_ITEM, verb="delete")
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

		with opened() as world:
			located = _locate(world, _asked(which, "Which one?"), kinds=ANY_ITEM, verb="restore")
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

		with opened() as world:
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
			_suggest(console, f'subroutine add "something to do +{created.key}"')

	@project_app.command("list")
	def project_list (
		json_output: bool = typer.Option(False, "--json", help="Print the list as JSON."),
	) -> None:
		"""Show the projects you can see, with what is inside what.

		Examples:

		  subroutine project list
		"""

		with opened() as world:
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

		with opened() as world:
			where = world.writing_to()
			workspace = _writing_workspace(world)

			# **Counted before anything changes, so the question is answerable.** "This will
			# break addresses" is abstract; "this project holds 137 items and three commands
			# will stop finding it" is a thing somebody can weigh. The count is the reason
			# this reads the project first rather than renaming and reporting.
			held = where.client.tasks(
				workspace=workspace, project=key, include_completed=True
			)

			if not yes:
				say(f"Renaming {key} to {to.upper()}.")
				say(f"  {len(held)} item{'' if len(held) == 1 else 's'} keep their numbers.")
				say(f"  '{key}' stops working: as an address, in '+{key}', and in any")
				say("  .subroutine file that names it.")

				if not typer.confirm("Go on?"):
					stop("Nothing was renamed.")

			renamed = where.client.rename_project(key, key=to, workspace=workspace)

			say(f"Renamed to {renamed.key} — {renamed.title}")

			# The marker in *this* directory is the one that can be repaired from here, and
			# the one most likely to be stale a second from now (`#177`).
			if world.marker is not None and world.marker.project == key.upper():
				_suggest(console, f"subroutine use --here --project {renamed.key}")

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

		# **Neither, or both, is a refusal rather than a default** — this is the one project
		# command with no undo, and `POST /v1/projects/{key}/move` refuses the same way for
		# the same reason. Guessing either direction is how a subtree gets flattened.
		if bool(under) == root:
			stop(
				"Say where to move it.",
				"'--under KEY' puts it inside another project; '--root' makes it top-level.",
			)

		with opened() as world:
			place = world.writing_to()
			workspace = _writing_workspace(world)

			# Counted before anything changes, like `project rename` — "this moves a subtree"
			# is abstract, and "this moves 3 projects and 137 items" is something somebody can
			# weigh. Reading first is the whole reason this is not a one-liner.
			tree = place.client.projects(workspace=workspace)
			moving = _subtree(tree, key)

			if not moving:
				stop(
					f"There is no project called {key.upper()!r} here.",
					"Run 'subroutine project list' to see what there is.",
				)

			# **Every project in the subtree, not just the named one.** Asking only about `key`
			# reported one item where two were moving, which is the exact opposite of what a
			# count is for. One request per project in the subtree is a real cost and the
			# right one to pay here: this runs once, on a rare operation, to answer a question
			# somebody is about to say yes to.
			held = sum(
				len(
					place.client.tasks(
						workspace=workspace, project=item.key, include_completed=True
					)
				)
				for item in moving
			)

			if not yes:
				destination = "the top level" if root else under.upper()
				projects = f"{len(moving)} project{'' if len(moving) == 1 else 's'}"
				items = f"{held} item{'' if held == 1 else 's'}"

				say(f"Moving {key.upper()} to {destination}.")
				say(f"  {projects} move, and {items} {'goes' if held == 1 else 'go'} with them.")
				say("  Every number stays the same, and nothing is refiled.")

				if not typer.confirm("Go on?"):
					stop("Nothing was moved.")

			moved = place.client.move_project(
				key, parent=None if root else under, workspace=workspace
			)

			say(f"Moved {moved.key} — {moved.title}")

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

		wanted = key.strip().upper()
		root = next((item for item in tree if item.key == wanted), None)

		if root is None:
			return []

		inside = {root.id}

		for item in tree:
			if item.parent_id in inside:
				inside.add(item.id)

		return [item for item in tree if item.id in inside]

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
		json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
	) -> None:
		"""Add somebody to this instance.

		Examples:

		  subroutine user create thomas --name "Thomas Anderson"

		  subroutine user create thomas --name "Thomas Anderson" --email thomas@example.com

		A new account belongs to no workspace yet, and until it does there is nothing it can
		see. 'subroutine user add' is the second half, and this command says so when it is
		done rather than leaving somebody with an account that appears not to work.

		There is no password. Subroutine authenticates with tokens, so what a new person needs
		next is one of those.
		"""

		with opened() as world:
			where = world.writing_to()

			# Read *before* creating, because the question is how many accounts there were —
			# see `_keep_the_operators_own_list` for why that is the one that matters.
			before = where.client.users() if where.client.connection.is_local else []

			created = where.client.create_user(
				username=username,
				display_name=display_name.strip() or None,
				email=email.strip() or None,
				is_service_account=agent,
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

		with opened() as world:
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

		with opened() as world:
			where = world.writing_to()
			joined = where.client.add_member(
				username=username,
				role=role.strip(),
				workspace=workspace.strip() or _writing_workspace(world),
			)

			say(f"{joined.user.username} is now {joined.role} in {joined.workspace.slug}")

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

		with opened() as world:
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

			with opened() as world:
				say(
					f"Now working in {world.current.describe(qualified=world.qualifies_connection)}."
					if removed is not None
					else "There was nothing to reset."
				)

			return

		with opened() as world:
			if here:
				_use_here(world, where, project)

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

			connection, workspace = _chosen(world, where)
			subroutine.context.store(connection, workspace)

			shown = f"{connection}/{workspace}" if world.qualifies_connection else workspace
			say(f"Now working in {shown}.")
			say("")
			_suggest(console, "subroutine today")

	def _use_here (world: World, where: str, project: str) -> None:
		"""Write a marker into the current directory, and say what it will do.

		The connection and workspace come from the *current context* unless the caller named
		them, so ``subroutine use --here --project SR`` records where they already are rather
		than making them type it again — which is the whole difference between adopting a
		repository in one command and adopting it in an interview.
		"""

		connection, workspace = (
			_chosen(world, where)
			if where.strip()
			else (world.current.connection, world.current.workspace)
		)
		key = project.strip().upper() or None
		identifier = None if key is None else _project_id_of(world, key)

		if key is not None and identifier is None:
			stop(
				f"There is no project {key!r} here.",
				"Run 'subroutine project list' to see them, or "
				f"'subroutine project create {key} \"A title\"' to make it.",
			)

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
			project=key,
			# **The id is what makes this survive a rename** (`#177`). The key is written
			# beside it so the file stays readable, and is the half that goes stale.
			project_id=identifier,
		)

		say(f"Wrote {written}.")

		if key is None:
			say("New work here goes to the Inbox. Add --project to file it somewhere.")

			return

		say(f"New work started in this directory goes to {key}, unless a line says otherwise.")
		say("")
		_suggest(console, "subroutine add \"something to do\"")

	def _project_id_of (world: World, key: str) -> str | None:
		"""Return the permanent id of the project this key names, or ``None`` if there is none.

		Checked before the file is written, because a marker naming a project that does not
		exist fails on the *next* capture rather than here — and the person who would have to
		work out why is not the one who typed this.

		Returns the **id** rather than a yes-or-no, because that is what the marker records
		(`#177`) and asking twice would be two chances for the answers to differ.
		"""

		where = world.writing_to()

		for row in where.client.projects(workspace=_writing_workspace(world)):
			if row.key.upper() == key:
				return str(row.id)

		return None


	def _default_project (world: World, text: str) -> str | None:
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

			warn(
				f"{FILE_NAME} here names project {shown!r}, which is not on "
				f"{world.current.connection}. Ignoring it."
			)

			return None

		# **The one moment this file can explain itself.** The id resolved and the key beside
		# it did not match, which is exactly what a rename leaves behind — so say it once,
		# here, rather than letting the file go on quietly disagreeing with itself.
		if world.marker.project and world.marker.project.upper() != named.upper():
			warn(
				f"{FILE_NAME} here still says {world.marker.project!r}; that project is now "
				f"{named}. Run 'subroutine use --here --project {named}' to bring it up to date."
			)

		return named

	def _project_named_by (world: World, marker: subroutine.directory.Marker) -> str | None:
		"""Return the current key of the project a marker names, or ``None`` if there is none.

		The matching itself is `subroutine.directory.resolve`, which is shared with the MCP
		server — this half is only the fetching, because that is the part that needs a world
		to fetch through. It was one function here until `#232` found the other surface had no
		equivalent at all.
		"""

		where = world.writing_to()
		found = where.client.projects(workspace=_writing_workspace(world))

		return subroutine.directory.resolve(marker, found)

	# **This docstring is published as `--help`**, so the reasoning lives out here. `#278`:
	# the listing marks the connection being written to as well as the one that is merely the
	# fallback. Those are different questions, and only the second used to be answered — under
	# a word, "default", that reads like the first. An agent read it, told Simon local was
	# where writes went, and a bare `add` filed to the other instance.
	@app.command(hidden=not _worth_showing(settings))
	def connections () -> None:
		"""List the instances this reaches, which one you are working in, and where each
		one's token came from.

		No token is ever printed, and none can be recovered from what is. Which of the four
		places supplied it is the useful part — the standing footgun in comparable tooling is
		not having several sources but not knowing which one won.
		"""

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

		warning = subroutine.credentials.permission_warning()

		if warning is not None:
			warn(warning)

		rows = [_connection_row(connection, roster, resolved, current) for connection in roster]
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

	def _connection_row (
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
		where = mask(resolved.database_url) if connection.is_local else str(connection.url)

		return (connection.name, where, token, ", ".join(notes))

	def _chosen (world: World, where: str) -> tuple[str, str]:
		"""Read what ``use`` was given into a connection and a workspace."""

		parts = [part.strip() for part in where.split(subroutine.domain.refs.SEPARATOR)]

		if len(parts) > 2 or any(not part for part in parts):
			stop(
				f"{where!r} is not a place to work.",
				"Give a workspace, or a connection and a workspace — 'subroutine use "
				"work/acme'.",
			)

		name = parts[0] if len(parts) == 2 else world.current.connection
		wanted = parts[-1]
		item = world.connection(name)

		if item is None:
			stop(
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
				stop(
					f"{wanted!r} is a connection, not a workspace.",
					_completions(named),
				)

			stop(f"There is nothing called {wanted!r} on {item.name}.", _workspace_hint(item))

		return item.name, wanted

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

	def _require_connection (
		world: World, name: str
	) -> subroutine.clients.base.Client:
		"""Return the open client for a connection a lookup already found something on."""

		item = world.connection(name)

		if item is None:
			stop(f"{name} could not be reached.")

		return item.client

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

	def _report (world: World, failures: typing.Sequence[subroutine.fanout.Failure]) -> None:
		"""Name every connection that could not be reached, and carry on.

		To standard error, and the command still exits 0: an agenda that refuses to print
		because one of three servers is down is worse than an agenda with a line saying which
		one. ``--strict`` is how a script says it would rather stop.
		"""

		for failure in (*world.unreachable, *failures):
			warn(failure.describe())

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
			else event.item_title or event.entity_type
		)
		verb = event.action.replace("_", " ")

		# A comment is the one action whose entity is not what it is about, and "commented on"
		# reads as what happened where "created" would name the comment row nobody can see.
		if event.entity_type == "comment":
			verb = f"{verb} a comment on"

		fields = sorted(event.changes or {})
		listed = f"  ({', '.join(fields)})" if fields and event.action == "updated" else ""

		return f"{verb:<12}  {named}{listed}"

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

			for workspace in () if item is None else item.identity.workspaces:
				rows.extend(
					(client.connection.name, found)
					for found in client.tasks(
						workspace=workspace.slug,
						limit=asked,
						order=order,
						project=project,
						deferred="include" if deferred else "exclude",
						q=q,
						ready=ready,
						deleted=trash,
					)
				)

				# **`--ready` is about work you could start, so a document is not an answer to
				# it** (`#136`). §6.14 says a document is not scheduled and nothing blocks one,
				# so including them would mean every specification and decision in the instance
				# reported as ready — which is true and useless, and would bury the tasks.
				# The trash is a different list, not a wider one: nothing parked is reported
				# beside it, and a deferral count would be about the live list.
				if ready or trash:
					continue

				if not deferred:
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
						)
					)
				rows.extend(
					(client.connection.name, found)
					for found in client.documents(
						workspace=workspace.slug,
						limit=asked,
						order=order if shared else None,
						project=project,
						q=q,
						deleted=trash,
					)
				)

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
	shell (SPEC.md §12.2a), so a suggestion carries the bare number or the qualified path.

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

	SPEC.md §12.2a: bare commands prompt rather than error. A required-argument error is a
	dead end where a question would do — and in a pipe, where there is nobody to ask, the
	prompt fails with the usage anyway, which is the right answer there.
	"""

	if given.strip():
		return given

	# To stderr, so that a `--json` reader on stdout is never handed a prompt (see `add`).
	answer: str = typer.prompt(question, err=True)

	return answer


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

	buckets = (
		("Overdue", "overdue", True),
		("Today", "today", False),
		("Next 7 days", "upcoming", False),
		("Unscheduled", "unscheduled", False),
	)
	rows: dict[str, list[Row]] = {}

	for _heading, field, _late in buckets:
		# **Re-sorted across connections, not concatenated.** Each connection answers already
		# ordered, and appending one block after another left `--merged` showing all of A
		# newest-first and then all of B — two sorted runs end to end, which §13.7 explicitly
		# rules out ("sorting is re-applied after the merge"). It also made the suggested
		# `done` command name the first *connection's* first row rather than the newest one.
		rows[field] = _in_order(
			[
				(answer.connection.name, task)
				for answer in gathered.answers
				for task in getattr(answer.value, field)
			],
			field,
		)

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
) -> None:
	"""Print one list, every row addressed by the shortest form that resolves.

	``columns`` is passed in when the page is larger than these rows — a grouped listing
	measures across every connection, so the addresses line up down the whole output rather
	than stepping in and out as each heading changes what is below it.
	"""

	measured = Columns.measured(world, rows, term=term) if columns is None else columns

	for connection, task in rows:
		console.print(_item_line(world, connection, task, late=False, columns=measured))


def _grouped (
	world: World,
	gathered: subroutine.fanout.Gathered[Listing],
	*,
	console: rich.console.Console,
	say: typing.Callable[[str], None],
	term: str | None = None,
) -> None:
	"""Print a group per connection, which is what a flat listing has instead of structure.

	Unlike the agenda, a list of open tasks has no ordering a person already holds in their
	head, so the connection is the only structure there is — and a heading carries the label
	once rather than repeating it on every line (§13.7).
	"""

	printed = False
	columns = Columns.measured(
		world, [row for answer in gathered.answers for row in answer.value.rows], term=term
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
	"""Print the command to try next (SPEC.md §12.2a).

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
	if columns.started:
		line.append(f"{_started_cell(item):<{columns.started}}  ", style=DETAIL)

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

	line.append(item.title)

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
		console.print("")
		console.print(rich.text.Text("What happened", style=HEADING))

		for remark in remarks:
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
	# tester assumed it had not saved. `status_is_default` is what lets this say `blocked`
	# without saying `open` on every shopping-list item, which §1.4 would not survive.
	# Not when it is finished: the `done <date>` fact below says that better, and a document
	# has no `completed_at` to ask about — the category is the question both kinds answer.
	if not item.status_is_default and item.status_category != "done":
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
		if item.start_at is not None:
			facts.append(f"from {_render_date(item.start_at, item.timezone)}")

		if item.due_at is not None:
			facts.append(f"due {_render_date(item.due_at, item.timezone)}")

		if item.planned_for is not None:
			facts.append(f"for {_render_day(item.planned_for)}")

		if item.completed_at is not None:
			facts.append(f"done {_render_date(item.completed_at, item.timezone)}")

		if item.tags:
			facts.extend(f"#{tag}" for tag in item.tags)

	# The project only when it is one somebody filed this in. The Inbox is where things go
	# when nobody said, so naming it would be reporting the absence of a decision.
	if item.project_key and item.project_key.lower() != "inbox":
		facts.append(item.project_key)

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


def _render_day (day: datetime.date | None) -> str:
	"""Render a calendar date the way a person reads one."""

	return "—" if day is None else _dated(day)


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
		deferred = f"from {_render_date(task.start_at, task.timezone)}"

		if task.due_at is not None:
			return f"  ({deferred}, due {_render_date(task.due_at, task.timezone)})"

		return f"  ({deferred})"

	if task.due_at is not None:
		return f"  (due {_render_date(task.due_at, task.timezone)})"

	if task.planned_for is not None:
		return f"  (for {_render_day(task.planned_for)})"

	return ""


def _deferred (task: subroutine.views.Task) -> bool:
	"""Return whether this task's start has not come round yet.

	**Only while it is still hiding something**, which is why this is not simply
	``start_at is not None``. A listing row has one short phrase to spend, and once the
	instant has passed the defer explains nothing: the task is startable and behaves like any
	other. ``show`` reports it either way, because there the question being asked is "what has
	been decided about this", not "why is this not in front of me".
	"""

	if task.start_at is None:
		return False

	return task.start_at > datetime.datetime.now(datetime.UTC)


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
		"planned_for": None if task.planned_for is None else task.planned_for.isoformat(),
		"start_at": None if task.start_at is None else task.start_at.isoformat(),
		"importance": task.importance,
		"urgency": task.urgency,
		"estimate_minutes": task.estimate_minutes,
		"tags": list(task.tags),
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

	buckets = ("overdue", "today", "upcoming", "unscheduled")
	first = gathered.answers[0].value if gathered.answers else None

	return {
		"date": None if first is None else first.date.isoformat(),
		"timezone": None if first is None else first.timezone,
		**{
			field: [
				_as_json(world, answer.connection.name, task)
				for answer in gathered.answers
				for task in getattr(answer.value, field)
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

	The second is the comparison, with ``ref`` appended following the last key's direction,
	exactly as :func:`subroutine.domain.ordering.clauses` makes a query's tiebreaker follow
	it. An unknown field is refused here, before a single request goes out.
	"""

	wanted = subroutine.domain.ordering.requested(
		order,
		allowed=subroutine.domain.ordering.TASK_FIELDS,
		default=subroutine.domain.ordering.DEFAULT_TASK_ORDER,
	)

	shared = all(
		name in subroutine.domain.ordering.DOCUMENT_FIELDS for name, _descending in wanted
	)
	trailing = wanted[-1][1] if wanted else True

	return shared, (*wanted, ("ref", trailing))


def _merged (
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

	rows = [row for answer in gathered.answers for row in answer.value.rows]

	return subroutine.domain.ordering.merged(rows, key=lambda row: row[1], order=order)


def _in_order (
	rows: list[Row], bucket: str
) -> list[Row]:
	"""Order one agenda bucket the way that bucket is read.

	The three dated buckets read by date — soonest first, because that is the order the days
	arrive in — and `unscheduled` has no date to sort by, so it falls back to newest first like
	a listing. The ref is the tiebreak throughout, so two tasks with the same date do not swap
	places between runs.
	"""

	if bucket == "unscheduled":
		rows.sort(key=lambda row: (row[1].created_at, row[1].ref), reverse=True)

		return rows

	# NULLs last, explicitly: a task in `today` may be there for `planned_for` and carry no
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
	"""Print the command to try next, on the shared console (SPEC.md §12.2a).

	The public face of :func:`_suggest`, for callers outside this module that have no console
	of their own — the bare invocation and ``--version`` in ``cli/main``. Kept as one function
	so the styling cannot drift into a second definition, which it had begun to: both of those
	callers used to pad their own explanation into a column, which was a second shape for one
	thing and lined up with nothing else on the screen.
	"""

	_suggest(rich.console.Console(), command, about)
