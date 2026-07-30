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

import fastapi

import subroutine.api.dependencies
import subroutine.api.query
import subroutine.api.security
import subroutine.db.types
import subroutine.domain.agenda
import subroutine.domain.instances
import subroutine.domain.schedule
import subroutine.domain.workspaces
import subroutine.views

router = fastapi.APIRouter(prefix="/v1", tags=["agenda"])


@router.get(
	"/agenda",
	summary="What am I doing today?",
	dependencies=[subroutine.api.query.UnknownQueryDep],
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
		# Every workspace the caller can read, narrowed by a pinned token exactly as
		# everything else is.
		workspace_ids=[
			workspace.id for workspace in subroutine.domain.workspaces.readable(session, actor)
		],
		now=now,
		timezone=zone,
		date=date,
		horizon_days=horizon_days,
		unscheduled_limit=unscheduled_limit,
	)

	return subroutine.views.agenda(session, built)
