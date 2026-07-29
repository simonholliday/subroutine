"""The ``subroutine`` command.

``init`` is the only thing a new user runs before the one they actually wanted, so it
prints **one line** (SPEC.md §12.1). The workspace, the Inbox, the role assignment and the
instance identity are all created and none of them are announced: someone setting up a
to-do list has not asked about workspaces. ``--verbose`` prints the full transcript for
whoever does want it.
"""

import contextlib
import getpass
import pathlib
import sys
import tomllib
import typing

import pydantic
import rich.console
import sqlalchemy.engine
import sqlalchemy.exc
import sqlalchemy.orm
import typer

import subroutine.cli.personal
import subroutine.cli.topics
import subroutine.config
import subroutine.db.migrate
import subroutine.db.session
import subroutine.domain.bootstrap
import subroutine.errors

app = typer.Typer(
	name="subroutine",
	help="Project management for people and agents, in equal measure.",
	# **Not** `no_args_is_help`. SPEC.md §12.2a: the first thing this tool does unprompted
	# should be useful, so a bare `subroutine` prints today's agenda rather than a help
	# wall. `--help` is still one keystroke away for anyone who wants the wall.
	invoke_without_command=True,
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


def _stop (message: str, hint: str | None = None) -> typing.NoReturn:
	"""Report a failure that has no error code yet, and stop."""

	_err.print(message, markup=False, highlight=False)

	if hint is not None:
		_err.print(hint, markup=False, highlight=False)

	raise typer.Exit(code=1)


def safe_url (url: str) -> str:
	"""Return a database URL with any password replaced by ``***``.

	Every path that shows a URL goes through this. The URL is the one piece of
	configuration that routinely carries a credential, and it is also the value the CLI
	tells people to check when a connection fails — so it is exactly what ends up pasted
	into a bug report. Masking the signing key while printing a PostgreSQL password beside
	it would be a strange place to draw the line.
	"""

	try:
		parsed = sqlalchemy.engine.make_url(url)

	except Exception:
		# An unparseable URL cannot be masked field by field. Rather than guess at its
		# shape with a regular expression, say nothing about its contents.
		return "(unparseable database_url)"

	return parsed.render_as_string(hide_password=True)


def _settings () -> subroutine.config.Settings:
	"""Resolve configuration, explaining a bad value rather than raising through it.

	Every command starts here, so every command inherits the explanation. An unreadable
	config file or a mistyped environment variable is an ordinary mistake, and it should
	read like one.
	"""

	try:
		return subroutine.config.load_settings()

	except tomllib.TOMLDecodeError as error:
		_stop(
			f"{subroutine.config.config_file_path()} is not valid TOML: {error}",
			"Fix the file, or move it aside and run 'subroutine init' to write a new one.",
		)

	except pydantic.ValidationError as error:
		problems = "; ".join(
			f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
			for item in error.errors()
		)

		_stop(
			f"A configuration value could not be used: {problems}",
			"Check your SUBROUTINE_* environment variables and "
			f"{subroutine.config.config_file_path()}.",
		)


@contextlib.contextmanager
def _database (settings: subroutine.config.Settings) -> typing.Iterator[sqlalchemy.engine.Engine]:
	"""Yield an engine, turning connection failures into sentences.

	The failures here are ordinary operational ones — a server that is not running, a URL
	with a typo, a password that changed — and none of them is a bug in Subroutine. A
	traceback tells the user nothing they can act on and hides the one line that would.
	"""

	try:
		engine = subroutine.db.session.create_engine(settings.database_url)

	except Exception as error:
		_stop(
			f"That database URL cannot be used: {error}",
			"Check 'database_url' in 'subroutine config show'.",
		)

	try:
		yield engine

	except sqlalchemy.exc.OperationalError as error:
		_stop(
			f"Could not reach the database at {safe_url(settings.database_url)}: "
			f"{error.orig or error}",
			"Check that the server is running, and check 'database_url' in "
			"'subroutine config show'.",
		)

	except sqlalchemy.exc.SQLAlchemyError as error:
		_stop(
			f"The database rejected that: {error.orig if hasattr(error, 'orig') else error}",
			"If this looks like a bug rather than a bad value, please report it.",
		)

	finally:
		engine.dispose()


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

	settings = _settings()

	_refuse_unusable_storage(settings)

	password = _read_password(password_stdin, non_interactive)
	_key, written_to = subroutine.config.ensure_secret_key(settings)

	if verbose:
		_say(f"Database:   {safe_url(settings.database_url)}")

		if written_to is not None:
			_say(f"Config:     {written_to} (signing key written)")

	try:
		subroutine.db.migrate.upgrade(settings.database_url)

	except sqlalchemy.exc.SQLAlchemyError as error:
		_stop(
			f"Could not prepare the database at {safe_url(settings.database_url)}: "
			f"{getattr(error, 'orig', None) or error}",
			"Check that the server is running, and check 'database_url' in "
			"'subroutine config show'.",
		)

	if verbose:
		_say(f"Schema:     migrated to {subroutine.db.migrate.head_revision()}")

	with _database(settings) as engine:
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

	_say('Ready. Try: subroutine add "something to do"')


@app.command("help")
def help_topic (
	topic: str = typer.Argument("", help="A concept to explain. Omit to list them."),
) -> None:
	"""Explain a concept — refs, dates, the capture grammar, scripting.

	Examples:

	  subroutine help

	  subroutine help dates
	"""

	if not topic.strip():
		_say("Concepts this tool can explain:")
		_say("")

		width = max(len(item.name) for item in subroutine.cli.topics.TOPICS)

		for item in subroutine.cli.topics.TOPICS:
			_say(f"  {item.name.ljust(width)}  {item.summary}")

		_say("")
		_say("  subroutine help dates")
		_say("  subroutine --help          for the list of commands")

		return

	found = subroutine.cli.topics.find(topic)

	if found is None:
		_stop(
			f"There is nothing to say about {topic!r}.",
			f"Topics: {', '.join(subroutine.cli.topics.names())}.",
		)

	_say(found.body)


@config_app.command("show")
def config_show () -> None:
	"""Print the resolved settings and where each value came from."""

	settings = _settings()
	sources = subroutine.config.setting_sources(settings)
	fields = sorted(type(settings).model_fields)
	width = max(len(name) for name in fields)

	for name in fields:
		value = getattr(settings, name)

		# Two settings are never printed as they stand. The signing key is not shown at
		# all — whether it is set is the useful part. The database URL carries a password
		# on any networked backend, and this output is what people paste into bug reports.
		if name == "secret_key":
			value = "(set)" if value else "(not set)"

		elif name == "database_url":
			value = safe_url(str(value))

		_say(f"{name.ljust(width)}  {value}  [{sources[name]}]")


@database_app.command("upgrade")
def database_upgrade () -> None:
	"""Bring the database up to the newest schema."""

	settings = _settings()

	_refuse_unusable_storage(settings)

	try:
		subroutine.db.migrate.upgrade(settings.database_url)

	except sqlalchemy.exc.SQLAlchemyError as error:
		_stop(
			f"Could not upgrade the database at {safe_url(settings.database_url)}: "
			f"{getattr(error, 'orig', None) or error}",
			"Check that the server is running, and check 'database_url' in "
			"'subroutine config show'.",
		)

	_say(f"Schema is at {subroutine.db.migrate.head_revision()}.")


@database_app.command("current")
def database_current () -> None:
	"""Report which migration the database is at."""

	settings = _settings()

	if _database_is_absent(settings):
		_say("There is no database here yet. Run 'subroutine init'.")

		return

	with _database(settings) as engine:
		current = subroutine.db.migrate.current_revision(engine)
		head = subroutine.db.migrate.head_revision()

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

	``None`` is the ordinary case. Local mode opens the database directly, so a password is
	only wanted once there is a server to log in to — asking for one during setup would be
	ceremony in service of nothing.

	An empty pipe under ``--password-stdin`` is refused rather than treated as "no
	password". Passing the flag states that a password is coming, so nothing arriving means
	a broken pipeline — most likely a secret that failed to mount in a container. Creating
	a passwordless account and exiting 0 would be the worst of the available outcomes,
	because it succeeds and nobody investigates a zero exit code.
	"""

	if not from_stdin:
		return None

	password = sys.stdin.readline().rstrip("\n")

	if not password:
		_stop(
			"--password-stdin was given but nothing arrived on standard input.",
			"Pipe the password in, or leave the flag off to create an account with no "
			"password (local use needs none).",
		)

	return password


def _default_instance_name () -> str:
	"""Return a name for this installation, from the machine it is on."""

	try:
		return pathlib.Path("/etc/hostname").read_text(encoding="utf-8").strip() or "Subroutine"

	except OSError:
		return "Subroutine"


_show_today = subroutine.cli.personal.register(
	app, say=_say, fail=_fail, stop=_stop, settings=_settings, console=_out
)


@app.callback()
def _default (context: typer.Context) -> None:
	"""Project management for people and agents, in equal measure.

	Run with no arguments, this shows today's agenda.

	Examples:

	  subroutine add "Call the dentist before Sunday"

	  subroutine today

	  subroutine done 1
	"""

	if context.invoked_subcommand is not None:
		return

	# The bare invocation (SPEC.md §12.2a). `today` answers the question somebody opening
	# this tool is actually asking; a help wall answers one nobody asked.
	_show_today()


def main () -> None:
	"""Entry point for the ``subroutine`` command."""

	app()
