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
import shlex
import typing

import rich.console
import rich.text
import typer

import subroutine.clients.base
import subroutine.clients.http
import subroutine.clients.local
import subroutine.clients.opening
import subroutine.config
import subroutine.connections
import subroutine.context
import subroutine.credentials
import subroutine.db.types
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

	def address_of (self, connection: str, workspace_id: typing.Any, ref: int) -> str:
		"""Return the shortest address that resolves to this item, from here.

		A row inside the current context is a bare ``#42``; one in another workspace carries
		it; one on another connection carries both. That is what makes a merged listing safe
		to copy out of — a bare number beside an item somewhere else is an invitation to act
		on the wrong one.
		"""

		item = self.connection(connection)
		slug = None if item is None else item.slug_of(workspace_id)
		in_context = connection == self.current.connection and slug == self.current.workspace

		if in_context or slug is None:
			return subroutine.domain.refs.format_ref(ref)

		if connection == self.current.connection or not self.qualifies_connection:
			return subroutine.domain.refs.format_address(ref, workspace=slug)

		return subroutine.domain.refs.format_address(ref, workspace=slug, connection=connection)

	def address_of_item (self, connection: str, item: Item) -> str:
		"""Return the shortest address that resolves to this item, task or document."""

		return self.address_of(connection, item.workspace_id, item.ref)

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
			priority=_column(_priority_cell(item) for _name, item in rows),
			estimate=_column(_estimate_cell(item) for _name, item in rows),
		)


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
			current = subroutine.context.resolve(
				roster, connection=selected.connection, workspace=selected.workspace
			)

		except subroutine.errors.SubroutineError as error:
			fail(error)

		with contextlib.ExitStack() as stack:
			clients = []

			for connection in roster:
				try:
					clients.append(stack.enter_context(_client(connection, roster, resolved)))

				except subroutine.errors.SubroutineError as error:
					if strict:
						fail(error)

					warn(f"{connection.label}: {error.detail}")

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

				for failure in gathered.failures:
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
					unreachable=gathered.failures,
					settings=resolved,
				)

			except subroutine.errors.SubroutineError as error:
				fail(error)

	def _settled (
		roster: subroutine.connections.Roster,
		current: subroutine.context.Current,
		reached: typing.Sequence[Reached],
	) -> subroutine.context.Current:
		"""Answer steps 4 and 5 of §13.7's order, now that the connections have been asked."""

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
		json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
	) -> None:
		"""Add something to your list.

		Examples:

		  subroutine add "Call the dentist before Sunday"

		  subroutine add "Write the report by friday !3 ~2h #work"
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
			captured = where.client.capture(text=text, workspace=_writing_workspace(world))

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

			say(f"Added: {captured.task.title}{_when(captured.task)}{read}")

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

			say("")
			_suggest(
				console,
				f"subroutine show {_typeable(world, rows[0][0], rows[0][1])}",
				"read one of them in full",
			)

	# **Registered twice, and `list` is the one the help shows.** Simon's preference, and the
	# right way round: a real word teaches itself, where `ls` only reads as "list" to somebody
	# who already knows Unix — which is not the audience §1.4 is written for. `ls` keeps
	# working because it is in muscle memory and in every note anybody has written, and is
	# hidden rather than removed: a synonym in the help is a second thing to choose between.
	@app.command("list")
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
	) -> None:
		"""List everything still open — tasks and documents — newest first.

		Examples:

		  subroutine list

		  subroutine list --limit 10

		  subroutine list --order -priority_score

		  subroutine list --project SR --order due_at
		"""

		_listed(
			limit=limit,
			json_output=json_output,
			merged=merged,
			strict=strict,
			order=order or None,
			project=project or None,
			deferred=deferred,
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

	@app.command("ls", hidden=True)
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
	) -> None:
		"""The short name for 'subroutine list'. Both do the same thing.

		Examples:

		  subroutine ls
		"""

		_listed(
			limit=limit,
			json_output=json_output,
			merged=merged,
			strict=strict,
			order=order or None,
			project=project or None,
			deferred=deferred,
		)

	@app.command()
	def show (
		which: str = typer.Argument("", help="An item number, as shown by 'ls'."),
		json_output: bool = typer.Option(False, "--json", help="Print as JSON."),
	) -> None:
		"""Read one item — what it is, what it is joined to, and what happened to it.

		Works on a task or on a document, because one counter per workspace serves both and
		a number on a command line does not say which it is.

		Examples:

		  subroutine show 42

		  subroutine show 42 --json
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

			if json_output:
				say(
					json.dumps(
						_shown_as_json(world, located, links, remarks, children), indent=2
					)
				)

				return

			_render_item(world, located, links, remarks, children, console=console)
			say("")
			_suggest(
				console,
				f"subroutine comment {world.address_of_located(located).replace(subroutine.domain.refs.SIGIL, '')} "
				f'"what happened"',
			)

	@app.command()
	def done (
		which: str = typer.Argument("", help="A task number, as shown by 'ls'."),
	) -> None:
		"""Tick something off.

		Examples:

		  subroutine done 42
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

			say(_acted(world, dataclasses.replace(located, item=finished), "Done"))
			_suggest(console, "subroutine today")

	@app.command()
	def plan (
		which: str = typer.Argument("", help="A task number, as shown by 'ls'."),
		when: str = typer.Argument("", help="A day — 'today', 'tomorrow', 'friday', '2026-08-01'."),
	) -> None:
		"""Say which day you will do something.

		Examples:

		  subroutine plan 1 tomorrow

		  subroutine plan 42 friday
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
			say(
				_acted(
					world,
					dataclasses.replace(located, item=changed),
					f"Planned for {_render_day(changed.planned_for)}",
				)
			)
			_suggest(console, "subroutine today")

	@app.command()
	def defer (
		which: str = typer.Argument("", help="A task number, as shown by 'ls'."),
		when: str = typer.Argument("", help="A day to hide it until."),
	) -> None:
		"""Hide something until later.

		Examples:

		  subroutine defer 1 monday

		  subroutine defer 42 2026-09-01
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

			say(
				_acted(
					world,
					dataclasses.replace(located, item=changed),
					# The *task's* zone, like every other instant this program renders.
					# `start_at` is midnight where the task lives, so re-reading it in a
					# westward client zone named the day before the one that was asked for.
					f"Hidden until {_render_date(changed.start_at, changed.timezone)}",
				)
			)
			_suggest(console, "subroutine today")

	@app.command()
	def comment (
		which: str = typer.Argument("", help="An item number, as shown by 'ls'."),
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

		**The key is yours to choose and cannot be changed afterwards.** It becomes part of
		how every item in the project is addressed, and those strings end up in commit
		messages and other people's notes. A to Z and 0 to 9, starting with a letter, up to
		sixteen characters.
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

	# **Hidden until there is something to choose between** (§1.4). `use` and `connections`
	# are the full model's vocabulary — a workspace, an instance — and somebody with one
	# database and one workspace has no use for either. Both stay fully documented, fully
	# callable and fully discoverable through `--help` on themselves; they are simply not in
	# the way of the six commands §12.2 puts first.
	@app.command(hidden=not _worth_showing(settings))
	def use (
		where: str = typer.Argument("", help="A workspace, or 'connection/workspace'."),
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

		  subroutine use --reset
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
			if not where.strip():
				say(f"Working in {world.current.describe(qualified=world.qualifies_connection)}.")
				say("")
				_suggest(console, "subroutine use --reset", "go back to working everywhere")

				return

			connection, workspace = _chosen(world, where)
			subroutine.context.store(connection, workspace)

			shown = f"{connection}/{workspace}" if world.qualifies_connection else workspace
			say(f"Now working in {shown}.")
			say("")
			_suggest(console, "subroutine today")

	@app.command(hidden=not _worth_showing(settings))
	def connections () -> None:
		"""List the instances this reaches, and where each one's token came from.

		No token is ever printed, and none can be recovered from what is. Which of the four
		places supplied it is the useful part — the standing footgun in comparable tooling is
		not having several sources but not knowing which one won.
		"""

		resolved = settings()

		try:
			roster = subroutine.connections.roster(resolved)

		except subroutine.errors.SubroutineError as error:
			fail(error)

		warning = subroutine.credentials.permission_warning()

		if warning is not None:
			warn(warning)

		rows = [_connection_row(connection, roster, resolved) for connection in roster]
		widths = [max(len(row[column]) for row in rows) for column in range(3)]

		for row in rows:
			say(
				f"{row[0].ljust(widths[0])}  {row[1].ljust(widths[1])}  "
				f"{row[2].ljust(widths[2])}  {row[3]}"
			)

		say("")
		_suggest(console, "subroutine use")

	def _connection_row (
		connection: subroutine.connections.Connection,
		roster: subroutine.connections.Roster,
		resolved: subroutine.config.Settings,
	) -> tuple[str, str, str, str]:
		"""Describe one connection: its name, where it is, its token, and what it is."""

		try:
			token = subroutine.credentials.resolve(
				connection, default_connection=roster.default, describe_only=True
			).source

		except subroutine.errors.SubroutineError as error:
			token = f"unusable — {error.detail}"

		notes = []

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
			stop(f"There is nothing called {wanted!r} on {item.name}.", _workspace_hint(item))

		return item.name, wanted

	def _require_connection (
		world: World, name: str
	) -> subroutine.clients.base.Client:
		"""Return the open client for a connection a lookup already found something on."""

		item = world.connection(name)

		if item is None:
			stop(f"{name} could not be reached.")

		return item.client

	def _day (world: World, written: str) -> datetime.date:
		"""Read a day the user named, in their timezone."""

		resolved = subroutine.domain.schedule.interpret_day(
			written,
			timezone=world.settings.default_timezone,
			now=subroutine.db.types.utcnow(),
			field="when",
		)

		if resolved is None:
			raise subroutine.errors.ValidationError(
				f"{written!r} is not a day this understands.",
				hint="Try 'today', 'tomorrow', a weekday name, or a date like 2026-08-01.",
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

	def _listing (
		world: World,
		*,
		limit: int,
		strict: bool,
		order: str | None = None,
		project: str | None = None,
		deferred: bool = False,
		q: str | None = None,
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
					)
				)

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
	"""

	return world.address_of_item(connection, item).replace(subroutine.domain.refs.SIGIL, "")


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
		if not answer.value.rows:
			continue

		if printed:
			say("")

		console.print(rich.text.Text(answer.connection.label, style=GROUP))
		printed = True

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
		console.print("")
		console.print(rich.text.Text("Links", style=HEADING))

		width = max(len(link.label) for link in links)

		for link in links:
			line = rich.text.Text()
			line.append(f"  {link.label:<{width}}  ", style=DETAIL)
			line.append(
				f"{subroutine.domain.refs.format_ref(link.other.ref):>4}  ", style=POSITION
			)
			line.append(link.other.title)
			console.print(line)

	if remarks:
		console.print("")
		console.print(rich.text.Text("What happened", style=HEADING))

		for remark in remarks:
			line = rich.text.Text()
			line.append(f"  {remark.created_at.date().isoformat()}  ", style=DETAIL)
			line.append(remark.body)
			console.print(line)


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

	if isinstance(item, subroutine.views.Task):
		if item.importance is not None or item.urgency is not None:
			facts.append(f"!{item.importance or '—'}/u{item.urgency or '—'}")

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


def _render_day (day: datetime.date | None) -> str:
	"""Render a calendar date the way a person reads one."""

	return "—" if day is None else day.strftime("%a %-d %b")


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

	return local.strftime("%a %-d %b")


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
