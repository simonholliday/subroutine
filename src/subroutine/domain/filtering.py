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

import subroutine.db.mixins
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
import subroutine.domain.tags
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

#: Asking whether a field has a value at all — `#1804`, design `#1801` §5.
#:
#: **Not in :data:`OPERATORS`, and that is the distinction rather than an omission.** Every
#: entry there takes the field's own kind of value and compares it; this one takes one of two
#: reserved words and asks a question about the *column*, so it is compiled by
#: :func:`_condition_predicate` before a kind is consulted at all. Putting it in that table
#: would make it a comparison against a value called "unset", which is the confusion the whole
#: `is` / `eq` split exists to prevent.
IS = "is"

#: Asking whether a field holds any of several values — `#1804`, design `#1801` §5.
#:
#: **Comma-separated, and Simon took the consequence the same day**: a comma becomes illegal in
#: a tag name. Measured before the rule rather than after — the `projects` workspace holds 34
#: tags and not one contains a comma or a space — so it costs nothing now and prevents an
#: ambiguity that would otherwise be permanent. Project keys, status keys, type keys and
#: usernames are already constrained and cannot hold one.
#:
#: **Any of these, never all of them.** *Both* tags rather than *either* is a real question and
#: nothing has asked for it; `#1801` §5 names it so the absence is not mistaken for an
#: oversight.
#:
#: **Not in :data:`OPERATORS` either**, for :data:`IS`'s reason one step along: every entry
#: there compares one value, and this splits its argument and resolves each part through the
#: field's own resolver. Which is also why it is compiled by a reference's own function rather
#: than centrally — a central version would need the resolver anyway and would be a second
#: place the splitting rule lives.
IN = "in"

#: What separates the values of an :data:`IN`.
#:
#: **Declared by :mod:`subroutine.domain.tags` and read here**, which is the way round the
#: dependency has to run: compiling a ``tag`` filter needs :func:`subroutine.domain.tags.
#: carrying`, so this module imports that one. The rule it enforces belongs to a tag's *name*
#: and this is the grammar that makes it necessary, so one declaration serves both and neither
#: can drift.
IN_SEPARATOR = subroutine.domain.tags.REFUSED_IN_A_NAME

#: Every operator a caller may write, whichever kind it turns out to be.
EVERY_OPERATOR = frozenset(OPERATORS) | {IS, IN}


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
	# **`is` alongside the four, since `#1804`.** *Has a deadline at all* is a question no
	# comparison can put — `due_at.gt=1970-01-01` is the workaround people reach for, and it is
	# wrong about a date before the epoch and unreadable about what was meant.
	operators=frozenset({"gt", "gte", "lt", "lte", IS}),
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
	# **And `is` is refused too, unlike every other kind** (`#1804`). This field has no column:
	# it compiles into a correlated `EXISTS` over the event table, so *set* and *unset* would
	# have to mean *has ever been touched by anybody*, which is true of every row that exists.
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
	# **`is` too**: *nobody has estimated this* is what a planner asks before anything else, and
	# `estimate_minutes.lte=<huge>` cannot express it — an unestimated task has no value to
	# compare rather than a large one.
	operators=frozenset(OPERATORS) | {IS},
)


class Property (typing.NamedTuple):
	"""One property of an item, and what a listing may do with it — `#1803`, design `#1801`.

	**Filterable, orderable and groupable were declared three times, and did not agree.**
	Measured on 2026-09-01: eleven fields in this module, twelve in ``api.tasks.SORTABLE``, one
	in ``domain.grouping.AXES``, and **no field in all three**. A reader could sort the whole
	backlog by urgency and could not ask for the urgent ones — which is not a missing feature
	but three lists nothing held against each other. This codebase's signature defect, on the
	vocabulary that decides what a caller may ask.

	So each property is declared once here and the three lists are *derived* from it by
	:func:`filters`, :func:`orderable` and :func:`axes`. They cannot disagree, because there is
	one of them.

	**The declaration lives in this module because a circular import decides it.**
	:func:`understood` reads the registry at module scope, so a separate registry module
	importing this one could never be imported back by it — and passing the registry to
	``understood``, ``asked``, ``names`` and ``about`` instead would put two modules in front
	of fourteen call sites that today know one. :mod:`subroutine.domain.ordering` and
	:mod:`subroutine.domain.grouping` import *this*, and nothing here imports either.

	**A capability whose mechanism belongs to another module is still declared here.**
	``priority_score`` is a banded expression that :mod:`subroutine.domain.ordering` builds and
	this module could not; its entry says *orderable, not filterable*, and carries the reason.
	The registry declares **capability** and a module owns **mechanism** — which is what stops
	an order-only field being absent from the registry altogether and taking its asymmetry with
	it.
	"""

	#: What SQL compares, orders or groups on. ``None`` for a property whose expression is
	#: another module's — see the class docstring — and for one that is only an axis.
	column: typing.Any = None

	#: How a filter reads its value, and ``None`` for a property that cannot be filtered on.
	#:
	#: **One optional argument rather than a boolean beside it**, because two fields saying the
	#: same thing is the shape that lets them disagree: a ``filterable=True`` with no kind is a
	#: promise nothing can keep, and a kind with ``filterable=False`` is a reader nothing calls.
	kind: Kind | None = None

	#: Whether ``?order=`` may name it.
	orderable: bool = False

	#: Every key of the axis, in the order a reader meets them, or ``None`` for a property that
	#: is not one.
	#:
	#: **The keys rather than a flag**, because grouping asks one query per group and an axis
	#: has to be *bounded* to be affordable — see :mod:`subroutine.domain.grouping`. A boolean
	#: would let somebody declare an assignee groupable, which is an N+1 wearing a parameter.
	groupable: tuple[str, ...] | None = None

	#: Which properties compile into one predicate. See :class:`Filterable`.
	group: str | None = None

	#: Why a capability is absent where its neighbours are present.
	#:
	#: **Required wherever the three disagree**, and that is the point of the registry rather
	#: than a side effect of it: nine fields were asymmetric when this was written and not one
	#: carried a reason anywhere. Some asymmetries are right — ``priority_score`` is computed
	#: and has no value to compare — and they are worth as much written down as the gaps are.
	#: ``tests/test_filtering.py`` is what makes it mechanical.
	because: str | None = None


