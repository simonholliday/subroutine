"""Taking a faithful copy of the database, and putting one back (docs/design.md §12.6).

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
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import time

import alembic.script
import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.exc

import subroutine.config
import subroutine.db.migrate
import subroutine.db.models.system
import subroutine.db.session
import subroutine.db.types
import subroutine.errors

#: Where backups go, under the instance's own data directory — so a profile's backups belong
#: to that profile and destroying it takes them with it (docs/design.md §12.5).
DIRECTORY_NAME = "backups"

#: ``.db`` for a SQLite copy, which really is a database; ``.sql`` for a PostgreSQL dump,
#: which is a script. The suffix is what tells ``restore`` how to read a file it is handed.
SQLITE_SUFFIX = ".db"
POSTGRESQL_SUFFIX = ".sql"

#: What a PostgreSQL backup is written as since `#1554`, and the whole of that fix.
#:
#: **A plain dump is a script `psql` executes**, and `psql` interprets backslash meta-commands
#: inside one — ``\!`` is a shell escape. :func:`refuse_unsafe_commands` tried to find them by
#: reading the file the way `psql` reads it, and could not: five evasion shapes were driven
#: past it. A custom-format archive is read by ``pg_restore``, **which has no meta-command
#: lexer at all**, so the instruction cannot exist rather than having to be found.
#:
#: Measured before it was chosen: a row whose *content* is ``a row with a backslash \! echo
#: pwned`` restores as data.
#:
#: **`.sql` is still read**, because every backup taken before this is one and refusing them
#: would take somebody's only copy away on the worst day they will have. Nothing writes one
#: any more, so that path empties as old files age out.
POSTGRESQL_ARCHIVE_SUFFIX = ".dump"

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

#: The tables every Subroutine database has had since the beginning, and still has (`#928`).
#:
#: **A schema version alone does not make a file a backup.** ``check_restorable`` used to ask
#: only whether ``alembic_version`` held a revision this installation could migrate forward
#: from — so a 12 KB file holding that one table and a table called ``loot`` was accepted,
#: installed over the live database, and reported as a success.
#:
#: **Why these tables and not the current ones.** A backup may legitimately be older than this
#: installation, so it cannot be held to the shape of ``Base.metadata`` today. These are the
#: tables the initial migration creates; every revision since has added tables and dropped
#: none, so they are the floor at *every* restorable revision rather than at the newest one.
#: ``tests/test_backup.py`` asserts they are still a subset of the live metadata, so dropping
#: one fails the build loudly instead of quietly making every backup unrestorable.
CORE_TABLES = frozenset(
	{
		"alembic_version",
		"api_token",
		"comment",
		"document",
		"event",
		"instance",
		"item_type",
		"link",
		"link_type",
		"mention",
		"project",
		"role",
		"status",
		"tag",
		"task",
		"user",
		"workspace",
	}
)

#: The backslash commands a ``pg_dump`` script legitimately contains, and the only ones a
#: restore will carry to ``psql`` (`#928`).
#:
#: **``psql`` interprets backslash meta-commands inside a ``--file`` script**: ``\!`` runs a
#: shell command, ``\i`` includes any file, ``\copy`` reads and writes the filesystem. So a
#: backup file is a code-execution vector unless something reads it first.
#:
#: **Measured rather than assumed, because refusing all of them would refuse our own dumps.** A
#: real dump of this schema carries 25: twenty-three ``\.`` terminating ``COPY`` blocks, and one
#: each of ``\restrict`` and ``\unrestrict``, which are PostgreSQL's own guard against exactly
#: this and which ``psql`` 16 honours — verified, a ``\!`` after ``\restrict`` is refused by
#: name. That guard protects a *genuine* dump and does nothing about a forged one, which simply
#: omits the pair, so the scan below is ours to do.
_SAFE_META_COMMANDS = frozenset({".", "restrict", "unrestrict"})

#: A ``COPY … FROM stdin;`` line, after which every line is *data* until a lone ``\.`` — so a
#: scan for meta-commands has to know which of the two it is reading.
_COPY_BEGINS = re.compile(r"^\s*COPY\s+.*\bFROM\s+stdin\s*;", re.IGNORECASE)

#: A backslash command: the leading backslash, then the name. ``psql`` allows leading
#: whitespace, so the scan does too rather than matching only at column one.
_META_COMMAND = re.compile(r"^\s*\\([a-zA-Z.?!]+)")

#: How long ``pg_dump`` and ``psql`` are given. Generous, because a large database on slow
#: storage is not an error, and bounded, because a hung subprocess with a full pipe is.
_SUBPROCESS_TIMEOUT_SECONDS = 600

#: How long ``in_use_by`` waits before asking a second time (`#725`). Long enough that a
#: backend on its way out has gone, short enough to be imperceptible on a command run once
#: during a recovery — and it is only ever paid when the first answer found *something*, so a
#: database nobody is touching costs nothing at all.
_SETTLE_SECONDS = 0.25


@dataclasses.dataclass(frozen=True)
class Backup:
	"""One backup on disk, described well enough to choose between several."""

	path: pathlib.Path
	taken_at: datetime.datetime
	schema_head: str
	size_bytes: int
	profile: str | None

	#: How many rows of *work* the source held when this was taken — item `#395`. ``None`` for
	#: a backup found on disk, where nothing recorded it and opening the file to count would
	#: turn listing a directory into reading every file in it.
	#:
	#: **A backup of an empty instance passed every check there was.** Size, and the schema head
	#: read back from inside the copy — both correct, because an empty database is a *valid*
	#: database. §12.6's verification asks whether the file arrived intact and never asked
	#: whether it holds anything, so four hollow backups reported "458,752 bytes, schema
	#: d5d0458f5ad5" and nothing about that sentence was false.
	holdings: dict[str, int] | None = None

	#: What this copy was taken for — :data:`ROUTINE`, :data:`BEFORE_UPGRADE` or
	#: :data:`BEFORE_RESTORE` — or ``None`` for one taken before anything recorded it.
	#:
	#: **``None`` is *not recorded*, and it is not *routine***, which is `#432`'s distinction one
	#: field along. Retention treats the two the same on purpose: an unlabelled copy is kept
	#: unless somebody asks for it to go, the reading that keeps more than it deletes. A
	#: *listing* must still tell them apart, because a directory of pre-upgrade copies taken
	#: before this existed would otherwise describe itself as a directory of routine backups —
	#: which is the one thing an operator looking at an unexplained pile needs to know.
	taken_for: str | None = None

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
	kind of volume somebody sensibly wants their backups on (docs/design.md §12.6b). The data directory
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


#: What a copy was taken for, recorded beside it so that retention can tell three lifetimes
#: apart (`#1712`). One directory holds all three and they are not interchangeable: a routine
#: backup is one the operator asked for, and the other two are copies the program took on its
#: own initiative at a moment it knew was risky.
#:
#: **Recorded rather than spelled into the filename**, which was the alternative and costs a
#: contract change: the name is what ``catalogue`` parses, ``restore`` reads,
#: ``docs/hosting.md`` quotes and a guard checks. A reason written *beside* the copy also
#: survives the one thing a path cannot — the same file is ``/var/lib/subroutine/<slug>``
#: inside a systemd unit and ``/var/lib/private/subroutine/<slug>`` outside it, so anything
#: joining on the path matches nothing, ever, and nothing complains about it.
ROUTINE = "routine"
BEFORE_UPGRADE = "upgrade"
BEFORE_RESTORE = "restore"

#: What each reason is called in something a person reads. The stored value is a key and the
#: rendered value is a word (`#1717`) — an operator listing a directory should not have to know
#: which of the two they are looking at.
TAKEN_FOR_WORDS = {
	ROUTINE: "routine",
	BEFORE_UPGRADE: "before an upgrade",
	BEFORE_RESTORE: "before a restore",
}

#: How long a restore-safety copy lives before the next restore clears it away (`#1712`).
#:
#: **A constant rather than a setting**, deliberately. How many upgrades back somebody might
#: want to go is a question about a disk, and varies between a laptop and a fleet sharing a
#: volume — so that one is configurable. How long until you know a restore was right is a
#: question about a person, and does not. An operator who wants a copy kept past this has
#: ``db backup``, which takes one they own and which nothing here removes.
RESTORE_SAFETY_LIFETIME = datetime.timedelta(days=7)


#: What a backup's own record of itself is called. **Beside the copy rather than inside it**:
#: a `pg_dump` script is not a database anything can open without restoring it somewhere first,
#: and writing into a SQLite copy would break the property §12.6 verifies — that the file on
#: disk is byte-for-byte what was taken.
RECORD_SUFFIX = ".counts.json"


def _record_beside (path: pathlib.Path) -> pathlib.Path:
	"""Return where one backup's record of what it held lives."""

	return path.with_name(path.name + RECORD_SUFFIX)


