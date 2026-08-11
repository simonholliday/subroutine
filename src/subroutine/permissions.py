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
#: (SPEC.md §7.3a). **That fact lived only here until `#703`** — see :data:`COVERAGE`.
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

#: Everything a credential may do without changing anything — the ``viewer`` role's whole
#: grant, and what the ``observer`` profile narrows a credential to (decision `#370`).
#:
#: **Named explicitly, for the same reason as the set below.** "Anything ending in `:read`"
#: would be a convention deciding a security control, and it happens to be right today only
#: because nobody has yet added a verb that reads without saying so — `changes:read` and a
#: feed subscription are both plausible, and either would join this set by somebody deciding
#: it does rather than by how it was spelled.
READS: frozenset[str] = frozenset(
	{
		WORKSPACE_READ,
		PROJECT_READ,
		TASK_READ,
		COMMENT_READ,
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


#: What a permission covers that its own name does not say — item ``#703``.
#:
#: **A permission is a promise to a reader who has no other source.** An agent asks what it may
#: do, is handed a list of verbs, and acts on it; there is nothing else to consult. So a verb
#: whose subject is wider than its prefix is a sentence that reads as complete and is not, and
#: the reader's own care is what makes it wrong — they reason correctly from a true list to a
#: false conclusion.
#:
#: That is not hypothetical. The agent on nuc14 read its grants, found no ``document:*``, wrote
#: up a substantial measurement as a *comment* rather than as the finding it was, and asked for
#: its credential to be widened — work that would have been done for nothing. It had held the
#: capability all along. Driven against the served instance with that exact credential:
#: ``POST /v1/documents`` answered **201**.
#:
#: Only entries whose subject is not simply their own prefix belong here. The rest would be
#: §12.2a's column that says the same thing on every row, and a note beside ``comment:write``
#: saying *comments* teaches nothing while making the two that matter harder to see.
#:
#: **Complete for documents by derivation, not by care.**
#: ``test_every_permission_that_gates_a_document_says_so``, in ``tests/test_authorization.py``,
#: reads the permissions ``domain/documents.py`` actually checks and fails if one of them has no
#: entry here — so gating documents on a new verb cannot silently reintroduce this.
#:
#: That sentence named ``tests/test_permissions.py``, which does not exist (`#742`). A citation
#: to nothing is worse than none: it is the thing a reader checks *instead of* the claim, and
#: finding nothing there tells them less than being sent nowhere would have.
COVERAGE: dict[str, str] = {
	TASK_READ: "tasks and documents",
	TASK_WRITE: "tasks and documents",
	TASK_DELETE: "tasks and documents",
	# Not instance-wide account administration, which is `instance:user_create` and is a tier
	# up. This is membership of *this* workspace: inviting, removing, changing a role.
	USER_ADMIN: "who belongs to this workspace",
}


def worth_listing (names: typing.Iterable[str]) -> bool:
	"""Report whether spelling out these permissions tells a reader anything — `#717`.

	**Anything short of everything is worth listing.** ``whoami`` used to show the list only
	when the *credential* had been narrowed, on the reasoning that "an unnarrowed owner would
	otherwise be handed twenty keys it already holds". That reasoning is sound and it is about
	the wrong case: an owner holding everything is exactly where a list says nothing, and a
	contributor holds six of seventeen — the case where it says the most. So an agent learned
	**more** about what it could do by being *restricted*, and a plain contributor credential
	was handed the word *Contributor* and nothing else.

	`GET /v1/me` carries the permissions either way, so this only ever governed the rendering —
	which meant an agent reading `whoami`, the channel built for this question, was worse
	informed than one calling `call_api` on `/v1/me`. `#499`'s rule points the other way.

	Here rather than in either surface, for the reason :func:`described` gives one line below:
	the CLI and the MCP tool answer the same question, and two copies of a rule is what this
	project keeps finding wrong.
	"""

	return frozenset(names) != WORKSPACE_LEVEL


def described (names: typing.Iterable[str]) -> list[str]:
	"""Return each permission with what it covers, where the name does not already say.

	**One renderer, because two would disagree.** The CLI's ``whoami`` and the MCP tool both
	answer *what may I do here*, and this project's signature defect is two copies of one rule
	drifting apart — most recently eleven copies of a project key's normalisation, all correct
	while they agreed (`#508`).

	A list rather than a joined string, so each surface keeps its own punctuation.
	"""

	return [
		name if name not in COVERAGE else f"{name} ({COVERAGE[name]})"
		for name in names
	]
