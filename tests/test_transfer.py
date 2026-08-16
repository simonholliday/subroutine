"""Moving an instance between engines — item ``#155``.

**These are the only tests in the suite that hold both backends open at once.** Everything
else is parameterised over one or the other, which is the right shape for code that must
behave identically on each — and structurally cannot see a defect in code whose whole job is
to carry data *between* them.
"""

import pathlib
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.engine

import conftest
import subroutine.db.base
import subroutine.db.migrate
import subroutine.db.session
import subroutine.db.transfer
import subroutine.errors
import test_migrations


@pytest.fixture
def sqlite_url (tmp_path: pathlib.Path) -> str:
	"""An empty SQLite database on local disk.

	``tmp_path``, never the working tree: this share cannot give SQLite the locking it needs
	and the failure is intermittent rather than obvious.
	"""

	return f"sqlite:///{tmp_path / 'source.db'}"


@pytest.fixture
def postgres_database (postgres_url: str) -> typing.Iterator[str]:
	"""Create and drop a PostgreSQL database of its own, and yield its URL."""

	name = f"subroutine_copy_{uuid.uuid4().hex[:12]}"
	admin = sqlalchemy.engine.make_url(postgres_url).set(database="postgres")
	engine = sqlalchemy.create_engine(admin, isolation_level="AUTOCOMMIT")

	with engine.connect() as connection:
		connection.execute(sqlalchemy.text(f'CREATE DATABASE "{name}"'))

	yield conftest.with_database(postgres_url, name)

	with engine.connect() as connection:
		connection.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{name}"'))

	engine.dispose()


def _filled (url: str) -> str:
	"""Migrate a database and put a row in every table, then return its URL."""

	subroutine.db.migrate.upgrade(url)
	engine = subroutine.db.session.create_engine(url)

	try:
		test_migrations._populate(engine)

	finally:
		engine.dispose()

	return url


def _counts (url: str) -> dict[str, int]:
	"""Return every table's row count."""

	engine = subroutine.db.session.create_engine(url)

	try:
		return subroutine.db.transfer._counts(engine)

	finally:
		engine.dispose()


def test_an_instance_moves_from_sqlite_to_postgresql (
	sqlite_url: str, postgres_database: str
) -> None:
	"""`#155`, and the reason it was a release blocker rather than a gap.

	`docs/hosting.md` said when to switch and how to point `database_url` at PostgreSQL, and
	never how the data got there — so following it exactly produced an empty database. §12.6's
	backups are per-engine, so the move a reader would guess is the one that cannot work.
	"""

	_filled(sqlite_url)

	before = _counts(sqlite_url)
	copied = subroutine.db.transfer.copy_into(sqlite_url, postgres_database)

	assert copied.rows == sum(before.values())
	assert _counts(postgres_database) == before


def test_an_instance_moves_back_from_postgresql_to_sqlite (
	sqlite_url: str, postgres_database: str
) -> None:
	"""The same code, and the direction somebody moving back or making a laptop copy needs.

	Written because a transfer tested one way round is tested against the conversions that
	happened to be exercised — a UUID rendered as hex reads back as a UUID on either side, and
	only going both ways says so.
	"""

	_filled(postgres_database)

	before = _counts(postgres_database)

	subroutine.db.migrate.upgrade(sqlite_url)

	copied = subroutine.db.transfer.copy_into(postgres_database, sqlite_url)

	assert copied.rows == sum(before.values())
	assert _counts(sqlite_url) == before


def test_the_copy_can_be_written_to_afterwards (
	sqlite_url: str, postgres_database: str
) -> None:
	"""**The defect a count check cannot see**, and the one that arrives late.

	Inserting explicit ids does not advance the sequence backing a PostgreSQL column, so
	without a reset `event.seq` is still at 1 while the table holds every event the instance
	ever recorded. Everything looks right — the counts match, the data reads back — and the
	*first write* after the move fails on a duplicate key, minutes after somebody was told
	the copy succeeded and hours before they would think to blame it.
	"""

	_filled(sqlite_url)
	subroutine.db.transfer.copy_into(sqlite_url, postgres_database)

	engine = subroutine.db.session.create_engine(postgres_database)

	try:
		events = subroutine.db.base.Base.metadata.tables["event"]
		workspace = subroutine.db.base.Base.metadata.tables["workspace"]

		with engine.begin() as connection:
			owner = connection.execute(sqlalchemy.select(workspace.c.id)).scalars().first()

			# No `seq`: the point is that the database allocates the next one itself.
			connection.execute(
				sqlalchemy.insert(events).values(
					workspace_id=owner,
					entity_type="task",
					entity_id=uuid.uuid4(),
					action="created",
				)
			)

	finally:
		engine.dispose()


