"""Tests for first-run setup, and for the command that performs it.

The done-criterion for S1-11 is behavioural and unusually literal: a fresh ``init`` on an
empty directory produces a queryable database and says exactly one thing. Both halves are
checked here — the domain function against both backends, and the command itself end to
end against a real SQLite file in a temporary home.
"""

import pathlib
import subprocess
import sys
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.config
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.system
import subroutine.domain.bootstrap
import subroutine.domain.instances
import subroutine.domain.tasks
import subroutine.errors

EXPECTED_FIRST_LINE = 'Ready. Try: subroutine add "something to do"'


@pytest.fixture
def isolated_home (tmp_path: pathlib.Path) -> typing.Iterator[dict[str, str]]:
	"""Yield an environment pointing XDG at a throwaway directory on local disk.

	Never inside the working tree: this repository lives on a network share where SQLite
	cannot take a lock, which is the very thing ``init`` refuses to proceed against.
	"""

	config_home = tmp_path / "config"
	data_home = tmp_path / "data"
	config_home.mkdir()
	data_home.mkdir()

	yield {"XDG_CONFIG_HOME": str(config_home), "XDG_DATA_HOME": str(data_home)}


def _run (environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
	"""Run the CLI as a subprocess, the way a user would."""

	return subprocess.run(
		[sys.executable, "-c", "import subroutine.cli.main; subroutine.cli.main.main()", *arguments],
		capture_output=True,
		text=True,
		env={"PATH": "/usr/bin:/bin", "HOME": str(pathlib.Path.home()), **environment},
		check=False,
	)


def test_initialising_creates_everything_a_first_task_needs (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Instance, user, workspace, Inbox and an owner membership, in one transaction."""

	result = subroutine.domain.bootstrap.initialise(
		session, username="simon", instance_name="Laptop"
	)

	assert result.created
	assert result.instance.name == "Laptop"
	assert result.user.username == "simon"
	assert result.workspace.slug == "simon"
	assert result.inbox.key == "INBOX"
	assert result.inbox.is_inbox
	assert result.inbox.template == "personal"

	membership = session.scalars(
		sqlalchemy.select(subroutine.db.models.identity.WorkspaceMember).where(
			subroutine.db.models.identity.WorkspaceMember.workspace_id == result.workspace.id
		)
	).one()

	assert membership.user_id == result.user.id


def test_the_inbox_is_immediately_usable (session: sqlalchemy.orm.Session) -> None:
	"""The point of setting up is being able to add something."""

	result = subroutine.domain.bootstrap.initialise(
		session, username="simon", instance_name="Laptop"
	)

	task = subroutine.domain.tasks.create(
		session, project=result.inbox, title="Call the dentist"
	)

	assert task.ref == "INBOX-1"


def test_initialising_twice_changes_nothing (session: sqlalchemy.orm.Session) -> None:
	"""Re-running setup against an existing database is uneventful, not an error."""

	first = subroutine.domain.bootstrap.initialise(
		session, username="simon", instance_name="Laptop"
	)
	second = subroutine.domain.bootstrap.initialise(
		session, username="someone-else", instance_name="Different"
	)

	assert not second.created
	assert second.instance.id == first.instance.id
	assert second.workspace.id == first.workspace.id
	assert second.user.id == first.user.id

	workspaces = session.scalars(
		sqlalchemy.select(sqlalchemy.func.count()).select_from(
			subroutine.db.models.identity.Workspace
		)
	).one()

	assert workspaces == 1


def test_the_instance_identity_is_minted_once (session: sqlalchemy.orm.Session) -> None:
	"""SPEC.md §13.7: generated at init and never changed."""

	instance, created = subroutine.domain.instances.establish(session, name="Laptop")

	assert created

	again, created_again = subroutine.domain.instances.establish(session, name="Renamed")

	assert not created_again
	assert again.id == instance.id
	assert again.name == "Laptop", "the name is not overwritten by a later run"


def test_an_uninitialised_database_says_what_to_do (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Errors say what to do next."""

	with pytest.raises(subroutine.errors.NotFound) as error:
		subroutine.domain.instances.require(session)

	assert error.value.hint is not None
	assert "subroutine init" in error.value.hint


def test_the_inbox_is_findable_by_flag (session: sqlalchemy.orm.Session) -> None:
	"""A task created with no project is filed here (SPEC.md §6.8)."""

	result = subroutine.domain.bootstrap.initialise(
		session, username="simon", instance_name="Laptop"
	)

	assert subroutine.domain.bootstrap.inbox_for(session, result.workspace) is not None
	assert (
		subroutine.domain.bootstrap.inbox_for(session, result.workspace).id  # type: ignore[union-attr]
		== result.inbox.id
	)


def test_a_password_may_be_set_at_setup (session: sqlalchemy.orm.Session) -> None:
	"""Optional: local mode opens the database directly and needs no login."""

	result = subroutine.domain.bootstrap.initialise(
		session,
		username=f"simon-{uuid.uuid4().hex[:6]}",
		instance_name="Laptop",
		password="a decent passphrase",
	)

	assert result.user.password_hash is not None


def test_init_says_one_line_and_leaves_a_working_database (
	isolated_home: dict[str, str],
) -> None:
	"""The S1-11 done-criterion, run as a user would run it."""

	result = _run(isolated_home, "init")

	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == EXPECTED_FIRST_LINE

	database = pathlib.Path(isolated_home["XDG_DATA_HOME"]) / "subroutine" / "subroutine.db"

	assert database.is_file()

	configuration = pathlib.Path(isolated_home["XDG_CONFIG_HOME"]) / "subroutine" / "config.toml"

	assert configuration.is_file()
	assert "secret_key" in configuration.read_text(encoding="utf-8")


def test_init_is_safe_to_run_again (isolated_home: dict[str, str]) -> None:
	"""Containers restart; setup should not fail the second time."""

	assert _run(isolated_home, "init").returncode == 0

	again = _run(isolated_home, "init")

	assert again.returncode == 0
	assert "Already set up" in again.stdout


def test_init_announces_nothing_a_person_did_not_ask_about (
	isolated_home: dict[str, str],
) -> None:
	"""SPEC.md §12.1: the workspace and the Inbox are created and not mentioned."""

	output = _run(isolated_home, "init").stdout

	for jargon in ("workspace", "Workspace", "Inbox", "instance", "migrat"):
		assert jargon not in output, f"{jargon!r} should not appear in ordinary output"


def test_verbose_prints_the_transcript (isolated_home: dict[str, str]) -> None:
	"""For whoever does want to know what happened."""

	output = _run(isolated_home, "init", "--verbose").stdout

	for expected in ("Database:", "Schema:", "Instance:", "User:", "Workspace:", "Inbox:"):
		assert expected in output

	assert EXPECTED_FIRST_LINE in output


def test_the_signing_key_is_never_printed (isolated_home: dict[str, str]) -> None:
	"""``config show`` reports whether it is set, not what it is."""

	assert _run(isolated_home, "init").returncode == 0

	configuration = pathlib.Path(isolated_home["XDG_CONFIG_HOME"]) / "subroutine" / "config.toml"
	stored = configuration.read_text(encoding="utf-8").split('"')[1]

	shown = _run(isolated_home, "config", "show")

	assert shown.returncode == 0
	assert stored not in shown.stdout
	assert "secret_key" in shown.stdout
	assert "(set)" in shown.stdout


def test_config_show_reports_where_each_value_came_from (
	isolated_home: dict[str, str],
) -> None:
	"""The S1-02 done-criterion, finally wired to a command."""

	shown = _run({**isolated_home, "SUBROUTINE_PORT": "9999"}, "config", "show")

	assert shown.returncode == 0
	assert "[environment]" in shown.stdout
	assert "9999" in shown.stdout
	assert "[default]" in shown.stdout


def test_db_current_reports_an_empty_database_honestly (
	isolated_home: dict[str, str],
) -> None:
	"""Before setup there is no schema, and saying so beats an exception."""

	before = _run(isolated_home, "db", "current")

	assert before.returncode == 0
	assert "no database here yet" in before.stdout

	assert _run(isolated_home, "init").returncode == 0

	after = _run(isolated_home, "db", "current")

	assert "Schema is at" in after.stdout
