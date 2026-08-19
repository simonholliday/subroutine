"""Asking a listing a question about one of its fields — item `#815`, decision `#817`.

**§9.6's spelling, for a subset of §9.** A caller writes ``?created_at.gte=yesterday`` and the
field, the operator and the value are three separate things the server can name back when any
of them is wrong. Spec `#456` specifies the whole grammar — a JSON body, nested boolean
composition, string and collection operators — and this is deliberately the part Simon asked
for: comparison operators, over a declared set of fields, on a ``GET``.

**Why not a query parameter per field per direction.** ``created_after``, ``completed_before``
and the rest come to about twenty names across tasks and documents for one kind of question,
and the agent tool surface is a budget (§21.2) — every name an agent must be *taught* is
context spent for ever, where one grammar it can *discover* from ``/v1/meta`` is not. That is
the whole of the requirement this was built for: an agent should be able to generate the
request, not remember the vocabulary.

**The subtle half was already built and unreachable.** :func:`subroutine.domain.dates.resolve`
has understood ``yesterday``, ``now-7d``, ``start_of_week`` and ``start_of_month+1M`` since M1
— in the caller's timezone, with ``m`` and ``M`` distinguished, minutes and hours as elapsed
time against days and larger as calendar units, and month arithmetic clamped to the end of the
month. It reached *writes* only: ``?due_before=start_of_week+3d`` was a 422 saying "invalid
character in year", while ``/v1/meta`` advertised that grammar to agents with examples. This is
what joins the two halves.

**A registry, not a branch** (`#661`'s lesson from ``ORDERINGS``). A field is an entry saying
what column it is and what kind of value it takes; a second kind — integers for
``importance.gte`` — is another entry rather than a condition somewhere. Nothing here knows
about HTTP, because both clients need it and a vocabulary declared in the transport is
reachable by one of them (`#501`, which is why ``ordering.PROJECT_FIELDS`` moved).
"""

import datetime
import typing
import uuid

import sqlalchemy
import sqlalchemy.orm

import subroutine.db.models.activity
import subroutine.db.models.identity
import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.domain.authentication
import subroutine.domain.durations
import subroutine.domain.events
import subroutine.domain.instances
import subroutine.domain.schedule
import subroutine.domain.selection
import subroutine.errors

#: What separates a field from the operator applied to it. §9.6's spelling.
SEPARATOR = "."


#: The comparison operators, and what each does in SQL.
#:
#: **Comparison only, deliberately.** §9.2 also specifies `contains`, `startswith`, `in`, `any`
#: and more; each is a new way to write a query the database cannot serve from an index, and
#: none is needed by the questions this was built for. Decision `#817` records that the rest of
#: §9 arrives when something needs it rather than in advance — a half-built grammar promises
#: more than it does, which is worse than a small one that is honest.
OPERATORS: dict[str, typing.Callable[[typing.Any, typing.Any], typing.Any]] = {
	"eq": lambda column, value: column == value,
	"ne": lambda column, value: column != value,
	"gt": lambda column, value: column > value,
	"gte": lambda column, value: column >= value,
	"lt": lambda column, value: column < value,
	"lte": lambda column, value: column <= value,
}


#: Which end of a whole day each operator means, and this is the part that produces plausible
#: wrong answers if it is got wrong.
#:
#: A caller writing a *day* where a column holds an *instant* means a range, and which end
#: depends on the comparison: `created_at.lte=yesterday` that resolved to yesterday's midnight
#: would exclude all but the first microsecond of the day it names, and return a confidently
#: short list. The rule is that the **inclusive** operators take in the whole day and the
#: **exclusive** ones leave it out — so `gte` and `lt` want its start, `gt` and `lte` its end.
#:
#: These four are exactly :data:`INSTANT`'s operators, which is what lets the lookup be direct
#: rather than defaulted — `eq` and `ne` are refused before anything reaches here.
BOUNDARIES: dict[str, subroutine.domain.schedule.Boundary] = {
	"gte": subroutine.domain.schedule.Boundary.START,
	"lt": subroutine.domain.schedule.Boundary.START,
	"gt": subroutine.domain.schedule.Boundary.END,
	"lte": subroutine.domain.schedule.Boundary.END,
}


