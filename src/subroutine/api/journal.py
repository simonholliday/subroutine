"""What happened over a period, joined — item `#1430`, decision `#1429`.

**Two reads of one store, and only this one joins.** ``/v1/changes`` answers *what changed*:
raw, cheap, complete, resumable from a cursor, and what a client polling for work should read.
This answers *what happened*, which is what a person or an agent is asked when somebody says
*run me through what we did on Friday* — and the difference is three joins the audit log
deliberately does not carry.

**Its own route rather than a flag on the feed**, which is Simon's decision of 2026-08-27 and
his reason is the one that matters: an agent has to be able to *discover* the distinction, and a
flag on an existing route is something you have to already know exists. They also want different
defaults — the feed reads forwards from a cursor at fifty rows a page, and these entries carry
whole comment bodies.

**Nothing here narrows differently.** The scoping, the watermark, both cursor refusals and the
period filter are all :mod:`subroutine.domain.events`' and :mod:`subroutine.domain.filtering`'s,
reached through the same functions the feed uses — so a journal cannot show an event the feed
would have withheld.
"""

import typing

import fastapi

import subroutine.api.changes
import subroutine.api.dependencies
import subroutine.api.filters
import subroutine.api.routing
import subroutine.api.security
import subroutine.api.shaping
import subroutine.domain.events
import subroutine.domain.paging
import subroutine.domain.scoping
import subroutine.domain.selection
import subroutine.domain.workspaces
import subroutine.views

router = fastapi.APIRouter(
	prefix="/v1/journal", tags=["journal"], route_class=subroutine.api.routing.Transactional
)

SELECTABLE = subroutine.api.shaping.selectable(subroutine.views.JournalEntry)

@router.get(
	"",
	summary="What happened over a period, with what was said",
	response_model=subroutine.views.Journal,
	name="read_journal",
)
def reading (
	actor: subroutine.api.security.PrincipalDep,
	session: subroutine.api.dependencies.SessionDep,
	settings: subroutine.api.dependencies.SettingsDep,
	dated: subroutine.api.filters.EventFilters,
	workspace_id: str | None = fastapi.Query(
		None, description="Narrow to one workspace, by id or slug. The default spans all of them."
	),
	actor_filter: str | None = fastapi.Query(
		None,
		alias="actor",
		description="'me' for what this credential itself did, or a username for everything "
		"that account did through any of its credentials. Omit for everybody.",
	),
	oldest: bool = fastapi.Query(
		False,
		description="Read forwards from the start of the period rather than back from its end.",
	),
	limit: int | None = fastapi.Query(None, description="How many entries to return."),
	format: str | None = subroutine.api.shaping.FORMAT_QUERY,
	fields: str | None = subroutine.api.shaping.FIELDS_QUERY,
) -> typing.Any:
	"""Return what happened, newest first, with who did it and what they said.

	**This is the change feed joined, not a second record of anything.** Every entry is one
	event; what is added is the comment's body, the actor's name, and the meaning of the values
	inside a change — three things the feed leaves as an id or omits, and three things nobody
	can reconstruct from it.

	**Ask for a period with `?created_at.gte=`**, in the same grammar every listing takes.
	Without one you get the most recent entries, which is what somebody arriving with no
	particular day in mind wants.

	**Newest first**, unlike the change feed. A feed is read forwards because it resumes; a
	journal is a report about a past stretch of time, and the recent end is the one somebody
	asking for it usually means. Pass `oldest=true` to read a period in the order it happened,
	which is what you want when writing it up.
	"""

	if workspace_id is None:
		workspaces = subroutine.domain.workspaces.readable(session, actor)

	else:
		workspaces = [
			subroutine.domain.selection.workspace(session, actor, requested=workspace_id)
		]

	workspace_ids = [workspace.id for workspace in workspaces]

	# **Resolved through the same selector every other "who" here uses** (`#1120`), so an
	# account that does not exist is refused by name and with the members listed.
	by = (
		None
		if actor_filter is None or actor_filter == subroutine.api.changes.ACTOR_ME
		else subroutine.domain.selection.user(session, actor_filter, caller=actor.user).id
	)

	# **The instance's page size, not one of this route's own.** An entry here can carry a
	# whole comment, so a smaller default is tempting — and `domain.paging.size` is the one
	# arbiter of a page size by decision, after two clients answered `limit=1000` with 250
	# rows and 200 against the same database. A second number would also make that function's
	# own refusal wrong, since its hint names `settings.default_page_size` by value.
	size = subroutine.domain.paging.size(limit, settings)
	rows, has_more = subroutine.domain.events.page(
		session,
		actor,
		workspace_ids=workspace_ids,
		size=size,
		mine=actor_filter == subroutine.api.changes.ACTOR_ME,
		by=by,
		# **`newest` is the default here and is the feed's exception**, which is the one place
		# these two routes disagree about the same underlying call. `events.page` still returns
		# the page reading forwards either way.
		newest=not oldest,
		narrowing=subroutine.api.filters.across(
			dated, session=session, actor=actor, workspace_ids=workspace_ids
		),
	)

	return subroutine.api.shaping.response(
		subroutine.views.journal_entries(session, rows),
		# **No cursor, exactly as the feed has none.** A journal is asked about a period and is
		# not resumable; a caller wanting the page before this one narrows the period.
		subroutine.views.Page(limit=size, has_more=has_more, total=None),
		subroutine.api.shaping.wanted(
			format=format,
			fields=fields,
			available=SELECTABLE,
			entity="journal entry",
			timezone=subroutine.views.reader_zone(session, actor),
		),
		# **Said on every answer, narrowed or not** (`#1085`), for the reason the feed says it:
		# without it, *nothing happened on Friday* and *I am not shown that* are one sentence.
		covers=subroutine.domain.scoping.readable_event_kinds(actor),
	)
