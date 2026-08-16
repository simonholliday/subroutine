"""The one place a listing is narrowed to what its caller may see.

SPEC.md §7.3 has asked for this since slice 1 — "every repository query is scoped by
workspace through a single injected helper" — and until now nothing built it, so every
listing narrowed itself by hand. Two of them got it wrong, and both were found by running
the code rather than reading it:

* ``subroutine ls`` filtered by workspace and never joined the project at all, so a member
  of a workspace saw the titles of tasks in private projects they were not a member of.
  The agenda, three modules away, did it correctly.
* the agenda narrowed by project *visibility* but not by a token's ``project_scope``, so an
  agent restricted to one project was correctly refused writes elsewhere and then shown
  everything anyway. §7.3 calls ``project_scope`` a restriction on *which rows*, which is
  precisely the thing a listing decides.

Neither is a mistake anybody would make twice; both are mistakes anybody would make once,
which is why the answer is one function rather than more care. Everything that lists tasks
or projects starts from here, and ``tests/test_scoping.py`` fails the build when a query
elsewhere reaches those tables without a written reason.

**A superuser is narrowed like everyone else.** Roles are bypassed for superusers (§7.3);
visibility is not. A privacy control a role can override is not a privacy control, and an
operator with database access has honest ways to read the data.
"""

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.hierarchy
import subroutine.permissions


def refuse_a_read_out_of_scope (
	principal: subroutine.domain.authentication.Principal, permission: str
) -> None:
	"""Refuse a listing the credential's own scopes do not reach (`#930`).

	**A read verb gated nothing until this existed.** ``task:read``, ``project:read`` and
	``workspace:read`` appeared in no check anywhere, so a token issued ``--scope task:delete``
	read every task, document, agenda and change feed it could reach, while ``/v1/me``
	reported the one permission it had. Every read-narrowed credential was wider than it was
	issued (`#927` H-2).

	**Here rather than at the callers**, of which there are ten: this module is already the
	one place a listing is narrowed, and ``tests/test_scoping.py`` fails the build when a
	query reaches those tables from anywhere else. A check spread over the call sites is a
	list, and a list falls behind.

	**Scopes only, and that is complete rather than partial.** The other half of
	``role ∩ scopes`` is the role, and every seeded role carries every read verb — measured,
	all five — while `#826` records that no installation can add one. So for reads the role
	half is vacuous by construction, and the credential's own narrowing is the only thing that
	can decide. It is also the half that needs no session, which is what lets the check live
	in a query builder at all.

	**It refuses rather than narrowing to nothing.** An empty page is a plausible, complete,
	wrong answer to *may I read this*, and the operator's remedy is the refusal.
	"""

	if not subroutine.domain.authorization.outside_token_scope(principal, permission):
		return

	raise subroutine.domain.authorization.AuthorizationError(
		subroutine.domain.authorization.AuthorizationFailure.OUT_OF_TOKEN_SCOPE,
		permission=permission,
	)


def within_project_scope (
	principal: subroutine.domain.authentication.Principal,
) -> sqlalchemy.ColumnElement[bool]:
	"""Return a predicate selecting the projects a credential's scope admits.

	A scoped project brings its whole subtree, for the reason
	:func:`subroutine.domain.authorization._within_project_scope` gives about the
	single-project form: restricting an agent to a project and then refusing it the
	sub-projects underneath makes the restriction useless below one level. This is the
	same rule as a predicate, so a listing can apply it to rows it has not loaded.

	**``None`` and ``[]`` are different things and neither is a guess** (`#201`). ``None`` is
	the sentinel and narrows nothing; an empty *list* is a restriction that admits no project,
	which is what its counterpart in ``authorization`` has always said. This built
	``sqlalchemy.or_()`` with no clauses for it — an empty ``BooleanClauseList``, which
	renders as nothing at all, so the ``WHERE`` lost the restriction entirely and the listing
	returned every project. Two copies of one rule disagreeing on one edge, in opposite
	directions, with the query side failing **open**.

	Unreachable today: ``_canonical_project_scope`` refuses an empty list at issue time and is
	the only writer. That is what makes this a tripwire rather than a live defect — a restore
	of hand-edited data, an importer, or an endpoint that learns to write ``project_scope``
	would all land on it, and a security control that fails open is not one to leave resting
	on a validator two modules away.
	"""

	allowed = principal.project_scope

	# The sentinel: no list means no restriction, never "no projects" (SPEC.md §7.3).
	if allowed is None:
		return sqlalchemy.true()

	if not allowed:
		return sqlalchemy.false()

	project = subroutine.db.models.project.Project

	# A project's `path` contains its *own* id as well as every ancestor's — measured, not
	# assumed: a root's path is `/<its own id>/`. So one substring test covers both "this
	# is the scoped project" and "this is underneath it", and no separate check on `id` is
	# needed. Never a range comparison, which is wrong under a non-byte-wise collation.
	return sqlalchemy.or_(
		*[project.path.contains(f"/{identifier}/") for identifier in allowed]
	)


