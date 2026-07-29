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
import pydantic

import subroutine.api.dependencies
import subroutine.api.security
import subroutine.api.views
import subroutine.db.types
import subroutine.domain.agenda
import subroutine.domain.instances
import subroutine.domain.schedule
import subroutine.domain.workspaces

router = fastapi.APIRouter(prefix="/v1", tags=["agenda"])


class Agenda(pydantic.BaseModel):
	"""The four buckets, and what they were computed against."""

	#: The local date the agenda is for, and the zone that made it a date. Both reported
	#: because "today" is not a fact about the server (SPEC.md §6.5).
	date: datetime.date
	timezone: str

	overdue: list[subroutine.api.views.Task]
	today: list[subroutine.api.views.Task]
	upcoming: list[subroutine.api.views.Task]
	unscheduled: list[subroutine.api.views.Task]

	#: How many unscheduled tasks there are in total, which is usually more than are
	#: listed: an agenda that dumped a 400-item backlog would not be an agenda.
	unscheduled_total: int


@router.get("/agenda", summary="What am I doing today?")
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
) -> Agenda:
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

	everything = [*built.overdue, *built.today, *built.upcoming, *built.unscheduled]
	vocabulary = subroutine.api.views.Vocabulary.for_tasks(session, everything)

	return Agenda(
		date=built.date,
		timezone=built.timezone,
		overdue=[subroutine.api.views.task(row, vocabulary) for row in built.overdue],
		today=[subroutine.api.views.task(row, vocabulary) for row in built.today],
		upcoming=[subroutine.api.views.task(row, vocabulary) for row in built.upcoming],
		unscheduled=[subroutine.api.views.task(row, vocabulary) for row in built.unscheduled],
		unscheduled_total=built.unscheduled_total,
	)
