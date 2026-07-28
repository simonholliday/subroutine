"""Tests for the vocabulary a workspace starts with, and for how it changes on upgrade.

The interesting cases are not "did the rows appear". They are the three ways a versioned
seed routine goes wrong in the field: it overwrites something an installation renamed, it
resurrects something an installation deleted, or it silently fails to apply a new row
because the version bookkeeping did not survive a JSON column.
"""

import typing
import uuid

import pytest
import sqlalchemy
import sqlalchemy.orm

import subroutine.db.mixins
import subroutine.db.models.identity
import subroutine.db.models.vocabulary
import subroutine.db.seed
import subroutine.permissions


def _make_workspace (session: sqlalchemy.orm.Session) -> subroutine.db.models.identity.Workspace:
	"""Create an unseeded workspace."""

	workspace = subroutine.db.models.identity.Workspace(
		slug=f"ws-{uuid.uuid4().hex[:8]}", title="Test workspace"
	)
	session.add(workspace)
	session.flush()

	return workspace


def _count (
	session: sqlalchemy.orm.Session, model: typing.Any, workspace_id: uuid.UUID
) -> int:
	"""Return how many rows of one kind belong to a workspace."""

	statement = (
		sqlalchemy.select(sqlalchemy.func.count())
		.select_from(model)
		.where(model.workspace_id == workspace_id)
	)

	return session.scalar(statement) or 0


def _statuses (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, entity_type: str
) -> list[subroutine.db.models.vocabulary.Status]:
	"""Return one entity type's statuses, in display order."""

	model = subroutine.db.models.vocabulary.Status
	statement = (
		sqlalchemy.select(model)
		.where(model.workspace_id == workspace_id, model.entity_type == entity_type)
		.order_by(model.position)
	)

	return list(session.scalars(statement))


def test_a_fresh_workspace_gets_a_complete_vocabulary (session: sqlalchemy.orm.Session) -> None:
	"""Seeding an empty workspace writes every role, status, type and link type."""

	workspace = _make_workspace(session)

	report = subroutine.db.seed.seed_workspace(session, workspace)

	assert report.roles == 5
	assert report.statuses == 14
	assert report.item_types == 11
	assert report.link_types == 5
	assert report.from_version == 0
	assert report.to_version == subroutine.db.seed.SEED_VERSION

	task_keys = {status.key for status in _statuses(session, workspace.id, "task")}
	assert task_keys == {"open", "in_progress", "blocked", "needs_input", "done", "cancelled"}

	link_keys = set(
		session.scalars(
			sqlalchemy.select(subroutine.db.models.vocabulary.LinkType.key).where(
				subroutine.db.models.vocabulary.LinkType.workspace_id == workspace.id
			)
		)
	)
	assert link_keys == {"blocks", "relates_to", "duplicates", "derives_from", "documents"}


def test_seeding_twice_changes_nothing (session: sqlalchemy.orm.Session) -> None:
	"""The second run writes no rows and does not touch the workspace itself."""

	workspace = _make_workspace(session)
	subroutine.db.seed.seed_workspace(session, workspace)

	before = {
		model.__name__: _count(session, model, workspace.id)
		for model in (
			subroutine.db.models.identity.Role,
			subroutine.db.models.vocabulary.Status,
			subroutine.db.models.vocabulary.ItemType,
			subroutine.db.models.vocabulary.LinkType,
		)
	}
	touched_at = workspace.updated_at

	report = subroutine.db.seed.seed_workspace(session, workspace)

	assert report.total == 0
	assert report.from_version == subroutine.db.seed.SEED_VERSION

	after = {
		model.__name__: _count(session, model, workspace.id)
		for model in (
			subroutine.db.models.identity.Role,
			subroutine.db.models.vocabulary.Status,
			subroutine.db.models.vocabulary.ItemType,
			subroutine.db.models.vocabulary.LinkType,
		)
	}
	assert after == before

	# An unconditional rewrite of `settings` would move this even though nothing changed.
	assert workspace.updated_at == touched_at


def test_the_applied_version_is_recorded (session: sqlalchemy.orm.Session) -> None:
	"""The workspace remembers how far it has been seeded, through a JSON column."""

	workspace = _make_workspace(session)
	subroutine.db.seed.seed_workspace(session, workspace)

	session.expire(workspace)

	assert workspace.settings[subroutine.db.seed.SEED_VERSION_KEY] == subroutine.db.seed.SEED_VERSION


