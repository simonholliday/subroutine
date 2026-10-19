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
import subroutine.config
import subroutine.db.mixins
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.types
import subroutine.domain.accountability
import subroutine.domain.authentication
import subroutine.domain.authorization
import subroutine.domain.events
import subroutine.domain.hierarchy
import subroutine.domain.patch
import subroutine.domain.repair
import subroutine.domain.scoping
import subroutine.domain.settings
import subroutine.domain.stewardship
import subroutine.domain.text
import subroutine.domain.users
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

#: What separates one key from the next in a project's address (decision `#957`).
#:
#: **The same character a URL and a folder use**, because that is what the address *is*:
#: ``substation/dist`` is one project, reached through another, and the alternative
#: considered — a different separator, so that a path could never be mistaken for a key —
#: buys nothing once a key is forbidden to contain this one.
PATH_SEPARATOR = "/"

#: What a template writes into ``project.settings``, and nothing else (docs/design.md §6.12).
#:
#: **Every one of them writes nothing, as of `#1028`.** The single key these carried,
#: ``visible_status_keys``, was seeded by three templates and read **nowhere** in ``src/`` —
#: stored, published on the project view, and consumed by nothing. Ninth instance of the
#: declared-and-read-by-nothing family, and `domain/settings.py` exists because of it.
#:
#: **The names are kept and so is the column**, on Simon's decision of 2026-08-19. `#1029` is
#: what gives them a job again: the statuses a project's board shows, which is the only thing a
#: template was ever meant to decide. Until then ``template`` is accepted, validated and
#: without effect — said plainly here rather than left for somebody to discover by changing it
#: and watching nothing happen, which is how this family is always found.
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
	"personal": {},
	"software": {},
	"blank": {},
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
	max_depth: int | None = None,
	settings: subroutine.config.Settings | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.project.Project:
	"""Create a project, placed in the tree and stamped with its template's defaults."""

	title = subroutine.domain.text.fit(
		subroutine.domain.text.require(title, field="title"), field="title", limit=512
	)
	# Bounded as well as readable since `#1601` — `text.summary` is where both rules meet,
	# and it is what lets a listing render this with a worst case somebody chose.
	description = subroutine.domain.text.summary(description)

	# **Asked of the place it lands on, and only that** (`#4558`, decision `#4527`): the project it
	# goes into (`#4013`, M-10 of the cold review of 2026-09-30), where asking the workspace alone let
	# a credential writing only in ``web`` make ``ops/sub``, and asking the workspace as well refused
	# one making a project inside ``web`` (`#4095`). At the top level the place is the workspace,
	# which a credential narrowed to some projects never holds: a new project cannot already be named
	# in a scope, so it would be beyond it the moment it was made, and in front of everybody.
	if parent is not None:
		subroutine.domain.authorization.authorize_on(
			session, actor, subroutine.permissions.PROJECT_WRITE, parent
		)

	elif actor is not None:
		subroutine.domain.authorization.authorize(
			session,
			actor,
			subroutine.permissions.PROJECT_WRITE,
			workspace_id=workspace_id,
			instead=lambda: _inside_instead(session, actor, moving=None),
		)

	# **Whoever makes it owns it first, set here rather than by each door** (`SR#4573`, decision
	# `#4523`): four doors each wrote *the creator owns it* and this defaulted to nobody, so a caller
	# that forgot made a project nobody but an administrator could hide.
	creator = None if actor is None else actor.user.id
	handing_over = creator is not None and owner_id is not None and owner_id != creator

	# **Naming somebody else is creating it and then handing it over**, so they are asked about
	# before anything is made: a refusal after it would leave a project nobody asked for.
	if handing_over and owner_id is not None:
		member_who_sees(
			session,
			workspace_id,
			owner_id,
			within=parent,
			field="owner_id",
			what="a project inside it cannot be handed to them",
			actor=actor,
		)

	made = _made(
		session,
		workspace_id=workspace_id,
		key=key,
		title=title,
		description=description,
		parent=parent,
		template=template,
		visibility=visibility,
		owner_id=owner_id if creator is None else creator,
		max_depth=max_depth,
		settings=settings,
		actor=actor,
	)

	# **The creator keeps their share, and the new owner is shared in**, as any new owner is: making
	# a private project for somebody else shared them in and not its maker, who could no longer see
	# what they had just made (D2b of the cold review of 2026-10-05). Handing it over is the creator's
	# act as its steward at that moment.
	if handing_over:
		update(session, made, owner_id=owner_id, actor=actor)

	return made


