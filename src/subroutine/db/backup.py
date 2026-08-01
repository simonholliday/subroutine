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

import contextlib
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

	#: What taking this one *deleted*, when ``--keep`` asked for it. Carried back rather than
	#: discarded because ``hosting.md`` recommends running the backup from a timer, and a timer's
	#: log is the only record there will ever be of what went (`#175`). A deletion nothing
	#: reports is one nobody can audit, on the command whose whole subject is not losing data.
	removed: tuple["Backup", ...] = ()

	@property
	def name (self) -> str:
		"""Return the filename, which is what an operator names on the command line."""

		return self.path.name


def directory (settings: subroutine.config.Settings) -> pathlib.Path:
	"""Return the directory holding this instance's backups, creating it if needed.

	``settings`` is here to say **where**, never *what kind* — the backend is always asked of the
	engine (see ``_is_sqlite``). Keeping those two questions apart is what stopped ``VACUUM
	INTO`` being aimed at PostgreSQL, so do not collapse them again.

	Unset ``backup_directory`` means the instance's own data directory, which follows the active
	profile and is right for one machine. Pointing it at a network volume is the case §12.6b
	exists for.
	"""

	configured = (settings.backup_directory or "").strip()
	path = (
		pathlib.Path(configured).expanduser()
		if configured
		else subroutine.config.data_home() / DIRECTORY_NAME
	)

	try:
		path.mkdir(parents=True, exist_ok=True)

	except OSError as error:
		raise subroutine.errors.ServiceUnavailable(
			f"The backup directory {path} could not be used: {error}. If it is on a network "
			f"volume, check that the volume is mounted."
		) from error

	return path


def _staging_directory () -> pathlib.Path:
	"""Return a local directory to build a backup in before moving it to its destination.

	**Always local, and that is the point.** ``VACUUM INTO`` creates a database and takes a lock
	on it, so it cannot be aimed at a filesystem where SQLite cannot lock — which is exactly the
	kind of volume somebody sensibly wants their backups on (SPEC.md §12.6b). The data directory
	is guaranteed usable, because the database itself lives there and ``probe_sqlite_locking``
	refuses an installation where it would not work.
	"""

	path = subroutine.config.data_home() / DIRECTORY_NAME / ".staging"
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
	into: pathlib.Path,
	moment: datetime.datetime,
	profile: str | None,
	head: str,
	suffix: str,
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


def catalogue (settings: subroutine.config.Settings) -> list[Backup]:
	"""Return this instance's backups, newest first."""

	found = [
		described
		for path in directory(settings).iterdir()
		if (described := _described(path))
	]
	found.sort(key=lambda backup: backup.taken_at, reverse=True)

	return found


def head_in (path: pathlib.Path) -> str:
	"""Return the schema revision recorded *inside* a backup.

	The authority for whether a backup can be restored. Read from the copy itself rather than
	from its name, because a name can be changed by anybody with a shell and the answer decides
	whether data gets misread.
	"""

	# Through `engine_in` so the refusal for an unrecognised name exists once. The two
	# questions really are the same one: which tool wrote this file decides both which engine
	# can read it back and how its schema version is found inside it.
	if engine_in(path) == "SQLite":
		return _head_in_sqlite(path)

	return _head_in_dump(path)


#: What each suffix says a backup was taken from, in the words an operator would use rather
#: than the dialect names. Read from the *name* because that is what the writer chose it for:
#: `.db` is a SQLite database and `.sql` is a script only ``psql`` can read, and the two are
#: not interchangeable in either direction.
ENGINE_OF_SUFFIX = {SQLITE_SUFFIX: "SQLite", POSTGRESQL_SUFFIX: "PostgreSQL"}


def engine_in (path: pathlib.Path) -> str:
	"""Return which engine a backup was taken from — ``SQLite`` or ``PostgreSQL``."""

	found = ENGINE_OF_SUFFIX.get(path.suffix)

	if found is None:
		raise subroutine.errors.BadRequest(
			f"'{path.name}' is not a Subroutine backup: expected a name ending in "
			f"{SQLITE_SUFFIX} or {POSTGRESQL_SUFFIX}."
		)

	return found