def _instant_predicate (
	column: typing.Any,
	operator: str,
	value: str,
	field: str,
	now: datetime.datetime,
	timezone: str,
) -> typing.Any:
	"""Compare an instant column against whatever the caller wrote.

	**Through `schedule.interpret`, which already owns "whatever the caller supplied"** — a
	date, a datetime, an ISO string or a §9.3 expression. Reaching for `dates.resolve` directly
	was the first version and it refused `2026-08-04` outright, because that function answers
	the narrower question of what a *keyword expression* means. Found by driving it rather than
	by reading it, on the example this was built for: *what items were created before the 4th
	August*.

	**Every operator that reaches here has a boundary**, because :data:`INSTANT` allows only
	those four — so the fallback in :data:`BOUNDARIES` is unreachable and the `eq` handling this
	function used to carry is gone with it. It was written before Simon's decision and left
	behind after it: a branch no caller can take, which is the shape this project keeps finding
	as a control that does nothing.
	"""

	moment = subroutine.domain.schedule.interpret(
		value,
		boundary=BOUNDARIES[operator],
		timezone=timezone,
		now=now,
		field=field,
	)

	if moment.instant is None:
		raise _unreadable(field, value)

	return OPERATORS[operator](column, moment.instant)


def _no_predicate_of_its_own (
	column: typing.Any,
	operator: str,
	value: str,
	field: str,
	now: datetime.datetime,
	timezone: str,
) -> typing.Any:
	"""Refuse to compile a field that only means anything beside its group.

	Reached only if a grouped field were declared with no group, which is a mistake in the
	registry rather than anything a caller can do — so it raises rather than returning
	something a listing would silently narrow by.
	"""

	raise AssertionError(f"{field!r} compiles as part of a group, not on its own")


def _duration_predicate (
	column: typing.Any,
	operator: str,
	value: str,
	field: str,
	now: datetime.datetime,
	timezone: str,
) -> typing.Any:
	"""Compare a column holding minutes against §6.4's grammar.

	**Through `durations.parse`, which is the one place that grammar lives** — so `2h` means
	here exactly what `~2h` means in a captured line, and `1d` means 24 hours here exactly as
	it does there. That last one is a trap `#544` records and this deliberately does not
	soften: a filter that read a working day where the rest of the program reads a calendar one
	would be a second answer to a question already settled.
	"""

	try:
		minutes = subroutine.domain.durations.parse(value, field=field)

	except subroutine.errors.ValidationError:
		raise _unreadable(field, value, DURATION) from None

	return OPERATORS[operator](column, minutes)


class Kind (typing.NamedTuple):
	"""How a field's values are read, which comparisons it allows, and what a refusal says."""

	#: Builds the predicate. Raises :class:`subroutine.errors.ValidationError`, field named.
	predicate: typing.Callable[
		[typing.Any, str, str, str, datetime.datetime, str], typing.Any
	]

	#: What a refusal says this field takes.
	expects: str

	#: Which of :data:`OPERATORS` mean anything here. A kind that allows all of them says so
	#: by listing them, because an empty set reading as "everything" is the sort of default
	#: that ships a control nobody declared.
	operators: frozenset[str]


#: A moment: a literal, or any expression `/v1/meta` publishes under `relative_dates`.
#:
#: **`eq` and `ne` are refused, and that is Simon's decision of 2026-08-11 rather than an
#: omission.** A timestamp is stored to the microsecond, so equality against one is almost
#: never what somebody means — and the two ways of being helpful about it are both worse.
#: Comparing exactly makes `created_at.eq=yesterday` match nothing and read as an empty
#: backlog rather than as a misunderstanding.
#:
#: **One of the two arguments for that has since gone, and the decision stands on the other.**
#: This used to add that widening `eq` to the whole day would make it mean two things
#: depending on how the value was written, because `schedule.interpret` infers "a whole day"
#: from the input's *shape* — measured at the time, the literal `2026-08-04` was a whole day
#: and the keyword `yesterday` was not. `#988` ended that: a word that names a day is
#: day-scale on every surface now, so the two spellings agree and nothing is hidden by which
#: one was used. What is left is the microsecond, which is enough on its own.
#:
#: So it is refused by name, pointing at the pair that says what they meant.
INSTANT = Kind(
	predicate=_instant_predicate,
	expects="a date or time, or an expression like `yesterday` or `now-7d`",
	operators=frozenset({"gt", "gte", "lt", "lte"}),
)

