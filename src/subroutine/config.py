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

import os
import pathlib
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


def config_home () -> pathlib.Path:
	"""Return the directory holding the configuration file."""

	base = os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config"

	return pathlib.Path(base) / APPLICATION_NAME


def data_home () -> pathlib.Path:
	"""Return the directory holding the database and other durable state."""

	base = os.environ.get("XDG_DATA_HOME") or pathlib.Path.home() / ".local" / "share"

	return pathlib.Path(base) / APPLICATION_NAME


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


def _read_config_file () -> dict[str, typing.Any]:
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

		return _read_config_file().get(field_name), field_name, False

	def __call__ (self) -> dict[str, typing.Any]:
		"""Return every setting present in the configuration file."""

		data = _read_config_file()

		return {key: value for key, value in data.items() if value is not None}


class Settings(pydantic_settings.BaseSettings):
	"""Process configuration, resolved from flags, environment, file, then defaults."""

	model_config = pydantic_settings.SettingsConfigDict(
		env_prefix="SUBROUTINE_",
		extra="ignore",
		validate_default=True,
	)

	database_url: str = pydantic.Field(default_factory=default_database_url)
	host: str = "127.0.0.1"
	port: int = 8471
	secret_key: str | None = None
	dev_mode: bool = False
	log_level: str = "INFO"
	cors_origins: list[str] = pydantic.Field(default_factory=list)
	default_timezone: str = pydantic.Field(default_factory=system_timezone)

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

	file_data = _read_config_file()
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

	Appends rather than rewriting. A configuration file belongs to whoever edits it, and
	silently reformatting one — dropping their comments and their ordering — is a thing a
	program should not do to a file it does not own.
	"""

	path = config_file_path()
	path.parent.mkdir(parents=True, exist_ok=True)

	escaped = value.replace("\\", "\\\\").replace('"', '\\"')
	line = f'{name} = "{escaped}"\n'

	if not path.exists():
		path.write_text(
			f"# Subroutine configuration. See 'subroutine config show'.\n{line}",
			encoding="utf-8",
		)

		# Readable only by its owner: this is where the signing key lives.
		path.chmod(0o600)

		return path

	existing = path.read_text(encoding="utf-8")
	separator = "" if existing.endswith("\n") or not existing else "\n"

	path.write_text(f"{existing}{separator}{line}", encoding="utf-8")

	return path


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
