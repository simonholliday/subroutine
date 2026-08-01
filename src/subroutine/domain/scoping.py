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

import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.authorization


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


def readable_projects (
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	include_archived: bool = False,
) -> sqlalchemy.Select[tuple[subroutine.db.models.project.Project]]:
	"""Return a select over the projects this principal may see, and no others.

	``workspace_ids`` is required and is never allowed to be empty-meaning-all: a listing
	that quietly spans every workspace when handed an empty list is one refactor away from
	spanning every workspace belonging to everybody.
	"""

	project = subroutine.db.models.project.Project

	statement = sqlalchemy.select(project).where(
		project.workspace_id.in_(workspace_ids),
		project.deleted_at.is_(None),
		subroutine.domain.authorization.visible_projects(principal),
		within_project_scope(principal),
	)

	if not include_archived:
		statement = statement.where(project.archived_at.is_(None))

	return statement


def readable_documents (
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
	include_deleted: bool = False,
	include_archived: bool = False,
) -> sqlalchemy.Select[tuple[subroutine.db.models.work.Document]]:
	"""Return a select over the documents this principal may see, and no others.

	The same narrowing as :func:`readable_tasks`, because a document is a work item under
	the same permissions as the task beside it (SPEC.md §5.6, §7.3a) — a specification in a
	private project is exactly as hidden as the work derived from it, and it would be an odd
	kind of privacy if it were not.
	"""

	document = subroutine.db.models.work.Document
	project = subroutine.db.models.project.Project

	statement = (
		sqlalchemy.select(document)
		.join(project, project.id == document.project_id)
		.where(
			document.workspace_id.in_(workspace_ids),
			project.deleted_at.is_(None),
			subroutine.domain.authorization.visible_projects(principal),
			within_project_scope(principal),
		)
	)

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
	include_completed: bool = True,
	include_archived: bool = False,
	include_templates: bool = False,
) -> sqlalchemy.Select[tuple[subroutine.db.models.work.Task]]:
	"""Return a select over the tasks this principal may see, and no others.

	The join to ``project`` is what makes the visibility rules expressible at all, and is
	the step ``subroutine ls`` was missing. The defaults describe an ordinary listing:
	nothing deleted, nothing archived, and no recurrence templates, which are machinery
	rather than work (§6.7).
	"""

	task = subroutine.db.models.work.Task
	project = subroutine.db.models.project.Project

	statement = (
		sqlalchemy.select(task)
		.join(project, project.id == task.project_id)
		.where(
			task.workspace_id.in_(workspace_ids),
			project.deleted_at.is_(None),
			subroutine.domain.authorization.visible_projects(principal),
			within_project_scope(principal),
		)
	)

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
