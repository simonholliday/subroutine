"""Where Subroutine keeps its files, and how its settings are resolved.

Two precedence chains exist and are deliberately kept apart (SPEC.md §12.3):

* **Process configuration** — where the database is, what port to listen on, which key
  signs things. Resolved here, in the order: command-line flag, then environment
  variable, then the configuration file, then the built-in default.
* **Behavioural settings** — how long the trash is kept, whether completing a task needs
  evidence. Those are resolved per project, then per workspace, and only fall back to
  the values here as an installation-wide default. This module holds the last link in
  that chain, never the whole of it.
"""

import contextlib
import os
import pathlib
import re
import secrets
import sqlite3
import tempfile
import tomllib
import typing

import pydantic
import pydantic_settings

APPLICATION_NAME = "subroutine"

#: Filesystems on which SQLite cannot reliably take a lock. WAL mode needs a shared
#: memory region that these do not provide, and the failure reads as `database is
#: locked` on the first write, which looks like a concurrency bug rather than a storage
#: one. The list is a courtesy for a better error message; `probe_sqlite_locking` is the
#: definitive check.
NETWORK_FILESYSTEMS = frozenset(
	{"cifs", "smbfs", "smb3", "nfs", "nfs4", "ncpfs", "afs", "fuse.sshfs", "fuse.davfs"}
)


#: The environment variable naming the active instance (SPEC.md §12.5). Read on every path
#: lookup rather than captured once, so a test or a subprocess can change instance without
#: reloading the module.
PROFILE_VARIABLE = "SUBROUTINE_PROFILE"

#: The directory level a profile inserts under each XDG root. A literal rather than part of
#: the name, so the default instance's paths are untouched and nobody is migrated.
PROFILES_DIRECTORY = "profiles"

#: A profile name must be a safe single path segment: a letter first, then letters, digits,
#: hyphens and underscores. Same shape as a workspace short name (SPEC.md §13.7) and for the
#: same reason — a name that is all digits, or that carries a separator, stops being a name
#: and becomes a path.
_PROFILE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

#: Long enough for any sensible name, short enough that the resulting paths stay printable.
MAX_PROFILE_NAME_LENGTH = 32


def _application_directory (variable: str, *fallback: str) -> pathlib.Path:
	"""Return this application's directory under one XDG root, ignoring any profile.

	The unprofiled form, which is what enumerating and deleting profiles needs: asking a
	profiled path where the profiles live would nest one inside another.
	"""

	base = os.environ.get(variable) or pathlib.Path.home().joinpath(*fallback)

	return pathlib.Path(base) / APPLICATION_NAME


def check_profile_name (name: str) -> str:
	"""Return ``name`` if it is a usable profile name, and explain the rule if it is not."""

	cleaned = name.strip()

	if not _PROFILE_NAME.match(cleaned) or len(cleaned) > MAX_PROFILE_NAME_LENGTH:
		raise ValueError(
			f"'{name}' is not a usable instance name. Start with a letter, then use "
			f"letters, digits, hyphens or underscores, up to {MAX_PROFILE_NAME_LENGTH} "
			f"characters."
		)

	return cleaned


def profile () -> str | None:
	"""Return the name of the instance this process is acting on, or ``None`` for the default.

	``None`` is not a fallback for a broken value — a name that cannot be used raises, because
	continuing would silently act on the *default* instance, which is the one holding real
	work (SPEC.md §12.5).
	"""

	name = os.environ.get(PROFILE_VARIABLE, "").strip()

	if not name:
		return None

	return check_profile_name(name)


def use_profile (name: str | None) -> None:
	"""Make every path lookup in this process refer to ``name``, or to the default instance.

	Called once, early, by the ``--profile`` option. Set through the environment rather than a
	module global so that anything this process starts inherits the same instance.
	"""

	if name is None:
		os.environ.pop(PROFILE_VARIABLE, None)

		return

	os.environ[PROFILE_VARIABLE] = check_profile_name(name)


def _within_profile (root: pathlib.Path) -> pathlib.Path:
	"""Return ``root`` itself for the default instance, or its profile subdirectory."""

	name = profile()

	return root if name is None else root / PROFILES_DIRECTORY / name


