"""``GET /v1/agenda`` — "what am I doing today?" as one request.

The four buckets are §8.6's, and they are **disjoint by priority**: a task appears in
exactly one of them, so a client can render the whole thing without deduplicating and a
count means what it says. Overdue wins over today, today over upcoming, and anything with
no date at all falls to unscheduled.

Unlike ``GET /v1/tasks`` this spans every workspace the caller can read, because "what am I
doing today" is a question about a person's day rather than about a workspace — the dentist
appointment and the deployment are both in it (SPEC.md §13.7).
"""

import datetime
import uuid

import fastapi
import sqlalchemy.orm

import subroutine.api.dependencies
import subroutine.api.routing
import subroutine.api.security
import subroutine.db.types
import subroutine.domain.agenda
import subroutine.domain.authentication
import subroutine.domain.instances
import subroutine.domain.schedule
import subroutine.domain.selection
import subroutine.domain.workspaces
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
	date: datetime.date | None = fastapi.Query(
		None, description="The day to build the agenda for. Defaults to today, in your zone."
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
) -> subroutine.views.Agenda:
	"""Return today's work, in four disjoint buckets."""

	now = subroutine.db.types.utcnow()
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
		date=date,
		horizon_days=horizon_days,
		unscheduled_limit=unscheduled_limit,
	)

	return subroutine.views.agenda(session, built)


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