def _record (target: pathlib.Path, holdings: dict[str, int], taken_for: str) -> None:
	"""Write what the source held and what this copy is for, beside the copy.

	**Failure is swallowed, for `#505`'s reason.** The bytes have already landed by the time
	this runs; a backup that arrived intact must never be reported as failed because a note
	about it could not be written. The listing degrades to *not recorded*, which is what every
	backup taken before this existed says anyway.

	**What that costs once retention reads this** (`#1712`): a copy whose note could not be
	written is unlabelled, so it is treated as routine, so nothing removes it unless the
	operator asks. That is the safe direction — a rollback point nobody can identify is kept
	rather than deleted — and it is why this may stay best-effort.
	"""

	with contextlib.suppress(OSError, TypeError, ValueError):
		beside = _record_beside(target)

		beside.write_text(
			json.dumps({"holdings": holdings, "taken_for": taken_for}, indent=1),
			encoding="utf-8",
		)

		# **The same mode as the backup it describes** (`#927`'s L-8). This was written at
		# whatever umask was in force, so a directory of `-rw-------` copies carried one
		# `-rw-rw-r--` note beside each — and the note says how many rows of each kind the
		# instance holds. Row counts are not the tasks, which is why this is small; a backup
		# directory where one file in two is world-readable is the part worth not having.
		subroutine.config.keep_private(beside)


@dataclasses.dataclass(frozen=True)
class _Note:
	"""What a backup's own record says about it, with every part of it optional.

	Both fields are absent from some real file on some real disk: ``holdings`` from any copy
	taken before `#432`, and ``taken_for`` from any taken before `#1712`. Reading a note is
	therefore never all-or-nothing — an old copy answers the first question and not the second.
	"""

	holdings: dict[str, int] | None = None
	taken_for: str | None = None


def _recorded (path: pathlib.Path) -> _Note:
	"""Return what a backup recorded about itself, with ``None`` for anything nothing recorded.

	**``None`` is *not recorded*, and it is not *empty*** — the distinction `#432` is about.
	Every backup taken before this existed has no record, and reporting those as holding
	nothing would be the same false confidence in the opposite direction.
	"""

	record = _record_beside(path)

	if not record.is_file():
		return _Note()

	try:
		loaded = json.loads(record.read_text(encoding="utf-8"))

	except (OSError, ValueError):
		return _Note()

	if not isinstance(loaded, dict):
		return _Note()

	held = loaded.get("holdings")
	reason = loaded.get("taken_for")

	return _Note(
		holdings=(
			{
				str(name): int(count)
				for name, count in held.items()
				if isinstance(count, int)
			}
			if isinstance(held, dict)
			else None
		),

		# **Anything not in the vocabulary is read as unrecorded**, rather than carried through
		# as itself. A note is a file on a disk somebody else administers, so it can hold a word
		# from a later version or a word somebody typed — and an unknown reason matching no
		# retention rule would be a copy nothing ever removes, which is the leak this closes.
		taken_for=reason if reason in TAKEN_FOR_WORDS else None,
	)


