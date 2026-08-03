"""The verbs an actor can be granted — and nothing about who holds them.

Kept apart from both the models and the service layer because three things need the same
vocabulary and none of them should own it: the seed routine writes these strings into role
rows, the authorisation check reads them back out, and the API publishes them. A permission
is a plain string precisely so that a custom role is a data change and not a migration
(SPEC.md §7.2).

There are **two tiers**, and they are kept in separate sets because they are checked
against different things (SPEC.md §7.1). A workspace permission answers "may this person
do X *in workspace W*" and is granted by a role. An instance permission answers "may this
person do X *to this installation*" — creating a second workspace, creating an account —
and has no workspace to be checked against, so no role can carry it and only a superuser
holds it.

Nothing here decides anything. Which permissions a role carries is seed data
(``subroutine.db.seed``); how they combine with a token's scopes is SPEC.md §7.3.
"""

import typing

#: Reading, changing and administering the workspace itself — its title, its settings and
#: who belongs to it.
WORKSPACE_READ = "workspace:read"
WORKSPACE_WRITE = "workspace:write"
WORKSPACE_ADMIN = "workspace:admin"

#: Deleting the whole workspace, separate from administering it because it is the one
#: thing an owner can do that an admin cannot (SPEC.md §7.2).
WORKSPACE_DELETE = "workspace:delete"

PROJECT_READ = "project:read"
PROJECT_WRITE = "project:write"
PROJECT_DELETE = "project:delete"

#: Tasks and documents share these: a document is a work item under the same permissions
#: as the task beside it, and giving it its own verbs would double the matrix to no end
#: (SPEC.md §7.3a).
TASK_READ = "task:read"
TASK_WRITE = "task:write"
TASK_DELETE = "task:delete"

COMMENT_READ = "comment:read"
COMMENT_WRITE = "comment:write"

#: Curating the words a workspace uses to describe its work. Creating a tag as a side
#: effect of writing a task needs only ``task:write`` — these cover deliberate curation,
#: not incidental use (SPEC.md §7.3).
TAG_WRITE = "tag:write"
STATUS_WRITE = "status:write"
LINK_TYPE_WRITE = "link_type:write"

#: Managing who belongs to *this workspace* and what they may do here — inviting,
#: removing, changing a member's role. Not the same thing as creating an account, which is
#: :data:`INSTANCE_USER_CREATE` and belongs to the tier below (SPEC.md §7.1).
USER_ADMIN = "user:admin"
TOKEN_ADMIN = "token:admin"

#: Every permission that is granted by a role and checked against a workspace.
WORKSPACE_LEVEL: frozenset[str] = frozenset(
	{
		WORKSPACE_READ,
		WORKSPACE_WRITE,
		WORKSPACE_ADMIN,
		WORKSPACE_DELETE,
		PROJECT_READ,
		PROJECT_WRITE,
		PROJECT_DELETE,
		TASK_READ,
		TASK_WRITE,
		TASK_DELETE,
		COMMENT_READ,
		COMMENT_WRITE,
		TAG_WRITE,
		STATUS_WRITE,
		LINK_TYPE_WRITE,
		USER_ADMIN,
		TOKEN_ADMIN,
	}
)

#: The verbs whose effect lands *inside a project*, which is what makes them the ones a
#: credential's write set narrows (§7.3, decision `#370`, item `#371`).
#:
#: **Named explicitly rather than derived from the string.** "Anything not ending in `:read`"
#: would be an implicit convention deciding a security control, and it is wrong in both
#: directions: `tag:write`, `status:write` and `link_type:write` curate the *workspace's*
#: vocabulary rather than anything in a project, and `workspace:admin` is not about a project
#: at all. A verb added later joins this set by somebody deciding it does, which is the
#: property a suffix rule cannot have.
#:
#: Reads are deliberately absent. A credential's *reach* — `project_scope` — already decides
#: which rows exist for it, and narrowing reads twice would mean two controls with one job.
WRITES_INSIDE_A_PROJECT: frozenset[str] = frozenset(
	{
		PROJECT_WRITE,
		PROJECT_DELETE,
		TASK_WRITE,
		TASK_DELETE,
		COMMENT_WRITE,
	}
)

#: Creating the second workspace happens outside every existing workspace, and creating an
#: account happens before that account belongs to one — so neither can be expressed as a
#: role permission, and without their own verbs the only way to do either is to skip the
#: check (SPEC.md §7.1).
INSTANCE_WORKSPACE_CREATE = "instance:workspace_create"
INSTANCE_USER_CREATE = "instance:user_create"

#: The installation's own identity and settings, and anything that reads across every
#: workspace at once.
INSTANCE_ADMIN = "instance:admin"

#: Every permission held by superusers and by nobody else. A role may not carry one of
#: these; a token may still narrow to one, which is what lets an agent be given the
#: authority to create a workspace without being given everything else (SPEC.md §7.3).
INSTANCE_LEVEL: frozenset[str] = frozenset(
	{
		INSTANCE_WORKSPACE_CREATE,
		INSTANCE_USER_CREATE,
		INSTANCE_ADMIN,
	}
)

#: Every permission this build recognises, of either tier. A token may narrow to nothing
#: outside it. A *role* is narrower still — see :data:`WORKSPACE_LEVEL`.
ALL: frozenset[str] = WORKSPACE_LEVEL | INSTANCE_LEVEL


def unknown (
	candidates: typing.Iterable[str], *, within: frozenset[str] = ALL
) -> tuple[str, ...]:
	"""Return those candidates that are not permissions this build recognises.

	``within`` narrows what counts as recognised, so a role can be checked against
	:data:`WORKSPACE_LEVEL` alone: ``instance:user_create`` is a real permission and still
	not a thing a role may grant.

	Order and duplicates are preserved, so an error message can quote the offending value
	back in the form it was written rather than a tidied-up version of it.
	"""

	return tuple(candidate for candidate in candidates if candidate not in within)


def sorted_permissions (candidates: typing.Iterable[str]) -> list[str]:
	"""Return a de-duplicated permission list in a stable order.

	Stored on a role as JSON, so a predictable order keeps two identical roles comparing
	equal and keeps a database dump free of spurious differences.
	"""

	return sorted(set(candidates))
