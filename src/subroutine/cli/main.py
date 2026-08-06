"""The ``subroutine`` command.

``init`` is the only thing a new user runs before the one they actually wanted, so it
prints **one line** (SPEC.md §12.1). The workspace, the Inbox, the role assignment and the
instance identity are all created and none of them are announced: someone setting up a
to-do list has not asked about workspaces. ``--verbose`` prints the full transcript for
whoever does want it.
"""

import contextlib
import datetime
import getpass
import pathlib
import re
import shutil
import sys
import tomllib
import traceback
import typing

import pydantic
import rich.console
import rich.text
import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.exc
import sqlalchemy.orm
import typer

import subroutine
import subroutine.auth
import subroutine.cli.personal
import subroutine.cli.topics
import subroutine.clients.base
import subroutine.clients.opening
import subroutine.config
import subroutine.connections
import subroutine.context
import subroutine.credentials
import subroutine.db.backup
import subroutine.db.migrate
import subroutine.db.models.identity
import subroutine.db.session
import subroutine.db.transfer
import subroutine.db.types
import subroutine.diagnosis
import subroutine.directory
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.local
import subroutine.domain.profiles
import subroutine.domain.schedule
import subroutine.domain.tokens
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.installations
import subroutine.releases
import subroutine.views

app = typer.Typer(
	name="subroutine",
	# **No `help=` here, deliberately.** An explicit help string overrides the callback's
	# docstring, and Typer then shows only its first line — which silently dropped the
	# worked examples from the one page a new user reads first, while every subcommand had
	# them (SPEC.md §12.2a). The docstring on `_default` is the help text.
	#
	# **Not** `no_args_is_help` either: the first thing this tool does unprompted should be
	# useful, so a bare `subroutine` prints today's agenda rather than a help wall.
	# `--help` is still one keystroke away for anyone who wants the wall.
	invoke_without_command=True,
	add_completion=False,
	# **No boxed traceback** (`#258`). Typer renders an unhandled exception as source with a
	# caret, which is a developer's view of a developer's problem — and it prints from inside
	# `app()`, so `main`'s handler below would otherwise report the same defect a second time
	# in a different voice. Off here, reported there, once.
	pretty_exceptions_enable=False,
	# **`subroutine help` prints this page too** (`#154`). The two used to differ, which made
	# one question have two answers; now the epilog offers `explain` as a *second* thing to
	# reach for rather than as a correction to what the reader just typed.
	epilog=(
		"Try 'subroutine explain dates' for the ideas behind the commands — how a date is "
		"written, what a number means, what the capture shorthand does."
	),
)

config_app = typer.Typer(help="Inspect and manage configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")

database_app = typer.Typer(help="Look after the database.", no_args_is_help=True)
app.add_typer(database_app, name="db")

token_app = typer.Typer(help="Issue credentials for agents and other machines.", no_args_is_help=True)
app.add_typer(token_app, name="token")

agent_app = typer.Typer(
	help="Set an AI agent up as a principal of its own.", no_args_is_help=True
)
app.add_typer(agent_app, name="agent")