def _described (path: pathlib.Path) -> Backup | None:
	"""Describe a file if its name is one of ours, or return ``None`` if it is not.

	Deliberately tolerant: a directory that has had something else dropped into it should
	list the backups it does have rather than refuse to list anything.
	"""

	# **Derived from :data:`ENGINE_OF_SUFFIX` rather than listed** (`SR#1554`). This was a
	# hardcoded pair and it went stale the moment there was a third suffix: archives were
	# written correctly, restored correctly, and were invisible to `catalogue`, `prune` and
	# every surface that lists what is there. Two declarations of one set, agreeing until
	# somebody added to one of them.
	if path.suffix not in ENGINE_OF_SUFFIX or not path.is_file():
		return None

	parts = path.stem.split("-")

	if len(parts) < 4 or parts[0] != subroutine.config.APPLICATION_NAME:
		return None

	taken_at = _parsed_stamp(parts[-2])

	if taken_at is None:
		return None

	label = "-".join(parts[1:-2])
	note = _recorded(path)

	return Backup(
		path=path,
		taken_at=taken_at,
		schema_head=parts[-1],
		size_bytes=path.stat().st_size,
		profile=None if label == DEFAULT_LABEL else label,
		holdings=note.holdings,
		taken_for=note.taken_for,
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
#: `.db` is a SQLite database, `.sql` is a script only ``psql`` can read, and `.dump` is an
#: archive only ``pg_restore`` can read. None of the three is interchangeable with another.
#:
#: **Two PostgreSQL entries, and the engine is not what tells them apart** — :func:`is_archive`
#: is. The engine decides which server can read a file back; the *format* decides which tool
#: does, and since `#1554` those are different questions.
ENGINE_OF_SUFFIX = {
	SQLITE_SUFFIX: "SQLite",
	POSTGRESQL_SUFFIX: "PostgreSQL",
	POSTGRESQL_ARCHIVE_SUFFIX: "PostgreSQL",
}


def engine_in (path: pathlib.Path) -> str:
	"""Return which engine a backup was taken from — ``SQLite`` or ``PostgreSQL``."""

	found = ENGINE_OF_SUFFIX.get(path.suffix)

	if found is None:
		raise subroutine.errors.BadRequest(
			f"'{path.name}' is not a Subroutine backup: expected a name ending in "
			f"{SQLITE_SUFFIX}, {POSTGRESQL_ARCHIVE_SUFFIX} or {POSTGRESQL_SUFFIX}."
		)

	return found


def is_archive (path: pathlib.Path) -> bool:
	"""Report whether this backup is a ``pg_dump`` archive rather than a script or a database.

	**The question `engine_in` cannot answer**, and since `#1554` it decides four things: which
	tool restores the file, how its schema head and its tables are read out of it, and whether
	the meta-command scan applies to it at all.

	Asked of the name for :data:`ENGINE_OF_SUFFIX`'s reason. The archive also begins with a
	``PGDMP`` magic number, and ``pg_restore`` refuses a text dump by name — so every read
	below fails honestly on a renamed file rather than misreading it.
	"""

	return path.suffix == POSTGRESQL_ARCHIVE_SUFFIX


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

	**Asked twice, and something that has gone by the second answer was not using it** (`#725`).
	The gate refused a restore twice in one day over *"1 other connection"* that nothing could
	account for, on a database the test had exclusively to itself — four hypotheses were put up
	and measured away (a lingering ``pg_dump``, a disposed pool's backend, an autovacuum worker,
	and 504 restore runs under eight-way parallel load without a reproduction), so the cause is
	**not identified** and this does not claim to fix it.

	What it does claim is narrower and holds whatever the cause turns out to be: **this guard
	protects against a database somebody is *using*, and use persists.** A running service holds
	its connection for as long as it runs, so a recheck a moment later cannot miss one — while
	anything that has vanished in that moment was, by any reading of the word, not using it.

	So the retry costs a quarter of a second on a command run rarely and under pressure, and it
	buys back a refusal that an operator mid-recovery cannot diagnose and can only respond to by
	guessing. The failure it removes is the expensive direction: this refuses *safe*, and a
	guard that cries wolf during a recovery is one somebody learns to pass ``--force`` to.
	"""

	# Our own pool would otherwise answer for somebody else. This is called before anything is
	# read or written, so there is nothing in flight to lose.
	engine.dispose()

	asked = _sqlite_in_use_by if _is_sqlite(engine) else _postgresql_in_use_by
	holder = asked(engine)

	if holder is None:
		return None

	time.sleep(_SETTLE_SECONDS)

	# **The second answer is the one reported**, not the first: if something really is holding
	# the database, its description a moment later is the more current of the two.
	return asked(engine)


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
	"""Report who else is connected to the PostgreSQL database, and what they are doing.

	**Named rather than counted** (`#725`). This said *"1 other connection to the database"*,
	which is the same sentence whether a colleague is connected, your own service is running,
	or something is on its way out — three situations with three different next actions, and an
	operator halfway through a recovery cannot tell them apart. ``pg_stat_activity`` already
	carries every column needed to say which it is.
	"""

	try:
		with engine.connect() as connection:
			others = connection.execute(
				sqlalchemy.text(
					"SELECT backend_type, state, application_name, "
					"       extract(epoch from (now() - backend_start)) AS age "
					"FROM pg_stat_activity "
					"WHERE datname = current_database() AND pid <> pg_backend_pid()"
				)
			).all()

	except sqlalchemy.exc.SQLAlchemyError:
		# Same reasoning as the SQLite side: a database that cannot be reached is not one
		# somebody else is using, and refusing here would block the rescue.
		return None

	if not others:
		return None

	described = [
		", ".join(
			part for part in (
				str(row.backend_type or "connection"),
				str(row.state) if row.state else None,
				f"as {row.application_name}" if row.application_name else None,
				f"{float(row.age):.0f}s old" if row.age is not None else None,
			) if part
		)
		# Three is enough to recognise what they are; a service with forty would otherwise
		# fill the terminal with the same line.
		for row in others[:3]
	]

	if len(others) > 3:
		described.append(f"and {len(others) - 3} more")

	plural = "" if len(others) == 1 else "s"

	return f"{len(others)} other connection{plural} — {'; '.join(described)}"


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


def _text_of (path: pathlib.Path) -> str:
	"""Return a backup's text, refusing by name when it cannot be read — `SR#1695`.

	**The one failure the module did not handle was the most mundane one.**
	:func:`check_restorable` refuses a schema newer than this code by name, and tells a file
	that records a schema but has no core tables that it is not a backup rather than that its
	schema is too new (`#928`) — and then read the file with no guard at all, so an unreadable
	one raised ``PermissionError`` through Typer and wrote a crash report asking the operator
	to open an issue.

	**It lands at the worst moment.** A restore is what somebody runs when something has
	already gone wrong, and permissions are *most* likely to be wrong exactly then: a dump
	copied between machines, pulled out of object storage as root, or carried into a
	``DynamicUser`` unit's state directory by a root process — which is where this was found.
	A stack trace is the wrong answer to *the file is 0600 and owned by somebody else*.

	**One reader for all three call sites**, rather than a guard at the one that was reported.
	``_head_in_dump``, ``_tables_in_dump`` and :func:`refuse_unsafe_commands` each read the same
	path, and the item was found through the first — so fixing only that one would have left a
	restore of a readable-then-unreadable file crashing two functions later. The SQLite side
	needs none of this: it opens through ``sqlite3``, whose ``OperationalError`` is already
	caught and reported.

	``strerror`` rather than the whole exception, because ``str(OSError)`` carries the errno and
	the path again and the message already names the file.
	"""

	try:
		return path.read_text(encoding="utf-8", errors="replace")

	except OSError as error:
		raise subroutine.errors.BadRequest(
			f"'{path.name}' could not be read: {error.strerror}.",
			hint="Check the file is readable by the account running this. A backup copied "
			"from another machine, or written by a different service, often is not.",
		) from error


def _as_text (path: pathlib.Path, *arguments: str) -> str:
	"""Return part of a custom-format archive as the plain text ``pg_dump`` would have written.

	**This is what lets `#1554` change the format without changing what reads it.** The two
	parsers below — one for ``alembic_version``, one for ``CREATE TABLE`` — were written
	against a text dump and are unchanged; an archive is converted to the same text in front of
	them, for the part being asked about rather than for the whole file.

	Nothing here connects to a server: ``pg_restore`` writing to a file is a pure read of the
	archive, so no credential is needed and none is passed.

	A renamed text dump fails here by name — *"input file appears to be a text format dump"* —
	rather than being misread, which is what makes :func:`is_archive` safe to ask of a suffix.
	"""

	return _run(
		["pg_restore", *arguments, "--file", "-", str(path)], what="pg_restore"
	)


def _head_in_dump (path: pathlib.Path) -> str:
	"""Find ``alembic_version`` in a PostgreSQL backup, whichever format it is in."""

	# **One table out of the archive rather than all of it** — the answer is one short `COPY`
	# block, and converting a whole dump to text to read six characters out of it would make
	# `check_restorable` proportional to the size of the database.
	text = (
		_as_text(path, "--data-only", "--table=alembic_version")
		if is_archive(path)
		else _text_of(path)
	)
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


def _refuse_a_corrupt_copy (path: pathlib.Path) -> None:
	"""Refuse a delivered SQLite backup whose pages do not hold together (`#928`).

	**The size and the schema head are both satisfied by a torn file.** A copy that lost pages
	in the middle is very often exactly the right length and still answers
	``SELECT version_num`` — the two checks beside this one were measured passing 112 of 121
	deliberately corrupted copies. ``PRAGMA integrity_check`` is what actually reads the pages.

	**A custom-format archive is checked too, and by asking its own reader** (`#1554`).
	``pg_restore --list`` reads the whole table of contents and fails on a truncated file —
	measured: an archive cut short answers *"could not read from input file: end of file"*.
	That is strictly better than what a plain dump gets, which is still only the size
	comparison, because a script has no structure to check.
	"""

	if is_archive(path):
		_refuse_an_unreadable_archive(path)

		return

	if engine_in(path) != "SQLite":
		return

	connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

	try:
		# The pragma answers a single row reading ``ok``, or one row per problem found — and
		# a copy torn badly enough raises instead of answering at all. Both are the same fact
		# about the file, so both become the same sentence rather than one of them reaching
		# the operator as a driver error.
		answered = [row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()]

	except sqlite3.Error as error:
		raise subroutine.errors.ServiceUnavailable(
			f"The backup written to {path} is the right size and names its schema, and "
			f"could not be read back: {error}. It has been removed rather than left looking "
			f"usable."
		) from error

	finally:
		connection.close()

	if answered != ["ok"]:
		raise subroutine.errors.ServiceUnavailable(
			f"The backup written to {path} is the right size and names its schema, and its "
			f"contents do not hold together. It has been removed rather than left looking "
			f"usable."
		)


def _refuse_an_unreadable_archive (path: pathlib.Path) -> None:
	"""Refuse a delivered archive ``pg_restore`` cannot read back.

	The archive's counterpart to ``PRAGMA integrity_check``: the table of contents is at the
	end of the file, so listing it reads through the whole thing and a torn write cannot pass.

	**Its message is the SQLite one's**, because it is the same fact about the same moment —
	the copy is the right size, it names its schema, and it will not read back. What differs is
	only which tool noticed.
	"""

	try:
		_run(["pg_restore", "--list", str(path)], what="pg_restore")

	except subroutine.errors.SubroutineError as error:
		raise subroutine.errors.ServiceUnavailable(
			f"The backup written to {path} is the right size and names its schema, and "
			f"could not be read back: {error}. It has been removed rather than left looking "
			f"usable."
		) from error


def tables_in (path: pathlib.Path) -> frozenset[str]:
	"""Return the names of the tables a backup contains.

	The companion to :func:`head_in`, and asked for the same reason: what a file *claims* about
	itself decides whether real data gets overwritten, so the claim is read from the file
	rather than believed.
	"""

	if engine_in(path) == "SQLite":
		return _tables_in_sqlite(path)

	return _tables_in_dump(path)


def _tables_in_sqlite (path: pathlib.Path) -> frozenset[str]:
	"""List the tables in a SQLite backup."""

	try:
		connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

	except sqlite3.Error as error:
		raise subroutine.errors.BadRequest(
			f"'{path.name}' could not be opened as a database: {error}"
		) from error

	try:
		rows = connection.execute(
			"SELECT name FROM sqlite_master WHERE type = 'table'"
		).fetchall()

	except sqlite3.Error as error:
		raise subroutine.errors.BadRequest(
			f"'{path.name}' could not be read as a database: {error}"
		) from error

	finally:
		connection.close()

	return frozenset(str(row[0]) for row in rows)


def _tables_in_dump (path: pathlib.Path) -> frozenset[str]:
	"""List the tables a PostgreSQL backup creates, whichever format it is in.

	**The schema rather than ``pg_restore --list``**, deliberately. The archive's table of
	contents would answer this too, and its columns are a report rather than a contract —
	parsing it would be a second way of reading a table name, which is this codebase's
	signature defect. Converting the schema back to text costs one subprocess and lets the
	regex below stay the only place that knows what a table declaration looks like.
	"""

	text = _as_text(path, "--schema-only") if is_archive(path) else _text_of(path)

	# The schema qualifier and the quoting are both optional and both appear in the wild, so
	# the name is taken as the last dotted part with any quotes stripped.
	found = re.findall(r"CREATE TABLE\s+([\w.\"]+)", text, re.IGNORECASE)

	return frozenset(name.split(".")[-1].strip('"') for name in found)


def refuse_unsafe_commands (path: pathlib.Path) -> None:
	"""Refuse a dump carrying a ``psql`` meta-command that is not one ``pg_dump`` writes.

	**A restore runs its source through ``psql``, which executes backslash commands.** ``\\!``
	is a shell escape, ``\\i`` includes another file and ``\\copy`` reads and writes the
	filesystem — so without this a backup file is arbitrary code execution as the operator, and
	``docs/hosting.md`` invites putting backups on a shared volume (`#928`).

	**This does not read as ``psql`` reads it, and it used to say that it did** (`SR#1554`).
	The sentence here was *"read as psql reads it, because anything less is a different
	question"*, which is the requirement rather than a description — and it is why nobody
	looked. What this actually models is two states, inside or outside a ``COPY … FROM stdin;``
	block. ``psql`` also tracks single-quoted strings, E-strings, dollar-quoted strings, ``--``
	comments and ``/* … */`` comments, and accepts a meta-command **anywhere a statement may
	end** rather than only at the start of a line.

	**Five shapes were driven past it**, each allowed here and executed by psql: a mid-line
	``SELECT 1; \\! …``; and a ``COPY … FROM stdin;`` inside a block comment, inside a
	dollar-quote, or inside a string literal, each of which puts this scan into copy mode —
	where psql is not — after which every remaining line is skipped until a lone ``\\.`` that a
	forged file never provides.

	**`SR#1554` closed the class rather than widening this.** Every backup this program writes
	is a ``pg_dump --format=custom`` archive now, restored by ``pg_restore``, which has no
	meta-command lexer at all — so there is nowhere in a forged file to put an instruction, and
	this returns immediately for one. Widening the scan would have meant reimplementing
	``psqlscan.l`` and being wrong again, which is what this docstring used to promise.

	**What is left is the files that already exist.** A plain `.sql` taken before that change is
	still restorable, because refusing it would take away somebody's only copy on the worst day
	they will have — so this still runs on those, still catches the shapes it knows, and is
	still not a boundary. That path empties as old backups age out and nothing refills it.

	The COPY tracking below is still right about what it does: inside such a block every line
	is data and a leading backslash is an escape, so a scan without it would refuse ordinary
	rows that happen to begin with one.
	"""

	# **Not a scan that passes — a question that does not arise** (`SR#1554`). An archive is
	# not a script, so there is no lexer to disagree with and nothing to read.
	if is_archive(path) or engine_in(path) != "PostgreSQL":
		return

	inside_copy = False

	for number, line in enumerate(_text_of(path).splitlines(), start=1):
		if inside_copy:
			inside_copy = line.rstrip() != "\\."
			continue

		if _COPY_BEGINS.match(line):
			inside_copy = True
			continue

		found = _META_COMMAND.match(line)

		if found is None or found.group(1) in _SAFE_META_COMMANDS:
			continue

		raise subroutine.errors.BadRequest(
			f"'{path.name}' line {number} carries the command '\\{found.group(1)}', which "
			f"'pg_dump' does not write. A backup that runs commands is not a backup — this "
			f"one has not been restored.",
			hint="Take a fresh backup with 'subroutine db backup', and treat this file as "
			"hostile rather than as damaged.",
		)


def take (
	engine: sqlalchemy.engine.Engine,
	settings: subroutine.config.Settings,
	*,
	taken_for: str = ROUTINE,
	keep: int | None = None,
	moment: datetime.datetime | None = None,
) -> Backup:
	"""Copy the database into a datetime-stamped file and return what was written.

	``keep`` prunes to that many most recent **routine** backups afterwards, and applies to
	nothing else. Nothing routine is deleted unless it is asked for: a backup command that
	quietly removes old backups is one bad default away from causing the loss it exists to
	prevent.

	``taken_for`` says which of three lifetimes this copy has, and **the two the program takes
	on its own initiative bound themselves** (`#1712`). That is not a contradiction of the
	sentence above: an operator's routine copies are theirs and are removed only on request,
	while a pre-upgrade rollback point and a pre-restore safety copy are copies nobody asked
	for, and something has to bound them or they accumulate for ever — which was `#1676`.

	**The retention rule is chosen here, at the one place every writer passes through**, so a
	surface that has not been written yet inherits it. It has a default because a copy taken by
	something that never considered the question is safest counted as routine, and a guard in
	``tests/test_instances.py`` requires every caller under ``src`` to say which it means anyway.
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

	suffix = SQLITE_SUFFIX if _is_sqlite(engine) else POSTGRESQL_ARCHIVE_SUFFIX
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

	held = _holdings(engine)

	# **Written now, because now is the only moment anything knows** (`#432`). The counts come
	# off the *source*, and by the time somebody lists this directory the source may have moved
	# on or gone. A listing that derived them instead would have to open every copy, which turns
	# a directory scan into reading every file in it — and cannot do it at all for a `pg_dump`.
	_record(target, held, taken_for)

	return Backup(
		path=target,
		taken_at=taken_at,
		schema_head=head,
		size_bytes=size,
		holdings=held,
		profile=active,
		taken_for=taken_for,
		removed=tuple(_pruned_after(settings, taken_for=taken_for, keep=keep, now=taken_at)),
	)


def _pruned_after (
	settings: subroutine.config.Settings,
	*,
	taken_for: str,
	keep: int | None,
	now: datetime.datetime,
) -> list[Backup]:
	"""Apply the retention rule for the kind of copy that was just taken.

	**Each kind counts only its own** (`#1712`), which is the whole of the fix. An hourly timer
	asking for the newest twenty-four cannot then evict the rollback point for the upgrade that
	most recently went wrong, and an upgrade cannot evict a month of somebody's nightly copies
	as a side effect of upgrading — the two failures that made one shared counter unworkable.
	"""

	if taken_for == BEFORE_UPGRADE:
		return prune_rollback_points(settings, keep=settings.backup_keep_upgrades)

	if taken_for == BEFORE_RESTORE:
		return prune_restore_copies(settings, now=now)

	# `keep` is the operator's, and it is the only one of the three that is opt-in. Its absence
	# means they did not ask, which is different from asking for everything to be kept — but
	# not here, because the two have the same answer.
	return [] if keep is None else prune(settings, keep=keep)


#: What is counted to say whether a backup holds anything — item `#395`. The tables that carry
#: *work* rather than vocabulary: a seeded but unused instance has statuses and roles in it, so
#: counting those would report every empty instance as full.
COUNTED = ("workspace", "project", "task", "document")


def _holdings (engine: sqlalchemy.engine.Engine) -> dict[str, int]:
	"""Count what the source held, so an empty backup cannot read as a successful one.

	**Counted on the source rather than on the copy**, which is the cheaper half of the same
	question and the one that is always answerable: the copy may be a `pg_dump` script, which
	is not a database anything can open without restoring it somewhere first.

	Failures are swallowed to an empty mapping on purpose. This exists to make a backup
	*legible*, and a backup that succeeded must not be reported as failed because a count
	afterwards did — that would be this check causing the loss it was written to prevent.
	"""

	counted: dict[str, int] = {}

	# **A connection per table, deliberately** (`#430`). Each iteration isolates its own
	# failure: on PostgreSQL a refused count aborts the transaction it is in, so a shared
	# connection would let one unreadable table poison every count after it — and this
	# function's whole purpose is being *legible* about what a backup holds. Hoisting the
	# connection out reads as the obvious tidy-up, which is why it is worth a sentence.
	for table in COUNTED:
		try:
			with engine.connect() as connection:
				found = connection.execute(
					# `table` is never user input: it comes from `COUNTED`, a literal tuple in
					# this module, and an interpolation is needed because a table name cannot
					# be a bound parameter.
					sqlalchemy.text(f"select count(*) from {table}")
				).scalar_one()

		except sqlalchemy.exc.SQLAlchemyError:
			return {}

		counted[table] = int(found)

	return counted


def _delivered (
	staged: pathlib.Path, target: pathlib.Path, *, head: str, size: int
) -> None:
	"""Copy a finished backup to its destination and prove that what arrived is readable.

	**A half-written file on a network volume is the failure worth spending code on**, because it
	looks like a backup: it appears in the catalogue, its name says which schema it holds, and it
	is discovered to be short only on the day it is needed. So the copy is checked where it landed
	— its size, and that its schema version can still be read out of it — and a file that fails
	is deleted rather than left looking valid.

	**Data only, and that is the whole of `#505`.** A rename cannot cross a filesystem and this
	share does not honour the create-then-rename dance either, so neither ``os.replace`` nor
	``os.rename`` can be used — but ``shutil.move``'s cross-filesystem fallback is ``copy2``,
	which copies *metadata* as well as data, and ``copystat``'s ``os.utime`` and ``os.chmod``
	raise ``EPERM`` for a process that does not own the destination file. Under a ``forceuid``
	mount that is every process but one, so it is permanent rather than intermittent.

	**The bytes have already arrived when it raises.** The served instance wrote three complete,
	restorable backups to the RAID volume on 2026-08-05 and reported every one of them as a
	failure — over HTTP as a ``503``, which says the *instance* is unwell rather than that one
	operation could not finish. A backup's mode and timestamps carry nothing anyway: the name
	holds the moment and the schema head, and :func:`subroutine.config.keep_private` sets the
	mode on the next line.

	The staged copy is not removed here. :func:`take` owns it in a ``finally``, so it goes
	whether this succeeds or not.
	"""

	try:
		shutil.copyfile(staged, target)

	except OSError as error:
		# **A copy that stopped part way leaves a short file behind**, which is the exact thing
		# this function exists to prevent — and the verification below never runs on this path,
		# so the removal cannot be left to it.
		with contextlib.suppress(OSError):
			target.unlink(missing_ok=True)

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

		_refuse_a_corrupt_copy(target)

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
	"""Write a custom-format ``pg_dump`` archive of the configured database.

	**``--format=custom`` is the fix for `#1554` and not an optimisation.** A plain dump is a
	script `psql` runs, and `psql` executes backslash meta-commands inside one; an archive is
	read by ``pg_restore``, which has no meta-command lexer, so a forged file has nowhere to
	put an instruction. :data:`POSTGRESQL_ARCHIVE_SUFFIX` carries the measurement.

	The two flags beside it are unchanged and are about *restoring somewhere else*: a dump that
	names its original owner and grants cannot be loaded by an account that is not that owner.
	"""

	_run(
		[
			"pg_dump",
			"--format=custom",
			"--no-owner",
			"--no-privileges",
			"--file",
			str(target),
			"--dbname",
			_connectable(engine),
		],
		what="pg_dump",
		secrets=_secret_of(engine),
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

	**The password is not in it**, and :func:`_secret_of` is the other half: a command line is
	world-readable in ``/proc/<pid>/cmdline`` for as long as the process runs, and a dump of a
	large database runs for minutes. It travels in the child's environment instead, which is
	what ``PGPASSWORD`` is for.
	"""

	url = engine.url

	# **``_replace`` rather than ``set``, and this is a trap worth naming.** ``URL.set`` builds
	# from the arguments that are *not* ``None``, so ``set(password=None)`` is "leave it alone"
	# and quietly returns the URL with the password still in it — which passes every reading of
	# this function and defeats the whole point of it. ``set(password="")`` leaves a bare colon
	# that some tools then send as an empty password.
	return url._replace(
		drivername=url.get_backend_name(), password=None
	).render_as_string(hide_password=False)


def _secret_of (engine: sqlalchemy.engine.Engine) -> dict[str, str]:
	"""Return the environment a PostgreSQL tool needs to authenticate, if it needs one.

	Empty where the URL carries no password — the ordinary case here, where authentication is
	by Unix socket — so the child inherits this process's environment untouched and nothing
	about the common path changes.
	"""

	password = engine.url.password

	return {} if not password else {"PGPASSWORD": str(password)}


def _run (
	command: list[str], *, what: str, secrets: dict[str, str] | None = None
) -> str:
	"""Run one of the PostgreSQL tools, reporting its complaint rather than a traceback.

	``secrets`` are added to the child's environment rather than written into ``command``,
	because everything in a command line is readable by every process on the machine for as
	long as this one runs (`#927`'s M-22).

	**Returns what the tool wrote to standard output**, which was captured and discarded until
	`#1554`. Reading a custom-format archive is done by asking ``pg_restore`` to write part of
	it back as text, so the answer is the point rather than a by-product — and the pipe had to
	be drained either way.
	"""

	try:
		process = subprocess.Popen(
			command,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			env=None if not secrets else {**os.environ, **secrets},
		)

	except FileNotFoundError as error:
		raise subroutine.errors.ServiceUnavailable(
			f"{what} is not installed, and a PostgreSQL backup needs it. It comes with the "
			f"PostgreSQL client tools."
		) from error

	# `communicate` rather than `wait`: a pipe left open trips the ResourceWarning this project
	# turns into an error, and the traceback then points at the wrong place entirely.
	output, complaint = process.communicate(timeout=_SUBPROCESS_TIMEOUT_SECONDS)

	if process.returncode != 0:
		reported = complaint.strip() or f"exit status {process.returncode}"

		raise subroutine.errors.BadRequest(f"{what} failed: {reported}")

	return output


def taken_for (backup: Backup, reason: str) -> bool:
	"""Say whether a copy counts as one taken for ``reason``.

	**An unlabelled copy is routine** (`#1712`), which is the reading that keeps more than it
	deletes: every copy on every disk predates this, so the alternative — counting unlabelled
	as *none of the three* — would make them all immortal, and the alternative to that would
	have an upgrade delete backups it cannot identify. Neither is a thing to do to somebody
	else's data.
	"""

	if backup.taken_for is None:
		return reason == ROUTINE

	return backup.taken_for == reason


def _removed (backups: list[Backup]) -> list[Backup]:
	"""Delete these copies along with their records, and return what went."""

	for backup in backups:
		backup.path.unlink(missing_ok=True)

		# The record goes with the copy it describes. Left behind it would accumulate one file
		# per deleted backup for ever, and — worse — a later backup that happened to be given
		# the same name would inherit somebody else's counts.
		_record_beside(backup.path).unlink(missing_ok=True)

	return backups


def prune (settings: subroutine.config.Settings, *, keep: int) -> list[Backup]:
	"""Delete all but the ``keep`` most recent *routine* backups, returning what was removed.

	**Routine only, since `#1712`.** This counted every copy in the directory by age, so an
	hourly timer with ``--keep 24`` reached back one day and deleted the pre-upgrade copy for
	an upgrade that had gone wrong the day before — the copy somebody wants precisely then.
	"""

	if keep < 1:
		raise subroutine.errors.ValidationError(
			f"--keep must be 1 or more, not {keep}: pruning to nothing would delete every "
			f"routine backup this instance has."
		)

	routine = [backup for backup in catalogue(settings) if taken_for(backup, ROUTINE)]

	return _removed(routine[keep:])


def prune_rollback_points (settings: subroutine.config.Settings, *, keep: int) -> list[Backup]:
	"""Delete all but the ``keep`` most recent pre-upgrade copies, returning what was removed.

	**This may never reach a routine backup**, which is the door `#1712` deliberately kept shut:
	an operator with thirty nightly copies must not lose twenty-seven of them as a side effect
	of upgrading. Destroying somebody's backups is not something an upgrade may do quietly.
	"""

	if keep < 1:
		raise subroutine.errors.ValidationError(
			f"backup_keep_upgrades must be 1 or more, not {keep}: an upgrade takes a rollback "
			f"point and then keeps this many, so nought would delete the copy it just took — "
			f"leaving the upgrade with no way back at the moment one is most likely wanted."
		)

	points = [backup for backup in catalogue(settings) if taken_for(backup, BEFORE_UPGRADE)]

	return _removed(points[keep:])


def prune_restore_copies (
	settings: subroutine.config.Settings, *, now: datetime.datetime | None = None
) -> list[Backup]:
	"""Delete pre-restore safety copies older than :data:`RESTORE_SAFETY_LIFETIME`.

	**By age rather than by count**, because what this copy protects against is a restore that
	turns out to have been the wrong one — and that is discovered on a human timescale rather
	than after a certain number of further restores. Somebody who restores three times in an
	afternoon keeps all three ways back; somebody who restored in March does not still carry it.
	"""

	edge = (now or datetime.datetime.now(datetime.UTC)) - RESTORE_SAFETY_LIFETIME
	stale = [
		backup
		for backup in catalogue(settings)
		if taken_for(backup, BEFORE_RESTORE) and backup.taken_at < edge
	]

	return _removed(stale)


def check_restorable (path: pathlib.Path) -> str:
	"""Return a backup's schema head, refusing one this installation cannot interpret.

	The asymmetry is the safety property (docs/design.md §12.6). An *older* schema can be migrated
	forward, which is what Alembic is for. A *newer* one is refused: the running code does not
	know the columns, so "try anyway" means a silent misread rather than a visible failure.
	"""

	backup_head = head_in(path)
	ours = subroutine.db.migrate.head_revision()

	if ours is None:
		raise subroutine.errors.InternalError(
			"This installation has no migrations, so it cannot judge whether a backup fits."
		)

	# A schema version is one string, and one string is not evidence that a file holds a
	# database. Checked before the revision comparison below so that a file which is not a
	# backup is told so, rather than being told its schema is too new (`#928`).
	missing = CORE_TABLES - tables_in(path)

	if missing:
		raise subroutine.errors.BadRequest(
			f"'{path.name}' records a schema version but is missing "
			f"{len(missing)} of the tables every Subroutine database has: "
			f"{', '.join(sorted(missing))}. It has not been restored.",
			hint="Check that this is the file you meant. A backup taken by "
			"'subroutine db backup' carries the whole database.",
		)

	refuse_unsafe_commands(path)

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

	# **The copy carries the source's mode, so the live database inherits it** (`SR#1563`).
	# `copy2` preserves permissions, and `docs/hosting.md` invites keeping backups on a shared
	# volume — so restoring a file that arrived 0644 left the database holding every task,
	# comment and token hash readable by every account on the machine. `#175`'s own argument,
	# undone by a restore: *"§12.1a says there is no local password prompt because anyone who
	# can read the file can read every row with sqlite3 — which is an argument for the
	# filesystem permission being right, not for it being ignored."*
	#
	# **`migrate.upgrade` does not reach here and its docstring said it did.** It is called
	# only when the backup's schema head differs from the running one, so the protection held
	# for an older backup and was absent for a current one — a conditional protection described
	# as unconditional, which is why nobody looked.
	subroutine.config.keep_private(target)

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
	"""Empty the PostgreSQL database and load the backup into it.

	**Which tool does the loading is the whole of `SR#1554`.** An archive goes through
	``pg_restore``, which executes no meta-commands; a plain script taken before that change
	still goes through ``psql``, guarded by :func:`refuse_unsafe_commands` and by that
	function's honest account of what it can and cannot see.
	"""

	# The dump recreates every table it holds, so what is there now has to go first. Dropping
	# the *schema* rather than the database means no maintenance connection is needed, and the
	# restore works on a managed server where creating databases is not permitted.
	with engine.begin() as connection:
		connection.exec_driver_sql("DROP SCHEMA public CASCADE")
		connection.exec_driver_sql("CREATE SCHEMA public")

	engine.dispose()

	if is_archive(source):
		_run(
			[
				"pg_restore",
				"--no-owner",
				"--no-privileges",
				# The same guarantee `--single-transaction` buys the script path: a restore
				# that fails part-way leaves nothing rather than half a schema and no data.
				"--single-transaction",
				# Without this `pg_restore` reports errors and carries on, so a failed restore
				# would exit zero and be reported as a success.
				"--exit-on-error",
				"--dbname",
				_connectable(engine),
				str(source),
			],
			what="pg_restore",
			secrets=_secret_of(engine),
		)

		return

	_run(
		[
			"psql",
			"--quiet",
			# The operator's ``~/.psqlrc`` is a script this process would otherwise run as a
			# side effect of restoring somebody else's file (`#928`).
			"--no-psqlrc",
			# So a dump that fails part-way leaves nothing rather than half a schema and no
			# data. The schema is dropped in its own committed transaction above, so the
			# failure is visible either way — this decides whether it is also recoverable.
			"--single-transaction",
			"--set",
			"ON_ERROR_STOP=on",
			"--file",
			str(source),
			"--dbname",
			_connectable(engine),
		],
		what="psql",
		secrets=_secret_of(engine),
	)


def _reidentify (database_url: str) -> None:
	"""Give the restored database a new ``instance_id``, and forget the stored context.

	The context goes because it names a connection and a workspace that belonged to the
	original (docs/design.md §13.7), and a clone pointing at the original's context is the confusion
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
