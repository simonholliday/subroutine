"""Creating the tenancy root, and the one member it cannot exist without.

A workspace is invisible to a person using Subroutine alone — ``subroutine init`` makes
one and never mentions it again (SPEC.md §1.4). It matters here because everything else
hangs off it: the vocabulary, the roles, and the rule that every query is scoped by it.
"""

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.seed
import subroutine.domain.authentication
import subroutine.domain.events
import subroutine.domain.text
import subroutine.errors

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