# **No `create` here, deliberately.** Every writer makes its own parent directories, so
# `subroutine --profile scratch init` already brings a new instance into being (SPEC.md §12.5).
# A `profile create` would be a second way to do the same thing, and the two would drift.
profile_app = typer.Typer(
	help="Keep separate installations on one machine.", no_args_is_help=True
)
app.add_typer(profile_app, name="profile")

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

	The same detail, the same hint **and the same per-field hints** the HTTP layer returns, so
	a problem looks the same whichever way you meet it.

	The last of those was missing until `#79`. A `FieldError` carries a hint naming the valid
	alternatives, and this printed only its message — so `subroutine list --order banana` said
	the field was unknown and never said which ones are not, which is the half a reader can
	act on. It survived because most field hints repeat their message, and the two that differ
	are exactly the two worth having.

	Nothing is printed twice. A field's message is skipped when it merely restates the detail,
	and a field's hint when it restates the overall hint or its own message — a refusal that
	says one thing three ways is read as noise, and then so is the next one.
	"""

	_printed(error)

	raise typer.Exit(code=1)


#: Where a crash report is written, under the state directory. XDG's description of
#: ``STATE_HOME`` fits it exactly — useful to keep, safe to lose — and it is per-profile with
#: everything else (§12.5), so a disposable instance's crashes do not accumulate in the real
#: one's directory.
CRASH_DIRECTORY = "crashes"

#: A token, in whatever argument it turned up in. Tokens are not supposed to be passed on a
#: command line (§7.4) and this does not make it acceptable — it makes the *report* safe when
#: somebody has done it anyway.
_TOKEN_ARGUMENT = re.compile(rf"^{subroutine.auth.TOKEN_SCHEME}_[A-Za-z0-9_.\-]+$")


def _masked_arguments () -> list[str]:
	"""Return the command line with anything secret in it replaced.

	**A crash report is a file people are asked to send**, which makes it the same hazard
	``safe_url`` exists for one surface along — and `#189` is this project's recorded instance
	of that going wrong, where verifying a document's output put real tokens into it. Two
	things reach here: a database URL carrying a password, which ``db copy --to`` takes
	routinely, and a token somebody passed as an argument.
	"""

	masked = []

	for argument in sys.argv:
		if _TOKEN_ARGUMENT.match(argument):
			masked.append(f"{subroutine.auth.TOKEN_SCHEME}_***")

		elif "://" in argument:
			masked.append(safe_url(argument))

		else:
			masked.append(argument)

	return masked


def _crash_report (exception: BaseException) -> pathlib.Path | None:
	"""Write the traceback where it can be found later, or return ``None`` if it cannot be.

	**Nothing in here may raise.** It runs because something has already gone wrong, and a
	crash handler that crashes replaces a bad message with a worse one — an unwritable state
	directory is `#255`'s exact condition, and the moment somebody most needs the sentence
	this makes possible. A report that cannot be written is a missing convenience; an
	exception thrown from the handler is a second bug on top of the first.
	"""

	try:
		directory = subroutine.config.state_home() / CRASH_DIRECTORY
		directory.mkdir(parents=True, exist_ok=True)

		now = datetime.datetime.now()
		path = directory / f"crash-{now.strftime('%Y%m%d-%H%M%S-%f')}.txt"

		path.write_text(
			f"subroutine {subroutine.__version__}\n"
			f"{now.isoformat(timespec='seconds')}\n"
			f"{' '.join(_masked_arguments())}\n\n"
			+ "".join(traceback.format_exception(exception)),
			encoding="utf-8",
		)

		return path

	# Broad on purpose, and the docstring above is the reason. Anything at all raised while
	# reporting a crash must end as "no file", never as a traceback about the reporter.
	except Exception:
		return None


def _report_a_defect (exception: BaseException) -> None:
	"""Say that something broke, in a sentence, and where the details went.

	§13.5's voice rule applied to the case it is hardest to apply to: the reader is somebody
	setting up a to-do list, the program has no idea what went wrong, and forty lines of
	``pathlib`` recursion answers a question they did not ask. What they can act on is that it
	is not their fault, that the details are kept, and where to send them.
	"""

	path = _crash_report(exception)

	_err.print("Something went wrong that should not have.", markup=False, highlight=False)

	if path is None:
		# No file, so the trace is the only copy there will ever be — and withholding it now
		# would leave nothing to report at all. This is the one path that prints a stack.
		_err.print(
			"The details could not be written down, so they are here instead:",
			markup=False,
			highlight=False,
		)
		_err.print(
			"".join(traceback.format_exception(exception)), markup=False, highlight=False
		)

	else:
		_err.print(f"The details are in {path}", markup=False, highlight=False)

	_err.print(
		f"Please report it at {subroutine.ISSUES_URL}, with that file attached.",
		markup=False,
		highlight=False,
	)


def _printed (error: subroutine.errors.SubroutineError) -> None:
	"""Write a refusal to standard error, without deciding how the process ends.

	Split out from `_fail` so that `main` can report a refusal that escaped a command
	entirely: `_fail` raises `typer.Exit`, which means nothing once click's own runner has
	been left behind, and reusing it there turned a missing backup directory into a traceback
	about `typer.Exit`.
	"""

	_err.print(error.detail, markup=False, highlight=False)

	if error.hint is not None:
		_err.print(error.hint, markup=False, highlight=False)

	for field in error.errors:
		# A single field error whose message is already the detail — or already the hint —
		# says nothing new. `subroutine add "#tag !3"` said "A title is required." and then
		# "  title: A title is required."; a bad date printed a 200-character remedy and then
		# repeated it verbatim under `when:`, adding one word for the second copy.
		#
		# **Only when it is the only one.** With several fields, naming each is the whole
		# value of the list, however much any one of them repeats.
		said = len(error.errors) == 1 and field.message in (error.detail, error.hint)

		if not said:
			_err.print(f"  {field.field}: {field.message}", markup=False, highlight=False)

		if field.hint is None or field.hint in (error.hint, field.message):
			continue

		# Indented under the field it belongs to when that was printed, so a refusal naming
		# several fields does not run their remedies together.
		_err.print(f"{'  ' if said else '    '}{field.hint}", markup=False, highlight=False)


def _stop (message: str, hint: str | None = None) -> typing.NoReturn:
	"""Report a failure that has no error code yet, and stop."""

	_err.print(message, markup=False, highlight=False)

	if hint is not None:
		_err.print(hint, markup=False, highlight=False)

	raise typer.Exit(code=1)


def _warn (message: str) -> None:
	"""Report something that went wrong without stopping.

	To standard error, so that ``--json`` and a pipe stay clean, and the command still exits
	0. A connection being unreachable is the case this exists for (SPEC.md §13.7): an agenda
	that refuses to print because one of three servers is down is worse than an agenda with a
	line saying which one.
	"""

	_err.print(message, markup=False, highlight=False)


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


#: Whether this process has already named the settings nobody reads. Module state because the
#: warning belongs to the run, not to any one call — see `_settings`.
_said_unknown_settings = False


def _settings () -> subroutine.config.Settings:
	"""Resolve configuration, explaining a bad value rather than raising through it.

	Every command starts here, so every command inherits the explanation. An unreadable
	config file or a mistyped environment variable is an ordinary mistake, and it should
	read like one.

	**A setting nobody reads is said out loud, on every command** (`#175`). It goes to
	standard error, so a pipe stays clean and the exit code is untouched — this is a warning
	about the configuration, not a refusal of the work. Repeating it every time is the point:
	a typo that silently turns off the confirmation on destructive commands should be
	annoying until somebody fixes it.
	"""

	# Once per process, not once per call: `_settings()` is reached more than once by some
	# commands, and a warning printed twice reads as two problems.
	global _said_unknown_settings

	if not _said_unknown_settings:
		_said_unknown_settings = True

		for line in subroutine.config.describe_unknown_settings():
			_warn(line)

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


# Registered before `init` and `help` so that they head the command list. SPEC.md §12.2
# puts `add`, `today`, `ls`, `done`, `plan` first and says the ordering is deliberate: they
# are the whole surface a personal user needs, and Typer lists commands in registration
# order.
_show_today, _selected = subroutine.cli.personal.register(
	app,
	say=_say,
	fail=_fail,
	stop=_stop,
	settings=_settings,
	console=_out,
	warn=_warn,
	mask=safe_url,
)


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

	_warn_an_earlier_instance_is_elsewhere(settings)

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
	_warn_an_environment_database_is_not_recorded(settings)


#: Host names that mean "this machine only" without being addresses. ``ipaddress`` cannot
#: parse a name, and refusing to serve on ``localhost`` because it is not spelled ``127.0.0.1``
#: would be a check failing on the one case it exists to allow.
@app.command()
def mcp (
	connection: str = typer.Option(
		"", "--connection", help="Which instance to work in. Defaults to the current one."
	),
	workspace: str = typer.Option(
		"", "--workspace", help="Which workspace its calls land in. Unset means say each time."
	),
) -> None:
	"""Serve this instance to an AI agent over MCP, on stdin and stdout.

	Examples:

	  subroutine mcp

	  subroutine mcp --connection work

	  subroutine mcp --connection work --workspace acme

	Speaks the Model Context Protocol over stdio, so a client starts this as a child process
	rather than connecting to a port. There is nothing to expose and no listener: if the
	client is not running it, nothing is serving.

	One connection, chosen here. Unlike 'today', which merges every configured instance
	because a person has one day, a tool call writes somewhere — and where it writes has to
	be a decision you can see rather than one this process takes for you.

	'--workspace' is the same argument one level down, and it matters as soon as an instance
	holds two: without it every read is refused as ambiguous, and the agent has no way to
	learn a name it was never told. It is a default rather than a limit — a call naming a
	workspace still goes there, and a token pinned to one narrows it for real.
	"""

	# Imported inside the function like `serve`'s uvicorn: an MCP session is a long-lived
	# child process where an extra import costs nothing, and every other `subroutine`
	# invocation is a command line where it would.
	import subroutine.mcp.relay

	subroutine.mcp.relay.run(
		sys.stdin,
		sys.stdout,
		connection=connection or None,
		workspace=workspace or None,
		settings=_settings(),
	)


@app.command()
def serve (
	host: str = typer.Option("", "--host", help="What to listen on. Defaults to 127.0.0.1."),
	# `0` means "whatever is configured", and printing it as the default said the program
	# listens on port zero (`#170`). `--host` has always described its default in words for
	# the same reason; this is the same treatment on the option beside it.
	port: int = typer.Option(
		0,
		"--port",
		show_default=False,
		help="Which port to listen on. Defaults to the one in 'subroutine config show'.",
	),
	insecure: bool = typer.Option(
		False,
		"--insecure",
		help="Listen beyond this machine without TLS. Say this out loud, or set public_url.",
	),
	log_level: str = typer.Option("", "--log-level", help="How much to log."),
) -> None:
	"""Serve the HTTP API.

	Examples:

	  subroutine serve

	  subroutine serve --host 0.0.0.0 --insecure

	There is no setting that turns the API on or off, deliberately: if this process is not
	running there is no socket, and a configuration key that made 'serve' refuse to start
	would be a confusing way of saying "do not run it". The control that actually controls
	anything is the bind address, and its default is loopback.
	"""

	settings = _settings()
	# Brackets are how a *URL* writes an IPv6 address, not how a socket takes one. `is_loopback`
	# strips them so `[::1]` is recognised; handing them on unstripped made uvicorn die in
	# `getaddrinfo` with `[Errno -2]` after the safety check had already approved the bind.
	where = (host.strip() or settings.host).strip("[]")
	listening = port or settings.port

	if not 1 <= listening <= 65535:
		_stop(
			f"{listening} is not a port.",
			"A port is between 1 and 65535. Leave --port off to use "
			f"{settings.port}.",
		)

	_refuse_unusable_storage(settings)
	_refuse_public_bind(settings, where, insecure=insecure)

	if _database_is_absent(settings):
		_refuse_absent_database(settings, doing=", so there would be nothing to serve")

	# **Imported here rather than at the top of the module**, and measured rather than
	# assumed: FastAPI and uvicorn together cost 0.3 seconds of this program's 0.8-second
	# start, on every `subroutine add` — for one command most people never run. The `as`
	# aliases are the house style's documented exception for a nested import, because a plain
	# `import subroutine.api.app` here would bind `subroutine` as a *local* name and shadow
	# every other use of it in this function.
	from uvicorn import run as listen

	from subroutine.api import app as api

	shown = f"[{where}]" if ":" in where else where

	_say(f"Serving on http://{shown}:{listening} — the agent guide is at /v1/docs/agent.")

	listen(
		api.create_app(settings=settings),
		host=where,
		port=listening,
		log_level=(log_level.strip() or settings.log_level).lower(),
	)


def _refuse_public_bind (
	settings: subroutine.config.Settings, host: str, *, insecure: bool
) -> None:
	"""Refuse a non-loopback bind unless somebody has said out loud that it is intended.

	SPEC.md §12.4. Binding beyond this machine is the moment bearer tokens start crossing a
	network, and the previous posture — a note about TLS in the documentation — put the
	warning where it would not be read. One-time friction, imposed at exactly the moment the
	risk appears.

	The check passes when ``public_url`` is an ``https://`` address, which is the correct
	production setup and says a TLS-terminating proxy is in front; or when ``--insecure`` is
	passed, which is the honest way to say "this is a home LAN and I know". A *warning* was
	rejected rather than overlooked: a warning on a long-running server scrolls away in the
	first minute.
	"""

	if insecure or subroutine.config.is_loopback(host):
		return

	public = (settings.public_url or "").strip()

	if public.lower().startswith("https://"):
		return

	if public:
		_stop(
			f"Refusing to listen on {host} without TLS: public_url is set to {public!r}, "
			"which is not an https:// address.",
			"Point public_url at the https:// address your proxy serves this on, or pass "
			"--insecure if this network is genuinely trusted.",
		)

	_stop(
		f"Refusing to listen on {host} without TLS: bearer tokens sent over plain HTTP are "
		"compromised tokens.",
		"Either put a TLS-terminating proxy in front and set public_url to its https:// "
		"address, or pass --insecure if this network is genuinely trusted.",
	)


@app.command("upgrade", hidden=True)
def upgrade_moved () -> None:
	"""Say where this went. It is not an alias and does not upgrade anything.

	'subroutine db upgrade' is the command now.
	"""

	# **Not the alias Simon turned down** (`#509`), and the distinction is the whole reason this
	# exists: an alias would *do the upgrade* under a name whose sibling `db upgrade` used to
	# mean the blunt migrator, so one spelling would mean two things depending on when somebody
	# learned it. This refuses, which is what a removed command should do.
	#
	# **Without it the removal misdirects.** Typer offers the nearest command it can find, and
	# for `upgrade` that is `update` — so an operator migrating a database was pointed at the
	# one that edits a task. A bare removal was worse than either option on the table.
	_say("'subroutine upgrade' is now 'subroutine db upgrade'.")
	_say("")
	_say("Everything that acts on the database is under 'db'. The blunt migrator that used")
	_say("to be 'db upgrade' — no backup, no confirmation — is now 'db migrate'.")

	raise typer.Exit(2)


@app.command("help")
def help_command (context: typer.Context) -> None:
	"""Show what this can do — the same as 'subroutine --help'.

	The same answer, deliberately. This used to explain concepts while '--help' listed
	commands, so one question had two answers and the reader had to learn which was which
	before learning either. 'help' is what everybody types first, so it answers the
	commonest question; the concepts moved to 'explain', whose name says what it is for in
	a way 'help <topic>' never did.
	"""

	parent = context.parent

	_say(context.get_help() if parent is None else parent.get_help())


@app.command("explain")
def explain_topic (
	topic: str = typer.Argument("", help="A concept to explain. Omit to list them."),
) -> None:
	"""Explain a concept — refs, dates, the capture grammar, scripting.

	Examples:

	  subroutine explain

	  subroutine explain dates
	"""

	if not topic.strip():
		_say("Concepts this tool can explain:")
		_say("")

		width = max(len(item.name) for item in subroutine.cli.topics.TOPICS)

		for item in subroutine.cli.topics.TOPICS:
			_say(f"  {item.name.ljust(width)}  {item.summary}")

		_say("")
		_say("  subroutine explain dates")
		_say("  subroutine help            for the list of commands")

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

		# **A setting whose default is a *place* names the place** (`#175`). `backup_directory`
		# printed `None [default]`, which says where backups go only to somebody who already
		# knows — and the thing it does not say is the one that matters: unset means beside the
		# database, which is the arrangement this project's own hosting page opens by calling
		# "not a backup". `subroutine db upgrade` takes one automatically, so an operator who never
		# set the value has their pre-upgrade copy on the disk they are worried about.
		elif name == "backup_directory" and not value:
			value = f"(unset — beside the database, in {subroutine.db.backup.directory(settings)})"

		_say(f"{name.ljust(width)}  {value}  [{sources[name]}]")


@database_app.command("migrate")
def database_migrate () -> None:
	"""Migrate the database, with none of the ceremony.

	No backup, no confirmation and no version report. That is deliberate: this has to work
	when everything else refuses, which is what makes it the thing to reach for during a
	recovery and the wrong thing to reach for otherwise.

	Use 'subroutine db upgrade' unless you know why you are here. It runs this, after
	reporting both versions and taking a backup it has verified.
	"""

	# **Renamed by `#509`. This was `db upgrade`, and the safe procedure was a top-level
	# `upgrade`** — two commands one word apart, wildly different safety, and nothing in either
	# name saying which was which. The dangerous one sat in the namespace where a careful reader
	# would look for the safe one; the safe one sat where `upgrade` reads as *upgrade the
	# software*, which §12.4a says it must never do, so its help had to spend a paragraph
	# denying it.
	#
	# **The old spelling is gone rather than kept as an alias** (Simon, 2026-08-05): `db
	# upgrade` now means very nearly the opposite of what it meant, so an alias would answer to
	# a name whose meaning had changed underneath it. A removed command refuses; a repurposed
	# one does the other thing and says nothing.

	settings = _settings()

	_refuse_unusable_storage(settings)

	# **A database that is not there is not one to migrate** (`#394`). Alembic will happily
	# create it, run every migration into it and leave an empty instance behind — and this then
	# reported a schema head as though all was well. The empty file sits at the default path
	# and shadows the real instance for every later command run without the right
	# configuration; on this machine it absorbed four backups, each reporting a plausible size
	# and a correct schema.
	#
	# **§12.4's blunt-tool licence does not stretch to this.** It is deliberately without
	# confirmation, a backup or a version report, so that recovery works when everything else
	# refuses — that is about skipping ceremony. There is nothing to recover from a database
	# that does not exist.
	#
	# The three commands beside it already refuse this way, which is what made the silence
	# here read as house style rather than as a gap.
	if _database_is_absent(settings):
		_refuse_absent_database(settings)

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


@database_app.command("copy")
def database_copy (
	to: str = typer.Option(..., "--to", help="The database URL to copy into."),
) -> None:
	"""Copy this instance's data into another database — SQLite to PostgreSQL, or back.

	Examples:

	  subroutine db copy --to postgresql+psycopg:///subroutine

	A copy, and the original is untouched. Nothing here writes to or deletes the current
	database: when the new one looks right, point 'database_url' at it. Until then you have
	two, which is the reassurance somebody changing engines on a Tuesday evening actually
	wants.

	The target must be empty. Merging two instances is not this command, and doing it by
	accident would leave neither of them right.
	"""

	settings = _settings()

	if _database_is_absent(settings):
		_refuse_absent_database(settings)

	unusable = subroutine.db.transfer.unusable_target(to)

	if unusable is not None:
		_stop(
			unusable,
			"A URL looks like 'postgresql+psycopg:///subroutine' or "
			"'sqlite:////var/lib/subroutine/subroutine.db'.",
		)

	_say(f"Copying {safe_url(settings.database_url)}")
	_say(f"     to {safe_url(to)}")

	try:
		copied = subroutine.db.transfer.copy_into(settings.database_url, to)

	except subroutine.errors.SubroutineError as error:
		_fail(error)

	except sqlalchemy.exc.SQLAlchemyError as error:
		_stop(
			f"Could not copy into {safe_url(to)}: {getattr(error, 'orig', None) or error}",
			"The database this instance uses is untouched. Nothing has been lost.",
		)

	_say("")

	for line in subroutine.db.transfer.summarise(copied):
		_say(line)

	_say("")
	_say(f"Copied {copied.rows:,} rows, and read them back to check.")
	_say("")
	_say("Nothing has changed here yet. To start using the copy, set in config.toml:")
	_say(f'  database_url = "{to}"')


@database_app.command("current")
def database_current () -> None:
	"""Report which migration the database is at."""

	settings = _settings()

	if _database_is_absent(settings):
		_say(f"There is no database at {safe_url(settings.database_url)}.")
		_say(_where_a_database_would_come_from(settings))

		return

	with _database(settings) as engine:
		current = subroutine.db.migrate.current_revision(engine)
		head = subroutine.db.migrate.head_revision()

	if current is None:
		_say("This database has no schema yet. Run 'subroutine init'.")

		return

	_say(f"Schema is at {current}." if current == head else f"Schema is at {current}; newest is {head}.")


def _schema_now (settings: subroutine.config.Settings) -> str:
	"""Report which migration the database is at, for a message written after a failure.

	Never raises. It is called on the way out of a failed upgrade, and a second failure while
	trying to describe the first one would replace the sentence somebody needs with a traceback
	about our own reporting.
	"""

	try:
		with _database(settings) as engine:
			return subroutine.db.migrate.current_revision(engine) or "no schema at all"

	except Exception:
		return "a revision that could not be read"


@app.command("doctor")
def doctor () -> None:
	"""Say whether this machine's installation is coherent, and change nothing.

	Examples:

	  subroutine doctor

	What is running and where it came from, which configuration it is reading, what each
	connection answers, and when a backup was last taken. One command because getting it wrong
	is nearly always the same mistake: running something without the environment the service
	uses, which acts on a different database and looks exactly like success.

	It exits non-zero if anything needs attention, so it can be the last line of an update
	script rather than something a person reads and forgets.

	It talks to the instances you have configured and to nothing else. Whether a newer release
	exists is a different question, asked with 'subroutine db upgrade --check'.
	"""

	findings = subroutine.diagnosis.examine(_settings())
	width = max(len(finding.area) for finding in findings)

	for finding in findings:
		line = f"  {finding.area.ljust(width)}  {finding.detail}"

		# **Colour marks the exception and never carries the meaning** (decision `#102`). A
		# failing line says so in a word as well, because a reader piping this into a log, or
		# one who cannot distinguish red, has to get the same answer.
		if finding.ok:
			_say(line)

		else:
			_out.print(
				rich.text.Text(f"{line}  — needs attention", style="red"),
				markup=False,
				highlight=False,
			)

	_say("")
	_say(f"  {subroutine.diagnosis.verdict(findings)}")

	if any(not finding.ok for finding in findings):
		raise typer.Exit(1)


@database_app.command("upgrade")
def upgrade (
	yes: bool = typer.Option(False, "--yes", help="Do not ask, even if protected."),
	check: bool = typer.Option(
		False, "--check", help="Ask whether a newer release exists, and change nothing."
	),
) -> None:
	"""Bring the database up to the schema this version needs, backing it up first.

	Examples:

	  subroutine db upgrade

	  subroutine db upgrade --check

	This does not install anything, and will not try to. Update Subroutine itself with
	whatever you installed it with — pip, pipx, uv, your package manager, a new container —
	and then run this to bring the database along.

	'--check' asks whether a newer release exists and whether it changes the database schema,
	which is the part worth knowing in advance: it is the difference between planning a short
	outage and meeting one halfway through an install. It touches nothing.

	Nothing else here ever reaches the network. Subroutine does not check for updates on its
	own, and there is no setting that makes it — asking is a thing you do, not a thing it does.
	"""

	if check:
		_report_the_newest_release()

		return

	settings = _settings()

	if _database_is_absent(settings):
		_refuse_absent_database(settings)

	_refuse_unusable_storage(settings)

	with _database(settings) as engine:
		current = subroutine.db.migrate.current_revision(engine)

	expected = subroutine.db.migrate.head_revision()

	# **Both numbers, before anything happens and whatever the answer turns out to be.** They
	# are step 1 of decision `#97`, and they are what the whole conversation is about: one
	# says what the software wants and the other what it has got.
	_say(f"This version expects schema {expected}.")
	_say(
		f"The database is at {current}."
		if current is not None
		else "The database has no schema recorded."
	)

	if current == expected:
		_say("Nothing to do.")

		return

	if current is None:
		_stop(
			"There is no Subroutine schema in that database.",
			"Run 'subroutine init' to set it up.",
		)

	if not subroutine.db.migrate.knows_revision(current):
		# **Ahead, not behind, and there is no way back.** Migrating cannot produce a revision
		# this build has never seen, so "run the upgrade" would be a confident instruction to
		# do nothing. `db restore` refuses a newer backup for the same reason (§12.6).
		_stop(
			f"That database is newer than this software: it is at {current}, which this "
			f"version has never heard of.",
			"Update Subroutine rather than the database — there is no downgrade. Whatever "
			"installed it is what upgrades it.",
		)

	_confirm_destructive(settings, "About to upgrade the database of", yes=yes)

	# **Back up before touching anything, always.** `take` verifies the copy where it landed
	# rather than where it was aimed (§12.6b) — size, and the schema head read back out — so
	# "backed up" here means a file somebody could actually restore, not a write that returned.
	with _database(settings) as engine:
		try:
			written = subroutine.db.backup.take(engine, settings)

		except subroutine.errors.SubroutineError as error:
			_fail(error)

	_say(f"Backed up to {written.path} ({written.size_bytes:,} bytes).")

	try:
		subroutine.db.migrate.upgrade(settings.database_url)

	except sqlalchemy.exc.SQLAlchemyError as error:
		# **Say where it stopped, never "the database is unchanged".** Alembic runs each
		# migration in its own transaction, so a failure part-way along a chain of three leaves
		# the first two applied and committed — and an upgrade spanning several releases is
		# exactly when this fires. Read the revision back and report it; the one thing that is
		# certainly true is where the backup is.
		_stop(
			f"The upgrade failed: {getattr(error, 'orig', None) or error}",
			f"It stopped at {_schema_now(settings)}. What was there before the upgrade is at "
			f"{written.path} — 'subroutine db restore {written.path} --recover' puts it back.",
		)

	# **Read it back rather than assuming.** A migration that reports success and leaves the
	# schema somewhere unexpected is exactly the state somebody needs to be told about, and
	# `alembic upgrade head` is not the only thing that could have written here.
	with _database(settings) as engine:
		landed = subroutine.db.migrate.current_revision(engine)

	if landed != expected:
		_stop(
			f"The upgrade ran but the database is at {landed}, not {expected}.",
			f"'subroutine db restore {written.path} --recover' puts back what was there "
			f"before.",
		)

	_say(f"Upgraded from {current} to {landed}.")


def _instance_label () -> str:
	"""Name the instance a command is about to act on, for output that must be unambiguous.

	Every destructive command says this before doing anything (SPEC.md §12.5). The isolation
	between instances is invisible, which is what makes it safe to use and what makes it
	dangerous to trust silently.
	"""

	active = subroutine.config.profile()

	return "the default instance" if active is None else f"instance '{active}'"


def _confirm_destructive (
	settings: subroutine.config.Settings, action: str, *, yes: bool
) -> None:
	"""Name the target, and require agreement before damaging a protected instance.

	Protection is a property of the instance rather than of the command, because the thing
	worth protecting is a particular database and a flag on the command only protects whoever
	remembers to type it.
	"""

	_say(f"{action} {_instance_label()}, at {safe_url(settings.database_url)}.")

	if not settings.protected or yes:
		return

	if not sys.stdin.isatty():
		_stop(
			f"{_instance_label().capitalize()} is marked protected and this is not an "
			f"interactive terminal.",
			"Pass --yes if you are certain, or unset 'protected' in its config.toml.",
		)

	if not typer.confirm("This instance is marked protected. Go on?"):
		_stop("Nothing was changed.")


@database_app.command("backup")
def database_backup (
	# `0` means "prune nothing", and printing it as the default put `[default: 0]` directly
	# beneath "delete all but this many of the newest backups" — which reads as *delete every
	# backup you have*, on the one command somebody runs because they are worried about losing
	# data (`#170`). The guard found this; the clean-room review did not.
	keep: int = typer.Option(
		0,
		"--keep",
		show_default=False,
		help=(
			"Afterwards, delete all but this many of the newest backups. "
			"Nothing is deleted unless you ask."
		),
	),
) -> None:
	"""Take a datetime-stamped copy of the database.

	The copy records the schema it was taken on, so a restore can tell whether this version
	is able to read it.
	"""

	settings = _settings()

	if _database_is_absent(settings):
		_refuse_absent_database(settings)

	with _database(settings) as engine:
		try:
			written = subroutine.db.backup.take(
				engine, settings, keep=keep if keep > 0 else None
			)

		except subroutine.errors.SubroutineError as error:
			_fail(error)

	_say(f"Backed up {_instance_label()} to {written.path}")
	_say(f"{written.size_bytes:,} bytes, schema {written.schema_head}.")

	# **Says what it copied, because a hollow backup passed every check there was** (`#395`).
	# Size and schema were both correct for four backups of an empty instance — an empty
	# database is a valid one — and the sentence gave an operator nothing to be suspicious of.
	# A count is the one fact that distinguishes them.
	_say(_what_it_held(written))

	# Named, not counted. This is recommended for a timer, and the timer's log is the only
	# record there will ever be of which backups stopped existing.
	for gone in written.removed:
		_say(f"Deleted {gone.path} to keep {keep}.")


def _what_it_held (written: subroutine.db.backup.Backup) -> str:
	"""Say how much work the backup contains, in the terms somebody would recognise it by.

	**An empty instance is stated rather than left to be inferred from four zeroes.** Somebody
	reading a backup line is looking for a reason to stop worrying, so the one case worth
	spending a whole sentence on is the case where they should not.
	"""

	held = written.holdings

	if not held:
		return "  Could not count what it holds; check the copy before relying on it."

	if not any(held.values()):
		return (
			"  It holds nothing — no workspaces, no projects, no tasks and no documents. "
			"That is a backup of an empty instance, not of your work."
		)

	return "  " + ", ".join(
		f"{count:,} {name}{'' if count == 1 else 's'}"
		for name, count in held.items()
		if count
	) + "."


def _holdings_cell (backup: subroutine.db.backup.Backup) -> str:
	"""Say what one backup in a listing holds, in one line under it — item `#432`.

	**The surface an operator actually reads on the day they need one**, and the one `#395` did
	not change. It made ``db backup`` say what it had copied; the listing beside it went on
	reporting a size and a schema head, both of which are *correct* for a backup of an empty
	instance — so four hollow copies sat at the top of the list, newest first, with nothing in
	the line to be suspicious of. Restoring "the latest backup" got an empty database.

	**Three states, and the third is why this is not a boolean.** Not recorded is not the same
	as empty: every backup taken before the counts were written beside them has no record, and
	saying those hold nothing would be the same false confidence pointing the other way.
	"""

	held = backup.holdings

	if held is None:
		return (
			"Holdings not recorded — taken before this was written down. Check it before "
			"relying on it."
		)

	if not any(held.values()):
		return "Holds nothing: an empty instance, not your work."

	return ", ".join(
		f"{count:,} {name}{'' if count == 1 else 's'}"
		for name, count in held.items()
		if count
	)


@database_app.command("backups")
def database_backups () -> None:
	"""List the backups this instance has, newest first."""

	settings = _settings()
	found = subroutine.db.backup.catalogue(settings)

	if not found:
		_say(f"No backups of {_instance_label()} yet. Run 'subroutine db backup'.")

		return

	_say(f"Backups of {_instance_label()}, in {subroutine.db.backup.directory(settings)}:")

	# **The engine is shown only when the list holds more than one of them** — §12.2a's rule
	# that a column saying the same thing on every row says nothing. It earns its place here
	# exactly when it matters: a directory holding both kinds is where somebody picks the
	# wrong row, which was `#172`. When they are all the same, no wrong row exists to pick.
	engines = {subroutine.db.backup.engine_in(backup.path) for backup in found}

	for backup in found:
		when = backup.taken_at.strftime("%Y-%m-%d %H:%M UTC")
		kind = f"  {subroutine.db.backup.engine_in(backup.path)}" if len(engines) > 1 else ""

		_say(
			f"  {backup.name}  {when}  {backup.size_bytes:,} bytes  "
			f"schema {backup.schema_head}{kind}"
		)
		_say(f"    {_holdings_cell(backup)}")


@database_app.command("restore")
def database_restore (
	source: str = typer.Argument(..., help="The backup file to put back."),
	recover: bool = typer.Option(
		False, "--recover", help="This instance's own data, coming back. Keeps its identity."
	),
	as_clone: bool = typer.Option(
		False, "--as-clone", help="A copy standing up as a separate instance. New identity."
	),
	yes: bool = typer.Option(False, "--yes", help="Do not ask, even if protected."),
	safety_backup: bool = typer.Option(
		True,
		"--safety-backup/--no-safety-backup",
		help="Back up what is about to be replaced. Failing to is reported, never fatal.",
	),
	force: bool = typer.Option(
		False, "--force", help="Restore even though something else is using the database."
	),
) -> None:
	"""Put a backup back, replacing this instance's database.

	Restoring is two different operations and this will not guess which. --recover is your
	own data returning: the instance keeps its identity, because agents and configuration
	files already refer to it. --as-clone is a copy becoming a separate instance: it gets a
	new identity, because two live instances may not claim the same one.

	Stop the service first. Restoring underneath a running one does not reach it: it goes on
	writing to the file that was replaced, and its next checkpoint can corrupt the restored
	one. This refuses when it can see another connection, and --force overrides that.
	"""

	if recover == as_clone:
		_stop(
			"Say which kind of restore this is: --recover for this instance's own data "
			"coming back, or --as-clone for a copy becoming a separate instance.",
			"They differ in whether the instance keeps its identity, and guessing wrong is "
			"not visible until an agent's cached knowledge disagrees with reality.",
		)

	settings = _settings()
	path = pathlib.Path(source).expanduser()

	if not path.is_file():
		candidate = subroutine.db.backup.directory(settings) / source

		if not candidate.is_file():
			_stop(
				f"No backup at {path}.",
				"Run 'subroutine db backups' to see what this instance has.",
			)

		path = candidate

	# Before anything is destroyed, and before the operator is asked to agree to anything:
	# refuse a database something else is using (`#171`), a backup taken from the other engine
	# (`#172`), and one this version cannot read. All three are knowable while the current
	# database is still intact, and being asked to confirm a destructive act that is then
	# refused teaches an operator to stop reading the question.
	try:
		with _database(settings) as engine:
			if not force:
				subroutine.db.backup.check_unused(engine)

			subroutine.db.backup.check_engine(engine, path)

		head = subroutine.db.backup.check_restorable(path)

	except subroutine.errors.SubroutineError as error:
		_fail(error)

	_confirm_destructive(settings, "About to replace the database of", yes=yes)

	if safety_backup and not _database_is_absent(settings):
		_safety_copy(settings, yes=yes)

	with _database(settings) as engine:
		try:
			subroutine.db.backup.restore(engine, path, as_clone=as_clone, force=force)

		except subroutine.errors.SubroutineError as error:
			_fail(error)

	_say(f"Restored {path.name} into {_instance_label()}.")

	if as_clone:
		_say("This is now a separate instance: new identity, and the stored context cleared.")

	newest = subroutine.db.migrate.head_revision()

	if head != newest:
		_say(f"That backup is on schema {head}; this version is at {newest}.")

		if yes or typer.confirm("Upgrade the restored database now?", default=True):
			subroutine.db.migrate.upgrade(settings.database_url)
			_say(f"Schema is at {subroutine.db.migrate.head_revision()}.")


@profile_app.command("list")
def profile_list () -> None:
	"""Show every installation on this machine."""

	names = subroutine.config.profile_names()

	_say("default   (no --profile)")

	for name in names:
		_say(f"{name}")

	if not names:
		_say("")
		_say("Only the default. 'subroutine --profile <name> init' makes another.")


@profile_app.command("destroy")
def profile_destroy (
	name: str = typer.Argument(..., help="The instance to remove."),
	confirm: str = typer.Option(
		"", "--confirm", help="The name again, to show you mean this one."
	),
	yes: bool = typer.Option(False, "--yes", help="Do not ask, even if protected."),
) -> None:
	"""Delete a separate installation and everything in it.

	The default instance cannot be removed this way: it has no name to pass, which is the
	point. Disposable instances are meant to be thrown away, and the one holding real work
	should not be reachable by a command whose whole purpose is deletion.
	"""

	try:
		wanted = subroutine.config.check_profile_name(name)

	except ValueError as error:
		_stop(str(error))

	if wanted not in subroutine.config.profile_names():
		_stop(
			f"There is no instance called '{wanted}'.",
			"Run 'subroutine profile list' to see what exists.",
		)

	if confirm != wanted:
		_stop(
			f"Pass --confirm {wanted} as well, to show which instance you mean.",
			"Everything in it is deleted, including its backups.",
		)

	if _profile_is_protected(wanted) and not yes:
		if not sys.stdin.isatty():
			_stop(
				f"Instance '{wanted}' is marked protected and this is not an interactive "
				f"terminal.",
				"Pass --yes if you are certain.",
			)

		if not typer.confirm(f"Instance '{wanted}' is marked protected. Delete it anyway?"):
			_stop("Nothing was changed.")

	for directory in subroutine.config.profile_directories(wanted):
		if directory.is_dir():
			shutil.rmtree(directory)
			_say(f"Removed {directory}")

	_say(f"Instance '{wanted}' is gone.")


def _profile_is_protected (name: str) -> bool:
	"""Report whether another instance has marked itself protected.

	Reads that instance's own configuration by standing in it briefly, so the answer comes
	from the same resolution chain every other setting uses rather than a second reader that
	could disagree with it.
	"""

	was = subroutine.config.profile()

	try:
		subroutine.config.use_profile(name)

		return _settings().protected

	finally:
		subroutine.config.use_profile(was)


#: The role a new service account is given in the workspace it is made for. ``contributor``
#: reads everything and writes tasks and comments, and cannot restructure projects — which is
#: the right starting authority for an agent, and is narrowable further by the token's own
#: scopes (SPEC.md §7.3).


def _shaped_by_profile (
	profile: str,
	*,
	project: list[str] | None,
	write: list[str] | None,
	scope: list[str] | None,
	workspace: str,
) -> subroutine.domain.profiles.Shape:
	"""Turn what was typed into the arguments a credential is issued with — item ``#372``.

	**One helper for both commands**, because ``token create`` and ``agent create`` are two
	ways of taking the same decision and a profile that behaved differently between them would
	be the divergence this codebase keeps meeting. The tidying of whitespace happens here too,
	so the two cannot part company over whether ``--project ' SR'`` names a project.

	No profile means exactly what it meant before profiles existed: whatever was typed, and no
	narrowing that nobody asked for.
	"""

	projects = [item.strip() for item in (project or []) if item.strip()]
	writes = [item.strip() for item in (write or []) if item.strip()]
	scopes = [item.strip() for item in (scope or []) if item.strip()]
	chosen = profile.strip()

	if not chosen:
		return subroutine.domain.profiles.Shape(
			scopes=scopes, projects=projects or None, writes=writes or None
		)

	try:
		return subroutine.domain.profiles.expand(
			subroutine.domain.profiles.named(chosen),
			projects=projects,
			writes=writes,
			scopes=scopes,
			workspace=workspace.strip() or None,
		)

	except subroutine.errors.SubroutineError as error:
		_fail(error)


@token_app.command("create")
def token_create (
	title: str = typer.Option("", "--title", help="What this credential is for."),
	username: str = typer.Option(
		"",
		"--username",
		help="Issue for somebody who already has an account, by the name 'user list' shows.",
	),
	service_account: str = typer.Option(
		"",
		"--service-account",
		help="Issue for a machine identity of this name, creating it if needed.",
	),
	workspace: str = typer.Option(
		"", "--workspace", help="Pin the token to one workspace. Unset means all of them."
	),
	scope: list[str] = typer.Option(
		None, "--scope", help="Narrow the token to these permissions. Repeatable."
	),
	project: list[str] = typer.Option(
		None,
		"--project",
		help="Restrict it to this project and everything under it. Repeatable.",
	),
	write: list[str] = typer.Option(
		None,
		"--write",
		help="Only let it change things in this project. Must be one it can reach.",
	),
	profile: str = typer.Option(
		"", "--profile", help=subroutine.domain.profiles.catalogue_help()
	),
	expires: str = typer.Option(
		"", "--expires", help="Stop it working after this day, e.g. 2026-09-01 or now+30d."
	),
	store: str = typer.Option(
		"", "--store", help="Also write it to credentials.toml under this connection name."
	),
) -> None:
	"""Issue a token, and print it once.

	Examples:

	  subroutine token create --title "My laptop"

	  subroutine token create --username thomas --title "Thomas's laptop"

	  subroutine token create --service-account claude --scope task:read --scope task:write

	  subroutine token create --service-account claude --workspace projects --project WEB

	  subroutine token create --title "Acme, this month" --expires now+30d

	Named on its own it is yours. '--username' issues for somebody who already has an
	account; '--service-account' issues for a machine identity and creates one if there is
	none. They are separate flags because they are separate decisions: naming a person under
	'--service-account' is refused rather than quietly handing out their credential.

	Neither will issue for an account that could not use it. A deactivated account is turned
	down here, rather than given a token that fails the first time it is presented.

	'--expires' names a whole day and the credential works through the end of it, the same
	reading a deadline gets. A token that stopped at midnight starting the day somebody
	named is the kind of surprise that arrives at the worst moment.

	The secret is readable exactly once, here. Nothing recovers it afterwards, including
	this program: what is stored is a hash. It is never passed as an argument to anything,
	because that would put it in 'ps' output and shell history.

	'--scope' and '--project' narrow different things and are both worth saying for an agent.
	A scope decides which *verbs* the credential carries; a project decides which *items* it
	can reach at all, and it brings everything underneath that project with it. Giving an
	agent one project and nothing else is what makes it a bounded worker rather than a
	trusted one.

	'--profile' names a scenario instead, and expands into exactly those flags: 'worker' for
	an agent that owns one project, 'collaborator' to read several and write one, 'observer'
	to report on work and change nothing, 'colleague' for a second person in one workspace.
	It refuses a combination that means two things at once rather than picking one.

	'--store' is opt-in rather than the default, and that is a deliberate choice. Writing a
	narrow token into credentials.toml under the local connection would silently narrow your
	own CLI to whatever the agent was given — a token that quietly takes authority away is
	worse than one you have to paste somewhere.
	"""

	shaped = _shaped_by_profile(
		profile, project=project, write=write, scope=scope, workspace=workspace
	)

	# Checked *before* anything is issued, so a credential is never minted and then
	# stranded. `store` reads the existing file (which refuses unparseable TOML) and writes
	# a new one; both can fail, and doing that after the commit left a live token whose
	# secret had never been shown and cannot be recovered — only a hash is kept (§7.4).
	target = store.strip()

	if target:
		_refuse_unusable_credentials_file(target)

	with _administering() as client:
		try:
			minted = client.issue_token(
				title=title.strip() or None,
				username=username.strip() or None,
				service_account=service_account.strip() or None,
				workspace=workspace.strip() or None,
				scopes=shaped.scopes,
				projects=shaped.projects,
				writes=shaped.writes,
				expires=expires.strip() or None,
			)

		except subroutine.errors.SubroutineError as error:
			_fail(error)

	secret = minted.token

	# Printed before it is stored. If the write fails now, the secret is at least on screen.
	if minted.account_created:
		_say(
			f"Created service account {minted.username}, with the "
			f"{subroutine.domain.tokens.SERVICE_ACCOUNT_ROLE} role."
		)

	# **Said back, because the subtree is the part nobody would guess.** A restriction to `SR`
	# also reaches `SR/WEB` and everything under it (§7.3), which is what makes it usable on a
	# tree deeper than one level and is not visible in what was typed.
	restricted = subroutine.views.reach(minted)

	if restricted:
		_say(f"Restricted to {', '.join(restricted)} and anything filed underneath.")

	_say("")
	_say(secret)
	_say("")
	_say("That is the only time it is shown. Store it now.")

	if target:
		try:
			written = subroutine.credentials.store(target, secret)

		except subroutine.errors.SubroutineError as error:
			_fail(error)

		_say(f"Written to {written} for connection {target!r}.")

	else:
		_say(
			f"Give it to a client as {subroutine.credentials.DEFAULT_VARIABLE}, or add it to "
			f"{subroutine.credentials.credentials_file_path()}."
		)


def _expiry (
	written: str, settings: subroutine.config.Settings
) -> datetime.datetime | None:
	"""Read ``--expires`` as the last instant of the day it names, or ``None``.

	The same grammar every other date in this program takes, resolved in the instance's own
	zone — a credential belongs to the installation rather than to whoever happens to be
	typing, and §6.5's chain has nothing narrower to offer here: an administrative command
	has no task and no workspace to inherit from.
	"""

	if not written.strip():
		return None

	try:
		moment = subroutine.domain.schedule.interpret(
			written.strip(),
			boundary=subroutine.domain.schedule.Boundary.END,
			timezone=settings.default_timezone,
			now=subroutine.db.types.utcnow(),
			field="expires",
		)

	except subroutine.errors.SubroutineError as error:
		_fail(error)

	return moment.instant


@token_app.command("list")
def token_list () -> None:
	"""Show the credentials this instance has issued.

	Examples:

	  subroutine token list

	Prefixes, never secrets. Only a hash is stored, so there is nothing here to leak — and
	the prefix is what 'token revoke' takes, which is the point of printing it.

	Each credential says what it can reach and when it was last used, so "which of these can
	write?" and "is this one still in use?" are answerable here rather than by reading the
	database. A credential narrowed to nothing in particular says so in one word.
	"""

	with _administering() as client:
		try:
			listed = client.tokens()

			# One call, and the only thing it is for: a pin is stored as an id and a listing
			# that printed one would be sending its reader to look something up. The workspaces
			# a connection reaches come back with both, so this is a lookup rather than a query.
			places = {
				workspace.id: workspace.slug for workspace in client.identity().workspaces
			}

		except subroutine.errors.SubroutineError as error:
			_fail(error)

	if not listed:
		_say("No credentials have been issued.")
		_say("  subroutine token create --title \"My laptop\"")

		return

	width = max(len(row.prefix) for row in listed)

	for row in listed:
		pin = None if row.workspace_id is None else places.get(row.workspace_id)

		_say(f"  {row.prefix.ljust(width)}  {row.username}  {row.title}  {_credential_state(row)}")
		_say(f"  {' ' * width}  {_credential_reach(row, pin)}")


def _credential_reach (token: subroutine.views.Token, pinned: str | None) -> str:
	"""Say what a credential can reach, and when it was last used.

	**"Which of my tokens can write?" had no answer** (`#175`). The listing showed a prefix, an
	owner, a title and an expiry, and every fact that decides what a leaked credential could
	*do* was absent — which is the question somebody is asking at the moment they read this.

	An empty `scopes` means no narrowing rather than no permission (§12.1a), and that reversal
	is exactly the kind of thing nobody should have to remember while working out whether to
	revoke something. It is spelled out.
	"""

	parts = ["everything its owner can do" if not token.scopes else ", ".join(token.scopes)]

	if pinned is not None:
		parts.append(f"in {pinned} only")

	named = subroutine.views.reach(token)

	if named:
		parts.append(f"projects {', '.join(named)}")

	# A credential issued and never presented is the interesting case here — it is either
	# unused or was pasted somewhere that has not run yet — so it is stated rather than left
	# as a blank the reader has to interpret.
	parts.append(
		"never used"
		if token.last_used_at is None
		else f"last used {token.last_used_at.date().isoformat()}"
	)

	return " · ".join(parts)


def _credential_state (token: subroutine.views.Token) -> str:
	"""Say whether a credential still works, and until when.

	**Reported rather than left to be worked out from two nullable columns.** A listing whose
	reader has to compare `expires_at` against the clock is one where somebody eventually
	reads a dead credential as live, on the day they are checking whether it is.
	"""

	if token.revoked_at is not None:
		return f"revoked {token.revoked_at.date().isoformat()}"

	if token.expires_at is None:
		return "no expiry"

	if token.expires_at <= subroutine.db.types.utcnow():
		return f"expired {token.expires_at.date().isoformat()}"

	return f"until {token.expires_at.date().isoformat()}"


@agent_app.command("create")
def agent_create (
	name: str = typer.Argument("", help="What to call the agent, e.g. 'claude'."),
	project: list[str] = typer.Option(
		None,
		"--project",
		help="Restrict it to this project and everything under it. Repeatable.",
	),
	write: list[str] = typer.Option(
		None,
		"--write",
		help="Only let it change things in this project. Must be one it can reach.",
	),
	workspace: str = typer.Option(
		"", "--workspace", help="Which workspace it works in. Pins the credential to it."
	),
	scope: list[str] = typer.Option(
		None, "--scope", help="Narrow it to these permissions. Repeatable."
	),
	profile: str = typer.Option(
		"", "--profile", help=subroutine.domain.profiles.catalogue_help()
	),
	expires: str = typer.Option(
		"", "--expires", help="Stop it working after this day, e.g. 2026-09-01 or now+30d."
	),
	title: str = typer.Option("", "--title", help="What this credential is for."),
) -> None:
	"""Give an agent an identity of its own, and say how to hand it over.

	Examples:

	  subroutine agent create claude --profile worker --project WEB

	  subroutine agent create sam --profile collaborator --project SR --write SUBSAMPLE

	  subroutine agent create reporter --profile observer --expires now+30d

	  subroutine agent create subsample --workspace projects --project SUBSAMPLE

	This is 'user create', 'user add' and 'token create' as one act, because they are one
	decision: an account with no membership authenticates and can do nothing, which reads as a
	broken token rather than as a missing role.

	It prints an environment line, and that line is half the work. An agent that can run
	shell commands reaches this instance two ways — through the tools its editor wired up, and
	by running 'subroutine' itself — and those resolve credentials separately. Give the token
	only to the editor and the agent is itself over the tools and *you* in its shell, which is
	worse than plainly acting as you: half its work is correctly attributed, so a spot check
	finds its name and concludes the setup worked.

	Setting the variable in the environment the agent's session starts from covers both halves.

	'--profile' says what the agent is *for*, and expands into the flags below it. 'worker'
	owns one project; 'collaborator' reads several and writes one of them; 'observer' reports
	and changes nothing. A combination that means two things at once is refused rather than
	resolved — '--profile observer --write WEB' is not a narrower observer.

	The credential is checked by being presented, not by being described: what it can actually
	do is read back from the instance before this command claims anything.
	"""

	shaped = _shaped_by_profile(
		profile, project=project, write=write, scope=scope, workspace=workspace
	)
	wanted = name.strip()

	if not wanted:
		_stop(
			"Say what to call the agent.",
			"For example: subroutine agent create claude --project WEB",
		)

	with _administering() as client:
		try:
			# **Who this machine acts as *now*, asked before anything is minted.** It is the
			# comparison the closing sentence needs, and asking afterwards would report the
			# same thing while being one step further from the truth.
			operator = client.me()
			minted = client.issue_token(
				service_account=wanted,
				title=title.strip() or f"{wanted} agent",
				workspace=workspace.strip() or None,
				scopes=shaped.scopes,
				projects=shaped.projects,
				writes=shaped.writes,
				expires=expires.strip() or None,
			)

		except subroutine.errors.SubroutineError as error:
			_fail(error)

		variable = subroutine.credentials.variable_for(client.connection.name)
		checked = _what_the_credential_can_do(client, minted.token)

	if minted.account_created:
		_say(
			f"Created service account {minted.username}, with the "
			f"{subroutine.domain.tokens.SERVICE_ACCOUNT_ROLE} role."
		)

	_say("")
	_say("Set this in the environment the agent's session starts from:")
	_say("")
	_say(f"  {variable}={minted.token}")
	_say("")
	_say("That is the only time the credential is shown. Nothing recovers it afterwards.")

	if checked is not None:
		_say("")
		_say(f"Checked, by presenting it: {checked}")

	# **The sentence that stops this looking finished when it is not.** Until the variable is
	# set, the agent's shell keeps resolving whatever the command line resolves — normally the
	# operator's own credential, which on this machine is the *unbounded* one. Naming who that
	# is makes the gap concrete rather than theoretical.
	_say("")
	_say(
		f"Until then its shell acts as {operator.user.username}, and nothing above bounds "
		f"what it does there."
	)


def _what_the_credential_can_do (
	client: subroutine.clients.base.Client, secret: str
) -> str | None:
	"""Present a freshly minted credential and describe what the instance says it may do.

	**Checked rather than described.** A report assembled from what was *asked for* agrees with
	itself whatever the instance decided — and the interesting failures are exactly the ones
	where those differ: a scope that names a permission the owner's role does not carry, a
	workspace pin on a workspace the account was never added to. Presenting the credential is
	the only version of this check worth printing.

	``None`` when the check itself could not be made, which is reported as silence rather than
	as a claim: the credential exists either way and the secret is on screen.
	"""

	try:
		settings = _settings()
		roster = subroutine.connections.roster(settings)

		with subroutine.clients.opening.for_connection(
			client.connection, roster, settings, token=secret
		) as presented:
			answer = presented.me()

	except subroutine.errors.SubroutineError:
		return None

	credential = answer.credential
	kind = "agent" if answer.user.is_service_account else "person"
	where = ", ".join(
		f"{workspace.slug} ({', '.join(workspace.permissions)})"
		for workspace in answer.workspaces
	)

	if not where:
		# The failure this check exists to catch: a credential that authenticates and reaches
		# nothing reads, from every other command, as an instance with no work in it.
		return f"{answer.user.username} ({kind}), and it can read no workspace at all"

	if credential is None or not credential.narrows:
		return f"{answer.user.username} ({kind}), unnarrowed, in {where}"

	# **The project restriction is stated separately because it is the only real boundary.**
	# A scope decides which verbs; a workspace pin decides where — a project decides which
	# *items* exist at all for this credential, which is what `#216` exists for and what makes
	# an agent bounded rather than merely named.
	#
	# Not `views.narrowing()`, deliberately: this sentence already carries the workspace and
	# the effective permissions in `where`, so that renderer would say both twice. The *rule*
	# it shares — reach and write set are two answers and both get named (`#403`) — is what
	# has to stay in step, and a test drives this command to check it.
	named = subroutine.views.reach(credential)
	within = f", and only within {', '.join(named)}" if named else ""
	changing = subroutine.views.writable(credential)
	writes = f", writing only in {', '.join(changing)}" if changing else ""

	return f"{answer.user.username} ({kind}), in {where}{within}{writes}"


@token_app.command("revoke")
def token_revoke (
	prefix: str = typer.Argument("", help="The credential's prefix, as 'token list' shows."),
) -> None:
	"""Stop a credential working, now.

	Examples:

	  subroutine token revoke sr_a1b2c3d4

	Immediate. A revoked credential is checked on every request rather than cached, so there
	is no session to wait out — which is what makes this the answer when a token has leaked
	or a piece of work has ended.
	"""

	named = _named_prefix(prefix)

	with _administering() as client:
		# **Asked before, so "already revoked" can be said.** Revoking keeps the *first*
		# instant, so the response alone cannot tell a first call from a repeat — and which of
		# the two it was is the fact somebody re-running this wants.
		try:
			before = next(
				(row for row in client.tokens() if row.prefix == named), None
			)

			if before is None:
				_stop(
					f"There is no credential with the prefix {named!r}.",
					"Run 'subroutine token list' to see them.",
				)

			stopped = client.revoke_token(id_or_prefix=named)

		except subroutine.errors.SubroutineError as error:
			_fail(error)

	when = "?" if stopped.revoked_at is None else stopped.revoked_at.date().isoformat()

	if before.revoked_at is not None:
		_say(f"{named} was already revoked, on {when}.")

		return

	_say(f"Revoked {named}. It stops working immediately.")


def _named_prefix (given: str) -> str:
	"""Read the prefix out of whatever the caller pasted, or refuse.

	A token is written ``sr_<prefix>_<secret>`` and only the prefix is stored, so ``token
	list`` prints eight hex characters while the thing somebody kept is the whole string.
	Both spellings of the *prefix* are taken — with the scheme and without — for the same
	reason a ref accepts ``42`` and ``#42``: the notation the program printed should be one
	it reads back.

	**A whole token is refused rather than accepted**, and that is the point of this
	function. It would work — the prefix is right there in it — and it would put a live
	credential into shell history and into ``ps`` output for every process on the machine,
	which is the one thing §7.4 never lets a secret do.
	"""

	named = given.strip()

	if not named:
		_stop("Which credential?", "Run 'subroutine token list' to see their prefixes.")

	parts = named.split("_")

	if len(parts) > 2:
		_stop(
			"That is a whole token, not a prefix.",
			f"Pass only the prefix — '{parts[1]}' here — so the secret stays out of your "
			f"shell history. 'subroutine token list' prints them.",
		)

	if len(parts) == 2 and parts[0] == subroutine.auth.TOKEN_SCHEME:
		named = parts[1]

	return named


def _refuse_unusable_credentials_file (name: str) -> None:
	"""Check the credentials file can be read, before a token is issued against it.

	Only the read is checked here — a directory that becomes unwritable between this and the
	write is not worth guarding against, and the secret is now printed before the write in any
	case. What this catches is the common one: a `credentials.toml` that does not parse.
	"""

	try:
		subroutine.credentials.read_file()

	except subroutine.errors.SubroutineError as error:
		_fail(error)


@contextlib.contextmanager
def _administering () -> typing.Iterator[subroutine.clients.base.Client]:
	"""Yield the client a credential command should act through — item `#348`.

	**Which one is decided by the connection, not by a flag.** A flag would leave the bare
	command still failing on every machine whose work is on a served instance, which is the
	whole complaint: `token list`, `create` and `revoke` opened a local database because §12.4
	requires the administrative commands to work when the service will not start, and where
	there is no local database they simply refused.

	So: a remote connection goes over the wire, and a local one opens the database directly as
	it always has. That falls out correctly on both sides of the case this was found in. On a
	laptop the direct path would target whatever ``database_url`` names, which after a
	migration is a file nobody uses — the wrong answer, silently. On the server, the service
	account's own connection *is* local, so §12.4's recovery path is exactly where it was: the
	database is reachable with the service stopped, because reaching it never involved the
	service.

	The connection is the one a write would go to, so `-c` chooses it for one command the same
	way it chooses where `add` lands.
	"""

	settings = _settings()

	try:
		roster = subroutine.connections.roster(settings)

		# **Where a write would go, not the configured default.** `subroutine use` and a
		# `.subroutine` marker both move that, and a credential command that ignored them would
		# put `token create` and `add` on different instances from one directory — which is the
		# shape `#278` found and is worse here, because the thing that lands in the wrong place
		# is authority. Resolved exactly as the personal commands resolve it.
		current = subroutine.context.resolve(
			roster,
			connection=_selected.connection,
			workspace=_selected.workspace,
			marker=subroutine.directory.find(),
		)
		connection = roster.require(current.connection)

	except subroutine.errors.SubroutineError as error:
		_fail(error)

	if connection.is_local and _database_is_absent(settings):
		_refuse_absent_database(settings)

	try:
		with subroutine.clients.opening.for_connection(connection, roster, settings) as client:
			yield client

	except subroutine.errors.SubroutineError as error:
		_fail(error)


def _operator (
	session: sqlalchemy.orm.Session, settings: subroutine.config.Settings
) -> subroutine.domain.authentication.Principal:
	"""Return who is running an administrative command, honouring a presented token.

	**The token is not optional here, and leaving it out was a privilege escalation.** §12.1a
	says the check runs in local mode exactly as it runs over HTTP; this path resolved the
	principal with no token at all, so an agent holding a credential scoped to `task:read`
	could not add a task and *could* mint itself an unrestricted one — because it was
	authorised as the sole human, which after `init` is a superuser. The scoping refusal was
	correct, well-worded, and bypassable by the command next to it.

	The token is resolved the way every other connection's is (§12.3a), so `SUBROUTINE_TOKEN`,
	`token_env`, `token_command` and `credentials.toml` all behave here as they do elsewhere.

	**And the refusal says which of those it came from** (`#199`). `#175` gave `local.principal`
	a `token_source` for exactly that, `clients/local.py` passes it, and this call site did not —
	so an unusable credential in `credentials.toml` told an operator "the token supplied could
	not be used" and offered to issue another, which does not remove the one in the file that is
	refusing every command. It is the ordinary command beside this one that named the file, and
	§12.4 makes these the commands that have to work when the ordinary ones do not.
	"""

	roster = subroutine.connections.roster(settings)
	local = roster.find(subroutine.connections.LOCAL_NAME)

	# `local` can be turned off (§13.7), in which case there is no local credential to read
	# and an administrative command still operates on this database.
	resolved = (
		subroutine.credentials.Resolved(token=None, source="nowhere")
		if local is None
		else subroutine.credentials.resolve(local, default_connection=roster.default)
	)

	return subroutine.domain.local.principal(
		session,
		token=resolved.token,
		local_user=settings.local_user,
		token_source=resolved.source,
	)


def _pinned_workspace (
	session: sqlalchemy.orm.Session,
	user: subroutine.db.models.identity.User,
	workspace: str,
) -> subroutine.db.models.identity.Workspace | None:
	"""Return the workspace a token is pinned to, or ``None`` for all of them.

	**Never pinned by default** (SPEC.md §7.4, §13.7). A presented token should give the
	access it gives locally; narrowing a credential to shorten an address, or because one
	workspace is the common case, is letting a convenience dictate the access model.
	"""

	wanted = workspace.strip()

	if not wanted:
		return None

	model = subroutine.db.models.identity.Workspace
	found = session.scalars(
		sqlalchemy.select(model).where(
			model.slug == subroutine.domain.workspaces.normalize_slug(wanted),
			model.deleted_at.is_(None),
		)
	).one_or_none()

	if found is None:
		_stop(
			f"There is no workspace called {wanted!r} here.",
			"Run 'subroutine use' to see which one you are in.",
		)

	return found


def _sole_workspace (
	session: sqlalchemy.orm.Session,
) -> subroutine.db.models.identity.Workspace:
	"""Return the only workspace, or refuse because a new account needs a home."""

	model = subroutine.db.models.identity.Workspace
	found = list(
		session.scalars(
			sqlalchemy.select(model)
			.where(model.deleted_at.is_(None))
			.order_by(model.created_at)
			.limit(2)
		)
	)

	if len(found) == 1:
		return found[0]

	if not found:
		_stop("There are no workspaces here.", "Run 'subroutine init' first.")

	_stop(
		"There is more than one workspace, so a new service account needs to be told which "
		"one it works in.",
		f"Pass --workspace, one of: {', '.join(item.slug for item in found)}.",
	)


def _safety_copy (settings: subroutine.config.Settings, *, yes: bool) -> None:
	"""Save what a restore is about to replace, and let the operator decide if that cannot be done.

	**Best effort, and never fatal on its own** (`#173`). A restore is most often the answer to a
	database that is already unwell, so this is the copy in the whole program most likely to
	fail — and aborting the rescue because the broken thing could not be copied first withholds
	the remedy on account of the symptom. §12.4's argument is that recovery works under
	pressure; a safety net that blocks the rescue is not a safety net.

	**And never silent, which is the half that was missing.** The previous form suppressed the
	failure and printed nothing, so a restore that saved nothing looked exactly like one that
	had: the operator was told it succeeded, and discovered they had no way back only on the day
	they wanted one.

	So the failure is reported, and then it is *their* call, because only they know whether the
	state about to be overwritten was worth anything.
	"""

	try:
		engine = subroutine.db.session.create_engine(settings.database_url)

		try:
			kept = subroutine.db.backup.take(engine, settings)

		finally:
			engine.dispose()

	# Broad on purpose. Every failure here has the same answer — say so, then ask — and
	# narrowing it means the one storage error nobody predicted aborts the restore all over
	# again. `_database` cannot be reused for this: it turns a database error into `_stop`,
	# which is precisely the behaviour being fixed.
	except Exception as error:
		_warn(f"The database being replaced could not be backed up: {error}")
		_warn(
			"That usually means it is already damaged, which is a reason to go on rather than "
			"to stop — but it does mean there is no way back to the state it is in now."
		)

		if not (yes or typer.confirm("Restore anyway?", default=True)):
			_stop(
				"Nothing restored. The database is as it was.",
				"To keep a way back, copy the database file somewhere by hand first. To skip "
				"this copy every time, add --no-safety-backup.",
			)

		return

	_say(f"The database being replaced was saved to {kept.path}")


def _report_the_newest_release () -> None:
	"""Say what is running, what has been released, and whether the gap moves the schema.

	**The only outbound request this program makes**, and only when somebody typed
	``--check``. §12.4a's rule is that a self-hosted tool must never phone home uninvited; a
	command is the invitation, and an instance that never runs this never talks to anybody.

	The version reported is what is *running* rather than what a package index thinks is
	installed — `#321` was found on an instance reporting ``0.1.5.dev7`` while running
	something thirty-four commits later, with a laptop beside it reporting a third number.
	"""

	try:
		record = subroutine.releases.published()

	except subroutine.errors.SubroutineError as error:
		_fail(error)

	newest = record[0] if record else None
	standing = subroutine.releases.Standing(
		running=subroutine.installations.program(),
		schema=subroutine.db.migrate.head_revision(),
		published=tuple(record),
		# **Asked of the migration history, not of the version strings.** A revision this
		# build knows is one it could migrate forward from, so the release is behind it; one
		# it has never seen was written by a later release. That is the whole direction.
		knows_the_newest_schema=(
			newest is not None and subroutine.db.migrate.knows_revision(newest.schema)
		),
	)

	for line in subroutine.releases.describe(standing):
		_say(line)


def _database_is_absent (settings: subroutine.config.Settings) -> bool:
	"""Report whether the configured SQLite file has not been created yet.

	One definition, shared with the local client, which needs the same question answered to
	tell "never set up" from "cannot be reached" (`#165`). They had two readings of it for a
	day and only the administrative half was right.
	"""

	return settings.has_no_instance_yet()


def _refuse_unusable_storage (settings: subroutine.config.Settings) -> None:
	"""Make every directory ``init`` writes into, and stop with a sentence if one cannot be.

	SPEC.md §10.4 for the second half — SQLite's locking failure otherwise arrives as
	``database is locked`` on the first write, which reads as a concurrency bug rather than as
	"this directory is on a network share", and by then there is a half-built database to clean
	up.

	**All three XDG directories, not only the database's** (`#255`). This checked
	``settings.sqlite_path``, which is ``None`` on PostgreSQL, so it returned immediately and
	``ensure_secret_key`` walked into an unwritable configuration directory a moment later —
	reaching a person setting up a server as four frames of ``pathlib.mkdir`` recursion and a
	bare ``PermissionError``. Simon met that following ``docs/hosting.md`` on a clean Ubuntu
	server, where ``/var/lib/subroutine`` does not exist until the service first starts and the
	service cannot start until this has run.

	Not PostgreSQL-specific, which is what makes it worth the wider check: a writable data
	directory and an unwritable configuration one produced the same traceback on SQLite.
	"""

	for what, directory in (
		("configuration", subroutine.config.config_home()),
		("data", subroutine.config.data_home()),
		("state", subroutine.config.state_home()),
	):
		_make_directory(what, directory)

	path = settings.sqlite_path

	if path is None:
		return

	# Usually inside the data directory and already made above, but `database_url` may name
	# somewhere else entirely, and that somewhere still has to exist.
	_make_directory("database", path.parent)

	problem = subroutine.config.probe_sqlite_locking(path.parent)

	if problem is not None:
		_err.print(problem, markup=False, highlight=False)

		raise typer.Exit(code=1)


def _make_directory (what: str, directory: pathlib.Path) -> None:
	"""Create one of the directories ``init`` needs, or stop and say what to do about it.

	**Names the outermost part that is missing, not the leaf.** Told it cannot create
	``/var/lib/subroutine/config/subroutine``, somebody reasonably tries to create exactly
	that and is refused again; the part they can actually act on is ``/var/lib/subroutine``,
	four levels up, and it is the only one that needs root.
	"""

	try:
		directory.mkdir(parents=True, exist_ok=True)

	except OSError as error:
		blocked = _outermost_missing(directory)

		_err.print(
			f"Cannot create the {what} directory {directory}: {error.strerror or error}.",
			markup=False,
			highlight=False,
		)
		_err.print("", markup=False, highlight=False)

		if blocked != directory:
			_err.print(
				f"{blocked} does not exist, and this account cannot create it. Make it as "
				f"root and hand it to this account, or point the XDG directories somewhere "
				f"this account can already write.",
				markup=False,
				highlight=False,
			)

		else:
			_err.print(
				f"Give this account permission to write in {directory.parent}, or point the "
				f"XDG directories somewhere it can already write.",
				markup=False,
				highlight=False,
			)

		raise typer.Exit(code=1) from None


def _outermost_missing (directory: pathlib.Path) -> pathlib.Path:
	"""Return the highest ancestor of ``directory`` that does not exist, or ``directory``."""

	missing = directory

	for parent in directory.parents:
		if parent.exists():
			break

		missing = parent

	return missing


def _warn_an_earlier_instance_is_elsewhere (settings: subroutine.config.Settings) -> None:
	"""Say so before building a *second* instance, when the first one is somewhere else (`#267`).

	The state is specific and the signal is exact rather than heuristic: ``config.toml``
	already carries a ``secret_key``, which only ``init`` writes, so ``init`` has run in this
	configuration directory before — and the database it is looking at now is absent, so that
	earlier run cannot have used it. The first instance is therefore somewhere this
	configuration does not name.

	**Which is what happens on a PostgreSQL install where the URL was given in the environment
	and never written down** (`#265`). Without this, ``init`` reports ``Ready.``, ``db current``
	reports a healthy schema and ``list`` reports an empty backlog — three confident answers
	about a database nobody wanted, and a service that then starts against it and serves
	nothing. Worse than the restart loop of `#264`, because a loud failure has become a quiet
	wrong answer whose shape is "the data has gone".

	**A warning rather than a refusal, and not out of caution.** ``ensure_secret_key`` runs
	before ``migrate.upgrade``, so an ``init`` that failed while preparing its database leaves a
	key behind and no database; refusing would block that retry. It has to come *before* the
	work, though — after ``Ready.`` it reads as a footnote to a success.
	"""

	if not subroutine.config.config_file_path().exists():
		return

	if "secret_key" not in subroutine.config.read_config_file():
		return

	if not _database_is_absent(settings):
		return

	# **Phrased as a condition, not as a finding.** The same two facts are left behind by an
	# `init` that failed while preparing its database, where no earlier instance exists at all —
	# so asserting that one does would be false exactly when somebody is already recovering.
	_warn(
		f"'init' has run here before — {subroutine.config.config_file_path()} already holds a "
		f"signing key — and there is no database at {safe_url(settings.database_url)}. If you "
		f"set an instance up earlier and it is not there, it is somewhere this configuration "
		f"does not name: put its 'database_url' in that file and run this again, or carrying "
		f"on will build a second, empty one."
	)


def _warn_an_environment_database_is_not_recorded (
	settings: subroutine.config.Settings
) -> None:
	"""Say so when the database just built is one nothing will remember (`#265`).

	A fresh PostgreSQL installation has a chicken-and-egg in it: ``config.toml`` does not exist
	until ``init`` has run, so the database is named in the environment for that one run. It
	works, the schema lands, and **nothing records where it went** — ``init`` writes only
	``secret_key``. The variable dies with the shell, the unit sets only the XDG paths, and the
	service comes up configured for SQLite and restarts every five seconds.

	**Writing it to ``config.toml`` here would be the wrong fix and is not on the table.** A
	PostgreSQL URL routinely carries a password and §12.3a is that this file holds no secrets —
	that is the entire reason ``credentials.toml`` exists. Persisting an environment value into
	a file would also invert the precedence the configuration model rests on.

	So the value cannot be written for the operator, and the operator can be told they must
	write it. This is the moment to do that: before the mistake, rather than after it, in a
	service log.
	"""

	if subroutine.config.setting_sources(settings).get("database_url") != "environment":
		return

	_warn(
		f"This database came from the environment, and nothing has recorded it. Put "
		f"'database_url' in {subroutine.config.config_file_path()}, or anything started "
		f"without that variable — a service, another shell — will look somewhere else."
	)


def _refuse_absent_database (
	settings: subroutine.config.Settings, *, doing: str = ""
) -> typing.NoReturn:
	"""Stop, naming the database that is not there and what to do about it.

	**Written once and called eight times, because it was written eight times** — six of them
	character-identical. That is how a message comes to be improved in one command and not the
	other seven, and how `#264` cost Simon an afternoon: a service in ``Restart=on-failure``
	repeating "run 'subroutine init' first" while a populated PostgreSQL database sat beside it
	and the configuration quietly pointed at a SQLite path nobody had chosen.
	"""

	_stop(
		f"There is no database at {safe_url(settings.database_url)}{doing}.",
		_where_a_database_would_come_from(settings),
	)


def _instances_reached_from_here (settings: subroutine.config.Settings) -> tuple[str, ...]:
	"""Return the names of the connections that are somebody else's instance, if any.

	**A refusal is the wrong place to raise from**, so a configuration this cannot read is
	reported as no connections at all and the caller falls back to the advice it gave before
	this existed. Losing one sentence beats replacing a clear message with a traceback.
	"""

	try:
		found = subroutine.connections.roster(settings)

	except subroutine.errors.SubroutineError:
		return ()

	return tuple(
		connection.name for connection in found if not connection.is_local
	)


def _where_a_database_would_come_from (settings: subroutine.config.Settings) -> str:
	"""Return the advice to print beside "there is no database", given what this machine has.

	**Three different faults wear the same message.** Somebody who has never set an instance up
	needs ``init``. Somebody who set one up in PostgreSQL, whose service is looking at a SQLite
	path, needs to know ``database_url`` is not configured — telling *them* to run ``init``
	sends them back to the command that already worked. And somebody whose work is on a served
	instance needs to be told that this command is not about that instance at all: ``init``
	would build a second, empty one beside a connection that works, which is the outcome `#264`
	and `#267` exist to prevent, arriving by a different door (`#328`).

	The first is distinguished by where the setting came from, and ``setting_sources`` already
	knows: a default means nobody chose this database, anything else means somebody did. The
	third is distinguished by whether any connection reaches somewhere else — which was the one
	fact this never consulted, though it sat one module away.

	**Only the default branch is widened.** Naming a ``database_url`` states an intention to
	keep an instance here, so a connection beside it changes nothing about that fault.
	"""

	if subroutine.config.setting_sources(settings).get("database_url") != "default":
		return "Run 'subroutine init' first."

	elsewhere = _instances_reached_from_here(settings)

	if elsewhere:
		# Not a prose list: `Roster.alternatives` joins with commas for the same reason, and a
		# second way of writing a series is a second thing to keep consistent.
		return (
			f"This machine has no instance of its own; it reaches {', '.join(elsewhere)}. "
			f"This command acts on a local database, so run it where that instance lives — "
			f"'subroutine init' would set up a second, empty one here."
		)

	return (
		"Nothing has configured 'database_url', so this is the default. Run 'subroutine init' "
		f"to set an instance up here, or name the database you meant in "
		f"{subroutine.config.config_file_path()} — a URL given only in the environment is not "
		f"recorded anywhere, so a service started without it will not find it."
	)


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


def _report_version (asked: bool) -> None:
	"""Print what is installed, and stop before anything else runs.

	Two numbers, because two different conversations need them. The **release** is what a bug
	report is asked for first. The **schema** is what an upgrade is about (SPEC.md §12.4a): the
	package manager moves the code and this command says which shape of database that code now
	expects, so a migration can be planned rather than discovered.

	The version is read from the installed distribution rather than written here, because a
	constant in the source is a second copy of the one in ``pyproject.toml`` and the two would
	part company at the first release. Running from a source tree with nothing installed says
	``0.0.0+unknown``, which is true and is not a version anybody will mistake for one.

	**Handled as a parameter callback, which is what lets it answer through a broken profile.**
	A bad ``--profile`` — or a stale ``SUBROUTINE_PROFILE`` in the environment — refuses every
	command by design (SPEC.md §12.5), and "what am I running?" is the question somebody asks
	*while* working that out. Parameters are processed before the callback body, so this runs
	and exits before :func:`subroutine.config.use_profile` is ever reached; printing from the
	body instead makes ``subroutine --profile ../evil --version`` exit 2 with a message about
	the profile, which ``tests/test_smoke.py`` was checked against. ``is_eager`` is not what
	buys that today — no other option here has a callback to be ordered against — it is what
	keeps it true if one grows.
	"""

	if not asked:
		return

	_say(f"subroutine {subroutine.__version__}")
	_say(f"schema {subroutine.db.migrate.head_revision() or 'unknown'}")

	# The schema line above is what this build *expects*; `db current` is what the database in
	# front of you actually has. One without the other cannot answer whether an upgrade is owed.
	subroutine.cli.personal.suggest("subroutine db current", "what your database is at")

	raise typer.Exit()


@app.callback()
def _default (
	context: typer.Context,
	workspace: str = typer.Option(
		"", "--workspace", "-w", help="Which workspace this command is about."
	),
	connection: str = typer.Option(
		"", "--connection", "-c", help="Which instance this command is about."
	),
	profile: str = typer.Option(
		"",
		"--profile",
		help="Act on a separate installation on this machine, by name.",
		envvar=subroutine.config.PROFILE_VARIABLE,
	),
	version: bool = typer.Option(
		False,
		"--version",
		callback=_report_version,
		is_eager=True,
		help="Print the installed version and the schema it expects.",
	),
) -> None:
	"""Project management for people and agents, in equal measure.

	Examples:

	  subroutine add "Call the dentist before Sunday"

	  subroutine today

	  subroutine done 1

	  subroutine explain dates

	Run with no arguments, this shows today's agenda.
	"""

	# **First, before anything reads a path.** A profile decides where the configuration file,
	# the database, the credentials and the current context all live (SPEC.md §12.5), so it has
	# to be settled before any of them is looked up. It goes here rather than on each command
	# because it applies to all of them — which does mean it precedes the subcommand:
	# `subroutine --profile scratch db backup`, not `subroutine db backup --profile scratch`.
	try:
		subroutine.config.use_profile(profile.strip() or None)

	except ValueError as error:
		raise typer.BadParameter(str(error), param_hint="--profile") from error

	# Before the subcommand, because that is where Typer puts an application-wide option and
	# because these two change what every command means rather than what one of them does.
	# `subroutine use` makes the same choice durably (SPEC.md §13.7).
	_selected.workspace = workspace.strip() or None
	_selected.connection = connection.strip() or None

	if context.invoked_subcommand is not None:
		return

	# The bare invocation (SPEC.md §12.2a). `today` answers the question somebody opening
	# this tool is actually asking; a help wall answers one nobody asked.
	_show_today()

	# **And one line saying there is more.** §12.2a's habit is that the user is never left
	# wondering what exists — every command prints the next one to try — and this, the single
	# most likely first thing anybody types, had no such line at all.
	#
	# On the *bare* invocation only, never on an explicit `subroutine today`. The two are the
	# same output and different questions: bare is somebody arriving, `today` is somebody who
	# already knows what they want, and a daily habit should not carry a beginner's signpost
	# forever. `invoked_subcommand` is what tells them apart.
	subroutine.cli.personal.suggest("subroutine --help", "everything it can do")


def main () -> None:
	"""Entry point for the 'subroutine' command.

	**A refusal that reaches here is still a sentence, not a traceback** (`#175`). Every
	command is supposed to catch its own and call `_fail`, and most do — but `db backups`
	printed sixty lines of Python and three chained exceptions for an unusable
	`backup_directory`, while `db backup` beside it caught the identical condition and said
	something useful. That is the failure `docs/hosting.md` predicts, and the command an
	operator would check with was the one that exploded.

	Catching it once here is the fix that does not rot. A per-command `try` is a thing to
	remember on every command ever added, and the evidence that it gets forgotten is the item
	this note comes from. Commands still catch what they can say something *better* about;
	this is only the floor.
	"""

	try:
		app()

	except subroutine.errors.SubroutineError as error:
		_printed(error)

		raise SystemExit(1) from None

	# **Everything else is a defect, and is reported as one** (`#258`). Typer's boxed source
	# and caret is a developer's view of a developer's problem; §1.4's reader is setting up a
	# to-do list and can act on none of it.
	#
	# **Caught after the refusal above, which is what stops the two being flattened.** A
	# failure the program understands — an unwritable directory the *operator* chose, a
	# database that is not there — is a `SubroutineError` with a sentence naming the thing,
	# and `#255` is the item that made those specific. This only ever sees what nothing
	# anticipated.
	#
	# `Exception` rather than `BaseException`, so `KeyboardInterrupt` still ends the process
	# quietly and `SystemExit` still carries its own code.
	except Exception as defect:
		_report_a_defect(defect)

		raise SystemExit(1) from None
