"""Which workspace a request is about (SPEC.md §8.2).

Resolution order: what the caller asked for, then the workspace their token is pinned to,
then their only one. **Ambiguity is a refusal, never a guess** — with two workspaces and
nothing specified, picking one means a task filed somewhere the caller did not look, and
they find out days later.

The refusal lists the workspaces they can reach, with both id and slug, so the second
attempt is informed rather than another guess.
"""

import typing
import uuid

import sqlalchemy.orm

import subroutine.db.models.identity
import subroutine.domain.authentication
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
