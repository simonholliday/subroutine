"""Everything a usable installation needs, created in one transaction.

``subroutine init`` is the only command a new user runs before the one they actually
wanted, so it has one job: leave behind a database they can immediately add a task to.
That means an instance identity, a user — a superuser, since they are the one installing
it — a workspace with its vocabulary, an Inbox to file things in, and an owner membership:
five things, none of which the person asked for and none of which they should have to
think about (docs/design.md §12.1).

Kept apart from the CLI so it can be tested, called from a container's entrypoint, and
later reused by whatever sets up a second workspace. The CLI's share of this is printing
one line.
"""

import dataclasses

import sqlalchemy
import sqlalchemy.orm

import subroutine.addressing
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.system
import subroutine.domain.instances
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors

#: The project a task with no project of its own is filed in. One per workspace, not one
#: per user: ``project.key`` is unique per workspace, so per-user inboxes collide on the
#: second user, and nothing in the schema records whose inbox a project is (docs/design.md §6.8).
#: Re-exported, because `#301` moved the Inbox's creation into `domain.workspaces` and the
#: names belong beside it. Kept resolving here so a caller that learned them from this module
#: is not broken by where they moved to.
INBOX_KEY = subroutine.domain.workspaces.INBOX_KEY
INBOX_TITLE = subroutine.domain.workspaces.INBOX_TITLE

#: The Inbox is the clearest case for the personal template — two statuses, no evidence
#: gate. Someone setting up a to-do list should never meet an acceptance criterion unless
#: they go looking for one (docs/design.md §6.12).
INBOX_TEMPLATE = subroutine.domain.workspaces.INBOX_TEMPLATE


@dataclasses.dataclass(frozen=True)
class Bootstrap:
	"""What an installation was left with.

	``created`` is false when everything was already there. That is a success, not an
	error: re-running ``init`` against an existing database should be uneventful.
	"""

	instance: subroutine.db.models.system.Instance
	user: subroutine.db.models.identity.User
	workspace: subroutine.db.models.identity.Workspace
	inbox: subroutine.db.models.project.Project
	created: bool


def initialise (
	session: sqlalchemy.orm.Session,
	*,
	username: str,
	instance_name: str,
	workspace_title: str = "Personal",
	workspace_slug: str | None = None,
	password: str | None = None,
	timezone: str = "UTC",
	display_name: str | None = None,
) -> Bootstrap:
	"""Set up a fresh installation, or report the one that is already here.

	Idempotent by the instance row: if this database has been initialised, nothing is
	written and the existing setup is described instead. That check is deliberately the
	first thing, so a re-run cannot half-create a second workspace before noticing.
	"""

	# The machine's own zone, which for a personal installation is also the person's.
	# Everything below inherits it unless it says otherwise (docs/design.md §6.5).
	instance, created = subroutine.domain.instances.establish(
		session, name=instance_name, timezone=timezone
	)

	if not created:
		return _describe(session, instance)

	# A superuser, because this is the person who installed it. Without the flag they would
	# own the workspace ``init`` just made and be unable to create a second one or add
	# anybody, since neither act is a workspace permission any role can carry
	# (docs/design.md §7.1). Nobody else gets it by default.
	user = subroutine.domain.users.create(
		session,
		username=username,
		display_name=display_name,
		password=password,
		timezone=timezone,
		is_superuser=True,
	)

	workspace = subroutine.domain.workspaces.create(
		session,
		slug=workspace_slug or _derived_slug(workspace_title, username),
		title=workspace_title,
		owner=user,
		timezone=timezone,
	)

	# **Made by `workspaces.create`, not here** (`#301`). It used to be this call, which meant
	# `init` produced a complete workspace and every other route produced one that could not
	# be captured into. Asked for rather than assumed, so a workspace that somehow arrived
	# without one is a failure here rather than a puzzle three commands later.
	inbox = inbox_for(session, workspace)

	if inbox is None:
		raise subroutine.errors.SubroutineError(
			"This workspace was created without an Inbox.",
			hint="Nothing has been set up. Report this — it should not be reachable.",
		)

	session.flush()

	return Bootstrap(
		instance=instance, user=user, workspace=workspace, inbox=inbox, created=True
	)


def inbox_for (
	session: sqlalchemy.orm.Session, workspace: subroutine.db.models.identity.Workspace
) -> subroutine.db.models.project.Project | None:
	"""Return a workspace's Inbox, the project a task with no project is filed in."""

	model = subroutine.db.models.project.Project

	return session.scalars(
		sqlalchemy.select(model)
		.where(
			model.workspace_id == workspace.id,
			model.is_inbox.is_(True),
			model.deleted_at.is_(None),
		)
		.order_by(model.created_at)
	).first()


