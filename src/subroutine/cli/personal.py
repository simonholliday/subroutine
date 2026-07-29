"""``add``, ``today``, ``ls``, ``done``, ``plan``, ``defer`` — the whole personal path.

These five or six commands are the entire surface a person needs, and §13.5b says so with
a stopwatch: a fresh installation to a working to-do list in three commands, a task
completed with a fourth, and **not one of those outputs mentioning a workspace, a status, a
project, a criterion, a verification, a session or a claim**. That is the guard on §1.4's
progressive-disclosure rule, and it will fail the first time somebody adds a required field
for an agent's benefit — which is the point of having it.

Everything here runs in **local mode** (§12.1a): the database is opened directly and the
service layer is called, with a real principal and the same ``authorize()`` the API will
use. No server, no token, no environment variables.

The rendering obligations are §12.2a's, and they are product surface rather than polish:
every command suggests the next one, tasks are addressed by a ref that never changes, and
``--json`` on every read command so the human path and the scripted path are the same code.
"""

import contextlib
import dataclasses
import datetime
import json
import os
import typing

import rich.console
import rich.text
import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.orm
import typer

import subroutine.config
import subroutine.db.models.identity
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.session
import subroutine.db.types
import subroutine.domain.agenda
import subroutine.domain.authentication
import subroutine.domain.dates
import subroutine.domain.local
import subroutine.domain.refs
import subroutine.domain.schedule
import subroutine.domain.scoping
import subroutine.domain.tags
import subroutine.domain.tasks
import subroutine.errors

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


@dataclasses.dataclass(frozen=True)
class Context:
	"""One command's view of the world: a session, who is acting, and where."""

	session: sqlalchemy.orm.Session
	principal: subroutine.domain.authentication.Principal
	workspace: subroutine.db.models.identity.Workspace
	timezone: str
	now: datetime.datetime

	@property
	def workspace_ids (self) -> list[typing.Any]:
		"""Return every workspace this principal may read."""

		return subroutine.domain.local.readable_workspace_ids(self.session, self.principal)