#: What ``is`` compares against: whether the field has a value at all, never what it is.
#:
#: **Two reserved words and no more, which is the whole discipline** — `#1804`, design `#1801`
#: §5, Simon's decision of 2026-09-01. ``eq`` compares the field's *value*, drawn from the
#: data's own vocabulary; ``is`` asks about its *condition*, and its argument is one of these
#: two, which are not data and cannot collide with any.
#:
#: **`is` must never become a grab-bag.** GitHub's ``is:`` carries a type (``is:issue``), a
#: state (``is:open``) and a condition (``is:draft``) under one word — three different
#: questions, which is why it has to be learned rather than read. Here it answers exactly one:
#: *does this field have a value?* Anything that reads like a state — *overdue*, *blocked*,
#: *ready* — is a policy (`#1801` §3) or its own registry field, and never a value of ``is``.
SET = "set"
UNSET = "unset"

CONDITIONS = (SET, UNSET)


def _condition_predicate (
	column: typing.Any,
	operator: str,
	value: str,
	field: str,
	now: datetime.datetime,
	timezone: str,
) -> typing.Any:
	"""Ask whether a field has a value at all, whatever that value is — `#1804`.

	**Refuses anything but the two reserved words, by name.** The two are the whole vocabulary,
	so a refusal can list it — which is the property `#1801` §5 gives as the reason for keeping
	``is`` narrow, and it stops being true the moment a third word is admitted.
	"""

	if value not in CONDITIONS:
		raise subroutine.errors.ValidationError(
			f"{value!r} is not something a field can be.",
			errors=[
				subroutine.errors.FieldError(
					field=field,
					code="invalid_field_value",
					message=f"{field}.is takes {' or '.join(CONDITIONS)}, not {value!r}.",
					hint=(
						f"Use {field}.is={UNSET} for items where nobody has set it, or "
						f"{field}.eq=<value> to compare what it holds."
					),
				)
			],
		)

	return column.is_(None) if value == UNSET else column.is_not(None)


def _number_predicate (
	column: typing.Any,
	operator: str,
	value: str,
	field: str,
	now: datetime.datetime,
	timezone: str,
) -> typing.Any:
	"""Compare a whole number a person typed — `#1804`.

	**Every operator, unlike :data:`INSTANT`.** The argument that refuses ``created_at.eq`` is
	about precision: a timestamp is stored to the microsecond, so equality against one almost
	never matches what somebody meant. A rank is one of five values a person chose, so
	``importance.eq=5`` compares two small integers and means exactly what it says.
	"""

	try:
		number = int(value)

	except ValueError:
		raise _unreadable(field, value, NUMBER) from None

	return OPERATORS[operator](column, number)


#: A whole number a person chose — `importance` and `urgency` (§6.3), and later a count.
#:
#: **This is Simon's own `urgent>3` example**, and its absence was `#1801` §1's finding rather
#: than a missing feature: both were orderable and not filterable, so the backlog could be
#: *sorted* by urgency and not *asked* for the urgent ones. Two lists, nothing holding them
#: together.
#:
#: **`is` alongside the comparisons, because unranked is a real answer.** §6.3a's whole
#: argument is that ranked, part-ranked and unranked are three states — so
#: ``importance.is=unset`` is *nobody has judged this*, which no comparison can express and
#: which an ordering has needed a band for since it was written.
NUMBER = Kind(
	predicate=_number_predicate,
	expects="a whole number",
	operators=frozenset(OPERATORS) | {IS},
)


#: A field naming something the instance has to look up — `#1804`, design `#1801` §5.
#:
#: **The value is a name a person has**, not an id: a tag, a username, a project key, a ref.
#: That is what the flat route parameters have always taken, and it is where a good refusal
#: comes from — ``selection.user`` names the account it could not find and points at the command
#: that lists them, where an unresolved value answered as an empty listing is indistinguishable
#: from *there is none of that*.
#:
#: **Compiled through :data:`GROUPS` rather than by a predicate of its own**, exactly as
#: :data:`WHO` is. Resolving needs the session and often the workspace, and a kind's predicate
#: is handed a value and a clock. That mechanism already exists for `#817`'s reason and needed
#: nothing rewritten; widening the predicate signature to carry a :class:`Where` would have
#: meant rewriting the one path every listing's filters compile through, in the same breath as
#: adding a kind.
#:
#: **`ne` is deliberately absent.** *Not this tag* over a join table is *no row joins it*, which
#: is a different query from *a row joins it and is not this* — and the second is what a naive
#: negation produces. `#1801` §9 keeps the query string flat and ANDed; a negation wants the
#: `POST` body `#817` reserved for exactly this.
REFERENCE = Kind(
	predicate=_no_predicate_of_its_own,
	expects="a name, a key or a username",
	operators=frozenset({"eq", IN, IS}),
)


