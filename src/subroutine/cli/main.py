"""The ``subroutine`` command.

``init`` is the only thing a new user runs before the one they actually wanted, so it
prints **one line** (SPEC.md §12.1). The workspace, the Inbox, the role assignment and the
instance identity are all created and none of them are announced: someone setting up a
to-do list has not asked about workspaces. ``--verbose`` prints the full transcript for
whoever does want it.
"""

import contextlib
import getpass
import ipaddress
import pathlib
import sys
import tomllib
import typing

import pydantic
import rich.console
import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.exc
import sqlalchemy.orm
import typer

import subroutine.cli.personal
import subroutine.cli.topics
import subroutine.config
import subroutine.credentials
import subroutine.db.migrate
import subroutine.db.models.identity
import subroutine.db.session
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.local
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors

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
)

config_app = typer.Typer(help="Inspect and manage configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")

database_app = typer.Typer(help="Look after the database.", no_args_is_help=True)
app.add_typer(database_app, name="db")

token_app = typer.Typer(help="Issue credentials for agents and other machines.", no_args_is_help=True)
app.add_typer(token_app, name="token")

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
		# A single field error whose message *is* the detail says nothing new. Printing it
		# twice reads as a stutter — `subroutine add "#tag !3"` said "A title is required."
		# and then "  title: A title is required."
		if len(error.errors) == 1 and field.message == error.detail:
			continue

		_err.print(f"  {field.field}: {field.message}", markup=False, highlight=False)

	raise typer.Exit(code=1)


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


#: Host names that mean "this machine only" without being addresses. ``ipaddress`` cannot
#: parse a name, and refusing to serve on ``localhost`` because it is not spelled ``127.0.0.1``
#: would be a check failing on the one case it exists to allow.
LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


def is_loopback (host: str) -> bool:
	"""Report whether binding to this host keeps the socket on one machine.

	A wildcard — ``0.0.0.0`` or ``::`` — is *not* loopback even though it includes it: it
	accepts a connection from anywhere the machine has an address, which is the whole of what
	SPEC.md §12.4 is about. An unparseable name is treated as non-loopback, because guessing
	the safe answer wrong in that direction only costs one flag.
	"""

	name = host.strip().lower().strip("[]")

	if name in LOOPBACK_NAMES:
		return True

	try:
		address = ipaddress.ip_address(name)

	except ValueError:
		return False

	return address.is_loopback


