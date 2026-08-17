"""Which workspace — and which project — a request is about (docs/design.md §8.2, §5.2).

Resolution order: what the caller asked for, then the workspace their token is pinned to,
then their only one. **Ambiguity is a refusal, never a guess** — with two workspaces and
nothing specified, picking one means a task filed somewhere the caller did not look, and
they find out days later.

The refusal lists the workspaces they can reach, with both id and slug, so the second
attempt is informed rather than another guess.

**In the domain rather than in ``api``, because the CLI applies the identical rule.** §13.7
resolves a current context in five steps — flag, environment, stored context, the sole
workspace, then refuse — and steps four and five *are* this function. Two copies of "which
workspace is this about" would be two copies free to disagree about the one case that
matters, which is the ambiguous one.
"""

import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.hierarchy
import subroutine.domain.projects
import subroutine.domain.scoping
import subroutine.domain.users
import subroutine.domain.workspaces
import subroutine.errors


def workspace (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	*,
	requested: str | None = None,
) -> subroutine.db.models.identity.Workspace:
	"""Return the workspace this request is about, or refuse with the alternatives.

	``requested`` accepts an id or a slug. The parameter is named ``workspace_id`` in the
	API because §8.2 names it that, and it takes a slug as well because a person typing one
	by hand has the slug in front of them and the id nowhere.
	"""

	reachable = subroutine.domain.workspaces.readable(session, principal)

	if requested is not None:
		return _named(requested, reachable)

	if principal.pinned_workspace_id is not None:
		# The pin already narrowed `reachable` to one; this is only reporting it clearly if
		# that workspace has since been deleted or the membership withdrawn.
		return _only(reachable, "The workspace this token is pinned to is no longer readable.")

	return _only(reachable, "You are not a member of any workspace.")


#: The word a caller uses for its own account. **Opt-in per call site rather than accepted
#: everywhere** (`#518`): the resolver below also serves minting a sign-in link and signing an
#: account out of every browser, and a sentinel that quietly worked there would widen the
#: grammar of two credential routes as a side effect of adding a listing filter. `#829` is what
#: that costs when it goes unnoticed, so a caller that means to allow this says so.
CALLER = "me"


def user (
	session: sqlalchemy.orm.Session,
	given: str,
	*,
	caller: subroutine.db.models.identity.User | None = None,
) -> subroutine.db.models.identity.User:
	"""Return the account this names, whether it was named by username or by id.

	``caller`` is the account ``me`` stands for, and passing none means ``me`` is an ordinary
	username that will not resolve — which is the answer every credential route here wants.

	**``me`` is the person, where ``?actor=me`` on the change feed is the credential** (`#158`).
	Two spellings of one word and they are not the same thing, which `#335` measured: an agent
	with a shell is two principals. The field is what settles it — *who did this* is a question
	about a credential, *who holds this* is a question about an account — and each takes the one
	it is about. Documented here because "which does `me` mean" is the question a reader of
	either will have.

	**The house pattern for an identifier a person types** (`#501`). ``/v1/tasks/{id_or_ref}``
	and ``/v1/projects/{id_or_key}`` both take either form, and an assignee should not be the
	one place a caller has to be holding a UUID — asking *"what is Simon working on"* needs
	the word ``simon``, which is what somebody has.

	**Resolved here rather than in each client, deliberately.** Two clients resolving a
	username would be two copies of one rule, which is this codebase's recorded signature
	defect; and the refusal is the half that matters — ``users.by_username`` names the account
	it could not find and points at the command that lists them, so both transports inherit
	one sentence rather than inventing two.

	A value that parses as a UUID is taken as an id. A username cannot collide with one in
	practice — :func:`subroutine.domain.users.normalize` requires a leading letter and a UUID's
	first group is eight characters of hex — but the order is stated rather than left to be
	inferred, because "which wins" is exactly the question a reader of this will have.
	"""

	if caller is not None and given == CALLER:
		return caller

	try:
		identifier = uuid.UUID(given)

	except ValueError:
		return subroutine.domain.users.by_username(session, given)

	found = session.get(subroutine.db.models.identity.User, identifier)

	if found is None or found.deleted_at is not None:
		raise subroutine.errors.NotFound(
			f"There is no account with the id {given!r} here.",
			hint="Name the account by its username instead — 'subroutine user list' shows them.",
		)

	return found