def _describe (
	session: sqlalchemy.orm.Session, instance: subroutine.db.models.system.Instance
) -> Bootstrap:
	"""Report an installation that already exists, without changing any of it."""

	workspace = session.scalars(
		sqlalchemy.select(subroutine.db.models.identity.Workspace)
		.where(subroutine.db.models.identity.Workspace.deleted_at.is_(None))
		.order_by(subroutine.db.models.identity.Workspace.created_at)
	).first()

	if workspace is None:
		raise subroutine.errors.InternalError(
			"This database has an instance identity but no workspace.",
			hint="It was interrupted part-way through setup; restore it, or start again "
			"from an empty database.",
		)

	membership = session.scalars(
		sqlalchemy.select(subroutine.db.models.identity.WorkspaceMember)
		.where(subroutine.db.models.identity.WorkspaceMember.workspace_id == workspace.id)
		.order_by(subroutine.db.models.identity.WorkspaceMember.created_at)
	).first()

	user = None if membership is None else session.get(
		subroutine.db.models.identity.User, membership.user_id
	)
	inbox = inbox_for(session, workspace)

	if user is None or inbox is None:
		raise subroutine.errors.InternalError(
			"This database is set up but incomplete — its workspace has no owner or no Inbox.",
			hint="Restore it, or start again from an empty database.",
		)

	return Bootstrap(
		instance=instance, user=user, workspace=workspace, inbox=inbox, created=False
	)


#: What the first workspace is called when neither its title nor the username can produce a
#: legal short name — `#690`. The default title's own slug, so the commonest instance and the
#: last-resort one are addressed the same way. ``tests/test_bootstrap.py`` asserts it survives
#: the reserved list, which is the half that could otherwise rot.
LAST_RESORT_SLUG = "personal"


def _derived_slug (title: str, username: str) -> str:
	"""Return a short name for the first workspace, from its title.

	**Named after the workspace, not after the person.** The slug used to be the username,
	which was invisible while nothing printed it — and became wrong the moment §13.7 made a
	slug part of an address: somebody who ran ``init --workspace Acme`` then found
	``subroutine use acme`` did not work, because their workspace was addressed by their login
	name.

	**Derived, therefore shaped rather than validated.** A title may be 255 characters and a
	slug 64, and passing the derived value to ``workspaces.create`` unchanged made
	``init --workspace "<a 69-character title>"`` refuse to set up the database at all — with a
	413 about a "short name" the user had never typed, naming a field they had never heard of.
	``text.fit`` exists to refuse *input*; this is not input, so it is trimmed. The same applies
	to §5.4's leading-letter rule and to the reserved words: a title that cannot produce a legal
	slug falls back to the username rather than becoming a refusal, because the user asked for a
	*workspace* and the short name is this function's problem.

	**And the fallback obeys the same rule, which it did not until `#690`.** The username was
	taken unchecked, so when *both* the title and the username derived a reserved word,
	``workspaces.create`` refused and ``init`` created nothing — the exact failure the paragraph
	above says this function exists to prevent, with the refusal naming ``slug``, a field the
	user never typed, on their first command. Driven:
	``init --username app --workspace MCP`` said *'app' cannot be a workspace's short name*.

	It needed `#678` to become plausible: the reserved words used to be ``all``, ``default``,
	``local``, ``me`` and the like, and `#678` added ``app``, ``mcp``, ``v1`` and ``signin`` —
	and **``app`` is an ordinary name for a service account**, so a container running as one
	meets this on installation.
	"""

	candidate = subroutine.domain.workspaces.normalize_slug(title)

	# Trimmed at a hyphen where there is one within reach, so a truncated name reads as words
	# rather than as a word cut in half.
	if len(candidate) > subroutine.domain.workspaces.MAX_SLUG_LENGTH:
		candidate = candidate[: subroutine.domain.workspaces.MAX_SLUG_LENGTH]
		spare = candidate.rsplit("-", 1)[0]
		candidate = spare if len(spare) >= subroutine.domain.workspaces.MAX_SLUG_LENGTH // 2 else candidate

	# **Every candidate in turn, against one rule.** The last is a constant rather than
	# somebody's input precisely so that there is an end to the chain: a test asserts it is
	# itself usable, so this cannot come to have no answer if the reserved list grows again.
	for found in (
		candidate.strip("-"),
		subroutine.domain.workspaces.normalize_slug(username),
		LAST_RESORT_SLUG,
	):
		if _usable(found):
			return found

	return LAST_RESORT_SLUG


def _usable (candidate: str) -> bool:
	"""Report whether a derived slug is one ``workspaces.create`` will accept."""

	return bool(
		candidate
		and candidate[0].isascii()
		and candidate[0].isalpha()
		and not subroutine.addressing.is_reserved_workspace_word(candidate)
	)
