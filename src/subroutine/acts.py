"""Every act a principal can ask of an instance: the verbs it asks, and where - `SR#4579`.

**One table, two readers** (decisions `#4532` and `#4512`). ``tests/test_permission_matrix.py``
drives every act here, for every kind of principal, through every way a request reaches an instance,
and holds each to the one decision; ``scripts/permissions_page.py`` writes ``docs/permissions.md``
from it, and a test fails when the page and this table disagree, as ``docs/errors.md`` and the error
registry do.

**It describes and does not decide.** The permission check never reads it - roles and credentials
decide (:mod:`subroutine.domain.authorization`) - and what keeps it true is the matrix, which fails
when an act's verbs or place stop being what the check asks. Simon chose on 2026-10-10 (`#4579`) to
keep the description here, beside the vocabulary in :mod:`subroutine.permissions`, and the driving
of each act in the matrix, joined by its key.
"""

import dataclasses
import textwrap

import subroutine.db.seed
import subroutine.permissions

#: **Where an act's verbs are asked**, in the words the page uses. An act with no place asks nothing
#: of a workspace: what it answers is the asker's own, or the installation's to give.
INSTALLATION = "the installation"
WORKSPACE = "the workspace"
PROJECT = "a project"
PRIVATE_PROJECT = "a private project"


@dataclasses.dataclass(frozen=True)
class Act:
	"""One thing a principal can ask an instance to do.

	``verbs`` are what the permission check is asked, each on ``place``. ``reads`` says the act
	changes nothing, which a read-only connection and the installation's verbs both turn on: those
	verbs gate the installation's listings as well as its changes (decision `#4510`). ``rule`` is a
	rule of the act's own that can refuse what its verbs allow.

	``secret`` says the answer is a credential, a sign-in link or a calendar feed, which no session
	through the agent tools is handed (decision `#4520`). ``own`` says the act is on somebody's own
	thing, which anybody else needs ``workspace:admin`` for - a verb the agent tools never hold
	(`SR#4563`).
	"""

	key: str
	says: str
	verbs: tuple[str, ...] = ()
	place: str | None = None
	reads: bool = False
	rule: str | None = None
	secret: bool = False
	own: bool = False
	group: str = ""

	def never_through_the_agent_tools (self) -> bool:
		"""Say whether the agent tools refuse this act to everybody, whatever they hold.

		Decision `#4520`: a session through ``/mcp`` or ``subroutine mcp`` is handed no secret,
		holds no instance verb, and holds neither ``workspace:admin`` nor ``workspace:delete``.
		"""

		return self.secret or any(
			verb in subroutine.permissions.INSTANCE_LEVEL
			or verb in subroutine.permissions.ABOVE_THE_AGENT_TOOLS
			for verb in self.verbs
		)


def _in (group: str, *acts: Act) -> tuple[Act, ...]:
	"""Return these acts, filed under one heading of the page."""

	return tuple(dataclasses.replace(act, group=group) for act in acts)


#: Somebody else's account, named by every act on one (decision `#4525`).
SOMEBODY = "A person administers another account, or that account's own chain does."

