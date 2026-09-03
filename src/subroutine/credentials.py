"""Where the tokens are — docs/design.md §12.3a.

Two files, on the SSH model, because they have different lifetimes and different audiences:

===================================================  ======  =====================================
``$XDG_CONFIG_HOME/subroutine/config.toml``          0600    connections, urls, defaults, and
                                                             ``secret_key``. **No tokens.**
``$XDG_CONFIG_HOME/subroutine/credentials.toml``     0600    the tokens for each connection
                                                             name: the person's, and the
                                                             agent's (`#1449`).
===================================================  ======  =====================================

**Both are 0600, and this table said ``config.toml`` was 0644 and held no secrets until `#831`.**
Neither half was true: :func:`subroutine.config._write_private` has written 0600 deliberately
since `#205`, and ``ensure_secret_key`` puts ``secret_key`` in there, which ``init`` always does.
The correct sentence already existed in ``docs/hosting.md`` — *"it is not ``secret_key``, which
is the only thing in ``config.toml`` that looks like a credential"* — so this was two places
stating one rule and disagreeing, in the module whose whole subject is where secrets live.

The split still earns its keep, and the reason survives the correction with one word changed:
``config.toml`` is the file you can **diff, sync between machines and reason about**, and
``credentials.toml`` is the one that never leaves the machine. What is no longer true is that
you could paste it into a support thread — ``secret_key`` is in it. A single combined file —
which an earlier draft specified — would still be wrong, because the two have different
lifetimes: a connection outlives every token issued for it.

``secret_key`` signs pagination cursors and nothing else (§7.4), so what it costs to leak is
bounded and small. That is a reason not to panic about it, and not a reason to describe the file
as holding nothing.

**A token is never written to ``config.toml``, never passed as a command-line argument** (it
would land in ``ps`` output and shell history) **and never accepted in a query string**
(§7.4).

There is no keyring integration, deliberately. Depending on ``keyring`` drags in D-Bus and
Secret Service, which are absent or broken on exactly the headless servers and containers
where a work instance actually runs, and it prompts at moments nobody predicted. One line of
subprocess gets ``pass``, ``gpg``, ``secret-tool``, 1Password and anything else a person has
already chosen — which is what ``ssh`` does too, and why ``ssh`` works the same everywhere.
"""

import contextlib
import dataclasses
import os
import pathlib
import re
import shlex
import stat
import subprocess
import tomllib
import typing

import subroutine.config
import subroutine.connections
import subroutine.errors

#: How long to wait for a ``token_command`` before giving up. A credential helper that
#: prompts for a passphrase needs longer than a network call, and shorter than forever.
COMMAND_TIMEOUT_SECONDS = 30.0

#: The variable that supplies the *default* connection's token, for the common case of one
#: instance and one exported secret.
DEFAULT_VARIABLE = "SUBROUTINE_TOKEN"

#: Non-alphanumerics become underscores, so ``[connections.my-work]`` is
#: ``SUBROUTINE_TOKEN_MY_WORK``. A hyphen cannot appear in a shell variable name, and a
#: person who has to discover that by trial is a person who gives up.
_UNSAFE = re.compile(r"[^A-Za-z0-9]")


@dataclasses.dataclass(frozen=True, repr=False)
class Stored:
	"""What the credentials file holds for one connection: a person's token, and an agent's.

	**Two tokens under one connection, rather than two connections** — `#1449`. A second
	connection naming the same instance is what `#327` and `#337` measured and refused: the
	merged read refuses a duplicate instance, so ``subroutine today`` stops working while
	``list``, ``show`` and ``whoami`` go on answering.

	Either may be absent. A machine where only agents work legitimately holds the agent's alone,
	which is the arrangement ``docs/hosting.md`` already recommends where it fits.
	"""

	token: str | None = None
	agent_token: str | None = None

	def __repr__ (self) -> str:
		"""Describe this without either secret in it, for the reason :class:`Resolved` does."""

		return (
			f"Stored(token={'<set>' if self.token else None}, "
			f"agent_token={'<set>' if self.agent_token else None})"
		)


