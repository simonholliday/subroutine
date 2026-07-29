"""Tests for the service layer: refs, events, paths and mentions.

The done-criteria for this slice live here — creating a task allocates ``SR-1``, writes
one event and sets a correct path, and a description citing it produces exactly one
mention row that disappears when the sentence does.

Everything runs on both backends. One test does not: concurrent ref allocation is
meaningless on SQLite, which has a single writer, and it is precisely the bug that is
invisible there by construction.
"""

import concurrent.futures
import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.seed
import subroutine.db.session
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.events
import subroutine.domain.mentions
import subroutine.domain.projects
import subroutine.domain.refs
import subroutine.domain.tasks
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors


def _founder (session: sqlalchemy.orm.Session) -> subroutine.db.models.identity.User:
	"""Create the user a workspace is founded by."""

	return subroutine.domain.users.create(
		session, username=f"founder-{uuid.uuid4().hex[:8]}"
	)


def _workspace (
	session: sqlalchemy.orm.Session,
) -> subroutine.db.models.identity.Workspace:
	"""Create a fully seeded workspace with an owner."""

	return subroutine.domain.workspaces.create(
		session,
		slug=f"ws-{uuid.uuid4().hex[:8]}",
		title="Test workspace",
		owner=_founder(session),
	)


def _project (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	**kwargs: typing.Any,
) -> subroutine.db.models.project.Project:
	"""Create a project in a workspace."""

	kwargs.setdefault("key", f"P{uuid.uuid4().hex[:10].upper()}")
	kwargs.setdefault("title", "Test project")

	return subroutine.domain.projects.create(session, workspace_id=workspace.id, **kwargs)


def _events (
	session: sqlalchemy.orm.Session,
	workspace_id: uuid.UUID,
	entity_type: str,
	entity_id: uuid.UUID,
) -> list[subroutine.db.models.activity.Event]:
	"""Return one entity's events, oldest first."""

	model = subroutine.db.models.activity.Event

	return list(
		session.scalars(
			sqlalchemy.select(model)
			.where(
				model.workspace_id == workspace_id,
				model.entity_type == entity_type,
				model.entity_id == entity_id,
			)
			.order_by(model.seq)
		)
	)


