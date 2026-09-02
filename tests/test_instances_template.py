"""The guard on `SR#1830`'s substitution: a planted instance is one a real ``init`` would make.

``tests/instance_templates.py`` stands in for ``subroutine init`` in the two terminal test files,
because building one costs 456 ms and copying one costs 0.5 ms. **That is a harness supplying
what its subject was meant to obtain**, which is this project's own recorded defect (`SR#405`),
and the only honest answer to it is a test that runs both and compares.

**The 410 call sites are the wider falsification** — every one of them asserts something about
what a fresh instance looks like, and they would fail together if the copy were wrong. What
they cannot see is the *difference* between the two, because they only ever meet one. That is
what these do.

**Why the databases are not compared byte for byte.** A ``secret_key``, an ``instance.id`` and
a row of timestamps differ between any two inits, which is correct and is not what a test of the
copy should care about. What has to match is the **schema** — the thing the migration replay
exists to produce, and the thing `SR#1830` refused to shortcut with ``create_all`` because
autogenerate cannot see a CHECK constraint.
"""

import os
import pathlib
import sqlite3
import typing

import pytest
import typer.testing

import instance_templates
import subroutine.cli.main


def _aim (root: pathlib.Path) -> None:
	"""Point the XDG variables at a home of this test's own."""

	for variable in instance_templates.VARIABLES:
		os.environ[variable] = str(root / variable.lower())


def _invoked (*arguments: str) -> typer.testing.Result:
	"""Run the CLI wherever the environment currently points, and insist it worked.

	**It must not aim the environment itself**, and that is the contract the store relies on:
	:meth:`Store._template` points the variables at a template *around* this call, so a runner
	that re-aimed would build somewhere else and leave the template empty. Met while writing
	this file, which is the reason the sentence is here.
	"""

	result = typer.testing.CliRunner().invoke(subroutine.cli.main.app, list(arguments))

	assert result.exit_code == 0, f"{arguments}\n{result.output}\n{result.exception!r}"

	return result


def _really (root: pathlib.Path, arguments: tuple[str, ...]) -> typer.testing.Result:
	"""Run ``init`` for real into a home nothing has touched."""

	_aim(root)

	return _invoked(*arguments)


def _schema (root: pathlib.Path) -> tuple[str, list[str]]:
	"""Return the migration head, and everything the schema declares, order-insensitively.

	**Two real ``init`` runs do not produce the same DDL text**, which is worth knowing before
	anybody writes a stricter version of this. ``batch_alter_table`` rebuilds a SQLite table by
	reflecting it and re-emitting it, and the reflected constraints come back in an order that
	varies between runs — so ``document`` declares its four foreign keys in a different sequence
	each time, with the same four constraints. Measured by running ``init`` twice into two homes
	and diffing, before this comparison was written.

	So the lines are sorted within each statement. What that gives up is *column order*, which
	nothing asserts and which a copy could not change anyway; what it keeps is every column,
	every constraint and every CHECK — the last being the whole reason `SR#1830` copies a
	migrated database rather than building one with ``create_all``.
	"""

	database = root / "xdg_data_home" / "subroutine" / "subroutine.db"

	assert database.is_file(), f"no database under {root}"

	connection = sqlite3.connect(database)

	try:
		head = connection.execute("select version_num from alembic_version").fetchone()[0]
		statements = [row[0] or "" for row in connection.execute("select sql from sqlite_master")]

	finally:
		connection.close()

	return head, sorted(_settled(statement) for statement in statements)


def _settled (statement: str) -> str:
	"""Put one statement's lines in an order two runs will agree on."""

	return "\n".join(sorted(line.strip().rstrip(",") for line in statement.splitlines()))


def _files (root: pathlib.Path) -> list[str]:
	"""Return every file an instance left behind, relative to its home."""

	return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