def create_inbox (
	session: sqlalchemy.orm.Session,
	*,
	workspace_id: uuid.UUID,
	key: str,
	title: str,
	template: str,
	owner_id: uuid.UUID,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.project.Project:
	"""Make the Inbox a workspace is founded with, asking the credential nothing - `#4135`.

	**Founding a workspace is one act** (decision `#4136`), and
	:func:`subroutine.domain.workspaces.create` has asked ``instance:workspace_create`` of it. Its
	Inbox is a side effect of that, as a tag made while filing a task is (§7.3, *Side effects do not
	escalate*), so a credential narrowed to that one verb founds a whole workspace rather than being
	refused ``project:write`` half way through. The actor is still recorded, so the Inbox says who
	founded it, with what credential and by which route.

	**Founding calls this and nothing else does**, which ``tests/test_api_workspaces.py`` holds:
	every other project asks the credential, through :func:`create`.
	"""

	return _made(
		session,
		workspace_id=workspace_id,
		key=key,
		title=title,
		template=template,
		owner_id=owner_id,
		is_inbox=True,
		actor=actor,
	)


def _made (
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
	max_depth: int | None = None,
	settings: subroutine.config.Settings | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.project.Project:
	"""Make a project somebody has already been allowed to make, and record who made it."""

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
					# **`parent`, not `parent_id`** (`#1534`). `projects.Create` accepts
					# `parent`; `owner_id` below is left alone because that endpoint
					# accepts it by exactly that name.
					field="parent",
					code="invalid_field_value",
					message="That project belongs to a different workspace.",
				)
			],
		)

	_refuse_duplicate_key(
		session,
		workspace_id,
		normalized_key,
		parent_id=None if parent is None else parent.id,
	)

	# **A member of the workspace, and so somebody at all** (`#3934`). An id naming nobody reached
	# the foreign key as a 500, and one naming an account outside the workspace was taken: a
	# private project made so was invisible to whoever made it. `users.member` is the question
	# a task's assignee and a document's owner are already asked.
	if owner_id is not None:
		member_who_sees(
			session,
			workspace_id,
			owner_id,
			within=parent,
			field="owner_id",
			what="a project inside it cannot be handed to them",
			actor=actor,
			handed=False,
		)

	project = subroutine.db.models.project.Project(
		id=subroutine.db.types.new_uuid(),
		workspace_id=workspace_id,
		parent_id=None if parent is None else parent.id,
		visibility=visibility,
		key=normalized_key,
		title=title,
		description=description,
		status_id=status_for(session, workspace_id).id,
		owner_id=owner_id,
		is_inbox=is_inbox,
		template=template,
		settings=dict(TEMPLATES[template]),
		path="",
		depth=0,
		created_by=None if actor is None else actor.user.id,
	)
	subroutine.domain.hierarchy.place(project, parent, max_depth=subroutine.domain.hierarchy.depth_limit(max_depth, settings))

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
		# lock its owner out of it. The row grants sight and nothing else: its holder keeps
		# whatever role they hold in the workspace.
		_ensure_member(session, project, owner_id, actor=actor)

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
	max_depth: int | None = None,
	settings: subroutine.config.Settings | None = None,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> int:
	"""Move a project and everything under it, returning how many rows were rewritten.

	**A move can now collide, where it never could before** (`#957`). A key was unique
	across the workspace until this change, so a destination could not already be holding
	one; among siblings it can, and two projects sharing a parent and a key would be two
	rows at one address — the state the whole change exists to make impossible.
	"""

	subroutine.domain.authorization.authorize_on(
		session, actor, subroutine.permissions.PROJECT_WRITE, project
	)

	# **And of where it goes** (`#4013`), for :func:`create`'s reason: asked of the project moved
	# alone, a credential writing only in ``web`` moved it under ``ops``. **The top level is the
	# workspace's place** (`#4558`, Q15 of the cold review of 2026-10-05, decision `#4527`), as it is
	# for making one there: a credential narrowed to ``ops`` no longer moves ``ops`` itself out to
	# it, which `#4095` allowed by reading the top level a second way.
	if parent is not None:
		subroutine.domain.authorization.authorize_on(
			session, actor, subroutine.permissions.PROJECT_WRITE, parent
		)

	elif actor is not None:
		subroutine.domain.authorization.authorize(
			session,
			actor,
			subroutine.permissions.PROJECT_WRITE,
			workspace_id=project.workspace_id,
			instead=lambda: _inside_instead(session, actor, moving=project),
		)

	# **Nor the Inbox under another project** (`#4143`): a private project above it would hide it.
	refuse_unsettling_the_inbox(project, parent=parent)

	# **Across a privacy boundary only by its steward** (`SR#4570`, decision `#4509`): privacy inherits
	# down the tree, so a move under a private project takes it out of sight as making it private does,
	# and one out from under it shows it to everybody as making it public does. A member moved
	# somebody else's projects under a private one of their own (`#4383`), and one shared into a
	# private project moved its sub-project out to the top level (S5 of the cold review of
	# 2026-10-05). **A move within one private tree crosses nothing** and is not refused, where
	# `#4384`'s gate refused any move under a private project (NEW-O-1 of its verification).
	leaving = subroutine.domain.stewardship.private_above(
		session,
		None
		if project.parent_id is None
		else session.get(subroutine.db.models.project.Project, project.parent_id),
	)
	arriving = subroutine.domain.stewardship.private_above(session, parent)

	subroutine.domain.stewardship.refuse_crossing(
		session,
		actor,
		steward_of=project,
		leaving=leaving,
		arriving=arriving,
		what="it",
		field="parent",
		under=True,
	)

	# **Nor where it changes which of its rules bind everyone** (`SR#4576`, decision `#4134`): privacy
	# inherits down the tree, so a move under a private project hides a rule as making it private does
	# (`#4286`), and one out from under it, to a public place, shows it again (`#4389`).
	_refuse_changing_its_rules(session, actor, project, field="parent", parent=parent)

	# **Nor by a credential narrowed to some projects** (`SR#5102`, decision `#5097`), as it may not
	# make the project private or public: asked after the rules' refusal above.
	subroutine.domain.stewardship.refuse_narrowed_crossing(
		session,
		actor,
		workspace_id=project.workspace_id,
		leaving=leaving,
		arriving=arriving,
		permission=subroutine.permissions.PROJECT_WRITE,
		field="parent",
	)

	subroutine.domain.versions.require(project, expected_version, noun="project")

	destination = None if parent is None else parent.id

	if destination != project.parent_id:
		_refuse_duplicate_key(
			session,
			project.workspace_id,
			project.key,
			parent_id=destination,
			moving=project.key,
		)

	# **Nor where it leaves a private project with nobody who could see it** (`SR#5101`, M-4 of the
	# cold review of 2026-10-17, decision `#5097`): only arriving under a private project it was not
	# under hides anything. Asked after the reasons above, which are the more particular, and not of a
	# move beneath itself, which the move refuses by name.
	above = (
		()
		if parent is None
		else tuple(
			uuid.UUID(segment) for segment in subroutine.domain.hierarchy.path_segments(parent.path)
		)
	)

	if parent is not None and arriving - leaving and project.id not in above:
		_refuse_stranding(
			session,
			actor,
			project,
			afterwards=subroutine.domain.repair.Afterwards(moved=project.id, above=above),
			doing=f"Moving {project.key} under {parent.key}",
			field="parent",
		)

	previous_parent = project.parent_id
	previous_path = project.path

	moved = subroutine.domain.hierarchy.reparent(
		session, subroutine.db.models.project.Project, project, parent, max_depth=subroutine.domain.hierarchy.depth_limit(max_depth, settings)
	)

	if moved == 0:
		return 0

	project.parent_id = None if parent is None else parent.id

	# `version` is the ETag (docs/design.md §8.9), so anything a client can read has to move it.
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
	status_key: str = subroutine.domain.patch.UNSET,
	settings: dict[str, typing.Any] = subroutine.domain.patch.UNSET,
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

	subroutine.domain.authorization.authorize_on(
		session, actor, subroutine.permissions.PROJECT_WRITE, project
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
			_refuse_duplicate_key(
				session, project.workspace_id, cleaned_key, parent_id=project.parent_id
			)

	if title is not subroutine.domain.patch.UNSET:
		cleaned_title = subroutine.domain.text.fit(
			subroutine.domain.text.require(title, field="title"), field="title", limit=512
		)

	if description is not subroutine.domain.patch.UNSET:
		description = subroutine.domain.text.summary(description)

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

	# **The Inbox stays where everybody in the workspace can see it** (`#4143`).
	refuse_unsettling_the_inbox(project, visibility=visibility)

	# **Who can see it is its steward's to change, in both directions** (`SR#4570`, decision `#4509`):
	# making it private takes it out of sight of everybody not shared into it, administrators
	# included (`#4383`), and making it public puts it in front of the whole workspace, which any
	# member shared in could do (S5 of the cold review of 2026-10-05).
	if visibility is not subroutine.domain.patch.UNSET and visibility != project.visibility:
		subroutine.domain.stewardship.refuse_unless_steward(
			session, actor, project, act=f"make it {visibility}", field="visibility"
		)

	# **Nor a project holding a rule that binds everyone, unless by somebody who may mark one**
	# (`#4286`, `#4389`, `SR#4576`, decision `#4134`), in either direction: making it private takes
	# the rule out of sight of everybody not shared into it, and making it public brings every rule it
	# holds into everybody's conventions.
	if visibility is not subroutine.domain.patch.UNSET and visibility != project.visibility:
		_refuse_changing_its_rules(
			session, actor, project, field="visibility", private=visibility == "private"
		)

	# **Who sees a project is decided at the workspace's place** (`#4558`, C I-3 of the cold review
	# of 2026-10-05, decision `#4509`), in both directions: making one public puts it in front of the
	# whole workspace, and making one private takes it from in front of it. A credential narrowed to
	# some projects published the one it was narrowed to, while refused sharing a view (`#3151`).
	# The role already granted the verb in the project above, so only the place is new here, and it
	# is asked after the rules above, whose reasons are the more particular.
	if (
		actor is not None
		and visibility is not subroutine.domain.patch.UNSET
		and visibility != project.visibility
	):
		subroutine.domain.authorization.authorize(
			session, actor, subroutine.permissions.PROJECT_WRITE, workspace_id=project.workspace_id
		)

	# **Membership, not existence** (`#3934`): an account outside the workspace was taken here too,
	# handing a project to somebody who could not see it.
	new_owner: subroutine.db.models.identity.User | None = None

	# **Only a change of owner is asked** (`SR#5104`), as a task's assignee and a document's maintainer
	# are: the browser sends every field it shows, unchanged ones included, so asking a re-sent owner
	# who has since left refused every save of the project.
	if (
		owner_id is not subroutine.domain.patch.UNSET
		and owner_id is not None
		and str(owner_id) != str(project.owner_id)
	):
		new_owner = member_who_sees(
			session,
			project.workspace_id,
			owner_id,
			within=None
			if project.parent_id is None
			else session.get(subroutine.db.models.project.Project, project.parent_id),
			field="owner_id",
			what="this project cannot be handed to them",
			actor=actor,
		)

	# **And only by its steward** (`#4382`, R2-M5 of the cold review of 2026-10-04; `SR#4570`, decision
	# `#4509`): `project:write` was enough, so a member shared into a private project made themselves
	# its owner and then removed the owner, the one share :func:`unshare` protects.
	if owner_id is not subroutine.domain.patch.UNSET and str(owner_id) != str(project.owner_id):
		subroutine.domain.stewardship.refuse_unless_steward(
			session, actor, project, act="change who owns it", field="owner_id"
		)

	# Resolved here rather than in the assignment pass, because a key that names no status
	# must refuse before anything has been assigned — the rule this function's docstring
	# states, and the reason the two passes are separate.
	cleaned_status: typing.Any = subroutine.domain.patch.UNSET

	if status_key is not subroutine.domain.patch.UNSET:
		cleaned_status = status_for(session, project.workspace_id, status_key).id

	# **Merged per key rather than replaced**, and validated here in the same pass as everything
	# else — a settings map naming a key nothing declares must refuse before any assignment, the
	# rule this function's docstring states. `domain.settings.applied` is where the merge rule
	# and its argument live.
	cleaned_settings: typing.Any = subroutine.domain.patch.UNSET

	if settings is not subroutine.domain.patch.UNSET:
		# **What the registry says gates each key, asked before any value is read** (`#2120`).
		# `PROJECT_WRITE` above is what changing a project's own fields costs and is what both
		# of today's settings need here; the registry is what would say otherwise, and asking it
		# is what stops a setting declaring a verb nothing consults.
		subroutine.domain.settings.authorized(
			session,
			actor,
			settings,
			scope=subroutine.domain.settings.PROJECT,
			workspace_id=project.workspace_id,
			project=project,
		)

		cleaned_settings = subroutine.domain.settings.applied(
			project.settings, settings, scope=subroutine.domain.settings.PROJECT
		)

		# **The half a registry entry cannot check on its own.** A `Kind` reads a value's shape
		# and has no session; a setting naming *this workspace's own vocabulary* — which status
		# keys are real — needs one. Run against the merged map rather than the incoming one, so
		# that a key already stored stays checked when something beside it changes.
		subroutine.domain.settings.verified(session, project.workspace_id, cleaned_settings)

	# **Nor where it leaves a private project with nobody who could see it** (`SR#5101`, M-4 of the
	# cold review of 2026-10-17, decision `#5097`): jo made ``outer`` private, and pat's private
	# project inside it, which pat saw without being shared into ``outer``, was seen by nobody. Asked
	# last, since the reasons above are the more particular, and counting a new owner, whom this same
	# change shares in.
	if visibility == "private" and project.visibility != "private":
		_refuse_stranding(
			session,
			actor,
			project,
			afterwards=subroutine.domain.repair.Afterwards(
				private=frozenset({project.id}),
				granted=frozenset() if new_owner is None else frozenset({(project.id, new_owner.id)}),
			),
			doing=f"Making {project.key} private",
			field="visibility",
		)

	# Assignment pass.
	changes: dict[str, typing.Any] = {}

	for field, value in (
		("key", cleaned_key),
		("title", cleaned_title),
		("description", description),
		("visibility", visibility),
		("owner_id", owner_id),
		("status_id", cleaned_status),
		# **The whole dict, replaced rather than mutated** (`#42`). SQLAlchemy does not watch
		# inside a JSON column, so assigning into one is silently never written — and the
		# equality check just below is what stops an identical map moving `updated_at`.
		("settings", cleaned_settings),
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
		_ensure_member(session, project, project.owner_id, actor=actor)

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


def _inside_instead (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	*,
	moving: subroutine.db.models.project.Project | None,
) -> str:
	"""Say where a credential refused the top level may put the project instead - decision `#4095`.

	**The hint of the one refusal every narrowed act meets** (`#4558`), naming where it *can* put
	the project, which that refusal's own hint, *use a token that is not narrowed*, does not.
	"""

	named = ", ".join(
		keys_for(session, actor, list(subroutine.domain.authorization.write_places(actor) or ()))
	)
	inside = f"inside a project it may change ({named})" if named else "inside a project"

	# **A move is told how to move** (`#4323`, of the cold review of 2026-10-03): it was told to use
	# ``--parent``, which ``project move`` does not take.
	if moving is None:
		return (
			f"Make it {inside}: --parent in the terminal, parent in the agent tools, or use a token "
			"that is not narrowed to projects."
		)

	return (
		f"Move it {inside}: 'subroutine project move {path_of(session, moving)} --under <key>' "
		"in the terminal, parent in the move request, or use a token that is not narrowed to "
		"projects."
	)


def delete (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	*,
	now: datetime.datetime | None = None,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.project.Project:
	"""Move a project to the trash, where it stays recoverable (docs/design.md §6.9).

	Soft, and idempotent: when something was thrown away is a fact worth not overwriting.
	Its tasks are not touched, and do not need to be — every listing joins the project and
	excludes deleted ones, so they leave the visible world with it and come back with it.
	"""

	subroutine.domain.authorization.authorize_on(
		session, actor, subroutine.permissions.PROJECT_DELETE, project
	)
	subroutine.domain.versions.require(project, expected_version, noun="This project")

	if project.deleted_at is not None:
		return project

	refuse_unsettling_the_inbox(project, deleting=True)

	# **Nor a project holding a rule that binds everyone** (`#4286`): the rule goes to the trash with
	# it, which retires it for everybody.
	_refuse_changing_its_rules(session, actor, project, field="project", deleted=True)

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
	"""Return each identifier as the project address it names, or unchanged where it names none.

	Built for ``token list`` (`#203`), which printed a credential's ``project_scope`` as raw
	UUIDs on the line *below* one that resolves the workspace pin to its slug — same output,
	same argument, applied once. A UUID in a listing is something to go and look up, which is
	the opposite of what a listing is for.

	**The whole address rather than the bare key, since `#957`.** A key stopped being unique
	in its workspace, so ``dist`` printed here may name two projects and cannot be typed back
	into ``token create --project`` — which is `#151`'s rule, that what a caller is shown is
	what it can send. The field is still called ``project_scope_keys`` on the wire: a client
	reading it is reading *what a person types*, and that is what changed underneath rather
	than which question the field answers.

	**Here rather than in the command**, because it narrows: a key discloses more than an id,
	so resolving one for a reader who cannot see that project would turn a listing of their own
	credentials into a way of learning a private project's name. Narrowing belongs beside the
	other narrowing, and ``tests/test_scoping.py`` is what said so.

	Anything that does not resolve — malformed, deleted, or simply not visible — is passed
	through as it was stored. A listing whose job is "what can this credential reach" must never
	report a *narrower* reach than the credential has.

	**Only among the workspaces the caller reaches** (`#4556`, S15 of the cold review of
	2026-10-05). This resolved across every workspace on the installation, so somebody taken out of
	one went on seeing a project there renamed, under its new address, in their own token listing.
	A project in a workspace they have left is passed through as its id, as one they cannot see is.
	The listing asks reach itself since `#4675`, so nothing here names the workspaces.
	"""

	named: list[str | subroutine.db.models.project.Project] = []

	for identifier in identifiers:
		try:
			wanted = uuid.UUID(str(identifier))

		except ValueError:
			named.append(identifier)

			continue

		found = session.scalars(
			# **Named, not read, and the reason is whose rows these are** (`#930`,
			# `scoping.nameable_projects`). Every id here came out of the caller's *own* token, so
			# naming them discloses nothing they do not already hold — where a listing of
			# `/v1/projects` would. Asking `project:read` here would mean a credential narrowed to
			# `task:read` could not run `whoami` or read `/v1/me`, the two things built to tell an
			# agent the truth about itself, and `#203`'s whole point was that a reader should see
			# `inbox` rather than a UUID to go and look up. The *visibility* narrowing still
			# applies, which is what this function's docstring is about: a private project
			# somebody cannot see is still passed through as its stored id.
			subroutine.domain.scoping.nameable_projects(
				session, principal, include_deleted=True, include_archived=True
			).where(subroutine.db.models.project.Project.id == wanted)
		).first()

		named.append(identifier if found is None else found)

	rows = [item for item in named if not isinstance(item, str)]
	addresses = paths_for(session, [row.id for row in rows])

	return [item if isinstance(item, str) else addresses[item.id] for item in named]


def restore (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	*,
	expected_version: int | None = None,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.project.Project:
	"""Take a project back out of the trash, and its contents with it (docs/design.md §6.9).

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

	subroutine.domain.authorization.authorize_on(
		session, actor, subroutine.permissions.PROJECT_DELETE, project
	)
	subroutine.domain.versions.require(project, expected_version, noun="This project")

	if project.deleted_at is None:
		return project

	# **Nor one holding a rule that binds everyone** (`#4389`): its rules come back with it, into
	# everybody's conventions, as :func:`delete` takes them away.
	_refuse_changing_its_rules(session, actor, project, field="project", deleted=False)

	# An ancestor still in the trash would put this row back into a subtree nobody can see, so
	# the caller would be told it worked and nothing would appear. Refused by name instead.
	buried = _deleted_ancestor(session, project)

	if buried is not None:
		raise subroutine.errors.ValidationError(
			f"'{project.key}' is inside '{buried.key}', which is also in the trash.",
			hint=f"Restore '{buried.key}' first, and this comes back with it.",
		)

	# **The key it had may not be free any more**, and the index that guarantees so is partial
	# — it ignores deleted rows, which is what lets the key be reused in the first place. So a
	# project deleted, replaced and then restored met the constraint at flush time and left as
	# an unhandled `IntegrityError`: a 500 over HTTP and a bare traceback at the terminal, for
	# an ordinary sequence of three commands.
	#
	# **Among its siblings since `#957`**, through the same question the other three ask. Asked
	# workspace-wide it would refuse this restore because a *cousin* took the key — a name the
	# constraint no longer minds sharing, and a refusal with no way out.
	taken = sibling_using(
		session,
		project.workspace_id,
		project.key,
		parent_id=project.parent_id,
		besides=project.id,
	)

	if taken is not None:
		raise subroutine.errors.Conflict(
			f"'{project.key}' is another project's key now.",
			code="duplicate_key",
			errors=[
				subroutine.errors.FieldError(
					field="key",
					code="duplicate_key",
					message=f"A live project already answers to {project.key!r} in this "
					f"workspace.",
				)
			],
			hint=f"Rename that one with 'subroutine project rename {project.key} <new-key>', "
			f"then restore this.",
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
	*,
	actor: subroutine.domain.authentication.Principal | None,
) -> None:
	"""Make sure a user holds a membership row for a project, without duplicating one.

	Called when ownership changes, for the same reason :func:`create` writes one: §7.3a
	grants sight of a private project to members and to nobody else, so handing somebody a
	private project without this would hand them one they cannot see.

	**And who granted it** (`SR#4572`, C section 5 of the cold review of 2026-10-05): nothing wrote
	``created_by``, so reporting who let somebody in would have reported nobody on every row.
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
			created_by=None if actor is None else actor.user.id,
		)
	)
	session.flush()


def hidden_by (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	user_id: uuid.UUID,
	*,
	without: subroutine.db.models.project.ProjectMember | None = None,
	afterwards: subroutine.domain.repair.Afterwards | None = None,
) -> subroutine.db.models.project.Project | None:
	"""Return the project whose privacy keeps this one out of one person's sight, or ``None``.

	§7.3a hides a project when it is private **or any ancestor of it is**, unless the person
	holds a ``project_member`` row on that private one. So what has to be shared is not always
	the project somebody names: a public project under a private parent is hidden, and a row
	on the child grants nothing at all.

	**A row of their own, or of an agent that answers to them** (`SR#4944`, decision `#4943`):
	sight follows the chain upward, and :func:`subroutine.domain.accountability.sees_as` says
	whose rows count, here as in the predicate. ``without`` asks it of a future in which that
	one row has gone, which is what unsharing somebody asks before it takes it, and ``afterwards``
	of one in which a project is made private, moved or given a new owner (`SR#5101`).

	Returns the **outermost** private ancestor this user has no row for, which is the one
	blocking them today. Sharing that is what changes their answer — and if something further
	in is private too, they are blocked by that next and this returns it instead. So a private
	subtree opens one deliberate step at a time rather than all at once, which is the same
	answer §7.3a gives and not a rule of this function's own.

	Reads the materialised ``path`` rather than walking ``parent_id``, which is the choice
	:func:`subroutine.domain.authorization.visible_projects` makes for the same reason — an
	ancestor test is a prefix match there and a list of ids here.
	"""

	model = subroutine.db.models.project.Project
	membership = subroutine.db.models.project.ProjectMember

	identifiers = [
		uuid.UUID(segment)
		for segment in subroutine.domain.hierarchy.path_segments(project.path)
	]

	if afterwards is not None:
		identifiers = afterwards.path(identifiers)

	ancestors = {
		row.id: row
		for row in session.scalars(sqlalchemy.select(model).where(model.id.in_(identifiers)))
	}

	sights = subroutine.domain.accountability.sees_as(user_id)
	accounts: set[uuid.UUID] | None = None

	# **Whose shares this person sees by once the act is taken** (`SR#5101`): an agent handed over
	# takes its own with it, and those of every agent beneath it.
	if afterwards is not None and (afterwards.handed is not None or afterwards.granted):
		accounts = afterwards.seeing(user_id, set(session.scalars(sights)))

	held = sqlalchemy.select(membership.project_id).where(
		membership.user_id.in_(sights if accounts is None else accounts),
		membership.project_id.in_(identifiers),
	)

	if without is not None:
		held = held.where(membership.id != without.id)

	shared = set(session.scalars(held))
	private: frozenset[uuid.UUID] = frozenset() if afterwards is None else afterwards.private

	if afterwards is not None and accounts is not None:
		shared |= {identifier for identifier, holder in afterwards.granted if holder in accounts}

	for identifier in identifiers:
		ancestor = ancestors.get(identifier)

		if ancestor is None:
			continue

		if (ancestor.visibility == "private" or ancestor.id in private) and ancestor.id not in shared:
			return ancestor

	return None


def member_who_sees (
	session: sqlalchemy.orm.Session,
	workspace_id: uuid.UUID,
	user_id: uuid.UUID,
	*,
	within: subroutine.db.models.project.Project | None,
	field: str,
	what: str,
	actor: subroutine.domain.authentication.Principal | None,
	handed: bool = True,
) -> subroutine.db.models.identity.User:
	"""Return the member work is handed to, refusing one who cannot see where it is - `SR#4571`.

	``handed`` is false where nobody hands it to them - a project made for its maker - so whether
	they can act there is not asked (`SR#5104`).

	**Work is handed only to a member of the workspace who can see the project it is in** (decision
	`#4509`, S14 of the cold review of 2026-10-05): a task's assignee, a document's maintainer and a
	project's owner. A task in a private project was assigned, and a document there given a
	maintainer, who could not see it, and the task domain took an assignee from no workspace at all
	(E #9). **Both questions, every time**: sight alone takes an outsider on a public project, and
	membership alone a member a private project hides.

	``within`` is the project whose sight decides - for a project's owner, the one above it, since
	handing a project over shares its new owner into it. ``what`` finishes the refusal. The project
	that hides it is the work's own or one above it, which a caller acting on the work can see, so
	naming it names nothing hidden from them.
	"""

	named = subroutine.domain.users.member(session, workspace_id, str(user_id), field=field)

	# **And who can act there** (`SR#5104`, M-10 of the cold review of 2026-10-17, decision `#4509`):
	# a member who had left, or an agent that had stopped, was handed a task, a document or a project
	# nobody could then act on. Said in its own sentence, since they are a member: the lookup's own
	# refusal would say there is nobody of that name. **Only where it is handed to them**: a project
	# made for its own maker is not, nor the Inbox for whoever founds the workspace.
	if handed and not subroutine.domain.accountability.can_act(
		session, named, workspace_id=workspace_id
	):
		raise subroutine.errors.ValidationError(
			f"{named.username} "
			f"{'has stopped working here' if named.is_service_account else 'has left'}, so {what}.",
			errors=[
				subroutine.errors.FieldError(
					field=field,
					code="invalid_field_value",
					message=f"{named.username} cannot act here.",
					hint="Choose somebody who can act.",
				)
			],
		)

	blocking = None if within is None else hidden_by(session, within, named.id)

	if blocking is None:
		return named

	# **A share is offered only to a caller it would not refuse** (`SR#5106`, M-23 of the cold review
	# of 2026-10-17, decision `#4521`): a steward on a credential that may not share was offered the
	# command and then refused it. Somebody who is not the steward is pointed at the owner.
	if may_share(session, actor, blocking):
		hint = (
			f"'subroutine project share {blocking.key} {named.username}' first, or choose somebody who "
			"can see it."
		)

	elif subroutine.domain.stewardship.stewards(session, actor, blocking):
		hint = "Choose somebody who can see it."

	else:
		hint = (
			f"Choose somebody who can see it, or ask the owner of '{blocking.key}' to share it with "
			"them."
		)

	raise subroutine.errors.ValidationError(
		f"{named.username} cannot see {blocking.key}, so {what}.",
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="invalid_field_value",
				message=f"{named.username} is not shared into {blocking.key}.",
				hint=hint,
			)
		],
	)