def test_exactly_one_default_per_entity_type (session: sqlalchemy.orm.Session) -> None:
	"""Every kind of thing has one status and one type it is given when unspecified."""

	workspace = _make_workspace(session)
	subroutine.db.seed.seed_workspace(session, workspace)

	for entity_type in subroutine.db.mixins.STATUS_ENTITY_TYPES:
		defaults = [
			status.key
			for status in _statuses(session, workspace.id, entity_type)
			if status.is_default
		]

		assert len(defaults) == 1, f"{entity_type} statuses have defaults {defaults}"

	for entity_type in subroutine.db.mixins.ITEM_ENTITY_TYPES:
		model = subroutine.db.models.vocabulary.ItemType
		defaults = list(
			session.scalars(
				sqlalchemy.select(model.key).where(
					model.workspace_id == workspace.id,
					model.entity_type == entity_type,
					model.is_default.is_(True),
				)
			)
		)

		assert len(defaults) == 1, f"{entity_type} types have defaults {defaults}"


def test_status_categories_suit_their_entity_type (session: sqlalchemy.orm.Session) -> None:
	"""Tasks and projects use the task categories; documents use their own."""

	workspace = _make_workspace(session)
	subroutine.db.seed.seed_workspace(session, workspace)

	for entity_type in ("task", "project"):
		for status in _statuses(session, workspace.id, entity_type):
			assert status.category in subroutine.db.mixins.TASK_STATUS_CATEGORIES

	for status in _statuses(session, workspace.id, "document"):
		assert status.category in subroutine.db.mixins.DOCUMENT_STATUS_CATEGORIES


def test_positions_are_distinct_and_ordered (session: sqlalchemy.orm.Session) -> None:
	"""Statuses come back in the order they were seeded, with room to insert between."""

	workspace = _make_workspace(session)
	subroutine.db.seed.seed_workspace(session, workspace)

	positions = [status.position for status in _statuses(session, workspace.id, "task")]

	assert positions == sorted(positions)
	assert len(set(positions)) == len(positions)
	assert positions[0] == subroutine.db.mixins.POSITION_GAP


def test_every_seeded_permission_is_recognised (session: sqlalchemy.orm.Session) -> None:
	"""No role is granted a permission the authorisation layer has never heard of."""

	workspace = _make_workspace(session)
	subroutine.db.seed.seed_workspace(session, workspace)

	roles = list(
		session.scalars(
			sqlalchemy.select(subroutine.db.models.identity.Role).where(
				subroutine.db.models.identity.Role.workspace_id == workspace.id
			)
		)
	)

	for role in roles:
		assert subroutine.permissions.unknown(role.permissions) == ()


def test_the_role_ladder_narrows_at_every_step (session: sqlalchemy.orm.Session) -> None:
	"""Each seeded role is a strict subset of the one above it."""

	workspace = _make_workspace(session)
	subroutine.db.seed.seed_workspace(session, workspace)

	granted = {
		role.key: set(role.permissions)
		for role in session.scalars(
			sqlalchemy.select(subroutine.db.models.identity.Role).where(
				subroutine.db.models.identity.Role.workspace_id == workspace.id
			)
		)
	}

	assert granted["owner"] == set(subroutine.permissions.ALL)

	# The whole difference between an owner and an admin, per SPEC.md §7.2.
	assert granted["owner"] - granted["admin"] == {subroutine.permissions.WORKSPACE_DELETE}

	assert granted["viewer"] < granted["contributor"] < granted["member"] < granted["admin"]

	for permission in granted["viewer"]:
		assert permission.endswith(":read")


def test_workspaces_do_not_share_a_vocabulary (session: sqlalchemy.orm.Session) -> None:
	"""Two workspaces get their own rows, and the same keys do not collide."""

	first = _make_workspace(session)
	second = _make_workspace(session)

	subroutine.db.seed.seed_workspace(session, first)
	subroutine.db.seed.seed_workspace(session, second)

	model = subroutine.db.models.vocabulary.Status

	assert _count(session, model, first.id) == _count(session, model, second.id) == 14

	first_open = session.scalars(
		sqlalchemy.select(model).where(
			model.workspace_id == first.id, model.entity_type == "task", model.key == "open"
		)
	).one()
	second_open = session.scalars(
		sqlalchemy.select(model).where(
			model.workspace_id == second.id, model.entity_type == "task", model.key == "open"
		)
	).one()

	assert first_open.id != second_open.id


