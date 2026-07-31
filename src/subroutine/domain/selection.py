"""Which workspace — and which project — a request is about (SPEC.md §8.2, §5.2).

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

import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.domain.authentication
import subroutine.domain.bootstrap
import subroutine.domain.projects
import subroutine.domain.scoping
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
			hint="Pass 'workspace_id', or use a token pinned to one workspace.",
		)

	return reachable[0]


def _alternatives (
	reachable: typing.Sequence[subroutine.db.models.identity.Workspace],
) -> str:
	"""Describe the workspaces the caller can reach, by both names."""

	if not reachable:
		return "You are not a member of any workspace."

	listed = ", ".join(f"{candidate.slug} ({candidate.id})" for candidate in reachable)

	return f"Workspaces you can reach: {listed}."


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
		inbox = subroutine.domain.bootstrap.inbox_for(session, workspace)

		if inbox is None:
			raise subroutine.errors.InternalError(
				"This workspace has no Inbox to file a task in.",
				hint="It was interrupted part-way through setup; run 'subroutine init' again.",
			)

		return inbox

	model = subroutine.db.models.project.Project
	statement = subroutine.domain.scoping.readable_projects(
		actor, workspace_ids=[workspace.id], include_archived=True
	)

	# A key and an id are told apart by whether the text parses as one, rather than by a
	# flag: §5.2 makes a key start with a letter, so the two spaces cannot overlap.
	try:
		found = session.scalars(statement.where(model.id == uuid.UUID(wanted.strip()))).first()

	except ValueError:
		found = session.scalars(
			statement.where(model.key == subroutine.domain.projects.normalize_key(wanted))
		).first()

	if found is None:
		raise subroutine.errors.NotFound(
			f"There is no project {wanted!r} here.",
			errors=[
				subroutine.errors.FieldError(
					field="project",
					code="not_found",
					message=f"No project in {workspace.slug} answers to {wanted!r}.",
					hint="Use a project key like 'SR' or a project id. GET /v1/projects lists "
					"what you can see.",
				)
			],
		)

	return found