def may_share (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	project: subroutine.db.models.project.Project,
) -> bool:
	"""Report whether this caller may share ``project`` with somebody, as :func:`share` asks it.

	**Asked rather than demanded, for a hint** (`SR#5106`, decision `#4521`: the hint offers only
	what this caller can do): its steward, holding ``project:write`` there on this credential.
	"""

	if actor is None:
		return True

	return subroutine.domain.stewardship.stewards(session, actor, project) and (
		subroutine.domain.authorization.refusal(
			session,
			actor,
			subroutine.permissions.PROJECT_WRITE,
			workspace_id=project.workspace_id,
			project=project,
		)
		is None
	)


def may_publish (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	project: subroutine.db.models.project.Project,
) -> bool:
	"""Report whether this caller may make ``project`` public, as :func:`update` asks it - `SR#5106`.

	**Its steward, holding ``project:write`` at the workspace's place**, where who sees a project
	is decided: a credential narrowed to some projects may share one and not publish it.
	"""

	if actor is None:
		return True

	return subroutine.domain.stewardship.stewards(session, actor, project) and (
		subroutine.domain.authorization.refusal(
			session, actor, subroutine.permissions.PROJECT_WRITE, workspace_id=project.workspace_id
		)
		is None
	)


def _refuse_changing_its_rules (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	project: subroutine.db.models.project.Project,
	*,
	field: str,
	**change: typing.Any,
) -> None:
	"""Refuse a change to a project that changes which of its rules bind everyone, as `domain.documents` says.

	**Reached late, because ``domain.documents`` imports ``domain.tasks``**, whose ordering reads
	``domain.filtering`` at module scope while that is still being imported through this module: a
	module-level import here is a circular import that fails at load. The house style's
	nested-import exception covers exactly this, and the alias keeps ``subroutine`` from being
	rebound as a local.
	"""

	from subroutine.domain import documents as rules

	rules.refuse_changing_what_its_rules_bind(session, actor, project, field=field, **change)


