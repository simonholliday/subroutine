"""The set of instances a client can reach — docs/design.md §13.7.

A person keeps their own life in one place and their employer's work in another, on
different servers under different ownership, and they must stay that way. But the questions
cross the boundary constantly — "am I free on Thursday?" — so the *client* holds a set of
named connections and asks all of them.

**The local database is a connection like any other, and that is the rule the whole design
rests on.** Local mode began as what happened when nothing was configured, which would have
given one person two different experiences of one command depending on where their tasks
were. Instead this installation's own database is a connection named ``local``, present
without being declared, and ``subroutine agenda`` fans out across it and every remote in
exactly the same way. Declare it only to rename it or to turn it off.

Three names are involved and confusing them is how a merged view starts lying:
``instance_id`` is the server's identity, minted once at ``init`` and never changed;
``instance_name`` is what whoever runs it calls it; and the **connection name** is *your*
nickname for it, the key in your own configuration file. Two colleagues may call the same
server ``work`` and ``acme``, and one person may connect to two servers both calling
themselves "Office". Neither is a problem, because ``instance_id`` settles what is what.

No tokens live here. Where they are is :mod:`subroutine.credentials`, and the split between
the two files is §12.3a's. Said as *tokens* rather than *secrets* because ``config.toml`` does
hold one thing — ``secret_key``, which signs pagination cursors — and this sentence used to be
one of five places implying otherwise (`#831`).
"""

import dataclasses
import re
import typing
import urllib.parse

import subroutine.config
import subroutine.errors

#: The connection that reaches this installation's own database. It exists whether or not
#: anybody declares it, because a person who has never heard of a connection still has one.
LOCAL_NAME = "local"

#: What the local connection is called in output, unless it has been renamed. Capitalised
#: because it is a label a person reads, not a key they type.
LOCAL_LABEL = "Local"

#: How long to wait for one connection before giving up on it and saying so (§13.7). Short
#: on purpose: the cost of waiting is a person staring at a blank terminal, and the cost of
#: giving up early is one line saying which server did not answer.
DEFAULT_TIMEOUT_SECONDS = 5.0

#: A connection name, which becomes the first segment of an address — ``work/acme/42``. The
#: shape is a workspace slug's, plus a required leading letter: a name of all digits would
#: read as a ref to every human who saw it, and the point of a name is to be typed.
_NAME = re.compile(r"\A[a-z][a-z0-9_-]*\Z")

#: What may appear inside a ``[connections.x]`` table. Anything else is refused rather than
#: ignored, because the failure mode of a silently-dropped key is the wrong posture: a
#: mistyped ``read_only`` would leave an agent able to write to an employer's instance while
#: the configuration says, to the person reading it, that it cannot.
KNOWN_KEYS = frozenset(
	{
		"url",
		"display_name",
		"read_only",
		"token_env",
		"token_command",
		"timeout_seconds",
		"enabled",
	}
)

#: Schemes a connection may use. Anything else is a mistake worth naming — a ``url`` of
#: ``tasks.example.com`` with no scheme parses as a *path* and would otherwise be attempted
#: and fail with something about a relative URL.
SCHEMES = frozenset({"http", "https"})


@dataclasses.dataclass(frozen=True)
class Connection:
	"""One instance this client can reach, and how to reach it.

	``url`` is ``None`` for exactly one connection — the local database, opened directly
	rather than over a socket. Every other field means the same thing either way, which is
	what lets the fan-out code below never ask which kind it has.
	"""

	name: str

	#: Where the instance is, or ``None`` for this installation's own database.
	url: str | None = None

	#: What to print when results are grouped, for anyone who wants "Acme" in the output and
	#: ``work`` on the command line.
	display_name: str | None = None

	#: Refuse writes to this connection, client-side. Pointing an agent at a company
	#: instance for context while forbidding it to write there is a reasonable posture and a
	#: common one (§13.7).
	read_only: bool = False

	token_env: str | None = None
	token_command: str | None = None

	timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

	#: Turned off without being deleted, which is the only reason to declare ``local``
	#: besides renaming it.
	enabled: bool = True

	@property
	def is_local (self) -> bool:
		"""Report whether this connection is this installation's own database."""

		return self.url is None

	@property
	def label (self) -> str:
		"""Return what to print for this connection when results are grouped."""

		if self.display_name:
			return self.display_name

		return LOCAL_LABEL if self.is_local else self.name


