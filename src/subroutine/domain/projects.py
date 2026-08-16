"""Creating and moving projects — the container for work and the unit of permission.

A project is a node in a tree, and moving one takes its whole subtree with it. That is the
only genuinely awkward operation in this module, and it is awkward for a reason worth
keeping: the materialised path that makes "everything under here" cheap to read is what
makes moving expensive to write, and reads outnumber moves by a very wide margin.
"""

import datetime
import re
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.addressing
import subroutine.db.mixins
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.types
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.events
import subroutine.domain.hierarchy
import subroutine.domain.patch
import subroutine.domain.scoping
import subroutine.domain.text
import subroutine.domain.versions
import subroutine.errors
import subroutine.permissions

#: The shape a project key must take. It is deliberately identical to the ref half of
#: ``subroutine.domain.mentions.REF_PATTERN``, and that is the whole reason it is
#: constrained: a key is a path segment in `/v1/projects/WEB`, a `+WEB` in a capture line
#: and a word people type, so it has to be unambiguous in all three.
#:
#: **It is not part of a ref, and this comment said it was until `#176`.** That was true
#: under `SR-42` and stopped being true on 2026-07-29, when §6.2 made a ref a bare
#: workspace-scoped integer. The rule survived its own reasoning in four places at once.
#:
#: The cost is that a key must be ASCII — 'CAFÉ' is refused. Titles, descriptions, tags and
#: comments are all fully Unicode; only this one identifier is not, because it is the piece
#: that ends up in commit messages, chat and URLs, where being typeable matters more.
#: How long a key may be. **Shorter than a workspace slug's 64 on purpose**: a slug is typed
#: when somebody switches context, and a key is typed in every captured line that mentions a
#: project (`+web-sales`). Thirty-two is double the longest anybody has needed and still fits
#: the hyphenated compounds `#508` added it for — `service-marketing` is seventeen.
MAX_KEY_LENGTH = 32

#: What a project key may be — lower case, digits and interior hyphens (`#508`).
#:
#: **Lower case, matching a workspace slug.** §13.7 writes an address as
#: ``connection/workspace/ref`` and a slug has always been lower case, so keys being shouted
#: made the two halves of one address obey different conventions for no reason a reader could
#: infer. Nothing ever depended on the case — measured: no parser, resolver or address in
#: ``addressing.py`` or ``selection.py`` branches on it.
#:
#: **Hyphens between things, never at an edge and never doubled.** The alternation says that
#: structurally rather than by a second check, so ``web-`` and ``a--b`` are refused by the same
#: rule that accepts ``web-sales`` — a key that renders as two words when it is one is the
#: confusion the hyphen was added to remove.
KEY_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")

#: What a template writes into ``project.settings``, and nothing else (SPEC.md §6.12).
#: Templates are seed-time only: they set defaults and then have no further effect, so a
#: project stays reconfigurable and no template is a cage.
#:
#: **A template may only write a setting something reads** (`#133`). Until 2026-07-31 the
#: software one wrote ``require_verification_to_complete: True`` and nothing anywhere read it,
#: which is two defects rather than one. It was a claim stored in the data — a caller reading
#: `project.settings` was told completion is gated, and it is not — and it was a behaviour
#: change waiting to fire, because building verification would have made every project ever
#: created from that template start refusing completions, arriving with a release about
#: something else and looking exactly like a regression. Nobody chose that; a template did,
#: possibly months earlier.
#:
#: The rule generalises past the instance: **a setting for an unbuilt feature belongs with the
#: feature.** The other unbuilt things here — `db export --format json`, calendar feeds,
#: recurrence — are parsed and *honestly refused*; a stored value cannot refuse, so it should
#: not be stored. `config.Settings` keeps the instance-level default, which is `False` and so
#: claims nothing, and is where the setting will land when there is something to switch on.
TEMPLATES: dict[str, dict[str, typing.Any]] = {
	"personal": {"visible_status_keys": ["open", "done"]},
	"software": {
		"visible_status_keys": [
			"open",
			"in_progress",
			"blocked",
			"needs_input",
			"done",
			"cancelled",
		]
	},
	"blank": {"visible_status_keys": ["open", "done"]},
}