@pytest.fixture
def bare (
	tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> typing.Iterator[pathlib.Path]:
	"""Give this test a home of its own, with nothing of the machine's in it."""

	for name in list(os.environ):
		if name.startswith(("SUBROUTINE_TOKEN", "SUBROUTINE_WORKSPACE", "SUBROUTINE_CONNECTION")):
			monkeypatch.delenv(name, raising=False)

	monkeypatch.setenv("SUBROUTINE_DEFAULT_TIMEZONE", "Europe/London")

	for variable in instance_templates.VARIABLES:
		monkeypatch.setenv(variable, str(tmp_path / "unused" / variable.lower()))

	yield tmp_path


@pytest.mark.parametrize(
	"arguments", sorted(instance_templates.CACHEABLE), ids=lambda shape: " ".join(shape)
)
def test_a_planted_instance_is_what_a_real_init_would_have_built (
	bare: pathlib.Path, instances: instance_templates.Store, arguments: tuple[str, ...]
) -> None:
	"""Every shape the store stands in for produces the same tree as running it (`SR#1830`).

	**Output, file set and schema**, which between them are everything a caller of ``init`` can
	observe about it that is not deliberately unique to an instance. Falsified by planting from
	a template built with different arguments: the schema and the files still match — they would
	— and nothing else here would notice, which is why the third assertion is the tree behaving
	rather than merely existing.
	"""

	real = bare / "real"
	planted = bare / "planted"

	expected = _really(real, arguments)

	_aim(planted)
	recorded = instances.planted(arguments, _invoked)

	assert recorded is not None, "the store declined a shape it declares it holds"

	assert recorded.output == expected.output, "a planted instance reports what a built one does"
	assert _files(planted) == _files(real), "and leaves the same files behind"
	assert _schema(planted) == _schema(real), "and the schema the migrations produce"


def test_a_planted_instance_works_like_one_that_was_built (
	bare: pathlib.Path, instances: instance_templates.Store
) -> None:
	"""The tree that arrives takes work, lists it and completes it.

	**The assertion the three above cannot make.** A file set and a schema would still match if
	the database arrived without its seeded vocabulary, and a workspace with no statuses refuses
	the first task filed into it (`SR#301`). This is §13.5b's own four commands, asked of a
	planted instance rather than a built one.
	"""

	planted = bare / "planted"
	_aim(planted)

	recorded = instances.planted(("init",), _invoked)

	assert recorded is not None
	assert recorded.output.strip() == 'Ready. Try: subroutine add "something to do"'

	runner = typer.testing.CliRunner()

	added = runner.invoke(subroutine.cli.main.app, ["add", "Call the dentist"])
	assert added.exit_code == 0, added.output
	assert "Call the dentist" in added.output

	listed = runner.invoke(subroutine.cli.main.app, ["list"])
	assert listed.exit_code == 0, listed.output
	assert "Call the dentist" in listed.output

	finished = runner.invoke(subroutine.cli.main.app, ["done", "1"])
	assert finished.exit_code == 0, finished.output
	assert "Done: Call the dentist" in finished.output


def test_a_home_that_already_holds_an_instance_is_never_planted_over (
	bare: pathlib.Path, instances: instance_templates.Store
) -> None:
	"""A second ``init`` in one test still runs for real, and says what it really says.

	**Not a refusal**, which is what made this worth a test rather than an assumption: a second
	``init`` prints ``Already set up.`` and exits **0**. So a store that replayed the first
	result would answer ``Ready. Try: …`` to a test asking whether the product notices, and the
	test would pass while checking nothing.
	"""

	home = bare / "home"
	_aim(home)

	first = instances.planted(("init",), _invoked)
	assert first is not None, "the first one is planted"

	again = instances.planted(("init",), _invoked)
	assert again is None, "and the second is handed back to the caller"

	real = typer.testing.CliRunner().invoke(subroutine.cli.main.app, ["init"])

	assert real.exit_code == 0
	assert "Already set up" in real.output


def test_a_shape_nothing_declares_is_run_rather_than_stood_in_for (
	bare: pathlib.Path, instances: instance_templates.Store
) -> None:
	"""Falling through is the default, so an unweighed flag cannot be quietly substituted.

	``--verbose`` is the case that matters: it prints the database path, so a planted tree would
	report the **template's** location as the caller's. It is not in :data:`CACHEABLE` and does
	not have to be excluded by name — anything absent from that set is declined.
	"""

	_aim(bare / "home")

	assert ("init", "--verbose") not in instance_templates.CACHEABLE

	assert instances.planted(("init", "--verbose"), _invoked) is None


def test_every_shape_the_store_holds_is_one_the_terminal_tests_ask_for () -> None:
	"""A cached shape nobody uses is a template built for nothing, and a stale claim.

	**The other direction is deliberately not asserted.** A shape the tests use and the store
	does not hold simply runs for real, which is correct and slow rather than wrong — so this
	reads as a bound on what may be substituted, not as a demand that everything be.
	"""

	asked: set[tuple[str, ...]] = set()

	for name in ("test_personal_path.py", "test_cli_connections.py"):
		text = (pathlib.Path(__file__).parent / name).read_text(encoding="utf-8")

		for shape in instance_templates.CACHEABLE:
			spelled = ", ".join(f'"{word}"' for word in shape)

			if f"run({spelled})" in text:
				asked.add(shape)

	assert asked == set(instance_templates.CACHEABLE), (
		"CACHEABLE holds a shape neither terminal test file runs: "
		f"{sorted(set(instance_templates.CACHEABLE) - asked)}"
	)
