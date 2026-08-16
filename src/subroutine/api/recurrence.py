"""``POST /v1/recurrence/parse`` — what a written repeat turns out to mean.

§6.7 reserved this and gave the reason: it **turns an ambiguous natural-language feature
into a checkable one**. A caller sends the words and gets back the stored rule, a canonical
description in different words from the ones typed, and the next few dates — so an agent can
confirm it was understood before committing, and a form can show a person the same thing
while they are still typing.

**Reading it back in the same words would confirm nothing.** The description is generated
from the rule rather than echoed from the input, which is what makes it evidence: *"every
other tuesday"* comes back as *"every other week, on Tuesday"*, and somebody who meant every
Tuesday of alternate weeks can see that is what they got.

**It writes nothing and needs no workspace.** The grammar is pure text processing (§6.13's
second rule, applied to repeats), so this is a calculator: no task, no row, and nothing to
undo if the answer was not what the caller wanted.
"""

import datetime

import fastapi
import pydantic

import subroutine.api.dependencies
import subroutine.api.routing
import subroutine.api.schemas
import subroutine.api.security
import subroutine.db.types
import subroutine.domain.instances
import subroutine.domain.recurrence
import subroutine.domain.schedule
import subroutine.domain.selection
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1",
	tags=["recurrence"],
	route_class=subroutine.api.routing.Transactional,
)

class Parse(subroutine.api.schemas.RequestModel):
	"""What ``POST /v1/recurrence/parse`` accepts."""

	#: The words, or an ``RRULE`` directly. Both are accepted for the reason ``due`` takes a
	#: date, a datetime and an expression: a caller holding a calendar's rule already should
	#: not have to translate it into English so that this can translate it back.
	text: str

	#: Where the dates are computed from. Defaults to now, which is what a form wants — it is
	#: asking *what would this mean*, and the answer for an unfiled task starts today.
	from_: datetime.datetime | None = pydantic.Field(default=None, alias="from")

	timezone: str | None = None


@router.post(
	"/recurrence/parse",
	summary="What does this repeat mean?",
)
def parse (
	body: Parse,
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
) -> subroutine.views.Reading:
	"""Read a written repeat without storing anything, and say what it means.

	**A refusal here is the same refusal a create would give**, because it is the same
	function — so a caller that checks first and then commits cannot be told two different
	things about one phrase, which is the divergence every two-implementations defect in this
	codebase has been.
	"""

	zone = body.timezone or subroutine.domain.schedule.zone_for(
		user=actor.user,
		workspace=subroutine.domain.selection.workspace(session, actor, requested=None),
		instance=subroutine.domain.instances.get(session),
	)

	read = subroutine.domain.recurrence.rule(body.text, field="text")
	start = body.from_ or subroutine.db.types.utcnow()

	return subroutine.views.Reading(
		rule=read.rule,
		description=subroutine.domain.recurrence.describe(read.rule),
		text=read.text,
		occurrences=subroutine.domain.recurrence.occurrences(
			read.rule, start=start, timezone=zone, limit=subroutine.domain.recurrence.AHEAD
		),
	)