@dataclasses.dataclass(frozen=True, repr=False)
class Resolved:
	"""One connection's token, and which of the four places supplied it.

	``source`` exists for ``subroutine connections``, which reports where each token came from
	*without printing any of them*. The standing footgun in comparable tooling is not having
	several sources but not knowing which one won.
	"""

	token: str | None
	source: str

	#: Whether this is the connection's *agent* credential, chosen because the process asking
	#: was started by an agent. A flag rather than something read back out of ``source``,
	#: because a caller matching on that string would be reading a sentence written for a
	#: person and would break the day the wording improved.
	by_agent: bool = False

	@property
	def found (self) -> bool:
		"""Report whether there is a token at all."""

		return self.token is not None

	def __repr__ (self) -> str:
		"""Describe this without the secret in it.

		The generated ``__repr__`` printed the token. Nothing called it, which is exactly why it
		was worth removing: a live local in two ``opened`` functions reaches any
		traceback-with-locals renderer, ``pytest -l``, or a debug log — and this module's whole
		purpose is that the token is never written down anywhere.
		"""

		return (
			f"Resolved(token={'<set>' if self.found else None}, source={self.source!r}, "
			f"by_agent={self.by_agent})"
		)


def credentials_file_path () -> pathlib.Path:
	"""Return the path of the credentials file, whether or not it exists."""

	return subroutine.config.config_home() / "credentials.toml"


def variable_for (name: str) -> str:
	"""Return the environment variable that names one connection's token."""

	return f"{DEFAULT_VARIABLE}_{_UNSAFE.sub('_', name).upper()}"


def resolve (
	connection: subroutine.connections.Connection,
	*,
	default_connection: str,
	describe_only: bool = False,
) -> Resolved:
	"""Find one connection's token, in §12.3a's order, first hit winning.

	1. ``SUBROUTINE_TOKEN_<NAME>`` in the environment — ``<NAME>`` upper-cased, with
	   non-alphanumerics as underscores. ``SUBROUTINE_TOKEN`` alone applies to the default
	   connection.
	2. ``token_env`` on the connection, naming a variable explicitly.
	3. ``token_command`` on the connection: a command whose standard output is the token.
	4. The connection's entry in ``credentials.toml`` — the **agent's** token where one is
	   stored and the connection's ``agent_when`` variable is set in this process's
	   environment, and the ordinary one otherwise.

	**Step 4 is where an agent and the person at one machine stop sharing a name** (`#1449`).
	They are the same account, in the same directory, reading the same two files; the only thing
	that differs is the environment each process was started in, so that is what is asked. It
	sits *below* every explicit source deliberately: a variable somebody set by hand still wins,
	which keeps `#1455` true and makes removing the second token a complete undo.

	``describe_only`` answers *where* the token would come from without fetching it, which is
	all ``subroutine connections`` needs. Without it, listing connections spawned every
	``token_command`` — ``pass show``, ``gpg`` — purely to build a string it already had the
	ingredients for: an informational, read-only command that could prompt for a passphrase and
	block for thirty seconds per connection.

	Returns a :class:`Resolved` with ``token=None`` when there is none anywhere, rather than
	refusing. **The local connection legitimately has no token** — the filesystem permission
	on the database is the authentication (§12.1a) — so "no token" is an ordinary answer here
	and only the caller knows whether it is a problem.
	"""

	specific = os.environ.get(variable_for(connection.name))

	if specific:
		return Resolved(token=specific, source=variable_for(connection.name))

	# **Required, not defaulted.** It fell back to `"local"`, which is wrong exactly when
	# `local` is not the default — and `connections._default_name` returns the first declared
	# connection whenever `local` is turned off. The bare `SUBROUTINE_TOKEN` would then have
	# been offered to the local database instead of to the remote it was set for.
	if connection.name == default_connection:
		general = os.environ.get(DEFAULT_VARIABLE)

		if general:
			return Resolved(token=general, source=DEFAULT_VARIABLE)

	if connection.token_env is not None:
		named = os.environ.get(connection.token_env)

		if named:
			return Resolved(token=named, source=f"{connection.token_env} (token_env)")

		# Naming a variable that is not set is a mistake worth reporting rather than falling
		# through to the file: the person has said where the token is.
		raise subroutine.errors.Unauthenticated(
			f"Connection {connection.name!r} reads its token from {connection.token_env}, "
			"which is not set.",
			hint=f"Export {connection.token_env}, or change 'token_env' in "
			f"{subroutine.config.config_file_path()}.",
		)

	if connection.token_command is not None:
		return Resolved(
			token=None if describe_only else _from_command(connection),
			source=f"{connection.token_command!r} (token_command)",
		)

	return _from_file(connection)


