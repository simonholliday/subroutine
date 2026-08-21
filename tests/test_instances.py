"""Separate instances on one machine, and backing them up — docs/design.md §12.5, §12.6 and §12.6a.

These exist because the project is about to keep its own plan in a database it can no longer
reset. Two properties matter more than the rest and each has a test named for it: **a profile
isolates completely**, and **a restore never guesses whether it is a recovery or a clone**.

Restore is tested against a database this file creates and owns. It must never run against the
shared session-scoped one: the PostgreSQL path drops and recreates `public`, and the shared
database is where every other test in the suite lives.
"""

import datetime
import errno
import itertools
import os
import pathlib
import sqlite3
import subprocess
import sys
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm
import typer.testing

import conftest
import subroutine.cli.main
import subroutine.config
import subroutine.db.backup
import subroutine.db.base
import subroutine.db.migrate
import subroutine.db.models.system
import subroutine.db.session
import subroutine.errors
import test_api_tasks


@pytest.fixture
def home (tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
	"""Point every XDG directory at a fresh temporary home, with nothing inherited.

	``SUBROUTINE_PROFILE`` is cleared along with the rest: a leaked profile name would send
	every one of these tests at a different instance than the one it built.
	"""

	root = tmp_path / "home"

	for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
		monkeypatch.setenv(variable, str(root / variable.lower()))

	for name in list(os.environ):
		if name.startswith("SUBROUTINE_"):
			monkeypatch.delenv(name, raising=False)

	monkeypatch.setenv("SUBROUTINE_DEFAULT_TIMEZONE", "Europe/London")

	return root


@pytest.fixture
def run (home: pathlib.Path) -> typing.Callable[..., typer.testing.Result]:
	"""Return a runner for the real CLI, failing loudly on an unexpected exit code."""

	runner = typer.testing.CliRunner()

	def invoke (
		*arguments: str, expect: int = 0, answers: str | None = None
	) -> typer.testing.Result:
		"""Run one command and check how it ended.

		The profile variable is cleared first, so each call behaves like a fresh shell.
		``--profile`` deliberately *exports* itself so that anything the process starts
		inherits the same instance (§12.5) — which in one process makes the choice stick to
		the next invocation, and a test that shared it would be testing the runner.

		``answers`` is what a person would type at a prompt, which is the only way to exercise
		a command's *declining* path — and a destructive command's refusal is as much of its
		behaviour as the act.
		"""

		# Each call is a fresh shell, and in a real one each command is its own process —
		# so the once-per-process configuration warning is once per command. Reset here
		# rather than per test, or the first `init` in a test consumes it for the rest.
		subroutine.cli.main._said_unknown_settings = False

		os.environ.pop(subroutine.config.PROFILE_VARIABLE, None)

		result = runner.invoke(subroutine.cli.main.app, list(arguments), input=answers)

		assert result.exit_code == expect, (
			f"'subroutine {' '.join(arguments)}' exited {result.exit_code}\n"
			f"{result.output}\n{result.exception!r}"
		)

		return result

	return invoke


# --------------------------------------------------------------------------------------
# Profile isolation (§12.5)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
	"name",
	[
		"2026",  # all digits — would become a path segment that reads as a number
		"a/b",  # a separator is a path, not a name
		"",
		" ",
		"-leading",
		"x" * (subroutine.config.MAX_PROFILE_NAME_LENGTH + 1),
	],
)
def test_an_unusable_profile_name_is_refused (name: str) -> None:
	"""§12.5 shapes a profile name like a workspace short name, and for the same reason."""

	with pytest.raises(ValueError):
		subroutine.config.check_profile_name(name)


@pytest.mark.parametrize("name", ["scratch", "test-2", "Work_1", "a"])
def test_a_usable_profile_name_is_accepted (name: str) -> None:
	"""The rule refuses what it must and nothing more."""

	assert subroutine.config.check_profile_name(name) == name


def test_a_profile_moves_every_directory_together (home: pathlib.Path) -> None:
	"""All four of an instance's files move, or the isolation is a half-measure."""

	default = (
		subroutine.config.config_home(),
		subroutine.config.data_home(),
		subroutine.config.state_home(),
	)

	subroutine.config.use_profile("scratch")

	moved = (
		subroutine.config.config_home(),
		subroutine.config.data_home(),
		subroutine.config.state_home(),
	)

	for before, after in zip(default, moved, strict=True):
		assert after == before / subroutine.config.PROFILES_DIRECTORY / "scratch"

	# And the database follows, which is the one that actually holds the work.
	assert "profiles/scratch" in subroutine.config.default_database_url()


