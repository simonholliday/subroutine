"""A migration's refusal is said in a sentence, with where the upgrade stopped - `SR#5155`.

M-20 of the cold review of 2026-10-17, decided in `#5097`: a migration that will not run over what
a database holds raised a bare ``RuntimeError``, so ``db upgrade``, ``db migrate``, ``init`` and
``db restore``'s offered upgrade ended in the crash report, and the statements it wrote for the
operator were at the foot of the crash file. **It is a ``schema_mismatch`` now, said with the
revision the database reached**, which is read back: on SQLite every step before the refusal has
committed, and on PostgreSQL the whole run is one transaction and none has.
"""

import ast
import pathlib
import re
import typing

import pytest
import sqlalchemy
import typer.testing

import subroutine.config
import subroutine.db.migrate
import subroutine.db.session
import subroutine.db.types
import subroutine.errors
import test_instances
import test_migrations
import test_personal_path

#: The personal path's fresh home and its runner, and a migrated database on each backend, bound
#: here so pytest finds them as fixtures.
home = test_personal_path.home
run = test_personal_path.run
migrated_url = test_migrations.migrated_url

Run = typing.Callable[..., typer.testing.Result]

#: The schema v0.10.0 shipped, the last before every Inbox was held public at the top level.
RELEASED = "6c708db1582c"

#: The schema v0.9.13 shipped, two revisions before it: an upgrade from there moves twice and is
#: refused at the third, which is where SQLite and PostgreSQL leave a database in different places.
EARLIER = "124f22afc629"


def _said (result: typer.testing.Result) -> str:
	"""Return what a command printed, as one line."""

	return " ".join(result.output.split())


def _at (url: str) -> str | None:
	"""Return the migration a database is at."""

	engine = subroutine.db.session.create_engine(url)

	try:
		return subroutine.db.migrate.current_revision(engine)

	finally:
		engine.dispose()


def _with_a_private_inbox (revision: str) -> str:
	"""Take this instance's database back to ``revision``, make its Inbox private, and return it."""

	url = subroutine.config.load_settings().database_url
	subroutine.db.migrate.downgrade(url, revision)
	engine = subroutine.db.session.create_engine(url)

	try:
		with engine.begin() as connection:
			connection.execute(
				sqlalchemy.text("UPDATE project SET visibility = 'private' WHERE is_inbox")
			)

	finally:
		engine.dispose()

	return url


def _refused_in_a_sentence (refused: typer.testing.Result, url: str) -> None:
	"""Assert a refused upgrade was said rather than crashed, naming where the database is."""

	said = _said(refused)

	assert isinstance(refused.exception, SystemExit), repr(refused.exception)
	assert "Something went wrong" not in said, said
	assert "The Inbox of 'metacortex': it is private." in said, said
	assert "UPDATE project SET visibility = 'public' WHERE id = '" in said, said
	assert "then upgrade it again." in said, said

	stopped = re.search(r"The upgrade stopped at (\w+)\.", said)

	assert stopped is not None, said
	assert stopped.group(1) == _at(url), "the revision named is not the one the database is at"


@pytest.mark.parametrize(
	"arguments",
	[("db", "upgrade", "--yes"), ("db", "migrate"), ("init", "--workspace", "metacortex")],
	ids=lambda arguments: " ".join(arguments),
)
def test_an_upgrade_a_migration_refuses_is_said_with_where_it_stopped (
	run: Run, arguments: tuple[str, ...]
) -> None:
	"""Each command that migrates prints the refusal, its statements and the revision reached."""

	run("init", "--workspace", "metacortex")
	url = _with_a_private_inbox(RELEASED)

	_refused_in_a_sentence(run(*arguments, expect=1), url)

	assert _at(url) == RELEASED


def test_an_upgrade_refused_after_it_moved_on_names_where_it_stopped_and_the_backup (
	run: Run,
) -> None:
	"""From v0.9.13's schema the upgrade moves twice before the Inbox's step refuses, and said
	*Nothing has been changed* of a database two revisions on."""

	run("init", "--workspace", "metacortex")
	url = _with_a_private_inbox(EARLIER)

	refused = run("db", "upgrade", "--yes", expect=1)

	_refused_in_a_sentence(refused, url)

	assert "This step changed nothing." in _said(refused)
	assert "What was there before the upgrade is at" in _said(refused)


def test_a_restored_backup_whose_upgrade_is_refused_says_so (run: Run) -> None:
	"""``db restore`` offers to upgrade an older backup once it is back, and that upgrade crashed."""

	run("init", "--workspace", "metacortex")
	url = _with_a_private_inbox(RELEASED)
	name = test_instances._backup_name(run("db", "backup").output)

	_refused_in_a_sentence(run("db", "restore", name, "--recover", "--yes", expect=1), url)


@pytest.mark.parametrize("migrated_url", ["sqlite", "postgresql"], indirect=True)
def test_the_revision_a_refused_upgrade_names_is_where_each_backend_left_it (
	migrated_url: str,
) -> None:
	"""On both backends, through :func:`subroutine.db.migrate.upgrade`, which every command calls."""

	subroutine.db.migrate.downgrade(migrated_url, EARLIER)
	engine = subroutine.db.session.create_engine(migrated_url)
	workspace, status = subroutine.db.types.new_uuid(), subroutine.db.types.new_uuid()
	inbox = subroutine.db.types.new_uuid()

	try:
		with engine.begin() as connection:
			test_migrations._insert(
				connection, "workspace", {"id": workspace, "slug": "zion", "title": "Zion"}
			)
			test_migrations._insert(
				connection,
				"status",
				{"id": status, "workspace_id": workspace, "key": "active", "label": "Active"},
			)
			test_migrations._insert(
				connection,
				"project",
				{
					"id": inbox,
					"workspace_id": workspace,
					"key": "inbox",
					"title": "Inbox",
					"status_id": status,
					"is_inbox": True,
					"visibility": "private",
					"path": f"/{inbox}/",
				},
			)

	finally:
		engine.dispose()

	with pytest.raises(subroutine.errors.SchemaMismatch) as refused:
		subroutine.db.migrate.upgrade(migrated_url)

	assert "The Inbox of 'zion': it is private." in refused.value.detail, refused.value.detail
	assert refused.value.hint == f"The upgrade stopped at {_at(migrated_url)}.", refused.value.hint


def _bare_refusals (versions: pathlib.Path) -> list[str]:
	"""Return each migration under ``versions`` that raises a bare ``RuntimeError``.

	**The directory is an argument** (`SR#405`), so a planted migration reaches the same scan.
	"""

	found = []

	for path in sorted(versions.glob("*.py")):
		for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
			if (
				isinstance(node, ast.Raise)
				and isinstance(node.exc, ast.Call)
				and isinstance(node.exc.func, ast.Name)
				and node.exc.func.id == "RuntimeError"
			):
				found.append(path.name)

	return found


def test_every_migration_refuses_with_the_refusal_an_upgrade_says (tmp_path: pathlib.Path) -> None:
	"""A migration that raises a bare ``RuntimeError`` reaches the crash report, so none does, and
	the next one written that way is found here rather than by an operator."""

	versions = subroutine.db.migrate.MIGRATIONS_DIRECTORY / "versions"

	assert _bare_refusals(versions) == []

	(tmp_path / "0001_planted.py").write_text(
		'def upgrade () -> None:\n    raise RuntimeError("planted")\n', encoding="utf-8"
	)

	assert _bare_refusals(tmp_path) == ["0001_planted.py"]