def profile_directories (name: str) -> tuple[pathlib.Path, ...]:
	"""Return every directory one profile owns, across the three XDG roots.

	The whole of an instance, which is what creating and destroying one has to act on. The
	tuple is ordered configuration, data, state — deleting the database last means an
	interrupted removal leaves something recognisable rather than orphaned data.
	"""

	checked = check_profile_name(name)

	return tuple(
		root / PROFILES_DIRECTORY / checked
		for root in (
			_application_directory("XDG_CONFIG_HOME", ".config"),
			_application_directory("XDG_STATE_HOME", ".local", "state"),
			_application_directory("XDG_DATA_HOME", ".local", "share"),
		)
	)


def profile_names () -> list[str]:
	"""Return every profile that has a configuration directory, alphabetically.

	Read from the configuration root rather than the data root: a profile is created by
	``init`` writing a `config.toml`, so that is the directory whose presence means the
	instance exists at all.
	"""

	directory = _application_directory("XDG_CONFIG_HOME", ".config") / PROFILES_DIRECTORY

	if not directory.is_dir():
		return []

	return sorted(
		entry.name
		for entry in directory.iterdir()
		if entry.is_dir() and _PROFILE_NAME.match(entry.name)
	)


def config_home () -> pathlib.Path:
	"""Return the directory holding the configuration file."""

	return _within_profile(_application_directory("XDG_CONFIG_HOME", ".config"))


def data_home () -> pathlib.Path:
	"""Return the directory holding the database and other durable state."""

	return _within_profile(_application_directory("XDG_DATA_HOME", ".local", "share"))


def state_home () -> pathlib.Path:
	"""Return the directory holding state that is useful to keep but safe to lose.

	The current context lives here — which connection and which workspace a bare number
	means (SPEC.md §13.7). It is deliberately not in the data directory, because XDG's own
	description of ``STATE_HOME`` fits it exactly: state that should persist between
	restarts but is not important enough for the data directory.

	**The test that keeps it safe to lose: losing this file must degrade to a question,
	never to a different outcome.** It holds only which workspace is current, and every ref
	stays absolute within one — so a missing file makes ``subroutine done 42`` ask which
	``42``. That is what distinguishes it from the *other* file that lived here, the
	number-to-item map deleted in §12.2a, whose loss silently changed what an identifier
	meant.
	"""

	return _within_profile(_application_directory("XDG_STATE_HOME", ".local", "state"))


def config_file_path () -> pathlib.Path:
	"""Return the path of the configuration file, whether or not it exists."""

	return config_home() / "config.toml"


def default_database_path () -> pathlib.Path:
	"""Return the default location of the SQLite database.

	This lives under the XDG data directory rather than beside the project, so that it
	survives working-directory changes and is on local storage on a normal installation.
	"""

	return data_home() / f"{APPLICATION_NAME}.db"


def default_database_url () -> str:
	"""Return the default database URL, pointing at local SQLite storage."""

	return f"sqlite:///{default_database_path()}"


def system_timezone () -> str:
	"""Return the machine's IANA timezone name, falling back to UTC.

	Python has no portable way to ask for this, so the two usual Linux sources are tried
	in turn: the plain-text ``/etc/timezone``, then the target of the ``/etc/localtime``
	symlink.
	"""

	timezone_file = pathlib.Path("/etc/timezone")

	if timezone_file.is_file():
		name = timezone_file.read_text(encoding="utf-8").strip()

		if name:
			return name

	localtime = pathlib.Path("/etc/localtime")

	if localtime.is_symlink():
		target = str(localtime.resolve())
		marker = "/zoneinfo/"

		if marker in target:
			return target.split(marker, 1)[1]

	return "UTC"