#: Every act, in the order the page lists them.
ACTS: tuple[Act, ...] = (
	*_in(
		"Reading the installation and oneself",
		Act("identity", "Say which instance this is", reads=True),
		Act("reference", "Read the agent's guide", reads=True),
		Act("meta", "Read a workspace's vocabulary", place=WORKSPACE, reads=True),
		Act("me", "Say who is asking", reads=True),
		Act(
			"user",
			"Read an account by name",
			reads=True,
			rule="An account out of the asker's reach is answered as a name nobody has.",
		),
		Act("users", "List the accounts", reads=True),
		Act("tokens", "List one's credentials", reads=True),
		Act("calendars", "List one's calendar feeds", reads=True),
		Act(
			"instance workspaces",
			"List every workspace on the installation",
			verbs=(subroutine.permissions.INSTANCE_ADMIN,),
			place=INSTALLATION,
			reads=True,
		),
		Act(
			"unreachable projects",
			"List the projects nobody can reach",
			verbs=(subroutine.permissions.INSTANCE_ADMIN,),
			place=INSTALLATION,
			reads=True,
		),
		Act(
			"unadministered workspaces",
			"List the workspaces nobody can administer",
			verbs=(subroutine.permissions.INSTANCE_ADMIN,),
			place=INSTALLATION,
			reads=True,
		),
		Act(
			"unowned workspaces",
			"List the workspaces with no owner who can act",
			verbs=(subroutine.permissions.INSTANCE_ADMIN,),
			place=INSTALLATION,
			reads=True,
		),
		Act(
			"backups",
			"List the installation's backups",
			verbs=(subroutine.permissions.INSTANCE_ADMIN,),
			place=INSTALLATION,
			reads=True,
		),
		Act("workspaces", "List the workspaces one reaches", reads=True),
	),
	*_in(
		"Reading a workspace",
		Act(
			"workspace settings",
			"Read a workspace's settings",
			verbs=(subroutine.permissions.WORKSPACE_READ,),
			place=WORKSPACE,
			reads=True,
		),
		Act(
			"workspace",
			"Read a workspace",
			verbs=(subroutine.permissions.WORKSPACE_READ,),
			place=WORKSPACE,
			reads=True,
		),
		Act(
			"members",
			"List a workspace's members",
			verbs=(subroutine.permissions.WORKSPACE_READ,),
			place=WORKSPACE,
			reads=True,
		),
		Act("statuses", "List the statuses", place=WORKSPACE, reads=True),
		Act("link types", "List the link types", place=WORKSPACE, reads=True),
		Act("tags", "List the tags", place=WORKSPACE, reads=True),
		Act("saved views", "List the saved views", place=WORKSPACE, reads=True),
		Act("saved view", "Read a shared saved view", place=WORKSPACE, reads=True),
		Act(
			"projects",
			"List the projects",
			verbs=(subroutine.permissions.PROJECT_READ,),
			place=WORKSPACE,
			reads=True,
		),
		Act(
			"project",
			"Read a project",
			verbs=(subroutine.permissions.PROJECT_READ,),
			place=PROJECT,
			reads=True,
		),
		Act(
			"project history",
			"Read what happened to a project",
			verbs=(subroutine.permissions.PROJECT_READ,),
			place=PROJECT,
			reads=True,
		),
		Act(
			"project settings",
			"Read a project's settings",
			verbs=(subroutine.permissions.PROJECT_READ,),
			place=PROJECT,
			reads=True,
		),
		Act(
			"project members",
			"List who a private project is shared with",
			verbs=(subroutine.permissions.PROJECT_READ,),
			place=PRIVATE_PROJECT,
			reads=True,
		),
		Act(
			"agenda",
			"Read the agenda",
			verbs=(subroutine.permissions.TASK_READ,),
			place=WORKSPACE,
			reads=True,
		),
		Act(
			"count tasks",
			"Count the work",
			verbs=(subroutine.permissions.TASK_READ,),
			place=WORKSPACE,
			reads=True,
		),
		Act(
			"tasks",
			"List the work",
			verbs=(subroutine.permissions.TASK_READ,),
			place=WORKSPACE,
			reads=True,
		),
		Act(
			"search",
			"Search the work",
			verbs=(subroutine.permissions.TASK_READ,),
			place=WORKSPACE,
			reads=True,
		),
		Act(
			"documents",
			"List the documents",
			verbs=(subroutine.permissions.TASK_READ,),
			place=WORKSPACE,
			reads=True,
		),
		Act(
			"count documents",
			"Count the documents",
			verbs=(subroutine.permissions.TASK_READ,),
			place=WORKSPACE,
			reads=True,
		),
		Act("changes", "Read what changed", place=WORKSPACE, reads=True),
		Act("journal", "Read what happened", place=WORKSPACE, reads=True),
		Act("export workspace", "Take away every workspace row", place=WORKSPACE, reads=True),
		Act("export tasks", "Take away every task row", place=WORKSPACE, reads=True),
		Act("export comments", "Take away every comment row", place=WORKSPACE, reads=True),
		Act("export events", "Take away every event row", place=WORKSPACE, reads=True),
	),
	*_in(
		"Reading one item",
		Act(
			"task",
			"Read a task",
			verbs=(subroutine.permissions.TASK_READ,),
			place=PROJECT,
			reads=True,
		),
		Act(
			"private task",
			"Read a task in a private project",
			verbs=(subroutine.permissions.TASK_READ,),
			place=PRIVATE_PROJECT,
			reads=True,
		),
		Act(
			"document",
			"Read a document",
			verbs=(subroutine.permissions.TASK_READ,),
			place=PROJECT,
			reads=True,
		),
		Act(
			"comments",
			"Read a task's comments",
			verbs=(subroutine.permissions.COMMENT_READ,),
			place=PROJECT,
			reads=True,
		),
		Act(
			"history",
			"Read a task's history",
			verbs=(subroutine.permissions.TASK_READ,),
			place=PROJECT,
			reads=True,
		),
		Act(
			"item journal",
			"Read what happened to a task",
			verbs=(subroutine.permissions.TASK_READ,),
			place=PROJECT,
			reads=True,
		),
		Act(
			"links",
			"Read a task's links",
			verbs=(subroutine.permissions.TASK_READ,),
			place=PROJECT,
			reads=True,
		),
		Act(
			"backlinks",
			"Read what mentions a task",
			verbs=(subroutine.permissions.TASK_READ,),
			place=PROJECT,
			reads=True,
		),
		Act(
			"beneath",
			"Read what is beneath a task",
			verbs=(subroutine.permissions.TASK_READ,),
			place=PROJECT,
			reads=True,
		),
		Act(
			"governing",
			"Read what governs a task",
			verbs=(subroutine.permissions.TASK_READ,),
			place=PROJECT,
			reads=True,
		),
		Act(
			"verifications",
			"Read what was checked against a task",
			verbs=(subroutine.permissions.TASK_READ,),
			place=PROJECT,
			reads=True,
		),
		Act(
			"proposed links",
			"Read the links a task's words suggest",
			verbs=(subroutine.permissions.TASK_READ,),
			place=PROJECT,
			reads=True,
		),
		Act(
			"occurrences",
			"Read when a repeating task comes round",
			verbs=(subroutine.permissions.TASK_READ,),
			place=PROJECT,
			reads=True,
		),
	),
	*_in(
		"Writing work",
		Act(
			"capture",
			"File a task in a project",
			verbs=(subroutine.permissions.TASK_WRITE,),
			place=PROJECT,
		),
		Act(
			"update",
			"Retitle a task",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.TASK_WRITE),
			place=PROJECT,
		),
		Act(
			"schedule",
			"Plan a task for a day",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.TASK_WRITE),
			place=PROJECT,
		),
		Act(
			"complete",
			"Finish a task",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.TASK_WRITE),
			place=PROJECT,
		),
		Act(
			"complete beneath the trash",
			"Finish a task whose parent is in the trash",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.TASK_WRITE),
			place=PROJECT,
			rule="Nothing in the trash, or beneath it, changes but by restoring or deleting it.",
		),
		Act(
			"skip",
			"Let one occurrence of a repeat go by",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.TASK_WRITE),
			place=PROJECT,
		),
		Act(
			"move",
			"Put a task under another",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.TASK_WRITE),
			place=PROJECT,
		),
		Act(
			"claim",
			"Take a task",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.TASK_WRITE),
			place=PROJECT,
		),
		Act(
			"release",
			"Give a task back",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.TASK_WRITE),
			place=PROJECT,
		),
		Act(
			"discard",
			"Put a task in the trash",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.TASK_DELETE),
			place=PROJECT,
		),
		Act(
			"undiscard",
			"Take a task out of the trash",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.TASK_DELETE),
			place=PROJECT,
		),
		Act(
			"verify",
			"Record a check against a task",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.TASK_WRITE),
			place=PROJECT,
		),
		Act(
			"link",
			"Link two tasks",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.TASK_WRITE),
			place=PROJECT,
		),
		Act(
			"link to private work",
			"Link a task to one in a private project",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.TASK_WRITE),
			place=PROJECT,
			rule="Both ends must be work the asker can see.",
		),
		Act(
			"unlink",
			"Withdraw a link",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.TASK_WRITE),
			place=PROJECT,
		),
		Act(
			"remark",
			"Comment on a task",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.COMMENT_WRITE),
			place=PROJECT,
		),
		Act(
			"uncomment",
			"Delete a comment its author wrote",
			verbs=(
				subroutine.permissions.TASK_READ,
				subroutine.permissions.COMMENT_READ,
				subroutine.permissions.COMMENT_WRITE,
			),
			place=PROJECT,
			rule="A comment is deleted by its author or by an administrator.",
			own=True,
		),
		Act(
			"edit comment",
			"Rewrite a comment",
			verbs=(
				subroutine.permissions.TASK_READ,
				subroutine.permissions.COMMENT_READ,
				subroutine.permissions.COMMENT_WRITE,
			),
			place=PROJECT,
			# Decision `#4525`.
			rule="Only the person who wrote a comment rewrites it.",
		),
		Act(
			"create document",
			"Write a document",
			verbs=(subroutine.permissions.TASK_WRITE,),
			place=PROJECT,
		),
		Act(
			"update document",
			"Revise a document",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.TASK_WRITE),
			place=PROJECT,
		),
	),
	*_in(
		"A workspace's vocabulary and views",
		Act(
			"create status",
			"Add a status",
			verbs=(subroutine.permissions.STATUS_WRITE,),
			place=WORKSPACE,
		),
		Act(
			"update status",
			"Rename a status",
			verbs=(subroutine.permissions.STATUS_WRITE,),
			place=WORKSPACE,
		),
		Act(
			"delete status",
			"Remove a status",
			verbs=(subroutine.permissions.STATUS_WRITE,),
			place=WORKSPACE,
		),
		Act(
			"create link type",
			"Add a link type",
			verbs=(subroutine.permissions.LINK_TYPE_WRITE,),
			place=WORKSPACE,
		),
		Act(
			"update link type",
			"Reword a link type",
			verbs=(subroutine.permissions.LINK_TYPE_WRITE,),
			place=WORKSPACE,
		),
		Act(
			"delete link type",
			"Remove a link type",
			verbs=(subroutine.permissions.LINK_TYPE_WRITE,),
			place=WORKSPACE,
		),
		Act("create tag", "Add a tag", verbs=(subroutine.permissions.TAG_WRITE,), place=WORKSPACE),
		Act(
			"update tag",
			"Rename a tag",
			verbs=(subroutine.permissions.TAG_WRITE,),
			place=WORKSPACE,
		),
		Act(
			"delete tag",
			"Remove a tag",
			verbs=(subroutine.permissions.TAG_WRITE,),
			place=WORKSPACE,
		),
		Act(
			"rename a hidden tag",
			"Rename a tag used only on private work",
			verbs=(subroutine.permissions.TAG_WRITE,),
			place=WORKSPACE,
			rule="A tag the asker cannot see is not there to rename.",
		),
		Act("save view", "Save a view", place=WORKSPACE),
		Act(
			"update saved view",
			"Change somebody else's shared view",
			place=WORKSPACE,
			rule="A saved view is changed by whoever saved it.",
		),
		Act(
			"forget saved view",
			"Forget somebody else's shared view",
			place=WORKSPACE,
			rule="A saved view is forgotten by whoever saved it.",
			own=True,
		),
		Act(
			"stop sharing a view",
			"Take somebody else's shared view from in front of the workspace",
			place=WORKSPACE,
			# Decision `#4525`.
			rule="A shared view is managed by whoever saved it, or an administrator.",
			own=True,
		),
	),
	*_in(
		"Projects",
		Act(
			"create project",
			"Make a project",
			verbs=(subroutine.permissions.PROJECT_WRITE,),
			place=WORKSPACE,
			# Decision `#4527`.
			rule="A credential narrowed to projects makes none outside them.",
		),
		Act(
			"update project",
			"Retitle a project",
			verbs=(subroutine.permissions.PROJECT_WRITE,),
			place=PROJECT,
		),
		Act(
			"rename project",
			"Change a project's key",
			verbs=(subroutine.permissions.PROJECT_WRITE,),
			place=PROJECT,
		),
		Act(
			"move project",
			"Put a project inside another",
			verbs=(subroutine.permissions.PROJECT_WRITE,),
			place=PROJECT,
			rule="The new parent is written into, so it is within reach too.",
		),
		Act(
			"reveal a private project",
			"Make a private project public",
			verbs=(subroutine.permissions.PROJECT_WRITE,),
			place=PRIVATE_PROJECT,
			# Decision `#4509`.
			rule="Whoever stewards a private project decides who sees it.",
			own=True,
		),
		Act(
			"share project",
			"Share a private project with one more person",
			verbs=(subroutine.permissions.PROJECT_WRITE,),
			place=PRIVATE_PROJECT,
			# Decision `#4509`.
			rule="Whoever stewards a private project decides who sees it.",
			own=True,
		),
		Act(
			"unshare project",
			"Take a private project away from somebody",
			verbs=(subroutine.permissions.PROJECT_WRITE,),
			place=PRIVATE_PROJECT,
			# Decision `#4509`.
			rule="Whoever stewards a private project decides who sees it.",
		),
		Act(
			"hide a project",
			"Make a public project private",
			verbs=(subroutine.permissions.PROJECT_WRITE,),
			place=PROJECT,
			# Decision `#4509`.
			rule="Whoever stewards a project decides who sees it.",
			own=True,
		),
		Act(
			"move work into a private project",
			"Move a task out of a public project into a private one",
			verbs=(subroutine.permissions.TASK_READ, subroutine.permissions.TASK_WRITE),
			place=PRIVATE_PROJECT,
			# Decision `#4509`.
			rule="Work leaves the sight of who can see it only by its project's steward.",
			own=True,
		),
		Act(
			"delete project",
			"Put a project in the trash",
			verbs=(subroutine.permissions.PROJECT_DELETE,),
			place=PROJECT,
		),
		Act(
			"restore project",
			"Take a project out of the trash",
			verbs=(subroutine.permissions.PROJECT_DELETE,),
			place=PROJECT,
		),
	),
	*_in(
		"Workspaces",
		Act(
			"create workspace",
			"Make a workspace",
			verbs=(subroutine.permissions.INSTANCE_WORKSPACE_CREATE,),
			place=INSTALLATION,
		),
		Act(
			"update workspace",
			"Describe a workspace",
			verbs=(subroutine.permissions.WORKSPACE_WRITE,),
			place=WORKSPACE,
		),
		Act(
			"rename workspace",
			"Change a workspace's short name",
			verbs=(subroutine.permissions.WORKSPACE_WRITE,),
			place=WORKSPACE,
		),
		Act(
			"colour workspace",
			"Choose the colour a workspace's work is marked with",
			verbs=(subroutine.permissions.WORKSPACE_WRITE, subroutine.permissions.WORKSPACE_ADMIN),
			place=WORKSPACE,
		),
		Act(
			"delete workspace",
			"Delete a workspace",
			verbs=(subroutine.permissions.WORKSPACE_DELETE,),
			place=WORKSPACE,
		),
		Act(
			"restore workspace",
			"Bring a deleted workspace back",
			verbs=(subroutine.permissions.WORKSPACE_DELETE,),
			rule="Only somebody who could delete it brings a workspace back.",
		),
		Act(
			"add member",
			"Add somebody to a workspace",
			verbs=(subroutine.permissions.USER_ADMIN,),
			place=WORKSPACE,
		),
		Act(
			"set member role",
			"Change somebody's role",
			verbs=(subroutine.permissions.USER_ADMIN,),
			place=WORKSPACE,
		),
		Act(
			"remove member",
			"Take somebody out of a workspace",
			verbs=(subroutine.permissions.USER_ADMIN,),
			place=WORKSPACE,
		),
	),
	*_in(
		"Accounts, credentials and the installation",
		Act("set own timezone", "Say where one keeps one's diary"),
		Act(
			"issue own token",
			"Mint a credential for oneself",
			# Decision `#4527`.
			rule="A credential never mints a wider one.",
			secret=True,
		),
		Act(
			"create calendar",
			"Mint a calendar feed for oneself",
			place=WORKSPACE,
			# Decision `#4527`.
			rule="A credential never mints a wider one.",
			secret=True,
		),
		Act(
			"create user",
			"Make an account for a person",
			verbs=(subroutine.permissions.INSTANCE_USER_CREATE,),
			place=INSTALLATION,
			# Decision `#4515`.
			rule="Only a person makes a person.",
		),
		Act(
			"issue token for somebody",
			"Mint a credential for somebody else",
			verbs=(subroutine.permissions.INSTANCE_USER_CREATE,),
			place=INSTALLATION,
			rule=SOMEBODY,
			secret=True,
		),
		Act(
			"sign-in link for somebody",
			"Mint a sign-in link for somebody else",
			verbs=(subroutine.permissions.INSTANCE_USER_CREATE,),
			place=INSTALLATION,
			rule=SOMEBODY,
			secret=True,
		),
		Act(
			"sign somebody out",
			"Sign another person out everywhere",
			verbs=(subroutine.permissions.INSTANCE_USER_CREATE,),
			place=INSTALLATION,
			rule=SOMEBODY,
		),
		Act(
			"revoke somebody's token",
			"Revoke another person's credential",
			verbs=(subroutine.permissions.INSTANCE_USER_CREATE,),
			place=INSTALLATION,
			rule=SOMEBODY,
		),
		Act(
			"reset somebody's feed",
			"Reset another person's calendar feed",
			# Decision `#4525`.
			rule="Only its owner resets a feed.",
			secret=True,
		),
		Act(
			"revoke somebody's feed",
			"Revoke another person's calendar feed",
			verbs=(subroutine.permissions.INSTANCE_USER_CREATE,),
			place=INSTALLATION,
			rule=SOMEBODY,
		),
		Act(
			"deactivate somebody",
			"Mark somebody as having left",
			verbs=(subroutine.permissions.INSTANCE_USER_CREATE,),
			place=INSTALLATION,
			rule=SOMEBODY,
		),
		Act(
			"transfer an agent",
			"Hand somebody's agent to another person",
			verbs=(subroutine.permissions.INSTANCE_USER_CREATE,),
			place=INSTALLATION,
			rule=SOMEBODY,
		),
		Act(
			"update instance",
			"Rename the installation",
			verbs=(subroutine.permissions.INSTANCE_ADMIN,),
			place=INSTALLATION,
		),
		Act(
			"take backup",
			"Take a backup of the installation",
			verbs=(subroutine.permissions.INSTANCE_ADMIN,),
			place=INSTALLATION,
		),
	),
)