def _named (
	requested: str,
	reachable: typing.Sequence[subroutine.db.models.identity.Workspace],
) -> subroutine.db.models.identity.Workspace:
	"""Return the requested workspace, if the caller can reach it."""

	wanted = requested.strip()
	parsed: uuid.UUID | None = None

	try:
		parsed = uuid.UUID(wanted)

	except ValueError:
		parsed = None

	for candidate in reachable:
		if candidate.id == parsed or candidate.slug == subroutine.domain.workspaces.normalize_slug(wanted):
			return candidate

	# Not distinguishing "no such workspace" from "not yours", per §8.7: saying which would
	# confirm the existence of a workspace this caller has no business knowing about.
	raise subroutine.errors.NotFound(
		f"There is no workspace {requested!r} that you can reach.",
		errors=[
			subroutine.errors.FieldError(
				field="workspace_id",
				code="not_found",
				message=f"No readable workspace matches {requested!r}.",
				hint=_alternatives(reachable),
			)
		],
	)


def _only (
	reachable: typing.Sequence[subroutine.db.models.identity.Workspace], empty: str
) -> subroutine.db.models.identity.Workspace:
	"""Return the sole reachable workspace, refusing when there is a choice to be made."""

	if not reachable:
		raise subroutine.errors.NotFound(
			empty,
			hint="Ask an administrator to add you to one, or create one if you may "
			"(POST /v1/workspaces).",
		)

	if len(reachable) > 1:
		raise subroutine.errors.ValidationError(
			"This request could be about any of several workspaces, so it needs to say which.",
			errors=[
				subroutine.errors.FieldError(
					field="workspace_id",
					code="missing_field",
					message="You can reach more than one workspace, and this request named none.",
					hint=_alternatives(reachable),
				)
			],
			# **The field is not named here, and that is deliberate** (`#547`). This refusal is
			# raised below the transport and read on two of them, which call the same thing by
			# two names — ``workspace_id`` in a problem document, ``workspace`` as an MCP tool's
			# argument. Naming one spelling in the prose made the advice wrong for the other
			# reader, who then followed it and was refused again. The ``FieldError`` beside this
			# carries the name, and each surface renders that in its own vocabulary.
			hint="Say which workspace this is about, or use a token pinned to one.",
		)

	return reachable[0]


def _alternatives (
	reachable: typing.Sequence[subroutine.db.models.identity.Workspace],
) -> str:
	"""Describe the workspaces the caller can reach, by both names.

	**Saying that either form is accepted, without naming the field it goes in** — `#676`
	asked for the parameter to be named here and `#547` is why it is not: this refusal is
	raised below the transport and read on two of them, which call the same thing
	``workspace_id`` and ``workspace``, so any spelling in this prose is wrong for one of
	its two readers. The ``FieldError`` carries the name and each surface renders it.

	What the pairs *did* leave unsaid is that both halves of each pair work. Listing a name
	beside an id and saying nothing reads as a menu of one kind of value with an
	unexplained bracket after it, and a caller who takes the id when a name would have done
	has learned the harder of the two.
	"""

	if not reachable:
		return "You are not a member of any workspace."

	listed = ", ".join(f"{candidate.slug} ({candidate.id})" for candidate in reachable)

	return f"Workspaces you can reach, by either name or id: {listed}."


def project (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	workspace: subroutine.db.models.identity.Workspace,
	wanted: str | None,
) -> typing.Any:
	"""Find a project by key or id, defaulting to the workspace's Inbox.

	The Inbox default is what makes ``POST /v1/tasks {"title": "…"}`` work without the caller
	knowing that projects exist — §1.4's rule, applied to the API rather than only to the CLI.

	**Here rather than in the router**, for the reason :mod:`subroutine.views` is: both
	transports have to resolve ``SR`` to the same project and refuse an unknown key the same
	way. It lived in ``api/tasks.py``, which the local client may not import, so
	``subroutine list --project`` had no way to reach it and would have grown a second
	resolver — which is the divergence S3-07 removed for the task shape, and ``domain/links``
	for the link view.

	Narrowed by :func:`subroutine.domain.scoping.readable_projects`, so a private project
	somebody is not a member of is *not found* rather than forbidden, and a token's project
	scope narrows this exactly as it narrows a listing.
	"""

	if wanted is None:
		return _files_where(session, actor, workspace)

	return addressed(session, actor, workspace, wanted, field="project")


