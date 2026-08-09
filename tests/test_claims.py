"""Taking a task and giving it back — SPEC.md §14.11, items ``#350`` and ``#354``.

**Its own module because `#350` shipped without one.** Claims were exercised only through
`tests/test_transport_equivalence.py`, which asks whether the two transports agree — a real
question, and a different one from whether the rule underneath them is right. The concurrency
test below is the reason that gap mattered: it fails against the code as shipped.

Every test that has a clock puts the expiry in the past rather than waiting, which is the only
way to test a lease without one.
"""

import concurrent.futures
import datetime
import threading
import uuid

import pydantic
import pytest
import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.orm

import subroutine.config
import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.db.session
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.claims
import subroutine.domain.events
import subroutine.domain.projects
import subroutine.domain.readiness
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.permissions
import subroutine.views


def _person (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace | None = None,
	*,
	role_key: str = "contributor",
) -> subroutine.domain.authentication.Principal:
	"""Create an account, optionally a member of one workspace, and return it as a principal."""

	user = subroutine.domain.users.create(
		session, username=f"worker-{uuid.uuid4().hex[:8]}"
	)

	if workspace is not None:
		subroutine.domain.workspaces.add_member(
			session, workspace, user, role_key=role_key
		)

	return subroutine.domain.authentication.Principal(user=user)


def _scoped (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	*scopes: str,
) -> subroutine.domain.authentication.Principal:
	"""Return the same principal presenting a token narrowed to those permissions."""

	token, _issued = subroutine.domain.authentication.issue_token(
		session, user=principal.user, title="Narrow", scopes=list(scopes)
	)

	return subroutine.domain.authentication.Principal(
		user=principal.user, token=token
	)


def _place (
	session: sqlalchemy.orm.Session,
) -> tuple[
	subroutine.db.models.identity.Workspace,
	subroutine.db.models.project.Project,
	subroutine.domain.authentication.Principal,
]:
	"""Create a workspace with a project and an owner, and return all three."""

	owner = subroutine.domain.users.create(
		session, username=f"owner-{uuid.uuid4().hex[:8]}"
	)
	workspace = subroutine.domain.workspaces.create(
		session, slug=f"ws-{uuid.uuid4().hex[:8]}", title="Claims", owner=owner
	)
	project = subroutine.domain.projects.create(
		session, workspace_id=workspace.id, key="work", title="Work"
	)

	return workspace, project, subroutine.domain.authentication.Principal(user=owner)


def _task (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	title: str = "Something to take",
) -> subroutine.db.models.work.Task:
	"""Create a task to be claimed."""

	return subroutine.domain.tasks.create(session, project=project, title=title)


def _events (
	session: sqlalchemy.orm.Session, task: subroutine.db.models.work.Task
) -> list[subroutine.db.models.activity.Event]:
	"""Return one task's events, oldest first."""

	model = subroutine.db.models.activity.Event

	return list(
		session.scalars(
			sqlalchemy.select(model)
			.where(model.entity_id == task.id)
			.order_by(model.seq)
		)
	)


def test_a_lease_runs_out_and_stops_counting (session: sqlalchemy.orm.Session) -> None:
	"""The whole of what makes it a lease: nothing has to run for the work to come back."""

	_workspace, project, owner = _place(session)
	task = _task(session, project)

	subroutine.domain.claims.claim(session, task, actor=owner)

	assert subroutine.domain.claims.held_by(task) == owner.user.id

	task.claim_expires_at = subroutine.db.types.utcnow() - datetime.timedelta(minutes=1)
	session.flush()

	assert subroutine.domain.claims.held_by(task) is None
	assert task.claimed_by_id is not None, "who was working on it is still worth knowing"


