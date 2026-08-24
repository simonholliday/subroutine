"""``GET /v1/agenda`` — "what am I doing today?" as one request.

The four buckets are §8.6's, and they are **disjoint by priority**: a task appears in
exactly one of them, so a client can render the whole thing without deduplicating and a
count means what it says. Overdue wins over today, today over upcoming, and anything with
no date at all falls to unscheduled.

Unlike ``GET /v1/tasks`` this spans every workspace the caller can read, because "what am I
doing today" is a question about a person's day rather than about a workspace — the dentist
appointment and the deployment are both in it (docs/design.md §13.7).
"""

import typing
import uuid

import fastapi
import sqlalchemy.orm

import subroutine.api.dependencies
import subroutine.api.routing
import subroutine.api.security
import subroutine.db.models.project
import subroutine.db.types
import subroutine.domain.agenda
import subroutine.domain.authentication
import subroutine.domain.instances
import subroutine.domain.schedule
import subroutine.domain.selection
import subroutine.domain.workspaces
import subroutine.errors
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1",
	tags=["agenda"],
	route_class=subroutine.api.routing.Transactional,
)


@router.get(
	"/agenda",
	summary="What am I doing today?",
)
def read (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	date: str | None = fastapi.Query(
		None,
		description=(
			"The day to build the agenda for — 2026-09-01, 'friday' or 'today'. Defaults to "
			"today, in your zone."
		),
	),
	timezone: str | None = fastapi.Query(
		None, description="Interpret the day in this zone, overriding your own."
	),
	horizon_days: int | None = fastapi.Query(
		None, ge=1, description="How far ahead 'upcoming' reaches."
	),
	unscheduled_limit: int = fastapi.Query(
		subroutine.domain.agenda.DEFAULT_UNSCHEDULED_LIMIT,
		ge=0,
		description="How many undated tasks to list. The total is always reported.",
	),
	workspace_id: str | None = fastapi.Query(
		None,
		description="Narrow to one workspace, by id or short name. Defaults to all of them.",
	),
	project: str | None = fastapi.Query(
		None,
		description=(
			"Narrow to one project and everything under it, by key or id. Needs a workspace, "
			"since a project belongs to one."
		),
	),
) -> subroutine.views.Agenda:
	"""Return today's work, in four disjoint buckets."""

	now = subroutine.db.types.utcnow()
	narrowing = _within(session, actor, workspace_id, project)
	zone = subroutine.domain.schedule.zone_for(
		user=actor.user,
		instance=subroutine.domain.instances.get(session),
		explicit=timezone,
	)

	built = subroutine.domain.agenda.build(
		session,
		principal=actor,
		workspace_ids=_scope(session, actor, workspace_id),
		now=now,
		timezone=zone,
		# **The word is read here rather than by whoever typed it** (`#1083`, decision
		# `#1088`). This used to be a `datetime.date`, so every client had to decide what
		# `today` meant before asking — the terminal against the laptop's zone, an agent
		# against the server's — while the answer coming back was bucketed in the account's.
		# So `subroutine agenda today` and a bare `subroutine agenda` could be about different
		# days. One is resolved in `zone` above, which is the same chain the buckets use.
		#
		# An ISO date still parses, so nothing a client sent before this reads differently.
		date=(
			None
			if date is None
			else subroutine.domain.schedule.interpret_written_day(
				date, timezone=zone, now=now, field="date"
			)
		),
		horizon_days=horizon_days,
		unscheduled_limit=unscheduled_limit,
		project=narrowing,
	)

	return subroutine.views.agenda(session, built)


def _within (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	workspace_id: str | None,
	wanted: str | None,
) -> subroutine.db.models.project.Project | None:
	"""Return the project this agenda is narrowed to, refusing one with nowhere to look.

	**A project needs a workspace and this says so by name** (`#1215`). Refs, keys and the
	vocabularies around them are per workspace (§5.4, §6.2), so ``?project=web`` on a request
	spanning every workspace a credential reaches is a question with more than one answer —
	and picking one would file the reader's whole agenda under whichever workspace happened to
	sort first.

	The refusal is shaped like ``/v1/tasks``'s for ``subtree`` without ``parent``: it says what
	the parameter means, why the other is needed, and both ways out.

	Resolved through :func:`subroutine.domain.selection.project` like every other listing, so a
	key works, case is not significant, and a project the caller cannot read reads as absent
	rather than forbidden (§7.3a).
	"""

	if wanted is None:
		return None

	if workspace_id is None:
		raise subroutine.errors.ValidationError(
			"'project' names a project inside one workspace, so it needs a workspace.",
			errors=[
				subroutine.errors.FieldError(
					field="project",
					code="invalid_field_value",
					message="'project' has no meaning without 'workspace_id'.",
					hint="Pass workspace_id=<slug> as well, or drop project.",
				)
			],
		)

	# **Cast, because `selection.project` is annotated `Any`** — it defaults to the Inbox and
	# predates the model being importable here. The annotation on this function is what the
	# domain is handed, so it is the one that has to be true.
	return typing.cast(
		subroutine.db.models.project.Project,
		subroutine.domain.selection.project(
			session,
			actor,
			subroutine.domain.selection.workspace(session, actor, requested=workspace_id),
			wanted,
		),
	)


def _scope (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	wanted: str | None,
) -> list[uuid.UUID]:
	"""Return the workspaces this agenda covers: one if asked for, otherwise all readable.

	**Spanning everything is the default and stays the default** — "what am I doing today" is a
	question about a person's day, and an agenda that silently covered one workspace would hide
	the dentist behind a work backlog.

	The filter exists because the reverse also bites: one instance holding a personal to-do list
	*and* a project's backlog put seven undated project tasks above "buy salad" the first time
	this project used itself. Every other listing already took ``workspace_id``; the agenda was
	the only one that did not, so this is a consistency repair as much as a feature.

	Resolved through ``selection.workspace`` like every other listing, so a short name works, a
	token's pin still applies, and a workspace the caller cannot read reads as absent rather than
	forbidden (§7.3a).
	"""

	if wanted is not None:
		return [subroutine.domain.selection.workspace(session, actor, requested=wanted).id]

	return [
		workspace.id for workspace in subroutine.domain.workspaces.readable(session, actor)
	]