#: A username, for asking whose activity — `#815`. Resolved against the whole instance rather
#: than one workspace, which is `#501`'s split: a *filter* must not refuse in a workspace
#: somebody has not joined, where *assigning* work to them there would be unfair.
#:
#: **`eq` only, and `ne` is refused on purpose.** These compile into one correlated `EXISTS`,
#: so `touched_by.ne=si` would mean *there is an event in the window that si did not write* —
#: which is true of anything two people touched, and is not the question anybody is asking.
#: *Not touched by si* is a different query and would need its own operator.
WHO = Kind(
	predicate=_no_predicate_of_its_own,
	expects="a username",
	operators=frozenset({"eq"}),
)

#: How long the work is expected to take — `#319`, and the half of that question there was no
#: way to express at all.
#:
#: **Every operator, unlike :data:`INSTANT`.** The argument that refuses `created_at.eq` is
#: about precision: a timestamp is stored to the microsecond, so equality against one almost
#: never matches what somebody meant. An estimate is a whole number of minutes that a person
#: typed, so `estimate_minutes.eq=2h` compares two numbers and means what it says — and `ne`
#: with it, which reads as *not the ones I said were two hours*.
DURATION = Kind(
	predicate=_duration_predicate,
	expects="a length of time, like `30m`, `2h` or `1h30m` — or a bare number of minutes",
	operators=frozenset(OPERATORS),
)


class Filterable (typing.NamedTuple):
	"""One field a listing can be asked about."""

	#: What it compares against in SQL. For a field with no column of its own — `touched_at` —
	#: this is the entity's identity, which is what the subquery correlates on.
	column: typing.Any

	#: How its value is read.
	kind: Kind

	#: Which fields compile *together*, or ``None`` for one that stands alone.
	#:
	#: **`touched_at` and `touched_by` are one predicate, not two** (decision `#817`). Compiled
	#: independently they would mean *any event in the window* and *any event by si* — possibly
	#: different events — so an item somebody else touched yesterday and si touched last month
	#: would answer *what did si work on yesterday*. One correlated `EXISTS` is the difference.
	group: str | None = None


#: *When was this worked on* — created, edited, completed, commented on, linked, status
#: changed. `#815`'s third and fourth questions, and the two this file exists for.
TOUCHED_AT = "touched_at"

#: *And by whom.* Beside :data:`TOUCHED_AT` it narrows the same events; on its own it means
#: *worked on at any time by this person*.
TOUCHED_BY = "touched_by"


def _worked_on (identity: typing.Any) -> dict[str, Filterable]:
	"""Declare the pair that asks about activity, for one entity's identity column.

	**Not a stored column, and deliberately not one.** A maintained `last_activity_at` was
	considered and deferred (decision `#817`): it is sortable, which an `EXISTS` is not
	cheaply, and `events.record` is a single funnel so it would have one write site — but it
	is a second copy of a fact, which is this codebase's named signature defect. The trigger
	for revisiting it is somebody wanting to **sort** by activity rather than filter on it.
	"""

	return {
		TOUCHED_AT: Filterable(column=identity, kind=INSTANT, group="touched"),
		TOUCHED_BY: Filterable(column=identity, kind=WHO, group="touched"),
	}


def _instants (**fields: typing.Any) -> dict[str, Filterable]:
	"""Declare several instant-valued fields at once, so a registry reads as a list of names."""

	return {name: Filterable(column=column, kind=INSTANT) for name, column in fields.items()}