def addressed (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal | None,
	workspace: subroutine.db.models.identity.Workspace,
	wanted: str,
	*,
	field: str,
	include_deleted: bool = False,
) -> subroutine.db.models.project.Project:
	"""Find the one project this names — by id, by whole address, or by bare name.

	**A name resolves by search; an address resolves exactly** (decision `#957`, Simon's
	answer of 2026-08-17). ``dist`` works while only one project is keyed that way and is
	**refused by name, with the candidates listed**, once two are; ``substation/dist`` always
	works. So nothing anybody has already typed stops working, and the address is what you
	reach for when the name stops being enough rather than a form you must learn first.

	The accepted cost is that a command can begin failing because somebody *else* created a
	second ``dist``. The refusal is what teaches the address form, at the moment it matters.

	**An address is absolute within the workspace**, walked from a root. A relative one would
	put the ambiguity back: ``substation/dist`` would then also match a ``dist`` under some
	other ``substation`` further down, which is the thing being removed.

	``field`` is the name the caller's own surface gives this, because this refusal is read on
	both transports and they do not agree on one — ``project`` in a filter, ``id_or_key`` in a
	path (`#547`, met where a refusal named a parameter no tool declares).

	**One function, so no two surfaces can drift.** ``api/projects.resolve`` and
	``domain.tasks._project_by_key`` were each a second copy of *text → project* — the same
	shape S3-07 removed for tasks — and both are this now. `#957` said there was one resolver
	and there were three; the capture path's was the one that mattered, because it read a key
	with ``.one_or_none()`` and so raised a raw ``MultipleResultsFound`` at the reader the
	first time two projects shared a name.

	``actor`` is ``None`` for the unauthenticated internal caller — bootstrap and the tests —
	which holds no credential and so is narrowed by none of this (§12.1a). It is the one case
	``readable_projects`` cannot express, since it has no principal to be handed.
	"""

	model = subroutine.db.models.project.Project
	statement = (
		sqlalchemy.select(model).where(
			model.workspace_id == workspace.id,
			sqlalchemy.true() if include_deleted else model.deleted_at.is_(None),
		)
		if actor is None
		else subroutine.domain.scoping.readable_projects(
			actor,
			workspace_ids=[workspace.id],
			include_deleted=include_deleted,
			include_archived=True,
		)
	)

	# A key and an id are told apart by whether the text parses as one, rather than by a
	# flag: §5.2 makes a key start with a letter, so the two spaces cannot overlap.
	try:
		identifier = uuid.UUID(wanted.strip())

	except ValueError:
		identifier = None

	if identifier is not None:
		found = session.scalars(statement.where(model.id == identifier)).first()

	else:
		# **The address is tried first, whatever its length, and then the name.** For a single
		# segment that means *a root keyed this* before *anything keyed this*, and the order is
		# forced rather than chosen: with a root ``dist`` beside a nested ``substation/dist``,
		# the root's own address is the bare word — so searching first would refuse it as
		# ambiguous with itself and leave that project reachable by no string at all.
		#
		# It is not a guess, which is what `#957` ruled out. An exact address resolving exactly
		# is that decision's other half, and this is the one-segment case of it.
		segments = subroutine.domain.projects.path_segments(wanted)
		found = _walked(session, statement, segments)

		# **Only a single segment falls back to a search.** A whole address that does not
		# resolve is a whole address that does not resolve; searching for its first segment
		# instead would answer ``substation/nope`` with ``substation``, which is a different
		# project and a plausible, complete, wrong answer.
		if found is None and len(segments) == 1:
			found = _by_name(session, statement, segments[0], wanted, workspace, field=field)

	if found is None:
		raise subroutine.errors.NotFound(
			f"There is no project {wanted!r} here.",
			errors=[
				subroutine.errors.FieldError(
					field=field,
					code="not_found",
					message=f"No project in {workspace.slug} answers to {wanted!r}.",
					hint=_alternative_projects(session, statement),
				)
			],
		)

	return found


