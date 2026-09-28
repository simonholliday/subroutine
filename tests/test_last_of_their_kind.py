"""The last workspace, the last administrator and the last superuser, against two callers at once.

Item ``#1178``. Each rule counted what would be left and then acted, so two callers acting at
once each counted the other as staying: both went through, leaving the state the rule exists to
prevent. Only a real race shows it, so these run on PostgreSQL, on connections of their own, and
they commit - which makes each responsible for deleting everything it wrote, on every path, as
``test_concurrent_ref_allocation_never_duplicates`` learnt. SQLite has one writer, so it has no
race to show.
"""

import concurrent.futures
import dataclasses
import time
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.session
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors

Factory = sqlalchemy.orm.sessionmaker[sqlalchemy.orm.Session]
Step = typing.Callable[[sqlalchemy.orm.Session], object]

Workspace = subroutine.db.models.identity.Workspace
User = subroutine.db.models.identity.User


@dataclasses.dataclass
class Committed:
	"""Sessions on connections of their own, and what the test made, to delete afterwards."""

	factory: Factory
	workspaces: list[uuid.UUID] = dataclasses.field(default_factory=list)
	users: list[uuid.UUID] = dataclasses.field(default_factory=list)


@pytest.fixture
def committed (
	engine: sqlalchemy.engine.Engine, postgres_url: str
) -> typing.Iterator[Committed]:
	"""Yield real connections to the shared database, and delete what was made through them."""

	if engine.dialect.name != "postgresql":
		pytest.skip("SQLite serialises writers, so there is no race to show")

	setup_engine = subroutine.db.session.create_engine(postgres_url)
	made = Committed(sqlalchemy.orm.sessionmaker(bind=setup_engine, expire_on_commit=False))

	try:
		yield made

	finally:
		try:
			with made.factory() as cleanup:
				for workspace_id in made.workspaces:
					cleanup.execute(sqlalchemy.delete(Workspace).where(Workspace.id == workspace_id))

				for user_id in made.users:
					cleanup.execute(sqlalchemy.delete(User).where(User.id == user_id))

				cleanup.commit()

		finally:
			setup_engine.dispose()


def _two_at_once (factory: Factory, first: Step, second: Step) -> None:
	"""Take ``first`` up to its commit, start ``second`` beside it, then commit ``first``.

	``second`` runs on its own connection, and whatever it raised is raised here, so a test says
	with ``pytest.raises`` how it has to end. **Half a second is long enough for ``second`` to
	reach the lock ``first`` holds, or, where nothing locks, to finish** - which is the defect.
	"""

	with factory() as one:
		first(one)
		one.flush()

		def run_second () -> None:
			"""Do ``second`` in a transaction of its own, and commit it."""

			with factory() as two:
				second(two)
				two.commit()

		with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
			waiting = pool.submit(run_second)
			time.sleep(0.5)
			one.commit()
			waiting.result(timeout=30)


def _named (prefix: str) -> str:
	"""Return a name nothing else in the shared database holds."""

	return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _live_workspaces (factory: Factory) -> int:
	"""Count the workspaces not in the trash, across the whole database."""

	with factory() as reading:
		return len(
			reading.scalars(sqlalchemy.select(Workspace.id).where(Workspace.deleted_at.is_(None))).all()
		)


def test_two_callers_cannot_delete_the_last_two_workspaces (committed: Committed) -> None:
	"""Deleted one after the other, the second is refused; at once, it has to be refused too."""

	with committed.factory() as setup:
		founder = subroutine.domain.users.create(setup, username=_named("founder"))
		first = subroutine.domain.workspaces.create(
			setup, slug=_named("first"), title="First", owner=founder
		)
		second = subroutine.domain.workspaces.create(
			setup, slug=_named("second"), title="Second", owner=founder
		)
		setup.commit()

	committed.users.append(founder.id)
	committed.workspaces += [first.id, second.id]

	assert _live_workspaces(committed.factory) == 2, (
		"another workspace is live in this database, so neither of these is the last one"
	)

	def deleting (workspace_id: uuid.UUID) -> Step:
		"""Return a step that moves one workspace to the trash."""

		return lambda session: subroutine.domain.workspaces.delete(
			session, session.get_one(Workspace, workspace_id)
		)

	with pytest.raises(subroutine.errors.ValidationError, match="only workspace here"):
		_two_at_once(committed.factory, deleting(first.id), deleting(second.id))

	assert _live_workspaces(committed.factory) == 1


def test_two_callers_cannot_remove_a_workspace_s_last_two_administrators (
	committed: Committed,
) -> None:
	"""The owner and one administrator, each removed by a different caller at the same moment."""

	with committed.factory() as setup:
		founder = subroutine.domain.users.create(setup, username=_named("founder"))
		other = subroutine.domain.users.create(setup, username=_named("other"))
		workspace = subroutine.domain.workspaces.create(
			setup, slug=_named("team"), title="Team", owner=founder
		)
		subroutine.domain.workspaces.add_member(setup, workspace, other, role_key="admin")
		setup.commit()

	committed.users += [founder.id, other.id]
	committed.workspaces.append(workspace.id)

	def removing (user_id: uuid.UUID) -> Step:
		"""Return a step that takes one person out of the workspace."""

		return lambda session: subroutine.domain.workspaces.remove_member(
			session, session.get_one(Workspace, workspace.id), session.get_one(User, user_id)
		)

	with pytest.raises(subroutine.errors.ValidationError, match="nobody who can administer"):
		_two_at_once(committed.factory, removing(founder.id), removing(other.id))


def test_two_callers_cannot_deactivate_the_last_two_superusers (committed: Committed) -> None:
	"""The instance's own tier of the same rule, which `#1178` said would otherwise stay half-fixed."""

	with committed.factory() as setup:
		one = subroutine.domain.users.create(setup, username=_named("root"), is_superuser=True)
		two = subroutine.domain.users.create(setup, username=_named("root"), is_superuser=True)
		setup.commit()

	committed.users += [one.id, two.id]

	with committed.factory() as reading:
		active = reading.scalars(
			sqlalchemy.select(User.id).where(
				User.is_superuser.is_(True), User.is_active.is_(True), User.deleted_at.is_(None)
			)
		).all()

	assert len(active) == 2, "another superuser is active in this database, so neither is the last"

	def leaving (user_id: uuid.UUID) -> Step:
		"""Return a step that marks one superuser as having left."""

		return lambda session: subroutine.domain.users.set_active(
			session, session.get_one(User, user_id), active=False
		)

	with pytest.raises(subroutine.errors.ValidationError, match="only person who can administer"):
		_two_at_once(committed.factory, leaving(one.id), leaving(two.id))
