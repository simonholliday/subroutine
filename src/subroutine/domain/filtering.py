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

import subroutine.db.models.project
import subroutine.db.models.work
import subroutine.domain.schedule
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


def _day_predicate (
	column: typing.Any,
	operator: str,
	value: str,
	field: str,
	now: datetime.datetime,
	timezone: str,
) -> typing.Any:
	"""Compare a column that stores a calendar day and nothing else.

	No boundary to choose: §6.5 keeps no time and no timezone here, so a day is the whole of
	the value. A relative expression is resolved and then read as a date **in the caller's
	timezone**, which is the step that makes `planned_for.eq=today` mean today where they are.
	"""

	day = subroutine.domain.schedule.interpret_day(
		value, timezone=timezone, now=now, field=field
	)

	if day is None:
		raise _unreadable(field, value)

	return OPERATORS[operator](column, day)


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
#: backlog rather than as a misunderstanding; widening it to the whole day makes `eq` mean
#: two different things depending on how the value was written, because
#: `schedule.interpret` infers "a whole day" from the input's *shape* — measured, the literal
#: `2026-08-04` is a whole day and the keyword `yesterday` is not, and nothing in the answer
#: would show which reading applied.
#:
#: So it is refused by name, pointing at the pair that says what they meant. **The only option
#: of the three with no invisible failure.**
INSTANT = Kind(
	predicate=_instant_predicate,
	expects="a date or time, or an expression like `yesterday` or `now-7d`",
	operators=frozenset({"gt", "gte", "lt", "lte"}),
)

#: A calendar day, for a column that stores one — `planned_for` and nothing else so far.
#: **Equality is right here and is kept**, which is the other half of the rule above: this
#: column stores a day and nothing finer, so `planned_for.eq=today` compares two days and
#: means exactly what it says.
DAY = Kind(
	predicate=_day_predicate,
	expects="a day, or an expression like `today` or `start_of_week`",
	operators=frozenset(OPERATORS),
)


class Filterable (typing.NamedTuple):
	"""One field a listing can be asked about."""

	#: What it compares against in SQL.
	column: typing.Any

	#: How its value is read.
	kind: Kind


def _instants (**fields: typing.Any) -> dict[str, Filterable]:
	"""Declare several instant-valued fields at once, so a registry reads as a list of names."""

	return {name: Filterable(column=column, kind=INSTANT) for name, column in fields.items()}


#: What a task listing can be asked about.
#:
#: **Every entry is a promise about an index**, exactly as ``ordering.TASK_FIELDS`` is: a filter
#: the database cannot serve cheaply is worse than no filter, because it looks like it works
#: until the backlog grows. ``created_at``, ``updated_at``, ``due_at`` and ``planned_for`` all
#: have one; ``completed_at`` and ``start_at`` do not yet and are here because the questions
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
		start_at=subroutine.db.models.work.Task.start_at,
		content_updated_at=subroutine.db.models.work.Task.content_updated_at,
	),
	# **A `date` column, so it takes a day rather than an instant** — §6.5 stores no time and no
	# timezone here, and comparing it against a UTC instant is `#773` waiting to happen.
	"planned_for": Filterable(
		column=subroutine.db.models.work.Task.planned_for, kind=DAY
	),
}

#: What a document listing can be asked about.
#:
#: **Shorter for §6.14's reason** — a document is not scheduled, so it has no deadline and no
#: planned day to ask about. It is here at all because one ref counter serves both (§6.2), so
#: *"what was created yesterday"* answered for tasks alone would be wrong about half of what a
#: number can name.
DOCUMENT_FILTERS: dict[str, Filterable] = _instants(
	created_at=subroutine.db.models.work.Document.created_at,
	updated_at=subroutine.db.models.work.Document.updated_at,
	content_updated_at=subroutine.db.models.work.Document.content_updated_at,
)

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
	"""

	available = FILTERS.get(entity, {})
	comparisons = []

	for name, value in parameters:
		if SEPARATOR not in name:
			continue

		field, _, operator = name.partition(SEPARATOR)
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


def predicates (
	comparisons: typing.Iterable[Comparison], *, now: datetime.datetime, timezone: str
) -> list[typing.Any]:
	"""Read each comparison's value and return what to narrow a listing with.

	``now`` and ``timezone`` are passed in rather than read here, so that every expression in
	one request resolves against one instant: §9.3's rule, and the reason ``start_of_day`` and
	``end_of_day`` in a single filter cannot land on different days.
	"""

	return [
		comparison.against.kind.predicate(
			comparison.against.column,
			comparison.operator,
			comparison.value,
			comparison.field,
			now,
			timezone,
		)
		for comparison in comparisons
	]


def asked (
	parameters: typing.Iterable[tuple[str, str]],
	*,
	entity: str,
	now: datetime.datetime,
	timezone: str,
) -> list[typing.Any]:
	"""Compile every dotted parameter into predicates, refusing anything it cannot.

	Both halves in one call, for a caller that has everything it needs at once — the CLI's
	local client, and every test of the grammar itself.
	"""

	return predicates(understood(parameters, entity=entity), now=now, timezone=timezone)


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


def _unreadable (field: str, value: str) -> subroutine.errors.ValidationError:
	"""Refuse a value that named no moment at all, which is what an empty one does."""

	return subroutine.errors.ValidationError(
		f"{value!r} does not say when.",
		errors=[
			subroutine.errors.FieldError(
				field=field,
				code="invalid_field_value",
				message=f"{field!r} could not be read as a moment.",
				hint="GET /v1/meta publishes the date grammar under `relative_dates`.",
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