def test_a_local_rename_survives_reseeding (session: sqlalchemy.orm.Session) -> None:
	"""An installation's own wording is never overwritten by ours."""

	workspace = _make_workspace(session)
	subroutine.db.seed.seed_workspace(session, workspace)

	model = subroutine.db.models.vocabulary.Status
	blocked = session.scalars(
		sqlalchemy.select(model).where(
			model.workspace_id == workspace.id, model.entity_type == "task", model.key == "blocked"
		)
	).one()
	blocked.label = "Stuck"
	blocked.colour = "#000000"
	session.flush()

	subroutine.db.seed.seed_workspace(session, workspace)
	session.expire(blocked)

	assert blocked.label == "Stuck"
	assert blocked.colour == "#000000"


def test_a_deleted_row_is_not_resurrected (session: sqlalchemy.orm.Session) -> None:
	"""Removing a status we shipped is a decision, and reseeding respects it."""

	workspace = _make_workspace(session)
	subroutine.db.seed.seed_workspace(session, workspace)

	model = subroutine.db.models.vocabulary.Status
	spike = session.scalars(
		sqlalchemy.select(model).where(
			model.workspace_id == workspace.id,
			model.entity_type == "task",
			model.key == "needs_input",
		)
	).one()
	session.delete(spike)
	session.flush()

	report = subroutine.db.seed.seed_workspace(session, workspace)

	assert report.total == 0
	assert {status.key for status in _statuses(session, workspace.id, "task")} == {
		"open",
		"in_progress",
		"blocked",
		"done",
		"cancelled",
	}


def test_a_later_version_adds_only_its_own_rows (
	session: sqlalchemy.orm.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""An upgrade applies the new set and leaves every earlier decision alone."""

	workspace = _make_workspace(session)
	subroutine.db.seed.seed_workspace(session, workspace)

	model = subroutine.db.models.vocabulary.Status
	cancelled = session.scalars(
		sqlalchemy.select(model).where(
			model.workspace_id == workspace.id,
			model.entity_type == "task",
			model.key == "cancelled",
		)
	).one()
	session.delete(cancelled)
	session.flush()

	monkeypatch.setitem(
		subroutine.db.seed.SEED_SETS,
		2,
		subroutine.db.seed.SeedSet(
			statuses=(
				subroutine.db.seed.StatusSeed("task", "on_ice", "On ice", "todo", "#0891b2"),
			),
			link_types=(subroutine.db.seed.LinkTypeSeed("supersedes", "Supersedes", "Superseded by"),),
		),
	)
	monkeypatch.setattr(subroutine.db.seed, "SEED_VERSION", 2)

	report = subroutine.db.seed.seed_workspace(session, workspace)

	assert report.statuses == 1
	assert report.link_types == 1
	assert report.roles == 0
	assert report.item_types == 0
	assert report.from_version == 1
	assert report.to_version == 2

	task_statuses = _statuses(session, workspace.id, "task")
	keys = [status.key for status in task_statuses]

	assert "on_ice" in keys
	assert "cancelled" not in keys

	# The new row is appended, not interleaved, so a reordered list stays reordered.
	assert keys[-1] == "on_ice"
	assert task_statuses[-1].position > task_statuses[-2].position


def test_a_corrupt_version_is_treated_as_unseeded (session: sqlalchemy.orm.Session) -> None:
	"""Nonsense in the settings blob costs a wasted pass, never a broken workspace."""

	workspace = _make_workspace(session)
	subroutine.db.seed.seed_workspace(session, workspace)

	workspace.settings = {subroutine.db.seed.SEED_VERSION_KEY: "banana"}
	session.flush()

	report = subroutine.db.seed.seed_workspace(session, workspace)

	assert report.from_version == 0
	assert report.total == 0
	assert _count(session, subroutine.db.models.vocabulary.Status, workspace.id) == 14


def test_every_permission_constant_is_listed () -> None:
	"""``ALL`` cannot fall behind the constants above it."""

	declared = {
		value
		for name, value in vars(subroutine.permissions).items()
		if name.isupper() and isinstance(value, str)
	}

	assert declared == set(subroutine.permissions.ALL)


def test_unknown_permissions_are_reported_verbatim () -> None:
	"""The caller gets its own spelling back, so the error can quote it."""

	assert subroutine.permissions.unknown(["task:read", "task:teleport", "TASK:READ"]) == (
		"task:teleport",
		"TASK:READ",
	)