def filesystem_type (path: pathlib.Path) -> str | None:
	"""Report the filesystem type holding ``path``, or ``None`` if it cannot be told.

	Walks up to the nearest existing ancestor, so an as-yet-uncreated database file can
	still be checked.
	"""

	mounts = pathlib.Path("/proc/mounts")

	if not mounts.is_file():
		return None

	target = path.absolute()

	while not target.exists() and target != target.parent:
		target = target.parent

	best_point = ""
	best_type = None

	for line in mounts.read_text(encoding="utf-8").splitlines():
		fields = line.split()

		if len(fields) < 3:
			continue

		point, fs_type = fields[1], fields[2]

		# Longest matching mount point wins, since mounts nest.
		if (str(target) == point or str(target).startswith(point.rstrip("/") + "/")) and len(
			point
		) >= len(best_point):
			best_point, best_type = point, fs_type

	return best_type


def is_network_filesystem (path: pathlib.Path) -> bool:
	"""Report whether ``path`` sits on a filesystem known to break SQLite locking."""

	fs_type = filesystem_type(path)

	return fs_type is not None and fs_type in NETWORK_FILESYSTEMS


def probe_sqlite_locking (directory: pathlib.Path) -> str | None:
	"""Check that SQLite can actually lock and use WAL mode in ``directory``.

	Returns ``None`` when everything works, or a sentence explaining what went wrong that
	is suitable for showing to a user. This is the definitive test — a filesystem we have
	never heard of still gets caught.
	"""

	try:
		directory.mkdir(parents=True, exist_ok=True)
		handle, probe_name = tempfile.mkstemp(
			prefix=".subroutine-probe-", suffix=".db", dir=directory
		)
		os.close(handle)

	except OSError as error:
		fs_type = filesystem_type(directory) or "unknown"

		return (
			f"Cannot create a database file in {directory} ({fs_type} filesystem): "
			f"{error}. Choose a directory you can write to, or use PostgreSQL."
		)

	probe = pathlib.Path(probe_name)

	try:
		connection = sqlite3.connect(probe)

		try:
			connection.execute("PRAGMA journal_mode=WAL")
			connection.execute("CREATE TABLE probe (value INTEGER)")
			connection.execute("INSERT INTO probe VALUES (1)")
			connection.commit()

			mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()

		finally:
			connection.close()

	except sqlite3.Error as error:
		fs_type = filesystem_type(directory) or "unknown"

		return (
			f"SQLite cannot write to {directory} ({fs_type} filesystem): {error}. "
			f"Network filesystems such as SMB and NFS do not support the locking SQLite "
			f"needs. Choose a directory on local disk, or use PostgreSQL."
		)

	finally:
		for suffix in ("", "-wal", "-shm"):
			pathlib.Path(str(probe) + suffix).unlink(missing_ok=True)

	if mode != "wal":
		fs_type = filesystem_type(directory) or "unknown"

		return (
			f"SQLite fell back to '{mode}' journalling in {directory} ({fs_type} "
			f"filesystem) instead of WAL. Concurrent reads will block. Choose a "
			f"directory on local disk, or use PostgreSQL."
		)

	return None


def read_config_file () -> dict[str, typing.Any]:
	"""Read the configuration file, returning an empty mapping when there is none."""

	path = config_file_path()

	if not path.is_file():
		return {}

	with path.open("rb") as handle:
		return tomllib.load(handle)


class TomlSettingsSource(pydantic_settings.PydanticBaseSettingsSource):
	"""Feed settings from ``config.toml`` into the resolution chain."""

	def get_field_value (
		self, field: pydantic.fields.FieldInfo, field_name: str
	) -> tuple[typing.Any, str, bool]:
		"""Return one field's value as read from the configuration file."""

		return read_config_file().get(field_name), field_name, False

	def __call__ (self) -> dict[str, typing.Any]:
		"""Return every setting present in the configuration file."""

		data = read_config_file()

		return {key: value for key, value in data.items() if value is not None}