#: What a task listing can be asked about.
#:
#: **Every entry is a promise about an index**, exactly as ``ordering.TASK_FIELDS`` is: a filter
#: the database cannot serve cheaply is worse than no filter, because it looks like it works
#: until the backlog grows. ``created_at``, ``updated_at``, ``due_at`` and ``starts_at`` all
#: have one; ``completed_at`` and ``snoozed_until`` do not yet and are here because the questions
#: `#815` was filed for need them — measured against this instance, where the largest workspace
#: holds hundreds rather than millions of rows.
TASK_FILTERS: dict[str, Filterable] = {
	**_instants(
		created_at=subroutine.db.models.work.Task.created_at,
		updated_at=subroutine.db.models.work.Task.updated_at,
		# **Not `updated_at` with a status test.** §10.7 invariant 5 maintains this as non-null
		# exactly when the status category is finished, which is what makes "completed
		# yesterday" mean finished yesterday rather than edited yesterday while finished.
		completed_at=subroutine.db.models.work.Task.completed_at,
		# **The one that already had a bespoke pair**, `due_before` and `due_after`. Those keep
		# working and are documented as the older spelling; this is the one that takes
		# `end_of_week`.
		due_at=subroutine.db.models.work.Task.due_at,
		snoozed_until=subroutine.db.models.work.Task.snoozed_until,
		# **Was a `DATE` called `planned_for` and took `eq`** (`#854`). It is an instant now,
		# so it takes the instant operators like every other timestamp — *what starts today*
		# is `starts_at.gte=today` with `starts_at.lt=tomorrow`. Equality is deliberately not
		# carried over: on a timestamp it is the thing `#815` refuses by name, because two
		# instants are equal to the microsecond and almost never to the caller.
		starts_at=subroutine.db.models.work.Task.starts_at,
		content_updated_at=subroutine.db.models.work.Task.content_updated_at,
	),
	# `#319`. **No index, and here anyway on the same measured grounds as `completed_at` and
	# `snoozed_until` above**: the question it was filed for — *what is short and not blocked* —
	# needs it, and the largest workspace on this instance holds 163 open tasks. The comment at
	# the head of this registry is the promise being weighed, and this entry is a place to look
	# when it stops being true.
	"estimate_minutes": Filterable(
		column=subroutine.db.models.work.Task.estimate_minutes, kind=DURATION
	),
	**_worked_on(subroutine.db.models.work.Task.id),
}

#: What a document listing can be asked about.
#:
#: **Shorter for §6.14's reason** — a document is not scheduled, so it has no deadline and no
#: planned day to ask about. It is here at all because one ref counter serves both (§6.2), so
#: *"what was created yesterday"* answered for tasks alone would be wrong about half of what a
#: number can name.
DOCUMENT_FILTERS: dict[str, Filterable] = {
	**_instants(
		created_at=subroutine.db.models.work.Document.created_at,
		updated_at=subroutine.db.models.work.Document.updated_at,
		content_updated_at=subroutine.db.models.work.Document.content_updated_at,
	),
	# **A document is worked on too**, and a comment on one moves nothing in its row — which is
	# the whole reason this is an `EXISTS`. `#815`'s question is about items, and a ref names
	# either kind (§6.2).
	**_worked_on(subroutine.db.models.work.Document.id),
}

#: What a project listing can be asked about.
PROJECT_FILTERS: dict[str, Filterable] = _instants(
	created_at=subroutine.db.models.project.Project.created_at,
	updated_at=subroutine.db.models.project.Project.updated_at,
)

#: Every registry, by the entity name a refusal uses. Named here so `/v1/meta` publishes them
#: from the same place the listings read them, rather than from a second list that agrees today.
FILTERS: dict[str, dict[str, Filterable]] = {
	"task": TASK_FILTERS,
	"document": DOCUMENT_FILTERS,
	"project": PROJECT_FILTERS,
}

def names (entity: str) -> frozenset[str]:
	"""Return every ``field.operator`` this entity accepts, for a caller that lists them.

	**Over each field's own operators rather than over all of them**, which the first version
	got wrong: it published the product of the two tables, so `/v1/meta` would have advertised
	`created_at.eq` — a combination :func:`asked` refuses by name. A published contract nothing
	enforces is the defect this project keeps meeting; here the enforcement existed and the
	publication disagreed with it. Caught by the test asking the question in both directions.
	"""

	# Built rather than stored, because storing it would be a second copy of the product of
	# two tables — and the two are exactly what a refusal names separately.
	return frozenset(
		f"{name}{SEPARATOR}{operator}"
		for name, field in FILTERS.get(entity, {}).items()
		for operator in field.kind.operators
	)


