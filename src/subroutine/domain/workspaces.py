"""Creating the tenancy root, and the one member it cannot exist without.

A workspace is invisible to a person using Subroutine alone — ``subroutine init`` makes
one and never mentions it again (SPEC.md §1.4). It matters here because everything else
hangs off it: the vocabulary, the roles, and the rule that every query is scoped by it.
"""

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.addressing
import subroutine.db.models.identity
import subroutine.db.seed
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.dates
import subroutine.domain.events
import subroutine.domain.patch
import subroutine.domain.projects
import subroutine.domain.text
import subroutine.domain.versions
import subroutine.errors
import subroutine.permissions

#: The role the creating user is given. SPEC.md §10.7 invariant 7 requires at least one
#: owner per workspace, and creating one without its owner would break that between two
#: statements of the same transaction.
FOUNDING_ROLE = "owner"

#: Column widths from SPEC.md §10.6. See `subroutine.domain.text` for why these are checked
#: in Python rather than left to the backend.
MAX_SLUG_LENGTH = 64
MAX_TITLE_LENGTH = 255


#: The project a task with no project is filed in. Named here rather than in ``bootstrap``
#: because this is where an Inbox is made — ``bootstrap`` was the only caller when it owned
#: these, and stopped being so when `#301` moved the creation (§6.14).
INBOX_KEY = "inbox"
INBOX_TITLE = "Inbox"

#: Two statuses and no evidence requirement: the clearest case for the personal template,
#: since the Inbox is where a capture lands before anybody has decided anything about it.
INBOX_TEMPLATE = "personal"