def within_project (
	project: subroutine.db.models.project.Project,
) -> sqlalchemy.ColumnElement[bool]:
	"""Return a predicate selecting one project **and everything filed underneath it** (`#320`).

	**A named project means that area of work, not that one node.** Every listing that took a
	``project`` compared ``project_id`` to a single id, so a parent's listing excluded its own
	children: ``project list`` drew a tree and ``list --project PARENT`` returned only what was
	filed directly in it. A hierarchy whose parent answers for none of its contents is a
	decoration, and it is precisely the thing sub-projects are for.

	**The rule already existed one function away, saying the opposite.**
	:func:`within_project_scope` narrows a *credential* by subtree, and argues it in writing —
	"restricting an agent to a project and then refusing it the sub-projects underneath makes
	the restriction useless below one level". That argument is unchanged for a listing. Two
	copies of one rule disagreeing is this codebase's signature defect, and this was a fresh
	instance of it.

	Through :func:`subroutine.domain.hierarchy.subtree`, so the ``LIKE``-not-a-range decision is
	made once: a half-open range over ``path`` silently drops descendants under a non-byte-wise
	collation, correctly on SQLite and wrongly on PostgreSQL.

	**Every caller must already have joined ``project``**, which the three ``readable_*``
	statements do — the predicate is over the project's path, not the item's.
	"""

	return subroutine.domain.hierarchy.subtree(
		subroutine.db.models.project.Project, project
	)


def readable_projects (
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	include_deleted: bool = False,
	include_archived: bool = False,
	enforce_read_scope: bool = True,
) -> sqlalchemy.Select[tuple[subroutine.db.models.project.Project]]:
	"""Return a select over the projects this principal may see, and no others.

	``workspace_ids`` is required and is never allowed to be empty-meaning-all: a listing
	that quietly spans every workspace when handed an empty list is one refactor away from
	spanning every workspace belonging to everybody.

	``include_deleted`` did not exist until `#307`, and its absence was invisible because
	every other parameter here has one: :func:`visible_events` asked for archived and template
	rows, could not ask for deleted projects, and so reported nothing when one was thrown
	away. A list of ``include_`` flags reads as considered; the one that is missing reads as
	nothing at all.
	"""

	# **Opt-out rather than opt-in, so forgetting it is safe.** The single caller that turns it
	# off is `projects.keys_for`, which resolves ids out of the caller's own token for display
	# and is named in that function's own comment (`#930`).
	if enforce_read_scope:
		refuse_a_read_out_of_scope(principal, subroutine.permissions.PROJECT_READ)

	project = subroutine.db.models.project.Project

	statement = sqlalchemy.select(project).where(
		project.workspace_id.in_(workspace_ids),
		subroutine.domain.authorization.visible_projects(principal),
		within_project_scope(principal),
	)

	if not include_deleted:
		statement = statement.where(project.deleted_at.is_(None))

	if not include_archived:
		statement = statement.where(project.archived_at.is_(None))

	return statement


