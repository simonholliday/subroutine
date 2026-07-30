"""``add``, ``today``, ``ls``, ``done``, ``plan``, ``defer``, ``use`` — the personal path.

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
import typing

import rich.console
import rich.text
import typer

import subroutine.clients.base
import subroutine.clients.http
import subroutine.clients.local
import subroutine.config
import subroutine.connections
import subroutine.context
import subroutine.credentials
import subroutine.db.types
import subroutine.domain.agenda
import subroutine.domain.dates
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

	def address_of_task (self, connection: str, task: subroutine.views.Task) -> str:
		"""Return the shortest address that resolves to this task."""

		return self.address_of(connection, task.workspace_id, task.ref)


@dataclasses.dataclass(frozen=True)
class Located:
	"""One task, and which connection and workspace it was found on."""

	connection: str
	workspace: str
	task: subroutine.views.Task


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
				stop(
					"No connection could be reached.",
					"Run 'subroutine connections' to see what is configured.",
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

	def _locate (world: World, given: str) -> Located:
		"""Resolve an address into one task, or refuse in a way that can be acted on.

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
		"""

		address = subroutine.domain.refs.parse_address(given)

		if address is None:
			stop(
				f"{given!r} is not a task number.",
				"Tasks are named by the number 'subroutine ls' prints beside them — "
				"'subroutine done 42'.",
			)

		named = _named_place(world, address)

		if named is None:
			return _unqualified(world, address.ref, given)

		found = named[0].client.task(ref=address.ref, workspace=named[1])

		if found is not None:
			return Located(connection=named[0].name, workspace=named[1], task=found)

		# Not in the place the address named. Ask everywhere else before giving up, so the
		# refusal can say where it *is* — the docstring above promised this and the code did
		# not do it, which is the documented-but-absent shape this project keeps meeting.
		# Only for a *bare* number: if the caller named a workspace, they meant that one.
		elsewhere = (
			[]
			if address.workspace is not None or address.connection is not None
			else [item for item in _everywhere(world, address.ref) if item.workspace != named[1]]
		)

		if not elsewhere:
			stop(
				f"There is no task {subroutine.domain.refs.format_ref(address.ref)} in "
				f"{named[1]}.",
				"Run 'subroutine ls' to see what there is.",
			)

		shown = [_absolute(world, item) for item in elsewhere]
		width = max(len(text) for text in shown)
		listed = "\n".join(
			f"    {text:>{width}}  {item.task.title}"
			for text, item in zip(shown, elsewhere, strict=True)
		)

		stop(
			f"There is no {subroutine.domain.refs.format_ref(address.ref)} in {named[1]}, "
			f"but there is one here:\n{listed}",
			f"Say which — 'subroutine done "
			f"{shown[0].replace(subroutine.domain.refs.SIGIL, '')}'.",
		)

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

	def _unqualified (world: World, ref: int, given: str) -> Located:
		"""Resolve a bare number when nothing has chosen a context, or refuse with the choice."""

		candidates = _everywhere(world, ref)

		if not candidates:
			stop(
				f"There is no task {subroutine.domain.refs.format_ref(ref)} here.",
				"Run 'subroutine ls' to see what there is.",
			)

		if len(candidates) == 1:
			return candidates[0]

		shown = [_absolute(world, item) for item in candidates]
		width = max(len(text) for text in shown)
		listed = "\n".join(
			f"    {text:>{width}}  {item.task.title}"
			for text, item in zip(shown, candidates, strict=True)
		)

		stop(
			f"{given!r} could mean any of these:\n{listed}",
			f"Say which — 'subroutine done "
			f"{shown[0].replace(subroutine.domain.refs.SIGIL, '')}', or "
			f"'subroutine use {_place_of(world, candidates[0])}' to keep working there.",
		)

	def _everywhere (world: World, ref: int) -> list[Located]:
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
					task = item.client.task(ref=ref, workspace=workspace.slug)

					if task is not None:
						found.append(
							Located(
								connection=item.name, workspace=workspace.slug, task=task
							)
						)

		return found

	def _absolute (world: World, located: Located) -> str:
		"""Return a candidate's address, qualified as far as this world needs."""

		return subroutine.domain.refs.format_address(
			located.task.ref,
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

				say(json.dumps(body, indent=2))

				return

			say(f"Added: {captured.task.title}{_when(captured.task)}")

			if captured.unparsed:
				console.print(
					rich.text.Text(
						f"  Left as written: {', '.join(captured.unparsed)}"
						" — recurring tasks are not supported yet.",
						style=DETAIL,
					)
				)

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
		"""

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
				),
				strict=strict,
			)

			_report(world, gathered.failures)

			if json_output:
				say(json.dumps(_agenda_json(world, gathered), indent=2))

				return

			_render(world, gathered, say=say, console=console)

	@app.command("ls")
	def list_tasks (
		limit: int = typer.Option(DEFAULT_LIST_LIMIT, "--limit", help="How many to show."),
		json_output: bool = typer.Option(False, "--json", help="Print the list as JSON."),
		merged: bool = typer.Option(
			False, "--merged", help="One list rather than a group per connection."
		),
		strict: bool = typer.Option(
			False, "--strict", help="Stop if any connection cannot be reached."
		),
	) -> None:
		"""List everything still open, newest first.

		Examples:

		  subroutine ls

		  subroutine ls --limit 10
		"""

		with opened(strict=strict) as world:
			gathered = _listing(world, limit=limit, strict=strict)

			_report(world, gathered.failures)

			rows = _merged(gathered)

			if json_output:
				say(json.dumps([_as_json(world, name, task) for name, task in rows], indent=2))

				return

			if not rows:
				say("Nothing on your list.")
				_suggest(console, 'subroutine add "something to do"')

				return

			if merged or not world.qualifies_connection:
				_flat(world, rows, console=console)

			else:
				_grouped(world, gathered, console=console, say=say)

			say("")
			_suggest(console, f"subroutine done {_typeable(world, rows[0][0], rows[0][1])}")

	@app.command()
	def done (
		which: str = typer.Argument("", help="A task number, as shown by 'ls'."),
	) -> None:
		"""Tick something off.

		Examples:

		  subroutine done 42
		"""

		with opened() as world:
			located = _locate(
				world, _asked(which, "Which one? (a number like 42 — a shell eats '#42')")
			)

			if located.task.completed_at is not None:
				# Saying so beats reporting success twice. The case this is really about is
				# an up-arrow repeat, which used to land on whatever had taken that number.
				say(_acted(world, located, "Already done"))
				_suggest(console, "subroutine ls")

				return

			client = _require_connection(world, located.connection)
			finished = client.complete(ref=located.task.ref, workspace=located.workspace)

			say(_acted(world, dataclasses.replace(located, task=finished), "Done"))
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
			located = _locate(
				world, _asked(which, "Which one? (a number like 42 — a shell eats '#42')")
			)
			client = _require_connection(world, located.connection)

			changed = client.schedule(
				ref=located.task.ref,
				workspace=located.workspace,
				planned_for=_day(world, _asked(when, "Which day?")),
			)

			# The planned day, not `_when`'s answer. `_when` prefers a deadline, which is
			# right in a list and wrong in the confirmation of a command whose whole job was
			# to set the other field — the user said "tomorrow" and was shown Friday.
			say(
				_acted(
					world,
					dataclasses.replace(located, task=changed),
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
			located = _locate(
				world, _asked(which, "Which one? (a number like 42 — a shell eats '#42')")
			)
			client = _require_connection(world, located.connection)

			changed = client.schedule(
				ref=located.task.ref,
				workspace=located.workspace,
				start=_day(world, _asked(when, "Hide it until when?")),
			)

			say(
				_acted(
					world,
					dataclasses.replace(located, task=changed),
					# The *task's* zone, like every other instant this program renders.
					# `start_at` is midnight where the task lives, so re-reading it in a
					# westward client zone named the day before the one that was asked for.
					f"Hidden until {_render_date(changed.start_at, changed.timezone)}",
				)
			)
			_suggest(console, "subroutine today")

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
				_suggest(console, "subroutine use --reset")

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
		world: World, *, limit: int, strict: bool
	) -> subroutine.fanout.Gathered[list[tuple[str, subroutine.views.Task]]]:
		"""List every reachable workspace's tasks, one request per workspace.

		Per workspace rather than per connection because ``GET /v1/tasks`` refuses an
		ambiguous one (§8.2) — and a local client that quietly spanned them would return
		different rows depending on where the tasks were, which is the divergence this whole
		arrangement exists to prevent.
		"""

		def ask (
			client: subroutine.clients.base.Client,
		) -> list[tuple[str, subroutine.views.Task]]:
			"""Ask one connection for each of its workspaces in turn."""

			item = world.connection(client.connection.name)
			rows: list[tuple[str, subroutine.views.Task]] = []

			for workspace in () if item is None else item.identity.workspaces:
				rows.extend(
					(client.connection.name, task)
					for task in client.tasks(workspace=workspace.slug, limit=limit)
				)

			# Re-sorted after the merge, on a field the client can compute for itself
			# (§13.7). A merged result is a merge of pages, not one ordered page, so the
			# limit is per workspace and applied again here.
			rows.sort(key=lambda row: row[1].created_at, reverse=True)

			return rows[:limit]

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
			return f"{verb}: {located.task.title}"

		absolute = subroutine.domain.refs.format_address(
			located.task.ref,
			workspace=located.workspace,
			connection=located.connection if world.qualifies_connection else None,
		)

		return f"{verb}: {absolute}  {located.task.title}"

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

	if connection.is_local:
		return subroutine.clients.local.opened(
			connection, settings, default_connection=roster.default
		)

	return subroutine.clients.http.opened(connection, default_connection=roster.default)


def _workspace_hint (item: Reached) -> str:
	"""Describe the workspaces one connection reaches."""

	if not item.identity.workspaces:
		return "That connection reaches no workspaces at all."

	listed = ", ".join(workspace.slug for workspace in item.identity.workspaces)

	return f"Workspaces on {item.name}: {listed}."


def _typeable (world: World, connection: str, task: subroutine.views.Task) -> str:
	"""Return what to type to reach one task — the printed form without its sigil.

	A suggested command has to be one that works, and ``#`` starts a comment in every POSIX
	shell (SPEC.md §12.2a), so a suggestion carries the bare number or the qualified path.
	"""

	return world.address_of_task(connection, task).replace(subroutine.domain.refs.SIGIL, "")


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
	rows: dict[str, list[tuple[str, subroutine.views.Task]]] = {}

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
	# than stepping in and out as the sections change.
	width = _width(world, [row for group in rows.values() for row in group])
	remaining = sum(
		answer.value.unscheduled_total - len(answer.value.unscheduled)
		for answer in gathered.answers
	)
	printed = False
	first: tuple[str, subroutine.views.Task] | None = None

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
			console.print(_task_line(world, connection, task, late=late, width=width))

	if remaining > 0:
		console.print(rich.text.Text(f"      and {remaining} more unscheduled", style=DETAIL))

	if first is None:
		_suggest(console, 'subroutine add "something to do"')

		return

	say("")
	_suggest(console, f"subroutine done {_typeable(world, first[0], first[1])}")


def _flat (
	world: World,
	rows: typing.Sequence[tuple[str, subroutine.views.Task]],
	*,
	console: rich.console.Console,
) -> None:
	"""Print one list, every row addressed by the shortest form that resolves."""

	width = _width(world, rows)

	for connection, task in rows:
		console.print(_task_line(world, connection, task, late=False, width=width))


def _grouped (
	world: World,
	gathered: subroutine.fanout.Gathered[list[tuple[str, subroutine.views.Task]]],
	*,
	console: rich.console.Console,
	say: typing.Callable[[str], None],
) -> None:
	"""Print a group per connection, which is what a flat listing has instead of structure.

	Unlike the agenda, a list of open tasks has no ordering a person already holds in their
	head, so the connection is the only structure there is — and a heading carries the label
	once rather than repeating it on every line (§13.7).
	"""

	printed = False

	for answer in gathered.answers:
		if not answer.value:
			continue

		if printed:
			say("")

		console.print(rich.text.Text(answer.connection.label, style=GROUP))
		printed = True

		_flat(world, answer.value, console=console)


def _width (world: World, rows: typing.Sequence[tuple[str, subroutine.views.Task]]) -> int:
	"""Return how wide the address column needs to be for these rows."""

	return max(
		(len(world.address_of_task(connection, task)) for connection, task in rows), default=0
	)


def _suggest (console: rich.console.Console, command: str) -> None:
	"""Print the command to try next (SPEC.md §12.2a).

	The single most valuable habit here: the user is never left wondering what exists.
	"""

	console.print(rich.text.Text(f"  {command}", style=SUGGESTION))


def _task_line (
	world: World,
	connection: str,
	task: subroutine.views.Task,
	*,
	late: bool,
	width: int = 0,
) -> rich.text.Text:
	"""Return one task line, addressed by a ref that never changes.

	**The identifier shown is the task's own ref.** It used to be the row's position in the
	last listing, and that was a quiet trap: completing something renumbered everything below
	it, so re-running ``done 1`` after a fresh ``ls`` marked a *different* task done — one
	up-arrow away, and wrong without saying so.

	Built with :class:`rich.text.Text` rather than markup, because a title is user data: a
	task called ``Fix [bold] handling`` must print as written, not as an instruction.
	"""

	line = rich.text.Text()
	shown = world.address_of_task(connection, task)
	line.append(f"  {shown:>{max(width, 3)}}  ", style=POSITION)
	line.append(task.title)

	detail = _when(task)

	if detail:
		line.append(detail, style=LATE if late else DETAIL)

	return line


def _render_day (day: datetime.date | None) -> str:
	"""Render a calendar date the way a person reads one."""

	return "—" if day is None else day.strftime("%a %-d %b")


def _when (task: subroutine.views.Task) -> str:
	"""Return a short trailing phrase describing a task's dates, or nothing at all.

	Nothing at all is the common case, and it matters: a to-do list that annotates every
	line with empty fields is one that looks like a database (§1.4).
	"""

	if task.due_at is not None:
		return f"  (due {_render_date(task.due_at, task.timezone)})"

	if task.planned_for is not None:
		return f"  (for {_render_day(task.planned_for)})"

	return ""


def _render_date (instant: datetime.datetime | None, timezone: str | None) -> str:
	"""Render an instant the way a person reads a date."""

	if instant is None:
		return "—"

	local = instant.astimezone(
		subroutine.domain.dates.zone(timezone or subroutine.domain.schedule.DEFAULT_TIMEZONE)
	)

	return local.strftime("%a %-d %b")


def _as_json (
	world: World, connection: str, task: subroutine.views.Task
) -> dict[str, typing.Any]:
	"""Return a task as the scripted path sees it.

	Carries the *address* as well as the ref, because a script merging two connections needs
	the thing it can type back — which is exactly what a bare number stops being once there
	is more than one place a task could live.
	"""

	return {
		"ref": task.ref,
		"address": world.address_of_task(connection, task),
		"connection": connection,
		"title": task.title,
		"due_at": None if task.due_at is None else task.due_at.isoformat(),
		"due_is_all_day": task.due_is_all_day,
		"planned_for": None if task.planned_for is None else task.planned_for.isoformat(),
		"start_at": None if task.start_at is None else task.start_at.isoformat(),
		"importance": task.importance,
		"estimate_minutes": task.estimate_minutes,
		"tags": list(task.tags),
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


def _merged (
	gathered: subroutine.fanout.Gathered[list[tuple[str, subroutine.views.Task]]],
) -> list[tuple[str, subroutine.views.Task]]:
	"""Flatten a listing across connections, newest first.

	§13.7: "sorting is re-applied after the merge". Each connection answers already ordered, so
	concatenating them produced one sorted run per connection rather than one ordered list —
	which is not what "newest first" means and is not what the suggested next command assumed.
	"""

	rows = [row for answer in gathered.answers for row in answer.value]
	rows.sort(key=lambda row: (row[1].created_at, row[1].ref), reverse=True)

	return rows


def _in_order (
	rows: list[tuple[str, subroutine.views.Task]], bucket: str
) -> list[tuple[str, subroutine.views.Task]]:
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
	rows.sort(
		key=lambda row: (
			row[1].due_at is None,
			row[1].due_at or datetime.datetime.max.replace(tzinfo=datetime.UTC),
			row[1].ref,
		)
	)

	return rows
