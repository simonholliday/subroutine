"""Taking a faithful copy of the database, and putting one back (SPEC.md §12.6).

This is not ``db export`` and does not replace it. An export is *logical and portable* —
readable, diffable, loadable into a different version. A backup is *exact and operational*:
this database as it is, for putting back after a loss. A project that keeps its own plan in a
database has to be able to do the second one.

**A file copy of a live SQLite database is not a snapshot.** Copying while a write is in
flight yields a file that is subtly torn and that usually *opens* successfully, which is the
worst of the available outcomes. So each backend is backed up by the mechanism that is
consistent by construction — ``VACUUM INTO`` for SQLite, which also compacts, and ``pg_dump``
for PostgreSQL — and an unrecognised backend is refused rather than guessed at.

**A backup carries its own schema version and needs no manifest.** Alembic's
``alembic_version`` is an ordinary table, so it is inside the copy. The filename echoes it as
a courtesy to whoever is reading a directory listing, but the value inside is the authority,
because a filename can be renamed and a table cannot.
"""

import dataclasses
import datetime
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess

import alembic.script
import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.exc
import sqlalchemy.orm

import subroutine.config
import subroutine.db.migrate
import subroutine.db.models.system
import subroutine.db.session
import subroutine.db.types
import subroutine.errors

#: Where backups go, under the instance's own data directory — so a profile's backups belong
#: to that profile and destroying it takes them with it (SPEC.md §12.5).
DIRECTORY_NAME = "backups"

#: ``.db`` for a SQLite copy, which really is a database; ``.sql`` for a PostgreSQL dump,
#: which is a script. The suffix is what tells ``restore`` how to read a file it is handed.
SQLITE_SUFFIX = ".db"
POSTGRESQL_SUFFIX = ".sql"

#: What the default instance calls itself in a filename. A profile is part of the name so that
#: two instances' backups can share a directory without either overwriting the other.
DEFAULT_LABEL = "default"

#: A plain ``pg_dump`` writes its data as a ``COPY`` block; ``--inserts`` writes statements
#: instead. Both forms appear in the wild — a dump may have been taken by hand — so the head is
#: looked for in either, and finding neither is an error rather than an assumption.
_COPY_BLOCK = re.compile(
	r"COPY\s+[\w.\"]*alembic_version[^\n]*FROM stdin;\s*\n([0-9a-z]+)", re.IGNORECASE
)
_INSERT_STATEMENT = re.compile(
	r"INSERT INTO\s+[\w.\"]*alembic_version[^\n]*VALUES\s*\(\s*'([0-9a-z]+)'", re.IGNORECASE
)

#: How long ``pg_dump`` and ``psql`` are given. Generous, because a large database on slow
#: storage is not an error, and bounded, because a hung subprocess with a full pipe is.
_SUBPROCESS_TIMEOUT_SECONDS = 600


@dataclasses.dataclass(frozen=True)
class Backup:
	"""One backup on disk, described well enough to choose between several."""

	path: pathlib.Path
	taken_at: datetime.datetime
	schema_head: str
	size_bytes: int
	profile: str | None

	@property
	def name (self) -> str:
		"""Return the filename, which is what an operator names on the command line."""

		return self.path.name


def directory () -> pathlib.Path:
	"""Return the directory holding this instance's backups, creating it if needed.

	Takes no settings: the path follows the active profile through ``data_home``, so asking
	for it always answers for the instance this process is acting on.
	"""

	path = subroutine.config.data_home() / DIRECTORY_NAME
	path.mkdir(parents=True, exist_ok=True)

	return path