#: The field whose presence in a filter says the caller is asking about finished work —
#: `#818`. Named here rather than spelled in each listing, because it is one fact and the
#: places that need it are on both transports.
COMPLETION_FIELD = "completed_at"


def about (names: typing.Iterable[str], field: str) -> bool:
	"""Report whether any of these dotted names filters on one field.

	Reads the *field* half of each name rather than matching the whole thing, so it answers for
	every operator at once — the question is "did they ask about this column", and
	``completed_at.gte`` and ``completed_at.lt`` are both yes.
	"""

	return any(name.partition(SEPARATOR)[0] == field for name in names)


def timezone_for (
	session: sqlalchemy.orm.Session,
	actor: subroutine.domain.authentication.Principal,
	workspace: subroutine.db.models.identity.Workspace,
) -> str:
	"""Return the zone a listing's dates are read in: §6.5's chain, assembled once.

	**One function because there are three callers and being wrong is invisible.** A day read
	in the wrong zone is right in winter and wrong in summer (`#773`), and the HTTP listing,
	the local client's tasks and its documents would otherwise each assemble this — which is
	this codebase's signature defect on the one rule with no visible symptom.
	"""

	return subroutine.domain.schedule.zone_for(
		user=actor.user,
		workspace=workspace,
		instance=subroutine.domain.instances.get(session),
	)


def parsed (given: typing.Iterable[str]) -> dict[str, str]:
	"""Read ``field.operator=value`` as somebody types it, refusing anything shapeless.

	The form a *terminal* takes, since a query string's separator is not something to type by
	hand. Here rather than in the CLI so that the refusal is one sentence wherever it is met,
	and so a second surface taking the same spelling cannot invent a second one.

	**The value is split on the first ``=`` only**, because a date expression may legitimately
	contain one later and losing the tail would produce a filter that parses and means
	something else.
	"""

	found = {}

	for entry in given:
		name, separator, value = entry.partition("=")

		if not separator or not name.strip():
			raise subroutine.errors.ValidationError(
				f"{entry!r} is not a filter.",
				errors=[
					subroutine.errors.FieldError(
						field="filter",
						code="invalid_field_value",
						message=f"{entry!r} has no '=' in it.",
						hint="Write it as field.operator=value, like created_at.gte=yesterday.",
					)
				],
			)

		found[name.strip()] = value.strip()

	return found


class Comparison (typing.NamedTuple):
	"""One question a caller asked, with the field and the operator already resolved."""

	#: As the caller wrote it, so a refusal about the value names the parameter they sent.
	name: str

	#: The field's own name, for a message that talks about the field rather than the pair.
	field: str

	#: Which of :data:`OPERATORS`.
	operator: str

	#: What it compares against, and how its value is read.
	against: Filterable

	#: Exactly as it arrived. Reading it needs a timezone, which is why this is not a moment.
	value: str

	@property
	def reported (self) -> str:
		"""What a refusal about this value should call it.

		**The field for a dotted name, the name itself for an alias** (`#1017`). A caller who
		wrote ``estimate_minutes.lte`` is told about ``estimate_minutes``, because the operator
		is not what they got wrong; a caller who wrote ``due_after`` must not be told about
		``due_at``, which is not a parameter this route accepts flat and which they never sent.

		Derived rather than stored, so it cannot fall out of step with :data:`ALIASES`.
		"""

		return self.field if SEPARATOR in self.name else self.name


#: The flat parameters that mean exactly what a dotted one means, by entity.
#:
#: **`due_before` and `due_after` predate §9.6's grammar** and were the only two
#: ``datetime.datetime`` query parameters in the API — so they were the one shape the newer
#: machinery could not protect, and a bare date reached a column as a *naive* datetime and came
#: back as a 500 (`#1017`). Routing them through the same resolution makes the two spellings one
#: implementation rather than two that agree today: a change to :data:`BOUNDARIES` now moves
#: both, where before it could move one and leave the other.
#:
#: **A table rather than a branch**, because the next legacy parameter is an entry. Each maps to
#: the dotted spelling it is a synonym for, and the operator is what decides its boundary — so
#: ``due_before`` is ``lt`` and takes the *start* of the day it names, exactly as
#: ``due_at.lt`` does.
ALIASES: dict[str, dict[str, tuple[str, str]]] = {
	"task": {
		"due_before": ("due_at", "lt"),
		"due_after": ("due_at", "gt"),
	},
}