def _from_file (connection: subroutine.connections.Connection) -> Resolved:
	"""Return the stored token for this connection, choosing by who is asking — `#1449`.

	**The condition is the presence of a variable, never its contents.** Reading a value would
	make this a fifth place a token can live; reading only whether it is set keeps it a question
	about the process, which is the one fact that separates the agent from the person here.

	**Attribution, not authority** (`#1426`). Setting the variable by hand selects the *narrower*
	credential — an agent's is bounded and the operator's is not — so there is nothing to gain by
	claiming to be the agent, and the token still carries whatever it carries.

	The source says which half answered and why. A credential chosen by an invisible condition is
	the defect this exists to fix, and it must not be fixed by adding a second invisible one.
	"""

	stored = read_file().get(connection.name)

	if stored is None:
		return Resolved(token=None, source="nowhere")

	path = credentials_file_path()
	variable = connection.agent_variable

	if stored.agent_token and os.environ.get(variable):
		return Resolved(
			token=stored.agent_token,
			source=f"{path} (agent, {variable} is set)",
			by_agent=True,
		)

	if stored.token:
		return Resolved(token=stored.token, source=str(path))

	# Reached where the file holds an agent's token alone and the person is asking. "nowhere"
	# is the honest answer: there is no token *for them*, and saying the file supplied one would
	# send somebody to read a line that is not about them.
	return Resolved(token=None, source="nowhere")


def _from_command (connection: subroutine.connections.Connection) -> str:
	"""Run a connection's credential helper and return what it printed.

	Only the first line is taken, and it is stripped: ``pass show`` prints the secret on line
	one and may print anything at all after it, and a token with a trailing newline
	authenticates as a different token — which fails as ``401`` and looks like a revoked
	credential rather than a whitespace bug.
	"""

	command = typing.cast(str, connection.token_command)

	try:
		completed = subprocess.run(
			shlex.split(command),
			capture_output=True,
			text=True,
			timeout=COMMAND_TIMEOUT_SECONDS,
			check=False,
		)

	except FileNotFoundError:
		raise subroutine.errors.Unauthenticated(
			f"Connection {connection.name!r} gets its token from {command!r}, and that "
			"command is not installed.",
			hint=f"Install it, or change 'token_command' in "
			f"{subroutine.config.config_file_path()}.",
		) from None

	except subprocess.TimeoutExpired:
		raise subroutine.errors.Unauthenticated(
			f"Connection {connection.name!r} gets its token from {command!r}, and that "
			f"command did not finish within {COMMAND_TIMEOUT_SECONDS:.0f} seconds.",
			hint="A credential helper that waits for input will not work here. Try running "
			"it by hand.",
		) from None

	except OSError as error:
		raise subroutine.errors.Unauthenticated(
			f"Connection {connection.name!r} gets its token from {command!r}, which could "
			f"not be run: {error}.",
			hint=f"Check 'token_command' in {subroutine.config.config_file_path()}.",
		) from None

	if completed.returncode != 0:
		# The helper's own message is the useful part — "gpg: decryption failed" says what
		# to do, and "the command failed" does not. Its *output* is not printed, because that
		# is where the secret would be on success.
		detail = completed.stderr.strip().splitlines()
		said = detail[-1] if detail else f"it exited with status {completed.returncode}"

		raise subroutine.errors.Unauthenticated(
			f"Connection {connection.name!r} gets its token from {command!r}, which failed: "
			f"{said}",
			hint="Try running that command by hand.",
		)

	token = completed.stdout.strip().splitlines()

	if not token or not token[0].strip():
		raise subroutine.errors.Unauthenticated(
			f"Connection {connection.name!r} gets its token from {command!r}, which "
			"succeeded but printed nothing.",
			hint="Check that the command prints the token on its first line.",
		)

	return token[0].strip()