class Settings(pydantic_settings.BaseSettings):
	"""Process configuration, resolved from flags, environment, file, then defaults."""

	model_config = pydantic_settings.SettingsConfigDict(
		env_prefix="SUBROUTINE_",
		extra="ignore",
		validate_default=True,
	)

	database_url: str = pydantic.Field(default_factory=default_database_url)

	# Where this instance's source can be obtained. **A product requirement, not a legal
	# footnote** (SPEC.md §2.2): the AGPL's network clause means a *served* instance must
	# offer its source to the people using it, so `/v1/meta` publishes this and any future
	# web UI carries it in a footer. It lives in configuration rather than in the database
	# because it describes a deployment — somebody running a modified fork must be able to
	# point at *their* source, and will be wrong to point at this one.
	source_url: str = "https://github.com/simonholliday/subroutine"

	host: str = "127.0.0.1"
	port: int = 8471

	# Marks this instance as one whose data matters (SPEC.md §12.5). A protected instance
	# refuses `db restore`, `db upgrade` and its own deletion unless the operator confirms or
	# passes `--yes`. It is a property of the *instance* rather than of the command on
	# purpose: the thing worth protecting is a particular database, and a flag on the command
	# only protects whoever remembers to type it.
	protected: bool = False

	# Where `db backup` writes (SPEC.md §12.6b). Unset means the instance's own data directory,
	# which is right for one laptop and wrong as soon as the point of a backup is surviving the
	# disk it is on. A network volume is a good destination and a **bad** place for the database
	# itself — so this is a separate setting rather than a directory beside `database_url`.
	backup_directory: str | None = None

	# The https:// address a TLS-terminating proxy serves this instance on. Unset is the
	# ordinary case — one person on a laptop, listening on loopback. Setting it is what makes
	# a non-loopback bind something `serve` will agree to (SPEC.md §12.4).
	public_url: str | None = None

	# Which connection a write goes to when the command did not say (SPEC.md §13.7). The
	# connections themselves are tables rather than settings, and live in
	# `subroutine.connections`; this is the one scalar among them.
	default_connection: str = "local"

	secret_key: str | None = None
	dev_mode: bool = False
	log_level: str = "INFO"
	cors_origins: list[str] = pydantic.Field(default_factory=list)
	default_timezone: str = pydantic.Field(default_factory=system_timezone)

	# Which account local mode acts as, when the database holds more than one (SPEC.md
	# §12.1a). Unset is the ordinary case: with a single user there is nothing to choose,
	# and with several, guessing whose to-do list is on screen is not an error that
	# announces itself.
	local_user: str | None = None

	# Installation-wide defaults for behavioural settings. A workspace or a project may
	# override any of these; see the module docstring.
	trash_retention_days: int = 30
	events_retention_days: int = 180
	default_page_size: int = 50
	max_page_size: int = 200
	claim_lease_minutes: int = 30
	require_verification_to_complete: bool = False

	# Bounds how deep a project or subtask tree may nest, and with it the length of a
	# materialised path and the cost of a move (SPEC.md §5.4).
	max_hierarchy_depth: int = 10

	@classmethod
	def settings_customise_sources (
		cls,
		settings_cls: type[pydantic_settings.BaseSettings],
		init_settings: pydantic_settings.PydanticBaseSettingsSource,
		env_settings: pydantic_settings.PydanticBaseSettingsSource,
		dotenv_settings: pydantic_settings.PydanticBaseSettingsSource,
		file_secret_settings: pydantic_settings.PydanticBaseSettingsSource,
	) -> tuple[pydantic_settings.PydanticBaseSettingsSource, ...]:
		"""Order the sources: explicit arguments, environment, file, then defaults."""

		return (init_settings, env_settings, TomlSettingsSource(settings_cls))

	@property
	def is_sqlite (self) -> bool:
		"""Report whether the configured database is SQLite."""

		return self.database_url.startswith("sqlite")

	@property
	def sqlite_path (self) -> pathlib.Path | None:
		"""Return the SQLite file path, or ``None`` for any other backend."""

		if not self.is_sqlite:
			return None

		_, _, remainder = self.database_url.partition("///")

		return pathlib.Path(remainder) if remainder else None

	def require_secret_key (self) -> str:
		"""Return the signing key, refusing to run without one outside development.

		The key signs pagination cursors. It deliberately does *not* pepper stored token
		hashes (SPEC.md §7.4), so rotating it costs an in-flight page of results rather
		than every credential in the installation. Starting with a generated-per-process
		key would still break cursors on every restart, so this fails loudly instead.
		"""

		if self.secret_key:
			return self.secret_key

		if self.dev_mode:
			# Deterministic within a process, and never written down. Development only.
			return "development-only-key"

		raise RuntimeError(
			"No secret_key is configured. Run 'subroutine init' to create one, set "
			"SUBROUTINE_SECRET_KEY, or pass --dev-mode for local development."
		)


