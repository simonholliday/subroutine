"""What am I doing today — the one question a personal to-do list has to answer well.

Named buckets rather than a flat list, because a person's day has structure and
because a flat list loses the most common kind of personal task (docs/design.md §8.6).

**:data:`BUCKETS` is the list, in priority order, and it is the only one** (`#1244`). It
decides how a day reads *and* which bucket claims a row that qualifies for two, because
:func:`build` walks it and each bucket subtracts what the ones before it took. This paragraph
used to name five of them, which was a fourth copy and had been wrong since two more were
added.

**The ``unscheduled`` bucket is what makes quick capture worth having.** Most personal
tasks are captured with no date, and without a bucket for them they would never appear in
the agenda at any point, ever — a to-do list you can write to and not read from. §13.5b
tests for it directly.

**``upcoming`` is off unless asked for, and the CLI always asks.** The API keeps it behind
``include=upcoming`` so that a client can reason about the window it gets; the CLI renders
a seven-day look-ahead by default because a to-do list that shows nothing when something is
due on Friday is one nobody keeps using. The two are not in tension — one is a transport,
the other is a product — but they were in outright contradiction until a review caught it,
so the reason is written down in §8.6 and again here.
"""

import dataclasses
import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.project
import subroutine.db.models.vocabulary
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.dates
import subroutine.domain.ordering
import subroutine.domain.readiness
import subroutine.domain.schedule
import subroutine.domain.scoping

#: How many undated tasks the agenda shows before it stops. A person with two hundred
#: captured-and-forgotten tasks does not want all of them every morning; they want the
#: reminder that the pile exists, which is what :attr:`Agenda.unscheduled_total` is for.
DEFAULT_UNSCHEDULED_LIMIT = 20

#: How many blocked-by-somebody-else rows the agenda shows before it stops (`#1285`, decision
#: `#1267` §3b). Fewer than :data:`DEFAULT_UNSCHEDULED_LIMIT`, and the reason is what the two
#: sections are for: ``unscheduled`` is the pile you pick your next job out of, where **nothing
#: here can be started at all**. It is context and a prompt to chase somebody, so it earns
#: enough lines to chase from and not a wall. :attr:`Agenda.blocked_by_others_total` says how
#: many more there are, which is the condition Simon set on any cap.
#:
#: **Not a request parameter, unlike ``unscheduled_limit``**, deliberately: no client has asked
#: to move it, and an argument nothing passes is a control that grants nothing (`#303`). The
#: count is on every surface and ``subroutine list`` shows every row, so *a way to see it all*
#: is already answered without one. :func:`build` still takes it, which is what the tests use.
DEFAULT_BLOCKED_LIMIT = 5

#: The CLI's default look-ahead. Not the API's — it has none (docs/design.md §8.6).
DEFAULT_HORIZON_DAYS = 7

#: What separates two rows a bucket's own keys cannot tell apart: oldest first, always
#: ascending. Simon's decision of 2026-08-13 — age is *"one of the least significant ordering
#: fields, maybe the last"* and not a signal, so it says nothing beyond *these are not the same
#: row*. Named once because two readers need it and a second spelling is a page boundary that
#: lands where the next page does not start.
TIEBREAK = "created_at"

#: The status key that says a piece of work is waiting on a person (`#1116`).
#:
#: **A key rather than a category, which is the one place this file reads one.** `#96` refused
#: a fifth status category on the grounds that the distinction that matters is *who ends the
#: wait* — a `blocks` link resolves itself, where this needs somebody to answer — so there is no
#: category to ask for and the seeded key is what there is. A workspace that renames it has
#: renamed the thing this bucket is about, and the bucket is then empty rather than wrong.
#:
#: It has been seeded since M1, published in `/v1/meta`, settable through every client,
#: filterable and rendered by the board, and **used zero times in 925 tasks** — because nothing
#: ever put it in front of the person who could answer.
WAITING_STATUS = "needs_input"

#: How each bucket is ordered, in ``?order=``'s own grammar.
#:
#: **Declared rather than written out in SQL, because a second reader re-sorts these in
#: Python** (`#993`). ``subroutine agenda`` asks one connection per place and merges the
#: answers, so the arrangement has to be reapplied after the merge — and it was reapplied on
#: *different keys*: the ref where the server breaks ties on :data:`TIEBREAK`, and nothing at
#: all where the server reads ``starts_at``. Refs are allocated per workspace, so those agreed
#: for exactly as long as an agenda was dominated by one.
#:
#: This is `#71`'s shape, which ``domain/ordering.py``'s own docstring records: an ordering
#: chosen by the server and discarded one level up, where **the output looks entirely
#: reasonable**. One declaration is what makes that impossible rather than unlikely.
ORDERS: dict[str, tuple[str, ...]] = {
	# **Oldest first**, which is what the tiebreak already does and is the right key here for
	# once: a question that has been waiting three days is more overdue than one asked this
	# morning, and there is nothing else about it to rank by — it is not the asker's to
	# prioritise, and whoever has to answer wants the one they have kept waiting longest.
	"waiting": (),
	# Soonest first, because that is the order the days arrive in.
	"overdue": ("due_at",),
	# **``starts_at``, because an occasion has no deadline to sort by** — decision `#1235`, and
	# it is the whole of what makes one an occasion. A fortnight that began last week sits above
	# a birthday today, which is the order the days arrive in read honestly: the one already
	# under way started first.
	"occasions": ("starts_at",),
	"today": ("due_at",),
	# **Ranked, because it is capped** (`#1285`, decision `#1267` §3b). Simon's qualifier was
	# *"if those items would ordinarily be urgent/important enough to be included"*, and a bar
	# read off `priority_score` directly cannot be honest — the score is null unless both axes
	# are set, so most of a backlog would fall under any threshold silently. Ordering by rank
	# and capping is the same intent implemented in a way that says what it left out.
	"blocked_by_others": ("-priority_score",),
	# **Ranked, which is the same rule ``?order=-priority_score`` applies** (`#853`), so the
	# agenda and a ranked listing cannot disagree about which item is the one to start.
	"in_progress": ("-priority_score",),
	# **``starts_at`` second, and it is the key the client used to drop.** An appointment next
	# Tuesday carrying no deadline is ordered by when it begins; without this it fell to the
	# tiebreak and sorted by age, which reads as an arbitrary arrangement of things that have
	# a very obvious one.
	"upcoming": ("due_at", "starts_at"),
	"unscheduled": ("-priority_score",),
}