#: A field a caller may ask *whether* about and not yet *what* — `#1804`.
#:
#: **A kind whose only operator is `is`**, for a column that has a name behind it nothing can
#: resolve yet. ``assignee`` and ``parent`` are both real columns holding ids, and *nobody has
#: this* and *this is not a sub-task* are questions a planner asks constantly — where *whose*
#: and *whose parent* need a username or a ref turned into a UUID, which is the ``REFERENCE``
#: kind and the rest of this item.
#:
#: **Better than offering `eq` early.** A caller writing ``assignee.eq=si`` against a raw
#: ``assignee_id`` would be refused for a value the flat ``assignee=si`` accepts, which is one
#: field answering the same question two ways. Refusing the operator by name says *not yet*;
#: accepting a UUID says *you are holding it wrong*.
CONDITION = Kind(
	predicate=_no_predicate_of_its_own,
	expects="set or unset",
	operators=frozenset({IS}),
)


class Filterable (typing.NamedTuple):
	"""One field a listing can be asked about.

	**Derived from :class:`Property` by :func:`filters`, rather than declared beside it.** The
	filter machinery reads ``kind`` without a guard — :attr:`Comparison.against` is one of these
	and the predicate is ``against.kind.predicate(…)`` — and a registry entry's kind is optional
	because *not filterable* is a state it has to be able to describe. Two types, one
	declaration, and the narrowing happens in one function.
	"""

	#: What it compares against in SQL. For a field with no column of its own — `touched_at` —
	#: this is the entity's identity, which is what the subquery correlates on.
	column: typing.Any

	#: How its value is read.
	kind: Kind

	#: Which comparisons this particular field allows — the kind's, narrowed by the column.
	#:
	#: **Because :data:`IS` is meaningless on a column that cannot be null** (`#1804`). A kind
	#: says which operators make sense for a *sort of value*; whether *unset* is a state this
	#: field can be in is a fact about the column, and `created_at.is=set` is every row while
	#: `created_at.is=unset` is none. Publishing it would be a filter that can only ever answer
	#: all or nothing — this codebase's inert-control defect, arriving through a generalisation
	#: rather than through a constant nobody wired up.
	#:
	#: Found by ``test_every_published_filter_is_accepted_by_the_listing_that_publishes_it``,
	#: which drove every published combination and reported four routes as broken.
	operators: frozenset[str] = frozenset()

	#: Which fields compile *together*, or ``None`` for one that stands alone.
	#:
	#: **`touched_at` and `touched_by` are one predicate, not two** (decision `#817`). Compiled
	#: independently they would mean *any event in the window* and *any event by si* — possibly
	#: different events — so an item somebody else touched yesterday and si touched last month
	#: would answer *what did si work on yesterday*. One correlated `EXISTS` is the difference.
	group: str | None = None


#: Which group compiles the two fields naming an account that *holds* an item — `#1804`.
#:
#: **A group of one field at a time, and that is what the mechanism is for.** ``assignee`` and
#: ``claimed_by`` never compile together — they are separate questions about separate columns —
#: but each needs the session to turn a username into an id, and a kind's predicate is handed a
#: value and a clock. :data:`GROUPS` is where a field whose compilation needs more than its
#: value goes, which `#817` built for ``touched_at`` and which needed nothing rewritten here.
WHO_HOLDS_IT = "holder"

#: Which group compiles ``tag``, whose predicate is a subquery over a join table — `#1804`.
TAGGED = "tagged"

#: *When was this worked on* — created, edited, completed, commented on, linked, status
#: changed. `#815`'s third and fourth questions, and the two this file exists for.
TOUCHED_AT = "touched_at"

#: *And by whom.* Beside :data:`TOUCHED_AT` it narrows the same events; on its own it means
#: *worked on at any time by this person*.
TOUCHED_BY = "touched_by"


def _worked_on (identity: typing.Any) -> dict[str, Property]:
	"""Declare the pair that asks about activity, for one entity's identity column.

	**Not a stored column, and deliberately not one.** A maintained `last_activity_at` was
	considered and deferred (decision `#817`): it is sortable, which an `EXISTS` is not
	cheaply, and `events.record` is a single funnel so it would have one write site — but it
	is a second copy of a fact, which is this codebase's named signature defect. The trigger
	for revisiting it is somebody wanting to **sort** by activity rather than filter on it.
	"""

	return {
		TOUCHED_AT: Property(
			column=identity, kind=INSTANT, group="touched", because=NOT_A_COLUMN
		),
		TOUCHED_BY: Property(
			column=identity, kind=WHO, group="touched", because=NOT_A_COLUMN
		),
	}


def _instants (**fields: typing.Any) -> dict[str, Property]:
	"""Declare several instant-valued properties, so a registry reads as a list of names.

	**Filterable and orderable together**, which is what a timestamp with a column of its own
	always was on both counts — the two lists happened to agree about most of these and nothing
	held them to it.
	"""

	return {
		name: Property(column=column, kind=INSTANT, orderable=True)
		for name, column in fields.items()
	}


#: Why a property that has no column of its own is not orderable.
#:
#: Shared by the two the activity pair declares, because it is one reason: ``touched_at`` and
#: ``touched_by`` compile into a correlated ``EXISTS`` over the event table and there is
#: nothing on the row to sort by. `#817` weighed a maintained ``last_activity_at`` and deferred
#: it — sortable, one write site, and a second copy of a fact whose drift is silent — and
#: **named the trigger for revisiting: somebody wanting to sort by activity rather than filter
#: on it.** That sentence is quoted rather than re-derived, and `#1801` §7 reached the same
#: shape again on a second field the same way.
NOT_A_COLUMN = (
	"an `EXISTS` over the event table, with nothing on the row to sort by. `#817` deferred a "
	"maintained column and named the trigger: somebody wanting to sort by activity rather "
	"than filter on it."
)

