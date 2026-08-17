"""Two writers, one SQLite file — the configuration most installations will actually be in.

Deliberately SQLite-only, and the one place in this suite where that is right. The dual
backend rule exists because SQLite cannot express the failures PostgreSQL can; this file is
about the failure only SQLite has. PostgreSQL takes concurrent writers without comment.

The case is `subroutine serve` running for an agent while its owner types `subroutine done
3` in another terminal: two processes, two connection pools, one file (docs/design.md §10.4).
WAL and `busy_timeout` are what make it work and both are applied per connection — but
"it happens to work" and "we promise it works" are different claims, and only the second
one gets a test.
"""

import pathlib
import threading
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.session

#: Writers, and rows each. Small enough to stay fast, large enough that the two threads
#: genuinely interleave rather than finishing one after the other.
WRITERS = 2
ROWS_EACH = 25


@pytest.fixture
def database (tmp_path: pathlib.Path) -> typing.Iterator[str]:
	"""Yield a URL for a SQLite file on local disk, with the schema created.

	``tmp_path`` rather than anywhere in the working tree, which is on a network share
	where SQLite cannot take a lock at all.
	"""

	url = f"sqlite:///{tmp_path / 'concurrent.db'}"
	engine = subroutine.db.session.create_engine(url)

	try:
		subroutine.db.session.create_all(engine)

	finally:
		engine.dispose()

	yield url


def test_wal_is_actually_applied (database: str) -> None:
	"""A silent downgrade to rollback-journal is the other way this presents.

	Asserted separately from the concurrency test below, because if WAL is off that test
	would fail with a lock error and the cause would look like contention rather than
	configuration.
	"""

	engine = subroutine.db.session.create_engine(database)

	try:
		with engine.connect() as connection:
			mode = connection.execute(sqlalchemy.text("PRAGMA journal_mode")).scalar_one()
			timeout = connection.execute(sqlalchemy.text("PRAGMA busy_timeout")).scalar_one()

	finally:
		engine.dispose()

	assert str(mode).lower() == "wal"
	assert int(timeout) >= 5000


def test_two_writers_on_one_file_both_succeed (database: str) -> None:
	"""Neither writer sees ``database is locked``, and every row survives.

	Each thread gets its own engine, because that is what a separate process would have.
	A barrier holds them until both are ready, so the writes overlap rather than politely
	queueing. Verified to be a real guard rather than a vacuous one: with
	``busy_timeout = 0`` this fails with ``database is locked`` on both journal modes, so
	it is the test that notices if that pragma is ever dropped.
	"""

	barrier = threading.Barrier(WRITERS)
	failures: list[BaseException] = []
	lock = threading.Lock()

	def write (label: str) -> None:
		"""Insert a run of workspaces, committing each one separately."""

		engine = subroutine.db.session.create_engine(database)

		try:
			barrier.wait(timeout=30)

			with sqlalchemy.orm.Session(engine) as session:
				for index in range(ROWS_EACH):
					session.add(
						subroutine.db.models.identity.Workspace(
							slug=f"{label}-{index}-{uuid.uuid4().hex[:8]}",
							title=f"Workspace {label} {index}",
						)
					)

					# One transaction per row, on purpose: it maximises the number of times
					# the two writers contend for the write lock.
					session.commit()

		except BaseException as error:
			with lock:
				failures.append(error)

		finally:
			engine.dispose()

	threads = [
		threading.Thread(target=write, args=(f"writer{number}",), name=f"writer{number}")
		for number in range(WRITERS)
	]

	for thread in threads:
		thread.start()

	for thread in threads:
		thread.join(timeout=60)

		assert not thread.is_alive(), "a writer never finished — it is probably still blocked"

	assert failures == [], f"a concurrent writer failed: {failures[0]!r}"

	engine = subroutine.db.session.create_engine(database)

	try:
		with engine.connect() as connection:
			written = connection.execute(
				sqlalchemy.select(
					sqlalchemy.func.count()
				).select_from(subroutine.db.models.identity.Workspace)
			).scalar_one()

	finally:
		engine.dispose()

	assert written == WRITERS * ROWS_EACH


def test_a_reader_never_sees_an_uncommitted_write (database: str) -> None:
	"""A second connection sees the last commit, and nothing that is still in flight.

	This is what stops the CLI reporting a task the server is halfway through writing.
	Measured rather than assumed, and the measurement corrected the claim this test was
	first written with: it passes under rollback-journal too, because an uncommitted
	``INSERT`` holds only a RESERVED lock and readers are excluded no earlier than the
	commit itself. It is therefore *not* evidence that WAL is on —
	:func:`test_wal_is_actually_applied` is, and the writer test above is what fails if
	``busy_timeout`` is ever dropped.
	"""

	writer = subroutine.db.session.create_engine(database)
	reader = subroutine.db.session.create_engine(database)

	try:
		with sqlalchemy.orm.Session(writer) as session:
			session.add(
				subroutine.db.models.identity.Workspace(slug="committed", title="Committed")
			)
			session.commit()

			# Open and hold a write transaction without committing it.
			session.add(
				subroutine.db.models.identity.Workspace(slug="in-flight", title="In flight")
			)
			session.flush()

			with reader.connect() as connection:
				visible = connection.execute(
					sqlalchemy.select(subroutine.db.models.identity.Workspace.slug)
				).scalars().all()

			assert list(visible) == ["committed"]

			session.rollback()

	finally:
		writer.dispose()
		reader.dispose()