#: The agenda's buckets, in the order a day is read (docs/design.md §8.6).
#:
#: **One list, and it decides two things at once — which is the whole of `#1244`.** It is the
#: order the sections are *shown* in on all three surfaces (`#992`), and it is the order
#: :func:`build` *computes* them in, which is what makes them disjoint: every bucket subtracts
#: what the ones before it took, so a row qualifying for two belongs to whichever comes first.
#:
#: **Those were two declarations until 2026-08-25 and nothing had ever compared them.** They
#: agreed for as long as nobody moved one. Moving `in_progress` to the front of the displayed
#: order alone produced a page whose headings promised *In progress* above *Overdue* while the
#: membership still gave a started, overdue task to *Overdue* — so the item appeared under a
#: heading further down than the reader had been told to look. The suite stayed green, because
#: every guard read the displayed list and nothing read the other one.
#:
#: **:data:`subroutine.views.AGENDA_BUCKETS` is this tuple**, not a copy of it. It is aliased
#: there because the surfaces reach for it by that name and the domain cannot import the
#: views, which import it.
BUCKETS: tuple[str, ...] = (
	# **First, and it is Simon's decision of 2026-08-25** (`#1243`): *"I would naturally
	# complete a task before starting another."* Work already in hand is the first thing to
	# look at, because everything below it is a candidate to *begin* and this is the only
	# section that is not.
	#
	# **It outranks `overdue` as well, and that is the part with a consequence.** The buckets
	# are disjoint in order, so a started task with a passed deadline is reported here rather
	# than under *Overdue* — which is right (you are already on it) and which means the late
	# marking cannot come from the section. Both surfaces mark the row instead; the browser
	# always did.
	"in_progress",
	# **Before `overdue`, and that is the whole of the decision** (`#1116`). A task that is
	# both overdue and waiting on an answer belongs here: *you owe an answer* is the more
	# actionable truth than *this is late*, because the lateness is a consequence of the
	# question and nobody can act on the task until it is answered. Every other bucket is work
	# the reader could pick up; this one is work they are holding up.
	"waiting",
	# **Directly under `waiting`, and the pair is what makes both legible** (`#1285`, decision
	# `#1267` §3): *Waiting on you* is a question somebody parked for you, and *Waiting on
	# somebody else* is your work held up by their item.
	#
	# **Above `overdue`, and that is the part with a consequence.** A blocked task whose
	# deadline has passed is reported here rather than as late — *you are late* is not the
	# useful sentence about work you cannot start, because chasing the other person is the only
	# move available and this is the section that says so. Same reasoning as `#1116` for
	# `waiting` and `#1243` for `in_progress`.
	"blocked_by_others",
	"overdue",
	# **Above the day's own work, and below what is late** (decision `#1235` §4). Everything
	# around it is work; this is what is happening *to* the reader, and a code freeze or a
	# fortnight off is the context the rest of the page is read in — so it goes before *Today*
	# and after the things that are already owed.
	#
	# **Which also takes its rows before `today` can**, since one list decides both. That is
	# not a coincidence to be maintained; it is why there is one list.
	"occasions",
	"today",
	"upcoming",
	"unscheduled",
)


def order_for (bucket: str) -> tuple[tuple[str, bool], ...]:
	"""Return one bucket's ordering as ``(field, descending)`` pairs, tiebreak included.

	For a caller sorting rows it has already been given — :func:`subroutine.domain.ordering.
	merged` is the other half. The SQL side of the same declaration is :func:`_ordered`, and
	the two appending the same tiebreak is the whole reason it has a name.
	"""

	keys = subroutine.domain.ordering.requested(
		None, allowed=subroutine.domain.ordering.TASK_FIELDS, default=ORDERS[bucket]
	)

	return (*keys, (TIEBREAK, False))