def read_file () -> dict[str, Stored]:
	"""Return what the file holds for each connection, keyed by connection name.

	A file that cannot be parsed is reported rather than treated as empty. Treating it as
	empty would present as "401 unauthorized" against every remote at once, which is a long
	way from "line 7 of credentials.toml".
	"""

	path = credentials_file_path()

	if not path.is_file():
		return {}

	try:
		with path.open("rb") as handle:
			data = tomllib.load(handle)

	except tomllib.TOMLDecodeError as error:
		raise subroutine.errors.ValidationError(
			f"{path} is not valid TOML: {error}",
			code="invalid_field_value",
			hint="Each connection is a table with one key — '[work]' then "
			'\'token = "sr_…"\'.',
		) from None

	except OSError as error:
		raise subroutine.errors.ValidationError(
			f"{path} could not be read: {error}",
			code="invalid_field_value",
			hint="Check the file's ownership and permissions.",
		) from None

	held: dict[str, Stored] = {}

	for name, table in data.items():
		if not isinstance(table, dict):
			continue

		entry = Stored(token=_token_in(table, "token"), agent_token=_token_in(table, "agent_token"))

		# A table holding neither is not an entry. Keeping it would make `store` write a bare
		# `[name]` header back, which reads as a connection whose token somebody removed.
		if entry.token or entry.agent_token:
			held[name] = entry

	return held


def _token_in (table: dict[str, typing.Any], key: str) -> str | None:
	"""Return one token from a connection's table, or ``None`` where there is not one."""

	value = table.get(key)

	if not isinstance(value, str) or not value.strip():
		return None

	return value.strip()


def store (name: str, token: str, *, agent: bool = False) -> pathlib.Path:
	"""Record one connection's token, and return where it was written.

	``agent`` writes the credential an agent's own process will resolve, leaving the person's
	untouched — and the other way round, which is what makes setting up an agent on a machine
	somebody already works on a safe act rather than one that takes their own list away.

	The whole file is rewritten from the tokens it held, which loses any comments in it —
	the one place in this program where that is the right trade. A comment in a secrets file
	is worth less than the certainty that the file contains exactly the four tokens the
	program will read back, and rewriting is what lets the mode be reasserted on every write
	rather than only on creation.
	"""

	path = credentials_file_path()
	path.parent.mkdir(parents=True, exist_ok=True)

	held = read_file() if path.is_file() else {}
	existing = held.get(name, Stored())
	secret = token.strip()

	held[name] = (
		dataclasses.replace(existing, agent_token=secret)
		if agent
		else dataclasses.replace(existing, token=secret)
	)

	lines = [
		"# Subroutine credentials. Keep this file private and out of any repository.",
		"# See 'subroutine connections' for which token each connection is using.",
	]

	for key in sorted(held):
		entry = held[key]
		lines.extend(("", f"[{key}]"))

		if entry.token:
			lines.append(f'token = "{_escaped(entry.token)}"')

		if entry.agent_token:
			lines.append(f'agent_token = "{_escaped(entry.agent_token)}"')

	# Created 0600 *before* anything is written, so the secret is never briefly readable by
	# anyone else. Writing and then tightening leaves a window, and it is exactly the kind of
	# window that is invisible on a single-user laptop and real on a shared server.
	with contextlib.suppress(OSError):
		path.touch(mode=0o600, exist_ok=True)
		path.chmod(0o600)

	path.write_text("\n".join(lines) + "\n", encoding="utf-8")

	return path


def _escaped (value: str) -> str:
	"""Return a token as the contents of a TOML basic string."""

	return value.replace("\\", "\\\\").replace('"', '\\"')


def permission_warning () -> str | None:
	"""Return a warning if the credentials file is readable by anyone else.

	``ssh`` refuses a private key with loose permissions outright. This warns instead,
	because the consequence of refusing is that a person cannot see their own to-do list —
	and their tasks are not their SSH key.

	**Reported by every command that opens a connection**, which is what this sentence said
	and was not. There was one caller — ``subroutine connections`` — and §1.4 hides that from
	``--help`` until a second connection exists, so the promise reached nobody who had not
	already gone looking. ``cli/personal._warn_about_the_credentials_file`` is where it is
	said now, once per invocation.
	"""

	path = credentials_file_path()

	if not path.is_file():
		return None

	try:
		mode = path.stat().st_mode

	except OSError:
		return None

	loose = stat.S_IMODE(mode) & (stat.S_IRWXG | stat.S_IRWXO)

	if not loose:
		return None

	# Some filesystems — the CIFS share this project is developed on among them — report a
	# mode they do not enforce and cannot change, so this cannot be an error.
	return (
		f"{path} is readable by other users (mode {stat.S_IMODE(mode):04o}). "
		f"Run 'chmod 600 {path}'."
	)
