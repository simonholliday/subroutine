"""Tests that the schema builds, and that its constraints do what they claim.

The constraint tests matter more than the shape tests. A unique index that silently
permits duplicates, or a foreign key that is decorative on one backend, is worse than an
absent one — it is a guarantee the rest of the code is entitled to rely on.
"""

import datetime
import uuid

import pytest
import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.exc
import sqlalchemy.orm

import subroutine.db.base
import subroutine.db.mixins
import subroutine.db.models
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.db.types

EXPECTED_TABLES = {
	"api_token",
	"comment",
	"document",
	"document_tag",
	"event",
	"item_type",
	"link",
	"link_type",
	"project",
	"project_member",
	"role",
	"status",
	"tag",
	"task",
	"task_tag",
	"user",
	"workspace",
	"workspace_member",
}


def _make_workspace (session: sqlalchemy.orm.Session) -> subroutine.db.models.identity.Workspace:
	"""Create a minimal workspace to hang other rows from."""

	workspace = subroutine.db.models.identity.Workspace(
		slug=f"ws-{uuid.uuid4().hex[:8]}", title="Test workspace"
	)
	session.add(workspace)
	session.flush()

	return workspace


def _make_project (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	*,
	key: str = "SR",
) -> subroutine.db.models.project.Project:
	"""Create a project with the status and type vocabulary it needs."""

	status = subroutine.db.models.vocabulary.Status(
		workspace_id=workspace.id,
		entity_type="project",
		key=f"active-{uuid.uuid4().hex[:6]}",
		label="Active",
		category="in_progress",
		position=1,
	)
	session.add(status)
	session.flush()

	project = subroutine.db.models.project.Project(
		workspace_id=workspace.id,
		key=key,
		title="Test project",
		status_id=status.id,
		path="/",
	)
	session.add(project)
	session.flush()

	return project


def _make_task (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	project: subroutine.db.models.project.Project,
	*,
	number: int,
	**overrides: object,
) -> subroutine.db.models.work.Task:
	"""Create a task with the vocabulary rows it depends on."""

	status = subroutine.db.models.vocabulary.Status(
		workspace_id=workspace.id,
		entity_type="task",
		key=f"open-{uuid.uuid4().hex[:6]}",
		label="Open",
		category="todo",
		position=1,
	)
	item_type = subroutine.db.models.vocabulary.ItemType(
		workspace_id=workspace.id,
		entity_type="task",
		key=f"task-{uuid.uuid4().hex[:6]}",
		label="Task",
		position=1,
	)
	session.add_all([status, item_type])
	session.flush()

	fields: dict[str, object] = {
		"workspace_id": workspace.id,
		"project_id": project.id,
		"origin_project_id": project.id,
		"type_id": item_type.id,
		"status_id": status.id,
		"ref": f"{project.key}-{number}",
		"number": number,
		"title": "A task",
		"path": "/",
	}
	fields.update(overrides)

	task = subroutine.db.models.work.Task(**fields)
	session.add(task)
	session.flush()

	return task


def test_every_expected_table_exists (engine: sqlalchemy.engine.Engine) -> None:
	"""The schema contains exactly the tables the specification lists."""

	inspector = sqlalchemy.inspect(engine)

	assert set(inspector.get_table_names()) >= EXPECTED_TABLES


def test_metadata_matches_the_expected_table_set () -> None:
	"""No table has been added or removed without the test being updated too."""

	declared = set(subroutine.db.base.Base.metadata.tables)

	# Exactly equal, not merely a superset: test-only tables live on their own metadata
	# (tests/sample_models.py) so that the migration-drift check has a clean comparison.
	assert declared == EXPECTED_TABLES


def test_workspace_and_task_round_trip (session: sqlalchemy.orm.Session) -> None:
	"""A task can be created through the full chain of its dependencies."""

	workspace = _make_workspace(session)
	project = _make_project(session, workspace)
	task = _make_task(session, workspace, project, number=1)

	assert task.ref == "SR-1"
	assert task.spent_minutes == 0
	assert task.is_template is False
	assert task.meta == {}
	assert task.content_updated_at is not None


def test_task_ref_is_unique_among_live_rows (session: sqlalchemy.orm.Session) -> None:
	"""Two live tasks cannot share a ref.

	The constraint is a *partial* unique index. Written the obvious way — including
	``deleted_at`` in a plain UNIQUE — it would permit unlimited duplicates, because
	NULLs compare as distinct on both backends.
	"""

	workspace = _make_workspace(session)
	project = _make_project(session, workspace)
	_make_task(session, workspace, project, number=1)

	with pytest.raises(sqlalchemy.exc.IntegrityError):
		_make_task(session, workspace, project, number=2, ref="SR-1")


def test_a_deleted_ref_can_be_reused (session: sqlalchemy.orm.Session) -> None:
	"""Soft-deleting a task frees its ref, which a plain UNIQUE would not allow."""

	workspace = _make_workspace(session)
	project = _make_project(session, workspace)
	first = _make_task(session, workspace, project, number=1)

	first.deleted_at = subroutine.db.types.utcnow()
	session.flush()

	second = _make_task(session, workspace, project, number=2, ref="SR-1")

	assert second.ref == "SR-1"


def test_task_numbers_are_unique_within_their_originating_project (
	session: sqlalchemy.orm.Session,
) -> None:
	"""The number belongs to the project that minted it, not the current one.

	Keying on ``project_id`` would collide as soon as a moved task's new home reached the
	same number — from an entirely legitimate action.
	"""

	workspace = _make_workspace(session)
	project = _make_project(session, workspace)
	_make_task(session, workspace, project, number=1)

	with pytest.raises(sqlalchemy.exc.IntegrityError):
		_make_task(session, workspace, project, number=1, ref="SR-99")