def _membership_of (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	user_id: uuid.UUID,
) -> subroutine.db.models.project.ProjectMember | None:
	"""Return this user's membership row for this project, or ``None``."""

	model = subroutine.db.models.project.ProjectMember

	return session.scalars(
		sqlalchemy.select(model).where(
			model.project_id == project.id, model.user_id == user_id
		)
	).first()


def _refuse_somebody_outside_the_workspace (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	user: subroutine.db.models.identity.User,
) -> None:
	"""Refuse sharing with somebody who is not in the workspace, by name.

	``ProjectMember`` has no workspace check of its own and ``workspaces.readable`` is an
	inner join on membership — *membership is what grants reach*. So the row would be written,
	the person would go on seeing nothing, and nothing anywhere would say why. Naming the
	remedy is the whole of this: the answer is one command, and it is not this one.
	"""

	model = subroutine.db.models.identity.WorkspaceMember

	inside = session.scalars(
		sqlalchemy.select(model).where(
			model.workspace_id == project.workspace_id, model.user_id == user.id
		)
	).first()

	if inside is not None:
		return

	raise subroutine.errors.ValidationError(
		f"{user.username} is not in this workspace, so sharing a project with them would "
		f"change nothing.",
		hint=f"'subroutine user add {user.username} --role member' first.",
		errors=[
			subroutine.errors.FieldError(
				field="username",
				code="invalid_field_value",
				message=f"{user.username} is not a member of this workspace.",
			)
		],
	)


class Unreachable(typing.NamedTuple):
	"""A private project nobody who can act here is able to see, and where it is - `#1453`."""

	project: subroutine.db.models.project.Project
	workspace: subroutine.db.models.identity.Workspace
	address: str
	members: int

	#: Whether the caller belongs to the workspace it is in - `#2626`. Letting somebody in happens
	#: inside a workspace, and an administrator joins one rather than reaching into it (`#1860`), so
	#: where this is false, joining comes before the share.
	member_of_workspace: bool