def _also (act: Act) -> str:
	"""Return what a row adds to its permissions: the act's own rule, and the agent tools' reach."""

	said = [act.rule] if act.rule is not None else []

	if act.never_through_the_agent_tools():
		said.append("Never through the agent tools.")

	elif act.own:
		said.append("Through the agent tools, only on one's own.")

	return " ".join(said) or "-"


#: **What the page says above its tables**, a paragraph a string: :func:`page_markdown` wraps them.
INTRODUCTION = (
	(
		"Everything somebody can ask an instance to do, the permission each act asks, and where "
		"it asks it. A role in a workspace grants a set of permissions, a credential can be "
		"narrowed to fewer, and a private project is seen only by the accounts it is shared with "
		"and whoever those answer to, whatever their role. [Section 7 of the design]"
		"(design.md#7-identity-authentication-and-authorisation) sets out the whole model."
	),
	(
		"**How to read a row.** *Asks* is what the permission check is asked, and *On* is where: "
		"the installation, the workspace, or the project the act lands in. An act asking no "
		"permission is answered without one, and one on the workspace still needs somebody who "
		"can reach it. *Also* is a rule of the act's own that can refuse what its permissions "
		"allow, and whether the agent tools can do it: a session through them is never handed a "
		"credential, a sign-in link or a calendar feed, and never holds a permission over the "
		"installation, `workspace:admin` or `workspace:delete`."
	),
	(
		"**A listing holds only what its asker can see.** Reading the work of a workspace asks "
		"`task:read` once, and the work of a private project is still left out for anybody who "
		"cannot see it. The accounts listed are those within the asker's reach: whoever shares a "
		"workspace with them, and their own chain."
	),
	(
		"The page is generated from the acts the permission tests drive through every way in - "
		"the terminal, the HTTP API and the agent tools - so it says what the instance does."
	),
)