#: The task properties an ordering may name and a filter may not — `#1803`, design `#1801` §1.
#:
#: **Five, and none of them carried a reason before this.** They were simply in one list and
#: not the other, which is how ``importance`` and ``urgency`` came to be sortable and
#: unaskable — Simon's own ``urgent>3`` example, and the finding that decided the registry's
#: shape. Two of the five are gaps with items against them; three are arguments.
_ORDER_ONLY: dict[str, Property] = {
	# **Both filterable since `#1804`**, which is what the registry was built to make possible:
	# they were sortable and unaskable, so a reader could sort the whole backlog by urgency and
	# not ask for the urgent ones. Simon's own `urgent>3` example, and it was two lists rather
	# than a missing feature.
	"importance": Property(
		column=subroutine.db.models.work.Task.importance, kind=NUMBER, orderable=True
	),
	"urgency": Property(
		column=subroutine.db.models.work.Task.urgency, kind=NUMBER, orderable=True
	),
	# **Declared with no column, because `ordering` owns the expression** — see `Property`. A
	# banded `CASE` cannot be built here and the module that builds it imports this one.
	"priority_score": Property(
		orderable=True,
		because=(
			"computed, and banded by §6.3a rather than stored — there is no value to compare "
			"against. The three ranking states are what an ordering arranges; what a caller "
			"reads is `importance * urgency`, and the two are deliberately different things."
		),
	),
	"ref": Property(
		column=subroutine.db.models.work.Task.ref,
		orderable=True,
		because=(
			"a ref names exactly one item and `show` is how you ask for it; a *set* of refs "
			"would be `in` on a lookup, and nothing has asked for one."
		),
	),
	"title": Property(
		column=subroutine.db.models.work.Task.title,
		orderable=True,
		because=(
			"`q` already matches the title, and `title:foo` is a filter wearing search syntax "
			"— `#1801` §8, and the grammar gives it for nothing once `#1806` lands."
		),
	),
}

#: What ``?group_by=`` calls the axis a board is arranged on.
#:
#: **A category rather than a status key**, for :func:`subroutine.domain.tasks.
#: statuses_in_category`'s reason: a key is per-workspace and renameable, so a board keyed on
#: one stops working on the first installation that renames it. The category is the fixed field
#: published beside it precisely so a client may branch on it.
#:
#: **Here rather than in :mod:`subroutine.domain.grouping`, since `#1803`.** That module reads
#: its axes from this registry now, so it cannot also be where their names are decided — and a
#: name spelled in both is the duplication the registry exists to remove.
STATUS_CATEGORY = "status_category"

#: The task properties a listing can be asked *whether* about, and not yet *what* — `#1804`.
#:
#: **Both answer a question that had no spelling at all.** ``parent=none`` looked up a task
#: called *none* and answered **404**; ``assignee=none`` did the same for an account. Those are
#: two of the four rows in `#1804`'s table, and both are now ``.is=unset``.
#:
#: **Declared with a kind that offers only `is`**, because *which* parent and *which* assignee
#: need a name resolved to an id — a ``REFERENCE`` kind, which is the rest of `#1804` and lands
#: with the flat parameters it takes over. Offering ``eq`` here before that exists would accept
#: a UUID and refuse the username the flat spelling already takes, which is worse than not
#: offering it.
_CONDITION_ONLY: dict[str, Property] = {
	"assignee": Property(
		column=subroutine.db.models.work.Task.assignee_id,
		kind=REFERENCE,
		group=WHO_HOLDS_IT,
		because="ordering by an account id means nothing; ordering by who has what is `#1805`.",
	),
	"claimed_by": Property(
		column=subroutine.db.models.work.Task.claimed_by_id,
		kind=REFERENCE,
		group=WHO_HOLDS_IT,
		because="ordering by an account id means nothing — `claimed_at` is the sort that "
		"answers *taken longest ago*, and it is orderable.",
	),
	"tag": Property(
		column=subroutine.db.models.work.Task.id,
		kind=REFERENCE,
		group=TAGGED,
		because=(
			"a row carries several tags, so there is no one value to sort it by. Ordering a "
			"listing by a set is a different question and nothing has asked it. `tag.is` is "
			"absent for a second reason worth knowing: the column here is the item's own "
			"identity, so `_allowed` refuses it — and *has no tags at all* really is a "
			"different query, a `NOT EXISTS` over the join table rather than a null column."
		),
	),
	"parent": Property(
		column=subroutine.db.models.work.Task.parent_task_id,
		kind=CONDITION,
		because=(
			"`parent=<ref>` is the flat spelling and resolves a ref to an id, and it carries "
			"`subtree` with it — one parameter, two questions, which has to be settled before "
			"this can take a value. Ordering by a parent id means nothing."
		),
	),
}


#: The task properties a listing may be grouped by and nothing else — `#1803`.
#:
#: **The first entry to carry the third capability on its own**, and it is what the registry
#: makes visible: ``status_category`` was declared in :mod:`subroutine.domain.grouping` and in
#: neither of the other two lists, so *this is groupable* and *this is filterable* were facts
#: kept in different modules about the same word. It reaches a listing today as a flat route
#: parameter, which `#1804` is what changes.
_AXES_ONLY: dict[str, Property] = {
	STATUS_CATEGORY: Property(
		groupable=subroutine.db.mixins.TASK_STATUS_CATEGORIES,
		because=(
			"a flat route parameter today rather than a dotted filter, and an ordering by "
			"category would sort by an id — `#1804` gives it an ENUM kind and `#1805` the "
			"ordering, if a workspace's own status order turns out to be what people mean."
		),
	),
}