@dataclasses.dataclass(frozen=True)
class Agenda:
	"""One day's work, in buckets, with enough context to render it honestly."""

	date: datetime.date
	timezone: str

	#: What is waiting on a person — status ``needs_input`` (`#1116`). **First**, because it is
	#: the only bucket that is not work the reader could do: it is work somebody else cannot do
	#: until they answer, so leaving it below the day's own list buries the one thing that
	#: unblocks anybody else.
	waiting: tuple[subroutine.db.models.work.Task, ...]

	overdue: tuple[subroutine.db.models.work.Task, ...]
	today: tuple[subroutine.db.models.work.Task, ...]
	upcoming: tuple[subroutine.db.models.work.Task, ...]
	unscheduled: tuple[subroutine.db.models.work.Task, ...]

	#: What is already started — status category ``in_progress`` (`#853`). Between *today* and
	#: the rest, because work somebody is in the middle of is neither scheduled nor a candidate
	#: to pick up, and an agenda that could not say so left an agent unable to see its own
	#: half-finished work (`#841`).
	in_progress: tuple[subroutine.db.models.work.Task, ...] = ()

	#: What is happening to you today rather than being done by you — the ``occasion`` type
	#: category (decision `#1235` §4). A birthday, a booked fortnight, a street closed by the
	#: council, a code freeze.
	#:
	#: **Its own section rather than the ``today`` bucket**, because *today* answers *what can I
	#: pick up* and an event is not an answer to it. The measured defect: a birthday planned to a
	#: date that has passed sat in Today every day for ever, was offered by ``--ready``, and the
	#: agenda's own tip read ``subroutine done 2``.
	#:
	#: **Membership is overlap with the day being shown**, so it leaves by itself the morning
	#: after with nobody acting — which is `#1235` §3's *derived, never written*.
	occasions: tuple[subroutine.db.models.work.Task, ...] = ()

	#: Work of yours that an item somebody else is assigned to is holding up (`#1285`, decision
	#: `#1267` §3). The other kind of waiting: :attr:`waiting` is a question parked for you,
	#: this is your work held up by somebody's else's row.
	#:
	#: **Narrow on purpose** — a blocker with no assignee does not count, and neither does one
	#: of your own. *Blocked by anything* floods a solo instance, whose blockers are its own
	#: work, which is `#96`'s argument and still holds there.
	#:
	#: **Capped, and :attr:`blocked_by_others_total` says by how much.** Nothing in it can be
	#: started, so it is context rather than the day's work.
	blocked_by_others: tuple[subroutine.db.models.work.Task, ...] = ()

	#: How many undated tasks there are in total, which is usually more than were returned.
	#: Carried so a client can say "and 14 more" rather than implying the list is complete.
	unscheduled_total: int = 0

	#: How much work somebody else is holding up in total, which may be more than
	#: :attr:`blocked_by_others` lists. **A cap must say it is one** — Simon's condition on
	#: `unscheduled_total`, and the reason `passed_total` exists.
	blocked_by_others_total: int = 0

	#: How much work this agenda holds back because somebody deferred it — `#1215`, Simon's
	#: decision of 2026-08-24 amending `#649`.
	#:
	#: **The exclusion existed from the start and said nothing**, which was harmless while the
	#: agenda lived at one address with nothing to compare it against. Beside `?view=list` on
	#: the *same* address it is a gap a reader can see and cannot explain: measured on this
	#: project, 136 rows in the list against 126 the agenda accounts for.
	#:
	#: **Counted before the defer is applied**, so it and the buckets partition the scope rather
	#: than overlapping — a deferred row never reaches the bucketing at all.
	deferred_total: int = 0

	#: How many occasions this agenda leaves out because they have already happened — decision
	#: `#1235` §3, and the count `tests/test_agenda.py`'s arithmetic demanded the moment there
	#: was a fifth way to be left out.
	#:
	#: **A listing still shows them and this view does not**, which is exactly the unexplained
	#: difference `#649`'s amendment forbids: a passed event is not *completed*, so nothing
	#: hides it from ``?view=list``, and the agenda drops it because a day that went by is not
	#: part of today. Saying how many is what makes that a decision rather than a gap.
	passed_total: int = 0

	#: How much undated work is in a project nobody is running — `#983`, reported since `#1215`.
	#:
	#: **Counted after the defer and after the buckets have taken theirs**, mirroring `undated`
	#: exactly with its running-project clause negated. Otherwise a row that is both deferred and
	#: in a paused project would be counted twice, and the sum this exists to make true would
	#: stop being true.
	paused_total: int = 0

	#: How many *dated* tasks this agenda does not show — further out than the look-ahead, or
	#: past today where no look-ahead was asked for (`#997`). The same job
	#: :attr:`unscheduled_total` does for the other pile: the window has an edge on every
	#: surface, and until this existed nothing said so, so a deadline three weeks away was
	#: absent from the view whose whole job is *what is coming* with no sign it had been left
	#: out.
	later_total: int = 0

	@property
	def is_empty (self) -> bool:
		"""Report whether there is nothing at all to show.

		Walked from :data:`BUCKETS` rather than listed, so a bucket added tomorrow counts here
		without anybody remembering to add it — this had been a written-out list of seven and
		was a third place the set of buckets was declared (`#1244`).
		"""

		return not any(getattr(self, bucket) for bucket in BUCKETS)


