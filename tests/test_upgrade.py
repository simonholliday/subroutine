"""The upgrade path — SPEC.md §12.4a, decision ``#97``, item ``#89``.

Two halves that have to hold together. **A command run against a database this build does not
match is refused with the remedy**, rather than failing somewhere inside SQLAlchemy with a
message about a missing column; and **``subroutine upgrade`` is the safe sequence** — report
both versions, back up and verify, migrate, read it back — over machinery that already existed.

The third property is the one worth naming, because it is the one that is easy to break by
accident: **the administrative commands must keep working while the check is firing.** They are
what somebody reaches for once it fires, so a check that covered them too would be a lock with
the key inside.

SQLite throughout, and deliberately: the thing under test is the version comparison and the
ordering around it, neither of which is backend-specific, and a test that drops and rebuilds a
schema must never be pointed at the shared PostgreSQL database the rest of the suite lives in.
"""

import os
import pathlib
import typing

import alembic.script
import pytest
import sqlalchemy
import typer.testing

import subroutine.cli.main
import subroutine.db.migrate

#: Not a revision anybody has written, and not one anybody will: the check only has to find it
#: absent from the migration directory to conclude the database came from a later release.
FROM_THE_FUTURE = "ffffffffffff"


@pytest.fixture
def home (tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
	"""Point every XDG directory at a fresh temporary home, with nothing inherited."""

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
		"""Run one command and check how it ended."""

		result = runner.invoke(subroutine.cli.main.app, list(arguments))

		assert result.exit_code == expect, (
			f"'subroutine {' '.join(arguments)}' exited {result.exit_code}\n"
			f"{result.output}\n{result.exception!r}"
		)

		return result

	return invoke


@pytest.fixture
def database (home: pathlib.Path) -> pathlib.Path:
	"""Return where this temporary instance keeps its database."""

	return home / "xdg_data_home" / "subroutine" / "subroutine.db"


def head_revision () -> str:
	"""Return the newest migration, as the string it always is in a built package.

	:func:`subroutine.db.migrate.head_revision` is typed ``str | None`` for the empty-directory
	case, which cannot arise here — the migrations ship inside the package.
	"""

	newest = subroutine.db.migrate.head_revision()

	assert newest is not None, "the package ships with migrations"

	return newest


def previous_revision () -> str:
	"""Return the migration immediately before the newest one.

	Derived rather than written down. A hard-coded revision id makes a test that starts failing
	the day somebody adds a migration, which teaches people to edit tests to make builds green.
	"""

	script = alembic.script.ScriptDirectory.from_config(
		subroutine.db.migrate.build_config("sqlite://")
	)

	return list(script.walk_revisions())[1].revision


def stamp (database: pathlib.Path, revision: str) -> None:
	"""Record a database as being at a revision, without moving the schema.

	Enough for everything that only *compares* the two numbers, and much steadier than running
	a real downgrade for a test that is not about migrating. The tests that are about migrating
	do the real thing.
	"""

	engine = sqlalchemy.create_engine(f"sqlite:///{database}")

	try:
		with engine.begin() as connection:
			connection.exec_driver_sql(
				"UPDATE alembic_version SET version_num = ?", (revision,)
			)

	finally:
		engine.dispose()


def test_a_database_behind_this_build_is_refused_with_the_remedy (
	run: typing.Callable[..., typer.testing.Result], database: pathlib.Path
) -> None:
	"""The gap decision ``#97`` names: the CLI made this comparison nowhere.

	It failed instead with ``no such column: workspace.next_ref_number`` — a sentence about our
	internals, arriving at the moment somebody has least patience for one.
	"""

	run("init", "--workspace", "Personal")
	stamp(database, previous_revision())

	result = run("today", expect=1)

	assert previous_revision() in result.output, "it says where the database is"
	assert head_revision() in result.output, "and what is expected"
	assert "subroutine upgrade" in result.output, "and what to do about it"


def test_a_database_ahead_of_this_build_says_to_update_the_software (
	run: typing.Callable[..., typer.testing.Result], database: pathlib.Path
) -> None:
	"""The opposite direction, and the opposite remedy.

	Migrating cannot produce a revision this build has never seen, so telling somebody to
	upgrade the database would be a confident instruction to do nothing at all.
	"""

	run("init", "--workspace", "Personal")
	stamp(database, FROM_THE_FUTURE)

	result = run("today", expect=1)

	assert FROM_THE_FUTURE in result.output
	assert "no downgrade" in result.output
	assert "subroutine upgrade" not in result.output, "the wrong remedy is worse than none"


def test_the_administrative_commands_still_work_while_the_check_is_firing (
	run: typing.Callable[..., typer.testing.Result], database: pathlib.Path
) -> None:
	"""**The property that makes the check safe to have at all** (SPEC.md §12.4).

	These are what somebody reaches for once the refusal appears, so a check that covered them
	too would leave an instance nobody could get out of. ``db backup`` in particular: taking a
	copy of a database you are about to migrate is the whole of the recovery story.
	"""

	run("init", "--workspace", "Personal")

	# A real downgrade rather than a stamped number, because the last of these four actually
	# migrates: against a stamped database it would try to apply a migration already applied
	# and fail for a reason that has nothing to do with what this test is about.
	older = previous_revision()
	subroutine.db.migrate.downgrade(f"sqlite:///{database}", older)

	assert older in run("db", "current").output
	assert "Backed up" in run("db", "backup").output
	assert "schema" in run("db", "backups").output

	# And the way out, which is the one command that must never be refused for being needed.
	assert "Upgraded from" in run("upgrade").output


def test_upgrade_reports_both_versions_before_it_does_anything (
	run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""Step 1 of decision ``#97``, and the answer when there is nothing to do.

	Both numbers even in the quiet case: "nothing to do" is only trustworthy beside the two
	values it was concluded from.
	"""

	run("init", "--workspace", "Personal")

	result = run("upgrade")

	assert head_revision() in result.output
	assert "Nothing to do." in result.output


def test_upgrade_takes_a_backup_before_it_migrates (
	run: typing.Callable[..., typer.testing.Result],
	database: pathlib.Path,
	home: pathlib.Path,
) -> None:
	"""A real downgrade and a real migration, not a stamped number.

	The ordering is the feature. Anybody can run the two commands; what nobody manages
	reliably is running them in that order at the moment something is already wrong.
	"""

	run("init", "--workspace", "Personal")
	run("add", "Something from before the upgrade")

	older = previous_revision()
	subroutine.db.migrate.downgrade(f"sqlite:///{database}", older)

	result = run("upgrade")

	assert "Backed up to" in result.output
	assert f"Upgraded from {older} to {head_revision()}" in result.output

	backups = list((home / "xdg_data_home" / "subroutine" / "backups").glob("*.db"))

	assert len(backups) == 1, f"expected exactly one backup, found {backups}"
	assert older in backups[0].name, "the copy records the schema it was taken on"

	# And the instance is usable again, which is the only outcome that matters to its owner.
	assert "Something from before the upgrade" in run("today").output


def test_upgrade_refuses_a_database_from_the_future_without_touching_it (
	run: typing.Callable[..., typer.testing.Result],
	database: pathlib.Path,
	home: pathlib.Path,
) -> None:
	"""And takes no backup on the way, because it is not about to change anything."""

	run("init", "--workspace", "Personal")
	stamp(database, FROM_THE_FUTURE)

	result = run("upgrade", expect=1)

	assert "newer than this software" in result.output
	assert not list((home / "xdg_data_home" / "subroutine" / "backups").glob("*")), (
		"a refusal that backs up first has done work nobody asked for"
	)