def test_creating_a_task_allocates_a_ref_an_event_and_a_path (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The done-criterion for S1-10."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	task = subroutine.domain.tasks.create(session, project=project, title="First thing")

	assert task.ref == "SR-1"
	assert task.number == 1
	assert task.origin_project_id == project.id
	assert task.path == f"/{task.id}/"
	assert task.depth == 0

	events = _events(session, workspace.id, "task", task.id)

	assert len(events) == 1
	assert events[0].action == "created"
	assert events[0].changes == {"ref": {"from": None, "to": "SR-1"}, "title": {"from": None, "to": "First thing"}}


def test_refs_are_sequential_and_shared_with_documents (
	session: sqlalchemy.orm.Session,
) -> None:
	"""One counter per project, so a ref names exactly one thing (SPEC.md §6.2)."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	refs = [
		subroutine.domain.tasks.create(session, project=project, title=f"Task {index}").ref
		for index in range(5)
	]

	assert refs == ["SR-1", "SR-2", "SR-3", "SR-4", "SR-5"]

	# The next allocation continues the same sequence whoever asks for it.
	assert subroutine.domain.refs.allocate(session, project) == ("SR-6", 6)


def test_a_ref_survives_its_task_moving_project (session: sqlalchemy.orm.Session) -> None:
	"""``origin_project_id`` is what stops a move colliding with the destination."""

	workspace = _workspace(session)
	home = _project(session, workspace, key="HOME")
	other = _project(session, workspace, key="SR")

	task = subroutine.domain.tasks.create(session, project=home, title="Moves later")

	assert task.ref == "HOME-1"

	task.project_id = other.id
	session.flush()

	# The destination mints its own number 1 without a collision, because uniqueness is
	# keyed on the project that minted the number, not the one holding the task.
	sibling = subroutine.domain.tasks.create(session, project=other, title="Native")

	assert task.ref == "HOME-1"
	assert sibling.ref == "SR-1"


def test_split_and_format_are_inverses () -> None:
	"""A ref parses back into the parts it was built from."""

	assert subroutine.domain.refs.format_ref("SR", 42) == "SR-42"
	assert subroutine.domain.refs.split_ref("SR-42") == ("SR", 42)
	assert subroutine.domain.refs.split_ref("HOME-3") == ("HOME", 3)
	assert subroutine.domain.refs.split_ref("nonsense") is None
	assert subroutine.domain.refs.split_ref("SR-") is None


def test_a_ref_resolves_to_the_thing_it_names (session: sqlalchemy.orm.Session) -> None:
	"""What the mention index is built on."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(session, project=project, title="Findable")

	assert subroutine.domain.refs.find(session, workspace.id, "SR-1") == ("task", task.id)
	assert subroutine.domain.refs.find(session, workspace.id, "SR-99") is None


def test_a_new_workspace_arrives_complete (session: sqlalchemy.orm.Session) -> None:
	"""Vocabulary, an owner and an event, or none of it (SPEC.md §10.7 invariant 7)."""

	owner = _founder(session)
	workspace = subroutine.domain.workspaces.create(
		session, slug="Home Workspace", title="Home", owner=owner
	)

	assert workspace.slug == "home-workspace"
	assert workspace.settings[subroutine.db.seed.SEED_VERSION_KEY] == subroutine.db.seed.SEED_VERSION

	membership = session.scalars(
		sqlalchemy.select(subroutine.db.models.identity.WorkspaceMember).where(
			subroutine.db.models.identity.WorkspaceMember.workspace_id == workspace.id
		)
	).one()

	assert membership.user_id == owner.id

	role = session.get(subroutine.db.models.identity.Role, membership.role_id)

	assert role is not None
	assert role.key == "owner"

	# Creation, then the vocabulary that was written for it — one event for ~35 rows, so
	# the feed's first page is not entirely statuses and roles (SPEC.md §10.7 invariant 9).
	events = _events(session, workspace.id, "workspace", workspace.id)

	assert [event.action for event in events] == ["created", "seeded"]

	seeded = events[1].changes

	assert seeded is not None
	assert seeded["seed_version"]["to"] == subroutine.db.seed.SEED_VERSION
	assert seeded["roles"]["to"] == 5
	assert seeded["statuses"]["to"] == 14


def test_a_duplicate_workspace_slug_is_refused_by_name (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The error names the field, not the constraint."""

	subroutine.domain.workspaces.create(
		session, slug="home", title="Home", owner=_founder(session)
	)

	with pytest.raises(subroutine.errors.Conflict) as error:
		subroutine.domain.workspaces.create(
			session, slug="Home", title="Home again", owner=_founder(session)
		)

	assert error.value.status == 409
	assert error.value.errors[0].field == "slug"


def test_a_duplicate_username_is_refused_by_name (session: sqlalchemy.orm.Session) -> None:
	"""Normalised comparison, so case is not a way around it."""

	subroutine.domain.users.create(session, username="Simon")

	with pytest.raises(subroutine.errors.Conflict) as error:
		subroutine.domain.users.create(session, username="simon")

	assert error.value.errors[0].field == "username"


def test_a_weak_password_is_refused_with_the_reason (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The message is for the person choosing it."""

	with pytest.raises(subroutine.errors.ValidationError) as error:
		subroutine.domain.users.create(session, username="someone", password="short")

	assert error.value.errors[0].field == "password"
	assert "12" in error.value.detail


def test_a_service_account_cannot_have_a_password (
	session: sqlalchemy.orm.Session,
) -> None:
	"""An agent authenticates with a token that can be bounded and revoked."""

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.users.create(
			session, username="claude", password="a decent passphrase", is_service_account=True
		)


def test_a_password_verifies_and_rehashes_transparently (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Login is the only moment the plaintext exists, so it is when the hash is upgraded."""

	user = subroutine.domain.users.create(
		session, username="simon", password="a decent passphrase"
	)

	assert subroutine.domain.users.verify_password(session, user, "a decent passphrase")
	assert not subroutine.domain.users.verify_password(session, user, "the wrong one")


def test_creating_a_project_makes_its_owner_a_member_of_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Otherwise a private project is invisible to the person who created it.

	SPEC.md §7.3a grants sight of a private project to holders of a ``project_member`` row
	and to nobody else. Nothing in the application ever wrote one until this was added —
	every row in existence had been inserted by a test — so private visibility was a
	feature that could not be reached through any supported entry point. The row is written
	for public projects too, so that making one private later does not lock its owner out.
	"""

	workspace = _workspace(session)
	owner = _founder(session)
	project = _project(session, workspace, owner_id=owner.id, visibility="private")

	model = subroutine.db.models.project.ProjectMember
	membership = session.scalars(
		sqlalchemy.select(model).where(
			model.project_id == project.id, model.user_id == owner.id
		)
	).one()

	assert membership.role_id is None, "an owner keeps their workspace role, not a new one"

	principal = subroutine.domain.authentication.Principal(user=owner)

	assert subroutine.domain.authorization.is_visible(session, principal, project)


def test_a_project_template_writes_settings_and_nothing_else (
	session: sqlalchemy.orm.Session,
) -> None:
	"""SPEC.md §6.12: templates are seed-time only and create no statuses."""

	workspace = _workspace(session)

	personal = _project(session, workspace, template="personal")
	software = _project(session, workspace, template="software")

	assert personal.settings["visible_status_keys"] == ["open", "done"]
	assert personal.settings["require_verification_to_complete"] is False
	assert software.settings["require_verification_to_complete"] is True
	assert "in_progress" in software.settings["visible_status_keys"]

	# Both projects live in one workspace with one set of statuses, which is the whole
	# reason a template writes settings rather than seeding rows.
	statuses = session.scalars(
		sqlalchemy.select(subroutine.db.models.vocabulary.Status.key).where(
			subroutine.db.models.vocabulary.Status.workspace_id == workspace.id,
			subroutine.db.models.vocabulary.Status.entity_type == "task",
		)
	).all()

	assert len(statuses) == len(set(statuses)) == 6


def test_an_unknown_template_lists_the_real_ones (session: sqlalchemy.orm.Session) -> None:
	"""Errors name the valid alternatives."""

	workspace = _workspace(session)

	with pytest.raises(subroutine.errors.ValidationError) as error:
		_project(session, workspace, template="agile")

	hint = error.value.errors[0].hint

	assert hint is not None
	assert "software" in hint


def test_a_project_tree_maintains_its_paths (session: sqlalchemy.orm.Session) -> None:
	"""SPEC.md §10.7 invariant 1: path and depth always agree with parent_id."""

	workspace = _workspace(session)
	root = _project(session, workspace)
	middle = _project(session, workspace, parent=root)
	leaf = _project(session, workspace, parent=middle)

	assert root.path == f"/{root.id}/"
	assert middle.path == f"/{root.id}/{middle.id}/"
	assert leaf.path == f"/{root.id}/{middle.id}/{leaf.id}/"
	assert (root.depth, middle.depth, leaf.depth) == (0, 1, 2)


def test_moving_a_project_takes_its_subtree_with_it (
	session: sqlalchemy.orm.Session,
) -> None:
	"""And rewrites every descendant, which is the price of a materialised path."""

	workspace = _workspace(session)
	root = _project(session, workspace)
	middle = _project(session, workspace, parent=root)
	leaf = _project(session, workspace, parent=middle)
	elsewhere = _project(session, workspace)

	rewritten = subroutine.domain.projects.move(session, middle, parent=elsewhere)

	assert rewritten == 2, "the moved project and its one descendant"

	session.refresh(middle)
	session.refresh(leaf)

	assert middle.path == f"/{elsewhere.id}/{middle.id}/"
	assert leaf.path == f"/{elsewhere.id}/{middle.id}/{leaf.id}/"
	assert (middle.depth, leaf.depth) == (1, 2)
	assert middle.parent_id == elsewhere.id

	events = _events(session, workspace.id, "project", middle.id)

	assert [event.action for event in events] == ["created", "moved"]


def test_a_project_cannot_be_moved_inside_itself (session: sqlalchemy.orm.Session) -> None:
	"""The cycle check, including the degenerate case of moving under itself."""

	workspace = _workspace(session)
	root = _project(session, workspace)
	child = _project(session, workspace, parent=root)

	for target in (root, child):
		with pytest.raises(subroutine.errors.Conflict) as error:
			subroutine.domain.projects.move(session, root, parent=target)

		assert error.value.code == "cycle_detected"


def test_depth_is_bounded_for_the_whole_subtree (session: sqlalchemy.orm.Session) -> None:
	"""Checked against the deepest descendant, since a move brings everything with it."""

	workspace = _workspace(session)
	root = _project(session, workspace)
	child = _project(session, workspace, parent=root)
	deep = _project(session, workspace)

	# `child` sits one below `root`; moving `root` under `deep` would put it two down.
	with pytest.raises(subroutine.errors.Conflict) as error:
		subroutine.domain.projects.move(session, root, parent=deep, max_depth=1)

	assert error.value.code == "cycle_detected"
	assert "limit is 1" in error.value.detail
	assert child.path.startswith(root.path), "the refused move must have changed nothing"


def test_moving_to_the_same_place_does_nothing (session: sqlalchemy.orm.Session) -> None:
	"""No rows rewritten and no event, so the feed records changes rather than requests."""

	workspace = _workspace(session)
	root = _project(session, workspace)
	child = _project(session, workspace, parent=root)

	assert subroutine.domain.projects.move(session, child, parent=root) == 0
	assert [event.action for event in _events(session, workspace.id, "project", child.id)] == [
		"created"
	]


def test_subtasks_get_paths_too (session: sqlalchemy.orm.Session) -> None:
	"""The same machinery serves both trees."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	parent = subroutine.domain.tasks.create(session, project=project, title="Parent")
	child = subroutine.domain.tasks.create(
		session, project=project, title="Child", parent=parent
	)

	assert child.path == f"/{parent.id}/{child.id}/"
	assert child.depth == 1


def test_a_mention_appears_and_disappears_with_the_sentence (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The other half of the S1-10 done-criterion (SPEC.md §6.15)."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	target = subroutine.domain.tasks.create(session, project=project, title="The spec")
	citing = subroutine.domain.tasks.create(
		session, project=project, title="Implements it", description=f"As decided in {target.ref}."
	)

	mentions = subroutine.domain.mentions.backlinks(
		session, workspace_id=workspace.id, target_type="task", target_id=target.id
	)

	assert len(mentions) == 1
	assert mentions[0].source_id == citing.id

	subroutine.domain.tasks.update(session, citing, description="No longer refers to anything.")

	assert (
		subroutine.domain.mentions.backlinks(
			session, workspace_id=workspace.id, target_type="task", target_id=target.id
		)
		== []
	)


def test_the_same_ref_twice_is_one_mention (session: sqlalchemy.orm.Session) -> None:
	"""Repeated references collapse to one edge."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	target = subroutine.domain.tasks.create(session, project=project, title="Cited")

	subroutine.domain.tasks.create(
		session,
		project=project,
		title=f"See {target.ref}",
		description=f"{target.ref} again, and {target.ref} once more.",
	)

	assert (
		len(
			subroutine.domain.mentions.backlinks(
				session, workspace_id=workspace.id, target_type="task", target_id=target.id
			)
		)
		== 1
	)


def test_an_unresolvable_ref_stays_prose (session: sqlalchemy.orm.Session) -> None:
	""""The SR-71 Blackbird" is not a reference to anything."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	task = subroutine.domain.tasks.create(
		session, project=project, title="Reading", description="The SR-71 Blackbird, and IR-35."
	)

	mentions = list(
		session.scalars(
			sqlalchemy.select(subroutine.db.models.work.Mention).where(
				subroutine.db.models.work.Mention.source_id == task.id
			)
		)
	)

	assert mentions == []
	assert task.description is not None
	assert "SR-71" in task.description, "the text is never altered"


def test_a_task_does_not_mention_itself (session: sqlalchemy.orm.Session) -> None:
	"""Quoting your own ref records nothing."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(session, project=project, title="First")

	subroutine.domain.tasks.update(session, task, description=f"This is {task.ref}.")

	assert (
		subroutine.domain.mentions.backlinks(
			session, workspace_id=workspace.id, target_type="task", target_id=task.id
		)
		== []
	)


def test_the_explicit_link_form_is_recognised (session: sqlalchemy.orm.Session) -> None:
	"""``[label](subroutine:SR-1)`` means the same as a bare ref."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	target = subroutine.domain.tasks.create(session, project=project, title="The spec")

	subroutine.domain.tasks.create(
		session,
		project=project,
		title="Implements",
		description=f"Implements [the spec](subroutine:{target.ref}).",
	)

	assert (
		len(
			subroutine.domain.mentions.backlinks(
				session, workspace_id=workspace.id, target_type="task", target_id=target.id
			)
		)
		== 1
	)


def test_a_cross_workspace_link_is_not_resolved_locally (
	session: sqlalchemy.orm.Session,
) -> None:
	"""``subroutine:acme/SR-1`` names a workspace this index does not cover."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	target = subroutine.domain.tasks.create(session, project=project, title="Local SR-1")

	subroutine.domain.tasks.create(
		session,
		project=project,
		title="Elsewhere",
		description=f"See [theirs](subroutine:acme/{target.ref}).",
	)

	assert (
		subroutine.domain.mentions.backlinks(
			session, workspace_id=workspace.id, target_type="task", target_id=target.id
		)
		== []
	)


def test_an_update_that_changes_nothing_writes_no_event (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The change feed records changes, not requests."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(session, project=project, title="Steady")

	version = task.version
	subroutine.domain.tasks.update(session, task, title="Steady")

	assert task.version == version
	assert [event.action for event in _events(session, workspace.id, "task", task.id)] == [
		"created"
	]


def test_an_update_records_only_what_moved (session: sqlalchemy.orm.Session) -> None:
	"""And bumps the version once."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(session, project=project, title="Before")

	subroutine.domain.tasks.update(session, task, title="After", importance=3)

	events = _events(session, workspace.id, "task", task.id)

	assert [event.action for event in events] == ["created", "updated"]
	assert events[1].changes == {
		"title": {"from": "Before", "to": "After"},
		"importance": {"from": None, "to": 3},
	}
	assert task.version == 2


def test_absent_and_null_mean_different_things (session: sqlalchemy.orm.Session) -> None:
	"""SPEC.md §8.3: leaving a field out keeps it; passing null clears it."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(
		session, project=project, title="Has one", description="Something."
	)

	subroutine.domain.tasks.update(session, task, title="Renamed")

	assert task.description == "Something."

	subroutine.domain.tasks.update(session, task, description=None)

	assert task.description is None


def test_an_unknown_status_lists_the_real_ones (session: sqlalchemy.orm.Session) -> None:
	"""SPEC.md §8.8's worked example, as an actual error."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	with pytest.raises(subroutine.errors.ValidationError) as error:
		subroutine.domain.tasks.create(
			session, project=project, title="Bad status", status_key="in-progress"
		)

	assert error.value.code == "invalid_status"
	assert error.value.errors[0].field == "status"

	hint = error.value.errors[0].hint

	assert hint is not None
	assert "in_progress" in hint


def test_events_carry_their_actor (session: sqlalchemy.orm.Session) -> None:
	"""Attribution is what makes the trail worth keeping."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	user = subroutine.domain.users.create(session, username="agent-owner")

	# A member, because the service now enforces the permission check. Before it did, this
	# test passed with an actor who belonged to no workspace at all — which is what made
	# the missing enforcement invisible.
	subroutine.domain.workspaces.add_member(session, workspace, user, role_key="member")

	token, _issued = subroutine.domain.authentication.issue_token(
		session, user=user, title="Agent"
	)
	principal = subroutine.domain.authentication.Principal(user=user, token=token)

	task = subroutine.domain.tasks.create(
		session, project=project, title="Done by an agent", actor=principal
	)

	event = _events(session, workspace.id, "task", task.id)[0]

	assert event.actor_user_id == user.id
	assert event.actor_token_id == token.id
	assert task.created_by == user.id


def test_event_changes_survive_json (session: sqlalchemy.orm.Session) -> None:
	"""UUIDs and datetimes serialise nowhere by default, and appear in changes constantly."""

	moment = subroutine.db.types.utcnow()
	identifier = subroutine.db.types.new_uuid()

	converted = subroutine.domain.events.jsonable(
		{"when": moment, "who": identifier, "tags": {"a", "b"}}
	)

	assert converted["when"] == moment.isoformat()
	assert converted["who"] == str(identifier)
	assert sorted(converted["tags"]) == ["a", "b"]


def test_seq_orders_events_across_entities (session: sqlalchemy.orm.Session) -> None:
	"""The change cursor has to increase, or syncing has nothing to page on."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	for index in range(3):
		subroutine.domain.tasks.create(session, project=project, title=f"Task {index}")

	sequences = list(
		session.scalars(
			sqlalchemy.select(subroutine.db.models.activity.Event.seq)
			.where(subroutine.db.models.activity.Event.workspace_id == workspace.id)
			.order_by(subroutine.db.models.activity.Event.seq)
		)
	)

	assert sequences == sorted(sequences)
	assert len(sequences) == len(set(sequences))


def test_concurrent_ref_allocation_never_duplicates (
	engine: sqlalchemy.engine.Engine, postgres_url: str
) -> None:
	"""The bug SQLite cannot express, because it has one writer.

	Runs on PostgreSQL only, with real connections rather than the shared-transaction
	fixture: the whole question is what two transactions do to one row at once, and the
	fixture exists to stop tests seeing each other's transactions.
	"""

	if engine.dialect.name != "postgresql":
		pytest.skip("SQLite serialises writers, so there is no contention to test")

	workers = 4
	each = 10

	setup_engine = subroutine.db.session.create_engine(postgres_url)
	factory = sqlalchemy.orm.sessionmaker(bind=setup_engine, expire_on_commit=False)

	try:
		with factory() as setup:
			workspace = _workspace(setup)
			project = _project(setup, workspace, key="RACE")
			setup.commit()
			project_id = project.id

		def allocate_many () -> list[int]:
			"""Claim refs from an independent connection."""

			with factory() as worker:
				held = worker.get(subroutine.db.models.project.Project, project_id)
				assert held is not None

				numbers = [subroutine.domain.refs.allocate(worker, held)[1] for _ in range(each)]
				worker.commit()

				return numbers

		with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
			results = [future.result() for future in [pool.submit(allocate_many) for _ in range(workers)]]

		allocated = sorted(number for batch in results for number in batch)

		assert allocated == sorted(set(allocated)), "the same ref was handed out twice"
		assert allocated == list(range(1, workers * each + 1))

		with factory() as cleanup:
			cleanup.execute(
				sqlalchemy.delete(subroutine.db.models.identity.Workspace).where(
					subroutine.db.models.identity.Workspace.id == workspace.id
				)
			)
			cleanup.commit()

	finally:
		setup_engine.dispose()


def test_a_refused_update_changes_nothing (session: sqlalchemy.orm.Session) -> None:
	"""A caller holds a live session it may still commit, so a half-applied update ships."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(session, project=project, title="Original")

	with pytest.raises(subroutine.errors.ValidationError):
		subroutine.domain.tasks.update(
			session, task, title="Changed", status_key="no-such-status"
		)

	assert task.title == "Original", "the title was assigned before the status was validated"
	assert task.version == 1
	assert [event.action for event in _events(session, workspace.id, "task", task.id)] == [
		"created"
	]


def test_an_update_holds_a_title_to_the_same_rule_as_a_create (
	session: sqlalchemy.orm.Session,
) -> None:
	"""A task whose title has been blanked is not a task anybody can find again."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(session, project=project, title="Findable")

	with pytest.raises(subroutine.errors.ValidationError) as error:
		subroutine.domain.tasks.update(session, task, title="   ")

	assert error.value.errors[0].field == "title"
	assert task.title == "Findable"


def test_over_length_text_is_refused_the_same_way_on_both_backends (
	session: sqlalchemy.orm.Session,
) -> None:
	"""SQLite does not enforce VARCHAR lengths and PostgreSQL does; neither should decide."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")

	with pytest.raises(subroutine.errors.PayloadTooLarge) as too_long:
		subroutine.domain.tasks.create(session, project=project, title="x" * 513)

	assert too_long.value.status == 413
	assert too_long.value.errors[0].field == "title"

	with pytest.raises(subroutine.errors.PayloadTooLarge):
		subroutine.domain.users.create(session, username="u" * 65)

	with pytest.raises(subroutine.errors.PayloadTooLarge):
		subroutine.domain.workspaces.create(
			session, slug="s" * 65, title="Fine", owner=_founder(session)
		)


def test_completing_a_task_records_when (session: sqlalchemy.orm.Session) -> None:
	"""SPEC.md §10.7 invariant 5: completed_at is set exactly when the category is final."""

	workspace = _workspace(session)
	project = _project(session, workspace, key="SR")
	task = subroutine.domain.tasks.create(session, project=project, title="Finish me")

	# Read through a local each time: asserting on the attribute directly narrows its type
	# for the rest of the function, and mypy cannot see that the next update changes it.
	created = task.completed_at

	assert created is None

	subroutine.domain.tasks.update(session, task, status_key="done")
	finished = task.completed_at

	assert finished is not None

	subroutine.domain.tasks.update(session, task, status_key="open")
	reopened = task.completed_at

	assert reopened is None, "reopening must clear it, or the invariant is one-way"

	subroutine.domain.tasks.update(session, task, status_key="cancelled")
	cancelled = task.completed_at

	assert cancelled is not None, "cancelled is a finished category too"


def test_moving_a_project_moves_every_etag_it_changed (
	session: sqlalchemy.orm.Session,
) -> None:
	"""`version` is the ETag (SPEC.md §8.9), and a move rewrites descendants' paths."""

	workspace = _workspace(session)
	root = _project(session, workspace)
	child = _project(session, workspace, parent=root)
	grandchild = _project(session, workspace, parent=child)
	elsewhere = _project(session, workspace)

	versions = {p.id: p.version for p in (child, grandchild)}

	subroutine.domain.projects.move(session, child, parent=elsewhere)

	for project in (child, grandchild):
		session.refresh(project)

		assert project.version > versions[project.id], (
			f"{project.key}'s path changed but its ETag did not"
		)


def test_a_project_key_must_be_one_the_mention_index_can_see (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Otherwise every ref the project mints is invisible to backlinks, permanently."""

	workspace = _workspace(session)

	for refused in ("3D", "CAFÉ", "123", "!!"):
		with pytest.raises(subroutine.errors.ValidationError) as error:
			subroutine.domain.projects.create(
				session, workspace_id=workspace.id, key=refused, title="No"
			)

		assert error.value.errors[0].field == "key"

	project = subroutine.domain.projects.create(
		session, workspace_id=workspace.id, key="web2", title="Yes"
	)
	task = subroutine.domain.tasks.create(session, project=project, title="Findable")

	assert project.key == "WEB2"
	assert subroutine.domain.mentions.REF_PATTERN.fullmatch(task.ref) is not None


def test_a_deleted_workspace_releases_its_short_name (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Every other identifier frees on soft delete; this one used to be the exception."""

	first = subroutine.domain.workspaces.create(
		session, slug="reusable", title="First", owner=_founder(session)
	)
	first.deleted_at = subroutine.db.types.utcnow()
	session.flush()

	second = subroutine.domain.workspaces.create(
		session, slug="reusable", title="Second", owner=_founder(session)
	)

	assert second.slug == "reusable"
	assert second.id != first.id