#: What a task listing can be asked about.
#:
#: **Every entry is a promise about an index**, exactly as ``ordering.TASK_FIELDS`` is: a filter
#: the database cannot serve cheaply is worse than no filter, because it looks like it works
#: until the backlog grows. ``created_at``, ``updated_at``, ``due_at`` and ``starts_at`` all
#: have one; ``completed_at`` and ``snoozed_until`` do not yet and are here because the questions
#: `#815` was filed for need them — measured against this instance, where the largest workspace
#: holds hundreds rather than millions of rows.
TASK_PROPERTIES: dict[str, Property] = {
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
		# **When somebody took it, which is not when they will finish** (`#1120`). Reported on
		# every row since the lease was built and reachable by no question, so *what has been
		# held since before lunch* — the one a person asks when an agent has gone quiet — had
		# no query. Null unless it is claimed, and NULLS LAST does the rest.
		claimed_at=subroutine.db.models.work.Task.claimed_at,
	),
	# **Filterable and not orderable, and now it says why** — `#1803`. Both were simply in one
	# list and not the other; neither absence had been argued, and `#1805` is where they are
	# decided rather than inherited.
	# **Both orderable since `#1805`**, which is that item's whole thesis: a field filterable
	# and plausibly orderable *is* orderable, without a second list being edited. They were in
	# one list and not the other and neither absence had ever been argued.
	#
	# **`snoozed_until` does not duplicate `ordering.DEFERRED`.** That is a *band* — startable
	# against put-off — added per request because it is a fact about an instant rather than
	# about a column. This is the date itself, so ascending with NULLS LAST is *coming back
	# soonest first*, which is a question the band cannot answer at all.
	#
	# **And `content_updated_at` does not duplicate `updated_at`**: one is *when did the prose
	# change* and the other *when did anything about this move*. `#815` made that distinction
	# worth a filter; it is worth a sort for the same reason.
	"snoozed_until": Property(
		column=subroutine.db.models.work.Task.snoozed_until, kind=INSTANT, orderable=True
	),
	"content_updated_at": Property(
		column=subroutine.db.models.work.Task.content_updated_at, kind=INSTANT, orderable=True
	),
	# `#319`. **No index, and here anyway on the same measured grounds as `completed_at` and
	# `snoozed_until` above**: the question it was filed for — *what is short and not blocked* —
	# needs it, and the largest workspace on this instance holds 163 open tasks. The comment at
	# the head of this registry is the promise being weighed, and this entry is a place to look
	# when it stops being true.
	"estimate_minutes": Property(
		column=subroutine.db.models.work.Task.estimate_minutes, kind=DURATION, orderable=True
	),
	**_worked_on(subroutine.db.models.work.Task.id),
	**_ORDER_ONLY,
	**_AXES_ONLY,
	**_CONDITION_ONLY,
}

#: What a document listing can be asked about.
#:
#: **Shorter for §6.14's reason** — a document is not scheduled, so it has no deadline and no
#: planned day to ask about. It is here at all because one ref counter serves both (§6.2), so
#: *"what was created yesterday"* answered for tasks alone would be wrong about half of what a
#: number can name.
DOCUMENT_PROPERTIES: dict[str, Property] = {
	**_instants(
		created_at=subroutine.db.models.work.Document.created_at,
		updated_at=subroutine.db.models.work.Document.updated_at,
	),
	# **Orderable since `#1805`**, and on a document the distinction is sharper still: a
	# document is *read* for its prose, so *what changed recently* is a question about the body
	# rather than about the row.
	"content_updated_at": Property(
		column=subroutine.db.models.work.Document.content_updated_at,
		kind=INSTANT,
		orderable=True,
	),
	# **A document is worked on too**, and a comment on one moves nothing in its row — which is
	# the whole reason this is an `EXISTS`. `#815`'s question is about items, and a ref names
	# either kind (§6.2).
	**_worked_on(subroutine.db.models.work.Document.id),
	# **A document is grouped on the same axis and its keys are its own** (`#1790`). Four
	# categories a *document* has, which are not a task's four — `db.mixins` keeps them apart
	# and this is where the two registries stop agreeing by accident.
	STATUS_CATEGORY: Property(
		groupable=subroutine.db.mixins.DOCUMENT_STATUS_CATEGORIES,
		because="a flat route parameter today rather than a dotted filter — `#1804`.",
	),
	"title": Property(
		column=subroutine.db.models.work.Document.title,
		orderable=True,
		because=(
			"`q` already matches the title, and `title:foo` is a filter wearing search syntax "
			"— `#1801` §8, and the grammar gives it for nothing once `#1806` lands."
		),
	),
	"ref": Property(
		column=subroutine.db.models.work.Document.ref,
		orderable=True,
		because=(
			"a ref names exactly one item and `show` is how you ask for it; a *set* of refs "
			"would be `in` on a lookup, and nothing has asked for one."
		),
	),
}