def create (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	key: str,
	title: str,
	description: str | None = None,
	parent: subroutine.db.models.project.Project | None = None,
	template: str = "blank",
	visibility: str = "public",
	owner_id: uuid.UUID | None = None,
	is_inbox: bool = False,
	max_depth: int = subroutine.domain.hierarchy.DEFAULT_MAX_DEPTH,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.project.Project:
	"""Create a project, placed in the tree and stamped with its template's defaults."""

	title = subroutine.domain.text.fit(
		subroutine.domain.text.require(title, field="title"), field="title", limit=512
	)
	_permitted(session, actor, subroutine.permissions.PROJECT_WRITE, workspace_id=workspace_id)

	normalized_key = normalize_key(key)

	check_key(normalized_key, given=key)

	if template not in TEMPLATES:
		raise subroutine.errors.ValidationError(
			f"There is no {template!r} project template.",
			errors=[
				subroutine.errors.FieldError(
					field="template",
					code="invalid_field_value",
					message=f"Unknown template {template!r}.",
					hint=f"Available templates: {', '.join(sorted(TEMPLATES))}.",
				)
			],
		)

	if visibility not in subroutine.db.mixins.PROJECT_VISIBILITIES:
		raise subroutine.errors.ValidationError(
			f"A project is public or private, not {visibility!r}.",
			errors=[
				subroutine.errors.FieldError(
					field="visibility",
					code="invalid_field_value",
					message=f"Unknown visibility {visibility!r}.",
					hint=f"Valid values: {', '.join(subroutine.db.mixins.PROJECT_VISIBILITIES)}.",
				)
			],
		)

	if parent is not None and parent.workspace_id != workspace_id:
		raise subroutine.errors.ValidationError(
			"A parent project must be in the same workspace.",
			errors=[
				subroutine.errors.FieldError(
					field="parent_id",
					code="invalid_field_value",
					message="That project belongs to a different workspace.",
				)
			],
		)

	_refuse_duplicate_key(session, workspace_id, normalized_key)

	project = subroutine.db.models.project.Project(
		id=subroutine.db.types.new_uuid(),
		workspace_id=workspace_id,
		parent_id=None if parent is None else parent.id,
		visibility=visibility,
		key=normalized_key,
		title=title,
		description=description,
		status_id=default_status(session, workspace_id).id,
		owner_id=owner_id,
		is_inbox=is_inbox,
		template=template,
		settings=dict(TEMPLATES[template]),
		path="",
		depth=0,
		created_by=None if actor is None else actor.user.id,
	)
	subroutine.domain.hierarchy.place(project, parent, max_depth=max_depth)

	session.add(project)
	session.flush()

	if owner_id is not None:
		# **An owner is a member of their own project.** Without this row a private project
		# is invisible to the person who created it: §7.3a grants sight of a private project
		# to holders of a ``project_member`` row and to nobody else, and until now nothing
		# anywhere in the application ever wrote one — the only rows in existence were the
		# ones the tests inserted by hand, which is why the feature looked covered.
		#
		# Written for public projects too, so that making a project private later does not
		# lock its owner out of it. ``role_id`` stays null, which means "keeps whatever role
		# they hold at workspace level" rather than granting anything new.
		_ensure_member(session, project, owner_id)

	subroutine.domain.events.record(
		session,
		workspace_id=workspace_id,
		entity_type="project",
		entity_id=project.id,
		action=subroutine.domain.events.EventAction.CREATED,
		changes={"key": {"from": None, "to": normalized_key}, "title": {"from": None, "to": title}},
		actor=actor,
	)
	session.flush()

	return project


def move (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	*,
	parent: subroutine.db.models.project.Project | None,
	max_depth: int = subroutine.domain.hierarchy.DEFAULT_MAX_DEPTH,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> int:
	"""Move a project and everything under it, returning how many rows were rewritten."""

	_permitted(session, actor, subroutine.permissions.PROJECT_WRITE, project=project)

	subroutine.domain.versions.require(project, expected_version, noun="project")

	previous_parent = project.parent_id
	previous_path = project.path

	moved = subroutine.domain.hierarchy.reparent(
		session, subroutine.db.models.project.Project, project, parent, max_depth=max_depth
	)

	if moved == 0:
		return 0

	project.parent_id = None if parent is None else parent.id

	# `version` is the ETag (SPEC.md §8.9), so anything a client can read has to move it.
	# `reparent` rewrote `path` and `depth` on this row and every descendant with one Core
	# UPDATE, which sets no version — so the descendants are bumped here too, or a client
	# holding an ETag for a child cannot tell that the child's path changed.
	project.version += 1
	project.updated_by = None if actor is None else actor.user.id

	model = subroutine.db.models.project.Project
	session.execute(
		sqlalchemy.update(model)
		.where(
			model.workspace_id == project.workspace_id,
			subroutine.domain.hierarchy.subtree(model, project),
			model.id != project.id,
		)
		.values(version=model.version + 1, updated_by=project.updated_by)
		.execution_options(synchronize_session=False)
	)
	session.expire_all()
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=project.workspace_id,
		entity_type="project",
		entity_id=project.id,
		action=subroutine.domain.events.EventAction.MOVED,
		changes={
			"parent_id": {"from": previous_parent, "to": project.parent_id},
			"path": {"from": previous_path, "to": project.path},
			"descendants_rewritten": {"from": None, "to": moved - 1},
		},
		actor=actor,
	)
	session.flush()

	return moved


def update (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	*,
	key: str = subroutine.domain.patch.UNSET,
	title: str = subroutine.domain.patch.UNSET,
	description: str | None = subroutine.domain.patch.UNSET,
	visibility: str = subroutine.domain.patch.UNSET,
	owner_id: uuid.UUID | None = subroutine.domain.patch.UNSET,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.project.Project:
	"""Change a project, recording only what actually changed.

	Anything left at ``subroutine.domain.patch.UNSET`` is untouched; passing ``None`` clears the field. **Everything
	is validated before anything is assigned**, for the reason ``tasks.update`` gives: the
	caller holds a live session it may still commit, so a half-applied change that raised on
	the way through would be committed silently alongside whatever else was in flight.

	**The key may be changed, and the reason it could not be was false** (`#176`). Four
	surfaces said it is "the first half of every ref the project mints" — this docstring among
	them. That stopped being true on 2026-07-29, when §6.2 made a ref a bare workspace-scoped
	integer allocated from `workspace.next_ref_number`. A project key is in no ref at all.

	What a rename really costs is addresses, not identifiers: `/v1/projects/WEB` as a URL
	somebody cached, `project = "WEB"` in a `.subroutine` marker in another checkout, `+WEB` in
	a capture line and in shell history. `project.id` is a UUID and does not move, so nothing
	joined to this project is disturbed.

	**Simon's decision, 2026-08-01: the old key simply stops working, and we say so loudly.**
	No alias, because an alias keeps a name resolving that its owner deliberately retired. The
	loud half belongs to the surfaces — this records the change and the event; the confirmation
	that says what will break is the CLI's, where somebody can still say no.
	"""

	_permitted(
		session, actor, subroutine.permissions.PROJECT_WRITE, project=project
	)
	subroutine.domain.versions.require(project, expected_version, noun="This project")

	# Validation pass. Nothing below this point may raise.
	cleaned_title: typing.Any = subroutine.domain.patch.UNSET
	cleaned_key: typing.Any = subroutine.domain.patch.UNSET

	if key is not subroutine.domain.patch.UNSET:
		# Through the same checks creation uses, so a key that could not have been chosen
		# cannot be arrived at by renaming — the shape, the reserved words and the duplicate.
		cleaned_key = normalize_key(key)

		check_key(cleaned_key, given=key)

		if cleaned_key != project.key:
			_refuse_duplicate_key(session, project.workspace_id, cleaned_key)

	if title is not subroutine.domain.patch.UNSET:
		cleaned_title = subroutine.domain.text.fit(
			subroutine.domain.text.require(title, field="title"), field="title", limit=512
		)

	if visibility is not subroutine.domain.patch.UNSET and visibility not in subroutine.db.mixins.PROJECT_VISIBILITIES:
		raise subroutine.errors.ValidationError(
			f"A project is public or private, not {visibility!r}.",
			errors=[
				subroutine.errors.FieldError(
					field="visibility",
					code="invalid_field_value",
					message=f"Unknown visibility {visibility!r}.",
					hint=f"Valid values: {', '.join(subroutine.db.mixins.PROJECT_VISIBILITIES)}.",
				)
			],
		)

	if owner_id is not subroutine.domain.patch.UNSET and owner_id is not None:
		owner = session.get(subroutine.db.models.identity.User, owner_id)

		if owner is None:
			raise subroutine.errors.ValidationError(
				"That owner does not exist.",
				errors=[
					subroutine.errors.FieldError(
						field="owner_id",
						code="not_found",
						message=f"No user with id {owner_id}.",
					)
				],
			)

	# Assignment pass.
	changes: dict[str, typing.Any] = {}

	for field, value in (
		("key", cleaned_key),
		("title", cleaned_title),
		("description", description),
		("visibility", visibility),
		("owner_id", owner_id),
	):
		if value is subroutine.domain.patch.UNSET:
			continue

		previous = getattr(project, field)

		if previous == value:
			continue

		setattr(project, field, value)
		changes[field] = {"from": previous, "to": value}

	if not changes:
		# An update that changes nothing writes no event, so the change feed stays a record
		# of changes rather than of requests.
		return project

	if "owner_id" in changes and project.owner_id is not None:
		_ensure_member(session, project, project.owner_id)

	project.version += 1
	project.updated_by = None if actor is None else actor.user.id
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=project.workspace_id,
		entity_type="project",
		entity_id=project.id,
		action=subroutine.domain.events.EventAction.UPDATED,
		changes=changes,
		actor=actor,
	)
	session.flush()

	return project


def delete (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	*,
	now: datetime.datetime | None = None,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.project.Project:
	"""Move a project to the trash, where it stays recoverable (SPEC.md §6.9).

	Soft, and idempotent: when something was thrown away is a fact worth not overwriting.
	Its tasks are not touched, and do not need to be — every listing joins the project and
	excludes deleted ones, so they leave the visible world with it and come back with it.
	"""

	_permitted(
		session, actor, subroutine.permissions.PROJECT_DELETE, project=project
	)
	subroutine.domain.versions.require(project, expected_version, noun="This project")

	if project.deleted_at is not None:
		return project

	if project.is_inbox:
		raise subroutine.errors.ValidationError(
			"The Inbox cannot be deleted.",
			errors=[
				subroutine.errors.FieldError(
					field="project",
					code="invalid_field_value",
					message="This is the project tasks are filed in when they have no other, "
					"so a workspace is not usable without it.",
				)
			],
		)

	project.deleted_at = now if now is not None else subroutine.db.types.utcnow()
	project.version += 1
	project.updated_by = None if actor is None else actor.user.id
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=project.workspace_id,
		entity_type="project",
		entity_id=project.id,
		action=subroutine.domain.events.EventAction.DELETED,
		actor=actor,
	)
	session.flush()

	return project


def keys_for (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	identifiers: typing.Sequence[str],
) -> list[str]:
	"""Return each identifier as the project key it names, or unchanged where it names nothing.

	Built for ``token list`` (`#203`), which printed a credential's ``project_scope`` as raw
	UUIDs on the line *below* one that resolves the workspace pin to its slug — same output,
	same argument, applied once. A UUID in a listing is something to go and look up, which is
	the opposite of what a listing is for.

	**Here rather than in the command**, because it narrows: a key discloses more than an id,
	so resolving one for a reader who cannot see that project would turn a listing of their own
	credentials into a way of learning a private project's name. Narrowing belongs beside the
	other narrowing, and ``tests/test_scoping.py`` is what said so.

	Anything that does not resolve — malformed, deleted, or simply not visible — is passed
	through as it was stored. A listing whose job is "what can this credential reach" must never
	report a *narrower* reach than the credential has.
	"""

	spaces = list(
		session.scalars(sqlalchemy.select(subroutine.db.models.identity.Workspace.id))
	)
	named: list[str] = []

	for identifier in identifiers:
		try:
			wanted = uuid.UUID(str(identifier))

		except ValueError:
			named.append(identifier)

			continue

		found = session.scalars(
			subroutine.domain.scoping.readable_projects(
				principal,
				workspace_ids=spaces,
				include_deleted=True,
				include_archived=True,
				# **The one place a read scope is not applied, and the reason is whose rows
				# these are** (`#930`). Every id here came out of the caller's *own* token, so
				# naming them discloses nothing they do not already hold — where a listing of
				# `/v1/projects` would. Enforcing it here would mean a credential narrowed to
				# `task:read` could not run `whoami` or read `/v1/me`, the two things built to
				# tell an agent the truth about itself, and `#203`'s whole point was that a
				# reader should see `inbox` rather than a UUID to go and look up.
				#
				# The *visibility* narrowing above still applies, which is what this function's
				# docstring is about: a private project somebody cannot see is still passed
				# through as its stored id.
				enforce_read_scope=False,
			).where(subroutine.db.models.project.Project.id == wanted)
		).first()

		named.append(identifier if found is None else found.key)

	return named


def restore (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	*,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.project.Project:
	"""Take a project back out of the trash, and its contents with it (SPEC.md §6.9).

	**:func:`delete` has always promised this and nothing provided it** (`#308`). Its docstring
	says a project's tasks "leave the visible world with it and come back with it", which is a
	statement about a journey only half of which existed: `#140` gave tasks and documents a
	restore and did not give one to the container both of them hang off. So deleting a project
	took every item inside it out of sight permanently, by a route that looked reversible.

	Nothing here touches the contents, for the same reason ``delete`` does not: every listing
	joins the project, so undeleting the project is the whole of undeleting what is in it. That
	is what makes this a one-row write rather than a cascade to reverse.

	The same permission as deleting, and the same symmetry: restoring twice is not an error, and
	neither call moves a timestamp that is already where it belongs.
	"""

	_permitted(
		session, actor, subroutine.permissions.PROJECT_DELETE, project=project
	)
	subroutine.domain.versions.require(project, expected_version, noun="This project")

	if project.deleted_at is None:
		return project

	# An ancestor still in the trash would put this row back into a subtree nobody can see, so
	# the caller would be told it worked and nothing would appear. Refused by name instead.
	buried = _deleted_ancestor(session, project)

	if buried is not None:
		raise subroutine.errors.ValidationError(
			f"'{project.key}' is inside '{buried.key}', which is also in the trash.",
			hint=f"Restore '{buried.key}' first, and this comes back with it.",
		)

	project.deleted_at = None

	# A restore is a change, and §8.9's guard compares a number that has to move or it silently
	# passes for every caller reading stale state.
	project.version += 1
	project.updated_by = None if actor is None else actor.user.id
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=project.workspace_id,
		entity_type="project",
		entity_id=project.id,
		action=subroutine.domain.events.EventAction.RESTORED,
		actor=actor,
	)
	session.flush()

	return project


def _deleted_ancestor (
	session: sqlalchemy.orm.Session, project: subroutine.db.models.project.Project
) -> subroutine.db.models.project.Project | None:
	"""Return the nearest ancestor still in the trash, or ``None`` when the way up is clear."""

	model = subroutine.db.models.project.Project
	current = project

	while current.parent_id is not None:
		parent = session.get(model, current.parent_id)

		if parent is None:
			return None

		if parent.deleted_at is not None:
			return parent

		current = parent

	return None


def _ensure_member (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	user_id: uuid.UUID,
) -> None:
	"""Make sure a user holds a membership row for a project, without duplicating one.

	Called when ownership changes, for the same reason :func:`create` writes one: §7.3a
	grants sight of a private project to members and to nobody else, so handing somebody a
	private project without this would hand them one they cannot see.
	"""

	model = subroutine.db.models.project.ProjectMember

	existing = session.scalars(
		sqlalchemy.select(model).where(
			model.project_id == project.id, model.user_id == user_id
		)
	).first()

	if existing is not None:
		return

	session.add(
		model(
			workspace_id=project.workspace_id,
			project_id=project.id,
			user_id=user_id,
			role_id=None,
		)
	)
	session.flush()


def default_status (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID
) -> subroutine.db.models.vocabulary.Status:
	"""Return the status a new project is given."""

	model = subroutine.db.models.vocabulary.Status

	status = session.scalars(
		sqlalchemy.select(model)
		.where(
			model.workspace_id == workspace_id,
			model.entity_type == "project",
			model.is_default.is_(True),
		)
		.order_by(model.position)
	).first()

	if status is None:
		raise subroutine.errors.ValidationError(
			"This workspace has no default project status.",
			code="invalid_status",
			hint="Seed the workspace, or mark one project status as the default.",
		)

	return status


def _permitted (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	permission: str,
	*,
	project: subroutine.db.models.project.Project | None = None,
	workspace_id: uuid.UUID | None = None,
) -> None:
	"""Check that an actor may do this, or raise. ``None`` is an internal caller.

	See ``domain.tasks._permitted`` for why the ``None`` case is a skip and what stops it
	being a silent hole.

	Pass ``project`` whenever there is one. Checking against the workspace alone skips the
	two rules that are about the individual project — whether it is private and out of
	sight, and whether the token's ``project_scope`` admits it — so an existing project must
	never be checked by workspace id. Only :func:`create`, where there is no project yet,
	has any business doing that.
	"""

	if actor is None:
		return

	scope = workspace_id if project is None else project.workspace_id

	if scope is None:
		raise ValueError("A workspace or a project is needed to check a permission against.")

	subroutine.domain.authorization.authorize(
		session, actor, permission, workspace_id=scope, project=project
	)


def check_key (normalized_key: str, *, given: str | None = None) -> None:
	"""Refuse a project key that cannot be used, saying which rule it broke.

	Extracted so that creating a project and **renaming** one apply the same rules (`#176`).
	Before that there was only one caller and the checks were inline; a rename that skipped
	them could arrive at a key nobody could have chosen in the first place, which is a worse
	state than either command allows on its own.

	``given`` is what the caller actually typed, for the message. ``'café'`` normalises to
	``'CAFÉ'`` and is refused; telling somebody ``'CAFÉ'`` is not usable when they wrote
	something else reads as the program mangling their input and then blaming them for it.
	"""

	wrote = given if given is not None else normalized_key

	# **Length before shape**, so "too long" is not reported as "not a usable key". The
	# pattern has no bound of its own since `#508`: expressing one inside an alternation that
	# already forbids edge and doubled hyphens makes both rules unreadable.
	if len(normalized_key) > MAX_KEY_LENGTH:
		raise subroutine.errors.ValidationError(
			f"{wrote!r} cannot be used as a project key.",
			errors=[
				subroutine.errors.FieldError(
					field="key",
					code="invalid_field_value",
					message=f"A key may be up to {MAX_KEY_LENGTH} characters and that is "
					f"{len(normalized_key)}.",
					hint="A key is typed in every line that mentions the project, so shorter "
					"is kinder — 'web-sales' rather than 'website-sales-and-marketing'.",
				)
			],
		)

	if not KEY_PATTERN.fullmatch(normalized_key):
		raise subroutine.errors.ValidationError(
			f"{wrote!r} cannot be used as a project key.",
			errors=[
				subroutine.errors.FieldError(
					field="key",
					code="invalid_field_value",
					message=f"{wrote!r} contains nothing usable as a key."
					if not normalized_key
					else f"{normalized_key!r} is not a usable key.",
					hint="A key is lower case: a letter, then letters, digits and hyphens "
					"between them — 'sr', 'home', 'web-sales'.",
				)
			],
		)

	# A key becomes a path segment, and some segments belong to an endpoint. Refused here
	# rather than at the API, because the alternative is a project that exists, is listed,
	# and cannot be opened — and because the CLI can create one without an API in sight.
	if subroutine.addressing.is_reserved_word(normalized_key):
		reserved = ", ".join(sorted(subroutine.addressing.RESERVED_PATH_WORDS))

		raise subroutine.errors.ValidationError(
			f"{normalized_key!r} cannot be used as a project key.",
			errors=[
				subroutine.errors.FieldError(
					field="key",
					code="invalid_field_value",
					message=f"{normalized_key!r} is reserved: a project keyed that way "
					f"would share an address with one of this API's own endpoints.",
					hint=f"Reserved keys are: {reserved}. Any other key is fine.",
				)
			],
		)


def normalize_key (key: str) -> str:
	"""Return the stored form of a project key: trimmed and lower-cased, nothing more.

	Deliberately *not* a filter, unlike :func:`subroutine.domain.workspaces.normalize_slug`
	which rewrites what it is given. An earlier version here dropped any character outside the
	allowed set, which turned ``'CAFÉ'`` into the perfectly valid key ``'CAF'`` — the user
	asked for one project and silently got another. Case-folding is the only change anyone
	would expect to be made on their behalf; everything else is refused by :func:`check_key`
	with an explanation, which is the honest half of the same job.

	**Lower rather than upper since `#508`**, and that is a stored-form change: every lookup
	compares this against ``project.key``, so a database of upper-case keys and a normaliser
	that lower-cases would match nothing at all. Migration ``c858f2942244`` rewrites
	them, and it is not optional.
	"""

	return key.strip().lower()


def _refuse_duplicate_key (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, key: str
) -> None:
	"""Raise if a live project in this workspace already uses this key."""

	model = subroutine.db.models.project.Project

	existing = session.scalars(
		sqlalchemy.select(model.id).where(
			model.workspace_id == workspace_id, model.key == key, model.deleted_at.is_(None)
		)
	).first()

	if existing is not None:
		raise subroutine.errors.Conflict(
			f"A project with the key {key!r} already exists here.",
			code="duplicate_key",
			errors=[
				subroutine.errors.FieldError(
					field="key",
					code="duplicate_key",
					message=f"The key {key!r} is already in use in this workspace.",
					hint="A key is how this project is addressed here, so no two can share one.",
				)
			],
		)