def test_a_target_that_already_holds_data_is_refused (
	sqlite_url: str, postgres_database: str
) -> None:
	"""Merging two instances is not this command.

	Doing it by accident leaves neither of them right, and the refusal names the tables so
	the operator can see whether they pointed at the wrong database or the right one twice.
	"""

	_filled(sqlite_url)
	_filled(postgres_database)

	with pytest.raises(subroutine.errors.SubroutineError) as refused:
		subroutine.db.transfer.copy_into(sqlite_url, postgres_database)

	assert "already holds data" in str(refused.value)


def test_a_target_that_is_refused_is_not_migrated_on_the_way (
	sqlite_url: str, postgres_database: str
) -> None:
	"""Naming somebody else's instance with ``--to`` must not move their schema (`#306`).

	The refusal above is only worth having if it happens *before* anything is written. It did
	not: the target was brought to head first, so pointing at a live instance from an older
	release migrated it through every intervening revision and then reported that nothing had
	happened. There is no downgrade, so the build serving it would not start again.

	Written from the older side deliberately — a target already at head cannot show this,
	which is why the test beside it passed throughout.
	"""

	_filled(sqlite_url)
	_filled(postgres_database)
	subroutine.db.migrate.downgrade(postgres_database, test_migrations._BEFORE_SUBJECTS)

	engine = subroutine.db.session.create_engine(postgres_database)

	try:
		before = subroutine.db.migrate.current_revision(engine)

	finally:
		engine.dispose()

	with pytest.raises(subroutine.errors.SubroutineError) as refused:
		subroutine.db.transfer.copy_into(sqlite_url, postgres_database)

	assert "already holds data" in str(refused.value)

	engine = subroutine.db.session.create_engine(postgres_database)

	try:
		assert subroutine.db.migrate.current_revision(engine) == before

	finally:
		engine.dispose()


def test_copying_a_database_into_itself_is_refused (sqlite_url: str) -> None:
	"""It would double every table it did not fail on, which is worse than either outcome."""

	_filled(sqlite_url)

	with pytest.raises(subroutine.errors.SubroutineError) as refused:
		subroutine.db.transfer.copy_into(sqlite_url, sqlite_url)

	assert "same database" in str(refused.value)


def test_a_source_behind_this_build_is_refused (
	sqlite_url: str, postgres_database: str
) -> None:
	"""Copying an un-upgraded database would carry an old schema into a new home.

	The target is migrated to *head*, so the two would disagree about their own shape — and
	the operator would find out on the first query rather than here.
	"""

	_filled(sqlite_url)
	subroutine.db.migrate.downgrade(sqlite_url, test_migrations._BEFORE_SUBJECTS)

	with pytest.raises(subroutine.errors.SubroutineError) as refused:
		subroutine.db.transfer.copy_into(sqlite_url, postgres_database)

	assert "schema" in str(refused.value)


def test_the_target_is_left_migrated_rather_than_merely_built (
	sqlite_url: str, postgres_database: str
) -> None:
	"""A database this leaves behind has to be one ``subroutine db upgrade`` will accept later.

	``create_all`` would produce the right tables and no ``alembic_version`` row, so the next
	release's migration would try to build what is already there.
	"""

	_filled(sqlite_url)
	subroutine.db.transfer.copy_into(sqlite_url, postgres_database)

	engine = subroutine.db.session.create_engine(postgres_database)

	try:
		assert subroutine.db.migrate.is_up_to_date(engine)

	finally:
		engine.dispose()