#: What a project listing can be asked about.
PROJECT_PROPERTIES: dict[str, Property] = {
	**_instants(
		created_at=subroutine.db.models.project.Project.created_at,
		updated_at=subroutine.db.models.project.Project.updated_at,
	),
	# **Three orderable and unaskable, and the reason is one sentence for all three**: a
	# project is *found* by its address rather than narrowed to by its name, and the parameter
	# that does that — `parent` — is a flat one. `#1804` is where a REFERENCE kind would make
	# them askable if anybody wanted it; nobody has.
	**{
		name: Property(
			column=column,
			orderable=True,
			because=(
				"a project is reached by its address rather than narrowed to by its name — "
				"`parent` is the flat parameter that does it, and `#1804` is where a "
				"REFERENCE kind would change that."
			),
		)
		for name, column in {
			"key": subroutine.db.models.project.Project.key,
			"title": subroutine.db.models.project.Project.title,
			"path": subroutine.db.models.project.Project.path,
		}.items()
	},
}

#: What the change feed and the journal can be asked about — `#1431`, decision `#1429`.
#:
#: **One field, and it is the only one an event has of its own.** Everything else a reader
#: wants to narrow by — which project, which item — is a property of the thing the event is
#: *about*, and reaching it means a join this registry has no way to express. That is filed
#: separately rather than bent into a `Filterable`.
#:
#: **The index this registry's head demands already exists.** `ix_event_workspace_id_created_at`
#: was added by `#815` for `touched_at`, whose `EXISTS` asks this table the same question from
#: the other side — so a date range over the feed reaches a real index on the day it ships,
#: which is rarer here than it should be.
#:
#: **`seq` is deliberately not filterable.** `?since=` already takes one and means something
#: stronger: it is a *resumable cursor* with inclusive-with-dedupe semantics (§5.11), where a
#: filter would be an ordinary comparison. Two spellings of one number, one of which quietly
#: loses the resume guarantee, is the shape `#1017` warns about.
EVENT_PROPERTIES: dict[str, Property] = {
	"created_at": Property(
		column=subroutine.db.models.activity.Event.created_at,
		kind=INSTANT,
		because=(
			"a feed always runs forwards and the caller does not choose — `domain.events.feed`. "
			"`newest` picks which end to start from and is a flat parameter, so this listing "
			"offers no ordering at all rather than one that would contradict the cursor."
		),
	),
}

#: Every registry, by the entity name a refusal uses. Named here so `/v1/meta` publishes them
#: from the same place the listings read them, rather than from a second list that agrees today.
PROPERTIES: dict[str, dict[str, Property]] = {
	"task": TASK_PROPERTIES,
	"document": DOCUMENT_PROPERTIES,
	"project": PROJECT_PROPERTIES,
	"event": EVENT_PROPERTIES,
}


def filters (entity: str) -> dict[str, Filterable]:
	"""Return what this entity's listing can be *asked about*, from the one declaration.

	**Derived rather than declared**, which is the whole of `#1803`: the filterable, orderable
	and groupable sets were three lists in three modules with no field in all three, and a
	guard comparing them could only ever have reported the disagreement after it happened.

	A property with no :attr:`Property.kind` is not filterable and is simply absent — so the
	dict this returns is exactly what it always was, and every caller of it is untouched.
	"""

	found = {}

	for name, held in PROPERTIES.get(entity, {}).items():
		if held.kind is None:
			continue

		found[name] = Filterable(
			column=held.column,
			kind=held.kind,
			group=held.group,
			operators=_allowed(held.kind, held.column),
		)

	return found


def _allowed (kind: Kind, column: typing.Any) -> frozenset[str]:
	"""Return the operators one property really takes: its kind's, minus what its column cannot.

	**Only :data:`IS` is narrowed, and only by nullability.** A kind's other operators are about
	the *sort* of value and are true wherever that sort is; whether a field can be *unset* is a
	fact about the column, and a ``NOT NULL`` one answers `is=set` with every row and `is=unset`
	with none.

	**Given the kind and the column rather than the property**, because a property's kind is
	optional — *not filterable* is a state it has to describe — and this is only ever called
	where one has been established. mypy said so.

	**A property with no column of its own keeps whatever its kind allows.** ``touched_by``
	compiles into a correlated ``EXISTS`` and its column is the entity's identity, so asking
	this about nullability would answer about the wrong thing — :data:`WHO` refuses ``is``
	itself, which is where that decision belongs.
	"""

	nullable = getattr(column, "nullable", None)

	if IS in kind.operators and nullable is False:
		return kind.operators - {IS}

	return kind.operators


def orderable (entity: str) -> dict[str, typing.Any]:
	"""Return what this entity's listing can be *ordered by*, as a column each.

	**Columns only.** A property whose ordering expression belongs to another module —
	``priority_score``, which :mod:`subroutine.domain.ordering` bands — declares the capability
	here and is added there, because this module cannot build one and importing the module that
	can would be the cycle :class:`Property` records.
	"""

	return {
		name: held.column
		for name, held in PROPERTIES.get(entity, {}).items()
		if held.orderable and held.column is not None
	}


def axes (entity: str) -> dict[str, tuple[str, ...]]:
	"""Return what this entity's listing can be *grouped by*, with each axis's keys."""

	return {
		name: held.groupable
		for name, held in PROPERTIES.get(entity, {}).items()
		if held.groupable is not None
	}


#: What a task listing can be asked about — derived, and unchanged in shape or name.
TASK_FILTERS: dict[str, Filterable] = filters("task")

#: What a document listing can be asked about.
DOCUMENT_FILTERS: dict[str, Filterable] = filters("document")

#: **A project's and an event's have no name of their own**, and `#202`'s guard is what decided
#: that: derived beside these two they were declared and read by nothing, where the pair above
#: are read by the agent surface and the terminal. `filters("project")` is how to ask.

