"""The ``subroutine`` command.

``init`` is the only thing a new user runs before the one they actually wanted, so it
prints **one line** (SPEC.md §12.1). The workspace, the Inbox, the role assignment and the
instance identity are all created and none of them are announced: someone setting up a
to-do list has not asked about workspaces. ``--verbose`` prints the full transcript for
whoever does want it.
"""

import getpass
import pathlib
import sys
import typing

import rich.console
import sqlalchemy.exc
import sqlalchemy.orm
import typer

import subroutine.config
import subroutine.db.migrate
import subroutine.db.session
import subroutine.domain.bootstrap
import subroutine.errors

app = typer.Typer(
	name="subroutine",
	help="Project management for people and agents, in equal measure.",
	no_args_is_help=True,
	add_completion=False,
)

config_app = typer.Typer(help="Inspect and manage configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")

database_app = typer.Typer(help="Look after the database.", no_args_is_help=True)
app.add_typer(database_app, name="db")

#: Soft wrapping leaves line breaking to the terminal. Rich's own wrapper breaks inside a
#: long path, which makes an unreadable mess of exactly the values a user needs to copy.
_out = rich.console.Console(soft_wrap=True)
_err = rich.console.Console(stderr=True, soft_wrap=True)


def _say (message: str) -> None:
	"""Print a line of ordinary output.

	Markup is off: these lines carry user data — titles, paths, project keys — and a
	square bracket in a task title should appear, not be interpreted.
	"""

	_out.print(message, markup=False, highlight=False)


def _fail (error: subroutine.errors.SubroutineError) -> typing.NoReturn:
	"""Report a failure the way the API would, and stop.

	The same detail and the same hint the HTTP layer will return, so a problem looks the
	same whichever way you meet it.
	"""

	_err.print(error.detail, markup=False, highlight=False)

	if error.hint is not None:
		_err.print(error.hint, markup=False, highlight=False)

	for field in error.errors:
		_err.print(f"  {field.field}: {field.message}", markup=False, highlight=False)

	raise typer.Exit(code=1)


@app.command()
def init (
	username: str = typer.Option(
		"", "--username", help="Who you are. Defaults to your system username."
	),
	workspace: str = typer.Option(
		"Personal", "--workspace", help="What to call your first workspace."
	),
	instance_name: str = typer.Option(
		"", "--instance-name", help="What to call this installation. Defaults to the hostname."
	),
	password_stdin: bool = typer.Option(
		False, "--password-stdin", help="Read a password from standard input, for scripts."
	),
	non_interactive: bool = typer.Option(
		False, "--non-interactive", help="Never prompt; fail instead."
	),
	verbose: bool = typer.Option(False, "--verbose", help="Print what was created."),
) -> None:
	"""Set up Subroutine: create the database and everything a first task needs."""

	settings = subroutine.config.load_settings()

	_refuse_unusable_storage(settings)

	_key, written_to = subroutine.config.ensure_secret_key(settings)
	password = _read_password(password_stdin, non_interactive)

	if verbose:
		_say(f"Database:   {settings.database_url}")

		if written_to is not None:
			_say(f"Config:     {written_to} (signing key written)")

	subroutine.db.migrate.upgrade(settings.database_url)

	if verbose:
		_say(f"Schema:     migrated to {subroutine.db.migrate.head_revision()}")

	engine = subroutine.db.session.create_engine(settings.database_url)

	try:
		factory = sqlalchemy.orm.sessionmaker(bind=engine, expire_on_commit=False)

		with factory() as session:
			try:
				result = subroutine.domain.bootstrap.initialise(
					session,
					username=username or getpass.getuser(),
					instance_name=instance_name or _default_instance_name(),
					workspace_title=workspace,
					password=password,
					timezone=settings.default_timezone,
				)

			except subroutine.errors.SubroutineError as error:
				session.rollback()
				_fail(error)

			session.commit()

			if verbose:
				_say(f"Instance:   {result.instance.id}")
				_say(f"User:       {result.user.username}")
				_say(f"Workspace:  {result.workspace.title} ({result.workspace.slug})")
				_say(f"Inbox:      {result.inbox.key}")

			if not result.created:
				_say("Already set up. Try: subroutine add \"something to do\"")

				return

	finally:
		engine.dispose()

	_say('Ready. Try: subroutine add "something to do"')