def unreachable (
	session: sqlalchemy.orm.Session,
	*,
	actor: subroutine.domain.authentication.Principal | None = None,
	leaving: subroutine.db.models.identity.User | None = None,
	transferring: tuple[subroutine.db.models.identity.User, subroutine.db.models.identity.User]
	| None = None,
) -> list[Unreachable]:
	"""Return the private projects no person who can act here is able to see - item `#1453`.

	**Decided by Simon on 2026-09-14, and it is `#1418`'s answer one level down**: membership is
	reach, and an administrator may *discover* what nobody can reach and *join* it, and joining
	is recorded. A private project whose last member leaves - or whose only member is an agent
	whose person leaves - is otherwise invisible to every surface, and the only way back in was
	to act as the person who left.

	**Only what nobody can reach, and nothing inside it.** The key, the title, the workspace and
	how many memberships it holds. A private project somebody can still see is not listed at all,
	so its existence stays that somebody's to disclose.

	**``leaving`` asks what one person's departure would strand**: projects no person could see once
	they and every agent answering to them have stopped, where they or one of those agents can see
	it now. That is the question `user deactivate` puts before it acts, which is the other half of
	the decision. **Where they could repair it now, too** (`SR#4944`): somebody who sees a project
	only through an agent that has been stopped holds no row, and neither does anything of theirs
	that can act.

	**``transferring`` asks what handing an agent to somebody else would strand** (`SR#5101`,
	decision `#5097`): projects somebody could see now, through a share the agent or one beneath it
	holds, that nobody could once it answers to its new person - the question `user transfer` puts
	before it acts. Refused where the transfer would be, in the transfer's own words.

	**People only** (`SR#4569`, decision `#4526` and Simon's answer of 2026-10-09): who could repair
	a private project is a person who can act and see it, so one only an agent can see is listed -
	an agent's own included - and is named when the agent's person leaves.

	Needs ``instance:admin``, and a credential pinned to one workspace or narrowed to some projects
	is refused: this answers for every project on the installation (`#2619`). Both are refused by
	``authorize_instance`` itself, since decisions `#3802` and `#4006`.
	"""

	if actor is not None:
		subroutine.domain.authorization.authorize_instance(
			actor, subroutine.permissions.INSTANCE_ADMIN, reading=True
		)

	afterwards: subroutine.domain.repair.Afterwards | None = None
	moving: frozenset[uuid.UUID] = frozenset()

	if transferring is not None:
		agent, to = transferring
		subroutine.domain.users.refuse_transferring(session, agent, to=to)
		moving = frozenset(
			{
				agent.id,
				*(one.id for one in subroutine.domain.accountability.agents_answering_to(session, agent)),
			}
		)
		afterwards = subroutine.domain.repair.Afterwards(handed=(agent.id, to.id), moving=moving)

	project = subroutine.db.models.project.Project
	workspace = subroutine.db.models.identity.Workspace
	membership = subroutine.db.models.project.ProjectMember

	# **Instance-wide by construction, and bounded by how many private projects exist.** Each is
	# asked about its own members, which is a handful of reads per project; there were none on
	# this project's own instance when the question was filed.
	candidates = session.execute(
		sqlalchemy.select(project, workspace)
		.join(workspace, workspace.id == project.workspace_id)
		.where(
			project.visibility == "private",
			project.deleted_at.is_(None),
			workspace.deleted_at.is_(None),
		)
		.order_by(workspace.slug, project.key, project.id)
	).all()

	gone: tuple[uuid.UUID, ...] = ()
	theirs: set[uuid.UUID] = set()

	if leaving is not None:
		gone = (leaving.id,)
		theirs = subroutine.domain.repair.departing(session, leaving)

	# **Once per private project, and a membership lookup where somebody is leaving** (`#2638`,
	# `SR#4569`): a project is theirs to strand where they, or an agent answering to them, holds a
	# row on it. Each walk is a members query and a climb of the project's chain, bounded, as above,
	# by how many private projects exist - which is the number to watch if this grows slow.
	def asked (row: subroutine.db.models.project.Project) -> bool:
		"""Report whether this project is one the question is about: every one, or what an act leaves."""

		if leaving is not None:
			return subroutine.domain.repair.takes_part(
				session, row, theirs
			) or leaving.id in subroutine.domain.repair.repairers(session, row)

		# **Only what the transfer changes** (`SR#5101`): a project somebody could see now, through a
		# share the agent or one beneath it holds. One nobody could see already is listed as it is.
		if transferring is not None:
			return subroutine.domain.repair.takes_part(session, row, moving) and bool(
				subroutine.domain.repair.repairers(session, row)
			)

		return True

	stranded = [
		(row, place)
		for row, place in candidates
		if not subroutine.domain.repair.repairers(session, row, leaving=gone, afterwards=afterwards)
		and asked(row)
	]

	if not stranded:
		return []

	counts = dict(
		session.execute(
			sqlalchemy.select(membership.project_id, sqlalchemy.func.count())
			.where(membership.project_id.in_([row.id for row, _place in stranded]))
			.group_by(membership.project_id)
		).all()
	)
	addresses = paths_for(session, [row.id for row, _place in stranded])

	# **Which of these workspaces the caller is inside** (`#2626`), from the same membership rows
	# `reachable_by_anybody` joins: this answers for the installation, and a share does not.
	belongs = subroutine.db.models.identity.WorkspaceMember
	mine = (
		set()
		if actor is None
		else set(
			session.scalars(
				sqlalchemy.select(belongs.workspace_id).where(belongs.user_id == actor.user.id)
			)
		)
	)

	return [
		Unreachable(
			project=row,
			workspace=place,
			address=addresses.get(row.id, row.key),
			members=counts.get(row.id, 0),
			member_of_workspace=place.id in mine,
		)
		for row, place in stranded
	]


def asked_about (
	session: sqlalchemy.orm.Session,
	*,
	leaving: str | None = None,
	transferring: str | None = None,
	to: str | None = None,
) -> tuple[
	subroutine.db.models.identity.User | None,
	tuple[subroutine.db.models.identity.User, subroutine.db.models.identity.User] | None,
]:
	"""Resolve by name what :func:`unreachable` is asked about, refusing a question half asked.

	**Both doors ask it here** (`SR#5101`, decision `#5097`), the API and the local client: a transfer
	named without who it goes to, or beside somebody leaving, is refused in one sentence. Naming an
	account to ask about it is not a read of it (decision `#4943`).
	"""

	if (transferring is None) != (to is None):
		given, missing = ("transferring", "to") if to is None else ("to", "transferring")

		raise subroutine.errors.ValidationError(
			f"'{given}' needs '{missing}' beside it: the agent, and who it would be handed to.",
			errors=[
				subroutine.errors.FieldError(
					field=missing, code="missing_field", message=f"'{missing}' goes with '{given}'."
				)
			],
		)

	if leaving is not None and transferring is not None:
		raise subroutine.errors.ValidationError(
			"Ask what somebody leaving would strand, or what a transfer would, not both at once.",
			errors=[
				subroutine.errors.FieldError(
					field="transferring",
					code="invalid_field_value",
					message="'transferring' and 'leaving' are two questions.",
				)
			],
		)

	departing = None if leaving is None else subroutine.domain.users.by_username(session, leaving)

	if transferring is None or to is None:
		return departing, None

	return departing, (
		subroutine.domain.users.by_username(session, transferring),
		subroutine.domain.users.by_username(session, to),
	)


def share (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	user: subroutine.db.models.identity.User,
	*,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> subroutine.db.models.project.ProjectMember:
	"""Let one more person see a private project — item `#1444`.

	**Its steward's, with ``project:write``, and so is removing somebody else** (`SR#4570`, decision
	`#4509`). It was ``project:write`` alone, on the argument that anybody holding it could already
	publish the whole project by setting its visibility; publishing is the steward's now, so a member
	shared in no longer shares it onward, nor evicts anybody else, an administrator included, which
	reverses the cost `#1444` accepted (S5 of the cold review of 2026-10-05).

	**Presence, never a role.** The row holds no role, because §7.3a reads nothing but its
	existence. `#1452` is where a project-scoped role belongs and it is a
	different act: sight inherits down the tree and authority deliberately does not, so one
	command granting both would carry two scoping rules and mean opposite things on a public
	project and a private one.

	**The event names the project as its subject**, which is what makes it visible to exactly
	those who can see the project and to nobody else. ``scoping.visible_events`` already has
	that clause, so this needs no scoping change — where recording it the way a *workspace*
	membership is recorded would have published who can see a private project to the whole
	workspace.
	"""

	# **An administrator may let somebody into a project nobody can reach** - `#1453`, decided
	# with `#1418`'s rule. Nobody holds `project:write` *on* such a project, because nobody who can
	# act can see it; that is the state being repaired. Only while it is unreachable, so a private
	# project somebody can still see stays theirs to share. The event below records who did it.
	if not rescuable(session, actor, project):
		subroutine.domain.authorization.authorize_on(
			session, actor, subroutine.permissions.PROJECT_WRITE, project
		)
		subroutine.domain.stewardship.refuse_unless_steward(
			session, actor, project, act="share it with somebody", field="username"
		)

	_refuse_somebody_outside_the_workspace(session, project, user)

	existing = _membership_of(session, project, user.id)
	blocking = hidden_by(session, project, user.id)

	# **Somebody already shared in sees it only while nothing above hides it** (`#2631`). This was
	# asked before what hides it, so a member a private parent hides was told they could already
	# see the project - sending whoever is letting them in to the wrong remedy. The sentence about a
	# hiding parent, below, is theirs too.
	if existing is not None and blocking is None:
		raise subroutine.errors.ValidationError(
			f"{user.username} can already see {project.key}.",
			errors=[
				subroutine.errors.FieldError(
					field="username",
					code="duplicate_key",
					message=f"{user.username} is already shared into {project.key}.",
				)
			],
		)

	# **Somebody who sees it only through an agent is given a row of their own** (`SR#4944`,
	# decision `#4943`): nothing hides it from them and no row is theirs, which before sight
	# followed the chain could only be a project that was not private. Their own row is what they
	# keep when the agent is handed to somebody else, or unshared.
	if blocking is None and project.visibility != "private":
		raise subroutine.errors.ValidationError(
			f"{project.key} is visible to everybody in this workspace, so there is nobody to "
			f"share it with.",
			hint=f"'subroutine project update {project.key} --private' first, if it should not "
			f"be.",
			errors=[
				subroutine.errors.FieldError(
					field="id_or_key",
					code="invalid_field_value",
					message=f"{project.key} is not private.",
				)
			],
		)

	if blocking is not None and blocking.id != project.id:
		raise subroutine.errors.ValidationError(
			f"{project.key} is hidden by {blocking.key}, which is the project to share.",
			hint=f"Sharing {blocking.key} shows everything inside it that is not private in "
			f"its own right.",
			errors=[
				subroutine.errors.FieldError(
					field="id_or_key",
					code="invalid_field_value",
					message=f"A membership on {project.key} grants nothing while {blocking.key} "
					f"is private.",
				)
			],
		)

	membership = subroutine.db.models.project.ProjectMember(
		workspace_id=project.workspace_id,
		project_id=project.id,
		user_id=user.id,
		created_by=None if actor is None else actor.user.id,
	)
	session.add(membership)
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=project.workspace_id,
		entity_type="project_member",
		entity_id=membership.id,
		action=subroutine.domain.events.EventAction.CREATED,
		subject_type="project",
		subject_id=project.id,
		changes={"user_id": {"from": None, "to": user.id}},
		actor=actor,
	)

	return membership