def register (
	app: typer.Typer,
	*,
	say: typing.Callable[[str], None],
	fail: typing.Callable[[subroutine.errors.SubroutineError], typing.NoReturn],
	stop: typing.Callable[..., typing.NoReturn],
	settings: typing.Callable[[], subroutine.config.Settings],
	console: rich.console.Console,
) -> typing.Callable[[], None]:
	"""Add the personal commands to the application, and return the bare-invocation one.

	Takes its output helpers from ``main`` rather than importing them, so that there is one
	definition of how this program speaks and prints — and so this module does not import
	the one that imports it.

	The returned callable is what a bare ``subroutine`` runs (§12.2a). It is handed back
	rather than looked up afterwards because Typer leaves an ``OptionInfo`` as the default
	of every option, so calling a registered command's function without arguments passes an
	object where a boolean belongs — and ``--json`` would silently be on.
	"""

	@contextlib.contextmanager
	def opened () -> typing.Iterator[Context]:
		"""Yield a context, committing on success and explaining any refusal."""

		resolved = settings()
		engine = subroutine.db.session.create_engine(resolved.database_url)

		try:
			factory = sqlalchemy.orm.sessionmaker(bind=engine, expire_on_commit=False)

			with factory() as session:
				try:
					principal = subroutine.domain.local.principal(
						session,
						token=_token_from_environment(),
						local_user=resolved.local_user,
					)
					workspace = subroutine.domain.local.workspace_for(session, principal)

				except subroutine.errors.SubroutineError as error:
					fail(error)

				zone = subroutine.domain.schedule.zone_for(
					user=principal.user, workspace=workspace
				)

				try:
					yield Context(
						session=session,
						principal=principal,
						workspace=workspace,
						timezone=zone,
						now=subroutine.db.types.utcnow(),
					)

				except subroutine.errors.SubroutineError as error:
					session.rollback()
					fail(error)

				session.commit()

		except sqlalchemy.exc.OperationalError as error:
			stop(
				f"Could not open your to-do list: {error.orig or error}",
				"Run 'subroutine init' if you have not set up yet.",
			)

		finally:
			engine.dispose()

	def _lookup (context: Context, given: str) -> subroutine.db.models.work.Task:
		"""Resolve a ref into a task.

		**A ref is a number, and it is never a row.** It used to be a position in the last
		listing, which meant the number for a task changed every time something above it was
		completed — so an absent-minded up-arrow re-ran ``done 1`` against a different task
		and said nothing about it. A ref is allocated once from the workspace's counter and
		is never reused, so the number somebody memorised while working on something goes on
		meaning it.

		``42`` and ``#42`` are the same request: listings print the sigil because it reads
		as an identifier rather than a count, and it is optional on input because a shell
		would eat it (SPEC.md §12.2a).

		Resolved **through the scoping helper**, so a task in a project this caller cannot
		see is reported as absent rather than fetched. Completed tasks are included on
		purpose: running ``done 42`` twice should say the thing is already done, not that
		there is no such task.
		"""

		wanted = subroutine.domain.refs.parse_ref(given)

		if wanted is None:
			stop(
				f"{given!r} is not a task number.",
				"Tasks are named by the number 'subroutine ls' prints beside them — "
				"'subroutine done 42'.",
			)

		model = subroutine.db.models.work.Task

		task = context.session.scalars(
			subroutine.domain.scoping.readable_tasks(
				context.principal,
				workspace_ids=context.workspace_ids,
				include_archived=True,
			).where(model.ref == wanted)
		).first()

		if task is None:
			stop(
				f"There is no task {subroutine.domain.refs.format_ref(wanted)} here.",
				"Run 'subroutine ls' to see what there is.",
			)

		return task

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
			# A required-argument error is a dead end where a question would do (§12.2a).
			text = typer.prompt("What do you need to do?")

		with opened() as context:
			task, captured = subroutine.domain.tasks.create_from_text(
				context.session,
				workspace=context.workspace,
				text=text,
				now=context.now,
				timezone=context.timezone,
				actor=context.principal,
			)

			if json_output:
				say(json.dumps(_as_json(context, task), indent=2))

				return

			say(f"Added: {task.title}{_when(context, task)}")

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
	) -> None:
		"""Show what you are doing today.

		Examples:

		  subroutine today
		"""

		with opened() as context:
			agenda = subroutine.domain.agenda.build(
				context.session,
				principal=context.principal,
				workspace_ids=context.workspace_ids,
				now=context.now,
				timezone=context.timezone,
				horizon_days=subroutine.domain.agenda.DEFAULT_HORIZON_DAYS,
			)

			if json_output:
				say(json.dumps(_agenda_json(context, agenda), indent=2))

				return

			_render(context, agenda, say=say, console=console)

	@app.command("ls")
	def list_tasks (
		limit: int = typer.Option(DEFAULT_LIST_LIMIT, "--limit", help="How many to show."),
		json_output: bool = typer.Option(False, "--json", help="Print the list as JSON."),
	) -> None:
		"""List everything still open, newest first.

		Examples:

		  subroutine ls

		  subroutine ls --limit 10
		"""

		with opened() as context:
			model = subroutine.db.models.work.Task

			# Narrowed by `scoping`, not by hand. This query filtered by workspace and never
			# joined the project, so it listed the titles of tasks in private projects the
			# caller was not a member of — while the agenda, three modules away, hid them.
			tasks = list(
				context.session.scalars(
					subroutine.domain.scoping.readable_tasks(
						context.principal,
						workspace_ids=context.workspace_ids,
						include_completed=False,
					)
					.order_by(sqlalchemy.desc(model.created_at))
					.limit(limit)
				)
			)

			if json_output:
				say(json.dumps([_as_json(context, task) for task in tasks], indent=2))

				return

			if not tasks:
				say("Nothing on your list.")
				_suggest(console, 'subroutine add "something to do"')

				return

			_numbered(context, tasks, console=console)
			say("")
			_suggest(console, f"subroutine done {tasks[0].ref}")

	@app.command()
	def done (
		which: str = typer.Argument("", help="A task number, as shown by 'ls'."),
	) -> None:
		"""Tick something off.

		Examples:

		  subroutine done 42

		  subroutine done 42
		"""

		with opened() as context:
			task = _lookup(context, _asked(which, "Which one? (a number like 42 — a shell eats '#42')"))

			if task.completed_at is not None:
				# Saying so beats reporting success twice. The case this is really about is
				# an up-arrow repeat, which used to land on whatever had taken that number.
				say(f"Already done: {task.title}")
				_suggest(console, "subroutine ls")

				return

			subroutine.domain.tasks.complete(
				context.session, task, now=context.now, actor=context.principal
			)

			say(f"Done: {task.title}")
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

		with opened() as context:
			task = _lookup(context, _asked(which, "Which one? (a number like 42 — a shell eats '#42')"))

			subroutine.domain.tasks.update(
				context.session,
				task,
				planned_for=_day(context, _asked(when, "Which day?")),
				now=context.now,
				actor=context.principal,
			)

			# The planned day, not `_when`'s answer. `_when` prefers a deadline, which is
			# right in a list and wrong in the confirmation of a command whose whole job was
			# to set the other field — the user said "tomorrow" and was shown Friday.
			say(f"Planned for {_render_day(task.planned_for)}: {task.title}")
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

		with opened() as context:
			task = _lookup(context, _asked(which, "Which one? (a number like 42 — a shell eats '#42')"))

			subroutine.domain.tasks.update(
				context.session,
				task,
				start=_day(context, _asked(when, "Hide it until when?")),
				now=context.now,
				actor=context.principal,
			)

			say(f"Hidden until {_render_date(task.start_at, context.timezone)}: {task.title}")
			_suggest(console, "subroutine today")

	def show_today () -> None:
		"""Print today's agenda, as a bare ``subroutine`` invocation does."""

		today(json_output=False)

	return show_today


