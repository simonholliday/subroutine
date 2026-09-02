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
	category: str
	is_default: bool = False


@dataclasses.dataclass(frozen=True)
class LinkTypeSeed:
	"""A way two work items can relate, named from both ends."""

	key: str
	title: str
	inverse_title: str
	category: str
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

#: The category on each is decision `#1133`'s table, and it is what a client draws by when it
#: does not recognise the key. Task-side it is Simon's own naming rule of 2026-07-31 with the
#: types collected under each clause: `work` says what will be true when it is done, `defect`
#: says what is wrong, `question` says the question. Document-side: `decision` is something
#: settled including a route closed off, `reference` is how a thing is meant to be, `record` is
#: what was observed.
#:
#: **Every document type here starts *in force* rather than as a draft**, because
#: :data:`subroutine.domain.documents.IN_FORCE_WHEN_WRITTEN` is derived from this tuple. Adding
#: a seventh document type therefore decides that for it — say so there if it should start as a
#: draft instead. A type an installation adds for itself is not covered and falls through to the
#: workspace's own default, which is what keeps ``draft``'s ``is_default`` below meaningful.
ITEM_TYPES = (
	ItemTypeSeed("task", "task", "Task", "work", is_default=True),
	ItemTypeSeed("task", "bug", "Bug", "defect"),
	ItemTypeSeed("task", "feature", "Feature", "work"),
	ItemTypeSeed("task", "chore", "Chore", "work"),
	ItemTypeSeed("task", "spike", "Spike", "question"),
	ItemTypeSeed("document", "note", "Note", "record", is_default=True),
	ItemTypeSeed("document", "spec", "Specification", "reference"),
	ItemTypeSeed("document", "design", "Design", "reference"),
	ItemTypeSeed("document", "decision", "Decision", "decision"),
	ItemTypeSeed("document", "finding", "Finding", "record"),
	ItemTypeSeed("document", "dead_end", "Dead end", "decision"),
)

#: What decision `#1235` adds, at seed version 2 — the first set after the one every workspace
#: was born with.
#:
#: **One row, and it is a *type* rather than an entity.** An event needs a ref, a project,
#: comments, links, a description and a claim, and every one of those is already a task's;
#: building a third kind of item to gain a discriminator a column already provides is this
#: codebase's signature defect at the largest scale available. What makes it an event is
#: ``occasion``, which ``--ready`` and the agenda read.
#:
#: **``occasion`` for the category and ``event`` for the type**, mirroring ``defect``/``bug``:
#: the category is the abstract kind and the type is the word people use. It leaves a workspace
#: room to add ``holiday`` or ``freeze`` under the same category once `#1129` lands, and each of
#: those inherits the behaviour without another release.
EVENT_TYPES = (
	ItemTypeSeed("task", "event", "Event", "occasion"),
)

#: ``derives_from`` is the one that earns its place twice over: it is how the tasks
#: implementing a specification point back at it, and how a bug points back at the failing
#: check that found it (docs/design.md §5.7).
#: The category on each is decision `#1157`'s table, and it is what every rule about a relation
#: reads — never the key, which a workspace may rename. **Nothing is seeded `ordering`**: that is
#: what a workspace's own *precedes* would be, asserting a sequence that holds nothing up, and
#: whether every workspace should be given one is `#1151`.
LINK_TYPES = (
	LinkTypeSeed("blocks", "Blocks", "Blocked by", "gating"),
	LinkTypeSeed("relates_to", "Relates to", "Relates to", "describing", is_symmetric=True),
	LinkTypeSeed("duplicates", "Duplicates", "Duplicated by", "describing"),
	LinkTypeSeed("derives_from", "Derives from", "Derived into", "governing"),
	LinkTypeSeed("documents", "Documents", "Documented by", "governing"),
)

#: What a task says when the work is still wanted and has moved somewhere else — `#1685`.
#:
#: **Category ``cancelled``, and that is the load-bearing part.** §10.7's invariant 5 makes
#: ``completed_at`` non-null exactly for the ``done`` and ``cancelled`` categories, and
#: ``readiness.unblocked`` reads *finished* off that column rather than off the vocabulary. So a
#: superseded task stops blocking its successor — which is the ordinary case, since the
#: successor is usually the thing that superseded it.
#:
#: **It exists because ``cancelled`` says something false.** Cancelled means somebody decided
#: not to do this; superseded means it is being done somewhere else. Simon met the difference on
#: `#589`, which was absorbed into a milestone and had to be marked as abandoned to stop holding
#: that milestone up.
SUPERSEDED_STATUS = (
	StatusSeed("task", "superseded", "Superseded", "cancelled"),
)