def _alternative_projects (session: sqlalchemy.orm.Session, statement: typing.Any) -> str:
	"""Say what the caller could have meant, in the form they would have to type.

	**Addresses rather than bare keys**, because a key stopped being unique (`#957`): a list
	holding ``dist`` twice tells somebody the spelling was right and nothing else, where
	``substation/dist, websites/dist`` is the answer *and* teaches the form that resolves.

	**Narrowed, because ``statement`` already is.** ``domain.tasks``' own version of this was
	the outlier that was not, and answered ``+nosuchkey`` with every private project's key in
	the workspace — the disclosure ``projects.keys_for`` describes, met one module along.

	Only on the refusal path, so the extra read costs nothing anybody is waiting on.
	"""

	rows = list(session.scalars(statement))

	if not rows:
		return "There are no projects here you can see."

	addresses = subroutine.domain.projects.paths_for(session, [row.id for row in rows])
	listed = ", ".join(sorted(addresses.values()))

	return f"Projects here, by name or whole address: {listed}."


def _walked (
	session: sqlalchemy.orm.Session,
	statement: typing.Any,
	segments: typing.Sequence[str],
) -> subroutine.db.models.project.Project | None:
	"""Follow a whole address, a key at a time, from a root of the workspace.

	One query per segment, which is at most :data:`hierarchy.DEFAULT_MAX_DEPTH` and in
	practice two — this resolves a single entity, where the same walk on a listing would be
	`#39`'s N+1. Reading ``project.path`` instead is not the shortcut it looks like: that
	path is made of **ids**, so composing one from keys would need every ancestor fetched
	anyway.
	"""

	model = subroutine.db.models.project.Project
	found: subroutine.db.models.project.Project | None = None

	for segment in segments:
		found = session.scalars(
			statement.where(
				model.key == segment,
				model.parent_id == found.id if found is not None else model.parent_id.is_(None),
			)
		).first()

		if found is None:
			return None

	return found


def _by_name (
	session: sqlalchemy.orm.Session,
	statement: typing.Any,
	key: str,
	wanted: str,
	workspace: subroutine.db.models.identity.Workspace,
	*,
	field: str,
) -> subroutine.db.models.project.Project | None:
	"""Find the project a bare name refers to, refusing rather than guessing between two.

	``None`` when nothing matches, so the caller raises the one *not found* both routes here
	share. Ambiguity raises instead, because the two are different answers: *there is no such
	project* is acted on by checking the spelling, and *there are two* by saying which.

	**Every candidate here is below a root**, because :func:`addressed` tried the address
	first and a root's address is its bare key. So the addresses this offers back are all
	longer than what was typed, which is what makes the advice actionable rather than a
	repetition of the question.
	"""

	model = subroutine.db.models.project.Project
	candidates = list(session.scalars(statement.where(model.key == key)))

	if len(candidates) < 2:
		return candidates[0] if candidates else None

	addresses = subroutine.domain.projects.paths_for(
		session, [candidate.id for candidate in candidates]
	)
	listed = ", ".join(sorted(addresses[candidate.id] for candidate in candidates))

	raise subroutine.errors.ValidationError(
		f"More than one project in {workspace.slug} is called {wanted!r}: {listed}.",
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="invalid_field_value",
				message=f"{wanted!r} names each of: {listed}.",
				hint=f"Say which, by its whole address — '{sorted(addresses.values())[0]}'.",
			)
		],
	)