def build (
	session: sqlalchemy.orm.Session,
	*,
	principal: subroutine.domain.authentication.Principal,
	workspace_ids: typing.Sequence[uuid.UUID],
	now: datetime.datetime,
	timezone: str,
	date: datetime.date | None = None,
	horizon_days: int | None = None,
	unscheduled_limit: int = DEFAULT_UNSCHEDULED_LIMIT,
	blocked_limit: int = DEFAULT_BLOCKED_LIMIT,
	project: subroutine.db.models.project.Project | None = None,
) -> Agenda:
	"""Return the agenda for one day, in the caller's timezone.

	``project`` narrows to one area of work — that project **and everything under it**, which
	is what a named project means everywhere else here (`#320`). It belongs to exactly one
	workspace, so a caller passing it has already narrowed ``workspace_ids`` to that one.

	``horizon_days`` of ``None`` omits the ``upcoming`` bucket entirely, which is the API's
	default; the CLI passes :data:`DEFAULT_HORIZON_DAYS`.

	**The buckets are disjoint**, in the order :data:`BUCKETS` declares. A task that is both
	overdue and planned for today belongs in ``overdue`` — it is the more urgent truth about
	it, and showing one task twice in a five-line summary makes the summary useless.
	"""

	day = date or subroutine.domain.schedule.local_date(now, timezone)
	model = subroutine.db.models.work.Task

	day_start = _boundary(day, timezone, end=False)
	day_end = _boundary(day, timezone, end=True)

	# **Resolved once, before anything is built** (`#986`, decision `#982`). One project per
	# workspace may be prioritised, and its subtree's ranked work rises inside its band. The
	# paths are looked up here and passed into the ordering as literals, which is the cheaper
	# spelling — measured, and the reason it is *not* `#856` is written beside the term. A merged
	# agenda spans workspaces, so this is a set.
	#
	# **Nothing prioritised is the ordinary case and returns the module's own vocabulary**, so
	# an instance that has never used this pays not one extra clause.
	sortable = subroutine.domain.ordering.prioritising(
		subroutine.domain.ordering.TASK_FIELDS,
		prefixes=subroutine.domain.scoping.prioritised_paths(
			session, principal, workspace_ids=workspace_ids
		),
	)

	base = _visible(
		session, principal, workspace_ids, until=day_end, sortable=sortable, project=project
	)

	# **The look-ahead, resolved before the buckets so that `upcoming` is a predicate like the
	# rest of them.** ``None`` means no window was asked for, which is the API's default, and
	# that bucket is then empty rather than absent — the section still exists, it holds nothing.
	horizon = (
		None
		if horizon_days is None
		else _boundary(day + datetime.timedelta(days=horizon_days), timezone, end=True)
	)

	# **A project that is not running keeps its dated work on the agenda and loses this
	# bucket** (`#983`). Putting a project down says something about *what to work on*, and
	# this is the bucket that answers that question — where Overdue, Today and Upcoming answer
	# *what is due*, which `#857` settled is a different question that the priority rank is
	# kept out of for the same reason.
	#
	# **The conservative half of the choice is deliberate.** OmniFocus and Things both drop
	# dated items from a paused project too; the cost there is that a deadline can pass in
	# silence because somebody put a project down months earlier, and a deadline is usually a
	# commitment to somebody else that pausing your own work does not cancel. Nothing dated
	# disappears here, so the failure mode is a row you have to ignore rather than one you
	# never see.
	undated = base.where(
		model.starts_at.is_(None),
		model.due_at.is_(None),
		subroutine.domain.readiness.in_a_running_project(model),
	)

	# **What each bucket is about — and deliberately not what order they come in** (`#1244`).
	# This is a mapping keyed by bucket, so the sequence it happens to be written in decides
	# nothing at all; the loop below reads :data:`BUCKETS`, which is the one place the order
	# exists. Every bucket keeps its own predicate, because they answer genuinely different
	# questions — a status category, a status key, a pair of dates — and only the sequencing
	# is shared.
	membership: dict[
		str, sqlalchemy.Select[tuple[subroutine.db.models.work.Task]] | None
	] = {
		# **Read off the status *category* rather than a key, because a workspace may rename
		# the row** (`#853`) — `in_progress` is one of the five categories §6.5 fixes, and the
		# key beside it is not.
		#
		# **Deliberately unlimited, unlike `unscheduled`** — Simon's decision of 2026-08-14,
		# `#888`: *"a user viewing their own agenda should see all in-progress items. Hiding
		# some risks misleading the user. They may start others instead of finishing items we
		# didn't show them."*
		#
		# **Measured before deciding**, because the cold review raised it as unbounded and the
		# word is doing a lot of work: 2 in-progress against 179 unscheduled on the served
		# instance. The argument is what the two are bounded *by* rather than the numbers —
		# `unscheduled` grows with the backlog and has no ceiling at all, where this is bounded
		# by how many workers there are times how much each holds at once, which §14.11's
		# leases keep small on purpose.
		#
		# **What would change it is team size**, since every bucket here is scoped by
		# readability rather than by assignee. If it ever does, the shape is already beside it:
		# Simon's condition was that a cap must *say* it is one, count what is hidden and offer
		# a way to see it all, which is exactly what `unscheduled_total` is.
		"in_progress": base.join(
			subroutine.db.models.vocabulary.Status,
			subroutine.db.models.vocabulary.Status.id == model.status_id,
		).where(subroutine.db.models.vocabulary.Status.category == "in_progress"),
		# **Read by key, which nothing else here does.** `WAITING_STATUS` carries why: `#96`
		# refused a fifth status category, so there is none to ask for.
		"waiting": base.join(
			subroutine.db.models.vocabulary.Status,
			subroutine.db.models.vocabulary.Status.id == model.status_id,
		).where(subroutine.db.models.vocabulary.Status.key == WAITING_STATUS),
		# **The other kind of waiting, and the narrow reading of it** (`#1285`, decision
		# `#1267` §3a): a live blocker that somebody who is not the caller is assigned to.
		# The predicate is `unblocked`'s edges with one more join, and the reasoning for
		# every clause in it — including why an unassigned blocker does not count — is on
		# `readiness.blocked_by_somebody_else`.
		#
		# **`principal.user` is the person asking.** A principal always has one, so there is
		# no branch here; an agent's credential and its operator's are two principals with
		# two accounts (`#335`), which is what makes an agent's *waiting on somebody else*
		# mean the agent's own work rather than Simon's.
		"blocked_by_others": base.where(
			subroutine.domain.readiness.blocked_by_somebody_else(
				model, now=now, user_id=principal.user.id
			)
		),
		# **Uncapped, and bounded by nothing — which is not the reason `#888` gave** (`#927`
		# M-18, Simon's decision of 2026-08-17). That item declined a cap on `in_progress` and
		# said in passing that *"`overdue` and `today` are unlimited too and are naturally
		# bounded by dates"*. Dates do not bound this: a deadline that has passed goes on
		# having passed, so this bucket grows with however much you are late on and has no
		# ceiling at all — `in_progress`'s bound, workers times leases, does not apply here.
		#
		# **It stays uncapped anyway, on `#888`'s other argument**, which is the one that
		# carries: hiding work misleads the reader into starting something else, and that is
		# worse for late work than for anything on the page. Measured before deciding: 2
		# overdue, 1 today, on the instance this project runs on.
		#
		# **What would change it, and what the change must look like.** A backlog large enough
		# that a day's agenda is unreadable — every row here renders through `views.task`, and
		# this is also MCP's `subroutine_list(today=true)`, where §13's context economy is a
		# first-order cost. If it ever comes to that, `#888` already fixed the shape: a cap
		# must *say* it is one, count what is hidden and offer a way to see it all, which is
		# exactly what `unscheduled_total` is. Do not add a bare `.limit()`.
		"overdue": base.where(model.due_at.is_not(None), model.due_at < day_start),
		# **Overlap with the day, which is what makes a passed event leave on its own**
		# (decision `#1235` §4). An occasion is here when it has begun by tonight and is not
		# over before this morning; its end is `ends_at` where there is one and its start
		# otherwise. An all-day start is the first instant of its day and an all-day end the
		# last (§6.5), so a birthday is current for exactly its own day and a fortnight for
		# exactly its fifteen.
		#
		# **Nothing is written and no scheduler runs** — `#1235` §3, which is `#915` §1's
		# argument reapplied: a timer that only fires when the program happens to be up is
		# worse than none, because it teaches somebody to trust it.
		"occasions": base.where(
			subroutine.domain.readiness.is_occasion(model),
			model.starts_at.is_not(None),
			model.starts_at <= day_end,
			sqlalchemy.func.coalesce(model.ends_at, model.starts_at) >= day_start,
		),
		"today": base.where(
			# **And it is not an occasion** (decision `#1235` §4). Without this the defect the
			# section was built to fix survives it: `starts_at <= day_end` keeps a past start
			# in today's bucket deliberately — right for work you meant to begin — and a
			# birthday in March is then in Today in August, every day, for ever. The bucket
			# above takes the ones that are actually happening; this clause is what stops the
			# rest coming back through the door beside it.
			sqlalchemy.not_(subroutine.domain.readiness.is_occasion(model)),
			sqlalchemy.or_(
				# **Compared against the end of the day, not against the day** (`#854`).
				# This used to read `planned_for <= day`, a `DATE` against a `date`; the
				# column is an instant now, so the boundary has to be one too or every
				# comparison is an instant against midnight in whichever zone the driver
				# guessed. Everything that has begun by tonight belongs to today, which is
				# what `<=` said before and still says.
				#
				# **So a start date in the past stays in today's bucket, and that is
				# deliberate** (`#927` M-18, Simon's decision of 2026-08-17). It is the
				# `starts_at` analogue of `overdue` above: work you meant to begin and did
				# not is work for today, every day, until you do it or move it.
				#
				# **Narrowing this to starts falling *within* today would lose the task
				# entirely**, which is why the obvious fix is the wrong one — `undated`
				# above is `starts_at IS NULL AND due_at IS NULL`, so a task with a start
				# and no deadline is in no other bucket at all. The agenda would stop
				# mentioning it, in silence, and `list` would become the only place it
				# appears — which is a worse answer than showing it every day.
				model.starts_at <= day_end,
				sqlalchemy.and_(model.due_at >= day_start, model.due_at <= day_end),
			)
		),
		"upcoming": None if horizon is None else base.where(
			sqlalchemy.or_(
				sqlalchemy.and_(model.due_at > day_end, model.due_at <= horizon),
				sqlalchemy.and_(model.starts_at > day_end, model.starts_at <= horizon),
			)
		),
		"unscheduled": undated,
	}

	# **Which buckets are capped, and by what.** Two are, and the arguments for leaving the
	# others alone are written beside them above. Every entry here gets a total beside it,
	# because a cap must say it is one, count what is hidden and offer a way to see it all.
	caps = {"unscheduled": unscheduled_limit, "blocked_by_others": blocked_limit}

	# **The buckets are disjoint in the order :data:`BUCKETS` declares, and that is now the
	# only order there is** (`#1244`). Each one subtracts what its predecessors took, so a row
	# qualifying for two belongs to whichever comes first — and the reader is promised exactly
	# that arrangement, because the headings are walked from the same tuple.
	#
	# **Subtracted in the query rather than in Python**, which the buckets above `unscheduled`
	# used to do. A cap applied before the subtraction returns fewer rows than it claims, so
	# the one capped bucket has always had to do it this way; doing it once, for all of them,
	# is what lets the loop treat every bucket alike.
	#
	# **`seen` is therefore everything the page shows**, and there is no second name for it.
	# `upcoming` used to be left out of it and unioned back in below, because it was computed
	# after the subtraction rather than as part of it.
	#
	# **A cap is a display choice and never a membership one, which is the part that was
	# wrong the first time.** A bucket owns every row its predicate claims; the cap decides
	# how many are drawn. So the full set joins `seen` and the slice happens afterwards —
	# otherwise a row `blocked_by_others` hid would fall through into `unscheduled` and be
	# offered under *Next* as something to pick up, which is the one thing it is not. The
	# accounting guard in `tests/test_agenda.py` is what found that, by counting it twice.
	#
	# **The last bucket is the exception and it is derived rather than named.** Nothing
	# follows it, so what its cap hides has nowhere to fall — and it is `unscheduled`, whose
	# set is the whole backlog. Loading every row of that in order to throw all but twenty
	# away is the cost the cap exists to avoid, so there alone the limit goes into the query
	# and a `COUNT` says what was left out.
	rows: dict[str, tuple[subroutine.db.models.work.Task, ...]] = {}
	totals: dict[str, int] = {}
	seen: set[uuid.UUID] = set()
	last = BUCKETS[-1]

	for bucket in BUCKETS:
		statement = membership[bucket]

		if statement is None:
			rows[bucket] = ()
			continue

		if seen:
			statement = statement.where(model.id.not_in(seen))

		cap = caps.get(bucket)

		if cap is not None and bucket == last:
			found = _run(session, statement.limit(cap), bucket, sortable)
			totals[bucket] = session.scalar(
				sqlalchemy.select(sqlalchemy.func.count()).select_from(statement.subquery())
			) or 0
		else:
			found = _run(session, statement, bucket, sortable)

			if cap is not None:
				totals[bucket] = len(found)

		seen.update(task.id for task in found)
		rows[bucket] = found if cap is None else found[:cap]


	# **How much dated work this agenda does not show** (`#997`). The window has an edge and
	# every surface has the same edge, so a deadline three weeks out is in **no bucket at
	# all** — `unscheduled` requires both dates to be null, so dated work leaves it and there
	# is nowhere else to go. It reappears seven days before it is due.
	#
	# **Simon's decision of 2026-08-18 is that the edge stays and gets said**: the agenda is a
	# day view (§8.6) and a listing already answers *what is due this quarter*, so the defect
	# was never the edge — it was that nothing told a reader one existed. `unscheduled_total`
	# is the worked precedent for *there is more, here is how much*.
	#
	# **Defined as "dated and not shown" rather than "past the horizon"**, which is the same
	# thing when a horizon was asked for and is still right when one was not: `GET /v1/agenda`
	# omits `upcoming` unless asked, so on that call *everything* dated beyond today is unshown
	# and this counts all of it. A predicate written against `horizon` would have reported zero
	# there, which is the answer that looks like good news.
	later = base.where(
		sqlalchemy.or_(model.starts_at.is_not(None), model.due_at.is_not(None)),
		# **Behind you is not further out** (decision `#1235` §3). An occasion that has gone by
		# is dated and unshown, so without this it would be counted here and the terminal would
		# say *and 3 dated further out* about three birthdays in March. The two counts partition
		# the dated-and-unshown rows rather than overlapping, which is what keeps the
		# arithmetic below meaning anything.
		sqlalchemy.not_(subroutine.domain.readiness.passed(model, now=now)),
	)

	if seen:
		later = later.where(model.id.not_in(seen))

	beyond = session.scalar(
		sqlalchemy.select(sqlalchemy.func.count()).select_from(later.subquery())
	)

	# **The fifth thing a day leaves out, and the first that nobody chose** (decision `#1235`
	# §3). A defer and a paused project are decisions; a cap and a window are edges; this is
	# simply a day that went by. It is reported for `#649`'s reason all the same — a listing at
	# the same scope still shows these rows, because a passed event is not *completed* and
	# nothing hides it there.
	gone = base.where(subroutine.domain.readiness.passed(model, now=now))

	if seen:
		gone = gone.where(model.id.not_in(seen))

	# **What the day holds back, counted so the page can account for itself** (`#1215`).
	#
	# The two counts above report a *cap* and a *window edge* — things the reader did not choose
	# to hide. These two report things they did: a defer is somebody saying *not until Tuesday*,
	# and a paused project is somebody putting work down. Simon's decision of 2026-08-24 is that
	# all four are said anyway, on one line, because the agenda now sits beside `?view=list` at
	# the same address and an unexplained difference between them is what `#649` exists to
	# prevent. The objection — that this tells a reader daily about their own decisions — is on
	# the item with the measurement that prompted it.
	#
	# **They partition, and that is load-bearing rather than tidy.** `deferred` is counted on the
	# scope *before* `_visible` applies the defer, and `paused` on the rows that survived it and
	# were not taken by a bucket. A guard adds the four to the agenda's own rows and compares
	# against the listing at the same scope, so a fifth exclusion added later cannot be silent.
	held = _deferred(
		session, principal, workspace_ids, until=day_end, sortable=sortable, project=project
	)
	put_down = base.where(
		model.starts_at.is_(None),
		model.due_at.is_(None),
		sqlalchemy.not_(subroutine.domain.readiness.in_a_running_project(model)),
		model.id.not_in(seen),
	)

	return Agenda(
		date=day,
		timezone=timezone,
		waiting=rows["waiting"],
		overdue=rows["overdue"],
		occasions=rows["occasions"],
		today=rows["today"],
		in_progress=rows["in_progress"],
		upcoming=rows["upcoming"],
		unscheduled=rows["unscheduled"],
		blocked_by_others=rows["blocked_by_others"],
		unscheduled_total=totals["unscheduled"],
		blocked_by_others_total=totals["blocked_by_others"],
		later_total=beyond or 0,
		deferred_total=session.scalar(
			sqlalchemy.select(sqlalchemy.func.count()).select_from(held.subquery())
		) or 0,
		paused_total=session.scalar(
			sqlalchemy.select(sqlalchemy.func.count()).select_from(put_down.subquery())
		) or 0,
		passed_total=session.scalar(
			sqlalchemy.select(sqlalchemy.func.count()).select_from(gone.subquery())
		) or 0,
	)