def setting_sources (settings: Settings) -> dict[str, str]:
	"""Report where each setting's value came from, for ``subroutine config show``.

	Users debugging a surprising value need to know which of four places supplied it, and
	guessing is the slowest possible way to find out.
	"""

	file_data = read_config_file()
	sources: dict[str, str] = {}

	for name in type(settings).model_fields:
		if os.environ.get(f"SUBROUTINE_{name.upper()}") is not None:
			sources[name] = "environment"

		elif name in file_data:
			sources[name] = str(config_file_path())

		else:
			sources[name] = "default"

	return sources


def generate_secret_key () -> str:
	"""Return a fresh signing key for a new installation."""

	return secrets.token_urlsafe(32)


def store_setting (name: str, value: str) -> pathlib.Path:
	"""Record one setting in the configuration file, and return where it was written.

	Rewrites the line in place when the setting is already there, and otherwise inserts it
	*before the first table header* — because everything this program reads is a top-level
	key, and text appended after a ``[table]`` header belongs to that table, not to the
	document. Getting either wrong produces a file TOML cannot parse, after which every
	command fails, including the ``config show`` that error messages recommend.

	Comments and ordering are preserved everywhere else. A configuration file belongs to
	whoever edits it, and silently reformatting one is not a thing a program should do to a
	file it does not own.
	"""

	path = config_file_path()
	path.parent.mkdir(parents=True, exist_ok=True)

	line = f"{name} = {_toml_string(value)}"

	if not path.exists():
		_write_private(path, f"# Subroutine configuration. See 'subroutine config show'.\n{line}\n")

		return path

	lines = path.read_text(encoding="utf-8").splitlines()
	assignment = re.compile(rf"^\s*{re.escape(name)}\s*=")
	table = re.compile(r"^\s*\[")
	replaced = False

	for index, existing in enumerate(lines):
		if table.match(existing):
			# Past this point the key would belong to a table rather than the document.
			break

		if assignment.match(existing):
			lines[index] = line
			replaced = True
			break

	if not replaced:
		insert_at = next((i for i, text in enumerate(lines) if table.match(text)), len(lines))
		lines.insert(insert_at, line)

	_write_private(path, "\n".join(lines) + "\n")

	return path


def _toml_string (value: str) -> str:
	"""Return a value as a TOML basic string, escaped."""

	escaped = (
		value.replace("\\", "\\\\")
		.replace('"', '\\"')
		.replace("\n", "\\n")
		.replace("\r", "\\r")
		.replace("\t", "\\t")
	)

	return f'"{escaped}"'


def _write_private (path: pathlib.Path, text: str) -> None:
	"""Write the configuration file, keeping it readable only by its owner.

	The permissions are reasserted on every write, not only on creation: the signing key
	can be added to a file the user made earlier to set a port or a database URL, and that
	file will have been created with their default umask.
	"""

	path.write_text(text, encoding="utf-8")

	# Some filesystems — the CIFS share this project is developed on among them — do not
	# carry POSIX modes. Refusing to write the config over that would be worse than
	# writing it without the tightened permissions.
	with contextlib.suppress(OSError):
		path.chmod(0o600)


def ensure_secret_key (settings: Settings) -> tuple[str, pathlib.Path | None]:
	"""Return the signing key, generating and storing one if there is none.

	Returns the key and where it was written, or ``None`` for the path when one already
	existed. Generating a fresh key on every start would invalidate every pagination
	cursor on every restart, which is why this is written down rather than held in memory.
	"""

	if settings.secret_key:
		return settings.secret_key, None

	key = generate_secret_key()

	return key, store_setting("secret_key", key)


def load_settings (**overrides: typing.Any) -> Settings:
	"""Resolve settings, with any keyword arguments taking highest precedence."""

	return Settings(**overrides)