def _files_where (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	workspace: subroutine.db.models.identity.Workspace,
) -> typing.Any:
	"""Return where a caller who named no project means, which is not always the Inbox.

	**The Inbox is the answer for a caller who can reach it** — §1.4's rule, and what makes
	``POST /v1/tasks {"title": "…"}`` work without knowing that projects exist.

	**It is the wrong answer for a credential scoped to a project** (`#369`). The Inbox is
	outside such a credential's reach, so the default sent the primary capture path — the one
	thing §1.4 is built around — into a refusal, on the first write, for exactly the agents
	`#216` exists to bound. Measured over HTTPS before it was fixed: ``403 Not permitted``.

	So a credential that can reach one project files there, which is the same rule the Inbox
	default is: **the default is the only place the caller could have meant.** Reaching two or
	more is ambiguous and is refused with them named, for the reason :func:`workspace` refuses
	an unspecified choice between two — a task filed somewhere the author did not look is found
	days later, if at all.

	**Where it may *write*, not merely where it can see** (`#416`). `#369` fixed this by the
	reach, which was the only list there was; `#371` then added a narrower one and this went on
	asking the wider question. A ``collaborator`` reaching ``SR`` and ``WEB`` but writing only in
	``WEB`` was told its task "could be filed under any of several projects" — with exactly one
	of them legal, and the answer sitting unread in the credential. That is `#369` again on
	§1.4's primary path, for the shape decision `#370` was taken for.

	The same correction one line up: a credential that can *reach* the Inbox and not write there
	filed into it and met a ``403`` on the write, which is the original defect with an extra
	step.
	"""

	inbox = subroutine.domain.bootstrap.inbox_for(session, workspace)

	# The unrestricted caller, unchanged and by far the commonest: §1.4's reader, who has not
	# heard of a project and must not have to.
	if actor.project_scope is None and actor.project_write_scope is None:
		if inbox is None:
			raise subroutine.errors.InternalError(
				"This workspace has no Inbox to file a task in.",
				hint="It was interrupted part-way through setup; run 'subroutine init' again.",
			)

		return inbox

	if inbox is not None and _may_file_in(actor, inbox):
		return inbox

	# **Its write set where it has one, its reach where it does not** — the same fallback
	# `authorization._within_write_scope` makes, so what a credential is *offered* here and what
	# it is *allowed* at the check cannot come apart.
	writes = actor.project_write_scope
	pointed = writes if writes is not None else actor.project_scope
	candidates = _named_within(session, actor, workspace, pointed)

	if len(candidates) == 1:
		return candidates[0]

	# Says which of the two restrictions left nowhere to file, because they have different
	# remedies and only one of them is about what the credential can see.
	bounded = "write in" if writes is not None else "reach"

	if not candidates:
		raise subroutine.errors.Forbidden(
			f"This credential cannot {bounded} any project in this workspace, so there is "
			f"nowhere to file it.",
			errors=[
				subroutine.errors.FieldError(
					field="project",
					code="forbidden",
					message=f"The credential is restricted to projects it can {bounded} "
					f"elsewhere.",
					hint="Ask whoever issued it to widen it, or name a workspace it reaches.",
				)
			],
		)

	named = ", ".join(sorted(found.key for found in candidates))

	raise subroutine.errors.ValidationError(
		"This could be filed under any of several projects, so it needs to say which.",
		errors=[
			subroutine.errors.FieldError(
				field="project",
				code="missing_field",
				message=f"This credential can {bounded} {named}, and this request named none.",
				hint=f"Name one of: {named}.",
			)
		],
	)


def _may_file_in (
	actor: subroutine.domain.authentication.Principal,
	project: subroutine.db.models.project.Project,
) -> bool:
	"""Report whether a credential could put a new item in this project at all.

	Both restrictions, in the one implementation ``authorization`` applies — a null on either
	means no narrowing on that axis, and a list is subtree-inclusive (`#413`). Deliberately
	**not** the role or the scopes: those decide whether this caller may write *anything*, and
	the permission check answers that a moment later with a message about the verb. This
	answers only "which project did they mean", where a place they could never write to is not
	a candidate.
	"""

	for allowed in (actor.project_scope, actor.project_write_scope):
		# The sentinel both restrictions share: no list means no narrowing on that axis.
		if allowed is None:
			continue

		if not subroutine.domain.hierarchy.within(
			allowed, identifier=str(project.id), path=project.path
		):
			return False

	return True