def _deferred (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	workspace_ids: typing.Sequence[uuid.UUID],
	*,
	until: datetime.datetime,
	sortable: typing.Mapping[str, subroutine.domain.ordering.Sortable],
	project: subroutine.db.models.project.Project | None = None,
) -> sqlalchemy.Select[tuple[subroutine.db.models.work.Task]]:
	"""Return the work this day is hiding because somebody deferred it past the end of it.

	**The same scope :func:`_visible` builds, with the defer inverted rather than dropped**
	(`#1215`). Written as a second call to the one function that knows what an agenda's scope is,
	so the two cannot disagree about privacy, the token's project scope or the workspace — which
	is the duplication `readable_tasks` exists to prevent and which this file has paid for once
	already.
	"""

	model = subroutine.db.models.work.Task

	# **`_scoped`, not `_visible`.** The latter has already applied the defer, so negating it a
	# second time asks for rows that are both deferred and not, which is nothing at all — written
	# that way first, and it returned zero against data holding nine.
	return _scoped(
		workspace_ids, principal=principal, sortable=sortable, project=project
	).where(
		sqlalchemy.not_(subroutine.domain.readiness.undeferred(model, now=until))
	)


def _scoped (
	workspace_ids: typing.Sequence[uuid.UUID],
	*,
	principal: subroutine.domain.authentication.Principal,
	sortable: typing.Mapping[str, subroutine.domain.ordering.Sortable],
	project: subroutine.db.models.project.Project | None = None,
) -> sqlalchemy.Select[tuple[subroutine.db.models.work.Task]]:
	"""Return the live, unfinished, visible work this agenda is about, before any of its rules.

	**The place, and nothing about the day.** Everything concerning *who may see what* — the
	workspace scope, project visibility and the token's project scope — comes from
	:func:`subroutine.domain.scoping.readable_tasks`, which is the one copy of those rules
	(§7.3). The agenda kept its own until the slice-2 review found two copies disagreeing about
	whether privacy reaches a private project's children.

	**Lifted out of :func:`_visible` because two callers need the scope without the defer**
	(`#1215`). ``_visible`` hides deferred work and ``_deferred`` counts exactly what it hid, so
	one of them has to be able to ask the question before that rule is applied — and asking it by
	negating a clause the select already carries returns nothing, correctly and uselessly.

	**The project narrowing goes here, beside the workspace one, rather than per bucket.** Every
	bucket narrows this, so one clause covers all seven and a bucket added later is scoped
	without anybody remembering.

	**``within_project`` rather than an id comparison**, so a named project means that area of
	work and not that one node (`#320`). An agenda for a parent project that excluded its own
	sub-projects would answer *nothing due today* about a tree full of deadlines.
	"""

	narrowed = (
		[]
		if project is None
		else [subroutine.domain.scoping.within_project(project)]
	)

	return (
		subroutine.domain.scoping.readable_tasks(
			principal, workspace_ids=workspace_ids, include_completed=False
		)
		.where(*narrowed)
		# **Every row carries the ordering value, because a merged agenda re-sorts in Python**
		# (`#853`). Two of the buckets are ranked, and `subroutine agenda` asks one connection
		# per place and merges the answers — so the rank has to survive the wire or the merge
		# sorts on nulls and silently keeps whichever connection answered first. The
		# expression is a plain `CASE` over two columns, so this costs nothing a sort by it
		# was not paying anyway.
		#
		# **Through `ordering.options` rather than a hand-rolled `with_expression`** (`#986`).
		# The value carried and the value ordered by have to be the same expression, and since
		# a prioritised project changes it per request there are now two ways for them to
		# disagree. One function reading one vocabulary is what makes that impossible: the
		# bucket named here is only there to select `priority_score`, which every ranked
		# bucket uses.
		.options(
			*subroutine.domain.ordering.options(
				None, allowed=sortable, default=ORDERS["unscheduled"]
			)
		)
	)