def readable_documents (
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	include_deleted: bool = False,
	include_deleted_projects: bool = False,
	include_archived: bool = False,
) -> sqlalchemy.Select[tuple[subroutine.db.models.work.Document]]:
	"""Return a select over the documents this principal may see, and no others.

	The same narrowing as :func:`readable_tasks`, because a document is a work item under
	the same permissions as the task beside it (SPEC.md §5.6, §7.3a) — a specification in a
	private project is exactly as hidden as the work derived from it, and it would be an odd
	kind of privacy if it were not.

	The two deletion flags are separate axes and must stay so (`#307`). ``include_deleted`` is
	about *this row*; ``include_deleted_projects`` is about its container, which ``projects.
	delete`` deliberately does not touch — a deleted project's documents are hidden by this
	join rather than thrown away, so folding the two together would put a document nobody
	deleted into the trash listing.
	"""

	# A document is a work item under a task's permissions, so it is `task:read` that reaches
	# one and there is no `document:read` to hold it to (§7.3a, `permissions.COVERAGE`).
	refuse_a_read_out_of_scope(principal, subroutine.permissions.TASK_READ)

	document = subroutine.db.models.work.Document
	project = subroutine.db.models.project.Project

	statement = (
		sqlalchemy.select(document)
		.join(project, project.id == document.project_id)
		.where(
			document.workspace_id.in_(workspace_ids),
			subroutine.domain.authorization.visible_projects(principal),
			within_project_scope(principal),
		)
	)

	if not include_deleted_projects:
		statement = statement.where(project.deleted_at.is_(None))

	if not include_deleted:
		statement = statement.where(document.deleted_at.is_(None))

	if not include_archived:
		statement = statement.where(document.archived_at.is_(None))

	return statement


def readable_tasks (
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	include_deleted: bool = False,
	include_deleted_projects: bool = False,
	include_completed: bool = True,
	include_archived: bool = False,
	include_templates: bool = False,
) -> sqlalchemy.Select[tuple[subroutine.db.models.work.Task]]:
	"""Return a select over the tasks this principal may see, and no others.

	The join to ``project`` is what makes the visibility rules expressible at all, and is
	the step ``subroutine ls`` was missing. The defaults describe an ordinary listing:
	nothing deleted, nothing archived, and no recurrence templates, which are machinery
	rather than work (§6.7).

	``include_deleted`` and ``include_deleted_projects`` are separate axes and must stay so
	(`#307`). The first is about *this row*; the second is about its container, which
	``projects.delete`` deliberately leaves alone — "its tasks are not touched … they leave the
	visible world with it and come back with it". One flag for both would put a task nobody
	deleted into the trash.
	"""

	refuse_a_read_out_of_scope(principal, subroutine.permissions.TASK_READ)

	task = subroutine.db.models.work.Task
	project = subroutine.db.models.project.Project

	statement = (
		sqlalchemy.select(task)
		.join(project, project.id == task.project_id)
		.where(
			task.workspace_id.in_(workspace_ids),
			subroutine.domain.authorization.visible_projects(principal),
			within_project_scope(principal),
		)
	)

	if not include_deleted_projects:
		statement = statement.where(project.deleted_at.is_(None))

	if not include_deleted:
		statement = statement.where(task.deleted_at.is_(None))

	if not include_completed:
		# `completed_at` is non-null exactly when the status category is done or cancelled
		# (invariant 5), so this needs no join to the status table.
		statement = statement.where(task.completed_at.is_(None))

	if not include_archived:
		statement = statement.where(task.archived_at.is_(None))

	if not include_templates:
		statement = statement.where(task.is_template.is_(False))

	return statement


def the_other_kind (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_id: uuid.UUID,
	ref: int | None,
	asked_for: str,
) -> subroutine.db.models.work.Task | subroutine.db.models.work.Document | None:
	"""Return the item of the *other* kind answering to this ref, so a refusal can name it.

	One counter per workspace serves tasks and documents (§6.2), so a ref that names no task
	may name a document perfectly well — and *"there is no task 480"* about something the
	caller has just read in a listing is a refusal asserting a cause it has not established.
	§12.2c settled this for the command line on 2026-07-30, where ``done 4`` answered "there is
	no task #4" about an item printed directly above it; `#488` is the same defect on the API,
	inherited by every caller over HTTP and by the whole agent surface.

	**Both directions, from one function.** Asking a document endpoint about a task's ref was
	wrong in exactly the same way and had exactly the same message, and two mirror-image
	helpers would be two places for the rule to drift — which is the second-copy defect this
	codebase spends most of its time on.

	Searched through the same scoping the caller's own lookup used, so an item they may not see
	stays absent rather than being named by the refusal. Naming it would turn a helpful message
	into a way of asking whether a private project holds a given number (§7.3a).
	"""

	if ref is None:
		return None

	if asked_for == "task":
		return session.scalars(
			readable_documents(
				principal,
				workspace_ids=[workspace_id],
				include_deleted=True,
				include_archived=True,
			).where(subroutine.db.models.work.Document.ref == ref)
		).first()

	return session.scalars(
		readable_tasks(
			principal,
			workspace_ids=[workspace_id],
			include_deleted=True,
			include_archived=True,
			include_templates=True,
		).where(subroutine.db.models.work.Task.ref == ref)
	).first()