def _asked (given: str, question: str) -> str:
	"""Return an argument, asking for it if it was left out.

	SPEC.md §12.2a: bare commands prompt rather than error. A required-argument error is a
	dead end where a question would do — and in a pipe, where there is nobody to ask, the
	prompt fails with the usage anyway, which is the right answer there.
	"""

	if given.strip():
		return given

	answer: str = typer.prompt(question)

	return answer


def _day (context: Context, written: str) -> datetime.date:
	"""Read a day the user named, in their timezone."""

	resolved = subroutine.domain.schedule.interpret_day(
		written, timezone=context.timezone, now=context.now, field="when"
	)

	if resolved is None:
		raise subroutine.errors.ValidationError(
			f"{written!r} is not a day this understands.",
			hint="Try 'today', 'tomorrow', a weekday name, or a date like 2026-08-01.",
		)

	return resolved


def _render (
	context: Context,
	agenda: subroutine.domain.agenda.Agenda,
	*,
	say: typing.Callable[[str], None],
	console: rich.console.Console,
) -> None:
	"""Print the agenda, each line addressed by the ref that will still mean it tomorrow."""

	sections = (
		("Overdue", agenda.overdue, True),
		("Today", agenda.today, False),
		("Next 7 days", agenda.upcoming, False),
		("Unscheduled", agenda.unscheduled, False),
	)
	shown: list[int] = []
	printed = False

	# One width across every bucket, so the refs line up down the whole agenda rather than
	# stepping in and out as the sections change.
	width = _ref_width(
		[*agenda.overdue, *agenda.today, *agenda.upcoming, *agenda.unscheduled]
	)

	if not agenda.overdue and not agenda.today:
		say("Nothing due today.")

	for heading, tasks, late in sections:
		if not tasks:
			continue

		if printed:
			say("")

		console.print(rich.text.Text(heading, style=LATE if late else HEADING))
		printed = True

		for task in tasks:
			shown.append(task.ref)
			console.print(_task_line(context, task, late=late, width=width))

	if agenda.unscheduled_total > len(agenda.unscheduled):
		remaining = agenda.unscheduled_total - len(agenda.unscheduled)

		console.print(
			rich.text.Text(f"      and {remaining} more unscheduled", style=DETAIL)
		)

	if not shown:
		_suggest(console, 'subroutine add "something to do"')

		return

	say("")
	_suggest(console, f"subroutine done {shown[0]}")


def _numbered (
	context: Context,
	tasks: typing.Sequence[subroutine.db.models.work.Task],
	*,
	console: rich.console.Console,
) -> None:
	"""Print a list, each line addressed by the ref that will still mean it tomorrow."""

	width = _ref_width(tasks)

	for task in tasks:
		console.print(_task_line(context, task, late=False, width=width))