@dataclasses.dataclass(frozen=True)
class Roster:
	"""Every connection configured, and which one is asked when nothing has said.

	``default`` is the *fallback*, not the answer: a write goes to the current context
	(§13.7), and this only decides matters when no flag, environment variable, marker or
	stored context has chosen one. Saying otherwise here is what made ``subroutine
	connections`` read as an answer to "where do my writes go" (`#278`).
	"""

	connections: tuple[Connection, ...]
	default: str

	def __len__ (self) -> int:
		"""Return how many connections there are."""

		return len(self.connections)

	def __iter__ (self) -> typing.Iterator[Connection]:
		"""Iterate the connections, in the order output should group them."""

		return iter(self.connections)

	@property
	def names (self) -> tuple[str, ...]:
		"""Return every connection name, in roster order."""

		return tuple(connection.name for connection in self.connections)

	@property
	def qualifies (self) -> bool:
		"""Report whether an address here needs to name its connection.

		With one connection there is nothing to disambiguate and no label appears anywhere,
		which is the overwhelmingly common case and the whole of §13.5b's output.
		"""

		return len(self.connections) > 1

	def default_connection (self) -> Connection:
		"""Return the connection a write goes to when nothing said which."""

		return self.require(self.default)

	def find (self, name: str) -> Connection | None:
		"""Return the connection with this name, or ``None``."""

		wanted = name.strip().lower()

		for connection in self.connections:
			if connection.name == wanted:
				return connection

		return None

	def require (self, name: str) -> Connection:
		"""Return the connection with this name, or refuse with the ones that exist."""

		found = self.find(name)

		if found is not None:
			return found

		raise subroutine.errors.NotFound(
			f"There is no connection called {name!r}.",
			hint=self.alternatives(),
		)

	def alternatives (self) -> str:
		"""Describe the connections that do exist, for a refusal."""

		listed = ", ".join(self.names)

		return f"Connections you have configured: {listed}."


def roster (settings: subroutine.config.Settings | None = None) -> Roster:
	"""Read the configured connections, with ``local`` present whether declared or not.

	Ordered with the **default connection first** and the rest as the file declares them, so
	that the group a write would land in leads the output rather than appearing wherever the
	alphabet put it.

	Every refusal here names the key and the file. A configuration mistake is an ordinary
	one, and the person making it is looking at the file.
	"""

	declared = _declared_tables()
	built: dict[str, Connection] = {}

	for name, table in declared.items():
		built[name] = _connection(name, table)

	if LOCAL_NAME not in built:
		built[LOCAL_NAME] = Connection(name=LOCAL_NAME)

	live = [connection for connection in built.values() if connection.enabled]
	default = _default_name(settings, live)
	ordered = sorted(live, key=lambda connection: 0 if connection.name == default else 1)

	return Roster(connections=tuple(ordered), default=default)


def _declared_tables () -> dict[str, dict[str, typing.Any]]:
	"""Return the ``[connections.*]`` tables, keyed by validated name."""

	data = subroutine.config.read_config_file().get("connections")

	if data is None:
		return {}

	if not isinstance(data, dict):
		raise _refusal(
			"connections",
			"'connections' must be a table of connections, each written "
			"'[connections.work]'.",
		)

	tables: dict[str, dict[str, typing.Any]] = {}

	for name, table in data.items():
		validated = _valid_name(name)

		if not isinstance(table, dict):
			raise _refusal(
				f"connections.{name}",
				f"'{name}' must be a table — write '[connections.{name}]' and put its "
				"settings under it.",
			)

		tables[validated] = table

	return tables


def declared_names () -> frozenset[str]:
	"""Return every connection the configuration file declares, enabled or not.

	Not the roster, deliberately: :func:`roster` drops anything with ``enabled = false``, so a
	caller asking "is this name taken" would be told no about a table that is right there in
	the file. Adding a second connection under that name would then produce a file whose
	meaning depends on which of two tables TOML kept.
	"""

	return frozenset(_declared_tables())