def understood (
	parameters: typing.Iterable[tuple[str, str]], *, entity: str
) -> list[Comparison]:
	"""Resolve every dotted parameter to a field and an operator, refusing anything else.

	**Every parameter carrying the separator belongs to this function**, which is what lets
	``api/query.refuse_unknown`` keep owning the flat names without either of them holding a
	list of the other's. A misspelled field is refused *here*, by name, with the vocabulary —
	so nothing is quietly ignored, which is the property that module exists for.

	**Separate from :func:`predicates` because the two need different things.** Resolving a
	name needs only the registry, so it can run as a request dependency — before the handler,
	where forgetting it is impossible. Reading a *value* needs the timezone, which is not known
	until the workspace has been resolved inside the handler. Doing both late would mean
	``refuse_unknown`` had to let dotted names through on faith.

	**Plus the handful of flat names in :data:`ALIASES`**, which are older spellings of a dotted
	one and are resolved into it here so that both take the same values and land on the same
	boundary (`#1017`).
	"""

	available = FILTERS.get(entity, {})
	aliases = ALIASES.get(entity, {})
	comparisons = []

	for name, value in parameters:
		aliased = aliases.get(name)

		if aliased is not None:
			field, operator = aliased
		elif SEPARATOR in name:
			field, _, operator = name.partition(SEPARATOR)
		else:
			continue

		found = available.get(field)

		if found is None:
			raise _no_such_field(name, field, available)

		if operator not in OPERATORS:
			raise _no_such_operator(name, field, operator)

		if operator not in found.kind.operators:
			raise _wrong_operator_for_the_field(name, field, operator, found.kind)

		comparisons.append(
			Comparison(name=name, field=field, operator=operator, against=found, value=value)
		)

	return comparisons


class Where (typing.NamedTuple):
	"""What compiling a filter needs beyond the value the caller wrote.

	``now`` and ``timezone`` are passed in rather than read, so every expression in one request
	resolves against one instant: §9.3's rule, and the reason ``start_of_day`` and
	``end_of_day`` in a single filter cannot land on different days.

	The rest is only needed by a group that reaches another table. ``touched_at`` joins through
	:func:`subroutine.domain.scoping.visible_events`, because §5.11a makes an event exactly as
	visible as the item it describes — without it a reader would learn an item exists from an
	event they may not read.
	"""

	now: datetime.datetime
	timezone: str

	#: Only for a group that resolves a name. ``touched_by`` takes a username.
	session: sqlalchemy.orm.Session | None = None

	#: The account ``me`` stands for, where a filter takes a username (`#518`). ``None`` means
	#: the word is an ordinary username and will not resolve, which is what a caller with no
	#: principal to offer wants — the sentinel is a courtesy to somebody asking about
	#: themselves, never a way to ask about somebody whose name you do not know.
	caller: subroutine.db.models.identity.User | None = None

	#: Which workspaces the listing is already narrowed to, so a subquery over another table
	#: can reach an index keyed on one. **Not a visibility control** — :func:`_touched` explains
	#: why the join decision `#817` called for turned out to narrow nothing.
	workspace_ids: typing.Sequence[uuid.UUID] = ()


def predicates (
	comparisons: typing.Iterable[Comparison], *, where: Where
) -> list[typing.Any]:
	"""Read each comparison's value and return what to narrow a listing with.

	**Fields declaring a group compile together, once.** Everything else is one predicate per
	comparison, which is the ordinary case and the reason a group is opt-in rather than the
	shape everything is forced into.
	"""

	alone = []
	grouped: dict[str, list[Comparison]] = {}

	for comparison in comparisons:
		if comparison.against.group is not None:
			grouped.setdefault(comparison.against.group, []).append(comparison)

			continue

		alone.append(
			comparison.against.kind.predicate(
				comparison.against.column,
				comparison.operator,
				comparison.value,
				comparison.reported,
				where.now,
				where.timezone,
			)
		)

	return alone + [
		GROUPS[name](members, where) for name, members in sorted(grouped.items())
	]