def refuse_unsettling_the_inbox (
	project: subroutine.db.models.project.Project,
	*,
	visibility: typing.Any = None,
	parent: subroutine.db.models.project.Project | None = None,
	deleting: bool = False,
) -> None:
	"""Refuse what would take the Inbox from where everybody files - `#4549`, decision `#4532`.

	**The Inbox is public, at the top level and out of the trash** (`#4143`, decided on `#3946`).
	Everything filed in a workspace without a project goes to it, so everybody in the workspace has
	to be able to reach it: a private one refused every other member's capture as though they had
	named a project they could not see, one under a private project is hidden the same way, since
	privacy inherits down the tree, and a workspace without one has nowhere to file at all.

	**Said once, each door naming the part it would break**: ``visibility`` is what an update would
	make it, ``parent`` where a move would put it, and ``deleting`` a move to the trash. Three doors
	said it in their own places. ``ck_project_inbox_settled`` holds the same rule beneath, for a
	door that forgets to ask - a refusal by name here, and an integrity error there.
	"""

	if not project.is_inbox:
		return

	if deleting:
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

	if visibility == "private":
		raise subroutine.errors.ValidationError(
			"The Inbox cannot be made private.",
			errors=[
				subroutine.errors.FieldError(
					field="visibility",
					code="invalid_field_value",
					message="Everything filed in this workspace without a project goes to the Inbox, "
					"so everybody in the workspace has to be able to see it.",
					hint="For a list of your own, make a private project and file into it with +key.",
				)
			],
		)

	if parent is not None:
		raise subroutine.errors.ValidationError(
			"The Inbox cannot be moved under another project.",
			errors=[
				subroutine.errors.FieldError(
					field="parent",
					code="invalid_field_value",
					message="A private project above it would hide the Inbox, where everything filed "
					"in this workspace without a project goes.",
				)
			],
		)


def may_rescue (actor: subroutine.domain.authentication.Principal | None) -> bool:
	"""Report whether this credential may let somebody into a project nobody can reach - `#1453`.

	**The credential's half of :func:`rescuable`**, asked on its own where there is no project to
	ask about yet. ``instance:admin``, on a credential that answers for every project on the
	installation: not pinned to one workspace, and **not narrowed to some projects** (`#2619`). The
	share this admits skips the check that applies a credential's project narrowing, so a narrowed
	credential not refused here is a narrowing applied nowhere. ``may_instance`` refuses both.
	"""

	if actor is None:
		return False

	return subroutine.domain.authorization.may_instance(actor, subroutine.permissions.INSTANCE_ADMIN)


def rescuable (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	project: subroutine.db.models.project.Project,
) -> bool:
	"""Report whether this caller may let somebody into a project nobody can reach - `#1453`.

	A credential :func:`may_rescue` admits, and a private project with no person who can act and
	see it (`SR#4569`, decision `#4526`). Both, every time: the first is who may repair the state,
	and the second is the state, so neither widens anything while a person can reach the project.
	"""

	if project.visibility != "private" or not may_rescue(actor):
		return False

	return not subroutine.domain.repair.repairers(session, project)