def check_engine (engine: sqlalchemy.engine.Engine, source: pathlib.Path) -> None:
	"""Refuse a backup taken from the other engine, before anything is destroyed (`#172`).

	**The whole defect was one of order.** Restoring into PostgreSQL drops and recreates
	``public`` and *then* hands the file to ``psql``, so a SQLite backup chosen by mistake took
	the instance with it and reported a raw encoding error — ``invalid byte sequence for
	encoding "UTF8"`` — that never says "SQLite", never says "wrong file", and leaves an empty
	database behind. The one thing an operator needed to know was knowable before a single row
	was dropped.

	``docs/hosting.md`` already stated the rule, which is the shape worth noticing: the
	document knew and the program did not, so the only thing standing between a correct
	instance and an empty one was whether somebody had read the right paragraph.

	Asked of the **engine**, never of ``settings`` — see ``_is_sqlite``.
	"""

	held = engine_in(source)
	ours = "SQLite" if _is_sqlite(engine) else "PostgreSQL"

	if held == ours:
		return

	raise subroutine.errors.ValidationError(
		f"'{source.name}' is a {held} backup and this instance runs on {ours}.",
		hint=(
			f"Backups are taken with the tools of one engine and cannot be read by the other, "
			f"so this one cannot be restored here — nothing has been changed. A {ours} backup "
			f"of this instance ends in "
			f"{SQLITE_SUFFIX if ours == 'SQLite' else POSTGRESQL_SUFFIX}; "
			f"'subroutine db backups' lists them with their engine. To move an instance "
			f"between engines, use 'subroutine db copy'."
		),
	)


def in_use_by (engine: sqlalchemy.engine.Engine) -> str | None:
	"""Return what else is holding this database, or ``None`` if nothing is (`#171`).

	**Restoring underneath a running service destroys the instance and reports success.** The
	serving process keeps its descriptors on the file that was just replaced — ``subroutine.db
	(deleted)`` in ``/proc``, with its ``-wal`` and ``-shm`` beside it — so every write it
	accepts is lost, every read is stale, and its next WAL checkpoint lands on top of the
	restored file and corrupts it. The API answers 200 throughout, including ``/readyz``, which
	is the endpoint an operator would use to check that the restore worked.

	§12.4's argument is that recovery works under pressure, and this is the command run under
	pressure. A sentence in a document is not enough: ``docs/hosting.md`` says "stop the
	service first" for ``db copy``, which is the *safer* of the two.

	**Asked of the database itself rather than of the process table.** SQLite answers by
	refusing an exclusive lock — an idle pooled connection in another process is enough, which
	is exactly the case a live server presents — and PostgreSQL answers from
	``pg_stat_activity``. Both are the real question; scanning ``/proc`` would be a Linux-only
	guess at it.
	"""

	# Our own pool would otherwise answer for somebody else. This is called before anything is
	# read or written, so there is nothing in flight to lose.
	engine.dispose()

	if _is_sqlite(engine):
		return _sqlite_in_use_by(engine)

	return _postgresql_in_use_by(engine)