def _ref_width (tasks: typing.Sequence[subroutine.db.models.work.Task]) -> int:
	"""Return how wide the ref column needs to be for these tasks."""

	return max(
		(len(subroutine.domain.refs.format_ref(task.ref)) for task in tasks), default=0
	)


def _suggest (console: rich.console.Console, command: str) -> None:
	"""Print the command to try next (SPEC.md §12.2a).

	The single most valuable habit here: the user is never left wondering what exists.
	"""

	console.print(rich.text.Text(f"  {command}", style=SUGGESTION))


def _task_line (
	context: Context, task: subroutine.db.models.work.Task, *, late: bool, width: int = 0
) -> rich.text.Text:
	"""Return one task line, addressed by its ref and styled without interpreting the title.

	**The identifier shown is the task's own ref, which never changes.** It used to be the
	row's position in the last listing, and that was a quiet trap: completing something
	renumbered everything below it, so re-running ``done 1`` after a fresh ``ls`` marked a
	*different* task done — one up-arrow away, and wrong without saying so.

	Built with :class:`rich.text.Text` rather than markup, because a title is user data: a
	task called ``Fix [bold] handling`` must print as written, not as an instruction.
	"""

	line = rich.text.Text()
	shown = subroutine.domain.refs.format_ref(task.ref)
	line.append(f"  {shown:>{max(width, 3)}}  ", style=POSITION)
	line.append(task.title)

	detail = _when(context, task)

	if detail:
		line.append(detail, style=LATE if late else DETAIL)

	return line


def _render_day (day: datetime.date | None) -> str:
	"""Render a calendar date the way a person reads one."""

	return "—" if day is None else day.strftime("%a %-d %b")


def _when (context: Context, task: subroutine.db.models.work.Task) -> str:
	"""Return a short trailing phrase describing a task's dates, or nothing at all.

	Nothing at all is the common case, and it matters: a to-do list that annotates every
	line with empty fields is one that looks like a database (§1.4).
	"""

	if task.due_at is not None:
		return f"  (due {_render_date(task.due_at, context.timezone)})"

	if task.planned_for is not None:
		return f"  (for {_render_day(task.planned_for)})"

	return ""


def _render_date (instant: datetime.datetime | None, timezone: str) -> str:
	"""Render an instant the way a person reads a date."""

	if instant is None:
		return "—"

	local = instant.astimezone(subroutine.domain.dates.zone(timezone))

	return local.strftime("%a %-d %b")


def _as_json (
	context: Context, task: subroutine.db.models.work.Task
) -> dict[str, typing.Any]:
	"""Return a task as the scripted path sees it.

	The same fields the human path shows, plus the ref — enough to act on without a second
	call, which is the same obligation §13.6 places on the API.
	"""

	return {
		"ref": task.ref,
		"title": task.title,
		"due_at": None if task.due_at is None else task.due_at.isoformat(),
		"due_is_all_day": task.due_is_all_day,
		"planned_for": None if task.planned_for is None else task.planned_for.isoformat(),
		"start_at": None if task.start_at is None else task.start_at.isoformat(),
		"importance": task.importance,
		"estimate_minutes": task.estimate_minutes,
		"tags": [tag.name for tag in subroutine.domain.tags.for_task(context.session, task)],
	}


def _agenda_json (
	context: Context, agenda: subroutine.domain.agenda.Agenda
) -> dict[str, typing.Any]:
	"""Return the agenda as the scripted path sees it."""

	return {
		"date": agenda.date.isoformat(),
		"timezone": agenda.timezone,
		"overdue": [_as_json(context, task) for task in agenda.overdue],
		"today": [_as_json(context, task) for task in agenda.today],
		"upcoming": [_as_json(context, task) for task in agenda.upcoming],
		"unscheduled": [_as_json(context, task) for task in agenda.unscheduled],
		"unscheduled_total": agenda.unscheduled_total,
	}


def _token_from_environment () -> str | None:
	"""Return ``SUBROUTINE_TOKEN`` if it is set (SPEC.md §12.1a).

	Read here rather than through ``Settings`` because it is a credential, not a setting:
	it must never end up in ``config show``, in a config file, or in a bug report.
	"""

	return os.environ.get("SUBROUTINE_TOKEN") or None