#: Narrowed by the workspace and nothing further, because belonging to the workspace is the
#: whole of the test — there is no project standing between the caller and the fact that
#: somebody was added to one.
_WORKSPACE_LEVEL = ("workspace", "workspace_member")


def visible_events (
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
) -> sqlalchemy.ColumnElement[bool]:
	"""Return a predicate selecting the events this principal may see, and no others.

	**An event is exactly as visible as the entity it describes** (SPEC.md §5.11a). A
	per-entity history gets that free by resolving its subject through the entity's own
	narrowed statement; the change feed has no subject to resolve and must compose the same
	predicates itself. §5.11a predicted that this would be the genuinely shared work, and it
	is why the histories were built first.

	**The three statements are asked for everything they would otherwise hide.** Deleted,
	archived and template rows are all included, **and so are rows whose *project* is deleted**,
	because a *deletion* is the event most worth reporting and the defaults would hide precisely
	it — a feed that goes quiet when something is removed cannot be resumed from. That this is
	expressible at all rests on nothing in this system hard-deleting: the row is still joinable,
	so the feed can report the deletion *and* still check who is entitled to know of it. §5.11a
	names that property as load-bearing and this is the second place to lean on it.

	**The container clause is the one that was missing** (`#307`), and it failed in two
	directions at once. A project's own deletion went unreported, and — because a task is
	reached through a join to its project — deleting a project retroactively removed every
	event about everything inside it, so a client polling afterwards was told those items had
	never existed. Rewriting the past is the one failure §5.11a says a resumable feed cannot
	have; this had it, while the paragraph above claimed it did not.

	**A comment is matched through its subject**, the pair
	:func:`subroutine.domain.events.selected` already joins on: an event whose entity is a
	comment carries the item the comment was written on, so it is visible exactly when that
	item is.

	**A link is matched through its subject too**, since ``#252`` gave link events one: a link
	on a task in a private project is exactly as visible as that task. **What that does not
	check is the far end** (`#302`) — a link's visibility is really the conjunction of two
	items', and one subject can only express one of them, so an event whose source is visible
	reports the *ref* of a target that may not be. A number rather than a title, and a
	workspace's refs are close to guessable anyway, but it is recorded rather than assumed
	away.

	**Everything unlisted is excluded, and the clauses below are the whole of that rule**
	(`#303`). A kind nobody wrote a clause for matches none of them and is invisible — to
	everybody, including whoever caused it. There is deliberately no second declaration of
	the list: ``FEED_ENTITY_TYPES`` used to be one, it was read by nothing, it had been wrong
	once, and a constraint restating these clauses would be one more place for the two to
	disagree.

	``tests/test_events_scoping.py`` is what makes adding a kind a deliberate act. It reads
	the ``entity_type`` out of every call that emits one, requires each to be classified, and
	**measures** the classification against a real feed rather than trusting it. Both this
	docstring and ``api/changes.py`` cited that file for weeks before it existed, which is the
	defect `#303` is.
	"""

	model = subroutine.db.models.activity.Event

	# Read out of the statements every other listing starts from, rather than restated here.
	# A hand-written copy of these predicates is exactly how `ls` and the agenda came to
	# disagree about who may see a private project.
	identifiers = {
		"task": readable_tasks(
			principal,
			workspace_ids=workspace_ids,
			include_deleted=True,
			include_deleted_projects=True,
			include_archived=True,
			include_templates=True,
		).with_only_columns(subroutine.db.models.work.Task.id),
		"project": readable_projects(
			principal,
			workspace_ids=workspace_ids,
			include_deleted=True,
			include_archived=True,
		).with_only_columns(subroutine.db.models.project.Project.id),
		"document": readable_documents(
			principal,
			workspace_ids=workspace_ids,
			include_deleted=True,
			include_deleted_projects=True,
			include_archived=True,
		).with_only_columns(subroutine.db.models.work.Document.id),
	}

	clauses = [
		sqlalchemy.and_(model.entity_type == kind, model.entity_id.in_(rows))
		for kind, rows in identifiers.items()
	]

	# The same three sets answer the comments, because `subject_type` is only ever one of them.
	clauses += [
		sqlalchemy.and_(model.subject_type == kind, model.subject_id.in_(rows))
		for kind, rows in identifiers.items()
	]

	clauses.append(model.entity_type.in_(_WORKSPACE_LEVEL))

	return sqlalchemy.or_(*clauses)