@app.command()
def serve (
	host: str = typer.Option("", "--host", help="What to listen on. Defaults to 127.0.0.1."),
	port: int = typer.Option(0, "--port", help="Which port to listen on."),
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

	There is no setting that turns the API on or off, deliberately (SPEC.md §12.4): if this
	process is not running there is no socket, and a configuration key that made ``serve``
	refuse to start would be a confusing way of saying "do not run it". The control that
	actually controls anything is the bind address, and its default is loopback.
	"""

	settings = _settings()
	where = host.strip() or settings.host
	listening = port or settings.port

	_refuse_unusable_storage(settings)
	_refuse_public_bind(settings, where, insecure=insecure)

	if _database_is_absent(settings):
		_stop(
			"There is no database here yet, so there would be nothing to serve.",
			"Run 'subroutine init' first.",
		)

	# **Imported here rather than at the top of the module**, and measured rather than
	# assumed: FastAPI and uvicorn together cost 0.3 seconds of this program's 0.8-second
	# start, on every `subroutine add` — for one command most people never run. The `as`
	# aliases are the house style's documented exception for a nested import, because a plain
	# `import subroutine.api.app` here would bind `subroutine` as a *local* name and shadow
	# every other use of it in this function.
	from uvicorn import run as listen

	from subroutine.api import app as api

	_say(f"Serving on http://{where}:{listening} — the agent guide is at /v1/docs/agent.")

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

	if insecure or is_loopback(host):
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


#: The role a new service account is given in the workspace it is made for. ``contributor``
#: reads everything and writes tasks and comments, and cannot restructure projects — which is
#: the right starting authority for an agent, and is narrowable further by the token's own
#: scopes (SPEC.md §7.3).
SERVICE_ACCOUNT_ROLE = "contributor"


@token_app.command("create")
def token_create (
	title: str = typer.Option("", "--title", help="What this credential is for."),
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
	store: str = typer.Option(
		"", "--store", help="Also write it to credentials.toml under this connection name."
	),
) -> None:
	"""Issue a token, and print it once.

	Examples:

	  subroutine token create --title "My laptop"

	  subroutine token create --service-account claude --scope task:read --scope task:write

	**The secret is readable exactly once**, here. Nothing recovers it afterwards, including
	this program: what is stored is a hash (SPEC.md §7.4). It is never passed as an argument
	to anything, because that would put it in ``ps`` output and shell history.

	``--store`` is opt-in rather than the default, and that is a deliberate choice. Writing a
	narrow token into ``credentials.toml`` under the local connection would silently narrow
	*your own* CLI to whatever the agent was given — a token that quietly takes authority away
	is worse than one you have to paste somewhere.
	"""

	settings = _settings()

	if _database_is_absent(settings):
		_stop("There is no database here yet.", "Run 'subroutine init' first.")

	with _database(settings) as engine:
		factory = sqlalchemy.orm.sessionmaker(bind=engine, expire_on_commit=False)

		with factory() as session:
			try:
				# Who is running this, and the actor every service call below is checked
				# against. Creating an account is an *instance* permission held only by a
				# superuser (§7.1), and `init` makes the first user one — so the operator's
				# own authority is what decides whether they may mint an agent, rather than
				# their having a shell. `tests/test_actor_discipline.py` is what caught this
				# being omitted, which is the whole reason that check exists.
				operator = subroutine.domain.local.principal(
					session, local_user=settings.local_user
				)
				owner, created = _token_owner(session, operator, service_account, workspace)
				pinned = _pinned_workspace(session, owner, workspace)
				_row, issued = subroutine.domain.authentication.issue_token(
					session,
					user=owner,
					title=title.strip() or f"{owner.username}'s token",
					workspace_id=None if pinned is None else pinned.id,
					scopes=[item.strip() for item in (scope or []) if item.strip()],
				)

			except subroutine.errors.SubroutineError as error:
				session.rollback()
				_fail(error)

			session.commit()

			secret = issued.value.get_secret_value()
			written = subroutine.credentials.store(store.strip(), secret) if store.strip() else None

	if created:
		_say(f"Created service account {owner.username}, with the {SERVICE_ACCOUNT_ROLE} role.")

	_say("")
	_say(secret)
	_say("")
	_say("That is the only time it is shown. Store it now.")

	if written is not None:
		_say(f"Written to {written} for connection {store.strip()!r}.")

	else:
		_say(
			f"Give it to a client as {subroutine.credentials.DEFAULT_VARIABLE}, or add it to "
			f"{subroutine.credentials.credentials_file_path()}."
		)


def _token_owner (
	session: sqlalchemy.orm.Session,
	operator: subroutine.domain.authentication.Principal,
	service_account: str,
	workspace: str,
) -> tuple[subroutine.db.models.identity.User, bool]:
	"""Return whose token this is, making a service account if one was named.

	Returns ``(user, created)``. Running the same command twice reuses the account rather than
	refusing: issuing a second token for one agent is an ordinary thing to want, and "that
	name is taken" would be a strange thing to say about the account you asked for.
	"""

	name = service_account.strip()

	if not name:
		return operator.user, False

	normalized = subroutine.domain.users.normalize(name)
	model = subroutine.db.models.identity.User
	existing = session.scalars(
		sqlalchemy.select(model).where(
			model.username_normalized == normalized, model.deleted_at.is_(None)
		)
	).one_or_none()

	if existing is not None:
		return existing, False

	account = subroutine.domain.users.create(
		session, username=name, is_service_account=True, actor=operator
	)
	home = _pinned_workspace(session, account, workspace) or _sole_workspace(session)

	# An account with no role can authenticate and do nothing, which reads as a broken token
	# rather than as a missing membership. Given the narrowest role that can actually work.
	subroutine.domain.workspaces.add_member(
		session, home, account, role_key=SERVICE_ACCOUNT_ROLE, actor=operator
	)

	return account, True


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


@app.callback()
def _default (
	context: typer.Context,
	workspace: str = typer.Option(
		"", "--workspace", "-w", help="Which workspace this command is about."
	),
	connection: str = typer.Option(
		"", "--connection", "-c", help="Which instance this command is about."
	),
) -> None:
	"""Project management for people and agents, in equal measure.

	Examples:

	  subroutine add "Call the dentist before Sunday"

	  subroutine today

	  subroutine done 1

	  subroutine help dates

	Run with no arguments, this shows today's agenda.
	"""

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


def main () -> None:
	"""Entry point for the ``subroutine`` command."""

	app()