#: How an item says what replaced it, and what it replaced — `#1685`, `#1688`.
#:
#: **``governing`` rather than ``describing``, which is a decision about where it sorts.**
#: `#1157` §2's categories are nested by how much a relation binds and `#1535` orders an item's
#: links by exactly that, so on a superseded item this prints above `Relates to` and
#: `Duplicates`. On a dead item the line saying where the work went is the one worth reading
#: first, and `describing` would have put it last. It is defensible on the merits too: the
#: successor governs what happens to the work.
#:
#: **It is not the browser's *Read first*, and nothing may imply it is.** That section is
#: documents-only — ``links.governing`` filters to governing *document* types that are in force
#: — so a task superseded by a task does not appear there.
#:
#: **Nothing here moves a status**, and since `SR#1684` nothing anywhere does. `documents.update`
#: used to, as a side effect of a ``supersedes`` column that no surface rendered; `#1685`
#: deliberately did not repeat that for the link — a link that rewrites another row is unlike
#: every other link type, and unlinking would then have to guess at reversing it — and the
#: column was retired rather than kept beside it. So a person moves the status, on purpose.
SUPERSEDES_LINK = (
	LinkTypeSeed("supersedes", "Supersedes", "Superseded by", "governing"),
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
		item_types=ITEM_TYPES,
		link_types=LINK_TYPES,
	),
	# **The first set added after release, and the machinery above is what it proves.** A
	# workspace already at 1 applies this and nothing else; one created after it applies both,
	# in order. Decision `#1235`.
	#
	# **A workspace that predates the migration carrying this is at version 1 and has the row
	# anyway**, because the migration inserts it directly — the seeder is idempotent by key, so
	# a later call finds it present, writes nothing and moves the number up. Doing it in the
	# migration rather than by rewriting a JSON settings blob on two backends is the cheaper
	# half of the same outcome.
	2: SeedSet(item_types=EVENT_TYPES),
	# **Decision `#1685`, built by `#1688`.** Two rows saying one thing from two directions:
	# the link says *where the work went*, and the status says *not here any more*. Neither
	# writes the other, which is `#84`'s rule — a status inferred from a graph edge is a write
	# nobody made.
	3: SeedSet(statuses=SUPERSEDED_STATUS, link_types=SUPERSEDES_LINK),
}

#: The version a freshly seeded workspace ends up at. Derived rather than declared, so
#: adding a seed set cannot be half-done.
SEED_VERSION = max(SEED_SETS)

#: Every item type a fully seeded workspace ends up with, in the order the sets apply.
#:
#: **Derived, because :data:`ITEM_TYPES` stopped being the answer the moment there were two
#: sets.** Three guards read *the seeds* to ask what every seeded type must have — a glyph in
#: the browser, a mention in ``--help``, a category the backfill agrees about — and all three
#: read set 1 by name. A type added at version 2 would have escaped every one of them in
#: silence, which is a guard reading a list that no longer means what its name says.
SEEDED_ITEM_TYPES: tuple[ItemTypeSeed, ...] = tuple(
	seed for version in sorted(SEED_SETS) for seed in SEED_SETS[version].item_types
)

#: Every link type a fully seeded workspace ends up with, in the order the sets apply.
#:
#: **The same derivation as :data:`SEEDED_ITEM_TYPES`, and for the same reason arriving a second
#: time** (`#1688`). :data:`LINK_TYPES` stopped being the answer the moment a set added one, and
#: four guards read it by name — the backfill check, the compatibility map, the terminal's help
#: and the tool schemas — each asking *what does a seeded workspace have* and being answered
#: *what set 1 has*. Every one of them went quietly wrong on the day `supersedes` was seeded.
SEEDED_LINK_TYPES: tuple[LinkTypeSeed, ...] = tuple(
	seed for version in sorted(SEED_SETS) for seed in SEED_SETS[version].link_types
)

#: Every status a fully seeded workspace ends up with, in the order the sets apply.
#:
#: Derived for the reason above, before anything reads ``_STATUSES`` and means this.
SEEDED_STATUSES: tuple[StatusSeed, ...] = tuple(
	seed for version in sorted(SEED_SETS) for seed in SEED_SETS[version].statuses
)

def named_types (entity_type: str) -> str:
	"""Return one kind of item's seeded types, as prose a help text can carry — `#1240`.

	**Built rather than typed out.** The same list was written by hand in six places — twice in
	``--help``, twice in a tool schema and twice in a model docstring — and every one of them
	had to be found and edited the day decision `#1235` seeded ``event``. A list somebody has to
	remember to update is one that is wrong between the change and the day it is noticed, and
	here *wrong* means withholding a capability and offering ones that may not exist.

	**This is not the whole answer and the item says so.** A derived list still names only what
	the *seeds* contain, so a type a workspace added or renamed itself (`#1129`) is still absent
	from ``--help``. The complete answer reads the live vocabulary the way the browser already
	does, through ``/v1/meta``. What this removes is the copies; what is left is one known gap
	instead of six unknown ones.

	Read from :data:`SEEDED_ITEM_TYPES` rather than :data:`ITEM_TYPES`, which stopped being the
	answer the moment there were two seed sets — the same trap three guards fell into.
	"""

	return ", ".join(
		seed.key for seed in SEEDED_ITEM_TYPES if seed.entity_type == entity_type
	)


def named_link_types () -> str:
	"""Return the seeded ways two items can relate, as prose a help text can carry — `#1688`.

	**Built rather than typed out**, which is :func:`named_types`' argument one vocabulary along
	and it had already gone wrong here: ``subroutine link``'s ``relation`` argument listed five
	relations by hand, and the day a sixth was seeded the help offered a set the product did not
	have. A guard caught it, which is the only reason this is a helper rather than a sixth
	hand-written list.

	**Hyphens rather than underscores**, because that is what somebody types at a terminal —
	both spellings are accepted (`#1619`) and this offers the one the rest of the CLI uses.

	The same known gap as :func:`named_types`: this names what the *seeds* hold, so a relation a
	workspace added itself is absent. The complete answer is the live vocabulary, which
	``/v1/meta`` publishes and the browser reads.
	"""

	return ", ".join(seed.key.replace("_", "-") for seed in SEEDED_LINK_TYPES)


def default_type (entity_type: str) -> str:
	"""Return the type something gets when nobody says — the other half of the same sentence.

	Beside :func:`named_types` because a help text that lists the vocabulary almost always goes
	on to name the default, and two derived halves beside one hand-written one is the seam this
	is closing.
	"""

	return next(
		seed.key
		for seed in SEEDED_ITEM_TYPES
		if seed.entity_type == entity_type and seed.is_default
	)


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
				category=seed.category,
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
				category=seed.category,
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