def unshare (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	user: subroutine.db.models.identity.User,
	*,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> None:
	"""Take somebody's sight of a private project away again — item `#1444`.

	Gated by ``project:write`` and its steward, like :func:`share`, and the reasoning is written
	there. **Removing yourself is yours** (decision `#4509`), as it always was.

	**Two refusals, and both protect against a project nobody can reach.** The owner's own row
	is not removable while the owner can act — :func:`create` writes it *"so that making a project
	private later does not lock its owner out"*, and this is that same sentence one command along.
	**An owner who can no longer act holds it no more** (`SR#4572`, C section 2.4 of the cold review
	of 2026-10-05): their row pinned a project they could not see, and its steward's stand-in, who
	can, may take it on. Nor is the row of
	the last person who can see it: `#1453` is the state this avoids, where a private project has
	nobody who can see it or make it public again. **A person who can act, by the one predicate**
	(`SR#4569`, decision `#4526`): this counted rows, those of people who had left included, so the
	last member who could act could strand it (G11 of the cold review of 2026-10-05). **And every
	private project inside a private one**, which the row being taken away is what lets them see.
	**An agent's row too** (`SR#4944`, decision `#4943`): whoever it answers to sees through it,
	so the question is who could still see each project with this one row gone, whoever holds it.

	**It works whatever the project's visibility is**, unlike :func:`share`. A row on a public
	project grants nothing today and everything the day somebody makes it private, so leaving
	it unremovable would make privacy a one-way door in the other direction.
	"""

	subroutine.domain.authorization.authorize_on(
		session, actor, subroutine.permissions.PROJECT_WRITE, project
	)

	if not subroutine.domain.stewardship.holds(actor, user.id):
		subroutine.domain.stewardship.refuse_unless_steward(
			session, actor, project, act="remove somebody else from it", field="username"
		)

	membership = _membership_of(session, project, user.id)

	if membership is None:
		raise subroutine.errors.NotFound(
			f"{project.key} is not shared with {user.username}.",
			hint=f"'subroutine project share {project.key} {user.username}' would do that.",
		)

	if project.owner_id == user.id and subroutine.domain.accountability.can_act(
		session, user, workspace_id=project.workspace_id
	):
		raise subroutine.errors.ValidationError(
			f"{user.username} owns {project.key} and cannot be removed from it.",
			# **No command is named here on purpose.** Ownership moves through
			# `PATCH /v1/projects/{id_or_key}` and no terminal surface sets it, so naming one
			# would be `#1709`'s defect — a remedy the reader cannot run.
			hint="Hand the project over first, or make it public.",
			errors=[
				subroutine.errors.FieldError(
					field="username",
					code="invalid_field_value",
					message=f"{user.username} is this project's owner.",
				)
			],
		)

	# Refusing only a change from somebody to nobody: a project no person could see already is not
	# this act's to refuse, and an instance administrator's rescue is what brings one back.
	for one in _losing_sight(session, project):
		could = subroutine.domain.repair.repairers(session, one)

		if not could or subroutine.domain.repair.repairers(session, one, unsharing=membership):
			continue

		# **One row gives sight to one person**: its holder, or whoever its holder answers to.
		last = session.get(subroutine.db.models.identity.User, next(iter(could)))
		seer = user.username if last is None else last.username
		inside = "," if one.id == project.id else f", inside {project.key},"

		# **Named only to a caller who can see it** (`SR#5100`, H-4 of the cold review of
		# 2026-10-17, decision `#4521`): a private child the caller cannot see was named three times
		# over, so an owner could read its key, and who alone sees it, from a refusal. The refusal
		# stands - #4943 rule 4 - and the person being unshared, who can see it, is who to ask.
		key = key_named_to(session, actor, one)
		named = key or subroutine.domain.scoping.UNSEEN

		if seer != user.username:
			inside = f"{inside} and only through their agent {user.username},"

		# **A remedy this caller can take** (`SR#4570`): making it public and sharing it are its
		# steward's now, so somebody removing themselves who is not is pointed at the one who is. **And
		# only what their credential may do** (`SR#5106`): making it public is asked at the workspace's
		# place, which a credential narrowed to some projects does not reach.
		if key is None:
			hint = f"Ask {seer} to share it with somebody else first."

		elif may_publish(session, actor, one):
			hint = f"'subroutine project update {key} --public' first, or share it with somebody else."

		elif may_share(session, actor, one):
			hint = "Share it with somebody else first."

		else:
			hint = f"Ask the owner of '{key}' to share it with somebody else first."

		raise subroutine.errors.ValidationError(
			f"{seer} is the only person who can see {named}{inside} so removing "
			f"{'them' if seer == user.username else user.username} would leave no person able "
			f"to reach it.",
			hint=hint,
			errors=[
				subroutine.errors.FieldError(
					field="username",
					code="invalid_field_value",
					message=f"{seer} is the last person who can see {named}.",
				)
			],
		)

	identifier = membership.id

	session.delete(membership)
	session.flush()

	subroutine.domain.events.record(
		session,
		workspace_id=project.workspace_id,
		entity_type="project_member",
		entity_id=identifier,
		action=subroutine.domain.events.EventAction.DELETED,
		subject_type="project",
		subject_id=project.id,
		changes={"user_id": {"from": user.id, "to": None}},
		actor=actor,
	)


def key_named_to (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	project: subroutine.db.models.project.Project,
) -> str | None:
	"""Return a project's key for a refusal to name to this caller, or ``None`` where they cannot see it.

	**A refusal that names another row resolves it through the caller's reach first** (decision
	`#4521`, `SR#5100`): one that does not resolve is said as :data:`scoping.UNSEEN`, and its hint
	offers only what this caller can do. ``None`` as the actor is an internal caller, which reads
	everything.
	"""

	if actor is None:
		return project.key

	failure = subroutine.domain.authorization.refusal(
		session,
		actor,
		subroutine.permissions.PROJECT_READ,
		workspace_id=project.workspace_id,
		project=project,
	)

	return project.key if failure is None else None


def _losing_sight (
	session: sqlalchemy.orm.Session, project: subroutine.db.models.project.Project
) -> list[subroutine.db.models.project.Project]:
	"""Return what somebody's row on this project lets them see: it, and if private, what is inside.

	Privacy inherits down the tree, so a private project's row is what lets its member see every
	project beneath it; a public one's lets them see nothing beyond itself. The private ones are
	those a person could be the last to see.
	"""

	if project.visibility != "private":
		return [project]

	model = subroutine.db.models.project.Project

	return [
		project,
		*session.scalars(
			sqlalchemy.select(model)
			.where(
				model.workspace_id == project.workspace_id,
				model.path.like(f"{project.path}%"),
				model.id != project.id,
				model.visibility == "private",
				model.deleted_at.is_(None),
			)
			.order_by(model.path)
		),
	]


def _refuse_stranding (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	project: subroutine.db.models.project.Project,
	*,
	afterwards: subroutine.domain.repair.Afterwards,
	doing: str,
	field: str,
) -> None:
	"""Refuse making a project private, or moving it, where a private project is left with nobody.

	**The last-one question, asked of two more acts** (`SR#5101`, M-4 of the cold review of
	2026-10-17, decision `#5097`): asked of the project and each private project inside it, of who
	could see it once the act is taken, computed rather than rehearsed. **Refusing only a change from
	somebody to nobody**, as :func:`unshare` does: one nobody could reach already is the rescue's.

	``doing`` opens the refusal, *Making outer private*. **Named only to a caller who can see it**
	(decision `#4521`), and the hint says what to do first: make somebody who can see it the owner of
	the project made private, since a public project cannot be shared, or share the private project
	the move would hide it beneath.
	"""

	# **The workspace is locked before anybody is counted**, as removing somebody locks it (`#1178`,
	# `#3899`), so this cannot race a removal or an unshare counting the same people.
	place = subroutine.db.models.identity.Workspace
	session.execute(
		sqlalchemy.update(place)
		.where(place.id == project.workspace_id)
		.values(updated_at=place.updated_at)
		.execution_options(synchronize_session=False)
	)

	model = subroutine.db.models.project.Project
	inside = list(
		session.scalars(
			sqlalchemy.select(model)
			.where(
				model.workspace_id == project.workspace_id,
				subroutine.domain.hierarchy.subtree(model, project),
				model.visibility == "private",
				model.deleted_at.is_(None),
			)
			.order_by(model.path)
		)
	)
	becoming = project.visibility != "private" and project.id in afterwards.private

	for one in [project, *inside] if becoming else inside:
		# **A project being made private has somebody before**: whoever makes it private can see it,
		# and so does the person an agent doing so answers to. Each other is asked who sees it now.
		could = None if one is project and becoming else subroutine.domain.repair.repairers(session, one)

		if could == set() or subroutine.domain.repair.repairers(session, one, afterwards=afterwards):
			continue

		key = key_named_to(session, actor, one)
		named = key or subroutine.domain.scoping.UNSEEN
		people = sorted(
			(
				row
				for row in (
					session.get(subroutine.db.models.identity.User, identifier)
					for identifier in could or ()
				)
				if row is not None
			),
			key=lambda row: row.username,
		)
		seer = people[0] if people else None
		hiding = None if seer is None else hidden_by(session, one, seer.id, afterwards=afterwards)

		if seer is None:
			hint = "Make somebody who can act its owner first."

		elif hiding is not None and hiding.id != project.id:
			hint = (
				f"'subroutine project share {hiding.key} {seer.username}' first."
				if subroutine.domain.stewardship.stewards(session, actor, hiding)
				else f"Ask the owner of '{hiding.key}' to share it with {seer.username} first."
			)

		elif key is None:
			hint = f"Make {seer.username} the owner of {project.key} first, since they can see it."

		else:
			hint = f"Make {seer.username}, who can see {key}, the owner of {project.key} first."

		raise subroutine.errors.ValidationError(
			f"{doing} would leave no person able to reach "
			f"{'it' if one.id == project.id else f'{named} inside it'}.",
			hint=hint,
			errors=[
				subroutine.errors.FieldError(
					field=field,
					code="invalid_field_value",
					message="It would be hidden from everybody who can see it now.",
				)
			],
		)


def members (
	session: sqlalchemy.orm.Session,
	project: subroutine.db.models.project.Project,
	*,
	actor: subroutine.domain.authentication.Principal | None = None,
) -> list[tuple[subroutine.db.models.project.ProjectMember, subroutine.db.models.identity.User]]:
	"""Return who has been shared into a project, oldest first.

	Requires ``project:read``. Nothing anywhere else answers *who can see this*, which is why
	it is worth a route of its own even before `#1451` gives a project a page to render it on.

	A public project ordinarily reports just its owner, and that is honest rather than
	misleading: the rows say who would still see it if somebody made it private, which is the
	question anybody about to do that is asking.
	"""

	subroutine.domain.authorization.authorize_on(
		session, actor, subroutine.permissions.PROJECT_READ, project
	)

	model = subroutine.db.models.project.ProjectMember
	account = subroutine.db.models.identity.User

	rows = session.execute(
		sqlalchemy.select(model, account)
		.join(account, account.id == model.user_id)
		.where(model.project_id == project.id)
		.order_by(model.created_at, account.username)
	).all()

	return [(membership, holder) for membership, holder in rows]


def status_for (
	session: sqlalchemy.orm.Session, workspace_id: uuid.UUID, key: str | None = None
) -> subroutine.db.models.vocabulary.Status:
	"""Return a project status by key, or the one a new project is given when none is named.

	**Widened from ``default_status`` by `#983`**, which found that three of the four seeded
	project statuses could never be reached: ``PATCH /v1/projects`` did not accept a status and
	no other route set one, so every project ever created was ``active`` for ever. Shaped after
	:func:`subroutine.domain.tasks.status_for` rather than beside it, because the two differ
	only in ``entity_type`` and a reader who knows one should recognise the other.

	The refusal lists what would have worked. §5.5 makes a workspace's vocabulary its own, so
	``on_hold`` is a seeded key rather than a promised one and naming it in a message would be
	asserting something an installation is free to have renamed.
	"""

	# **Half a character is refused by name** (`#4287`, M4 of the cold review of 2026-10-03):
	# the driver cannot encode a lone surrogate, so it reached the database as a 500 and the
	# terminal as a traceback.
	subroutine.domain.text.readable(key, field="status")

	model = subroutine.db.models.vocabulary.Status

	statement = sqlalchemy.select(model).where(
		model.workspace_id == workspace_id, model.entity_type == "project"
	)

	if key is None:
		status = session.scalars(
			statement.where(model.is_default.is_(True)).order_by(model.position)
		).first()

	else:
		status = session.scalars(statement.where(model.key == key)).one_or_none()

	if status is not None:
		return status

	available = sorted(
		session.scalars(
			sqlalchemy.select(model.key).where(
				model.workspace_id == workspace_id, model.entity_type == "project"
			)
		)
	)

	raise subroutine.errors.ValidationError(
		"This workspace has no default project status."
		if key is None
		else f"There is no project status called {key!r} here.",
		code="invalid_status",
		errors=[
			subroutine.errors.FieldError(
				field="status",
				code="not_found",
				message=f"No project status with key {key!r} exists in this workspace."
				if key is not None
				else "No project status is marked as the default.",
				hint=f"Statuses here: {', '.join(available)}." if available else None,
			)
		],
	)


def check_key (normalized_key: str, *, given: str | None = None) -> None:
	"""Refuse a project key that cannot be used, saying which rule it broke.

	Extracted so that creating a project and **renaming** one apply the same rules (`#176`).
	Before that there was only one caller and the checks were inline; a rename that skipped
	them could arrive at a key nobody could have chosen in the first place, which is a worse
	state than either command allows on its own.

	``given`` is what the caller actually typed, for the message. ``'Café'`` normalises to
	``'café'`` and is refused for its accent; telling somebody ``'café'`` is not usable when they
	wrote something else reads as the program mangling their input and then blaming them for it.
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
					"is kinder - 'web-sales' rather than 'website-sales-and-marketing'.",
				)
			],
		)

	# **An address is not a key, and saying so is the whole of this branch.** A project is
	# named `dist` and *addressed* `substation/dist` (decision `#957`), so somebody creating
	# one and typing the address has said something coherent that this cannot act on — where
	# the generic refusal below would tell them a letter must come first, which is true of
	# what they wrote and no help at all.
	if PATH_SEPARATOR in normalized_key:
		raise subroutine.errors.ValidationError(
			f"{wrote!r} cannot be used as a project key.",
			errors=[
				subroutine.errors.FieldError(
					field="key",
					code="invalid_field_value",
					message=f"{wrote!r} is an address rather than a key.",
					hint=f"A project is keyed by its last part alone - "
					f"{normalized_key.rsplit(PATH_SEPARATOR, 1)[-1]!r} - and put inside "
					f"another by naming that one as its parent.",
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
					"between them - 'sr', 'home', 'web-sales'.",
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


def normalize_path (wanted: str) -> str:
	"""Return the stored form of a whole project address: each segment, normalised.

	**Empty segments are dropped**, so ``substation//dist`` and a stray leading or trailing
	separator all mean what somebody typing them meant. That is the one liberty taken here,
	and it is the same liberty :func:`normalize_key` takes with case: a correction nobody
	would be surprised by. Everything else is refused with an explanation by
	:func:`check_key`, which sees each segment separately and so can say *which* one.
	"""

	segments = [normalize_key(segment) for segment in wanted.split(PATH_SEPARATOR)]

	return PATH_SEPARATOR.join(segment for segment in segments if segment)


def path_segments (wanted: str) -> list[str]:
	"""Return the keys along an address, outermost first."""

	return [segment for segment in normalize_path(wanted).split(PATH_SEPARATOR) if segment]


def prioritised_addresses (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	*,
	workspace_ids: typing.Sequence[uuid.UUID],
) -> dict[uuid.UUID, str]:
	"""Return each workspace's prioritised project as an address a person reads — `#986`.

	The label half of decision ``#982``. ``scoping.prioritised_paths`` is the other one, and the
	two are separate because **they need different strings**: an ordering compares against
	``project.path``, which is a materialised path of ids, while a surface has to say
	``web/dist``. Reported as one string rather than as an id for `#957`'s reason — an address is
	what ``PATCH /v1/workspaces`` accepts back, so what a reader is shown is also what they can
	send.

	**Here rather than in ``scoping``**, which cannot compose an address: this module imports
	that one, and the reverse is the import cycle `#202` exists to catch. It is also where every
	other project address is composed, through :func:`paths_for`, so there is one answer to
	*what is this project called* rather than two that can disagree.
	"""

	chosen = subroutine.domain.scoping.prioritised_projects(
		session, principal, workspace_ids=workspace_ids
	)

	if not chosen:
		return {}

	addresses = paths_for(session, {row.id for row in chosen.values()})

	return {
		workspace_id: addresses[row.id]
		for workspace_id, row in chosen.items()
		if row.id in addresses
	}


def paths_for (
	session: sqlalchemy.orm.Session, ids: typing.Collection[uuid.UUID]
) -> dict[uuid.UUID, str]:
	"""Return the address of each of these projects — its ancestors' keys and its own.

	**Two queries for a whole page, never one per row.** ``project.path`` already carries
	every ancestor's id, so the ancestors need looking up rather than walking: this reads the
	paths, then every key those name, and joins in Python. The per-row version is `#39`'s N+1
	on a listing that renders a project column on every line.

	**By id rather than by loaded rows**, because the caller that matters is
	``views.Vocabulary``, which knows a page's project ids and has no rows. The two queries
	overlap slightly with the keys that class loads beside them; merging them would save one
	round trip on a page and cost this being the only place an address is composed, which is
	the trade `#508` is the worked example of getting wrong.

	**Deliberately not narrowed by scoping, and that discloses nothing.** Visibility
	inherits *down* a project tree — §7.3a hides a project when it or any ancestor is
	private without a membership — so anybody who can see a row can already see every
	ancestor whose key this composes. Narrowing here would answer a question nobody asked
	and would render a partial address, which is worse than none: an address that resolves
	somewhere else is the failure this whole change exists to remove.
	"""

	if not ids:
		return {}

	model = subroutine.db.models.project.Project

	# `.all()` rather than the `Result` itself: a bare `dict(session.execute(...))`
	# raises, because a `Result` has a `.keys()` method and `dict` therefore treats it as a
	# mapping. That is a recorded trap here, met once as a ruff C416 suggestion applied to
	# working code.
	paths = dict(
		session.execute(sqlalchemy.select(model.id, model.path).where(model.id.in_(set(ids))))
		.all()
	)
	ancestry = {
		uuid.UUID(segment)
		for path in paths.values()
		for segment in subroutine.domain.hierarchy.path_segments(path)
	}

	if not ancestry:
		return {}

	keys = dict(
		session.execute(sqlalchemy.select(model.id, model.key).where(model.id.in_(ancestry)))
		.all()
	)
	composed: dict[uuid.UUID, str] = {}

	for identifier, path in paths.items():
		segments = [
			keys.get(uuid.UUID(segment))
			for segment in subroutine.domain.hierarchy.path_segments(path)
		]

		# **Nothing at all when an ancestor could not be read, rather than a suffix.** The
		# foreign key is ``RESTRICT`` and a delete is soft, so every ancestor row exists and
		# this cannot happen through any supported path. It matters which way it fails
		# anyway: a suffix is an address that resolves *somewhere else*, where an absence is
		# a column that says nothing.
		if all(segment is not None for segment in segments):
			composed[identifier] = PATH_SEPARATOR.join(
				segment for segment in segments if segment is not None
			)

	return composed


def path_of (
	session: sqlalchemy.orm.Session, row: subroutine.db.models.project.Project
) -> str:
	"""Return one project's address. :func:`paths_for` is the form a listing wants."""

	return paths_for(session, [row.id]).get(row.id, row.key)


def sibling_using (
	session: sqlalchemy.orm.Session,
	workspace_id: uuid.UUID,
	key: str,
	*,
	parent_id: uuid.UUID | None,
	besides: uuid.UUID | None = None,
) -> uuid.UUID | None:
	"""Return the id of a live project already keyed this **under the same parent**, or none.

	**One query, three sentences.** Creating, renaming, moving and restoring all ask this and
	each has its own thing to say about the answer — a restore in particular has to name the
	project that took the key and how to rename it. Sharing the *question* is what stops the
	four coming apart; sharing the wording would make three of them worse.

	``parent_id`` of ``None`` means the top level, and is compared with ``IS NULL`` rather
	than ``=`` — the same rule that makes the database's guarantee two partial indexes rather
	than one (`#957`). ``besides`` excludes a row from the comparison, for the caller asking
	whether *somebody else* has the key.
	"""

	model = subroutine.db.models.project.Project

	return session.scalars(
		sqlalchemy.select(model.id).where(
			model.workspace_id == workspace_id,
			model.key == key,
			model.parent_id == parent_id
			if parent_id is not None
			else model.parent_id.is_(None),
			model.deleted_at.is_(None),
			sqlalchemy.true() if besides is None else model.id != besides,
		)
	).first()


def _refuse_duplicate_key (
	session: sqlalchemy.orm.Session,
	workspace_id: uuid.UUID,
	key: str,
	*,
	parent_id: uuid.UUID | None,
	moving: str | None = None,
) -> None:
	"""Raise if a live sibling already uses this key — decision `#957`.

	**Among its siblings, not across the workspace.** ``web-ui`` belongs under any number of
	parents; what has to stay unique is the address, which is the path. The database says the
	same thing in two partial indexes, and this says it in a sentence somebody can act on.

	``moving`` names the project being reparented, for the one caller where the collision is
	not about a key anybody just typed: :func:`move` refuses a destination that already holds
	this key, and *"a project with the key 'dist' already exists here"* about a key the caller
	never mentioned reads as the wrong project having been named.
	"""

	if sibling_using(session, workspace_id, key, parent_id=parent_id) is None:
		return

	where = "at the top level" if parent_id is None else "in that project"

	if moving is not None:
		raise subroutine.errors.Conflict(
			f"There is already a project keyed {key!r} {where}.",
			code="duplicate_key",
			errors=[
				subroutine.errors.FieldError(
					field="parent",
					code="duplicate_key",
					message=f"Moving {moving!r} there would give two projects the same "
					f"address.",
					hint="Rename one of them first, or move it somewhere else.",
				)
			],
		)

	raise subroutine.errors.Conflict(
		f"A project with the key {key!r} already exists {where}.",
		code="duplicate_key",
		errors=[
			subroutine.errors.FieldError(
				field="key",
				code="duplicate_key",
				message=f"The key {key!r} is already in use {where}.",
				hint="A key is how this project is addressed among its siblings, so no two "
				"under one parent can share it. Somewhere else in this workspace is fine.",
			)
		],
	)
