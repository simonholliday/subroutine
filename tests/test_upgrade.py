"""The upgrade path — docs/design.md §12.4a, decision ``#97``, item ``#89``.

Two halves that have to hold together. **A command run against a database this build does not
match is refused with the remedy**, rather than failing somewhere inside SQLAlchemy with a
message about a missing column; and **``subroutine db upgrade`` is the safe sequence** — report
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
import subroutine.installations

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

	result = run("agenda", expect=1)

	assert previous_revision() in result.output, "it says where the database is"
	assert head_revision() in result.output, "and what is expected"
	assert "subroutine db upgrade" in result.output, "and what to do about it"


def test_a_database_ahead_of_this_build_says_to_update_the_software (
	run: typing.Callable[..., typer.testing.Result], database: pathlib.Path
) -> None:
	"""The opposite direction, and the opposite remedy.

	Migrating cannot produce a revision this build has never seen, so telling somebody to
	upgrade the database would be a confident instruction to do nothing at all.
	"""

	run("init", "--workspace", "Personal")
	stamp(database, FROM_THE_FUTURE)

	result = run("agenda", expect=1)

	assert FROM_THE_FUTURE in result.output
	assert "no downgrade" in result.output
	assert "subroutine db upgrade" not in result.output, "the wrong remedy is worse than none"


def test_the_administrative_commands_still_work_while_the_check_is_firing (
	run: typing.Callable[..., typer.testing.Result], database: pathlib.Path
) -> None:
	"""**The property that makes the check safe to have at all** (docs/design.md §12.4).

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
	assert "Upgraded from" in run("db", "upgrade").output


def test_upgrade_reports_both_versions_before_it_does_anything (
	run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""Step 1 of decision ``#97``, and the answer when there is nothing to do.

	Both numbers even in the quiet case: "nothing to do" is only trustworthy beside the two
	values it was concluded from.
	"""

	run("init", "--workspace", "Personal")

	result = run("db", "upgrade")

	assert head_revision() in result.output
	assert "Nothing to do." in result.output


def test_upgrade_names_the_program_it_is_upgrading_to (
	run: typing.Callable[..., typer.testing.Result]
) -> None:
	"""`#343`. Two schema numbers cannot say whether the *software* moved.

	Found on the served instance on 2026-08-03: it was upgraded, the upgrade did not happen —
	`pip` declined, because the installed copy came from a checkout and its development version
	compared as newer than the index's — and the one command in the procedure that reports
	anything printed exactly what a successful upgrade carrying no migration prints.

	So the schema report is not evidence of an upgrade, and it was the only evidence anybody
	had. The version is a number the command already knows and the operator can compare against
	what they thought they installed.
	"""

	run("init", "--workspace", "Personal")

	result = run("db", "upgrade")

	assert f"Subroutine {subroutine.installations.program()} expects" in result.output, (
		f"the report names no program version, so 'Nothing to do.' cannot be told from an "
		f"upgrade that never happened: {result.output}"
	)


def test_upgrade_says_when_the_installed_copy_could_not_have_been_replaced (
	run: typing.Callable[..., typer.testing.Result], monkeypatch: pytest.MonkeyPatch
) -> None:
	"""The other half of `#343`: a version comparison against an index is then meaningless.

	Derived from ``installations.ordered`` rather than spelled again — it already declines
	anything that is not three plain numbers, which is exactly the shape of a build made from a
	checkout. Two spellings of "is this a release" is how the reach and the write set came
	apart in `#413`.
	"""

	run("init", "--workspace", "Personal")

	monkeypatch.setattr(subroutine.installations, "program", lambda: "0.5.1.dev29+g1b5e9a698")

	said = run("db", "upgrade").output

	assert "development build" in said, (
		f"a checkout build is reported as though it were a release, which is the state where "
		f"'pip install --upgrade' declines and says nothing: {said}"
	)

	# And a real release must not carry the caveat, or it becomes noise on every upgrade.
	monkeypatch.setattr(subroutine.installations, "program", lambda: "0.5.0")

	assert "development build" not in run("db", "upgrade").output


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

	result = run("db", "upgrade")

	assert "Backed up to" in result.output
	assert f"Upgraded from {older} to {head_revision()}" in result.output

	backups = list((home / "xdg_data_home" / "subroutine" / "backups").glob("*.db"))

	assert len(backups) == 1, f"expected exactly one backup, found {backups}"
	assert older in backups[0].name, "the copy records the schema it was taken on"

	# **And it says the copy is nobody's to remove** (`#1676`). `take` is called with no
	# `keep`, so one accumulates per upgrade and the only symptom is a full disk. Asserted
	# here rather than only in the transcript, because the transcript is prose and this is
	# the command actually running.
	assert "Nothing deletes that copy for you" in result.output
	assert "--keep N" in result.output, (
		"the second half is the one nobody would guess — the command that prunes counts "
		"these alongside routine backups, so an hourly timer can delete this one"
	)

	# And the instance is usable again, which is the only outcome that matters to its owner.
	assert "Something from before the upgrade" in run("agenda").output


def test_upgrade_refuses_a_database_from_the_future_without_touching_it (
	run: typing.Callable[..., typer.testing.Result],
	database: pathlib.Path,
	home: pathlib.Path,
) -> None:
	"""And takes no backup on the way, because it is not about to change anything."""

	run("init", "--workspace", "Personal")
	stamp(database, FROM_THE_FUTURE)

	result = run("db", "upgrade", expect=1)

	assert "newer than this software" in result.output
	assert not list((home / "xdg_data_home" / "subroutine" / "backups").glob("*")), (
		"a refusal that backs up first has done work nobody asked for"
	)


def test_the_old_upgrade_spelling_says_where_it_went (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""`#509`. **Not an alias** — Simon turned that down, and it refuses rather than acting.

	The distinction is the point. An alias would *do the upgrade* under a name whose sibling
	``db upgrade`` used to mean the blunt migrator, so one spelling would mean two things
	depending on when somebody learned it — and the two differ by whether the database is
	backed up first.

	**Removing it outright was worse than either option, which only driving it showed.** Typer
	offers the nearest command it can find, and the nearest to ``upgrade`` is ``update`` — so
	an operator migrating a database was pointed at the one that edits a task.
	"""

	result = run("upgrade", expect=2)

	assert "subroutine db upgrade" in result.output, "it has to name where the command went"
	assert "db migrate" in result.output, "and what took its old name, since that one is blunt"
	assert "update" not in result.output, "the suggestion this exists to prevent"


def test_the_signpost_stays_out_of_the_help (
	run: typing.Callable[..., typer.testing.Result],
) -> None:
	"""§12.2a's rule for ``ls``: a synonym you can see is a second thing to choose between.

	It is worth having where somebody types the old name from memory or from a runbook, and
	worth nothing in a list of what this can do — where it would read as a command rather than
	as a redirection.
	"""

	assert "upgrade" not in run("--help").output