def create (
	session: sqlalchemy.orm.Session,
	*,
	slug: str,
	title: str,
	owner: subroutine.db.models.identity.User,
	timezone: str = "UTC",
	settings: dict[str, typing.Any] | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.identity.Workspace:
	"""Create a workspace, stock its vocabulary, and make ``owner`` its owner.

	All four in one transaction, because a workspace without roles cannot be joined and a
	workspace without an owner cannot be administered — neither is a state worth being
	able to reach.
	"""

	# The instance tier (SPEC.md §7.1). This act happens outside every workspace, so it is
	# checked against the installation rather than against one — and `authorize_instance`
	# honours a token's scopes even for a superuser, which is what makes it safe to hand an
	# agent a token that may do this and nothing else.
	if actor is not None:
		subroutine.domain.authorization.authorize_instance(
			actor, subroutine.permissions.INSTANCE_WORKSPACE_CREATE
		)


	title = subroutine.domain.text.fit(
		subroutine.domain.text.require(title, field="title"),
		field="title",
		limit=MAX_TITLE_LENGTH,
	)
	normalized = validated_slug(session, slug)

	# Same check as `update`, and it was missing here: `create` took whatever string it was
	# handed, so `{"timezone": "Mars/Olympus"}` was stored and only surfaced later, as a
	# refusal that named the caller's request rather than the workspace holding the bad value.
	subroutine.domain.dates.zone(timezone)

	workspace = subroutine.db.models.identity.Workspace(
		slug=normalized,
		title=title,
		timezone=timezone,
		settings=dict(settings or {}),
	)
	session.add(workspace)
	session.flush()

	report = subroutine.db.seed.seed_workspace(session, workspace)
	add_member(session, workspace, owner, role_key=FOUNDING_ROLE, actor=actor)

	subroutine.domain.events.record(
		session,
		workspace_id=workspace.id,
		entity_type="workspace",
		entity_id=workspace.id,
		action=subroutine.domain.events.EventAction.CREATED,
		changes={"slug": {"from": None, "to": normalized}},
		actor=actor,
	)
	record_seeding(session, workspace, report, actor=actor)

	# **An Inbox is part of what a workspace is** (`#301`), which is the same argument the
	# paragraph above makes about roles and an owner: a workspace without one refuses every
	# task filed with no project, and that is the ordinary way to file one (§6.14, §1.4).
	#
	# It was `bootstrap`'s job until now, so `init` produced a complete workspace and every
	# other route produced one that could not be captured into — `POST /v1/workspaces` has
	# shipped that since M1. Made here so there is one answer rather than a step each caller
	# has to remember.
	subroutine.domain.projects.create(
		session,
		workspace_id=workspace.id,
		key=INBOX_KEY,
		title=INBOX_TITLE,
		template=INBOX_TEMPLATE,
		owner_id=owner.id,
		is_inbox=True,
		actor=actor,
	)
	session.flush()

	return workspace


def update (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	*,
	slug: str = subroutine.domain.patch.UNSET,
	title: str = subroutine.domain.patch.UNSET,
	description: str | None = subroutine.domain.patch.UNSET,
	timezone: str | None = subroutine.domain.patch.UNSET,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.identity.Workspace:
	"""Change a workspace, recording only what actually changed.

	Anything left at ``patch.UNSET`` is untouched; passing ``None`` clears the field (§8.3).
	**Everything is validated before anything is assigned**, for the reason ``projects.update``
	gives: the caller holds a live session it may still commit, so a half-applied change that
	raised on the way through would be committed silently alongside whatever else was in flight.

	**The slug may be changed as of `#295`**, and the argument against it did not survive
	being checked. It ran: a project key is an address within one instance, while a slug also
	appears in other people's notes, in shell history and in ``config.toml`` on other
	machines. The third is simply false — no connection and no setting names a workspace, so
	nothing on another machine holds one except a ``.subroutine`` marker, which is exactly
	what renaming a project key already breaks (`#176`). And nothing *inside* the database
	references a slug at all: every table keys on ``workspace_id``, so a rename moves no
	relationship and breaks no join.

	What is left is real and is handled the way `#176` handled it — by counting first and
	saying what stops working, rather than by refusing. The one difference worth respecting is
	that a workspace is a tenancy boundary, so a rename changes the address for everybody who
	can reach it: hence ``WORKSPACE_WRITE`` below, and hence the CLI naming members as well as
	items before it asks.

	``timezone`` may be set to ``None``, and that is meaningful rather than sloppy: null means
	"not stated", which lets the instance's own zone show through (§12.3). It was
	``NOT NULL DEFAULT 'UTC'`` until migration ``233f898a2bee`` precisely because a default here
	shadowed the instance and left a step in the chain nothing could reach.
	"""

	if actor is not None:
		subroutine.domain.authorization.authorize(
			session,
			actor,
			subroutine.permissions.WORKSPACE_WRITE,
			workspace_id=workspace.id,
		)

	subroutine.domain.versions.require(workspace, expected_version, noun="This workspace")

	changed: dict[str, typing.Any] = {}

	if slug is not subroutine.domain.patch.UNSET:
		# Validated by the same function `create` uses, so a rename cannot arrive at a name
		# creation would have refused — which is how two paths drift, and how renaming becomes
		# the way to reach a slug nobody could have chosen.
		changed["slug"] = validated_slug(session, slug, except_id=workspace.id)

	if title is not subroutine.domain.patch.UNSET:
		changed["title"] = subroutine.domain.text.fit(
			subroutine.domain.text.require(title, field="title"),
			field="title",
			limit=MAX_TITLE_LENGTH,
		)

	if description is not subroutine.domain.patch.UNSET:
		changed["description"] = description

	if timezone is not subroutine.domain.patch.UNSET:
		# Validated on the way *in*. Unvalidated, a bad zone is stored happily and then fails
		# on every later date computation with a 422 naming the *request's* timezone — a message
		# about the wrong thing entirely, arriving days after the mistake.
		if timezone is not None:
			subroutine.domain.dates.zone(timezone)

		changed["timezone"] = timezone

	before = {name: getattr(workspace, name) for name in changed}

	for name, value in changed.items():
		setattr(workspace, name, value)

	differences = subroutine.domain.events.changes_between(before, changed)

	if differences:
		# **Bumped by hand, because `VersionMixin` is a plain column.** There is no
		# `version_id_col` on the mapper, so nothing increments it for us — every service that
		# mutates has to, and the one that forgets leaves §8.9's check comparing a number that
		# never moves. Which means `expected_version` silently *passes* for every stale caller:
		# the failure mode is a concurrency guard that is present, exercised, and useless.
		workspace.version += 1

		subroutine.domain.events.record(
			session,
			workspace_id=workspace.id,
			entity_type="workspace",
			entity_id=workspace.id,
			action=subroutine.domain.events.EventAction.UPDATED,
			changes=differences,
			actor=actor,
		)

	session.flush()

	return workspace


def record_seeding (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	report: subroutine.db.seed.SeedReport,
	*,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> None:
	"""Record that a workspace's vocabulary was seeded, if anything was written.

	One event carrying the version and the counts, rather than ~35 events for individual
	role and status rows — a change feed whose first page is entirely vocabulary is exactly
	the noise the feed exists to cut through (SPEC.md §10.7 invariant 9).

	It matters most on the path that has no creation event to stand in for it: a later
	release seeding new rows into a workspace that already exists. Without this, those rows
	appear with nothing in the history to say where they came from.
	"""

	if report.total == 0:
		return

	subroutine.domain.events.record(
		session,
		workspace_id=workspace.id,
		entity_type="workspace",
		entity_id=workspace.id,
		action=subroutine.domain.events.EventAction.SEEDED,
		changes={
			"seed_version": {"from": report.from_version, "to": report.to_version},
			"roles": {"from": None, "to": report.roles},
			"statuses": {"from": None, "to": report.statuses},
			"item_types": {"from": None, "to": report.item_types},
			"link_types": {"from": None, "to": report.link_types},
		},
		actor=actor,
	)


def add_member (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	user: subroutine.db.models.identity.User,
	*,
	role_key: str,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.identity.WorkspaceMember:
	"""Give a user a role in a workspace.

	**Requires ``workspace:admin``, and did not check anything at all until `#188`.** It took an
	actor, recorded an event attributed to it, and performed no check — while `CLAUDE.md`'s list
	of the services that check permissions named this one explicitly. It was not exploitable
	only because nothing reached it: no endpoint, no command. `#174` is what changes that, which
	is why this is being fixed in the same sitting rather than after.

	``workspace:admin`` rather than ``workspace:write``, because deciding who belongs in a
	workspace is not the same act as doing work in one — and a member who can add members can
	grant themselves anything the roles allow.
	"""

	if actor is not None:
		# **`user:admin`, which is what both published descriptions of it say** (`#930`).
		# `permissions.py` calls it "managing who belongs to this workspace — inviting,
		# removing, changing a member's role" and `COVERAGE` says "who belongs to this
		# workspace"; this checked `workspace:admin`, so the verb named for the job gated
		# nothing and the verb named for something else did it.
		#
		# **A no-op for every role and not for a token.** Measured: `workspace:admin`,
		# `user:admin` and `token:admin` are held by owner and admin and by nobody else, so
		# no seeded role changes hands here — but a token scoped `user:admin` could not
		# administer membership and one scoped `workspace:admin` could, which is backwards
		# from what an operator reading either description would expect.
		subroutine.domain.authorization.authorize(
			session,
			actor,
			subroutine.permissions.USER_ADMIN,
			workspace_id=workspace.id,
		)

	role = find_role(session, workspace.id, role_key)

	membership = subroutine.db.models.identity.WorkspaceMember(
		workspace_id=workspace.id, user_id=user.id, role_id=role.id
	)
	session.add(membership)
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=workspace.id,
		entity_type="workspace_member",
		entity_id=membership.id,
		action=subroutine.domain.events.EventAction.CREATED,
		changes={"user_id": {"from": None, "to": user.id}, "role": {"from": None, "to": role_key}},
		actor=actor,
	)

	return membership


#: One membership with the three rows it joins, already loaded. Returned instead of the bare
#: join row so that rendering a listing is not `#39`'s N+1 with a different name on it.
Membership = tuple[
	subroutine.db.models.identity.WorkspaceMember,
	subroutine.db.models.identity.User,
	subroutine.db.models.identity.Role,
]


def members (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	*,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> list[Membership]:
	"""Return who belongs to this workspace and with what role — item ``#174``.

	Requires ``workspace:read``: knowing who you are working alongside is part of working in a
	workspace, and it is the question anybody about to add or remove somebody has to ask first.
	"""

	if actor is not None:
		subroutine.domain.authorization.authorize(
			session,
			actor,
			subroutine.permissions.WORKSPACE_READ,
			workspace_id=workspace.id,
		)

	member = subroutine.db.models.identity.WorkspaceMember
	account = subroutine.db.models.identity.User
	role = subroutine.db.models.identity.Role

	rows = session.execute(
		sqlalchemy.select(member, account, role)
		.join(account, account.id == member.user_id)
		.join(role, role.id == member.role_id)
		.where(member.workspace_id == workspace.id, account.deleted_at.is_(None))
		.order_by(account.created_at, account.username)
	).all()

	return [(found, holder, held) for found, holder, held in rows]


def remove_member (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	user: subroutine.db.models.identity.User,
	*,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> None:
	"""Take somebody out of a workspace — item ``#174``.

	**Worth having beside ``add_member`` rather than later**, for the reason `#140` gives about
	anything that can be added: somebody added by mistake sees a private project they should
	not, and a membership that can only be granted is one whose mistakes are permanent.

	**The last administrator cannot be removed.** A workspace nobody can administer is one where
	the remedy for every later mistake — including this one — has been thrown away, and it
	cannot be undone from inside. Refused with the count, so the operator can see what they are
	being told rather than only that they were told something.
	"""

	if actor is not None:
		subroutine.domain.authorization.authorize(
			session,
			actor,
			subroutine.permissions.USER_ADMIN,
			workspace_id=workspace.id,
		)

	model = subroutine.db.models.identity.WorkspaceMember
	found = session.scalars(
		sqlalchemy.select(model).where(
			model.workspace_id == workspace.id, model.user_id == user.id
		)
	).first()

	if found is None:
		raise subroutine.errors.NotFound(
			f"{user.username} is not a member of {workspace.slug}.",
			hint=f"Run 'subroutine workspace members {workspace.slug}' to see who is.",
		)

	_refuse_removing_the_last_administrator(session, workspace, found)

	subroutine.domain.events.record(
		session,
		workspace_id=workspace.id,
		entity_type="workspace_member",
		entity_id=found.id,
		action=subroutine.domain.events.EventAction.DELETED,
		changes={"user_id": {"from": user.id, "to": None}},
		actor=actor,
	)

	session.delete(found)
	session.flush()


def _refuse_removing_the_last_administrator (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	going: subroutine.db.models.identity.WorkspaceMember,
) -> None:
	"""Refuse a removal that would leave a workspace with nobody able to administer it."""

	member = subroutine.db.models.identity.WorkspaceMember
	role = subroutine.db.models.identity.Role

	# One query for every membership's permissions, not one per membership. The obvious
	# version of this asks the database once per row and is `#39`'s N+1 on the path of a
	# command somebody runs while tidying up a team.
	rows = session.execute(
		sqlalchemy.select(member.id, role.permissions)
		.join(role, role.id == member.role_id)
		.where(member.workspace_id == workspace.id)
	).all()

	administrators = {
		found
		for found, permissions in rows
		if subroutine.permissions.WORKSPACE_ADMIN in (permissions or [])
	}

	if going.id not in administrators or administrators - {going.id}:
		return

	raise subroutine.errors.ValidationError(
		f"{workspace.slug} would be left with nobody who can administer it.",
		hint=(
			"Give somebody else an administrator's role there first. A workspace with no "
			"administrator cannot be repaired from inside it."
		),
	)


def readable (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
) -> list[subroutine.db.models.identity.Workspace]:
	"""Return every workspace this principal may read, oldest first.

	Membership is what grants reach, so this is the member rows joined to their workspaces.
	A token pinned to one workspace narrows the result to that one — which is the whole
	point of pinning, and doing it here means every caller inherits it rather than each
	remembering to (SPEC.md §7.3).
	"""

	member = subroutine.db.models.identity.WorkspaceMember
	workspace = subroutine.db.models.identity.Workspace

	statement = (
		sqlalchemy.select(workspace)
		.join(member, member.workspace_id == workspace.id)
		.where(member.user_id == principal.user.id, workspace.deleted_at.is_(None))
		.order_by(workspace.created_at)
	)

	if principal.pinned_workspace_id is not None:
		statement = statement.where(workspace.id == principal.pinned_workspace_id)

	return list(session.scalars(statement))


def find_role (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, key: str
) -> subroutine.db.models.identity.Role:
	"""Return a workspace's role by key, or say which keys exist."""

	model = subroutine.db.models.identity.Role

	role = session.scalars(
		sqlalchemy.select(model).where(model.workspace_id == workspace_id, model.key == key)
	).one_or_none()

	if role is not None:
		return role

	available = sorted(
		session.scalars(sqlalchemy.select(model.key).where(model.workspace_id == workspace_id))
	)

	raise subroutine.errors.ValidationError(
		f"This workspace has no role called {key!r}.",
		errors=[
			subroutine.errors.FieldError(
				field="role",
				code="not_found",
				message=f"No role with key {key!r} exists in this workspace.",
				hint=f"Roles here: {', '.join(available)}." if available else None,
			)
		],
	)


def validated_slug (
	session: sqlalchemy.orm.Session,
	slug: str,
	*,
	except_id: uuid.UUID | None = None,
) -> str:
	"""Return the stored form of a workspace short name, or refuse and say which rule it broke.

	**One copy, shared by ``create`` and ``update``** (`#295`). A rename has to arrive at a
	name creation would have accepted, or the two paths drift and renaming becomes the way to
	get a slug nobody could have chosen. Five rules, in the order a reader meets them: it must
	contain something usable, fit the length, begin with a letter, not be a reserved word, and
	not already be in use.

	``except_id`` is the workspace being renamed, which must not collide with itself. Renaming
	``acme`` to ``acme`` is a no-op rather than a duplicate-key conflict — and that call
	arrives from any client that sends every field whether or not it changed.
	"""

	normalized = normalize_slug(slug)

	if not normalized:
		raise subroutine.errors.ValidationError(
			"A workspace needs a short name made of letters, numbers and hyphens.",
			errors=[
				subroutine.errors.FieldError(
					field="slug",
					code="invalid_field_value",
					message=f"{slug!r} contains nothing usable as a short name.",
					hint="Try something like 'home' or 'acme-engineering'.",
				)
			],
		)

	normalized = subroutine.domain.text.fit(
		normalized, field="slug", limit=MAX_SLUG_LENGTH, label="short name"
	)

	# **A short name must begin with a letter, and this is structural rather than cosmetic.**
	# §5.4 forces the same on a project key so that `/v1/tasks/42` and `/v1/projects/WEB`
	# cannot be confused; §13.7 then made a workspace slug the middle segment of
	# `connection/workspace/ref`, and gave it no equivalent rule. A slug of `2026` reads as a
	# number wherever an address is written, and `subroutine use 2026` is a sentence nobody can
	# parse at a glance.
	if not normalized[0].isascii() or not normalized[0].isalpha():
		raise subroutine.errors.ValidationError(
			"A workspace's short name has to start with a letter.",
			errors=[
				subroutine.errors.FieldError(
					field="slug",
					code="invalid_field_value",
					message=f"{normalized!r} starts with {normalized[0]!r}.",
					hint="A short name is part of an address — 'work/acme/42' — so it must not "
					"read as a number. Try 'acme' or 'q3-planning'.",
				)
			],
		)

	if subroutine.addressing.is_reserved_workspace_word(normalized):
		raise subroutine.errors.ValidationError(
			f"{normalized!r} cannot be a workspace's short name.",
			errors=[
				subroutine.errors.FieldError(
					field="slug",
					code="invalid_field_value",
					message=f"{normalized!r} is reserved, because it means something else in "
					"an address or a command.",
					hint="Reserved short names: "
					f"{', '.join(sorted(subroutine.addressing.RESERVED_WORKSPACE_WORDS))}.",
				)
			],
		)

	if _slug_taken(session, normalized, except_id=except_id):
		raise subroutine.errors.Conflict(
			f"A workspace called {normalized!r} already exists.",
			code="duplicate_key",
			errors=[
				subroutine.errors.FieldError(
					field="slug",
					code="duplicate_key",
					message=f"The short name {normalized!r} is already in use.",
				)
			],
		)


	return normalized


def normalize_slug (slug: str) -> str:
	"""Return the stored form of a workspace short name."""

	kept = [character if character.isalnum() else "-" for character in slug.strip().lower()]
	collapsed = "".join(kept).strip("-")

	while "--" in collapsed:
		collapsed = collapsed.replace("--", "-")

	return collapsed


def _slug_taken (
	session: sqlalchemy.orm.Session, slug: str, *, except_id: uuid.UUID | None = None
) -> bool:
	"""Report whether a live workspace already uses this short name.

	Deleted workspaces are ignored, matching the partial unique index: a name in the trash
	is available again. ``except_id`` exempts the workspace asking, so renaming one to the
	name it already has is not a conflict with itself.
	"""

	model = subroutine.db.models.identity.Workspace
	statement = sqlalchemy.select(model.id).where(
		model.slug == slug, model.deleted_at.is_(None)
	)

	if except_id is not None:
		statement = statement.where(model.id != except_id)

	return session.scalars(statement).first() is not None