def _sqlite_in_use_by (engine: sqlalchemy.engine.Engine) -> str | None:
	"""Report whether another process holds the SQLite database open."""

	database = engine.url.database

	if database is None or database == ":memory:" or not pathlib.Path(database).exists():
		return None

	path = pathlib.Path(database)

	# **Descriptors first, because they are the only signal damage cannot corrupt.** They also
	# answer the worst case directly: a process still holding the file a previous restore
	# unlinked appears here as `(deleted)`, which is the state `#171` leaves behind.
	holders = _holders_in_proc(path)

	if holders:
		return f"process {', '.join(str(pid) for pid in holders)} has the database file open"

	# **And the lock probe only on a database that can be read at all.** A damaged one with a
	# stale write-ahead log reports "database is locked" while trying to recover a log it
	# cannot read — with nothing else running. Reading that as "somebody is using it" refuses
	# the rescue on the strength of the damage, which is `#173` reintroduced by `#171`'s fix.
	# Measured: a failed safety copy leaves exactly that pair of files behind, so the two
	# defects meet on the ordinary path rather than in some corner.
	if not _sqlite_readable(path):
		return None

	try:
		connection = sqlite3.connect(database, timeout=0)

	except sqlite3.Error:
		return None

	if connection is not None:
		try:
			# An exclusive lock needs sole access to the shared-memory index, which another
			# connection holds simply by existing. Verified against an *idle* connection,
			# because a pooled one between requests takes no lock of its own and would
			# otherwise pass — ``db/session.py`` puts every connection into WAL, so any
			# process of ours holding this database has produced the index this looks for.
			connection.execute("PRAGMA locking_mode=EXCLUSIVE")
			connection.execute("BEGIN IMMEDIATE")
			connection.execute("COMMIT")

		except sqlite3.Error as error:
			return f"another process has the database open ({error})"

		finally:
			connection.close()

	return None


def _sqlite_readable (path: pathlib.Path) -> bool:
	"""Report whether this file can be opened and read as a database at all.

	The question the lock probe needs answered first, because "locked" and "damaged" arrive as
	the same sentence and mean opposite things: one is a reason to refuse a restore and the
	other is the reason to run one.

	Opened read-write rather than read-only, deliberately — reading a database with a
	write-ahead log beside it may need to recover that log, which a read-only connection
	cannot do, and a healthy database would then be reported unreadable.
	"""

	if not path.exists():
		return False

	try:
		connection = sqlite3.connect(path, timeout=0)

	except sqlite3.Error:
		return False

	try:
		connection.execute("SELECT count(*) FROM sqlite_master").fetchone()

	except sqlite3.Error:
		return False

	finally:
		connection.close()

	return True


def _holders_in_proc (path: pathlib.Path) -> list[int]:
	"""Return the pids holding this file open, where ``/proc`` can say.

	**Best effort, and openly so.** It answers on Linux, which is what this is deployed on and
	what ``docs/hosting.md`` describes; elsewhere it returns nothing and the lock probe is the
	whole answer. A process owned by another user is unreadable and so invisible, which is why
	this is a second signal rather than the only one — a check that quietly saw nothing would
	be worse than no check, because it would be believed.
	"""

	root = pathlib.Path("/proc")

	if not root.is_dir():
		return []

	try:
		wanted = str(path.resolve())

	except OSError:
		return []

	# The unlinked form is the state `#171` leaves behind: the serving process goes on writing
	# to a file that no longer has a name, which is why the corruption is invisible until its
	# next checkpoint.
	names = {wanted, f"{wanted} (deleted)"}
	found = []

	for entry in root.iterdir():
		if not entry.name.isdigit() or int(entry.name) == os.getpid():
			continue

		try:
			descriptors = list((entry / "fd").iterdir())

		except OSError:
			# Another user's process, or one that exited while this was walking. Both are
			# ordinary here and neither is an answer.
			continue

		for descriptor in descriptors:
			try:
				link = os.readlink(descriptor)

			except OSError:
				continue

			if link in names:
				found.append(int(entry.name))

				break

	return sorted(found)


def _postgresql_in_use_by (engine: sqlalchemy.engine.Engine) -> str | None:
	"""Report how many other sessions are connected to the PostgreSQL database."""

	try:
		with engine.connect() as connection:
			others = connection.execute(
				sqlalchemy.text(
					"SELECT count(*) FROM pg_stat_activity "
					"WHERE datname = current_database() AND pid <> pg_backend_pid()"
				)
			).scalar_one()

	except sqlalchemy.exc.SQLAlchemyError:
		# Same reasoning as the SQLite side: a database that cannot be reached is not one
		# somebody else is using, and refusing here would block the rescue.
		return None

	if not others:
		return None

	return f"{others} other connection{'' if others == 1 else 's'} to the database"