def _valid_name (name: str) -> str:
	"""Return a connection name, refusing one that could not be typed in an address."""

	if _NAME.match(name):
		return name

	raise _refusal(
		f"connections.{name}",
		f"{name!r} cannot be a connection name. A name starts with a letter and uses "
		"letters, numbers, hyphens and underscores — it becomes the first part of an "
		"address, as in 'work/acme/42'.",
	)


def check_name (name: str) -> str:
	"""Return a connection name somebody typed, or refuse it in the terms they typed it in.

	The same rule as :func:`_valid_name` and a different refusal, which is the point rather
	than a duplication: that one is read by somebody looking at a file and names the key and
	the line, and this one is read by somebody who has just pressed return. Both match
	``_NAME``, so the rule itself exists once.

	**Lower-cased rather than refused**, because :meth:`Roster.find` already lower-cases
	everything it looks up — so ``Work`` has always resolved to ``work`` everywhere a
	connection is named, and refusing it here would be the one place that did not. It is what a
	project key does with the same reasoning (§5.4): input is case-insensitive, and what is
	stored and printed is one form, so an address is predictable.
	"""

	wanted = name.strip().lower()

	if _NAME.match(wanted):
		return wanted

	raise subroutine.errors.ValidationError(
		f"{name!r} cannot be a connection name.",
		code="invalid_field_value",
		hint="A name starts with a letter and uses letters, numbers, hyphens and "
		"underscores. It becomes the first part of an address, as in 'work/acme/42'.",
	)


def check_url (value: str) -> str:
	"""Return an instance's address as it will be stored, or refuse what was typed."""

	text = _trimmed_url(value)

	if text is not None:
		return text

	raise subroutine.errors.ValidationError(
		f"{value!r} is not an address this can reach.",
		code="invalid_field_value",
		hint="It needs a scheme and a host, as in 'https://tasks.example.com'.",
	)


def in_the_clear (connection: Connection) -> bool:
	"""Report whether reaching this connection puts its token on the network unprotected.

	**The rule ``serve`` already enforces, asked from the other end.** An instance refuses to
	listen beyond its own machine without TLS, in as many words — *"bearer tokens sent over
	plain HTTP are compromised tokens"* — and a *client* would happily be pointed at exactly
	that address, store the token and send it on every request, saying nothing. The README
	states the rule twice; nothing on this side of it ever did.

	Loopback is not in the clear: nothing leaves the machine, which is the same exception
	``serve`` makes and for the same reason. A URL that cannot be parsed is treated as
	exposed, because guessing wrong in that direction only costs a line of output.
	"""

	if connection.url is None:
		return False

	parsed = urllib.parse.urlparse(connection.url)

	if parsed.scheme != "http":
		return False

	return not subroutine.config.is_loopback(parsed.hostname or "")


def _connection (name: str, table: dict[str, typing.Any]) -> Connection:
	"""Build one connection from its configuration table."""

	unknown = sorted(set(table) - KNOWN_KEYS)

	if unknown:
		raise _refusal(
			f"connections.{name}.{unknown[0]}",
			f"{unknown[0]!r} is not something a connection has. A connection takes: "
			f"{', '.join(sorted(KNOWN_KEYS))}.",
		)

	url = _url(name, table.get("url"))

	if name == LOCAL_NAME and url is not None:
		# Silently accepting this would take the person's own database out of the roster
		# while leaving a connection named after it, so their own tasks would stop appearing
		# and the configuration would look like the reason they should.
		raise _refusal(
			"connections.local.url",
			"'local' always means this installation's own database, so it cannot have a "
			"url. Give the remote instance a name of its own — '[connections.work]'.",
		)

	if name != LOCAL_NAME and url is None:
		raise _refusal(
			f"connections.{name}.url",
			f"{name!r} needs a url saying where that instance is, as in "
			"'url = \"https://tasks.example.com\"'.",
		)

	return Connection(
		name=name,
		url=url,
		display_name=_text(name, table, "display_name"),
		read_only=_flag(name, table, "read_only", default=False),
		token_env=_text(name, table, "token_env"),
		token_command=_text(name, table, "token_command"),
		timeout_seconds=_timeout(name, table),
		enabled=_flag(name, table, "enabled", default=True),
	)