def test_the_predicate_and_the_row_reader_agree_about_a_half_set_claim (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`#362`. A holder with no expiry is unreachable through any endpoint, and both readings
	of the row have to agree about it anyway.

	``NOT (a AND b)`` is *null* rather than true when ``b`` is null, so before this the row
	vanished from every listing while ``held_by`` said nobody held it — the two halves of one
	rule, disagreeing about a state neither of them can produce.
	"""

	_workspace, project, owner = _place(session)
	task = _task(session, project)
	moment = subroutine.db.types.utcnow()

	task.claimed_by_id = owner.user.id
	task.claimed_at = moment
	task.claim_expires_at = None
	session.flush()

	assert subroutine.domain.claims.held_by(task, now=moment) is None

	model = subroutine.db.models.work.Task
	free = list(
		session.scalars(
			sqlalchemy.select(model).where(
				model.id == task.id,
				subroutine.domain.readiness.unclaimed(model, now=moment, by=None),
			)
		)
	)

	assert free == [task], "the predicate has to read it the way held_by does"


def test_claiming_and_renewing_write_the_events_that_are_the_only_record (
	session: sqlalchemy.orm.Session,
) -> None:
	"""An expired lease leaves nothing in the row, so the history is where it happened.

	That is the module's own argument for recording these at all, and until now nothing checked
	that it does — a control that is specified, documented and inert is this codebase's second
	signature defect.
	"""

	_workspace, project, owner = _place(session)
	task = _task(session, project)

	subroutine.domain.claims.claim(session, task, actor=owner)
	first = task.claimed_at

	subroutine.domain.claims.claim(session, task, actor=owner)

	assert task.claimed_at == first, "renewing keeps the instant it was first taken"

	subroutine.domain.claims.release(session, task, actor=owner)

	written = [
		(event.action, event.changes)
		for event in _events(session, task)
		if event.action
		in {
			subroutine.domain.events.EventAction.CLAIMED,
			subroutine.domain.events.EventAction.RELEASED,
		}
	]

	assert [action for action, _changes in written] == ["claimed", "claimed", "released"]
	assert written[0][1] is not None and written[0][1]["renewed"] is False
	assert written[1][1] is not None and written[1][1]["renewed"] is True


def test_releasing_what_nobody_holds_records_nothing (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A worker tidying up after itself should not have to check first — and an event saying a
	lease that had already run out was released would be noise in the one place the record is
	meant to be read."""

	_workspace, project, owner = _place(session)
	task = _task(session, project)

	subroutine.domain.claims.release(session, task, actor=owner)

	assert not [
		event
		for event in _events(session, task)
		if event.action == subroutine.domain.events.EventAction.RELEASED
	]


def test_anybody_who_may_change_it_may_release_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The case this exists for is a worker that died holding a lease.

	Requiring its own credential would leave the remedy with the one principal that cannot act,
	so this is deliberately not restricted to the holder. Two principals, because a test where
	the holder releases its own claim passes whether or not the rule is there.
	"""

	workspace, project, owner = _place(session)
	task = _task(session, project)
	somebody_else = _person(session, workspace)

	subroutine.domain.claims.claim(session, task, actor=owner)
	subroutine.domain.claims.release(session, task, actor=somebody_else)

	assert subroutine.domain.claims.held_by(task) is None
	assert task.claimed_by_id is None
	assert task.claimed_at is None


def test_a_reader_cannot_park_work_it_could_not_then_do (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Claiming needs ``task:write``, because it reserves the right to do the work."""

	workspace, project, _owner = _place(session)
	task = _task(session, project)
	reader = _scoped(
		session, _person(session, workspace), subroutine.permissions.TASK_READ
	)

	with pytest.raises(subroutine.errors.SubroutineError):
		subroutine.domain.claims.claim(session, task, actor=reader)

	assert task.claimed_by_id is None


def test_a_conflict_is_reported_only_to_somebody_who_may_touch_the_task (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Whether somebody else is working on this is a fact about the workspace.

	A caller refused the task is refused before it learns who holds it, which is why the
	permission check runs first. Asserted on the message rather than on the order of two lines,
	since the order is the implementation and this is the promise.
	"""

	workspace, project, owner = _place(session)
	task = _task(session, project)
	reader = _scoped(
		session, _person(session, workspace), subroutine.permissions.TASK_READ
	)

	subroutine.domain.claims.claim(session, task, actor=owner)

	with pytest.raises(subroutine.errors.SubroutineError) as refused:
		subroutine.domain.claims.claim(session, task, actor=reader)

	assert owner.user.username not in str(refused.value)
	assert owner.user.username not in str(refused.value.hint)


def test_the_refusal_names_who_holds_it_and_until_when (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Both facts, because either alone leaves the other worker stuck."""

	workspace, project, owner = _place(session)
	task = _task(session, project)
	somebody_else = _person(session, workspace)

	subroutine.domain.claims.claim(session, task, minutes=45, actor=owner)

	with pytest.raises(subroutine.errors.Conflict) as refused:
		subroutine.domain.claims.claim(session, task, actor=somebody_else)

	hint = str(refused.value.hint)

	assert owner.user.username in hint
	assert "until" in hint


def test_a_holder_who_has_left_is_still_named (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Deactivating an account does not take its name off the work it is holding.

	**Written to reach `_who_holds_it`'s "somebody since deleted" wording, which turned out to
	be unreachable.** ``claimed_by_id`` carries ``ondelete="SET NULL"``, so a holder cannot
	outlive its row: hard-deleting the account clears the column, and every other kind of
	leaving is a soft delete that leaves the name readable. Setting the column to an id nobody
	holds is refused by the foreign key. The branch stays because ``session.get`` is typed
	``User | None`` and the type checker is owed an answer; the sentence in it describes a state
	the schema forbids, and that is now written down beside it rather than implied by a test
	that could never fail.
	"""

	workspace, project, owner = _place(session)
	task = _task(session, project)
	somebody_else = _person(session, workspace)

	subroutine.domain.claims.claim(session, task, actor=owner)

	owner.user.is_active = False
	owner.user.deleted_at = subroutine.db.types.utcnow()
	session.flush()

	with pytest.raises(subroutine.errors.Conflict) as refused:
		subroutine.domain.claims.claim(session, task, actor=somebody_else)

	assert owner.user.username in str(refused.value.hint)


@pytest.mark.parametrize("asked", [0, -1, subroutine.domain.claims.MAX_LEASE_MINUTES + 1])
def test_a_lease_nobody_could_mean_is_refused_by_name (
	session: sqlalchemy.orm.Session, asked: int
) -> None:
	"""Anything a client can send is checked where the message can name the field and the
	range — a lease of zero minutes is expired before it is written, and an unbounded one is a
	lock wearing a lease's clothes.

	**The last assertion is the one that found something.** Against the shipped code this test
	failed, because the refusal was raised half way through the write: ``claimed_by_id`` was
	assigned, then ``_lease`` raised, and the loaded row was left holding a holder with no
	expiry — the exact half-set state `#362` describes, reached by a caller passing a number
	nobody would accept. Only in memory, and rolled back by both transports, so it was never
	persisted; it was reachable all the same, and it is not now, because the whole write is one
	statement whose arguments are evaluated before any of it happens.
	"""

	_workspace, project, owner = _place(session)
	task = _task(session, project)

	with pytest.raises(subroutine.errors.ValidationError) as refused:
		subroutine.domain.claims.claim(session, task, minutes=asked, actor=owner)

	assert refused.value.errors is not None
	assert [error.field for error in refused.value.errors] == ["minutes"]
	assert str(subroutine.domain.claims.MAX_LEASE_MINUTES) in str(
		refused.value.errors[0].hint
	)
	assert task.claimed_by_id is None, "a refused lease leaves nothing half written"


def test_the_instances_configured_lease_is_what_a_claim_lasts (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`claim_lease_minutes` was declared, printed by `config show` and read by nothing until
	claims existed. Asserted so that it cannot go back to being inert."""

	_workspace, project, owner = _place(session)
	task = _task(session, project)
	moment = subroutine.db.types.utcnow()

	subroutine.domain.claims.claim(
		session,
		task,
		settings=subroutine.config.Settings(claim_lease_minutes=90),
		now=moment,
		actor=owner,
	)

	assert task.claim_expires_at == moment + datetime.timedelta(minutes=90)


@pytest.mark.parametrize(
	"configured", [0, -5, subroutine.domain.claims.MAX_LEASE_MINUTES + 1]
)
def test_a_configured_lease_is_held_to_the_bound_the_argument_is (configured: int) -> None:
	"""`#358`. The path a caller controls was bounded; the path the operator controls was not.

	**Zero is the one worth naming.** It put every expiry on the instant of the claim, so
	`held_by` reported that nobody held it — claiming succeeded, printed a confirmation, and
	did nothing, silently, for every worker on the instance at once. The other direction is a
	ten-week lease, which is the lock the bound exists to refuse.

	Refused where the settings are loaded rather than where a claim is taken, so it is one
	message about the configuration at startup instead of a refused claim every time somebody
	works.
	"""

	with pytest.raises(pydantic.ValidationError) as refused:
		subroutine.config.Settings(claim_lease_minutes=configured)

	assert "claim_lease_minutes" in str(refused.value)


def test_two_workers_cannot_both_take_the_same_task (
	engine: sqlalchemy.engine.Engine, postgres_url: str
) -> None:
	"""`#354`. The test the feature was named after, and it failed against the code that
	shipped: 2 of 2 workers succeeded and the row held the first.

	**Real connections rather than the shared-transaction fixture**, for the reason the ref
	allocation test uses them: the whole question is what two transactions do to one row at
	once, and that fixture exists to stop tests seeing each other's transactions. A test of
	this written inside one transaction cannot fail, which is why `#350` had one and this is
	somewhere else.

	Both workers start together on a barrier, so the reads genuinely overlap.
	"""

	if engine.dialect.name != "postgresql":
		pytest.skip("SQLite serialises writers, so there is no contention to test")

	setup_engine = subroutine.db.session.create_engine(postgres_url)
	factory = sqlalchemy.orm.sessionmaker(bind=setup_engine, expire_on_commit=False)
	workspace_id: uuid.UUID | None = None
	accounts: list[uuid.UUID] = []

	try:
		with factory() as setup:
			workspace, project, owner = _place(setup)
			task = _task(setup, project, "Two agents would both pick this up")
			workers = [_person(setup, workspace) for _ in range(2)]

			setup.commit()
			workspace_id = workspace.id
			task_id = task.id
			accounts = [owner.user.id, *[worker.user.id for worker in workers]]
			contenders = [worker.user.id for worker in workers]

		gate = threading.Barrier(len(contenders))

		def take (user_id: uuid.UUID) -> uuid.UUID | None:
			"""Claim the task from an independent connection, returning who succeeded."""

			with factory() as worker:
				person = worker.get(subroutine.db.models.identity.User, user_id)
				row = worker.get(subroutine.db.models.work.Task, task_id)

				assert person is not None and row is not None

				actor = subroutine.domain.authentication.Principal(user=person)

				# Both threads read the row, then wait, then write. Without the barrier one
				# would ordinarily finish before the other started, and this test would pass
				# against the defect it was written for.
				gate.wait(timeout=30)

				try:
					subroutine.domain.claims.claim(worker, row, actor=actor)
					worker.commit()

					return user_id

				except subroutine.errors.Conflict:
					worker.rollback()

					return None

		with concurrent.futures.ThreadPoolExecutor(max_workers=len(contenders)) as pool:
			results = [
				future.result()
				for future in [pool.submit(take, who) for who in contenders]
			]

		winners = [result for result in results if result is not None]

		assert len(winners) == 1, "two workers both believe they took the same task"

		with factory() as check:
			row = check.get(subroutine.db.models.work.Task, task_id)

			assert row is not None
			assert row.claimed_by_id == winners[0], (
				"and the one who was told they took it is the one the row says has it"
			)

	finally:
		# In the `finally`, and everything this test created: it commits to the shared
		# PostgreSQL database, so anything left behind is there for the rest of the run.
		with factory() as cleanup:
			if workspace_id is not None:
				cleanup.execute(
					sqlalchemy.delete(subroutine.db.models.identity.Workspace).where(
						subroutine.db.models.identity.Workspace.id == workspace_id
					)
				)

			for account in accounts:
				cleanup.execute(
					sqlalchemy.delete(subroutine.db.models.identity.User).where(
						subroutine.db.models.identity.User.id == account
					)
				)

			cleanup.commit()

		setup_engine.dispose()


def test_renewing_under_contention_does_not_hand_the_task_over (
	engine: sqlalchemy.engine.Engine, postgres_url: str
) -> None:
	"""The other side of `#354`, and the one a conditional update could plausibly get wrong.

	A holder renewing its own live lease must succeed while somebody else's attempt is refused
	— so the ``WHERE`` clause has to let the holder through and nobody else, at the same
	instant.

	**This one passes against the shipped code too, and says so rather than implying
	otherwise.** Both principals read an already-committed live claim, so there is no window to
	lose: it is a regression test for the predicate that replaced the branch, not a second
	falsification of the defect. The test above is the one that fails against what shipped.
	"""

	if engine.dialect.name != "postgresql":
		pytest.skip("SQLite serialises writers, so there is no contention to test")

	setup_engine = subroutine.db.session.create_engine(postgres_url)
	factory = sqlalchemy.orm.sessionmaker(bind=setup_engine, expire_on_commit=False)
	workspace_id: uuid.UUID | None = None
	accounts: list[uuid.UUID] = []

	try:
		with factory() as setup:
			workspace, project, owner = _place(setup)
			task = _task(setup, project, "Held, and wanted")
			holder = _person(setup, workspace)
			rival = _person(setup, workspace)

			subroutine.domain.claims.claim(setup, task, actor=holder)
			setup.commit()

			workspace_id = workspace.id
			task_id = task.id
			accounts = [owner.user.id, holder.user.id, rival.user.id]
			holder_id = holder.user.id
			rival_id = rival.user.id

		gate = threading.Barrier(2)
		outcomes: dict[uuid.UUID, bool] = {}
		guard = threading.Lock()

		def attempt (user_id: uuid.UUID) -> None:
			"""Try to take it, recording whether this principal was allowed to."""

			with factory() as worker:
				person = worker.get(subroutine.db.models.identity.User, user_id)
				row = worker.get(subroutine.db.models.work.Task, task_id)

				assert person is not None and row is not None

				actor = subroutine.domain.authentication.Principal(user=person)

				gate.wait(timeout=30)

				try:
					subroutine.domain.claims.claim(worker, row, actor=actor)
					worker.commit()
					succeeded = True

				except subroutine.errors.Conflict:
					worker.rollback()
					succeeded = False

				with guard:
					outcomes[user_id] = succeeded

		with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
			for future in [
				pool.submit(attempt, who) for who in (holder_id, rival_id)
			]:
				future.result()

		assert outcomes[holder_id] is True, "a holder must be able to renew"
		assert outcomes[rival_id] is False, "and nobody else may take it from under them"

	finally:
		with factory() as cleanup:
			if workspace_id is not None:
				cleanup.execute(
					sqlalchemy.delete(subroutine.db.models.identity.Workspace).where(
						subroutine.db.models.identity.Workspace.id == workspace_id
					)
				)

			for account in accounts:
				cleanup.execute(
					sqlalchemy.delete(subroutine.db.models.identity.User).where(
						subroutine.db.models.identity.User.id == account
					)
				)

			cleanup.commit()

		setup_engine.dispose()


def test_deleting_the_holder_leaves_the_task_takeable_again (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``ondelete="SET NULL"`` clears the holder and leaves the two dates behind.

	Worth asserting because it is the shape a CHECK constraint saying "all three move together"
	would forbid — that constraint was considered for `#362` and would have made deleting an
	account fail whenever it held a claim. The predicate reads this correctly instead.
	"""

	workspace, project, owner = _place(session)
	task = _task(session, project)
	leaving = _person(session, workspace)

	subroutine.domain.claims.claim(session, task, actor=leaving)
	session.flush()

	session.execute(
		sqlalchemy.delete(subroutine.db.models.identity.User).where(
			subroutine.db.models.identity.User.id == leaving.user.id
		)
	)
	session.expire(task)

	assert task.claimed_by_id is None
	assert task.claim_expires_at is not None, "the dates are left where they were"
	assert subroutine.domain.claims.held_by(task) is None

	taken = subroutine.domain.claims.claim(session, task, actor=owner)

	assert taken.claimed_by_id == owner.user.id


def test_a_claimed_task_reports_the_holder_by_name (session: sqlalchemy.orm.Session) -> None:
	"""`#726`. A view reporting only ``claimed_by_id`` makes every surface resolve a uuid first.

	The same shape as ``assignee`` beside ``assignee_id`` (`#511`), and the same reason: a
	username is how a person is addressed, so a browser wanting to say *who is on this* would
	otherwise need a request per row to find out.

	**`views.Task` argued the opposite and cited `assignee_id` as its precedent** — which `#511`
	had already moved. The comment went on supporting itself with the one thing that contradicted
	it, which is why this test exists rather than only the field.
	"""

	_workspace, project, owner = _place(session)
	task = _task(session, project)

	before = subroutine.views.task(task, subroutine.views.Vocabulary.for_tasks(session, [task]))

	assert before.claimed_by is None, "an unclaimed task named a holder"

	subroutine.domain.claims.claim(session, task, actor=owner)
	session.flush()

	after = subroutine.views.task(task, subroutine.views.Vocabulary.for_tasks(session, [task]))

	assert after.claimed_by == owner.user.username, (
		f"a claimed task reports {after.claimed_by!r} rather than the username that took it"
	)

	assert after.claimed_by_id == owner.user.id, "the id and the name must name one account"