def check_unused (engine: sqlalchemy.engine.Engine) -> None:
	"""Refuse to restore over a database something else is using (`#171`)."""

	holder = in_use_by(engine)

	if holder is None:
		return

	raise subroutine.errors.ValidationError(
		f"Something else is using this database: {holder}.",
		hint=(
			"Restoring underneath a running service does not reach it — it keeps writing to "
			"the file that was replaced, and its next checkpoint can corrupt the restored "
			"one. Stop the service first ('systemctl stop subroutine', or however it is run), "
			"then restore. Use --force only if you are certain nothing is connected."
		),
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
	settings: subroutine.config.Settings,
	*,
	keep: int | None = None,
	moment: datetime.datetime | None = None,
) -> Backup:
	"""Copy the database into a datetime-stamped file and return what was written.

	``keep`` prunes to that many most recent backups afterwards. Nothing is deleted unless it is
	asked for: a backup command that quietly removes old backups is one bad default away from
	causing the loss it exists to prevent.
	"""

	try:
		head = subroutine.db.migrate.current_revision(engine)

	except sqlalchemy.exc.SQLAlchemyError as error:
		# Translated for the reason `_take_sqlite` gives, and for one more that cost a rescue
		# (`#173`). This is the *first* thing `take` does, so a damaged database fails here
		# rather than in the copy — and a raw driver error escaping at this point walks past
		# every caller that guards itself against a `SubroutineError`, which is exactly what
		# aborted a `db restore --recover` on the only database that needed one.
		raise subroutine.errors.BadRequest(
			f"This database could not be read in order to back it up: "
			f"{getattr(error, 'orig', None) or error}"
		) from error

	if head is None:
		raise subroutine.errors.BadRequest(
			"This database records no schema version, so a backup of it could not be "
			"restored. Run 'subroutine db upgrade' first."
		)

	suffix = SQLITE_SUFFIX if _is_sqlite(engine) else POSTGRESQL_SUFFIX
	active = subroutine.config.profile()
	into = directory(settings)
	taken_at, target = _free_name(
		into, moment or datetime.datetime.now(datetime.UTC), active, head, suffix
	)

	# Built locally, then moved. See `_staging_directory` — the destination may be a volume
	# SQLite cannot write a database to, which is a perfectly good place to keep a backup.
	staged = _staging_directory() / target.name

	try:
		if _is_sqlite(engine):
			_take_sqlite(engine, staged)

		else:
			_take_postgresql(engine, staged)

		size = staged.stat().st_size
		_delivered(staged, target, head=head, size=size)

	finally:
		staged.unlink(missing_ok=True)

	return Backup(
		path=target,
		taken_at=taken_at,
		schema_head=head,
		size_bytes=size,
		profile=active,
		removed=() if keep is None else tuple(prune(settings, keep=keep)),
	)


def _delivered (
	staged: pathlib.Path, target: pathlib.Path, *, head: str, size: int
) -> None:
	"""Move a finished backup to its destination and prove that what arrived is readable.

	**A half-written file on a network volume is the failure worth spending code on**, because it
	looks like a backup: it appears in the catalogue, its name says which schema it holds, and it
	is discovered to be short only on the day it is needed. So the copy is checked where it landed
	— its size, and that its schema version can still be read out of it — and a file that fails
	is deleted rather than left looking valid.

	``shutil.move`` rather than ``os.replace``: the destination is usually on another filesystem,
	where a rename cannot work at all, and this share does not honour the create-then-rename dance
	either.
	"""

	try:
		shutil.move(str(staged), str(target))

	except OSError as error:
		raise subroutine.errors.ServiceUnavailable(
			f"The backup could not be written to {target}: {error}. If that is a network "
			f"volume, check that it is mounted and writable."
		) from error

	# A backup is the database, so it gets the database's permissions. Doing it before the
	# verification below means a copy that fails the check was never readable by anyone else
	# either, however briefly.
	subroutine.config.keep_private(target)

	try:
		arrived = target.stat().st_size

		if arrived != size:
			raise subroutine.errors.ServiceUnavailable(
				f"The backup written to {target} is {arrived} bytes and should be {size}. "
				f"It has been removed rather than left looking usable."
			)

		if head_in(target) != head:
			raise subroutine.errors.ServiceUnavailable(
				f"The backup written to {target} could not be read back as schema {head}. "
				f"It has been removed rather than left looking usable."
			)

	except Exception:
		with contextlib.suppress(OSError):
			target.unlink(missing_ok=True)

		raise


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


