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
every command suggests the next one, positions from the last listing address tasks, and
``--json`` on every read command so the human path and the scripted path are the same code.
"""

import contextlib
import dataclasses
import datetime
import json
import os
import typing

import rich.console
import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.orm
import typer

import subroutine.cli.listing
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
import subroutine.domain.tags
import subroutine.domain.tasks
import subroutine.errors

#: How many tasks ``ls`` shows before it stops. Enough to scroll, few enough to read.
DEFAULT_LIST_LIMIT = 50


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
		"""Resolve a ref or a position from the last listing into a task."""

		ref = given.strip()

		if ref.isdigit():
			remembered = subroutine.cli.listing.resolve(int(ref))

			if remembered is None:
				stop(
					f"There is no {ref} in the last list shown.",
					"Run 'subroutine today' or 'subroutine ls' first, or name the task by "
					"its ref — 'subroutine done SR-42'.",
				)

			ref = remembered

		found = subroutine.domain.refs.find(context.session, context.workspace.id, ref.upper())

		if found is None or found[0] != "task":
			stop(
				f"There is no task called {given!r}.",
				"Refs look like SR-42. Run 'subroutine today' to see what there is.",
			)

		task = context.session.get(subroutine.db.models.work.Task, found[1])

		if task is None or task.deleted_at is not None:
			stop(f"There is no task called {given!r}.")

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
				say(
					f"  Left as written: {', '.join(captured.unparsed)} "
					"— recurring tasks are not supported yet."
				)

			say("  subroutine today")

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

			tasks = list(
				context.session.scalars(
					sqlalchemy.select(model)
					.where(
						model.workspace_id.in_(context.workspace_ids),
						model.deleted_at.is_(None),
						model.completed_at.is_(None),
						model.is_template.is_(False),
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
				say('  subroutine add "something to do"')

				return

			_numbered(context, tasks, say=say)
			say("  subroutine done 1")

	@app.command()
	def done (
		which: str = typer.Argument(..., help="A ref like SR-42, or a number from the last list."),
	) -> None:
		"""Tick something off.

		Examples:

		  subroutine done 1

		  subroutine done SR-42
		"""

		with opened() as context:
			task = _lookup(context, which)

			subroutine.domain.tasks.update(
				context.session,
				task,
				status_key=_finished_status_key(context),
				now=context.now,
				actor=context.principal,
			)

			say(f"Done: {task.title}")
			say("  subroutine today")

	@app.command()
	def plan (
		which: str = typer.Argument(..., help="A ref like SR-42, or a number from the last list."),
		when: str = typer.Argument(..., help="A day — 'today', 'tomorrow', 'friday', '2026-08-01'."),
	) -> None:
		"""Say which day you will do something.

		Examples:

		  subroutine plan 1 tomorrow

		  subroutine plan SR-42 friday
		"""

		with opened() as context:
			task = _lookup(context, which)

			subroutine.domain.tasks.update(
				context.session,
				task,
				planned_for=_day(context, when),
				now=context.now,
				actor=context.principal,
			)

			say(f"Planned: {task.title}{_when(context, task)}")
			say("  subroutine today")

	@app.command()
	def defer (
		which: str = typer.Argument(..., help="A ref like SR-42, or a number from the last list."),
		when: str = typer.Argument(..., help="A day to hide it until."),
	) -> None:
		"""Hide something until later.

		Examples:

		  subroutine defer 1 monday

		  subroutine defer SR-42 2026-09-01
		"""

		with opened() as context:
			task = _lookup(context, which)

			subroutine.domain.tasks.update(
				context.session,
				task,
				start=_day(context, when),
				now=context.now,
				actor=context.principal,
			)

			say(f"Hidden until {_render_date(task.start_at, context.timezone)}: {task.title}")
			say("  subroutine today")

	def show_today () -> None:
		"""Print today's agenda, as a bare ``subroutine`` invocation does."""

		today(json_output=False)

	return show_today


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


def _finished_status_key (context: Context) -> str:
	"""Return the key of a status meaning finished, whatever this workspace calls it.

	Statuses are data — an installation renames and adds them freely (§5.5) — so the
	personal path cannot hard-code ``"done"``. It asks for the first status in the ``done``
	category instead, which is what makes ``subroutine done`` keep working after somebody
	renames it to "Shipped".
	"""

	model = subroutine.db.models.vocabulary.Status

	found = context.session.scalars(
		sqlalchemy.select(model)
		.where(
			model.workspace_id == context.workspace.id,
			model.entity_type == "task",
			model.category == "done",
		)
		.order_by(model.position)
	).first()

	if found is None:
		raise subroutine.errors.InternalError(
			"This workspace has no status meaning 'done'.",
			hint="Its vocabulary is incomplete; restore it, or start again from an empty "
			"database.",
		)

	return found.key


def _render (
	context: Context,
	agenda: subroutine.domain.agenda.Agenda,
	*,
	say: typing.Callable[[str], None],
	console: rich.console.Console,
) -> None:
	"""Print the agenda, and record the numbering so ``done 1`` works afterwards."""

	sections = (
		("Overdue", agenda.overdue),
		("Today", agenda.today),
		("Next 7 days", agenda.upcoming),
		("Unscheduled", agenda.unscheduled),
	)
	shown: list[str] = []
	printed = False

	if not agenda.overdue and not agenda.today:
		say("Nothing due today.")

	for heading, tasks in sections:
		if not tasks:
			continue

		if printed:
			say("")

		say(heading)
		printed = True

		for task in tasks:
			shown.append(task.ref)
			say(f"  {len(shown):>2}  {task.title}{_when(context, task)}")

	if agenda.unscheduled_total > len(agenda.unscheduled):
		say(f"      and {agenda.unscheduled_total - len(agenda.unscheduled)} more unscheduled")

	subroutine.cli.listing.remember(shown)

	if not shown:
		say('  subroutine add "something to do"')

		return

	say("")
	say("  subroutine done 1")


def _numbered (
	context: Context,
	tasks: typing.Sequence[subroutine.db.models.work.Task],
	*,
	say: typing.Callable[[str], None],
) -> None:
	"""Print a numbered list and remember the numbering."""

	for position, task in enumerate(tasks, start=1):
		say(f"  {position:>2}  {task.title}{_when(context, task)}")

	subroutine.cli.listing.remember([task.ref for task in tasks])


def _when (context: Context, task: subroutine.db.models.work.Task) -> str:
	"""Return a short trailing phrase describing a task's dates, or nothing at all.

	Nothing at all is the common case, and it matters: a to-do list that annotates every
	line with empty fields is one that looks like a database (§1.4).
	"""

	if task.due_at is not None:
		return f"  (due {_render_date(task.due_at, context.timezone)})"

	if task.planned_for is not None:
		return f"  (for {task.planned_for.strftime('%a %-d %b')})"

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