#: Actions that say somebody *administered* an item rather than worked on it — decision `#817`.
#:
#: **An exclusion rather than a list of what counts**, deliberately: a sixth action added later
#: is included by default, so the failure direction is too many rows rather than work that is
#: silently missing. Measured on this instance, claiming and releasing are about a fifth of the
#: event volume, and `#726` records the case they would misreport — a claim is a lease somebody
#: may take to *read* an item and then decide it is not for them.
BOOKKEEPING = frozenset(
	{
		subroutine.domain.events.EventAction.CLAIMED,
		subroutine.domain.events.EventAction.RELEASED,
	}
)


def _touched (comparisons: list[Comparison], where: Where) -> typing.Any:
	"""Compile *worked on* — one correlated ``EXISTS`` over the event feed.

	**Because `updated_at` cannot see a comment.** Measured on the live instance: a task read
	before and after a comment carries the same `updated_at`, to the microsecond. Simon's
	question names *commented on* explicitly, so a filter built on the row's own timestamps
	would answer it **wrongly rather than partially**, and nothing in the answer would say so.

	**Both ends of the event are matched.** An edit names the item as its `entity`; a comment
	or a link names it as its `subject` (`#252`). Matching only the first would lose exactly
	the case that made a stored column insufficient.

	**Links are counted against the item that was edited**, which is Simon's principle and
	already what the code does — an event names the item somebody was working on, not the far
	end. `#816` is the single exception, where the browser's inverse control swaps the ends;
	`#815` ships with that documented as a false positive, which a reader can see, rather than
	as missing work, which they cannot.

	**The workspace clause is here for the index and not for visibility, and that is worth
	saying plainly** because decision `#817` says to join through
	:func:`subroutine.domain.scoping.visible_events` and this does not. That was written before
	the code existed, and building it showed the join narrows nothing: this subquery correlates
	on the outer row's **own identity**, the outer statement is already narrowed by
	``readable_tasks``, and §5.11a makes an event exactly as visible as the entity it describes
	— so every event it can match belongs to an item the reader may already read. Measured
	rather than argued: removing it failed no test in ``test_isolation``, ``test_scoping``,
	``test_multi_user`` or ``test_events_scoping``, which is what a control that does nothing
	looks like. What it *would* have cost is real — several correlated ``EXISTS`` clauses
	evaluated once per candidate row.

	``workspace_id`` stays because ``ix_event_workspace_id_created_at`` leads on it. Measured on
	SQLite's planner, 2026-08-11: *SEARCH event USING INDEX ix_event_workspace_id_created_at
	(workspace_id=? AND created_at>?)*. Without it the index this filter exists to use cannot
	be reached at all.
	"""

	event = subroutine.db.models.activity.Event
	identity = comparisons[0].against.column

	narrowing = [
		sqlalchemy.or_(
			event.entity_id == identity, event.subject_id == identity
		),
		event.action.notin_([action.value for action in BOOKKEEPING]),
	]

	if where.workspace_ids:
		narrowing.append(event.workspace_id.in_(list(where.workspace_ids)))

	for comparison in comparisons:
		if comparison.field == TOUCHED_BY:
			narrowing.append(event.actor_user_id == _whoever(comparison, where))

			continue

		moment = subroutine.domain.schedule.interpret(
			comparison.value,
			boundary=BOUNDARIES[comparison.operator],
			timezone=where.timezone,
			now=where.now,
			field=comparison.field,
		)

		if moment.instant is None:
			raise _unreadable(comparison.field, comparison.value)

		narrowing.append(
			OPERATORS[comparison.operator](event.created_at, moment.instant)
		)

	return sqlalchemy.exists().where(*narrowing)


def _whoever (comparison: Comparison, where: Where) -> uuid.UUID:
	"""Resolve the username ``touched_by`` names, refusing one that is nobody."""

	if where.session is None:
		raise AssertionError("touched_by needs a session to resolve a username")

	return subroutine.domain.selection.user(
		where.session, comparison.value, caller=where.caller
	).id


#: Which fields compile together, and what compiles them. One entry, so far.
GROUPS: dict[str, typing.Callable[[list[Comparison], Where], typing.Any]] = {
	"touched": _touched,
}


