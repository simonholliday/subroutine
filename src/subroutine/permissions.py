"""The verbs an actor can be granted — and nothing about who holds them.

Kept apart from both the models and the service layer because three things need the same
vocabulary and none of them should own it: the seed routine writes these strings into role
rows, the authorisation check reads them back out, and the API publishes them. A permission
is a plain string precisely so that a custom role is a data change and not a migration
(SPEC.md §7.2).

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

USER_ADMIN = "user:admin"
TOKEN_ADMIN = "token:admin"

#: Every permission this build recognises. A role may be granted nothing outside it, and
#: a token may narrow to nothing outside it.
ALL: frozenset[str] = frozenset(
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


def unknown (candidates: typing.Iterable[str]) -> tuple[str, ...]:
	"""Return those candidates that are not permissions this build recognises.

	Order and duplicates are preserved, so an error message can quote the offending value
	back in the form it was written rather than a tidied-up version of it.
	"""

	return tuple(candidate for candidate in candidates if candidate not in ALL)


def sorted_permissions (candidates: typing.Iterable[str]) -> list[str]:
	"""Return a de-duplicated permission list in a stable order.

	Stored on a role as JSON, so a predictable order keeps two identical roles comparing
	equal and keeps a database dump free of spurious differences.
	"""

	return sorted(set(candidates))
