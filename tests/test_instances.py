"""Separate instances on one machine, and backing them up — SPEC.md §12.5, §12.6 and §12.6a.

These exist because the project is about to keep its own plan in a database it can no longer
reset. Two properties matter more than the rest and each has a test named for it: **a profile
isolates completely**, and **a restore never guesses whether it is a recovery or a clone**.

Restore is tested against a database this file creates and owns. It must never run against the
shared session-scoped one: the PostgreSQL path drops and recreates `public`, and the shared
database is where every other test in the suite lives.
"""

import datetime
import os
import pathlib
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

	def invoke (*arguments: str, expect: int = 0) -> typer.testing.Result:
		"""Run one command and check how it ended.

		The profile variable is cleared first, so each call behaves like a fresh shell.
		``--profile`` deliberately *exports* itself so that anything the process starts
		inherits the same instance (§12.5) — which in one process makes the choice stick to
		the next invocation, and a test that shared it would be testing the runner.
		"""

		os.environ.pop(subroutine.config.PROFILE_VARIABLE, None)

		result = runner.invoke(subroutine.cli.main.app, list(arguments))

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
		subroutine.db.backup.restore(engine, written.path, as_clone=False)

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
		subroutine.db.backup.restore(engine, written.path, as_clone=True)

	finally:
		engine.dispose()

	restored = _instance_id(own_database)

	assert restored is not None
	assert restored != identity


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
	"""An agent about to do something bulk should be able to snapshot first (§12.6)."""

	world = test_api_tasks._world(session)
	response = world.call("POST", "/v1/admin/backups")

	assert response.status_code == 201

	body = response.json()

	assert body["schema_head"] == subroutine.db.migrate.head_revision()
	assert body["size_bytes"] > 0
	assert pathlib.Path(body["path"]).is_file()

	listed = world.call("GET", "/v1/admin/backups")

	assert listed.status_code == 200
	assert [item["name"] for item in listed.json()["items"]] == [body["name"]]


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

	def truncating_move (source: str, destination: str) -> str:
		"""Stand in for a network write that stops half way through."""

		pathlib.Path(destination).write_bytes(pathlib.Path(source).read_bytes()[:512])
		pathlib.Path(source).unlink()

		return destination

	# Named as a string: the module under test reaches `shutil` through its own namespace, and
	# `--strict` will not have an attribute access into another module's imports.
	monkeypatch.setattr("subroutine.db.backup.shutil.move", truncating_move)

	with pytest.raises(subroutine.errors.ServiceUnavailable):
		subroutine.db.backup.take(engine, _settings())

	assert subroutine.db.backup.catalogue(_settings()) == [], (
		"a truncated backup must not survive to be listed as one"
	)