#: Every filter registry, by the entity name a refusal uses.
FILTERS: dict[str, dict[str, Filterable]] = {
	entity: filters(entity) for entity in PROPERTIES
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
		for operator in field.operators
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
	workspace: subroutine.db.models.identity.Workspace | None,
) -> str:
	"""Return the zone a listing's dates are read in: §6.5's chain, assembled once.

	**One function because there are five callers and being wrong is invisible.** A day read
	in the wrong zone is right in winter and wrong in summer (`#773`), and the HTTP listing,
	the local client's tasks and its documents would otherwise each assemble this — which is
	this codebase's signature defect on the one rule with no visible symptom.

	**``None`` is for a feed, and it is a step omitted rather than a step guessed at** (`#1431`).
	A listing is always inside one workspace; `/v1/changes` deliberately answers across every
	workspace a caller can read, so there is no workspace whose zone would be the right one —
	and taking whichever happened to be in hand would read *yesterday* in a colleague's zone
	depending on which workspace sorted first. The chain is then user to instance, which is
	`#1091`'s reasoning for `views.reader_zone` reached through the function that already owns
	the chain rather than by a second assembly of it.
	"""

	return subroutine.domain.schedule.zone_for(
		user=actor.user,
		workspace=workspace,
		instance=subroutine.domain.instances.get(session),
	)


