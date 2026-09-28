"""The last workspace, the last administrator and the last superuser, against two callers at once.

Item ``#1178``. Each rule counted what would be left and then acted, so two callers acting at
once each counted the other as staying: both went through, leaving the state the rule exists to
prevent. Only a real race shows it, so these run on connections of their own, and they commit -
which makes each responsible for deleting everything it wrote, on every path, as
``test_concurrent_ref_allocation_never_duplicates`` learnt.

**SQLite as well** (`#1178`, reopened). This said SQLite has one writer and so no race to show, and
a writer there holds its lock only from its first write: two callers that counted first both went
through, leaving no workspace at all. A database file of its own, since the suite's is one
connection's. **And the locks themselves, on PostgreSQL** (`#3899`): the first ones deadlocked with a
foreign key's check, and held up filing in every workspace while one was deleted.
"""

import concurrent.futures
import dataclasses
import pathlib
import time
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.session
import subroutine.domain.bootstrap
import subroutine.domain.events
import subroutine.domain.tasks
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
	engine: sqlalchemy.engine.Engine, request: pytest.FixtureRequest, tmp_path: pathlib.Path
) -> typing.Iterator[Committed]:
	"""Yield real connections to a database, and delete what was made through them.

	On SQLite, a database file of this test's own, which goes with its directory; on PostgreSQL,
	the shared one, so what the test made is deleted afterwards.
	"""

	if engine.dialect.name != "postgresql":
		racing = subroutine.db.session.create_engine(f"sqlite:///{tmp_path / 'racing.db'}")

		try:
			subroutine.db.session.create_all(racing)

			yield Committed(sqlalchemy.orm.sessionmaker(bind=racing, expire_on_commit=False))

		finally:
			racing.dispose()

		return

	setup_engine = subroutine.db.session.create_engine(request.getfixturevalue("postgres_url"))
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


def _only_on_postgresql (committed: Committed) -> None:
	"""Skip a test about PostgreSQL's locks where the database is SQLite, which has none of them."""

	if committed.factory.kw["bind"].dialect.name != "postgresql":
		pytest.skip("a lock PostgreSQL takes, which SQLite has no counterpart to")


def test_a_removal_beside_a_promotion_does_not_deadlock (committed: Committed) -> None:
	"""`SR#3899`: the administrators' guard locked the workspace ``FOR UPDATE``, and deadlocked.

	A promotion writes the membership and then records its event, whose foreign key to the
	workspace is checked with a ``KEY SHARE`` lock, which ``FOR UPDATE`` does not let past. So a
	removal of the member being promoted, locking the workspace between the two, waited on the
	membership while the promotion waited on it: PostgreSQL broke the deadlock by failing one, which
	a caller met as a 503. **The guard's lock lets a foreign key's check past now**, so the promotion
	finishes and the removal follows it. The steps are the verification's, in the domain's order.
	"""

	_only_on_postgresql(committed)

	with committed.factory() as setup:
		founder = subroutine.domain.users.create(setup, username=_named("founder"))
		promoted = subroutine.domain.users.create(setup, username=_named("promoted"))
		workspace = subroutine.domain.workspaces.create(
			setup, slug=_named("team"), title="Team", owner=founder
		)
		subroutine.domain.workspaces.add_member(setup, workspace, promoted, role_key="member")
		setup.commit()

	committed.users += [founder.id, promoted.id]
	committed.workspaces.append(workspace.id)

	member = subroutine.db.models.identity.WorkspaceMember
	role = subroutine.db.models.identity.Role
	outcome: dict[str, str] = {}

	with committed.factory() as promoting, committed.factory() as removing:
		administrator = promoting.scalars(
			sqlalchemy.select(role.id).where(role.workspace_id == workspace.id, role.key == "admin")
		).one()
		promoting.execute(
			sqlalchemy.update(member)
			.where(member.workspace_id == workspace.id, member.user_id == promoted.id)
			.values(role_id=administrator)
		)

		losing = removing.scalars(
			sqlalchemy.select(member).where(
				member.workspace_id == workspace.id, member.user_id == promoted.id
			)
		).one()
		subroutine.domain.workspaces._refuse_leaving_nobody_who_can_administer(
			removing, removing.get_one(Workspace, workspace.id), losing
		)

		def promotion_records_its_event () -> None:
			"""Record the promotion's event and commit, as ``set_member_role`` goes on to."""

			try:
				subroutine.domain.events.record(
					promoting,
					workspace_id=workspace.id,
					entity_type="workspace",
					entity_id=workspace.id,
					action="updated",
				)
				promoting.commit()
				outcome["promotion"] = "committed"

			except sqlalchemy.exc.DBAPIError as failed:
				promoting.rollback()
				outcome["promotion"] = type(failed.orig).__name__

		with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
			waiting = pool.submit(promotion_records_its_event)
			time.sleep(0.5)

			try:
				removing.execute(sqlalchemy.delete(member).where(member.id == losing.id))
				removing.commit()
				outcome["removal"] = "committed"

			except sqlalchemy.exc.DBAPIError as failed:
				removing.rollback()
				outcome["removal"] = type(failed.orig).__name__

			waiting.result(timeout=30)

	assert outcome == {"promotion": "committed", "removal": "committed"}, outcome


def test_deleting_a_workspace_does_not_hold_up_filing_in_another (committed: Committed) -> None:
	"""`SR#3899`: the last-workspace guard locked every live workspace, so filing anywhere waited.

	Filing an item writes its workspace's counter, and every workspace was locked until the delete's
	transaction ended. **A lock nothing else takes serialises the deletes instead.** Filing is given
	a lock timeout here, so waiting at all is a failure.
	"""

	_only_on_postgresql(committed)

	with committed.factory() as setup:
		founder = subroutine.domain.users.create(setup, username=_named("founder"))
		leaving = subroutine.domain.workspaces.create(
			setup, slug=_named("leaving"), title="Leaving", owner=founder
		)
		staying = subroutine.domain.workspaces.create(
			setup, slug=_named("staying"), title="Staying", owner=founder
		)
		setup.commit()

	committed.users.append(founder.id)
	committed.workspaces += [leaving.id, staying.id]

	with committed.factory() as deleting:
		subroutine.domain.workspaces.delete(deleting, deleting.get_one(Workspace, leaving.id))
		deleting.flush()

		with committed.factory() as filing:
			filing.execute(sqlalchemy.text("SET lock_timeout = '2s'"))
			inbox = subroutine.domain.bootstrap.inbox_for(filing, filing.get_one(Workspace, staying.id))

			assert inbox is not None, "the workspace has no Inbox to file in"

			try:
				subroutine.domain.tasks.create(
					filing, project=inbox, title="Filed while another workspace was deleted"
				)
				filing.flush()

			except sqlalchemy.exc.OperationalError as failed:
				pytest.fail(f"filing in another workspace waited for the delete: {failed.orig}")

			finally:
				filing.rollback()

		deleting.rollback()