def test_a_broken_profile_name_raises_rather_than_using_the_default (
	home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""**The important one.** Falling back would act on the instance holding real work.

	A mistyped ``SUBROUTINE_PROFILE`` must not quietly resolve to the default instance — that
	is precisely the accident §12.5 exists to prevent, and it would be invisible.
	"""

	monkeypatch.setenv(subroutine.config.PROFILE_VARIABLE, "2026")

	with pytest.raises(ValueError):
		subroutine.config.config_home()


def test_profiles_are_listed_once_they_exist (home: pathlib.Path) -> None:
	"""``profile list`` reads the configuration root, so an instance appears when it is made."""

	assert subroutine.config.profile_names() == []

	for name in ("beta", "alpha"):
		subroutine.config.use_profile(name)
		subroutine.config.config_home().mkdir(parents=True)

	subroutine.config.use_profile(None)

	assert subroutine.config.profile_names() == ["alpha", "beta"]


def test_profile_directories_covers_all_three_roots (home: pathlib.Path) -> None:
	"""Destroying an instance has to remove all of it, so this is what it removes."""

	directories = subroutine.config.profile_directories("scratch")

	assert len(directories) == 3
	assert all(path.name == "scratch" for path in directories)
	assert len({path.parent.parent for path in directories}) == 3


def test_two_instances_do_not_see_each_other (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""End to end: the whole point of §12.5, through the real command surface."""

	run("init", "--workspace", "Real")
	run("add", "Keep me")

	run("--profile", "scratch", "init", "--workspace", "Scratch")
	run("--profile", "scratch", "add", "Throwaway")

	assert "Keep me" in run("ls").output
	assert "Throwaway" not in run("ls").output

	scratch = run("--profile", "scratch", "ls").output

	assert "Throwaway" in scratch
	assert "Keep me" not in scratch


def test_destroying_an_instance_takes_all_of_it (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""A disposable instance has to be genuinely disposable, including its backups."""

	run("--profile", "scratch", "init", "--workspace", "Scratch")

	assert "scratch" in run("profile", "list").output

	# The name has to be typed back, so a shell-history recall cannot delete an instance.
	run("profile", "destroy", "scratch", expect=1)
	run("profile", "destroy", "scratch", "--confirm", "scratch")

	assert subroutine.config.profile_names() == []

	for directory in subroutine.config.profile_directories("scratch"):
		assert not directory.exists()


def test_a_protected_instance_refuses_a_restore_without_agreement (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""Protection belongs to the instance, not to whoever remembers a flag (§12.5)."""

	run("init", "--workspace", "Real")
	run("add", "Keep me")
	taken = run("db", "backup")

	configuration = subroutine.config.config_file_path()

	with configuration.open("a", encoding="utf-8") as handle:
		handle.write("\nprotected = true\n")

	name = _backup_name(taken.output)
	refused = run("db", "restore", name, "--recover", expect=1)

	assert "protected" in refused.output

	# And it goes ahead when told to out loud.
	run("db", "restore", name, "--recover", "--yes")


def _damage (path: pathlib.Path) -> None:
	"""Scramble a SQLite database's schema page, leaving the header intact.

	How a crash or a bad disk actually leaves a file: the magic still says "SQLite", so it
	opens, and it fails on the first real read. Truncating it instead would be caught earlier
	by something else and would not reach the code these tests are about.
	"""

	with path.open("r+b") as handle:
		handle.seek(100)
		handle.write(b"\x00\xff" * 1998)


def test_a_rescue_restore_completes_when_the_damaged_database_cannot_be_copied (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""`#173`, and the worst defect the clean-room review found in this command.

	The safety copy is taken *first*, it reads the database being replaced, and its failure
	used to abort the restore — so ``db restore --recover`` failed on exactly the damaged
	database it exists for. §12.4's argument is that recovery works when nothing else does;
	a safety net that blocks the rescue is not a safety net.

	The escape hatch existed — ``--no-safety-backup`` — and was named in no document and in no
	message, so the operator meeting this at 2 a.m. had no way to find it.
	"""

	run("init", "--workspace", "Real")
	run("add", "Worth recovering")

	name = _backup_name(run("db", "backup").output)
	database = _settings().sqlite_path

	assert database is not None

	_damage(database)

	restored = run("db", "restore", name, "--recover", "--yes")

	assert "Restored" in restored.output
	assert "Worth recovering" in run("list").output


def test_a_safety_copy_that_fails_is_said_out_loud (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""The other half, and the one nothing would ever have reported.

	The first fix for `#173` suppressed the failure, so a restore that saved nothing printed
	exactly what one that saved everything printed. The operator was told it worked and found
	out there was no way back only on the day they wanted one — which is later than any other
	moment they could have been told.
	"""

	run("init", "--workspace", "Real")

	name = _backup_name(run("db", "backup").output)
	database = _settings().sqlite_path

	assert database is not None

	_damage(database)

	restored = run("db", "restore", name, "--recover", "--yes")

	assert "could not be backed up" in restored.output
	assert "no way back" in restored.output

	# And the healthy case still says where the copy went, so the sentence above is a
	# difference the operator can act on rather than one that is always printed.
	healthy = run("db", "restore", name, "--recover", "--yes")

	assert "was saved to" in healthy.output
	assert "could not be backed up" not in healthy.output


def test_declining_after_a_failed_safety_copy_restores_nothing (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""It is the operator's call, which means "no" has to be a real answer.

	Going ahead regardless would be the old bug with better prose: only they know whether the
	state about to be overwritten was worth anything, and the refusal names what to do instead.
	"""

	run("init", "--workspace", "Real")

	name = _backup_name(run("db", "backup").output)
	database = _settings().sqlite_path

	assert database is not None

	_damage(database)

	refused = run("db", "restore", name, "--recover", expect=1, answers="n\n")

	assert "Nothing restored" in refused.output
	assert "--no-safety-backup" in refused.output


def _backup_name (output: str) -> str:
	"""Pull the written backup's filename out of what ``db backup`` printed."""

	for word in output.split():
		if word.endswith((".db", ".sql")):
			return pathlib.Path(word).name

	raise AssertionError(f"no backup filename in: {output}")


# --------------------------------------------------------------------------------------
# Backup, on both backends (§12.6)
# --------------------------------------------------------------------------------------


def test_a_backup_records_the_schema_it_was_taken_on (
	engine: sqlalchemy.engine.Engine, home: pathlib.Path
) -> None:
	"""The value inside is the authority, and the filename echoes it (§12.6).

	Runs on both backends: ``VACUUM INTO`` and ``pg_dump`` are different mechanisms and the
	property they have to share is that the copy says which schema it holds.
	"""

	written = subroutine.db.backup.take(engine, _settings())
	head = subroutine.db.migrate.head_revision()

	assert written.path.is_file()
	assert written.size_bytes > 0
	assert written.schema_head == head

	# Read back out of the file, not off its name.
	assert subroutine.db.backup.head_in(written.path) == head
	assert head is not None and head in written.name


def test_a_file_that_is_not_a_backup_is_refused (home: pathlib.Path, tmp_path: pathlib.Path) -> None:
	"""Told plainly, rather than reported as a backup with no schema version."""

	stray = tmp_path / "notes.db"
	stray.write_text("this is not a database", encoding="utf-8")

	with pytest.raises(subroutine.errors.BadRequest):
		subroutine.db.backup.head_in(stray)

	unknown = tmp_path / "notes.txt"
	unknown.write_text("nor is this", encoding="utf-8")

	with pytest.raises(subroutine.errors.BadRequest):
		subroutine.db.backup.head_in(unknown)


def test_a_newer_schema_is_refused_and_an_equal_one_is_not (
	engine: sqlalchemy.engine.Engine, home: pathlib.Path, tmp_path: pathlib.Path
) -> None:
	"""§12.6's asymmetry. Refusing the newer case is the only honest answer available."""

	written = subroutine.db.backup.take(engine, _settings())

	# Equal: this installation took it, so it can put it back.
	assert subroutine.db.backup.check_restorable(written.path) == (
		subroutine.db.migrate.head_revision()
	)

	forged = _with_schema_head(written.path, "ffffffffffff", tmp_path)

	with pytest.raises(subroutine.errors.SchemaMismatch):
		subroutine.db.backup.check_restorable(forged)


def test_the_core_tables_are_still_tables_this_schema_has () -> None:
	"""`#928`'s floor stays honest, or every backup becomes unrestorable in silence.

	``CORE_TABLES`` names what a file must contain to be a backup at all. It is deliberately
	the *initial* migration's tables rather than today's, so that an older backup still passes
	— but that means nothing else notices if one is ever dropped, and the failure would be a
	refusal to restore anything rather than an error anybody could read.
	"""

	live = set(subroutine.db.base.Base.metadata.tables)

	# `alembic_version` is Alembic's and is not in our metadata, so it is excused by name
	# rather than by weakening the comparison.
	assert subroutine.db.backup.CORE_TABLES - {"alembic_version"} <= live


def test_a_file_that_records_a_schema_and_holds_no_database_is_refused (
	tmp_path: pathlib.Path,
) -> None:
	"""`#928`. A schema version is one string, and one string is not a database.

	Measured before the fix: a 12 KB file holding ``alembic_version`` and a table called
	``loot`` was accepted, installed over the live database, and reported as a success.
	"""

	forged = tmp_path / "forged.db"
	connection = sqlite3.connect(forged)

	try:
		connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
		connection.execute(
			"INSERT INTO alembic_version VALUES (?)", (subroutine.db.migrate.head_revision(),)
		)
		connection.execute("CREATE TABLE loot (secret TEXT)")
		connection.commit()

	finally:
		connection.close()

	with pytest.raises(subroutine.errors.BadRequest) as refused:
		subroutine.db.backup.check_restorable(forged)

	# Named, because "this is not a backup" and "this backup is too new" send the reader to
	# completely different places.
	assert "missing" in str(refused.value)
	assert "task" in str(refused.value)


def test_a_dump_that_runs_a_command_is_refused (tmp_path: pathlib.Path) -> None:
	"""`#928`. ``psql --file`` executes backslash commands, so a backup is code until read.

	Reproduced before the fix with the restore's own invocation: ``\\!`` ran and psql exited 0.
	"""

	dump = tmp_path / "hostile.sql"
	dump.write_text(
		"SET client_encoding = 'UTF8';\n"
		"\\! touch /tmp/owned\n"
		"CREATE TABLE alembic_version (version_num character varying(32));\n"
	)

	with pytest.raises(subroutine.errors.BadRequest) as refused:
		subroutine.db.backup.refuse_unsafe_commands(dump)

	# The line number, because a dump is thousands of lines and "somewhere in here" is not
	# something an operator can act on.
	assert "line 2" in str(refused.value)


def test_data_that_begins_with_a_backslash_is_not_read_as_a_command (
	tmp_path: pathlib.Path,
) -> None:
	"""`#928`'s other half, and the one a careless scan gets wrong.

	Inside a ``COPY … FROM stdin;`` block a leading backslash is data — ``\\N`` is how every
	NULL is written — and the block ends at a lone ``\\.``. A scan that does not track which
	of the two it is reading refuses ordinary rows, so both directions are asserted here.
	"""

	body = (
		"COPY public.task (id, title) FROM stdin;\n"
		"\\N\tsomething\n"
		"\\.\n"
		"CREATE TABLE alembic_version (version_num character varying(32));\n"
	)

	inside = tmp_path / "ordinary.sql"
	inside.write_text(body)

	subroutine.db.backup.refuse_unsafe_commands(inside)

	# The same file with a command *after* the block closes must still be refused, or the
	# tolerance above is simply a scan that stopped looking.
	after = tmp_path / "after.sql"
	after.write_text(body + "\\i /etc/passwd\n")

	with pytest.raises(subroutine.errors.BadRequest):
		subroutine.db.backup.refuse_unsafe_commands(after)


def test_a_backup_whose_pages_do_not_hold_together_is_refused (
	engine: sqlalchemy.engine.Engine, home: pathlib.Path, tmp_path: pathlib.Path
) -> None:
	"""`#928`. The size and the schema head are both satisfied by a torn file.

	This is the check that reads the pages. Falsified by corrupting a copy in place, which
	keeps the length identical and leaves the header — and therefore the recorded schema —
	perfectly readable.
	"""

	if engine.dialect.name != "sqlite":
		pytest.skip("The page check is SQLite's; a dump is a script, and truncation is size.")

	written = subroutine.db.backup.take(engine, _settings())

	torn = tmp_path / "torn.db"
	original = written.path.read_bytes()

	# Past the header and into the b-tree, and the same length, so every other check passes.
	damaged = original[:2048] + bytes(len(original) - 4096) + original[-2048:]
	torn.write_bytes(damaged)

	assert len(torn.read_bytes()) == len(original)

	with pytest.raises(subroutine.errors.ServiceUnavailable):
		subroutine.db.backup._refuse_a_corrupt_copy(torn)


def _with_schema_head (
	source: pathlib.Path, head: str, into: pathlib.Path
) -> pathlib.Path:
	"""Copy a backup with its recorded schema head replaced, to stand in for another version.

	The only way to test the refusal today: there is one migration, so no *genuinely* newer
	backup can be produced from this tree.
	"""

	target = into / f"subroutine-default-20260730T120000Z-{head}{source.suffix}"

	if source.suffix == subroutine.db.backup.SQLITE_SUFFIX:
		import shutil
		import sqlite3

		shutil.copy2(source, target)
		connection = sqlite3.connect(target)

		try:
			connection.execute("UPDATE alembic_version SET version_num = ?", (head,))
			connection.commit()

		finally:
			connection.close()

		return target

	text = source.read_text(encoding="utf-8")
	current = subroutine.db.migrate.head_revision()

	assert current is not None

	target.write_text(text.replace(current, head), encoding="utf-8")

	return target


def test_pruning_keeps_the_newest_and_refuses_to_keep_none (
	engine: sqlalchemy.engine.Engine, home: pathlib.Path
) -> None:
	"""Nothing is deleted unless asked, and "keep zero" is not a thing anyone means."""

	moment = datetime.datetime(2026, 7, 30, 12, 0, tzinfo=datetime.UTC)

	for offset in range(3):
		subroutine.db.backup.take(
			engine, _settings(), moment=moment + datetime.timedelta(minutes=offset)
		)

	assert len(subroutine.db.backup.catalogue(_settings())) == 3

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.db.backup.prune(_settings(), keep=0)

	assert len(subroutine.db.backup.catalogue(_settings())) == 3

	subroutine.db.backup.prune(_settings(), keep=1)
	remaining = subroutine.db.backup.catalogue(_settings())

	assert len(remaining) == 1
	assert remaining[0].taken_at == moment + datetime.timedelta(minutes=2)


# --------------------------------------------------------------------------------------
# Restore, on a database this file owns (§12.6a)
# --------------------------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "postgresql"])
def own_database (
	request: pytest.FixtureRequest, home: pathlib.Path, tmp_path: pathlib.Path
) -> typing.Iterator[str]:
	"""Yield a database URL this test owns outright, on each backend in turn.

	**Not the shared engine.** Restoring on PostgreSQL drops and recreates ``public``, and the
	session-scoped database is where the rest of the suite lives. So a restore test gets its own
	database and drops it afterwards.
	"""

	if request.param == "sqlite":
		yield f"sqlite:///{tmp_path / 'own.db'}"

		return

	reason = conftest._postgres_unavailable_reason()

	if reason is not None:
		if conftest.REQUIRE_POSTGRES:
			pytest.fail(reason)

		pytest.skip(reason)

	name = f"subroutine_restore_{os.getpid()}_{abs(hash(tmp_path)) % 100000}"
	admin = sqlalchemy.create_engine(
		conftest.POSTGRES_ADMIN_URL, isolation_level="AUTOCOMMIT"
	)

	try:
		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{name}"'))
			connection.execute(sqlalchemy.text(f'CREATE DATABASE "{name}"'))

		yield conftest.with_database(conftest.POSTGRES_ADMIN_URL, name)

		with admin.connect() as connection:
			connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{name}"'))

	finally:
		admin.dispose()


def test_a_restore_puts_the_data_back (
	own_database: str,
) -> None:
	"""The whole point: what was lost comes back, on both backends."""


	subroutine.db.migrate.upgrade(own_database)
	identity = _seed_instance(own_database)

	engine = subroutine.db.session.create_engine(own_database)

	try:
		written = subroutine.db.backup.take(engine, _settings())

	finally:
		engine.dispose()

	_forget_the_instance_row(own_database)

	assert _instance_id(own_database) is None

	engine = subroutine.db.session.create_engine(own_database)

	try:
		_restored(engine, written.path, as_clone=False)

	finally:
		engine.dispose()

	# Recovered, and it kept the identity it had — agents and configuration refer to it.
	assert _instance_id(own_database) == identity


def test_a_clone_keeps_the_data_and_takes_a_new_identity (
	own_database: str,
) -> None:
	"""§12.6a: two live instances may never claim one ``instance_id``.

	The failure this prevents is invisible at restore time — ``refuse_duplicate_instances``
	starts refusing legitimate fan-out, and an agent files two datasets under one cache key.
	"""


	subroutine.db.migrate.upgrade(own_database)
	identity = _seed_instance(own_database)

	engine = subroutine.db.session.create_engine(own_database)

	try:
		written = subroutine.db.backup.take(engine, _settings())

	finally:
		engine.dispose()

	engine = subroutine.db.session.create_engine(own_database)

	try:
		_restored(engine, written.path, as_clone=True)

	finally:
		engine.dispose()

	restored = _instance_id(own_database)

	assert restored is not None
	assert restored != identity


#: What a refusal says when something else is connected to the database (`#171`).
#:
#: `#377`. These tests each own their database outright, so this refusal is never a legitimate
#: outcome for one of them — and when it fires it *pre-empts* the check under test, which then
#: reports an opaque substring mismatch about a message it never expected to see.
_IN_USE = "using this database"


def _refusal (raised: subroutine.errors.SubroutineError) -> str:
	"""Return a refusal's text, failing loudly where it is `#377`'s race instead.

	**Twice this cost most of a session** (`#377`, and `#284` before it was merged into it).
	The assertion read ``assert 'runs on' in 'Something else is using this database: 1 other
	connection'`` — true of nothing, about a database the change in flight had never touched,
	in a test whose name mentions engines. Both times it was read as a regression from whatever
	was being worked on, and both times it passed on a re-run.

	**The cause has never been identified and this does not claim to fix it.** `#725` measured
	four hypotheses away — a lingering ``pg_dump``, a disposed pool's backend, an autovacuum
	worker, and 504 restore runs under eight-way parallel load with no reproduction — and
	shipped the two things `#377` asked for: the refusal names the connection rather than
	counting it, and it is asked twice so something on its way out does not count. There has
	been no sighting since the day that landed.

	What this adds is the cheap half that holds whatever the cause turns out to be: **a failure
	that says which failure it is.** The message now carries the backend type, its state, its
	application name and its age, so a recurrence is a minute's reading rather than an
	afternoon's diagnosis of an innocent change.
	"""

	said = str(raised)

	assert _IN_USE not in said, (
		f"`#377`: a connection leaked into this test's own database, so the in-use refusal "
		f"fired before the check under test could. **This is not a regression in whatever you "
		f"are changing** — it has been seen three times since 2026-08-02, always in an "
		f"unrelated change, and it passes on a re-run. What the database said: {said}"
	)

	return said


def _restored (engine: typing.Any, path: pathlib.Path, **how: typing.Any) -> None:
	"""Restore, turning `#377`'s race into a failure that names itself.

	The counterpart to :func:`_refusal`, for the tests that expect a restore to *succeed* —
	which is where the 2026-08-09 sighting landed, on ``test_a_restore_puts_the_data_back``.
	There the refusal is not caught at all, so it surfaces as a ``ValidationError`` about a
	database the change in flight never touched.
	"""

	try:
		subroutine.db.backup.restore(engine, path, **how)

	except subroutine.errors.SubroutineError as refused:
		_refusal(refused)

		raise


def test_a_backup_from_the_other_engine_is_refused_before_anything_is_dropped (
	own_database: str,
) -> None:
	"""`#172`. The defect was one of order, and the cost was the whole instance.

	Restoring into PostgreSQL drops and recreates ``public`` and *then* hands the file to
	``psql``, so a SQLite backup picked by mistake destroyed the database and reported an
	encoding error that never said "SQLite" or "wrong file". It was recoverable only because
	the safety copy happened to have run.

	Runs on both backends because the mistake is symmetrical, and asserts the *data* survived
	rather than only that an error was raised — an error raised after the drop is the bug.
	"""

	subroutine.db.migrate.upgrade(own_database)
	identity = _seed_instance(own_database)

	engine = subroutine.db.session.create_engine(own_database)

	try:
		written = subroutine.db.backup.take(engine, _settings())

	finally:
		engine.dispose()

	# **A backup that reads as perfectly valid**, which is what makes this dangerous. Copying
	# the bytes under the other suffix would be caught by `head_in` anyway and would prove
	# nothing: the reported failure is a *real* backup of the other engine, whose schema head
	# is readable, that passes every check there was and is then handed to the wrong loader.
	foreign = _a_real_backup_of_the_other_engine(written.path)

	engine = subroutine.db.session.create_engine(own_database)

	try:
		with pytest.raises(subroutine.errors.SubroutineError) as refused:
			subroutine.db.backup.restore(engine, foreign, as_clone=False)

	finally:
		engine.dispose()

	assert "runs on" in _refusal(refused.value)

	# The point of the item: still there, and still itself.
	assert _instance_id(own_database) == identity


def test_a_restore_is_refused_while_something_else_holds_the_database (
	own_database: str,
) -> None:
	"""`#171`. It reported success and left the instance permanently corrupt.

	The serving process keeps its descriptors on the file that was replaced, so every write it
	accepts is lost and its next checkpoint lands on top of the restored database. The API
	answered 200 throughout — including ``/readyz``, the endpoint an operator would use to
	check that the restore had worked.

	One holder, both backends, because the question is the same one asked two ways: SQLite
	answers by refusing an exclusive lock, PostgreSQL out of ``pg_stat_activity``.
	"""

	subroutine.db.migrate.upgrade(own_database)
	identity = _seed_instance(own_database)

	engine = subroutine.db.session.create_engine(own_database)

	try:
		written = subroutine.db.backup.take(engine, _settings())

	finally:
		engine.dispose()

	holder = subroutine.db.session.create_engine(own_database)

	try:
		# Idle, exactly as a serving process is between requests — which is what makes this
		# hard to see and is why an idle connection was the case it was verified against.
		with holder.connect():
			engine = subroutine.db.session.create_engine(own_database)

			try:
				# **Not through `_restored`**, which exists to turn this refusal into a
				# failure naming `#377`. Here it is the behaviour under test: something
				# genuinely is connected, and refusing is the whole point.
				with pytest.raises(subroutine.errors.SubroutineError) as refused:
					subroutine.db.backup.restore(engine, written.path, as_clone=False)

			finally:
				engine.dispose()

			assert "using this database" in str(refused.value)

			# And --force is a real way through, for the operator who knows better.
			engine = subroutine.db.session.create_engine(own_database)

			try:
				subroutine.db.backup.restore(
					engine, written.path, as_clone=False, force=True
				)

			finally:
				engine.dispose()

	finally:
		holder.dispose()

	assert _instance_id(own_database) == identity


def test_a_restore_is_not_undone_by_the_log_it_replaced (tmp_path: pathlib.Path) -> None:
	"""`#194`. The restore reported success and the database held what it had before.

	SQLite replays a ``-wal`` it finds beside a database when that database is opened, and
	nothing removed the one belonging to the file a restore had just replaced. So the backup's
	content was discarded on the next read and the pre-restore state came back — `#171`'s
	signature exactly, on the command §12.4 says has to work under pressure.

	**The path is the ordinary recovery, not a corner.** The operator loses the database file
	and reaches for ``db restore --recover``; the sidecars are still there because nothing
	deleted them. Written this way rather than with a live holder because the two steps that
	happen to save the ordinary path — the safety copy, and ``_sqlite_readable`` inside
	``check_unused`` — both need the main file to exist. With it gone neither runs, which is
	what makes this reachable and what made it invisible.

	SQLite only: PostgreSQL has no such file, and ``restore`` refuses a backup from the other
	engine long before any of this.
	"""

	url = f"sqlite:///{tmp_path / 'own.db'}"
	database = tmp_path / "own.db"

	subroutine.db.migrate.upgrade(url)
	_seed_instance(url)

	# The two states have to differ in something the assertion can *see*. The identity survives
	# a replay — the log carries back the same row — so the name is what tells them apart, and
	# asserting on the id would be a test that passes either way. It did, on the first attempt.
	backed_up = _instance_name(url)

	engine = subroutine.db.session.create_engine(url)

	try:
		written = subroutine.db.backup.take(engine, _settings())

	finally:
		engine.dispose()

	# A writer that dies without closing, which is what leaves a log behind at all. Its work is
	# committed and uncheckpointed, so the log is valid and SQLite will happily replay it.
	_a_writer_that_never_closed(database)

	assert database.with_name("own.db-wal").exists(), "the reproduction needs a log to survive"

	# The loss the command answers. Deleting the database and not its sidecars is the state a
	# person is in when they run this.
	database.unlink()

	engine = subroutine.db.session.create_engine(url)

	try:
		_restored(engine, written.path, as_clone=False)

	finally:
		engine.dispose()

	# The assertion the item is about: reading it back gives the backup, not what the log said.
	assert _instance_name(url) == backed_up

	for suffix in subroutine.db.backup.SQLITE_SIDECARS:
		assert not database.with_name(f"own.db{suffix}").exists()


def _a_writer_that_never_closed (database: pathlib.Path) -> None:
	"""Commit a change to a SQLite database and abandon the process, leaving its log behind.

	In a subprocess because that is the only way to get the state honestly: a connection closed
	by any means checkpoints its log away, and writing the file by hand would prove that a
	hand-written file is ignored rather than that a real one is replayed.
	"""

	code = (
		"import os, sqlite3, sys\n"
		"connection = sqlite3.connect(sys.argv[1])\n"
		"connection.execute('PRAGMA journal_mode=WAL')\n"
		"connection.execute(\"UPDATE instance SET name = 'from the abandoned log'\")\n"
		"connection.commit()\n"
		"os._exit(0)\n"
	)
	process = subprocess.Popen(
		[sys.executable, "-c", code, str(database)],
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
	)

	# `communicate` rather than `wait`: a pipe left open trips the ResourceWarning this suite
	# turns into an error, and the traceback then points at the wrong place entirely.
	_output, complaint = process.communicate(timeout=60)

	assert process.returncode == 0, complaint


def _a_real_backup_of_the_other_engine (beside: pathlib.Path) -> pathlib.Path:
	"""Build a backup of whichever engine ``beside`` is *not*, and return where it went.

	Real on both sides, because a synthetic file proves the wrong thing. A SQLite backup is a
	migrated database, so it is built by migrating one; a PostgreSQL dump is a script, and the
	only part any of this reads is the ``alembic_version`` block, so that is written out in
	``pg_dump``'s own plain form rather than by standing up a server for one table.
	"""

	head = subroutine.db.migrate.head_revision()

	if beside.suffix == subroutine.db.backup.SQLITE_SUFFIX:
		dump = beside.with_suffix(subroutine.db.backup.POSTGRESQL_SUFFIX)
		dump.write_text(
			"--\n-- PostgreSQL database dump\n--\n\n"
			"COPY public.alembic_version (version_num) FROM stdin;\n"
			f"{head}\n\\.\n",
			encoding="utf-8",
		)

		return dump

	database = beside.with_suffix(subroutine.db.backup.SQLITE_SUFFIX)
	subroutine.db.migrate.upgrade(f"sqlite:///{database}")

	return database


def _seed_instance (url: str) -> str:
	"""Write the single ``instance`` row a real installation has, and return its id."""

	engine = subroutine.db.session.create_engine(url)

	try:
		factory = subroutine.db.session.create_session_factory(engine)

		with subroutine.db.session.session_scope(factory) as session:
			instance = subroutine.db.models.system.Instance(
				singleton=1, name="Test instance", timezone="Europe/London"
			)
			session.add(instance)
			session.flush()

			return str(uuid.UUID(str(instance.id)))

	finally:
		engine.dispose()


def _instance_name (url: str) -> str | None:
	"""Return this database's instance name, which is what a replayed log would change."""

	engine = subroutine.db.session.create_engine(url)

	try:
		with engine.connect() as connection:
			rows = connection.exec_driver_sql("SELECT name FROM instance").fetchall()

	finally:
		engine.dispose()

	return str(rows[0][0]) if rows else None


def _forget_the_instance_row (url: str) -> None:
	"""Delete the instance row, standing in for the loss a recovery answers."""

	engine = subroutine.db.session.create_engine(url)

	try:
		with engine.begin() as connection:
			connection.exec_driver_sql("DELETE FROM instance")

	finally:
		engine.dispose()


def _instance_id (url: str) -> str | None:
	"""Return this database's instance identity, or ``None`` if it has none."""

	engine = subroutine.db.session.create_engine(url)

	try:
		with engine.connect() as connection:
			rows = connection.exec_driver_sql("SELECT id FROM instance").fetchall()

	finally:
		engine.dispose()

	# Normalised through `uuid.UUID`: SQLite stores the value as bare hex and PostgreSQL as a
	# dashed uuid, so comparing the raw strings compares storage formats, not identities.
	return str(uuid.UUID(str(rows[0][0]))) if rows else None


# --------------------------------------------------------------------------------------
# POST /v1/admin/backups (§12.6)
# --------------------------------------------------------------------------------------


@pytest.fixture
def elsewhere (tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
	"""Send the data directory somewhere temporary, so a test never writes to a real home."""

	root = tmp_path / "served"
	monkeypatch.setenv("XDG_DATA_HOME", str(root))

	return root


def test_an_admin_can_take_a_backup_over_http (
	session: sqlalchemy.orm.Session, elsewhere: pathlib.Path
) -> None:
	"""An agent about to do something bulk should be able to snapshot first (§12.6).

	**The file is checked through the directory rather than through the response** (`#186`).
	It used to be opened at the path the body reported, which was the neatest available check
	and was also the reason the path was there — a test that reads a field is a reader, and it
	made a field nobody else could use look used.
	"""

	world = test_api_tasks._world(session)
	response = world.call("POST", "/v1/admin/backups")

	assert response.status_code == 201

	body = response.json()

	assert body["schema_head"] == subroutine.db.migrate.head_revision()
	assert body["size_bytes"] > 0

	written = [one.name for one in elsewhere.rglob(body["name"])]

	assert written == [body["name"]], (
		f"the response named {body['name']!r} and the data directory holds {written}, so the "
		f"name a caller is given does not identify the file that was made"
	)

	listed = world.call("GET", "/v1/admin/backups")

	assert listed.status_code == 200
	assert [item["name"] for item in listed.json()["items"]] == [body["name"]]


def test_a_backup_over_http_is_named_without_naming_the_server_s_filesystem (
	session: sqlalchemy.orm.Session, elsewhere: pathlib.Path
) -> None:
	"""`#186`. A caller over HTTP is somewhere else, by construction.

	They cannot open the file, and **no endpoint takes a path** — §12.4 gives restore none on
	purpose, so the one thing a reader might do with an absolute path is the one thing they
	cannot. What it did say is where this instance keeps its data, which is a fact about the
	machine rather than about the backup.

	**Asserted on the whole body rather than on the absence of one key**, because the value is
	the disclosure and it could come back under any name — `location`, `file`, `directory`.
	The data directory is a temporary one here, so its own path is the thing to search for.
	"""

	world = test_api_tasks._world(session)
	taken = world.call("POST", "/v1/admin/backups")

	assert taken.status_code == 201

	listed = world.call("GET", "/v1/admin/backups")

	assert listed.status_code == 200

	root = str(elsewhere)

	for what, body in (("the backup it took", taken.text), ("the catalogue", listed.text)):
		assert root not in body, (
			f"{what} carries {root!r}, which is this server's own filesystem layout handed to "
			f"somebody who is not on this server and has no endpoint that would take it"
		)

	assert taken.json()["name"], "the backup is not identified by anything at all"


def test_a_narrowed_token_cannot_take_a_backup (
	session: sqlalchemy.orm.Session, elsewhere: pathlib.Path
) -> None:
	"""``instance:admin`` is not something a task-scoped agent quietly acquires (§7.3).

	The shape this guards against has recurred three times here: a rule documented, believed,
	and enforced nowhere. A backup is a complete copy of everything in the instance, so an agent
	holding ``task:read`` obtaining one would be an exfiltration path, not an inconvenience.
	"""

	world = test_api_tasks._world(session, scopes=["task:read"])

	assert world.call("POST", "/v1/admin/backups").status_code == 403
	assert world.call("GET", "/v1/admin/backups").status_code == 403


def _settings () -> subroutine.config.Settings:
	"""Resolve settings the way a command does, for the active instance."""

	return subroutine.config.load_settings()


# --------------------------------------------------------------------------------------
# Where backups go (§12.6b)
# --------------------------------------------------------------------------------------


def test_backups_go_where_they_are_configured_to (
	engine: sqlalchemy.engine.Engine,
	home: pathlib.Path,
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""The point of a backup is surviving the disk it is on, so the path has to be settable."""

	elsewhere = tmp_path / "volume" / "subroutine-backups"
	monkeypatch.setenv("SUBROUTINE_BACKUP_DIRECTORY", str(elsewhere))

	written = subroutine.db.backup.take(engine, _settings())

	assert written.path.parent == elsewhere
	assert written.path.is_file()

	# And the catalogue reads the same place, or `db backups` would list nothing.
	assert [found.name for found in subroutine.db.backup.catalogue(_settings())] == [
		written.name
	]


def test_a_listed_backup_says_what_it_holds (
	engine: sqlalchemy.engine.Engine,
	home: pathlib.Path,
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`#432`. `#395` made the *taking* say what it copied and left the *listing* saying size.

	Both facts in that line are correct for a backup of an empty instance — an empty database is
	a valid one — so four hollow copies sat at the top of the list, newest first, with nothing
	to be suspicious of. The counts are known only at the moment the backup is taken, off the
	source, so they are written beside the copy and read back here.
	"""

	monkeypatch.setenv("SUBROUTINE_BACKUP_DIRECTORY", str(tmp_path / "volume"))

	written = subroutine.db.backup.take(engine, _settings())

	assert written.holdings is not None

	[listed] = subroutine.db.backup.catalogue(_settings())

	assert listed.holdings == written.holdings, (
		"a backup listed from disk reports what it recorded, not what a fresh count says — "
		"the source may have moved on or gone by the time anybody lists it"
	)


def test_a_backup_with_no_record_is_unknown_rather_than_empty (
	engine: sqlalchemy.engine.Engine,
	home: pathlib.Path,
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`#432`'s third state, and the reason this is not a boolean.

	Every backup taken before the counts were written beside them has no record. Reporting
	those as holding nothing would be the same false confidence pointing the other way — and
	they are exactly the backups an operator is most likely to be reaching for.
	"""

	monkeypatch.setenv("SUBROUTINE_BACKUP_DIRECTORY", str(tmp_path / "volume"))

	written = subroutine.db.backup.take(engine, _settings())
	beside = written.path.with_name(written.path.name + subroutine.db.backup.RECORD_SUFFIX)

	assert beside.is_file(), "the record is written beside the copy it describes"

	beside.unlink()

	[listed] = subroutine.db.backup.catalogue(_settings())

	assert listed.holdings is None, "no record must not read as a count of zero"


def test_pruning_takes_a_backups_record_with_it (
	engine: sqlalchemy.engine.Engine,
	home: pathlib.Path,
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Or a directory accumulates one orphan per deleted backup, for ever.

	Worse than clutter: `_free_name` walks a colliding instant forward, so a later backup could
	be given a name a deleted one had — and would inherit its counts.
	"""

	monkeypatch.setenv("SUBROUTINE_BACKUP_DIRECTORY", str(tmp_path / "volume"))

	first = subroutine.db.backup.take(engine, _settings())
	subroutine.db.backup.take(engine, _settings())

	subroutine.db.backup.prune(_settings(), keep=1)

	assert not first.path.exists()
	assert not first.path.with_name(
		first.path.name + subroutine.db.backup.RECORD_SUFFIX
	).exists()


def test_the_database_never_gets_written_to_the_backup_volume (
	engine: sqlalchemy.engine.Engine,
	home: pathlib.Path,
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""§12.6b: a backup is built locally and moved, because the destination may not take a lock.

	``VACUUM INTO`` creates a database and locks it, and the volume somebody sensibly wants
	backups on is often one where SQLite cannot lock at all. Nothing proves the staging happened
	from the outside, so this asserts what it is for: no leftovers, and a readable result.
	"""

	elsewhere = tmp_path / "volume"
	monkeypatch.setenv("SUBROUTINE_BACKUP_DIRECTORY", str(elsewhere))

	written = subroutine.db.backup.take(engine, _settings())
	staging = subroutine.config.data_home() / subroutine.db.backup.DIRECTORY_NAME / ".staging"

	assert list(staging.iterdir()) == [], "the staged copy should not be left behind"
	assert subroutine.db.backup.head_in(written.path) == subroutine.db.migrate.head_revision()


def test_a_backup_that_does_not_arrive_intact_is_not_left_looking_usable (
	engine: sqlalchemy.engine.Engine,
	home: pathlib.Path,
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""**The failure worth spending code on.** A short file on a flaky mount looks like a backup.

	It appears in the catalogue, its name states which schema it holds, and it is discovered to be
	truncated on the one day it matters. So delivery is verified where the file landed, and a copy
	that fails is removed rather than left.
	"""

	elsewhere = tmp_path / "volume"
	monkeypatch.setenv("SUBROUTINE_BACKUP_DIRECTORY", str(elsewhere))

	def truncating_copy (source: str, destination: str) -> str:
		"""Stand in for a network write that stops half way through."""

		pathlib.Path(destination).write_bytes(pathlib.Path(source).read_bytes()[:512])

		return destination

	# Named as a string: the module under test reaches `shutil` through its own namespace, and
	# `--strict` will not have an attribute access into another module's imports.
	monkeypatch.setattr("subroutine.db.backup.shutil.copyfile", truncating_copy)

	with pytest.raises(subroutine.errors.ServiceUnavailable):
		subroutine.db.backup.take(engine, _settings())

	assert subroutine.db.backup.catalogue(_settings()) == [], (
		"a truncated backup must not survive to be listed as one"
	)


def test_a_backup_arrives_on_a_volume_whose_files_this_account_cannot_own (
	engine: sqlalchemy.engine.Engine,
	home: pathlib.Path,
	tmp_path: pathlib.Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`#505`. The served instance wrote three perfect backups and called every one a failure.

	§12.6b says a network mount is the intended destination, and the commonest kind — CIFS with
	``forceuid``, NFS with ``root_squash`` — presents every file as owned by somebody the writing
	process is not. ``shutil.move`` falls back to ``copy2`` across a filesystem boundary, and
	``copy2`` copies metadata: ``os.utime`` and ``os.chmod`` then raise ``EPERM`` **after the
	bytes have landed**, so the operator is told there is no backup while holding one.

	Both halves of the real mount are reproduced rather than the library step between them: a
	destination on another filesystem, and a destination whose files this account cannot own.
	"""

	elsewhere = tmp_path / "volume"
	monkeypatch.setenv("SUBROUTINE_BACKUP_DIRECTORY", str(elsewhere))

	def somewhere_else (path: object) -> bool:
		"""Whether this path is on the pretend volume, so the real filesystem is left alone."""

		return str(path).startswith(str(elsewhere))

	renames, changes_ownership, restamps = os.rename, os.chmod, os.utime

	def across_a_filesystem_boundary (source: typing.Any, destination: typing.Any) -> None:
		"""What a rename onto another filesystem does, and why ``move`` falls back at all."""

		if somewhere_else(destination):
			raise OSError(errno.EXDEV, "Invalid cross-device link")

		renames(source, destination)

	def refuses_to_be_owned (path: typing.Any, *rest: typing.Any, **named: typing.Any) -> None:
		"""``forceuid`` makes every file somebody else's, so metadata calls are refused."""

		if somewhere_else(path):
			raise PermissionError(errno.EPERM, "Operation not permitted")

		changes_ownership(path, *rest, **named)

	def refuses_to_be_restamped (path: typing.Any, *rest: typing.Any, **named: typing.Any) -> None:
		"""The other half of ``copystat``, and the one that raises first."""

		if somewhere_else(path):
			raise PermissionError(errno.EPERM, "Operation not permitted")

		restamps(path, *rest, **named)

	monkeypatch.setattr(os, "rename", across_a_filesystem_boundary)
	monkeypatch.setattr(os, "chmod", refuses_to_be_owned)
	monkeypatch.setattr(os, "utime", refuses_to_be_restamped)

	written = subroutine.db.backup.take(engine, _settings())

	assert written.path.exists(), "the backup arrived and must be reported as having arrived"
	assert written.path.stat().st_size == written.size_bytes
	assert subroutine.db.backup.head_in(written.path) == subroutine.db.migrate.head_revision()

	assert [found.path for found in subroutine.db.backup.catalogue(_settings())] == [written.path]


# --------------------------------------------------------------------------------------
# The operations surface — item `#175`
# --------------------------------------------------------------------------------------


def test_an_unsupported_backend_is_named_rather_than_missing_a_driver (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""`#175`. 'No module named MySQLdb' invites installing a driver that cannot help.

	It is not a missing dependency, it is a database this is not built for — and the message
	that reads like the first sends somebody off to install something, meet a stranger failure
	after they have, and never learn the actual answer.
	"""

	run("init", "--workspace", "Real")

	configuration = subroutine.config.config_file_path()

	with configuration.open("a", encoding="utf-8") as handle:
		handle.write('\ndatabase_url = "mysql://user@localhost/thing"\n')

	refused = run("list", expect=1)

	assert "not a database Subroutine can use" in refused.output
	assert "sqlite" in refused.output and "postgresql" in refused.output
	assert "MySQLdb" not in refused.output


def test_a_supported_backend_with_no_driver_names_what_installs_it (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""`#927`'s H-20 — the sibling of the test above, and the commoner of the two.

	PostgreSQL is an *optional* dependency, deliberately: §1.4's shopping-list user must not be
	made to install a database driver. So a `postgresql://` URL on a machine that never took
	the extra is an ordinary state — anybody who edits `database_url` before running
	`pip install 'subroutine[postgres]'` — where `mysql://` is a mistake.

	**It reported that as a bug in Subroutine.** `ModuleNotFoundError` escaped
	`clients/local.Client.__init__`, which builds its engine outside every guard `cli/main`
	has, so the answer was *"Something went wrong that should not have… please report it"*
	plus a crash file holding a traceback. The one line that fixes it is in the README and
	appeared on none of the three surfaces that met this.

	Driven through `list` rather than by calling `create_engine`, because the defect was never
	in what that raised — it was that nothing between it and the reader turned it into a
	sentence.

	A bare `postgresql://` names no driver, so SQLAlchemy reaches for `psycopg2`, which this
	project does not ship under any extra. That is the same failure as a missing `psycopg` and
	is reachable on a machine that *has* the extra installed, which is what makes it testable
	here at all.
	"""

	run("init", "--workspace", "Real")

	configuration = subroutine.config.config_file_path()

	with configuration.open("a", encoding="utf-8") as handle:
		handle.write('\ndatabase_url = "postgresql://user@localhost/thing"\n')

	refused = run("list", expect=1)

	assert "no postgresql driver" in refused.output
	assert "subroutine[postgres]" in refused.output

	# **The half that is the finding.** The message it replaced was the crash report, which
	# tells a reader to open an issue about their own configuration.
	assert "should not have" not in refused.output
	assert "report it" not in refused.output


def test_a_failed_pragma_closes_the_connection_it_was_handed (tmp_path: pathlib.Path) -> None:
	"""`#228`. The one moment nothing else can clean up after a connection.

	SQLite opens lazily, so a damaged file is accepted by ``connect`` and only rejected when
	``journal_mode=WAL`` reads its header — at which point the driver's connection exists and
	the pool has not recorded it, so ``engine.dispose()`` will never see it. Every attempt to
	open a corrupt database leaked a file handle until the process ended, and
	``db restore --recover`` — the command most likely to meet one — opens the database twice.

	**Asserted on the listener rather than through an engine, deliberately.** Reaching it the
	public way means provoking the leak and then waiting for the garbage collector to notice,
	which is what made this invisible on every Python before 3.13: the warning is real on
	3.11 and 3.12 too and simply never fires. A test that has to collect garbage to reach its
	subject is a test that reports the interpreter's mood.
	"""

	corrupt = tmp_path / "corrupt.db"
	corrupt.write_bytes(b"this is definitely not a database" * 40)

	connection = sqlite3.connect(corrupt)

	with pytest.raises(sqlite3.DatabaseError):
		subroutine.db.session._apply_sqlite_pragmas(connection, None)

	# Closed, not merely unusable. `sqlite3` refuses a closed connection by name, which is
	# what tells this apart from the connection simply being broken.
	with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
		connection.execute("SELECT 1")


def test_a_setting_nobody_reads_is_said_out_loud (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""`#175`. Silence is how somebody comes to believe they set something they did not.

	The two that matter fail dangerously and quietly: a misspelled `protected` means the
	destructive commands stop asking, and a misspelled `backup_directory` means backups go
	next to the database. `docs/errors.md` argues exactly this for request bodies, and the
	configuration file was the one place the principle did not hold.
	"""

	run("init", "--workspace", "Real")

	configuration = subroutine.config.config_file_path()

	with configuration.open("a", encoding="utf-8") as handle:
		handle.write('\nprotectd = true\n')

	warned = run("list")

	assert "protectd" in warned.output
	assert "protected" in warned.output, "the nearest real setting is the useful half"


def test_pruning_names_what_it_deleted (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""`#175`. `hosting.md` recommends a timer, whose log is the only record there will be.

	A deletion nothing reports cannot be audited, on the command whose entire subject is not
	losing data.
	"""

	run("init", "--workspace", "Real")

	first = _backup_name(run("db", "backup").output)
	second = _backup_name(run("db", "backup").output)

	assert first != second, "the two backups collided, so this is not testing pruning"

	pruned = run("db", "backup", "--keep", "1")

	assert "Deleted" in pruned.output
	assert first in pruned.output


def test_the_database_and_its_backups_are_owner_only (
	run: typing.Callable[..., typer.testing.Result], home: pathlib.Path
) -> None:
	"""`#175`. `config.toml` was 0600 and the database beside it was 0644.

	§12.1a's argument for having no local password is that anyone who can read the file can
	read every row with `sqlite3` — which makes the filesystem permission the authentication,
	not a detail. A backup is the whole database, so it is exactly as sensitive.
	"""

	run("init", "--workspace", "Real")
	taken = _backup_name(run("db", "backup").output)

	database = _settings().sqlite_path

	assert database is not None
	assert database.stat().st_mode & 0o077 == 0, oct(database.stat().st_mode)

	copy = subroutine.db.backup.directory(_settings()) / taken

	assert copy.stat().st_mode & 0o077 == 0, oct(copy.stat().st_mode)

	# **And everything written beside it** (`#927`'s L-8). The row-count note was written at
	# whatever umask was in force, so a directory of `-rw-------` copies carried a `-rw-rw-r--`
	# file beside each one — and this test passed throughout, because it looked at the backup
	# and not at the directory. Row counts are not the rows, which is why this was Low; a
	# backup directory where one file in two is world-readable is the part worth not having.
	beside = [
		found
		for found in copy.parent.iterdir()
		if found.is_file() and found.name.startswith(copy.name) and found != copy
	]

	assert beside, "nothing was written beside the backup, so this checks nothing"

	for found in beside:
		assert found.stat().st_mode & 0o077 == 0, (
			f"{found.name} is {oct(found.stat().st_mode)} beside a backup that is not"
		)


def test_a_backup_says_how_much_it_copied (
	own_database: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""`#395`. A backup of an empty instance passed every check there was.

	Size, and the schema head read back from inside the copy — both correct, because an empty
	database is a *valid* database. §12.6's verification asks whether the file arrived intact
	and never asked whether it held anything.

	**Measured on the machine this was found on: the empty backup and a real one were the same
	size, 458,752 bytes, at the same schema.** So neither figure in the reported line could
	have told them apart, and four hollow backups reported success over a day.

	Counted on the *source*, which is the half that is always answerable — a PostgreSQL backup
	is a `pg_dump` script, not a database anything can open without restoring it first.
	"""

	monkeypatch.setenv("SUBROUTINE_BACKUP_DIRECTORY", str(tmp_path))
	subroutine.db.migrate.upgrade(own_database)

	settings = subroutine.config.Settings(
		database_url=own_database, backup_directory=str(tmp_path)
	)
	engine = subroutine.db.session.create_engine(own_database)

	try:
		empty = subroutine.db.backup.take(engine, settings)

		assert empty.holdings is not None
		assert not any(empty.holdings.values()), (
			"a migrated but unused instance holds no work, and must say so"
		)

		# A workspace rather than `_seed_instance`, which writes only the `instance` row —
		# that is configuration, not work, and counting it would report an empty instance as
		# full. Which is the distinction this whole check exists to make.
		factory = subroutine.db.session.create_session_factory(engine)

		with subroutine.db.session.session_scope(factory) as session:
			session.add(
				subroutine.db.models.identity.Workspace(
					slug=f"ws-{uuid.uuid4().hex[:8]}", title="Something to back up"
				)
			)

		filled = subroutine.db.backup.take(engine, settings)

		assert filled.holdings is not None
		assert filled.holdings["workspace"] >= 1, "and a real one reports what it copied"

	finally:
		engine.dispose()


def test_counting_a_backup_never_turns_a_good_one_into_a_failure (
	own_database: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The check must not become the thing that loses the data.

	A backup that succeeded and then could not be *described* is still a backup. Reporting it
	as failed would make a legibility feature into the loss it was written to prevent — so an
	unreadable count is an empty mapping and a sentence telling the operator to look, never an
	exception.
	"""

	monkeypatch.setenv("SUBROUTINE_BACKUP_DIRECTORY", str(tmp_path))
	subroutine.db.migrate.upgrade(own_database)
	_seed_instance(own_database)

	settings = subroutine.config.Settings(
		database_url=own_database, backup_directory=str(tmp_path)
	)
	engine = subroutine.db.session.create_engine(own_database)

	monkeypatch.setattr(subroutine.db.backup, "COUNTED", ("no_such_table",))

	try:
		written = subroutine.db.backup.take(engine, settings)

	finally:
		engine.dispose()

	assert written.path.exists(), "the backup itself must still have been taken"
	assert written.holdings == {}, "an uncountable backup is described as unknown, not refused"
	assert "check the copy" in subroutine.cli.main._what_it_held(written)


def test_a_connection_that_has_gone_by_the_second_look_is_not_using_the_database (
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""`#725`. The gate refused a restore twice in one day over a connection nobody could name.

	**The cause is not identified and this does not claim to fix it.** Four hypotheses were put
	up and measured away — a lingering ``pg_dump``, a disposed pool's backend, an autovacuum
	worker, and 504 restore runs under eight-way parallel load with no reproduction.

	What is claimed is narrower and holds whatever the cause turns out to be: this guard is
	about a database somebody is **using**, and use persists. A running service holds its
	connection for as long as it runs, so a look a moment later cannot miss one — and anything
	gone within that moment was not using it.

	Driven through ``in_use_by``'s own entry point with the backend answer replaced, because the
	real race is rare enough that 504 attempts did not produce it and a test that waits for it
	would be the flake it is fixing.
	"""

	answers = iter(["1 other connection — client backend, idle", None])
	asked = []

	def _answered (engine: sqlalchemy.engine.Engine) -> str | None:
		asked.append(engine)

		return next(answers)

	monkeypatch.setattr(subroutine.db.backup, "_postgresql_in_use_by", _answered)
	monkeypatch.setattr(subroutine.db.backup, "_is_sqlite", lambda _engine: False)
	# The wait itself is not what is being checked, and a test that pauses for it is a test
	# that pauses.
	monkeypatch.setattr(subroutine.db.backup, "_SETTLE_SECONDS", 0)

	engine = sqlalchemy.create_engine("postgresql+psycopg:///nowhere")

	try:
		assert subroutine.db.backup.in_use_by(engine) is None, (
			"a connection that had gone by the second look was still reported as a holder"
		)

	finally:
		engine.dispose()

	assert len(asked) == 2, f"the database was asked {len(asked)} times rather than twice"


def test_something_still_there_on_the_second_look_is_reported (
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""The half that stops the retry becoming "never refuse".

	Without it, `#725`'s fix and a deleted guard are indistinguishable — which is the shape
	`#405` is about, and the reason a refusal test always needs its opposite beside it.

	**The second answer is what is reported**, not the first: if something really is holding the
	database, its description a moment later is the more current of the two.
	"""

	# The first look of every pair says "idle" and the second says "active", so both callers
	# below get a fresh pair — `check_unused` asks `in_use_by`, which asks twice itself.
	looks = itertools.count()

	monkeypatch.setattr(
		subroutine.db.backup,
		"_postgresql_in_use_by",
		lambda _engine: (
			"1 other connection — idle" if next(looks) % 2 == 0
			else "1 other connection — active"
		),
	)
	monkeypatch.setattr(subroutine.db.backup, "_is_sqlite", lambda _engine: False)
	# The wait itself is not what is being checked, and a test that pauses for it is a test
	# that pauses.
	monkeypatch.setattr(subroutine.db.backup, "_SETTLE_SECONDS", 0)

	engine = sqlalchemy.create_engine("postgresql+psycopg:///nowhere")

	try:
		assert subroutine.db.backup.in_use_by(engine) == "1 other connection — active"

		with pytest.raises(subroutine.errors.ValidationError) as refused:
			subroutine.db.backup.check_unused(engine)

	finally:
		engine.dispose()

	assert "active" in str(refused.value)


def test_a_database_nobody_is_using_reports_nobody_however_it_is_asked (
	own_database: str,
) -> None:
	"""`SR#741`. The other half of `SR#725`, driven against a real database rather than a patched one.

	**The refusal half is driven with a real second connection open**, deliberately, because a
	hand-made row would only prove the formatting. The same reasoning applies here and had not
	been applied: an idle database answering *nobody* was checked only with
	``_postgresql_in_use_by`` replaced, which proves the retry's control flow and nothing about
	the query.

	**That is the half the retry can break, and it is not hypothetical.** ``in_use_by`` now looks
	twice, so the second look happens while the first look's own connection may still be open —
	and ``pg_stat_activity`` excludes only the backend asking. A change to pooling, to where
	``engine.dispose()`` sits, or to the settle time could make this function report *itself*.
	Every test would stay green while an operator halfway through a recovery is refused for a
	reason nothing can account for, which is the exact failure `SR#725` exists to remove.

	**Both connection paths, because they are not the same risk.** One engine asked repeatedly
	reuses a pooled backend, which is excluded by pid; a fresh engine each time — what
	``db restore`` actually does — opens and closes one per call, and a backend on its way out is
	precisely what the four refuted hypotheses were about.
	"""

	subroutine.db.migrate.upgrade(own_database)

	reused = subroutine.db.session.create_engine(own_database)

	try:
		pooled = [subroutine.db.backup.in_use_by(reused) for _ in range(5)]

	finally:
		reused.dispose()

	fresh = []

	for _ in range(5):
		engine = subroutine.db.session.create_engine(own_database)

		try:
			fresh.append(subroutine.db.backup.in_use_by(engine))

		finally:
			engine.dispose()

	assert pooled == [None] * 5, (
		f"asking one engine repeatedly reported somebody on a database nobody is using: {pooled}"
	)

	assert fresh == [None] * 5, (
		f"a fresh engine per call — what db restore does — reported somebody on a database "
		f"nobody is using: {fresh}"
	)

	# And the guard built on it agrees, since that is what an operator actually meets.
	quiet = subroutine.db.session.create_engine(own_database)

	try:
		subroutine.db.backup.check_unused(quiet)

	finally:
		quiet.dispose()

	# **The case ``engine.dispose()`` is actually there for**, which nothing exercised: an engine
	# whose pool already holds idle connections. One of them answers the question and the rest
	# look like somebody else — *"our own pool would otherwise answer for somebody else"*, which
	# is a comment nothing was measuring. Two, because with one the pool hands back the same
	# backend and it is excluded by pid, so the defect cannot appear.
	warm = subroutine.db.session.create_engine(own_database)

	try:
		if not own_database.startswith("sqlite"):
			pooled_pair = [warm.connect(), warm.connect()]

			for connection in pooled_pair:
				connection.execute(sqlalchemy.text("SELECT 1"))

			for connection in pooled_pair:
				connection.close()

			assert subroutine.db.backup.in_use_by(warm) is None, (
				"this engine's own idle pool was reported as somebody else using the database"
			)

	finally:
		warm.dispose()


def test_a_refusal_says_who_is_holding_it_rather_than_how_many (
	own_database: str,
) -> None:
	"""`#725`. *"1 other connection to the database"* is the same sentence for three situations.

	A colleague connected, your own service running, and something on its way out all produced
	it — three different next actions, and an operator halfway through a recovery could not tell
	which they had. Every column needed to say so was already in ``pg_stat_activity``.

	Driven against a real database holding a real second connection, because what is being
	checked is that the *query* returns those columns — a hand-made row would only prove the
	formatting.
	"""

	if own_database.startswith("sqlite"):
		pytest.skip("the columns being checked are PostgreSQL's")

	subroutine.db.migrate.upgrade(own_database)

	held = subroutine.db.session.create_engine(own_database)
	asking = subroutine.db.session.create_engine(own_database)

	try:
		with held.connect() as connection:
			connection.execute(sqlalchemy.text("SELECT 1"))

			said = subroutine.db.backup.in_use_by(asking)

	finally:
		held.dispose()
		asking.dispose()

	assert said is not None, "a database with another connection open reported nobody"

	# What it is, not merely that there is one. `backend_type` is what separates a client from
	# an autovacuum worker, which was one of the hypotheses this could not previously rule out.
	assert "client backend" in said, f"the refusal does not say what is holding it: {said}"
	assert "s old" in said, f"the refusal does not say how long it has been there: {said}"


def test_a_database_password_is_never_written_onto_a_command_line () -> None:
	"""``/proc/<pid>/cmdline`` is world-readable, and a dump of a large database runs for minutes.

	The URL handed to ``pg_dump`` and ``psql`` carried the password, so for the whole life of a
	backup every process on the machine could read it — including one belonging to somebody who
	has an account and nothing else. ``PGPASSWORD`` in the child's environment is the supported
	alternative and is what these tools document.

	Both halves are asserted, because passing neither is also a way to keep the password off the
	command line, and it does not work.
	"""

	# A real engine, not a mock: `_connectable` and `_secret_of` both read `engine.url`, and a
	# stand-in without one would be a test of the stand-in. Nothing connects — SQLAlchemy builds
	# an engine lazily — so the address need not exist.
	engine = sqlalchemy.create_engine("postgresql+psycopg://someone:hunter2@db.example:5432/work")

	connectable = subroutine.db.backup._connectable(engine)

	assert "hunter2" not in connectable, connectable
	assert "someone@db.example" in connectable, "and everything else survives"
	assert connectable.startswith("postgresql://"), "the DBAPI is still stripped"

	assert subroutine.db.backup._secret_of(engine) == {"PGPASSWORD": "hunter2"}


def test_a_url_with_no_password_adds_nothing_to_the_environment () -> None:
	"""Which is the ordinary case here — authentication by Unix socket — and it must not change.

	An empty mapping means the child inherits this process's environment untouched, so the
	common path is byte for byte what it was.
	"""

	engine = sqlalchemy.create_engine("postgresql+psycopg:///subroutine")

	assert subroutine.db.backup._secret_of(engine) == {}