def _url (name: str, value: typing.Any) -> str | None:
	"""Return a connection's base URL, or ``None`` when it has none."""

	if value is None:
		return None

	if not isinstance(value, str) or not value.strip():
		raise _refusal(
			f"connections.{name}.url",
			"A url is text, as in 'url = \"https://tasks.example.com\"'.",
		)

	text = _trimmed_url(value)

	if text is None:
		raise _refusal(
			f"connections.{name}.url",
			f"{value!r} is not an address this can reach. It needs a scheme and a host, as "
			"in 'https://tasks.example.com'.",
		)

	return text


def _trimmed_url (value: str) -> str | None:
	"""Return an address in the form a connection stores it, or ``None`` if it is not one.

	The rule itself, with no message attached, because two callers need the same answer and
	different refusals — the file's names a key, the command line's names what was typed.
	"""

	text = value.strip().rstrip("/")
	parsed = urllib.parse.urlsplit(text)

	if parsed.scheme not in SCHEMES or not parsed.netloc:
		return None

	return text


def _text (name: str, table: dict[str, typing.Any], key: str) -> str | None:
	"""Return one text setting from a connection's table, or ``None``."""

	value = table.get(key)

	if value is None:
		return None

	if not isinstance(value, str) or not value.strip():
		raise _refusal(f"connections.{name}.{key}", f"{key!r} must be some text.")

	return value.strip()


def _flag (name: str, table: dict[str, typing.Any], key: str, *, default: bool) -> bool:
	"""Return one true-or-false setting from a connection's table."""

	value = table.get(key)

	if value is None:
		return default

	if not isinstance(value, bool):
		raise _refusal(
			f"connections.{name}.{key}", f"{key!r} is either true or false, without quotes."
		)

	return value


def _timeout (name: str, table: dict[str, typing.Any]) -> float:
	"""Return how long to wait for this connection before giving up on it."""

	value = table.get("timeout_seconds")

	if value is None:
		return DEFAULT_TIMEOUT_SECONDS

	if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
		raise _refusal(
			f"connections.{name}.timeout_seconds",
			"'timeout_seconds' is how many seconds to wait, and must be more than zero.",
		)

	return float(value)


def _default_name (
	settings: subroutine.config.Settings | None, live: typing.Sequence[Connection]
) -> str:
	"""Return which connection a write goes to, refusing a name that is not there.

	A ``default_connection`` naming something absent — a typo, or a connection turned off
	and forgotten — would otherwise send the next ``subroutine add`` somewhere the person
	did not mean, which is the failure this whole module's noisiness exists to prevent.
	"""

	names = {connection.name for connection in live}

	if not names:
		raise _refusal(
			"connections",
			"Every connection is turned off, so there is nowhere to keep anything. Set "
			"'enabled = true' on at least one.",
		)

	wanted = (settings.default_connection if settings is not None else None) or LOCAL_NAME

	if wanted in names:
		return wanted

	if wanted != LOCAL_NAME:
		raise _refusal(
			"default_connection",
			f"'default_connection' is set to {wanted!r}, which is not a connection here. "
			f"Configured: {', '.join(sorted(names))}.",
		)

	# `local` was turned off and nothing named a replacement. Rather than refuse, take the
	# first one the file declares: somebody who disabled their own database has said where
	# their work is, and a hard failure here would make that configuration unusable.
	return live[0].name


def _refusal (field: str, message: str) -> subroutine.errors.SubroutineError:
	"""Return the refusal for a bad connection setting, naming the key and the file."""

	return subroutine.errors.ValidationError(
		f"{subroutine.config.config_file_path()} cannot be used: {message}",
		code="invalid_field_value",
		errors=[
			subroutine.errors.FieldError(
				field=field, code="invalid_field_value", message=message
			)
		],
		hint="Fix the file, or run 'subroutine connections' once it parses to see what "
		"this reads.",
	)
