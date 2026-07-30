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

	# Same check as `update`, and it was missing here: `create` took whatever string it was
	# handed, so `{"timezone": "Mars/Olympus"}` was stored and only surfaced later, as a
	# refusal that named the caller's request rather than the workspace holding the bad value.
	subroutine.domain.dates.zone(timezone)

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

	if _slug_taken(session, normalized):
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
	session.flush()

	return workspace


def update (
	session: sqlalchemy.orm.Session,
	workspace: subroutine.db.models.identity.Workspace,
	*,
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

	**The slug is deliberately not changeable**, for the same reason a project key is not. It is
	the middle segment of every address this workspace's items are written as —
	``work/acme/#42`` (§13.7) — and those strings are in other people's notes, in shell history
	and in `config.toml` files on other machines. Renaming it here would not rewrite them.

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
	"""Give a user a role in a workspace."""

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


def normalize_slug (slug: str) -> str:
	"""Return the stored form of a workspace short name."""

	kept = [character if character.isalnum() else "-" for character in slug.strip().lower()]
	collapsed = "".join(kept).strip("-")

	while "--" in collapsed:
		collapsed = collapsed.replace("--", "-")

	return collapsed


def _slug_taken (session: sqlalchemy.orm.Session, slug: str) -> bool:
	"""Report whether a live workspace already uses this short name.

	Deleted workspaces are ignored, matching the partial unique index: a name in the trash
	is available again.
	"""

	model = subroutine.db.models.identity.Workspace

	return (
		session.scalars(
			sqlalchemy.select(model.id).where(model.slug == slug, model.deleted_at.is_(None))
		).first()
		is not None
	)