def _named_within (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	workspace: subroutine.db.models.identity.Workspace,
	identifiers: typing.Sequence[str] | None,
) -> list[typing.Any]:
	"""Return the projects a restricted credential is pointed at inside one workspace.

	**The projects it was *named* with, not everything those reach.** A scope of ``SR`` reaches
	``SR/WEB`` as well, and answering "which project did you mean" with the whole subtree would
	make a one-project credential look ambiguous.

	Still narrowed through :func:`subroutine.domain.scoping.readable_projects`, so a credential
	is never offered somewhere it cannot see — the write set is a subset of the reach, but that
	is enforced at issue and this is not the place to assume it held.
	"""

	if identifiers is None:
		return []

	model = subroutine.db.models.project.Project
	wanted = [uuid.UUID(item) for item in identifiers]

	return list(
		session.scalars(
			subroutine.domain.scoping.readable_projects(
				actor, workspace_ids=[workspace.id], include_archived=True
			).where(model.id.in_(wanted))
		)
	)


def token_projects (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	wanted: typing.Sequence[str] | None,
	*,
	workspace: subroutine.db.models.identity.Workspace | None = None,
) -> list[subroutine.db.models.project.Project] | None:
	"""Resolve what somebody typed into the projects a credential is restricted to — `#216`.

	``None`` when nothing was named, which is a credential not restricted by project at all.

	**An empty sequence is passed through as an empty list rather than folded into ``None``**,
	and the difference is the whole of §7.3's sentinel: not given means every project, given as
	empty means something the caller has not said clearly enough to act on, and
	``authentication`` refuses it by name. Folding them here would answer that question on the
	caller's behalf — and in the widening direction, which is the one that matters. The
	existing endpoint test caught exactly that when this function first took the argument.

	**Keys in, ids out.** ``api_token.project_scope`` holds ids, because a key can be renamed
	(`#176`) and a credential must not quietly follow it onto whatever takes the old name; a
	person setting an agent up types ``SR``. Resolving one to the other is the whole of this
	function — and it means the CLI insists the project *exists*, where ``POST /v1/tokens``
	deliberately does not. The API's permissiveness is for a caller naming a project it cannot
	see or one that does not exist yet; a mistyped key typed by a person is a credential
	silently refused everywhere it is presented, which is the worse failure of the two.

	``workspace`` narrows where each name is looked for. Without one, every workspace this
	actor can read is a candidate and an ambiguous name is refused rather than picked —
	a key is unique per workspace, not per instance (§5.2), so two of them may each hold a
	``WEB``, and handing an agent authority over the wrong tree is a mistake nothing
	downstream could notice: the credential works.

	**Here rather than in the CLI, for the reason ``expires_on`` is in the domain**: the
	grammar has to be the same grammar whichever surface took it, and this is where a project
	name is already turned into a project. It also keeps the resolution narrowed through
	:func:`subroutine.domain.scoping.readable_projects`, so a project the operator cannot see
	is not one they can scope a credential to.
	"""

	if wanted is None:
		return None

	asked = [item.strip() for item in wanted if item.strip()]

	if not asked:
		return []

	places = (
		[workspace]
		if workspace is not None
		else subroutine.domain.workspaces.readable(session, actor)
	)

	return [_sole_project(session, actor, places, item) for item in asked]


def _sole_project (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	places: typing.Sequence[subroutine.db.models.identity.Workspace],
	wanted: str,
) -> subroutine.db.models.project.Project:
	"""Find the one project a name refers to, refusing an unknown or an ambiguous one."""

	found: list[tuple[subroutine.db.models.identity.Workspace, typing.Any]] = []

	for place in places:
		try:
			found.append((place, project(session, actor, place, wanted)))

		except subroutine.errors.NotFound:
			continue

	if not found:
		raise subroutine.errors.NotFound(
			f"There is no project {wanted!r} here.",
			errors=[
				subroutine.errors.FieldError(
					field="project",
					code="not_found",
					message=f"No workspace you can read holds a project called {wanted!r}.",
					hint="Run 'subroutine project list' to see the keys, and check you are "
					"naming a project rather than a workspace.",
				)
			],
		)

	if len(found) > 1:
		where = ", ".join(sorted(place.slug for place, _found in found))

		raise subroutine.errors.ValidationError(
			f"More than one workspace here has a project called {wanted!r}: {where}.",
			errors=[
				subroutine.errors.FieldError(
					field="project",
					code="invalid_field_value",
					message=f"{wanted!r} names a project in each of: {where}.",
					hint="Say which workspace the credential is for — 'token create "
					"--workspace <name>'.",
				)
			],
		)

	single: subroutine.db.models.project.Project = found[0][1]

	return single