def test_a_moved_task_keeps_its_ref_without_colliding (session: sqlalchemy.orm.Session) -> None:
	"""A task moved to another project keeps its ref; the new project reuses the number."""

	workspace = _make_workspace(session)
	home = _make_project(session, workspace, key="HOME")
	work = _make_project(session, workspace, key="SR")

	moved = _make_task(session, workspace, home, number=3, ref="HOME-3")
	moved.project_id = work.id
	session.flush()

	# `SR` mints its own number 3. Nothing collides, because the moved task's number
	# still belongs to HOME.
	native = _make_task(session, workspace, work, number=3, ref="SR-3")

	assert moved.ref == "HOME-3"
	assert native.ref == "SR-3"


def test_importance_is_constrained_to_the_documented_range (
	session: sqlalchemy.orm.Session,
) -> None:
	"""Importance outside 1-5 is refused by the database, not merely by validation."""

	workspace = _make_workspace(session)
	project = _make_project(session, workspace)

	with pytest.raises(sqlalchemy.exc.IntegrityError):
		_make_task(session, workspace, project, number=1, importance=7)


def test_status_category_is_constrained (session: sqlalchemy.orm.Session) -> None:
	"""A status must map to one of the known categories."""

	workspace = _make_workspace(session)

	session.add(
		subroutine.db.models.vocabulary.Status(
			workspace_id=workspace.id,
			entity_type="task",
			key="invented",
			label="Invented",
			category="whenever",
			position=1,
		)
	)

	with pytest.raises(sqlalchemy.exc.IntegrityError):
		session.flush()


def test_documents_use_the_same_ref_counter_as_tasks (session: sqlalchemy.orm.Session) -> None:
	"""A ref names exactly one thing, whether it is a task or a document."""

	workspace = _make_workspace(session)
	project = _make_project(session, workspace)
	_make_task(session, workspace, project, number=1)

	status = subroutine.db.models.vocabulary.Status(
		workspace_id=workspace.id,
		entity_type="document",
		key="draft",
		label="Draft",
		category="draft",
		position=1,
	)
	item_type = subroutine.db.models.vocabulary.ItemType(
		workspace_id=workspace.id, entity_type="document", key="spec", label="Spec", position=1
	)
	session.add_all([status, item_type])
	session.flush()

	document = subroutine.db.models.work.Document(
		workspace_id=workspace.id,
		project_id=project.id,
		origin_project_id=project.id,
		type_id=item_type.id,
		status_id=status.id,
		ref="SR-2",
		number=2,
		title="A specification",
		path="/",
	)
	session.add(document)
	session.flush()

	assert document.ref == "SR-2"


def test_documents_have_no_scheduling_columns () -> None:
	"""Documents genuinely lack the fields that only apply to work.

	This is the split from the specification made mechanical: if one of these ever
	appears, every scheduling query in the system needs an entity-type filter forever.
	"""

	columns = set(subroutine.db.models.work.Document.__table__.columns.keys())
	forbidden = {
		"assignee_id",
		"due_at",
		"estimate_minutes",
		"importance",
		"planned_for",
		"start_at",
		"urgency",
	}

	assert columns & forbidden == set()


def test_a_link_cannot_point_at_itself (session: sqlalchemy.orm.Session) -> None:
	"""Self-links are rejected by the database."""

	workspace = _make_workspace(session)
	link_type = subroutine.db.models.vocabulary.LinkType(
		workspace_id=workspace.id, key="blocks", title="blocks", inverse_title="is blocked by"
	)
	session.add(link_type)
	session.flush()

	subject = subroutine.db.types.new_uuid()
	session.add(
		subroutine.db.models.work.Link(
			workspace_id=workspace.id,
			source_type="task",
			source_id=subject,
			target_type="task",
			target_id=subject,
			link_type_id=link_type.id,
		)
	)

	with pytest.raises(sqlalchemy.exc.IntegrityError):
		session.flush()


def test_events_receive_ascending_sequence_numbers (session: sqlalchemy.orm.Session) -> None:
	"""The change-feed cursor increases, which is the whole basis of syncing."""

	workspace = _make_workspace(session)
	first = subroutine.db.models.activity.Event(
		workspace_id=workspace.id,
		entity_type="task",
		entity_id=subroutine.db.types.new_uuid(),
		action="created",
	)
	second = subroutine.db.models.activity.Event(
		workspace_id=workspace.id,
		entity_type="task",
		entity_id=subroutine.db.types.new_uuid(),
		action="updated",
	)
	session.add_all([first, second])
	session.flush()

	assert second.seq > first.seq


def test_foreign_keys_are_enforced (session: sqlalchemy.orm.Session) -> None:
	"""A row cannot reference a workspace that does not exist.

	SQLite enforces this only because the connection sets ``PRAGMA foreign_keys=ON``;
	without it every foreign key in the schema would be decorative.
	"""

	session.add(
		subroutine.db.models.vocabulary.Tag(
			workspace_id=subroutine.db.types.new_uuid(), name="orphan", name_normalized="orphan"
		)
	)

	with pytest.raises(sqlalchemy.exc.IntegrityError):
		session.flush()


def test_timestamps_are_populated_and_aware (session: sqlalchemy.orm.Session) -> None:
	"""Every mutable row records when it was made, in UTC."""

	workspace = _make_workspace(session)

	assert workspace.created_at.tzinfo is not None
	assert workspace.created_at.utcoffset() == datetime.timedelta(0)
	assert workspace.updated_at is not None
	assert workspace.version == 1
	assert workspace.deleted_at is None
