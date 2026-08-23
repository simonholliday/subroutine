"""The vocabulary every workspace starts with, and the rule for adding to it later.

Seeds are data, not schema, so they are applied by this module rather than by a migration
(docs/design.md §10.8). Two properties follow, and both are deliberate:

* **Seeding is idempotent.** Running it again on a workspace that already has a vocabulary
  changes nothing at all, so ``subroutine init`` and workspace creation can both call it
  without either needing to know whether the other has.
* **Seeding is versioned.** Each release's additions are listed under their own version
  number, and a workspace records how far it has been seeded. An upgrade that adds a
  status therefore adds *only* that status: it does not restore rows an installation
  deleted on purpose, and it never overwrites one that has been renamed.
  Local edits win over ours, without exception.

The second point is the reason this is not a migration. A migration runs once per
database and knows nothing about which of its rows are still wanted.
"""

import dataclasses
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.mixins
import subroutine.db.models.identity
import subroutine.db.models.vocabulary
import subroutine.permissions

#: Where a workspace records how far it has been seeded, inside ``workspace.settings``.
SEED_VERSION_KEY = "seed_version"


@dataclasses.dataclass(frozen=True)
class RoleSeed:
	"""A role every workspace starts with, and the permissions it carries."""

	key: str
	title: str
	description: str
	permissions: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class StatusSeed:
	"""A workflow state for tasks, projects or documents."""

	entity_type: str
	key: str
	label: str
	category: str
	is_default: bool = False


@dataclasses.dataclass(frozen=True)
class ItemTypeSeed:
	"""A kind of task or document."""

	entity_type: str
	key: str
	label: str
	is_default: bool = False


@dataclasses.dataclass(frozen=True)
class LinkTypeSeed:
	"""A way two work items can relate, named from both ends."""

	key: str
	title: str
	inverse_title: str
	is_symmetric: bool = False


@dataclasses.dataclass(frozen=True)
class SeedSet:
	"""Everything one release adds to a workspace's vocabulary."""

	roles: tuple[RoleSeed, ...] = ()
	statuses: tuple[StatusSeed, ...] = ()
	item_types: tuple[ItemTypeSeed, ...] = ()
	link_types: tuple[LinkTypeSeed, ...] = ()


@dataclasses.dataclass(frozen=True)
class SeedReport:
	"""What a run of the seed routine actually wrote.

	``total`` is zero for every run after the first, which is the property the caller —
	and the test suite — cares about.
	"""

	roles: int
	statuses: int
	item_types: int
	link_types: int
	from_version: int
	to_version: int

	@property
	def total (self) -> int:
		"""Return how many rows were written, across every kind."""

		return self.roles + self.statuses + self.item_types + self.link_types


#: The ``viewer`` role's whole grant, and the floor under ``member`` and ``contributor``.
#: Lives in :mod:`subroutine.permissions` because a *credential* can be narrowed to the same
#: set — decision `#370`'s ``observer`` profile — and two copies of "what reading is" would
#: be one more instance of this codebase's signature defect.
_READ_EVERYTHING = tuple(
	subroutine.permissions.sorted_permissions(subroutine.permissions.READS)
)

#: An admin differs from an owner in exactly one thing, and this is it.
_EVERYTHING_BUT_DELETION = subroutine.permissions.WORKSPACE_LEVEL - {
	subroutine.permissions.WORKSPACE_DELETE
}