def asked (
	parameters: typing.Iterable[tuple[str, str]],
	*,
	entity: str,
	now: datetime.datetime,
	timezone: str,
	session: sqlalchemy.orm.Session | None = None,
	caller: subroutine.db.models.identity.User | None = None,
	workspace_ids: typing.Sequence[uuid.UUID] = (),
) -> list[typing.Any]:
	"""Compile every dotted parameter into predicates, refusing anything it cannot.

	Both halves in one call, for a caller that has everything it needs at once — the CLI's
	local client, and every test of the grammar itself.
	"""

	return predicates(
		understood(parameters, entity=entity),
		where=Where(
			now=now,
			timezone=timezone,
			session=session,
			caller=caller,
			workspace_ids=workspace_ids,
		),
	)


def _no_such_field (
	name: str, field: str, available: dict[str, Filterable]
) -> subroutine.errors.ValidationError:
	"""Refuse a field this entity has no filter for, naming the ones it has."""

	listed = ", ".join(sorted(available)) or "nothing"

	return subroutine.errors.ValidationError(
		f"{field!r} is not a field this endpoint can filter on.",
		errors=[
			subroutine.errors.FieldError(
				field=name,
				code="invalid_field_value",
				message=f"No filterable field is called {field!r}.",
				hint=f"This endpoint filters on: {listed}.",
			)
		],
		hint="GET /v1/meta lists these too, so this can be checked without guessing.",
	)


def _no_such_operator (
	name: str, field: str, operator: str
) -> subroutine.errors.ValidationError:
	"""Refuse an operator that does not exist, naming the ones that do.

	Separate from the field's refusal on purpose: ``created_at.after=x`` and
	``creatd_at.gte=x`` are different mistakes, and one message covering both would tell
	whoever made either of them to check the wrong half.
	"""

	return subroutine.errors.ValidationError(
		f"{operator!r} is not an operator this endpoint understands.",
		errors=[
			subroutine.errors.FieldError(
				field=name,
				code="invalid_field_value",
				message=f"{field!r} is a field here, but {operator!r} is not an operator.",
				hint=f"The operators are: {', '.join(sorted(OPERATORS))}.",
			)
		],
	)


def _unreadable (
	field: str, value: str, kind: Kind = INSTANT
) -> subroutine.errors.ValidationError:
	"""Refuse a value this field could not read, saying what it does take.

	**The wording comes from the kind rather than from this function** (`#319`). It said
	*"does not say when"* and pointed at the date grammar, which was true of every field there
	was until an estimate became filterable — and would then have answered a caller who wrote
	``estimate_minutes.lte=fortnight`` with advice about `relative_dates`. One of a thing, in a
	refusal: the message was correct for as long as there was only one kind of value.

	``INSTANT`` is the default because three of the four callers are date-shaped and passing it
	at each would be noise; the one that is not says so.
	"""

	return subroutine.errors.ValidationError(
		f"{value!r} could not be read.",
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="invalid_field_value",
				message=f"{field!r} takes {kind.expects}.",
				hint=(
					"GET /v1/meta publishes the date grammar under `relative_dates`."
					if kind is INSTANT
					else "GET /v1/meta publishes what each filter accepts."
				),
			)
		],
	)


def _wrong_operator_for_the_field (
	name: str, field: str, operator: str, kind: Kind
) -> subroutine.errors.ValidationError:
	"""Refuse a real operator on a field where it would not mean anything.

	**The refusal a caller most needs, because the alternative is silence.** ``eq`` on a
	timestamp is the case this exists for: it parses, it runs, and it matches nothing, which
	reads as an empty backlog rather than as a question the server did not understand.
	"""

	return subroutine.errors.ValidationError(
		f"{field!r} cannot be filtered with {operator!r}.",
		errors=[
			subroutine.errors.FieldError(
				field=name,
				code="invalid_field_value",
				message=(
					f"{field!r} is stored to the microsecond, so {operator!r} would compare "
					f"against one instant and almost always match nothing."
					if operator in {"eq", "ne"}
					else f"{operator!r} does not apply to {field!r}."
				),
				hint=(
					f"Use a range: {field}.gte and {field}.lt. "
					f"This field takes {', '.join(sorted(kind.operators))}."
				),
			)
		],
	)