#: What the page says above the roles.
ROLES = (
	"A member of a workspace holds one of these roles, and it grants the permissions marked. The "
	"permissions over the installation itself - its accounts, its workspaces and its settings - "
	"are held by an administrator of the installation and are never part of a role."
)


def _paragraph (text: str) -> list[str]:
	"""Return a paragraph wrapped as the page's other prose is, and the blank line after it."""

	return [*textwrap.wrap(text, width=92, break_long_words=False, break_on_hyphens=False), ""]


def page_markdown () -> str:
	"""Render the acts, and the roles a workspace is given, as ``docs/permissions.md``.

	Generated rather than written, so the page cannot drift from the acts the matrix drives or from
	the roles the seed writes; a test asserts the file on disk is this, and
	``scripts/permissions_page.py`` writes it. The roles are there because a row saying an act asks
	``task:write`` answers nobody's question until they know who holds it (Simon, 2026-10-10).
	"""

	lines = ["# Permissions", ""]

	for paragraph in INTRODUCTION:
		lines.extend(_paragraph(paragraph))

	lines.append("<!-- Generated from subroutine/acts.py. Do not edit by hand. -->")
	header = ["| Act | Asks | On | Also |", "| --- | --- | --- | --- |"]
	group = None

	for act in ACTS:
		if act.group != group:
			group = act.group
			lines.extend(["", f"## {group}", "", *header])

		asks = ", ".join(f"`{verb}`" for verb in act.verbs) or "-"

		lines.append(f"| {act.says} | {asks} | {act.place or '-'} | {_also(act)} |")

	roles = [
		role
		for version in sorted(subroutine.db.seed.SEED_SETS)
		for role in subroutine.db.seed.SEED_SETS[version].roles
	]
	titles = " | ".join(role.title for role in roles)

	lines.extend(["", "## The roles every workspace is given", "", *_paragraph(ROLES)])
	lines.extend([f"| Permission | Covers | {titles} |", "| --- | --- |" + " --- |" * len(roles)])

	for verb in subroutine.permissions.sorted_permissions(subroutine.permissions.WORKSPACE_LEVEL):
		covers = subroutine.permissions.COVERAGE.get(verb, "-")
		held = " | ".join("yes" if verb in role.permissions else "-" for role in roles)

		lines.append(f"| `{verb}` | {covers} | {held} |")

	return "\n".join(lines) + "\n"