def _visible (
	session: sqlalchemy.orm.Session,
	principal: subroutine.domain.authentication.Principal,
	workspace_ids: typing.Sequence[uuid.UUID],
	*,
	until: datetime.datetime,
	sortable: typing.Mapping[str, subroutine.domain.ordering.Sortable],
	project: subroutine.db.models.project.Project | None = None,
) -> sqlalchemy.Select[tuple[subroutine.db.models.work.Task]]:
	"""Return the select every bucket narrows: live, unfinished, actionable, visible work.

	Everything about *who may see what* — the workspace scope, project visibility and the
	token's project scope — comes from :func:`subroutine.domain.scoping.readable_tasks`,
	which is the one copy of those rules (§7.3). The agenda kept its own until the slice-2
	review found two copies disagreeing about whether privacy reaches a private project's
	children; a third copy is not the lesson to take from that.

	What is left here is what the *agenda* means, as opposed to what the caller may read:

	- **not finished** — ``completed_at`` is non-null exactly when the status category is
	  done or cancelled (invariant 5), so this needs no join to the status;
	- **not deferred past the end of this day** — ``snoozed_until`` beyond it means "don't show me
	  this yet" (§6.5).

	**``until`` is the end of the day being shown, not the current instant, and that is the
	whole of `#771`.** It was ``now``, so a dentist appointment at 14:00 was hidden from the
	morning's agenda — from *every* bucket at once, which is why a workspace holding one open
	task reported ``unscheduled_total`` of zero. The capture grammar made it systematic rather
	than rare, because a time of day was written into the *defer* column: ``Dentist appointment
	at 2pm`` deferred itself until two o'clock, so every appointment written with a time was
	invisible until it began. `#854` moved that to ``starts_at``, which hides nothing — so this
	guard now protects against a deliberate defer alone rather than against the grammar.

	**A defer hides something until a day, not until an o'clock.** The agenda is a day view, so
	its horizon is that day: a task starting later today belongs to today, and one starting
	tomorrow does not. ``starts_at`` of today is the reader saying *this belongs to this day*,
	and a defer inside the same day may not overrule it.

	:func:`subroutine.domain.readiness.undeferred` is unchanged and keeps comparing against an
	instant, because ``?ready=`` asks *what can I start now* — a different question, to which an
	appointment at 14:00 is honestly "not yet".
	"""

	model = subroutine.db.models.work.Task

	return (
		_scoped(workspace_ids, principal=principal, sortable=sortable, project=project)
		.where(subroutine.domain.readiness.undeferred(model, now=until))
	)