def _stamp (moment: datetime.datetime) -> str:
	"""Render an instant as a filename-safe UTC stamp, to the second."""

	return moment.astimezone(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def _parsed_stamp (text: str) -> datetime.datetime | None:
	"""Read back a stamp written by ``_stamp``, or ``None`` if it is not one."""

	try:
		naive = datetime.datetime.strptime(text, "%Y%m%dT%H%M%SZ")

	except ValueError:
		return None

	return naive.replace(tzinfo=datetime.UTC)


def filename (
	profile: str | None, moment: datetime.datetime, head: str, suffix: str
) -> str:
	"""Compose a backup's filename: instance, instant, schema head, then the suffix."""

	label = profile or DEFAULT_LABEL

	return (
		f"{subroutine.config.APPLICATION_NAME}-{label}-{_stamp(moment)}-{head}{suffix}"
	)


#: How far ``_free_name`` will walk forward before giving up. Only reachable by taking
#: thousands of backups within a couple of minutes, which is a runaway loop and not a use.
_MAX_NAME_ATTEMPTS = 120


def _free_name (
	moment: datetime.datetime, profile: str | None, head: str, suffix: str
) -> tuple[datetime.datetime, pathlib.Path]:
	"""Return an instant and path that no existing backup already occupies.

	Stamps have second resolution, which is what makes a directory listing readable — and it
	means two backups taken in the same second want the same name. That is not rare: ``db
	restore`` takes a safety copy immediately before restoring, and the pair collided. It
	surfaced as ``VACUUM INTO`` refusing with "output file already exists", a message about
	SQLite rather than about anything the operator did.

	So the instant walks forward until the name is free. The recorded ``taken_at`` moves with
	it, because the filename is what a catalogue reads back and the two must not disagree.
	"""

	into = directory()

	for step in range(_MAX_NAME_ATTEMPTS):
		when = moment + datetime.timedelta(seconds=step)
		candidate = into / filename(profile, when, head, suffix)

		if not candidate.exists():
			return when, candidate

	raise subroutine.errors.BadRequest(
		f"Could not find an unused backup name near {_stamp(moment)} after "
		f"{_MAX_NAME_ATTEMPTS} tries."
	)


def _described (path: pathlib.Path) -> Backup | None:
	"""Describe a file if its name is one of ours, or return ``None`` if it is not.

	Deliberately tolerant: a directory that has had something else dropped into it should
	list the backups it does have rather than refuse to list anything.
	"""

	if path.suffix not in {SQLITE_SUFFIX, POSTGRESQL_SUFFIX} or not path.is_file():
		return None

	parts = path.stem.split("-")

	if len(parts) < 4 or parts[0] != subroutine.config.APPLICATION_NAME:
		return None

	taken_at = _parsed_stamp(parts[-2])

	if taken_at is None:
		return None

	label = "-".join(parts[1:-2])

	return Backup(
		path=path,
		taken_at=taken_at,
		schema_head=parts[-1],
		size_bytes=path.stat().st_size,
		profile=None if label == DEFAULT_LABEL else label,
	)


def catalogue () -> list[Backup]:
	"""Return this instance's backups, newest first."""

	found = [
		described for path in directory().iterdir() if (described := _described(path))
	]
	found.sort(key=lambda backup: backup.taken_at, reverse=True)

	return found


def head_in (path: pathlib.Path) -> str:
	"""Return the schema revision recorded *inside* a backup.

	The authority for whether a backup can be restored. Read from the copy itself rather than
	from its name, because a name can be changed by anybody with a shell and the answer decides
	whether data gets misread.
	"""

	if path.suffix == SQLITE_SUFFIX:
		return _head_in_sqlite(path)

	if path.suffix == POSTGRESQL_SUFFIX:
		return _head_in_dump(path)

	raise subroutine.errors.BadRequest(
		f"'{path.name}' is not a Subroutine backup: expected a name ending in "
		f"{SQLITE_SUFFIX} or {POSTGRESQL_SUFFIX}."
	)


def _head_in_sqlite (path: pathlib.Path) -> str:
	"""Read ``alembic_version`` out of a SQLite backup file."""

	# Read-only and through a URI, so that sqlite3 does not helpfully create an empty database
	# when handed a path that is not one — which would turn "this file is not a backup" into
	# "this backup has no schema version", a much less useful thing to be told.
	try:
		connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

	except sqlite3.Error as error:
		raise subroutine.errors.BadRequest(
			f"'{path.name}' could not be opened as a database: {error}"
		) from error

	try:
		rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()

	except sqlite3.Error as error:
		raise subroutine.errors.BadRequest(
			f"'{path.name}' does not look like a Subroutine backup: no schema version is "
			f"recorded in it ({error})."
		) from error

	finally:
		connection.close()

	return _single_head([str(row[0]) for row in rows], path)


def _head_in_dump (path: pathlib.Path) -> str:
	"""Find ``alembic_version`` in a PostgreSQL text dump."""

	text = path.read_text(encoding="utf-8", errors="replace")
	found = _COPY_BLOCK.search(text) or _INSERT_STATEMENT.search(text)

	if found is None:
		raise subroutine.errors.BadRequest(
			f"'{path.name}' does not look like a Subroutine backup: it records no "
			f"alembic_version. A dump taken by 'subroutine db backup' does."
		)

	return found.group(1)


def _single_head (values: list[str], path: pathlib.Path) -> str:
	"""Return the one recorded revision, refusing none and refusing several."""

	if len(values) != 1:
		raise subroutine.errors.BadRequest(
			f"'{path.name}' records {len(values)} schema versions where exactly one is "
			f"expected. It may be a copy of a database taken mid-migration."
		)

	return values[0]


def take (
	engine: sqlalchemy.engine.Engine,
	*,
	keep: int | None = None,
	moment: datetime.datetime | None = None,
) -> Backup:
	"""Copy the database into a datetime-stamped file and return what was written.

	``keep`` prunes to that many most recent backups afterwards. Nothing is deleted unless it is
	asked for: a backup command that quietly removes old backups is one bad default away from
	causing the loss it exists to prevent.
	"""

	head = subroutine.db.migrate.current_revision(engine)

	if head is None:
		raise subroutine.errors.BadRequest(
			"This database records no schema version, so a backup of it could not be "
			"restored. Run 'subroutine db upgrade' first."
		)

	suffix = SQLITE_SUFFIX if _is_sqlite(engine) else POSTGRESQL_SUFFIX
	active = subroutine.config.profile()
	taken_at, target = _free_name(
		moment or datetime.datetime.now(datetime.UTC), active, head, suffix
	)

	if _is_sqlite(engine):
		_take_sqlite(engine, target)

	else:
		_take_postgresql(engine, target)

	written = Backup(
		path=target,
		taken_at=taken_at,
		schema_head=head,
		size_bytes=target.stat().st_size,
		profile=active,
	)

	if keep is not None:
		prune(keep=keep)

	return written


def _take_sqlite (engine: sqlalchemy.engine.Engine, target: pathlib.Path) -> None:
	"""Write a consistent SQLite copy with ``VACUUM INTO``.

	Consistent by construction — SQLite takes its own read transaction for the duration — and
	compacted on the way out, which a file copy is not and does not.

	Issued on the **driver** connection rather than through ``exec_driver_sql``, for the reason
	``migrations/env.py`` documents: the SQLAlchemy path opens a transaction, and ``VACUUM``
	cannot run inside one.
	"""

	try:
		with engine.connect() as connection:
			raw = connection.connection.driver_connection

			if raw is None:
				raise subroutine.errors.InternalError(
					"The SQLite connection could not be reached to take a backup."
				)

			raw.execute("VACUUM INTO ?", (str(target),))

	except (sqlalchemy.exc.SQLAlchemyError, sqlite3.Error) as error:
		# Translated rather than allowed out. A storage failure here reaches a person as a
		# `db backup` that did not work, and a driver traceback describes SQLite rather than
		# anything they can act on — the same discipline `fanout` applies to a connection.
		raise subroutine.errors.BadRequest(
			f"Could not write the backup to {target}: {error}"
		) from error


def _take_postgresql (engine: sqlalchemy.engine.Engine, target: pathlib.Path) -> None:
	"""Write a plain-format ``pg_dump`` of the configured database."""

	_run(
		[
			"pg_dump",
			"--no-owner",
			"--no-privileges",
			"--file",
			str(target),
			"--dbname",
			_connectable(engine),
		],
		what="pg_dump",
	)


def _is_sqlite (engine: sqlalchemy.engine.Engine) -> bool:
	"""Report whether this engine is SQLite.

	**Asked of the engine, never of the settings.** They can disagree — a served application may
	be bound to a database the configured URL does not name, which is exactly what the test
	harness does — and the thing being copied is the engine's database. Branching on
	``settings.is_sqlite`` while operating on ``engine`` sent ``VACUUM INTO`` at PostgreSQL.
	"""

	return engine.dialect.name == "sqlite"


def _connectable (engine: sqlalchemy.engine.Engine) -> str:
	"""Return the database URL in a form that can actually be connected with.

	Two traps, both of which produce a URL that looks right and does not work:

	* ``str()`` on a SQLAlchemy ``URL`` renders the password as ``***``, so a URL turned back
	  into a string authenticates as the literal ``***``. Invisible where authentication is by
	  Unix socket and fatal everywhere it is real.
	* SQLAlchemy's drivername carries the DBAPI — ``postgresql+psycopg`` — and ``pg_dump`` has
	  never heard of it. Handed one, it reads the *whole URL* as a database name and reports
	  that no such database exists, which sends the reader looking in the wrong place.
	"""

	url = engine.url

	return url.set(drivername=url.get_backend_name()).render_as_string(hide_password=False)


def _run (command: list[str], *, what: str) -> None:
	"""Run one of the PostgreSQL tools, reporting its complaint rather than a traceback."""

	try:
		process = subprocess.Popen(
			command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
		)

	except FileNotFoundError as error:
		raise subroutine.errors.ServiceUnavailable(
			f"{what} is not installed, and a PostgreSQL backup needs it. It comes with the "
			f"PostgreSQL client tools."
		) from error

	# `communicate` rather than `wait`: a pipe left open trips the ResourceWarning this project
	# turns into an error, and the traceback then points at the wrong place entirely.
	_output, complaint = process.communicate(timeout=_SUBPROCESS_TIMEOUT_SECONDS)

	if process.returncode != 0:
		reported = complaint.strip() or f"exit status {process.returncode}"

		raise subroutine.errors.BadRequest(f"{what} failed: {reported}")


def prune (*, keep: int) -> list[Backup]:
	"""Delete all but the ``keep`` most recent backups, returning what was removed."""

	if keep < 1:
		raise subroutine.errors.ValidationError(
			f"--keep must be 1 or more, not {keep}: pruning to nothing would delete every "
			f"backup this instance has."
		)

	removed = catalogue()[keep:]

	for backup in removed:
		backup.path.unlink(missing_ok=True)

	return removed


def check_restorable (path: pathlib.Path) -> str:
	"""Return a backup's schema head, refusing one this installation cannot interpret.

	The asymmetry is the safety property (SPEC.md §12.6). An *older* schema can be migrated
	forward, which is what Alembic is for. A *newer* one is refused: the running code does not
	know the columns, so "try anyway" means a silent misread rather than a visible failure.
	"""

	backup_head = head_in(path)
	ours = subroutine.db.migrate.head_revision()

	if ours is None:
		raise subroutine.errors.InternalError(
			"This installation has no migrations, so it cannot judge whether a backup fits."
		)

	if backup_head == ours or _is_ancestor(backup_head, ours):
		return backup_head

	raise subroutine.errors.SchemaMismatch(
		f"'{path.name}' was taken on database schema {backup_head}, which this installation "
		f"does not know — it is at {ours}. Restoring it would mean reading data whose shape "
		f"this version cannot know. Upgrade Subroutine to a version that has migration "
		f"{backup_head}, then restore."
	)


def _is_ancestor (candidate: str, head: str) -> bool:
	"""Report whether ``candidate`` is a revision this installation can migrate forward from."""

	# The URL is never connected to — `ScriptDirectory` only needs the configuration in order
	# to find the migration files.
	script = alembic.script.ScriptDirectory.from_config(
		subroutine.db.migrate.build_config("sqlite://")
	)

	try:
		known = {revision.revision for revision in script.walk_revisions("base", head)}

	except Exception:
		# An unknown or unreachable revision is not an ancestor. The caller turns that into
		# "cannot restore", which is the right answer and better than a traceback from Alembic.
		return False

	return candidate in known


def restore (
	engine: sqlalchemy.engine.Engine,
	source: pathlib.Path,
	*,
	as_clone: bool,
) -> str:
	"""Put a backup back, and return the schema head that was restored.

	``as_clone`` settles the question §12.6a says must never be guessed. A **clone** gets a
	fresh ``instance_id``, because two live instances may not claim one identity: an agent keys
	its caches on it and ``fanout.refuse_duplicate_instances`` compares it. A **recovery** keeps
	the identity it had, because agents and configuration files already refer to it.

	Both are done here rather than by the caller so that neither can be forgotten at one of
	two call sites.
	"""

	head = check_restorable(source)

	url = engine.url.render_as_string(hide_password=False)

	if _is_sqlite(engine):
		_restore_sqlite(engine, source)

	else:
		_restore_postgresql(engine, source)

	if as_clone:
		_reidentify(url)

	return head


def _restore_sqlite (
	engine: sqlalchemy.engine.Engine, source: pathlib.Path
) -> None:
	"""Replace the SQLite database file with the backup."""

	database = engine.url.database
	target = pathlib.Path(database) if database else None

	if target is None:
		raise subroutine.errors.InternalError(
			"The configured SQLite path could not be read."
		)

	# Every connection has to be closed before the file underneath it is replaced, or SQLite
	# goes on reading the old inode and the restore appears to have done nothing at all.
	engine.dispose()

	target.parent.mkdir(parents=True, exist_ok=True)
	staged = target.with_name(target.name + ".restoring")

	shutil.copy2(source, staged)
	os.replace(staged, target)


def _restore_postgresql (
	engine: sqlalchemy.engine.Engine, source: pathlib.Path
) -> None:
	"""Empty the PostgreSQL database and load the dump into it."""

	# The dump recreates every table it holds, so what is there now has to go first. Dropping
	# the *schema* rather than the database means no maintenance connection is needed, and the
	# restore works on a managed server where creating databases is not permitted.
	with engine.begin() as connection:
		connection.exec_driver_sql("DROP SCHEMA public CASCADE")
		connection.exec_driver_sql("CREATE SCHEMA public")

	engine.dispose()

	_run(
		[
			"psql",
			"--quiet",
			"--set",
			"ON_ERROR_STOP=on",
			"--file",
			str(source),
			"--dbname",
			_connectable(engine),
		],
		what="psql",
	)


def _reidentify (database_url: str) -> None:
	"""Give the restored database a new ``instance_id``, and forget the stored context.

	The context goes because it names a connection and a workspace that belonged to the
	original (SPEC.md §13.7), and a clone pointing at the original's context is the confusion
	this whole distinction exists to prevent. Credentials are deliberately *left alone*: a token
	is scoped to a user and a workspace, both of which survive the copy, and revoking them would
	make a test clone useless for testing.
	"""

	engine = subroutine.db.session.create_engine(database_url)

	try:
		factory = subroutine.db.session.create_session_factory(engine)

		with subroutine.db.session.session_scope(factory) as session:
			instance = session.execute(
				sqlalchemy.select(subroutine.db.models.system.Instance)
			).scalar_one()

			instance.id = subroutine.db.types.new_uuid()

	finally:
		engine.dispose()

	context = subroutine.config.state_home() / "context.toml"
	context.unlink(missing_ok=True)