_SYSTEM_ROLES = (
	RoleSeed(
		key="owner",
		title="Owner",
		description="Full control, including deleting the workspace. Every workspace has at least one.",
		# The top of one workspace, not of the installation: creating a second workspace or
		# a new account needs an instance permission, which no role carries (docs/design.md §7.2).
		permissions=tuple(
			subroutine.permissions.sorted_permissions(subroutine.permissions.WORKSPACE_LEVEL)
		),
	),
	RoleSeed(
		key="admin",
		title="Admin",
		description="Everything an owner can do, except delete the workspace itself.",
		permissions=tuple(subroutine.permissions.sorted_permissions(_EVERYTHING_BUT_DELETION)),
	),
	RoleSeed(
		key="member",
		title="Member",
		description="Creates and changes projects, tasks, comments and tags.",
		permissions=tuple(
			subroutine.permissions.sorted_permissions(
				(
					*_READ_EVERYTHING,
					subroutine.permissions.PROJECT_WRITE,
					subroutine.permissions.TASK_WRITE,
					subroutine.permissions.COMMENT_WRITE,
					subroutine.permissions.TAG_WRITE,
				)
			)
		),
	),
	RoleSeed(
		key="contributor",
		title="Contributor",
		description="Reads everything, and writes tasks and comments. Cannot change projects.",
		permissions=tuple(
			subroutine.permissions.sorted_permissions(
				(
					*_READ_EVERYTHING,
					subroutine.permissions.TASK_WRITE,
					subroutine.permissions.COMMENT_WRITE,
				)
			)
		),
	),
	RoleSeed(
		key="viewer",
		title="Viewer",
		description="Reads everything. Changes nothing.",
		permissions=tuple(subroutine.permissions.sorted_permissions(_READ_EVERYTHING)),
	),
)

#: Task and project statuses map onto the four task categories; documents map onto their
#: own four, because a superseded specification is not "done" (docs/design.md §5.5).
#:
#: **A seeded colour was here and is gone** (`#523`, decision `#906` §7). It claimed every
#: client would agree on what "blocked" looks like; no client ever read one, and `#1023`
#: settled that colour carries the *project* — from a named palette, resolved through the
#: settings registry — so a per-status hex was both unread and the wrong shape. What tells
#: two statuses apart is the `category` beside them, which is fixed, published and load-bearing.
_STATUSES = (
	StatusSeed("task", "open", "Open", "todo", is_default=True),
	StatusSeed("task", "in_progress", "In progress", "in_progress"),
	StatusSeed("task", "blocked", "Blocked", "todo"),
	# **The one seeded key a query reads by name**, because there is no category for it and
	# `#96` refused a fifth: the distinction that matters is *who ends the wait*, and a `blocks`
	# link resolves itself where this needs a person. `#1116` is the agenda bucket it feeds, and
	# a workspace that renames this key has renamed the thing that bucket is about.
	StatusSeed("task", "needs_input", "Needs input", "todo"),
	StatusSeed("task", "done", "Done", "done"),
	StatusSeed("task", "cancelled", "Cancelled", "cancelled"),
	StatusSeed("project", "active", "Active", "in_progress", is_default=True),
	StatusSeed("project", "on_hold", "On hold", "todo"),
	StatusSeed("project", "completed", "Completed", "done"),
	StatusSeed("project", "archived", "Archived", "cancelled"),
	StatusSeed("document", "draft", "Draft", "draft", is_default=True),
	StatusSeed("document", "active", "Active", "current"),
	StatusSeed("document", "superseded", "Superseded", "superseded"),
	StatusSeed("document", "archived", "Archived", "archived"),
)

_ITEM_TYPES = (
	ItemTypeSeed("task", "task", "Task", is_default=True),
	ItemTypeSeed("task", "bug", "Bug"),
	ItemTypeSeed("task", "feature", "Feature"),
	ItemTypeSeed("task", "chore", "Chore"),
	ItemTypeSeed("task", "spike", "Spike"),
	ItemTypeSeed("document", "note", "Note", is_default=True),
	ItemTypeSeed("document", "spec", "Specification"),
	ItemTypeSeed("document", "design", "Design"),
	ItemTypeSeed("document", "decision", "Decision"),
	ItemTypeSeed("document", "finding", "Finding"),
	ItemTypeSeed("document", "dead_end", "Dead end"),
)