def _run (
	session: sqlalchemy.orm.Session,
	statement: sqlalchemy.Select[tuple[subroutine.db.models.work.Task]],
	bucket: str,
	sortable: typing.Mapping[str, subroutine.domain.ordering.Sortable],
) -> tuple[subroutine.db.models.work.Task, ...]:
	"""Execute one bucket's query in the order :data:`ORDERS` declares for it.

	:data:`TIEBREAK` is appended by :func:`subroutine.domain.ordering.clauses` so that two
	tasks with identical sort keys do not swap places between calls — a list that reorders
	itself while nothing changed is one nobody trusts.

	``sortable`` is the request's own vocabulary rather than the module's, because a workspace's
	prioritised project changes what ``-priority_score`` means for this call (`#986`). It is the
	same map :func:`_visible` carried the value with, which is what stops the order and the
	carried value disagreeing.
	"""

	return tuple(
		session.scalars(
			statement.order_by(
				*subroutine.domain.ordering.clauses(
					None,
					allowed=sortable,
					default=ORDERS[bucket],
					tiebreak=subroutine.db.models.work.Task.created_at,
				)
			)
		)
	)


def _boundary (day: datetime.date, timezone: str, *, end: bool) -> datetime.datetime:
	"""Return the first or last instant of a local day, as UTC.

	The same boundaries §6.5 stores an all-day deadline at, so a task due all-day today
	lands inside today's window rather than one microsecond outside it.
	"""

	moment = subroutine.domain.dates.LAST_MICROSECOND if end else datetime.time.min

	return datetime.datetime.combine(
		day, moment, tzinfo=subroutine.domain.dates.zone(timezone)
	).astimezone(datetime.UTC)