def prune (settings: subroutine.config.Settings, *, keep: int) -> list[Backup]:
	"""Delete all but the ``keep`` most recent backups, returning what was removed."""

	if keep < 1:
		raise subroutine.errors.ValidationError(
			f"--keep must be 1 or more, not {keep}: pruning to nothing would delete every "
			f"backup this instance has."
		)

	removed = catalogue(settings)[keep:]

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
	force: bool = False,
) -> str:
	"""Put a backup back, and return the schema head that was restored.

	``as_clone`` settles the question §12.6a says must never be guessed. A **clone** gets a
	fresh ``instance_id``, because two live instances may not claim one identity: an agent keys
	its caches on it and ``fanout.refuse_duplicate_instances`` compares it. A **recovery** keeps
	the identity it had, because agents and configuration files already refer to it.

	Both are done here rather than by the caller so that neither can be forgotten at one of
	two call sites.
	"""

	# Every check before anything is touched, and in this order: a file from the other engine
	# is refused as such rather than as a file with no schema version in it (`#172`), and a
	# database somebody else is using is refused before either (`#171`).
	if not force:
		check_unused(engine)

	check_engine(engine, source)

	head = check_restorable(source)

	url = engine.url.render_as_string(hide_password=False)

	if _is_sqlite(engine):
		_restore_sqlite(engine, source)

	else:
		_restore_postgresql(engine, source)

	if as_clone:
		_reidentify(url)

	return head


#: What SQLite keeps beside a database in WAL mode. Both belong to the file they are named
#: after and neither survives it — see :func:`_restore_sqlite`.
SQLITE_SIDECARS = ("-wal", "-shm")


def _restore_sqlite (
	engine: sqlalchemy.engine.Engine, source: pathlib.Path
) -> None:
	"""Replace the SQLite database file with the backup, and take its sidecars with it."""

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

	_discard_the_replaced_log(target)


def _discard_the_replaced_log (target: pathlib.Path) -> None:
	"""Remove the write-ahead log the restored database has just displaced (`#194`).

	**A ``-wal`` left beside the restored file undoes the restore.** SQLite replays it on the
	next open, so the backup's content is discarded and the state that was just replaced comes
	back — after the command has printed that it succeeded. That is `#171`'s signature exactly:
	restore reports success, database is not restored. Reproduced through the CLI, on the
	ordinary recovery path where the database file has been lost and its sidecars have not.

	It went unnoticed because the ordinary path is saved by accident: the safety copy and
	``_sqlite_readable`` both open and cleanly close the database, and each of those checkpoints
	the log away. Nothing chose that, nothing wrote it down, and it is absent with
	``--no-safety-backup``, after a safety copy the operator overrode, and on a database too
	damaged to open.

	**After the replace, never before.** The log belongs to the database being replaced, so
	deleting it ahead of a copy that then failed would throw away the very state the operator is
	still standing on.

	**And a failure here is raised**, unlike the permission-tightening elsewhere in this module.
	A sidecar left behind is not a lesser version of the job — it is the whole defect.
	"""

	for suffix in SQLITE_SIDECARS:
		beside = target.with_name(target.name + suffix)

		try:
			beside.unlink(missing_ok=True)

		except OSError as error:
			raise subroutine.errors.ServiceUnavailable(
				f"The database was restored, but {beside.name} could not be removed: {error}. "
				f"Delete it before anything opens this database — SQLite would replay it over "
				f"the restored file and put back what was just replaced."
			) from error


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