#: ``derives_from`` is the one that earns its place twice over: it is how the tasks
#: implementing a specification point back at it, and how a bug points back at the failing
#: check that found it (docs/design.md §5.7).
_LINK_TYPES = (
	LinkTypeSeed("blocks", "Blocks", "Blocked by"),
	LinkTypeSeed("relates_to", "Relates to", "Relates to", is_symmetric=True),
	LinkTypeSeed("duplicates", "Duplicates", "Duplicated by"),
	LinkTypeSeed("derives_from", "Derives from", "Derived into"),
	LinkTypeSeed("documents", "Documents", "Documented by"),
)

#: Keyed by the release that introduced them. To add a status in a later version, add a
#: new key here holding only the new rows — never edit an existing set, because a
#: workspace already past that number will not look at it again, and moving a row between
#: versions would re-offer something an installation has already declined.
#:
#: A new set must not introduce a second default for an entity type that already has one.
SEED_SETS: dict[int, SeedSet] = {
	1: SeedSet(
		roles=_SYSTEM_ROLES,
		statuses=_STATUSES,
		item_types=_ITEM_TYPES,
		link_types=_LINK_TYPES,
	),
}

#: The version a freshly seeded workspace ends up at. Derived rather than declared, so
#: adding a seed set cannot be half-done.
SEED_VERSION = max(SEED_SETS)

#: The two tables whose rows carry a display order, handled together because the rule for
#: where a newly seeded row goes is the same for both.
_PositionedModel: typing.TypeAlias = (
	type[subroutine.db.models.vocabulary.Status] | type[subroutine.db.models.vocabulary.ItemType]
)


def seed_workspace (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
) -> SeedReport:
	"""Give a workspace the vocabulary it needs, and record how far it has been seeded.

	Safe to call on a workspace that already has one: anything whose key is already present
	is left exactly as it is, a local rename included. The caller owns the
	transaction — nothing here commits.
	"""

	# Rows cannot point at a workspace that has no primary key yet, and its settings
	# cannot be read back before its column defaults have been applied.
	session.add(workspace)
	session.flush()

	applied = _applied_version(workspace)

	roles = 0
	statuses = 0
	item_types = 0
	link_types = 0

	for version in sorted(SEED_SETS):
		if version <= applied:
			continue

		seeds = SEED_SETS[version]

		roles += _seed_roles(session, workspace.id, seeds.roles)
		statuses += _seed_statuses(session, workspace.id, seeds.statuses)
		item_types += _seed_item_types(session, workspace.id, seeds.item_types)
		link_types += _seed_link_types(session, workspace.id, seeds.link_types)

	# Only written when it changes. Reassigning an identical value would still mark the
	# row dirty and move `updated_at`, which would make "seeding twice changes nothing"
	# quietly untrue.
	if applied != SEED_VERSION:
		_record_version(workspace, SEED_VERSION)

	session.flush()

	return SeedReport(
		roles=roles,
		statuses=statuses,
		item_types=item_types,
		link_types=link_types,
		from_version=applied,
		to_version=SEED_VERSION,
	)


def _applied_version (workspace: subroutine.db.models.identity.Workspace) -> int:
	"""Return how far this workspace has already been seeded."""

	recorded = workspace.settings.get(SEED_VERSION_KEY, 0)

	# Anything unexpected in the settings blob is read as "nothing has been applied".
	# Starting over is harmless: every seeder skips a row whose key it already finds.
	if isinstance(recorded, bool) or not isinstance(recorded, int) or recorded < 0:
		return 0

	return recorded


def _record_version (workspace: subroutine.db.models.identity.Workspace, version: int) -> None:
	"""Note on the workspace that seeds up to ``version`` have been applied."""

	# Replaced wholesale rather than mutated in place: SQLAlchemy does not watch the
	# inside of a JSON column, so `settings[key] = value` would never reach the database.
	settings = dict(workspace.settings)
	settings[SEED_VERSION_KEY] = version

	workspace.settings = settings