def refuse_names_that_are_not_filters (given: typing.Iterable[str]) -> None:
	"""Refuse a name that could not be a filter for anything — `SR#1626`.

	**For a caller that owns its whole namespace**, which :func:`understood` deliberately does
	not. Over HTTP the flat names belong to the endpoint — ``status``, ``limit``, ``project``
	are real query parameters — so ``understood`` skips a name with no separator and lets
	``api.query.refuse_unknown`` answer for it. That division is correct there and is the whole
	of why this function exists somewhere else: a surface whose ``filter`` argument is *only*
	ever filters has no second owner, so a name that reaches it and is not a filter is nobody's
	and was being dropped in silence.

	**The wrong answer was a superset**, which is what made it survive. An agent asking for
	``{"status": "needs_input"}`` got every row back and no indication that its question had
	been ignored — measured on this instance, fifteen rows where three were true.

	**Shape only, because a parser does not know the entity.** ``created_at.gte`` is filter-
	shaped everywhere; whether *this* listing has a ``created_at`` is :func:`understood`'s
	question, and it answers it by name with the vocabulary. So the two refusals are different
	sentences about different mistakes, and neither is a copy of the other's register.

	**Aliases are taken across every entity**, which is deliberately looser than it could be.
	``due_before`` is a filter on a task and nothing on a document, and refusing it here would
	be this function guessing at an entity it was not given — where letting it through means
	``understood`` names it, for the right listing, with the fields that listing does have.
	"""

	flat = {
		name
		for names in ALIASES.values()
		for name in names
	}
	stray = sorted(
		name for name in given if SEPARATOR not in name and name not in flat
	)

	if not stray:
		return

	named = ", ".join(repr(name) for name in stray)

	raise subroutine.errors.ValidationError(
		f"{named} is not a filter." if len(stray) == 1 else f"{named} are not filters.",
		errors=[
			subroutine.errors.FieldError(
				field="filter",
				code="invalid_field_value",
				message=(
					f"A filter is written field.operator, and {named} has no operator."
					if len(stray) == 1
					else f"A filter is written field.operator, and {named} have none."
				),
				hint="Write it as field.operator=value, like created_at.gte=yesterday.",
			)
		],
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

	# **Before a client is chosen, which is what makes both transports agree** (`SR#1626`).
	# The terminal's ``--filter`` is only ever filters, so a flat name here is nobody's — and
	# the local client and the HTTP client would otherwise drop it in two different places for
	# two different reasons.
	refuse_names_that_are_not_filters(found)

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
	list of the other's. A misspelled field is refused *here*, by name, with the vocabulary.

	**A name with no separator is skipped, and that is only safe where somebody else owns it**
	(`SR#1626`). This used to say *"so nothing is quietly ignored, which is the property that
	module exists for"* — a claim about the whole program made from inside the one caller where
	it happens to hold. Over HTTP it does: ``status`` and ``limit`` are real query parameters
	and the neighbour above refuses the ones nobody declared. Everywhere else the flat names
	are nobody's, and skipping them silently widened the answer instead of refusing it.

	So the rule is now stated where it can be kept: a caller whose namespace is *only* filters
	calls :func:`refuse_names_that_are_not_filters` first — the terminal through :func:`parsed`,
	and the agent surface through ``mcp.tools._filters``. This function keeps the skip, because
	the mixed namespace it was written for still needs it.

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

		if operator not in EVERY_OPERATOR:
			raise _no_such_operator(name, field, operator)

		if operator not in found.operators:
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

	**:data:`IS` is compiled here rather than by a kind** — `#1804`. It asks about the *column*
	and never about the field's own sort of value, so every kind that has a column answers it
	the same way and giving each one a branch would be the same rule written four times. Which
	kinds allow it at all is still theirs to say: :data:`WHO` refuses it, because a field
	compiled as a correlated ``EXISTS`` has no column to be null.
	"""

	alone = []
	grouped: dict[str, list[Comparison]] = {}

	for comparison in comparisons:
		# **:data:`IS` is decided before the group, and that order is load-bearing** —
		# `#1804`. A reference names a group so that resolving a *name* can reach the session;
		# ``is`` resolves nothing, and routing it there sent ``assignee.is=unset`` to
		# `selection.user`, which answered **404: there is no account called 'unset'**. Caught
		# by the guard that drives every published combination, which is the second defect it
		# has found in this item.
		if comparison.operator == IS:
			alone.append(
				_condition_predicate(
					comparison.against.column,
					comparison.operator,
					comparison.value,
					comparison.reported,
					where.now,
					where.timezone,
				)
			)

			continue

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


def _values (comparison: Comparison) -> list[str]:
	"""Split what a comparison names into the one or several values it stands for.

	``eq`` and ``is`` name one; :data:`IN` names several, separated by
	:data:`IN_SEPARATOR`. **Empty parts are refused rather than dropped** — ``tag.in=ops,``
	is a caller who meant something, and silently answering about *ops* alone is the
	drop-what-you-do-not-understand defect `#1626` was filed for.
	"""

	if comparison.operator != IN:
		return [comparison.value]

	given = [part.strip() for part in comparison.value.split(IN_SEPARATOR)]

	if not all(given):
		raise subroutine.errors.ValidationError(
			f"{comparison.value!r} has an empty entry in it.",
			errors=[
				subroutine.errors.FieldError(
					field=comparison.field,
					code="invalid_field_value",
					message=(
						f"{comparison.reported} lists its values separated by "
						f"{IN_SEPARATOR!r} and one of them is empty."
					),
					hint="Write them as 'ops,web' — no trailing separator.",
				)
			],
		)

	return given


def _held_by (comparisons: list[Comparison], where: Where) -> typing.Any:
	"""Compile *whose is this* — ``assignee`` and ``claimed_by`` — `#1804`.

	**One username resolved to one account, by the same function the flat parameter uses.**
	`selection.user` takes a username *or* an id, understands ``me``, and refuses by name — so
	the dotted spelling accepts exactly what ``?assignee=si`` has always accepted rather than
	being a second, narrower door onto the same column.

	**Each comparison is its own clause, ANDed with the rest.** Two entries about one field is
	a caller asking for both at once and getting nothing, which is what a conjunction means and
	is `#1801` §9's stated shape for the query string.
	"""

	if where.session is None:
		raise AssertionError("a reference needs a session to resolve a name")

	narrowing = []

	for comparison in comparisons:
		column = comparison.against.column
		found = [
			subroutine.domain.selection.user(
				where.session, value, caller=where.caller
			).id
			for value in _values(comparison)
		]

		narrowing.append(
			column.in_(found) if comparison.operator == IN else column == found[0]
		)

	return sqlalchemy.and_(*narrowing)


def _tagged (comparisons: list[Comparison], where: Where) -> typing.Any:
	"""Compile ``tag`` — `#1804`, on the read side `#1319` built.

	**Through :func:`subroutine.domain.tags.carrying`, which the flat parameter already uses**,
	so a tag nobody has applied is refused *by name* rather than answered with an empty listing.
	A tag spelled wrongly and a tag nobody uses produce the same empty page and the second is
	far commoner.

	**A subquery per value rather than a join** — that function's own rule, and it is why the
	row count cannot change: an item carries a tag once, but a join in a listing multiplies its
	rows by however many matched.

	**`in` is any of these, so the clauses are ORed and `eq` is the single case.** Two separate
	comparisons about ``tag`` are still ANDed, which is how a caller asks for *both* — the
	question `#1801` §5 records as unasked under a name of its own.
	"""

	if where.session is None:
		raise AssertionError("a tag needs a session to resolve a name")

	if len(where.workspace_ids) != 1:
		raise subroutine.errors.ValidationError(
			"A tag can only be asked about inside one workspace.",
			errors=[
				subroutine.errors.FieldError(
					field="tag",
					code="invalid_field_value",
					message="Tags are a workspace's own vocabulary, and this reads several.",
					hint="Ask one workspace at a time — 'workspace_id' narrows a listing.",
				)
			],
		)

	identity = typing.cast(typing.Any, comparisons[0].against.column)
	joined = subroutine.domain.tags.JOINS[identity.parent.class_]
	narrowing = []

	for comparison in comparisons:
		# **One `IN` over every value rather than an `OR` of subqueries.** `carrying` returns a
		# `SELECT` of the items carrying one tag, and `or_` coerces a bare `SELECT` to a
		# *scalar* subquery — which SQLAlchemy warns about and the suite turns into a 500. So
		# each is put behind `IN` first, which is what the flat parameter has always done.
		narrowing.append(
			sqlalchemy.or_(
				*[
					identity.in_(
						subroutine.domain.tags.carrying(
							where.session,
							where.workspace_ids[0],
							value,
							joined=joined.rows,
							holder=joined.owner,
						)
					)
					for value in _values(comparison)
				]
			)
		)

	return sqlalchemy.and_(*narrowing)


#: Which fields compile through a function that needs more than the value they carry — and,
#: where several name one group, together.
#:
#: **The name says *together* and the mechanism is wider than that** (`#1804`). ``touched_at``
#: and ``touched_by`` really do compile as one predicate, which is what this was built for; a
#: reference compiles alone and is here because resolving a name needs the session. Both are
#: *a field whose predicate cannot be built from its value and a clock*, which is the property
#: :class:`Kind`'s own signature cannot express.
GROUPS: dict[str, typing.Callable[[list[Comparison], Where], typing.Any]] = {
	"touched": _touched,
	WHO_HOLDS_IT: _held_by,
	TAGGED: _tagged,
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
				hint=f"The operators are: {', '.join(sorted(EVERY_OPERATOR))}.",
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