@config_app.command("show")
def config_show () -> None:
	"""Print the resolved settings and where each value came from."""

	settings = subroutine.config.load_settings()
	sources = subroutine.config.setting_sources(settings)
	fields = sorted(type(settings).model_fields)
	width = max(len(name) for name in fields)

	for name in fields:
		value = getattr(settings, name)

		# The signing key is the one setting that must never be printed; whether it is set
		# is the useful part anyway.
		if name == "secret_key":
			value = "(set)" if value else "(not set)"

		_say(f"{name.ljust(width)}  {value}  [{sources[name]}]")


@database_app.command("upgrade")
def database_upgrade () -> None:
	"""Bring the database up to the newest schema."""

	settings = subroutine.config.load_settings()

	_refuse_unusable_storage(settings)
	subroutine.db.migrate.upgrade(settings.database_url)

	_say(f"Schema is at {subroutine.db.migrate.head_revision()}.")


@database_app.command("current")
def database_current () -> None:
	"""Report which migration the database is at."""

	settings = subroutine.config.load_settings()

	if _database_is_absent(settings):
		_say("There is no database here yet. Run 'subroutine init'.")

		return

	engine = subroutine.db.session.create_engine(settings.database_url)

	try:
		current = subroutine.db.migrate.current_revision(engine)
		head = subroutine.db.migrate.head_revision()

	except sqlalchemy.exc.OperationalError as error:
		_err.print(
			f"Could not reach the database at {settings.database_url}: "
			f"{error.orig or error}",
			markup=False,
			highlight=False,
		)
		_err.print(
			"Check 'database_url' in 'subroutine config show'.", markup=False, highlight=False
		)

		raise typer.Exit(code=1) from None

	finally:
		engine.dispose()

	if current is None:
		_say("This database has no schema yet. Run 'subroutine init'.")

		return

	_say(f"Schema is at {current}." if current == head else f"Schema is at {current}; newest is {head}.")


def _database_is_absent (settings: subroutine.config.Settings) -> bool:
	"""Report whether the configured SQLite file has not been created yet.

	Only answerable for SQLite: a missing file is a fact on disk, whereas a PostgreSQL
	database that cannot be reached might be absent, asleep or behind a firewall, and
	guessing which would produce confident bad advice.
	"""

	path = settings.sqlite_path

	return path is not None and not path.exists()


def _refuse_unusable_storage (settings: subroutine.config.Settings) -> None:
	"""Prepare the storage directory, and stop if SQLite cannot lock in it.

	SPEC.md §10.4. The locking failure otherwise arrives as ``database is locked`` on the
	first write, which reads as a concurrency bug rather than as "this directory is on a
	network share" — and by then there is a half-built database to clean up.
	"""

	path = settings.sqlite_path

	if path is None:
		return

	try:
		path.parent.mkdir(parents=True, exist_ok=True)

	except OSError as error:
		_err.print(f"Cannot create {path.parent}: {error}", markup=False, highlight=False)

		raise typer.Exit(code=1) from None

	problem = subroutine.config.probe_sqlite_locking(path.parent)

	if problem is not None:
		_err.print(problem, markup=False, highlight=False)

		raise typer.Exit(code=1)


def _read_password (from_stdin: bool, non_interactive: bool) -> str | None:
	"""Return the first user's password, if one was offered.

	None is the ordinary case. Local mode opens the database directly, so a password is
	only wanted once there is a server to log in to — asking for one during setup would be
	ceremony in service of nothing.
	"""

	if from_stdin:
		return sys.stdin.readline().rstrip("\n") or None

	if non_interactive:
		return None

	return None


def _default_instance_name () -> str:
	"""Return a name for this installation, from the machine it is on."""

	try:
		return pathlib.Path("/etc/hostname").read_text(encoding="utf-8").strip() or "Subroutine"

	except OSError:
		return "Subroutine"


def main () -> None:
	"""Entry point for the ``subroutine`` command."""

	app()