def _seed_roles (
	session: sqlalchemy.orm.Session,
	workspace_id: uuid.UUID,
	seeds: tuple[RoleSeed, ...],
) -> int:
	"""Add the roles this workspace does not have yet, and report how many."""

	if not seeds:
		return 0

	model = subroutine.db.models.identity.Role

	existing = set(
		session.scalars(sqlalchemy.select(model.key).where(model.workspace_id == workspace_id))
	)

	written = 0

	for seed in seeds:
		if seed.key in existing:
			continue

		session.add(
			model(
				workspace_id=workspace_id,
				key=seed.key,
				title=seed.title,
				description=seed.description,
				permissions=list(seed.permissions),
				is_system=True,
			)
		)
		written += 1

	return written


def _seed_statuses (
	session: sqlalchemy.orm.Session,
	workspace_id: uuid.UUID,
	seeds: tuple[StatusSeed, ...],
) -> int:
	"""Add the statuses this workspace does not have yet, and report how many."""

	if not seeds:
		return 0

	model = subroutine.db.models.vocabulary.Status

	existing = {
		tuple(row)
		for row in session.execute(
			sqlalchemy.select(model.entity_type, model.key).where(
				model.workspace_id == workspace_id
			)
		)
	}
	positions = _highest_positions(session, model, workspace_id)

	written = 0

	for seed in seeds:
		if (seed.entity_type, seed.key) in existing:
			continue

		positions[seed.entity_type] = (
			positions.get(seed.entity_type, 0) + subroutine.db.mixins.POSITION_GAP
		)

		session.add(
			model(
				workspace_id=workspace_id,
				entity_type=seed.entity_type,
				key=seed.key,
				label=seed.label,
				category=seed.category,
				position=positions[seed.entity_type],
				is_default=seed.is_default,
			)
		)
		written += 1

	return written


def _seed_item_types (
	session: sqlalchemy.orm.Session,
	workspace_id: uuid.UUID,
	seeds: tuple[ItemTypeSeed, ...],
) -> int:
	"""Add the item types this workspace does not have yet, and report how many."""

	if not seeds:
		return 0

	model = subroutine.db.models.vocabulary.ItemType

	existing = {
		tuple(row)
		for row in session.execute(
			sqlalchemy.select(model.entity_type, model.key).where(
				model.workspace_id == workspace_id
			)
		)
	}
	positions = _highest_positions(session, model, workspace_id)

	written = 0

	for seed in seeds:
		if (seed.entity_type, seed.key) in existing:
			continue

		positions[seed.entity_type] = (
			positions.get(seed.entity_type, 0) + subroutine.db.mixins.POSITION_GAP
		)

		session.add(
			model(
				workspace_id=workspace_id,
				entity_type=seed.entity_type,
				key=seed.key,
				label=seed.label,
				position=positions[seed.entity_type],
				is_default=seed.is_default,
				is_system=True,
			)
		)
		written += 1

	return written


def _seed_link_types (
	session: sqlalchemy.orm.Session,
	workspace_id: uuid.UUID,
	seeds: tuple[LinkTypeSeed, ...],
) -> int:
	"""Add the link types this workspace does not have yet, and report how many."""

	if not seeds:
		return 0

	model = subroutine.db.models.vocabulary.LinkType

	existing = set(
		session.scalars(sqlalchemy.select(model.key).where(model.workspace_id == workspace_id))
	)

	written = 0

	for seed in seeds:
		if seed.key in existing:
			continue

		session.add(
			model(
				workspace_id=workspace_id,
				key=seed.key,
				title=seed.title,
				inverse_title=seed.inverse_title,
				is_symmetric=seed.is_symmetric,
				is_system=True,
			)
		)
		written += 1

	return written


def _highest_positions (
	session: sqlalchemy.orm.Session,
	model: _PositionedModel,
	workspace_id: uuid.UUID,
) -> dict[str, int]:
	"""Return the highest display position in use, per entity type.

	Newly seeded rows go after everything already there, so a later release appends to a
	list an installation has reordered rather than interleaving itself through it.
	"""

	statement = (
		sqlalchemy.select(model.entity_type, sqlalchemy.func.max(model.position))
		.where(model.workspace_id == workspace_id)
		.group_by(model.entity_type)
	)

	return dict(session.execute(statement).tuples().all())