def test_every_table_is_carried_rather_than_a_listed_few (
	sqlite_url: str, postgres_database: str
) -> None:
	"""The guard against this file rotting: the tables come from the models, not from here.

	A transfer that copied a hand-written list would be correct on the day it was written and
	silently lossy the first time somebody added a table — which is the failure mode nothing
	else in this suite would catch, because every other test builds both sides the same way.
	"""

	_filled(sqlite_url)

	before = _counts(sqlite_url)
	copied = subroutine.db.transfer.copy_into(sqlite_url, postgres_database)

	# Every table the models declare is *considered*, which is the anti-rot half.
	assert set(copied.counts) == set(subroutine.db.base.Base.metadata.tables)

	# And every table that actually had rows arrived with all of them. `instance` is empty
	# here because `_populate` seeds what *references* a rebuilt table and nothing references
	# that one — so asserting "all non-empty" would be asserting a property of the fixture.
	assert copied.counts == before
	assert sum(1 for count in before.values() if count) >= 15, (
		"the fixture stopped filling most tables, so this test is no longer measuring much"
	)


def test_a_child_stored_before_its_parent_still_copies (
	sqlite_url: str, postgres_database: str
) -> None:
	"""``sorted_tables`` orders tables and says nothing about the rows inside one.

	Five tables here point at themselves — a sub-task, a section of a document, a project
	inside a project, the person an agent answers to, a reply to a comment — and the copy
	inserted their rows in whatever order the source returned them. A child before its parent
	is a foreign key violation, so ``add``, ``add``, ``move --under`` was enough to make the
	migration this command exists for permanently impossible.

	**Read order is not creation order**, which is what makes this reachable rather than
	theoretical, and PostgreSQL is where it is easiest to show: an updated row is written to
	the end of the heap, so a parent edited after its child comes back second. The direction is
	PostgreSQL to SQLite for that reason — the case is about the *source*, and this is the
	source that can be put into the state.
	"""

	_filled(postgres_database)

	engine = subroutine.db.session.create_engine(postgres_database)
	tasks = subroutine.db.base.Base.metadata.tables["task"]

	try:
		with engine.begin() as connection:
			parent = connection.execute(
				sqlalchemy.select(tasks).where(tasks.c.parent_task_id.is_(None))
			).mappings().one()

			# Rewriting the row moves it to the end of the heap, which is what a `move --under`
			# or any other edit does to a parent in the ordinary course of using this.
			connection.execute(
				sqlalchemy.update(tasks)
				.where(tasks.c.id == parent["id"])
				.values(title=parent["title"] + " (edited)")
			)

		with engine.connect() as connection:
			read = connection.execute(sqlalchemy.select(tasks.c.id)).scalars().all()

		assert read[0] != parent["id"], "the parent is still read first, so this proves nothing"

	finally:
		engine.dispose()

	before = _counts(postgres_database)
	copied = subroutine.db.transfer.copy_into(postgres_database, sqlite_url)

	assert copied.rows == sum(before.values())
	assert _counts(sqlite_url) == before

	# And the tree survived: a copy that dropped the reference to satisfy the constraint would
	# pass every count above.
	target = subroutine.db.session.create_engine(sqlite_url)

	try:
		with target.connect() as connection:
			joined = connection.execute(
				sqlalchemy.select(sqlalchemy.func.count())
				.select_from(tasks)
				.where(tasks.c.parent_task_id.is_not(None))
			).scalar_one()

		assert joined == 1, "the child came across with no parent"

	finally:
		target.dispose()


def test_a_database_holding_somebody_elses_tables_is_refused (
	sqlite_url: str, postgres_database: str
) -> None:
	"""The empty check walked *this* schema's tables, so anything else read as empty.

	`#306` moved that check before the migration precisely so a target is established as unused
	before anything touches it — and a database belonging to another application answered "no
	rows in any of Subroutine's tables", which is true and is not the question. The migration
	that followed would then have created an instance's tables alongside somebody's data.
	"""

	_filled(sqlite_url)

	engine = subroutine.db.session.create_engine(postgres_database)

	try:
		with engine.begin() as connection:
			connection.execute(sqlalchemy.text("CREATE TABLE invoices (id integer)"))

	finally:
		engine.dispose()

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.db.transfer.copy_into(sqlite_url, postgres_database)

	assert "invoices" in str(refused.value)
